"""Propose CLAUDE.md region entries from session insights.

The post-archive insights pipeline (``ciao/insights.py``) appends a
``## Session insights`` section to each archived chat. That section already
contains the high-signal facts we'd want in memory — errors, decisions, new
entities, user corrections, reusable snippets.

Rather than ask a second model to redo the work, this module does a cheap
heuristic pass over the existing section:

* "User corrections" + "Decisions" feed the ``ciao:memory`` region.
* "New entities" with type=person tagged as User feed ``ciao:profile``
  (and durable identity facts also belong on ``People/User.md``).
* Other facts go to ``ciao:memory`` if they look durable.

The output is written as a Markdown bullet list to the archive owner's
``<workspace-vault>/Workspace/Memory-Proposals.md``. A human (or the agent
via Edit on the CLAUDE.md regions, then ``memory_proposal_resolve`` to
dismiss) reviews and promotes them. Auto-apply is intentionally NOT the
default — the agent layer is the right place to make the consolidation call.

One exception: "User corrections" are rare, inherently durable, and
highest-signal, so :func:`promote_user_corrections` appends them straight to
the matching CLAUDE.md region at archive time (when the caller opts in via
``auto_promote_memory``). Exact duplicates are dropped; on any write failure
the entry falls back to the proposals file for the daily curator.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


_PROPOSALS_RELATIVE = "Workspace/Memory-Proposals.md"


# Insight sections safe to apply to bounded memory without review: rare,
# behavioral, and durable by construction. Everything else stays in the
# proposals file for the daily curator.
_AUTO_PROMOTE_SECTIONS = ("User corrections",)

# Section headers used by ``_INSIGHTS_SYSTEM_PROMPT`` in ``ciao/insights.py``.
_BEHAVIORAL_SECTIONS = ("User corrections", "Decisions")
_IDENTITY_SECTIONS = ("New entities",)


@dataclass(slots=True, frozen=True)
class MemoryProposal:
    """One proposed memory entry with its routing target."""

    target: str  # "memory" or "profile" (legacy "user" normalized on write)
    text: str
    source_section: str

    def as_bullet(self) -> str:
        # Deliberately total: an unknown target is written through rather than
        # raising, so one odd proposal cannot fail a whole archive batch.
        target = "profile" if self.target == "user" else self.target
        return f"- [{target}] {self.text}  _(from: {self.source_section})_"


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
        text = re.sub(r"\s*\[idx=\d+\]\s*$", "", text).strip()
        if text:
            sections[current].append(text)
    return sections


def _is_durable(text: str) -> bool:
    """Reject obvious per-session noise before proposing."""
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
            # Person entries about the operator feed the profile region;
            # durable identity also belongs on People/User.md (curator routes).
            if re.match(r"^person\s*:\s*(operator|user)", item, re.I):
                target = "profile"
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
    guide_path: Path | None = None,
) -> tuple[list[MemoryProposal], list[str]]:
    """Apply "User corrections" proposals straight to CLAUDE.md regions.

    Returns ``(remaining, promoted_texts)``. Exact duplicates are dropped
    from both. On any write failure the proposal stays reviewable.
    """
    from ciao.memory_tool import ensure_regions, read_region, resolve_region, write_region

    remaining: list[MemoryProposal] = []
    promoted: list[str] = []
    if guide_path is None:
        return list(proposals), promoted

    try:
        ensure_regions(guide_path)
    except Exception:  # noqa: BLE001
        logger.exception("memory promote: ensure_regions failed for %s", guide_path)
        return list(proposals), promoted

    for proposal in proposals:
        if proposal.source_section not in _AUTO_PROMOTE_SECTIONS:
            remaining.append(proposal)
            continue
        try:
            region = resolve_region(proposal.target)
            entries, diags = read_region(guide_path, region)
            if diags:
                logger.info(
                    "memory promote: falling back to proposals (%s)",
                    "; ".join(d.message for d in diags),
                )
                remaining.append(proposal)
                continue
            if proposal.text in entries:
                logger.info(
                    "memory promote: dropped exact duplicate %r",
                    proposal.text[:80],
                )
                continue
            write_region(guide_path, region, entries + [proposal.text])
            promoted.append(proposal.text)
        except Exception as exc:  # noqa: BLE001
            logger.info("memory promote: falling back to proposals (%s)", exc)
            remaining.append(proposal)
    return remaining, promoted


# ── Persistence ───────────────────────────────────────────────────────────


def append_proposals(
    proposals: list[MemoryProposal],
    workspace_vault_root: Path,
    *,
    source_path: Path | None = None,
) -> Path | None:
    """Append a timestamped batch to ``Workspace/Memory-Proposals.md``."""
    if not proposals:
        return None

    out_path = workspace_vault_root / _PROPOSALS_RELATIVE
    out_path.parent.mkdir(parents=True, exist_ok=True)

    existing = out_path.read_text(encoding="utf-8") if out_path.exists() else _STUB_HEADER

    already = _existing_proposal_texts(existing)
    fresh = [p for p in proposals if p.text.strip() not in already]
    if not fresh:
        return None

    header = _proposals_header_block(source_path)
    lines = [p.as_bullet() for p in fresh]
    block = header + "\n".join(lines) + "\n"

    out_path.write_text(existing + "\n" + block, encoding="utf-8")
    return out_path


# Matches a bullet written by ``MemoryProposal.as_bullet``.
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
    "timestamped. Review and promote durable cross-session facts into the "
    "`ciao:memory` / `ciao:profile` regions of the workspace `CLAUDE.md` with "
    "Edit (edit the region first), then dismiss the proposal with "
    "`memory_proposal_resolve`. Identity facts about the operator also belong "
    "on `People/User.md`. Standing directives stay in the CLAUDE.md body "
    "outside the fenced regions.\n"
)


def _proposals_header_block(source_path: Path | None) -> str:
    ts = datetime.now(UTC).isoformat(timespec="seconds")
    if source_path is not None:
        return f"\n## {ts} — from `{source_path.name}`\n\n"
    return f"\n## {ts}\n\n"


# ── Pipeline entry point ──────────────────────────────────────────────────


def proposals_from_archive(
    archive_path: Path,
    workspace_vault_root: Path,
    *,
    auto_promote_memory: bool = False,
    guide_path: Path | None = None,
) -> Path | None:
    """Read an archived chat, extract insights, propose, optionally promote.

    With ``auto_promote_memory`` set, "User corrections" are applied straight
    to the CLAUDE.md regions via :func:`promote_user_corrections`; everything
    else — plus any correction that could not be written — lands in the
    proposals file.

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
                proposals, guide_path=guide_path
            )
            if promoted:
                logger.info(
                    "memory proposals: auto-promoted %d user correction(s) from %s",
                    len(promoted),
                    archive_path.name,
                )
        return append_proposals(
            proposals,
            workspace_vault_root,
            source_path=archive_path,
        )
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
