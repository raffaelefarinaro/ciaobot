"""Schedule-run driver for the care measurement harness.

Runs one materialized system schedule through the real schedule-run route of an
isolated engine the caller owns, and returns one closed outcome. It never boots,
stops or installs an engine, never calls a model, and never calls
``task_eval.start_prompt`` in place of the schedule.

Only ``bind_isolated`` can produce the ``BoundEngine`` that ``run_schedule``
accepts. The binder refuses anything that is not the harness's own loopback
engine under a harness workdir, before a single call is made.

Call order: GET the schedule list (exact id, ``scope == "system"``, the expected
workspace); PATCH ``enabled: false`` if enabled and GET again to confirm it
stuck; GET for the pre-trigger ``last_dispatch_id``; POST
``/api/schedule-run/{id}`` with no body; GET again, requiring a new
``last_dispatch_id`` and ``last_run_chat_id == chat_id``; then poll the job-run
log for the one settled row carrying exactly that dispatch, schedule, workspace
and chat.

The schedule is left paused when the driver returns. Restoring or discarding it
is the caller's job; the driver never re-enables it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ciao.job_runs import JOB_RUNS_NAME
from ciao.task_eval import EVAL_WORKDIR_PREFIX, EngineAPIError, IsolatedEngine

# Ceiling on one read of the runtime job-run log. Above it the file is refused
# rather than truncated: a truncated read could hide the settled row and turn
# a finished run into a timeout.
MAX_JOB_RUNS_BYTES: int = 16 * 1024 * 1024

REASON_NOT_FOUND = "schedule not found"
REASON_NOT_SYSTEM = "schedule is not a system schedule"
REASON_WORKSPACE = "schedule belongs to another workspace"
REASON_ENABLED_UNKNOWN = "schedule enabled state is not a boolean"
REASON_PAUSE_REJECTED = "pause request was rejected"
REASON_PAUSE_NOT_STUCK = "pause did not stick"
REASON_CONFLICT = "schedule is paused or busy"
REASON_ENGINE_REFUSED = "engine refused the schedule run"
REASON_ENGINE_ERROR = "engine error"
REASON_NO_DISPATCH_ID = "no dispatch id recorded"
REASON_DISPATCH_NOT_NEW = "dispatch id did not change after the trigger"
# A command-only schedule has no chat, so it never reaches the chat-run path
# this driver checks. Out of scope; refused under its own reason, not MissingChat.
REASON_NO_CHAT_TARGET = "schedule has no chat target (command-only runs are out of scope)"

AMBIGUOUS_DUPLICATE_SCHEDULE = "more than one schedule has this id"
AMBIGUOUS_DUPLICATE_ROW = "two settled rows match the dispatch"
AMBIGUOUS_OTHER_CHAT = "a settled row names another chat for this dispatch"
AMBIGUOUS_RUN_CHAT = "schedule last run chat differs from the trigger's chat"
AMBIGUOUS_UNKNOWN_STATUS = "settled row has an unknown status"
AMBIGUOUS_UNREADABLE = "job run log unreadable"


class JobRunsUnreadable(RuntimeError):
    """The runtime job-run log is a symlink, oversize, or not readable UTF-8."""


class IsolatedGuardError(ValueError):
    """The engine handed to ``bind_isolated`` is not a harness-owned loopback engine."""


_BIND_TOKEN = object()


@dataclass(frozen=True)
class BoundEngine:
    """What ``run_schedule`` accepts: an API callable and a job-run reader for one engine.

    Built only by ``bind_isolated``; the token check in ``run_schedule`` refuses
    a value constructed by hand.
    """

    api: Callable[[str, str, Any], Any]
    read_job_runs: Callable[[], list[dict[str, Any]]]
    _token: object | None = field(default=None, repr=False, compare=False)


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


def bind_isolated(engine: IsolatedEngine) -> BoundEngine:
    """Bind the driver to one harness-owned, loopback-only isolated engine.

    Refuses, before any call is made, unless: the base is exactly
    ``http://127.0.0.1:<engine.port>``; the workdir and the workspace under it
    are absolute, the workdir carries the harness prefix, and the workspace is
    inside the workdir with no ``..``. The job-run reader is bound to
    ``<ws>/.runtime``, nothing else.
    """
    workdir = Path(engine.workdir)
    ws = Path(engine.ws)
    if engine.base != f"http://127.0.0.1:{engine.port}":
        raise IsolatedGuardError("engine base is not the harness loopback address")
    if not workdir.is_absolute() or not any(
        part.startswith(EVAL_WORKDIR_PREFIX) for part in workdir.parts
    ):
        raise IsolatedGuardError("engine workdir is not a harness workdir")
    if not ws.is_absolute() or not ws.is_relative_to(workdir) or ".." in ws.parts:
        raise IsolatedGuardError("engine workspace is outside the harness workdir")
    return BoundEngine(
        api=engine.api,
        read_job_runs=_JobRunsReader(ws / ".runtime"),
        _token=_BIND_TOKEN,
    )


class _JobRunsReader:
    """Reads the runtime job-run log, re-parsing only when the file changed.

    The cache key is (inode, mtime_ns, size); the log is append-only, so a
    change always moves at least one of them.
    """

    def __init__(self, runtime_dir: Path) -> None:
        self._runtime_dir = runtime_dir
        self._key: tuple[int, int, int] | None = None
        self._rows: list[dict[str, Any]] = []

    def __call__(self) -> list[dict[str, Any]]:
        path = self._runtime_dir / JOB_RUNS_NAME
        if path.is_symlink():
            raise JobRunsUnreadable(JOB_RUNS_NAME)
        try:
            stat = path.stat()
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise JobRunsUnreadable(JOB_RUNS_NAME) from exc
        key = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        if key != self._key:
            self._rows = read_runtime_job_runs(self._runtime_dir)
            self._key = key
        return list(self._rows)


def run_schedule(
    bound: BoundEngine,
    request: ScheduleRunRequest,
    *,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> ScheduleRunOutcome:
    """Trigger one system schedule through the real route and settle it.

    Never raises for an engine answer, a transport failure or an unreadable log:
    each becomes a closed outcome. Raises ``TypeError`` only when ``bound`` did
    not come from ``bind_isolated``.
    """
    if bound._token is not _BIND_TOKEN:
        raise TypeError("run_schedule accepts only the BoundEngine from bind_isolated")
    try:
        return _drive(bound.api, bound.read_job_runs, request, clock, sleep)
    except _Stop as stop:
        return stop.outcome
    except JobRunsUnreadable:
        return Ambiguous(AMBIGUOUS_UNREADABLE)


def _drive(
    api: Callable[[str, str, Any], Any],
    read_job_runs: Callable[[], list[dict[str, Any]]],
    request: ScheduleRunRequest,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> ScheduleRunOutcome:
    schedule_path = f"/api/schedules/{request.schedule_id}"

    entry = _system_entry(api, request)
    if bool(entry["enabled"]):
        _call(api, "PATCH", schedule_path, {"enabled": False})
        entry = _system_entry(api, request)
        if entry["enabled"]:
            raise _Stop(Refused(REASON_PAUSE_NOT_STUCK))
    # Read again after the pause so the pre-trigger id is the one the engine holds now.
    before = str(entry.get("last_dispatch_id") or "")

    result = _call(api, "POST", f"/api/schedule-run/{request.schedule_id}", None)
    chat_id = str(result.get("chat_id") or "") if isinstance(result, dict) else ""
    if not chat_id:
        raise _Stop(Refused(REASON_NO_CHAT_TARGET))

    entry = _system_entry(api, request)
    dispatch_id = str(entry.get("last_dispatch_id") or "")
    if not dispatch_id:
        raise _Stop(Refused(REASON_NO_DISPATCH_ID))
    if dispatch_id == before:
        raise _Stop(Refused(REASON_DISPATCH_NOT_NEW))
    if str(entry.get("last_run_chat_id") or "") != chat_id:
        raise _Stop(Ambiguous(AMBIGUOUS_RUN_CHAT))

    deadline = clock() + request.deadline_s
    while True:
        outcome = _judge(read_job_runs(), request, dispatch_id, chat_id)
        if outcome is not None:
            return outcome
        if clock() >= deadline:
            # Superseded only when the engine itself moved on to another dispatch.
            current = _system_entry(api, request)
            if str(current.get("last_dispatch_id") or "") != dispatch_id:
                return Superseded()
            return TimedOut(dispatch_id=dispatch_id)
        sleep(request.poll_s)


def _call(api: Callable[[str, str, Any], Any], method: str, path: str, body: Any) -> Any:
    try:
        return api(method, path, body)
    except EngineAPIError as exc:
        raise _Stop(_http_outcome(method, exc)) from None
    except (OSError, ValueError):
        # Transport failure, or a success body that is not JSON.
        raise _Stop(Refused(REASON_ENGINE_ERROR)) from None


def _http_outcome(method: str, exc: EngineAPIError) -> ScheduleRunOutcome:
    if method == "PATCH":
        # Every non-2xx pause is its own refusal, never the trigger's.
        return Refused(REASON_PAUSE_REJECTED)
    if exc.status == 404:
        return Refused(REASON_NOT_FOUND)
    if method == "POST":
        if exc.status == 409 and exc.body is not None and exc.body.get("status") == "missing-chat":
            return MissingChat()
        if exc.status == 409:
            return Refused(REASON_CONFLICT)
        if exc.status == 400:
            return Refused(REASON_ENGINE_REFUSED)
    return Refused(REASON_ENGINE_ERROR)


def _system_entry(
    api: Callable[[str, str, Any], Any], request: ScheduleRunRequest
) -> dict[str, Any]:
    rows = _call(api, "GET", "/api/schedules", None)
    if not isinstance(rows, list):
        raise _Stop(Refused(REASON_ENGINE_ERROR))
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
    if not isinstance(entry.get("enabled"), bool):
        raise _Stop(Refused(REASON_ENABLED_UNKNOWN))
    return entry


def _judge(
    rows: list[dict[str, Any]],
    request: ScheduleRunRequest,
    dispatch_id: str,
    chat_id: str,
) -> ScheduleRunOutcome | None:
    """Settle on the one row for this exact dispatch, or None if it is not there yet.

    Rows for other dispatch ids are never matched.
    """
    matches: list[tuple[dict[str, Any], str]] = []
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
            continue
        matches.append((row, str(extra.get("chat_id") or "")))

    if any(row_chat != chat_id for _, row_chat in matches):
        return Ambiguous(AMBIGUOUS_OTHER_CHAT)
    if not matches:
        return None
    if len(matches) > 1:
        return Ambiguous(AMBIGUOUS_DUPLICATE_ROW)

    row = matches[0][0]
    status = row.get("status")
    if status not in ("ok", "error", "skipped"):
        return Ambiguous(AMBIGUOUS_UNKNOWN_STATUS)
    return Settled(
        status=status,
        dispatch_id=dispatch_id,
        chat_id=chat_id,
        job_run=JobRunRef(
            started_at=str(row.get("started_at") or ""),
            ended_at=str(row.get("ended_at") or ""),
            duration_ms=int(row.get("duration_ms") or 0),
        ),
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
