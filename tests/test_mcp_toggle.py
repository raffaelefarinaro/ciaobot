from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao import agent_paths
from ciao.web.mcp_toggle import (
    BUSY_MESSAGE,
    UNREADABLE_MESSAGE,
    McpToggleError,
    McpToggleResult,
    _claude_config_lock,
    set_mcp_server_enabled,
)

_key = agent_paths.claude_config_project_key


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "home", tmp_path / "ws"


@pytest.fixture(autouse=True)
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CONFIG_PATH",
        "OPENCODE_CONFIG",
        "OPENCODE_CONFIG_DIR",
        "OPENCODE_CONFIG_CONTENT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    home, root = _paths(tmp_path)
    home.mkdir()
    root.mkdir()
    monkeypatch.setattr("ciao.web.mcp_toggle._claude_managed_names", lambda _root: set())


def _write_claude(home: Path, data: object) -> Path:
    config = home / ".claude.json"
    config.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return config


def _claude_list(config: Path, root: Path) -> list[str]:
    return json.loads(config.read_text(encoding="utf-8"))["projects"][_key(root)]["disabledMcpServers"]


def test_claude_turn_off_appends_and_keeps_other_keys(tmp_path: Path) -> None:
    home, root = _paths(tmp_path)
    config = home / ".claude.json"
    original = json.dumps(
        {
            "numStartups": 3,
            "big": 12345678901234567890,
            "uni": "ü€ 😀",
            "projects": {_key(root): {"allowedTools": [], "disabledMcpServers": ["a"]}},
        },
        ensure_ascii=False,
    )
    config.write_text(original, encoding="utf-8")

    result = set_mcp_server_enabled(provider="claude", server="notion", agent_root=root, enabled=False)

    assert result == McpToggleResult("claude", "notion", False, True, _key(root), None)
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["projects"][_key(root)]["disabledMcpServers"] == ["a", "notion"]
    assert data["projects"][_key(root)]["allowedTools"] == []
    assert data["big"] == 12345678901234567890
    assert data["uni"] == "ü€ 😀"
    assert data["numStartups"] == 3
    backup = home / ".claude.json.ciao-bak"
    assert backup.read_text(encoding="utf-8") == original
    if os.name != "nt":
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert not (home / ".claude.json.lock").exists()

    again = set_mcp_server_enabled(provider="claude", server="notion", agent_root=root, enabled=False)
    assert again.changed is False


def test_backup_is_made_once(tmp_path: Path) -> None:
    home, root = _paths(tmp_path)
    config = _write_claude(home, {"projects": {_key(root): {"disabledMcpServers": []}}})
    original = config.read_bytes()

    set_mcp_server_enabled(provider="claude", server="x", agent_root=root, enabled=False)
    set_mcp_server_enabled(provider="claude", server="y", agent_root=root, enabled=False)

    assert (home / ".claude.json.ciao-bak").read_bytes() == original


def test_claude_turn_on_removes_the_name(tmp_path: Path) -> None:
    home, root = _paths(tmp_path)
    config = _write_claude(home, {"projects": {_key(root): {"disabledMcpServers": ["notion", "a"]}}})

    result = set_mcp_server_enabled(provider="claude", server="notion", agent_root=root, enabled=True)

    assert result.changed is True
    assert result.warning is None
    assert _claude_list(config, root) == ["a"]


def test_claude_creates_the_project_entry(tmp_path: Path) -> None:
    home, root = _paths(tmp_path)
    config = _write_claude(home, {"projects": {}})

    set_mcp_server_enabled(provider="claude", server="notion", agent_root=root, enabled=False)

    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["projects"] == {_key(root): {"disabledMcpServers": ["notion"]}}


def test_claude_without_a_settings_file_is_refused(tmp_path: Path) -> None:
    home, root = _paths(tmp_path)

    with pytest.raises(McpToggleError) as caught:
        set_mcp_server_enabled(provider="claude", server="notion", agent_root=root, enabled=False)

    assert caught.value.reason == "unreadable_config"
    assert caught.value.status == 409
    assert caught.value.message == UNREADABLE_MESSAGE
    assert not (home / ".claude.json").exists()
    assert not (home / ".claude.json.ciao-bak").exists()


def test_claude_honours_claude_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, root = _paths(tmp_path)
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    (cfg / ".claude.json").write_text(json.dumps({"projects": {}}), encoding="utf-8")

    set_mcp_server_enabled(provider="claude", server="notion", agent_root=root, enabled=False)

    assert _claude_list(cfg / ".claude.json", root) == ["notion"]
    assert not (home / ".claude.json").exists()


def test_claude_managed_server_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, root = _paths(tmp_path)
    config = _write_claude(home, {"projects": {}})
    original = config.read_bytes()
    monkeypatch.setattr("ciao.web.mcp_toggle._claude_managed_names", lambda _root: {"corp"})

    with pytest.raises(McpToggleError) as caught:
        set_mcp_server_enabled(provider="claude", server="corp", agent_root=root, enabled=False)

    assert caught.value.reason == "managed"
    assert caught.value.status == 403
    assert config.read_bytes() == original


def test_claude_busy_lock_is_never_stolen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, root = _paths(tmp_path)
    config = _write_claude(home, {"projects": {}})
    original = config.read_bytes()
    monkeypatch.setattr("ciao.web.mcp_toggle.CLAUDE_LOCK_WAIT_S", 0.1)
    lock = home / ".claude.json.lock"
    lock.mkdir()
    os.utime(lock, (time.time() - 3600, time.time() - 3600))
    inode = lock.stat().st_ino

    with pytest.raises(McpToggleError) as caught:
        set_mcp_server_enabled(provider="claude", server="notion", agent_root=root, enabled=False)

    assert caught.value.reason == "config_busy"
    assert caught.value.message == BUSY_MESSAGE
    assert lock.exists()
    assert lock.stat().st_ino == inode
    assert config.read_bytes() == original


def test_two_waiters_serialise(tmp_path: Path) -> None:
    home, root = _paths(tmp_path)
    config = _write_claude(home, {"projects": {}})
    errors: list[BaseException] = []

    def turn_off(name: str) -> None:
        try:
            set_mcp_server_enabled(provider="claude", server=name, agent_root=root, enabled=False)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=turn_off, args=(name,)) for name in ("x", "y")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert sorted(_claude_list(config, root)) == ["x", "y"]


def test_release_leaves_a_lock_it_did_not_create(tmp_path: Path) -> None:
    home, _root = _paths(tmp_path)
    config = home / ".claude.json"
    config.write_text("{}", encoding="utf-8")
    lock = home / ".claude.json.lock"

    with _claude_config_lock(config):
        # Keep the old directory allocated (renamed aside) so the new one cannot reuse its inode.
        os.rename(lock, home / "aside")
        os.mkdir(lock)

    assert lock.exists()


def test_write_failure_is_unreadable_and_leaves_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, root = _paths(tmp_path)
    config = _write_claude(home, {"projects": {}})
    original = config.read_bytes()

    def refuse(_src: object, _dst: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr("ciao.web.mcp_toggle.os.replace", refuse)

    with pytest.raises(McpToggleError) as caught:
        set_mcp_server_enabled(provider="claude", server="notion", agent_root=root, enabled=False)

    assert caught.value.reason == "unreadable_config"
    assert caught.value.message == UNREADABLE_MESSAGE
    assert config.read_bytes() == original
    assert not list(home.glob(".claude.json.*.tmp"))
    assert not (home / ".claude.json.lock").exists()


def test_claude_top_level_list_warns(tmp_path: Path) -> None:
    home, root = _paths(tmp_path)
    _write_claude(home, {"disabledMcpServers": ["notion"], "projects": {}})

    result = set_mcp_server_enabled(provider="claude", server="notion", agent_root=root, enabled=True)

    assert result.changed is False
    assert result.warning is not None
    assert result.warning.startswith("Claude Code's global disabled list")


@pytest.mark.parametrize("server", ["", " notion", "a\nb", "x" * 129])
def test_invalid_server_names_are_refused(tmp_path: Path, server: str) -> None:
    _home, root = _paths(tmp_path)
    with pytest.raises(McpToggleError) as caught:
        set_mcp_server_enabled(provider="claude", server=server, agent_root=root, enabled=False)
    assert caught.value.reason == "invalid_server"
    with pytest.raises(McpToggleError) as caught:
        set_mcp_server_enabled(provider="codex", server="notion", agent_root=root, enabled=False)
    assert caught.value.reason == "invalid_provider"
    assert caught.value.status == 400


def test_opencode_v2_turn_off_sets_disabled(tmp_path: Path) -> None:
    _home, root = _paths(tmp_path)
    path = root / "opencode.json"
    path.write_text(
        json.dumps({"mcp": {"servers": {"linear": {"type": "remote", "url": "https://x.invalid/a//b", "enabled": True}}}}),
        encoding="utf-8",
    )

    result = set_mcp_server_enabled(provider="opencode", server="linear", agent_root=root, enabled=False)

    assert result.changed is True
    assert result.applies_to == str(path)
    assert result.warning is None
    entry = json.loads(path.read_text(encoding="utf-8"))["mcp"]["servers"]["linear"]
    assert entry == {"type": "remote", "url": "https://x.invalid/a//b", "disabled": True}

    result = set_mcp_server_enabled(provider="opencode", server="linear", agent_root=root, enabled=True)

    assert result.changed is True
    entry = json.loads(path.read_text(encoding="utf-8"))["mcp"]["servers"]["linear"]
    assert entry == {"type": "remote", "url": "https://x.invalid/a//b"}


def test_opencode_legacy_shape_uses_enabled(tmp_path: Path) -> None:
    _home, root = _paths(tmp_path)
    path = root / "opencode.json"
    path.write_text(
        json.dumps({"mcp": {"linear": {"type": "local", "command": ["x"], "disabled": True}}}),
        encoding="utf-8",
    )

    set_mcp_server_enabled(provider="opencode", server="linear", agent_root=root, enabled=True)

    entry = json.loads(path.read_text(encoding="utf-8"))["mcp"]["linear"]
    assert entry == {"type": "local", "command": ["x"], "enabled": True}


@pytest.mark.parametrize(
    "raw",
    [
        '{ // mine\n "mcp": {"servers": {"linear": {"type":"remote","url":"https://x.invalid"}}}}',
        '{"mcp": {"servers": {"linear": {"type":"remote","url":"https://x.invalid"},}}}',
    ],
)
def test_opencode_comments_or_trailing_commas_are_refused(tmp_path: Path, raw: str) -> None:
    _home, root = _paths(tmp_path)
    path = root / "opencode.jsonc"
    path.write_text(raw, encoding="utf-8")

    with pytest.raises(McpToggleError) as caught:
        set_mcp_server_enabled(provider="opencode", server="linear", agent_root=root, enabled=False)
    assert caught.value.reason == "config_has_comments"
    assert path.read_text(encoding="utf-8") == raw

    result = set_mcp_server_enabled(provider="opencode", server="linear", agent_root=root, enabled=True)
    assert result.changed is False
    assert path.read_text(encoding="utf-8") == raw


def test_opencode_env_config_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _home, root = _paths(tmp_path)
    monkeypatch.setenv(
        "OPENCODE_CONFIG_CONTENT", '{"mcp": {"servers": {"linear": {"type":"local","command":["x"]}}}}'
    )

    with pytest.raises(McpToggleError) as caught:
        set_mcp_server_enabled(provider="opencode", server="linear", agent_root=root, enabled=False)
    assert caught.value.reason == "env_config"
    assert caught.value.status == 409


def test_opencode_unknown_server_is_not_configured(tmp_path: Path) -> None:
    _home, root = _paths(tmp_path)

    with pytest.raises(McpToggleError) as caught:
        set_mcp_server_enabled(provider="opencode", server="linear", agent_root=root, enabled=False)
    assert caught.value.reason == "not_configured"
    assert caught.value.status == 404


def test_opencode_project_file_wins_over_user_file(tmp_path: Path) -> None:
    _home, root = _paths(tmp_path)
    definition = {"mcp": {"servers": {"linear": {"type": "remote", "url": "https://x.invalid"}}}}
    user_dir = tmp_path / "xdg" / "opencode"
    user_dir.mkdir(parents=True)
    user_file = user_dir / "opencode.json"
    user_file.write_text(json.dumps(definition), encoding="utf-8")
    project_file = root / "opencode.json"
    project_file.write_text(json.dumps(definition), encoding="utf-8")
    user_before = user_file.read_bytes()

    result = set_mcp_server_enabled(provider="opencode", server="linear", agent_root=root, enabled=False)

    assert result.applies_to == str(project_file)
    assert result.warning is None
    assert user_file.read_bytes() == user_before
    assert json.loads(project_file.read_text(encoding="utf-8"))["mcp"]["servers"]["linear"]["disabled"] is True


@pytest.mark.asyncio
async def test_reload_workspace_providers_never_touches_a_running_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_chat_actions_runtime import make_manager

    manager = make_manager(tmp_path)
    project = manager.create_project("P", workspace="work")
    other = manager.create_project("Q", workspace="other")
    a = manager.create_chat(project.project_id, title="a", provider="claude")
    b = manager.create_chat(project.project_id, title="b", provider="claude")
    manager.create_chat(project.project_id, title="d", provider="claude")
    c = manager.create_chat(project.project_id, title="c", provider="opencode")
    e = manager.create_chat(other.project_id, title="e", provider="claude")
    for chat in (a, b, c, e):
        manager._providers[chat.chat_id] = SimpleNamespace()
    monkeypatch.setattr(manager, "active_chat_ids", lambda: [b.chat_id])
    calls: list[str] = []

    async def reload(chat_id: str) -> bool:
        calls.append(chat_id)
        return True

    monkeypatch.setattr(manager, "reload_provider", reload)

    result = await manager.reload_workspace_providers("work", "claude")
    assert result == {"reloaded": [a.chat_id], "pending": [b.chat_id]}
    assert calls == [a.chat_id]

    async def refuse(chat_id: str) -> bool:
        calls.append(chat_id)
        return False

    monkeypatch.setattr(manager, "reload_provider", refuse)
    result = await manager.reload_workspace_providers("work", "claude")
    assert result == {"reloaded": [], "pending": sorted([a.chat_id, b.chat_id])}


def test_forget_mcp_auth_decline(tmp_path: Path) -> None:
    from ciao.chat_actions import action_key
    from tests.test_chat_actions_runtime import make_manager

    manager = make_manager(tmp_path)
    key = action_key("mcp_auth", {"server": "notion"})
    manager.declined_mcp_auth.add(("work", "claude", key))
    manager.declined_mcp_auth.add(("work", "claude", action_key("mcp_auth", {"server": "linear"})))

    manager.forget_mcp_auth_decline("work", "claude", "notion")

    assert manager.declined_mcp_auth == {("work", "claude", action_key("mcp_auth", {"server": "linear"}))}


def _turn_off_into(name: str, root: Path, errors: list[BaseException]) -> None:
    try:
        set_mcp_server_enabled(provider="claude", server=name, agent_root=root, enabled=False)
    except BaseException as exc:
        errors.append(exc)


@pytest.mark.skipif(os.name == "nt", reason="creating symlinks needs a privilege on Windows")
def test_symlinked_settings_file_uses_the_real_files_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, root = _paths(tmp_path)
    store = tmp_path / "store"
    store.mkdir()
    real = store / "real.json"
    real.write_text(json.dumps({"projects": {}}), encoding="utf-8")
    os.chmod(real, 0o640)
    config = home / ".claude.json"
    config.symlink_to(real)
    monkeypatch.setattr("ciao.web.mcp_toggle.CLAUDE_LOCK_WAIT_S", 0.1)
    held = store / "real.json.lock"
    os.mkdir(held)
    try:
        with pytest.raises(McpToggleError) as caught:
            set_mcp_server_enabled(provider="claude", server="notion", agent_root=root, enabled=False)
        assert caught.value.reason == "config_busy"
    finally:
        os.rmdir(held)

    result = set_mcp_server_enabled(provider="claude", server="notion", agent_root=root, enabled=False)

    assert result.changed is True
    assert config.is_symlink()
    assert _claude_list(real, root) == ["notion"]
    assert (store / "real.json.ciao-bak").exists()
    assert stat.S_IMODE(real.stat().st_mode) == 0o640


@pytest.mark.skipif(os.name == "nt", reason="creating symlinks needs a privilege on Windows")
def test_backup_never_follows_a_dangling_link(tmp_path: Path) -> None:
    home, root = _paths(tmp_path)
    config = _write_claude(home, {"projects": {}})
    victim = tmp_path / "victim"
    (home / ".claude.json.ciao-bak").symlink_to(victim)

    set_mcp_server_enabled(provider="claude", server="notion", agent_root=root, enabled=False)

    assert not victim.exists()
    assert _claude_list(config, root) == ["notion"]


def test_lock_is_removed_when_it_cannot_be_inspected_after_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, _root = _paths(tmp_path)
    config = home / ".claude.json"
    config.write_text("{}", encoding="utf-8")
    lock = home / ".claude.json.lock"
    real_stat = os.stat

    def stat_or_refuse(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if Path(str(path)) == lock:
            raise PermissionError("denied")
        return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("ciao.web.mcp_toggle.os.stat", stat_or_refuse)

    with pytest.raises(McpToggleError) as caught:
        with _claude_config_lock(config):
            pass
    assert caught.value.reason == "unreadable_config"
    assert not os.path.lexists(lock)


def test_text_that_cannot_be_encoded_is_unreadable(tmp_path: Path) -> None:
    home, root = _paths(tmp_path)
    config = home / ".claude.json"
    config.write_text('{"projects": {}, "uni": "\\ud800"}', encoding="utf-8")
    original = config.read_bytes()

    with pytest.raises(McpToggleError) as caught:
        set_mcp_server_enabled(provider="claude", server="notion", agent_root=root, enabled=False)

    assert caught.value.reason == "unreadable_config"
    assert caught.value.message == UNREADABLE_MESSAGE
    assert config.read_bytes() == original
    assert not list(home.glob(".claude.json.*.tmp"))


def test_config_that_does_not_round_trip_is_refused_before_any_backup(tmp_path: Path) -> None:
    home, root = _paths(tmp_path)
    config = home / ".claude.json"
    config.write_text('{"projects": {}, "x": NaN}', encoding="utf-8")
    original = config.read_bytes()

    with pytest.raises(McpToggleError) as caught:
        set_mcp_server_enabled(provider="claude", server="notion", agent_root=root, enabled=False)

    assert caught.value.reason == "unreadable_config"
    assert config.read_bytes() == original
    assert not (home / ".claude.json.ciao-bak").exists()


def test_a_held_lock_makes_a_writer_wait(tmp_path: Path) -> None:
    home, root = _paths(tmp_path)
    config = _write_claude(home, {"projects": {}})
    errors: list[BaseException] = []

    with _claude_config_lock(config):
        thread = threading.Thread(target=_turn_off_into, args=("notion", root, errors))
        thread.start()
        time.sleep(0.2)
        assert thread.is_alive()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert errors == []
    assert _claude_list(config, root) == ["notion"]
    assert not (home / ".claude.json.lock").exists()


def test_two_writers_released_together_both_land(tmp_path: Path) -> None:
    home, root = _paths(tmp_path)
    config = _write_claude(home, {"projects": {}})
    original = config.read_bytes()
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def run(name: str) -> None:
        try:
            barrier.wait(timeout=10)
            set_mcp_server_enabled(provider="claude", server=name, agent_root=root, enabled=False)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(name,)) for name in ("x", "y")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert sorted(_claude_list(config, root)) == ["x", "y"]
    assert (home / ".claude.json.ciao-bak").read_bytes() == original


def _refuse(*_args: object, **_kwargs: object) -> None:
    raise PermissionError("denied")


@pytest.mark.parametrize(
    "case", ["lock_mkdir", "read_error", "invalid_json", "projects_not_dict", "entry_not_dict", "opencode_replace"]
)
def test_os_and_shape_failures_are_unreadable_and_leave_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    home, root = _paths(tmp_path)
    provider, server, target = "claude", "notion", home / ".claude.json"
    if case == "invalid_json":
        target.write_text("{ not json", encoding="utf-8")
    elif case == "projects_not_dict":
        _write_claude(home, {"projects": []})
    elif case == "entry_not_dict":
        _write_claude(home, {"projects": {_key(root): []}})
    elif case == "opencode_replace":
        provider, server = "opencode", "linear"
        target = root / "opencode.json"
        target.write_text(
            json.dumps({"mcp": {"servers": {"linear": {"type": "remote", "url": "https://x.invalid"}}}}),
            encoding="utf-8",
        )
        monkeypatch.setattr("ciao.web.mcp_toggle.os.replace", _refuse)
    else:
        _write_claude(home, {"projects": {}})
    if case == "lock_mkdir":
        monkeypatch.setattr("ciao.web.mcp_toggle.os.mkdir", _refuse)
    if case == "read_error":
        real_read_text = Path.read_text

        def read_text_or_refuse(self: Path, *args: object, **kwargs: object) -> str:
            if self.name == ".claude.json":
                raise PermissionError("denied")
            return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "read_text", read_text_or_refuse)
    original = target.read_bytes()

    with pytest.raises(McpToggleError) as caught:
        set_mcp_server_enabled(provider=provider, server=server, agent_root=root, enabled=False)

    assert caught.value.reason == "unreadable_config"
    assert caught.value.message == UNREADABLE_MESSAGE
    assert target.read_bytes() == original
    assert not list(home.glob(".claude.json.*.tmp"))
    assert not list(root.glob(".opencode.json.*.tmp"))


def test_opencode_url_with_double_slash_survives_a_toggle(tmp_path: Path) -> None:
    _home, root = _paths(tmp_path)
    path = root / "opencode.jsonc"
    path.write_text(
        json.dumps({"mcp": {"servers": {"linear": {"type": "remote", "url": "https://x.invalid/a//b"}}}}),
        encoding="utf-8",
    )

    result = set_mcp_server_enabled(provider="opencode", server="linear", agent_root=root, enabled=False)

    assert result.changed is True
    entry = json.loads(path.read_text(encoding="utf-8"))["mcp"]["servers"]["linear"]
    assert entry == {"type": "remote", "url": "https://x.invalid/a//b", "disabled": True}


@pytest.mark.asyncio
async def test_reload_skips_a_chat_that_starts_a_turn_mid_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_chat_actions_runtime import make_manager

    manager = make_manager(tmp_path)
    project = manager.create_project("P", workspace="work")
    first, second = sorted(
        manager.create_chat(project.project_id, title=title, provider="claude").chat_id for title in ("x", "y")
    )
    for chat_id in (first, second):
        manager._providers[chat_id] = SimpleNamespace()
    busy: list[str] = []
    monkeypatch.setattr(manager, "active_chat_ids", lambda: list(busy))
    calls: list[str] = []

    async def reload(chat_id: str) -> bool:
        calls.append(chat_id)
        busy.append(second)  # the other chat starts a turn while this reload awaits
        return True

    monkeypatch.setattr(manager, "reload_provider", reload)

    result = await manager.reload_workspace_providers("work", "claude")

    assert calls == [first]
    assert result == {"reloaded": [first], "pending": [second]}
