"""Model-free orchestration of one care scenario run against a fake engine API.

Every engine call goes through a fake that refuses any route outside the
schedule routes, so no test can reach a provider, a model or a real engine.
Everything is written under ``tmp_path``.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from evals import care_eval_seed as seed
from ciao import curation_logs
from ciao import curation_run
from ciao import note_verification as nv
from evals.care_eval_run import bind_isolated
from evals.care_eval_scenario import (
    REASON_ATTEMPT1_COMPLETED,
    REASON_ATTEMPT1_NOT_SETTLED,
    REASON_ATTEMPT2_NO_PROGRESS,
    REASON_ATTEMPT2_NOT_SETTLED,
    REASON_ATTEMPT2_RESTARTED,
    REASON_CURSOR_UNSTABLE,
    Unavailable,
    run_continuation,
    run_scenario,
)
from ciao.curation_run import CURATION_LOG_RELATIVE
from ciao.job_runs import JOB_RUNS_NAME
from ciao.memory_receipts import content_revision
from evals.task_eval import EVAL_WORKDIR_PREFIX, EngineAPIError

DISPATCH_DATE = date(2026, 11, 24)
SID = "system-memory-curation@personal"
WS = "personal"
CHAT = "chat-1"
DISPATCH = "d-1"
STARTED = "2026-11-24T09:00:00+00:00"
RUN_ID = "11111111-2222-3333-4444-555555555555"
TRIGGER_S = 7.5
POLL_S = 2.0
DEADLINE_S = 10.0


def settled_row(
    *, status: str = "ok", dispatch: str = DISPATCH, chat: str = CHAT, started: str = STARTED
) -> dict[str, Any]:
    return {
        "job": "schedule_dispatch",
        "status": status,
        "started_at": started,
        "ended_at": "2026-11-24T09:01:00+00:00",
        "duration_ms": 60000,
        "extra": {"schedule_id": SID, "dispatch_id": dispatch, "chat_id": chat, "workspace": WS},
    }


def _note_revision(vault: Path) -> str:
    return content_revision((vault / seed.NOTE_RELATIVE).read_text(encoding="utf-8"))


def _write_check(vault: Path, revision: str) -> None:
    path = nv.note_check_state_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "relative_path": seed.NOTE_RELATIVE,
        "content_revision": revision,
        "outcome": "still_valid",
        "checked_at": "2026-11-24",
        "retry_after": "2026-12-24",
    }
    path.write_text(
        json.dumps({"schema": 1, "notes": {seed.NOTE_RELATIVE: row}, "entries": {}}),
        encoding="utf-8",
    )


class Harness:
    """Has the attributes ``bind_isolated`` reads, under a harness-prefixed workdir."""

    def __init__(self, root: Path, api: Any) -> None:
        self.workdir = root / f"{EVAL_WORKDIR_PREFIX}case"
        self.ws = self.workdir / "ws"
        self.port = 40124
        self.base = f"http://127.0.0.1:{self.port}"
        self.api = api
        (self.ws / ".runtime").mkdir(parents=True)


class World:
    """A seeded workspace, a fake engine API over its schedule routes, and a fake clock."""

    def __init__(self, root: Path, scenario: str) -> None:
        self.scenario = scenario
        self.now = 0.0
        self.enabled = True
        self.dispatch = "d-0"
        self.run_chat = ""
        self.calls: list[tuple[str, str]] = []
        self.post_error: Exception | None = None
        self.on_trigger: Any = None
        self.on_sleep: Any = None
        self.job_rows: list[dict[str, Any]] = []
        self.dispatch_ids: list[str] = []
        self.trigger_costs: list[float] = []
        self.harness = Harness(root, self.api)
        self.layout = seed.layout_for_workspace(self.harness.ws)
        self.manifest = seed.build_seed(scenario, self.layout, dispatch_date=DISPATCH_DATE)

    def api(self, method: str, path: str, body: Any = None) -> Any:
        self.calls.append((method, path))
        if method == "GET" and path == "/api/schedules":
            return [{
                "schedule_id": SID,
                "scope": "system",
                "workspace": WS,
                "enabled": self.enabled,
                "last_dispatch_id": self.dispatch,
                "last_run_chat_id": self.run_chat,
            }]
        if method == "PATCH" and path == f"/api/schedules/{SID}":
            self.enabled = bool(body["enabled"])
            return {"ok": True}
        if method == "POST" and path == f"/api/schedule-run/{SID}" and body is None:
            if self.post_error is not None:
                raise self.post_error
            self.dispatch = self.dispatch_ids.pop(0) if self.dispatch_ids else DISPATCH
            self.run_chat = CHAT
            self.now += self.trigger_costs.pop(0) if self.trigger_costs else TRIGGER_S
            if self.on_trigger is not None:
                self.on_trigger(self)
            self._write_rows()
            return {"schedule_id": SID, "chat_id": CHAT}
        raise AssertionError(f"unexpected engine call: {method} {path}")

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(self)
        self._write_rows()

    def _write_rows(self) -> None:
        path = self.harness.ws / ".runtime" / JOB_RUNS_NAME
        path.write_text("".join(json.dumps(row) + "\n" for row in self.job_rows), encoding="utf-8")

    def run(self, scenario: str) -> dict[str, Any]:
        bound = bind_isolated(self.harness)
        record = run_scenario(
            scenario,
            bound,
            self.layout,
            self.manifest,
            schedule_id=SID,
            workspace=WS,
            deadline_s=DEADLINE_S,
            poll_s=POLL_S,
            clock=self.clock,
            sleep=self.sleep,
            code_revision="0123456789abcdef0123456789abcdef01234567",
            dirty=False,
            run_id=RUN_ID,
        )
        return dict(record)


def _all_unknown(record: dict[str, Any]) -> bool:
    return all(value == "unknown" for value in record["grade"]["outcomes"].values())


def test_settled_nightly_with_a_verified_note_grades_independently(tmp_path: Path) -> None:
    world = World(tmp_path, "nightly")
    world.job_rows = [settled_row()]
    world.on_trigger = lambda w: _write_check(w.layout.vault_root, _note_revision(w.layout.vault_root))

    record = world.run("nightly")

    assert record["run_id"] == RUN_ID
    assert record["entry"] == "schedule"
    assert record["execution"]["status"] == "completed"
    assert record["grade"]["source"] == "independent"
    assert record["grade"]["outcomes"] == {"o001": "passed", "o002": "passed"}
    assert record["grade"]["safety"] == "passed"
    assert record["provenance"]["fixture_sha256"] == world.manifest.fixture_sha256
    assert record["metrics"]["wall_s"]["status"] == "complete"
    assert record["metrics"]["wall_s"]["value"] == TRIGGER_S
    assert record["metrics"]["wall_s"]["source"] == "monotonic_clock"
    assert world.enabled is False, "the schedule is left paused, never restored"


def test_settled_nightly_without_the_check_fails_the_verification_obligation(tmp_path: Path) -> None:
    world = World(tmp_path, "nightly")
    world.job_rows = [settled_row()]

    record = world.run("nightly")

    assert record["grade"]["source"] == "independent"
    assert record["grade"]["outcomes"] == {"o001": "failed", "o002": "passed"}


def test_unreadable_check_state_leaves_the_obligation_unknown_not_failed(tmp_path: Path) -> None:
    world = World(tmp_path, "nightly")
    world.job_rows = [settled_row()]

    def corrupt(w: World) -> None:
        path = nv.note_check_state_path(w.layout.vault_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")

    world.on_trigger = corrupt

    record = world.run("nightly")

    assert record["grade"]["source"] == "independent"
    assert record["grade"]["outcomes"] == {"o001": "unknown", "o002": "passed"}


def test_settled_weekly_marker_stays_unknown_without_index_and_audit_evidence(tmp_path: Path) -> None:
    world = World(tmp_path, "weekly_due")
    world.job_rows = [settled_row()]

    def stamp_today(w: World) -> None:
        marker = w.layout.vault_root / CURATION_LOG_RELATIVE
        marker.write_bytes(curation_logs.with_full_pass_marker(None, DISPATCH_DATE.isoformat()))

    world.on_trigger = stamp_today

    record = world.run("weekly_due")

    assert record["execution"]["status"] == "completed"
    assert record["grade"]["source"] == "independent"
    assert _all_unknown(record)
    assert record["grade"]["safety"] == "passed"


def test_timed_out_run_grades_nothing_and_reports_no_wall_time(tmp_path: Path) -> None:
    world = World(tmp_path, "nightly")

    record = world.run("nightly")

    assert record["execution"]["status"] == "timed_out"
    assert record["grade"]["source"] == "none"
    assert _all_unknown(record)
    assert record["metrics"]["wall_s"]["status"] == "unknown"


def test_superseded_run_is_skipped_with_no_grade(tmp_path: Path) -> None:
    world = World(tmp_path, "nightly")

    def someone_else(w: World) -> None:
        w.dispatch = "d-2"

    world.on_sleep = someone_else

    record = world.run("nightly")

    assert record["execution"]["status"] == "skipped"
    assert record["grade"]["source"] == "none"


def test_refused_trigger_is_skipped_and_never_observed(tmp_path: Path) -> None:
    world = World(tmp_path, "nightly")
    world.post_error = EngineAPIError(
        "POST -> 409", status=409, body={"status": "conflict"}
    )

    record = world.run("nightly")

    assert record["execution"]["status"] == "skipped"
    assert record["grade"]["source"] == "none"
    assert record["metrics"]["wall_s"]["status"] == "unknown"


def test_missing_chat_is_an_error_outcome(tmp_path: Path) -> None:
    world = World(tmp_path, "nightly")
    world.post_error = EngineAPIError(
        "POST -> 409", status=409, body={"status": "missing-chat"}
    )

    record = world.run("nightly")

    assert record["execution"]["status"] == "error"
    assert record["grade"]["source"] == "none"


def test_ambiguous_duplicate_rows_are_an_error_and_never_graded(tmp_path: Path) -> None:
    world = World(tmp_path, "nightly")
    world.job_rows = [settled_row(), settled_row()]

    record = world.run("nightly")

    assert record["execution"]["status"] == "error"
    assert record["grade"]["source"] == "none"


def test_run_scenario_refuses_budget_continuation_and_points_to_run_continuation(
    tmp_path: Path,
) -> None:
    world = World(tmp_path, "budget_continuation")

    with pytest.raises(ValueError, match="run_continuation"):
        world.run("budget_continuation")
    assert world.calls == []


class ContWorld(World):
    """Two attempts of the continuation scenario over the same fake engine and workspace."""

    def __init__(self, root: Path) -> None:
        super().__init__(root, "budget_continuation")
        self.attempt_1_rows = [settled_row(dispatch="d-1", started="2026-11-24T09:00:00+00:00")]
        self.attempt_2_rows = [settled_row(dispatch="d-2", started="2026-11-24T10:00:00+00:00")]
        self.dispatch_ids = ["d-1", "d-2"]
        self.trigger_costs = [7.5, 3.0]

    def run_continuation(self) -> Any:
        bound = bind_isolated(self.harness)
        return run_continuation(
            bound,
            self.layout,
            self.manifest,
            schedule_id=SID,
            workspace=WS,
            deadline_s=DEADLINE_S,
            poll_s=POLL_S,
            clock=self.clock,
            sleep=self.sleep,
            code_revision="0123456789abcdef0123456789abcdef01234567",
            dirty=False,
            run_id=RUN_ID,
        )


def _write_state(vault: Path, *, done: list[str], deferred: int, holder: str) -> None:
    path = curation_run.state_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = "2026-11-24T09:01:00+00:00"
    last_run = {
        "status": "ok",
        "finished_at": stamp,
        "planned": len(done) + deferred,
        "completed": len(done),
        "deferred": deferred,
        "reasons": [],
        "run_id": holder,
    }
    document = {
        "version": 1,
        "done_keys": {key: "2026-11-24" for key in done},
        "lease": {},
        "last_run": last_run,
    }
    path.write_text(json.dumps(document), encoding="utf-8")


def _cursor_after(attempt: int, world: ContWorld) -> None:
    """Hook for the trigger of attempt ``attempt``: writes the cursor that attempt left."""
    vault = world.layout.vault_root

    def hook(w: World) -> None:
        if attempt == 1:
            _write_state(vault, done=["k-a", "k-b"], deferred=2, holder="holder-a")
        else:
            _write_state(vault, done=["k-a", "k-b", "k-c", "k-d"], deferred=0, holder="holder-b")

    return hook


def _two_settled_attempts(world: ContWorld) -> None:
    world.job_rows = [*world.attempt_1_rows]
    calls = {"n": 0}

    def hook(w: World) -> None:
        calls["n"] += 1
        _cursor_after(calls["n"], world)(w)
        if calls["n"] == 2:
            w.job_rows = [*world.attempt_1_rows, *world.attempt_2_rows]

    world.on_trigger = hook


def test_resumed_second_attempt_is_the_one_graded_record(tmp_path: Path) -> None:
    world = ContWorld(tmp_path)
    _two_settled_attempts(world)

    result = world.run_continuation()

    assert isinstance(result, dict), "exactly one record comes back, from attempt 2"
    assert result["scenario"] == "budget_continuation"
    assert result["execution"]["status"] == "completed"
    assert result["grade"]["source"] == "independent"
    assert result["grade"]["safety"] == "passed"
    assert [method for method, _ in world.calls].count("POST") == 2
    assert world.enabled is False


def test_wall_time_is_reported_for_the_graded_attempt_only(tmp_path: Path) -> None:
    world = ContWorld(tmp_path)
    _two_settled_attempts(world)

    result = world.run_continuation()

    assert result["metrics"]["wall_s"]["value"] == 3.0, "attempt 2's own 3.0 s, not 10.5 s"
    assert result["metrics"]["wall_s"]["status"] == "complete"
    for name, metric in result["metrics"].items():
        if name != "wall_s":
            assert metric["status"] == "unknown", name
            assert metric["value"] is None, name


def test_attempt_one_completing_early_is_unavailable(tmp_path: Path) -> None:
    world = ContWorld(tmp_path)
    world.job_rows = [*world.attempt_1_rows]
    world.on_trigger = lambda w: _write_state(
        w.layout.vault_root, done=["k-a", "k-b"], deferred=0, holder="holder-a"
    )

    result = world.run_continuation()

    assert result == Unavailable(REASON_ATTEMPT1_COMPLETED)


def test_attempt_one_not_settled_is_unavailable(tmp_path: Path) -> None:
    world = ContWorld(tmp_path)
    world.post_error = EngineAPIError("POST -> 409", status=409, body={"status": "conflict"})

    result = world.run_continuation()

    assert result == Unavailable(REASON_ATTEMPT1_NOT_SETTLED)
    assert [method for method, _ in world.calls].count("POST") == 1


def test_cursor_reads_that_disagree_are_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from evals import care_eval_scenario as scenario_module

    world = ContWorld(tmp_path)
    _two_settled_attempts(world)
    readings = iter([frozenset({"k-a"}), frozenset({"k-a", "k-b"})])
    real = scenario_module.observe_cursor

    def flaky(vault_root: Path) -> Any:
        observation = real(vault_root)
        return seed.CursorObservation(done_keys=next(readings), status=observation.status)

    monkeypatch.setattr(scenario_module, "observe_cursor", flaky)

    assert world.run_continuation() == Unavailable(REASON_CURSOR_UNSTABLE)


def test_unreadable_cursor_after_attempt_one_is_unavailable(tmp_path: Path) -> None:
    world = ContWorld(tmp_path)
    world.job_rows = [*world.attempt_1_rows]

    def corrupt(w: World) -> None:
        path = curation_run.state_path(w.layout.vault_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")

    world.on_trigger = corrupt

    assert world.run_continuation() == Unavailable(REASON_CURSOR_UNSTABLE)


@pytest.mark.parametrize("ending", ["timed_out", "superseded"])
def test_attempt_two_not_settled_is_unavailable(tmp_path: Path, ending: str) -> None:
    world = ContWorld(tmp_path)
    world.job_rows = [*world.attempt_1_rows]
    calls = {"n": 0}

    def hook(w: World) -> None:
        calls["n"] += 1
        _cursor_after(calls["n"], world)(w)

    world.on_trigger = hook
    if ending == "superseded":
        def move_on(w: World) -> None:
            if calls["n"] == 2:
                w.dispatch = "d-9"
        world.on_sleep = move_on

    assert world.run_continuation() == Unavailable(REASON_ATTEMPT2_NOT_SETTLED)


def test_attempt_two_restarting_from_scratch_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evals import care_eval_scenario as scenario_module

    world = ContWorld(tmp_path)
    _two_settled_attempts(world)
    monkeypatch.setattr(
        scenario_module,
        "observe_effects",
        lambda vault, run_id, keys: {key: (1 if key == "k-a" else 0) for key in keys},
    )

    assert world.run_continuation() == Unavailable(REASON_ATTEMPT2_RESTARTED)


def test_attempt_two_recording_no_new_work_is_unavailable(tmp_path: Path) -> None:
    world = ContWorld(tmp_path)
    world.job_rows = [*world.attempt_1_rows]
    calls = {"n": 0}

    def hook(w: World) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            _write_state(w.layout.vault_root, done=["k-a", "k-b"], deferred=2, holder="holder-a")
        else:
            _write_state(w.layout.vault_root, done=["k-a", "k-b"], deferred=2, holder="holder-b")
            w.job_rows = [*world.attempt_1_rows, *world.attempt_2_rows]

    world.on_trigger = hook

    assert world.run_continuation() == Unavailable(REASON_ATTEMPT2_NO_PROGRESS)


def test_no_attempt_is_published_with_forbidden_identifiers(tmp_path: Path) -> None:
    world = ContWorld(tmp_path)
    _two_settled_attempts(world)

    result = world.run_continuation()
    assert isinstance(result, dict)
    serialized = json.dumps(result, sort_keys=True)

    for probe in (str(tmp_path), CHAT, "d-1", "d-2", "holder-a", "holder-b", "k-a", "k-c"):
        assert probe not in serialized


def test_seed_missing_from_the_booted_workspace_is_refused_before_any_call(tmp_path: Path) -> None:
    world = World(tmp_path, "nightly")
    (world.layout.agent_root / seed.GUIDE_RELATIVE).unlink()

    with pytest.raises(ValueError, match="manifest"):
        world.run("nightly")
    assert world.calls == []


def test_record_carries_no_workspace_path_chat_or_dispatch_id(tmp_path: Path) -> None:
    world = World(tmp_path, "nightly")
    world.job_rows = [settled_row()]

    serialized = json.dumps(world.run("nightly"), sort_keys=True)

    for probe in (str(tmp_path), CHAT, DISPATCH):
        assert probe not in serialized
