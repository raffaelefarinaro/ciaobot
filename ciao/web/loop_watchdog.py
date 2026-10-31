"""Log slow HTTP requests and event-loop stalls, naming the requests in flight (#2349, #2357).

A synchronous handler that holds the loop for a second stalls every concurrent
request, and the uvicorn access log has no durations to show it. This module
logs the two facts that point at the cause: a request that took at least
SLOW_REQUEST_SECONDS, and a loop that woke late by at least
LOOP_BLOCK_THRESHOLD_SECONDS, together with the requests in flight at that
moment. Paths are logged without the query string, which can carry tokens.
WebSockets are not timed; they are long-lived by design.

"slow" is the total time to send, including client-bound transfers: a large
FileResponse to a slow client can log a slow request without any loop block.
The `event loop blocked` line is the one that marks a real stall.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

SLOW_REQUEST_SECONDS = 1.0
LOOP_BLOCK_THRESHOLD_SECONDS = 0.25
WATCHDOG_INTERVAL_SECONDS = 0.1
SNAPSHOT_LIMIT = 10


def _ms(seconds: float) -> int:
    return int(seconds * 1000)


def _safe(text: str, limit: int) -> str:
    """Escape control characters and cap length, so a request line cannot forge log lines."""
    escaped = text.encode("unicode_escape").decode("ascii")
    if len(escaped) <= limit:
        return escaped
    return escaped[:limit] + "..."


@dataclass(frozen=True)
class _InFlight:
    method: str
    path: str
    started: float


class InFlightRequests:
    """HTTP requests currently inside the app. Only touched from the event loop."""

    def __init__(self) -> None:
        self._ids = itertools.count()
        self._entries: dict[int, _InFlight] = {}

    def add(self, method: str, path: str, started: float) -> int:
        key = next(self._ids)
        self._entries[key] = _InFlight(method, path, started)
        return key

    def remove(self, key: int) -> None:
        self._entries.pop(key, None)

    def snapshot(self, now: float) -> list[str]:
        """`METHOD /path (N ms)` for each request in flight, oldest first, capped."""
        oldest = sorted(self._entries.values(), key=lambda entry: entry.started)
        return [
            f"{entry.method} {entry.path} ({_ms(now - entry.started)} ms)"
            for entry in oldest[:SNAPSHOT_LIMIT]
        ]


class RequestTimingMiddleware:
    """Pure ASGI middleware: track in-flight HTTP requests and log the slow ones."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        registry: InFlightRequests,
        slow_seconds: float = SLOW_REQUEST_SECONDS,
    ) -> None:
        self.app = app
        self.registry = registry
        self.slow_seconds = slow_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # The path is percent-decoded by the server and this runs before auth, so
        # escape and cap it before it is stored or logged.
        method = _safe(scope.get("method", ""), 16)
        path = _safe(scope.get("path", ""), 200)
        started = time.monotonic()
        key = self.registry.add(method, path, started)
        try:
            await self.app(scope, receive, send)
        finally:
            self.registry.remove(key)
            elapsed = time.monotonic() - started
            if elapsed >= self.slow_seconds:
                logger.warning("slow request %s %s took %d ms", method, path, _ms(elapsed))


class EventLoopWatchdog:
    """Sleep in short steps and treat a late wake-up as a blocked event loop."""

    def __init__(
        self,
        registry: InFlightRequests,
        *,
        interval: float = WATCHDOG_INTERVAL_SECONDS,
        threshold: float = LOOP_BLOCK_THRESHOLD_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.registry = registry
        self.interval = interval
        self.threshold = threshold
        self.clock = clock
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="event-loop-watchdog")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        while True:
            started = self.clock()
            await asyncio.sleep(self.interval)
            late = self.clock() - started - self.interval
            if late >= self.threshold:
                in_flight = ", ".join(self.registry.snapshot(self.clock())) or "none"
                logger.warning(
                    "event loop blocked for %d ms; in flight: %s",
                    _ms(late),
                    in_flight,
                )
