"""One spawned ACP agent process and its client-side connection (#3137).

``AcpConnection.open`` spawns the agent from an ``AgentSpec`` (argv comes only
from the spec, never from request data), wires the SDK's client connection to
its stdio, and completes ``initialize``. ``close`` ends the whole process tree
and is safe to call any number of times; every error path in ``open`` calls it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import acp
from acp.schema import ClientCapabilities

from ciao import __version__
from ciao.host_terminal import _terminal_env
from ciao.os_support.processes import ProcessTree, tree_spawn_options
from ciao.providers.acp.spec import AgentSpec
from ciao.tool_path import prepend_engine_path, resolve_tool

_SHUTDOWN_TIMEOUT = 5.0
_STDIO_LIMIT = 8 * 1024 * 1024


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


class AcpConnection:
    def __init__(
        self,
        spec: AgentSpec,
        process: asyncio.subprocess.Process,
        tree: ProcessTree,
        conn: acp.ClientSideConnection,
        stderr: StderrRing,
        stderr_task: asyncio.Task[None],
    ) -> None:
        self.spec = spec
        self._process = process
        self._tree = tree
        self._conn = conn
        self._stderr = stderr
        self._stderr_task = stderr_task
        self._capabilities: Mapping[str, Any] = {}
        self._closed = False
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
        merged = {**_terminal_env(), **spec.env, **env}
        merged["PATH"] = prepend_engine_path(merged.get("PATH"))
        process = await asyncio.create_subprocess_exec(
            binary,
            *spec.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=merged,
            limit=_STDIO_LIMIT,
            **tree_spawn_options(dies_with_engine=True),
        )
        try:
            tree = ProcessTree(process.pid, dies_with_engine=True)
        except BaseException:
            with contextlib.suppress(ProcessLookupError, OSError):
                process.kill()
            raise
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        ring = StderrRing()
        stderr_task = asyncio.create_task(_drain_stderr(process.stderr, ring, spec.id))
        try:
            conn = acp.connect_to_agent(client, process.stdin, process.stdout)
        except BaseException:
            await _stop(process, tree, stderr_task)
            raise
        self = cls(spec, process, tree, conn, ring, stderr_task)
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
        exited = asyncio.ensure_future(self._process.wait())
        try:
            done, _pending = await asyncio.wait(
                {request, exited},
                timeout=spec.initialize_timeout_s,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if request in done and request.exception() is None:
                response = request.result()
                self._capabilities = (
                    response.agent_capabilities.model_dump(by_alias=True, exclude_none=True)
                    if response.agent_capabilities is not None
                    else {}
                )
                return
            await self._reap_stderr()
            if request in done:
                # The stream closed on us: if the process is gone, say how.
                with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
                    await asyncio.wait_for(asyncio.shield(exited), timeout=0.5)
            if exited.done():
                reason = f"the process exited with code {self._process.returncode}"
            elif request in done:
                reason = str(request.exception()) or type(request.exception()).__name__
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
            # Retrieve any exception so asyncio does not log it as never retrieved.
            for task in (request, exited):
                with contextlib.suppress(BaseException):
                    await task

    async def _reap_stderr(self) -> None:
        """Let the stderr reader see what the agent wrote before it died."""
        with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
            await asyncio.wait_for(asyncio.shield(self._stderr_task), timeout=0.5)

    @property
    def alive(self) -> bool:
        return self._process.returncode is None and not self._closed

    @property
    def agent_capabilities(self) -> Mapping[str, Any]:
        return self._capabilities

    @property
    def conn(self) -> acp.ClientSideConnection:
        return self._conn

    def stderr_tail(self, lines: int = 20) -> str:
        return self._stderr.tail(lines)

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._conn.close(), timeout=_SHUTDOWN_TIMEOUT)
            await _stop(self._process, self._tree, self._stderr_task)


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
        log.debug("%s", line)


async def _stop(
    process: asyncio.subprocess.Process,
    tree: ProcessTree,
    stderr_task: asyncio.Task[None],
) -> None:
    """Ask the tree to stop, wait ``_SHUTDOWN_TIMEOUT``, then kill it."""
    try:
        if process.returncode is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                tree.kill_descendants()
        else:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                tree.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=_SHUTDOWN_TIMEOUT)
            except (TimeoutError, asyncio.TimeoutError):
                with contextlib.suppress(ProcessLookupError, FileNotFoundError, PermissionError, OSError):
                    tree.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(process.wait(), timeout=_SHUTDOWN_TIMEOUT)
    finally:
        tree.close()
        stderr_task.cancel()
        with contextlib.suppress(BaseException):
            await stderr_task
