"""A schedule never runs in the app's own memory machinery.

The Memory project holds the engine's memory passes and nothing else, and a
pass is the app's own turn. A routine pointed at either would land an ordinary
chat in a project where every chat is silenced, or post its prompt into a pass
that never reaches the owner — so the app refuses the target, at the route
that configures it and at the dispatch that runs it.

The refusal is raised, not swallowed: the caller has to record it. A run that
never happened is not a run, and a one-shot entry that never ran must not be
consumed. It also must not abort the scheduler's tick, which prepares every
due entry in one loop.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.schedules import ScheduleEntry, ScheduleManager, ScheduleStore
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.memory_pass import MemoryProjectRefused
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import create_schedule, run_schedule_now, schedule_detail


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=f"memory-vault/{name}")
            for name in ("personal",)
        },
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )


def _pass_helper() -> dict:
    return {
        "kind": "memory_pass",
        "source_chat_id": "chat-1",
        "archive_policy": "when_clean",
    }


def _entry(**overrides) -> ScheduleEntry:
    fields = {
        "schedule_id": "sched-memory",
        "daily_time_utc": "08:00",
        "prompt": "Morning briefing.",
        "chat_id": 0,
        "created_at": "2026-09-01T00:00:00Z",
        "frequency": "daily",
        "workspace": "personal",
        "title": "Morning briefing",
    }
    fields.update(overrides)
    return ScheduleEntry(**fields)  # type: ignore[arg-type]


def _memory_project(manager: ProjectChatManager):
    return manager._memory_pass.ensure_project("personal")


def test_a_routine_never_runs_in_the_memory_project(tmp_path: Path) -> None:
    """The Memory project is app-owned: the run is refused, not dispatched."""
    manager = _make_manager(tmp_path)
    memory_project = _memory_project(manager)
    entry = _entry(web_project_id=memory_project.project_id)

    with pytest.raises(MemoryProjectRefused):
        manager.prepare_schedule_chat(entry, entry.prompt, "opus", "auto", "claude")

    # No ordinary chat was opened in that project either.
    assert manager.list_chats() == []


def test_a_routine_never_runs_inside_a_pass(tmp_path: Path) -> None:
    """A pass is the app's own turn: a routine posted into it reaches nobody."""
    manager = _make_manager(tmp_path)
    memory_project = _memory_project(manager)
    pass_chat = manager.create_chat(
        memory_project.project_id,
        title="Memory pass · Pricing rework",
        helper=_pass_helper(),
    )
    entry = _entry(web_chat_id=pass_chat.chat_id)

    with pytest.raises(MemoryProjectRefused):
        manager.prepare_schedule_chat(entry, entry.prompt, "opus", "auto", "claude")

    assert entry.web_chat_id == pass_chat.chat_id


def test_a_routine_still_runs_in_an_ordinary_project(tmp_path: Path) -> None:
    """Control: the refusal is about the Memory project, not about running."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Holder", workspace="personal")
    entry = _entry(web_project_id=project.project_id)

    chat_id = manager.prepare_schedule_chat(
        entry, entry.prompt, "opus", "auto", "claude"
    )

    assert chat_id is not None
    assert manager.get_chat(chat_id).project_id == project.project_id


def test_a_deleted_target_that_re_homes_into_the_memory_project_is_refused(
    tmp_path: Path,
) -> None:
    """A deleted chat re-homes into the project it lived in.

    A pass's project is the app's own, so that re-home has to be refused — and
    refused loudly: a command routine would otherwise run its command, and a
    one-shot entry would be consumed for a run that never happened.
    """
    manager = _make_manager(tmp_path)
    memory_project = _memory_project(manager)
    pass_chat = manager.create_chat(
        memory_project.project_id,
        title="Memory pass · Pricing rework",
        helper=_pass_helper(),
    )
    entry = _entry(web_chat_id=pass_chat.chat_id)
    entry.fallback_project_id = memory_project.project_id
    # The target chat is gone: the run would re-home into its project.
    del manager._chats[pass_chat.chat_id]

    with pytest.raises(MemoryProjectRefused):
        manager.prepare_schedule_chat(entry, entry.prompt, "opus", "auto", "claude")


def test_a_user_project_named_memory_is_not_the_apps_own(tmp_path: Path) -> None:
    """A user project may be called "Memory"; only the kind is the app's."""
    manager = _make_manager(tmp_path)
    user_project = manager.create_project("Memory", workspace="personal")
    _memory_project(manager)

    found = manager.find_project("Memory", "personal")

    assert found is not None
    assert found.project_id == user_project.project_id


def test_a_stale_id_resolves_to_the_user_project_not_the_apps(tmp_path: Path) -> None:
    """A routine whose project id decayed recovers by name — to its own."""
    manager = _make_manager(tmp_path)
    user_project = manager.create_project("Memory", workspace="personal")
    _memory_project(manager)

    resolved = manager._resolve_schedule_project(
        "proj-decayed", _entry(web_project_name="Memory")
    )

    assert resolved is not None
    assert resolved.project_id == user_project.project_id


async def test_a_refused_entry_does_not_stop_the_tick(tmp_path: Path) -> None:
    """The scheduler prepares every due entry in one loop.

    An entry that cannot run must not take the other due schedules down with
    it, and it must not consume a one-shot that never ran.
    """
    store = ScheduleStore(tmp_path)
    dispatched: list[str] = []

    async def dispatch(entry, model, mode, provider, *, target_chat_id=None):
        dispatched.append(entry.schedule_id)

    def prepare(entry, prompt, model, mode, provider):
        raise MemoryProjectRefused(
            "the Memory project holds the engine's own memory passes"
        )

    mgr = ScheduleManager(store=store, dispatch_to_web=dispatch, prepare_chat=prepare)
    refused = store.create(
        daily_time_utc="08:00",
        prompt="brief",
        model="opus",
        mode="auto",
        chat_id=0,
        frequency="once",
        run_at_date="2026-06-15",
        timezone_name="UTC",
        workspace="personal",
    )
    ok = store.create(
        daily_time_utc="08:00",
        prompt="brief",
        model="opus",
        mode="auto",
        chat_id=0,
        frequency="once",
        run_at_date="2026-06-15",
        timezone_name="UTC",
        workspace="personal",
    )
    # The tick skips an entry created after it, so both need a floor.
    for entry in (refused, ok):
        entry.created_at = "2026-01-01T00:00:00Z"
        store.replace(entry)
    # The second entry prepares fine; the first refuses.
    calls: list[str] = []

    def prepare_by_id(entry, prompt, model, mode, provider):
        calls.append(entry.schedule_id)
        if entry.schedule_id == refused.schedule_id:
            raise MemoryProjectRefused(
                "the Memory project holds the engine's own memory passes"
            )
        return "chat-ok"

    mgr = ScheduleManager(
        store=store, dispatch_to_web=dispatch, prepare_chat=prepare_by_id
    )

    await mgr.tick(now=datetime(2026, 6, 15, 8, 0, tzinfo=UTC))
    await asyncio.sleep(0.05)

    # Both entries were attempted, and the other one ran.
    assert len(calls) == 2
    assert ok.schedule_id in dispatched
    # The refused one-shot was not consumed, and says why it did not run.
    stored = store.get(refused.schedule_id)
    assert stored is not None
    assert stored.last_status == "error"


async def test_a_refused_command_routine_does_not_run_its_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command runs before any chat exists — but not before the target is
    checked: a routine the app will not dispatch into must not run its command
    either, and a quiet command would otherwise report success."""
    manager = _make_manager(tmp_path)
    memory_project = _memory_project(manager)
    ran: list[str] = []

    async def fake_run_command(command, cwd, env):
        ran.append(command)
        raise AssertionError("the command must not run")

    monkeypatch.setattr(
        "ciao.web.schedule_dispatch.run_schedule_command", fake_run_command
    )
    entry = _entry(
        web_project_id=memory_project.project_id, command="echo hello"
    )

    result = await manager.dispatch_schedule(
        entry, entry.prompt, "opus", "auto", "claude"
    )

    assert ran == []
    assert result["status"] == "error"


async def test_a_command_routine_whose_rehome_is_the_memory_project_does_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deleted target's re-home is the target the run would use."""
    manager = _make_manager(tmp_path)
    memory_project = _memory_project(manager)
    pass_chat = manager.create_chat(
        memory_project.project_id,
        title="Memory pass · Pricing rework",
        helper=_pass_helper(),
    )
    ran: list[str] = []

    async def fake_run_command(command, cwd, env):
        ran.append(command)
        raise AssertionError("the command must not run")

    monkeypatch.setattr(
        "ciao.web.schedule_dispatch.run_schedule_command", fake_run_command
    )
    entry = _entry(web_chat_id=pass_chat.chat_id, command="echo hello")
    entry.fallback_project_id = memory_project.project_id
    del manager._chats[pass_chat.chat_id]

    result = await manager.dispatch_schedule(
        entry, entry.prompt, "opus", "auto", "claude"
    )

    assert ran == []
    assert result["status"] == "error"


def _client(manager: ProjectChatManager, tmp_path: Path) -> TestClient:
    runtime = tmp_path / ".runtime"
    store = ScheduleStore(runtime)

    async def _dispatch(entry, model, mode, provider, *, target_chat_id=None):
        return {"status": "ok", "chat_id": target_chat_id}

    schedule_manager = ScheduleManager(
        store=store,
        dispatch_to_web=_dispatch,
        prepare_chat=lambda entry, prompt, model, mode, provider: entry.web_chat_id,
    )
    app = Starlette(
        routes=[
            Route("/api/schedules", create_schedule, methods=["POST"]),
            Route("/api/schedules/{schedule_id}", schedule_detail, methods=["PATCH"]),
            Route("/api/schedule-run/{schedule_id}", run_schedule_now, methods=["POST"]),
        ]
    )
    app.state.schedule_manager = schedule_manager
    app.state.project_chat_manager = manager
    app.state.state_store = StateStore(runtime / "state.json", tmp_path, runtime / "media")
    return TestClient(app)


def test_the_create_route_refuses_the_memory_project(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    memory_project = _memory_project(manager)
    client = _client(manager, tmp_path)

    response = client.post(
        "/api/schedules",
        json={
            "prompt": "brief",
            "frequency": "daily",
            "time": "08:00",
            "web_project_id": memory_project.project_id,
        },
    )

    assert response.status_code == 400
    assert "Memory" in response.json()["error"]


def test_the_update_route_refuses_a_pass_target(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    memory_project = _memory_project(manager)
    pass_chat = manager.create_chat(
        memory_project.project_id,
        title="Memory pass · Pricing rework",
        helper=_pass_helper(),
    )
    client = _client(manager, tmp_path)
    created = client.post(
        "/api/schedules",
        json={"prompt": "brief", "frequency": "daily", "time": "08:00"},
    )
    schedule_id = created.json()["schedule_id"]

    response = client.patch(
        f"/api/schedules/{schedule_id}", json={"web_chat_id": pass_chat.chat_id}
    )

    assert response.status_code == 400
    assert "memory pass" in response.json()["error"]


def test_an_invalid_routine_can_still_be_disabled(tmp_path: Path) -> None:
    """A routine the app will not run in must still be pausable and fixable."""
    manager = _make_manager(tmp_path)
    memory_project = _memory_project(manager)
    client = _client(manager, tmp_path)
    schedule_manager = client.app.state.schedule_manager
    entry = schedule_manager.create(
        daily_time_utc="08:00",
        prompt="brief",
        model="opus",
        mode="auto",
        chat_id=0,
        frequency="daily",
        web_project_id=memory_project.project_id,
        workspace="personal",
    )

    paused = client.patch(f"/api/schedules/{entry.schedule_id}", json={"enabled": False})
    assert paused.status_code == 200
    stored = schedule_manager._store.get(entry.schedule_id)
    assert stored is not None
    assert stored.enabled is False

    # Retargeting it to a project of the user's own is the repair.
    project = manager.create_project("Holder", workspace="personal")
    repaired = client.patch(
        f"/api/schedules/{entry.schedule_id}",
        json={"web_project_id": project.project_id},
    )
    assert repaired.status_code == 200
    stored = schedule_manager._store.get(entry.schedule_id)
    assert stored is not None
    assert stored.web_project_id == project.project_id


def test_running_a_refused_routine_says_why(tmp_path: Path) -> None:
    """A click that does nothing is not an answer: the route says why."""
    manager = _make_manager(tmp_path)
    memory_project = _memory_project(manager)
    client = _client(manager, tmp_path)
    schedule_manager = client.app.state.schedule_manager
    entry = schedule_manager.create(
        daily_time_utc="08:00",
        prompt="brief",
        model="opus",
        mode="auto",
        chat_id=0,
        frequency="daily",
        web_project_id=memory_project.project_id,
        workspace="personal",
    )

    response = client.post(f"/api/schedule-run/{entry.schedule_id}")

    assert response.status_code == 400
    assert "Memory" in response.json()["error"]
