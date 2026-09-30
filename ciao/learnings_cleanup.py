"""Settlement-linked removal of retired ``Workspace/Learnings.md`` entries.

What this is
------------
``ciao/skill_proposals.learning_cleanup_eligibility`` answers, per learning,
"has every finding filed against this been decided?" It is deliberately a hook:
nothing behind it removes anything. This module is the reconciliation that acts
on that answer — the idempotent, budgeted, revision-checked removal of Active
entries whose linked findings are durably settled, and the attended workflow
(``ciao learnings-cleanup``) that does the same thing to an install's *legacy*
entries with a person approving every row.

Why a removal needs this much machinery
---------------------------------------
A learning is the user's own words. Taking one out of their notes on the
strength of a bookkeeping row is the one edit in the memory system that cannot be
undone by re-running a command, because the thing being removed is the record
that the command would have read. So every property the migration
(``ciao/learnings_migrate.py``) already has is here too, and for the same
reasons:

**Lossless.** Only the spans of the eligible Active entries are spliced out.
Frontmatter, fenced examples, the ``## Promoted / Resolved`` section, blank
lines, the byte-order mark and the line endings all pass through byte for byte.
The single exception is the frontmatter's ``updated:``, which is restated to
today — the file did change, and a document whose ``updated:`` still says the
day before its last entry was removed is a document that lies about its own age.
An entry whose shape the parser cannot read is never a candidate: it is reported
as a conflict and left alone.

**Revision-checked, twice.** The whole-file revision the plan was computed from
is re-checked under the write lock, so a concurrent ``[learnings]`` accept
landing between the plan and the apply is a conflict and nothing is written.
Then each candidate is re-checked against the bytes under that lock — a learning
whose settlement changed, or whose line moved, since the plan is dropped from
*that* apply rather than removed on a stale answer. Both revisions, before and
after, go in the receipt.

**Receipt-backed and reversible.** The reverse map is built and serialized
*before* the file is written, per span, with the exact bytes each removal took
out — so ``--revert`` restores the removed lines from the record rather than
re-deriving what they probably said. A receipt that cannot be prepared removes
nothing.

**Idempotent by suppression.** A second run finds nothing: the entry is gone, and
if it comes *back* the same bytes are not removed again. ``(learning_id,
entry_revision)`` pairs that have been removed are recorded in
:data:`SUPPRESSION_RELATIVE`, and the planner skips a suppressed pair until the
entry changes (a new revision is a new fact) or a person reapproves it. That is
what makes undo safe to leave in place: ``--revert`` puts the bytes back and
does **not** lift the suppression, so the next nightly pass does not quietly
undo the undo. A learning that has been edited since is a learning somebody has
looked at again, and it is eligible afresh on its own merits.

Why not ``commit_note_change``
------------------------------
Same reason as the migration, and it is the writer's own reasoning rather than a
new one: ``Workspace/Learnings.md`` is bookkeeping, not an entity note. It is
written by the unattended care run and by every ``[learnings]`` accept, and its
undo log is the receipt below, not a per-write journal row. So this is a new
sibling of :func:`ciao.learnings_migrate.migrate_learnings_file` rather than a
mode of it: the same ``memory_receipts.queue_lock`` + ``_write_locked(expect=…)``
+ ``write_queue_atomically``, the same receipt directory, its own prefix, its own
schema. Two writers on one file, one of them reachable from an unattended pass,
means the second one cannot be a re-skinning of the first: they would have to
agree about the file forever.

What this refuses to do
-----------------------
* It never removes an entry whose findings are pending, implementing,
  interrupted or failed; that is a question being asked again.
* It never removes one that no proposal links, or that a linked proposal carries
  an unattributable finding for — the other half of that learning may be the
  finding this queue could not read.
* It never removes one whose line changed after the finding was filed. The
  compare is per entry (:func:`ciao.learning_records.entry_revision`), so an
  unrelated edit elsewhere in the document does not count — but an edit to *this*
  line does, and a whole-file revision recorded by an install that predates the
  move matches nothing and is kept, because rewriting it would assert a
  sameness nothing established.
* It never removes one whose only destination was an upstream issue. A filed
  issue means somebody was told; it does not mean the lesson is in any skill this
  workspace can see, and ``upstream_drafts``'s own comment says so. A *rejected*
  draft is the one case that clears, because that is a person saying the finding
  did not hold here.
* The unattended pass never removes a legacy entry. Entries no proposal has ever
  linked are exactly the ones a human has to look at, and that is
  ``ciao learnings-cleanup`` — dry-run first, one approved row at a time, with
  the evidence written down.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ciao import skill_proposals, upstream_drafts
from ciao.learning_records import (
    LEARNINGS_RELATIVE,
    SECTION_ACTIVE,
    BOM,
    LearningRecord,
    entry_revision,
    parse_learnings,
    render_learning,
)
from ciao.learnings_migrate import (
    _RevisionMoved,
    _write_locked,
    learnings_file,
)
from ciao.memory_receipts import content_revision, write_queue_atomically

logger = logging.getLogger(__name__)


# ── Shape ───────────────────────────────────────────────────────────────────

#: The suppression store, vault-relative. Beside the document it governs rather
#: than in the runtime, because it is the workspace's own bookkeeping: a synced
#: or backed-up vault carries it with the entries it describes, and a receipt
#: under ``.runtime`` would not. It is listed in
#: ``ciao.vault_index.RESERVED_UNINDEXED_FILES``, so it never becomes a recall
#: hit.
SUPPRESSION_RELATIVE = "Workspace/learnings-cleanup.json"

#: Bumped only when the document's shape changes incompatibly. A store carrying
#: any other value is reported as unreadable rather than half-trusted: skipping
#: every suppression because the schema moved would remove the entries this
#: module exists to be careful about.
SUPPRESSION_SCHEMA = 1

#: A removed pair is remembered for as long as the store is, not on a clock. The
#: bound is the number of removals, not their age: the answer to "was this exact
#: entry already retired" is a lookup, and an expiry would make a re-run months
#: later remove a line somebody had put back deliberately.
MAX_SUPPRESSED = 500

RECEIPT_VERSION = 1
RECEIPT_PREFIX = "learnings-cleanup-"
RECEIPT_STAMP = "%Y%m%d-%H%M%S"

#: What a row is proposed to become. Three answers, and never a fourth: a caller
#: that cannot place a row does not get to invent an action for it.
REMOVE = "remove"
KEEP = "keep"
CONFLICT = "conflict"

#: The kept reasons, as a closed vocabulary, because the curation output and the
#: table both have to group them and a caller must be able to test membership.
#:
#: * ``pending`` — a finding is open, or a chat is working on it. Not settled is
#:   not settled.
#: * ``unanswered`` — a finding is in a state that needs a person to say whether
#:   it ever held (``not_applicable``, ``unclear``) or a confirmation nobody has
#:   given (``already_covered``). Distinguished from ``pending`` because the work
#:   is not outstanding, only unjudged.
#: * ``never_proposed`` — no proposal links a finding to this learning. The
#:   legacy case, and the one an unattended pass must never act on.
#: * ``held_back`` — a linked proposal carries a finding naming no learning, so
#:   this one may be the other half of it.
#: * ``changed_since`` — the entry's own line moved after the finding was filed.
#: * ``unrecorded`` — an origin recorded no revision at all, so nothing can show
#:   the entry is what was filed against.
#: * ``stock_filed_upstream`` — the only destination was an upstream issue, which
#:   is somebody being told, not the lesson being in a skill.
#: * ``stock_waiting_upstream`` — an upstream draft is still awaiting a decision.
#: * ``reopened`` — every finding is decided and one has been reopened, so the
#:   question is being asked again.
#: * ``suppressed`` — already removed at this exact revision, and a person has
#:   not reapproved it.
#: * ``unreadable`` — the parser could not read the line. The bytes are the
#:   owner's, and this is reported, never repaired.
KEPT_REASONS: frozenset[str] = frozenset(
    {
        "pending",
        "unanswered",
        "never_proposed",
        "held_back",
        "changed_since",
        "unrecorded",
        "stock_filed_upstream",
        "stock_waiting_upstream",
        "reopened",
        "suppressed",
        "unreadable",
    }
)

#: Reasons that mean nothing in this workspace ever asked about this entry, so
#: the table reports them as *unmatched* rather than as a decision. A reviewer
#: reading the counts wants to know how much of the document is simply not
#: connected to anything yet.
UNMATCHED_REASONS: frozenset[str] = frozenset(
    {"never_proposed", "unreadable", "stock_filed_upstream", "stock_waiting_upstream"}
)

#: The origins that are a person's answer rather than an open question. A draft
#: that was *filed* is deliberately absent: an issue existing is not the lesson
#: landing anywhere, which is the module's own argument for keeping it.
CLEARED_DRAFTS = frozenset({upstream_drafts.DRAFT_REJECTED})


# ── Rows and plans ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CleanupRow:
    """One Active entry, and what the reconciliation proposes to do with it.

    ``entry_revision`` is the learning's own line hash, so it travels into the
    receipt and the approval and is what "the thing you reviewed" means later.
    ``start``/``end`` address the exact bytes in the document the plan was read
    from; they are re-derived and re-checked under the write lock rather than
    trusted, and a candidate whose span no longer holds the bytes it was planned
    from is dropped instead of removed.
    """

    learning_id: str
    key: str
    line: str
    entry_revision: str
    action: str
    reason: str
    detail: str = ""
    destination: str = ""
    evidence: str = ""
    start: int = 0
    end: int = 0
    source_text: str = ""

    @property
    def removable(self) -> bool:
        return self.action == REMOVE

    def as_dict(self) -> dict[str, Any]:
        return {
            "learning_id": self.learning_id,
            "key": self.key,
            "line": self.line,
            "entry_revision": self.entry_revision,
            "action": self.action,
            "reason": self.reason,
            "detail": self.detail,
            "destination": self.destination,
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class CleanupPlan:
    """What one reconciliation would do, computed without writing anything.

    ``revision`` is the whole document's revision, kept because the apply has to
    prove the file it is about to rewrite is still the one it planned against —
    a per-entry revision says an *entry* is unchanged and says nothing about a
    concurrent accept appending a new lesson below it.
    """

    workspace: str
    path: str
    revision: str
    removals: tuple[CleanupRow, ...] = ()
    kept: tuple[CleanupRow, ...] = ()
    conflicts: tuple[CleanupRow, ...] = ()
    active: int = 0
    over_cap: bool = False
    diagnostics: tuple[str, ...] = ()

    blocked: str = ""
    """Why this plan may not remove anything at all, if it may not.

    One case, and it is a refusal rather than a plan: the suppression store
    exists and cannot be read, so nothing already removed is known to be removed.
    Planning a full removal list off that store is how the same entry is retired
    twice, so the plan carries the refusal and no candidates."""

    @property
    def rows(self) -> tuple[CleanupRow, ...]:
        """Every Active row, removals first, in document order within each group."""
        return self.removals + self.kept + self.conflicts

    @property
    def empty(self) -> bool:
        return not self.removals

    @property
    def counts(self) -> dict[str, int]:
        """The numbers the table, the curation output and the completion check read.

        ``routes`` is counted separately from ``keep`` on purpose: an entry whose
        only destination is an upstream issue is not local work, and a report that
        folded it into "kept" would present somebody else's queue as this
        workspace's backlog.
        """
        return {
            "active": self.active,
            "remove": len(self.removals),
            "keep": len(self.kept),
            "conflict": len(self.conflicts),
            "routes": sum(
                1
                for row in self.kept
                if row.reason in {"stock_filed_upstream", "stock_waiting_upstream"}
            ),
            "unmatched": sum(
                1 for row in self.kept if row.reason in UNMATCHED_REASONS
            ),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "path": self.path,
            "revision": self.revision,
            "active": self.active,
            "over_cap": self.over_cap,
            "blocked": self.blocked,
            "counts": self.counts,
            "removals": [row.as_dict() for row in self.removals],
            "kept": [row.as_dict() for row in self.kept],
            "conflicts": [row.as_dict() for row in self.conflicts],
            "diagnostics": list(self.diagnostics),
        }


@dataclass(frozen=True, slots=True)
class CleanupResult:
    """What an apply did, or refused to do, and the receipt that reverses it."""

    workspace: str
    path: str
    applied: bool
    revision_before: str = ""
    revision_after: str = ""
    removed: tuple[CleanupRow, ...] = ()
    kept: tuple[CleanupRow, ...] = ()
    conflicts: tuple[str, ...] = ()
    suppressed: tuple[tuple[str, str], ...] = ()
    receipt: dict[str, Any] | None = None
    failed: tuple[str, ...] = ()
    skipped: str = ""

    @property
    def removed_count(self) -> int:
        return len(self.removed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "path": self.path,
            "applied": self.applied,
            "revision_before": self.revision_before,
            "revision_after": self.revision_after,
            "removed": [row.as_dict() for row in self.removed],
            "removed_count": self.removed_count,
            "kept": [row.as_dict() for row in self.kept],
            "conflicts": list(self.conflicts),
            "suppressed": [
                {"learning_id": learning_id, "entry_revision": revision}
                for learning_id, revision in self.suppressed
            ],
            "failed": list(self.failed),
            "skipped": self.skipped,
            "receipt": self.receipt,
        }


# ── The suppression store ───────────────────────────────────────────────────


def suppression_path(vault_root: Path) -> Path:
    """Where this workspace's removed-pair record lives."""
    return Path(vault_root) / SUPPRESSION_RELATIVE


def read_suppressions(vault_root: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """Every ``(learning_id, entry_revision)`` already removed, as a mapping.

    Unreadable is not empty. A store that exists and cannot be read is reported
    as an error by the caller rather than treated as "nothing was removed",
    because the other reading would re-remove exactly the entries the record was
    there to protect.
    """
    path = suppression_path(vault_root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise _SuppressionUnreadable(path) from None
    if not isinstance(data, dict) or data.get("schema_version") != SUPPRESSION_SCHEMA:
        raise _SuppressionUnreadable(path)
    entries = data.get("removed")
    if not isinstance(entries, list):
        raise _SuppressionUnreadable(path)
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for item in entries:
        if not isinstance(item, dict):
            continue
        learning_id = str(item.get("learning_id") or "").strip()
        revision = str(item.get("entry_revision") or "").strip()
        if learning_id and revision:
            found[(learning_id, revision)] = item
    return found


class _SuppressionUnreadable(RuntimeError):
    """The suppression store exists and this code does not understand it."""


def write_suppressions(
    vault_root: Path, added: list[tuple[str, str]], *, actor: str = "system"
) -> dict[tuple[str, str], dict[str, Any]]:
    """Record the pairs this run removed, and return the whole store.

    Bounded rather than appending forever: the store is a lookup table, and the
    oldest entries in it are the ones whose ``learning_id`` no longer exists in
    the document. Trimming is by insertion order, so the newest removals — the
    ones an undo could plausibly hit — are the ones kept.
    """
    existing = read_suppressions(vault_root)
    merged: list[dict[str, Any]] = []
    for (learning_id, revision), item in existing.items():
        merged.append(dict(item))
    for learning_id, revision in added:
        if (learning_id, revision) in existing:
            continue
        merged.append(
            {
                "learning_id": learning_id,
                "entry_revision": revision,
                "removed_at": _now(),
                "removed_by": actor,
            }
        )
    merged = merged[-MAX_SUPPRESSED:]
    payload = {
        "schema_version": SUPPRESSION_SCHEMA,
        "updated_at": _now(),
        "removed": merged,
    }
    path = suppression_path(vault_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_queue_atomically(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return {
        (str(item["learning_id"]), str(item["entry_revision"])): item for item in merged
    }


def clear_suppression(vault_root: Path, learning_id: str, revision: str) -> bool:
    """Drop one suppressed pair. True when there was one to drop.

    The two things that clear a suppression, and both are a decision somebody
    made: an approval that says *this one again*, and a line that has changed,
    which is not a decision but is the same statement in practice — the entry
    has been looked at and written differently, so it is a new fact rather than
    the one that was retired.
    """
    existing = read_suppressions(vault_root)
    if (learning_id, revision) not in existing:
        return False
    remaining = [
        dict(item)
        for (stored_id, stored_rev), item in existing.items()
        if (stored_id, stored_rev) != (learning_id, revision)
    ]
    payload = {
        "schema_version": SUPPRESSION_SCHEMA,
        "updated_at": _now(),
        "removed": remaining,
    }
    write_queue_atomically(
        suppression_path(vault_root),
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )
    return True


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# ── Reading ─────────────────────────────────────────────────────────────────


def _load_text(vault_root: Path) -> str:
    """The document's bytes as text, or ``""`` when there is no document.

    Decoded from bytes for the same reason the migration does it that way:
    ``read_text`` translates newlines, and a CRLF document would come back LF and
    be written out that way, so "every other byte is preserved" would be true of
    the preview and false of the file. A BOM is left in place — it is a file
    property and :mod:`ciao.learning_records` treats it as one.
    """
    path = learnings_file(vault_root)
    if not path.is_file():
        return ""
    return path.read_bytes().decode("utf-8")


def _upstream_origins(
    config: Any, workspace: str
) -> dict[str, list[upstream_drafts.UpstreamDraft]]:
    """Every upstream draft that names a learning, by that learning's id.

    Read across the *whole* draft sidecar including settled rows, for the reason
    ``learning_settlement`` reads settled proposals: a learning split across two
    drafts is only answered when both are, and a draft that has gone is not an
    answer.
    """
    by_learning: dict[str, list[upstream_drafts.UpstreamDraft]] = {}
    for draft in upstream_drafts.read_records(config, workspace):
        for origin in draft.origins:
            learning_id = str(origin.get("learning_id") or "").strip()
            if not learning_id:
                continue
            by_learning.setdefault(learning_id, []).append(draft)
    return by_learning


def _row_for(
    entry_key: str,
    record: LearningRecord,
    revision: str,
    *,
    action: str,
    reason: str,
    detail: str = "",
    destination: str = "",
    evidence: str = "",
    start: int = 0,
    end: int = 0,
    source_text: str = "",
) -> CleanupRow:
    return CleanupRow(
        learning_id=record.learning_id,
        key=entry_key,
        line=render_learning(record),
        entry_revision=revision,
        action=action,
        reason=reason,
        detail=detail,
        destination=destination,
        evidence=evidence,
        start=start,
        end=end,
        source_text=source_text,
    )


def _classify(
    reason_text: str, origins: list[dict[str, Any]]
) -> tuple[str, str]:
    """Map the fold's prose and the origin states onto a closed reason.

    The prose is the fold's own answer and is not re-derived here; what this adds
    is the *grouping*, so the table, the curation output and the tests can all
    talk about ``pending`` rather than about a sentence. Kept deliberately small
    — anything it cannot place is ``unanswered``, which is the safe direction,
    because every one of these keeps the entry.
    """
    if "no proposal links" in reason_text:
        return "never_proposed", "no proposal has ever linked a finding to this entry"
    if "name no learning" in reason_text:
        return (
            "held_back",
            "a finding on a linked proposal names no learning, so this may be "
            "its other half",
        )
    if "recorded no revision" in reason_text:
        return (
            "unrecorded",
            "a finding recorded no revision of this entry, so nothing can show "
            "it is what was filed against",
        )
    if "changed since" in reason_text:
        return "changed_since", "this entry moved after the finding was filed"
    if "not settled" in reason_text:
        states = sorted(
            {
                str(origin.get("state") or "")
                for origin in origins
                if str(origin.get("state") or "") not in skill_proposals.CLEARED_ORIGINS
            }
        )
        if set(states) & set(skill_proposals.OPEN_ORIGINS):
            return "pending", f"a finding is still open ({', '.join(states)})"
        if set(states) & set(skill_proposals.REVIEW_ORIGINS):
            return (
                "unanswered",
                f"a finding needs a person to confirm it ({', '.join(states)})",
            )
        return "reopened", f"a decided finding has been reopened ({', '.join(states)})"
    return "unanswered", reason_text


def plan_cleanup(
    vault_root: Path,
    *,
    workspace: str,
    config: Any,
    today: date | None = None,
    max_removals: int | None = None,
) -> CleanupPlan:
    """Compute what a reconciliation of one workspace's learnings would do.

    A pure plan: one parse of the document, one eligibility question per Active
    entry, and nothing written — not the file, not the suppression store, not a
    receipt. ``apply_cleanup`` re-asks every question against the bytes under
    its own lock, so a plan that has gone stale cannot remove anything; this is
    where the reasons and the evidence come from.

    Only the ``## Active`` section is ever a candidate. An entry under
    ``## Promoted / Resolved`` has already been decided by a person or a
    promotion, and re-deciding it every night is how a promoted learning gets
    retired on the strength of work nobody re-checked.

    Every Active entry gets a row, including the ones no proposal has ever
    linked. "Kept, because nothing has ever asked about it" is the answer an
    operator needs most from this command, and a plan that listed only the
    eligible entries would answer it by omission.

    ``max_removals`` bounds the pass the way the stale-note pass is bounded: a
    backlog that is allowed to spend the whole nightly budget starves every
    other key, and the entries behind it drain over several nights rather than
    forever, because each removal is real. ``plan.over_cap`` says a backlog was
    cut, because a plan that silently dropped candidates would read as a
    complete answer.
    """
    root = Path(vault_root)
    path = learnings_file(root)
    if not path.is_file():
        return CleanupPlan(
            workspace=workspace,
            path=LEARNINGS_RELATIVE,
            revision=content_revision(""),
            blocked=f"{LEARNINGS_RELATIVE} does not exist",
        )
    text = _load_text(root)
    document = parse_learnings(text, workspace=workspace)

    try:
        suppressed = read_suppressions(root)
    except _SuppressionUnreadable:
        # Refuse to plan at all rather than plan a full removal list off a store
        # this code cannot read: every entry in it is one this module already
        # removed once, and re-removing them is the failure the store prevents.
        return CleanupPlan(
            workspace=workspace,
            path=LEARNINGS_RELATIVE,
            revision=content_revision(text),
            active=sum(
                1 for entry in document.entries if entry.section == SECTION_ACTIVE
            ),
            blocked=(
                f"{SUPPRESSION_RELATIVE} exists and cannot be read, so nothing "
                "already removed is known to be removed"
            ),
            diagnostics=document.diagnostics,
        )

    drafts_by_learning = _upstream_origins(config, workspace)
    removals: list[CleanupRow] = []
    kept: list[CleanupRow] = []
    conflicts: list[CleanupRow] = []
    active = 0
    over_cap = False

    for entry in document.entries:
        if entry.section != SECTION_ACTIVE:
            continue
        active += 1
        record = entry.record
        if record is None:
            # Unreadable is a report, never a repair. The bytes stay exactly as
            # written and the line is named, because an owner who cannot see why
            # their entry is being ignored will assume the tool lost it.
            conflicts.append(
                CleanupRow(
                    learning_id="",
                    key=entry.source_text.strip()[:60],
                    line=entry.source_text,
                    entry_revision=content_revision(entry.source_text),
                    action=CONFLICT,
                    reason="unreadable",
                    detail="; ".join(entry.diagnostics) or "no record could be read",
                    start=entry.start,
                    end=entry.end,
                    source_text=entry.source_text,
                )
            )
            continue

        try:
            revision = entry_revision(record)
        except ValueError as exc:
            # A record that cannot be rendered has no canonical line, so it has
            # no revision and cannot be the thing a finding was filed against.
            conflicts.append(
                CleanupRow(
                    learning_id=record.learning_id,
                    key=record.key,
                    line=entry.source_text,
                    entry_revision="",
                    action=CONFLICT,
                    reason="unreadable",
                    detail=str(exc),
                    start=entry.start,
                    end=entry.end,
                    source_text=entry.source_text,
                )
            )
            continue

        if (record.learning_id, revision) in suppressed:
            kept.append(
                _row_for(
                    record.key,
                    record,
                    revision,
                    action=KEEP,
                    reason="suppressed",
                    detail=(
                        "already removed at this exact revision; it becomes "
                        "eligible again if the entry changes or somebody "
                        "reapproves it"
                    ),
                    start=entry.start,
                    end=entry.end,
                    source_text=entry.source_text,
                )
            )
            continue

        report = skill_proposals.learning_cleanup_eligibility(
            config, workspace, record, current_revision=revision
        )
        origins = list(report.get("origins") or [])
        reason_text = str(report.get("reason") or "")
        drafts = drafts_by_learning.get(record.learning_id, [])

        # Two destinations, two answers, and the decision is the *both* of them.
        # The local half is the fold's; the upstream half is the drafts'
        # (:func:`_upstream_verdict`), because a stock lesson is answered by a
        # person rejecting the finding and a filed issue is not an answer at all.
        verdict = _upstream_verdict(drafts, revision)
        local_open = any(not origin.get("clears") for origin in origins) or (
            "name no learning" in reason_text
        )
        local_settled = bool(report.get("eligible")) or (
            not origins and verdict.decided
        )
        # The order is the order a person needs it in, and it is also the order
        # the curation output has to present them in: a finding that is still open
        # is local work, an issue waiting on a maintainer is not.
        if local_open:
            keep = _classify(reason_text, origins)
        elif verdict.present and not verdict.decided:
            keep = (verdict.reason, verdict.detail)
        elif verdict.stale:
            keep = (
                "changed_since",
                "an upstream draft was filed against a different revision of this "
                "entry",
            )
        elif not local_settled:
            # Either the local fold is unanswered (and the prose says why, or says
            # nothing links this learning at all), or it is answered but every
            # origin recorded no revision of this entry. Both are the fold's own
            # answer; there is nothing here to add to it.
            keep = _classify(reason_text, origins)
        else:
            keep = ("", "")
        if keep[0]:
            kept.append(
                _row_for(
                    record.key,
                    record,
                    revision,
                    action=KEEP,
                    reason=keep[0],
                    detail=keep[1] or _draft_detail(drafts),
                    destination=", ".join(sorted({draft.skill for draft in drafts})),
                    evidence=_draft_evidence(drafts),
                    start=entry.start,
                    end=entry.end,
                    source_text=entry.source_text,
                )
            )
            continue

        if max_removals is not None and len(removals) >= max_removals:
            over_cap = True
            kept.append(
                _row_for(
                    record.key,
                    record,
                    revision,
                    action=KEEP,
                    reason="pending",
                    detail=(
                        f"eligible; this run is capped at {max_removals} "
                        "removal(s), the next run takes it"
                    ),
                    destination=", ".join(
                        sorted({str(origin.get("skill") or "") for origin in origins})
                    ),
                    evidence=_origin_evidence(origins),
                    start=entry.start,
                    end=entry.end,
                    source_text=entry.source_text,
                )
            )
            continue

        removals.append(
            _row_for(
                record.key,
                record,
                revision,
                action=REMOVE,
                reason="settled",
                detail=str(report.get("reason") or ""),
                destination=", ".join(
                    sorted({str(origin.get("skill") or "") for origin in origins})
                ),
                evidence=_origin_evidence(origins),
                start=entry.start,
                end=entry.end,
                source_text=entry.source_text,
            )
        )

    return CleanupPlan(
        workspace=workspace,
        path=LEARNINGS_RELATIVE,
        revision=content_revision(text),
        removals=tuple(removals),
        kept=tuple(kept),
        conflicts=tuple(conflicts),
        active=active,
        over_cap=over_cap,
        diagnostics=document.diagnostics,
    )


@dataclass(frozen=True, slots=True)
class _UpstreamVerdict:
    """What the upstream draft sidecar says about one learning.

    ``decided`` is true only when every draft naming this learning was *rejected*
    — a person's answer, the upstream equivalent of a local dismissal. A *filed*
    draft is deliberately not a decision: an issue existing is somebody being told,
    and this workspace cannot see whether the lesson landed anywhere. That is the
    module's whole reason for keeping a stock lesson, and it is why
    :data:`CLEARED_DRAFTS` holds one lifecycle.

    ``stale`` is the revision check on this side, held to the same rule as the
    local one: a draft filed against a different revision of this entry was
    written against bytes that are no longer there.
    """

    decided: bool
    stale: bool
    present: bool = False
    reason: str = ""
    detail: str = ""


def _upstream_verdict(
    drafts: list[upstream_drafts.UpstreamDraft], revision: str
) -> _UpstreamVerdict:
    """The upstream half of one learning's answer, or an empty verdict."""
    if not drafts:
        return _UpstreamVerdict(decided=False, stale=False, present=False)
    states = ", ".join(
        f"{draft.skill} ({draft.lifecycle})"
        for draft in sorted(drafts, key=lambda item: item.skill)
    )
    if all(draft.lifecycle in CLEARED_DRAFTS for draft in drafts):
        stale = any(
            str(origin.get("source_revision") or "") != revision
            for draft in drafts
            for origin in draft.origins
        )
        return _UpstreamVerdict(
            decided=True,
            stale=stale,
            present=True,
            detail=f"every upstream draft was rejected: {states}",
        )
    told = any(
        draft.lifecycle == upstream_drafts.DRAFT_FILED for draft in drafts
    )
    return _UpstreamVerdict(
        decided=False,
        stale=False,
        present=True,
        reason="stock_filed_upstream" if told else "stock_waiting_upstream",
        detail=(
            "an upstream issue is not the lesson landing anywhere, and this "
            f"workspace cannot see whether it did: {states}"
        ),
    )


def _draft_detail(drafts: list[upstream_drafts.UpstreamDraft]) -> str:
    """The draft lifecycles behind a row, for a reader who has to judge it."""
    if not drafts:
        return ""
    return ", ".join(
        f"{draft.skill} ({draft.lifecycle})"
        for draft in sorted(drafts, key=lambda item: item.skill)
    )


def _draft_evidence(drafts: list[upstream_drafts.UpstreamDraft]) -> str:
    """The filed issues behind a row, or an honest note that there are none."""
    urls = sorted({draft.issue_url for draft in drafts if draft.issue_url})
    return ", ".join(urls) or (
        "no draft has been filed yet" if drafts else ""
    )


def _origin_evidence(origins: list[dict[str, Any]]) -> str:
    """The receipts and rejections behind a removal, in one readable string.

    The verification is what makes an ``applied`` origin an answer rather than a
    claim, so it is the first thing printed. A row that removes somebody's notes
    on the strength of "it was handled" without saying how is the failure this
    whole module exists to make impossible.
    """
    parts: list[str] = []
    for origin in origins:
        skill = str(origin.get("skill") or "")
        state = str(origin.get("state") or "")
        verification = str(origin.get("verification") or "")
        parts.append(
            f"{skill}: {state}"
            + (f" ({verification})" if verification else "")
        )
    return "; ".join(parts)


# ── Frontmatter ─────────────────────────────────────────────────────────────


def _frontmatter_span(text: str) -> tuple[int, int] | None:
    """``(body_start, close_start)`` of the frontmatter, or ``None``.

    ``None`` for a document with no frontmatter at all, which is a real shape
    (:mod:`ciao.learning_records` treats frontmatter as optional) and not an
    error. The BOM is stepped over, because a document that starts with one does
    still have a frontmatter block and treating the mark as part of the opening
    fence would put the whole file inside it.
    """
    offset = 1 if text.startswith(BOM) else 0
    if not text[offset:].startswith("---"):
        return None
    first_newline = text.find("\n", offset)
    if first_newline == -1 or text[offset:first_newline].strip() != "---":
        return None
    body = first_newline + 1
    position = body
    while position <= len(text):
        newline = text.find("\n", position)
        end = len(text) if newline == -1 else newline
        content_end = end - 1 if end > position and text[end - 1] == "\r" else end
        if text[position:content_end].strip() in {"---", "..."}:
            return body, position
        if newline == -1:
            break
        position = newline + 1
    return None


def _restamp(text: str, *, today: date) -> str:
    """The text with the frontmatter's ``updated:`` set to *today*.

    The only byte this module writes that is not a removal, and it is not
    optional. A document whose ``updated:`` still names the day its last entry
    was retired is a document that misreports its own age, and every consumer
    that reads that field — the memory map's recency, the audit's horizon — would
    then treat a file that just changed as months stale.

    Added when the key is absent rather than refused: the field is bookkeeping
    the pipeline owns, and a document missing it is still a document whose
    ``updated:`` should say when it was last touched. The key's own spelling and
    the position of every other line are preserved, CRLF included, because the
    replaced line is located by prefix and its terminator is left where it was.
    """
    stamp = today.isoformat()
    span = _frontmatter_span(text)
    if span is None:
        return text
    body_start, close_start = span
    terminator = "\r\n" if text[close_start - 2 : close_start] == "\r\n" else "\n"
    position = body_start
    while position < close_start:
        newline = text.find("\n", position)
        end = len(text) if newline == -1 else newline
        content_end = end - 1 if end > position and text[end - 1] == "\r" else end
        line = text[position:content_end]
        if line.strip() in {"---", "..."}:
            break
        if line.strip().startswith("updated:"):
            indent = line[: len(line) - len(line.lstrip())]
            return f"{text[:position]}{indent}updated: {stamp}{text[content_end:]}"
        if newline == -1:
            break
        position = newline + 1
    return f"{text[:close_start]}updated: {stamp}{terminator}{text[close_start:]}"


# ── The apply ───────────────────────────────────────────────────────────────


def _splice(text: str, rows: list[CleanupRow]) -> tuple[str, list[dict[str, Any]]]:
    """The text with each row's span removed, back to front.

    Back to front, so every row's ``start`` is still valid in the string being
    built when its turn comes *and* stays valid afterwards — nothing below a span
    has moved. That is what lets the offsets recorded in the receipt be read as
    offsets into the text the run *leaves behind*, which is the only text an undo
    will ever be looking at.

    Each span takes its own line terminator with it, so removing an entry removes
    the blank-line pairing with it rather than leaving a doubled newline behind.
    The terminator is read from the document rather than from the platform: a
    CRLF document whose removals left ``\\n`` would be a document with mixed
    endings, which is exactly the byte-level damage this module promises not to
    do.

    Each span is returned with the bytes that will sit either side of the gap in
    the spliced text, because a removal leaves nothing at the offset to check
    against — an undo has to be able to prove the gap is still the same gap
    before it fills it.
    """
    out = text
    spans: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: item.start, reverse=True):
        end = row.end
        if out[end : end + 2] == "\r\n":
            end += 2
        elif out[end : end + 1] == "\n":
            end += 1
        removed = out[row.start : end]
        out = out[: row.start] + out[end:]
        spans.append(
            {
                "offset": row.start,
                "from": removed,
                "learning_id": row.learning_id,
                "entry_revision": row.entry_revision,
                "key": row.key,
            }
        )
    spans.reverse()
    return out, spans


#: How much of the surrounding text an undo checks before filling a gap. Long
#: enough that a hand edit anywhere near the removal changes it, short enough that
#: an unrelated edit three entries away does not — the point is to catch somebody
#: typing into the file this module is about to rewrite, not to make the document
#: immutable.
ANCHOR_CHARS = 24


def _with_anchors(spans: list[dict[str, Any]], spliced: str) -> None:
    """Attach the before/after context each gap will have, in place.

    Mutated rather than rebuilt so the offsets the splice computed are the ones
    that are recorded; the two cannot then describe different files.
    """
    for span in spans:
        offset = int(span["offset"])
        span["before"] = spliced[max(0, offset - ANCHOR_CHARS) : offset]
        span["after"] = spliced[offset : offset + ANCHOR_CHARS]


def _build_receipt(
    plan: CleanupPlan,
    spans: list[dict[str, Any]],
    *,
    vault_root: Path,
    before: str,
    after: str,
    actor: str,
    approvals: dict[str, dict[str, Any]],
    shift: int = 0,
) -> dict[str, Any]:
    """The reverse map, as the document that will be written.

    Pure, and it is built *before* the file is touched: a receipt that cannot be
    assembled removes nothing, because a removal nobody can reverse is the one
    outcome this module does not have. Each span carries the exact bytes it took
    out, the offset that gap occupies in the **post-removal** text, and the
    context either side of it — which is the direction ``--revert`` walks, and the
    only direction in which a removal can be checked at all.

    ``shift`` moves every offset by the amount the ``updated:`` restamp moved the
    document, so a receipt written after that edit still addresses the bytes the
    run actually left on disk.
    """
    return {
        "schema_version": RECEIPT_VERSION,
        "removed_at": _now(),
        "removed_by": actor,
        "vault_root": str(vault_root),
        "path": LEARNINGS_RELATIVE,
        "workspace": plan.workspace,
        "revision_before": before,
        "revision_after": after,
        "entries_removed": len(spans),
        "removals": [
            {**span, "offset": int(span["offset"]) + shift} for span in spans
        ],
        "kept": [row.as_dict() for row in plan.kept],
        "approvals": approvals,
        "diagnostics": list(plan.diagnostics),
    }


def apply_cleanup(
    vault_root: Path,
    plan: CleanupPlan,
    *,
    workspace: str,
    config: Any,
    actor: str = "system",
    today: date | None = None,
    reapprove: bool = False,
    approvals: dict[str, dict[str, Any]] | None = None,
    reviewed: bool = False,
    max_removals: int | None = None,
) -> CleanupResult:
    """Remove the plan's eligible entries, or nothing at all.

    An empty plan is a no-op that still says so, and a second run over an
    already-cleaned file finds nothing to do: the entries are gone, and the ones
    that were put back are suppressed until somebody says otherwise.

    Four checks stand between the plan and the bytes, in this order, and each one
    can only make the apply remove *less*:

    * the whole document still has the revision the plan was computed from;
    * every candidate's span still holds the exact bytes it was planned from;
    * every candidate is eligible *again*, against a record parsed from the bytes
      under this very lock, with the settlement re-folded from the queue;
    * the receipt can be assembled and serialized.

    Only then is the file written, through the same lock and atomic helper the
    migration and the proposal queue use, with ``expect=`` the revision read
    inside the lock. A failure anywhere above leaves the document exactly as it
    was, and the suppression store is not touched — so a run that could not
    remove anything also cannot make the next run think it did.

    ``reapprove`` lifts the suppression on every candidate. It is for the
    attended workflow only, where a person has just said *remove this one
    again*; the unattended pass never sets it, because an unattended pass that
    could re-remove a retired entry would make ``--revert`` unsafe.

    ``reviewed`` is the caller's attestation that a person has read this document
    as it stands. It earns a receipt on its own, with no removals in it, and that
    is the only durable difference between "a person looked at this and agreed"
    and "nobody looked" — the two things an update task's completion check has to
    tell apart, and the reason a fully reviewed no-op is a finished job.
    """
    root = Path(vault_root)
    stamp = today or datetime.now(UTC).date()
    approvals = dict(approvals or {})
    if plan.blocked:
        return CleanupResult(
            workspace=workspace,
            path=LEARNINGS_RELATIVE,
            applied=False,
            revision_before=plan.revision,
            kept=plan.kept,
            skipped=plan.blocked,
        )
    if not plan.removals:
        # A reviewed no-op is still a review, and the unattended surface needs to
        # be able to tell "a person looked at this and the answer was nothing to
        # remove" from "nobody looked". So when the caller attests to a review, the
        # receipt is written and returned even though the file is untouched: it
        # names the revision that was reviewed and the rows that were approved
        # against it, which is the whole difference between the two.
        review: dict[str, Any] | None = None
        if reviewed or approvals:
            review = {
                "schema_version": RECEIPT_VERSION,
                "removed_at": _now(),
                "removed_by": actor,
                "vault_root": str(root),
                "path": LEARNINGS_RELATIVE,
                "workspace": workspace,
                "revision_before": plan.revision,
                "revision_after": plan.revision,
                "entries_removed": 0,
                "removals": [],
                "kept": [row.as_dict() for row in plan.kept],
                "approvals": approvals,
                "diagnostics": list(plan.diagnostics),
            }
            json.dumps(review)
        return CleanupResult(
            workspace=workspace,
            path=LEARNINGS_RELATIVE,
            applied=False,
            revision_before=plan.revision,
            kept=plan.kept,
            conflicts=tuple(row.detail for row in plan.conflicts),
            receipt=review,
        )

    path = learnings_file(root)
    if not path.is_file():
        return CleanupResult(
            workspace=workspace,
            path=LEARNINGS_RELATIVE,
            applied=False,
            revision_before=plan.revision,
            skipped=f"{LEARNINGS_RELATIVE} does not exist",
        )
    try:
        text = _load_text(root)
    except (OSError, UnicodeDecodeError) as exc:
        return CleanupResult(
            workspace=workspace,
            path=LEARNINGS_RELATIVE,
            applied=False,
            revision_before=plan.revision,
            failed=(f"{LEARNINGS_RELATIVE}: {exc}",),
        )

    before = content_revision(text)
    if before != plan.revision:
        # Somebody wrote to the file between the plan and this call. The plan
        # is about a document that no longer exists, and a whole document is
        # exactly what a cleanup splices into, so nothing is removed.
        return CleanupResult(
            workspace=workspace,
            path=LEARNINGS_RELATIVE,
            applied=False,
            revision_before=before,
            kept=plan.kept,
            conflicts=(REVISION_MOVED,),
        )

    document = parse_learnings(text, workspace=workspace)
    try:
        suppressed = read_suppressions(root)
    except _SuppressionUnreadable as exc:
        return CleanupResult(
            workspace=workspace,
            path=LEARNINGS_RELATIVE,
            applied=False,
            revision_before=before,
            failed=(f"{SUPPRESSION_RELATIVE}: {exc}",),
        )

    selected: list[CleanupRow] = []
    dropped: list[str] = []
    for candidate in plan.removals:
        if max_removals is not None and len(selected) >= max_removals:
            dropped.append(f"{candidate.key}: over the cap for this run")
            continue
        if (candidate.learning_id, candidate.entry_revision) in suppressed and not reapprove:
            dropped.append(
                f"{candidate.key}: already removed at this exact revision"
            )
            continue
        entry = _entry_at(document, candidate)
        if entry is None or entry.record is None or entry.source_text != candidate.source_text:
            dropped.append(f"{candidate.key}: the entry changed since the plan")
            continue
        revision = entry_revision(entry.record)
        if revision != candidate.entry_revision:
            dropped.append(f"{candidate.key}: the entry's own revision moved")
            continue
        report = skill_proposals.learning_cleanup_eligibility(
            config, workspace, entry.record, current_revision=revision
        )
        if not report.get("eligible") and not _reapproved(candidate, approvals):
            dropped.append(f"{candidate.key}: {report.get('reason') or 'no longer eligible'}")
            continue
        selected.append(
            replace(candidate, start=entry.start, end=entry.end, source_text=entry.source_text)
        )
    if not selected:
        return CleanupResult(
            workspace=workspace,
            path=LEARNINGS_RELATIVE,
            applied=False,
            revision_before=before,
            kept=plan.kept,
            conflicts=tuple(dropped),
        )

    spliced, spans = _splice(text, selected)
    _with_anchors(spans, spliced)
    proposed = _restamp(spliced, today=stamp)
    after = content_revision(proposed)
    receipt = _build_receipt(
        plan,
        spans,
        vault_root=root,
        before=before,
        after=after,
        actor=actor,
        approvals=approvals,
        shift=len(proposed) - len(spliced),
    )
    # Prepared before the file is touched. A reverse map this code cannot
    # serialize is a removal nobody could undo, and the answer to that is to
    # remove nothing.
    json.dumps(receipt)

    try:
        _write_locked(path, proposed, expect=before)
    except _RevisionMoved:
        return CleanupResult(
            workspace=workspace,
            path=LEARNINGS_RELATIVE,
            applied=False,
            revision_before=before,
            kept=plan.kept,
            conflicts=("the file changed while the cleanup was running",),
        )
    except OSError as exc:
        return CleanupResult(
            workspace=workspace,
            path=LEARNINGS_RELATIVE,
            applied=False,
            revision_before=before,
            failed=(f"{LEARNINGS_RELATIVE}: {exc}",),
        )

    # The document is written, so the store has to catch up: a crash here leaves
    # the entries gone and unrecorded, which the *next* pass cannot distinguish
    # from work never done. Recording first would be worse — it would suppress
    # entries that are still on disk. So this is reported, not hidden, and the
    # receipt is the fallback: it names every pair the store should hold.
    remember: list[tuple[str, str]] = []
    try:
        write_suppressions(root, [(row.learning_id, row.entry_revision) for row in selected], actor=actor)
        remember = [(row.learning_id, row.entry_revision) for row in selected]
    except (OSError, _SuppressionUnreadable) as exc:
        logger.warning(
            "learnings cleanup: wrote the file but could not record the removals: %s", exc
        )
        remember = []

    return CleanupResult(
        workspace=workspace,
        path=LEARNINGS_RELATIVE,
        applied=True,
        revision_before=before,
        revision_after=after,
        removed=tuple(selected),
        kept=plan.kept,
        conflicts=tuple(dropped),
        suppressed=tuple(remember),
        receipt=receipt,
    )


REVISION_MOVED = "the learnings document changed while the cleanup was planned"


def _reapproved(
    row: CleanupRow, approvals: dict[str, dict[str, Any]]
) -> bool:
    """Whether this row was explicitly reapproved despite not being eligible.

    The attended path only. An entry nothing has ever proposed, or one whose
    finding somebody is working on right now, cannot be retired by the fold —
    there is no evidence for it and the queue is the only thing that could supply
    any. A person can say otherwise, and when they do, that decision *is* the
    evidence: it arrives with a stated reason, with the evidence behind it, bound
    to the exact bytes it was made about, and the receipt keeps all four. The
    unattended pass never sets ``reapprove``, so nothing here can be reached
    without a person in the loop.

    Every other check still runs: the document revision, the span's exact bytes,
    and the entry's own revision. What this skips is only the fold, and only for
    the row somebody has answered for.
    """
    return bool(approvals.get(row.learning_id, {}).get("reapprove"))


def _entry_at(document: Any, row: CleanupRow) -> Any:
    """The parsed entry a candidate addresses, by identity and by bytes.

    Both halves are required. The id alone is not enough — the same learning can
    legitimately appear twice in one document, and the cleanup removed one of
    them — and the bytes alone are not enough either, because a line that says
    the same thing at a different offset is a different line in a file this
    module is splicing by offset.
    """
    for entry in document.entries:
        if entry.start != row.start or entry.end != row.end:
            continue
        if entry.source_text != row.source_text:
            continue
        if entry.record is not None and entry.record.learning_id != row.learning_id:
            continue
        return entry
    return None


# ── The reverse ─────────────────────────────────────────────────────────────


def new_receipt_path(runtime_root: Path) -> Path:
    """A free timestamped receipt path, one per run.

    Its own prefix beside the migration's, so a cleanup receipt and a migration
    receipt for the same minute are two files and neither overwrites the other's
    reverse map.
    """
    from ciao.learnings_migrate import _receipt_dir

    directory = _receipt_dir(runtime_root)
    moment = datetime.now(UTC)
    for _ in range(60):
        path = directory / f"{RECEIPT_PREFIX}{moment.strftime(RECEIPT_STAMP)}.json"
        if not path.exists():
            return path
        moment = datetime.fromtimestamp(moment.timestamp() + 1, UTC)
    return directory / f"{RECEIPT_PREFIX}receipt.json"


def write_receipt(path: Path, receipt: dict[str, Any]) -> Path:
    """Persist the reverse map through a temp file and ``os.replace``.

    Called only after the write landed, so a receipt never claims spans that are
    not on disk, and written atomically so ``--revert`` never reads half of one.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(target)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return target


def read_receipt(path: Path) -> dict[str, Any] | None:
    """A cleanup receipt, or ``None`` when it cannot be read.

    A different schema is reported as absent rather than half-trusted: reversing
    from a reverse map this code does not fully understand would restore spans
    against a file it has not checked.
    """
    receipt = Path(path)
    if not receipt.is_file():
        return None
    try:
        data = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != RECEIPT_VERSION:
        return None
    return data


def unmigrate_cleanup(
    vault_root: Path,
    receipt: dict[str, Any],
    *,
    apply: bool = False,
    today: date | None = None,
) -> dict[str, Any]:
    """Put back every span a cleanup receipt records, exactly.

    Exact rather than re-derived: the bytes come from the receipt, so a line the
    owner wrote is not reconstructed by guessing at it. Back to front, so the
    recorded offsets stay valid as the text grows, and a file that disagrees with
    the receipt at any offset is reported and left **entirely** untouched — a
    half-reverted file is worse than an unreverted one.

    The suppression is deliberately *not* lifted. The undo puts the entry back
    for a person to read; leaving it suppressed is what stops the next nightly
    pass from removing the line they just restored. It comes back on its own
    when the entry is edited (a new revision) or when somebody reapproves it
    through ``--reapprove``.

    The check is on the *context* either side of each gap rather than on the
    bytes at the offset, because a removal leaves nothing at the offset to check.
    A hand edit anywhere near the removal changes that context and the whole undo
    is refused, which is the point: the run's reverse map is only an exact map of
    the file it produced.
    """
    root = Path(vault_root)
    summary: dict[str, Any] = {
        "vault_root": str(root),
        "receipt_vault_root": str(receipt.get("vault_root", "")),
        "path": str(receipt.get("path", LEARNINGS_RELATIVE)),
        "applied": bool(apply),
        "entries_reverted": 0,
        "reverted": [],
        "suppressions_kept": [],
        "failed": [],
    }
    removals = receipt.get("removals")
    if not isinstance(removals, list) or not removals:
        summary["skipped"] = "receipt records no removals to reverse"
        return summary
    path = learnings_file(root)
    if not path.is_file():
        summary["failed"].append({"path": LEARNINGS_RELATIVE, "error": "file is missing"})
        return summary
    try:
        text = _load_text(root)
    except (OSError, UnicodeDecodeError) as exc:
        summary["failed"].append({"path": LEARNINGS_RELATIVE, "error": str(exc)})
        return summary

    restored = text
    for span in sorted(
        (item for item in removals if isinstance(item, dict)),
        key=lambda item: int(item.get("offset", 0)),
        reverse=True,
    ):
        offset = int(span.get("offset", 0))
        original = str(span.get("from", ""))
        before_anchor = str(span.get("before", ""))
        after_anchor = str(span.get("after", ""))
        start = max(0, offset - len(before_anchor))
        if restored[start:offset] != before_anchor or restored[
            offset : offset + len(after_anchor)
        ] != after_anchor:
            # The context either side of this gap is not what the run left, so
            # the gap is not the same gap. Refusing the whole file rather than
            # filling this one is the same rule the migration follows: a
            # half-restored document is worse than an unrestored one, because
            # nothing in it can be trusted afterwards.
            summary["failed"].append(
                {
                    "path": LEARNINGS_RELATIVE,
                    "offset": offset,
                    "error": "the file changed since the cleanup",
                }
            )
            return summary
        restored = restored[:offset] + original + restored[offset:]
    if restored == text:
        return summary

    # The document grew, so its `updated:` is restamped like any other write.
    # Restoring the bytes without it would leave a file that has lines back while
    # still dating itself to the day they were taken out, and the shrink is
    # exactly what a reader checking this file is looking for.
    restored = _restamp(restored, today=today or datetime.now(UTC).date())

    if apply:
        try:
            _write_locked(path, restored, expect=content_revision(text))
        except _RevisionMoved:
            summary["failed"].append(
                {"path": LEARNINGS_RELATIVE, "error": "the file changed while reverting"}
            )
            return summary
        except OSError as exc:
            summary["failed"].append({"path": LEARNINGS_RELATIVE, "error": str(exc)})
            return summary
    summary["entries_reverted"] = len(removals)
    summary["reverted"].append(LEARNINGS_RELATIVE)
    summary["suppressions_kept"] = [
        {
            "learning_id": str(item.get("learning_id") or ""),
            "entry_revision": str(item.get("entry_revision") or ""),
        }
        for item in removals
        if isinstance(item, dict)
    ]
    summary["revision_before"] = content_revision(text)
    summary["revision_after"] = content_revision(restored)
    return summary



