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


def _linked_repo_manager(tmp_path: Path, scope: str, *, repo: Path):
    manager = _make_manager(tmp_path)
    config = manager._config
    workspace = config.workspace("personal")
    assert workspace is not None
    config.workspaces["personal"] = dataclasses.replace(workspace, agent_fs_scope=scope)
    project = manager.create_project("linked", workspace="personal")
    project.repo_path = str(repo)
    chat = manager.create_chat(project.project_id, provider="claude")
    chat.code_mode = True
    return manager, chat


def test_a_linked_repo_is_a_workspace_root_and_on_the_request(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    manager, chat = _linked_repo_manager(tmp_path, "workspace", repo=repo)
    config = manager._config

    request = manager.build_agent_request(chat, prompt="hi")

    agent_root = config.agent_root("personal").resolve()
    vault_root = config.workspace_vault_root("personal").resolve()
    expected = tuple(dict.fromkeys((str(agent_root), str(vault_root), str(repo.resolve()))))
    assert request.agent_roots == expected
    assert request.agent_roots[-1] == str(repo.resolve())
    assert request.linked_repo == str(repo.resolve())


def test_a_linked_repo_reaches_a_machine_scope_request(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    manager, chat = _linked_repo_manager(tmp_path, "machine", repo=repo)

    request = manager.build_agent_request(chat, prompt="hi")

    assert request.agent_roots == ()
    assert request.linked_repo == str(repo.resolve())


def test_a_missing_linked_repo_is_not_granted(tmp_path: Path) -> None:
    manager, chat = _linked_repo_manager(tmp_path, "workspace", repo=tmp_path / "gone")

    request = manager.build_agent_request(chat, prompt="hi")

    assert request.linked_repo == ""
    assert all("gone" not in root for root in request.agent_roots)


def test_a_memory_pass_chat_is_not_granted_the_linked_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    manager, _ = _linked_repo_manager(tmp_path, "workspace", repo=repo)
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
    assert str(repo.resolve()) not in request.agent_roots


def test_a_linked_repo_with_code_mode_off_is_not_granted(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    manager, chat = _linked_repo_manager(tmp_path, "workspace", repo=repo)
    chat.code_mode = False

    request = manager.build_agent_request(chat, prompt="hi")

    assert request.linked_repo == ""
    assert str(repo.resolve()) not in request.agent_roots


def test_workspace_scope_request_grants_runtime_files_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ciao.fs_sandbox.gws_tool_read_roots", lambda path: [])
    manager = _make_manager(tmp_path)
    config = manager._config
    workspace = config.workspace("personal")
    assert workspace is not None
    config.workspaces["personal"] = dataclasses.replace(workspace, agent_fs_scope="workspace")
    project = manager.create_project("scoped", workspace="personal")
    chat = manager.create_chat(project.project_id, provider="claude")

    request = manager.build_agent_request(chat, prompt="hi")

    runtime = config.state_path.parent
    expected = (
        str(runtime / "migration" / "workspace-rooting.json"),
        str(runtime / "workspaces.json"),
    )
    assert request.agent_read_only == expected
    assert not set(expected) & set(request.agent_roots)

    config.workspaces["personal"] = dataclasses.replace(workspace, agent_fs_scope="machine")
    assert manager.build_agent_request(chat, prompt="hi").agent_read_only == ()


def _workspace_manager(tmp_path: Path) -> tuple[ProjectChatManager, ChatInfo]:
    manager = _make_manager(tmp_path)
    config = manager._config
    workspace = config.workspace("personal")
    assert workspace is not None
    config.workspaces["personal"] = dataclasses.replace(workspace, agent_fs_scope="workspace")
    project = manager.create_project("scoped", workspace="personal")
    return manager, manager.create_chat(project.project_id, provider="claude")


def test_workspace_scope_request_grants_gws_tool_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gws_bin = tmp_path / "gws-bin"
    monkeypatch.setattr("ciao.fs_sandbox.gws_tool_read_roots", lambda path: [gws_bin])
    manager, chat = _workspace_manager(tmp_path)

    request = manager.build_agent_request(chat, prompt="hi")

    runtime = manager._config.state_path.parent
    assert request.agent_read_only == (
        str(runtime / "migration" / "workspace-rooting.json"),
        str(runtime / "workspaces.json"),
        str(gws_bin),
    )
    assert str(gws_bin) not in request.agent_roots

    config = manager._config
    config.workspaces["personal"] = dataclasses.replace(
        config.workspaces["personal"], agent_fs_scope="machine"
    )
    assert manager.build_agent_request(chat, prompt="hi").agent_read_only == ()


def test_workspace_scope_grants_linked_gws_credentials_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.gws_auth import profile_config_dir

    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    mark_born_per_root(tmp_path, runtime, ["personal"])
    reset_reroot_cache()
    try:
        manager, chat = _workspace_manager(tmp_path)
        monkeypatch.setattr(manager, "_workspace_gws_profile", lambda workspace: "personal")
        creds = profile_config_dir(manager._config, "personal")
        assert creds is not None
        creds.mkdir(parents=True)

        request = manager.build_agent_request(chat, prompt="hi")

        agent_root = manager._config.agent_root("personal").resolve()
        assert not creds.resolve().is_relative_to(agent_root)
        assert request.agent_roots.count(str(creds.resolve())) == 1
    finally:
        reset_reroot_cache()


def test_workspace_scope_skips_unlinked_or_missing_gws_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.gws_auth import profile_config_dir

    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    mark_born_per_root(tmp_path, runtime, ["personal"])
    reset_reroot_cache()
    try:
        manager, chat = _workspace_manager(tmp_path)
        monkeypatch.setattr(manager, "_workspace_gws_profile", lambda workspace: "")
        (tmp_path / ".runtime" / "gws-unlinked" / "personal").mkdir(parents=True)

        roots = manager.build_agent_request(chat, prompt="hi").agent_roots
        assert not any("secrets" in r or "gws-unlinked" in r for r in roots)

        monkeypatch.setattr(manager, "_workspace_gws_profile", lambda workspace: "personal")
        creds = profile_config_dir(manager._config, "personal")
        assert creds is not None and not creds.exists()

        roots = manager.build_agent_request(chat, prompt="hi").agent_roots
        assert str(creds.resolve()) not in roots
    finally:
        reset_reroot_cache()
