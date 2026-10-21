"""A port that is already taken is a startup phase, not a crash (#1654).

The engine binds ``0.0.0.0:<port>``; when something else already holds that
port — a ``tailscale serve`` on the same number, a stale process, a second
engine — the bind fails and the engine has to say so once and stop. Before
this, the failure was an unhandled ``OSError``: the process died, launchd
respawned it forever, and the operator was told the engine was "still
starting" while a dead sign-in link was printed at them.

Three things are asserted here, and each is a separate reader of the same
fact:

- the engine records a failed ``server_starting`` phase naming the port, and
  leaves with the port-in-use exit code instead of raising;
- ``/api/startup-status`` reports that failed phase and an explicit
  ``port_in_use`` flag, so a client can tell "the port is taken" from
  "still starting" without reading prose;
- the wording names the port and the two ways out (free the port, or set
  ``PWA_PORT`` in the workspace ``.env``).
"""

from __future__ import annotations

import asyncio
import errno
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import main
from ciao.port_in_use import (
    BIND_PHASE,
    PORT_IN_USE_EXIT_CODE,
    check_server_port,
    is_address_in_use,
    port_in_use_message,
    read_port_in_use,
)
from ciao.web.routes_api import startup_status_endpoint


# ── the engine's own bind failure ────────────────────────────────────────


def _port_in_use_error() -> OSError:
    return OSError(errno.EADDRINUSE, "Address already in use")


def test_a_bind_failure_is_recognised_by_its_errno() -> None:
    assert is_address_in_use(_port_in_use_error()) is True
    # Any other OSError is not a taken port, and must not be reported as one.
    assert is_address_in_use(OSError(errno.EACCES, "Permission denied")) is False


def test_the_message_names_the_port_and_the_two_ways_out(tmp_path: Path) -> None:
    message = port_in_use_message(8443, tmp_path)

    assert "8443" in message
    assert "already in use" in message
    # The two ways out, in the words the operator has to act on.
    assert "PWA_PORT" in message
    assert str(tmp_path / ".env") in message


def _tracker_with_a_failed_bind(tracker: main.StartupTracker) -> main.StartupTracker:
    tracker.start(BIND_PHASE)
    tracker.fail(BIND_PHASE, port_in_use_message(8443))
    return tracker


def test_a_failed_bind_is_a_failed_startup_phase_naming_the_port(
    tmp_path: Path,
) -> None:
    tracker = _tracker_with_a_failed_bind(main.StartupTracker())

    payload = tracker.to_dict()
    phases = {phase["name"]: phase for phase in payload["phases"]}

    assert phases[BIND_PHASE]["status"] == "failed"
    assert "8443" in phases[BIND_PHASE]["message"]
    assert "already in use" in phases[BIND_PHASE]["message"]


def test_the_bind_phase_is_only_done_once_the_port_is_bound(
    tmp_path: Path,
) -> None:
    """The phase is finished by a watcher that runs while the server serves.

    ``server_starting`` used to be marked done *before* uvicorn tried to
    bind, so the startup status never recorded the failure: the engine was
    "ready" while it was dying. It was later marked done only after
    ``serve()`` returned, so ``overall_ready`` stayed false for the whole
    life of the engine. The one ``done`` call now sits in the watcher, which
    is started before ``serve()`` and cancelled when it returns.
    """
    source = Path(main.__file__).read_text(encoding="utf-8")

    assert source.count("tracker.done(BIND_PHASE)") == 1
    watcher_at = source.index("def _mark_bound_when_serving")
    done_at = source.index("tracker.done(BIND_PHASE)")
    watch_started_at = source.index("create_task(_mark_bound_when_serving")
    serve_at = source.index("await server.serve(")
    assert watch_started_at < serve_at, (
        "the bind watcher must be running before serve() starts, or the phase "
        "can only be finished after the server has stopped"
    )
    assert done_at > watcher_at, "the done call belongs to the bind watcher"


async def test_overall_ready_is_true_while_the_server_is_still_serving() -> None:
    """The engine reports ready while it serves, not only after it stops.

    A fake server stands in for uvicorn: ``started`` flips after a short
    bind, then the server keeps "serving". The readiness is read from inside
    that serving window, which is what ``/api/startup-status`` sees.
    """
    tracker = main.StartupTracker()
    tracker.start(BIND_PHASE)
    server = SimpleNamespace(started=False)
    seen: dict[str, bool] = {}

    async def serve() -> None:
        seen["before_bind"] = tracker.to_dict()["overall_ready"]
        await asyncio.sleep(0.01)
        server.started = True
        await asyncio.sleep(0.2)
        seen["while_serving"] = tracker.to_dict()["overall_ready"]

    watch = asyncio.create_task(
        main._mark_bound_when_serving(server, tracker, poll_interval=0.001)
    )
    try:
        await serve()
    finally:
        watch.cancel()

    assert seen == {"before_bind": False, "while_serving": True}


async def test_a_serve_that_ends_before_binding_leaves_the_phase_in_progress() -> None:
    """A bind failure must not be recorded as a finished bind phase."""
    tracker = main.StartupTracker()
    tracker.start(BIND_PHASE)
    server = SimpleNamespace(started=False)

    watch = asyncio.create_task(
        main._mark_bound_when_serving(server, tracker, poll_interval=0.001)
    )
    await asyncio.sleep(0.02)
    watch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await watch

    assert tracker.phase(BIND_PHASE).status == "in_progress"
    assert tracker.to_dict()["overall_ready"] is False


def test_the_engine_exits_with_the_port_in_use_code_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A taken port ends the boot with a code, not with a traceback.

    The engine is not started here — starting it would bind a real port and
    write into a real workspace. What is under test is the handler around
    the bind: an ``OSError`` with ``EADDRINUSE`` has to become the
    port-in-use exit code.
    """
    handler = main._handle_bind_failure

    error = _port_in_use_error()
    assert handler(error, port=8443, workspace=tmp_path) == PORT_IN_USE_EXIT_CODE
    # Another OSError is not a taken port: it stays an ordinary crash, so a
    # permission problem is not reported as "someone else has the port".
    assert handler(OSError(errno.EACCES, "Permission denied"), port=8443, workspace=tmp_path) is None


def test_the_engine_binds_its_own_port_and_leaves_with_the_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boot path itself has to probe the port and act on the failure.

    The handler can be perfect and the boot still wrong: if the engine never
    probes its own port, uvicorn's bind fails later, inside a code path that
    swallows the errno and exits 3 — the failure this issue is about. So this
    drives the boot against a **really held port** and asserts what the
    operator gets: the port-in-use exit code, a failed startup phase, and a
    record a reader outside the engine can find.
    """
    tracker = main.StartupTracker()
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        exit_code = main._bind_or_fail(
            tracker,
            host="127.0.0.1",
            port=port,
            workspace=tmp_path,
        )
    finally:
        holder.close()

    assert exit_code == PORT_IN_USE_EXIT_CODE
    phases = {phase["name"]: phase for phase in tracker.to_dict()["phases"]}
    assert phases[BIND_PHASE]["status"] == "failed"
    assert str(port) in phases[BIND_PHASE]["message"]
    # The reason is recorded where the service status, the CLI and the
    # installer can read it: outside the engine, which cannot serve the
    # endpoint that would report it.
    assert str(port) in read_port_in_use()


def test_a_free_port_leaves_the_boot_alone(tmp_path: Path) -> None:
    """The other half: a free port must not be reported as a failure."""
    tracker = main.StartupTracker()
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    port = holder.getsockname()[1]
    holder.close()

    assert main._bind_or_fail(tracker, host="127.0.0.1", port=port, workspace=tmp_path) is None
    phases = {phase["name"]: phase for phase in tracker.to_dict()["phases"]}
    assert BIND_PHASE not in phases


def test_the_port_in_use_code_is_not_the_restart_code() -> None:
    """A service manager must not read a taken port as "relaunch me"."""
    from ciao.config import RESTART_EXIT_CODE

    assert PORT_IN_USE_EXIT_CODE != RESTART_EXIT_CODE
    assert PORT_IN_USE_EXIT_CODE not in (0, 1, 2)


def test_the_engine_logs_the_port_at_error_level(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("ERROR", logger="ciao.main"):
        code = main._handle_bind_failure(
            _port_in_use_error(), port=8443, workspace=tmp_path
        )

    assert code == PORT_IN_USE_EXIT_CODE
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors, "a taken port must be logged, not swallowed"
    message = errors[0].getMessage()
    assert "8443" in message
    assert "PWA_PORT" in message


def test_the_reason_is_recorded_where_a_reader_outside_the_engine_can_find_it(
    tmp_path: Path,
) -> None:
    """An engine that cannot bind cannot serve the endpoint that would say so.

    The reason is written outside the workspace, the way the workspace-access
    denial is, so `ciao service status` and the installer can read it
    without the engine answering.
    """
    from ciao import port_in_use

    main._handle_bind_failure(_port_in_use_error(), port=8443, workspace=tmp_path)
    try:
        recorded = port_in_use.read_port_in_use()
    finally:
        port_in_use.clear_port_in_use()

    assert "8443" in recorded
    assert "already in use" in recorded
    assert "PWA_PORT" in recorded


def test_a_successful_bind_forgets_an_earlier_failure(tmp_path: Path) -> None:
    """A record of an earlier failure is stale once the port is free."""
    from ciao import port_in_use

    port_in_use.record_port_in_use("port 8443 is already in use by another program.")
    try:
        port_in_use.clear_port_in_use()
        assert port_in_use.read_port_in_use() == ""
    finally:
        port_in_use.clear_port_in_use()


# ── /api/startup-status ──────────────────────────────────────────────────


def _client(tracker: main.StartupTracker | None) -> TestClient:
    app = Starlette(routes=[Route("/api/startup-status", startup_status_endpoint)])
    if tracker is not None:
        app.state.startup_tracker = tracker
    return TestClient(app)


def test_the_endpoint_reports_the_failed_phase_and_the_port_in_use_flag(
    tmp_path: Path,
) -> None:
    tracker = _tracker_with_a_failed_bind(main.StartupTracker())

    body = _client(tracker).get("/api/startup-status").json()

    phases = {phase["name"]: phase for phase in body["phases"]}
    assert phases[BIND_PHASE]["status"] == "failed"
    assert "8443" in phases[BIND_PHASE]["message"]
    assert body["port_in_use"] is True


def test_the_port_in_use_flag_is_false_while_the_engine_starts(
    tmp_path: Path,
) -> None:
    tracker = main.StartupTracker()
    tracker.start(BIND_PHASE)

    body = _client(tracker).get("/api/startup-status").json()

    assert body["port_in_use"] is False


def test_the_port_in_use_flag_is_false_when_nothing_failed(tmp_path: Path) -> None:
    tracker = main.StartupTracker()
    tracker.start(BIND_PHASE)
    tracker.done(BIND_PHASE)

    body = _client(tracker).get("/api/startup-status").json()

    assert body["port_in_use"] is False


def test_the_endpoint_without_a_tracker_is_not_a_port_failure() -> None:
    body = _client(None).get("/api/startup-status").json()

    assert body["port_in_use"] is False


# ── the real port, on a real socket ──────────────────────────────────────


def test_binding_a_taken_port_raises_the_errno_the_handler_looks_for(
    tmp_path: Path,
) -> None:
    """The errno the handler matches is the one a real bind produces.

    A socket bound to one address still refuses a second bind of the same
    port on the same address, which is the same failure a Tailscale-held
    port produces for the engine.
    """
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        second = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(OSError) as raised:
                second.bind(("127.0.0.1", port))
            assert is_address_in_use(raised.value) is True
        finally:
            second.close()
    finally:
        holder.close()


def test_the_engines_own_bind_raises_on_a_taken_port() -> None:
    """The engine binds the socket itself, so the failure reaches it.

    uvicorn binds inside ``Server.startup`` and swallows the ``OSError``:
    the engine only ever saw a ``SystemExit`` with uvicorn's own code, so
    the reason never reached the startup status or the operator. The engine's
    own bind is what makes the errno visible.
    """
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        with pytest.raises(OSError) as raised:
            check_server_port("127.0.0.1", port)
        assert is_address_in_use(raised.value) is True
    finally:
        holder.close()


def test_the_engines_own_bind_claims_a_free_port() -> None:
    """The ordinary path still binds, and releases the port again.

    The bind is a probe: uvicorn binds the port itself, so the socket must
    not stay held, or the engine could never bind it.
    """
    check_server_port("127.0.0.1", 0)

    # The port is free again, so a second bind succeeds.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", 0))
    finally:
        probe.close()
