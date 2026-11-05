"""A schedule never runs in the app's own memory machinery.

The Memory project holds the engine's memory passes and nothing else, and a
pass is the app's own turn. A routine pointed at either would land an ordinary
chat in a project where every chat is silenced, or post its prompt into a pass
that never reaches the owner — so the app refuses the target, at the route
that configures it and at the dispatch that runs it.

The dispatch-side refusal is the guarantee, and it must not raise: the
scheduler's tick prepares every due entry in one loop, so an exception there
would take the other due schedules down with it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.schedules import ScheduleEntry, ScheduleManager, ScheduleStore
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
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
    """The Memory project is app-owned: the run is skipped, not dispatched."""
    manager = _make_manager(tmp_path)
    memory_project = _memory_project(manager)
    entry = _entry(web_project_id=memory_project.project_id)

    chat_id = manager.prepare_schedule_chat(
        entry, entry.prompt, "opus", "auto", "claude"
    )

    assert chat_id is None
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

    chat_id = manager.prepare_schedule_chat(
        entry, entry.prompt, "opus", "auto", "claude"
    )

    assert chat_id is None
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


def test_a_refused_target_does_not_stop_the_next_entry(tmp_path: Path) -> None:
    """The scheduler prepares every due entry in one loop.

    A refusal that raised would abort that loop, and every later due schedule
    would be skipped for as long as the invalid entry stayed due.
    """
    manager = _make_manager(tmp_path)
    memory_project = _memory_project(manager)
    refused = _entry(
        schedule_id="sched-refused", web_project_id=memory_project.project_id
    )
    project = manager.create_project("Holder", workspace="personal")
    due = _entry(schedule_id="sched-due", web_project_id=project.project_id)

    assert manager.prepare_schedule_chat(
        refused, refused.prompt, "opus", "auto", "claude"
    ) is None
    chat_id = manager.prepare_schedule_chat(
        due, due.prompt, "opus", "auto", "claude"
    )

    assert chat_id is not None


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


def test_running_a_refused_routine_says_why(tmp_path: Path) -> None:
    """A click that does nothing is not an answer: the route says why."""
    manager = _make_manager(tmp_path)
    memory_project = _memory_project(manager)
    client = _client(manager, tmp_path)
    created = client.post(
        "/api/schedules",
        json={
            "prompt": "brief",
            "frequency": "daily",
            "time": "08:00",
            "web_project_id": memory_project.project_id,
        },
    )
    assert created.status_code == 400
    # The route refuses the configuration, so there is no entry to run.
    assert client.app.state.schedule_manager.list_entries() == []


@pytest.mark.parametrize("target", ["project", "chat"])
def test_the_route_refusal_names_the_reason(
    tmp_path: Path, target: str
) -> None:
    """Both refusals say what is wrong, in the PWA's own words."""
    manager = _make_manager(tmp_path)
    memory_project = _memory_project(manager)
    client = _client(manager, tmp_path)
    body = {"prompt": "brief", "frequency": "daily", "time": "08:00"}
    if target == "project":
        body["web_project_id"] = memory_project.project_id
    else:
        pass_chat = manager.create_chat(
            memory_project.project_id,
            title="Memory pass · Pricing rework",
            helper=_pass_helper(),
        )
        body["web_chat_id"] = pass_chat.chat_id

    response = client.post("/api/schedules", json=body)

    assert response.status_code == 400
    assert response.json()["error"]
