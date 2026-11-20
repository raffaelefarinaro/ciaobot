"""Schedule-run driver for the care measurement harness.

Runs one materialized system schedule through the real schedule-run route of
an engine the caller already owns, and returns one closed outcome. It never
boots, stops or installs an engine, never calls a model, and never calls
``task_eval.start_prompt`` in place of the schedule.

Order of calls: GET the schedule list (exact id, ``scope == "system"``, the
expected workspace), PATCH ``enabled: false`` if still enabled and GET again to
confirm it stuck (so auto-fire cannot race the manual trigger), POST
``/api/schedule-run/{id}`` with no body, GET again for ``last_dispatch_id``,
then poll the job-run log for the one settled row carrying exactly that
dispatch id, schedule, workspace and chat.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ciao.job_runs import JOB_RUNS_NAME
from ciao.task_eval import EngineAPIError

# Ceiling on one read of the runtime job-run log. Above it the file is refused
# rather than truncated: a truncated read could hide the settled row and turn
# a finished run into a timeout.
MAX_JOB_RUNS_BYTES: int = 16 * 1024 * 1024

REASON_NOT_FOUND = "schedule not found"
REASON_NOT_SYSTEM = "schedule is not a system schedule"
REASON_WORKSPACE = "schedule belongs to another workspace"
REASON_LIST_UNREADABLE = "schedule list unreadable"
REASON_PAUSE_NOT_STUCK = "pause did not stick"
REASON_CONFLICT = "schedule is paused or busy"
REASON_ENGINE_REFUSED = "engine refused the schedule run"
REASON_NO_DISPATCH_ID = "no dispatch id recorded"

AMBIGUOUS_DUPLICATE_SCHEDULE = "more than one schedule has this id"
AMBIGUOUS_DUPLICATE_ROW = "two settled rows match the dispatch"
AMBIGUOUS_OTHER_CHAT = "a settled row names another chat for this dispatch"
AMBIGUOUS_UNKNOWN_STATUS = "settled row has an unknown status"
AMBIGUOUS_UNREADABLE = "job run log unreadable"

_HTTP_CODE = re.compile(r" -> (\d{3}): ")
_MISSING_CHAT_TEXT = "no longer exists"


class JobRunsUnreadable(RuntimeError):
    """The runtime job-run log is a symlink, oversize, or not readable UTF-8."""


@dataclass(frozen=True)
class ScheduleRunRequest:
    schedule_id: str
    workspace: str
    deadline_s: float
    poll_s: float


@dataclass(frozen=True)
class JobRunRef:
    started_at: str
    ended_at: str
    duration_ms: int


@dataclass(frozen=True)
class Settled:
    status: Literal["ok", "error", "skipped"]
    dispatch_id: str
    chat_id: str
    job_run: JobRunRef


@dataclass(frozen=True)
class TimedOut:
    dispatch_id: str


@dataclass(frozen=True)
class Refused:
    reason: str


@dataclass(frozen=True)
class Ambiguous:
    reason: str


@dataclass(frozen=True)
class Superseded:
    pass


@dataclass(frozen=True)
class MissingChat:
    pass


ScheduleRunOutcome = Settled | TimedOut | Refused | Ambiguous | Superseded | MissingChat


class _Stop(Exception):
    def __init__(self, outcome: ScheduleRunOutcome) -> None:
        super().__init__(type(outcome).__name__)
        self.outcome = outcome


def run_schedule(
    api: Callable[..., Any],
    read_job_runs: Callable[[], list[dict[str, Any]]],
    request: ScheduleRunRequest,
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> ScheduleRunOutcome:
    """Trigger one system schedule through the real route and settle it.

    ``api`` has the ``IsolatedEngine.api(method, path, body)`` shape and raises
    ``EngineAPIError`` on an error status; ``read_job_runs`` returns the rows of
    the engine's own ``job_runs.jsonl``. Neither is called with anything else.
    """
    try:
        return _drive(api, read_job_runs, request, clock, sleep)
    except _Stop as stop:
        return stop.outcome
    except JobRunsUnreadable:
        return Ambiguous(AMBIGUOUS_UNREADABLE)


def _drive(
    api: Callable[..., Any],
    read_job_runs: Callable[[], list[dict[str, Any]]],
    request: ScheduleRunRequest,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> ScheduleRunOutcome:
    schedule_path = f"/api/schedules/{request.schedule_id}"

    entry = _system_entry(api, request)
    if bool(entry.get("enabled")):
        _call(api, "PATCH", schedule_path, {"enabled": False})
        entry = _system_entry(api, request)
        if bool(entry.get("enabled")):
            raise _Stop(Refused(REASON_PAUSE_NOT_STUCK))

    result = _call(api, "POST", f"/api/schedule-run/{request.schedule_id}", None)
    chat_id = str(result.get("chat_id") or "") if isinstance(result, dict) else ""
    if not chat_id:
        raise _Stop(MissingChat())

    entry = _system_entry(api, request)
    dispatch_id = str(entry.get("last_dispatch_id") or "")
    if not dispatch_id:
        raise _Stop(Refused(REASON_NO_DISPATCH_ID))

    deadline = clock() + request.deadline_s
    while True:
        outcome, other_dispatch_seen = _judge(read_job_runs(), request, dispatch_id, chat_id)
        if outcome is not None:
            return outcome
        if clock() >= deadline:
            return Superseded() if other_dispatch_seen else TimedOut(dispatch_id=dispatch_id)
        sleep(request.poll_s)


def _call(api: Callable[..., Any], method: str, path: str, body: Any) -> Any:
    try:
        return api(method, path, body)
    except EngineAPIError as exc:
        match = _HTTP_CODE.search(str(exc))
        if match is None:
            raise
        code = int(match.group(1))
        if code == 404:
            raise _Stop(Refused(REASON_NOT_FOUND)) from None
        if code == 400:
            raise _Stop(Refused(REASON_ENGINE_REFUSED)) from None
        if code == 409:
            if _MISSING_CHAT_TEXT in str(exc):
                raise _Stop(MissingChat()) from None
            raise _Stop(Refused(REASON_CONFLICT)) from None
        raise


def _system_entry(api: Callable[..., Any], request: ScheduleRunRequest) -> dict[str, Any]:
    rows = _call(api, "GET", "/api/schedules", None)
    if not isinstance(rows, list):
        raise _Stop(Refused(REASON_LIST_UNREADABLE))
    found = [
        row for row in rows if isinstance(row, dict) and row.get("schedule_id") == request.schedule_id
    ]
    if not found:
        raise _Stop(Refused(REASON_NOT_FOUND))
    if len(found) > 1:
        raise _Stop(Ambiguous(AMBIGUOUS_DUPLICATE_SCHEDULE))
    entry = found[0]
    if entry.get("scope") != "system":
        raise _Stop(Refused(REASON_NOT_SYSTEM))
    if entry.get("workspace") != request.workspace:
        raise _Stop(Refused(REASON_WORKSPACE))
    return entry


def _judge(
    rows: list[dict[str, Any]],
    request: ScheduleRunRequest,
    dispatch_id: str,
    chat_id: str,
) -> tuple[ScheduleRunOutcome | None, bool]:
    """Settle on the one row for this exact dispatch, or say it is not there yet.

    Returns ``(outcome_or_None, other_dispatch_seen)``. Rows for other dispatch
    ids are never matched; they only mark the run as superseded if our own row
    never lands.
    """
    matches: list[tuple[dict[str, Any], str]] = []
    other_dispatch_seen = False
    for row in rows:
        if row.get("job") != "schedule_dispatch":
            continue
        extra = row.get("extra")
        if not isinstance(extra, dict):
            continue
        if str(extra.get("schedule_id") or "") != request.schedule_id:
            continue
        if str(extra.get("workspace") or "") != request.workspace:
            continue
        if str(extra.get("dispatch_id") or "") != dispatch_id:
            other_dispatch_seen = True
            continue
        matches.append((row, str(extra.get("chat_id") or "")))

    if any(row_chat != chat_id for _, row_chat in matches):
        return Ambiguous(AMBIGUOUS_OTHER_CHAT), other_dispatch_seen
    if not matches:
        return None, other_dispatch_seen
    if len(matches) > 1:
        return Ambiguous(AMBIGUOUS_DUPLICATE_ROW), other_dispatch_seen

    row = matches[0][0]
    status = row.get("status")
    if status not in ("ok", "error", "skipped"):
        return Ambiguous(AMBIGUOUS_UNKNOWN_STATUS), other_dispatch_seen
    return (
        Settled(
            status=status,
            dispatch_id=dispatch_id,
            chat_id=chat_id,
            job_run=JobRunRef(
                started_at=str(row.get("started_at") or ""),
                ended_at=str(row.get("ended_at") or ""),
                duration_ms=int(row.get("duration_ms") or 0),
            ),
        ),
        other_dispatch_seen,
    )


def read_runtime_job_runs(runtime_dir: Path) -> list[dict[str, Any]]:
    """Parsed rows of ``<runtime_dir>/job_runs.jsonl``; a missing file is no rows.

    Lines that are not JSON objects are skipped (the log writes whole lines).
    A symlink, an oversize file, or a file that is not UTF-8 raises
    ``JobRunsUnreadable`` instead of returning a partial log.
    """
    path = runtime_dir / JOB_RUNS_NAME
    try:
        if path.is_symlink():
            raise JobRunsUnreadable(JOB_RUNS_NAME)
        if not path.exists():
            return []
        if path.stat().st_size > MAX_JOB_RUNS_BYTES:
            raise JobRunsUnreadable(JOB_RUNS_NAME)
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise JobRunsUnreadable(JOB_RUNS_NAME) from exc

    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows

