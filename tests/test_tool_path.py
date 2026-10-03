"""ciao.tool_path merges the terminal PATH with the process one.

The platform half — what "the terminal's PATH" is, the login-shell probe on
POSIX, the registry read on Windows, PATHEXT and the npm shims — is
``ciao.os_support.tool_path`` and is tested in ``tests/test_os_support_tool_path.py``.
What is left here is the merge and the two lookups built on it, and those are
the same on every OS.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from ciao import tool_path


def _clear_cache():
    # `login_shell_path` memoizes for the process lifetime with lru_cache;
    # the terminal PATH keeps its own cache behind a module-level clear.
    clear = getattr(tool_path.login_shell_path, "cache_clear", None)
    if clear is not None:
        clear()
    tool_path.clear_terminal_path_cache()


def _tool_name(name: str) -> str:
    """The file a PATH search on this OS would find for a bare ``name``."""
    return f"{name}.exe" if sys.platform == "win32" else name


def _same_file(found: str, expected: Path) -> bool:
    """A resolved path equals the file, whatever case its suffix came back in."""
    return os.path.normcase(found) == os.path.normcase(str(expected))


def test_resolve_tool_finds_binary_on_login_shell_path(tmp_path, monkeypatch):
    _clear_cache()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / _tool_name("gws")
    fake.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setattr(tool_path, "login_shell_path", lambda: str(bin_dir))
    found = tool_path.resolve_tool("gws")
    assert found is not None and _same_file(found, fake)
    assert tool_path.resolve_tool("definitely-not-a-real-tool") is None


def test_login_shell_path_merges_shell_and_current_path_deduped(tmp_path, monkeypatch):
    _clear_cache()
    shell_dir = tmp_path / "shelldir"
    cur_dir = tmp_path / "curdir"
    shell_dir.mkdir()
    cur_dir.mkdir()
    monkeypatch.setattr(tool_path, "common_tool_dirs", lambda: [])
    # The terminal reports shell_dir plus cur_dir (a duplicate of the current PATH).
    monkeypatch.setattr(
        tool_path, "terminal_path", lambda: f"{shell_dir}{os.pathsep}{cur_dir}"
    )
    monkeypatch.setenv("PATH", str(cur_dir))

    result = tool_path.login_shell_path().split(os.pathsep)
    # Terminal PATH entries come first, current PATH merged, no duplicates.
    assert result.count(str(cur_dir)) == 1
    assert str(shell_dir) in result
    assert result.index(str(shell_dir)) < result.index(str(cur_dir))
    _clear_cache()


def test_login_shell_path_survives_shell_probe_failure(monkeypatch):
    _clear_cache()
    monkeypatch.setattr(tool_path, "terminal_path", lambda: "")
    monkeypatch.setattr(tool_path, "common_tool_dirs", lambda: [])
    monkeypatch.setenv("PATH", "/usr/bin")
    result = tool_path.login_shell_path()
    assert "/usr/bin" in result.split(os.pathsep)
    _clear_cache()


def test_login_shell_path_appends_the_tool_dirs_that_exist(tmp_path, monkeypatch):
    _clear_cache()
    present = tmp_path / "present"
    present.mkdir()
    absent = tmp_path / "absent"
    monkeypatch.setattr(tool_path, "terminal_path", lambda: "")
    monkeypatch.setattr(tool_path, "common_tool_dirs", lambda: [str(absent), str(present)])
    monkeypatch.setenv("PATH", "")

    result = tool_path.login_shell_path().split(os.pathsep)
    assert result == [str(present)]
    _clear_cache()


def test_resolve_on_terminal_path_ignores_the_fallback_dirs(tmp_path, monkeypatch):
    """`resolve_tool` searches ~/.local/bin and Homebrew whether or not the
    user's terminal has them; the terminal-only lookup must not."""
    _clear_cache()
    fallback = tmp_path / "local-bin"
    fallback.mkdir()
    fake = fallback / _tool_name("claude")
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setattr(tool_path, "common_tool_dirs", lambda: [str(fallback)])
    monkeypatch.setattr(tool_path, "terminal_path", lambda: "/usr/bin:/bin")
    # Keep the host's own PATH (which may hold a real `claude`) out of it.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    assert tool_path.resolve_on_terminal_path("claude") is None
    found = tool_path.resolve_tool("claude")
    assert found is not None and _same_file(found, fake)
    _clear_cache()


def test_resolve_on_terminal_path_returns_none_when_the_probe_fails(monkeypatch):
    _clear_cache()
    monkeypatch.setattr(tool_path, "terminal_path", lambda: "")
    assert tool_path.resolve_on_terminal_path("sh") is None
    _clear_cache()