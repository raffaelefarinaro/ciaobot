"""ciao.os_support.shell_hints: one PATH hint per OS, one implementation.

The Windows branch is exercised by patching ``sys.platform`` rather than by
running on Windows: the helper only does string work, so the platform string is
the whole difference.
"""

from __future__ import annotations

import sys
import os
import subprocess
from pathlib import Path

import pytest

from ciao.os_support.shell_hints import path_hint, path_hint_note


class _FakeRegistry:
    HKEY_LOCAL_MACHINE = "machine"
    HKEY_CURRENT_USER = "user"
    REG_SZ = 1
    REG_EXPAND_SZ = 2
    KEY_SET_VALUE = 2

    def __init__(self, values: dict[str, object], *, open_error: str = "") -> None:
        self.values = values
        self.open_error = open_error
        self.write_error = False
        self.write_open_error = False
        self.writes = 0

    def OpenKey(self, hive: str, subkey: str, *args: object) -> "_FakeKey":
        assert subkey == {
            "machine": r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            "user": "Environment",
        }[hive]
        if args and self.write_open_error:
            raise PermissionError("write access denied")
        if hive == self.open_error:
            raise FileNotFoundError("key missing")
        value = self.values[hive]
        if isinstance(value, PermissionError):
            raise value
        return _FakeKey(value)

    def QueryValueEx(self, key: "_FakeKey", name: str) -> object:
        assert name == "Path"
        if isinstance(key.value, Exception):
            raise key.value
        return key.value

    def SetValueEx(self, key: "_FakeKey", name: str, reserved: int, kind: int, value: str) -> None:
        self.writes += 1
        if self.write_error:
            raise OSError("uncertain write")
        self.values["user"] = (value, kind)


class _FakeKey:
    def __init__(self, value: object) -> None:
        self.value = value

    def __enter__(self) -> "_FakeKey":
        return self

    def __exit__(self, *args: object) -> None:
        pass


def test_windows_path_machine_read_denied_user_readable_refuses() -> None:
    from ciao.os_support.shell_hints import _read_windows_path_sources

    registry = _FakeRegistry({"machine": PermissionError(), "user": (r"C:\\Tools", 1)})
    with pytest.raises(PermissionError):
        _read_windows_path_sources(registry)


def test_windows_path_sources_preserve_presence_values_and_types() -> None:
    from ciao.os_support.shell_hints import _read_windows_path_sources

    registry = _FakeRegistry({"machine": FileNotFoundError(), "user": (r"%USERPROFILE%\\bin", 2)})
    assert _read_windows_path_sources(registry) == (
        (False, "", None), (True, r"%USERPROFILE%\\bin", 2)
    )
    registry = _FakeRegistry({"machine": ("", 1), "user": FileNotFoundError()})
    assert _read_windows_path_sources(registry) == ((True, "", 1), (False, "", None))


@pytest.mark.parametrize("source", ["machine", "user"])
def test_windows_path_sources_refuse_missing_keys(source: str) -> None:
    from ciao.os_support.shell_hints import _read_windows_path_sources

    registry = _FakeRegistry({"machine": ("system", 1), "user": ("user", 1)}, open_error=source)
    with pytest.raises(FileNotFoundError):
        _read_windows_path_sources(registry)


@pytest.mark.parametrize("bad", [(123, 1), ("text", 3), PermissionError()])
@pytest.mark.parametrize("source", ["machine", "user"])
def test_windows_path_sources_refuse_invalid_or_unreadable_values(bad: object, source: str) -> None:
    from ciao.os_support.shell_hints import _read_windows_path_sources

    values: dict[str, object] = {"machine": ("system", 1), "user": ("user", 2)}
    values[source] = bad
    with pytest.raises((ValueError, PermissionError)):
        _read_windows_path_sources(_FakeRegistry(values))


def _windows_fixture(tmp_path: Path) -> tuple[Path, _FakeRegistry, dict[str, str]]:
    exe = tmp_path / "new" / "ciao.exe"
    exe.parent.mkdir()
    exe.write_text("executable")
    exe.chmod(0o700)
    return exe, _FakeRegistry({"machine": ("", 1), "user": ("", 1)}), {"PATHEXT": ".CMD;.EXE"}


def _fake_resolve(command: str, path: str, pathext: str) -> str | None:
    for directory in path.split(";"):
        if not directory:
            continue
        for suffix in pathext.split(";"):
            candidate = Path(directory.strip('"')) / (command + suffix.lower())
            if candidate.is_file():
                return str(candidate)
    return None


def _preview(exe: Path, registry: _FakeRegistry, env: dict[str, str]) -> dict[str, str]:
    from ciao.os_support.shell_hints import _preview_windows_path_change
    return _preview_windows_path_change("ciao", str(exe), registry, env, _fake_resolve, "account")


def _apply(exe: Path, registry: _FakeRegistry, env: dict[str, str], revision: str, notify: object = None) -> dict[str, str]:
    from ciao.os_support.shell_hints import _apply_windows_path_change
    return _apply_windows_path_change("ciao", str(exe), revision, registry, env, _fake_resolve, "account", notify or (lambda: None))


def test_windows_path_append_preserves_raw_kind_and_deduplicates_expanded_entry(tmp_path: Path) -> None:
    exe, registry, env = _windows_fixture(tmp_path)
    env["USERPROFILE"] = str(tmp_path / "old")
    registry.values["user"] = (str(tmp_path / "%USERPROFILE%" / "bin"), 1)
    preview = _preview(exe, registry, env)
    assert preview["status"] == "ready"
    assert _apply(exe, registry, env, preview["revision"])["status"] == "applied"
    assert registry.values["user"] == (str(tmp_path / "%USERPROFILE%" / "bin") + ";" + str(exe.parent), 1)
    assert _preview(exe, registry, env)["status"] == "already_configured"


def test_windows_expand_kind_preserves_raw_and_expands_references(tmp_path: Path) -> None:
    from ciao.os_support.shell_hints import _windows_state
    exe, registry, env = _windows_fixture(tmp_path)
    registry.values["machine"] = ("%ROOT%/system", 2)
    registry.values["user"] = ("%ROOT%/user", 2)
    env["ROOT"] = str(tmp_path)
    state, status, _ = _windows_state("ciao", str(exe), registry, env, _fake_resolve, "account")
    assert status == "ready" and state["windows"]["expansion_inputs"] == {"ROOT": str(tmp_path)}
    revision = _preview(exe, registry, env)["revision"]
    assert _apply(exe, registry, env, revision)["status"] == "applied"
    assert registry.values["user"] == ("%ROOT%/user;" + str(exe.parent), 2)


def test_windows_sz_percent_values_remain_literal_and_candidate_is_not_expanded(tmp_path: Path) -> None:
    from ciao.os_support.shell_hints import _windows_state
    exe, registry, env = _windows_fixture(tmp_path)
    literal_dir = tmp_path / "%MISSING%"
    literal_dir.mkdir()
    exe = literal_dir / "ciao.exe"
    exe.write_text("fixture")
    exe.chmod(0o700)
    registry.values["machine"] = (str(tmp_path / "%MACHINE_MISSING%"), 1)
    registry.values["user"] = (str(tmp_path / "%USER_MISSING%"), 1)
    state, status, _ = _windows_state("ciao", str(exe), registry, env, _fake_resolve, "account")
    assert status == "ready" and state["windows"]["expansion_inputs"] == {}
    revision = _preview(exe, registry, env)["revision"]
    assert _apply(exe, registry, env, revision)["status"] == "applied"
    assert registry.values["user"] == (str(tmp_path / "%USER_MISSING%") + ";" + str(literal_dir), 1)
    assert _preview(exe, registry, env)["status"] == "already_configured"


@pytest.mark.parametrize("kind", [1, 2])
def test_windows_percent_source_kind_controls_collision_resolution(tmp_path: Path, kind: int) -> None:
    exe, registry, env = _windows_fixture(tmp_path)
    literal = tmp_path / "%ROOT%"
    expanded = tmp_path / "expanded"
    literal.mkdir()
    expanded.mkdir()
    conflict = (literal if kind == 1 else expanded) / "ciao.cmd"
    conflict.write_text("different target")
    env["ROOT"] = "expanded"
    registry.values["machine"] = (str(literal), kind)
    assert _preview(exe, registry, env)["status"] == "refused"
    assert registry.writes == 0


@pytest.mark.parametrize("source", ["machine", "user"])
@pytest.mark.parametrize("raw", ["bin", ";/absolute", "/absolute;", "/absolute;;/other"])
def test_windows_relative_or_empty_path_component_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, raw: str) -> None:
    exe, registry, env = _windows_fixture(tmp_path)
    terminal = tmp_path / "terminal"
    terminal.mkdir()
    (terminal / "bin").mkdir()
    (terminal / "bin" / "ciao.exe").write_text("different executable")
    monkeypatch.chdir(tmp_path)
    registry.values[source] = (raw, 1)
    preview = _preview(exe, registry, env)
    assert preview["status"] == "refused"
    result = _apply(exe, registry, env, "prior-revision")
    assert result["status"] == "stale" and result["mutation"] == "none"
    assert registry.writes == 0


def test_windows_expanded_proposed_relative_component_refused(tmp_path: Path) -> None:
    exe, registry, env = _windows_fixture(tmp_path)
    registry.values["user"] = ("%ROOT%", 2)
    env["ROOT"] = "relative"
    assert _preview(exe, registry, env)["status"] == "refused"
    assert registry.writes == 0


def test_windows_proposed_expansion_guard_independently_rejects_relative_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.os_support import shell_hints
    _, registry, env = _windows_fixture(tmp_path)
    directory = tmp_path / "%INJECT%"
    directory.mkdir()
    exe = directory / "ciao.exe"
    exe.write_text("owned executable")
    exe.chmod(0o700)
    registry.values["machine"] = (str(tmp_path / "machine"), 1)
    registry.values["user"] = (str(tmp_path / "user"), 2)
    env["INJECT"] = "absolute-prefix;relative;"
    calls: list[str] = []
    def resolve(command: str, path: str, pathext: str) -> str | None:
        calls.append(path)
        return str(exe) if ";relative;" in path else None

    result = shell_hints._preview_windows_path_change("ciao", str(exe), registry, env, resolve, "account")
    assert result["status"] == "refused" and registry.writes == 0
    assert not any(";relative;" in path for path in calls)

    validate = shell_hints._validate_windows_path
    checked: list[str] = []
    def omit_proposed_only(path: str) -> None:
        checked.append(path)
        if len(checked) != 3:
            validate(path)
    monkeypatch.setattr(shell_hints, "_validate_windows_path", omit_proposed_only)
    result = shell_hints._preview_windows_path_change("ciao", str(exe), registry, env, resolve, "account")
    assert result["status"] == "ready" and registry.writes == 0
    assert len(checked) == 3 and ";relative;" in checked[2]
    assert any(";relative;" in path for path in calls)


@pytest.mark.parametrize("path", [r"C:bin", r"\bin", "bin", r"C:\bin;", r"C:\bin;;C:\other"])
def test_windows_native_path_validation_rejects_ambiguous_entries(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    from ciao.os_support.shell_hints import _validate_windows_path
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(ValueError):
        _validate_windows_path(path)


def test_windows_native_path_validation_accepts_quoted_drive_and_unc(monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.os_support.shell_hints import _validate_windows_path
    monkeypatch.setattr(sys, "platform", "win32")
    _validate_windows_path('"C:\\Program Files\\bin";\\\\server\\share\\bin')


def test_windows_path_refuses_pathext_collision_and_registry_failure(tmp_path: Path) -> None:
    exe, registry, env = _windows_fixture(tmp_path)
    (exe.parent / "ciao.cmd").write_text("other")
    assert _preview(exe, registry, env)["status"] == "refused"
    registry.values["machine"] = PermissionError()
    assert _preview(exe, registry, env)["status"] == "refused"


@pytest.mark.parametrize("change", ["machine", "user", "type", "expansion", "pathext", "target"])
def test_revision_includes_machine_user_types_expansion_and_ancestors(tmp_path: Path, change: str) -> None:
    exe, registry, env = _windows_fixture(tmp_path)
    registry.values["machine"] = ("%ROOT%", 2)
    env["ROOT"] = str(tmp_path / "empty")
    revision = _preview(exe, registry, env)["revision"]
    assert revision
    if change == "machine": registry.values["machine"] = ("other", 2)
    if change == "user": registry.values["user"] = ("other", 1)
    if change == "type": registry.values["machine"] = ("%ROOT%", 1)
    if change == "expansion": env["ROOT"] += "-changed"
    if change == "pathext": env["PATHEXT"] = ".EXE"
    if change == "target": exe.write_text("changed executable")
    assert _apply(exe, registry, env, revision)["status"] == "stale"
    assert registry.writes == 0


def test_windows_notification_failure_verifies_before_applied_warning(tmp_path: Path) -> None:
    exe, registry, env = _windows_fixture(tmp_path)
    def fail() -> None:
        raise OSError("notify failed")
    revision = _preview(exe, registry, env)["revision"]
    result = _apply(exe, registry, env, revision, fail)
    assert result == {"status": "applied", "detail": result["detail"], "mutation": "written", "warning": "Windows was not notified; reopen terminals."}


def test_apply_closed_mutation_contract(tmp_path: Path) -> None:
    exe, registry, env = _windows_fixture(tmp_path)
    revision = _preview(exe, registry, env)["revision"]
    registry.write_error = True
    result = _apply(exe, registry, env, revision)
    assert set(result) == {"status", "detail", "mutation", "warning"}
    assert (result["status"], result["mutation"]) == ("write_failed", "unknown")
    registry.write_error = False
    assert _apply(exe, registry, env, revision)["status"] == "stale"
    assert registry.writes == 1


def test_revision_platform_fields_are_disjoint(tmp_path: Path) -> None:
    from ciao.os_support.shell_hints import _windows_state
    exe, registry, env = _windows_fixture(tmp_path)
    state, status, _ = _windows_state("ciao", str(exe), registry, env, _fake_resolve, "account")
    assert status == "ready"
    assert state["posix"] is None
    assert set(state["windows"]) == {"machine", "user", "expansion_inputs", "pathext", "proposed_user_path"}
    assert state["platform"] == "windows"


def test_windows_missing_expansion_refuses_and_postwrite_probe_failure_is_written(tmp_path: Path) -> None:
    exe, registry, env = _windows_fixture(tmp_path)
    registry.values["machine"] = ("%MISSING%", 2)
    assert _preview(exe, registry, env)["status"] == "refused"
    registry.values["machine"] = ("", 1)
    revision = _preview(exe, registry, env)["revision"]
    def fail_and_break_probe() -> None:
        registry.values["machine"] = PermissionError()
        raise OSError("notification failed")
    result = _apply(exe, registry, env, revision, fail_and_break_probe)
    assert result["status"] == "verification_failed"
    assert result["mutation"] == "written"
    assert result["warning"] == ""


def test_windows_fresh_preview_reauthorizes_unchanged_state_after_uncertain_write(tmp_path: Path) -> None:
    exe, registry, env = _windows_fixture(tmp_path)
    first = _preview(exe, registry, env)["revision"]
    registry.write_error = True
    assert _apply(exe, registry, env, first)["mutation"] == "unknown"
    registry.write_error = False
    assert _apply(exe, registry, env, first)["status"] == "stale"
    assert registry.writes == 1
    second = _preview(exe, registry, env)["revision"]
    assert second == first  # A revision fingerprints content; it is not an authorization nonce.
    assert _apply(exe, registry, env, second)["status"] == "applied"
    assert registry.writes == 2


def test_windows_write_open_failure_is_no_mutation(tmp_path: Path) -> None:
    exe, registry, env = _windows_fixture(tmp_path)
    revision = _preview(exe, registry, env)["revision"]
    registry.write_open_error = True
    result = _apply(exe, registry, env, revision)
    assert (result["status"], result["mutation"], registry.writes) == ("write_failed", "none", 0)


def test_windows_quoted_case_and_trailing_separator_entry_not_duplicated(tmp_path: Path) -> None:
    from ciao.os_support.shell_hints import _preview_windows_path_change
    exe, registry, env = _windows_fixture(tmp_path)
    registry.values["user"] = ('"' + str(exe.parent).upper() + '/"', 2)
    def resolve(command: str, path: str, pathext: str) -> str | None:
        for entry in path.split(";"):
            if entry.strip().strip('"').rstrip("\\/").casefold() == str(exe.parent).casefold():
                return str(exe)
        return None
    assert _preview_windows_path_change("ciao", str(exe), registry, env, resolve, "account")["status"] == "already_configured"
    assert registry.writes == 0


def test_windows_symlink_executable_and_retarget_stale(tmp_path: Path) -> None:
    exe, registry, env = _windows_fixture(tmp_path)
    target = tmp_path / "target"
    target.write_text("first")
    target.chmod(0o700)
    exe.unlink()
    exe.symlink_to(target)
    revision = _preview(exe, registry, env)["revision"]
    assert revision
    other = tmp_path / "other"
    other.write_text("second")
    other.chmod(0o700)
    exe.unlink()
    exe.symlink_to(other)
    assert _apply(exe, registry, env, revision)["status"] == "stale"
    assert registry.writes == 0


def test_windows_expanded_proposed_directory_must_resolve_intended_executable(tmp_path: Path) -> None:
    from ciao.os_support.shell_hints import _preview_windows_path_change
    directory = tmp_path / "%DEST%"
    directory.mkdir()
    exe = directory / "ciao.exe"
    exe.write_text("exe")
    exe.chmod(0o700)
    registry = _FakeRegistry({"machine": ("", 1), "user": FileNotFoundError()})
    env = {"DEST": "elsewhere", "PATHEXT": ".EXE"}
    assert _preview_windows_path_change("ciao", str(exe), registry, env, _fake_resolve, "account")["status"] == "refused"
    assert registry.writes == 0


def test_windows_notification_adapter_uses_bounded_environment_broadcast() -> None:
    from ciao.os_support.shell_hints import _broadcast_windows_path_change
    calls: list[tuple[object, ...]] = []
    def send(*args: object) -> int:
        calls.append(args)
        return 1
    _broadcast_windows_path_change(send)
    assert calls[0][:5] == (0xFFFF, 0x001A, 0, "Environment", 2)
    assert calls[0][5] == 5000
    with pytest.raises(OSError):
        _broadcast_windows_path_change(lambda *args: 0)


def test_windows_resolver_adapter_uses_shared_resolver_and_pathext(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from ciao.os_support import tool_path
    from ciao.os_support.shell_hints import _resolve_windows_path_executable
    exe = tmp_path / "ciao.exe"
    exe.write_text("owned fixture")
    monkeypatch.setenv("PATHEXT", ".EXE")
    monkeypatch.setattr(tool_path, "resolve_executable", lambda command, *, path: str(exe))
    assert _resolve_windows_path_executable("ciao", str(tmp_path), ".EXE") == str(exe)
    with pytest.raises(ValueError):
        _resolve_windows_path_executable("ciao", str(tmp_path), ".CMD")


def test_windows_apply_wires_resolver_and_notification_adapters(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from ciao.os_support import tool_path
    from ciao.os_support.shell_hints import (
        _apply_windows_path_change, _broadcast_windows_path_change,
        _preview_windows_path_change, _resolve_windows_path_executable,
    )
    exe, registry, env = _windows_fixture(tmp_path)
    monkeypatch.setenv("PATHEXT", env["PATHEXT"])
    monkeypatch.setattr(tool_path, "resolve_executable", lambda command, *, path: _fake_resolve(command, path, env["PATHEXT"]))
    notifications: list[tuple[object, ...]] = []
    def send(*args: object) -> int:
        notifications.append(args)
        return 1
    preview = _preview_windows_path_change("ciao", str(exe), registry, env, _resolve_windows_path_executable, "account")
    result = _apply_windows_path_change("ciao", str(exe), preview["revision"], registry, env,
                                        _resolve_windows_path_executable, "account",
                                        lambda: _broadcast_windows_path_change(send))
    assert result["status"] == "applied"
    assert registry.writes == 1
    assert len(notifications) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows executable resolution")
def test_windows_native_resolver_adapter_with_owned_executable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from ciao.os_support.shell_hints import _resolve_windows_path_executable
    exe = tmp_path / "ciao.exe"
    exe.write_bytes(b"fixture")
    monkeypatch.setenv("PATHEXT", ".EXE")
    assert Path(_resolve_windows_path_executable("ciao", str(tmp_path), ".EXE") or "") == exe


def _posix_fixture(tmp_path: Path) -> tuple[object, Path, dict[str, str]]:
    from ciao.os_support.shell_hints import _PosixAccount
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    shell = tmp_path / "bash"
    shell.write_text("fixture")
    shell.chmod(0o700)
    exe = tmp_path / "tools" / "ciao"
    exe.parent.mkdir()
    exe.write_text("fixture")
    exe.chmod(0o700)
    account = _PosixAccount(os.geteuid(), str(home), str(shell), "fixture")
    return account, exe, {"PATH": "/nonexistent", "HOME": str(home), "ZDOTDIR": "", "XDG_CONFIG_HOME": ""}


def _posix_preview(account: object, exe: Path, env: dict[str, str]) -> dict[str, str]:
    from ciao.os_support.shell_hints import _preview_posix_path_change
    return _preview_posix_path_change("ciao", str(exe), account, lambda _: env, "fixture")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX profile semantics")
def test_posix_preview_bash_selection_revision_and_read_only(tmp_path: Path) -> None:
    from ciao.os_support.shell_hints import _posix_state, _path_revision
    account, exe, env = _posix_fixture(tmp_path)
    home = Path(env["HOME"])
    (home / ".bash_login").write_bytes(b"export PATH=/usr/bin")
    (home / ".profile").write_bytes(b"other\n")
    state, status, _ = _posix_state("ciao", str(exe), account, env, "fixture")
    assert status == "ready" and state["windows"] is None
    assert state["posix"]["destination"] == str(home / ".bash_login")
    assert bytes.fromhex(state["posix"]["append_hex"]).startswith(b"\n# >>> Ciaobot PATH ")
    assert _posix_preview(account, exe, env)["revision"] == _path_revision(state)
    assert (home / ".bash_login").read_bytes() == b"export PATH=/usr/bin"
    assert sorted(p.name for p in home.iterdir()) == [".bash_login", ".profile"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX profile semantics")
def test_posix_preview_fish_missing_parents_and_quoted_path(tmp_path: Path) -> None:
    from ciao.os_support.shell_hints import _PosixAccount, _posix_state
    account, exe, env = _posix_fixture(tmp_path)
    fish = tmp_path / "fish"
    fish.write_text("fixture")
    fish.chmod(0o700)
    account = _PosixAccount(os.geteuid(), env["HOME"], str(fish), "fixture")
    env["XDG_CONFIG_HOME"] = env["HOME"] + "/custom"
    state, status, _ = _posix_state("ciao", str(exe), account, env, "fixture")
    assert status == "ready"
    assert state["posix"]["destination"].endswith("/custom/fish/config.fish")
    assert b"fish_add_path --append --path '" in bytes.fromhex(state["posix"]["append_hex"])
    assert not Path(env["XDG_CONFIG_HOME"]).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX profile semantics")
def test_posix_preview_rejects_collision_unsafe_markers_and_unrepresentable_directory(tmp_path: Path) -> None:
    account, exe, env = _posix_fixture(tmp_path)
    home = Path(env["HOME"])
    old = tmp_path / "old"
    old.mkdir()
    (old / "ciao").write_text("other")
    (old / "ciao").chmod(0o700)
    env["PATH"] = str(old)
    assert _posix_preview(account, exe, env)["status"] == "refused"
    env["PATH"] = "/nonexistent"
    (home / ".bash_profile").write_bytes(b"# >>> Ciaobot PATH ")
    assert _posix_preview(account, exe, env)["status"] == "refused"
    (home / ".bash_profile").unlink()
    (home / ".bash_profile").symlink_to(exe)
    assert _posix_preview(account, exe, env)["status"] == "refused"
    bad = tmp_path / "bad:name"
    bad.mkdir()
    (bad / "ciao").write_text("fixture")
    (bad / "ciao").chmod(0o700)
    assert _posix_preview(account, bad / "ciao", env)["status"] == "refused"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX account semantics")
def test_posix_probe_uses_injected_account_and_rejects_ambiguous_roots(tmp_path: Path) -> None:
    from ciao.os_support.shell_hints import _probe_posix_account
    account, exe, env = _posix_fixture(tmp_path)
    calls: list[tuple[object, ...]] = []
    def runner(*args: object) -> bytes:
        calls.append(args)
        return ("__ciaobot_path_probe_PATH_START__" + env["PATH"] + "__ciaobot_path_probe_PATH_END__"
                + "__ciaobot_path_probe_HOME_START__" + env["HOME"] + "__ciaobot_path_probe_HOME_END__"
                + "__ciaobot_path_probe_ZDOTDIR_START____ciaobot_path_probe_ZDOTDIR_END__"
                + "__ciaobot_path_probe_XDG_START____ciaobot_path_probe_XDG_END__").encode()
    assert _probe_posix_account(account, runner)["HOME"] == env["HOME"]
    assert len(calls) == 2
    assert calls[0][1] is True and calls[1][1] is False
    assert calls[0][2]["HOME"] == env["HOME"]
    assert "PATH" not in calls[0][2]
    def ambiguous(*args: object) -> bytes:
        value = runner(*args)
        return value.replace(b"__ciaobot_path_probe_ZDOTDIR_END__", (env["HOME"] + "/config__ciaobot_path_probe_ZDOTDIR_END__").encode()) if not args[1] else value
    with pytest.raises(ValueError):
        _probe_posix_account(account, ambiguous)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell semantics")
def test_posix_probe_actual_framing_with_owned_fixture_shell(tmp_path: Path) -> None:
    from ciao.os_support.shell_hints import _PosixAccount, _probe_posix_account
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    shell = tmp_path / "bash"
    shell.write_text('#!/bin/sh\nPATH=/usr/bin:/bin\nexport PATH\nexec /bin/bash --noprofile --norc -c "$2"\n')
    shell.chmod(0o700)
    account = _PosixAccount(os.geteuid(), str(home), str(shell), "fixture")
    result = _probe_posix_account(account)
    assert result["HOME"] == str(home)
    assert result["PATH"] and all(os.path.isabs(part) for part in result["PATH"].split(":"))
    assert result["ZDOTDIR"] == result["XDG_CONFIG_HOME"] == ""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX account lookup")
def test_posix_account_lookup_ignores_service_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pwd
    from types import SimpleNamespace
    from ciao.os_support.shell_hints import _posix_account
    account, _, env = _posix_fixture(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "service-home"))
    monkeypatch.setenv("SHELL", str(tmp_path / "service-shell"))
    monkeypatch.setattr(os, "geteuid", lambda: 12345)
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: SimpleNamespace(
        pw_uid=uid, pw_dir=env["HOME"], pw_shell=account.shell, pw_name="fixture"))
    result = _posix_account()
    assert (result.uid, result.home, result.shell) == (12345, env["HOME"], account.shell)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor semantics")
@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_posix_public_apply_creates_approved_profile_and_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shell: str) -> None:
    from ciao.os_support import shell_hints, users
    account, exe, env = _posix_fixture(tmp_path)
    executable = tmp_path / shell
    if shell != "bash":
        executable.write_text("fixture")
        executable.chmod(0o700)
        account = shell_hints._PosixAccount(os.geteuid(), env["HOME"], str(executable), "fixture")
    if shell == "zsh": env["ZDOTDIR"] = env["HOME"] + "/zconfig"
    if shell == "fish": env["XDG_CONFIG_HOME"] = env["HOME"] + "/custom"
    _, _, destination = shell_hints._posix_state("ciao", str(exe), account, env, "fixture")
    profile = Path(destination)
    def probe(_: object) -> dict[str, str]:
        return {**env, "PATH": str(exe.parent) if profile.exists() else "/nonexistent"}
    monkeypatch.setattr(shell_hints, "_posix_account", lambda: account)
    monkeypatch.setattr(shell_hints, "_probe_posix_account", probe)
    monkeypatch.setattr(users, "user_key", lambda: "fixture")
    preview = shell_hints.preview_path_change("ciao", str(exe))
    assert preview["status"] == "ready" and not profile.exists()
    result = shell_hints.apply_path_change("ciao", str(exe), preview["revision"])
    assert (result["status"], result["mutation"]) == ("applied", "written")
    assert profile.is_file() and b"Ciaobot PATH" in profile.read_bytes()
    assert shell_hints.preview_path_change("ciao", str(exe))["status"] == "already_configured"
    assert shell_hints.apply_path_change("ciao", str(exe), preview["revision"])["status"] == "stale"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor semantics")
def test_posix_apply_stale_and_short_write_are_truthful(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.os_support import shell_hints, users
    account, exe, env = _posix_fixture(tmp_path)
    monkeypatch.setattr(shell_hints, "_posix_account", lambda: account)
    monkeypatch.setattr(shell_hints, "_probe_posix_account", lambda _: env)
    monkeypatch.setattr(users, "user_key", lambda: "fixture")
    profile = Path(env["HOME"]) / ".bash_profile"
    preview = shell_hints.preview_path_change("ciao", str(exe))
    profile.write_bytes(b"# changed\n")
    assert shell_hints.apply_path_change("ciao", str(exe), preview["revision"])["status"] == "stale"
    assert profile.read_bytes() == b"# changed\n"
    preview = shell_hints.preview_path_change("ciao", str(exe))
    real_write = os.write
    def short(fd: int, data: bytes) -> int:
        return real_write(fd, data[:8])
    monkeypatch.setattr(os, "write", short)
    result = shell_hints.apply_path_change("ciao", str(exe), preview["revision"])
    assert (result["status"], result["mutation"]) == ("write_failed", "partial")
    assert shell_hints.preview_path_change("ciao", str(exe))["status"] == "refused"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor semantics")
def test_posix_apply_written_but_fresh_probe_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.os_support import shell_hints, users
    account, exe, env = _posix_fixture(tmp_path)
    profile = Path(env["HOME"]) / ".bash_profile"
    def probe(_: object) -> dict[str, str]:
        if profile.exists(): raise ValueError("private startup output")
        return env
    monkeypatch.setattr(shell_hints, "_posix_account", lambda: account)
    monkeypatch.setattr(shell_hints, "_probe_posix_account", probe)
    monkeypatch.setattr(users, "user_key", lambda: "fixture")
    preview = shell_hints.preview_path_change("ciao", str(exe))
    result = shell_hints.apply_path_change("ciao", str(exe), preview["revision"])
    assert (result["status"], result["mutation"]) == ("verification_failed", "written")
    assert "private" not in result["detail"] and profile.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor semantics")
def test_posix_apply_created_directory_then_failure_is_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.os_support import shell_hints, users
    account, exe, env = _posix_fixture(tmp_path)
    fish = tmp_path / "fish"
    fish.write_text("fixture")
    fish.chmod(0o700)
    account = shell_hints._PosixAccount(os.geteuid(), env["HOME"], str(fish), "fixture")
    monkeypatch.setattr(shell_hints, "_posix_account", lambda: account)
    monkeypatch.setattr(shell_hints, "_probe_posix_account", lambda _: env)
    monkeypatch.setattr(users, "user_key", lambda: "fixture")
    preview = shell_hints.preview_path_change("ciao", str(exe))
    real_mkdir = os.mkdir
    def fail_second(path: object, *args: object, **kwargs: object) -> None:
        if path == "fish": raise PermissionError("denied")
        real_mkdir(path, *args, **kwargs)
    monkeypatch.setattr(os, "mkdir", fail_second)
    result = shell_hints.apply_path_change("ciao", str(exe), preview["revision"])
    assert (result["status"], result["mutation"]) == ("write_failed", "partial")
    assert (Path(env["HOME"]) / ".config").is_dir()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor semantics")
def test_posix_apply_write_exception_is_unknown_and_preserves_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.os_support import shell_hints, users
    account, exe, env = _posix_fixture(tmp_path)
    profile = Path(env["HOME"]) / ".bash_profile"
    profile.write_bytes(b"# original\n")
    monkeypatch.setattr(shell_hints, "_posix_account", lambda: account)
    monkeypatch.setattr(shell_hints, "_probe_posix_account", lambda _: env)
    monkeypatch.setattr(users, "user_key", lambda: "fixture")
    preview = shell_hints.preview_path_change("ciao", str(exe))
    real_write = os.write
    def fail_write(fd: int, data: bytes) -> int:
        raise OSError("uncertain write")
    monkeypatch.setattr(os, "write", fail_write)
    result = shell_hints.apply_path_change("ciao", str(exe), preview["revision"])
    assert (result["status"], result["mutation"]) == ("write_failed", "unknown")
    assert profile.read_bytes() == b"# original\n"
    monkeypatch.setattr(os, "write", real_write)
    assert shell_hints.apply_path_change("ciao", str(exe), preview["revision"])["status"] == "stale"
    fresh = shell_hints.preview_path_change("ciao", str(exe))
    assert fresh["revision"] == preview["revision"]
    assert shell_hints.apply_path_change("ciao", str(exe), fresh["revision"])["mutation"] == "written"


def test_windows_public_dispatch_with_fake_registry_and_notification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.os_support import shell_hints, users
    exe, registry, env = _windows_fixture(tmp_path)
    validate = shell_hints._validate_windows_path
    if sys.platform != "win32":
        def validate_fixture_paths(path: str) -> None:
            entries = path.split(";")
            validate(";".join("C:" + entry.replace("/", "\\") if entry.startswith("/") else entry for entry in entries))
        monkeypatch.setattr(shell_hints, "_validate_windows_path", validate_fixture_paths)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", registry)
    monkeypatch.setattr(users, "user_key", lambda: "fixture")
    monkeypatch.setattr(shell_hints, "_resolve_windows_path_executable", _fake_resolve)
    calls: list[bool] = []
    monkeypatch.setattr(shell_hints, "_broadcast_windows_path_change", lambda: calls.append(True))
    monkeypatch.setenv("PATHEXT", env["PATHEXT"])
    preview = shell_hints.preview_path_change("ciao", str(exe))
    assert preview["status"] == "ready"
    result = shell_hints.apply_path_change("ciao", str(exe), preview["revision"])
    assert (result["status"], result["mutation"], calls) == ("applied", "written", [True])


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor semantics")
def test_posix_apply_refuses_ancestor_replacement_after_revalidation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.os_support import shell_hints, users
    account, exe, env = _posix_fixture(tmp_path)
    fish = tmp_path / "fish"
    fish.write_text("fixture")
    fish.chmod(0o700)
    account = shell_hints._PosixAccount(os.geteuid(), env["HOME"], str(fish), "fixture")
    config = Path(env["HOME"]) / ".config"
    config.mkdir(mode=0o700)
    monkeypatch.setattr(shell_hints, "_posix_account", lambda: account)
    monkeypatch.setattr(shell_hints, "_probe_posix_account", lambda _: env)
    monkeypatch.setattr(users, "user_key", lambda: "fixture")
    preview = shell_hints.preview_path_change("ciao", str(exe))
    real_open = os.open
    def race(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if path == ".config":
            config.rename(config.with_name("old-config"))
            config.symlink_to(tmp_path, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", race)
    result = shell_hints.apply_path_change("ciao", str(exe), preview["revision"])
    assert (result["status"], result["mutation"]) == ("write_failed", "none")
    assert not (tmp_path / "fish" / "config.fish").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor semantics")
@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_posix_apply_real_isolated_shell_sources_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shell: str) -> None:
    import shutil
    from ciao.os_support import shell_hints, users
    binary = shutil.which(shell)
    if not binary:
        pytest.skip(f"{shell} unavailable")
    account, exe, env = _posix_fixture(tmp_path)
    fixture_shell = tmp_path / shell
    if shell == "zsh":
        fixture_shell.write_text("fixture")
        fixture_shell.chmod(0o700)
        account = shell_hints._PosixAccount(os.geteuid(), env["HOME"], str(fixture_shell), "fixture")
    profile = Path(env["HOME"]) / (".bash_profile" if shell == "bash" else ".zshrc")
    original = b"# isolated comment\r\n"
    profile.write_bytes(original)
    def probe(_: object) -> dict[str, str]:
        args = [binary] + (["--noprofile", "--norc"] if shell == "bash" else ["-f"])
        args += ["-c", '. "$1"; printf %s "$PATH"', shell, str(profile)]
        completed = subprocess.run(args, cwd=env["HOME"], env={"HOME": env["HOME"], "PATH": "/usr/bin:/bin"}, capture_output=True, check=True)
        return {**env, "PATH": completed.stdout.decode()}
    monkeypatch.setattr(shell_hints, "_posix_account", lambda: account)
    monkeypatch.setattr(shell_hints, "_probe_posix_account", probe)
    monkeypatch.setattr(users, "user_key", lambda: "fixture")
    preview = shell_hints.preview_path_change("ciao", str(exe))
    assert preview["status"] == "ready"
    result = shell_hints.apply_path_change("ciao", str(exe), preview["revision"])
    assert (result["status"], result["mutation"]) == ("applied", "written")
    assert profile.read_bytes().startswith(original)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor semantics")
def test_path_apply_bash_first_existing_login_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.os_support import shell_hints, users
    account, exe, env = _posix_fixture(tmp_path)
    home = Path(env["HOME"])
    profile = home / ".bash_login"
    profile.write_bytes(b"# existing")
    profile.chmod(0o640)
    (home / ".profile").write_bytes(b"# untouched\n")
    def probe(_: object) -> dict[str, str]:
        return {**env, "PATH": str(exe.parent) if b"Ciaobot PATH" in profile.read_bytes() else env["PATH"]}
    monkeypatch.setattr(shell_hints, "_posix_account", lambda: account)
    monkeypatch.setattr(shell_hints, "_probe_posix_account", probe)
    monkeypatch.setattr(users, "user_key", lambda: "fixture")
    preview = shell_hints.preview_path_change("ciao", str(exe))
    assert preview["destination"] == str(profile)
    assert shell_hints.apply_path_change("ciao", str(exe), preview["revision"])["status"] == "applied"
    assert profile.read_bytes().startswith(b"# existing\n# >>>")
    assert profile.stat().st_mode & 0o777 == 0o640
    assert (home / ".profile").read_bytes() == b"# untouched\n"
    assert not (home / ".bash_profile").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor semantics")
def test_path_apply_preserves_unrelated_prior_resolutions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.os_support import shell_hints, users
    account, exe, env = _posix_fixture(tmp_path)
    old = tmp_path / "old"
    old.mkdir()
    for directory in (old, exe.parent):
        target = directory / "git"
        target.write_text("fixture")
        target.chmod(0o700)
    profile = Path(env["HOME"]) / ".bash_profile"
    profile.write_bytes(b"# existing\n")
    def probe(_: object) -> dict[str, str]:
        result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", '. "$1"; printf %s "$PATH"', "bash", str(profile)],
                                cwd=env["HOME"], env={"HOME": env["HOME"], "PATH": str(old) + ":/usr/bin:/bin"},
                                capture_output=True, check=True)
        return {**env, "PATH": result.stdout.decode()}
    monkeypatch.setattr(shell_hints, "_posix_account", lambda: account)
    monkeypatch.setattr(shell_hints, "_probe_posix_account", probe)
    monkeypatch.setattr(users, "user_key", lambda: "fixture")
    preview = shell_hints.preview_path_change("ciao", str(exe))
    assert shell_hints.apply_path_change("ciao", str(exe), preview["revision"])["status"] == "applied"
    result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", '. "$1"; command -v git', "bash", str(profile)],
                            cwd=env["HOME"], env={"HOME": env["HOME"], "PATH": str(old) + ":/usr/bin:/bin"},
                            capture_output=True, check=True)
    assert result.stdout.strip() == os.fsencode(old / "git")
    import shlex
    appended = f'export PATH="$PATH":{shlex.quote(str(exe.parent))}'.encode()
    prepended = f'export PATH={shlex.quote(str(exe.parent))}:"$PATH"'.encode()
    actual = profile.read_bytes()
    assert appended in actual
    negative = Path(env["HOME"]) / "negative-control"
    negative.write_bytes(actual.replace(appended, prepended))
    result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", '. "$1"; command -v git', "bash", str(negative)],
                            cwd=env["HOME"], env={"HOME": env["HOME"], "PATH": str(old) + ":/usr/bin:/bin"},
                            capture_output=True, check=True)
    assert result.stdout.strip() == os.fsencode(exe.parent / "git")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX account lookup")
def test_posix_public_failure_returns_closed_string_shapes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.os_support import shell_hints
    def fail() -> object:
        raise OSError("private data")
    monkeypatch.setattr(shell_hints, "_posix_account", fail)
    preview = shell_hints.preview_path_change("ciao", str(tmp_path / "ciao"))
    applied = shell_hints.apply_path_change("ciao", str(tmp_path / "ciao"), "revision")
    assert set(preview) == {"status", "detail", "executable", "directory", "destination", "revision"}
    assert set(applied) == {"status", "detail", "mutation", "warning"}
    assert preview["status"] == "refused" and applied["status"] == "stale"
    assert all(isinstance(value, str) for value in (*preview.values(), *applied.values()))
    assert "private data" not in str((preview, applied))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell semantics")
def test_zsh_apply_quoted_directory_real_shell_idempotence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil
    from ciao.os_support import shell_hints, users
    zsh = shutil.which("zsh")
    if not zsh:
        pytest.skip("zsh unavailable")
    account, _, env = _posix_fixture(tmp_path)
    fixture_shell = tmp_path / "zsh"
    fixture_shell.write_text("fixture")
    fixture_shell.chmod(0o700)
    account = shell_hints._PosixAccount(os.geteuid(), env["HOME"], str(fixture_shell), "fixture")
    directory = tmp_path / "O'Neil tools"
    directory.mkdir()
    exe = directory / "ciao"
    exe.write_text("fixture")
    exe.chmod(0o700)
    config = Path(env["HOME"]) / "zconfig"
    config.mkdir(mode=0o700)
    env["ZDOTDIR"] = str(config)
    profile = config / ".zshrc"
    original = b"# retained\n"
    profile.write_bytes(original)
    def probe(_: object) -> dict[str, str]:
        result = subprocess.run([zsh, "-f", "-c", '. "$1"; printf %s "$PATH"', "zsh", str(profile)],
                                cwd=env["HOME"], env={"HOME": env["HOME"], "PATH": "/usr/bin:/bin"},
                                capture_output=True, check=True)
        return {**env, "PATH": result.stdout.decode()}
    monkeypatch.setattr(shell_hints, "_posix_account", lambda: account)
    monkeypatch.setattr(shell_hints, "_probe_posix_account", probe)
    monkeypatch.setattr(users, "user_key", lambda: "fixture")
    preview = shell_hints.preview_path_change("ciao", str(exe))
    assert preview["status"] == "ready"
    assert shell_hints.apply_path_change("ciao", str(exe), preview["revision"])["status"] == "applied"
    saved = profile.read_bytes()
    assert saved.startswith(original) and b"'\"'\"'" in saved
    assert shell_hints.preview_path_change("ciao", str(exe))["status"] == "already_configured"
    assert shell_hints.apply_path_change("ciao", str(exe), preview["revision"])["status"] == "stale"
    assert profile.read_bytes() == saved


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX profile semantics")
def test_fish_apply_renders_quoted_space_quote_and_backslash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.os_support import shell_hints, users
    account, _, env = _posix_fixture(tmp_path)
    fish = tmp_path / "fish"
    fish.write_text("fixture")
    fish.chmod(0o700)
    account = shell_hints._PosixAccount(os.geteuid(), env["HOME"], str(fish), "fixture")
    directory = tmp_path / "O'Neil \\ tools"
    directory.mkdir()
    exe = directory / "ciao"
    exe.write_text("fixture")
    exe.chmod(0o700)
    env["XDG_CONFIG_HOME"] = env["HOME"] + "/custom"
    profile = Path(env["XDG_CONFIG_HOME"]) / "fish" / "config.fish"
    def probe(_: object) -> dict[str, str]:
        return {**env, "PATH": str(directory) if profile.exists() else "/usr/bin:/bin"}
    monkeypatch.setattr(shell_hints, "_posix_account", lambda: account)
    monkeypatch.setattr(shell_hints, "_probe_posix_account", probe)
    monkeypatch.setattr(users, "user_key", lambda: "fixture")
    preview = shell_hints.preview_path_change("ciao", str(exe))
    assert preview["status"] == "ready" and not profile.parent.exists()
    assert shell_hints.apply_path_change("ciao", str(exe), preview["revision"])["status"] == "applied"
    expected = "fish_add_path --append --path '" + str(directory).replace("\\", "\\\\").replace("'", "\\'") + "'"
    assert expected.encode() in profile.read_bytes().splitlines()
    assert shell_hints.preview_path_change("ciao", str(exe))["status"] == "already_configured"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX profile semantics")
@pytest.mark.parametrize("change", ["identity", "mode"])
def test_posix_ancestor_revision_change_is_stale_without_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    from ciao.os_support import shell_hints, users
    account, exe, env = _posix_fixture(tmp_path)
    fish = tmp_path / "fish"
    fish.write_text("fixture")
    fish.chmod(0o700)
    account = shell_hints._PosixAccount(os.geteuid(), env["HOME"], str(fish), "fixture")
    config = Path(env["HOME"]) / ".config"
    config.mkdir(mode=0o700)
    monkeypatch.setattr(shell_hints, "_posix_account", lambda: account)
    monkeypatch.setattr(shell_hints, "_probe_posix_account", lambda _: env)
    monkeypatch.setattr(users, "user_key", lambda: "fixture")
    preview = shell_hints.preview_path_change("ciao", str(exe))
    assert preview["status"] == "ready"
    if change == "identity":
        config.rename(config.with_name("old-config"))
        config.mkdir(mode=0o700)
    else:
        config.chmod(0o750)
    result = shell_hints.apply_path_change("ciao", str(exe), preview["revision"])
    assert (result["status"], result["mutation"]) == ("stale", "none")
    assert list(config.iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell semantics")
def test_bash_apply_unterminated_export_preserves_bytes_and_resolves_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.os_support import shell_hints, users
    account, exe, env = _posix_fixture(tmp_path)
    profile = Path(env["HOME"]) / ".bash_profile"
    original = b"export PATH=/usr/bin:/bin"
    profile.write_bytes(original)
    def probe(_: object) -> dict[str, str]:
        result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", '. "$1"; printf %s "$PATH"', "bash", str(profile)],
                                cwd=env["HOME"], env={"HOME": env["HOME"], "PATH": "/usr/bin:/bin"},
                                capture_output=True, check=True)
        return {**env, "PATH": result.stdout.decode()}
    monkeypatch.setattr(shell_hints, "_posix_account", lambda: account)
    monkeypatch.setattr(shell_hints, "_probe_posix_account", probe)
    monkeypatch.setattr(users, "user_key", lambda: "fixture")
    preview = shell_hints.preview_path_change("ciao", str(exe))
    assert preview["status"] == "ready"
    result = shell_hints.apply_path_change("ciao", str(exe), preview["revision"])
    assert (result["status"], result["mutation"]) == ("applied", "written")
    content = profile.read_bytes()
    assert content.startswith(original + b"\n# >>> Ciaobot PATH ")
    assert b"\r" not in content[len(original):]
    resolved = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", '. "$1"; command -v ciao', "bash", str(profile)],
                              cwd=env["HOME"], env={"HOME": env["HOME"], "PATH": "/usr/bin:/bin"},
                              capture_output=True, check=True)
    assert resolved.stdout.strip() == os.fsencode(exe)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell semantics")
def test_bash_apply_existing_empty_profile_has_no_separator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.os_support import shell_hints, users
    account, exe, env = _posix_fixture(tmp_path)
    profile = Path(env["HOME"]) / ".bash_profile"
    profile.write_bytes(b"")
    shell_env = {"HOME": env["HOME"], "PATH": "/usr/bin:/bin"}

    def probe(_: object) -> dict[str, str]:
        result = subprocess.run(
            ["/bin/bash", "--noprofile", "--norc", "-c", '. "$1"; printf %s "$PATH"', "bash", str(profile)],
            cwd=env["HOME"], env=shell_env, capture_output=True, check=True,
        )
        return {**env, "PATH": result.stdout.decode()}

    monkeypatch.setattr(shell_hints, "_posix_account", lambda: account)
    monkeypatch.setattr(shell_hints, "_probe_posix_account", probe)
    monkeypatch.setattr(users, "user_key", lambda: "fixture")
    preview = shell_hints.preview_path_change("ciao", str(exe))
    assert preview["status"] == "ready" and profile.read_bytes() == b""
    result = shell_hints.apply_path_change("ciao", str(exe), preview["revision"])
    assert (result["status"], result["mutation"]) == ("applied", "written")
    assert profile.read_bytes().startswith(b"# >>> Ciaobot PATH ")
    resolved = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", '. "$1"; command -v ciao', "bash", str(profile)],
        cwd=env["HOME"], env=shell_env, capture_output=True, check=True,
    )
    assert resolved.stdout.strip() == os.fsencode(exe)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor semantics")
@pytest.mark.parametrize("failure", ["file", "parent"])
def test_posix_complete_write_fsync_failure_is_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    import stat
    from ciao.os_support import shell_hints, tool_path, users
    account, exe, env = _posix_fixture(tmp_path)
    profile = Path(env["HOME"]) / ".bash_profile"
    original = b"# original\n"
    profile.write_bytes(original)
    state, _, _ = shell_hints._posix_state("ciao", str(exe), account, env, "fixture")
    payload = bytes.fromhex(state["posix"]["append_hex"])
    monkeypatch.setattr(shell_hints, "_posix_account", lambda: account)
    monkeypatch.setattr(shell_hints, "_probe_posix_account", lambda _: env)
    monkeypatch.setattr(users, "user_key", lambda: "fixture")
    revision = shell_hints.preview_path_change("ciao", str(exe))["revision"]
    real_fsync = os.fsync
    invalidations: list[bool] = []
    monkeypatch.setattr(tool_path, "clear_terminal_path_cache", lambda: invalidations.append(True))
    def fsync(fd: int) -> None:
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if is_directory == (failure == "parent"):
            raise OSError("injected fsync failure")
        real_fsync(fd)
    monkeypatch.setattr(os, "fsync", fsync)
    result = shell_hints.apply_path_change("ciao", str(exe), revision)
    assert (result["status"], result["mutation"]) == ("verification_failed", "written")
    assert profile.read_bytes() == original + payload
    assert invalidations == [True]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX descriptor semantics")
def test_posix_cache_invalidation_failure_preserves_written_outcome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.os_support import shell_hints, tool_path, users
    account, exe, env = _posix_fixture(tmp_path)
    profile = Path(env["HOME"]) / ".bash_profile"
    profile.write_bytes(b"# original\n")
    state, _, _ = shell_hints._posix_state("ciao", str(exe), account, env, "fixture")
    expected = b"# original\n" + bytes.fromhex(state["posix"]["append_hex"])
    monkeypatch.setattr(shell_hints, "_posix_account", lambda: account)
    monkeypatch.setattr(shell_hints, "_probe_posix_account", lambda _: env)
    monkeypatch.setattr(users, "user_key", lambda: "fixture")
    revision = shell_hints.preview_path_change("ciao", str(exe))["revision"]
    def fail_cache() -> None:
        raise OSError("injected cache failure")
    monkeypatch.setattr(tool_path, "clear_terminal_path_cache", fail_cache)
    result = shell_hints.apply_path_change("ciao", str(exe), revision)
    assert (result["status"], result["mutation"]) == ("verification_failed", "written")
    assert profile.read_bytes() == expected


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX hardlink semantics")
@pytest.mark.parametrize("race", [False, True])
def test_posix_profile_hardlink_refused_before_append(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, race: bool) -> None:
    from ciao.os_support import shell_hints, users
    account, exe, env = _posix_fixture(tmp_path)
    profile = Path(env["HOME"]) / ".bash_profile"
    outside = tmp_path / "outside"
    profile.write_bytes(b"# untouched\n")
    monkeypatch.setattr(shell_hints, "_posix_account", lambda: account)
    monkeypatch.setattr(shell_hints, "_probe_posix_account", lambda _: env)
    monkeypatch.setattr(users, "user_key", lambda: "fixture")
    if not race:
        os.link(profile, outside)
        assert shell_hints.preview_path_change("ciao", str(exe))["status"] == "refused"
    else:
        preview = shell_hints.preview_path_change("ciao", str(exe))
        assert preview["status"] == "ready"
        real_open = os.open
        def link_before_append(path: object, flags: int, *args: object, **kwargs: object) -> int:
            if path == ".bash_profile" and flags & os.O_RDWR:
                os.link(profile, outside)
            return real_open(path, flags, *args, **kwargs)
        monkeypatch.setattr(os, "open", link_before_append)
        result = shell_hints.apply_path_change("ciao", str(exe), preview["revision"])
        assert (result["status"], result["mutation"]) == ("write_failed", "none")
    assert profile.read_bytes() == outside.read_bytes() == b"# untouched\n"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PATH semantics")
@pytest.mark.parametrize("path", ["bin", ":/usr/bin", "/usr/bin:", "/usr/bin::/bin"])
def test_posix_relative_or_empty_path_entry_refused_without_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    from ciao.os_support import shell_hints, users
    account, exe, env = _posix_fixture(tmp_path)
    alternate = Path(env["HOME"]) / "bin"
    alternate.mkdir()
    (alternate / "ciao").write_text("different target")
    (alternate / "ciao").chmod(0o700)
    env["PATH"] = path
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shell_hints, "_posix_account", lambda: account)
    monkeypatch.setattr(shell_hints, "_probe_posix_account", lambda _: env)
    monkeypatch.setattr(users, "user_key", lambda: "fixture")
    assert shell_hints.preview_path_change("ciao", str(exe))["status"] == "refused"
    result = shell_hints.apply_path_change("ciao", str(exe), "old-revision")
    assert result["mutation"] == "none"
    assert not (Path(env["HOME"]) / ".bash_profile").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX account semantics")
@pytest.mark.parametrize("failure", ["nested", "out-of-order"])
def test_posix_probe_rejects_nested_and_out_of_order_frames(tmp_path: Path, failure: str) -> None:
    from ciao.os_support.shell_hints import _probe_posix_account
    account, _, env = _posix_fixture(tmp_path)
    path = b"__ciaobot_path_probe_PATH_START__/usr/bin__ciaobot_path_probe_PATH_END__"
    home = b"__ciaobot_path_probe_HOME_START__" + os.fsencode(env["HOME"]) + b"__ciaobot_path_probe_HOME_END__"
    rest = b"__ciaobot_path_probe_ZDOTDIR_START____ciaobot_path_probe_ZDOTDIR_END____ciaobot_path_probe_XDG_START____ciaobot_path_probe_XDG_END__"
    raw = (b"__ciaobot_path_probe_PATH_START__/usr/bin" + home + b"__ciaobot_path_probe_PATH_END__" + rest
           if failure == "nested" else home + path + rest)
    with pytest.raises(ValueError):
        _probe_posix_account(account, lambda *_: raw)


@pytest.mark.parametrize("stream,size", [(1, 65537), (2, 8193)])
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX child process")
def test_posix_owned_probe_child_output_limits(tmp_path: Path, stream: int, size: int) -> None:
    from ciao.os_support.shell_hints import _PosixAccount, _run_posix_probe
    home = tmp_path / "home"
    home.mkdir()
    shell = tmp_path / "bash"
    shell.write_text(f"#!{sys.executable}\nimport os\nos.write({stream}, b'x' * {size})\n")
    shell.chmod(0o700)
    account = _PosixAccount(os.geteuid(), str(home), str(shell), "fixture")
    with pytest.raises(ValueError, match="exceeded limit"):
        _run_posix_probe(account, True, {"HOME": str(home), "LANG": "C"})


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX child process")
def test_posix_probe_nonzero_and_timeout_cleanup_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.os_support import processes
    from ciao.os_support.shell_hints import _PosixAccount, _run_posix_probe
    home = tmp_path / "home"
    home.mkdir()
    shell = tmp_path / "bash"
    shell.write_text(f"#!{sys.executable}\nimport sys\nprint('private startup output')\nsys.exit(17)\n")
    shell.chmod(0o700)
    account = _PosixAccount(os.geteuid(), str(home), str(shell), "fixture")
    with pytest.raises(ValueError, match="Shell probe failed") as error:
        _run_posix_probe(account, True, {"HOME": str(home)})
    assert "private startup output" not in str(error.value)
    shell.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    def fail_tree_kill(self: object) -> None:
        raise OSError("injected tree failure")
    monkeypatch.setattr(processes.ProcessTree, "kill", fail_tree_kill)
    original_wait = subprocess.Popen.wait
    waits: list[float | None] = []
    def bounded_wait(self: object, timeout: float | None = None) -> int:
        waits.append(timeout)
        if timeout is None:
            raise AssertionError("unbounded wait")
        return original_wait(self, timeout=timeout)
    monkeypatch.setattr(subprocess.Popen, "wait", bounded_wait)
    with pytest.raises(TimeoutError):
        _run_posix_probe(account, True, {"HOME": str(home)})
    assert waits and all(timeout is not None for timeout in waits)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX child process")
def test_posix_probe_overflow_kills_owned_descendant(tmp_path: Path) -> None:
    from ciao.os_support.shell_hints import _PosixAccount, _run_posix_probe
    home = tmp_path / "home"
    home.mkdir()
    marker = home / "descendant-survived"
    shell = tmp_path / "bash"
    shell.write_text(f"#!{sys.executable}\nimport os,time\n"
                     f"if os.fork() == 0:\n time.sleep(1); open({str(marker)!r}, 'w').close(); os._exit(0)\n"
                     "os.write(1, b'x' * 65537)\ntime.sleep(30)\n")
    shell.chmod(0o700)
    account = _PosixAccount(os.geteuid(), str(home), str(shell), "fixture")
    with pytest.raises(ValueError, match="exceeded limit"):
        _run_posix_probe(account, True, {"HOME": str(home)})
    import threading
    threading.Event().wait(1.3)
    assert not marker.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX child process")
def test_posix_probe_cleanup_failure_does_not_wait_unbounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.os_support import processes
    from ciao.os_support.shell_hints import _PosixAccount, _run_posix_probe
    home = tmp_path / "home"
    home.mkdir()
    shell = tmp_path / "bash"
    shell.write_text(f"#!{sys.executable}\nimport os,time\nos.write(1, b'x' * 65537)\ntime.sleep(30)\n")
    shell.chmod(0o700)
    account = _PosixAccount(os.geteuid(), str(home), str(shell), "fixture")
    spawned: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen
    real_kill = real_popen.kill
    real_wait = real_popen.wait
    def spawn(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        child = real_popen(*args, **kwargs)
        spawned.append(child)
        return child
    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("injected cleanup failure")
    def wait_timeout(self: object, timeout: float | None = None) -> int:
        assert timeout is not None and timeout <= 1
        raise subprocess.TimeoutExpired("fixture", timeout)
    monkeypatch.setattr(processes.ProcessTree, "kill", fail)
    monkeypatch.setattr(real_popen, "kill", fail)
    monkeypatch.setattr(real_popen, "wait", wait_timeout)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    try:
        with pytest.raises(ValueError, match="exceeded limit"):
            _run_posix_probe(account, True, {"HOME": str(home)})
    finally:
        for child in spawned:
            real_kill(child)
            real_wait(child, timeout=2)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX profile semantics")
def test_posix_preview_crlf_marker_idempotence_and_partial_refusal(tmp_path: Path) -> None:
    from ciao.os_support.shell_hints import _posix_state
    account, exe, env = _posix_fixture(tmp_path)
    profile = Path(env["HOME"]) / ".bash_profile"
    profile.write_bytes(b"export PATH=/usr/bin\r\n# existing")
    state, status, _ = _posix_state("ciao", str(exe), account, env, "fixture")
    payload = bytes.fromhex(state["posix"]["append_hex"])
    assert status == "ready" and payload.startswith(b"\n# >>>")
    assert b"\r" not in payload
    profile.write_bytes(profile.read_bytes() + payload)
    assert _posix_preview(account, exe, env)["status"] == "refused"
    env["PATH"] = str(exe.parent)
    assert _posix_preview(account, exe, env)["status"] == "already_configured"
    env["PATH"] = "/nonexistent"
    profile.write_bytes(payload.splitlines()[1][:12])
    assert _posix_preview(account, exe, env)["status"] == "refused"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell semantics")
def test_posix_lf_append_sources_in_bash_and_crlf_negative_control(tmp_path: Path) -> None:
    from ciao.os_support.shell_hints import _posix_state
    account, exe, env = _posix_fixture(tmp_path)
    profile = Path(env["HOME"]) / ".bash_profile"
    original = b"# existing comment\r\n"
    profile.write_bytes(original)
    state, status, _ = _posix_state("ciao", str(exe), account, env, "fixture")
    assert status == "ready"
    payload = bytes.fromhex(state["posix"]["append_hex"])
    profile.write_bytes(original + payload)
    args = ["/bin/bash", "--noprofile", "--norc", "-c", '. "$1"; command -v ciao', "bash", str(profile)]
    environment = {"HOME": env["HOME"], "PATH": "/usr/bin:/bin"}
    positive = subprocess.run(args, cwd=env["HOME"], env=environment, capture_output=True, check=False)
    assert positive.returncode == 0 and positive.stdout.strip() == os.fsencode(exe)
    assert profile.read_bytes() == original + payload
    profile.write_bytes(original + payload.replace(b"\n", b"\r\n"))
    negative = subprocess.run(args, cwd=env["HOME"], env=environment, capture_output=True, check=False)
    assert negative.returncode != 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX account semantics")
@pytest.mark.parametrize("failure", ["duplicate", "inverted", "missing", "utf8", "empty", "home", "relative", "outside"])
def test_posix_probe_rejects_bad_frames_and_unsafe_environment(tmp_path: Path, failure: str) -> None:
    from ciao.os_support.shell_hints import _probe_posix_account
    account, _, env = _posix_fixture(tmp_path)
    raw = ("__ciaobot_path_probe_PATH_START__/usr/bin__ciaobot_path_probe_PATH_END__"
           + "__ciaobot_path_probe_HOME_START__" + env["HOME"] + "__ciaobot_path_probe_HOME_END__"
           + "__ciaobot_path_probe_ZDOTDIR_START____ciaobot_path_probe_ZDOTDIR_END__"
           + "__ciaobot_path_probe_XDG_START____ciaobot_path_probe_XDG_END__").encode()
    if failure == "duplicate": raw += b"__ciaobot_path_probe_PATH_START__"
    if failure == "inverted": raw = raw.replace(b"__ciaobot_path_probe_PATH_START__/usr/bin__ciaobot_path_probe_PATH_END__", b"__ciaobot_path_probe_PATH_END__/usr/bin__ciaobot_path_probe_PATH_START__")
    if failure == "missing": raw = raw.replace(b"__ciaobot_path_probe_XDG_END__", b"")
    if failure == "utf8": raw += b"\xff"
    if failure == "empty": raw = raw.replace(b"/usr/bin", b"")
    if failure == "home": raw = raw.replace(os.fsencode(env["HOME"]), b"/not-the-home")
    if failure == "relative": raw = raw.replace(b"__ciaobot_path_probe_ZDOTDIR_START____ciaobot_path_probe_ZDOTDIR_END__", b"__ciaobot_path_probe_ZDOTDIR_START__relative__ciaobot_path_probe_ZDOTDIR_END__")
    if failure == "outside": raw = raw.replace(b"__ciaobot_path_probe_XDG_START____ciaobot_path_probe_XDG_END__", b"__ciaobot_path_probe_XDG_START__/outside__ciaobot_path_probe_XDG_END__")
    with pytest.raises((ValueError, UnicodeDecodeError)):
        _probe_posix_account(account, lambda *_: raw)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX profile semantics")
@pytest.mark.parametrize("kind", ["duplicate", "nested", "mismatch", "truncated", "partial", "parent-link", "parent-mode"])
def test_posix_preview_rejects_unsafe_markers_and_ancestors(tmp_path: Path, kind: str) -> None:
    from ciao.os_support.shell_hints import _PosixAccount, _posix_state
    account, exe, env = _posix_fixture(tmp_path)
    profile = Path(env["HOME"]) / ".bash_profile"
    state, _, _ = _posix_state("ciao", str(exe), account, env, "fixture")
    block = bytes.fromhex(state["posix"]["append_hex"])
    lines = block.splitlines(keepends=True)
    if kind == "duplicate": profile.write_bytes(block + block)
    if kind == "nested": profile.write_bytes(lines[0] + lines[0] + lines[1] + lines[2])
    if kind == "mismatch": profile.write_bytes(lines[0] + b"wrong command\n" + lines[2])
    if kind == "truncated": profile.write_bytes(lines[0] + lines[1])
    if kind == "partial": profile.write_bytes(lines[0][:7])
    if kind in ("parent-link", "parent-mode"):
        fish = tmp_path / "fish"
        fish.write_text("fixture")
        fish.chmod(0o700)
        account = _PosixAccount(os.geteuid(), env["HOME"], str(fish), "fixture")
        config = Path(env["HOME"]) / ".config"
        if kind == "parent-link": config.symlink_to(tmp_path, target_is_directory=True)
        else:
            config.mkdir(mode=0o700)
            config.chmod(0o777)
    before = set(Path(env["HOME"]).rglob("*"))
    assert _posix_preview(account, exe, env)["status"] == "refused"
    assert set(Path(env["HOME"]).rglob("*")) == before


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX account semantics")
@pytest.mark.parametrize("kind", ["missing", "symlink", "writable", "relative"])
def test_posix_preview_rejects_unsafe_home_without_writes(tmp_path: Path, kind: str) -> None:
    from ciao.os_support.shell_hints import _PosixAccount
    account, exe, env = _posix_fixture(tmp_path)
    home = Path(env["HOME"])
    if kind == "missing": home.rmdir()
    if kind == "writable": home.chmod(0o777)
    if kind == "symlink":
        home.rmdir()
        home.symlink_to(tmp_path, target_is_directory=True)
    if kind == "relative":
        env["HOME"] = "relative"
        account = _PosixAccount(os.geteuid(), "relative", account.shell, "fixture")
    before = set(tmp_path.iterdir())
    assert _posix_preview(account, exe, env)["status"] == "refused"
    assert set(tmp_path.iterdir()) == before


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX profile semantics")
def test_posix_preview_zsh_zdotdir_and_shell_quoting(tmp_path: Path) -> None:
    from ciao.os_support.shell_hints import _PosixAccount, _posix_state
    account, exe, env = _posix_fixture(tmp_path)
    zsh = tmp_path / "zsh"
    zsh.write_text("fixture")
    zsh.chmod(0o700)
    account = _PosixAccount(os.geteuid(), env["HOME"], str(zsh), "fixture")
    directory = tmp_path / "O'Neil bin"
    directory.mkdir()
    exe = directory / "ciao"
    exe.write_text("fixture")
    exe.chmod(0o700)
    env["ZDOTDIR"] = env["HOME"] + "/zconfig"
    state, status, _ = _posix_state("ciao", str(exe), account, env, "fixture")
    assert status == "ready"
    assert state["posix"]["destination"] == env["ZDOTDIR"] + "/.zshrc"
    assert b"'\"'\"'" in bytes.fromhex(state["posix"]["append_hex"])
    assert not Path(env["ZDOTDIR"]).exists()

_POWERSHELL_USER_PATH = (
    "$k = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', "
    "$true); $v = $k.GetValue('Path', '', 'DoNotExpandEnvironmentNames'); "
    r"$k.SetValue('Path', 'C:\Tools\bin' + $(if ($v) { ';' + $v }), "
    "[Microsoft.Win32.RegistryValueKind]::ExpandString)"
)


def test_posix_session_hint_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("SHELL", "/usr/bin/fish")

    # The short line is what `ciao setup` has always printed, and it must not
    # start looking up $SHELL now that the wizard's longer line does.
    assert path_hint("/opt/venv/bin", persist=False) == 'export PATH="/opt/venv/bin:$PATH"'


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="pins POSIX output built from POSIX paths (`$HOME/...` with `/`)",
)
def test_posix_persist_zsh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    target = str(Path.home() / ".local" / "bin")
    expected = """echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc"""

    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert path_hint(target, persist=True) == expected

    # The engine usually runs under launchd, where SHELL is unset entirely.
    monkeypatch.delenv("SHELL", raising=False)
    assert path_hint(target, persist=True) == expected


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="pins POSIX output built from POSIX paths (`$HOME/...` with `/`)",
)
def test_posix_persist_bash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("SHELL", "/bin/bash")

    line = path_hint(str(Path.home() / ".local" / "bin"), persist=True)

    assert line == (
        """echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bash_profile && source ~/.bash_profile"""
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="pins POSIX output built from POSIX paths (`$HOME/...` with `/`)",
)
def test_posix_persist_fish(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("SHELL", "/opt/homebrew/bin/fish")

    assert path_hint(str(Path.home() / ".local" / "bin"), persist=True) == (
        "fish_add_path $HOME/.local/bin"
    )


def test_posix_persist_outside_home_is_not_collapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("SHELL", "/bin/zsh")

    # $HOME is only a placeholder the shell expands; outside it there is
    # nothing to collapse to.
    assert path_hint("/opt/tools/bin", persist=True) == (
        """echo 'export PATH="/opt/tools/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc"""
    )


def test_windows_line_is_a_persistent_user_path_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("SHELL", "/bin/zsh")

    # `export PATH=...` was the bug: it is POSIX syntax and does nothing in
    # PowerShell or cmd.
    for persist in (False, True):
        line = path_hint(r"C:\Tools\bin", persist=persist)
        assert line == _POWERSHELL_USER_PATH
        assert "export" not in line
        assert "$PATH" not in line


def test_windows_hint_never_uses_the_env_setter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    # [Environment]::SetEnvironmentVariable reads the old value expanded and
    # writes it back as REG_SZ, which would freeze every %VAR% in a
    # REG_EXPAND_SZ user PATH and change its type under every other tool.
    for persist in (False, True):
        line = path_hint(r"C:\Tools\bin", persist=persist)
        assert "SetEnvironmentVariable" not in line
        assert "GetEnvironmentVariable" not in line
        assert "DoNotExpandEnvironmentNames" in line
        assert "RegistryValueKind]::ExpandString" in line


def test_windows_single_quote_in_directory_is_doubled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    line = path_hint(r"C:\Users\O'Neil\bin", persist=True)

    # An unescaped ' would end the literal and swallow the rest of the path.
    assert r"'C:\Users\O''Neil\bin' + $(if ($v) { ';' + $v })" in line
    assert line == _POWERSHELL_USER_PATH.replace(
        r"'C:\Tools\bin'", r"'C:\Users\O''Neil\bin'"
    )


def test_note(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert path_hint_note() == "Open a new terminal for the change to take effect."

    monkeypatch.setattr(sys, "platform", "darwin")
    assert path_hint_note() == ""
