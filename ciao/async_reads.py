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
error is observed even when its last awaiter was cancelled. Each caller awaits
its own ``asyncio`` future fed by a done-callback, never the shared concurrent
future directly, so cancelling one waiter cannot cancel the work the other
waiters of the same key are still waiting on.

Synchronous writers that share a database are additionally serialized by
``keyed_lock``: concurrent index passes against one SQLite file would otherwise
compete for its write lock and surface as ``database is locked``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, InvalidStateError, ThreadPoolExecutor
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# A full vault index pass is I/O plus parsing. A small pool keeps several
# workspaces responsive without letting a request storm start that many
# simultaneous scans of the same disk.
MAX_VAULT_READ_WORKERS = 4

# Most outstanding vault reads (running *and* queued) allowed before a new
# distinct read waits. `max_workers` bounds running threads only;
# ``ThreadPoolExecutor``'s queue is unbounded, and because cancellation
# deliberately leaves a submitted read running, a burst of distinct keys — or
# disconnected callers — would otherwise pile up full-vault scans without limit.
# Coalescing shares one slot across callers of the same key.
MAX_VAULT_READ_BACKLOG = 12

_EXECUTOR_LOCK = threading.Lock()
_EXECUTOR: "_VaultReadExecutor | None" = None

_KEYED_LOCKS: dict[str, threading.Lock] = {}
_KEYED_LOCKS_GUARD = threading.Lock()


def keyed_lock(key: str) -> threading.Lock:
    """A process-wide lock for one shared resource, created on first use.

    Used to serialize writes to a shared SQLite file: two workers indexing the
    same database concurrently take turns instead of racing for its write lock.
    Locks are never evicted — the registry holds one small entry per database
    and root, and dropping a lock a worker still holds would let a later caller
    create a second one and defeat the serialization.
    """

    with _KEYED_LOCKS_GUARD:
        lock = _KEYED_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _KEYED_LOCKS[key] = lock
        return lock


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
    """A bounded executor that coalesces identical in-flight reads by key.

    A submitted operation's future is shared by every same-key waiter, but a
    caller never awaits it directly: :meth:`bridge` hands back a fresh,
    per-caller future tied to the shared one by a callback. Cancelling a caller
    therefore tears down only its own future and leaves the shared work — and
    its other waiters — untouched.

    ``max_workers`` bounds running threads; ``max_backlog`` bounds *outstanding*
    work (running plus queued), which ``ThreadPoolExecutor`` does not. Admission
    is atomic under one lock: a caller either joins an existing same-key read
    (no new slot), reserves one, or waits for a slot to free. Because
    cancellation leaves admitted work running, this is what stops a burst of
    distinct keys — or disconnected callers — from piling up full-vault scans
    without limit.
    """

    def __init__(
        self,
        max_workers: int = MAX_VAULT_READ_WORKERS,
        max_backlog: int = MAX_VAULT_READ_BACKLOG,
    ) -> None:
        self.max_workers = max_workers
        self.max_backlog = max(1, int(max_backlog))
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="ciao-vault-read",
        )
        self._inflight: dict[str, Future[Any]] = {}
        self._lock = threading.Lock()
        self._outstanding = 0
        # One wakeup event per event loop. Releasing a slot sets every waiter's
        # event so they re-check capacity; `asyncio.Event.set` before `wait`
        # still wakes the later `wait`.
        self._waiters: dict[asyncio.AbstractEventLoop, asyncio.Event] = {}

    # -- admission ------------------------------------------------------

    def acquire(
        self,
        key: str,
        operation: Callable[[], T],
        *,
        coalesce: bool,
    ) -> Future[T] | None:
        """Join a same-key read or reserve a slot and submit.

        Returns the shared future to await, or ``None`` when the backlog is
        full — the caller then waits for a slot and retries.
        """
        register = False
        with self._lock:
            if coalesce:
                existing = self._inflight.get(key)
                if existing is not None:
                    return existing  # type: ignore[return-value]
            if self._outstanding >= self.max_backlog:
                return None
            self._outstanding += 1
            future = self._executor.submit(operation)
            if coalesce:
                self._inflight[key] = future
            register = True
        # Registered outside the lock: the callback re-acquires it, and a
        # worker that finished during `add_done_callback` would otherwise run
        # `_release` while the lock is held (the same inversion the coalescing
        # forget callback must avoid).
        if register:
            future.add_done_callback(self._make_release(key))
        return future

    def submit(
        self,
        key: str,
        operation: Callable[[], T],
        *,
        coalesce: bool,
    ) -> Future[T]:
        """Uncapped synchronous submission that still counts outstanding work.

        Kept for callers that manage admission themselves (and for tests);
        :func:`run_read` is the capped async entry point. Bypassing the cap is
        deliberate here — the cap is enforced by :meth:`acquire`, and a caller
        already holding a slot must be able to start it.
        """
        register = False
        with self._lock:
            if coalesce:
                shared = self._inflight.get(key)
                if shared is None:
                    shared = self._executor.submit(operation)
                    self._inflight[key] = shared
                    self._outstanding += 1
                    register = True
            else:
                shared = self._executor.submit(operation)
                self._outstanding += 1
                register = True
        if register:
            # Outside the lock, for the same callback/lock inversion `acquire`
            # documents.
            shared.add_done_callback(self._make_release(key))
        bridged: Future[T] = self.bridge(shared)
        return bridged

    # -- backpressure ---------------------------------------------------

    def _make_release(self, key: str) -> Callable[[Future[Any]], None]:
        def _release(done: Future[Any]) -> None:
            with self._lock:
                if self._inflight.get(key) is done:
                    del self._inflight[key]
                self._outstanding = max(0, self._outstanding - 1)
                waiters = list(self._waiters.items())
            for loop, event in waiters:
                try:
                    loop.call_soon_threadsafe(event.set)
                except RuntimeError:
                    # The loop has closed; its waiters are gone.
                    with self._lock:
                        self._waiters.pop(loop, None)
            _observe_failure(done)

        return _release

    async def wait_for_slot(self, loop: asyncio.AbstractEventLoop) -> None:
        """Wait until a slot may be reserved, without blocking the loop."""
        with self._lock:
            event = self._waiters.get(loop)
            if event is None:
                event = asyncio.Event()
                self._waiters[loop] = event
            event.clear()
            if self._outstanding < self.max_backlog:
                return
        await event.wait()

    # -- futures --------------------------------------------------------

    def bridge(self, shared: Future[Any]) -> Future[Any]:
        """A per-caller future that mirrors ``shared`` without sharing its state.

        Copying the result out in the done-callback is deliberate: awaiting the
        shared future with ``asyncio.shield`` still leaves the caller's wrapper
        cancelled (it raises ``CancelledError`` at the await point), while
        awaiting the shared future directly lets one cancellation propagate into
        it. A separate destination future keeps the two apart.
        """

        destination: Future[Any] = Future()

        def _propagate(done: Future[Any]) -> None:
            try:
                if destination.cancelled():
                    return
                if done.cancelled():
                    destination.cancel()
                    return
                error = done.exception()
                if error is not None:
                    destination.set_exception(error)
                else:
                    destination.set_result(done.result())
            except InvalidStateError:
                # The caller was cancelled between the check above and this set,
                # or the destination was already settled. The shared future's
                # outcome is still observed by the release callback.
                return

        shared.add_done_callback(_propagate)
        return destination

    def inflight_keys(self) -> list[str]:
        with self._lock:
            return list(self._inflight)

    def outstanding(self) -> int:
        with self._lock:
            return self._outstanding

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

    Admission is bounded: when more than ``MAX_VAULT_READ_BACKLOG`` reads are
    outstanding, a caller waits for a slot here instead of queueing another
    full-vault scan. Coalescing means same-key callers never consume one.
    """

    loop = asyncio.get_running_loop()
    executor = _get_executor()
    while True:
        shared = executor.acquire(key, operation, coalesce=coalesce)
        if shared is not None:
            break
        await executor.wait_for_slot(loop)
    return await asyncio.wrap_future(executor.bridge(shared), loop=loop)
