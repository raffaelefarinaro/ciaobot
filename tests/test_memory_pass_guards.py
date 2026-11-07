"""The Memory project holds the engine's own passes, and only those.

The pass predicate falls back on the project kind: a chat in the app-owned
Memory project is silenced whatever its helper says, because a helper that
failed to normalize no longer identifies its pass. That is only safe while the
project holds the engine's own passes — so the engine refuses to open an
ordinary chat there, refuses to move one in, and refuses a client-supplied
pass helper. A client that could set either signal could silence any chat it
created.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.control_plane import AgentPrincipal, CiaoControlPlane, ControlPlaneError
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import create_project_chat


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
            for name in ("personal", "work")
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


def _client(manager: ProjectChatManager) -> TestClient:
    app = Starlette(
        routes=[
            Route("/api/projects/{project_id}/chats", create_project_chat, methods=["POST"])
        ]
    )
    app.state.project_chat_manager = manager
    return TestClient(app, raise_server_exceptions=False)


def test_the_create_route_refuses_a_client_supplied_pass_helper(tmp_path: Path) -> None:
    """A pass helper is the engine's own: it is what silences a chat."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Shipped", workspace="work")
    client = _client(manager)

    response = client.post(
        f"/api/projects/{project.project_id}/chats",
        json={
            "title": "Ordinary",
            "helper": {
                "kind": "memory_pass",
                "source_chat_id": "chat-1",
                "archive_policy": "when_clean",
            },
        },
    )

    assert response.status_code == 400
    assert "engine" in response.json()["error"]
    # Nothing was created.
    assert manager.list_chats() == []


def test_the_create_route_refuses_a_malformed_pass_helper(tmp_path: Path) -> None:
    """A helper that would not normalize is refused as well."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Shipped", workspace="work")
    client = _client(manager)

    response = client.post(
        f"/api/projects/{project.project_id}/chats",
        json={"title": "Ordinary", "helper": {"kind": "memory_pass"}},
    )

    assert response.status_code == 400
    assert manager.list_chats() == []


def test_the_create_route_refuses_a_chat_in_the_memory_project(tmp_path: Path) -> None:
    """The Memory project is app-owned: a client cannot open a chat in it."""
    manager = _make_manager(tmp_path)
    memory_project = manager._memory_pass.ensure_project("work")
    client = _client(manager)

    response = client.post(
        f"/api/projects/{memory_project.project_id}/chats", json={"title": "Ordinary"}
    )

    assert response.status_code == 400
    assert "Memory" in response.json()["error"]
    assert manager.list_chats() == []


def test_the_create_route_still_accepts_an_ordinary_chat(tmp_path: Path) -> None:
    """Control: the guard refuses the pass signal, not chat creation."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Shipped", workspace="work")
    client = _client(manager)

    response = client.post(
        f"/api/projects/{project.project_id}/chats", json={"title": "Ordinary"}
    )

    assert response.status_code == 201
    assert response.json()["project_id"] == project.project_id
    assert len(manager.list_chats()) == 1


def _plane(manager: ProjectChatManager) -> CiaoControlPlane:
    return CiaoControlPlane(
        manager._config,
        project_chat_manager=manager,
        schedule_manager=SimpleNamespace(),
    )


def _principal(project_id: str = "") -> AgentPrincipal:
    return AgentPrincipal(
        token_id="token-1",
        chat_id="chat-1",
        project_id=project_id,
        workspace="work",
        provider="claude",
    )


def test_the_agent_surface_refuses_a_chat_in_the_memory_project(tmp_path: Path) -> None:
    """`ciao chat create --project Memory` is refused, not silently muted."""
    manager = _make_manager(tmp_path)
    memory_project = manager._memory_pass.ensure_project("work")
    plane = _plane(manager)

    with pytest.raises(ControlPlaneError) as excinfo:
        plane.chat_create(_principal(), memory_project.project_id)

    assert excinfo.value.code == "invalid_project"
    assert manager.list_chats() == []


def test_the_agent_surface_refuses_a_move_into_the_memory_project(tmp_path: Path) -> None:
    """`ciao chat update --project Memory` is refused too."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Shipped", workspace="work")
    chat = manager.create_chat(project.project_id, title="Ordinary")
    memory_project = manager._memory_pass.ensure_project("work")
    plane = _plane(manager)

    with pytest.raises(ControlPlaneError) as excinfo:
        plane.chat_update(_principal(), chat.chat_id, project_id=memory_project.project_id)

    assert excinfo.value.code == "invalid_project"
    assert manager.get_chat(chat.chat_id).project_id == project.project_id


def test_the_agent_surface_still_opens_an_ordinary_chat(tmp_path: Path) -> None:
    """Control: the refusal is about the Memory project, not about creating."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Shipped", workspace="work")
    plane = _plane(manager)

    result = plane.chat_create(_principal(), project.project_id)

    assert result["ok"] is True
    assert result["data"]["project_id"] == project.project_id
