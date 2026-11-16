"""The OpenCode server is stopped as a process tree, never as one process (#803)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from ciao.providers import opencode


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
