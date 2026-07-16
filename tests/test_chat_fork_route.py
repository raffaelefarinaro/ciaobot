from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import chat_fork


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )


def _make_client(manager: ProjectChatManager) -> TestClient:
    app = Starlette(
        routes=[Route("/api/chats/{chat_id}/fork", chat_fork, methods=["POST"])]
    )
    app.state.project_chat_manager = manager
    return TestClient(app, raise_server_exceptions=False)


def _payload() -> dict:
    return {
        "messages": [
            {"role": "user", "content": "Question", "turn_index": 0},
            {"role": "assistant", "content": "Answer"},
        ],
        "turn_index": 0,
    }


def test_chat_fork_route_creates_and_returns_the_fork(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Forks", workspace="work")
    source = manager.create_chat(project.project_id, title="Original")
    client = _make_client(manager)

    response = client.post(f"/api/chats/{source.chat_id}/fork", json=_payload())

    assert response.status_code == 200
    data = response.json()
    assert data["title"] == "Original · Fork 1"
    assert data["project_id"] == project.project_id
    assert data["local"] is True
    assert manager.get_chat(data["chat_id"]) is not None


def test_chat_fork_route_returns_404_for_missing_source(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    client = _make_client(manager)

    response = client.post("/api/chats/chat-missing/fork", json=_payload())

    assert response.status_code == 404
    assert response.json() == {"error": "not found"}


def test_chat_fork_route_returns_400_for_invalid_payload(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Forks", workspace="work")
    source = manager.create_chat(project.project_id, title="Original")
    client = _make_client(manager)

    response = client.post(
        f"/api/chats/{source.chat_id}/fork",
        json={"messages": [{"role": "user", "content": "Question"}], "turn_index": 0},
    )

    assert response.status_code == 400
    assert "assistant" in response.json()["error"]


def test_chat_fork_route_returns_400_for_non_integer_turn(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Forks", workspace="work")
    source = manager.create_chat(project.project_id, title="Original")
    client = _make_client(manager)
    payload = _payload()
    payload["turn_index"] = "0"

    response = client.post(f"/api/chats/{source.chat_id}/fork", json=payload)

    assert response.status_code == 400
    assert "turn_index" in response.json()["error"]


def test_chat_fork_route_returns_500_without_a_leftover_on_save_failure(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Forks", workspace="work")
    source = manager.create_chat(project.project_id, title="Original")
    before = set(manager._chats)

    def fail_save(*args, **kwargs) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(manager, "_save", fail_save)
    client = _make_client(manager)

    response = client.post(f"/api/chats/{source.chat_id}/fork", json=_payload())

    assert response.status_code == 500
    assert "Failed to fork chat" in response.json()["error"]
    assert set(manager._chats) == before

