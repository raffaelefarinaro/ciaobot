from __future__ import annotations

import os
import stat

from ciao import tool_path


def _clear_cache():
    tool_path.login_shell_path.cache_clear()


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
