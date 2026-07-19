"""Propose memory.md / user.md entries from session insights.

The post-archive insights pipeline (``ciao/insights.py``) appends a
``## Session insights`` section to each archived chat. That section already
contains the high-signal facts we'd want in memory — errors, decisions, new
entities, user corrections, reusable snippets.

Rather than ask a second model to redo the work, this module does a cheap
heuristic pass over the existing section:

* "User corrections" + "Decisions" feed memory.md (durable behavioral facts).
* "New entities" with type=person tagged as User feed user.md.
* Other facts go to memory.md if they look durable (no dates, no per-turn
  noise).

The output is written as a Markdown bullet list to
``memory-vault/personal/Workspace/Memory-Proposals.md``. A human (or the agent on the
next session, via the ``memory`` tool) reviews and promotes them. Auto-apply
is intentionally NOT the default — the agent layer is the right place to make
the consolidation call.

One exception: "User corrections" are rare, inherently durable, and
highest-signal, so :func:`promote_user_corrections` applies them straight to
bounded memory at archive time (when the caller opts in via
``auto_promote_memory``). The write goes through the same validated
``memory_tool.add_entry`` path the CLI uses: prompt-injection patterns are
rejected, exact duplicates are dropped, and when the file is full the entry
falls back to the proposals file for the daily curator to consolidate.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


_PROPOSALS_PATH = "personal/Workspace/Memory-Proposals.md"

# Insight sections safe to apply to bounded memory without review: rare,
# behavioral, and durable by construction. Everything else stays in the
# proposals file for the daily curator.
_AUTO_PROMOTE_SECTIONS = ("User corrections",)

# Section headers used by ``_INSIGHTS_SYSTEM_PROMPT`` in ``ciao/insights.py``.
# Two of them are "behavioral" (durable preferences, decisions the user made),
# one is "identity" (new people/projects), and the rest are session-specific
# noise we ignore for memory purposes.
_BEHAVIORAL_SECTIONS = ("User corrections", "Decisions")
_IDENTITY_SECTIONS = ("New entities",)


@dataclass(slots=True, frozen=True)
class MemoryProposal:
    """One proposed memory entry with its routing target."""

    target: str  # "memory" or "user"
    text: str
    source_section: str

    def as_bullet(self) -> str:
        return f"- [{self.target}] {self.text}  _(from: {self.source_section})_"


# ── Parsing ───────────────────────────────────────────────────────────────


def _split_sections(insights_md: str) -> dict[str, list[str]]:
    """Group bullet lines by their ``## Heading``.

    Strips bullet markers and citation tags (``[idx=12]``) so the proposal is
    one clean sentence. Empty sections are dropped.
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in insights_md.splitlines():
        line = raw_line.rstrip()
        heading_match = re.match(r"^##+\s+(.+?)\s*$", line)
        if heading_match:
            current = heading_match.group(1).strip()
            sections.setdefault(current, [])
            continue
        if current is None:
            continue
        bullet = re.match(r"^\s*[-*]\s+(.+)$", line)
        if not bullet:
            continue
        text = bullet.group(1).strip()
        # Drop trailing ``[idx=12]`` citations; they only make sense in the
        # archive context.
        text = re.sub(r"\s*\[idx=\d+\]\s*$", "", text).strip()
        if text:
            sections[current].append(text)
    return sections


def _is_durable(text: str) -> bool:
    """Reject obvious per-session noise before proposing.

    The insights extractor already cites facts with ``[idx=N]`` — those
    citations are stripped earlier. What remains as noise: throwaway debug
    notes, "tried X then Y" dead ends, anything starting with a verb like
    "tried" or "asked".
    """
    lowered = text.lower()
    if any(lowered.startswith(p) for p in ("tried ", "asked ", "ran ")):
        return False
    if len(text) < 12 or len(text) > 400:
        return False
    return True


# ── Proposal generation ───────────────────────────────────────────────────


def propose_from_insights(insights_md: str) -> list[MemoryProposal]:
    """Scan an insights markdown blob and emit memory proposals."""
    if not insights_md.strip():
        return []

    sections = _split_sections(insights_md)
    proposals: list[MemoryProposal] = []

    for heading in _BEHAVIORAL_SECTIONS:
        for item in sections.get(heading, []):
            if _is_durable(item):
                proposals.append(MemoryProposal(
                    target="memory",
                    text=item,
                    source_section=heading,
                ))

    for heading in _IDENTITY_SECTIONS:
        for item in sections.get(heading, []):
            if not _is_durable(item):
                continue
            # New entities lead with "person:" or "project:". Person entries
            # about the user himself feed user.md; everything else feeds
            # memory.md so the next session knows the new entity exists.
            if re.match(r"^person\s*:\s*(operator|user)", item, re.I):
                target = "user"
            else:
                target = "memory"
            proposals.append(MemoryProposal(
                target=target,
                text=item,
                source_section=heading,
            ))

    return proposals


# ── Auto-promotion ────────────────────────────────────────────────────────


def promote_user_corrections(
    proposals: list[MemoryProposal],
    *,
    memory_dir: Path | None = None,
) -> tuple[list[MemoryProposal], list[str]]:
    """Apply "User corrections" proposals straight to bounded memory.

    Returns ``(remaining, promoted_texts)``: ``remaining`` keeps every
    proposal that was not promoted (other sections untouched; corrections
    that failed the add because the file is full or the entry was rejected),
    ``promoted_texts`` lists what was written. Exact duplicates are dropped
    from both — they are already remembered, so there is nothing to review.
    """
    from ciao.memory_tool import (
        DEFAULT_MEMORY_CHAR_LIMIT,
        DEFAULT_USER_CHAR_LIMIT,
        add_entry,
        path_for_target,
    )

    remaining: list[MemoryProposal] = []
    promoted: list[str] = []
    for proposal in proposals:
        if proposal.source_section not in _AUTO_PROMOTE_SECTIONS:
            remaining.append(proposal)
            continue
        if proposal.target == "user":
            limit = int(os.environ.get("CIAO_USER_CHAR_LIMIT", DEFAULT_USER_CHAR_LIMIT))
        else:
            limit = int(os.environ.get("CIAO_MEMORY_CHAR_LIMIT", DEFAULT_MEMORY_CHAR_LIMIT))
        path = path_for_target(proposal.target, memory_dir)  # type: ignore[arg-type]
        result = add_entry(path, proposal.text, char_limit=limit)
        if result.get("ok"):
            promoted.append(proposal.text)
        elif "duplicate" in str(result.get("error", "")):
            logger.info("memory promote: dropped exact duplicate %r", proposal.text[:80])
        else:
            # Full file or rejected entry: keep it reviewable instead of
            # silently losing it.
            logger.info(
                "memory promote: falling back to proposals (%s)",
                result.get("error", "unknown error"),
            )
            remaining.append(proposal)
    return remaining, promoted


# ── Persistence ───────────────────────────────────────────────────────────


def _extract_workspace_context(archive_path: Path, vault_root: Path) -> str:
    """Read the archive file and extract the context (workspace) from YAML frontmatter."""
    try:
        text = archive_path.read_text(encoding="utf-8")
        if text.startswith("---"):
            end_fm = text.find("---", 3)
            if end_fm > 0:
                fm_text = text[3:end_fm]
                for line in fm_text.splitlines():
                    parts = line.split(":", 1)
                    if len(parts) == 2 and parts[0].strip() == "context":
                        context_val = parts[1].strip().strip("'\"")
                        if context_val and (context_val in {"personal", "work"} or (vault_root / context_val).is_dir()):
                            return context_val
    except Exception:  # noqa: BLE001
        pass
    return "personal"


def append_proposals(
    proposals: list[MemoryProposal],
    vault_root: Path,
    *,
    source_path: Path | None = None,
) -> Path | None:
    """Append a timestamped batch to ``Workspace/Memory-Proposals.md``.

    Proposals whose text is already recorded in the file are dropped so
    recurring corrections don't stack up batch after batch. Returns the
    proposals file path when a batch was written, or None when the list is
    empty or every proposal was already present.
    """
    if not proposals:
        return None

    workspace = "personal"
    if source_path is not None:
        workspace = _extract_workspace_context(source_path, vault_root)

    out_path = vault_root / workspace / "Workspace/Memory-Proposals.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # File is append-only; new batches stack at the end with their own
    # timestamp so it's easy to see what came from which curation run.
    existing = out_path.read_text(encoding="utf-8") if out_path.exists() else _STUB_HEADER

    # Drop anything already sitting in the file. Corrections recur across
    # sessions ("Continue from where you left off.") and, while bounded memory
    # is full, keep failing promotion and landing here — without this guard the
    # same bullet stacks up every archive, growing the file unboundedly.
    already = _existing_proposal_texts(existing)
    fresh = [p for p in proposals if p.text.strip() not in already]
    if not fresh:
        return None

    header = _proposals_header_block(source_path)
    lines = [p.as_bullet() for p in fresh]
    block = header + "\n".join(lines) + "\n"

    out_path.write_text(existing + "\n" + block, encoding="utf-8")
    return out_path


# Matches a bullet written by ``MemoryProposal.as_bullet`` and captures its
# text payload (between the ``[target]`` tag and the trailing ``_(from: …)_``).
_BULLET_RE = re.compile(r"^- \[[^\]]+\] (.+?)  _\(from: [^)]*\)_\s*$")


def _existing_proposal_texts(file_text: str) -> set[str]:
    """Return the set of proposal texts already recorded in the file."""
    out: set[str] = set()
    for line in file_text.splitlines():
        m = _BULLET_RE.match(line)
        if m:
            out.add(m.group(1).strip())
    return out


_STUB_HEADER = (
    "---\n"
    "tags: [ciao, memory, proposals]\n"
    "---\n"
    "# Memory Proposals\n\n"
    "Auto-generated proposals from session-insights curation. Each batch is "
    "timestamped. Review and promote durable facts to `~/.ciao/memory.md` or "
    "`~/.ciao/user.md` via the `memory` MCP tool.\n"
)


def _proposals_header_block(source_path: Path | None) -> str:
    ts = datetime.now(UTC).isoformat(timespec="seconds")
    if source_path is not None:
        return f"\n## {ts} — from `{source_path.name}`\n\n"
    return f"\n## {ts}\n\n"


# ── Pipeline entry point ──────────────────────────────────────────────────


def proposals_from_archive(
    archive_path: Path,
    vault_root: Path,
    *,
    auto_promote_memory: bool = False,
    memory_dir: Path | None = None,
) -> Path | None:
    """Read an archived chat, extract its ``## Session insights`` body, propose, write.

    With ``auto_promote_memory`` set (callers gate it on the config's
    ``memory_enabled``), "User corrections" are applied straight to bounded
    memory via :func:`promote_user_corrections`; everything else — plus any
    correction that could not be added — lands in the proposals file.

    Returns the proposals file path when something was written, else None.
    Swallows all exceptions; this runs as a fire-and-forget step.
    """
    try:
        if not archive_path.exists():
            return None
        text = archive_path.read_text(encoding="utf-8")
        body = _extract_insights_section(text)
        if not body:
            return None
        proposals = propose_from_insights(body)
        if auto_promote_memory and proposals:
            proposals, promoted = promote_user_corrections(
                proposals, memory_dir=memory_dir
            )
            if promoted:
                logger.info(
                    "memory proposals: auto-promoted %d user correction(s) from %s",
                    len(promoted),
                    archive_path.name,
                )
        return append_proposals(proposals, vault_root, source_path=archive_path)
    except Exception:  # noqa: BLE001 — never crash the pipeline
        logger.exception("memory proposals failed for %s", archive_path)
        return None


def _extract_insights_section(archive_md: str) -> str:
    """Return the body under the first ``## Session insights`` header, or ''."""
    marker = "## Session insights"
    idx = archive_md.find(marker)
    if idx < 0:
        return ""
    return archive_md[idx + len(marker):].strip()
