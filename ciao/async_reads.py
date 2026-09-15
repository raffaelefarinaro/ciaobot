"""Bounded off-loop execution for synchronous vault reads.

Vault reads (FTS indexing/search, backlink traversal, markdown-path
enumeration) are synchronous disk and SQLite work. Run inline inside an async
handler they hold the event loop, so a full vault scan stalls heartbeats
(``/ws/chat`` keepalives) and unrelated health responses behind it.

``asyncio.to_thread`` moves one call off the loop but has two gaps this module
closes:

- It uses the default executor, whose width is shared with every other
  ``to_thread`` call in the process. A burst of requests can start that many
  full vault scans at once.
- It cannot be cancelled, so a cancelled request leaves its scan running with
  nobody observing its completion or its error.

``run_read`` runs the operation on a dedicated, bounded executor and coalesces
identical in-flight reads by key, so N concurrent ``vault_search`` calls for the
same workspace and query share one scan instead of launching N. The SQLite
connection is opened and closed inside the worker, so it never crosses threads.

Cancellation only detaches the caller: the worker thread cannot be stopped and
keeps running to completion. Coalescing plus the bounded width is what keeps
that from becoming unbounded background work, and every future's completion and
error is observed even when its last awaiter was cancelled.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# A full vault index pass is I/O plus parsing. A small pool keeps several
# workspaces responsive without letting a request storm start that many
# simultaneous scans of the same disk.
MAX_VAULT_READ_WORKERS = 4

_EXECUTOR_LOCK = threading.Lock()
_EXECUTOR: "_VaultReadExecutor | None" = None


def _observe_failure(future: Future[Any]) -> None:
    """Retrieve a worker's exception so a detached failure is never silent.

    ``asyncio`` warns "Future exception was never retrieved" only for its own
    futures; the underlying ``concurrent.futures.Future`` is what a cancelled
    caller leaves behind. Reading ``exception()`` here marks it observed without
    consuming it: an awaiter that is still attached receives the same exception.
    """

    if future.cancelled():
        return
    error = future.exception()
    if error is not None:
        logger.debug("Vault read worker failed: %s", error, exc_info=error)


class _VaultReadExecutor:
    """A bounded executor that coalesces identical in-flight reads by key."""

    def __init__(self, max_workers: int = MAX_VAULT_READ_WORKERS) -> None:
        self.max_workers = max_workers
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="ciao-vault-read",
        )
        self._inflight: dict[str, Future[Any]] = {}
        self._lock = threading.Lock()

    def submit(
        self,
        key: str,
        operation: Callable[[], T],
        *,
        coalesce: bool,
    ) -> Future[T]:
        if coalesce:
            with self._lock:
                pending = self._inflight.get(key)
                if pending is not None:
                    return pending  # type: ignore[return-value]
                future = self._executor.submit(operation)
                self._inflight[key] = future
            # Registered outside the lock: the callback re-acquires it, and a
            # worker that finished during `add_done_callback` would otherwise
            # invoke `_forget_key` while the lock is held (deadlock).
            future.add_done_callback(self._make_forget(key))
            return future

        future = self._executor.submit(operation)
        future.add_done_callback(_observe_failure)
        return future

    def _make_forget(self, key: str) -> Callable[[Future[Any]], None]:
        def _forget(done: Future[Any]) -> None:
            self._forget_key(key, done)

        return _forget

    def _forget_key(self, key: str, future: Future[Any]) -> None:
        with self._lock:
            if self._inflight.get(key) is future:
                del self._inflight[key]
        _observe_failure(future)

    def inflight_keys(self) -> list[str]:
        with self._lock:
            return list(self._inflight)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


def _get_executor() -> _VaultReadExecutor:
    global _EXECUTOR
    if _EXECUTOR is None:
        with _EXECUTOR_LOCK:
            if _EXECUTOR is None:
                _EXECUTOR = _VaultReadExecutor()
    return _EXECUTOR


def vault_read_executor() -> _VaultReadExecutor:
    """The process-wide vault read executor (created on first use)."""
    return _get_executor()


def reset_vault_read_executor() -> None:
    """Drop and recreate the executor. Test-only isolation hook."""
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is not None:
            _EXECUTOR.close()
        _EXECUTOR = None


async def run_read(
    key: str,
    operation: Callable[[], T],
    *,
    coalesce: bool = True,
) -> T:
    """Run ``operation`` in a bounded worker and return its result.

    Callers that await the same ``key`` while it is in flight share one worker
    invocation. Cancelling the returned awaitable detaches this caller; the
    worker keeps running, remains joinable by a later caller, and has its
    completion or error observed by the executor.
    """

    loop = asyncio.get_running_loop()
    future = _get_executor().submit(key, operation, coalesce=coalesce)
    return await asyncio.wrap_future(future, loop=loop)
