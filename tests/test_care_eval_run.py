from __future__ import annotations

from typing import Any

import pytest

import ciao.care_eval_run as driver
from ciao.care_eval_run import (
    AMBIGUOUS_DUPLICATE_ROW,
    AMBIGUOUS_DUPLICATE_SCHEDULE,
    AMBIGUOUS_OTHER_CHAT,
    AMBIGUOUS_UNKNOWN_STATUS,
    AMBIGUOUS_UNREADABLE,
    REASON_CONFLICT,
    REASON_ENGINE_REFUSED,
    REASON_LIST_UNREADABLE,
    REASON_NOT_FOUND,
    REASON_NOT_SYSTEM,
    REASON_NO_DISPATCH_ID,
    REASON_PAUSE_NOT_STUCK,
    REASON_WORKSPACE,
    Ambiguous,
    JobRunsUnreadable,
    MissingChat,
    Refused,
    ScheduleRunRequest,
    Settled,
    Superseded,
    TimedOut,
    read_runtime_job_runs,
    run_schedule,
)
from ciao.task_eval import EngineAPIError

SID = "system-memory-curation@personal"
WS = "personal"
CHAT = "chat-1"
REQ = ScheduleRunRequest(schedule_id=SID, workspace=WS, deadline_s=10.0, poll_s=2.0)


def schedule_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "schedule_id": SID,
        "scope": "system",
        "workspace": WS,
        "enabled": True,
        "last_dispatch_id": "d-1",
    }
    row.update(overrides)
    return row


def run_row(
    *,
    dispatch: str = "d-1",
    chat: str = CHAT,
    status: str = "ok",
    schedule: str = SID,
    workspace: str = WS,
    **top: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "job": "schedule_dispatch",
        "label": "Memory curation",
        "status": status,
        "started_at": "2026-11-20T00:01:00+00:00",
        "ended_at": "2026-11-20T00:02:00+00:00",
        "duration_ms": 60000,
        "extra": {
            "schedule_id": schedule,
            "dispatch_id": dispatch,
            "chat_id": chat,
            "workspace": workspace,
        },
    }
    row.update(top)
    return row


def http_error(method: str, path: str, code: int, text: str) -> EngineAPIError:
    return EngineAPIError(f"{method} {path} -> {code}: {text!r}")


class FakeEngine:
    """Stands in for IsolatedEngine.api; any call outside the three expected ones fails."""

    def __init__(self) -> None:
        self.schedules: list[dict[str, Any]] = [schedule_row()]
        self.patch_sticks = True
        self.patch_error: Exception | None = None
        self.post_result: Any = {"schedule_id": SID, "chat_id": CHAT}
        self.post_error: Exception | None = None
        self.dispatch_id = "d-1"
        self.calls: list[tuple[str, str, Any]] = []

    def api(self, method: str, path: str, body: Any = None) -> Any:
        self.calls.append((method, path, body))
        if method == "GET" and path == "/api/schedules":
            return [dict(row) for row in self.schedules]
        if method == "PATCH" and path == f"/api/schedules/{SID}":
            if self.patch_error is not None:
                raise self.patch_error
            if self.patch_sticks:
                for row in self.schedules:
                    row["enabled"] = body["enabled"]
            return {"ok": True}
        if method == "POST" and path == f"/api/schedule-run/{SID}" and body is None:
            if self.post_error is not None:
                raise self.post_error
            for row in self.schedules:
                row["last_dispatch_id"] = self.dispatch_id
            return self.post_result
        raise AssertionError(f"unexpected engine call: {method} {path}")


class FakeClock:
    def __init__(self, on_sleep: Any = None) -> None:
        self.now = 0.0
        self.sleeps = 0
        self.on_sleep = on_sleep

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(self.sleeps)


def drive(
    engine: FakeEngine,
    rows: list[dict[str, Any]] | None = None,
    *,
    on_sleep: Any = None,
    request: ScheduleRunRequest = REQ,
) -> tuple[Any, FakeClock]:
    store: list[dict[str, Any]] = [] if rows is None else rows
    clock = FakeClock(on_sleep)
    outcome = run_schedule(
        engine.api,
        lambda: list(store),
        request,
        clock=clock.clock,
        sleep=clock.sleep,
    )
    return outcome, clock


def test_accepted_then_running_then_terminal_settles_on_exact_row() -> None:
    engine = FakeEngine()
    rows: list[dict[str, Any]] = []

    def finish(n: int) -> None:
        if n == 2:
            rows.append(run_row())

    outcome, clock = drive(engine, rows, on_sleep=finish)

    assert isinstance(outcome, Settled)
    assert outcome.status == "ok"
    assert outcome.dispatch_id == "d-1"
    assert outcome.chat_id == CHAT
    assert outcome.job_run.started_at == "2026-11-20T00:01:00+00:00"
    assert outcome.job_run.duration_ms == 60000
    assert clock.sleeps == 2


def test_stale_row_then_newer_row_race_ignores_the_old_dispatch() -> None:
    engine = FakeEngine()
    rows = [run_row(dispatch="d-0", status="error")]

    def finish(n: int) -> None:
        if n == 1:
            rows.append(run_row(dispatch="d-1", status="ok"))

    outcome, _ = drive(engine, rows, on_sleep=finish)

    assert outcome == Settled(
        status="ok",
        dispatch_id="d-1",
        chat_id=CHAT,
        job_run=outcome.job_run,
    )


def test_newer_dispatch_row_never_settles_our_dispatch() -> None:
    engine = FakeEngine()
    rows: list[dict[str, Any]] = []

    def someone_else(n: int) -> None:
        if n == 1:
            rows.append(run_row(dispatch="d-2"))

    outcome, _ = drive(engine, rows, on_sleep=someone_else)

    assert outcome == Superseded()


def test_row_for_other_workspace_does_not_settle() -> None:
    engine = FakeEngine()
    rows = [run_row(workspace="other")]

    outcome, _ = drive(engine, rows)

    assert outcome == TimedOut(dispatch_id="d-1")


def test_missing_chat_from_post_is_missing_chat_and_never_polls() -> None:
    engine = FakeEngine()
    engine.post_result = {"schedule_id": SID, "chat_id": ""}

    outcome, clock = drive(engine)

    assert outcome == MissingChat()
    assert clock.sleeps == 0


def test_post_409_for_missing_chat_maps_to_missing_chat() -> None:
    engine = FakeEngine()
    engine.post_error = http_error(
        "POST", f"/api/schedule-run/{SID}", 409, "target chat no longer exists"
    )

    outcome, _ = drive(engine)

    assert outcome == MissingChat()


def test_deadline_with_no_row_is_timed_out() -> None:
    engine = FakeEngine()

    outcome, clock = drive(engine)

    assert outcome == TimedOut(dispatch_id="d-1")
    assert clock.sleeps == 5


def test_two_matching_rows_are_ambiguous() -> None:
    engine = FakeEngine()

    outcome, _ = drive(engine, [run_row(), run_row()])

    assert outcome == Ambiguous(AMBIGUOUS_DUPLICATE_ROW)


def test_matching_dispatch_with_other_chat_is_ambiguous() -> None:
    engine = FakeEngine()

    outcome, _ = drive(engine, [run_row(chat="chat-other")])

    assert outcome == Ambiguous(AMBIGUOUS_OTHER_CHAT)


def test_unknown_status_on_matching_row_is_ambiguous() -> None:
    engine = FakeEngine()

    outcome, _ = drive(engine, [run_row(status="weird")])

    assert outcome == Ambiguous(AMBIGUOUS_UNKNOWN_STATUS)


def test_row_with_error_status_is_settled_not_timed_out() -> None:
    engine = FakeEngine()

    outcome, _ = drive(engine, [run_row(status="error")])

    assert isinstance(outcome, Settled)
    assert outcome.status == "error"


def test_already_paused_schedule_is_not_patched() -> None:
    engine = FakeEngine()
    engine.schedules = [schedule_row(enabled=False)]

    drive(engine, [run_row()])

    assert [call[0] for call in engine.calls if call[0] == "PATCH"] == []


def test_pause_happens_before_trigger_and_order_is_fixed() -> None:
    engine = FakeEngine()

    drive(engine, [run_row()])

    assert engine.calls == [
        ("GET", "/api/schedules", None),
        ("PATCH", f"/api/schedules/{SID}", {"enabled": False}),
        ("GET", "/api/schedules", None),
        ("POST", f"/api/schedule-run/{SID}", None),
        ("GET", "/api/schedules", None),
    ]


def test_no_call_outside_the_three_schedule_routes() -> None:
    engine = FakeEngine()

    drive(engine, [run_row()])

    assert {(method, path) for method, path, _ in engine.calls} == {
        ("GET", "/api/schedules"),
        ("PATCH", f"/api/schedules/{SID}"),
        ("POST", f"/api/schedule-run/{SID}"),
    }


def test_outcomes_hold_no_transcript_token_or_path() -> None:
    engine = FakeEngine()
    row = run_row(
        error="/Users/someone/secret token=abc123",
        transcript="SECRET-TRANSCRIPT-LINE",
    )
    row["extra"]["archived_to"] = "/Users/someone/vault/note.md"

    outcome, _ = drive(engine, [row])

    text = repr(outcome)
    for leaked in ("SECRET-TRANSCRIPT", "abc123", "/Users/someone"):
        assert leaked not in text
    assert isinstance(outcome, Settled)


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        (lambda e: setattr(e, "schedules", []), REASON_NOT_FOUND),
        (
            lambda e: setattr(e, "schedules", [schedule_row(scope="user")]),
            REASON_NOT_SYSTEM,
        ),
        (
            lambda e: setattr(e, "schedules", [schedule_row(workspace="other")]),
            REASON_WORKSPACE,
        ),
        (
            lambda e: setattr(
                e,
                "schedules",
                [schedule_row(), schedule_row()],
            ),
            None,
        ),
        (lambda e: setattr(e, "patch_sticks", False), REASON_PAUSE_NOT_STUCK),
        (
            lambda e: setattr(
                e,
                "patch_error",
                http_error("PATCH", f"/api/schedules/{SID}", 409, "conflict"),
            ),
            REASON_CONFLICT,
        ),
        (
            lambda e: setattr(
                e,
                "post_error",
                http_error("POST", f"/api/schedule-run/{SID}", 404, "not found"),
            ),
            REASON_NOT_FOUND,
        ),
        (
            lambda e: setattr(
                e,
                "post_error",
                http_error("POST", f"/api/schedule-run/{SID}", 400, "refused"),
            ),
            REASON_ENGINE_REFUSED,
        ),
        (
            lambda e: setattr(
                e,
                "post_error",
                http_error("POST", f"/api/schedule-run/{SID}", 409, "schedule paused"),
            ),
            REASON_CONFLICT,
        ),
        (lambda e: setattr(e, "dispatch_id", ""), REASON_NO_DISPATCH_ID),
    ],
)
def test_every_refusal_carries_a_fixed_reason(setup: Any, expected: str | None) -> None:
    engine = FakeEngine()
    setup(engine)

    outcome, _ = drive(engine, [run_row()])

    if expected is None:
        assert outcome == Ambiguous(AMBIGUOUS_DUPLICATE_SCHEDULE)
        return
    assert outcome == Refused(reason=expected)


def test_unreadable_job_run_log_is_ambiguous_not_timed_out() -> None:
    engine = FakeEngine()

    def unreadable() -> list[dict[str, Any]]:
        raise JobRunsUnreadable("log")

    clock = FakeClock()
    outcome = run_schedule(
        engine.api, unreadable, REQ, clock=clock.clock, sleep=clock.sleep
    )

    assert outcome == Ambiguous(AMBIGUOUS_UNREADABLE)


def test_list_that_is_not_a_list_is_refused() -> None:
    engine = FakeEngine()
    original = engine.api

    def bad_list(method: str, path: str, body: Any = None) -> Any:
        if method == "GET":
            return {"schedules": []}
        return original(method, path, body)

    clock = FakeClock()
    outcome = run_schedule(
        bad_list, lambda: [], REQ, clock=clock.clock, sleep=clock.sleep
    )

    assert outcome == Refused(reason=REASON_LIST_UNREADABLE)


def test_read_runtime_job_runs_missing_file_is_empty(tmp_path: Any) -> None:
    assert read_runtime_job_runs(tmp_path) == []


def test_read_runtime_job_runs_skips_bad_lines(tmp_path: Any) -> None:
    import json

    path = tmp_path / "job_runs.jsonl"
    path.write_text(
        json.dumps(run_row()) + "\n" + "{not json\n" + "[1, 2]\n" + "\n",
        encoding="utf-8",
    )

    rows = read_runtime_job_runs(tmp_path)

    assert rows == [run_row()]


def test_read_runtime_job_runs_over_ceiling_refuses(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setattr(driver, "MAX_JOB_RUNS_BYTES", 10)
    (tmp_path / "job_runs.jsonl").write_text("x" * 11, encoding="utf-8")

    with pytest.raises(JobRunsUnreadable):
        read_runtime_job_runs(tmp_path)


def test_read_runtime_job_runs_refuses_symlink(tmp_path: Any) -> None:
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("", encoding="utf-8")
    (tmp_path / "job_runs.jsonl").symlink_to(target)

    with pytest.raises(JobRunsUnreadable):
        read_runtime_job_runs(tmp_path)
