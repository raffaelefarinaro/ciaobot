"""The platform terminal runs a given program when it is handed an argv."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from ciao.os_support.terminal import spawn_terminal

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX pty")


def test_spawn_runs_the_given_program(tmp_path: Path) -> None:
    terminal = spawn_terminal(cwd=tmp_path, argv=["/bin/echo", "helper-ok"])
    try:
        output = b""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            chunk = terminal.read()
            if chunk is None:
                time.sleep(0.05)
                continue
            if chunk == b"":
                break
            output += chunk
        assert b"helper-ok" in output
        deadline = time.monotonic() + 10
        while terminal.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert terminal.poll() == 0
    finally:
        terminal.close()


def test_spawn_refuses_a_relative_program(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        spawn_terminal(cwd=tmp_path, argv=["echo", "x"])


def test_spawn_without_argv_still_starts_the_shell(tmp_path: Path) -> None:
    terminal = spawn_terminal(cwd=tmp_path)
    terminal.close()
