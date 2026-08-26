"""Route session-insight facts to their real destination.

The post-archive insights pipeline (``ciao/insights.py``) appends a
``## Session insights`` section to each archived chat. That section already
contains the high-signal facts we'd want in memory — errors, decisions, new
entities, user corrections, reusable snippets.

This module turns those facts into *destination-addressed* proposals. Each
bullet may carry a trailing destination tag written by the extraction model:

* ``[memory]``   — cross-project preference/environment/lesson → the
  ``ciao:memory`` region of the workspace ``CLAUDE.md``.
* ``[profile]``  — identity/communication style → the ``ciao:profile`` region.
* ``[project]``  — true only within this project → the project's canonical
  doc (folded at archive time by :mod:`ciao.project_doc_update`; queued with
  the doc path only when the fold did not consume it).
* ``[people: <Name>]`` — durable fact about a person → ``People/<Name>.md``.
* ``[learnings]`` — reusable how-to knowledge → ``Workspace/Learnings.md``.
* ``[review]``   — the model was not sure → waits for human or curator review.

Untagged bullets fall back to conservative defaults derived from their
section (corrections → memory, operator identity → profile, everything else
→ review): a missing tag *is* uncertainty.

Auto-apply is the default posture (``auto_promote_memory``): every confident,
state-shaped fact is written straight to its destination at archive time —
regions through the :mod:`ciao.memory_audit` event-shape guard, people notes
as stubs when absent, learnings as dated bullets. Anything the guards reject,
any destination whose write fails, and every ``[review]`` bullet land in the
queue file instead: ``<workspace-vault>/Workspace/Memory-Proposals.md``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, date
from pathlib import Path

logger = logging.getLogger(__name__)


_PROPOSALS_RELATIVE = "Workspace/Memory-Proposals.md"
_LEARNINGS_RELATIVE = "Workspace/Learnings.md"
_PEOPLE_DIR = "People"


# ── Destinations ──────────────────────────────────────────────────────────


DESTINATIONS: tuple[str, ...] = (
    "memory",
    "profile",
    "project",
    "people",
    "learnings",
    "review",
)
"""Destination vocabulary shared with the extraction prompts. A bullet tagged
outside this set is treated as untagged and falls back to section defaults."""

# Matches a trailing destination tag: ``[memory]``, ``[project]``,
# ``[people: Mo Salah]``. The colon-payload form is what the extraction
# prompts ask for; the queue-file form uses a space (``[people Mo Salah]``),
# which :mod:`ciao.proposal_kinds` owns.
_DESTINATION_RE = re.compile(
    rf"\s*\[({'|'.join(DESTINATIONS)})(?::[ \t]*([^\]]+))?\]\s*$",
    re.IGNORECASE,
)

_IDX_TAG_RE = re.compile(r"\s*\[idx\s*=\s*[\d,\s]+\]\s*$")


def _peel_trailing_metadata(text: str) -> tuple[str, str, str]:
    """Split trailing citation and destination metadata off a bullet.

    Returns ``(kind, payload, remaining_text)``. Models write the tag after
    the citation per the prompt, but either order is accepted: trailing
    bracketed groups are peeled from the end, and each must be an ``[idx=…]``
    citation or a destination tag — anything else stops the peel and stays in
    the text rather than being guessed at. When several tags somehow stack up,
    the one closest to the end of the line wins.
    """
    kind, payload = "", ""
    while True:
        match = _DESTINATION_RE.search(text)
        if match is not None:
            if not kind:
                kind = match.group(1).lower()
                payload = (match.group(2) or "").strip()
            text = text[: match.start()].rstrip()
            continue
        match = _IDX_TAG_RE.search(text)
        if match is not None:
            text = text[: match.start()].rstrip()
            continue
        return kind, payload, text


@dataclass(slots=True, frozen=True)
class MemoryProposal:
    """One proposed memory entry with its routing target."""

    target: str  # a DESTINATIONS member (legacy "user" normalized on write)
    text: str
    source_section: str
    payload: str = ""  # e.g. the person name for [people], doc path for [project]

    def as_bullet(self) -> str:
        # Deliberately total: an unknown target is written through rather than
        # raising, so one odd proposal cannot fail a whole archive batch.
        target = "profile" if self.target == "user" else self.target
        head = f"[{target} {self.payload}]" if self.payload else f"[{target}]"
        return f"- {head} {self.text}  _(from: {self.source_section})_"


# ── Parsing ───────────────────────────────────────────────────────────────


def _split_sections(insights_md: str) -> dict[str, list[str]]:
    """Group bullet lines by their ``## Heading``.

    Strips bullet markers and citation tags — ``[idx=12]`` and the
    multi-index ``[idx=12,34]`` shape models improvise — so the proposal is
    one clean sentence. Destination tags survive here; they are split later,
    per bullet, where the routing decision happens. Empty sections are
    dropped.
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
        text = re.sub(r"\s*\[idx\s*=\s*[\d,\s]+\]\s*$", "", text).strip()
        if text:
            sections[current].append(text)
    return sections


def _is_durable(text: str) -> bool:
    """Reject obvious per-session noise before proposing.

    This is the second line of defence behind the extraction prompt: bullets
    that reach here are already terse, but the model still drifts toward the
    "User said: X -> assistant did Y" event shape. A correction that only
    records what happened in one chat is exactly the shape ``memory_audit``
    flags as rot if it ever reaches a region, so it is stopped at the queue —
    unless it carries a ``Durable rule:`` clause, which is the durable part
    and survives regardless (a placeholder rule still stays pending for the
    curator to judge rather than being auto-promoted).
    """
    from ciao.memory_audit import find_event_shaped

    lowered = text.lower()
    if any(lowered.startswith(p) for p in ("tried ", "asked ", "ran ")):
        return False
    if len(text) < 12 or len(text) > 400:
        return False
    if _durable_rule_of(text) is not None:
        # Has a Durable rule clause (real or placeholder): keep it pending so
        # the curator decides. A real clause is promoted; a placeholder stays
        # queued rather than being silently proposed as durable.
        return True
    # No durable-rule clause: a bullet shaped like a transcript event is
    # session noise, not state. `find_event_shaped` is the same detector
    # memory_audit uses, so a bullet that would be flagged as rot on promotion
    # is never proposed at all.
    return not find_event_shaped("memory", [text])


def _durable_rule_of(text: str) -> str | None:
    """The standing rule a bullet asserts, or None when it carries no clause.

    Returns ``None`` when there is no "Durable rule:" clause; otherwise the
    clause text, which may be empty or a placeholder ("None"/"n/a"/an echoed
    template). Callers decide what an empty/placeholder clause means.
    """
    matches = list(_DURABLE_RULE_RE.finditer(text))
    if not matches:
        return None
    return matches[-1].group(1).strip().rstrip(".").strip()


# ── Proposal generation ───────────────────────────────────────────────────


# Section headers used by the extraction prompts in ``ciao/insights.py``.
_BEHAVIORAL_SECTIONS = ("User corrections", "Decisions")
_IDENTITY_SECTIONS = ("New entities",)


def _default_destination(section: str, text: str) -> tuple[str, str]:
    """Where an untagged bullet goes, given its insight section.

    Conservative by design: corrections and operator identity were always
    region-bound, so they keep those targets; anything else the model did not
    classify goes to review rather than pretending a region wants it.
    """
    if section == "User corrections":
        return "memory", ""
    if section == "New entities":
        match = re.match(r"^person\s*:\s*(operator|user)\b", text, re.I)
        if match:
            return "profile", ""
        person = re.match(r"^person\s*:\s*([^-–—]+?)\s*-", text, re.I)
        if person:
            return "people", person.group(1).strip()
    return "review", ""


def propose_from_insights(insights_md: str) -> list[MemoryProposal]:
    """Scan an insights markdown blob and emit destination-addressed proposals."""
    if not insights_md.strip():
        return []

    sections = _split_sections(insights_md)
    proposals: list[MemoryProposal] = []

    for heading in (*_BEHAVIORAL_SECTIONS, *_IDENTITY_SECTIONS):
        for item in sections.get(heading, []):
            kind, payload, text = _peel_trailing_metadata(item)
            if not kind:
                kind, payload = _default_destination(heading, text)
            if not _is_durable(text):
                continue
            proposals.append(MemoryProposal(
                target=kind,
                text=text,
                source_section=heading,
                payload=payload,
            ))

    return proposals


# ── Auto-apply ────────────────────────────────────────────────────────────


# The extraction prompt asks for the standing preference a correction implies
# as a trailing "Durable rule: <...>" sentence. That clause — not the
# "User said X -> assistant did Y" event around it — is what belongs in a
# region: the regions are a state surface, and memory_audit flags the event
# shape as rot for the nightly curator to remove.
#
# Both extraction prompts in ciao.insights embed this label verbatim (a test
# asserts the link), and the regex is built from it so the producer prompts
# and this consumer cannot drift apart silently. Case-sensitive and anchored
# to a sentence start so a chat fragment quoted inside the bullet ("... as a
# durable rule: ...") never matches.
DURABLE_RULE_LABEL = "Durable rule:"
_DURABLE_RULE_RE = re.compile(
    rf"(?:^|[.!?]\s+){re.escape(DURABLE_RULE_LABEL)}\s*(.+)$"
)

# Contentless fillers models emit instead of omitting the clause.
_NO_OP_RULES = frozenset({"none", "n/a", "na", "no", "-", "unknown"})


def _is_placeholder_rule(rule: str) -> bool:
    """An echoed template placeholder or a contentless no-op, not a rule."""
    if "<" in rule and ">" in rule:
        return True
    return rule.lower() in _NO_OP_RULES


def _promotable_text(text: str) -> str | None:
    """The state-shaped text safe to write to a bounded region, or None.

    None means "keep it in the proposals queue": the curator rephrases
    event-shaped corrections into standing rules on the next pass. Writing
    them verbatim used to overflow the always-injected region with entries
    the shipped audit itself classifies as event-shaped rot.
    """
    from ciao.memory_audit import find_event_shaped

    matches = list(_DURABLE_RULE_RE.finditer(text))
    if matches:
        rule = matches[-1].group(1).strip().rstrip(".").strip()
        if (
            rule
            and not _is_placeholder_rule(rule)
            and not find_event_shaped("memory", [rule])
        ):
            return rule + "."
        return None
    if find_event_shaped("memory", [text]):
        return None
    return text


def _promote_to_region(
    proposal: MemoryProposal,
    guide_path: Path,
) -> tuple[str, str | None]:
    """Write one region-bound proposal.

    Returns ``(outcome, promotable_or_None)`` where outcome is ``"written"``,
    ``"duplicate"`` (already remembered — dropped from both applied and
    remaining), ``"failed"`` (the write itself failed; stays queued), or
    ``"unshaped"`` (not state-shaped text; stays queued for the curator to
    rephrase).
    """
    from ciao.memory_tool import ensure_regions, read_region, resolve_region, write_region

    promotable = _promotable_text(proposal.text)
    if promotable is None:
        return "unshaped", None
    try:
        ensure_regions(guide_path)
        region = resolve_region(proposal.target)
        entries, diags = read_region(guide_path, region)
        if diags:
            logger.info(
                "memory apply: falling back to proposals (%s)",
                "; ".join(d.message for d in diags),
            )
            return "failed", promotable
        if promotable in entries:
            logger.info(
                "memory apply: dropped exact duplicate %r",
                promotable[:80],
            )
            return "duplicate", promotable
        write_region(guide_path, region, entries + [promotable])
        return "written", promotable
    except Exception as exc:  # noqa: BLE001 — one bad write must not stop a batch
        logger.info("memory apply: falling back to proposals (%s)", exc)
        return "failed", promotable


def _safe_name(name: str) -> str:
    """A person payload as a filename stem, without path separators."""
    cleaned = re.sub(r"[\\/:*?\"<>|]+", " ", name).strip().rstrip(".")
    return cleaned[:80]


def write_people_note(vault_root: Path, name: str, text: str) -> bool:
    """Create a stub person note. False when it already exists (needs a merge).

    Public because accepting a ``[people]`` proposal from the review queue
    performs exactly this write.
    """
    stem = _safe_name(name)
    if not stem:
        return False
    path = vault_root / _PEOPLE_DIR / f"{stem}.md"
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    # `updated:` records the note's creation as its first verification, so a
    # person the system stopped hearing about ages out visibly instead of
    # relying on mtime (which file copies and migrations reset silently).
    note = (
        "---\n"
        "tags: [person]\n"
        f"updated: {date.today().isoformat()}\n"
        f"---\n# {stem}\n\n{text}\n"
    )
    path.write_text(note, encoding="utf-8")
    return True


def append_learning(vault_root: Path, text: str) -> bool:
    """Append one learning under the Active section of Workspace/Learnings.md.

    Public because accepting a ``[learnings]`` proposal from the review queue
    performs exactly this write.
    """
    path = vault_root / _LEARNINGS_RELATIVE
    if path.exists():
        existing = path.read_text(encoding="utf-8")
    else:
        existing = (
            "---\n"
            "tags: [ciao, learnings]\n"
            "---\n"
            "# Learnings\n\n"
            "Reusable cross-project knowledge. Active entries are candidates "
            "for promotion into canonical guidance.\n"
        )
    entry = f"- {text}"
    if entry in existing:
        return True
    marker = "\n## Active\n"
    if marker in existing:
        head, _, tail = existing.partition(marker)
        updated = f"{head}{marker}{entry}\n{tail}"
    else:
        updated = existing.rstrip() + f"\n\n## Active\n\n{entry}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")
    return True


def apply_proposals(
    proposals: list[MemoryProposal],
    *,
    guide_path: Path | None = None,
    vault_root: Path | None = None,
) -> tuple[list[MemoryProposal], list[str]]:
    """Write every confidently-addressed proposal to its destination.

    Returns ``(remaining, applied_texts)``. Regions take state-shaped text
    (see :func:`_promotable_text`); people notes are created only when absent;
    learnings append. ``[project]`` rows are owned by the archive-time doc
    fold and ``[review]`` rows by a human, so both stay in ``remaining``.
    Exact duplicates vanish from both lists: the fact is already remembered,
    which is neither an apply nor something to re-decide. On any write failure
    the proposal stays reviewable.
    """
    remaining: list[MemoryProposal] = []
    applied: list[str] = []

    for proposal in proposals:
        try:
            if proposal.target in ("memory", "profile"):
                if guide_path is None:
                    remaining.append(proposal)
                    continue
                outcome, promotable = _promote_to_region(proposal, guide_path)
                if outcome == "written":
                    applied.append(promotable or proposal.text)
                elif outcome != "duplicate":
                    # Failed writes and event-shaped text both stay queued:
                    # the first for a retry, the second for a curator to
                    # rephrase into a standing rule.
                    remaining.append(proposal)
            elif proposal.target == "people" and vault_root is not None:
                name = proposal.payload or _safe_name(proposal.text.split("-")[0])
                if write_people_note(vault_root, name, proposal.text):
                    applied.append(proposal.text)
                else:
                    remaining.append(proposal)
            elif proposal.target == "learnings" and vault_root is not None:
                if append_learning(vault_root, proposal.text):
                    applied.append(proposal.text)
                else:
                    remaining.append(proposal)
            else:
                # project (the fold owns it) and review (a human owns it).
                remaining.append(proposal)
        except Exception as exc:  # noqa: BLE001 — never lose the batch to one row
            logger.info("memory apply: %s stays queued (%s)", proposal.target, exc)
            remaining.append(proposal)
    return remaining, applied


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

    if out_path.exists():
        # An existing file's header may predate the bounded-region layout and
        # still name `~/.ciao/memory.md` / `ciao memory add`. Refresh just that
        # header so the corrected wording reaches installed queues; bullets and
        # anything below them are left byte-identical.
        existing = _refresh_header(out_path.read_text(encoding="utf-8"))
    else:
        existing = _STUB_HEADER

    already = _existing_proposal_texts(existing) | _dismissed_texts(out_path)
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


_DISMISSED_LOG_SUFFIX = ".dismissed.jsonl"
_DISMISSED_LOG_LEGACY_SUFFIXES = (".dismissed.log",)


def dismissed_log_path(proposals_path: Path) -> Path:
    """Sidecar that holds the texts of already-decided proposals.

    ``.jsonl`` rather than ``.log`` because setup writes ``*.log`` into the
    workspace gitignore: the history only prevents re-filing while it stays
    put, so it must be tracked and sync like the queue itself.
    """
    return proposals_path.with_suffix(_DISMISSED_LOG_SUFFIX)


def record_dismissal(proposals_path: Path, *, text: str, kind: str = "") -> bool:
    """Record a decided proposal so the queue stops re-asking about it.

    ``append_proposals`` dedupes against bullets still in the queue file, so
    removing a dismissed row erases the only evidence of the decision: the
    next curator pass that re-reads the same transcript would re-file the
    fact verbatim. The decision therefore has to outlive the row itself, in
    a sidecar this module owns rather than in the outcomes ledger (which
    counts by kind and is trimmed).
    """
    cleaned = text.strip()
    if not cleaned:
        return False
    log_path = dismissed_log_path(proposals_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "dismissed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "kind": kind,
        "text": cleaned,
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return True


def _dismissed_texts(proposals_path: Path) -> set[str]:
    """Texts of previously decided proposals, from the sidecar log."""
    out: set[str] = set()
    for suffix in (_DISMISSED_LOG_SUFFIX, *_DISMISSED_LOG_LEGACY_SUFFIXES):
        try:
            raw = proposals_path.with_suffix(suffix).read_text(encoding="utf-8")
        except OSError:
            continue
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            text = str(entry.get("text", "")).strip()
            if text:
                out.add(text)
    return out


_STUB_HEADER = (
    "---\n"
    "tags: [ciao, memory, proposals]\n"
    "---\n"
    "# Memory Proposals\n\n"
    "Auto-generated proposals from session-insights curation. Each batch is "
    "timestamped. Confident facts are applied automatically at archive time; "
    "what lands here waited because the model was unsure or a write failed.\n\n"
    "Destinations: `[memory]` / `[profile]` are the bounded `ciao:memory` / "
    "`ciao:profile` regions of the workspace `CLAUDE.md` (edit the region "
    "first, then dismiss with `ciao memory-proposal-dismiss <text> "
    "--promoted` so the outcome counts as a promotion); "
    "`[project <doc-path>]` folds into that canonical doc; `[people <Name>]` "
    "updates `People/<Name>.md`; `[learnings]` appends to "
    "`Workspace/Learnings.md`; `[review]` has no known destination yet — "
    "decide what it is first. Standing directives stay in the CLAUDE.md body "
    "outside the fenced regions.\n"
)


def _proposals_header_block(source_path: Path | None) -> str:
    ts = datetime.now(UTC).isoformat(timespec="seconds")
    if source_path is not None:
        return f"\n## {ts} — from `{source_path.name}`\n\n"
    return f"\n## {ts}\n\n"


def _refresh_header(file_text: str) -> str:
    """Replace a stale leading header with the current one, or return unchanged.

    Only a file that begins with YAML frontmatter is touched: that is the
    signal the leading block is the generated header rather than a section a
    user wrote by hand. The scan for the first timestamped batch or proposal
    bullet starts only after the frontmatter closes, so an indented YAML list
    inside the frontmatter is never mistaken for a bullet. When the boundary
    cannot be identified confidently the text is returned byte-identical rather
    than guessed at, and no bullet and no user text is ever edited.
    """
    if not file_text.startswith("---\n"):
        return file_text
    lines = file_text.splitlines(keepends=True)
    content_start = 0
    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            content_start = idx + 1
            break
    if not content_start:
        # Frontmatter opened and never closed. The boundary scan below would
        # then start at line 0 and mistake an indented YAML list item for a
        # proposal bullet, splicing the header into the middle of the
        # frontmatter. An unparseable file is left byte-identical.
        return file_text
    boundary = len(file_text)
    for idx in range(content_start, len(lines)):
        stripped = lines[idx].lstrip()
        if stripped.startswith("## ") or stripped.startswith("- "):
            boundary = sum(len(item) for item in lines[:idx])
            break
    if boundary >= len(file_text):
        return file_text
    return _STUB_HEADER + file_text[boundary:]


# ── Pipeline entry point ──────────────────────────────────────────────────


def proposals_from_archive(
    archive_path: Path,
    workspace_vault_root: Path,
    *,
    auto_promote_memory: bool = False,
    guide_path: Path | None = None,
    stats: dict[str, int] | None = None,
    project_doc_path: str = "",
    project_fold_wrote: bool = False,
) -> Path | None:
    """Read an archived chat, route its insights, optionally auto-apply.

    With ``auto_promote_memory`` set, every confidently-addressed proposal is
    written straight to its destination via :func:`apply_proposals`;
    everything else — plus any write that failed — lands in the proposals
    file.

    ``project_doc_path`` is the chat's canonical doc (workspace-root-relative
    or absolute). When ``project_fold_wrote`` is true the fold consumed this
    archive's insights, so ``[project]`` bullets are dropped rather than
    queued; otherwise they are queued addressed to that doc so a one-click
    accept can fold them later.

    Returns the proposals file path when something was written, else None.
    Swallows all exceptions; this runs as a fire-and-forget step.

    ``stats``, when given, is filled with ``proposed`` (how many proposals were
    written to the file) and ``promoted`` (how many were auto-applied). The
    archived chat reports these counts back to the user, which the returned
    path alone cannot express. It stays an out-parameter so the return
    contract every existing caller relies on is unchanged.
    """
    try:
        if not archive_path.exists():
            return None
        text = archive_path.read_text(encoding="utf-8")
        body = _extract_insights_section(text)
        if not body:
            return None
        proposals = propose_from_insights(body)

        if project_fold_wrote:
            consumed = [p for p in proposals if p.target == "project"]
            if consumed:
                logger.info(
                    "memory proposals: doc fold consumed %d project fact(s) from %s",
                    len(consumed),
                    archive_path.name,
                )
            proposals = [p for p in proposals if p.target != "project"]
        elif project_doc_path:
            proposals = [
                p if p.target != "project" or p.payload
                else MemoryProposal(
                    target=p.target,
                    text=p.text,
                    source_section=p.source_section,
                    payload=project_doc_path,
                )
                for p in proposals
            ]

        if auto_promote_memory and proposals:
            proposals, promoted = apply_proposals(
                proposals, guide_path=guide_path, vault_root=workspace_vault_root
            )
            if promoted:
                if stats is not None:
                    stats["promoted"] = len(promoted)
                logger.info(
                    "memory proposals: auto-applied %d fact(s) from %s",
                    len(promoted),
                    archive_path.name,
                )
        written = append_proposals(
            proposals,
            workspace_vault_root,
            source_path=archive_path,
        )
        if stats is not None:
            # Counted from what was actually filed, not from what was parsed:
            # auto-apply removes the applied facts from the list above.
            stats["proposed"] = len(proposals) if written else 0
        return written
    except Exception:  # noqa: BLE001 — never crash the pipeline
        logger.exception("memory proposals failed for %s", archive_path)
        return None


def _extract_insights_section(archive_md: str) -> str:
    """Return the body of the archive's real appended insights section, or ''.

    Delegates to :func:`ciao.insights.locate_insights_section` so a transcript
    that merely quotes the header (curation chats do) is never mistaken for
    the appended section — the old first-occurrence match re-proposed bullets
    the curator had already reviewed and deleted from the queue.
    """
    from ciao.insights import locate_insights_section

    location = locate_insights_section(archive_md)
    if location is None:
        return ""
    return archive_md[location[1]:].strip()


# ── Review surface (CLI) ──────────────────────────────────────────────────


def list_proposals(
    proposals_path: Path,
) -> list[dict[str, str]]:
    """Structured pending proposal bullets from a workspace's queue.

    Reuses the shared proposal-kind grammar so the CLI, the web layer, and the
    audit always agree on what a bullet is. Each row carries the raw ``kind``,
    the bullet ``text``, the optional ``source`` tag, and the optional
    ``target`` payload (a person name or doc path).
    """
    from ciao.proposal_kinds import parse_bullet

    if not proposals_path.exists():
        return []
    rows: list[dict[str, str]] = []
    for raw in proposals_path.read_text(encoding="utf-8").splitlines():
        bullet = parse_bullet(raw)
        if bullet is not None:
            rows.append(
                {
                    "kind": bullet.kind,
                    "text": bullet.text,
                    "source": bullet.source,
                    "target": bullet.target,
                }
            )
    return rows


def remove_proposal_by_substring(
    proposals_path: Path,
    needle: str,
) -> tuple[str, str] | None:
    """Remove the single proposal whose bullet matches ``needle``.

    Matches a unique substring case-insensitively across the pending bullets
    (the same contract the removed MCP resolve tool used). Returns the removed
    bullet's ``(kind, text)`` so the caller can record the decision, or None
    when the file is missing or no match exists. An ambiguous match (more
    than one) is left unresolved and returns None so the caller can ask for a
    longer substring.
    """
    from ciao.proposal_kinds import parse_bullet

    if not proposals_path.exists():
        return None
    needle = needle.strip()
    if not needle:
        return None
    lines = proposals_path.read_text(encoding="utf-8").splitlines()
    candidates: list[int] = []
    for index, line in enumerate(lines):
        if parse_bullet(line) is None:
            continue
        if needle.casefold() in line.casefold():
            candidates.append(index)
    if len(candidates) != 1:
        return None
    bullet = parse_bullet(lines[candidates[0]])
    if bullet is None:
        return None
    del lines[candidates[0]]
    proposals_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return bullet.kind, bullet.text


def dismiss_proposal_by_substring(
    proposals_path: Path,
    needle: str,
) -> bool:
    """Remove one matching proposal, answering in booleans.

    Thin view over :func:`remove_proposal_by_substring` for callers that only
    need to know whether anything was removed.
    """
    return remove_proposal_by_substring(proposals_path, needle) is not None
