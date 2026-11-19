"""ACP connection layer: spawn, initialize, stderr and tree shutdown (#3137)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import sys
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


_TREE_KEYS = ("pid", "child", "grandchild", "great")


def _tree_pids(tmp_path: Path) -> dict[str, int]:
    return json.loads((tmp_path / "scenario.json.pids.json").read_text(encoding="utf-8"))


async def _wait_for_pids(tmp_path: Path, timeout: float = 10.0) -> dict[str, int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return _tree_pids(tmp_path)
        except (OSError, ValueError):
            await asyncio.sleep(0.05)
    raise AssertionError("the fake agent never wrote its pids")


async def _assert_tree_gone(pids: dict[str, int]) -> None:
    for key in _TREE_KEYS:
        assert await _wait_gone(pids[key]), f"{key} ({pids[key]}) survived"


def _reap_leftovers(pids: dict[str, int]) -> None:
    for pid in pids.values():
        try:
            os.kill(pid, 9)
        except OSError:
            pass


@pytest.fixture(autouse=True)
def _fast_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connection_module, "_SHUTDOWN_TIMEOUT", 0.5)


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
        spawn_tree=True,
        spec_initialize_timeout_s=5.0,
    )
    pids: dict[str, int] = {}
    try:
        with pytest.raises(AgentStartError) as excinfo:
            await _open(spec, tmp_path)
        pids = _tree_pids(tmp_path)
        assert str(excinfo.value).startswith("fake agent did not start: ")
        assert "warming up the fake agent" in excinfo.value.stderr_tail
        await _assert_tree_gone(pids)
    finally:
        _reap_leftovers(pids)


@pytest.mark.asyncio
async def test_stderr_lines_are_redacted_in_the_debug_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    spec, _calls = write_scenario(tmp_path, stderr=["loading access_token=abc123secret now"])
    caplog.set_level(logging.DEBUG, logger="ciao.providers.acp.fake")
    conn = await _open(spec, tmp_path)
    try:
        deadline = time.monotonic() + 5.0
        while "loading" not in conn.stderr_tail():
            assert time.monotonic() < deadline, "the stderr line never arrived"
            await asyncio.sleep(0.02)
    finally:
        await conn.close()
    assert "abc123secret" not in caplog.text
    assert "access_token=[redacted]" in caplog.text
    # The ring keeps the raw line for the start-failure log; only the log line is redacted.
    assert "abc123secret" in conn.stderr_tail()


@pytest.mark.asyncio
async def test_exit_during_initialize_raises_with_stderr_tail_and_kills_orphans(tmp_path: Path) -> None:
    # The agent dies and leaves its tree re-parented to pid 1; the watch still knows it.
    spec, _calls = write_scenario(
        tmp_path, exit_during_initialize=True, initialize_delay_ms=1000,
        stderr=["fatal: no login"], spawn_tree=True,
    )
    started = time.monotonic()
    pids: dict[str, int] = {}
    try:
        with pytest.raises(AgentStartError) as excinfo:
            await _open(spec, tmp_path)
        pids = _tree_pids(tmp_path)
        # The exit is noticed at once, not after the initialize timeout.
        assert time.monotonic() - started < spec.initialize_timeout_s / 2
        assert "exited" in str(excinfo.value)
        assert "fatal: no login" in excinfo.value.stderr_tail
        await _assert_tree_gone(pids)
    finally:
        _reap_leftovers(pids)


@pytest.mark.asyncio
async def test_exit_during_initialize_notices_child_while_grandchild_holds_stdio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The parent's exit is visible while a descendant still holds its stdio.

    ``spawn_tree`` inherits fds 0, 1 and 2. Python 3.12's ``Process.wait``,
    which Linux CI runs, does not finish until those pipes disconnect, so this
    patches ``wait`` to that behaviour on every OS. Noticing the exit has to
    happen before the pipes close, and the orphaned tree is still killed.
    """

    async def wait_until_stdio_closes(self: asyncio.subprocess.Process) -> int:
        transport = self._transport
        while True:
            code = transport.get_returncode()
            pipes_closed = all(
                pipe is not None and pipe.disconnected for pipe in transport._pipes.values()
            )
            if code is not None and pipes_closed:
                return code
            await asyncio.sleep(0.05)

    held_stdio = False
    real_wait = connection_module._wait_child_exit

    async def watch(process: asyncio.subprocess.Process) -> int:
        nonlocal held_stdio
        code = await real_wait(process)
        held_stdio = any(
            pipe is not None and not pipe.disconnected
            for pipe in process._transport._pipes.values()
        )
        return code

    monkeypatch.setattr(asyncio.subprocess.Process, "wait", wait_until_stdio_closes)
    monkeypatch.setattr(connection_module, "_wait_child_exit", watch)
    spec, _calls = write_scenario(
        tmp_path, exit_during_initialize=True, initialize_delay_ms=1000,
        stderr=["fatal: no login"], spawn_tree=True,
    )
    started = time.monotonic()
    pids: dict[str, int] = {}
    try:
        with pytest.raises(AgentStartError) as excinfo:
            await _open(spec, tmp_path)
        pids = _tree_pids(tmp_path)
        assert time.monotonic() - started < spec.initialize_timeout_s / 2
        assert held_stdio
        assert "exited" in str(excinfo.value)
        assert "fatal: no login" in excinfo.value.stderr_tail
        await _assert_tree_gone(pids)
    finally:
        _reap_leftovers(pids)


@pytest.mark.asyncio
async def test_exit_during_initialize_without_children(tmp_path: Path) -> None:
    spec, _calls = write_scenario(tmp_path, exit_during_initialize=True, stderr=["fatal: no login"])
    started = time.monotonic()
    with pytest.raises(AgentStartError) as excinfo:
        await _open(spec, tmp_path)
    assert time.monotonic() - started < spec.initialize_timeout_s / 2
    assert "fatal: no login" in excinfo.value.stderr_tail


@pytest.mark.asyncio
async def test_cancelled_open_kills_whole_tree(tmp_path: Path) -> None:
    spec, _calls = write_scenario(tmp_path, initialize_delay_ms=30000, spawn_tree=True)
    pids: dict[str, int] = {}
    try:
        task = asyncio.create_task(_open(spec, tmp_path))
        pids = await _wait_for_pids(tmp_path)
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _assert_tree_gone(pids)
    finally:
        _reap_leftovers(pids)


@pytest.mark.asyncio
async def test_close_kills_whole_descendant_tree_and_is_idempotent(tmp_path: Path) -> None:
    spec, _calls = write_scenario(tmp_path, spawn_tree=True)
    connection = await _open(spec, tmp_path)
    pids = _tree_pids(tmp_path)
    try:
        assert all(_pid_alive(pid) for pid in pids.values())
        await connection.close()
        await connection.close()
        assert not connection.alive
        await _assert_tree_gone(pids)
    finally:
        _reap_leftovers(pids)


@pytest.mark.asyncio
async def test_close_kills_tree_of_an_agent_that_ignores_sigterm(tmp_path: Path) -> None:
    spec, _calls = write_scenario(tmp_path, spawn_tree=True, ignore_sigterm=True)
    connection = await _open(spec, tmp_path)
    pids = _tree_pids(tmp_path)
    try:
        await connection.close()
        await _assert_tree_gone(pids)
    finally:
        _reap_leftovers(pids)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_after", [0.0, 0.2])
async def test_cancelled_close_still_kills_tree_and_second_close_finishes(
    tmp_path: Path, cancel_after: float
) -> None:
    spec, _calls = write_scenario(tmp_path, spawn_tree=True)
    connection = await _open(spec, tmp_path)
    pids = _tree_pids(tmp_path)
    try:
        task = asyncio.create_task(connection.close())
        await asyncio.sleep(cancel_after)
        task.cancel()
        # The cancel may land before or after the conn.close step finishes.
        await asyncio.gather(task, return_exceptions=True)
        await connection.close()
        assert connection._closed
        assert connection._stderr_task.done()
        await _assert_tree_gone(pids)
    finally:
        _reap_leftovers(pids)


@pytest.mark.asyncio
async def test_cancelled_initialize_request_is_an_agent_start_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The SDK cancels the pending response future when it tears the connection down;
    # the real agent process and the real _initialize run, only the request future is cancelled.
    spec, _calls = write_scenario(tmp_path, initialize_delay_ms=30000, spec_initialize_timeout_s=10.0)

    async def cancelled(self: acp.ClientSideConnection, *args: Any, **kwargs: Any) -> Any:
        raise asyncio.CancelledError

    monkeypatch.setattr(acp.ClientSideConnection, "initialize", cancelled)
    with pytest.raises(AgentStartError, match="initialize request was cancelled"):
        await _open(spec, tmp_path)


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


@pytest.mark.asyncio
async def test_cwd_reaches_the_child(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    spec, calls = write_scenario(tmp_path)
    connection = await AcpConnection.open(spec, cwd=workdir, env={}, client=_Client())
    await connection.close()
    params = next(c for c in read_calls(calls) if c["method"] == "initialize")["params"]
    assert Path(params["cwd"]).resolve() == workdir.resolve()


@pytest.mark.asyncio
async def test_env_layers_spec_over_terminal_and_argument_over_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACP_TEST_LAYER", "engine")
    monkeypatch.setenv("ACP_TEST_ARG", "engine")
    spec, calls = write_scenario(
        tmp_path,
        record_env=["ACP_TEST_LAYER", "ACP_TEST_ARG"],
        spec_env={"ACP_TEST_LAYER": "spec", "ACP_TEST_ARG": "spec"},
    )
    connection = await _open(spec, tmp_path, env={"ACP_TEST_ARG": "argument"})
    await connection.close()
    env = next(c for c in read_calls(calls) if c["method"] == "initialize")["params"]["env"]
    assert env["ACP_TEST_LAYER"] == "spec"
    assert env["ACP_TEST_ARG"] == "argument"


@pytest.mark.asyncio
async def test_relative_path_entry_cannot_resolve_binary_to_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.os_support.tool_path import resolve_executable

    marker = tmp_path / "marker"
    script = tmp_path / "relagent"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.chdir(tmp_path)
    assert resolve_executable("relagent", path=".") is not None
    assert not os.path.isabs(resolve_executable("relagent", path=".") or "")
    monkeypatch.setattr(connection_module, "resolve_tool", lambda cmd: resolve_executable(cmd, path="."))
    with pytest.raises(AgentStartError, match="relative path"):
        await _open(AgentSpec(id="fake", binary="relagent"), tmp_path)
    await asyncio.sleep(0.3)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_agent_stays_in_the_engine_process_group(tmp_path: Path) -> None:
    spec, calls = write_scenario(tmp_path)
    connection = await _open(spec, tmp_path)
    try:
        params = next(c for c in read_calls(calls) if c["method"] == "initialize")["params"]
        assert params["pid"] != os.getpid()
        assert params["pgid"] == os.getpgrp()
        assert params["sid"] == os.getsid(0)
    finally:
        await connection.close()
