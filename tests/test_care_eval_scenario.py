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

from ciao import care_eval_seed as seed
from ciao import curation_logs
from ciao import note_verification as nv
from ciao.care_eval_run import bind_isolated
from ciao.care_eval_scenario import run_scenario
from ciao.curation_run import CURATION_LOG_RELATIVE
from ciao.job_runs import JOB_RUNS_NAME
from ciao.memory_receipts import content_revision
from ciao.task_eval import EVAL_WORKDIR_PREFIX, EngineAPIError

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


def settled_row(*, status: str = "ok", dispatch: str = DISPATCH, chat: str = CHAT) -> dict[str, Any]:
    return {
        "job": "schedule_dispatch",
        "status": status,
        "started_at": STARTED,
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
            self.dispatch = DISPATCH
            self.run_chat = CHAT
            self.now += TRIGGER_S
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


def test_budget_continuation_is_not_orchestrated_here(tmp_path: Path) -> None:
    world = World(tmp_path, "budget_continuation")

    with pytest.raises(ValueError, match="budget_continuation"):
        world.run("budget_continuation")
    assert world.calls == []


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
