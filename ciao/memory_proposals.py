"""The memory-proposal queue, the regions it promotes into, and the receipts
both leave behind.

A fact worth remembering reaches memory as a *proposal*: one bullet in
``<workspace-vault>/Workspace/Memory-Proposals.md``, tagged with where it
belongs. The destination vocabulary is :data:`DESTINATIONS`:

* ``[memory]``   — cross-project preference/environment/lesson → the
  ``ciao:memory`` region of the workspace ``AGENTS.md``.
* ``[profile]``  — identity/communication style → the ``ciao:profile`` region.
* ``[project]``  — true only within this project → the project's canonical
  doc, which :mod:`ciao.project_doc_update` folds.
* ``[people: <Name>]`` — durable fact about a person → the ``person``
  category's note, in the folder the vault's category registry names for it
  (``People/<Name>.md`` with the shipped categories).
* ``[learnings]`` — reusable how-to knowledge → ``Workspace/Learnings.md``.
* ``[review]``   — nobody was sure → waits for a human or the curator.

It also owns the one note-rewriting primitive the queue needs outside a region:
:func:`set_note_type`, which a `[category <id>]` accept uses to retype the notes
that were already filed under a spelling nobody had written down.

The one-shot archive-time producer of those bullets is gone (#627): the memory
pass, a chat of the app's own, now writes memory directly. What this module
owns is the half that outlives it — a person or the agent files a proposal by
hand (``ciao memory-proposal-add``, :func:`append_proposals`), the review
surface lists and dismisses them, and a user accepts one into its destination
(:func:`accept_region_fact`, :func:`reconcile_region_fact`). Every settled row
leaves a receipt, so the History tab can say who changed memory and from where.

The write is not a plain append. :func:`_promote_to_region` puts a fact
through the :mod:`ciao.memory_audit` event-shape guard first, records a
provenance row from :mod:`ciao.fact_candidates`, and copies any entry it
replaces into ``Workspace/Memory-Consolidations.md`` before that entry
disappears — nothing is dropped silently.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, date
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict

from ciao.learning_records import (
    LEARNINGS_RELATIVE,
    LEARNINGS_STUB,
    SECTION_ACTIVE,
    SECTION_HEADINGS,
    LearningObservation,
    LearningRecord,
    allocate_learning_id,
    learning_key,
    normalized_statement,
    observe_learning,
    parse_learnings,
    render_learning,
)


logger = logging.getLogger(__name__)


_PROPOSALS_RELATIVE = "Workspace/Memory-Proposals.md"

#: The category a ``[people]`` proposal's accept writes. ``people`` is the
#: queue's label for a fact about a person; ``person`` is the id that category
#: carries as a frontmatter ``type:`` and the folder it lives in. Named once so
#: the accept path and the writer cannot drift apart on the mapping between them.
PERSON_TYPE_ID = "person"


# ── Note types ─────────────────────────────────────────────────────────────
#
# The only note-rewriting the queue does, and it is deliberately narrower than
# `vault_migration._retype_frontmatter`: that one is the SAFE RENAME half of a
# migration, so it refuses a note with no frontmatter and a note whose `type:`
# line no longer says what it expected — both mean "not the note this pass
# planned for". A category accept is not that. It was decided against a cluster
# the owner was shown, and a note with no frontmatter at all is a perfectly
# ordinary member of one.
#
# So the baseline check lives in the CALLER, which knows the type the proposal
# was made against, and this function does the one thing it is for: set the
# `type:` to a given value, creating the block when there is none, and leave
# every other byte of the note alone.

# The opening fence, the block (which may be empty), and the closing fence WITH
# its line terminator. The block's own terminator is deliberately outside the
# capture so a retyped note is rebuilt from parsed lines with one join, and an
# empty block (``---\n---\n``) is a block rather than no block at all.
_FRONTMATTER_RE = re.compile(r"\A(---[ \t]*\r?\n)(.*?\r?\n?)(---[ \t]*(?:\r?\n|\Z))", re.DOTALL)


def read_note_type(path: Path) -> str:
    """The note's frontmatter ``type:``, or "" when it has none.

    "" covers both "no frontmatter" and "frontmatter with no ``type:`` line",
    because a category accept treats them the same way: a note that never said
    what it was is exactly the note a new category is being created for.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return ""
    for line in match.group(2).splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() == "type":
            return value.strip().strip("\"'")
    return ""


def set_note_type(path: Path, type_id: str) -> bool:
    """Set the note's frontmatter ``type:`` to *type_id*; report whether it took.

    Creates the frontmatter block when the note has none, rewrites only the
    ``type:`` line when it has one, and leaves every other key, their order, the
    body's bytes and the file's mode exactly as they were. A note already typed
    *type_id* is a no-op reported as success, so an accept retried against a
    half-finished cluster converges instead of failing.

    False means the note could not be read or written — never that the write was
    skipped for a judgement reason, so a caller can tell a refusal from a
    failure without re-reading the file.

    Atomic (a temp file beside the note plus one ``os.replace``) for the reason
    the queue is: every reader of the vault parses a note's frontmatter, and a
    truncated note is not "the old type" but an unparseable file that drops out
    of the index.
    """
    from ciao.memory_receipts import write_queue_atomically

    target = Path(path)
    if not type_id:
        return False
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        # No block at all: open one and put the type in it. The blank line after
        # the closing fence is the separator every note in the vault has, so a
        # retyped note reads like the ones around it.
        rewritten = f"---\ntype: {type_id}\n---\n\n{text}"
    else:
        opening, block, closing = match.group(1), match.group(2), match.group(3)
        eol = "\r\n" if opening.endswith("\r\n") else "\n"
        lines = block.splitlines()
        replaced = False
        for index, line in enumerate(lines):
            key, separator, value = line.partition(":")
            if not separator or key.strip() != "type":
                continue
            if value.strip().strip("\"'") == type_id:
                return True
            lines[index] = f"type: {type_id}"
            replaced = True
            break
        if not replaced:
            lines.insert(0, f"type: {type_id}")
        rewritten = (
            opening + eol.join(lines) + eol + closing + text[match.end() :]
        )
    if rewritten == text:
        return True
    try:
        write_queue_atomically(target, rewritten)
    except OSError:
        return False
    return True


# ── Typed decision statuses ───────────────────────────────────────────────
#
# The reconcile path used to speak in bare strings and a bare ``None``, and
# ``None`` carried two opposite meanings: "no reconcile was needed" and "the
# reconcile could not decide". Downstream they read the same, so a failed call
# took the plain append path and left an obsolete fact asserted beside its
# replacement in always-loaded memory. Every state the path can be in is named
# here instead, so the compiler — not a reader — checks that each one is
# handled.


ReconcileAction = Literal["add", "covered", "update", "defer"]
"""What the write-time reconcile decided about one candidate fact.

``add`` is new information (including the deterministic first entry into an
empty region), ``covered`` an exact or semantic duplicate, ``update`` a
validated supersession of exactly one existing entry, and ``defer`` every
uncertain state: a failed or unparseable reconciliation, a stale snapshot, a
row we could not read, and a fact the transcript does not support.
"""


class ReconcileDecision(TypedDict):
    """One candidate fact's decision row.

    ``index``/``text``/``old`` belong to ``update``: the 1-based entry to
    replace, the merged replacement, and the entry that index named *in the
    snapshot the model actually saw*, which the apply step re-checks against
    the live region before writing.

    ``reason`` and ``competing`` belong to ``defer``, and are the two things a
    deferral has to carry to be resolvable rather than merely safe: why this
    fact was not applied, and the region entries it may be in conflict with.
    ``evidence`` also belongs to ``defer``: the fact-candidate verdict row
    (:meth:`ciao.fact_candidates.Verdict.as_row`) behind an evidence refusal,
    which names *which* check said no rather than only that something did.
    """

    action: ReconcileAction
    index: NotRequired[int]
    text: NotRequired[str]
    old: NotRequired[str]
    reason: NotRequired[str]
    competing: NotRequired[list[str]]
    evidence: NotRequired[dict[str, Any]]


RegionDecisions = dict[str, ReconcileDecision]
"""``_decision_key`` (region + promotable fact text) → that fact's decision."""


PromotionOutcome = Literal[
    "written", "duplicate", "conflict", "failed", "unshaped", "deferred"
]
"""What happened to one region-bound fact.

``written`` and ``duplicate`` are settled — the fact is in the region either
way and leaves the queue. ``conflict`` (the destination moved under a
concurrent writer), ``failed`` (the write or its lock failed), ``unshaped``
(not state-shaped text) and ``deferred`` (nothing about this fact could be
trusted enough to write it unattended) all keep the fact queued.
"""


def _defer(
    reason: str,
    competing: list[str] | None = None,
    *,
    evidence: dict[str, Any] | None = None,
) -> ReconcileDecision:
    """A defer row carrying its reason and the entries it competes with.

    Every deferral is built here so none of them can be emitted bare: a queued
    fact with no reason is indistinguishable from an ordinary review row, and
    without the competing snapshot neither a human nor a retry knows what it
    was weighed against.

    ``evidence`` attaches the fact-candidate verdict row behind an evidence
    refusal, which carries the verdict code, the cited ids and the policy
    version alongside the human-readable reason.
    """
    row: ReconcileDecision = {"action": "defer", "reason": reason}
    if competing:
        # Capped: the snapshot is diagnostic, and a whole region in a log line
        # (or a run's extra) buries the reason it is attached to.
        row["competing"] = [_one_line(entry) for entry in competing[:5]]
    if evidence:
        row["evidence"] = evidence
    return row


# ── Destinations ──────────────────────────────────────────────────────────


DESTINATIONS: tuple[str, ...] = (
    "memory",
    "profile",
    "project",
    "people",
    "learnings",
    "review",
)
"""Destination vocabulary shared with the proposals queue and the stock memory
assets. A bullet tagged outside this set is treated as untagged and falls back
to section defaults."""

# Matches a trailing destination tag: ``[memory]``, ``[project]``,
# ``[people: Mo Salah]``. The colon-payload form is what the queue and the
# memory pass ask for; the queue-file form uses a space (``[people Mo Salah]``),
# which :mod:`ciao.proposal_kinds` owns.


def _one_line(value: str) -> str:
    """Collapse every whitespace run (newlines included) into a single space.

    The proposals queue is line-oriented Markdown: one bullet is one line. A
    field carrying an embedded newline would otherwise split into a truncated
    bullet plus a continuation the parser reads as its own spurious proposal,
    and the original value would never appear as one parsed bullet, so
    re-filing it would dodge the text dedupe.
    """
    return " ".join(value.split())


@dataclass(slots=True, frozen=True)
class MemoryProposal:
    """One proposed memory entry with its routing target."""

    target: str  # a DESTINATIONS member (legacy "user" normalized on write)
    text: str
    source_section: str
    payload: str = ""  # e.g. the person name for [people], doc path for [project]
    # Transcript message indices the bullet cited, as peeled from its
    # ``[idx=N]`` tag. Empty means the model cited nothing — which for a
    # region-bound fact is itself a reason to queue rather than auto-save.
    citations: tuple[int, ...] = ()
    # The user-request identifier a ``/remember`` of a lesson carries when the
    # sighting has no archived turn behind it. Rendered as its own
    # ``_(request: …)_`` tail rather than folded into ``source_section``, because
    # the two are different facts: one says where a transcript can be re-read,
    # the other says a person asked for this while typing. Folding them would
    # make a request id look like a chat id, which is the invented-citation
    # failure this field exists to remove. Only the ``[learnings]`` accept reads
    # it, and only to hand it to ``append_learning``.
    request: str = ""

    def as_bullet(self) -> str:
        # Deliberately total: an unknown target is written through rather than
        # raising, so one odd proposal cannot fail a whole archive batch.
        target = "profile" if self.target == "user" else self.target
        # Every field is forced onto one line and kept clear of the delimiter
        # that closes its own slot: a `]` inside the payload would end the
        # destination head early, and a `)` inside the source would break the
        # `_(from: ...)_` tail so the whole bullet stops parsing — invisible
        # to the review UI and to dedupe alike. The request tail has the same
        # `)` hazard, so it is cleared the same way.
        payload = _one_line(self.payload).replace("]", "")
        source = _one_line(self.source_section).replace(")", "")
        head = f"[{target} {payload}]" if payload else f"[{target}]"
        bullet = f"- {head} {_one_line(self.text)}  _(from: {source})_"
        if self.request.strip():
            request = _one_line(self.request).replace(")", "")
            bullet += f"  _(request: {request})_"
        return bullet


# ── Parsing ───────────────────────────────────────────────────────────────


def _split_sections(insights_md: str) -> dict[str, list[str]]:
    """Group bullet lines by their ``## Heading``.

    Strips bullet markers only. Citation tags — ``[idx=12]`` and the
    multi-index ``[idx=12,34]`` shape models improvise — and destination tags
    both survive here; they are split later, per bullet, by the accept path,
    where the routing decision happens.
    Stripping the citation at this stage destroyed the one piece of evidence
    that ties a fact to a real turn before anything could check it. Empty
    sections are dropped.
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
        if text:
            sections[current].append(text)
    return sections


# ── Promotion ─────────────────────────────────────────────────────────────


# A bullet states the standing preference a correction implies as a trailing
# "Durable rule: <...>" sentence. That clause — not the "User said X ->
# assistant did Y" event around it — is what belongs in a region: the regions
# are a state surface, and memory_audit flags the event shape as rot for the
# nightly curator to remove.
#
# The regex is built from the label, so a producer and this consumer cannot
# drift apart silently. Case-sensitive and anchored to a sentence start so a
# chat fragment quoted inside the bullet ("... as a durable rule: ...") never
# matches.
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


def _provenance_row(proposal: MemoryProposal) -> dict[str, Any]:
    """The evidence chain stamped onto a region write's receipt.

    Records where the fact came from and under which rules it was admitted:
    the transcript turns it cited, the insights section it was extracted
    from, its temporal bounds, and the extraction/policy versions in force.
    A bullet that cited nothing is recorded as ``provenance: "unknown"``
    rather than being given a plausible id — the receipt has to be able to say
    "this archive never said where this came from", which is a different fact
    from "it came from turn 3".
    """
    from ciao.fact_candidates import candidate_from_proposal

    candidate = candidate_from_proposal(proposal)
    return {
        "schema": candidate.schema,
        "source_message_ids": list(candidate.source_message_ids),
        "provenance": candidate.provenance,
        "section": candidate.section,
        "as_of": candidate.as_of,
        "expires": candidate.expires,
        "extraction_version": candidate.extraction_version,
        "policy_version": candidate.policy_version,
    }


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
            encoding="utf-8", newline="",
        )
    heading = f"\n## {date.today().isoformat()} — ciao:{region} ({label})\n"
    with path.open("a", encoding="utf-8", newline="") as f:
        f.write(heading + f"- {_one_line(old_entry)}\n")


def _promote_to_region(
    proposal: MemoryProposal,
    guide_path: Path,
    *,
    vault_root: Path | None = None,
    decision: ReconcileDecision | None = None,
    actor: str = "agent",
    source: str = "archive",
    workspace: str = "",
    deferral_out: list[ReconcileDecision] | None = None,
    receipt_out: dict[str, Any] | None = None,
) -> tuple[PromotionOutcome, str | None]:
    """Write one region-bound proposal.

    Returns ``(outcome, promotable_or_None)``; see :data:`PromotionOutcome`
    for what each status means and which of them leave the fact queued.

    ``deferral_out``, when given, receives the defer row behind a ``deferred``
    outcome — the one the caller handed in, or the one built here for a stale
    or unusable update. Deferring is only half the fix; a caller that cannot
    say why a fact was queued, or against which entries, has moved the problem
    rather than solved it.

    Fail-safe by construction: the guide lock is *required*, not best-effort.
    An earlier version caught a lock failure and proceeded with ``lock=None``,
    which reintroduced exactly the lost-update race the lock exists to prevent
    — two concurrent accepts both read, both append to their own snapshot, and
    the second write silently drops the first fact while its row is dismissed
    as promoted. A lock that cannot be taken now returns ``"failed"`` with zero
    guide writes and the fact left pending.

    Every write goes through :func:`ciao.memory_receipts.commit_region_change`,
    which records a stable-id receipt (actor/source, expected revision,
    before/after images, prepared/applied status) and re-checks the revision
    under the lock before replacing the file, so an external direct edit is
    reported as a conflict rather than overwritten.

    ``decision`` is this fact's row from :func:`reconcile_region_fact`, when
    the caller ran one: ``{"action": "covered"}`` drops the fact as already
    remembered, ``{"action": "update", "index": N, "text": ...}`` replaces
    entry ``N`` (1-based) with the merged text — the replaced entry goes to
    the consolidations undo log first — ``{"action": "defer", "reason": ...}``
    routes the fact to the queue, and ``{"action": "add"}`` or ``None`` is the
    plain append path.

    ``receipt_out`` is an optional caller-owned dict this fills with the
    receipt ``commit_region_change`` recorded, when a write actually happened.
    It is an out-parameter rather than a third return value on purpose: the
    only caller that needs the receipt is the PWA accept. The
    decision ledger records the ORIGINAL bullet — that is what append-time
    dedupe compares a re-extracted fact against — so an edited accept's ledger
    row cannot be matched back to its receipt by text. This is how the row gets
    the reference instead of guessing at it.
    """
    from ciao.memory_receipts import (
        RevisionConflict,
        commit_region_change,
        content_revision,
    )
    from ciao.memory_tool import (
        MemoryLockError,
        ensure_regions,
        guide_lock,
        read_region,
        release_guide_lock,
        resolve_region,
        serialize_entries,
    )

    promotable = _promotable_text(proposal.text)
    if promotable is None:
        return "unshaped", None
    from ciao.memory_audit import strip_learned_stamp

    # The whole read-merge-write, under the same lock `update_region` takes.
    # A lock we cannot take is a hard, retryable failure: never fall through to
    # an unlocked write.
    try:
        lock = guide_lock(guide_path)
    except MemoryLockError as exc:
        logger.info(
            "memory apply: guide lock unavailable, fact stays queued (%s)", exc
        )
        return "failed", promotable
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
        # Pin the exact body this merge was computed against. `commit_region_change`
        # re-reads under its lock and refuses on any drift, so a competing
        # managed write that landed between this read and the commit is a
        # conflict — the fact stays queued — instead of being overwritten.
        expected_revision = content_revision(serialize_entries(entries))
        # Compared with stamps stripped: the same fact promoted on two
        # different days is still the same fact.
        if promotable in {strip_learned_stamp(entry) for entry in entries}:
            logger.info(
                "memory apply: dropped exact duplicate %r",
                promotable[:80],
            )
            return "duplicate", promotable

        row: ReconcileDecision = decision or {"action": "add"}
        action: ReconcileAction = row.get("action") or "add"
        if action == "defer":
            # Reconcile ran against a non-empty region and came back with
            # nothing usable for this fact, so nobody knows whether it
            # supersedes an entry already there. Appending anyway is what put
            # "Insights model is deepseek-flash" and "Insights model is sonnet"
            # in the same always-loaded region; the queue holds the text
            # instead, so the fact is preserved without asserting itself.
            logger.info(
                "memory apply: deferring %r to the queue (%s)",
                promotable[:80],
                row.get("reason") or "uncertain reconcile",
            )
            if deferral_out is not None:
                deferral_out.append(row)
            return "deferred", promotable
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
        if action == "update":
            if vault_root is None:
                # The replaced entry is copied to the consolidations undo log
                # before it disappears, and there is nowhere to write that. The
                # update cannot run, and the fact the model read as superseding
                # an existing entry must not be appended next to it, so it
                # waits in the queue.
                logger.info(
                    "memory apply: deferring %r to the queue (no vault for the "
                    "consolidations undo log)",
                    promotable[:80],
                )
                if deferral_out is not None:
                    deferral_out.append(
                        _defer(
                            "no vault for the consolidations undo log",
                            entries,
                        )
                    )
                return "deferred", promotable
            index = row.get("index")
            merged = str(row.get("text", "")).strip()
            plan_old = str(row.get("old", ""))
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
                # The model is told to keep every still-true part of the old
                # entry, and the numbered list it reads carries that entry's
                # learned-at stamp, so a compliant merge often echoes it back.
                # Stamping on top of it produced `... [2026-01-01] [2026-09-02]`,
                # and `_LEARNED_STAMP_RE` is `$`-anchored, so stripping such an
                # entry never yields the fact's own text again — the duplicate
                # guard above stops recognising it and the fact gets appended a
                # second time on the next archive pass.
                updated[index - 1] = (
                    f"{strip_learned_stamp(merged)} [{date.today().isoformat()}]"
                )
                _log_consolidation(vault_root, region, old)
                receipt = commit_region_change(
                    guide_path,
                    region,
                    entries=updated,
                    actor=actor,
                    source=source,
                    workspace=workspace,
                    vault_root=vault_root,
                    lock=lock,
                    expected_revision=expected_revision,
                    fact_text=promotable,
                    destination=f"ciao:{region}",
                    removed_texts=[old],
                    kind="region_update",
                    provenance=_provenance_row(proposal),
                )
                if receipt_out is not None:
                    receipt_out.update(receipt)
                logger.info(
                    "memory apply: reconciled update of entry %d in ciao:%s",
                    index,
                    region,
                )
                return "written", promotable
            # The update named an entry we cannot safely replace — out of
            # range, empty or event-shaped merge text, or an entry that changed
            # during the up-to-two-minute model call. Appending instead used to
            # leave the superseded entry and its replacement both live in the
            # region every session loads; neither is dropped, the fact goes to
            # the queue for a human to resolve against the current region.
            stale = bool(
                isinstance(index, int)
                and 1 <= index <= len(entries)
                and plan_old
                and strip_learned_stamp(entries[index - 1])
                != strip_learned_stamp(plan_old)
            )
            reason = (
                f"the entry this update named (ciao:{region} #{index}) changed "
                "while the reconcile was running"
                if stale
                else f"unusable update of entry {index!r} in ciao:{region}"
            )
            logger.info(
                "memory apply: deferring %r to the queue (%s)",
                promotable[:80],
                reason,
            )
            if deferral_out is not None:
                competing = (
                    [entries[index - 1]]
                    if isinstance(index, int) and 1 <= index <= len(entries)
                    else entries
                )
                deferral_out.append(_defer(reason, competing))
            return "deferred", promotable
        # The learned-at stamp is system time — when this fact entered the
        # region — read by the aging audit so unverified old facts surface
        # for re-verification instead of asserting themselves forever.
        stamped = f"{promotable} [{date.today().isoformat()}]"
        receipt = commit_region_change(
            guide_path,
            region,
            entries=entries + [stamped],
            actor=actor,
            source=source,
            workspace=workspace,
            vault_root=vault_root,
            lock=lock,
            expected_revision=expected_revision,
            fact_text=promotable,
            destination=f"ciao:{region}",
            kind="region_apply",
            provenance=_provenance_row(proposal),
        )
        if receipt_out is not None:
            receipt_out.update(receipt)
        return "written", promotable
    except RevisionConflict as exc:
        logger.info("memory apply: destination changed, fact stays queued (%s)", exc)
        return "conflict", promotable
    except MemoryLockError as exc:
        logger.info("memory apply: lost the guide lock, fact stays queued (%s)", exc)
        return "failed", promotable
    except Exception as exc:  # noqa: BLE001 — one bad write must not stop a batch
        logger.info("memory apply: falling back to proposals (%s)", exc)
        return "failed", promotable
    finally:
        release_guide_lock(lock)



def accept_region_fact(
    *,
    guide_path: Path,
    target: str,
    text: str,
    vault_root: Path | None,
    decision: ReconcileDecision | None = None,
    actor: str = "operator",
    source: str = "pwa",
    workspace: str = "",
    deferral_out: list[ReconcileDecision] | None = None,
    receipt_out: dict[str, Any] | None = None,
) -> tuple[PromotionOutcome, str | None]:
    """Write one approved region fact through the guarded path.

    The UI accept button used to call ``update_region(action="add")`` directly,
    which skipped everything this path does: the event-shape guard (so an
    event-shaped bullet landed verbatim in always-loaded context), the
    stamp-stripped duplicate check, the learned-at stamp the aging audit reads,
    and the consolidations undo log.

    Deliberately synchronous and model-free: one ``run_oneshot`` per row on a
    click is a 120s timeout each, and the batch endpoint accepts rows
    sequentially inside one request. Reconciliation is offered alongside it
    rather than inside it — :func:`reconcile_region_fact` runs one fresh call
    against the *current* region and hands the result in as ``decision``, which
    is how a fact a previous reconcile could not decide gets resolved on a
    retry. A caller that passes none takes the plain append path.

    Returns ``_promote_to_region``'s ``(outcome, promotable)``. Both
    out-parameters are forwarded unchanged. ``deferral_out`` lets an
    interactive accept report *why* its fact stayed queued and against which
    entries — a review UI that can only say "refused" gives the person no way
    to judge whether another retry is worth a second model call. Pass
    ``receipt_out`` to also learn which receipt performed the write: the
    caller records the decision under the bullet's ORIGINAL text, so an
    accept of an edited wording has no way to find its own receipt again.
    """
    proposal = MemoryProposal(target=target, text=text, source_section="review")
    return _promote_to_region(
        proposal,
        guide_path,
        vault_root=vault_root,
        decision=decision,
        actor=actor,
        source=source,
        workspace=workspace,
        deferral_out=deferral_out,
        receipt_out=receipt_out,
    )


def _safe_name(name: str) -> str:
    """A proposal payload as a filename stem, without path separators."""
    cleaned = re.sub(r"[\\/:*?\"<>|]+", " ", name).strip().rstrip(".")
    return cleaned[:80]


EntityNoteOutcome = Literal["written", "exists", "refused"]
"""What one entity-note write did.

``written`` is the stub on disk. ``exists`` means a note is already there, so
the fact needs a *merge* into it rather than a create — the review queue folds
that with a model and reports no change when the note already covers the fact.
``refused`` is a routing failure with nothing written: a ``type_id`` the vault
has no category for, a category the owner disabled, a category with no folder to
write into, a folder that would put the note outside the vault, or a name that
cannot be a filename.
"""


def _entity_folder(registry_root: Path, type_id: str) -> str | None:
    """The folder *type_id*'s notes live in, or None when it takes no notes.

    Read from the category registry rather than from a constant here, which is
    what gives a category the owner added a writer at all. A disabled entry is
    refused with the rest: it is out of every derived view on purpose, so it is
    not a ``type:`` a new note should carry.

    *registry_root* is the vault that holds ``entity-types.yaml`` — the agent
    vault root, which is not the notes root the note itself lands in. See
    :func:`entity_note_path`, which takes both.
    """
    from ciao.entity_types import load_entity_types

    entry = load_entity_types(registry_root).get(type_id)
    if entry is None or not entry.enabled or not entry.folder:
        return None
    return entry.folder


def _inside(root: Path, candidate: Path) -> bool:
    """Whether *candidate* stays under *root*, without touching the disk.

    Lexical, because that is the comparison the caller makes: the destination is
    rendered with ``relative_to``, a parts comparison, so a resolved answer here
    could disagree with the path the operator is shown (a vault reached through
    a symlink — ``/var`` on macOS — resolves to a different string than the root
    it was joined to). It is also how a parts comparison reads: ``relative_to``
    matches a prefix, so ``vault/../elsewhere/Mo.md`` looks like a path *inside*
    ``vault`` with a ``..`` on the end rather than one outside it.

    A ``..`` is refused rather than collapsed, because collapsing it means
    resolving, and a category folder is one folder name, not a path expression.
    """
    root_parts = root.parts
    parts = candidate.parts
    if parts[: len(root_parts)] != root_parts:
        return False
    return ".." not in parts[len(root_parts):]


def entity_note_path(
    vault_root: Path, type_id: str, name: str, *, registry_root: Path
) -> Path | None:
    """Where a note in category *type_id* would be written, or ``None``.

    Two roots, and neither one implies the other. *vault_root* is where the note
    is written: a workspace's **notes** root, which is where that person's other
    notes already are. *registry_root* is where the category is looked up: the
    vault holding ``entity-types.yaml``, i.e. the **agent** vault root, the one
    that owns ``VOCABULARY.md`` and the file
    ``GET``/``PATCH /api/memory/entity-types`` reads and writes. They are the
    same directory only on a re-rooted install, so a caller that passes one for
    both is how an owner's category edit came to be invisible to the very accept
    meant to honour it.

    None covers the ways there is no such note: the category takes no notes
    (:func:`_entity_folder`), the name is not usable as a filename, or the
    category's folder would put the note outside the vault. That last one is a
    value the owner typed — an absolute folder, or one spelled with ``..`` — and
    it is refused with the rest rather than acted on: the note would be written
    outside the vault, and the caller's ``relative_to`` would raise instead of
    reporting it. The category editor validates the list before it stores it;
    a hand-edited ``entity-types.yaml`` is the reachable case.
    Public so the review queue can name the destination — and say whether the
    note already exists — before the accept runs, without a second copy of the
    rules :func:`write_entity_note` applies.
    """
    folder = _entity_folder(registry_root, type_id)
    stem = _safe_name(name)
    if folder is None or not stem:
        return None
    note = vault_root / folder / f"{stem}.md"
    if not _inside(vault_root, note):
        return None
    return note


def render_entity_note(type_id: str, name: str, text: str, *, today: str = "") -> str:
    """The note body :func:`write_entity_note` would write, without writing it.

    Split out for the reason :func:`render_learning_append` is: the review card
    shows the exact replacement before the accept performs it, so the preview and
    the write cannot disagree about what lands.

    ``updated:`` records the note's creation as its first verification, so an
    entity the system stopped hearing about ages out visibly instead of relying
    on mtime (which file copies and migrations reset silently).
    """
    return (
        "---\n"
        f"type: {type_id}\n"
        f"updated: {today or date.today().isoformat()}\n"
        f"tags: [{type_id}]\n"
        f"---\n# {_safe_name(name)}\n\n{text}\n"
    )


def write_entity_note(
    vault_root: Path, type_id: str, name: str, text: str, *, registry_root: Path
) -> EntityNoteOutcome:
    """Create a stub note for one entity, typed as *type_id* in its own folder.

    Public because accepting a ``[people]`` proposal from the review queue
    performs exactly this write, for :data:`PERSON_TYPE_ID`. Typed and
    registry-routed rather than hardcoded to ``People/``, so a category the owner
    added (``customer`` → ``Customers/``) has a writer and every note written
    here carries a ``type:`` the linter accepts — the missing ``type:`` on a
    person note is what left an accepted person note untyped and lint-flagged.
    The two roots are the ones :func:`entity_note_path` names: the note lands
    under the notes root, the category is read from the root that holds the
    registry.

    Never overwrites: an existing note is reported as ``exists`` so the caller
    merges into it.
    """
    path = entity_note_path(vault_root, type_id, name, registry_root=registry_root)
    if path is None:
        return "refused"
    if path.exists():
        return "exists"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_entity_note(type_id, name, text), encoding="utf-8", newline="")
    return "written"


# One structured learning line, and the only writer of one. The shape is a
# contract: the Workspace care schedule prompt reads the recurrence count to
# decide promotion (N ≥ 3) and the sources to cite episodes, so recurrence
# bookkeeping is mechanical instead of prose.
#
# What the line *is* — the key, the `xN` baseline, the machine-readable
# `<!-- ciao:learning {...} -->` comment carrying the identity and the
# deduplicated observations — belongs to `ciao.learning_records`. The old private
# regex, key, normalizer and line formatter that used to live here are deleted:
# they minted a fresh identity from the prose on every read, so a reworded
# statement became a different learning, a replay of the same source bumped the
# count anyway, and the same fact under `## Promoted / Resolved` was reactivated
# as if it were new. This module now decides *which* entry a sighting belongs to;
# the model owns what the entry is and how it is written.


def learnings_path(vault_root: Path) -> Path:
    """Where a ``[learnings]`` accept writes.

    The path and the stub a first write starts from both come from
    :mod:`ciao.learning_records`: the parser, the writer and the migration
    command all have to agree on them, and a stub defined twice is two answers
    to "what does an empty ``Learnings.md`` say".
    """
    return vault_root / LEARNINGS_RELATIVE


def _new_learning(
    text: str, *, workspace: str, stamp: date, observation: LearningObservation
) -> LearningRecord:
    """The record for a learning filed for the first time.

    ``count=1`` and today's date are facts about *this write*, not guesses: the
    file is being created because something was observed now, and the writer
    witnessed it. That is the one place an `x1` is honest — a record *read* off
    an existing line never gets one, because nobody witnessed its history.

    The only field that could be invented is the source, and a source-less
    sighting is not recorded at all — `observe_learning` refuses one, so the line
    renders ``(x1)`` with no citation rather than a chat id nobody supplied. The
    identity is derived from the workspace and the statement, so a preview and
    the accept it precedes mint the same one.
    """
    return LearningRecord(
        learning_id=allocate_learning_id(workspace, text),
        key=learning_key(text),
        text=text,
        first_seen=stamp,
        last_seen=stamp,
        count=1,
        observations=(observation,) if observation.identity else (),
    )


def render_learning_append(
    existing: str,
    text: str,
    *,
    workspace: str,
    source: str = "",
    request: str = "",
    today: str = "",
) -> tuple[str, str]:
    """The file ``append_learning`` would write, and which operation that is.

    Returns ``(updated_text, operation)`` — ``"add"`` for a new Active entry,
    ``"update"`` when an existing entry records a sighting it did not have, and
    ``"none"`` when the observation is one the file already accounts for and
    nothing is written (``updated_text`` is then ``existing``).

    Split out of :func:`append_learning` so the review queue can show the exact
    replacement *before* the accept performs it. The write path goes through
    this same function, so a preview and the accept it precedes cannot
    disagree about what lands.

    ``source`` and ``request`` are the two ways a sighting identifies where it
    was seen, and they are not interchangeable. ``source`` is a chat or archive:
    a stored transcript somebody can re-read. ``request`` is a user-request
    identifier, for the ``/remember`` that has no archived turn behind it —
    a lesson the user asked to be kept while they were typing it. The second
    exists because the alternative was manufacturing an archive, and a
    manufactured turn is a citation to a conversation that never happened.
    A ``/remember`` inside an archived chat may carry both; the citation shows
    the archive and the dedupe keys on the request, which is the narrower
    identity.

    Five cases, and the section an entry sits in decides the first two:

    * **Active, sighting not already recorded** — the observation is folded in
      and the entry is re-rendered in place, dates and count refreshed.
    * **Promoted / Resolved, sighting not already recorded** — the observation is
      recorded and the line re-rendered *where it already is*. The section does
      not move and the entry is not reactivated: a lesson that was promoted or
      resolved stays decided, and a later sighting of the same statement is
      evidence about a decision, not a reason to reopen it.
    * **Sighting that adds no evidence** — the file does not change, whatever
      the entry looks like. `observe_learning` returns the record untouched for
      two reasons: the source was already counted (a retry re-quoting an episode,
      which is the whole recurrence contract — it cannot inflate a count), or
      there was no source to count at all, which nothing can tell apart from a new
      sighting and is refused. This is also what leaves a legacy plain bullet
      alone: a line the owner wrote is not rewritten into a canonical one on the
      strength of a write that recorded nothing.
    * **Unreadable entry** — a line the parser could not read is not a learning
      to match against, and it is never rewritten. It is kept byte-exact, so a
      shape this code does not implement survives the write untouched rather than
      being guessed at.
    * **Not found** — a new Active entry is filed directly under the
      ``## Active`` heading, creating the section when the file has none. The
      heading is matched as a line and the inserted entry uses the file's own
      line ending, so a CRLF document does not gain a second ``## Active`` at the
      end of the file or a stray LF in a CRLF list.

    A legacy plain bullet *is* found: the model reads it as a record with no
    dates and no count, so a sighting of that statement with a source updates it
    in place and renders it canonically. What it never gets is an invented
    `x1` — a plain bullet has no recurrence history, and the count stays unknown
    until evidence accumulates.

    ``workspace`` scopes the identity a new entry is minted with, and is
    required rather than derived: the caller that renders a preview and the
    caller that performs the write both have the vault in hand, and a default
    here would be a way for the two to mint different identifiers for the same
    statement and disagree about what lands.
    """
    observation = LearningObservation(source=source, request=request)
    stamp = date.fromisoformat(today) if today else date.today()
    statement = _one_line(text)
    wanted = normalized_statement(statement)
    document = parse_learnings(existing, workspace=workspace)

    for entry in document.entries:
        record = entry.record
        if record is None or normalized_statement(record.text) != wanted:
            continue
        updated = observe_learning(record, observation, today=stamp)
        if updated == record:
            return existing, "none"
        rendered = render_learning(updated)
        return existing[: entry.start] + rendered + existing[entry.end :], "update"

    filed = render_learning(
        _new_learning(
            statement, workspace=workspace, stamp=stamp, observation=observation
        )
    )
    return _file_new_entry(existing, filed), "add"


def _newline(existing: str) -> str:
    """The line ending the document already uses.

    A CRLF file that gains ``\\n`` lines is a file this write has partially
    rewritten, which is the same defect as translating the whole thing: mixed
    endings render inconsistently, and every line ending after the insertion is
    now a byte the owner did not write. Read off the document's *first* line
    terminator, so a file whose endings already disagree keeps whatever its
    first line says rather than acquiring a rule from here.
    """
    first = existing.find("\n")
    return "\r\n" if first > 0 and existing[first - 1] == "\r" else "\n"


_ACTIVE_HEADING_RE = re.compile(
    rf"(?m)^#{{1,3}}[ \t]+{re.escape(SECTION_HEADINGS[SECTION_ACTIVE])}[ \t]*"
    r"(?P<eol>\r\n|\n|\Z)"
)
"""The ``## Active`` heading, *including* its line terminator.

Three things this has to get right, each of which was got wrong before.

**The terminator is part of the match.** Ending the pattern at ``\\r?$`` put
``match.end()`` on the ``\\n`` of a CRLF line, so inserting "one newline past
the heading" skipped the ``\\n`` *and* the first byte of the next line: the new
entry began with the existing entry's ``-`` and the owner's line lost its own
bullet. Consuming ``\\r\\n`` or ``\\n`` explicitly means ``match.end()`` is already
the offset to insert at, with no arithmetic to get wrong.

**The heading is matched whole.** The terminator alternation only matches where
the line actually ends, so ``## Active extras and notes`` is not this section and
does not get an entry filed into it. The ``[ \\t]*`` before it tolerates the
trailing whitespace a hand edit leaves behind, which would otherwise be a second
reason for the same heading not to be found.

**``\\Z`` is the unterminated case.** A heading on the final line with no newline
has nothing to insert after, and the entry has to be given a line of its own.
The terminator is a named group so the caller can tell the two apart by asking
the match, rather than by inspecting the two characters before its end — which
is a test that reads one way on an LF file and another on a CRLF one.

Not the literal ``\\n## Active\\n`` this replaced either: that never matched a
CRLF file, so a new entry was filed under a second ``## Active`` heading at the
end of the document. Matched at any level up to three because the model treats a
deeper heading as a group inside the section, and a file that has only ever used
``### Active`` still means the same thing. The heading text is the model's, so
the section the writer opens is the section the parser recognizes.
"""


def _file_new_entry(existing: str, filed: str) -> str:
    """*existing* with *filed* placed directly under its ``## Active`` section.

    The heading's terminator is already consumed by the match, so the insertion
    point is the end of the match and nothing has to be added to it. A heading
    that consumed no terminator — it was the last line and the file does not end
    in a newline — gets one supplied, or the entry would share its line and
    ``## Active- [key] …`` would be neither a heading nor an entry.

    A file with no such heading has the section opened after its last line of
    content, which is where the writer has always put one — a stub that has
    never been written to needs the heading before the entry can be under it.
    """
    newline = _newline(existing)
    heading = _ACTIVE_HEADING_RE.search(existing)
    if heading is None:
        title = SECTION_HEADINGS[SECTION_ACTIVE]
        return f"{existing.rstrip()}{newline * 2}## {title}{newline * 2}{filed}{newline}"
    # `\Z` is zero-width, so an empty group means the heading consumed no
    # terminator: it was the file's last line, and one has to be supplied.
    consumed_eol = heading.group("eol") != ""
    at = heading.end()
    return f"{existing[:at]}{'' if consumed_eol else newline}{filed}{newline}{existing[at:]}"


def read_learnings(vault_root: Path) -> str:
    """The current Learnings file, or the stub a first write would start from.

    Existence, not a swallowed read error, decides: a file that is there but
    unreadable must surface rather than be silently replaced by the stub,
    which a following write would then persist over the real content.

    Decoded from bytes rather than with ``read_text``, which translates newlines:
    a CRLF file would come back all-LF, and the preview the review card shows
    would then be of a different document from the one
    :func:`append_learning` writes. The preview is only worth showing if it is
    what lands.
    """
    path = learnings_path(vault_root)
    if path.exists():
        return path.read_bytes().decode("utf-8")
    return LEARNINGS_STUB


def append_learning(
    vault_root: Path,
    text: str,
    *,
    workspace: str,
    source: str = "",
    request: str = "",
) -> bool:
    """File one learning under the Active section of Workspace/Learnings.md.

    Writes through the canonical model: :mod:`ciao.learning_records` parses the
    document, :func:`observe_learning` decides whether this sighting is new
    evidence or a replay of one already counted, and :func:`render_learning`
    writes the single line. The contract that follows from that is the one the
    Workspace care schedule prompt promotes on — a learning recurs because it was
    observed again, counted mechanically rather than judged from prose.

    The read, the render and the write all happen under
    :func:`ciao.memory_receipts.queue_lock`, and the write goes through
    :func:`ciao.memory_receipts.write_queue_atomically`. Both are the same ones
    ``ciao learnings-migrate`` takes on this file, and both are load-bearing: the
    unattended care run, a ``[learnings]`` accept and the migration are three
    writers of one queue-shaped file, and a read-check-write that is not
    serialized is a read-check-write that races. The atomic rename is what makes
    a crash mid-write leave the old file rather than a truncated one. And the
    read is from bytes, because a CRLF file read through a translating API comes
    back rewritten in every line the writer did not mean to touch — which also
    invalidates the migration's ``--revert`` receipt, whose spans address exact
    offsets.

    Public because accepting a ``[learnings]`` proposal from the review queue
    performs exactly this write.

    ``request`` is the ``/remember`` provenance (see
    :func:`render_learning_append`): a user-request identifier for a sighting
    that has no archived turn behind it. It is passed straight into the
    :class:`~ciao.learning_records.LearningObservation`, so the line renders a
    ``req:`` citation rather than an invented chat id.

    ``workspace`` is the *registered* workspace name — the one the registry
    knows this vault by, not the vault directory's own name — and it is the
    scope a new entry's identity is minted under, so the id this write records
    is the one every later reader resolves it by.
    """
    from ciao.memory_receipts import queue_lock, write_queue_atomically

    path = learnings_path(vault_root)
    with queue_lock(path):
        # Read inside the lock, not before it: read-then-lock would leave a
        # window in which the text being replaced is not the text on disk.
        existing = (
            path.read_bytes().decode("utf-8") if path.exists() else LEARNINGS_STUB
        )
        updated, operation = render_learning_append(
            existing,
            text,
            source=source,
            request=request,
            workspace=workspace,
        )
        if operation == "none":
            return True
        write_queue_atomically(path, updated)
    return True


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


def _parse_reconcile_reply(raw: str, count: int) -> list[ReconcileDecision] | None:
    """Parse the model's JSON array; None when the shape is unusable.

    Per-row problems degrade that row to ``{"action": "defer"}`` — the
    candidate is compared against a non-empty region, so a row we cannot read
    says nothing about whether the fact supersedes an entry already there, and
    appending it regardless is how both ended up asserted at once. A reply that
    is not a JSON array of the right length is discarded whole rather than
    guessed at, and the caller defers every candidate in that batch.
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
    rows: list[ReconcileDecision] = []
    for item in data:
        if not isinstance(item, dict):
            rows.append(_defer("unreadable reconcile row"))
            continue
        action = str(item.get("action", "")).lower()
        if action == "update":
            index = item.get("index")
            merged = str(item.get("text", "")).strip()
            if isinstance(index, bool) or not isinstance(index, int) or not merged:
                rows.append(_defer("update row missing index or text"))
                continue
            rows.append({"action": "update", "index": index, "text": merged})
        elif action == "covered":
            rows.append({"action": "covered"})
        elif action == "add":
            rows.append({"action": "add"})
        else:
            rows.append(_defer("unreadable reconcile row"))
    return rows


async def _reconcile_region(
    region_name: str,
    entries: list[str],
    candidates: list[str],
    *,
    model: str,
    provider: str = "claude",
    cwd: Path | None = None,
    timeout_s: float = _RECONCILE_TIMEOUT_S,
) -> list[ReconcileDecision] | None:
    """One reconcile call: decide ADD / UPDATE / COVERED per candidate.

    Returns one row per candidate in the given order, or None when the call
    failed or replied unparseably — :func:`reconcile_region_fact` then defers
    the fact to the proposals queue, which never blocks and never loses it.

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
            timeout_s=timeout_s,
            provider=provider,
            cwd=cwd,
        )
    except Exception as exc:  # noqa: BLE001 — defer rather than append blind
        logger.info("memory reconcile: call failed (%s); deferring to review", exc)
        return None
    rows = _parse_reconcile_reply(reply, len(candidates))
    if rows is None:
        logger.info(
            "memory reconcile: unparseable reply for ciao:%s; deferring to review",
            region_name,
        )
        return None
    out: list[ReconcileDecision] = []
    for row in rows:
        if row.get("action") == "update":
            index = row.get("index")
            if not (isinstance(index, int) and 1 <= index <= len(entries)):
                # Out-of-range against the very snapshot the model saw, so the
                # decision is junk — but it still says this fact supersedes
                # something in the region, which is the one case a plain append
                # must not take.
                out.append(
                    _defer("update index outside the region snapshot", entries)
                )
                continue
            out.append(
                {
                    "action": "update",
                    "index": index,
                    "text": row.get("text", ""),
                    "old": entries[index - 1],
                }
            )
            continue
        if row.get("action") == "defer" and not row.get("competing"):
            # A row the parser could not read still competes with the whole
            # region it was weighed against; without that snapshot the queued
            # fact says only "something went wrong".
            out.append(_defer(row.get("reason") or "unreadable reconcile row", entries))
            continue
        out.append(row)
    return out


# The review path runs one call for one fact on a click, so it cannot wait the
# archive path's two minutes: the operator is watching a spinner, and the whole
# point of the retry is that they can take the deterministic append instead.
_REVIEW_RECONCILE_TIMEOUT_S = 45.0


async def reconcile_region_fact(
    guide_path: Path,
    target: str,
    text: str,
    *,
    model: str,
    provider: str = "claude",
    cwd: Path | None = None,
    timeout_s: float = _REVIEW_RECONCILE_TIMEOUT_S,
) -> ReconcileDecision | None:
    """Reconcile one queued fact against the region's *current* entries.

    This is the retry half of the deferral: a fact queued because a reconcile
    timed out, replied unusably, or named an entry that had moved is not stuck
    there — a later attempt reads the region as it is
    now and can come back with a usable ``add``/``covered``/``update``. The
    decision is planned against the snapshot read here and applied by
    :func:`_promote_to_region`, which re-reads under the guide lock and refuses
    on any drift, so the retry can only ever land against a region that has not
    changed since it was planned.

    ``None`` means "no model call was needed": an empty region (the first entry
    is a plain add), an exact duplicate (already remembered), or text the shape
    guard will reject anyway. Those are the deterministic paths, and spending a
    model call on them is the cost this deliberately avoids.

    A call that fails or replies unusably comes back as a ``defer`` row with
    its reason and the competing entries: a retry
    that cannot decide must not become a licence to append.
    """
    from ciao.memory_audit import strip_learned_stamp
    from ciao.memory_tool import read_region, resolve_region

    promotable = _promotable_text(text)
    if promotable is None:
        return None
    try:
        region = resolve_region(target)
        entries, diags = read_region(guide_path, region)
    except Exception:  # noqa: BLE001 — the apply step reports a bad region
        return None
    if diags or not entries:
        return None
    if promotable in {strip_learned_stamp(entry) for entry in entries}:
        return None

    rows = await _reconcile_region(
        region,
        entries,
        [promotable],
        model=model,
        provider=provider,
        cwd=cwd,
        timeout_s=timeout_s,
    )
    if not rows:
        return _defer(f"reconcile unavailable for ciao:{region}", entries)
    return rows[0]


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
            if not isinstance(entry, dict):
                continue
            if key not in entry:
                # The oldest sidecar shape records neither timestamp; it is a
                # dismissal, exactly as `read_decisions` and the append-time
                # dedupe in `_dismissed_texts` already treat it. Requiring the
                # key made those rows invisible here, so re-filing a fact an
                # ancient install had rejected was reported as "already in the
                # queue" when no queue row existed.
                if key != "dismissed_at" or "promoted_at" in entry:
                    continue
            if entry.get("history_only"):
                # Ledger-only row; it decided nothing, so it must not report
                # the fact as promoted or dismissed.
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
    """Append a timestamped batch to ``Workspace/Memory-Proposals.md``.

    Read-merge-write under :func:`ciao.memory_receipts.queue_lock` so a
    concurrent undo (or another writer) cannot land between the dedupe read and
    the rewrite and be silently overwritten.
    """
    if not proposals:
        return None

    from ciao.memory_receipts import queue_lock, write_queue_atomically

    out_path = workspace_vault_root / _PROPOSALS_RELATIVE
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with queue_lock(out_path):
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

        write_queue_atomically(out_path, existing + "\n" + block)
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


def record_dismissal(
    proposals_path: Path,
    *,
    text: str,
    kind: str = "",
    via: str = "",
    source: str = "",
    destination: str = "",
    outcome: str = "",
    proposal_id: str = "",
    receipt_id: str = "",
    learning_id: str = "",
    finding: str = "",
    once: bool = False,
) -> bool:
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

    ``learning_id`` and ``finding`` name the one finding a decision was about,
    for the writer that settles a derived finding per finding rather than a
    whole row: the skill-proposal queue, whose record links back to the
    learnings it came from. Both are optional and omitted when blank, so a row
    written without them is indistinguishable from one written before the
    fields existed.

    The extra fields (``via``, ``source``, ``destination``, ``outcome``,
    ``proposal_id``) turn this dedupe sidecar into the decision history the
    review page's History tab reads; they are optional and omitted when
    blank so the on-disk shape stays backward compatible with the readers
    below, which only ever look at the ``*_at`` key and ``text``.
    """
    return _record_decision(
        proposals_path,
        text=text,
        kind=kind,
        key="dismissed_at",
        via=via,
        source=source,
        destination=destination,
        outcome=outcome,
        proposal_id=proposal_id,
        receipt_id=receipt_id,
        learning_id=learning_id,
        finding=finding,
        once=once,
    )


def record_promotion(
    proposals_path: Path,
    *,
    text: str,
    kind: str = "",
    via: str = "",
    source: str = "",
    destination: str = "",
    outcome: str = "",
    proposal_id: str = "",
    receipt_id: str = "",
    learning_id: str = "",
    finding: str = "",
    once: bool = False,
    history_only: bool = False,
) -> bool:
    """Record an accepted proposal in the same decision history.

    Same sidecar and the same reason as :func:`record_dismissal`: the dedupe
    in ``append_proposals`` reads only the queue file and this log, never the
    region/doc the promotion wrote to, so without this entry the curator
    re-queues the accepted fact on its next pass over the same transcript.
    Mirrors the CLI's promote-then-dismiss flow, which records the text for
    every removal regardless of the outcome action.
    """
    return _record_decision(
        proposals_path,
        text=text,
        kind=kind,
        key="promoted_at",
        via=via,
        source=source,
        destination=destination,
        outcome=outcome,
        proposal_id=proposal_id,
        receipt_id=receipt_id,
        learning_id=learning_id,
        finding=finding,
        once=once,
        history_only=history_only,
    )


def _record_decision(
    proposals_path: Path,
    *,
    text: str,
    kind: str,
    key: str,
    via: str = "",
    source: str = "",
    destination: str = "",
    outcome: str = "",
    proposal_id: str = "",
    receipt_id: str = "",
    learning_id: str = "",
    finding: str = "",
    once: bool = False,
    history_only: bool = False,
) -> bool:
    cleaned = text.strip()
    if not cleaned:
        return False
    log_path = dismissed_log_path(proposals_path)
    if once and _has_decision(proposals_path, key=key, text=cleaned, outcome=outcome):
        # Idempotent paths only. A decision the operator makes is a fresh event
        # every time, but a repeated sweep re-derives the same
        # "already applied, skipped" verdict on every pass over the same
        # transcript, and appending a row per pass grew the history without
        # recording anything new.
        return False
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry: dict[str, Any] = {
        key: datetime.now(UTC).isoformat(timespec="seconds"),
        "kind": kind,
        "text": cleaned,
    }
    # Omit blanks rather than writing empty strings: keeps legacy-shaped
    # entries (just the ``*_at`` key, ``kind``, ``text``) indistinguishable
    # from ones written before these fields existed.
    if via:
        entry["via"] = via
    if source:
        entry["source"] = source
    if destination:
        entry["destination"] = destination
    if outcome:
        entry["outcome"] = outcome
    if proposal_id:
        entry["proposal_id"] = proposal_id
    if receipt_id:
        # The memory receipt (see :mod:`ciao.memory_receipts`) that performed
        # this decision's write. ``text`` above stays the ORIGINAL bullet,
        # because that is what append-time dedupe compares a re-extracted fact
        # against; when the operator edited the wording before accepting, the
        # receipt records the edited text and no text match can find it again.
        # Recording the id is what lets History show that decision's change and
        # offer an undo. Rows written before this existed carry no id and are
        # joined heuristically instead.
        entry["receipt_id"] = receipt_id
    if learning_id:
        # Which learning a derived finding was filed against, for the writers
        # that settle one finding at a time. Empty for every decision that is
        # about a whole row, and for every row written before the field existed.
        entry["learning_id"] = learning_id
    if finding:
        entry["finding"] = finding
    if history_only:
        # Ledger-only row: it records that a pass ran and decided nothing new,
        # so the dedupe readers must not treat it as a decision. Without this
        # the row makes ``was_promoted`` true and ``append_proposals`` refuses
        # the fact forever, so a reverted in-session edit could never be
        # re-queued. ``_has_decision`` still sees it, which is what keeps
        # ``once=True`` idempotent.
        entry["history_only"] = True
    with log_path.open("a", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return True


# Parsed sidecar lines, keyed by path and invalidated on stat identity.
_SIDECAR_CACHE: dict[Path, tuple[tuple[int, int, int], list[dict[str, Any]]]] = {}


def _sidecar_entries(path: Path) -> list[dict[str, Any]]:
    """Parsed JSON-object lines of one decision sidecar, newest last.

    Cached on ``(mtime_ns, size, inode)``: the suppression path calls
    :func:`_has_decision` once per proposal while archiving, so a long-lived
    ledger was otherwise re-read and re-parsed O(proposals x history) times in
    a single pass. Sidecars are append-only (and any rewrite lands on a new
    inode), so stat identity is a sound key. Callers must treat the returned
    entries as read-only.
    """
    try:
        stat = path.stat()
    except OSError:
        _SIDECAR_CACHE.pop(path, None)
        return []
    stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
    cached = _SIDECAR_CACHE.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        _SIDECAR_CACHE.pop(path, None)
        return []
    entries: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    _SIDECAR_CACHE[path] = (stamp, entries)
    return entries


def _sidecar_paths(proposals_path: Path) -> tuple[Path, ...]:
    """Current plus legacy sidecar paths for one proposal queue, current first."""
    return tuple(
        proposals_path.with_suffix(suffix)
        for suffix in (_DISMISSED_LOG_SUFFIX, *_DISMISSED_LOG_LEGACY_SUFFIXES)
    )


def _has_decision(proposals_path: Path, *, key: str, text: str, outcome: str) -> bool:
    """Whether this exact decision (action, text, outcome) is already recorded.

    Used only by ``once=True`` writers; matching on the outcome as well keeps a
    later real promotion of a fact recordable after an earlier "skipped,
    already known" row for the same text.
    """
    for path in _sidecar_paths(proposals_path):
        for entry in _sidecar_entries(path):
            if key not in entry:
                continue
            if str(entry.get("text", "")).strip() != text:
                continue
            if str(entry.get("outcome", "")) == outcome:
                return True
    return False


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
    for path in _sidecar_paths(proposals_path):
        for entry in _sidecar_entries(path):
            if keys and not any(key in entry for key in keys):
                continue
            if entry.get("history_only"):
                # Ledger-only row; it decided nothing, so it must not dedupe.
                continue
            text = _one_line(str(entry.get("text", "")))
            if text:
                out.add(text)
    return out


def history_row_id(entry: dict[str, Any], workspace: str = "") -> str:
    """Stable id for one decision-history row, derived from its content.

    Unlike the live queue's :func:`ciao.proposal_tracking.stable_proposal_id`,
    this cannot key off a path/line: the API only has a vault root, the CLI
    only has an optional workspace name, and legacy sidecar rows have neither.

    Timestamps are second-precision, so content alone is not unique: the same
    fact accepted in two workspaces within one second, or two undated legacy
    rows with identical text, hashed identically — and the History list keys
    its ``<li>`` on this id, so a collision dropped a row on patch. The
    ``workspace`` and the row's position in its sidecar (``log`` + ``seq``,
    both assigned by :func:`read_decisions`) disambiguate. They are stable
    across reads because each sidecar is append-only and ``seq`` counts within
    one sidecar — a shared counter would renumber every legacy row each time a
    decision landed in the current one.
    """
    import hashlib

    basis = "|".join(
        (
            str(entry.get("ts", "")),
            str(entry.get("action", "")),
            str(entry.get("kind", "")),
            str(entry.get("text", "")),
            workspace,
            str(entry.get("log", "")),
            str(entry.get("seq", "")),
        )
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def read_decisions(proposals_path: Path) -> list[dict[str, Any]]:
    """Every recorded decision for one workspace's proposal queue, newest last.

    Normalizes both the current sidecar shape and the legacy ``.dismissed.log``
    text-only rows into one shape: ``{ts, action, via, kind, text, source,
    destination, outcome, proposal_id, receipt_id, learning_id, finding, log,
    seq}``. ``receipt_id`` is empty for every row written before it was
    recorded, and for every decision made outside the receipt protocol, and
    ``learning_id``/``finding`` are empty for every decision about a whole row
    rather than one derived finding. This is the read side of the decision
    history the review page's History tab renders; :func:`record_dismissal`
    and :func:`record_promotion` are the write side.

    ``seq`` is the row's position *within its own* append-only sidecar, and
    ``log`` names that sidecar. Together they let :func:`history_row_id` tell
    apart two rows whose content is byte-identical; both are dropped before the
    row reaches the API payload. Counting per sidecar rather than across the
    concatenation matters: a shared counter offsets every legacy row by the
    current sidecar's length, so each new decision changed the id of every
    legacy row — the exact instability ``seq`` exists to prevent.
    """
    rows: list[dict[str, Any]] = []
    for suffix in (_DISMISSED_LOG_SUFFIX, *_DISMISSED_LOG_LEGACY_SUFFIXES):
        seq = 0
        for entry in _sidecar_entries(proposals_path.with_suffix(suffix)):
            text = _one_line(str(entry.get("text", "")))
            if not text:
                continue
            if "promoted_at" in entry:
                ts, action = str(entry["promoted_at"]), "accepted"
            elif "dismissed_at" in entry:
                ts, action = str(entry["dismissed_at"]), "dismissed"
            else:
                # Oldest sidecar shape: no timestamp, no action recorded.
                # Treat as a dismissal (append-time dedupe already did) but
                # never fabricate a time.
                ts, action = "", "dismissed"
            rows.append(
                {
                    "ts": ts,
                    "action": action,
                    "via": str(entry.get("via", "")),
                    "kind": str(entry.get("kind", "")),
                    "text": text,
                    "source": str(entry.get("source", "")),
                    "destination": str(entry.get("destination", "")),
                    "outcome": str(entry.get("outcome", "")),
                    "proposal_id": str(entry.get("proposal_id", "")),
                    "receipt_id": str(entry.get("receipt_id", "")),
                    "learning_id": str(entry.get("learning_id", "")),
                    "finding": str(entry.get("finding", "")),
                    "log": suffix,
                    "seq": seq,
                }
            )
            seq += 1
    return rows


_STUB_HEADER = (
    "---\n"
    "tags: [ciao, memory, proposals]\n"
    "---\n"
    "# Memory Proposals\n\n"
    "Memory proposals awaiting a decision. Each batch is timestamped. The "
    "memory pass records what it is confident about; what lands here is what "
    "it was unsure about, or a write that failed.\n\n"
    "Destinations: `[memory]` / `[profile]` are the bounded `ciao:memory` / "
    "`ciao:profile` regions of the workspace `AGENTS.md` (edit the region "
    "first, then dismiss with `ciao memory-proposal-dismiss --text-file <file> "
    "--promoted` so the outcome counts as a promotion); "
    "`[project <doc-path>]` folds into that canonical doc; `[people <Name>]` "
    "updates `People/<Name>.md`; `[learnings]` appends to "
    "`Workspace/Learnings.md`; `[review]` has no known destination yet — "
    "decide what it is first. Standing directives stay in the AGENTS.md body "
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


# ── Review surface (CLI) ──────────────────────────────────────────────────


def list_proposals(
    proposals_path: Path,
) -> list[dict[str, str]]:
    """Structured pending proposal bullets from a workspace's queue.

    Reuses the shared proposal-kind grammar so the CLI, the web layer, and the
    audit always agree on what a bullet is. Each row carries the raw ``kind``,
    the bullet ``text``, the optional ``source`` tag, the optional ``target``
    payload (a person name or doc path) and the optional ``request`` id a
    ``/remember`` of a lesson carries.
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
                    "request": bullet.request,
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
    from ciao.memory_receipts import queue_lock

    with queue_lock(proposals_path):
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
        from ciao.memory_receipts import write_queue_atomically

        write_queue_atomically(proposals_path, "\n".join(lines).rstrip() + "\n")
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
