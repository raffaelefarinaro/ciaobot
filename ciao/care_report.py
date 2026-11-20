"""Fold a closed care report plan and durable receipts into per-item outcomes.

Pure: no files, no clock, no model, no newer schedule row. The caller reads the
receipts (``memory_receipts.read_receipts``) and names each key's operation
ids; this module only decides what the evidence says for each planned,
deferred or blocked key.

A receipt counts for a key only when its ``operation_id`` is in that key's
list. A bound receipt that names another workspace is ``incomplete``, never
silently skipped. Missing evidence is ``unfinished``, never ``applied``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, get_args

from ciao import memory_receipts as mr
from ciao.curation_run import RunPlan

Disposition = Literal["planned", "deferred", "blocked"]

OutcomeStatus = Literal[
    "blocked",
    "deferred_budget",
    "unfinished",
    "queued",
    "done_unverified",
    "applied",
    "no_change",
    "failed",
    "conflict",
    "rolled_back",
    "undone",
    "incomplete",
]

DISPOSITIONS: tuple[str, ...] = get_args(Disposition)
OUTCOME_STATUSES: tuple[str, ...] = get_args(OutcomeStatus)

# Region writes and the other kinds that write a file of their own. A region
# write is the guide leg of an approval; its queue leg is ``queue_resolve``.
_REGION_KINDS = frozenset({"region_apply", "region_update", "region_remove", "region_undo"})
_QUEUE_KIND = "queue_resolve"
_OTHER_KINDS = frozenset(
    {
        "prune_expired",
        "category_apply",
        "note_apply",
        "note_undo",
        "repo_doc_apply",
        "repo_doc_undo",
    }
)
# Every kind a journal row can carry. A row outside this set is never folded.
RECEIPT_KINDS: frozenset[str] = _REGION_KINDS | _OTHER_KINDS | {_QUEUE_KIND}

_KNOWN_STATUSES = frozenset(
    {mr.PREPARED, mr.APPLIED, mr.FAILED, mr.ROLLED_BACK, mr.CONFLICT, mr.UNDONE}
)
_VERDICTS: Mapping[str, OutcomeStatus] = {
    mr.FAILED: "failed",
    mr.CONFLICT: "conflict",
    mr.ROLLED_BACK: "rolled_back",
}

_NO_OPERATIONS: Mapping[str, Sequence[str]] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class ReportItem:
    """One key of the run, with its disposition and the operation ids it may use."""

    pass_id: str
    item_key: str
    disposition: Disposition
    reason: str = ""
    operation_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CareReportPlan:
    run_id: str
    workspace: str
    items: tuple[ReportItem, ...]


@dataclass(frozen=True, slots=True)
class ItemOutcome:
    item_key: str
    pass_id: str
    status: OutcomeStatus
    receipt_ids: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True, slots=True)
class CareRunSummary:
    run_id: str
    workspace: str
    outcomes: tuple[ItemOutcome, ...]
    counts: Mapping[str, int]
    complete: bool
    weekly: Mapping[str, str] | None = None


def report_plan_from_run(
    plan: RunPlan,
    *,
    run_id: str,
    workspace: str,
    operation_ids: Mapping[str, Sequence[str]] = _NO_OPERATIONS,
) -> CareReportPlan:
    """Flatten a run plan into one report item per (pass, key).

    Order is planned, then deferred, then blocked. A key the plan blocks is
    reported as ``blocked`` only, even though :class:`RunPlan` also lists it in
    ``deferred``.
    """
    blocked_pairs = {(item.pass_id, key) for item in plan.blocked for key in item.keys}
    items: list[ReportItem] = []
    seen: set[tuple[str, str]] = set()

    def add(pass_id: str, key: str, disposition: Disposition, reason: str) -> None:
        if (pass_id, key) in seen:
            return
        seen.add((pass_id, key))
        items.append(
            ReportItem(
                pass_id=pass_id,
                item_key=key,
                disposition=disposition,
                reason=reason,
                operation_ids=tuple(operation_ids.get(key, ())),
            )
        )

    for item in plan.planned:
        for key in item.keys:
            add(item.pass_id, key, "planned", item.reason)
    for item in plan.deferred:
        for key in item.keys:
            if (item.pass_id, key) not in blocked_pairs:
                add(item.pass_id, key, "deferred", item.reason)
    for item in plan.blocked:
        for key in item.keys:
            add(item.pass_id, key, "blocked", item.reason)
    return CareReportPlan(run_id=run_id, workspace=workspace, items=tuple(items))


def summarize_care_run(
    plan: CareReportPlan,
    receipts: Sequence[Mapping[str, Any]],
    *,
    done_keys: frozenset[str] | set[str] = frozenset(),
    weekly: Mapping[str, str] | None = None,
) -> CareRunSummary:
    """Fold the receipts into one outcome per plan item, in plan order.

    ``receipts`` are the effective rows of the journal, one per id, oldest
    first. ``done_keys`` says which keys a pass reported finished; it only
    decides what a key with no receipt means, never more than that. ``weekly``
    is carried through untouched.
    """
    by_operation: dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for position, receipt in enumerate(receipts):
        operation = str(receipt.get("operation_id", ""))
        if operation:
            by_operation.setdefault(operation, []).append((position, receipt))

    outcomes = tuple(
        _outcome(item, plan.workspace, by_operation, done_keys) for item in plan.items
    )

    counts: dict[str, int] = {status: 0 for status in OUTCOME_STATUSES}
    counts["planned"] = sum(1 for item in plan.items if item.disposition == "planned")
    for outcome in outcomes:
        counts[outcome.status] += 1
    complete = not any(o.status in ("incomplete", "queued") for o in outcomes)
    return CareRunSummary(
        run_id=plan.run_id,
        workspace=plan.workspace,
        outcomes=outcomes,
        counts=counts,
        complete=complete,
        weekly=weekly,
    )


def _outcome(
    item: ReportItem,
    workspace: str,
    by_operation: Mapping[str, list[tuple[int, Mapping[str, Any]]]],
    done_keys: frozenset[str] | set[str],
) -> ItemOutcome:
    bound: list[tuple[int, Mapping[str, Any]]] = []
    for operation in dict.fromkeys(item.operation_ids):
        bound.extend(by_operation.get(operation, ()))
    bound.sort(key=lambda pair: pair[0])
    rows = [receipt for _, receipt in bound]
    # Sorted, so the outcome does not depend on the order the journal was read in.
    ids = tuple(sorted(str(receipt.get("id", "")) for receipt in rows))

    if not rows:
        status, detail = _without_evidence(item, done_keys)
        return ItemOutcome(item.item_key, item.pass_id, status, ids, detail)

    problem = _integrity_problem(rows, workspace)
    if problem:
        return ItemOutcome(item.item_key, item.pass_id, "incomplete", ids, problem)
    collapsed, problem = _collapse(rows)
    if problem:
        return ItemOutcome(item.item_key, item.pass_id, "incomplete", ids, problem)
    status, detail = _fold(collapsed)
    return ItemOutcome(item.item_key, item.pass_id, status, ids, detail)


def _without_evidence(
    item: ReportItem, done_keys: frozenset[str] | set[str]
) -> tuple[OutcomeStatus, str]:
    if item.disposition == "blocked":
        return "blocked", "blocked in this release"
    if item.disposition == "deferred":
        return "deferred_budget", "the run budget did not reach this key"
    if item.item_key in done_keys:
        return "done_unverified", "the pass reports done, with no effect receipt"
    return "unfinished", "no receipt for this key"


def _integrity_problem(rows: Sequence[Mapping[str, Any]], workspace: str) -> str:
    for row in rows:
        if str(row.get("kind", "")) not in RECEIPT_KINDS:
            return "unknown receipt kind"
        if str(row.get("status", "")) not in _KNOWN_STATUSES:
            return "unknown receipt status"
        bound_workspace = str(row.get("workspace", ""))
        if bound_workspace and bound_workspace != workspace:
            return "receipt names another workspace"
    return ""


def _collapse(rows: Sequence[Mapping[str, Any]]) -> tuple[list[Mapping[str, Any]], str]:
    """Keep one row per (operation, kind). Duplicates must agree on the shape."""
    first: dict[tuple[str, str], Mapping[str, Any]] = {}
    kept: list[Mapping[str, Any]] = []
    for row in rows:
        group = (str(row.get("operation_id", "")), str(row.get("kind", "")))
        seen = first.get(group)
        if seen is None:
            first[group] = row
            kept.append(row)
        elif _shape(seen) != _shape(row):
            return [], "conflicting duplicate receipts for one operation"
    return kept, ""


def _shape(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        row.get("status"),
        row.get("before_revision"),
        row.get("after_revision"),
        row.get("changed"),
    )


def _fold(rows: Sequence[Mapping[str, Any]]) -> tuple[OutcomeStatus, str]:
    """The verdict for one key's collapsed rows, most severe evidence first."""
    statuses = [str(row.get("status", "")) for row in rows]
    if mr.UNDONE in statuses:
        return "undone", "reversed after it was applied"

    guide = [r for r in rows if str(r.get("kind", "")) in (_REGION_KINDS | _OTHER_KINDS)]
    queue = [r for r in rows if str(r.get("kind", "")) == _QUEUE_KIND]
    region = [r for r in guide if str(r.get("kind", "")) in _REGION_KINDS]

    # A guide write decides its own status. The queue leg cannot change that.
    for row in guide:
        verdict = _VERDICTS.get(str(row.get("status", "")))
        if verdict:
            return verdict, "the guide write did not land"

    if any(str(r.get("status")) == mr.PREPARED for r in guide):
        if any(str(r.get("status")) == mr.APPLIED for r in [*guide, *queue]):
            return "incomplete", "a guide write is prepared beside an applied one"
        return "queued", "the guide write is prepared and not yet confirmed"

    if region:
        # Approved vs applied: a guide write without a confirmed queue leg is
        # not a finished decision.
        if not queue:
            return "incomplete", "guide applied, queue receipt missing"
        if any(str(r.get("status")) != mr.APPLIED for r in queue):
            return "incomplete", "guide applied, queue receipt not applied"
    else:
        for row in queue:
            verdict = _VERDICTS.get(str(row.get("status", "")))
            if verdict:
                return verdict, "the queue resolution did not land"

    if any(str(r.get("status")) == mr.PREPARED for r in queue):
        if guide:
            return "incomplete", "a queue receipt is prepared beside an applied guide write"
        return "queued", "the queue receipt is prepared and not yet confirmed"

    if any(bool(r.get("promoted")) for r in queue) and not region:
        return "unfinished", "approved in the queue, but no guide receipt exists"

    if guide and all(r.get("changed") is False for r in guide):
        return "no_change", "the destination already held the decided content"
    return "applied", ""
