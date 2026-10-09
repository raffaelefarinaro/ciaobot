"""A chat's workspace filesystem scope reaches the AgentRequest (issue #1149)."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

from ciao.config import CiaoConfig
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


def test_build_agent_request_copies_workspace_scope_and_roots(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    config = manager._config
    workspace = config.workspace("personal")
    assert workspace is not None
    config.workspaces["personal"] = dataclasses.replace(workspace, agent_fs_scope="workspace")
    project = manager.create_project("scoped", workspace="personal")
    chat = manager.create_chat(project.project_id, provider="claude")

    request = manager.build_agent_request(chat, prompt="hi")

    agent_root = config.agent_root("personal").resolve()
    vault_root = config.workspace_vault_root("personal").resolve()
    assert request.agent_fs_scope == "workspace"
    assert request.agent_roots == tuple(dict.fromkeys((str(agent_root), str(vault_root))))
    assert request.agent_roots[0] == str(agent_root)


def test_build_agent_request_machine_scope_has_no_roots(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    config = manager._config
    workspace = config.workspace("personal")
    assert workspace is not None
    config.workspaces["personal"] = dataclasses.replace(workspace, agent_fs_scope="machine")
    project = manager.create_project("unscoped", workspace="personal")
    chat = manager.create_chat(project.project_id, provider="claude")

    request = manager.build_agent_request(chat, prompt="hi")

    assert request.agent_fs_scope == "machine"
    assert request.agent_roots == ()


@pytest.mark.parametrize(("platform", "expected"), [("darwin", "workspace"), ("win32", "machine")])
def test_build_agent_request_unset_scope_uses_platform_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, expected: str
) -> None:
    """A workspace that never chose a File access scope runs under the platform default."""
    monkeypatch.setattr(sys, "platform", platform)
    manager = _make_manager(tmp_path)
    config = manager._config
    workspace = config.workspace("personal")
    assert workspace is not None
    config.workspaces["personal"] = dataclasses.replace(workspace, agent_fs_scope=None)
    project = manager.create_project("defaulted", workspace="personal")
    chat = manager.create_chat(project.project_id, provider="claude")

    request = manager.build_agent_request(chat, prompt="hi")

    assert request.agent_fs_scope == expected
