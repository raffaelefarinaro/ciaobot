"""The OpenCode server is stopped as a process tree, never as one process.

On Windows the process `resolve_opencode_binary()` hands us is the npm shim
(`opencode.CMD`, so `cmd.exe`), and the real `opencode.exe` is its child; on
every OS the server starts MCP servers of its own. Stopping only the spawned
process left `opencode serve` running, one per stop. A Python leader that
forks a heartbeat grandchild stands in for the shim and its server, so no test
starts a real `opencode serve` (#803).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from ciao.providers import opencode
from tests.test_os_support_processes import _beats_stopped, _leader, _wait_for_beat


@pytest.mark.asyncio
async def test_stopping_a_server_ends_its_whole_tree(tmp_path: Path) -> None:
    beat = tmp_path / "beat"
    process, tree = await opencode._spawn_server(
        *_leader(beat, then="time.sleep(300)"),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await asyncio.to_thread(_wait_for_beat, beat)
    await opencode._stop_server(process, tree)
    assert process.returncode is not None
    assert await asyncio.to_thread(_beats_stopped, beat), "the server's child survived the stop"


@pytest.mark.asyncio
async def test_a_server_that_already_exited_is_left_alone(tmp_path: Path) -> None:
    process, tree = await opencode._spawn_server(
        sys.executable,
        "-c",
        "pass",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await process.wait()
    await opencode._stop_server(process, tree)  # must not raise
    assert process.returncode == 0
