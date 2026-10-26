"""The browser terminal's session routes: list, create and end (#1799).

The real routes, the real grant store and the real session manager, with a
fake process behind it: no pty is opened here, and what is under test is
the wire contract and the access policy, not a shell.

What is pinned, beyond the shapes:

* the gate order is the unlock's — the terminal policy, then the signed
  session, then a fresh grant — so a request that is not this engine's own
  page never has its body read, and a browser that has not unlocked never
  reaches a session;
* no grant is ``403 locked`` on every one of the three routes;
* the caller names a *target*, never a folder: exactly one of ``chat_id``
  or ``project_id``, and the folder comes from ``host_terminal.resolve_folder``
  alone, so an unknown target is a 404 and a missing folder a 409;
* the manager's refusals keep their own reasons and their own statuses;
* the answers carry no output and no connection id.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Iterator

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.terminal_auth import TERMINAL_COOKIE, TerminalGrantStore
from ciao.terminal_sessions import (
    TERMINAL_MAX_SESSIONS,
    TerminalSessionError,
    TerminalSessionManager,
)
from ciao.web.app import create_app
from ciao.web.auth import SESSION_COOKIE, AuthMiddleware
from ciao.web.routes_terminal import terminal_unlock
from ciao.web.routes_terminal_sessions import (
    terminal_sessions_create,
    terminal_sessions_end,
    terminal_sessions_list,
)

SECRET = "test-secret"
PASSWORD = "correct horse battery staple"
BASE = "http://localhost:8443"
UNLOCK = "/api/terminal/unlock"
SESSIONS = "/api/terminal/sessions"

#: The keys a session record carries on the wire: the facts a client needs
#: to draw a terminal, and nothing else.
_INFO_KEYS = {
    "session_id",
    "folder",
    "cols",
    "rows",
    "seq",
    "attached",
    "exited",
    "exit_code",
}


# ── the fakes ──────────────────────────────────────────────────────────────


class FakeProcess:
    """A terminal that answers nothing and never blocks.

    It is not a pty: ``read`` reports the terminal as quiet, so the
    manager's reader polls it and waits, and ``close`` only marks it closed.
    """

    def __init__(self, pid: int = 1) -> None:
        self.pid = pid
        self.closed = False
        self.exit_code: int | None = None
        self.written = bytearray()

    def read(self, size: int = 4096) -> bytes | None:
        return None

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        return len(data)

    def resize(self, cols: int, rows: int) -> None:
        return None

    def poll(self) -> int | None:
        return self.exit_code

    def close(self) -> None:
        self.closed = True


class FakeFactory:
    """A process factory the test controls: how many, and whether one fails."""

    def __init__(self) -> None:
        self.calls = 0
        self.processes: list[FakeProcess] = []
        self.error: BaseException | None = None

    def __call__(self, *, cwd: Path, cols: int = 80, rows: int = 24) -> FakeProcess:
        self.calls += 1
        if self.error is not None:
            raise self.error
        process = FakeProcess(pid=4_000 + self.calls)
        self.processes.append(process)
        return process


class FakePcm:
    """The chat/project lookup ``resolve_folder`` asks the engine for."""

    def __init__(self, chats: dict[str, Any], projects: dict[str, Any]) -> None:
        self._chats, self._projects = chats, projects

    def get_chat(self, chat_id: str) -> Any:
        return self._chats.get(chat_id)

    def get_project(self, project_id: str) -> Any:
        return self._projects.get(project_id)


class RefusingManager:
    """A manager that refuses every create with one reason."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def create(self, folder: Path, kind: str, target_id: str) -> Any:
        raise TerminalSessionError(self.reason)


def _config(root: Path) -> Any:
    return SimpleNamespace(
        pwa_auth_token=PASSWORD,
        workspace_root=root,
        state_path=root / ".runtime" / "state.json",
        workspace=lambda name: None,
        agent_root=lambda name: root,
    )


def _pcm(root: Path) -> FakePcm:
    """Two chats in one project, and that project's folder is ``root``."""
    project = SimpleNamespace(id="p1", repo_path=str(root), workspace="")
    chat = SimpleNamespace(
        id="c1", project_id="p1", code_mode=False, repo_location={}
    )
    other = SimpleNamespace(
        id="c2", project_id="p1", code_mode=False, repo_location={}
    )
    return FakePcm({"c1": chat, "c2": other}, {"p1": project})


def _app(
    tmp_path: Path,
    *,
    root: Path | None = None,
    pcm: FakePcm | None = None,
    factory: FakeFactory | None = None,
    with_middleware: bool = True,
) -> Starlette:
    """A real app with the real middleware, the real routes and a fake pty."""
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
            Route(SESSIONS, terminal_sessions_list, methods=["GET"]),
            Route(SESSIONS, terminal_sessions_create, methods=["POST"]),
            Route(
                f"{SESSIONS}/{{session_id}}",
                terminal_sessions_end,
                methods=["DELETE"],
            ),
        ],
        middleware=(
            [Middleware(AuthMiddleware, serializer=serializer)]
            if with_middleware
            else []
        ),
        lifespan=_lifespan,
    )
    app.state.serializer = serializer
    app.state.config = _config(root if root is not None else tmp_path)
    app.state.terminal_clock = time.monotonic
    app.state.terminal_grants = TerminalGrantStore(clock=app.state.terminal_clock)
    app.state.terminal_sessions = manager
    app.state.project_chat_manager = pcm if pcm is not None else _pcm(tmp_path)
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


# ── the door: policy, session, grant ──────────────────────────────────────


def test_a_request_without_a_session_is_refused(tmp_path: Path) -> None:
    """The session is required on every route, on loopback as much as anywhere."""
    app = _app(tmp_path)
    with _client(app, signed_in=False, unlocked=False) as client:
        assert client.get(f"{SESSIONS}?chat_id=c1", headers=_origin()).status_code == 401
        assert (
            client.post(SESSIONS, json={"chat_id": "c1"}, headers=_origin()).status_code
            == 401
        )
        assert client.delete(f"{SESSIONS}/abcdef", headers=_origin()).status_code == 401


def test_the_route_checks_the_session_itself(tmp_path: Path) -> None:
    """Defence in depth: the route's own check is not the middleware's.

    The middleware is the outer gate; this app removes it, so the route's
    own session check is the only thing between a request and a session.
    """
    app = _app(tmp_path, with_middleware=False)
    with _client(app, signed_in=False, unlocked=False) as client:
        response = client.get(f"{SESSIONS}?chat_id=c1", headers=_origin())
        assert response.status_code == 401
        assert response.json()["reason"] == "unauthorized"


def test_the_session_routes_are_in_neither_middleware_allowlist() -> None:
    """No peer skips the session on a terminal route, not even a local one."""
    from ciao.web import auth

    assert SESSIONS not in auth._PUBLIC_API
    assert SESSIONS not in auth._LOOPBACK_ONLY_API
    assert f"{SESSIONS}/abc" not in auth._LOOPBACK_ONLY_API


def test_a_foreign_origin_is_refused_on_every_route(tmp_path: Path) -> None:
    """A page that is not this engine's own page reaches nothing."""
    app = _app(tmp_path)
    with _client(app) as client:
        foreign = _origin("http://evil.example:8443")

        # A read: the route's own policy answers, with its own reason.
        response = client.get(f"{SESSIONS}?chat_id=c1", headers=foreign)
        assert response.status_code == 403
        assert response.json()["reason"] == "forbidden_origin"

        # A write: the ordinary middleware origin check answers first.
        response = client.post(SESSIONS, json={"chat_id": "c1"}, headers=foreign)
        assert response.status_code == 403
        response = client.delete(f"{SESSIONS}/abcdef", headers=foreign)
        assert response.status_code == 403


def test_the_route_refuses_an_origin_that_is_not_a_bare_origin(tmp_path: Path) -> None:
    """The policy is stricter than the middleware's, and it says so.

    The middleware accepts a same-origin page address; the terminal's own
    policy does not accept an `Origin` that carries a path.
    """
    app = _app(tmp_path)
    with _client(app) as client:
        response = client.post(
            SESSIONS, json={"chat_id": "c1"}, headers=_origin(f"{BASE}/page")
        )
        assert response.status_code == 403
        assert response.json()["reason"] == "forbidden_origin"


def test_a_remote_plain_http_caller_is_refused(tmp_path: Path) -> None:
    """A remote browser must arrive over HTTPS, even with a live grant."""
    app = _app(tmp_path)
    store: TerminalGrantStore = app.state.terminal_grants
    session = app.state.serializer.dumps({"user": "owner"})
    token, _grant = store.issue(session)

    with TestClient(
        app, base_url="http://app.example", client=("100.64.0.7", 50000)
    ) as client:
        client.cookies.set(SESSION_COOKIE, session)
        client.cookies.set(TERMINAL_COOKIE, token)
        response = client.get(
            f"{SESSIONS}?chat_id=c1", headers=_origin("http://app.example")
        )

    assert response.status_code == 403
    assert response.json()["reason"] == "https_required"


def test_a_browser_that_has_not_unlocked_is_locked_out(tmp_path: Path) -> None:
    """No grant is `locked`, on every route: nothing is reachable before it."""
    app = _app(tmp_path)
    with _client(app, unlocked=False) as client:
        response = client.get(f"{SESSIONS}?chat_id=c1", headers=_origin())
        assert response.status_code == 403
        assert response.json()["reason"] == "locked"

        response = client.post(SESSIONS, json={"chat_id": "c1"}, headers=_origin())
        assert response.status_code == 403
        assert response.json()["reason"] == "locked"

        response = client.delete(f"{SESSIONS}/abcdef", headers=_origin())
        assert response.status_code == 403
        assert response.json()["reason"] == "locked"


def test_a_refusal_is_never_cacheable(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with _client(app, unlocked=False) as client:
        response = client.get(f"{SESSIONS}?chat_id=c1", headers=_origin())
        assert response.headers["cache-control"] == "no-store"


# ── the target: named, never a path ───────────────────────────────────────


def test_creating_a_session_answers_the_record(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with _client(app) as client:
        response = client.post(SESSIONS, json={"chat_id": "c1"}, headers=_origin())

        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"session"}
        info = body["session"]
        assert set(info) == _INFO_KEYS
        assert info["session_id"]
        assert info["folder"] == str(tmp_path.resolve())
        assert info["cols"] == 80
        assert info["rows"] == 24
        assert info["seq"] == 0
        assert info["attached"] is False
        assert info["exited"] is False
        assert info["exit_code"] is None
        # The answer is about a terminal, and it is never cacheable.
        assert response.headers["cache-control"] == "no-store"


def test_the_record_carries_no_output_and_no_connection_id(tmp_path: Path) -> None:
    """The bytes live in the ring; the controller lives in the manager."""
    app = _app(tmp_path)
    with _client(app) as client:
        response = client.post(SESSIONS, json={"chat_id": "c1"}, headers=_origin())

    info = response.json()["session"]
    assert "output" not in info
    assert "connection_id" not in info
    assert "replay_start" not in info
    assert "pending_write_bytes" not in info


@pytest.mark.parametrize(
    "query",
    ["", "?chat_id=c1&project_id=p1"],
    ids=["neither", "both"],
)
def test_the_query_must_name_exactly_one_target(tmp_path: Path, query: str) -> None:
    app = _app(tmp_path)
    with _client(app) as client:
        response = client.get(f"{SESSIONS}{query}", headers=_origin())
        assert response.status_code == 400
        assert response.json()["reason"] == "bad_request"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"chat_id": "c1", "project_id": "p1"},
        {"chat_id": "c1", "extra": 1},
        {"chat_id": 5},
        {"chat_id": None},
        {"chat_id": ""},
    ],
    ids=[
        "neither",
        "both",
        "an extra key",
        "a number",
        "null",
        "an empty id",
    ],
)
def test_the_body_must_name_exactly_one_target(tmp_path: Path, body: Any) -> None:
    app = _app(tmp_path)
    with _client(app) as client:
        response = client.post(SESSIONS, json=body, headers=_origin())
        assert response.status_code == 400
        assert response.json()["reason"] == "bad_request"


def test_an_unknown_target_is_not_found(tmp_path: Path) -> None:
    """`resolve_folder` decides whether the target exists."""
    app = _app(tmp_path)
    with _client(app) as client:
        response = client.get(f"{SESSIONS}?chat_id=nope", headers=_origin())
        assert response.status_code == 404
        assert response.json()["reason"] == "not_found"

        response = client.post(SESSIONS, json={"project_id": "nope"}, headers=_origin())
        assert response.status_code == 404
        assert response.json()["reason"] == "not_found"


def test_a_missing_folder_is_a_conflict(tmp_path: Path) -> None:
    """The project exists but its folder does not: the shell cannot start there."""
    gone = tmp_path / "gone"
    project = SimpleNamespace(id="p2", repo_path="", workspace="")
    pcm = FakePcm({}, {"p2": project})
    app = _app(tmp_path, root=gone, pcm=pcm)

    with _client(app) as client:
        response = client.post(SESSIONS, json={"project_id": "p2"}, headers=_origin())
        assert response.status_code == 409
        assert response.json()["reason"] == "folder_missing"


def test_a_body_over_the_cap_is_refused(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with _client(app) as client:
        body = b'{"chat_id": "' + b"a" * 5000 + b'"}'
        response = client.post(
            SESSIONS,
            content=body,
            headers={**_origin(), "Content-Type": "application/json"},
        )
        assert response.status_code == 413
        assert response.json()["reason"] == "too_large"


def test_malformed_json_is_refused(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with _client(app) as client:
        response = client.post(
            SESSIONS,
            content=b"{nope",
            headers={**_origin(), "Content-Type": "application/json"},
        )
        assert response.status_code == 400
        assert response.json()["reason"] == "bad_request"


# ── the manager's own refusals ────────────────────────────────────────────


def test_a_full_manager_refuses_another_session(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with _client(app) as client:
        for _ in range(TERMINAL_MAX_SESSIONS):
            response = client.post(SESSIONS, json={"chat_id": "c1"}, headers=_origin())
            assert response.status_code == 200

        response = client.post(SESSIONS, json={"chat_id": "c1"}, headers=_origin())
        assert response.status_code == 409
        assert response.json()["reason"] == "capacity"


def test_a_shell_that_cannot_start_is_a_server_error(tmp_path: Path) -> None:
    factory = FakeFactory()
    factory.error = RuntimeError("no shell here")
    app = _app(tmp_path, factory=factory)
    with _client(app) as client:
        response = client.post(SESSIONS, json={"chat_id": "c1"}, headers=_origin())
        assert response.status_code == 500
        assert response.json()["reason"] == "spawn_failed"


def test_a_manager_reason_the_route_does_not_know_is_a_bad_request(
    tmp_path: Path,
) -> None:
    """Every reason the manager carries maps to a status; the rest is a 400.

    The manager's vocabulary is closed and wider than these three routes
    need; a reason none of them raises is still a refusal, and a refusal
    the caller did wrong is a 400.
    """
    app = _app(tmp_path)
    app.state.terminal_sessions = RefusingManager("io_failed")
    with _client(app) as client:
        response = client.post(SESSIONS, json={"chat_id": "c1"}, headers=_origin())
        assert response.status_code == 400
        assert response.json()["reason"] == "io_failed"


# ── the list and the end ──────────────────────────────────────────────────


def test_the_list_is_scoped_to_its_target(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with _client(app) as client:
        chat = client.post(SESSIONS, json={"chat_id": "c1"}, headers=_origin())
        project = client.post(SESSIONS, json={"project_id": "p1"}, headers=_origin())
        assert chat.status_code == 200
        assert project.status_code == 200

        listed = client.get(f"{SESSIONS}?chat_id=c1", headers=_origin())
        assert listed.status_code == 200
        sessions = listed.json()["sessions"]
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == chat.json()["session"]["session_id"]
        assert listed.headers["cache-control"] == "no-store"

        # The project's own session is not the chat's, and the other chat
        # of the same project has none.
        listed = client.get(f"{SESSIONS}?project_id=p1", headers=_origin())
        assert len(listed.json()["sessions"]) == 1
        assert listed.json()["sessions"][0]["session_id"] == project.json()["session"][
            "session_id"
        ]

        listed = client.get(f"{SESSIONS}?chat_id=c2", headers=_origin())
        assert listed.status_code == 200
        assert listed.json()["sessions"] == []


def test_ending_a_session_removes_it_and_a_second_end_is_not_found(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with _client(app) as client:
        created = client.post(SESSIONS, json={"chat_id": "c1"}, headers=_origin())
        session_id = created.json()["session"]["session_id"]

        response = client.delete(f"{SESSIONS}/{session_id}", headers=_origin())
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert response.headers["cache-control"] == "no-store"

        response = client.delete(f"{SESSIONS}/{session_id}", headers=_origin())
        assert response.status_code == 404
        assert response.json()["reason"] == "not_found"


def test_a_malformed_session_id_is_refused(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with _client(app) as client:
        response = client.delete(f"{SESSIONS}/not%20a%20session%21", headers=_origin())
        assert response.status_code == 400
        assert response.json()["reason"] == "bad_request"


# ── the factory wires it up ───────────────────────────────────────────────


def _real_config(tmp_path: Path) -> Any:
    from ciao.config import CiaoConfig, WorkspaceConfig

    return CiaoConfig(
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


def test_the_factory_builds_one_manager_and_registers_the_three_routes(
    tmp_path: Path,
) -> None:
    app = create_app(_real_config(tmp_path))

    manager = app.state.terminal_sessions
    assert isinstance(manager, TerminalSessionManager)

    paths = {
        (route.path, tuple(sorted(route.methods or ())))
        for route in app.routes
        if getattr(route, "path", "").startswith("/api/terminal")
    }
    assert paths == {
        (UNLOCK, ("POST",)),
        ("/api/terminal/status", ("GET", "HEAD")),
        (SESSIONS, ("GET", "HEAD")),
        (SESSIONS, ("POST",)),
        (f"{SESSIONS}/{{session_id}}", ("DELETE",)),
    }


async def test_the_lifespan_starts_the_manager_and_shuts_it_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manager belongs to the app: it starts with it and closes with it."""
    monkeypatch.setattr(
        "ciao.setup_status.warm_claude_discovery_cache", lambda *a, **k: None
    )
    monkeypatch.setattr("ciao.tool_path.login_shell_path", lambda: "")

    app = create_app(_real_config(tmp_path))
    manager = app.state.terminal_sessions
    events: list[str] = []
    start, shutdown = manager.start, manager.shutdown

    async def _start() -> None:
        events.append("start")
        await start()

    async def _shutdown() -> None:
        events.append("shutdown")
        await shutdown()

    manager.start = _start  # type: ignore[method-assign]
    manager.shutdown = _shutdown  # type: ignore[method-assign]

    async with app.router.lifespan_context(app):
        assert events == ["start"]

    assert events == ["start", "shutdown"]
