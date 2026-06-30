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
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


_PROPOSALS_PATH = "personal/Workspace/Memory-Proposals.md"

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
            if re.match(r"^person\s*:\s*(raffa|user|raffaele)", item, re.I):
                target = "user"
            else:
                target = "memory"
            proposals.append(MemoryProposal(
                target=target,
                text=item,
                source_section=heading,
            ))

    return proposals


# ── Persistence ───────────────────────────────────────────────────────────


def append_proposals(
    proposals: list[MemoryProposal],
    vault_root: Path,
    *,
    source_path: Path | None = None,
) -> Path | None:
    """Append a timestamped batch to ``Workspace/Memory-Proposals.md``.

    Returns the proposals file path on success, or None when the proposal
    list is empty.
    """
    if not proposals:
        return None
    out_path = vault_root / _PROPOSALS_PATH
    out_path.parent.mkdir(parents=True, exist_ok=True)

    header = _proposals_header_block(source_path)
    lines = [p.as_bullet() for p in proposals]
    block = header + "\n".join(lines) + "\n"

    # File is append-only; new batches stack at the end with their own
    # timestamp so it's easy to see what came from which curation run.
    existing = out_path.read_text(encoding="utf-8") if out_path.exists() else _STUB_HEADER
    out_path.write_text(existing + "\n" + block, encoding="utf-8")
    return out_path


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
    archive_path: Path, vault_root: Path
) -> Path | None:
    """Read an archived chat, extract its ``## Session insights`` body, propose, write.

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
