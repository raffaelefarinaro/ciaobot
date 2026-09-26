from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import asdict

import httpx
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


async def _thread_running(runner: _Runner) -> None:
    """Wait for the background thread without blocking the loop it runs on.

    ``_Runner.started`` blocks, and the job it is waiting for is started by a
    task on that very loop — blocking here would stop the task being waited on.
    """
    assert await asyncio.to_thread(runner.entered.wait, 10), (
        "the background update thread never ran"
    )


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


@contextlib.asynccontextmanager
async def _asgi_client(app: Starlette) -> AsyncIterator[httpx.AsyncClient]:
    """A client that can overlap two requests on one event loop.

    ``TestClient`` is a portal thread with one blocking call at a time, so it
    cannot express "this request is still in flight while the next one runs" —
    which is the only way to test the stage guard against a second request.
    Driving the app through the ASGI transport instead puts both requests on
    the loop the route runs on, and a streaming body lets one of them sit
    mid-``await`` while the other is served.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://engine") as client:
        yield client


async def _settled(app: Starlette) -> None:
    """Wait for the background update task the route registered.

    A stager that fails at once can finish before this runs — the route's
    done-callback clears the slot — so a cleared slot *is* the settled state
    rather than a missing task. Awaiting the task is the event; a sleep would
    only be a guess at how long the half takes.
    """
    task = _task(app)
    assert isinstance(task, asyncio.Task) or task is None
    if task is not None:
        await asyncio.wait_for(task, timeout=10)


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


async def test_two_overlapping_stages_start_one_job(runner: _Runner) -> None:
    """Two stage requests in flight at once may only start one job.

    The overlap is at the ASGI level on purpose. A client whose body has not
    finished arriving is parked inside the route, and that is exactly the
    window the guard used to run in: it passed, then awaited the body, and the
    second request found no registered job to refuse. Both got a 202 and both
    built a release over one operation file, one lock and one staged env.
    """
    app = _app(
        engine_update_mode=lambda: "installer",
        engine_update_read=lambda: None,
        engine_update_stage=runner,
    )
    parked = asyncio.Event()
    arrive = asyncio.Event()

    async def slow_body() -> AsyncIterator[bytes]:
        # Half a body, then nothing until the test says so. The request is in
        # flight with its body unread, which is the state the guard has to be
        # safe in — not two finished POSTs, one after the other.
        yield b'{"version":'
        parked.set()
        await arrive.wait()
        yield b'"2.0.17"}'

    with runner.held():
        async with _asgi_client(app) as client:
            first = asyncio.create_task(client.post("/api/update/stage", content=slow_body()))
            await asyncio.wait_for(parked.wait(), timeout=10)

            # Served while the first request is still waiting for its body.
            second = await client.post("/api/update/stage", json={"version": "2.0.16"})
            await _thread_running(runner)

            arrive.set()
            first_resp = await asyncio.wait_for(first, timeout=10)
            # The one job that was started is all this test needs to see, so
            # it is let go and awaited rather than left for the teardown.
            runner.release.set()
            await _settled(app)

    assert [second.status_code, first_resp.status_code] == [202, 409]
    assert first_resp.json() == ALREADY_RUNNING
    # The refused request is the one whose body was still arriving: a job slot
    # is claimed only once the body it answers to is in hand, so the request
    # that got there first owns the run — and the other one's version is never
    # used at all.
    assert runner.calls == ["2.0.16"]


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


# A 202 is a promise the run started, and `engine_update` raises before it
# writes any record for several ordinary refusals — a latest-version lookup
# that failed, a version that is not a release, an engine already on the
# target, a lock it could not take. Swallowing those leaves the card polling a
# null record with no reason, so the route has to carry the reason itself.


def _failing_stage(message: str) -> Callable[[str | None], None]:
    """A stager that refuses the way the coordinator refuses, before a record."""

    def stage(version: str | None = None) -> None:
        raise engine_update.UpdateError(message)

    return stage


async def test_pre_record_failure_is_reported_through_status() -> None:
    app = _app(
        engine_update_mode=lambda: "installer",
        engine_update_read=lambda: None,
        engine_update_stage=_failing_stage(
            "could not resolve the latest release: network is unreachable"
        ),
    )

    async with _asgi_client(app) as client:
        started = await client.post("/api/update/stage")
        assert started.status_code == 202
        await _settled(app)
        status = await client.get("/api/update/status")

    # The card is the only thing the operator has here, so it has to carry the
    # refusal: the run was accepted and then died before it could record why,
    # and "no operation, no error" is indistinguishable from "never asked".
    body = status.json()
    assert body["operation"] is None
    assert body["error"] == "could not resolve the latest release: network is unreachable"


async def test_already_current_version_is_reported_through_status() -> None:
    # The engine is already on the target and the record still describes the
    # last run: raising here is correct, and it is the one refusal an operator
    # is most likely to hit by accident.
    stale = _op("failed")
    app = _app(
        engine_update_mode=lambda: "installer",
        engine_update_read=lambda: stale,
        engine_update_stage=_failing_stage("already on 1.2.3"),
    )

    async with _asgi_client(app) as client:
        started = await client.post("/api/update/stage")
        assert started.status_code == 202
        await _settled(app)
        status = await client.get("/api/update/status")

    body = status.json()
    assert body["error"] == "already on 1.2.3"
    # The record it was parked against is still the record, and is still shown:
    # the new refusal is an addition to the truth, not a replacement for it.
    assert body["operation"] == asdict(stale)


async def test_persisted_failure_record_wins_over_the_refusal() -> None:
    """A record the coordinator wrote is the truth; nothing rides along."""
    written: dict[str, engine_update.Operation | None] = {"record": None}

    def stage(version: str | None = None) -> None:
        record = _op("failed")
        record.error = "uv venv returned non-zero exit status 1: no space left on device"
        written["record"] = record
        # Not the record's wording: the record is the explanation, and the
        # thread's message is only ever a fallback for a run that wrote none.
        raise engine_update.UpdateError("uv venv returned non-zero exit status 1")

    app = _app(
        engine_update_mode=lambda: "installer",
        engine_update_read=lambda: written["record"],
        engine_update_stage=stage,
    )

    async with _asgi_client(app) as client:
        started = await client.post("/api/update/stage")
        assert started.status_code == 202
        await _settled(app)
        status = await client.get("/api/update/status")

    # The run moved the record, so the record is the answer and the transient
    # message is dropped: a second explanation next to the first would leave
    # the card showing two different failures for one run.
    body = status.json()
    assert body["operation"] == asdict(written["record"])
    assert body["operation"]["error"] == "uv venv returned non-zero exit status 1: no space left on device"
    assert "error" not in body


async def test_parked_refusal_is_dropped_once_the_record_moves() -> None:
    """A later run's record retires an earlier run's parked refusal."""
    records: list[engine_update.Operation | None] = [None]

    def read() -> engine_update.Operation | None:
        return records[-1]

    app = _app(
        engine_update_mode=lambda: "installer",
        engine_update_read=read,
        engine_update_error="already on 1.2.3",
        engine_update_error_for="",
    )

    async with _asgi_client(app) as client:
        first = await client.get("/api/update/status")
        # A stage the operator ran after the refusal wrote its own record.
        records.append(_op("staged"))
        second = await client.get("/api/update/status")

    assert first.json()["error"] == "already on 1.2.3"
    # Nothing is wrong any more, and a card that kept the old refusal would
    # report a failed update for an engine that is sitting on a staged one.
    assert second.json()["operation"] == asdict(_op("staged"))
    assert "error" not in second.json()


# A logged-in operator may start an engine update from the browser, exactly as
# they may POST /api/admin/restart. Neither "public" nor "loopback-only" is that:
# `_PUBLIC_API` is unauthenticated from anywhere, and `_LOOPBACK_ONLY_API` exists
# for the CLI's pre-session drain handshake, not for a browser action.
def test_update_routes_require_a_session() -> None:
    for path in ("/api/update/status", "/api/update/stage", "/api/update/apply"):
        assert path not in auth._PUBLIC_API, path
        assert path not in auth._LOOPBACK_ONLY_API, path
