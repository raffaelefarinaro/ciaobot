"""The persisted care run report and the exact-dispatch join that reads it (#3647).

Pure: no files, no clock, no model. Three jobs, all bounded:

- :func:`bounded_report` shapes the per-item outcomes the end of a run folded
  from its receipts into the capped ``last_run.report`` block, and
  :func:`report_valid` is the strict check both state codecs apply to it;
- :func:`select_run` picks the one ``schedule_dispatch`` job row of an exact
  dispatch id, mirroring ``care_eval_run._judge``: no row is pending, a
  duplicate or a row of another chat is ambiguous, and a row of any other
  dispatch is never matched, whatever its age;
- :func:`join_last_run` accepts the stored run only when it names that same
  dispatch id. A run with no dispatch id (written before this report existed,
  or by a path that has none) cannot be proven to belong to the dispatch, so it
  is superseded rather than guessed.

Nothing is read from a newer schedule row, from prose or from timestamps.
Trust boundary: as in :mod:`ciao.care_run_outcome`, whoever can write the vault
is trusted with it; this view yields statuses and keys, never note text.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from ciao.care_report import OUTCOME_STATUSES

MAX_ITEMS = 200
MAX_DETAIL_CHARS = 200
MAX_RECEIPT_IDS = 5
MAX_ID_CHARS = 200
MAX_KEY_CHARS = 512
MAX_WEEKLY = 8
MAX_WEEKLY_CHARS = 64

_ITEM_FIELDS = frozenset({"item_key", "pass_id", "status", "detail", "receipt_ids"})
_REPORT_FIELDS = frozenset({"items", "truncated", "weekly"})
_JOB_STATUSES = ("ok", "error", "skipped")

RunState = Literal["ok", "pending", "ambiguous"]


def bounded_report(outcomes: Sequence[Any], weekly: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The capped report block for ``outcomes`` (objects with the ``ItemOutcome`` fields).

    Items beyond :data:`MAX_ITEMS` are dropped and ``truncated`` says so. The
    ``weekly`` key is present only when given; ``end_run`` supplies it.
    """
    items = [
        {
            "item_key": str(outcome.item_key)[:MAX_KEY_CHARS],
            "pass_id": str(outcome.pass_id)[:MAX_ID_CHARS],
            "status": str(outcome.status),
            "detail": str(outcome.detail)[:MAX_DETAIL_CHARS],
            "receipt_ids": [str(rid)[:MAX_ID_CHARS] for rid in list(outcome.receipt_ids)[:MAX_RECEIPT_IDS]],
        }
        for outcome in outcomes[:MAX_ITEMS]
    ]
    report: dict[str, Any] = {"items": items, "truncated": len(outcomes) > MAX_ITEMS}
    if weekly is not None:
        report["weekly"] = dict(weekly)
    return report


def _text(value: object, limit: int) -> bool:
    return isinstance(value, str) and len(value) <= limit


def report_valid(report: object) -> bool:
    """Whether ``report`` has exactly the stored shape, types and caps."""
    if not isinstance(report, dict) or set(report) != _REPORT_FIELDS:
        return False
    items, weekly = report["items"], report["weekly"]
    if type(report["truncated"]) is not bool or not isinstance(items, list) or len(items) > MAX_ITEMS:
        return False
    if not isinstance(weekly, dict) or len(weekly) > MAX_WEEKLY:
        return False
    if any(not _text(k, MAX_WEEKLY_CHARS) or not _text(v, MAX_WEEKLY_CHARS) for k, v in weekly.items()):
        return False
    for item in items:
        if not isinstance(item, dict) or set(item) != _ITEM_FIELDS:
            return False
        ids = item["receipt_ids"]
        if not (
            _text(item["item_key"], MAX_KEY_CHARS)
            and _text(item["pass_id"], MAX_ID_CHARS)
            and item["status"] in OUTCOME_STATUSES
            and _text(item["detail"], MAX_DETAIL_CHARS)
            and isinstance(ids, list)
            and len(ids) <= MAX_RECEIPT_IDS
            and all(_text(rid, MAX_ID_CHARS) for rid in ids)
        ):
            return False
    return True


def without_invalid_report(last_run: Mapping[str, Any]) -> dict[str, Any]:
    """``last_run`` with a malformed ``report`` dropped.

    The report is a convenience view: a bad one reads as a legacy run
    (``report_available`` false) and never fails the curation state.
    """
    kept = dict(last_run)
    if "report" in kept and not report_valid(kept["report"]):
        del kept["report"]
    return kept


@dataclass(frozen=True, slots=True)
class RunSelection:
    state: RunState
    job_status: str = ""


def select_run(
    job_rows: Sequence[Mapping[str, Any]],
    schedule_id: str,
    workspace: str,
    dispatch_id: str,
    *,
    chat_id: str | None = None,
) -> RunSelection:
    """The one job row of exactly this dispatch. Filter first; nothing is limited or sorted."""
    matches: list[Mapping[str, Any]] = []
    for row in job_rows:
        extra = row.get("extra")
        if row.get("job") != "schedule_dispatch" or not isinstance(extra, Mapping):
            continue
        if (
            str(extra.get("schedule_id") or "") == schedule_id
            and str(extra.get("workspace") or "") == workspace
            and str(extra.get("dispatch_id") or "") == dispatch_id
        ):
            matches.append(row)
    if chat_id is not None and any(str(row["extra"].get("chat_id") or "") != chat_id for row in matches):
        return RunSelection("ambiguous")
    if not matches:
        return RunSelection("pending")
    if len(matches) > 1:
        return RunSelection("ambiguous")
    status = matches[0].get("status")
    if status not in _JOB_STATUSES:
        return RunSelection("ambiguous")
    return RunSelection("ok", str(status))


def join_last_run(last_run: Mapping[str, Any], dispatch_id: str) -> Literal["ok", "superseded"]:
    """``ok`` only when the stored run names this exact dispatch id."""
    if not dispatch_id or last_run.get("dispatch_id") != dispatch_id:
        return "superseded"
    return "ok"
