"""The menu bar reads update availability off /api/startup-status.

It polls that endpoint on a short client timeout to decide whether the engine is
alive at all, so the release lookup bolted onto it must never be able to slow it
down or fail it — a cold cache would otherwise make the tray report the engine
as down. `asyncio.to_thread` cannot be cancelled, so the lookup runs detached
and the endpoint only ever reads the last value it stored.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.main import StartupTracker
from ciao.web.routes_api import _cached_update_hint, startup_status_endpoint


def _request(fetcher=None, hint=None) -> SimpleNamespace:
    state = SimpleNamespace()
    if fetcher is not None:
        state.package_status_fetcher = fetcher
    if hint is not None:
        state.update_hint = hint
    return SimpleNamespace(app=SimpleNamespace(state=state))


async def test_hint_is_read_from_the_cache_not_fetched_inline() -> None:
    request = _request(
        fetcher=lambda: {"latest_version": "0.6.1", "update_available": True},
        hint=("0.6.1", True),
    )
    assert await _cached_update_hint(request) == ("0.6.1", True)


async def test_first_poll_reports_no_hint_then_the_refresh_populates_it() -> None:
    request = _request(fetcher=lambda: {"latest_version": "0.6.1", "update_available": True})

    # Cold: nothing cached yet, and the lookup has only been scheduled.
    assert await _cached_update_hint(request) == ("", False)
    await request.app.state.update_hint_task

    # The next poll — the tray polls every couple of seconds — sees it.
    assert await _cached_update_hint(request) == ("0.6.1", True)


async def test_a_slow_release_lookup_returns_immediately() -> None:
    def slow_fetcher() -> dict[str, object]:
        time.sleep(3)
        return {"latest_version": "9.9.9", "update_available": True}

    request = _request(fetcher=slow_fetcher)
    started = time.monotonic()
    result = await _cached_update_hint(request)
    elapsed = time.monotonic() - started

    assert result == ("", False)
    assert elapsed < 0.5, f"the update hint blocked the poll for {elapsed:.2f}s"
    request.app.state.update_hint_task.cancel()


async def test_a_failing_release_lookup_leaves_the_hint_empty() -> None:
    def boom() -> dict[str, object]:
        raise RuntimeError("github unreachable")

    request = _request(fetcher=boom)
    assert await _cached_update_hint(request) == ("", False)
    # The detached task must swallow it rather than raise into the event loop.
    await request.app.state.update_hint_task
    assert await _cached_update_hint(request) == ("", False)


async def test_missing_fetcher_is_not_an_error() -> None:
    assert await _cached_update_hint(_request()) == ("", False)


def _client(hint=None) -> TestClient:
    app = Starlette(routes=[Route("/api/startup-status", startup_status_endpoint)])
    if hint is not None:
        app.state.update_hint = hint
        app.state.package_status_fetcher = lambda: {
            "latest_version": hint[0],
            "update_available": hint[1],
        }
    return TestClient(app)


def test_endpoint_exposes_the_hint_alongside_the_fields_the_tray_needs() -> None:
    body = _client(hint=("0.6.1", True)).get("/api/startup-status").json()
    assert body["latest_version"] == "0.6.1"
    assert body["update_available"] is True
    assert "overall_ready" in body


def test_endpoint_serves_without_any_update_plumbing_configured() -> None:
    body = _client().get("/api/startup-status").json()
    assert body["update_available"] is False
    assert body["latest_version"] == ""


def test_endpoint_reports_only_current_engine_correlation_identity() -> None:
    app = Starlette(routes=[Route("/api/startup-status", startup_status_endpoint)])
    tracker = StartupTracker(startup_id="opaque-run-id", engine_pid=1234)
    app.state.startup_tracker = tracker
    client = TestClient(app)
    body = client.get("/api/startup-status").json()
    second_body = client.get("/api/startup-status").json()
    assert body["startup_id"] == "opaque-run-id"
    assert body["pid"] == 1234
    assert second_body["startup_id"] == body["startup_id"]
    assert second_body["pid"] == body["pid"]
    assert body["phases"] == []
    assert body["overall_ready"] is False
    assert not ({"workspace", "runtime", "token", "secret", "control_token"} & body.keys())


def test_endpoint_omits_identity_for_legacy_tracker() -> None:
    app = Starlette(routes=[Route("/api/startup-status", startup_status_endpoint)])
    app.state.startup_tracker = SimpleNamespace(to_dict=lambda: {"phases": [], "overall_ready": False})
    body = TestClient(app).get("/api/startup-status").json()
    assert "startup_id" not in body
    assert "pid" not in body


def test_endpoint_omits_invalid_startup_identity_fields() -> None:
    app = Starlette(routes=[Route("/api/startup-status", startup_status_endpoint)])
    tracker = StartupTracker()
    setattr(tracker, "startup_id", 123)
    setattr(tracker, "engine_pid", True)
    app.state.startup_tracker = tracker
    body = TestClient(app).get("/api/startup-status").json()
    assert "startup_id" not in body
    assert "pid" not in body


def test_endpoint_without_engine_tracker_does_not_fabricate_identity() -> None:
    body = _client().get("/api/startup-status").json()
    assert "startup_id" not in body
    assert "pid" not in body
