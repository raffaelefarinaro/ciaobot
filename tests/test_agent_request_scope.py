"""A chat's workspace filesystem scope reaches the AgentRequest (issue #1149)."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
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


def _workspace_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ProjectChatManager, ChatInfo]:
    # Never depend on whether gws is installed on the host running the tests.
    monkeypatch.setattr("ciao.fs_sandbox.gws_tool_read_roots", lambda path: [])
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
    manager, chat = _workspace_manager(tmp_path, monkeypatch)
    monkeypatch.setattr("ciao.fs_sandbox.gws_tool_read_roots", lambda path: [gws_bin])

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


def test_workspace_scope_grants_gh_config_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh_dir = tmp_path / "gh-config"
    gh_dir.mkdir()
    manager, chat = _workspace_manager(tmp_path, monkeypatch)
    monkeypatch.setattr("ciao.fs_sandbox.gh_config_read_root", lambda env: gh_dir)

    request = manager.build_agent_request(chat, prompt="hi")

    assert str(gh_dir) in request.agent_read_only
    assert str(gh_dir) not in request.agent_roots

    config = manager._config
    config.workspaces["personal"] = dataclasses.replace(
        config.workspaces["personal"], agent_fs_scope="machine"
    )
    assert manager.build_agent_request(chat, prompt="hi").agent_read_only == ()


def test_workspace_scope_injects_gh_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, chat = _workspace_manager(tmp_path, monkeypatch)
    monkeypatch.setattr("ciao.fs_sandbox.gh_token_for_sandbox", lambda gh, env, **kw: "tok")
    monkeypatch.setattr("ciao.tool_path.resolve_tool", lambda cmd: "/bin/gh")

    assert manager.build_agent_request(chat, prompt="hi").extra_env["GH_TOKEN"] == "tok"

    config = manager._config
    config.workspaces["personal"] = dataclasses.replace(
        config.workspaces["personal"], agent_fs_scope="machine"
    )
    assert "GH_TOKEN" not in manager.build_agent_request(chat, prompt="hi").extra_env


def _credentials_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, linked: bool = True
):
    manager, chat = _workspace_manager(tmp_path, monkeypatch)
    monkeypatch.setattr(
        manager, "_workspace_gws_profile", lambda workspace: "personal" if linked else ""
    )
    return manager, chat


def test_workspace_scope_grants_linked_gws_credentials_dir_writable_with_pinned_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.gws_auth import profile_config_dir

    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    mark_born_per_root(tmp_path, runtime, ["personal"])
    reset_reroot_cache()
    try:
        manager, chat = _credentials_request(tmp_path, monkeypatch)
        creds = profile_config_dir(manager._config, "personal")
        assert creds is not None
        creds.mkdir(parents=True)
        secret = creds.resolve() / "client_secret.json"
        refresh = creds.resolve() / "credentials.json"
        secret.write_text("{}", encoding="utf-8")
        refresh.write_text("{}", encoding="utf-8")

        request = manager.build_agent_request(chat, prompt="hi")

        assert request.agent_roots.count(str(creds.resolve())) == 1
        assert str(creds.resolve()) not in request.agent_read_only
        assert request.agent_read_only.count(str(secret)) == 1
        assert request.agent_read_only.count(str(refresh)) == 1

        secret.unlink()
        request = manager.build_agent_request(chat, prompt="hi")
        assert str(secret) not in request.agent_read_only
        assert str(refresh) in request.agent_read_only
    finally:
        reset_reroot_cache()


def test_workspace_scope_pins_gws_secrets_when_dir_already_under_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.gws_auth import profile_config_dir

    manager, chat = _credentials_request(tmp_path, monkeypatch)
    creds = profile_config_dir(manager._config, "personal")
    assert creds is not None
    creds.mkdir(parents=True)
    (creds / "client_secret.json").write_text("{}", encoding="utf-8")
    (creds / "credentials.json").write_text("{}", encoding="utf-8")
    agent_root = Path(manager._config.agent_root("personal")).resolve()
    assert creds.resolve().is_relative_to(agent_root)

    request = manager.build_agent_request(chat, prompt="hi")

    assert str(creds.resolve()) not in request.agent_roots
    assert str(creds.resolve() / "client_secret.json") in request.agent_read_only
    assert str(creds.resolve() / "credentials.json") in request.agent_read_only


def test_workspace_scope_skips_unlinked_gws_credentials_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.gws_auth import profile_config_dir

    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    mark_born_per_root(tmp_path, runtime, ["personal"])
    reset_reroot_cache()
    try:
        manager, chat = _credentials_request(tmp_path, monkeypatch, linked=False)
        creds = profile_config_dir(manager._config, "personal")
        assert creds is not None
        creds.mkdir(parents=True)
        unlinked = tmp_path / ".runtime" / "gws-unlinked" / "personal"
        unlinked.mkdir(parents=True)
        (creds / "client_secret.json").write_text("{}", encoding="utf-8")
        (unlinked / "client_secret.json").write_text("{}", encoding="utf-8")

        request = manager.build_agent_request(chat, prompt="hi")

        granted = {*request.agent_read_only, *request.agent_roots}
        assert str(creds.resolve()) not in granted
        assert str(unlinked.resolve()) not in granted
        assert str(creds.resolve() / "client_secret.json") not in granted
        assert str(unlinked.resolve() / "client_secret.json") not in granted
    finally:
        reset_reroot_cache()


def test_workspace_scope_skips_missing_gws_credentials_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.gws_auth import profile_config_dir

    manager, chat = _credentials_request(tmp_path, monkeypatch)
    creds = profile_config_dir(manager._config, "personal")
    assert creds is not None and not creds.exists()

    request = manager.build_agent_request(chat, prompt="hi")

    granted = {*request.agent_read_only, *request.agent_roots}
    assert str(creds.resolve()) not in granted
    assert str(creds.resolve() / "client_secret.json") not in granted


def test_workspace_scope_refuses_symlinked_gws_credentials_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform == "win32":
        return  # Creating a symlink needs a privilege Windows does not grant by default.
    from ciao.gws_auth import profile_config_dir

    manager, chat = _credentials_request(tmp_path, monkeypatch)
    creds = profile_config_dir(manager._config, "personal")
    assert creds is not None
    target = tmp_path / "elsewhere" / "ssh"
    target.mkdir(parents=True)
    (target / "client_secret.json").write_text("{}", encoding="utf-8")
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.symlink_to(target, target_is_directory=True)

    request = manager.build_agent_request(chat, prompt="hi")

    granted = {*request.agent_read_only, *request.agent_roots}
    assert str(creds) not in granted
    assert str(target.resolve()) not in granted
    assert str(target.resolve() / "client_secret.json") not in granted
    assert str(creds / "client_secret.json") not in granted


def test_workspace_scope_refuses_symlinked_secrets_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform == "win32":
        return  # Creating a symlink needs a privilege Windows does not grant by default.
    manager, chat = _credentials_request(tmp_path, monkeypatch)
    target = tmp_path / "elsewhere" / "secrets"
    (target / "gws-personal").mkdir(parents=True)
    (target / "gws-personal" / "client_secret.json").write_text("{}", encoding="utf-8")
    (tmp_path / "secrets").symlink_to(target, target_is_directory=True)

    request = manager.build_agent_request(chat, prompt="hi")

    granted = {*request.agent_read_only, *request.agent_roots}
    assert str(target.resolve()) not in granted
    assert str((target / "gws-personal").resolve()) not in granted
    assert str(tmp_path / "secrets" / "gws-personal") not in granted
    assert str(target.resolve() / "gws-personal" / "client_secret.json") not in granted
    assert str(tmp_path / "secrets" / "gws-personal" / "client_secret.json") not in granted


def _gh_token_spy(monkeypatch: pytest.MonkeyPatch, token: str = "tok") -> list[object]:
    calls: list[object] = []

    def spy(gh, env, **kwargs):
        calls.append(gh)
        return token

    monkeypatch.setattr("ciao.fs_sandbox.gh_token_for_sandbox", spy)
    return calls


def test_memory_pass_gets_no_gh_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, chat = _workspace_manager(tmp_path, monkeypatch)
    calls = _gh_token_spy(monkeypatch)
    monkeypatch.setattr("ciao.tool_path.resolve_tool", lambda cmd: "/bin/gh")
    monkeypatch.setattr("ciao.web.project_chats.is_memory_pass_chat", lambda chat, project: True)

    assert "GH_TOKEN" not in manager._build_extra_env(chat)
    assert calls == []


def test_unresolvable_gh_gets_no_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, chat = _workspace_manager(tmp_path, monkeypatch)
    calls = _gh_token_spy(monkeypatch)

    def broken(cmd):
        raise OSError("broken shim")

    monkeypatch.setattr("ciao.tool_path.resolve_tool", broken)

    assert "GH_TOKEN" not in manager._build_extra_env(chat)
    assert calls == []


def test_gh_inside_a_root_gets_no_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, chat = _workspace_manager(tmp_path, monkeypatch)
    calls = _gh_token_spy(monkeypatch)
    _, roots = manager._agent_fs_scope_for_chat(chat)
    planted = Path(roots[0]) / "gh"
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text("#!/bin/sh\n")
    monkeypatch.setattr("ciao.tool_path.resolve_tool", lambda cmd: str(planted))

    assert "GH_TOKEN" not in manager._build_extra_env(chat)
    assert calls == []


def test_warm_gh_token_runs_off_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    manager, chat = _workspace_manager(tmp_path, monkeypatch)
    monkeypatch.setattr("ciao.tool_path.resolve_tool", lambda cmd: "/bin/gh")
    calls = _gh_token_spy(monkeypatch)
    threaded: list[object] = []
    real_to_thread = asyncio.to_thread

    async def to_thread(func, *args, **kwargs):
        threaded.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr("ciao.web.project_chats.asyncio.to_thread", to_thread)

    asyncio.run(manager.warm_gh_token(chat))
    assert len(threaded) == 1
    assert calls == ["/bin/gh"]

    config = manager._config
    config.workspaces["personal"] = dataclasses.replace(
        config.workspaces["personal"], agent_fs_scope="machine"
    )
    asyncio.run(manager.warm_gh_token(chat))
    assert len(threaded) == 1


def _linked_account(manager: ProjectChatManager) -> Path:
    from ciao.gws_auth import profile_config_dir

    creds = profile_config_dir(manager._config, "personal")
    assert creds is not None
    creds.mkdir(parents=True)
    (creds / "credentials.json").write_text("{}", encoding="utf-8")
    return creds.resolve()


def test_pre_reroot_request_denies_secrets_and_regrants_linked_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, chat = _credentials_request(tmp_path, monkeypatch)
    creds = _linked_account(manager)
    (tmp_path / "secrets" / "gws-other").mkdir()

    request = manager.build_agent_request(chat, prompt="hi")

    assert request.agent_denied == (str((tmp_path / "secrets").resolve()),)
    assert request.agent_denied_except == (str(creds),)


def test_rerooted_request_has_no_denied_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    mark_born_per_root(tmp_path, runtime, ["personal"])
    reset_reroot_cache()
    try:
        manager, chat = _credentials_request(tmp_path, monkeypatch)
        _linked_account(manager)

        request = manager.build_agent_request(chat, prompt="hi")

        assert request.agent_denied == ()
        assert request.agent_denied_except == ()
    finally:
        reset_reroot_cache()


def test_code_mode_linked_repo_covering_secrets_denies_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    mark_born_per_root(tmp_path, runtime, ["personal"])
    reset_reroot_cache()
    try:
        manager, chat = _credentials_request(tmp_path, monkeypatch)
        creds = _linked_account(manager)
        project = manager._projects[chat.project_id]
        project.repo_path = str(tmp_path)
        chat.code_mode = True

        request = manager.build_agent_request(chat, prompt="hi")

        assert str(tmp_path.resolve()) in request.agent_roots
        assert request.agent_denied == (str((tmp_path / "secrets").resolve()),)
        assert request.agent_denied_except == (str(creds),)
    finally:
        reset_reroot_cache()


def test_machine_scope_request_has_no_denied_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, chat = _credentials_request(tmp_path, monkeypatch)
    _linked_account(manager)
    config = manager._config
    config.workspaces["personal"] = dataclasses.replace(
        config.workspaces["personal"], agent_fs_scope="machine"
    )

    request = manager.build_agent_request(chat, prompt="hi")

    assert request.agent_denied == ()
    assert request.agent_denied_except == ()


def test_unlinked_chat_denies_secrets_with_no_regrant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, chat = _credentials_request(tmp_path, monkeypatch, linked=False)
    (tmp_path / "secrets").mkdir()

    request = manager.build_agent_request(chat, prompt="hi")

    assert request.agent_denied == (str((tmp_path / "secrets").resolve()),)
    assert request.agent_denied_except == ()


def test_symlinked_secrets_inside_a_root_denies_the_resolved_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, chat = _credentials_request(tmp_path, monkeypatch, linked=False)
    target = tmp_path / "vault" / "real-secrets"
    target.mkdir(parents=True)
    (tmp_path / "secrets").symlink_to(target, target_is_directory=True)

    request = manager.build_agent_request(chat, prompt="hi")

    assert request.agent_denied == (str(target.resolve()),)
    assert request.agent_denied_except == ()


def test_symlinked_secrets_pointing_outside_every_root_is_not_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, chat = _credentials_request(tmp_path, monkeypatch, linked=False)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "secrets").symlink_to(outside, target_is_directory=True)

    request = manager.build_agent_request(chat, prompt="hi")

    assert request.agent_denied == ()


@pytest.fixture
def archive_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A per-root install on workspace scope, as a fresh macOS install is born (#2459)."""
    monkeypatch.setattr("ciao.fs_sandbox.gws_tool_read_roots", lambda path: [])
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    mark_born_per_root(tmp_path, runtime, ["personal"])
    reset_reroot_cache()
    manager = _make_manager(tmp_path)
    config = manager._config
    workspace = config.workspace("personal")
    assert workspace is not None
    config.workspaces["personal"] = dataclasses.replace(workspace, agent_fs_scope="workspace")
    try:
        yield manager
    finally:
        reset_reroot_cache()


_NEEDS_SYMLINK = pytest.mark.skipif(sys.platform == "win32", reason="needs a symlink privilege")


def _write_archive(config, chat_id: str = "src-1") -> Path:
    archive = config.logs_root / "Chats" / chat_id / "claude" / "2026-11-01T10-00-00-s.md"
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text("transcript", encoding="utf-8")
    return archive


def _pass_helper(archive: Path) -> dict:
    return {
        "kind": "memory_pass",
        "source_chat_id": "src-1",
        "archive_path": str(archive),
        "archive_policy": "when_clean",
    }


def _memory_pass_chat(manager: ProjectChatManager, helper: dict) -> ChatInfo:
    project = manager._memory_pass.ensure_project("personal")
    return manager.create_chat(project.project_id, provider="claude", helper=helper)


def test_a_memory_pass_chat_may_read_exactly_its_source_archive(archive_install) -> None:
    manager = archive_install
    config = manager._config
    archive = _write_archive(config)
    chat = _memory_pass_chat(manager, _pass_helper(archive))

    request = manager.build_agent_request(chat, prompt="hi")

    real = archive.resolve()
    assert str(real) in request.agent_read_only
    assert not any(real.is_relative_to(Path(root)) for root in request.agent_roots)
    logs = config.logs_root.resolve()
    granted = {Path(p) for p in (*request.agent_read_only, *request.agent_roots)}
    assert not granted & {logs, logs / "Chats", logs / "Chats" / "src-1"}
    assert not any(
        Path(p) != real and real.is_relative_to(Path(p)) for p in request.agent_read_only
    )


def test_a_relative_archive_path_resolves_against_the_workspace_root(archive_install) -> None:
    manager = archive_install
    config = manager._config
    archive = _write_archive(config)
    helper = _pass_helper(archive.relative_to(config.workspace_root))
    chat = _memory_pass_chat(manager, helper)

    request = manager.build_agent_request(chat, prompt="hi")

    assert str(archive.resolve()) in request.agent_read_only


def test_an_ordinary_chat_gets_no_archive_grant(archive_install) -> None:
    manager = archive_install
    config = manager._config
    _write_archive(config)
    project = manager.create_project("scoped", workspace="personal")
    chat = manager.create_chat(project.project_id, provider="claude")

    request = manager.build_agent_request(chat, prompt="hi")

    assert not any(
        Path(p).is_relative_to(config.logs_root.resolve()) for p in request.agent_read_only
    )


def test_a_memory_pass_in_machine_scope_gets_no_archive_grant(archive_install) -> None:
    manager = archive_install
    config = manager._config
    archive = _write_archive(config)
    chat = _memory_pass_chat(manager, _pass_helper(archive))
    workspace = config.workspace("personal")
    assert workspace is not None
    config.workspaces["personal"] = dataclasses.replace(workspace, agent_fs_scope="machine")

    assert manager.build_agent_request(chat, prompt="hi").agent_read_only == ()


@pytest.mark.parametrize(
    "case",
    [
        "outside_logs",
        "another_chat",
        pytest.param("symlink", marks=_NEEDS_SYMLINK),
        pytest.param("middle_symlink", marks=_NEEDS_SYMLINK),
        pytest.param("symlinked_source_folder", marks=_NEEDS_SYMLINK),
        "hardlink",
        "traversal",
        "missing",
        "directory",
    ],
)
def test_a_tampered_memory_pass_archive_path_is_not_granted(
    archive_install, tmp_path: Path, case: str
) -> None:
    manager = archive_install
    config = manager._config
    archive = _write_archive(config)
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    other = _write_archive(config, chat_id="other")
    chats = config.logs_root / "Chats"
    forbidden = {secret.resolve(), archive.resolve(), other.resolve()}
    helper = _pass_helper(archive)
    if case == "outside_logs":
        helper["archive_path"] = str(secret)
    elif case == "another_chat":
        helper["archive_path"] = str(other)
    elif case == "symlink":
        link = archive.with_name("link.md")
        link.symlink_to(secret)
        helper["archive_path"] = str(link)
    elif case == "middle_symlink":
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "s.md"
        target.write_text("outside", encoding="utf-8")
        (archive.parent / "linkdir").symlink_to(outside, target_is_directory=True)
        helper["archive_path"] = str(archive.parent / "linkdir" / "s.md")
        forbidden.add(target.resolve())
    elif case == "symlinked_source_folder":
        (chats / "linked").symlink_to(chats / "src-1", target_is_directory=True)
        helper["source_chat_id"] = "linked"
    elif case == "hardlink":
        alias = archive.with_name("h.md")
        os.link(secret, alias)
        helper["archive_path"] = str(alias)
        forbidden.add(alias.resolve())
    elif case == "traversal":
        helper["source_chat_id"] = "../src-1"
    elif case == "missing":
        helper["archive_path"] = str(archive.with_name("gone.md"))
    elif case == "directory":
        helper["archive_path"] = str(archive.parent)
    chat = _memory_pass_chat(manager, helper)

    request = manager.build_agent_request(chat, prompt="hi")

    granted = {Path(p) for p in request.agent_read_only}
    assert not granted & forbidden
    assert not any(p.is_relative_to(config.logs_root.resolve()) for p in granted)


def test_a_task_completion_pass_gets_no_archive_grant(archive_install) -> None:
    manager = archive_install
    config = manager._config
    archive = _write_archive(config)
    resolution = "Shipped the fix."
    project = manager._memory_pass.ensure_project("personal")
    chat = manager.create_chat(
        project.project_id,
        provider="claude",
        helper={
            "kind": "memory_pass",
            "source_kind": "task_completion",
            "task_path": "work/tasks/t.md",
            "completion_id": "0" * 32,
            "resolution": resolution,
            "resolution_sha256": hashlib.sha256(resolution.encode("utf-8")).hexdigest(),
            "archive_path": str(archive),
            "archive_policy": "when_clean",
        },
    )

    request = manager.build_agent_request(chat, prompt="hi")

    assert not any(
        Path(p).is_relative_to(config.logs_root.resolve()) for p in request.agent_read_only
    )


def test_a_hardlink_in_the_source_folder_is_not_granted(archive_install, tmp_path: Path) -> None:
    # A second name for a file another process can read is not a transcript
    # archive: st_nlink must be 1, as the Claude task-output check requires (#2459).
    manager = archive_install
    config = manager._config
    archive = _write_archive(config)
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    alias = archive.parent / "h.md"
    os.link(secret, alias)
    chat = _memory_pass_chat(manager, _pass_helper(alias))

    request = manager.build_agent_request(chat, prompt="hi")

    assert str(alias.resolve()) not in request.agent_read_only
    assert str(secret.resolve()) not in request.agent_read_only
