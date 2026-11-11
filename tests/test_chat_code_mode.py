"""Code mode is derived: a chat is in code mode exactly when its project's linked
repository exists on this machine, except a memory-pass chat. Nothing is stored
per chat and nothing switches it."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.providers.claude import ClaudeProvider
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import chat_detail
from tests.conftest import attach_stub_mcp


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
    return attach_stub_mcp(ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    ))


def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    (path / "AGENTS.md").write_text("Repo rules.\n", encoding="utf-8")
    return path


def test_a_chat_in_a_linked_project_is_in_code_mode(tmp_path: Path) -> None:
    manager = _build(tmp_path)
    repo = _git_repo(tmp_path / "repo")
    project = manager.create_project("Linked", workspace="work")
    project.repo_path = str(repo)
    chat = manager.create_chat(project.project_id)

    assert manager.linked_repo_for_chat(chat) == str(repo.resolve())
    prefix = manager._build_prompt_prefix(chat)
    assert "[Linked repository]" in prefix
    assert "Repo rules." in prefix


def test_a_chat_in_an_unlinked_project_is_not_in_code_mode(tmp_path: Path) -> None:
    manager = _build(tmp_path)
    project = manager.create_project("Plain", workspace="work")
    chat = manager.create_chat(project.project_id)

    assert manager.linked_repo_for_chat(chat) == ""
    assert "[Linked repository]" not in manager._build_prompt_prefix(chat)
    assert manager.build_agent_request(chat, prompt="hi").linked_repo == ""


def test_a_missing_repository_folder_turns_code_mode_off(tmp_path: Path) -> None:
    manager = _build(tmp_path)
    project = manager.create_project("Linked", workspace="work")
    project.repo_path = str(tmp_path / "gone")
    chat = manager.create_chat(project.project_id)

    assert manager.linked_repo_for_chat(chat) == ""
    assert manager.build_agent_request(chat, prompt="hi").linked_repo == ""
    # The agent is still told the link is broken rather than left guessing.
    assert "not available on this machine" in manager._build_prompt_prefix(chat)


def test_a_memory_pass_chat_is_never_in_code_mode(tmp_path: Path) -> None:
    manager = _build(tmp_path)
    repo = _git_repo(tmp_path / "repo")
    project = manager._memory_pass.ensure_project("work")
    project.repo_path = str(repo)
    chat = manager.create_chat(
        project.project_id,
        helper={"kind": "memory_pass", "source_chat_id": "chat-1", "archive_policy": "when_clean"},
    )

    assert manager.linked_repo_for_chat(chat) == ""
    assert "[Linked repository]" not in manager._build_prompt_prefix(chat)


def test_a_link_change_mid_chat_reaches_the_next_turn(tmp_path: Path) -> None:
    """Linking, relinking and unlinking the project re-sends the stable context
    and changes what the provider is started with, so the next turn restarts it."""
    manager = _build(tmp_path)
    repo_a = _git_repo(tmp_path / "repo-a")
    repo_b = _git_repo(tmp_path / "repo-b")
    project = manager.create_project("Later", workspace="work")
    chat = manager.create_chat(project.project_id, provider="claude")
    claude = ClaudeProvider(tmp_path)

    def take_turn() -> tuple[object, str]:
        """Build the next turn's request and mark its stable context as delivered."""
        request = manager.build_agent_request(chat, prompt="hi")
        chat.session_id = "sess-1"
        chat.context_digest, chat.context_session_id = manager._stable_context_marker(chat)
        return claude._spawn_key_for(request), request.linked_repo

    unlinked_key, linked = take_turn()
    assert linked == ""
    assert not manager._needs_stable_context(chat)

    manager.update_project(project.project_id, repo_path=str(repo_a))
    assert manager._needs_stable_context(chat)
    assert "[Linked repository]" in manager._build_prompt_prefix(chat)
    key_a, linked = take_turn()
    assert linked == str(repo_a.resolve())
    assert key_a != unlinked_key

    manager.update_project(project.project_id, repo_path=str(repo_b))
    assert manager._needs_stable_context(chat)
    key_b, linked = take_turn()
    assert linked == str(repo_b.resolve())
    assert key_b != key_a

    manager.update_project(project.project_id, repo_path="")
    assert manager._needs_stable_context(chat)
    assert "[Linked repository]" not in manager._build_prompt_prefix(chat)
    key_off, linked = take_turn()
    assert linked == ""
    assert key_off != key_b


def test_moving_a_chat_into_a_linked_project_puts_it_in_code_mode(tmp_path: Path) -> None:
    manager = _build(tmp_path)
    repo = _git_repo(tmp_path / "repo")
    plain = manager.create_project("Plain", workspace="work")
    linked = manager.create_project("Linked", workspace="work")
    linked.repo_path = str(repo)
    chat = manager.create_chat(plain.project_id)
    assert manager.linked_repo_for_chat(chat) == ""

    manager.update_chat(chat.chat_id, project_id=linked.project_id)
    assert manager.linked_repo_for_chat(chat) == str(repo.resolve())


def test_moving_a_chat_clears_its_repo_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _build(tmp_path)
    repo = _git_repo(tmp_path / "repo")
    linked = manager.create_project("Linked", workspace="work")
    linked.repo_path = str(repo)
    plain = manager.create_project("Plain", workspace="work")
    chat = manager.create_chat(linked.project_id)
    chat.repo_location = {"path": str(repo), "branch": "main", "is_worktree": False, "exists": True}
    published: list[dict] = []
    monkeypatch.setattr(manager._events, "publish", published.append)

    manager.update_chat(chat.chat_id, project_id=plain.project_id)

    assert chat.repo_location == {}
    assert [e["type"] for e in published] == ["chat_moved", "chat_repo_location"]
    assert published[1]["repo_location"] == {}


def test_a_chat_persisted_with_the_old_code_mode_key_still_loads(tmp_path: Path) -> None:
    manager = _build(tmp_path)
    project = manager.create_project("Linked", workspace="work")
    chat = manager.create_chat(project.project_id, title="old")
    path = tmp_path / ".runtime" / "web_projects.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["chats"][chat.chat_id]["code_mode"] = True
    path.write_text(json.dumps(data), encoding="utf-8")

    restarted = _build(tmp_path)

    reloaded = restarted.get_chat(chat.chat_id)
    assert reloaded is not None and reloaded.title == "old"
    assert "code_mode" not in restarted._state_payload()["chats"][chat.chat_id]


def test_the_chat_payload_and_patch_carry_no_code_mode(tmp_path: Path) -> None:
    manager = _build(tmp_path)
    project = manager.create_project("Plain", workspace="work")
    chat = manager.create_chat(project.project_id)
    assert "code_mode" not in chat.to_dict()

    app = Starlette(routes=[Route("/api/chats/{chat_id}", chat_detail, methods=["PATCH"])])
    app.state.project_chat_manager = manager
    app.state.config = manager._config
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.patch(f"/api/chats/{chat.chat_id}", json={"code_mode": True, "title": "t"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "t"
    assert "code_mode" not in resp.json()
    assert manager.linked_repo_for_chat(chat) == ""
