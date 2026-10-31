"""Slow-request and blocked-event-loop logging (#2357, follows #2349)."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web.loop_watchdog import (
    EventLoopWatchdog,
    InFlightRequests,
    RequestTimingMiddleware,
)

WATCHDOG_LOGGER = "ciao.web.loop_watchdog"


def _timed_app(registry: InFlightRequests, slow_seconds: float) -> Starlette:
    def slow(_request: Request) -> PlainTextResponse:
        time.sleep(0.05)
        return PlainTextResponse("ok")

    def boom(_request: Request) -> PlainTextResponse:
        raise RuntimeError("route failure")

    return Starlette(
        routes=[Route("/slow", slow), Route("/boom", boom)],
        middleware=[
            Middleware(RequestTimingMiddleware, registry=registry, slow_seconds=slow_seconds)
        ],
    )


def _warnings_containing(caplog: pytest.LogCaptureFixture, fragment: str) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and fragment in r.getMessage()
    ]


def test_slow_request_is_logged_with_its_path_but_not_its_query(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = TestClient(_timed_app(InFlightRequests(), slow_seconds=0.01))

    with caplog.at_level(logging.WARNING, logger=WATCHDOG_LOGGER):
        response = client.get("/slow?token=secret")

    assert response.status_code == 200
    messages = _warnings_containing(caplog, "slow request")
    assert len(messages) == 1
    assert "slow request GET /slow" in messages[0]
    assert "secret" not in messages[0]


def test_fast_request_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    client = TestClient(_timed_app(InFlightRequests(), slow_seconds=10))

    with caplog.at_level(logging.WARNING, logger=WATCHDOG_LOGGER):
        response = client.get("/slow?token=secret")

    assert response.status_code == 200
    assert _warnings_containing(caplog, "slow request") == []


def test_watchdog_reports_a_blocked_loop_with_the_in_flight_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = InFlightRequests()

    async def main() -> None:
        watchdog = EventLoopWatchdog(registry, interval=0.01, threshold=0.05)
        registry.add("GET", "/api/projects", time.monotonic())
        watchdog.start()
        # Let the watchdog take its first measurement before the loop blocks.
        await asyncio.sleep(0.02)
        time.sleep(0.2)
        await asyncio.sleep(0.05)
        await watchdog.stop()

    with caplog.at_level(logging.WARNING, logger=WATCHDOG_LOGGER):
        asyncio.run(main())

    blocked = _warnings_containing(caplog, "event loop blocked")
    assert blocked
    assert any("GET /api/projects" in message for message in blocked)


def test_watchdog_is_quiet_when_the_loop_is_free(caplog: pytest.LogCaptureFixture) -> None:
    registry = InFlightRequests()

    async def main() -> None:
        watchdog = EventLoopWatchdog(registry, interval=0.01, threshold=2.0)
        watchdog.start()
        await asyncio.sleep(0.1)
        await watchdog.stop()

    with caplog.at_level(logging.WARNING, logger=WATCHDOG_LOGGER):
        asyncio.run(main())

    assert _warnings_containing(caplog, "event loop blocked") == []


def test_in_flight_entry_is_removed_after_the_request() -> None:
    registry = InFlightRequests()
    client = TestClient(
        _timed_app(registry, slow_seconds=10), raise_server_exceptions=False
    )

    ok = client.get("/slow")
    failed = client.get("/boom")

    assert ok.status_code == 200
    assert failed.status_code == 500
    assert registry.snapshot(time.monotonic()) == []


def test_logged_path_cannot_forge_a_log_line(caplog: pytest.LogCaptureFixture) -> None:
    client = TestClient(_timed_app(InFlightRequests(), slow_seconds=0))

    with caplog.at_level(logging.WARNING, logger=WATCHDOG_LOGGER):
        client.get("/a%0Aforged")

    messages = _warnings_containing(caplog, "slow request")
    assert len(messages) == 1
    assert "\n" not in messages[0]
    assert "\r" not in messages[0]


def test_logged_path_is_length_capped(caplog: pytest.LogCaptureFixture) -> None:
    client = TestClient(_timed_app(InFlightRequests(), slow_seconds=0))

    with caplog.at_level(logging.WARNING, logger=WATCHDOG_LOGGER):
        client.get("/" + "a" * 5000)

    messages = _warnings_containing(caplog, "slow request")
    assert len(messages) == 1
    assert len(messages[0]) < 400
