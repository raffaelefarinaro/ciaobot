"""Tests for a schedule's optional shell command (issue #1258).

A command runs before the model turn. Exit 0 with empty stdout records the run
as done and opens no chat; output or a failure opens the run chat with the
command's outcome in front of the prompt.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import schedule_command as schedule_command_module
from ciao.control_plane import CiaoControlPlane, ControlPlaneError
from ciao.schedule_command import (
    SCHEDULE_COMMAND_OUTPUT_CAP,
    normalize_schedule_command,
    run_schedule_command,
)
from ciao.schedules import ScheduleManager, ScheduleStore
from ciao.web.routes_api import create_schedule, schedule_detail
from ciao.web.schedule_dispatch import ScheduleDispatchHost, ScheduleDispatcher
from tests.test_interval_schedule_api import _ProjectChats, _StateStore
from tests.test_schedule_dispatch_boundary import _entry, _ScheduleHost


def _py(code: str) -> str:
    """A shell command that runs ``code`` under this interpreter."""
    return shlex.join([sys.executable, "-c", code])


# ── Store round-trip and validation ──────────────────────────────────────


def test_command_round_trips_through_the_store(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    entry = store.create(
        daily_time_utc="08:00",
        prompt="Summarise the sync",
        model="",
        mode="auto",
        chat_id=0,
        web_project_id="proj-1",
        command="  ./scripts/sync.sh  ",
    )
    assert entry.command == "./scripts/sync.sh"

    reloaded = ScheduleStore(tmp_path).get(entry.schedule_id)
    assert reloaded is not None
    assert reloaded.command == "./scripts/sync.sh"

    reloaded.command = "   "
    store.replace(reloaded)
    cleared = ScheduleStore(tmp_path).get(entry.schedule_id)
    assert cleared is not None
    assert cleared.command == ""


def test_rows_written_before_the_field_load_as_no_command(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    entry = store.create(
        daily_time_utc="08:00",
        prompt="Plain prompt",
        model="",
        mode="auto",
        chat_id=0,
        web_project_id="proj-1",
    )
    assert entry.command == ""
    assert ScheduleStore(tmp_path).get(entry.schedule_id).command == ""  # type: ignore[union-attr]


def test_non_string_command_is_refused() -> None:
    assert normalize_schedule_command(None) == ""
    assert normalize_schedule_command("  ls  ") == "ls"
    with pytest.raises(ValueError):
        normalize_schedule_command(5)


# ── Running the command ──────────────────────────────────────────────────


async def test_silent_success_is_quiet(tmp_path: Path) -> None:
    run = await run_schedule_command(_py("pass"), tmp_path)
    assert run.exit_code == 0
    assert run.quiet is True


async def test_printed_output_is_not_quiet(tmp_path: Path) -> None:
    run = await run_schedule_command(_py("print('3 files changed')"), tmp_path)
    assert run.exit_code == 0
    assert run.quiet is False
    assert "3 files changed" in run.compose_prompt("Summarise it.")


async def test_failure_is_not_quiet_and_keeps_stderr(tmp_path: Path) -> None:
    run = await run_schedule_command(
        _py("import sys; sys.stderr.write('boom'); sys.exit(3)"), tmp_path
    )
    assert run.exit_code == 3
    assert run.quiet is False
    prompt = run.compose_prompt("Investigate.")
    assert "exited with code 3" in prompt
    assert "[stderr]" in prompt
    assert "boom" in prompt
    assert prompt.endswith("Investigate.")


async def test_command_runs_in_the_workspace_with_workspace_env(tmp_path: Path) -> None:
    run = await run_schedule_command(
        _py("import os, sys; sys.stdout.write(os.getcwd() + '|' + os.environ['CIAO_WORKSPACE'])"),
        tmp_path,
    )
    cwd, _, workspace = run.stdout.partition("|")
    assert Path(cwd).resolve() == tmp_path.resolve()
    assert Path(workspace).resolve() == tmp_path.resolve()


async def test_timeout_kills_the_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schedule_command_module, "SCHEDULE_COMMAND_TIMEOUT_S", 0.5)
    run = await run_schedule_command(_py("import time; time.sleep(30)"), tmp_path)
    assert run.timed_out is True
    assert run.exit_code is None
    assert run.quiet is False
    assert "timed out" in run.compose_prompt("Go.")


async def test_long_output_keeps_head_and_tail_within_the_cap(tmp_path: Path) -> None:
    run = await run_schedule_command(_py("print('a' * 20000 + 'END')"), tmp_path)
    prompt = run.compose_prompt("Go.")
    assert "characters omitted" in prompt
    assert "END" in prompt
    assert len(prompt) < SCHEDULE_COMMAND_OUTPUT_CAP + 500


# ── Dispatch outcomes ────────────────────────────────────────────────────


def _capture_run_records(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    rows: list[object] = []
    monkeypatch.setattr("ciao.web.schedule_dispatch.job_runs.record_run", rows.append)
    return rows


def _command_entry(command: str):
    entry = _entry()
    entry.command = command
    return entry


async def test_quiet_command_records_done_and_opens_no_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _ScheduleHost(tmp_path, [{"type": "result", "text": "unused", "is_error": False}])
    rows = _capture_run_records(monkeypatch)
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))

    result = await dispatcher.dispatch_schedule(
        _command_entry(_py("pass")), "Run the routine.", "opus", "auto", "claude"
    )

    assert result == {"status": "ok"}
    assert host.prepared == []
    assert host.started == []
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "ok"
    assert row.extra["chat_id"] == ""
    assert row.extra["output"] == "no output"
    assert row.extra["command_exit_code"] == 0


async def test_command_output_opens_chat_with_output_before_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _ScheduleHost(tmp_path, [{"type": "result", "text": "done", "is_error": False}])
    rows = _capture_run_records(monkeypatch)
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))

    result = await dispatcher.dispatch_schedule(
        _command_entry(_py("print('12 new notes')")), "Run the routine.", "opus", "auto", "claude"
    )

    assert result == {"chat_id": "chat-1", "status": "ok"}
    (_, prompt, _, _) = host.prepared[0]
    assert "12 new notes" in prompt
    assert prompt.endswith("Run the routine.")
    assert host.started[0][1] == prompt
    assert rows[0].extra["command_exit_code"] == 0  # type: ignore[attr-defined]


async def test_nonzero_exit_opens_chat_with_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _ScheduleHost(tmp_path, [{"type": "result", "text": "done", "is_error": False}])
    _capture_run_records(monkeypatch)
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))

    result = await dispatcher.dispatch_schedule(
        _command_entry(_py("import sys; sys.exit(2)")), "Run the routine.", "opus", "auto", "claude"
    )

    assert result["chat_id"] == "chat-1"
    assert "exited with code 2" in host.started[0][1]


async def test_timeout_opens_chat_with_timeout_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(schedule_command_module, "SCHEDULE_COMMAND_TIMEOUT_S", 0.5)
    host = _ScheduleHost(tmp_path, [{"type": "result", "text": "done", "is_error": False}])
    rows = _capture_run_records(monkeypatch)
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))

    result = await dispatcher.dispatch_schedule(
        _command_entry(_py("import time; time.sleep(30)")), "Run the routine.", "opus", "auto", "claude"
    )

    assert result["chat_id"] == "chat-1"
    assert "timed out" in host.started[0][1]
    assert rows[0].extra["command_timed_out"] is True  # type: ignore[attr-defined]


async def test_entry_without_command_is_dispatched_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _ScheduleHost(tmp_path, [{"type": "result", "text": "done", "is_error": False}])
    _capture_run_records(monkeypatch)
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))

    await dispatcher.dispatch_schedule(_entry(), "Run the routine.", "opus", "auto", "claude")

    assert host.prepared[0][1] == "Run the routine."
    assert host.started[0][1] == "Run the routine."


def test_manager_prepares_no_chat_ahead_of_a_command(tmp_path: Path) -> None:
    prepared: list[str] = []

    def prepare(entry, prompt, model, mode, provider):
        prepared.append(entry.schedule_id)
        return "chat-1"

    manager = ScheduleManager(
        store=ScheduleStore(tmp_path),
        prepare_chat=prepare,
    )
    plain = _entry()
    commanded = _command_entry("git pull")
    assert manager._prepare_run_chat(plain, "opus", "auto", "claude") == "chat-1"
    assert manager._prepare_run_chat(commanded, "opus", "auto", "claude") is None
    assert prepared == [plain.schedule_id]


# ── Control plane: an unattended turn cannot set a command ──────────────


def test_unattended_turn_cannot_set_a_command() -> None:
    fake = SimpleNamespace(_unattended_turn=lambda principal: True)
    with pytest.raises(ControlPlaneError) as excinfo:
        CiaoControlPlane._schedule_command(fake, None, "rm -rf x")  # type: ignore[arg-type]
    assert excinfo.value.code == "unattended_forbidden"
    # Clearing is always allowed, even from a scheduled turn.
    assert CiaoControlPlane._schedule_command(fake, None, "") == ""  # type: ignore[arg-type]


def test_attended_turn_sets_a_trimmed_command() -> None:
    fake = SimpleNamespace(_unattended_turn=lambda principal: False)
    assert CiaoControlPlane._schedule_command(fake, None, " ls ") == "ls"  # type: ignore[arg-type]


# ── API routes accept the field ──────────────────────────────────────────


@pytest.fixture
def api(tmp_path: Path):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    manager = ScheduleManager(store=ScheduleStore(runtime))
    app = Starlette(routes=[
        Route("/api/schedules", create_schedule, methods=["POST"]),
        Route("/api/schedules/{schedule_id}", schedule_detail, methods=["PATCH", "DELETE"]),
    ])
    app.state.schedule_manager = manager
    app.state.project_chat_manager = _ProjectChats()
    app.state.state_store = _StateStore()
    return TestClient(app)


def _interval_body(**overrides) -> dict:
    body = {
        "prompt": "sync notes",
        "frequency": "interval",
        "interval_minutes": 30,
        "web_chat_id": "chat-idle",
    }
    body.update(overrides)
    return body


def test_create_route_accepts_and_trims_the_command(api: TestClient) -> None:
    resp = api.post("/api/schedules", json=_interval_body(command="  ./scripts/sync.sh  "))
    assert resp.status_code == 201, resp.text
    assert resp.json()["command"] == "./scripts/sync.sh"


def test_patch_route_sets_and_clears_the_command(api: TestClient) -> None:
    created = api.post("/api/schedules", json=_interval_body()).json()
    assert created["command"] == ""

    set_resp = api.patch(f"/api/schedules/{created['schedule_id']}", json={"command": "make sync"})
    assert set_resp.status_code == 200, set_resp.text
    assert set_resp.json()["command"] == "make sync"

    clear_resp = api.patch(f"/api/schedules/{created['schedule_id']}", json={"command": ""})
    assert clear_resp.json()["command"] == ""


def test_route_refuses_a_non_string_command(api: TestClient) -> None:
    resp = api.post("/api/schedules", json=_interval_body(command=["ls"]))
    assert resp.status_code == 400
    assert "command" in resp.json()["error"]


