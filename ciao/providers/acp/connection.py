"""One spawned ACP agent process and its client-side connection (#3137).

``AcpConnection.open`` spawns the agent from an ``AgentSpec`` (argv comes only
from the spec, never from request data), wires the SDK's client connection to
its stdio, and completes ``initialize``. ``close`` ends the whole process tree
and is safe to call any number of times; every error path in ``open`` calls it.

The agent stays in the engine's process group (no new session), so an engine
crash or SIGKILL still takes it down with the supervisor's group kill. Because
that group is shared, the agent's descendants are found by walking ppid links
(``_TreeWatch``), not by signalling a group. Orphans are re-parented to pid 1
once their parent dies, so the set is polled while the agent runs and merged
with a fresh snapshot at stop time. A process that is spawned and orphaned
within one poll interval can still escape.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import time
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import acp
from acp.connection import StreamDirection, StreamEvent
from acp.schema import ClientCapabilities

from ciao import __version__
from ciao.host_terminal import PLATFORM_MACOS, host_platform
from ciao.os_support.processes import descendant_processes, process_table, signal_processes
from ciao.providers.acp.spec import AgentSpec
from ciao.redact import redact_text
from ciao.tool_path import prepend_engine_path, resolve_tool, terminal_path

logger = logging.getLogger(__name__)

_SHUTDOWN_TIMEOUT = 5.0
# The stream's buffer size. It is NOT a cap on one JSON-RPC line: the SDK's reader catches
# LimitOverrunError and keeps appending chunks, so a longer line is assembled in memory.
# No hard cap is enforced (the SDK offers no hook; docs/ARCHITECTURE.md, Known gaps).
_STDIO_LIMIT = 31 * 1024 * 1024
_WATCH_FAST_POLLS = 40
_WATCH_FAST_INTERVAL = 0.25
_WATCH_SLOW_INTERVAL = 3.0
_KILL_SETTLE_TIMEOUT = 3.0
_EXIT_POLL_INTERVAL = 0.05
# The ACP layer is POSIX-only for now; Windows gets the closest signal Python defines.
if sys.platform == "win32":
    _SIGKILL = signal.SIGTERM
else:
    _SIGKILL = signal.SIGKILL


class StderrRing:
    """The last stderr lines an agent wrote, bounded by line count and bytes."""

    def __init__(self, max_lines: int = 200, max_bytes: int = 64 * 1024) -> None:
        self._max_lines = max_lines
        self._max_bytes = max_bytes
        self._lines: deque[str] = deque()
        self._bytes = 0

    def feed(self, line: str) -> None:
        self._lines.append(line)
        self._bytes += len(line.encode("utf-8", "replace"))
        while self._lines and (len(self._lines) > self._max_lines or self._bytes > self._max_bytes):
            dropped = self._lines.popleft()
            self._bytes -= len(dropped.encode("utf-8", "replace"))

    def tail(self, lines: int = 20) -> str:
        if lines <= 0:
            return ""
        return "\n".join(list(self._lines)[-lines:])


class AgentStartError(RuntimeError):
    """The agent binary is missing, exited during startup, or failed initialize."""

    def __init__(self, message: str, *, stderr_tail: str = "") -> None:
        super().__init__(message)
        self.stderr_tail = stderr_tail


class AgentVersionError(AgentStartError):
    """The agent reports a version below its floor. The message is the gate's fixed refusal text."""


class _TreeWatch:
    """The agent's live descendants, tracked by ppid walk with start-time identity."""

    def __init__(self, root_pid: int) -> None:
        self.root_pid = root_pid
        self.known: dict[int, str] = {}
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._poll())

    async def pause(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _poll(self) -> None:
        polls = 0
        while True:
            await self.refresh()
            polls += 1
            await asyncio.sleep(_WATCH_FAST_INTERVAL if polls <= _WATCH_FAST_POLLS else _WATCH_SLOW_INTERVAL)

    async def refresh(self) -> dict[int, tuple[int, str]]:
        table = await asyncio.to_thread(process_table)
        if table:
            self.known = {pid: start for pid, start in self.known.items()
                          if pid in table and table[pid][1] == start}
            self.known.update(descendant_processes(self.root_pid, table=table))
        return table

    async def signal(self, sig: int) -> None:
        table = await self.refresh()
        signal_processes(self.known, sig, table=table)

    async def any_alive(self) -> bool:
        await self.refresh()
        return bool(self.known)


class _FlushedResponses:
    """Outgoing JSON-RPC responses observed after ``arm``, once each write has drained."""

    def __init__(self) -> None:
        self._remaining = 0
        self._future: asyncio.Future[None] | None = None

    def arm(self, count: int) -> asyncio.Future[None]:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        if count <= 0:
            future.set_result(None)
            return future
        self._remaining = count
        self._future = future
        return future

    def observe(self, event: StreamEvent) -> None:
        future = self._future
        if future is None or future.done():
            return
        if event.direction != StreamDirection.OUTGOING:
            return
        message = event.message
        # A response has an id and a result or error, and no method.
        if "id" not in message or "method" in message:
            return
        if "result" not in message and "error" not in message:
            return
        self._remaining -= 1
        if self._remaining <= 0:
            self._future = None
            future.set_result(None)


# Ciaobot's own internals, withheld from every agent process: the CIAO_* settings (the agent URL
# and token travel in the request's extra env instead) and the engine's PWA login token.
_ENGINE_INTERNAL_ENV_KEYS = frozenset({"PWA_AUTH_TOKEN"})


def agent_base_env(unset: tuple[str, ...]) -> dict[str, str]:
    """The engine's environment as an agent process inherits it.

    Every variable is passed through, provider credentials included: each CLI decides what it
    uses, so Ciaobot does not filter them. Only ``CIAO_*`` and the PWA token are withheld, and
    the spec's ``unset`` names. On non-macOS hosts ``PATH`` is the user's terminal PATH.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CIAO_")
        and key not in _ENGINE_INTERNAL_ENV_KEYS
        and key not in unset
    }
    path = terminal_path()
    if host_platform() != PLATFORM_MACOS and path:
        env["PATH"] = path
    return env


class AcpConnection:
    def __init__(
        self,
        spec: AgentSpec,
        binary: str,
        process: asyncio.subprocess.Process,
        watch: _TreeWatch,
        conn: acp.ClientSideConnection,
        stderr: StderrRing,
        stderr_task: asyncio.Task[None],
        flushed: _FlushedResponses,
    ) -> None:
        self.spec = spec
        self._binary = binary
        self._process = process
        self._watch = watch
        self._conn = conn
        self._stderr = stderr
        self._stderr_task = stderr_task
        self._flushed = flushed
        self._capabilities: Mapping[str, Any] = {}
        self._closing = False
        self._closed = False
        self._stop_task: asyncio.Task[None] | None = None
        self._close_lock = asyncio.Lock()

    @classmethod
    async def open(
        cls,
        spec: AgentSpec,
        *,
        cwd: Path,
        env: Mapping[str, str],
        client: acp.Client,
    ) -> AcpConnection:
        binary = resolve_tool(spec.binary)
        if binary is None:
            raise AgentStartError(f"{spec.binary} was not found on PATH.")
        if not os.path.isabs(binary):
            # A '.' or empty PATH entry resolves relative to the cwd we are about to set.
            raise AgentStartError(f"{spec.binary} resolved to a relative path ({binary}); refusing to start it.")
        merged = {**agent_base_env(spec.unset_env), **spec.env, **env}
        merged["PATH"] = prepend_engine_path(merged.get("PATH"))
        # No start_new_session: the agent stays in the engine's process group so the
        # supervisor's group kill reaches it when the engine dies.
        process = await asyncio.create_subprocess_exec(
            binary,
            *spec.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=merged,
            limit=_STDIO_LIMIT,
        )
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        watch = _TreeWatch(process.pid)
        ring = StderrRing()
        stderr_task = asyncio.create_task(_drain_stderr(process.stderr, ring, spec.id))
        watch.start()
        flushed = _FlushedResponses()
        try:
            conn = acp.connect_to_agent(
                client, process.stdin, process.stdout, observers=[flushed.observe]
            )
        except BaseException:
            await asyncio.shield(asyncio.ensure_future(_stop(process, watch, stderr_task)))
            raise
        self = cls(spec, binary, process, watch, conn, ring, stderr_task, flushed)
        try:
            await self._initialize()
        except BaseException as exc:
            await self.close()
            if isinstance(exc, AgentStartError):
                raise
            if isinstance(exc, Exception):
                reason = str(exc) or type(exc).__name__
                raise AgentStartError(
                    f"{spec.id} agent did not start: {reason}",
                    stderr_tail=self._stderr.tail(),
                ) from exc
            raise
        return self

    async def _initialize(self) -> None:
        spec = self.spec
        capabilities = ClientCapabilities.model_validate({
            "fs": {"readTextFile": False, "writeTextFile": False},
            "terminal": False,
            "session": {"compaction": {}},
            "_meta": dict(spec.client_meta),
        })
        request = asyncio.ensure_future(self._conn.initialize(
            protocol_version=acp.PROTOCOL_VERSION,
            client_capabilities=capabilities,
            client_info={"name": "ciaobot", "version": __version__},
        ))
        # Not Process.wait: on Python 3.12 that stays pending until every stdio
        # pipe disconnects, and a grandchild that inherited them holds it open
        # for the whole initialize timeout.
        exited = asyncio.ensure_future(_wait_child_exit(self._process))
        try:
            done, _pending = await asyncio.wait(
                {request, exited},
                timeout=spec.initialize_timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            request_error: BaseException | None = None
            if request in done:
                request_error = (
                    AgentStartError("the initialize request was cancelled")
                    if request.cancelled() else request.exception()
                )
            if request in done and request_error is None:
                response = request.result()
                self._capabilities = (
                    response.agent_capabilities.model_dump(by_alias=True, exclude_none=True)
                    if response.agent_capabilities is not None
                    else {}
                )
                await self._check_version(response)
                return
            await self._reap_stderr()
            if request in done:
                # The stream closed on us: if the process is gone, say how.
                with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                    await asyncio.wait_for(asyncio.shield(exited), timeout=0.5)
            # returncode is set when the child is reaped, even if exited is
            # still in its poll sleep or stdio is still open.
            if self._process.returncode is not None:
                reason = f"the process exited with code {self._process.returncode}"
            elif request in done:
                reason = str(request_error) or type(request_error).__name__
            else:
                reason = f"initialize did not answer within {spec.initialize_timeout_s:g}s"
            raise AgentStartError(
                f"{spec.id} agent did not start: {reason}",
                stderr_tail=self._stderr.tail(),
            )
        finally:
            for task in (request, exited):
                if not task.done():
                    task.cancel()
            # Retrieve any exception so asyncio does not log it as never retrieved;
            # gather lets a cancellation of this caller through.
            await asyncio.gather(request, exited, return_exceptions=True)

    async def _check_version(self, response: Any) -> None:
        """Refuse an agent below the spec's version floor, before any session opens.

        The version comes from ``agentInfo``; an agent that reports none is probed with the
        spec's probe on the same binary. The refusal is the gate's constant text only.
        """
        gate = self.spec.version_gate
        if gate is None:
            return
        info = getattr(response, "agent_info", None)
        version = getattr(info, "version", "") if info is not None else ""
        if not isinstance(version, str) or not version.strip():
            version = await asyncio.to_thread(gate.probe, self._binary)
        if not gate.supported(version.strip()):
            # The reported version is adapter-controlled: it is neither shown nor logged.
            logger.warning("%s agent refused: its version is below the supported floor.", self.spec.id)
            raise AgentVersionError(gate.refusal)

    async def _reap_stderr(self) -> None:
        """Let the stderr reader see what the agent wrote before it died."""
        with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
            await asyncio.wait_for(asyncio.shield(self._stderr_task), timeout=0.5)

    @property
    def alive(self) -> bool:
        return self._process.returncode is None and not self._closing

    @property
    def agent_capabilities(self) -> Mapping[str, Any]:
        return self._capabilities

    @property
    def conn(self) -> acp.ClientSideConnection:
        return self._conn

    def wait_until_responses_flushed(self, count: int) -> asyncio.Future[None]:
        """Resolves once ``count`` outgoing JSON-RPC responses have been written and drained.

        Arming is synchronous, so a response unblocked just before this call
        cannot flush until the caller awaits the future.
        """
        return self._flushed.arm(count)

    def stderr_tail(self, lines: int = 20) -> str:
        return self._stderr.tail(lines)

    async def close(self) -> None:
        """Stop the agent and its whole tree; survives cancellation of the caller.

        The stop runs as its own task awaited through ``shield``, so a cancelled
        caller still gets the tree killed, and a second ``close`` joins a stop
        that a cancelled first call left running. ``_closed`` is set only once
        the stop has finished.
        """
        async with self._close_lock:
            if self._closed:
                return
            self._closing = True
            try:
                if self._stop_task is None:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(self._conn.close(), timeout=_SHUTDOWN_TIMEOUT)
            finally:
                if self._stop_task is None:
                    self._stop_task = asyncio.ensure_future(
                        _stop(self._process, self._watch, self._stderr_task))
                    self._stop_task.add_done_callback(self._mark_closed)
                await asyncio.shield(self._stop_task)

    def _mark_closed(self, task: asyncio.Task[None]) -> None:
        self._closed = True
        if not task.cancelled():
            task.exception()


async def _wait_child_exit(process: asyncio.subprocess.Process) -> int:
    """Return this process's exit code once the process itself has exited.

    Python 3.12 wakes ``Process.wait`` only from ``_call_connection_lost``,
    after every stdio pipe has disconnected. A descendant that inherited those
    pipes (``close_fds`` still leaves 0, 1 and 2 open) keeps the wait pending
    until the pipes close, so an agent that exits during initialize is reported
    as the initialize timeout. The transport stores the return code as soon as
    the child is reaped, while those pipes are still open.
    """
    while True:
        code = process.returncode
        if code is not None:
            return code
        await asyncio.sleep(_EXIT_POLL_INTERVAL)


async def _drain_stderr(stream: asyncio.StreamReader, ring: StderrRing, agent_id: str) -> None:
    log = logging.getLogger(f"ciao.providers.acp.{agent_id}")
    while True:
        try:
            raw = await stream.readline()
        except (ValueError, asyncio.LimitOverrunError):
            # A line over the stream limit: drop what is buffered and keep reading.
            continue
        if not raw:
            return
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        ring.feed(line)
        log.debug("%s", redact_text(line))


def _signal_leader(process: asyncio.subprocess.Process, sig: int) -> None:
    if process.returncode is None and sys.platform != "win32":
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(process.pid, sig)


async def _stop(
    process: asyncio.subprocess.Process,
    watch: _TreeWatch,
    stderr_task: asyncio.Task[None],
) -> None:
    """SIGTERM the leader and every descendant, wait ``_SHUTDOWN_TIMEOUT``, then SIGKILL what is left.

    Descendants are collected before the leader is signalled: once it dies its
    children are re-parented to pid 1 and can no longer be found by ppid.
    """
    try:
        await watch.pause()
        await watch.refresh()
        _signal_leader(process, signal.SIGTERM)
        await watch.signal(signal.SIGTERM)
        deadline = time.monotonic() + _SHUTDOWN_TIMEOUT
        while time.monotonic() < deadline:
            if process.returncode is not None and not await watch.any_alive():
                break
            await asyncio.sleep(0.05)
        else:
            _signal_leader(process, _SIGKILL)
            await watch.signal(_SIGKILL)
        settle = time.monotonic() + _KILL_SETTLE_TIMEOUT
        while time.monotonic() < settle and (process.returncode is None or await watch.any_alive()):
            await asyncio.sleep(0.05)
    finally:
        stderr_task.cancel()
        await asyncio.gather(stderr_task, return_exceptions=True)
