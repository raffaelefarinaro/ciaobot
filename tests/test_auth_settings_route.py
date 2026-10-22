from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.terminal_auth import TERMINAL_COOKIE, TerminalGrantStore
from ciao.web.auth import SESSION_COOKIE, AuthMiddleware, make_serializer
from ciao.web import routes_auth
from ciao.web.routes_auth import auth_settings_get, auth_settings_update
from ciao.os_support.private import is_private
from tests.test_auth_security import (
    _TAILSCALE_SERVE_HEADERS,
    _assert_deletion_attributes,
    _cookie_morsel,
)


def _app(tmp_path: Path, *, token: str = "old-secret") -> Starlette:
    app = Starlette(
        routes=[
            Route("/api/auth/settings", auth_settings_get, methods=["GET"]),
            Route("/api/auth/settings", auth_settings_update, methods=["POST"]),
        ]
    )
    config = SimpleNamespace(
        pwa_auth_token=token,
        workspace_root=tmp_path,
    )
    app.state.config = config
    app.state.serializer = make_serializer(token)
    # The real grant store, wired the way the app wires it: the password
    # change reads it off `app.state`, so a fixture without one is a wiring
    # bug, not a request to answer. No production fallback is added for it.
    app.state.terminal_clock = time.monotonic
    app.state.terminal_grants = TerminalGrantStore(clock=app.state.terminal_clock)
    return app


def test_auth_settings_get(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path, token="old-secret"))
    res = client.get("/api/auth/settings")
    assert res.status_code == 200
    assert res.json() == {"password_configured": True, "on_host": False}


def test_auth_settings_change_writes_only_the_token(tmp_path: Path) -> None:
    app = _app(tmp_path, token="old-secret")
    client = TestClient(app, client=("127.0.0.1", 5555))

    res = client.post(
        "/api/auth/settings",
        json={"password": "hunter2", "current_password": "old-secret"},
    )
    assert res.status_code == 200
    assert res.json()["password_configured"] is True
    assert app.state.config.pwa_auth_token == "hunter2"
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "PWA_AUTH_TOKEN=hunter2" in env
    assert "PWA_AUTH_REQUIRED" not in env
    # .env holds the password in clear text by design, so it must at least be
    # owner-only
    assert is_private(tmp_path / ".env")
    assert "ciao_session=" in res.headers.get("set-cookie", "")


def test_auth_settings_change_requires_current_password(tmp_path: Path) -> None:
    app = _app(tmp_path, token="old-secret")
    client = TestClient(app)

    bad = client.post(
        "/api/auth/settings",
        json={"password": "new-secret", "current_password": "wrong"},
    )
    assert bad.status_code == 403
    assert bad.json()["code"] == "wrong_current_password"
    assert app.state.config.pwa_auth_token == "old-secret"
    assert "set-cookie" not in bad.headers

    ok = client.post(
        "/api/auth/settings",
        json={"password": "new-secret", "current_password": "old-secret"},
    )
    assert ok.status_code == 200
    assert ok.json()["password_configured"] is True


def test_loopback_peer_without_a_loopback_host_needs_the_current_password(tmp_path: Path) -> None:
    """A loopback peer is not the host unless Host also names loopback."""
    app = _app(tmp_path, token="old-secret")
    client = TestClient(app, client=("127.0.0.1", 5555))

    res = client.post("/api/auth/settings", json={"password": "hunter2"})

    assert res.status_code == 400
    assert res.json()["code"] == "current_password_required"
    assert app.state.config.pwa_auth_token == "old-secret"
    assert not (tmp_path / ".env").exists()


def test_auth_settings_wrong_current_password_is_never_401(tmp_path: Path) -> None:
    """401 means the session is gone and sends the PWA to /login."""
    serializer = make_serializer("old-secret")
    app = Starlette(
        routes=[Route("/api/auth/settings", auth_settings_update, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = SimpleNamespace(pwa_auth_token="old-secret", workspace_root=tmp_path)
    app.state.serializer = serializer
    app.state.terminal_clock = time.monotonic
    app.state.terminal_grants = TerminalGrantStore(clock=app.state.terminal_clock)
    client = TestClient(app, base_url="https://ciao.example")
    client.cookies.set(SESSION_COOKIE, serializer.dumps({"user": "owner"}))

    res = client.post(
        "/api/auth/settings",
        json={"password": "new-secret", "current_password": "wrong"},
        headers={"Origin": "https://ciao.example"},
    )

    assert res.status_code == 403
    assert res.json()["code"] == "wrong_current_password"


def test_auth_settings_rejects_a_too_short_password(tmp_path: Path) -> None:
    app = _app(tmp_path, token="old-secret")
    client = TestClient(app, client=("127.0.0.1", 5555))

    res = client.post(
        "/api/auth/settings",
        json={"password": "ab", "current_password": "old-secret"},
    )

    assert res.status_code == 400
    assert app.state.config.pwa_auth_token == "old-secret"


def test_auth_settings_change_from_remote_peer_still_works(tmp_path: Path) -> None:
    """The current password is the proof — location is not."""
    app = _app(tmp_path, token="old-secret")
    client = TestClient(app, client=("10.0.0.9", 5555))

    res = client.post(
        "/api/auth/settings",
        json={"password": "new-secret", "current_password": "old-secret"},
    )
    assert res.status_code == 200
    assert app.state.config.pwa_auth_token == "new-secret"


HOST_URL = "http://localhost:8443"
HOST_PEER = ("127.0.0.1", 5555)


def _app_with_middleware(tmp_path: Path, token: str = "old-secret") -> Starlette:
    serializer = make_serializer(token)
    app = Starlette(
        routes=[
            Route("/api/auth/settings", auth_settings_get, methods=["GET"]),
            Route("/api/auth/settings", auth_settings_update, methods=["POST"]),
        ],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = SimpleNamespace(pwa_auth_token=token, workspace_root=tmp_path)
    app.state.serializer = serializer
    app.state.terminal_clock = time.monotonic
    app.state.terminal_grants = TerminalGrantStore(clock=app.state.terminal_clock)
    return app


def _cookie(app: Starlette) -> dict[str, str]:
    return {SESSION_COOKIE: app.state.serializer.dumps({"user": "owner"})}


def test_auth_settings_get_reports_on_host_for_a_host_browser(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path), base_url=HOST_URL, client=HOST_PEER)
    assert client.get("/api/auth/settings").json()["on_host"] is True


def test_auth_settings_get_is_not_on_host_through_tailscale_serve(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path), base_url=HOST_URL, client=HOST_PEER)
    res = client.get("/api/auth/settings", headers=_TAILSCALE_SERVE_HEADERS)
    assert res.json()["on_host"] is False


def test_auth_settings_get_is_not_on_host_for_a_cross_origin_referer(tmp_path: Path) -> None:
    """The flag uses the same predicate as the change, so the UI never offers a refused path."""
    client = TestClient(_app(tmp_path), base_url=HOST_URL, client=HOST_PEER)
    res = client.get("/api/auth/settings", headers={"Origin": "http://localhost:9999"})
    assert res.json()["on_host"] is False


def test_host_browser_changes_the_password_without_the_current_one(tmp_path: Path) -> None:
    app = _app_with_middleware(tmp_path)
    client = TestClient(app, base_url=HOST_URL, client=HOST_PEER)
    res = client.post(
        "/api/auth/settings",
        json={"password": "hunter22"},
        cookies=_cookie(app),
        headers={"Origin": HOST_URL},
    )
    assert res.status_code == 200
    assert "PWA_AUTH_TOKEN=hunter22" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert "ciao_session=" in res.headers.get("set-cookie", "")


def test_host_browser_ignores_a_wrong_current_password(tmp_path: Path) -> None:
    app = _app_with_middleware(tmp_path)
    client = TestClient(app, base_url=HOST_URL, client=HOST_PEER)
    res = client.post(
        "/api/auth/settings",
        json={"password": "hunter22", "current_password": "nope"},
        cookies=_cookie(app),
        headers={"Origin": HOST_URL},
    )
    assert res.status_code == 200
    assert app.state.config.pwa_auth_token == "hunter22"


def test_host_browser_with_a_different_port_origin_still_needs_the_current_password(tmp_path: Path) -> None:
    """The route is host-browser-gated, and a different port is not the host."""
    app = _app(tmp_path)
    client = TestClient(app, base_url=HOST_URL, client=HOST_PEER)
    res = client.post(
        "/api/auth/settings",
        json={"password": "hunter22"},
        headers={"Origin": "http://localhost:9999"},
    )
    assert res.status_code == 400
    assert res.json()["code"] == "current_password_required"
    assert app.state.config.pwa_auth_token == "old-secret"


def test_host_browser_still_needs_a_session(tmp_path: Path) -> None:
    app = _app_with_middleware(tmp_path)
    client = TestClient(app, base_url=HOST_URL, client=HOST_PEER)
    res = client.post(
        "/api/auth/settings", json={"password": "hunter22"}, headers={"Origin": HOST_URL}
    )
    assert res.status_code == 401
    assert app.state.config.pwa_auth_token == "old-secret"
    assert not (tmp_path / ".env").exists()


def test_tailscale_serve_caller_still_needs_the_current_password(tmp_path: Path) -> None:
    app = _app_with_middleware(tmp_path)
    url = "https://mini.tail1.ts.net"
    client = TestClient(app, base_url=url, client=HOST_PEER)
    res = client.post(
        "/api/auth/settings",
        json={"password": "hunter22"},
        cookies=_cookie(app),
        headers={**_TAILSCALE_SERVE_HEADERS, "Origin": url},
    )
    assert res.status_code == 400
    assert res.json()["code"] == "current_password_required"
    assert app.state.config.pwa_auth_token == "old-secret"

    client = TestClient(app, base_url=HOST_URL, client=HOST_PEER)
    res = client.post(
        "/api/auth/settings",
        json={"password": "hunter22"},
        cookies=_cookie(app),
        headers={"X-Forwarded-For": "100.1.2.3", "Origin": HOST_URL},
    )
    assert res.status_code == 400
    assert app.state.config.pwa_auth_token == "old-secret"


def test_remote_peer_still_needs_the_current_password(tmp_path: Path) -> None:
    app = _app_with_middleware(tmp_path)
    client = TestClient(app, base_url=HOST_URL, client=("10.0.0.9", 5555))
    headers = {"Origin": HOST_URL}
    res = client.post(
        "/api/auth/settings", json={"password": "hunter22"}, cookies=_cookie(app), headers=headers
    )
    assert res.status_code == 400
    res = client.post(
        "/api/auth/settings",
        json={"password": "hunter22", "current_password": "wrong"},
        cookies=_cookie(app),
        headers=headers,
    )
    assert res.status_code == 403
    assert app.state.config.pwa_auth_token == "old-secret"


def test_rebound_host_on_loopback_peer_is_not_on_host(tmp_path: Path) -> None:
    app = _app_with_middleware(tmp_path)
    url = "http://evil.example:8443"
    client = TestClient(app, base_url=url, client=HOST_PEER)
    res = client.post(
        "/api/auth/settings",
        json={"password": "hunter22"},
        cookies=_cookie(app),
        headers={"Origin": url},
    )
    assert res.status_code == 400
    assert app.state.config.pwa_auth_token == "old-secret"


def test_host_browser_needs_a_new_password(tmp_path: Path) -> None:
    app = _app_with_middleware(tmp_path)
    client = TestClient(app, base_url=HOST_URL, client=HOST_PEER)
    res = client.post(
        "/api/auth/settings", json={}, cookies=_cookie(app), headers={"Origin": HOST_URL}
    )
    assert res.status_code == 400
    assert res.json()["code"] == "new_password_required"
    assert not (tmp_path / ".env").exists()


@pytest.fixture(autouse=True)
def _reset_password_change_failures():
    routes_auth._password_change_failures.clear()
    yield
    routes_auth._password_change_failures.clear()


def _wrong(client: TestClient, current: str = "wrong"):
    return client.post(
        "/api/auth/settings",
        json={"password": "new-secret", "current_password": current},
    )


def test_sixth_attempt_after_five_wrong_ones_is_rate_limited(tmp_path: Path) -> None:
    app = _app(tmp_path, token="old-secret")
    client = TestClient(app, client=("10.0.0.9", 5555))
    for _ in range(5):
        assert _wrong(client).status_code == 403

    res = _wrong(client, "old-secret")

    assert res.status_code == 429
    assert res.json() == {
        "error": "Too many wrong attempts. Wait a few minutes and try again.",
        "code": "rate_limited",
    }
    assert app.state.config.pwa_auth_token == "old-secret"


def test_missing_current_password_does_not_count_as_a_failure(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path), client=("10.0.0.9", 5555))
    for _ in range(8):
        res = client.post("/api/auth/settings", json={"password": "new-secret"})
        assert res.status_code == 400
        assert res.json()["code"] == "current_password_required"
    assert _wrong(client, "old-secret").status_code == 200


def test_successful_change_clears_the_failure_counter(tmp_path: Path) -> None:
    app = _app(tmp_path, token="old-secret")
    client = TestClient(app, client=("10.0.0.9", 5555))
    for _ in range(4):
        assert _wrong(client).status_code == 403
    assert _wrong(client, "old-secret").status_code == 200
    assert "10.0.0.9" not in routes_auth._password_change_failures
    for _ in range(5):
        assert _wrong(client).status_code == 403


def test_failures_expire_after_the_window(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path), client=("10.0.0.9", 5555))
    routes_auth._password_change_failures["10.0.0.9"] = [
        0.0 for _ in range(routes_auth._MAX_PASSWORD_CHANGE_FAILURES)
    ]
    assert _wrong(client, "old-secret").status_code == 200


def test_other_ips_are_not_limited(tmp_path: Path) -> None:
    app = _app(tmp_path)
    for _ in range(5):
        assert _wrong(TestClient(app, client=("10.0.0.9", 1))).status_code == 403
    assert _wrong(TestClient(app, client=("10.0.0.10", 1)), "old-secret").status_code == 200


def test_host_session_is_never_rate_limited(tmp_path: Path) -> None:
    app = _app_with_middleware(tmp_path)
    client = TestClient(app, base_url=HOST_URL, client=HOST_PEER)
    routes_auth._password_change_failures["127.0.0.1"] = [
        routes_auth.datetime.now(routes_auth.UTC).timestamp() for _ in range(10)
    ]
    res = client.post(
        "/api/auth/settings",
        json={"password": "hunter22"},
        cookies=_cookie(app),
        headers={"Origin": HOST_URL},
    )
    assert res.status_code == 200


def test_non_ascii_wrong_current_password_is_403_not_500(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path), client=("10.0.0.9", 5555))
    res = _wrong(client, "pässwörd\u2603")
    assert res.status_code == 403
    assert res.json()["code"] == "wrong_current_password"


def test_non_ascii_current_password_that_matches_works(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path, token="pässwörd"), client=("10.0.0.9", 5555))
    assert _wrong(client, "pässwörd").status_code == 200


def test_password_change_clears_welcome_pending(tmp_path: Path) -> None:
    from ciao.setup_marker import welcome_pending, write_welcome_pending

    write_welcome_pending(tmp_path / ".runtime")
    client = TestClient(_app(tmp_path, token="old-secret"), client=("127.0.0.1", 5555))

    res = client.post(
        "/api/auth/settings",
        json={"password": "hunter2-new", "current_password": "old-secret"},
    )

    assert res.status_code == 200
    assert welcome_pending(tmp_path / ".runtime") is False


def _grant(app: Starlette, session_cookie: str) -> str:
    """A live grant bearer, minted on ``session_cookie``."""
    store: TerminalGrantStore = app.state.terminal_grants
    token, _grant = store.issue(session_cookie)
    return token


def _clears_terminal_cookie(response) -> bool:
    """Whether the response clears the terminal cookie."""
    return any(
        line.startswith(f"{TERMINAL_COOKIE}=")
        for line in response.headers.get_list("set-cookie")
    )


def _assert_grant_retained(
    store: TerminalGrantStore,
    bearer: str,
    session_cookie: str,
    *,
    count: int,
) -> None:
    """The grant is still live and the store still holds exactly ``count``."""
    assert store.verify(bearer, session_cookie) is not None
    assert store.retained_grant_count() == count


def _assert_no_terminal_deletion(response) -> None:
    """The response clears no terminal cookie."""
    assert not _clears_terminal_cookie(response)


class _FrozenDatetime:
    """A `datetime` whose `now()` is one fixed instant.

    The route counts password-change failures in a rolling window against
    the wall clock. Pinning the module's own `datetime` name — not the
    stdlib class — makes the window deterministic without touching
    anything outside the test that patches it.
    """

    def __init__(self, instant: float) -> None:
        self._instant = instant

    def now(self, tz: object = None) -> datetime:
        return datetime.fromtimestamp(self._instant, tz)


def test_password_change_revokes_every_terminal_grant(tmp_path: Path) -> None:
    """A password change ends every terminal grant, on every browser.

    A change re-keys the sessions behind the grants too, so it has to end
    every grant — including the ones this browser holds.
    """
    app = _app(tmp_path, token="old-secret")
    store: TerminalGrantStore = app.state.terminal_grants
    client = TestClient(app, client=("10.0.0.9", 5555))
    session_cookie = _cookie(app)
    mine = _grant(app, session_cookie[SESSION_COOKIE])
    theirs = _grant(app, session_cookie[SESSION_COOKIE])
    assert store.retained_grant_count() == 2

    res = client.post(
        "/api/auth/settings",
        json={"password": "new-secret", "current_password": "old-secret"},
    )

    assert res.status_code == 200
    assert store.retained_grant_count() == 0
    assert store.verify(mine, session_cookie[SESSION_COOKIE]) is None
    assert store.verify(theirs, session_cookie[SESSION_COOKIE]) is None


def test_password_change_clears_the_terminal_cookie_and_keeps_the_session(
    tmp_path: Path,
) -> None:
    """The browser that changed the password keeps its session, but not its grant."""
    app = _app(tmp_path, token="old-secret")
    client = TestClient(app, base_url="https://ciao.example", client=("10.0.0.9", 5555))
    client.cookies.set(SESSION_COOKIE, app.state.serializer.dumps({"user": "owner"}))
    client.cookies.set(TERMINAL_COOKIE, "a-grant-bearer")

    res = client.post(
        "/api/auth/settings",
        json={"password": "new-secret", "current_password": "old-secret"},
        headers={"Origin": "https://ciao.example"},
    )

    assert res.status_code == 200
    set_cookies = res.headers.get_list("set-cookie")
    terminal = _cookie_morsel(set_cookies, TERMINAL_COOKIE)
    _assert_deletion_attributes(terminal, samesite="strict", secure=True)
    # The browser stays signed in: the change re-keys the signer and hands
    # this browser a fresh session cookie.
    session = [line for line in set_cookies if line.startswith(f"{SESSION_COOKIE}=")]
    assert len(session) == 1
    assert "Max-Age=0" not in session[0]


def test_password_change_to_the_same_secret_still_revokes_the_grants(
    tmp_path: Path,
) -> None:
    """Rewriting the same password is still a password change."""
    app = _app(tmp_path, token="old-secret")
    store: TerminalGrantStore = app.state.terminal_grants
    client = TestClient(app, client=("10.0.0.9", 5555))
    session_cookie = _cookie(app)[SESSION_COOKIE]
    live = _grant(app, session_cookie)

    res = client.post(
        "/api/auth/settings",
        json={"password": "old-secret", "current_password": "old-secret"},
    )

    assert res.status_code == 200
    assert store.verify(live, session_cookie) is None
    assert store.retained_grant_count() == 0


def test_failed_password_change_leaves_the_terminal_grants_usable(
    tmp_path: Path,
) -> None:
    """A refused password change ends no grant."""
    app = _app(tmp_path, token="old-secret")
    store: TerminalGrantStore = app.state.terminal_grants
    client = TestClient(app, client=("10.0.0.9", 5555))
    session_cookie = _cookie(app)[SESSION_COOKIE]
    live = _grant(app, session_cookie)

    wrong = client.post(
        "/api/auth/settings",
        json={"password": "new-secret", "current_password": "wrong"},
    )

    assert wrong.status_code == 403
    assert wrong.json()["code"] == "wrong_current_password"
    _assert_grant_retained(store, live, session_cookie, count=1)
    # A refused change does not clear the terminal cookie either.
    _assert_no_terminal_deletion(wrong)

    missing = client.post(
        "/api/auth/settings",
        json={"password": "new-secret"},
    )

    assert missing.status_code == 400
    assert missing.json()["code"] == "current_password_required"
    _assert_grant_retained(store, live, session_cookie, count=1)
    _assert_no_terminal_deletion(missing)


def test_a_too_short_new_password_keeps_every_terminal_grant(
    tmp_path: Path,
) -> None:
    """A too-short new password is a refusal: it ends no grant.

    The password is unchanged, so the unlocks the old password made are
    still the right ones to hold.
    """
    app = _app(tmp_path, token="old-secret")
    store: TerminalGrantStore = app.state.terminal_grants
    client = TestClient(app, client=("10.0.0.9", 5555))
    session_cookie = _cookie(app)[SESSION_COOKIE]
    live = _grant(app, session_cookie)

    res = client.post(
        "/api/auth/settings",
        json={"password": "ab", "current_password": "old-secret"},
    )

    assert res.status_code == 400
    assert app.state.config.pwa_auth_token == "old-secret"
    _assert_grant_retained(store, live, session_cookie, count=1)
    _assert_no_terminal_deletion(res)


def test_the_host_branch_new_password_required_keeps_every_terminal_grant(
    tmp_path: Path,
) -> None:
    """The host branch's own refusal ends no grant.

    The host browser sends no current password, so the host branch's
    refusals are its own — a missing new password is 400
    `new_password_required` — and it must leave the grants alone.
    """
    app = _app_with_middleware(tmp_path)
    store: TerminalGrantStore = app.state.terminal_grants
    client = TestClient(app, base_url=HOST_URL, client=HOST_PEER)
    session_cookie = _cookie(app)[SESSION_COOKIE]
    live = _grant(app, session_cookie)

    res = client.post(
        "/api/auth/settings",
        json={},
        cookies=_cookie(app),
        headers={"Origin": HOST_URL},
    )

    assert res.status_code == 400
    assert res.json()["code"] == "new_password_required"
    _assert_grant_retained(store, live, session_cookie, count=1)
    _assert_no_terminal_deletion(res)


def test_a_rate_limited_password_change_keeps_every_terminal_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rate-limited change ends no grant.

    The failure table is populated deterministically: the route's clock is
    frozen at a fixed instant, so the seeded failures are inside the
    window without reading the wall clock, and no test sleeps. The key is
    removed afterwards, so no test leaks state into the next one.
    """
    app = _app(tmp_path, token="old-secret")
    store: TerminalGrantStore = app.state.terminal_grants
    client = TestClient(app, client=("10.0.0.9", 5555))
    session_cookie = _cookie(app)[SESSION_COOKIE]
    live = _grant(app, session_cookie)

    # The route's own clock, frozen — the module's `datetime` name, not the
    # stdlib class, so nothing outside this test sees a changed `now`.
    frozen = 1_700_000_000.0
    monkeypatch.setattr(routes_auth, "datetime", _FrozenDatetime(frozen))
    monkeypatch.setitem(
        routes_auth._password_change_failures,
        "10.0.0.9",
        [frozen for _ in range(routes_auth._MAX_PASSWORD_CHANGE_FAILURES)],
    )
    try:
        res = client.post(
            "/api/auth/settings",
            json={"password": "new-secret", "current_password": "old-secret"},
        )
    finally:
        routes_auth._password_change_failures.pop("10.0.0.9", None)

    assert res.status_code == 429
    assert res.json()["code"] == "rate_limited"
    assert app.state.config.pwa_auth_token == "old-secret"
    _assert_grant_retained(store, live, session_cookie, count=1)
    _assert_no_terminal_deletion(res)
    # The failure table is clean again, so the next test starts fresh.
    assert "10.0.0.9" not in routes_auth._password_change_failures


def test_the_host_password_change_revokes_every_terminal_grant(
    tmp_path: Path,
) -> None:
    """The host branch's success revokes every grant too.

    The host branch changes the password without the current one, but it
    still *is* a password change: the password is the terminal's own
    credential, so every unlock it made ends — including this browser's.
    """
    app = _app_with_middleware(tmp_path)
    store: TerminalGrantStore = app.state.terminal_grants
    client = TestClient(app, base_url=HOST_URL, client=HOST_PEER)
    session_cookie = _cookie(app)[SESSION_COOKIE]
    mine = _grant(app, session_cookie)
    theirs = _grant(app, session_cookie)
    assert store.retained_grant_count() == 2

    res = client.post(
        "/api/auth/settings",
        json={"password": "hunter22"},
        cookies=_cookie(app),
        headers={"Origin": HOST_URL},
    )

    assert res.status_code == 200
    assert store.retained_grant_count() == 0
    assert store.verify(mine, session_cookie) is None
    assert store.verify(theirs, session_cookie) is None
    # The terminal cookie goes with the grants it named.
    terminal = _cookie_morsel(res.headers.get_list("set-cookie"), TERMINAL_COOKIE)
    _assert_deletion_attributes(terminal, samesite="strict", secure=False)


def test_the_host_password_change_to_the_same_secret_revokes_every_grant(
    tmp_path: Path,
) -> None:
    """Rewriting the same password on the host branch is still a change."""
    app = _app_with_middleware(tmp_path)
    store: TerminalGrantStore = app.state.terminal_grants
    client = TestClient(app, base_url=HOST_URL, client=HOST_PEER)
    session_cookie = _cookie(app)[SESSION_COOKIE]
    live = _grant(app, session_cookie)

    res = client.post(
        "/api/auth/settings",
        json={"password": "old-secret"},
        cookies=_cookie(app),
        headers={"Origin": HOST_URL},
    )

    assert res.status_code == 200
    assert store.verify(live, session_cookie) is None
    assert store.retained_grant_count() == 0
    terminal = _cookie_morsel(res.headers.get_list("set-cookie"), TERMINAL_COOKIE)
    _assert_deletion_attributes(terminal, samesite="strict", secure=False)


def test_the_settings_route_has_no_grant_store_fallback(tmp_path: Path) -> None:
    """A missing grant store raises; it never silently skips the revocation.

    A fallback that revoked nothing would make an un-wired app look like it
    had revoked the grants, and a fallback that invented one would hide the
    wiring bug. The store is always wired by the factory, so a route that
    cannot find it is a bug and says so.
    """
    app = _app_with_middleware(tmp_path)
    del app.state.terminal_grants
    client = TestClient(app, base_url="https://ciao.example", client=("10.0.0.9", 5555))
    client.cookies.set(SESSION_COOKIE, app.state.serializer.dumps({"user": "owner"}))

    with pytest.raises(AttributeError):
        client.post(
            "/api/auth/settings",
            json={"password": "new-secret", "current_password": "old-secret"},
            headers={"Origin": "https://ciao.example"},
        )
