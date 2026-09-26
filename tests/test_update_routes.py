from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from dataclasses import asdict

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import engine_update
from ciao.web import auth
from ciao.web.routes_node import (
    update_apply_endpoint,
    update_stage_endpoint,
    update_status_endpoint,
)

NOT_INSTALLER = (
    "in-app updates are only for engines installed with the Ciaobot engine installer"
)
ALREADY_RUNNING = {"error": "an engine update is already in progress"}
NOTHING_STAGED = {"error": "nothing staged; run: ciao update stage"}


def _op(phase: str) -> engine_update.Operation:
    """A record as the coordinator would have left it at ``phase``."""
    return engine_update.Operation(
        id="20260101T101500Z-4f2a",
        phase=phase,
        from_version="2.0.15",
        to_version="2.0.16",
        started_at="2026-01-01T10:15:00+00:00",
        updated_at="2026-01-01T10:16:20+00:00",
    )


class _Runner:
    """``stage_update``/``apply_update`` without the download and the swap.

    The real halves block for minutes, so the tests need a thread that is
    genuinely in flight when the next request arrives — and a signal that it
    started, rather than a sleep to find out.
    """

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls: list[str | None] = []

    def __call__(self, version: str | None = None) -> None:
        self.calls.append(version)
        self.entered.set()
        # Bounded, so a test that never releases costs this wait rather than
        # hanging: the point is to hold the thread open, not forever.
        self.release.wait(timeout=30)

    @contextlib.contextmanager
    def held(self) -> Iterator[None]:
        """Keep the run open for the body, and let it go before teardown.

        Releasing inside the client block is not tidiness: the loop joins its
        worker threads when the portal shuts down, so a thread still blocked
        at that point would silently cost the full wait instead of failing.
        """
        try:
            yield
        finally:
            self.release.set()

    def started(self) -> None:
        assert self.entered.wait(timeout=10), "the background update thread never ran"


@pytest.fixture
def runner() -> Iterator[_Runner]:
    """A blocking stage/apply stand-in, always released at teardown.

    Releasing here is not bookkeeping: a worker thread left blocked is joined
    by the interpreter at exit, so a test that forgot would hang the suite
    instead of failing.
    """
    fake = _Runner()
    yield fake
    fake.release.set()


def _app(**state: object) -> Starlette:
    """Only the three update routes, with fakes injected on ``app.state``."""
    app = Starlette(
        routes=[
            Route("/api/update/status", update_status_endpoint, methods=["GET"]),
            Route("/api/update/stage", update_stage_endpoint, methods=["POST"]),
            Route("/api/update/apply", update_apply_endpoint, methods=["POST"]),
        ]
    )
    for name, value in state.items():
        setattr(app.state, name, value)
    return app


def _task(app: Starlette) -> object:
    return getattr(app.state, "engine_update_task", None)


def test_status_reports_mode_and_record() -> None:
    op = _op("staged")
    app = _app(
        engine_update_mode=lambda: "installer",
        engine_update_read=lambda: op,
    )

    with TestClient(app) as client:
        resp = client.get("/api/update/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["install_mode"] == "installer"
    # The card needs to know whether to offer the button at all: only an
    # installer-managed env has a staged wheel to apply.
    assert body["can_update"] is True
    # The record is the whole progress display, phase and error included.
    assert body["operation"] == asdict(op)
    assert "error" not in body


def test_status_without_record_is_null() -> None:
    app = _app(
        engine_update_mode=lambda: "installer",
        engine_update_read=lambda: None,
    )

    with TestClient(app) as client:
        resp = client.get("/api/update/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["operation"] is None
    assert body["can_update"] is True


def test_status_read_failure_is_fail_safe() -> None:
    def boom() -> None:
        raise OSError("operation.json: permission denied")

    app = _app(engine_update_mode=lambda: "installer", engine_update_read=boom)

    with TestClient(app) as client:
        resp = client.get("/api/update/status")

    # 200, not 500: the card polls this, and read_operation is already
    # fail-safe — a record it cannot read must read as "no update", with the
    # reason attached, not as a card stuck in an error state.
    assert resp.status_code == 200
    body = resp.json()
    assert body["operation"] is None
    assert body["error"] == "operation.json: permission denied"
    # The engine is still described, so the card can tell an engine it may
    # update from one it may not.
    assert body["can_update"] is True


def test_stage_rejects_non_installer_mode() -> None:
    calls: list[str | None] = []
    app = _app(
        engine_update_mode=lambda: "editable",
        engine_update_read=lambda: None,
        engine_update_stage=lambda version: calls.append(version),
    )

    with TestClient(app) as client:
        resp = client.post("/api/update/stage")

    # 400: an editable checkout has no receipt, so there is no install to swap
    # and no receipt to write the previous one aside to.
    assert resp.status_code == 400
    assert resp.json() == {"error": NOT_INSTALLER}
    assert calls == []
    assert _task(app) is None


def test_stage_starts_and_is_idempotent_while_running(runner: _Runner) -> None:
    app = _app(
        engine_update_mode=lambda: "installer",
        engine_update_read=lambda: None,
        engine_update_stage=runner,
    )

    with TestClient(app) as client, runner.held():
        first = client.post("/api/update/stage")

        assert first.status_code == 202
        assert first.json() == {"started": True, "operation": None}

        # The real stager blocks for minutes, so the second POST has to be
        # refused by the running task, not by a sleep in the test.
        runner.started()
        second = client.post("/api/update/stage")

        assert second.status_code == 409
        assert second.json() == ALREADY_RUNNING
        assert runner.calls == [None]


def test_stage_passes_the_requested_version(runner: _Runner) -> None:
    app = _app(
        engine_update_mode=lambda: "installer",
        engine_update_read=lambda: None,
        engine_update_stage=runner,
    )

    with TestClient(app) as client, runner.held():
        resp = client.post("/api/update/stage", json={"version": "2.0.16"})

        assert resp.status_code == 202
        runner.started()

    assert runner.calls == ["2.0.16"]


def test_stage_rejects_when_record_not_terminal(runner: _Runner) -> None:
    app = _app(
        engine_update_mode=lambda: "installer",
        engine_update_read=lambda: _op("downloading"),
        engine_update_stage=runner,
    )

    with TestClient(app) as client:
        resp = client.post("/api/update/stage")

    # 409: a record still in `downloading` is a run that never reached a
    # terminal phase, and a second stager would race the first over the same
    # operation file and staged env.
    assert resp.status_code == 409
    assert resp.json() == ALREADY_RUNNING
    assert runner.calls == []
    assert _task(app) is None


def test_apply_requires_staged_record(runner: _Runner) -> None:
    for record in (None, _op("failed")):
        app = _app(
            engine_update_mode=lambda: "installer",
            engine_update_read=lambda record=record: record,
            engine_update_apply=runner,
        )

        with TestClient(app) as client:
            resp = client.post("/api/update/apply")

        # 400, with the CLI's wording: there is no staged env to swap, so the
        # operator has to stage first.
        assert resp.status_code == 400, record
        assert resp.json() == NOTHING_STAGED, record
        assert _task(app) is None, record

    assert runner.calls == []


def test_apply_starts_when_staged(runner: _Runner) -> None:
    app = _app(
        engine_update_mode=lambda: "installer",
        engine_update_read=lambda: _op("staged"),
        engine_update_apply=runner,
    )

    with TestClient(app) as client, runner.held():
        resp = client.post("/api/update/apply")

        assert resp.status_code == 202
        assert resp.json() == {"started": True}
        runner.started()

    assert runner.calls == [None]


def test_apply_rejects_non_installer_mode(runner: _Runner) -> None:
    app = _app(
        engine_update_mode=lambda: "bundled_app",
        engine_update_read=lambda: _op("staged"),
        engine_update_apply=runner,
    )

    with TestClient(app) as client:
        resp = client.post("/api/update/apply")

    assert resp.status_code == 400
    assert resp.json() == {"error": NOT_INSTALLER}
    assert runner.calls == []
    assert _task(app) is None


# A logged-in operator may start an engine update from the browser, exactly as
# they may POST /api/admin/restart. Neither "public" nor "loopback-only" is that:
# `_PUBLIC_API` is unauthenticated from anywhere, and `_LOOPBACK_ONLY_API` exists
# for the CLI's pre-session drain handshake, not for a browser action.
def test_update_routes_require_a_session() -> None:
    for path in ("/api/update/status", "/api/update/stage", "/api/update/apply"):
        assert path not in auth._PUBLIC_API, path
        assert path not in auth._LOOPBACK_ONLY_API, path
