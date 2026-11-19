"""The linked repository still reaches an agent request, with no filesystem scope."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from ciao.config import CiaoConfig
from ciao.models import AgentRequest
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager

from tests.conftest import attach_stub_mcp


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    return attach_stub_mcp(ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    ))


def _linked_repo_manager(tmp_path: Path, *, repo: Path):
    manager = _make_manager(tmp_path)
    project = manager.create_project("linked", workspace="personal")
    project.repo_path = str(repo)
    chat = manager.create_chat(project.project_id, provider="claude")
    return manager, chat


def test_agent_request_has_no_scope_fields() -> None:
    names = {field.name for field in dataclasses.fields(AgentRequest)}
    assert names.isdisjoint({
        "agent_fs_scope",
        "agent_roots",
        "agent_read_only",
        "agent_denied",
        "agent_denied_except",
        "memory_pass",
    })


def test_a_linked_repo_is_on_the_request(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    manager, chat = _linked_repo_manager(tmp_path, repo=repo)

    request = manager.build_agent_request(chat, prompt="hi")

    assert request.linked_repo == str(repo.resolve())


def test_a_missing_linked_repo_is_not_granted(tmp_path: Path) -> None:
    manager, chat = _linked_repo_manager(tmp_path, repo=tmp_path / "gone")

    request = manager.build_agent_request(chat, prompt="hi")

    assert request.linked_repo == ""


def test_a_memory_pass_chat_is_not_granted_the_linked_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    manager, _chat = _linked_repo_manager(tmp_path, repo=repo)
    project = manager._memory_pass.ensure_project("personal")
    project.repo_path = str(repo)
    chat = manager.create_chat(
        project.project_id,
        provider="claude",
        helper={
            "kind": "memory_pass",
            "source_chat_id": "chat-1",
            "archive_policy": "when_clean",
        },
    )

    request = manager.build_agent_request(chat, prompt="hi")

    assert request.linked_repo == ""
