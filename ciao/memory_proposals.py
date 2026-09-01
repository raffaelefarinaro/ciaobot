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
from typing import Any

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


def _one_line(value: str) -> str:
    """Collapse every whitespace run (newlines included) into a single space.

    The proposals queue is line-oriented Markdown: one bullet is one line. A
    field carrying an embedded newline would otherwise split into a truncated
    bullet plus a continuation the parser reads as its own spurious proposal,
    and the original value would never appear as one parsed bullet, so
    re-filing it would dodge the text dedupe.
    """
    return " ".join(value.split())


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
        # Every field is forced onto one line and kept clear of the delimiter
        # that closes its own slot: a `]` inside the payload would end the
        # destination head early, and a `)` inside the source would break the
        # `_(from: ...)_` tail so the whole bullet stops parsing — invisible
        # to the review UI and to dedupe alike.
        payload = _one_line(self.payload).replace("]", "")
        source = _one_line(self.source_section).replace(")", "")
        head = f"[{target} {payload}]" if payload else f"[{target}]"
        return f"- {head} {_one_line(self.text)}  _(from: {source})_"


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


def _log_consolidation(
    vault_root: Path, region: str, old_entry: str, *, label: str = "auto-reconcile"
) -> None:
    """Copy a replaced region entry into the undo log before it disappears.

    ``Workspace/Memory-Consolidations.md`` is the standing rule for unattended
    region edits: nothing is dropped silently, and the user can restore any
    line. The file is in ``RESERVED_UNINDEXED_FILES``, so it never pollutes
    recall. Appended rather than rewritten: the log grows without bound, and a
    full read-modify-write would slow every consolidation as history piles up.
    """
    path = vault_root / "Workspace" / "Memory-Consolidations.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(
            "---\ntags: [ciao, memory, undo-log]\n---\n"
            "# Memory Consolidations\n\n"
            "Undo log for bounded-memory edits: every removed or replaced "
            "entry is copied here first.\n",
            encoding="utf-8",
        )
    heading = f"\n## {date.today().isoformat()} — ciao:{region} ({label})\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(heading + f"- {_one_line(old_entry)}\n")


def _promote_to_region(
    proposal: MemoryProposal,
    guide_path: Path,
    *,
    vault_root: Path | None = None,
    decision: dict[str, Any] | None = None,
) -> tuple[str, str | None]:
    """Write one region-bound proposal.

    Returns ``(outcome, promotable_or_None)`` where outcome is ``"written"``,
    ``"duplicate"`` (already remembered — dropped from both applied and
    remaining), ``"failed"`` (the write itself failed; stays queued), or
    ``"unshaped"`` (not state-shaped text; stays queued for the curator to
    rephrase).

    ``decision`` is this fact's row from :func:`plan_region_reconcile`, when
    the caller ran one: ``{"action": "covered"}`` drops the fact as already
    remembered, ``{"action": "update", "index": N, "text": ...}`` replaces
    entry ``N`` (1-based) with the merged text — the replaced entry goes to
    the consolidations undo log first — and ``{"action": "add"}`` or ``None``
    is the plain append path.
    """
    from ciao.memory_tool import (
        _guide_lock,
        ensure_regions,
        read_region,
        resolve_region,
        write_region,
    )

    promotable = _promotable_text(proposal.text)
    if promotable is None:
        return "unshaped", None
    from ciao.memory_audit import strip_learned_stamp

    # The whole read-merge-write, under the same lock `update_region` takes.
    # Without it two concurrent accepts — or an accept racing an MCP
    # /remember — both read the region, both append to their own snapshot, and
    # the second write drops the first fact while its row is dismissed as
    # promoted.
    lock = None
    try:
        lock = _guide_lock(guide_path)
    except Exception:  # noqa: BLE001 — a lock we cannot take must not block a write
        lock = None
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
        # Compared with stamps stripped: the same fact promoted on two
        # different days is still the same fact.
        if promotable in {strip_learned_stamp(entry) for entry in entries}:
            logger.info(
                "memory apply: dropped exact duplicate %r",
                promotable[:80],
            )
            return "duplicate", promotable

        decision = decision or {}
        action = str(decision.get("action", "add"))
        if action == "covered":
            logger.info(
                "memory apply: reconcile says already covered: %r",
                promotable[:80],
            )
            # A model verdict, not a provable string match: leave a trace in
            # the undo log so a hallucinated "covered" never silently loses
            # the fact — the standing "nothing is dropped silently" contract.
            # With nowhere to write that trace, honour the contract instead of
            # the verdict and take the plain append: a duplicate is visible and
            # removable, a silently dropped fact is neither.
            if vault_root is None:
                logger.info(
                    "memory apply: reconcile said covered but there is no vault "
                    "to log it to; appending instead of dropping %r",
                    promotable[:80],
                )
            else:
                _log_consolidation(
                    vault_root,
                    region,
                    f"(incoming fact judged covered; not written) {promotable}",
                    label="auto-reconcile covered",
                )
                return "duplicate", promotable
        if action == "update" and vault_root is not None:
            index = decision.get("index")
            merged = str(decision.get("text", "")).strip()
            plan_old = str(decision.get("old", ""))
            from ciao.memory_audit import find_event_shaped

            if (
                isinstance(index, int)
                and 1 <= index <= len(entries)
                and merged
                # The region only ever holds state-shaped text; a model-merged
                # replacement must clear the same bar the plain append does.
                and not find_event_shaped(region, [merged])
                # The index was computed against a snapshot taken before an
                # up-to-two-minute model call; if the entry it names has
                # changed since (concurrent /remember, another archive's
                # apply), replacing it would overwrite an unrelated fact.
                and (
                    not plan_old
                    or strip_learned_stamp(entries[index - 1])
                    == strip_learned_stamp(plan_old)
                )
            ):
                old = entries[index - 1]
                updated = list(entries)
                updated[index - 1] = f"{merged} [{date.today().isoformat()}]"
                _log_consolidation(vault_root, region, old)
                write_region(guide_path, region, updated)
                logger.info(
                    "memory apply: reconciled update of entry %d in ciao:%s",
                    index,
                    region,
                )
                return "written", promotable
            # A malformed or stale update decision degrades to the safe plain
            # append — never to silently dropping either the old or the new
            # fact.
        # The learned-at stamp is system time — when this fact entered the
        # region — read by the aging audit so unverified old facts surface
        # for re-verification instead of asserting themselves forever.
        stamped = f"{promotable} [{date.today().isoformat()}]"
        write_region(guide_path, region, entries + [stamped])
        return "written", promotable
    except Exception as exc:  # noqa: BLE001 — one bad write must not stop a batch
        logger.info("memory apply: falling back to proposals (%s)", exc)
        return "failed", promotable
    finally:
        if lock is not None:
            try:
                lock.close()
            except Exception:  # noqa: BLE001
                pass


def accept_region_fact(
    *,
    guide_path: Path,
    target: str,
    text: str,
    vault_root: Path | None,
    decision: dict[str, Any] | None = None,
) -> tuple[str, str | None]:
    """Write one approved region fact through the guarded path.

    The UI accept button used to call ``update_region(action="add")`` directly,
    which skipped everything the archive-time path does: the event-shape guard
    (so an event-shaped bullet landed verbatim in always-loaded context), the
    stamp-stripped duplicate check, the learned-at stamp the aging audit reads,
    and the consolidations undo log.

    Deliberately synchronous and model-free. Reconciliation belongs here in
    principle — accepting an updated preference still appends beside the entry
    it supersedes — but one ``run_oneshot`` per row on a click is a 120s timeout
    each, and the batch endpoint accepts rows sequentially inside one request.
    A caller that has already reconciled elsewhere may pass ``decision``; the
    click path passes none.

    Returns ``_promote_to_region``'s ``(outcome, promotable)``.
    """
    proposal = MemoryProposal(target=target, text=text, source_section="review")
    return _promote_to_region(
        proposal,
        guide_path,
        vault_root=vault_root,
        decision=decision,
    )


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


# One structured learning line. The shape is a contract: the curation skill
# reads the recurrence count to decide promotion (N ≥ 3) and the sources to
# cite episodes, so recurrence bookkeeping is mechanical instead of prose.
_LEARNING_LINE_RE = re.compile(
    r"^- \[(?P<key>[a-z0-9][a-z0-9-]*)\] "
    r"\[(?P<first>\d{4}-\d{2}-\d{2}) → (?P<last>\d{4}-\d{2}-\d{2})\] "
    r"\(x(?P<count>\d+)\) "
    r"(?P<text>.*?)"
    r"(?: — sources: (?P<sources>.*))?$"
)

_LEARNING_KEY_WORDS = 4
_LEARNING_MAX_SOURCES = 8


def _learning_key(text: str) -> str:
    """A short kebab identifier from the statement's first distinctive words."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words[:_LEARNING_KEY_WORDS]) or "learning"


def _normalized_learning(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def format_learning_line(
    text: str,
    *,
    first_seen: str,
    last_seen: str,
    count: int,
    sources: list[str],
) -> str:
    line = (
        f"- [{_learning_key(text)}] [{first_seen} → {last_seen}] "
        f"(x{count}) {_one_line(text)}"
    )
    cited = [s for s in sources if s][:_LEARNING_MAX_SOURCES]
    if cited:
        line += f" — sources: {', '.join(cited)}"
    return line


def append_learning(vault_root: Path, text: str, *, source: str = "") -> bool:
    """File one learning under the Active section of Workspace/Learnings.md.

    Structured entries carry a key, first-seen/last-seen dates, a recurrence
    count, and source chat ids. Re-observing a learning (same normalized
    statement) increments its count and refreshes last-seen instead of
    appending a duplicate — recurrence is what the curation skill promotes on,
    so it must be counted mechanically, not judged from prose. Legacy plain
    bullets are left untouched; an exact legacy duplicate still short-circuits.

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
            "for promotion into canonical guidance once they recur (x3 or "
            "more).\n"
        )
    if f"- {_one_line(text)}" in existing:
        # Exact legacy duplicate: already recorded in the old plain shape.
        return True

    today = date.today().isoformat()
    normalized = _normalized_learning(text)
    lines = existing.split("\n")
    for index, line in enumerate(lines):
        match = _LEARNING_LINE_RE.match(line)
        if match is None:
            continue
        if _normalized_learning(match.group("text")) != normalized:
            continue
        sources = [
            item.strip()
            for item in (match.group("sources") or "").split(",")
            if item.strip()
        ]
        if source and source not in sources:
            sources.append(source)
        lines[index] = format_learning_line(
            match.group("text"),
            first_seen=match.group("first"),
            last_seen=today,
            count=int(match.group("count")) + 1,
            sources=sources,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
        return True

    entry = format_learning_line(
        text,
        first_seen=today,
        last_seen=today,
        count=1,
        sources=[source] if source else [],
    )
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
    region_decisions: dict[str, dict[str, Any]] | None = None,
    learning_source: str = "",
) -> tuple[list[MemoryProposal], list[str]]:
    """Write every confidently-addressed proposal to its destination.

    Returns ``(remaining, applied_texts)``. Regions take state-shaped text
    (see :func:`_promotable_text`); people notes are created only when absent;
    learnings append. ``[project]`` rows are owned by the archive-time doc
    fold and ``[review]`` rows by a human, so both stay in ``remaining``.
    Exact duplicates vanish from both lists: the fact is already remembered,
    which is neither an apply nor something to re-decide. On any write failure
    the proposal stays reviewable.

    ``region_decisions`` maps :func:`_decision_key` (region + promotable fact
    text) to its reconcile decision (see :func:`plan_region_reconcile`);
    absent or None, every region write is the plain append path.
    """
    from ciao.memory_tool import resolve_region

    remaining: list[MemoryProposal] = []
    applied: list[str] = []

    for proposal in proposals:
        try:
            if proposal.target in ("memory", "profile"):
                if guide_path is None:
                    remaining.append(proposal)
                    continue
                promotable_key = _promotable_text(proposal.text)
                decision = None
                if region_decisions and promotable_key:
                    decision = region_decisions.get(
                        _decision_key(resolve_region(proposal.target), promotable_key)
                    )
                outcome, promotable = _promote_to_region(
                    proposal,
                    guide_path,
                    vault_root=vault_root,
                    decision=decision,
                )
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
                if append_learning(vault_root, proposal.text, source=learning_source):
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


# ── Write-time reconcile (ADD / UPDATE / COVERED) ──────────────────────────


_RECONCILE_SYSTEM_PROMPT = """\
You maintain a small always-loaded memory region for a personal assistant.
You are given its current numbered entries and lettered candidate facts.
For each candidate, decide exactly one action:
- "add": genuinely new information no existing entry carries.
- "covered": an existing entry already states this fact (even in other words).
- "update": it supersedes or extends exactly ONE existing entry. Give that
  entry's number and the single merged replacement text: present tense, keep
  every part of the old entry that is still true, keep any [expires:] or
  [as-of:] tag that still applies, and never merge two unrelated facts.
Be conservative: when unsure between update and add, choose add.
Reply with ONLY a JSON array, one object per candidate in their given order:
[{"action": "add"}, {"action": "update", "index": 2, "text": "..."}, {"action": "covered"}]
No prose, no code fences, no trailing commentary.
"""

# One reconcile call is bounded by the region cap (~3000 chars) plus a few
# candidates, so a short timeout keeps a slow backend from stalling archive
# post-processing. Failure is safe: the caller degrades to plain appends.
_RECONCILE_TIMEOUT_S = 120.0


def _decision_key(region: str, fact: str) -> str:
    """The ``region_decisions`` map key for one candidate fact.

    Region-qualified: the same sentence can be bound to both ``memory`` and
    ``profile`` in one archive, and a bare-text key would let one region's
    ``update`` index be applied against the other region's entries.
    """
    return f"{region}\n{fact}"


def _parse_reconcile_reply(raw: str, count: int) -> list[dict[str, Any]] | None:
    """Parse the model's JSON array; None when the shape is unusable.

    Per-row problems degrade that row to ``{"action": "add"}`` — the plain
    append never loses a fact — but a reply that is not a JSON array of the
    right length is discarded whole rather than guessed at.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, list) or len(data) != count:
        return None
    rows: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            rows.append({"action": "add"})
            continue
        action = str(item.get("action", "add")).lower()
        if action == "update":
            index = item.get("index")
            merged = str(item.get("text", "")).strip()
            if isinstance(index, bool) or not isinstance(index, int) or not merged:
                rows.append({"action": "add"})
                continue
            rows.append({"action": "update", "index": index, "text": merged})
        elif action in ("covered", "add"):
            rows.append({"action": action})
        else:
            rows.append({"action": "add"})
    return rows


async def plan_region_reconcile(
    archive_path: Path,
    guide_path: Path,
    *,
    model: str,
    provider: str = "claude",
    cwd: Path | None = None,
) -> dict[str, dict[str, Any]] | None:
    """Decide ADD / UPDATE / COVERED for an archive's region-bound facts.

    The Mem0 pattern, done at write time where dedupe is cheap: before the
    sync apply step runs, one small model call per region compares the new
    facts against the region's current entries (the whole region fits in a
    prompt — it is capped at a few thousand characters). Returns a map from
    promotable fact text to its decision row, or None when there is nothing
    to reconcile or the call failed — the caller then takes today's plain
    append path, which never blocks archiving and never loses a fact.
    """
    from ciao.memory_tool import read_region, resolve_region

    try:
        text = archive_path.read_text(encoding="utf-8")
    except OSError:
        return None
    body = _extract_insights_section(text)
    if not body:
        return None

    from ciao.memory_audit import strip_learned_stamp

    # Candidates per region: state-shaped facts that are not already exact
    # duplicates (those need no model call to drop).
    by_region: dict[str, list[str]] = {}
    entries_by_region: dict[str, list[str]] = {}
    for proposal in propose_from_insights(body):
        if proposal.target not in ("memory", "profile"):
            continue
        promotable = _promotable_text(proposal.text)
        if promotable is None:
            continue
        try:
            region = resolve_region(proposal.target)
            if region not in entries_by_region:
                entries, diags = read_region(guide_path, region)
                if diags:
                    continue
                entries_by_region[region] = entries
        except Exception:  # noqa: BLE001 — reconcile is best-effort
            continue
        stripped = {
            strip_learned_stamp(entry) for entry in entries_by_region[region]
        }
        if promotable in stripped:
            continue
        # An empty region has nothing to reconcile against.
        if not entries_by_region[region]:
            continue
        by_region.setdefault(region, []).append(promotable)

    if not by_region:
        return None

    decisions: dict[str, dict[str, Any]] = {}
    for region_name, candidates in by_region.items():
        rows = await _reconcile_region(
            region_name,
            entries_by_region[region_name],
            candidates,
            model=model,
            provider=provider,
            cwd=cwd,
        )
        if rows is None:
            continue
        for fact, row in zip(candidates, rows):
            decisions[_decision_key(region_name, fact)] = row

    return decisions or None


async def _reconcile_region(
    region_name: str,
    entries: list[str],
    candidates: list[str],
    *,
    model: str,
    provider: str = "claude",
    cwd: Path | None = None,
) -> list[dict[str, Any]] | None:
    """One reconcile call: decide ADD / UPDATE / COVERED per candidate.

    Returns one row per candidate in the given order, or None when the call
    failed or replied unparseably — every caller then degrades to the plain
    append path, which never blocks and never loses a fact.

    An ``update`` row carries ``old``: the entry its index named in the snapshot
    the model actually saw. The apply step re-reads the region and refuses the
    update if that entry changed during the call, so a stale index cannot
    overwrite an unrelated fact.
    """
    from ciao.providers.oneshot import run_oneshot

    numbered = "\n".join(
        f"{index}. {entry}" for index, entry in enumerate(entries, start=1)
    )
    lettered = "\n".join(
        f"{chr(ord('A') + index)}. {fact}" for index, fact in enumerate(candidates)
    )
    prompt = (
        f"Region `ciao:{region_name}` current entries:\n{numbered}\n\n"
        f"Candidate facts:\n{lettered}\n"
    )
    try:
        reply = await run_oneshot(
            prompt,
            system_prompt=_RECONCILE_SYSTEM_PROMPT,
            model=model,
            timeout_s=_RECONCILE_TIMEOUT_S,
            provider=provider,
            cwd=cwd,
        )
    except Exception as exc:  # noqa: BLE001 — degrade to plain appends
        logger.info("memory reconcile: call failed (%s); using plain adds", exc)
        return None
    rows = _parse_reconcile_reply(reply, len(candidates))
    if rows is None:
        logger.info(
            "memory reconcile: unparseable reply for ciao:%s; using plain adds",
            region_name,
        )
        return None
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.get("action") == "update":
            index = row.get("index")
            if not (isinstance(index, int) and 1 <= index <= len(entries)):
                # Out-of-range against the very snapshot the model saw:
                # degrade now rather than carry a junk decision around.
                row = {"action": "add"}
            else:
                row = dict(row)
                row["old"] = entries[index - 1]
        out.append(row)
    return out


# ── Persistence ───────────────────────────────────────────────────────────


def _decided_with(workspace_vault_root: Path, text: str, key: str) -> bool:
    """Whether ``text`` was *rejected* before, not merely decided before.

    `append_proposals` dedupes against both pending bullets and the decision
    sidecar and returns None for either, so a caller cannot otherwise tell "it
    is already waiting for you" from "you rejected this before, and it will not
    come back". Those need different words: the second is the one that silently
    loses a user who has changed their mind.

    Reads the sidecar directly rather than through `_dismissed_texts`, which
    returns every decided text — `record_promotion` writes to the same log with
    a `promoted_at` key. Reusing it would tell someone their fact had been
    rejected when it was in fact accepted and may already be live.

    Compared through `_one_line`, exactly as `append_proposals` compares.
    """
    out_path = Path(workspace_vault_root) / _PROPOSALS_RELATIVE
    if not out_path.exists():
        return False
    wanted = _one_line(text)
    for suffix in (_DISMISSED_LOG_SUFFIX, *_DISMISSED_LOG_LEGACY_SUFFIXES):
        try:
            raw = out_path.with_suffix(suffix).read_text(encoding="utf-8")
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
            if not isinstance(entry, dict) or key not in entry:
                continue
            if _one_line(str(entry.get("text", ""))) == wanted:
                return True
    return False



def was_dismissed(workspace_vault_root: Path, text: str) -> bool:
    """Whether ``text`` was rejected before."""
    return _decided_with(workspace_vault_root, text, "dismissed_at")


def was_promoted(workspace_vault_root: Path, text: str) -> bool:
    """Whether ``text`` was accepted before, and so may already be live.

    The third state a caller needs. `append_proposals` refuses a re-file for a
    promoted fact exactly as it does for a dismissed one, and reporting either
    as "already in the queue" is wrong — a promoted fact is not in the queue at
    all, it is in its destination.
    """
    return _decided_with(workspace_vault_root, text, "promoted_at")


def append_proposals(
    proposals: list[MemoryProposal],
    workspace_vault_root: Path,
    *,
    source_path: Path | None = None,
    allow_dismissed: bool = False,
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

    decided = _promoted_texts(out_path) if allow_dismissed else _dismissed_texts(out_path)
    already = _existing_proposal_texts(existing) | decided
    # Compare the text exactly as ``as_bullet`` will write it, or a proposal
    # whose text is only whitespace-different from a queued/dismissed one
    # slips past dedupe and lands as a visually identical duplicate row.
    fresh = [p for p in proposals if _one_line(p.text) not in already]
    if not fresh:
        return None

    header = _proposals_header_block(source_path)
    lines = [p.as_bullet() for p in fresh]
    block = header + "\n".join(lines) + "\n"

    out_path.write_text(existing + "\n" + block, encoding="utf-8")
    return out_path


# Matches a bullet written by ``MemoryProposal.as_bullet``.
_BULLET_RE = re.compile(r"^- \[[^\]]+\] (.+?)  _\(from: [^)]*\)_\s*$")

# Matches a timestamped batch header written by ``_proposals_header_block``.
_BATCH_HEADER_RE = re.compile(r"^## \d{4}-\d{2}-\d{2}T\S+")


def _sweep_empty_batches(lines: list[str]) -> list[str]:
    """Drop timestamped batch headers whose bullets are all gone.

    Dismissal removes bullets one line at a time, so the last dismissal in a
    batch left its ``## <timestamp>`` header behind forever; on a real queue
    29 of 30 batches were empty headers plus blank lines. Precision-first:
    only a header this module wrote (the timestamp shape) whose whole section
    is blank is swept — a section holding any other text was written by a
    hand or an agent and is not this function's to delete.
    """
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if _BATCH_HEADER_RE.match(line):
            end = index + 1
            while end < len(lines) and not lines[end].startswith("## "):
                end += 1
            if all(not item.strip() for item in lines[index + 1:end]):
                index = end
                continue
        out.append(line)
        index += 1
    return out


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

    Accepted rows need the same treatment (``record_decision``): the nightly
    curator dedupes against the live queue and this sidecar, never against
    the promoted destination, so a promotion must record the text too or the
    same fact comes back the next time the transcript is re-read.
    """
    return _record_decision(proposals_path, text=text, kind=kind, key="dismissed_at")


def record_promotion(proposals_path: Path, *, text: str, kind: str = "") -> bool:
    """Record an accepted proposal in the same decision history.

    Same sidecar and the same reason as :func:`record_dismissal`: the dedupe
    in ``append_proposals`` reads only the queue file and this log, never the
    region/doc the promotion wrote to, so without this entry the curator
    re-queues the accepted fact on its next pass over the same transcript.
    Mirrors the CLI's promote-then-dismiss flow, which records the text for
    every removal regardless of the outcome action.
    """
    return _record_decision(proposals_path, text=text, kind=kind, key="promoted_at")


def _record_decision(proposals_path: Path, *, text: str, kind: str, key: str) -> bool:
    cleaned = text.strip()
    if not cleaned:
        return False
    log_path = dismissed_log_path(proposals_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        key: datetime.now(UTC).isoformat(timespec="seconds"),
        "kind": kind,
        "text": cleaned,
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return True


def _dismissed_texts(proposals_path: Path) -> set[str]:
    """Texts of previously decided proposals, from the sidecar log."""
    # Older sidecars recorded only ``text`` and ``kind``; keep treating those
    # entries as decisions when rebuilding the general dedupe set.
    return _decision_texts(proposals_path, keys=())


def _promoted_texts(proposals_path: Path) -> set[str]:
    """Texts of proposals previously accepted into their destination."""
    return _decision_texts(proposals_path, keys=("promoted_at",))


def _decision_texts(proposals_path: Path, *, keys: tuple[str, ...]) -> set[str]:
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
            if not isinstance(entry, dict) or (keys and not any(key in entry for key in keys)):
                continue
            text = _one_line(str(entry.get("text", "")))
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
    "first, then dismiss with `ciao memory-proposal-dismiss --text-file <file> "
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


# ── Already-applied guard ─────────────────────────────────────────────────

# Minimum character overlap for a proposal to be considered already present in
# a destination file. Shorter than a meaningful fact would be noise; longer
# than a sentence would require exact formatting.
_APPLIED_MIN_OVERLAP = 40


def _normalize_for_match(text: str) -> str:
    """Lowercased, whitespace-collapsed form for containment checks."""
    return " ".join(text.lower().split())


_NEGATION_RE = re.compile(r"\b(?:do not|don't|never|avoid)\b")

# Where one fact ends and the next begins, in text that has already been
# through `_normalize_for_match`. That collapse removes every newline, so a
# markdown file arrives here as one line and the bullet markers that started
# each item are the only separator left between two independent facts. Without
# them a single "- Never commit secrets" bullet reads as the containing
# sentence of everything below it, and every later fact in the file is scored
# as negated — so an already-applied proposal fails this guard and is queued
# back into Review, which is the exact loop the guard exists to close.
_SEGMENT_SPLIT_RE = re.compile(r"[.!?;\n]|(?:^|(?<= ))(?:[-*+\u2022\u2013\u2014]|\d+\.) ")


def _positive_contains(text: str, needle: str) -> bool:
    """Match *needle* unless its containing sentence is negated."""
    start = 0
    while True:
        at = text.find(needle, start)
        if at < 0:
            return False
        boundary = 0
        for match in _SEGMENT_SPLIT_RE.finditer(text, 0, at):
            boundary = match.end()
        if not _NEGATION_RE.search(text[boundary:at]):
            return True
        start = at + 1


def _is_already_in_file(path: Path, proposal_text: str) -> bool:
    """True when *path* already contains the proposal's substance."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return False
    norm_content = _normalize_for_match(content)
    norm_proposal = _normalize_for_match(proposal_text)
    if not norm_proposal:
        return False
    # Exact containment first (fast, precise for copy-paste facts like paths).
    if _positive_contains(norm_content, norm_proposal):
        return True
    if len(norm_proposal) < _APPLIED_MIN_OVERLAP:
        return False
    # Also check the promotable variant for memory/profile: the region holds
    # "Avoid em dashes; use commas instead." while the proposal may carry
    # "User said: ... Durable rule: Avoid em dashes; use commas instead."
    promotable = _promotable_text(proposal_text)
    if promotable and promotable != proposal_text:
        norm_promotable = _normalize_for_match(promotable)
        if len(norm_promotable) >= _APPLIED_MIN_OVERLAP and _positive_contains(norm_content, norm_promotable):
            return True
    # Do not use unordered token overlap here. It treats contradictory or
    # unrelated sentences as equivalent (for example, "use Python" versus
    # "do not use Python"). A false negative leaves a reviewable proposal;
    # a false positive silently loses a user fact.
    return False


def _is_already_in_region(guide_path: Path, region: str, proposal_text: str) -> bool:
    """True when a bounded region already holds the proposal."""
    from ciao.memory_tool import read_region

    try:
        entries, diags = read_region(guide_path, region)
    except Exception:
        return False
    if diags:
        return False
    # Exact promotable match is the canonical dedupe (see _promote_to_region),
    # but the guard also suppresses near-duplicates already present with
    # different punctuation/casing so they never reach the review tab.
    promotable = _promotable_text(proposal_text) or proposal_text
    norm_promotable = _normalize_for_match(promotable)
    for entry in entries:
        norm_entry = _normalize_for_match(entry)
        if norm_promotable == norm_entry:
            return True
        if len(norm_promotable) >= _APPLIED_MIN_OVERLAP and _positive_contains(norm_entry, norm_promotable):
            return True
        if len(norm_entry) >= _APPLIED_MIN_OVERLAP and _positive_contains(norm_promotable, norm_entry):
            return True
    return False


def _resolve_doc_path(vault_root: Path, doc_path: str) -> Path:
    """Resolve *doc_path* against *vault_root*, handling shared-prefix duplication.

    ``doc_path`` may be absolute, vault-relative, or already a full path that
    starts with the vault's own prefix (so ``vault_root / doc_path`` would
    duplicate). Try vault-relative first, then the literal path (cwd-relative
    or absolute), preferring whichever exists.
    """
    p = Path(doc_path)
    if p.is_absolute():
        return p
    candidate = vault_root / doc_path
    if candidate.exists():
        return candidate
    if p.exists():
        return p
    # Fall back to the vault-relative candidate even when neither exists yet;
    # the caller will check existence before reading.
    return candidate


def _is_already_applied(
    proposal: MemoryProposal,
    vault_root: Path,
    guide_path: Path | None,
    project_doc_path: str = "",
) -> bool:
    """True when the destination already holds the proposal's fact.

    This is the extra check before creating a review card: if the chat already
    applied the edit (via Edit/Write/memory_update in-session), the post-archive
    insight will re-extract the same fact and must not re-queue it.
    """
    if proposal.target in ("memory", "profile") and guide_path is not None:
        try:
            if guide_path.exists() and _is_already_in_region(
                guide_path, proposal.target, proposal.text
            ):
                return True
        except Exception:
            pass
    if proposal.target == "project":
        doc_path = proposal.payload or project_doc_path
        if doc_path:
            p = _resolve_doc_path(vault_root, doc_path)
            if _is_already_in_file(p, proposal.text):
                return True
    if proposal.target == "people" and vault_root is not None:
        name = proposal.payload or _safe_name(proposal.text.split("-")[0])
        p = vault_root / _PEOPLE_DIR / f"{_safe_name(name)}.md"
        if p.exists() and _is_already_in_file(p, proposal.text):
            return True
        # Also check if any people note already contains the fact (payload may
        # differ from canonical filename due to model paraphrase).
        try:
            people_dir = vault_root / _PEOPLE_DIR
            if people_dir.is_dir():
                for note in people_dir.glob("*.md"):
                    if _is_already_in_file(note, proposal.text):
                        return True
        except OSError:
            pass
    if proposal.target == "learnings" and vault_root is not None:
        p = vault_root / _LEARNINGS_RELATIVE
        if p.exists() and _is_already_in_file(p, proposal.text):
            return True
    if proposal.target == "review":
        # Review has no known destination; suppress only if the fact is
        # demonstrably already remembered somewhere obvious (memory/profile
        # regions or the canonical doc). Do not scan the whole vault.
        if guide_path is not None and guide_path.exists():
            for region in ("memory", "profile"):
                if _is_already_in_region(guide_path, region, proposal.text):
                    return True
        if project_doc_path:
            p = _resolve_doc_path(vault_root, project_doc_path)
            if p.exists() and _is_already_in_file(p, proposal.text):
                return True
    return False


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
    region_decisions: dict[str, dict[str, Any]] | None = None,
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
            # The chat's resolved canonical doc is authoritative. Do not trust
            # a path guessed by the extraction model, since that could route a
            # fact into a different project.
            proposals = [
                p if p.target != "project"
                else MemoryProposal(
                    target=p.target,
                    text=p.text,
                    source_section=p.source_section,
                    payload=project_doc_path,
                )
                for p in proposals
            ]
        else:
            # A General chat has no project document to own a project-scoped
            # fact. Keep the claim reviewable, but never create an unroutable
            # project proposal with a missing or model-invented destination.
            proposals = [
                p if p.target != "project"
                else MemoryProposal(
                    target="review",
                    text=p.text,
                    source_section=p.source_section,
                )
                for p in proposals
            ]

        # Extra guard before creating a review card: if the chat already
        # applied the change in-session (via memory_update/Edit/Write), the
        # destination now contains the fact and the insight must not re-queue it.
        if proposals:
            filtered: list[MemoryProposal] = []
            suppressed = 0
            for _p in proposals:
                if _is_already_applied(
                    _p, workspace_vault_root, guide_path, project_doc_path
                ):
                    suppressed += 1
                    logger.info(
                        "memory proposals: suppressed already-applied %r from %s",
                        _p.text[:80],
                        archive_path.name,
                    )
                    continue
                filtered.append(_p)
            if suppressed:
                logger.info(
                    "memory proposals: suppressed %d already-applied fact(s) from %s",
                    suppressed,
                    archive_path.name,
                )
            proposals = filtered
            if not proposals:
                if stats is not None:
                    stats["proposed"] = 0
                    stats["promoted"] = stats.get("promoted", 0)
                return None

        if auto_promote_memory and proposals:
            proposals, promoted = apply_proposals(
                proposals,
                guide_path=guide_path,
                vault_root=workspace_vault_root,
                region_decisions=region_decisions,
                learning_source=archive_path.stem,
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


def find_proposal_matches(proposals_path: Path, needle: str) -> list[int]:
    """Line indices of the pending bullets ``needle`` matches.

    `remove_proposal_by_substring` returns None both for no match and for an
    ambiguous one, which a caller that wants to try a second needle cannot act
    on: retrying after an AMBIGUOUS match can uniquely hit a different row and
    delete the wrong proposal. Indices rather than a count, because two needle
    forms matching one row each is not the same as them matching the same row —
    only the identities say which.

    Same matching rule as the remover — casefolded substring over parsed
    bullets — so the two never disagree.
    """
    from ciao.proposal_kinds import parse_bullet

    if not proposals_path.exists():
        return []
    needle = needle.strip()
    if not needle:
        return []
    folded = needle.casefold()
    lines = proposals_path.read_text(encoding="utf-8").splitlines()
    return [
        index
        for index, line in enumerate(lines)
        if parse_bullet(line) is not None and folded in line.casefold()
    ]


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
    lines = _sweep_empty_batches(lines)
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
