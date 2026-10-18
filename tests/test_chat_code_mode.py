"""Per-chat code mode (#1450): off by default, persisted, broadcast, marked."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import chat_detail


def _build(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=f"memory-vault/{name}")
            for name in ("personal", "work")
        },
    )
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )


def _make_client(manager: ProjectChatManager) -> TestClient:
    app = Starlette(routes=[Route("/api/chats/{chat_id}", chat_detail, methods=["PATCH"])])
    app.state.project_chat_manager = manager
    app.state.config = manager._config
    return TestClient(app, raise_server_exceptions=False)


def _linked(tmp_path: Path):  # type: ignore[no-untyped-def]
    manager = _build(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    project = manager.create_project("Linked", workspace="work")
    project.repo_path = str(repo)
    return manager, project, manager.create_chat(project.project_id), repo


def test_unlinked_chat_persists_without_code_mode_keys(tmp_path: Path) -> None:
    manager = _build(tmp_path)
    project = manager.create_project("Plain", workspace="work")
    chat = manager.create_chat(project.project_id)
    stored = manager._state_payload()["chats"][chat.chat_id]
    assert "code_mode" not in stored
    assert "code_mode_events" not in stored
    assert chat.to_dict()["code_mode"] is False
    assert chat.to_dict()["code_mode_events"] == []


def test_code_mode_round_trips_through_a_restart(tmp_path: Path) -> None:
    manager, _project, chat, _repo = _linked(tmp_path)
    manager.set_code_mode(chat.chat_id, True, actor="user")
    restarted = _build(tmp_path)
    reloaded = restarted._chats[chat.chat_id]
    assert reloaded.code_mode is True
    assert set(reloaded.code_mode_events[0]) == {"code_mode", "actor", "at", "after_turn_index"}
    restarted.set_code_mode(chat.chat_id, False, actor="user")
    assert "code_mode" not in restarted._state_payload()["chats"][chat.chat_id]


def test_set_code_mode_rejects_unlinked_missing_and_archived(tmp_path: Path) -> None:
    manager, project, chat, repo = _linked(tmp_path)
    assert manager.set_code_mode("nope", True, actor="user") is None
    plain = manager.create_project("Plain", workspace="work")
    plain_chat = manager.create_chat(plain.project_id)
    with pytest.raises(ValueError):
        manager.set_code_mode(plain_chat.chat_id, True, actor="user")
    repo.rmdir()
    with pytest.raises(ValueError):
        manager.set_code_mode(chat.chat_id, True, actor="user")
    repo.mkdir()
    chat.archived = True
    with pytest.raises(ValueError):
        manager.set_code_mode(chat.chat_id, True, actor="user")


def test_set_code_mode_publishes_once_and_records_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, project, chat, _repo = _linked(tmp_path)
    published: list[dict] = []
    monkeypatch.setattr(manager._events, "publish", published.append)
    manager.set_code_mode(chat.chat_id, True, actor="agent")
    assert len(published) == 1
    event = published[0]
    assert set(event) == {"type", "chat_id", "project_id", "code_mode", "actor", "at"}
    assert event["type"] == "chat_code_mode" and event["actor"] == "agent"
    manager.set_code_mode(chat.chat_id, True, actor="agent")
    assert len(published) == 1
    assert len(chat.code_mode_events) == 1
    assert chat.code_mode_events[0]["after_turn_index"] == chat.user_turn_count - 1


def test_switching_off_clears_repo_location_and_publishes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _project, chat, _repo = _linked(tmp_path)
    manager.set_code_mode(chat.chat_id, True, actor="user")
    chat.repo_location = {"branch": "main"}
    published: list[dict] = []
    monkeypatch.setattr(manager._events, "publish", published.append)
    manager.set_code_mode(chat.chat_id, False, actor="user")
    assert chat.repo_location == {}
    assert [e["type"] for e in published] == ["chat_code_mode", "chat_repo_location"]
    assert published[1]["repo_location"] == {}


def test_code_mode_flag_in_unlinked_project_is_not_effective(tmp_path: Path) -> None:
    manager = _build(tmp_path)
    project = manager.create_project("Plain", workspace="work")
    chat = manager.create_chat(project.project_id)
    chat.code_mode = True
    assert manager._linked_repo_for_chat(chat) == ""
    assert "[Linked repository]" not in manager._build_prompt_prefix(chat)


def test_patch_code_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _project, chat, _repo = _linked(tmp_path)
    client = _make_client(manager)
    published: list[dict] = []
    monkeypatch.setattr(manager._events, "publish", published.append)
    resp = client.patch(f"/api/chats/{chat.chat_id}", json={"code_mode": True})
    assert resp.status_code == 200
    assert resp.json()["code_mode"] is True
    assert published[0]["actor"] == "user"
    plain = manager.create_project("Plain", workspace="work")
    plain_chat = manager.create_chat(plain.project_id)
    assert client.patch(f"/api/chats/{plain_chat.chat_id}", json={"code_mode": True}).status_code == 400
    assert client.patch(f"/api/chats/{chat.chat_id}", json={"code_mode": "yes"}).status_code == 400


def test_moving_a_chat_turns_code_mode_off(tmp_path: Path) -> None:
    manager, _project, chat, _repo = _linked(tmp_path)
    manager.set_code_mode(chat.chat_id, True, actor="user")
    other = manager.create_project("Other", workspace="work")
    manager.update_chat(chat.chat_id, project_id=other.project_id)
    assert chat.code_mode is False
    assert [e["code_mode"] for e in chat.code_mode_events] == [True, False]
