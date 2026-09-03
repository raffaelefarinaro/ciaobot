from __future__ import annotations

import os
import stat

from ciao import tool_path


def _clear_cache():
    # Tolerant of monkeypatched replacements, which carry no cache.
    for name in ("login_shell_path", "terminal_path"):
        clear = getattr(getattr(tool_path, name), "cache_clear", None)
        if clear is not None:
            clear()


def test_resolve_tool_finds_binary_on_login_shell_path(tmp_path, monkeypatch):
    _clear_cache()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "gws"
    fake.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setattr(tool_path, "login_shell_path", lambda: str(bin_dir))
    assert tool_path.resolve_tool("gws") == str(fake)
    assert tool_path.resolve_tool("definitely-not-a-real-tool") is None


def test_login_shell_path_merges_shell_and_current_path_deduped(tmp_path, monkeypatch):
    _clear_cache()
    shell_dir = tmp_path / "shelldir"
    cur_dir = tmp_path / "curdir"
    shell_dir.mkdir()
    cur_dir.mkdir()

    class _Result:
        returncode = 0
        # Shell reports shell_dir plus cur_dir (a duplicate of the current PATH).
        stdout = f"{tool_path._START}{shell_dir}{os.pathsep}{cur_dir}{tool_path._END}"
        stderr = ""

    monkeypatch.setattr(tool_path.subprocess, "run", lambda *a, **k: _Result())
    monkeypatch.setenv("PATH", str(cur_dir))

    result = tool_path.login_shell_path().split(os.pathsep)
    # Shell PATH entries come first, current PATH merged, no duplicates.
    assert result.count(str(cur_dir)) == 1
    assert str(shell_dir) in result
    assert result.index(str(shell_dir)) < result.index(str(cur_dir))
    _clear_cache()


def test_login_shell_path_survives_shell_probe_failure(monkeypatch):
    _clear_cache()

    def boom(*a, **k):
        raise OSError("no shell")

    monkeypatch.setattr(tool_path.subprocess, "run", boom)
    monkeypatch.setenv("PATH", "/usr/bin")
    result = tool_path.login_shell_path()
    assert "/usr/bin" in result.split(os.pathsep)
    _clear_cache()


def test_terminal_path_does_not_inherit_our_own_path(tmp_path, monkeypatch):
    """The engine prepends common_tool_dirs() to its own PATH at startup
    (ciao/main.py). Handing that to the probe would make ~/.local/bin come
    back as if the user's terminal had it, and setup would then tell them to
    type a bare command their shell cannot resolve."""
    _clear_cache()
    captured: dict[str, object] = {}

    class _Result:
        returncode = 0
        stdout = f"{tool_path._START}/usr/bin{tool_path._END}"
        stderr = ""

    def fake_run(*args, **kwargs):
        captured["env"] = kwargs.get("env")
        return _Result()

    monkeypatch.setattr(tool_path.subprocess, "run", fake_run)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    assert tool_path.terminal_path() == "/usr/bin"
    env = captured["env"]
    assert isinstance(env, dict)
    assert "PATH" not in env
    # Everything else the rc files may need is still there.
    assert env["HOME"] == str(tmp_path)
    _clear_cache()


def test_resolve_on_terminal_path_ignores_the_fallback_dirs(tmp_path, monkeypatch):
    """`resolve_tool` searches ~/.local/bin and Homebrew whether or not the
    user's shell has them; the terminal-only lookup must not."""
    _clear_cache()
    fallback = tmp_path / "local-bin"
    fallback.mkdir()
    fake = fallback / "claude"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setattr(tool_path, "common_tool_dirs", lambda: [str(fallback)])
    monkeypatch.setattr(tool_path, "terminal_path", lambda: "/usr/bin:/bin")
    # Keep the host's own PATH (which may hold a real `claude`) out of it.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    assert tool_path.resolve_on_terminal_path("claude") is None
    assert tool_path.resolve_tool("claude") == str(fake)
    _clear_cache()


def test_resolve_on_terminal_path_returns_none_when_the_probe_fails(monkeypatch):
    _clear_cache()
    monkeypatch.setattr(tool_path, "terminal_path", lambda: "")
    assert tool_path.resolve_on_terminal_path("sh") is None
    _clear_cache()
