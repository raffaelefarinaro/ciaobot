"""Async JSON-lines RPC transport for provider CLI subprocesses.

Codex app-server and Gemini ACP both speak one JSON object per stdout line.
The envelope differs slightly between them, so this module intentionally does
not enforce a ``jsonrpc`` field; provider adapters own their wire codec while
this peer owns process lifecycle, request correlation, and server messages.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import deque
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_READ_LIMIT = 8 * 1024 * 1024
_STDERR_LINES = 80


class RpcError(RuntimeError):
    """Base error raised by :class:`StdioJsonRpcPeer`."""


class RpcResponseError(RpcError):
    """The provider returned an error response for a client request."""

    def __init__(self, method: str, error: object) -> None:
        self.method = method
        self.error = error
        if isinstance(error, dict):
            message = str(error.get("message") or error)
        else:
            message = str(error)
        super().__init__(f"{method}: {message}")


class RpcProcessError(RpcError):
    """The provider subprocess exited or its transport became unusable."""


class StdioJsonRpcPeer:
    """One JSON-lines RPC subprocess connection.

    Responses resolve the future created by :meth:`request`. Notifications and
    server-initiated requests are exposed through :meth:`next_message` so the
    provider adapter can normalize them into its own event model.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        name: str = "provider",
    ) -> None:
        self.command = [str(part) for part in command]
        self.cwd = Path(cwd)
        self.env = dict(env) if env is not None else None
        self.name = name
        self.process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int | str, tuple[str, asyncio.Future[Any]]] = {}
        self._messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._next_id = 1
        self._closed = False
        self._stderr: deque[str] = deque(maxlen=_STDERR_LINES)

    @property
    def running(self) -> bool:
        return bool(
            not self._closed
            and self.process is not None
            and self.process.returncode is None
        )

    @property
    def stderr_tail(self) -> str:
        return "\n".join(self._stderr)

    async def start(self) -> None:
        if self.running:
            return
        if not self.command:
            raise RpcProcessError(f"{self.name}: empty command")
        child_env = os.environ.copy()
        if self.env:
            child_env.update(self.env)
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.command,
                cwd=str(self.cwd),
                env=child_env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_READ_LIMIT,
            )
        except OSError as exc:
            raise RpcProcessError(
                f"{self.name}: failed to start {self.command[0]}: {exc}"
            ) from exc
        self._closed = False
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())

    async def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float = 120.0,
    ) -> Any:
        if not self.running:
            raise RpcProcessError(f"{self.name}: process is not running")
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = (method, future)
        try:
            await self._send(
                {"id": request_id, "method": method, "params": dict(params or {})}
            )
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise RpcError(f"{self.name}: {method} timed out after {timeout:g}s") from exc
        except BaseException:
            self._pending.pop(request_id, None)
            raise

    async def notify(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> None:
        await self._send({"method": method, "params": dict(params or {})})

    async def respond(
        self,
        request_id: int | str,
        *,
        result: object | None = None,
        error: object | None = None,
    ) -> None:
        payload: dict[str, object] = {"id": request_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result if result is not None else {}
        await self._send(payload)

    async def next_message(self, *, timeout: float | None = None) -> dict[str, Any]:
        if timeout is None:
            return await self._messages.get()
        return await asyncio.wait_for(self._messages.get(), timeout=timeout)

    async def _send(self, payload: Mapping[str, object]) -> None:
        process = self.process
        if not self.running or process is None or process.stdin is None:
            raise RpcProcessError(f"{self.name}: stdin is closed")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        async with self._write_lock:
            try:
                process.stdin.write(encoded.encode("utf-8") + b"\n")
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise RpcProcessError(f"{self.name}: transport closed") from exc

    async def _read_loop(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        failure: Exception | None = None
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                if len(line) > _READ_LIMIT:
                    raise RpcProcessError(f"{self.name}: oversized protocol line")
                try:
                    message = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise RpcProcessError(
                        f"{self.name}: malformed protocol line"
                    ) from exc
                if not isinstance(message, dict):
                    raise RpcProcessError(f"{self.name}: protocol message is not an object")
                request_id = message.get("id")
                if (
                    request_id is not None
                    and "method" not in message
                    and ("result" in message or "error" in message)
                ):
                    pending = self._pending.pop(request_id, None)
                    if pending is None:
                        logger.debug("%s: response for unknown id %r", self.name, request_id)
                        continue
                    method, future = pending
                    if future.done():
                        continue
                    if "error" in message:
                        future.set_exception(RpcResponseError(method, message["error"]))
                    else:
                        future.set_result(message.get("result"))
                    continue
                await self._messages.put(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced to every waiter
            failure = exc
        finally:
            returncode = process.returncode
            if returncode is None:
                try:
                    returncode = await process.wait()
                except Exception:  # noqa: BLE001
                    returncode = None
            detail = self.stderr_tail.strip()
            message = f"{self.name}: process exited"
            if returncode is not None:
                message += f" with status {returncode}"
            if detail:
                message += f": {detail.splitlines()[-1]}"
            error = failure or RpcProcessError(message)
            self._fail_pending(error)
            await self._messages.put(
                {
                    "_process_exit": True,
                    "returncode": returncode,
                    "error": str(error),
                    "stderr": detail,
                }
            )

    async def _stderr_loop(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr.append(text)
                    logger.debug("%s stderr: %s", self.name, text)
        except asyncio.CancelledError:
            raise

    def _fail_pending(self, error: Exception) -> None:
        for _method, future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self.process
        if process is not None and process.stdin is not None:
            process.stdin.close()
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3.0)
            except TimeoutError:
                process.kill()
                await process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._reader_task, self._stderr_task):
            if task is None:
                continue
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._fail_pending(RpcProcessError(f"{self.name}: connection closed"))
        self.process = None

