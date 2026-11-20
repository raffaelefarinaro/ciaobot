from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

import ciao.care_eval_run as driver
from ciao.care_eval_run import (
    AMBIGUOUS_DUPLICATE_ROW,
    AMBIGUOUS_DUPLICATE_SCHEDULE,
    AMBIGUOUS_OTHER_CHAT,
    AMBIGUOUS_RUN_CHAT,
    AMBIGUOUS_UNKNOWN_STATUS,
    AMBIGUOUS_UNREADABLE,
    REASON_CONFLICT,
    REASON_DISPATCH_NOT_NEW,
    REASON_ENABLED_UNKNOWN,
    REASON_ENGINE_ERROR,
    REASON_ENGINE_REFUSED,
    REASON_NO_CHAT_TARGET,
    REASON_NO_DISPATCH_ID,
    REASON_NOT_FOUND,
    REASON_NOT_SYSTEM,
    REASON_PAUSE_NOT_STUCK,
    REASON_PAUSE_REJECTED,
    REASON_WORKSPACE,
    Ambiguous,
    BoundEngine,
    IsolatedGuardError,
    JobRunsUnreadable,
    MissingChat,
    Refused,
    ScheduleRunRequest,
    Settled,
    Superseded,
    TimedOut,
    bind_isolated,
    read_runtime_job_runs,
    run_schedule,
)
from ciao.job_runs import JOB_RUNS_NAME
from ciao.task_eval import EVAL_WORKDIR_PREFIX, EngineAPIError, IsolatedEngine

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
        "last_dispatch_id": "d-0",
        "last_run_chat_id": "",
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


def api_error(
    method: str, path: str, status: int, body: Any = None, text: str = ""
) -> EngineAPIError:
    return EngineAPIError(
        f"{method} {path} -> {status}: {text!r}",
        status=status,
        body=body if isinstance(body, dict) else None,
    )


class FakeEngine:
    """Stands in for the engine's API; any call outside the schedule routes fails the test."""

    def __init__(self) -> None:
        self.schedules: list[dict[str, Any]] = [schedule_row()]
        self.patch_sticks = True
        self.get_error: Exception | None = None
        self.patch_error: Exception | None = None
        self.post_error: Exception | None = None
        self.post_result: Any = {"schedule_id": SID, "chat_id": CHAT}
        self.dispatch_id = "d-1"
        self.run_chat = CHAT
        self.calls: list[tuple[str, str, Any]] = []

    def api(self, method: str, path: str, body: Any = None) -> Any:
        self.calls.append((method, path, body))
        if method == "GET" and path == "/api/schedules":
            if self.get_error is not None:
                raise self.get_error
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
                row["last_run_chat_id"] = self.run_chat
            return self.post_result
        raise AssertionError(f"unexpected engine call: {method} {path}")


class Harness:
    """Has the attributes ``bind_isolated`` reads, rooted under a harness-prefixed workdir."""

    def __init__(self, root: Path, api: Any) -> None:
        self.workdir = root / f"{EVAL_WORKDIR_PREFIX}case"
        self.ws = self.workdir / "ws"
        self.port = 40123
        self.base = f"http://127.0.0.1:{self.port}"
        self.api = api
        (self.ws / ".runtime").mkdir(parents=True)


class Run:
    """A fake engine, its job-run log, and a fake clock that can change both between polls."""

    def __init__(self, root: Path, engine: FakeEngine, rows: list[dict[str, Any]] | None = None) -> None:
        self.engine = engine
        self.harness = Harness(root, engine.api)
        self.rows: list[dict[str, Any]] = list(rows or [])
        self.on_sleep: Any = None
        self.sleeps = 0
        self._write()

    def _write(self) -> None:
        path = self.harness.ws / ".runtime" / JOB_RUNS_NAME
        path.write_text("".join(json.dumps(row) + "\n" for row in self.rows), encoding="utf-8")

    def _sleep(self, seconds: float) -> None:
        self.sleeps += 1
        if self.on_sleep is not None:
            self.on_sleep(self.sleeps, self)
        self._write()

    def go(self, request: ScheduleRunRequest = REQ) -> Any:
        bound = bind_isolated(self.harness)
        now = [0.0]

        def clock() -> float:
            return now[0] + self.sleeps * request.poll_s

        return run_schedule(bound, request, clock=clock, sleep=self._sleep)


def test_accepted_then_running_then_terminal_settles_on_exact_row(tmp_path: Path) -> None:
    run = Run(tmp_path, FakeEngine())

    def finish(n: int, r: Run) -> None:
        if n == 2:
            r.rows.append(run_row())

    run.on_sleep = finish
    outcome = run.go()

    assert isinstance(outcome, Settled)
    assert outcome.status == "ok"
    assert outcome.dispatch_id == "d-1"
    assert outcome.chat_id == CHAT
    assert outcome.job_run.started_at == "2026-11-20T00:01:00+00:00"
    assert outcome.job_run.duration_ms == 60000
    assert run.sleeps == 2


def test_stale_row_then_newer_row_race_ignores_the_old_dispatch(tmp_path: Path) -> None:
    run = Run(tmp_path, FakeEngine(), [run_row(dispatch="d-0", status="error")])

    def finish(n: int, r: Run) -> None:
        if n == 1:
            r.rows.append(run_row(dispatch="d-1", status="ok"))

    run.on_sleep = finish
    outcome = run.go()

    assert isinstance(outcome, Settled)
    assert outcome.status == "ok"
    assert outcome.dispatch_id == "d-1"


def test_stale_older_row_with_no_new_row_is_timed_out(tmp_path: Path) -> None:
    run = Run(tmp_path, FakeEngine(), [run_row(dispatch="d-0", status="error")])

    assert run.go() == TimedOut(dispatch_id="d-1")


def test_newer_dispatch_moves_the_engine_on_so_the_run_is_superseded(tmp_path: Path) -> None:
    engine = FakeEngine()
    run = Run(tmp_path, engine)

    def someone_else(n: int, r: Run) -> None:
        if n == 1:
            engine.schedules[0]["last_dispatch_id"] = "d-2"
            r.rows.append(run_row(dispatch="d-2"))

    run.on_sleep = someone_else

    assert run.go() == Superseded()


def test_row_for_other_workspace_does_not_settle(tmp_path: Path) -> None:
    run = Run(tmp_path, FakeEngine(), [run_row(workspace="other")])

    assert run.go() == TimedOut(dispatch_id="d-1")


def test_deadline_with_no_row_is_timed_out(tmp_path: Path) -> None:
    run = Run(tmp_path, FakeEngine())

    assert run.go() == TimedOut(dispatch_id="d-1")
    assert run.sleeps == 5


def test_two_matching_rows_are_ambiguous(tmp_path: Path) -> None:
    run = Run(tmp_path, FakeEngine(), [run_row(), run_row()])

    assert run.go() == Ambiguous(AMBIGUOUS_DUPLICATE_ROW)


def test_matching_dispatch_with_other_chat_is_ambiguous(tmp_path: Path) -> None:
    run = Run(tmp_path, FakeEngine(), [run_row(chat="chat-other")])

    assert run.go() == Ambiguous(AMBIGUOUS_OTHER_CHAT)


def test_unknown_status_on_matching_row_is_ambiguous(tmp_path: Path) -> None:
    run = Run(tmp_path, FakeEngine(), [run_row(status="weird")])

    assert run.go() == Ambiguous(AMBIGUOUS_UNKNOWN_STATUS)


def test_row_with_error_status_is_settled_not_timed_out(tmp_path: Path) -> None:
    run = Run(tmp_path, FakeEngine(), [run_row(status="error")])

    outcome = run.go()

    assert isinstance(outcome, Settled)
    assert outcome.status == "error"


def test_already_paused_schedule_is_not_patched(tmp_path: Path) -> None:
    engine = FakeEngine()
    engine.schedules = [schedule_row(enabled=False)]

    Run(tmp_path, engine, [run_row()]).go()

    assert [call for call in engine.calls if call[0] == "PATCH"] == []


def test_pause_happens_before_trigger_and_order_is_fixed(tmp_path: Path) -> None:
    engine = FakeEngine()

    Run(tmp_path, engine, [run_row()]).go()

    assert engine.calls == [
        ("GET", "/api/schedules", None),
        ("PATCH", f"/api/schedules/{SID}", {"enabled": False}),
        ("GET", "/api/schedules", None),
        ("POST", f"/api/schedule-run/{SID}", None),
        ("GET", "/api/schedules", None),
    ]


def test_no_call_outside_the_three_schedule_routes(tmp_path: Path) -> None:
    engine = FakeEngine()

    Run(tmp_path, engine, [run_row()]).go()

    assert {(method, path) for method, path, _ in engine.calls} == {
        ("GET", "/api/schedules"),
        ("PATCH", f"/api/schedules/{SID}"),
        ("POST", f"/api/schedule-run/{SID}"),
    }


def test_outcomes_hold_no_transcript_token_or_path(tmp_path: Path) -> None:
    row = run_row(error="/Users/someone/secret token=abc123", transcript="SECRET-TRANSCRIPT-LINE")
    row["extra"]["archived_to"] = "/Users/someone/vault/note.md"

    outcome = Run(tmp_path, FakeEngine(), [row]).go()

    text = repr(outcome)
    for leaked in ("SECRET-TRANSCRIPT", "abc123", "/Users/someone"):
        assert leaked not in text
    assert isinstance(outcome, Settled)


def test_command_only_run_without_chat_is_refused_not_missing_chat(tmp_path: Path) -> None:
    # Command-only schedules return no chat from the trigger. They are out of
    # scope for this driver and get their own refusal, never MissingChat.
    engine = FakeEngine()
    engine.post_result = {"schedule_id": SID, "chat_id": ""}

    outcome = Run(tmp_path, engine).go()

    assert outcome == Refused(reason=REASON_NO_CHAT_TARGET)
    assert not isinstance(outcome, MissingChat)


def test_last_run_chat_mismatch_is_ambiguous(tmp_path: Path) -> None:
    engine = FakeEngine()
    engine.run_chat = "chat-other"

    assert Run(tmp_path, engine, [run_row()]).go() == Ambiguous(AMBIGUOUS_RUN_CHAT)


def _set(attr: str, value: Any) -> Any:
    def apply(engine: FakeEngine) -> None:
        setattr(engine, attr, value)

    return apply


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        (_set("schedules", []), Refused(REASON_NOT_FOUND)),
        (_set("schedules", [schedule_row(scope="user")]), Refused(REASON_NOT_SYSTEM)),
        (_set("schedules", [schedule_row(workspace="other")]), Refused(REASON_WORKSPACE)),
        (_set("schedules", [schedule_row(enabled="yes")]), Refused(REASON_ENABLED_UNKNOWN)),
        (
            _set("schedules", [schedule_row(), schedule_row()]),
            Ambiguous(AMBIGUOUS_DUPLICATE_SCHEDULE),
        ),
        (_set("patch_sticks", False), Refused(REASON_PAUSE_NOT_STUCK)),
        # Every non-2xx PATCH is a pause refusal, distinct from the trigger's.
        (
            _set("patch_error", api_error("PATCH", f"/api/schedules/{SID}", 400, text="bad")),
            Refused(REASON_PAUSE_REJECTED),
        ),
        (
            _set("patch_error", api_error("PATCH", f"/api/schedules/{SID}", 500)),
            Refused(REASON_PAUSE_REJECTED),
        ),
        (
            _set("patch_error", api_error("PATCH", f"/api/schedules/{SID}", 503, {"x": 1})),
            Refused(REASON_PAUSE_REJECTED),
        ),
        (
            _set("patch_error", api_error("PATCH", f"/api/schedules/{SID}", 400, text="<html>")),
            Refused(REASON_PAUSE_REJECTED),
        ),
        (_set("patch_error", OSError("connection reset")), Refused(REASON_ENGINE_ERROR)),
        (_set("get_error", api_error("GET", "/api/schedules", 500)), Refused(REASON_ENGINE_ERROR)),
        (_set("get_error", OSError("refused")), Refused(REASON_ENGINE_ERROR)),
        (
            _set("post_error", api_error("POST", f"/api/schedule-run/{SID}", 500)),
            Refused(REASON_ENGINE_ERROR),
        ),
        (
            _set("post_error", api_error("POST", f"/api/schedule-run/{SID}", 500, text="boom")),
            Refused(REASON_ENGINE_ERROR),
        ),
        (_set("post_error", OSError("timed out")), Refused(REASON_ENGINE_ERROR)),
        (
            _set("post_error", api_error("POST", f"/api/schedule-run/{SID}", 404)),
            Refused(REASON_NOT_FOUND),
        ),
        (
            _set("post_error", api_error("POST", f"/api/schedule-run/{SID}", 400, {"error": "x"})),
            Refused(REASON_ENGINE_REFUSED),
        ),
        (
            _set(
                "post_error",
                api_error("POST", f"/api/schedule-run/{SID}", 409, {"status": "busy"}),
            ),
            Refused(REASON_CONFLICT),
        ),
        (
            _set(
                "post_error",
                api_error("POST", f"/api/schedule-run/{SID}", 409, {"error": "paused"}),
            ),
            Refused(REASON_CONFLICT),
        ),
        # Same words in the text, no structured field: still a conflict, not a missing chat.
        (
            _set(
                "post_error",
                api_error(
                    "POST",
                    f"/api/schedule-run/{SID}",
                    409,
                    {"error": "target chat no longer exists"},
                    "target chat no longer exists",
                ),
            ),
            Refused(REASON_CONFLICT),
        ),
        (
            _set(
                "post_error",
                api_error("POST", f"/api/schedule-run/{SID}", 409, {"status": "missing-chat"}),
            ),
            MissingChat(),
        ),
        (_set("dispatch_id", "d-0"), Refused(REASON_DISPATCH_NOT_NEW)),
        (_set("dispatch_id", ""), Refused(REASON_NO_DISPATCH_ID)),
    ],
)
def test_every_refusal_carries_a_fixed_outcome(
    tmp_path: Path, setup: Any, expected: Any
) -> None:
    engine = FakeEngine()
    setup(engine)

    assert Run(tmp_path, engine, [run_row()]).go() == expected


def test_unreadable_job_run_log_is_ambiguous_not_timed_out(tmp_path: Path) -> None:
    run = Run(tmp_path, FakeEngine())
    log = run.harness.ws / ".runtime" / JOB_RUNS_NAME
    target = run.harness.ws / "elsewhere.jsonl"
    target.write_text("", encoding="utf-8")
    log.unlink()
    log.symlink_to(target)

    assert run.go() == Ambiguous(AMBIGUOUS_UNREADABLE)


def test_bound_engine_built_by_hand_is_refused_before_any_call(tmp_path: Path) -> None:
    engine = FakeEngine()
    forged = BoundEngine(api=engine.api, read_job_runs=lambda: [])

    with pytest.raises(TypeError):
        run_schedule(forged, REQ, clock=lambda: 0.0, sleep=lambda s: None)
    assert engine.calls == []


def _bind_refused(tmp_path: Path, mutate: Any) -> FakeEngine:
    engine = FakeEngine()
    harness = Harness(tmp_path, engine.api)
    mutate(harness)
    with pytest.raises(IsolatedGuardError):
        bind_isolated(harness)  # type: ignore[arg-type]
    assert engine.calls == []
    return engine


def test_binder_refuses_non_loopback_base(tmp_path: Path) -> None:
    _bind_refused(tmp_path, lambda h: setattr(h, "base", f"http://10.0.0.5:{h.port}"))


def test_binder_refuses_localhost_spelling(tmp_path: Path) -> None:
    _bind_refused(tmp_path, lambda h: setattr(h, "base", f"http://localhost:{h.port}"))


def test_binder_refuses_base_on_another_port(tmp_path: Path) -> None:
    _bind_refused(tmp_path, lambda h: setattr(h, "base", "http://127.0.0.1:8443"))


def test_binder_refuses_root_runtime_dir(tmp_path: Path) -> None:
    _bind_refused(tmp_path, lambda h: setattr(h, "ws", Path("/")))


def test_binder_refuses_home_ciao_runtime_dir(tmp_path: Path) -> None:
    _bind_refused(tmp_path, lambda h: setattr(h, "ws", Path.home() / ".ciao"))


def test_binder_refuses_workdir_without_harness_prefix(tmp_path: Path) -> None:
    _bind_refused(tmp_path, lambda h: setattr(h, "workdir", tmp_path / "plain"))


def test_binder_refuses_workspace_escaping_by_parent_segments(tmp_path: Path) -> None:
    _bind_refused(
        tmp_path,
        lambda h: setattr(h, "ws", h.workdir / "ws" / ".." / ".." / "victim"),
    )


def test_binder_accepts_harness_engine_and_reads_its_own_runtime(tmp_path: Path) -> None:
    run = Run(tmp_path, FakeEngine(), [run_row()])
    bound = bind_isolated(run.harness)  # type: ignore[arg-type]

    assert bound.read_job_runs() == [run_row()]


def test_job_run_reader_sees_appended_rows(tmp_path: Path) -> None:
    run = Run(tmp_path, FakeEngine(), [run_row()])
    bound = bind_isolated(run.harness)  # type: ignore[arg-type]
    assert len(bound.read_job_runs()) == 1

    run.rows.append(run_row(dispatch="d-9"))
    run._write()

    assert len(bound.read_job_runs()) == 2


def test_integration_real_schedule_routes_with_fake_dispatch(tmp_path: Path) -> None:
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from ciao.schedules import ScheduleManager, ScheduleStore
    from ciao.web.routes_api import list_schedules, run_schedule_now, schedule_detail

    workdir = tmp_path / f"{EVAL_WORKDIR_PREFIX}integration"
    ws = workdir / "ws"
    runtime = ws / ".runtime"
    runtime.mkdir(parents=True)
    # The packaged curation routine fans out to one system row per workspace.
    store = ScheduleStore(runtime, include_system=True, workspace_names=lambda: [WS])
    manager = ScheduleManager(store=store)
    stored = store.get(SID)
    assert stored is not None and stored.scope == "system"

    class _Routing:
        def schedule_effective_routing(self, entry: object) -> tuple[str, str, str]:
            return ("claude", "sonnet", WS)

        def schedule_target_refusal(self, entry: object) -> str:
            return ""

        def get_project(self, project_id: str) -> None:
            return None

        def get_chat(self, chat_id: str) -> None:
            return None

    dispatches = {"n": 0}

    async def fake_dispatch(schedule_id: str, *, now: object = None) -> dict[str, str]:
        # The dispatch row is faked: it stamps the entry the way the real
        # dispatcher does and appends the settled row to the engine's own log.
        dispatches["n"] += 1
        dispatch_id = f"d-{dispatches['n']}"
        store.update_fields(
            schedule_id,
            {"last_dispatch_id": dispatch_id, "last_run_chat_id": CHAT},
        )
        with (runtime / JOB_RUNS_NAME).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(run_row(dispatch=dispatch_id, schedule=schedule_id)) + "\n")
        return {"schedule_id": schedule_id, "chat_id": CHAT}

    manager.dispatch_now = fake_dispatch  # type: ignore[method-assign]
    app = Starlette(
        routes=[
            Route("/api/schedules", list_schedules, methods=["GET"]),
            Route("/api/schedules/{schedule_id}", schedule_detail, methods=["PATCH"]),
            Route("/api/schedule-run/{schedule_id}", run_schedule_now, methods=["POST"]),
        ]
    )
    app.state.schedule_manager = manager
    app.state.project_chat_manager = _Routing()
    client = TestClient(app)

    def api(method: str, path: str, body: Any = None) -> Any:
        response = client.request(method, path, json=body)
        if response.status_code >= 400:
            try:
                error_body = response.json()
            except ValueError:
                error_body = None
            raise EngineAPIError(
                f"{method} {path} -> {response.status_code}: {response.content[:300]!r}",
                status=response.status_code,
                body=error_body if isinstance(error_body, dict) else None,
            )
        return response.json() if response.content else None

    class _Harness:
        pass

    harness = _Harness()
    harness.workdir = workdir  # type: ignore[attr-defined]
    harness.ws = ws  # type: ignore[attr-defined]
    harness.port = 40124  # type: ignore[attr-defined]
    harness.base = "http://127.0.0.1:40124"  # type: ignore[attr-defined]
    harness.api = api  # type: ignore[attr-defined]

    request = ScheduleRunRequest(
        schedule_id=SID, workspace=WS, deadline_s=10.0, poll_s=1.0
    )
    outcome = run_schedule(
        bind_isolated(harness),  # type: ignore[arg-type]
        request,
        clock=lambda: 0.0,
        sleep=lambda s: None,
    )

    assert outcome == Settled(
        status="ok",
        dispatch_id="d-1",
        chat_id=CHAT,
        job_run=outcome.job_run,  # type: ignore[union-attr]
    )
    # The driver leaves the schedule paused; the caller owns restoring it.
    assert store.get(SID).enabled is False  # type: ignore[union-attr]


def test_isolated_engine_api_sets_status_and_body(tmp_path: Path, monkeypatch: Any) -> None:
    engine = IsolatedEngine(tmp_path / f"{EVAL_WORKDIR_PREFIX}api", keep_workdir=True)

    class _Opener:
        def __init__(self, raw: bytes) -> None:
            self.raw = raw

        def open(self, request: Any, timeout: float) -> None:
            raise urllib.error.HTTPError(
                request.full_url, 409, "Conflict", {}, io.BytesIO(self.raw)  # type: ignore[arg-type]
            )

    engine._opener = _Opener(b'{"status": "missing-chat"}')  # type: ignore[assignment]
    with pytest.raises(EngineAPIError) as found:
        engine.api("POST", "/api/schedule-run/x", None)
    assert found.value.status == 409
    assert found.value.body == {"status": "missing-chat"}
    assert str(found.value).startswith("POST /api/schedule-run/x -> 409: ")

    engine._opener = _Opener(b"<html>")  # type: ignore[assignment]
    with pytest.raises(EngineAPIError) as found_html:
        engine.api("PATCH", "/api/schedules/x", {"enabled": False})
    assert found_html.value.status == 409
    assert found_html.value.body is None


def test_read_runtime_job_runs_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_runtime_job_runs(tmp_path) == []


def test_read_runtime_job_runs_skips_bad_lines(tmp_path: Path) -> None:
    path = tmp_path / JOB_RUNS_NAME
    path.write_text(
        json.dumps(run_row()) + "\n" + "{not json\n" + "[1, 2]\n" + "\n",
        encoding="utf-8",
    )

    assert read_runtime_job_runs(tmp_path) == [run_row()]


def test_read_runtime_job_runs_over_ceiling_refuses(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(driver, "MAX_JOB_RUNS_BYTES", 10)
    (tmp_path / JOB_RUNS_NAME).write_text("x" * 11, encoding="utf-8")

    with pytest.raises(JobRunsUnreadable):
        read_runtime_job_runs(tmp_path)


def test_read_runtime_job_runs_refuses_symlink(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("", encoding="utf-8")
    (tmp_path / JOB_RUNS_NAME).symlink_to(target)

    with pytest.raises(JobRunsUnreadable):
        read_runtime_job_runs(tmp_path)
