"""ACP connection layer: spawn, initialize, stderr and tree shutdown (#3137)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import acp
import pytest

from ciao.models import AgentRequest, ResultEvent
from ciao.os_support.tool_path import engine_bin_dir
from ciao.providers.acp import connection as connection_module
from ciao.providers.acp.agents import OPENCODE, OpencodeAcpProvider
from ciao.providers.acp.connection import AcpConnection, AgentStartError, AgentVersionError, StderrRing
from ciao.providers.acp.spec import AgentSpec
from ciao.opencode_cli import OPENCODE_V2_REQUIRED, cached_opencode_version
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


# -- the OpenCode version floor at start (the gate in AgentSpec.version_gate) -------------------

_OPENCODE_FLOOR_REFUSED = ("2.0.16", "2.3.0", "2.3.1")
_OPENCODE_FLOOR_ACCEPTED = ("2.3.2", "2.4.0")


def test_opencode_floor_text_names_the_floor_version() -> None:
    assert OPENCODE_V2_REQUIRED.startswith("Ciaobot requires OpenCode 2.3.2 or newer.")
    assert "install OpenCode 2.3.2 or newer (" in OPENCODE_V2_REQUIRED
    # A bare "OpenCode 2" with no version is the wording the floor no longer uses.
    assert re.search(r"OpenCode 2(?![\d.])", OPENCODE_V2_REQUIRED) is None


def _opencode_on_fake(tmp_path: Path, **scenario: Any) -> tuple[AgentSpec, Path]:
    """The real OpenCode spec (its version gate included), pointed at the scripted agent."""
    spec, calls = write_scenario(tmp_path, **scenario)
    return dataclasses.replace(OPENCODE, binary=spec.binary, args=spec.args), calls


@pytest.mark.asyncio
@pytest.mark.parametrize("version", _OPENCODE_FLOOR_REFUSED)
async def test_version_below_the_floor_is_refused_at_start(tmp_path: Path, version: str) -> None:
    spec, _calls = _opencode_on_fake(tmp_path, agent_info={"name": "opencode", "version": version})
    with pytest.raises(AgentVersionError) as refused:
        await _open(spec, tmp_path)
    # The user-visible text is the constant alone: no version, no adapter text.
    assert str(refused.value) == OPENCODE_V2_REQUIRED
    assert version not in str(refused.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("version", _OPENCODE_FLOOR_ACCEPTED)
async def test_version_at_or_above_the_floor_starts(tmp_path: Path, version: str) -> None:
    spec, _calls = _opencode_on_fake(tmp_path, agent_info={"name": "opencode", "version": version})
    connection = await _open(spec, tmp_path)
    try:
        assert connection.alive
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_absent_agent_version_is_probed_from_the_binary(tmp_path: Path) -> None:
    probed: list[str] = []

    def probe(binary: str) -> str:
        probed.append(binary)
        return "2.0.16"

    spec, _calls = _opencode_on_fake(tmp_path)  # no agent_info: the agent reports no version
    gate = dataclasses.replace(OPENCODE.version_gate, probe=probe)
    with pytest.raises(AgentVersionError) as refused:
        await _open(dataclasses.replace(spec, version_gate=gate), tmp_path)
    assert str(refused.value) == OPENCODE_V2_REQUIRED
    assert probed == [spec.binary]


@pytest.mark.asyncio
async def test_absent_agent_version_probe_at_the_floor_starts(tmp_path: Path) -> None:
    spec, _calls = _opencode_on_fake(tmp_path)
    gate = dataclasses.replace(OPENCODE.version_gate, probe=lambda _binary: "2.3.2")
    connection = await _open(dataclasses.replace(spec, version_gate=gate), tmp_path)
    await connection.close()


def test_cached_version_probe_is_keyed_by_binary_path_and_mtime(tmp_path: Path) -> None:
    script = tmp_path / "opencode"
    count = tmp_path / "runs.txt"
    script.write_text(
        f"#!/bin/sh\necho run >> '{count}'\necho 'opencode v2.3.2'\n", encoding="utf-8"
    )
    script.chmod(0o755)
    assert cached_opencode_version(str(script)) == "opencode v2.3.2"
    assert cached_opencode_version(str(script)) == "opencode v2.3.2"
    assert count.read_text(encoding="utf-8").count("run") == 1  # the second call is cached
    # A new mtime (an upgrade in place) probes again.
    os.utime(script, (time.time() + 60, time.time() + 60))
    cached_opencode_version(str(script))
    assert count.read_text(encoding="utf-8").count("run") == 2


def test_failed_version_probe_is_not_cached(tmp_path: Path) -> None:
    missing = str(tmp_path / "no-such-opencode")
    assert cached_opencode_version(missing) == ""
    assert cached_opencode_version(missing) == ""


def test_failed_version_probe_of_a_present_binary_is_cached_briefly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ciao.opencode_cli as opencode_module

    script = tmp_path / "opencode"
    count = tmp_path / "runs.txt"
    script.write_text(f"#!/bin/sh\necho run >> '{count}'\nexit 1\n", encoding="utf-8")
    script.chmod(0o755)
    # A broken binary costs one probe per failure window, not one per chat start.
    assert cached_opencode_version(str(script)) == ""
    assert cached_opencode_version(str(script)) == ""
    assert count.read_text(encoding="utf-8").count("run") == 1
    # Once the negative entry is older than its window, the binary is probed again.
    monkeypatch.setattr(opencode_module, "_VERSION_FAILURE_TTL", 0.0)
    assert cached_opencode_version(str(script)) == ""
    assert count.read_text(encoding="utf-8").count("run") == 2


def test_version_probe_runs_with_the_agent_env_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    import ciao.opencode_cli as opencode_module

    dump = tmp_path / "env.txt"
    script = tmp_path / "opencode"
    script.write_text(f"#!/bin/sh\nenv > '{dump}'\necho 'opencode v2.3.2'\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("CIAO_PROBE_SECRET", "engine-internal")
    monkeypatch.setenv("PWA_AUTH_TOKEN", "pwa-token")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    seen: dict[str, Any] = {}
    real_run = subprocess.run

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    assert opencode_module.opencode_version_line(str(script)) == "opencode v2.3.2"
    env = dict(
        line.split("=", 1) for line in dump.read_text(encoding="utf-8").splitlines() if "=" in line
    )
    assert "CIAO_PROBE_SECRET" not in env
    assert "PWA_AUTH_TOKEN" not in env
    # The engine's bin dir leads PATH, so a `#!/usr/bin/env node` shim finds the engine's node.
    assert env["PATH"].split(os.pathsep)[0] == engine_bin_dir()
    assert "/usr/bin" in env["PATH"].split(os.pathsep)
    assert seen["stdin"] is subprocess.DEVNULL


@pytest.mark.asyncio
async def test_refused_start_yields_the_fixed_version_result(tmp_path: Path) -> None:
    spec, _calls = _opencode_on_fake(tmp_path, agent_info={"name": "opencode", "version": "2.0.16"})

    class OpencodeOnFake(OpencodeAcpProvider):
        pass

    OpencodeOnFake.spec = spec
    provider = OpencodeOnFake(tmp_path, config=SimpleNamespace(state_path=tmp_path / "state.json"))
    request = AgentRequest(prompt="hi", model="", mode="auto", provider="opencode")
    events = [event async for event in provider.run_streaming(request, lambda _handle: None)]
    assert len(events) == 1
    result = events[0]
    assert isinstance(result, ResultEvent)
    assert (result.is_error, result.result) == (True, OPENCODE_V2_REQUIRED)


# -- unprefixed extension notifications through the real SDK router (opencode child updates) ----


def _child_line(status: str) -> bytes:
    message = {
        "jsonrpc": "2.0",
        "method": "opencode/session/child_update",
        "params": {
            "rootSessionId": "ses_root", "childSessionId": "ses_child", "parentSessionId": "ses_root",
            "depth": 1, "title": "Explore", "type": "status", "status": status,
        },
    }
    return json.dumps(message).encode("utf-8") + b"\n"


async def _deliver(raw_line: bytes, extension_notifications: tuple[str, ...]) -> list[Any]:
    """Write one raw JSON-RPC line to a connection built by ``connect_client``; return what the provider emitted."""
    import socket

    from ciao.providers.acp.connection import connect_client
    from ciao.providers.acp.normalize import UpdateNormalizer
    from ciao.providers.acp.provider import AgentClient

    provider = OpencodeAcpProvider(Path("."))
    normalizer = UpdateNormalizer(OPENCODE, "ses_root")
    seen: list[Any] = []

    class _Session:
        def on_ext(self, method: str, payload: Any, hook: Any) -> None:
            seen.extend(hook(normalizer, method, payload))

    client = AgentClient(ext_hook=provider.ext_update)  # type: ignore[abstract]
    client.session = _Session()  # type: ignore[assignment]
    ours, peer = socket.socketpair()
    reader, writer = await asyncio.open_connection(sock=ours)
    conn = connect_client(client, writer, reader, observers=[], extension_notifications=extension_notifications)
    try:
        peer.sendall(raw_line)
        for _ in range(200):
            if seen:
                break
            await asyncio.sleep(0.01)
    finally:
        await conn.close()
        peer.close()
    return seen


def test_unprefixed_child_update_reaches_the_provider_through_the_router() -> None:
    seen = asyncio.run(_deliver(_child_line("created"), OPENCODE.extension_notifications))
    # The raw frame named the method with no underscore: the connection's route handed it to
    # ext_update, which opened the child's node.
    assert [(event.type, event.tool_name, event.tool_use_id) for event in seen] == [
        ("tool_use", "Subagent", "child:ses_child"),
    ]


def test_unprefixed_child_update_without_a_route_is_not_delivered() -> None:
    # Without the spec's name on the router, the SDK answers the notification as an unknown method.
    assert asyncio.run(_deliver(_child_line("created"), ())) == []


@pytest.mark.asyncio
async def test_claude_initialize_asks_for_terminal_output_meta_and_no_air_block(tmp_path: Path) -> None:
    from ciao.providers.acp.agents import CLAUDE

    spec, calls = write_scenario(tmp_path, spec_client_meta=dict(CLAUDE.client_meta))
    connection = await _open(spec, tmp_path)
    await connection.close()
    init = [c for c in read_calls(calls) if c["method"] == "initialize"]
    assert len(init) == 1
    capabilities = init[0]["params"]["clientCapabilities"]
    # terminal_output_delta makes the adapter send terminal_exit; the terminal capability stays off.
    assert capabilities["_meta"] == {"terminal_output_delta": True}
    assert not any("air" in key.lower() for key in capabilities["_meta"])
    assert capabilities["terminal"] is False
