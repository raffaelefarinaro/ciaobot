"""One managed accept of a pending ``[memory]``/``[profile]`` queue row.

:func:`apply_memory_accept` is the only accept path for an unattended run. It
performs two effects, the region write and the queue settlement, and records
one decision that owns both. It refuses at every step where authority can have
changed, and it can finish an operation that crashed between effects, because
every receipt it writes carries its ``operation_id``.

Lock order is guide, then queue, then state (see
:func:`ciao.curation_run.authorize_holder`). The region write takes the guide
lock inside :func:`ciao.memory_proposals.accept_region_fact`; the queue is only
ever taken after that, never around it.

The cap is counted from durable records, not from decisions: each operation
that wrote a region or settled a queue row left an agent receipt, and the
region write checks the count under the guide lock before it writes. Two
operations therefore cannot both take the fifth slot, because the first one's
receipt is on disk before the guide lock is released.

Authority is re-checked in three places: before the guide write, under the
guide lock during the write (through ``authorize``), and again under the queue
lock before the row is settled. Handover is read again at each of them.
A covered or exact-duplicate verdict writes no guide receipt, so the queue
re-check is the only check that row gets.

A row the guide write has already settled but whose queue removal did not land
is never removed unless the same row is still queued and the run still holds
the lease; otherwise the result is :class:`PartialConflict`, which leaves
recovery and undo as they are.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ciao import memory_proposals
from ciao.curation_run import (
    PASS_PROPOSALS,
    PROPOSALS_RELATIVE,
    CurationBusy,
    CurationStateError,
    _proposal_subject,
    active_lease,
    authorize_holder,
    item_key,
    load_state,
    record_done,
)
from ciao.fact_candidates import external_anchor
from ciao.memory_audit import strip_learned_stamp
from ciao.memory_proposals import (
    read_decisions,
    record_promotion,
    remove_proposal_by_substring,
)
from ciao.memory_receipts import (
    APPLIED,
    PREPARED,
    QueueReceiptUnavailable,
    content_revision,
    destination_revision,
    find_receipts_by_operation_id,
    journal_path,
    queue_lock,
    queue_resolution,
    read_receipts,
)
from ciao.memory_tool import ensure_regions, read_region
from ciao.proposal_kinds import ProposalBullet
from ciao.proposal_predictions import (
    HANDOVER_ACCEPT_CAP,
    REGION_KINDS,
    handover_allows,
    latest_predictions,
    prediction_key,
)
from ciao.proposal_tracking import QueuedProposal, walk_proposal_queue
from ciao.workspace_discovery import trusted_registry
from ciao.workspace_guide import guide_path

REGION_APPLY_KIND = "region_apply"
QUEUE_RESOLVE_KIND = "queue_resolve"
_OPERATION_KINDS = frozenset({REGION_APPLY_KIND, QUEUE_RESOLVE_KIND})
_UNPREPARED_CODES = frozenset({"missing", "missing_file"})

# Fixed reasons. None of them echoes proposal text.
REFUSED_NO_OPERATION = "the accept has no operation id or holder"
REFUSED_REGISTRY = "the installed engine does not register this workspace at this vault"
REFUSED_HANDOVER = "the workspace handover does not allow an accept"
REFUSED_KIND = "only [memory] and [profile] rows can be handed over"
REFUSED_DECIDE_YOU = "the row is marked decide: you"
REFUSED_GONE = "the row is no longer queued"
REFUSED_CHANGED = "the row changed since it was judged"
REFUSED_PREDICTION = "no stored accept prediction for this row"
REFUSED_NO_LEASE = "accepting needs a live curation run (take the lease with curation-begin)"
REFUSED_UNCERTAIN = "curation state is uncertain; handover is refused"
REFUSED_CAP = f"this curation run already accepted its cap of {HANDOVER_ACCEPT_CAP} facts"
REFUSED_NOT_AUTHORISED = "the run no longer holds the lease; the fact stays queued"
REFUSED_WRITE_NOT_AUTHORISED = "the write was not authorised; it stays queued"
REFUSED_UNSHAPED = "this reads as an event, not a standing rule; it stays queued"
REFUSED_DEFERRED = "this fact may supersede an entry already in the region; it stays queued"
REFUSED_DESTINATION = "the region changed while this was being applied; retry"
REFUSED_REGION_UNREADABLE = "the region could not be read"
FAILED_GUIDE_WRITE = "the region could not be written or its lock taken; the fact stays queued"
FAILED_INTERRUPTED = "an interrupted write for this operation must be recovered first"
FAILED_QUEUE_RECEIPT = "the memory receipt journal is unavailable; the row was not removed"
FAILED_LEASE_LOST = "this run no longer holds the curation lease; progress was not recorded"
FAILED_REMOVAL = "the queue row could not be removed exactly"
FAILED_VERIFY = "the queue or region did not show the expected state after the write"
FAILED_DECISION = "the decision could not be recorded; a retry finishes it"


@dataclass(frozen=True, slots=True)
class ManagedMemoryAcceptIntent:
    """What a caller judged, bound to the revisions it judged against."""

    operation_id: str
    workspace: str
    item_key: str
    proposal_id: str
    proposal_revision: str
    destination_revision: str
    prediction_revision: str
    holder: str


@dataclass(frozen=True, slots=True)
class Accepted:
    receipt_id: str
    queue_receipt_id: str
    outcome: Literal["written", "duplicate"]
    region: str


@dataclass(frozen=True, slots=True)
class AlreadyDone:
    receipt_id: str


@dataclass(frozen=True, slots=True)
class Refused:
    reason: str


@dataclass(frozen=True, slots=True)
class PartialConflict:
    guide_receipt_id: str


@dataclass(frozen=True, slots=True)
class Failed:
    reason: str


AcceptOutcome = Accepted | AlreadyDone | Refused | PartialConflict | Failed


def accept_item_key(bullet: ProposalBullet, dup: int) -> str:
    """The progress key the nightly worklist uses for an accept of this row."""
    return item_key(PASS_PROPOSALS, f"{_proposal_subject(bullet, dup)}\x00act:accept")


def accept_operation_id(workspace: str, proposal_id: str, revision: str, prediction: str) -> str:
    """The operation id for one row as judged.

    The prediction is part of the identity: a fact re-queued and predicted again
    is a new judgment and a new operation, while a retry of the same judgment
    reproduces the same id and resumes it.
    """
    payload = json.dumps([workspace, proposal_id, revision, prediction], ensure_ascii=False)
    return "cop_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def proposal_revision(bullet: ProposalBullet) -> str:
    """Digest of the row as the queue holds it, decider included."""
    return content_revision(
        json.dumps(
            [bullet.kind, bullet.text, bullet.source, bullet.decider or "", bullet.decide_reason],
            ensure_ascii=False,
        )
    )


def prediction_revision(row: dict[str, str]) -> str:
    """Digest of one stored prediction row."""
    return content_revision(
        json.dumps(
            [row.get("predicted_at", ""), row.get("verdict", ""), row.get("reason", "")],
            ensure_ascii=False,
        )
    )


def _read_queue(queue: Path) -> str:
    try:
        return queue.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _queued_row(queue: Path, workspace: str, proposal_id: str) -> QueuedProposal | None:
    """The one queued row with this id, or None when it is absent or ambiguous."""
    matches = [
        entry
        for entry in walk_proposal_queue(workspace, PROPOSALS_RELATIVE, _read_queue(queue))
        if entry.proposal_id == proposal_id
    ]
    return matches[0] if len(matches) == 1 else None


def plan_memory_accept(
    vault_root: Path,
    *,
    workspace: str,
    guide: Path,
    bullet: ProposalBullet,
    holder: str,
) -> ManagedMemoryAcceptIntent | Refused:
    """Bind one queued row, as the caller sees it now, into an intent.

    The operation id is derived from the row and its revision, so a retry of the
    same judged row resumes the same operation. The revisions are snapshots: the
    operation re-checks every one of them under its locks before it writes.
    """
    queue = Path(vault_root) / PROPOSALS_RELATIVE
    with queue_lock(queue):
        matches = [
            entry
            for entry in walk_proposal_queue(workspace, PROPOSALS_RELATIVE, _read_queue(queue))
            if entry.bullet == bullet
        ]
        predicted = latest_predictions(queue).get(prediction_key(bullet.kind, bullet.text))
    if len(matches) != 1:
        return Refused(REFUSED_GONE)
    try:
        entries, diags = read_region(guide, bullet.kind)
    except (OSError, ValueError):
        return Refused(REFUSED_REGION_UNREADABLE)
    # A guide without this region's markers yet reads as an empty region: the
    # write prepares the markers, and an empty body is the same revision.
    if any(diag.code not in _UNPREPARED_CODES for diag in diags):
        return Refused(REFUSED_REGION_UNREADABLE)
    entries = [] if diags else entries
    entry = matches[0]
    revision = proposal_revision(bullet)
    predicted_as = prediction_revision(predicted) if predicted else ""
    return ManagedMemoryAcceptIntent(
        operation_id=accept_operation_id(workspace, entry.proposal_id, revision, predicted_as),
        workspace=workspace,
        item_key=accept_item_key(bullet, entry.dup),
        proposal_id=entry.proposal_id,
        proposal_revision=revision,
        destination_revision=destination_revision(entries),
        prediction_revision=predicted_as,
        holder=holder,
    )


def _handover_open(vault: Path, workspace: str) -> bool:
    """Whether the installed registry allows an accept for this workspace, read now."""
    trusted = trusted_registry(workspace, vault)
    if trusted is None:
        return False
    registered = trusted.workspace(workspace)
    return registered is not None and handover_allows(registered.memory_handover, "accept")


def _since(stamp: object, started: datetime) -> bool:
    """Whether a recorded time is at or after the lease start. Unreadable times do not count."""
    try:
        moment = datetime.fromisoformat(str(stamp))
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment >= started


def _agent_slots(vault: Path, queue: Path, started: datetime, *, exclude: str) -> int:
    """How many of this run's accept slots are taken, not counting ``exclude``.

    An operation takes a slot when it writes its agent receipts (region or queue)
    and keeps it when its decision is recorded, so both are counted by operation
    id. A decision with no operation id (written before operations existed) takes
    one slot of its own.
    """
    operations: set[str] = set()
    for row in read_receipts(journal_path(vault)):
        if row.get("actor") != "agent" or row.get("kind") not in _OPERATION_KINDS:
            continue
        operation = str(row.get("operation_id", ""))
        if operation and _since(row.get("ts"), started):
            operations.add(operation)
    unowned = 0
    for row in read_decisions(queue):
        if row.get("via") != "agent" or row.get("action") != "accepted":
            continue
        if not _since(row.get("ts"), started):
            continue
        operation = str(row.get("operation_id", ""))
        if operation:
            operations.add(operation)
        else:
            unowned += 1
    operations.discard(exclude)
    return len(operations) + unowned


def _check_row(
    vault: Path, queue: Path, intent: ManagedMemoryAcceptIntent
) -> tuple[QueuedProposal | None, datetime | None, str]:
    """Every policy check that needs the queue or the registry, read now.

    Returns ``(row, lease_started_at, "")`` when the accept may proceed, else
    ``(None, None, reason)``. The caller holds the queue lock.
    """
    if not _handover_open(vault, intent.workspace):
        return None, None, REFUSED_HANDOVER
    entry = _queued_row(queue, intent.workspace, intent.proposal_id)
    if entry is None:
        return None, None, REFUSED_GONE
    bullet = entry.bullet
    if bullet.kind not in REGION_KINDS:
        return None, None, REFUSED_KIND
    if bullet.decider == "you":
        return None, None, REFUSED_DECIDE_YOU
    if proposal_revision(bullet) != intent.proposal_revision:
        return None, None, REFUSED_CHANGED
    predicted = latest_predictions(queue).get(prediction_key(bullet.kind, bullet.text))
    if (
        predicted is None
        or predicted.get("verdict") != "accept"
        or prediction_revision(predicted) != intent.prediction_revision
    ):
        return None, None, REFUSED_PREDICTION
    try:
        lease = active_lease(vault)
    except CurationStateError:
        return None, None, REFUSED_UNCERTAIN
    if lease is None:
        return None, None, REFUSED_NO_LEASE
    try:
        started = datetime.fromisoformat(str(lease.get("started_at", "")))
    except ValueError:
        return None, None, REFUSED_NO_LEASE
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    if _agent_slots(vault, queue, started, exclude=intent.operation_id) >= HANDOVER_ACCEPT_CAP:
        return None, None, REFUSED_CAP
    return entry, started, ""


@contextmanager
def _holder_may_accept(
    vault: Path,
    queue: Path,
    *,
    holder: str,
    started: datetime,
    operation_id: str,
) -> Iterator[bool]:
    """The lease holder is still this run and a slot is free for this operation.

    Holds the state lock for the caller's whole write, so the receipts it counts
    cannot be added to underneath it.
    """
    with authorize_holder(vault, holder=holder) as live:
        yield bool(live) and (
            _agent_slots(vault, queue, started, exclude=operation_id) < HANDOVER_ACCEPT_CAP
        )


def _entries_for(queue: Path, workspace: str, proposal_id: str) -> int:
    return sum(
        1
        for entry in walk_proposal_queue(workspace, PROPOSALS_RELATIVE, _read_queue(queue))
        if entry.proposal_id == proposal_id
    )


def _decision_for(queue: Path, operation_id: str) -> dict[str, Any] | None:
    for row in read_decisions(queue):
        if row.get("operation_id") == operation_id:
            return row
    return None


def _receipts(vault: Path, operation_id: str, kind: str) -> list[dict[str, Any]]:
    return [
        row
        for row in find_receipts_by_operation_id(vault, operation_id)
        if row.get("kind") == kind
    ]


def _progress(vault: Path, intent: ManagedMemoryAcceptIntent) -> str:
    """Record the progress key once the effects are verified. "" or a failure reason."""
    try:
        record_done(vault, [intent.item_key], holder=intent.holder)
    except CurationBusy:
        return FAILED_LEASE_LOST
    return ""


def _write_region(
    vault: Path,
    queue: Path,
    guide: Path,
    intent: ManagedMemoryAcceptIntent,
    bullet: ProposalBullet,
    started: datetime,
) -> tuple[Literal["written", "duplicate"], str] | Refused | Failed:
    """The guide write, under the guarded path. Returns ``(outcome, receipt_id)``."""
    try:
        ensure_regions(guide)
    except (OSError, ValueError):
        return Failed(FAILED_GUIDE_WRITE)
    receipt: dict[str, Any] = {}
    try:
        outcome, _written = memory_proposals.accept_region_fact(
            guide_path=guide,
            target=bullet.kind,
            text=bullet.text,
            vault_root=vault,
            actor="agent",
            source="cli",
            workspace=intent.workspace,
            source_section=external_anchor(bullet.source) or "review",
            receipt_out=receipt,
            expected_revision=intent.destination_revision,
            authorize=lambda: _holder_may_accept(
                vault,
                queue,
                holder=intent.holder,
                started=started,
                operation_id=intent.operation_id,
            ),
            operation_id=intent.operation_id,
            run_id=intent.holder,
            item_key=intent.item_key,
        )
    except (ValueError, OSError):
        return Failed(FAILED_GUIDE_WRITE)
    if outcome == "written":
        return "written", str(receipt.get("id", ""))
    if outcome == "duplicate":
        return "duplicate", str(receipt.get("id", ""))
    if outcome == "failed":
        return Failed(FAILED_GUIDE_WRITE)
    reasons = {
        "unshaped": REFUSED_UNSHAPED,
        "refused": REFUSED_WRITE_NOT_AUTHORISED,
        "conflict": REFUSED_DESTINATION,
        "deferred": REFUSED_DEFERRED,
    }
    return Refused(reasons.get(outcome, REFUSED_DESTINATION))


def _settle_queue(
    vault: Path,
    queue: Path,
    intent: ManagedMemoryAcceptIntent,
    *,
    guide_receipt_id: str,
) -> tuple[ProposalBullet, str] | Refused | PartialConflict | Failed:
    """Remove the queue row under the queue lock and the state lock.

    Returns ``(bullet, queue_receipt_id)``. Once the guide has been written
    (``guide_receipt_id`` set), any reason to stop is a :class:`PartialConflict`:
    the region already holds the fact.
    """

    def stop(reason: str) -> Refused | PartialConflict:
        return PartialConflict(guide_receipt_id) if guide_receipt_id else Refused(reason)

    with queue_lock(queue):
        entry, started, reason = _check_row(vault, queue, intent)
        if entry is None or started is None:
            return stop(reason or REFUSED_GONE)
        with _holder_may_accept(
            vault,
            queue,
            holder=intent.holder,
            started=started,
            operation_id=intent.operation_id,
        ) as allowed:
            if not allowed:
                return stop(REFUSED_NOT_AUTHORISED)
            needle = entry.raw.strip()
            lines = [
                line
                for line in _read_queue(queue).splitlines()
                if needle.casefold() in line.casefold()
            ]
            if len(lines) != 1:
                return stop(FAILED_REMOVAL)
            try:
                with queue_resolution(
                    queue,
                    removed_text=entry.bullet.text,
                    kind=entry.bullet.kind,
                    promoted=True,
                    actor="agent",
                    source="cli",
                    workspace=intent.workspace,
                    vault_root=vault,
                    operation_id=intent.operation_id,
                    run_id=intent.holder,
                    item_key=intent.item_key,
                    proposal_id=intent.proposal_id,
                    proposal_source=entry.bullet.source,
                ) as receipt:
                    removed = remove_proposal_by_substring(queue, needle)
            except QueueReceiptUnavailable:
                return stop(FAILED_QUEUE_RECEIPT)
            if removed is None or _entries_for(queue, intent.workspace, intent.proposal_id):
                return stop(FAILED_REMOVAL)
    return entry.bullet, str(receipt.get("id", ""))


def _verify_region(guide: Path, bullet_kind: str, text: str) -> bool:
    try:
        entries, diags = read_region(guide, bullet_kind)
    except (OSError, ValueError):
        return False
    return not diags and any(text in strip_learned_stamp(entry) for entry in entries)


def apply_memory_accept(intent: ManagedMemoryAcceptIntent, *, vault_root: Path) -> AcceptOutcome:
    """Perform one pending accept as a two-effect operation owned by ``intent.operation_id``."""
    vault = Path(vault_root)
    queue = vault / PROPOSALS_RELATIVE
    if not (intent.operation_id and intent.holder and intent.workspace):
        return Refused(REFUSED_NO_OPERATION)

    receipts = find_receipts_by_operation_id(vault, intent.operation_id)
    if any(row.get("status") == PREPARED for row in receipts):
        return Failed(FAILED_INTERRUPTED)
    guide_rows = [
        r for r in receipts if r.get("kind") == REGION_APPLY_KIND and r.get("status") == APPLIED
    ]
    queue_rows = [
        r for r in receipts if r.get("kind") == QUEUE_RESOLVE_KIND and r.get("status") == APPLIED
    ]
    guide_receipt_id = str(guide_rows[0]["id"]) if guide_rows else ""

    # Exactly one decision per operation, written after both effects. Finding it
    # means the operation finished; only the progress key may still be missing.
    decided = _decision_for(queue, intent.operation_id)
    if decided is not None:
        failure = _progress(vault, intent)
        return Failed(failure) if failure else AlreadyDone(str(decided.get("receipt_id", "")))

    trusted = trusted_registry(intent.workspace, vault)
    if trusted is None:
        return PartialConflict(guide_receipt_id) if guide_receipt_id else Refused(REFUSED_REGISTRY)
    guide = guide_path(trusted.agent_root(intent.workspace))

    bullet: ProposalBullet
    queue_receipt_id = ""
    if guide_receipt_id or queue_rows:
        # Resuming: the guide write (or the queue removal) already landed. The
        # queue settlement below re-checks handover and authority before it moves.
        outcome: Literal["written", "duplicate"] = "written" if guide_receipt_id else "duplicate"
        if queue_rows:
            bullet = ProposalBullet(
                kind=str(queue_rows[0].get("proposal_kind", "")),
                text=str(queue_rows[0].get("removed_text", "")),
                source=str(queue_rows[0].get("proposal_source", "")),
            )
            queue_receipt_id = str(queue_rows[0]["id"])
    else:
        with queue_lock(queue):
            entry, started, reason = _check_row(vault, queue, intent)
        if entry is None or started is None:
            return Refused(reason or REFUSED_GONE)
        written = _write_region(vault, queue, guide, intent, entry.bullet, started)
        if isinstance(written, (Refused, Failed)):
            return written
        outcome, guide_receipt_id = written
        bullet = entry.bullet

    if not queue_receipt_id:
        settled = _settle_queue(vault, queue, intent, guide_receipt_id=guide_receipt_id)
        if not isinstance(settled, tuple):
            return settled
        bullet, queue_receipt_id = settled

    if outcome == "written" and not _verify_region(guide, bullet.kind, bullet.text):
        return PartialConflict(guide_receipt_id)
    if _entries_for(queue, intent.workspace, intent.proposal_id):
        return PartialConflict(guide_receipt_id) if guide_receipt_id else Failed(FAILED_VERIFY)

    try:
        with queue_lock(queue):
            recorded = record_promotion(
                queue,
                text=bullet.text,
                kind=bullet.kind,
                via="agent",
                source=bullet.source,
                destination=f"ciao:{bullet.kind}",
                outcome=outcome,
                proposal_id=intent.proposal_id,
                receipt_id=guide_receipt_id,
                operation_id=intent.operation_id,
                run_id=intent.holder,
                item_key=intent.item_key,
            )
    except Exception:  # noqa: BLE001 — an unrecorded decision is finished by the replay
        recorded = False
    if not recorded:
        return Failed(FAILED_DECISION)
    failure = _progress(vault, intent)
    if failure:
        return Failed(failure)
    return Accepted(
        receipt_id=guide_receipt_id,
        queue_receipt_id=queue_receipt_id,
        outcome=outcome,
        region=bullet.kind,
    )


def find_resumable_accept(
    vault: Path, *, workspace: str, holder: str, text: str
) -> dict[str, Any] | None:
    """The one unfinished accept of this run that ``text`` names, as its queue receipt.

    Used when the queue row is already gone, so the row cannot be matched. A
    receipt matches when this run (``holder``) and this workspace wrote it, its
    removed text equals ``text`` after whitespace flattening, and its operation
    is unfinished: no decision yet, or a decision whose progress key is not
    recorded. Anything other than exactly one such operation returns None.
    """
    if not (workspace and holder and " ".join(text.split())):
        return None
    wanted = " ".join(text.split())
    queue = Path(vault) / PROPOSALS_RELATIVE
    done = load_state(vault).done_keys
    decided = {str(row["operation_id"]) for row in read_decisions(queue) if row.get("operation_id")}
    candidates = [
        row
        for row in read_receipts(journal_path(vault))
        if row.get("kind") == QUEUE_RESOLVE_KIND
        and row.get("status") == APPLIED
        and row.get("actor") == "agent"
        and row.get("workspace") == workspace
        and row.get("run_id") == holder
        and row.get("operation_id")
        and row.get("item_key")
        and row.get("proposal_id")
        and row.get("proposal_kind") in REGION_KINDS
        and " ".join(str(row.get("removed_text", "")).split()) == wanted
    ]
    unfinished = [
        row
        for row in candidates
        if not (str(row["operation_id"]) in decided and str(row["item_key"]) in done)
    ]
    operations = {str(row["operation_id"]) for row in unfinished}
    if len(operations) != 1 or len(unfinished) != 1:
        return None
    return unfinished[0]


def resume_memory_accept(
    vault_root: Path, *, workspace: str, holder: str, receipt: dict[str, Any]
) -> AcceptOutcome:
    """Finish an interrupted accept from its queue receipt, under the same authority.

    The row is already removed, so there is no row to re-check: the receipt is
    the record of what was removed and which operation removed it. Handover,
    the live lease held by ``holder``, and the cap are read here, as a new accept
    reads them; :func:`apply_memory_accept` then finishes the operation's
    missing decision or progress key under its own checks.
    """
    vault = Path(vault_root)
    queue = vault / PROPOSALS_RELATIVE
    operation_id = str(receipt.get("operation_id", ""))
    if not _handover_open(vault, workspace):
        return Refused(REFUSED_HANDOVER)
    try:
        lease = active_lease(vault)
    except CurationStateError:
        return Refused(REFUSED_UNCERTAIN)
    if lease is None or lease.get("holder") != holder:
        return Refused(REFUSED_NO_LEASE)
    try:
        started = datetime.fromisoformat(str(lease.get("started_at", "")))
    except ValueError:
        return Refused(REFUSED_NO_LEASE)
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    if _agent_slots(vault, queue, started, exclude=operation_id) >= HANDOVER_ACCEPT_CAP:
        return Refused(REFUSED_CAP)
    intent = ManagedMemoryAcceptIntent(
        operation_id=operation_id,
        workspace=workspace,
        item_key=str(receipt.get("item_key", "")),
        proposal_id=str(receipt.get("proposal_id", "")),
        proposal_revision="",
        destination_revision="",
        prediction_revision="",
        holder=holder,
    )
    return apply_memory_accept(intent, vault_root=vault)
