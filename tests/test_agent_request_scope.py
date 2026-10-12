"""A chat's workspace filesystem scope reaches the AgentRequest (issue #1149)."""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest

from ciao import backup_scope
from ciao.config import CiaoConfig, reset_reroot_cache
from ciao.workspace_reroot import mark_born_per_root
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ChatInfo, ProjectChatManager

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


def _workspace_scoped_chat(
    manager: ProjectChatManager, *, grants_data_root: bool = False
) -> ChatInfo:
    config = manager._config
    workspace = config.workspace("personal")
    assert workspace is not None
    config.workspaces["personal"] = dataclasses.replace(workspace, agent_fs_scope="workspace")
    project = manager.create_project("setup", workspace="personal")
    return manager.create_chat(
        project.project_id, provider="claude", grants_data_root=grants_data_root
    )


def test_a_setup_chat_is_granted_the_data_root_after_the_workspace_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The memory-backup setup chat (#1328) may write the install's data root,
    the parent of every workspace folder, and nothing else changes for it."""
    manager = _make_manager(tmp_path)
    config = manager._config
    data = tmp_path / "data root"
    data.mkdir()
    monkeypatch.setattr(backup_scope, "data_root", lambda _config: data)
    chat = _workspace_scoped_chat(manager, grants_data_root=True)

    request = manager.build_agent_request(chat, prompt="hi")

    agent_root = config.agent_root("personal").resolve()
    vault_root = config.workspace_vault_root("personal").resolve()
    assert request.agent_fs_scope == "workspace"
    assert request.agent_roots == (
        str(agent_root),
        str(vault_root),
        str(data.resolve()),
    )


def test_an_ordinary_chat_is_not_granted_the_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _make_manager(tmp_path)
    config = manager._config
    data = tmp_path / "data root"
    data.mkdir()
    monkeypatch.setattr(backup_scope, "data_root", lambda _config: data)
    chat = _workspace_scoped_chat(manager)

    request = manager.build_agent_request(chat, prompt="hi")

    assert request.agent_roots == tuple(dict.fromkeys((
        str(config.agent_root("personal").resolve()),
        str(config.workspace_vault_root("personal").resolve()),
    )))
    assert str(data.resolve()) not in request.agent_roots


def test_a_granted_data_root_that_is_the_agent_root_is_not_listed_twice(
    tmp_path: Path,
) -> None:
    """The default layout keeps the install root as both the agent root and the
    data root, so the grant adds nothing new and must not repeat a root."""
    manager = _make_manager(tmp_path)
    chat = _workspace_scoped_chat(manager, grants_data_root=True)

    request = manager.build_agent_request(chat, prompt="hi")

    assert request.agent_roots == tuple(dict.fromkeys(request.agent_roots))
    assert len(request.agent_roots) == 2


def test_a_setup_chat_in_machine_scope_has_no_roots(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    config = manager._config
    workspace = config.workspace("personal")
    assert workspace is not None
    config.workspaces["personal"] = dataclasses.replace(workspace, agent_fs_scope="machine")
    project = manager.create_project("setup", workspace="personal")
    chat = manager.create_chat(project.project_id, provider="claude", grants_data_root=True)

    assert manager._agent_fs_scope_for_chat(chat) == ("machine", ())


def test_the_data_root_grant_round_trips_and_is_off_for_old_records(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("setup", workspace="personal")
    granted = manager.create_chat(project.project_id, grants_data_root=True)
    plain = manager.create_chat(project.project_id)
    path = tmp_path / ".runtime" / "web_projects.json"

    reloaded = _make_manager(tmp_path)
    assert reloaded.get_chat(granted.chat_id).grants_data_root is True
    assert reloaded.get_chat(plain.chat_id).grants_data_root is False

    # A record written before the key existed restores as not granted.
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["chats"][granted.chat_id]["grants_data_root"]
    path.write_text(json.dumps(data), encoding="utf-8")
    legacy = _make_manager(tmp_path)
    assert legacy.get_chat(granted.chat_id).grants_data_root is False


def test_granting_an_existing_chat_persists_and_widens_its_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live setup chat from before the grant existed is granted in place."""
    manager = _make_manager(tmp_path)
    data = tmp_path / "data root"
    data.mkdir()
    monkeypatch.setattr(backup_scope, "data_root", lambda _config: data)
    chat = _workspace_scoped_chat(manager)
    assert manager.build_agent_request(chat, prompt="hi").agent_roots == tuple(dict.fromkeys((
        str(manager._config.agent_root("personal").resolve()),
        str(manager._config.workspace_vault_root("personal").resolve()),
    )))

    manager.grant_data_root(chat.chat_id)
    manager.grant_data_root(chat.chat_id)

    assert manager.get_chat(chat.chat_id).grants_data_root is True
    assert _make_manager(tmp_path).get_chat(chat.chat_id).grants_data_root is True
    assert str(data.resolve()) in manager.build_agent_request(chat, prompt="hi").agent_roots


def test_the_grant_on_a_per_root_install_reaches_the_parent_of_every_workspace(
    tmp_path: Path,
) -> None:
    """A fresh install is born per-root: each workspace is a folder under the
    install root, so the data root (the install root) is the parent of the
    setup chat's own agent root. Uses the real receipt, not a patched root."""
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    mark_born_per_root(tmp_path, runtime, ["personal"])
    reset_reroot_cache()
    try:
        manager = _make_manager(tmp_path)
        chat = _workspace_scoped_chat(manager, grants_data_root=True)

        request = manager.build_agent_request(chat, prompt="hi")

        agent_root = manager._config.agent_root("personal").resolve()
        assert agent_root == (tmp_path / "personal").resolve()
        assert request.agent_roots[0] == str(agent_root)
        assert str(tmp_path.resolve()) in request.agent_roots
        assert Path(request.agent_roots[-1]) == tmp_path.resolve()
    finally:
        reset_reroot_cache()
