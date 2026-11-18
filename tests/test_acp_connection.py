"""ACP connection layer: spawn, initialize, stderr and tree shutdown (#3137)."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import acp
import pytest

from ciao.os_support.tool_path import engine_bin_dir
from ciao.providers.acp import connection as connection_module
from ciao.providers.acp.connection import AcpConnection, AgentStartError, StderrRing
from ciao.providers.acp.spec import AgentSpec
from tests.acp_helpers import read_calls, write_scenario


class _Client(acp.Client):
    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        await asyncio.sleep(0.05)
    return not _pid_alive(pid)


async def _open(spec: AgentSpec, tmp_path: Path, env: dict[str, str] | None = None) -> AcpConnection:
    return await AcpConnection.open(spec, cwd=tmp_path, env=env or {}, client=_Client())


@pytest.mark.asyncio
async def test_open_initializes_with_protocol_version_and_raw_meta(tmp_path: Path) -> None:
    meta = {"opencode/child-session-updates": True}
    spec, calls = write_scenario(
        tmp_path, initialize={"loadSession": True}, spec_client_meta=meta
    )
    connection = await _open(spec, tmp_path)
    try:
        assert connection.alive
        assert connection.agent_capabilities["loadSession"] is True
    finally:
        await connection.close()
    init = [c for c in read_calls(calls) if c["method"] == "initialize"]
    assert len(init) == 1
    params = init[0]["params"]
    assert params["protocolVersion"] == 1
    assert params["clientCapabilities"]["_meta"] == meta
    assert params["clientCapabilities"]["fs"] == {"readTextFile": False, "writeTextFile": False}
    assert params["clientCapabilities"]["terminal"] is False
    assert params["clientInfo"]["name"] == "ciaobot"


@pytest.mark.asyncio
async def test_open_missing_binary_raises_agent_start_error(tmp_path: Path) -> None:
    spec = AgentSpec(id="fake", binary="ciao-no-such-agent-binary")
    with pytest.raises(AgentStartError, match="ciao-no-such-agent-binary was not found on PATH."):
        await _open(spec, tmp_path)


@pytest.mark.asyncio
async def test_initialize_timeout_closes_process_and_reports_stderr(tmp_path: Path) -> None:
    spec, _calls = write_scenario(
        tmp_path,
        initialize_delay_ms=30000,
        stderr=["warming up the fake agent"],
        spawn_child=True,
        spec_initialize_timeout_s=4.0,
    )
    with pytest.raises(AgentStartError) as excinfo:
        await _open(spec, tmp_path)
    assert str(excinfo.value).startswith("fake agent did not start: ")
    assert "warming up the fake agent" in excinfo.value.stderr_tail
    pids = json.loads((tmp_path / "scenario.json.pids.json").read_text(encoding="utf-8"))
    assert await _wait_gone(pids["pid"])
    assert await _wait_gone(pids["child"])


@pytest.mark.asyncio
async def test_exit_during_initialize_raises_with_stderr_tail(tmp_path: Path) -> None:
    spec, _calls = write_scenario(
        tmp_path, exit_during_initialize=True, stderr=["fatal: no login"],
    )
    started = time.monotonic()
    with pytest.raises(AgentStartError) as excinfo:
        await _open(spec, tmp_path)
    # The exit is noticed at once, not after the initialize timeout.
    assert time.monotonic() - started < spec.initialize_timeout_s / 2
    assert "exited" in str(excinfo.value)
    assert "fatal: no login" in excinfo.value.stderr_tail


@pytest.mark.asyncio
async def test_close_is_idempotent_and_kills_tree(tmp_path: Path) -> None:
    spec, _calls = write_scenario(tmp_path, spawn_child=True)
    connection = await _open(spec, tmp_path)
    pids = json.loads((tmp_path / "scenario.json.pids.json").read_text(encoding="utf-8"))
    assert _pid_alive(pids["pid"]) and _pid_alive(pids["child"])
    await connection.close()
    await connection.close()
    assert not connection.alive
    assert await _wait_gone(pids["pid"])
    assert await _wait_gone(pids["child"])


@pytest.mark.asyncio
async def test_close_kills_an_agent_that_ignores_sigterm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(connection_module, "_SHUTDOWN_TIMEOUT", 0.5)
    spec, _calls = write_scenario(tmp_path, spawn_child=True, ignore_sigterm=True)
    connection = await _open(spec, tmp_path)
    pids = json.loads((tmp_path / "scenario.json.pids.json").read_text(encoding="utf-8"))
    await connection.close()
    assert await _wait_gone(pids["pid"])
    # The fake ignores SIGTERM, so it never reaches its child; clean the child up.
    os.kill(pids["child"], 9)


def test_stderr_ring_bounds_lines_and_bytes() -> None:
    ring = StderrRing(max_lines=3, max_bytes=1024)
    for index in range(10):
        ring.feed(f"line {index}")
    assert ring.tail(100) == "line 7\nline 8\nline 9"
    assert ring.tail(2) == "line 8\nline 9"

    sized = StderrRing(max_lines=100, max_bytes=20)
    for index in range(10):
        sized.feed(f"{index}" * 8)
    kept = sized.tail(100).split("\n")
    assert sum(len(line) for line in kept) <= 20
    assert kept[-1] == "9" * 8


@pytest.mark.asyncio
async def test_env_has_engine_path_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    spec, calls = write_scenario(tmp_path, record_env=["PATH", "PWA_AUTH_TOKEN", "CIAO_X", "EXTRA"])
    monkeypatch.setenv("PWA_AUTH_TOKEN", "secret")
    monkeypatch.setenv("CIAO_X", "1")
    connection = await _open(spec, tmp_path, env={"EXTRA": "yes"})
    await connection.close()
    env = next(c for c in read_calls(calls) if c["method"] == "initialize")["params"]["env"]
    assert env["PATH"].split(os.pathsep)[0] == engine_bin_dir()
    assert env["EXTRA"] == "yes"
    assert env["PWA_AUTH_TOKEN"] is None
    assert env["CIAO_X"] is None
