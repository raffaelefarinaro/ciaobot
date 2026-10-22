"""The browser terminal's byte stream: `/ws/terminal/{session_id}` (#1800).

The real route, the real grant store and the real session manager, with a
fake process behind it: no pty is opened here, and what is under test is
the wire contract and the door in front of it, not a shell.

What is pinned, beyond the shapes:

* the door, in the unlock's order — the terminal policy, the signed
  session, a live grant — and the close code each refusal earns (4403 for
  the policy, 4001 for a missing session, 4401 for a missing grant);
* the first frame: exactly ``{type:'attach', offset:int, takeover:bool}``,
  within five seconds, and nothing else;
* one controller per session: a second connection needs ``takeover:true``
  and otherwise is 4409;
* the snapshot: ``ready``, the replay frames, ``replay_end`` — and the
  truncated snapshot a client that asks for an evicted offset gets;
* the live stream: ``output`` frames with exact byte offsets and strict
  base64, and a cursor that advances only over bytes actually sent;
* the client frames: ``input`` (strict base64, 64 KiB), ``resize`` (the
  manager's own bounds) and ``ping``/``pong``;
* the refusals: a revoked grant closes 4401, a consumer that fell behind
  the ring closes 4413, a malformed frame is ``bad_frame`` and 1002;
* the end: an ``exit`` frame, and a disconnect that detaches without
  ending the session.
"""

from __future__ import annotations

import base64
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Callable, Iterator

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ciao.terminal_auth import TERMINAL_COOKIE, TerminalGrantStore
from ciao.terminal_sessions import TerminalSessionManager
from ciao.web import routes_terminal_ws
from ciao.web.auth import SESSION_COOKIE, AuthMiddleware
from ciao.web.routes_terminal import terminal_unlock
from ciao.web.routes_terminal_sessions import (
    terminal_sessions_create,
    terminal_sessions_end,
)
from ciao.web.routes_terminal_ws import terminal_stream

SECRET = "test-secret"
PASSWORD = "correct horse battery staple"
BASE = "http://localhost:8443"
UNLOCK = "/api/terminal/unlock"
SESSIONS = "/api/terminal/sessions"
STREAM = "/ws/terminal"
#: The engine's own address, as a browser would dial it. The
#: TestClient's relative WebSocket URL is "testserver", which is neither
#: the engine's host nor a loopback name, so the handshake is absolute.
WS_BASE = "ws://localhost:8443"

#: The close codes the wire contract fixes.
CLOSE_POLICY = 4403
CLOSE_UNAUTHORIZED = 4401
CLOSE_NOT_FOUND = 4404
CLOSE_TAKEN_OVER = 4409
CLOSE_SLOW_CONSUMER = 4413
CLOSE_BAD_FRAME = 1002
CLOSE_UNAUTHORIZED_APP = 4001

#: The keys the ``ready`` frame carries: the facts a client needs to draw
#: a terminal, and nothing else.
_READY_KEYS = {
    "type",
    "session_id",
    "folder",
    "cols",
    "rows",
    "seq",
    "replay_start",
    "truncated",
}

#: How much output the ring holds, and how much one read may take.
_CHUNK = 64 * 1024


# ── the fakes ──────────────────────────────────────────────────────────────


class FakeProcess:
    """A terminal the test drives: quiet until it is given something to say.

    It is not a pty. ``read`` reports the terminal as quiet while there is
    nothing to say, and the bytes it is given are handed out in the size
    the manager asks for — never more, because the manager's replay ring
    refuses a chunk over 64 KiB and would end the session on it.
    """

    def __init__(self, pid: int = 1) -> None:
        self.pid = pid
        self.closed = False
        self.exit_code: int | None = None
        self.written = bytearray()
        self.size = (80, 24)
        self.pending = bytearray()
        self.finished = False

    def read(self, size: int = 4096) -> bytes | None:
        if self.finished:
            return b""
        if not self.pending:
            return None
        chunk = bytes(self.pending[:size])
        del self.pending[:size]
        return chunk

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        return len(data)

    def resize(self, cols: int, rows: int) -> None:
        self.size = (cols, rows)

    def poll(self) -> int | None:
        return self.exit_code

    def close(self) -> None:
        self.closed = True

    def say(self, data: bytes) -> None:
        """Queue output for the reader to pick up."""
        self.pending.extend(data)


class FakeFactory:
    """A process factory the test controls: how many, and which one."""

    def __init__(self) -> None:
        self.calls = 0
        self.processes: list[FakeProcess] = []

    def __call__(self, *, cwd: Path, cols: int = 80, rows: int = 24) -> FakeProcess:
        self.calls += 1
        process = FakeProcess(pid=4_000 + self.calls)
        self.processes.append(process)
        return process

    @property
    def process(self) -> FakeProcess:
        assert self.processes, "no terminal was started"
        return self.processes[-1]


class FakePcm:
    """The chat/project lookup ``resolve_folder`` asks the engine for."""

    def __init__(self, chats: dict[str, Any], projects: dict[str, Any]) -> None:
        self._chats, self._projects = chats, projects

    def get_chat(self, chat_id: str) -> Any:
        return self._chats.get(chat_id)

    def get_project(self, project_id: str) -> Any:
        return self._projects.get(project_id)


def _pcm(root: Path) -> FakePcm:
    """One chat in one project, and that project's folder is ``root``."""
    project = SimpleNamespace(id="p1", repo_path=str(root), workspace="")
    chat = SimpleNamespace(id="c1", project_id="p1", code_mode=False, repo_location={})
    return FakePcm({"c1": chat}, {"p1": project})


def _app(tmp_path: Path, *, factory: FakeFactory | None = None) -> Starlette:
    """A real app with the real routes, the real manager and a fake pty."""
    serializer = URLSafeTimedSerializer(SECRET)
    manager = TerminalSessionManager(factory=factory or FakeFactory())

    @asynccontextmanager
    async def _lifespan(_app: Starlette) -> AsyncIterator[None]:
        # One event loop for the whole test: the manager's tasks belong to
        # the loop that made them, and the client runs the lifespan.
        await manager.start()
        try:
            yield
        finally:
            await manager.shutdown()

    app = Starlette(
        routes=[
            Route(UNLOCK, terminal_unlock, methods=["POST"]),
            Route(SESSIONS, terminal_sessions_create, methods=["POST"]),
            Route(f"{SESSIONS}/{{session_id}}", terminal_sessions_end, methods=["DELETE"]),
            WebSocketRoute(f"{STREAM}/{{session_id}}", terminal_stream),
        ],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
        lifespan=_lifespan,
    )
    app.state.serializer = serializer
    app.state.config = SimpleNamespace(
        pwa_auth_token=PASSWORD,
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
    )
    app.state.terminal_clock = time.monotonic
    app.state.terminal_grants = TerminalGrantStore(clock=app.state.terminal_clock)
    app.state.terminal_sessions = manager
    app.state.project_chat_manager = _pcm(tmp_path)
    return app


def _origin(origin: str = BASE) -> dict[str, str]:
    """The browser's own origin header."""
    return {"Origin": origin}


@contextmanager
def _client(
    app: Starlette,
    *,
    peer: str = "127.0.0.1",
    base_url: str = BASE,
    signed_in: bool = True,
    unlocked: bool = True,
) -> Iterator[TestClient]:
    """A client with an owner session, and with the terminal unlocked.

    The session lives in the cookie jar rather than in an explicit header,
    so the grant cookie the unlock sets travels with it.
    """
    with TestClient(app, base_url=base_url, client=(peer, 50000)) as client:
        if signed_in:
            serializer = app.state.serializer
            client.cookies.set(SESSION_COOKIE, serializer.dumps({"user": "owner"}))
        if unlocked:
            response = client.post(
                UNLOCK, headers=_origin(base_url)
            )
            assert response.status_code == 200
        yield client


def _issue_grant(app: Starlette, client: TestClient) -> str:
    """Mint a grant for this client's session, without unlocking."""
    session = client.cookies.get(SESSION_COOKIE) or ""
    token, _grant = app.state.terminal_grants.issue(session)
    return token


def _create_session(client: TestClient) -> str:
    """Open a terminal session over HTTP and return its id."""
    response = client.post(SESSIONS, json={"chat_id": "c1"}, headers=_origin())
    assert response.status_code == 200
    return str(response.json()["session"]["session_id"])


def _wait_for_seq(
    manager: TerminalSessionManager,
    session_id: str,
    expected: int,
    timeout: float = 5.0,
) -> None:
    """Wait until the session's stream has reached ``expected`` bytes."""
    _wait_until(
        lambda: manager.get(session_id).seq >= expected,
        timeout=timeout,
        what="the terminal output",
    )


def _wait_until(
    predicate: Callable[[], bool], timeout: float = 5.0, what: str = "condition"
) -> None:
    """Wait for a predicate the engine's own loop has to reach."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def _close_code(ws: Any) -> int:
    """The close code the server ended the socket with."""
    with pytest.raises(WebSocketDisconnect) as exc:
        while True:
            ws.receive_json()
    return int(exc.value.code)


def _attach(ws: Any, offset: int = 0, takeover: bool = False) -> dict[str, Any]:
    """Send the attach frame and read the ``ready`` frame back."""
    ws.send_json({"type": "attach", "offset": offset, "takeover": takeover})
    ready = ws.receive_json()
    assert ready["type"] == "ready"
    return ready


def _replay(ws: Any) -> list[dict[str, Any]]:
    """The replay frames, up to and including the ``replay_end``."""
    frames: list[dict[str, Any]] = []
    while True:
        frame = ws.receive_json()
        frames.append(frame)
        if frame["type"] == "replay_end":
            return frames


# ── the door: policy, session, grant ──────────────────────────────────────


def test_a_foreign_origin_is_refused_before_the_socket_is_accepted(
    tmp_path: Path,
) -> None:
    """The terminal's own policy is the first gate, and it is the strict one."""
    app = _app(tmp_path)
    with _client(app) as client:
        session_id = _create_session(client)
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                f"{WS_BASE}{STREAM}/{session_id}", headers=_origin("http://evil.example:8443")
            ):
                pass  # pragma: no cover
    assert exc.value.code == CLOSE_POLICY


def test_a_remote_plain_http_caller_is_refused(tmp_path: Path) -> None:
    """A remote browser must arrive over HTTPS, even with a live grant."""
    app = _app(tmp_path)
    with _client(app, peer="100.64.0.7", base_url="http://app.example", unlocked=False) as client:
        token = _issue_grant(app, client)
        client.cookies.set(TERMINAL_COOKIE, token)
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                "ws://app.example" + f"{STREAM}/abcdef",
                headers=_origin("http://app.example"),
            ):
                pass  # pragma: no cover
    assert exc.value.code == CLOSE_POLICY


def test_a_socket_without_a_session_is_refused(tmp_path: Path) -> None:
    """The signed session is required, and the app's own gate answers."""
    app = _app(tmp_path)
    with _client(app, signed_in=False, unlocked=False) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"{WS_BASE}{STREAM}/abcdef", headers=_origin()):
                pass  # pragma: no cover
    assert exc.value.code == CLOSE_UNAUTHORIZED_APP


def test_a_browser_that_has_not_unlocked_is_refused(tmp_path: Path) -> None:
    """No grant is 4401, and the grant is checked before the session."""
    app = _app(tmp_path)
    with _client(app, unlocked=False) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(f"{WS_BASE}{STREAM}/abcdef", headers=_origin()):
                pass  # pragma: no cover
    assert exc.value.code == CLOSE_UNAUTHORIZED


def test_a_session_that_is_not_there_is_refused(tmp_path: Path) -> None:
    """The socket names a session; a session that is gone is a 4404."""
    app = _app(tmp_path)
    with _client(app) as client:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                f"{WS_BASE}{STREAM}/not-a-session", headers=_origin()
            ):
                pass  # pragma: no cover
    assert exc.value.code == CLOSE_NOT_FOUND


def test_a_credential_in_the_query_is_not_a_credential(tmp_path: Path) -> None:
    """Cookies only: a bearer in the query string grants nothing."""
    app = _app(tmp_path)
    with _client(app, unlocked=False) as client:
        token = _issue_grant(app, client)
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                f"{WS_BASE}{STREAM}/abcdef?token={token}", headers=_origin()
            ):
                pass  # pragma: no cover
    assert exc.value.code == CLOSE_UNAUTHORIZED


# ── the first frame ────────────────────────────────────────────────────────


def test_a_socket_that_never_attaches_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The attach frame is due within five seconds, and the socket waits."""
    monkeypatch.setattr(routes_terminal_ws, "ATTACH_TIMEOUT_S", 0.05)
    app = _app(tmp_path)
    with _client(app) as client:
        session_id = _create_session(client)
        with client.websocket_connect(
            f"{WS_BASE}{STREAM}/{session_id}", headers=_origin()
        ) as ws:
            assert _close_code(ws) == CLOSE_POLICY


def test_the_attach_window_is_five_seconds() -> None:
    """The contract's window is five seconds; a test may shorten it, not lengthen it."""
    assert 0 < routes_terminal_ws.ATTACH_TIMEOUT_S <= 5.0


@pytest.mark.parametrize(
    "frame",
    [
        {"type": "input", "data": ""},
        {"type": "attach", "offset": 0},
        {"type": "attach", "offset": 0, "takeover": False, "extra": 1},
        {"type": "attach", "offset": "0", "takeover": False},
        {"type": "attach", "offset": 0, "takeover": "no"},
        {"type": "attach", "offset": 0, "takeover": 1},
        {"type": "ping"},
        [],
        "attach",
    ],
    ids=[
        "another type",
        "no takeover",
        "an extra key",
        "a string offset",
        "a string takeover",
        "a number takeover",
        "a ping",
        "an array",
        "a string",
    ],
)
def test_a_first_frame_that_is_not_an_attach_is_refused(
    tmp_path: Path, frame: Any
) -> None:
    """The first frame is exactly the attach frame, and nothing else."""
    app = _app(tmp_path)
    with _client(app) as client:
        session_id = _create_session(client)
        with client.websocket_connect(
            f"{WS_BASE}{STREAM}/{session_id}", headers=_origin()
        ) as ws:
            ws.send_json(frame)
            assert _close_code(ws) == CLOSE_POLICY


def test_a_first_frame_that_is_not_json_is_refused(tmp_path: Path) -> None:
    """A frame that is not JSON is not an attach frame."""
    app = _app(tmp_path)
    with _client(app) as client:
        session_id = _create_session(client)
        with client.websocket_connect(
            f"{WS_BASE}{STREAM}/{session_id}", headers=_origin()
        ) as ws:
            ws.send_text("{nope")
            assert _close_code(ws) == CLOSE_POLICY


@pytest.mark.parametrize(
    "offset",
    [True, False, 0.0, 1.5, -1, -1000],
    ids=["true", "false", "a float", "a fractional float", "minus one", "far back"],
)
def test_an_offset_that_is_not_a_safe_integer_is_refused(
    tmp_path: Path, offset: Any
) -> None:
    """A bool is an int in Python and a float is not one at all."""
    app = _app(tmp_path)
    with _client(app) as client:
        session_id = _create_session(client)
        with client.websocket_connect(
            f"{WS_BASE}{STREAM}/{session_id}", headers=_origin()
        ) as ws:
            ws.send_json({"type": "attach", "offset": offset, "takeover": False})
            assert _close_code(ws) == CLOSE_POLICY


def test_an_offset_past_the_stream_is_refused(tmp_path: Path) -> None:
    """The offset is an absolute byte count, and it cannot be in the future."""
    app = _app(tmp_path)
    with _client(app) as client:
        session_id = _create_session(client)
        with client.websocket_connect(
            f"{WS_BASE}{STREAM}/{session_id}", headers=_origin()
        ) as ws:
            ws.send_json({"type": "attach", "offset": 1, "takeover": False})
            assert _close_code(ws) == CLOSE_POLICY


# ── the controller ─────────────────────────────────────────────────────────


def test_a_second_connection_is_refused_without_a_takeover(tmp_path: Path) -> None:
    """One session has one controller, and a second one has to say so."""
    app = _app(tmp_path)
    with _client(app) as client:
        session_id = _create_session(client)
        with client.websocket_connect(
            f"{WS_BASE}{STREAM}/{session_id}", headers=_origin()
        ) as first:
            _attach(first)
            with client.websocket_connect(
                f"{WS_BASE}{STREAM}/{session_id}", headers=_origin()
            ) as second:
                second.send_json(
                    {"type": "attach", "offset": 0, "takeover": False}
                )
                assert _close_code(second) == CLOSE_TAKEN_OVER


def test_a_takeover_replaces_the_controller_and_displaces_the_old_one(
    tmp_path: Path,
) -> None:
    """An explicit takeover is the only way in, and the old socket is told."""
    app = _app(tmp_path)
    with _client(app) as client:
        session_id = _create_session(client)
        with client.websocket_connect(
            f"{WS_BASE}{STREAM}/{session_id}", headers=_origin()
        ) as first:
            _attach(first)
            with client.websocket_connect(
                f"{WS_BASE}{STREAM}/{session_id}", headers=_origin()
            ) as second:
                _attach(second, takeover=True)
                # The displaced connection finds out it has been displaced.
                assert _close_code(first) == CLOSE_TAKEN_OVER


# ── the snapshot ───────────────────────────────────────────────────────────


def test_the_ready_frame_carries_the_snapshot_and_nothing_else(
    tmp_path: Path,
) -> None:
    """The snapshot is taken with the controller assignment."""
    app = _app(tmp_path)
    with _client(app) as client:
        session_id = _create_session(client)
        with client.websocket_connect(
            f"{WS_BASE}{STREAM}/{session_id}", headers=_origin()
        ) as ws:
            ready = _attach(ws)
            assert set(ready) == _READY_KEYS
            assert ready["session_id"] == session_id
            assert ready["folder"] == str(tmp_path.resolve())
            assert ready["cols"] == 80
            assert ready["rows"] == 24
            assert ready["seq"] == 0
            assert ready["replay_start"] == 0
            assert ready["truncated"] is False
            # Nothing was said yet, so the replay is empty and it is over.
            assert ws.receive_json() == {
                "type": "replay_end",
                "seq": 0,
                "truncated": False,
            }


def test_the_replay_carries_the_retained_bytes(tmp_path: Path) -> None:
    """A client that reconnects from zero gets the whole retained stream."""
    factory = FakeFactory()
    app = _app(tmp_path, factory=factory)
    manager: TerminalSessionManager = app.state.terminal_sessions
    with _client(app) as client:
        session_id = _create_session(client)
        factory.process.say(b"hello")
        _wait_for_seq(manager, session_id, 5)

        with client.websocket_connect(
            f"{WS_BASE}{STREAM}/{session_id}", headers=_origin()
        ) as ws:
            ready = _attach(ws)
            assert ready["seq"] == 5
            assert ready["truncated"] is False
            frames = _replay(ws)
            assert frames[0]["type"] == "output"
            assert frames[0]["start"] == 0
            assert frames[0]["end"] == 5
            assert frames[0]["data"] == base64.b64encode(b"hello").decode("ascii")
            assert frames[-1] == {"type": "replay_end", "seq": 5, "truncated": False}


def test_a_replay_from_an_evicted_offset_is_truncated(tmp_path: Path) -> None:
    """Bytes that are gone are a truncated snapshot, not a silent gap."""
    factory = FakeFactory()
    app = _app(tmp_path, factory=factory)
    manager: TerminalSessionManager = app.state.terminal_sessions
    with _client(app) as client:
        session_id = _create_session(client)
        # More than the ring holds, so the oldest bytes are evicted.
        factory.process.say(b"x" * (_CHUNK * 6))
        _wait_for_seq(manager, session_id, _CHUNK * 6)

        with client.websocket_connect(
            f"{WS_BASE}{STREAM}/{session_id}", headers=_origin()
        ) as ws:
            ready = _attach(ws)
            assert ready["truncated"] is True
            assert ready["replay_start"] > 0
            assert ready["seq"] == _CHUNK * 6
            frames = _replay(ws)
            assert frames[-1] == {
                "type": "replay_end",
                "seq": _CHUNK * 6,
                "truncated": True,
            }
            # The replay carries exactly the bytes that are still retained.
            replayed = b"".join(
                base64.b64decode(frame["data"], validate=True)
                for frame in frames[:-1]
            )
            assert len(replayed) == _CHUNK * 6 - ready["replay_start"]


# ── the live stream ────────────────────────────────────────────────────────


def test_the_output_frames_carry_the_exact_bytes(tmp_path: Path) -> None:
    """The offsets are absolute byte counts and the payload is strict base64."""
    factory = FakeFactory()
    app = _app(tmp_path, factory=factory)
    with _client(app) as client:
        session_id = _create_session(client)
        with client.websocket_connect(
            f"{WS_BASE}{STREAM}/{session_id}", headers=_origin()
        ) as ws:
            _attach(ws)
            _replay(ws)
            factory.process.say(b"abc")
            frame = ws.receive_json()
            assert frame["type"] == "output"
            assert frame["start"] == 0
            assert frame["end"] == 3
            assert frame["data"] == base64.b64encode(b"abc").decode("ascii")
            assert base64.b64decode(frame["data"], validate=True) == b"abc"


def test_the_client_frames_are_answered(tmp_path: Path) -> None:
    """Input reaches the terminal, a resize reaches the window, a ping is a pong."""
    factory = FakeFactory()
    app = _app(tmp_path, factory=factory)
    with _client(app) as client:
        session_id = _create_session(client)
        with client.websocket_connect(
            f"{WS_BASE}{STREAM}/{session_id}", headers=_origin()
        ) as ws:
            _attach(ws)
            _replay(ws)

            ws.send_json({"type": "input", "data": base64.b64encode(b"ls\n").decode()})
            _wait_until(
                lambda: bytes(factory.process.written) == b"ls\n", what="the input"
            )

            ws.send_json({"type": "resize", "cols": 100, "rows": 30})
            _wait_until(
                lambda: factory.process.size == (100, 30), what="the resize"
            )

            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}


def test_an_input_of_more_than_64_kib_is_refused(tmp_path: Path) -> None:
    """The input cap is 64 KiB, and a frame over it is a bad frame."""
    app = _app(tmp_path)
    with _client(app) as client:
        session_id = _create_session(client)
        with client.websocket_connect(
            f"{WS_BASE}{STREAM}/{session_id}", headers=_origin()
        ) as ws:
            _attach(ws)
            _replay(ws)
            payload = base64.b64encode(b"x" * (_CHUNK + 1)).decode("ascii")
            ws.send_json({"type": "input", "data": payload})
            assert ws.receive_json() == {"type": "error", "reason": "bad_frame"}
            assert _close_code(ws) == CLOSE_BAD_FRAME


def test_an_input_of_exactly_64_kib_is_accepted(tmp_path: Path) -> None:
    """The cap is inclusive: 64 KiB of input is a normal frame."""
    factory = FakeFactory()
    app = _app(tmp_path, factory=factory)
    with _client(app) as client:
        session_id = _create_session(client)
        with client.websocket_connect(
            f"{WS_BASE}{STREAM}/{session_id}", headers=_origin()
        ) as ws:
            _attach(ws)
            _replay(ws)
            payload = base64.b64encode(b"x" * _CHUNK).decode("ascii")
            ws.send_json({"type": "input", "data": payload})
            _wait_until(
                lambda: len(factory.process.written) == _CHUNK, what="the input"
            )
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}


@pytest.mark.parametrize(
    "data",
    ["not base64!", "YWJj=", "***"],
    ids=["not base64", "a bad length", "not the alphabet"],
)
def test_an_input_that_is_not_base64_is_refused(tmp_path: Path, data: str) -> None:
    """The input is strict base64, and a frame that is not is a bad frame."""
    app = _app(tmp_path)
    with _client(app) as client:
        session_id = _create_session(client)
        with client.websocket_connect(
            f"{WS_BASE}{STREAM}/{session_id}", headers=_origin()
        ) as ws:
            _attach(ws)
            _replay(ws)
            ws.send_json({"type": "input", "data": data})
            assert ws.receive_json() == {"type": "error", "reason": "bad_frame"}
            assert _close_code(ws) == CLOSE_BAD_FRAME


@pytest.mark.parametrize(
    "size",
    [{"cols": 10, "rows": 2}, {"cols": 501, "rows": 30}, {"cols": 80, "rows": 201}],
    ids=["too narrow", "too wide", "too tall"],
)
def test_a_resize_out_of_bounds_is_refused_by_the_manager(
    tmp_path: Path, size: dict[str, int]
) -> None:
    """The window bounds are the manager's, and it says so."""
    app = _app(tmp_path)
    with _client(app) as client:
        session_id = _create_session(client)
        with client.websocket_connect(
            f"{WS_BASE}{STREAM}/{session_id}", headers=_origin()
        ) as ws:
            _attach(ws)
            _replay(ws)
            ws.send_json({"type": "resize", **size})
            assert ws.receive_json() == {"type": "error", "reason": "invalid_size"}
            # The socket is still usable: the refusal was about the size.
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}


@pytest.mark.parametrize(
    "frame",
    [
        {"type": "nope"},
        {"type": "input"},
        {"type": "input", "data": 5},
        {"type": "resize", "cols": 80},
        {"type": "ping", "extra": 1},
        {"type": "input", "data": "", "extra": 1},
    ],
    ids=[
        "an unknown type",
        "an input with no data",
        "an input with a number",
        "a resize with no rows",
        "a ping with an extra key",
        "an input with an extra key",
    ],
)
def test_a_malformed_client_frame_is_a_bad_frame(tmp_path: Path, frame: Any) -> None:
    """A frame the protocol does not describe is refused, and it is bounded."""
    app = _app(tmp_path)
    with _client(app) as client:
        session_id = _create_session(client)
        with client.websocket_connect(
            f"{WS_BASE}{STREAM}/{session_id}", headers=_origin()
        ) as ws:
            _attach(ws)
            _replay(ws)
            ws.send_json(frame)
            assert ws.receive_json() == {"type": "error", "reason": "bad_frame"}
            assert _close_code(ws) == CLOSE_BAD_FRAME


# ── the end of the stream ──────────────────────────────────────────────────


def test_a_shell_that_exits_says_so(tmp_path: Path) -> None:
    """The exit frame carries the shell's own exit code."""
    factory = FakeFactory()
    app = _app(tmp_path, factory=factory)
    with _client(app) as client:
        session_id = _create_session(client)
        with client.websocket_connect(
            f"{WS_BASE}{STREAM}/{session_id}", headers=_origin()
        ) as ws:
            _attach(ws)
            _replay(ws)
            factory.process.exit_code = 7
            factory.process.finished = True
            assert ws.receive_json() == {"type": "exit", "code": 7}


def test_a_revoked_grant_closes_the_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The grant is re-checked, and a revoked one ends the stream."""
    monkeypatch.setattr(routes_terminal_ws, "GRANT_WATCHDOG_INTERVAL_S", 0.05)
    app = _app(tmp_path)
    with _client(app) as client:
        session_id = _create_session(client)
        with client.websocket_connect(
            f"{WS_BASE}{STREAM}/{session_id}", headers=_origin()
        ) as ws:
            _attach(ws)
            _replay(ws)
            token = client.cookies.get(TERMINAL_COOKIE) or ""
            assert token
            app.state.terminal_grants.revoke(token)
            assert _close_code(ws) == CLOSE_UNAUTHORIZED


def test_the_grant_watchdog_is_at_most_a_second() -> None:
    """The contract's watchdog is a second; a test may shorten it, not lengthen it."""
    assert 0 < routes_terminal_ws.GRANT_WATCHDOG_INTERVAL_S <= 1.0


def test_a_revoked_grant_is_noticed_on_the_next_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every message re-checks the grant, so a revocation is not waited for."""
    # The watchdog is out of the way: only the per-message check can see
    # this revocation, and the close has to arrive long before the
    # watchdog's own tick.
    monkeypatch.setattr(routes_terminal_ws, "GRANT_WATCHDOG_INTERVAL_S", 30.0)
    app = _app(tmp_path)
    with _client(app) as client:
        session_id = _create_session(client)
        with client.websocket_connect(
            f"{WS_BASE}{STREAM}/{session_id}", headers=_origin()
        ) as ws:
            _attach(ws)
            _replay(ws)
            token = client.cookies.get(TERMINAL_COOKIE) or ""
            assert token
            app.state.terminal_grants.revoke(token)
            started = time.monotonic()
            ws.send_json({"type": "ping"})
            assert _close_code(ws) == CLOSE_UNAUTHORIZED
            assert time.monotonic() - started < 5.0


def test_a_session_that_ends_closes_the_socket(tmp_path: Path) -> None:
    """The session is gone, so the stream is 4404 — it does not wait for it."""
    app = _app(tmp_path)
    with _client(app) as client:
        session_id = _create_session(client)
        with client.websocket_connect(
            f"{WS_BASE}{STREAM}/{session_id}", headers=_origin()
        ) as ws:
            _attach(ws)
            _replay(ws)
            response = client.delete(
                f"{SESSIONS}/{session_id}", headers=_origin()
            )
            assert response.status_code == 200
            assert _close_code(ws) == CLOSE_NOT_FOUND


def test_a_consumer_that_fell_behind_the_ring_is_closed(tmp_path: Path) -> None:
    """Bytes that were evicted cannot be handed over: the client is 4413."""
    factory = FakeFactory()
    app = _app(tmp_path, factory=factory)
    with _client(app) as client:
        session_id = _create_session(client)
        with client.websocket_connect(
            f"{WS_BASE}{STREAM}/{session_id}", headers=_origin()
        ) as ws:
            _attach(ws)
            _replay(ws)
            # More than the ring holds, all of it at once, so the bytes the
            # client has not read are gone by the time it reads.
            factory.process.say(b"x" * (_CHUNK * 6))
            assert _close_code(ws) == CLOSE_SLOW_CONSUMER


def test_a_disconnect_detaches_without_ending_the_session(tmp_path: Path) -> None:
    """A hidden pane detaches; it never ends the shell behind it."""
    factory = FakeFactory()
    app = _app(tmp_path, factory=factory)
    manager: TerminalSessionManager = app.state.terminal_sessions
    with _client(app) as client:
        session_id = _create_session(client)
        with client.websocket_connect(
            f"{WS_BASE}{STREAM}/{session_id}", headers=_origin()
        ) as ws:
            _attach(ws)
            _replay(ws)
        _wait_until(
            lambda: not manager.get(session_id).attached, what="the detach"
        )
        info = manager.get(session_id)
        assert info.attached is False
        assert info.exited is False
        assert factory.process.closed is False


# ── the wiring ─────────────────────────────────────────────────────────────


def test_the_factory_registers_the_stream_route(tmp_path: Path) -> None:
    """The route is in the app, next to the terminal block."""
    from ciao.config import CiaoConfig, WorkspaceConfig
    from ciao.web.app import create_app

    config = CiaoConfig(
        pwa_auth_token=SECRET,
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime",
        media_root=tmp_path / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={
            "personal": WorkspaceConfig(
                name="personal", vault_root="memory-vault/personal"
            )
        },
    )
    app = create_app(config)
    paths = {
        route.path
        for route in app.routes
        if getattr(route, "path", "").startswith("/ws/terminal")
    }
    assert paths == {"/ws/terminal/{session_id}"}
