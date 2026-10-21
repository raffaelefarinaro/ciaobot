from __future__ import annotations

import time
from http.cookies import Morsel, SimpleCookie
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import macos_service
from ciao.terminal_auth import TERMINAL_COOKIE, TerminalGrantStore
from ciao.web import auth
from ciao.web.auth import AuthMiddleware, SESSION_COOKIE
from ciao.web.routes_auth import auth_login, auth_logout


async def _ok(_request):
    return JSONResponse({"ok": True})


def _auth_cookie(serializer: URLSafeTimedSerializer) -> dict[str, str]:
    return {SESSION_COOKIE: serializer.dumps({"user": "owner"})}


def _protected_client(
    base_url: str = "https://ciao.example",
) -> tuple[TestClient, URLSafeTimedSerializer]:
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/demo", _ok, methods=["GET", "POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    return TestClient(app, base_url=base_url), serializer


def test_state_changing_request_rejects_cross_origin() -> None:
    client, serializer = _protected_client()
    resp = client.post(
        "/api/demo",
        cookies=_auth_cookie(serializer),
        headers={"Origin": "https://evil.example"},
    )

    assert resp.status_code == 403


def test_state_changing_request_allows_matching_origin() -> None:
    client, serializer = _protected_client()
    resp = client.post(
        "/api/demo",
        cookies=_auth_cookie(serializer),
        headers={"Origin": "https://ciao.example"},
    )

    assert resp.status_code == 200


def test_state_changing_request_rejects_a_scheme_mismatch() -> None:
    """The origin's scheme has to be the one the engine is serving.

    A page on `https://ciao.example` is a different origin from the engine
    on `http://ciao.example`, so its state-changing request is refused. The
    same host and the same port with only the scheme changed is refused too:
    there the port cannot carry the refusal on its own.
    """
    client, serializer = _protected_client(base_url="http://ciao.example")
    cookies = _auth_cookie(serializer)

    refused = client.post(
        "/api/demo",
        cookies=cookies,
        headers={"Origin": "https://ciao.example"},
    )
    allowed = client.post(
        "/api/demo",
        cookies=cookies,
        headers={"Origin": "http://ciao.example"},
    )

    assert refused.status_code == 403
    assert allowed.status_code == 200

    port_client, port_serializer = _protected_client(
        base_url="http://ciao.example:8443"
    )
    port_cookies = _auth_cookie(port_serializer)

    refused = port_client.post(
        "/api/demo",
        cookies=port_cookies,
        headers={"Origin": "https://ciao.example:8443"},
    )
    allowed = port_client.post(
        "/api/demo",
        cookies=port_cookies,
        headers={"Origin": "http://ciao.example:8443"},
    )

    assert refused.status_code == 403
    assert allowed.status_code == 200


def test_safe_request_does_not_require_origin() -> None:
    client, serializer = _protected_client()
    resp = client.get("/api/demo", cookies=_auth_cookie(serializer))

    assert resp.status_code == 200


def _origin_req(headers: dict[str, str], scheme: str = "https") -> object:
    return SimpleNamespace(
        headers={k.lower(): v for k, v in headers.items()},
        url=SimpleNamespace(hostname=None, port=None, scheme=scheme),
    )


def test_same_origin_accepts_matching_host() -> None:
    from ciao.web.auth import _same_origin

    req = _origin_req({"host": "ciao.example"})
    assert _same_origin(req, "https://ciao.example") is True


def test_same_origin_rejects_cross_origin() -> None:
    from ciao.web.auth import _same_origin

    req = _origin_req({"host": "ciao.example"})
    assert _same_origin(req, "https://evil.example") is False


def test_same_origin_rejects_an_explicit_port_mismatch() -> None:
    from ciao.web.auth import _same_origin

    req = _origin_req({"host": "ciao.example"})
    assert _same_origin(req, "https://ciao.example:444") is False
    assert _same_origin(req, "https://ciao.example") is True


def test_same_origin_accepts_proxy_forwarded_host() -> None:
    """Behind a proxy the bound Host differs from the browser origin; the
    proxy-declared X-Forwarded-Host makes the WS/state-change handshake pass."""
    from ciao.web.auth import _same_origin

    # The engine is on http behind the proxy, the browser origin is https:
    # the proxy-declared host matches on the host alone.
    req = _origin_req(
        {"host": "localhost:8765", "x-forwarded-host": "app.example"}, scheme="http"
    )
    assert _same_origin(req, "https://app.example") is True
    # A genuine cross-origin still fails even with the forwarded host present.
    assert _same_origin(req, "https://evil.example") is False


def test_proxy_forwarded_host_accepts_a_tls_terminated_origin() -> None:
    """A TLS-terminating proxy presents http; the browser origin is https.

    The proxy declares the public host in `X-Forwarded-Host`, and it may keep
    it in `Host` as well (nginx's `proxy_set_header Host $host`). The
    proxy-declared host carries the match, so the scheme the engine sees is
    not the one that refuses the request.
    """
    from ciao.web.auth import _same_origin

    req = _origin_req(
        {"host": "localhost:8765", "x-forwarded-host": "app.example"}, scheme="http"
    )
    assert _same_origin(req, "https://app.example") is True

    # The proxy keeps the public host in `Host` too, so the origin host is
    # the request host as well: the proxy-declared host still comes first.
    req = _origin_req(
        {"host": "app.example", "x-forwarded-host": "app.example"}, scheme="http"
    )
    assert _same_origin(req, "https://app.example") is True
    # A genuine cross-origin still fails even with the forwarded host present.
    assert _same_origin(req, "https://evil.example") is False

    client, serializer = _protected_client(base_url="http://localhost:8765")
    resp = client.post(
        "/api/demo",
        cookies=_auth_cookie(serializer),
        headers={"Origin": "https://app.example", "X-Forwarded-Host": "app.example"},
    )
    assert resp.status_code == 200

    # The same request with the public host kept in `Host` as well.
    client, serializer = _protected_client(base_url="http://app.example")
    resp = client.post(
        "/api/demo",
        cookies=_auth_cookie(serializer),
        headers={"Origin": "https://app.example", "X-Forwarded-Host": "app.example"},
    )
    assert resp.status_code == 200


def test_same_origin_rejects_a_scheme_mismatch() -> None:
    """A same host and port is not enough: the scheme has to match too.

    `https://ciao.example:8443` is a different origin from
    `http://ciao.example:8443`, so an engine serving plain http refuses the
    https origin, and an engine serving https refuses the http one.
    """
    from ciao.web.auth import _same_origin

    http_req = _origin_req({"host": "ciao.example:8443"}, scheme="http")
    assert _same_origin(http_req, "http://ciao.example:8443") is True
    assert _same_origin(http_req, "https://ciao.example:8443") is False

    https_req = _origin_req({"host": "ciao.example:8443"}, scheme="https")
    assert _same_origin(https_req, "https://ciao.example:8443") is True
    assert _same_origin(https_req, "http://ciao.example:8443") is False


def _auth_client() -> TestClient:
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[
            Route("/api/auth", auth_login, methods=["POST"]),
            Route("/api/auth/logout", auth_logout, methods=["POST"]),
        ],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = SimpleNamespace(pwa_auth_token="test-token")
    # The real grant store, wired the way the app wires it: the logout
    # route reads it off `app.state`, so a fixture without one is a wiring
    # bug, not a request to answer. No production fallback is added for it.
    app.state.terminal_clock = time.monotonic
    app.state.terminal_grants = TerminalGrantStore(clock=app.state.terminal_clock)
    return TestClient(app, base_url="https://ciao.example")


def _setup_token_client(
    tmp_path,
    *,
    base_url: str = "http://localhost:8443",
    peer: tuple[str, int] = ("127.0.0.1", 5555),
) -> TestClient:
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/", _ok, methods=["GET"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = SimpleNamespace(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
    )
    return TestClient(app, base_url=base_url, client=peer)


def test_login_with_a_non_ascii_token_is_401_not_500() -> None:
    res = _auth_client().post("/api/auth", json={"token": "pässwörd\u2603"})
    assert res.status_code == 401


def test_login_cookie_is_secure_and_host_only() -> None:
    resp = _auth_client().post("/api/auth", json={"token": "test-token"})

    assert resp.status_code == 200
    set_cookie = resp.headers["set-cookie"]
    assert "ciao_session=" in set_cookie
    # Host-only cookie: no Domain attribute, scoped to the exact host.
    assert "Domain=" not in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Secure" in set_cookie


def test_logout_clears_host_only_cookie() -> None:
    client = _auth_client()
    login = client.post("/api/auth", json={"token": "test-token"})
    assert login.status_code == 200

    resp = client.post(
        "/api/auth/logout",
        headers={"Origin": "https://ciao.example"},
    )

    assert resp.status_code == 200
    set_cookie = resp.headers["set-cookie"]
    assert "Domain=" not in set_cookie
    assert "Max-Age=0" in set_cookie


def test_logout_clears_the_session_cookie_on_its_own_header() -> None:
    """The session deletion keeps its own shape now that two cookies go out.

    A logout clears the login cookie *and* the terminal cookie, so the
    response carries two `Set-Cookie` headers. Each deletion is checked on
    the header that names it: the login cookie is host-only, `Path=/`,
    `HttpOnly`, `SameSite=lax` and `Secure` over HTTPS.
    """
    client = _auth_client()
    login = client.post("/api/auth", json={"token": "test-token"})
    assert login.status_code == 200

    resp = client.post(
        "/api/auth/logout",
        headers={"Origin": "https://ciao.example"},
    )

    assert resp.status_code == 200
    set_cookies = resp.headers.get_list("set-cookie")
    # The joined header would hide which deletion carries which attribute.
    assert len(set_cookies) == 2
    session = _cookie_morsel(set_cookies, SESSION_COOKIE)
    _assert_deletion_attributes(session, samesite="lax", secure=True)


def _grant(app: Starlette, session_cookie: str) -> str:
    """A live grant bearer, minted on ``session_cookie``."""
    store: TerminalGrantStore = app.state.terminal_grants
    token, _grant = store.issue(session_cookie)
    return token


def _terminal_cookie_header(set_cookies: list[str], name: str) -> str:
    """The one Set-Cookie line that names ``name``."""
    matches = [line for line in set_cookies if line.startswith(f"{name}=")]
    assert len(matches) == 1, f"expected one {name} cookie, got {set_cookies}"
    return matches[0]


def _cookie_morsel(set_cookies: list[str], name: str) -> Morsel:
    """The parsed attributes of the one Set-Cookie line that names ``name``.

    A substring check is not enough: ``"Path=/" in header`` also accepts
    ``Path=/api``, and a browser would then keep the bearer for a path the
    terminal never asks for. So the line is parsed and every attribute is
    compared as a value.
    """
    header = _terminal_cookie_header(set_cookies, name)
    jar = SimpleCookie()
    jar.load(header)
    assert name in jar, f"{name} did not parse as a cookie: {header!r}"
    return jar[name]


def _assert_deletion_attributes(
    morsel: Morsel,
    *,
    samesite: str,
    secure: bool,
) -> None:
    """Every attribute a cookie deletion has to carry, checked as a value.

    Host-only (no ``Domain``), ``Path=/`` exactly, ``Max-Age=0``, ``HttpOnly``,
    the given ``SameSite`` (case-insensitively) and ``Secure`` exactly when
    the request arrived over HTTPS.
    """
    assert morsel["path"] == "/", f"Path must be exactly '/', got {morsel['path']!r}"
    assert morsel["max-age"] == "0", f"Max-Age must be 0, got {morsel['max-age']!r}"
    assert morsel["httponly"] is True
    assert str(morsel["samesite"]).lower() == samesite.lower()
    assert morsel["domain"] == "", f"must be host-only, got {morsel['domain']!r}"
    assert bool(morsel["secure"]) is secure


def test_logout_revokes_only_the_presented_terminal_grant() -> None:
    """A logout ends the grant it presents and nothing else.

    Two browsers can hold byte-identical login cookies, so the login
    cookie cannot say which browser is asking. Revoking "the grant for
    this session" would end the other browser's terminal too.
    """
    client = _auth_client()
    app = client.app
    store: TerminalGrantStore = app.state.terminal_grants

    login = client.post("/api/auth", json={"token": "test-token"})
    assert login.status_code == 200
    session_cookie = client.cookies.get(SESSION_COOKIE) or ""
    assert session_cookie

    # Two browsers, one identical login cookie, two independent grants.
    other_session_cookie = session_cookie
    mine = _grant(app, session_cookie)
    theirs = _grant(app, other_session_cookie)
    assert mine != theirs
    assert store.retained_grant_count() == 2

    client.cookies.set(TERMINAL_COOKIE, mine)
    resp = client.post(
        "/api/auth/logout",
        headers={"Origin": "https://ciao.example"},
    )

    assert resp.status_code == 200
    # The presented grant is gone.
    assert store.verify(mine, session_cookie) is None
    # The other browser's grant is untouched, even though the login cookie
    # it is bound to is byte-identical.
    assert store.verify(theirs, other_session_cookie) is not None
    assert store.retained_grant_count() == 1


def test_logout_without_a_terminal_cookie_keeps_every_grant() -> None:
    """A logout that presents no grant ends no grant."""
    client = _auth_client()
    app = client.app
    store: TerminalGrantStore = app.state.terminal_grants

    login = client.post("/api/auth", json={"token": "test-token"})
    assert login.status_code == 200
    session_cookie = client.cookies.get(SESSION_COOKIE) or ""
    assert session_cookie

    live = _grant(app, session_cookie)
    assert store.retained_grant_count() == 1

    resp = client.post(
        "/api/auth/logout",
        headers={"Origin": "https://ciao.example"},
    )

    assert resp.status_code == 200
    assert store.verify(live, session_cookie) is not None
    assert store.retained_grant_count() == 1


def _logout_client(base_url: str) -> TestClient:
    """A client for a logout-only app, wired with a grant store."""
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/auth/logout", auth_logout, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = SimpleNamespace(pwa_auth_token="test-token")
    app.state.terminal_clock = time.monotonic
    app.state.terminal_grants = TerminalGrantStore(clock=app.state.terminal_clock)
    client = TestClient(app, base_url=base_url)
    client.cookies.set(SESSION_COOKIE, serializer.dumps({"user": "owner"}))
    return client


def test_logout_clears_the_terminal_cookie_with_the_grant_shape() -> None:
    """The terminal cookie is cleared the way it was set.

    Host-only (no `Domain`), `Path=/` exactly, `HttpOnly`, `SameSite=Strict`,
    and `Secure` when the request arrived over HTTPS — the same shape the
    unlock set, so the browser drops the bearer it is holding.
    """
    client = _auth_client()
    login = client.post("/api/auth", json={"token": "test-token"})
    assert login.status_code == 200

    resp = client.post(
        "/api/auth/logout",
        headers={"Origin": "https://ciao.example"},
    )

    assert resp.status_code == 200
    set_cookies = resp.headers.get_list("set-cookie")
    terminal = _cookie_morsel(set_cookies, TERMINAL_COOKIE)
    _assert_deletion_attributes(terminal, samesite="strict", secure=True)
    # The login cookie is still cleared, exactly as before.
    session = _terminal_cookie_header(set_cookies, SESSION_COOKIE)
    assert "Max-Age=0" in session


def test_logout_over_plain_http_does_not_mark_the_terminal_cookie_secure() -> None:
    """A `Secure` cookie would be dropped by a browser on plain HTTP."""
    client = _logout_client("http://localhost:8443")

    resp = client.post("/api/auth/logout", headers={"Origin": "http://localhost:8443"})

    assert resp.status_code == 200
    terminal = _cookie_morsel(resp.headers.get_list("set-cookie"), TERMINAL_COOKIE)
    _assert_deletion_attributes(terminal, samesite="strict", secure=False)


def test_logout_over_plain_http_ignores_a_forwarded_https_scheme() -> None:
    """`Secure` follows the effective scheme, not a forwarded header.

    A raw `X-Forwarded-Proto` is a header anyone can send. If it could mark
    the deletion `Secure` on a plain-HTTP request, the browser would refuse
    to apply the deletion — so the bearer would survive the logout.
    """
    client = _logout_client("http://localhost:8443")

    resp = client.post(
        "/api/auth/logout",
        headers={
            "Origin": "http://localhost:8443",
            "X-Forwarded-Proto": "https",
        },
    )

    assert resp.status_code == 200
    terminal = _cookie_morsel(resp.headers.get_list("set-cookie"), TERMINAL_COOKIE)
    _assert_deletion_attributes(terminal, samesite="strict", secure=False)


def test_logout_over_plain_http_clears_the_session_cookie_without_secure() -> None:
    """The session deletion follows the effective scheme too.

    A `Secure` deletion over plain HTTP is one the browser refuses to
    apply, so the login cookie would survive the logout.
    """
    client = _logout_client("http://localhost:8443")

    resp = client.post("/api/auth/logout", headers={"Origin": "http://localhost:8443"})

    assert resp.status_code == 200
    session = _cookie_morsel(resp.headers.get_list("set-cookie"), SESSION_COOKIE)
    _assert_deletion_attributes(session, samesite="lax", secure=False)


def test_logout_over_https_marks_the_terminal_cookie_secure_despite_a_forwarded_http(
) -> None:
    """The other direction: a forwarded `http` cannot downgrade the deletion.

    The effective scheme is what the server saw the request arrive on, so
    the deletion is `Secure` even when a header claims plain HTTP.
    """
    client = _auth_client()
    login = client.post("/api/auth", json={"token": "test-token"})
    assert login.status_code == 200

    resp = client.post(
        "/api/auth/logout",
        headers={
            "Origin": "https://ciao.example",
            "X-Forwarded-Proto": "http",
        },
    )

    assert resp.status_code == 200
    terminal = _cookie_morsel(resp.headers.get_list("set-cookie"), TERMINAL_COOKIE)
    _assert_deletion_attributes(terminal, samesite="strict", secure=True)


def test_logout_without_a_wired_grant_store_is_a_wiring_bug() -> None:
    """There is no fallback store: a missing one raises rather than skipping.

    A fallback that quietly revoked nothing would make an un-wired app look
    like it had revoked the grant, and a fallback that invented one would
    hide the wiring bug. The store is always wired by the factory, so a
    route that cannot find it is a bug and says so.
    """
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/auth/logout", auth_logout, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = SimpleNamespace(pwa_auth_token="test-token")
    client = TestClient(app, base_url="https://ciao.example")
    client.cookies.set(SESSION_COOKIE, serializer.dumps({"user": "owner"}))
    client.cookies.set(TERMINAL_COOKIE, "a-grant-bearer")

    with pytest.raises(AttributeError):
        client.post("/api/auth/logout", headers={"Origin": "https://ciao.example"})


def test_the_factory_app_revokes_the_grant_its_logout_presents(tmp_path) -> None:
    """The real app, wired the way the engine wires it.

    The factory is what puts the grant store on `app.state`; this proves
    the logout route finds it there and revokes the presented bearer, with
    no fallback of its own.
    """
    from ciao.config import CiaoConfig, WorkspaceConfig
    from ciao.web.app import create_app

    config = CiaoConfig(
        pwa_auth_token="test-secret",
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
    store: TerminalGrantStore = app.state.terminal_grants
    client = TestClient(app, base_url="https://ciao.example")
    client.cookies.set(SESSION_COOKIE, app.state.serializer.dumps({"user": "owner"}))

    session_cookie = client.cookies.get(SESSION_COOKIE) or ""
    token, _grant = store.issue(session_cookie)
    client.cookies.set(TERMINAL_COOKIE, token)

    resp = client.post("/api/auth/logout", headers={"Origin": "https://ciao.example"})

    assert resp.status_code == 200
    assert store.verify(token, session_cookie) is None
    assert store.retained_grant_count() == 0


def test_non_ascii_setup_token_is_401_not_500(tmp_path) -> None:
    token_path = tmp_path / ".runtime" / "setup-token"
    token_path.parent.mkdir()
    token_path.write_text("setup-secret\n", encoding="utf-8")

    resp = _setup_token_client(tmp_path).get("/?setup=s%C3%A9tup", follow_redirects=False)

    assert resp.status_code == 401
    assert token_path.exists()


def test_setup_token_redeems_localhost_session_and_deletes_token(tmp_path) -> None:
    token_path = tmp_path / ".runtime" / "setup-token"
    token_path.parent.mkdir()
    token_path.write_text("setup-secret\n", encoding="utf-8")

    resp = _setup_token_client(tmp_path).get("/?setup=setup-secret", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"] == "/"
    set_cookie = resp.headers["set-cookie"]
    assert "ciao_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Secure" not in set_cookie
    assert not token_path.exists()


def test_setup_token_rejects_remote_peer(tmp_path) -> None:
    token_path = tmp_path / ".runtime" / "setup-token"
    token_path.parent.mkdir()
    token_path.write_text("setup-secret\n", encoding="utf-8")

    resp = _setup_token_client(
        tmp_path, base_url="https://ciao.example", peer=("10.0.0.9", 5555)
    ).get("/?setup=setup-secret", follow_redirects=False)

    assert resp.status_code == 403
    assert "set-cookie" not in resp.headers
    assert token_path.exists()


def test_setup_token_rejects_remote_peer_spoofing_localhost_host(tmp_path) -> None:
    """The redemption gate reads the peer address, not the Host header."""
    token_path = tmp_path / ".runtime" / "setup-token"
    token_path.parent.mkdir()
    token_path.write_text("setup-secret\n", encoding="utf-8")

    resp = _setup_token_client(
        tmp_path, base_url="http://localhost:8443", peer=("10.0.0.9", 5555)
    ).get("/?setup=setup-secret", follow_redirects=False)

    assert resp.status_code == 403
    assert "set-cookie" not in resp.headers
    assert token_path.exists()


def test_setup_token_rejects_invalid_token(tmp_path) -> None:
    token_path = tmp_path / ".runtime" / "setup-token"
    token_path.parent.mkdir()
    token_path.write_text("setup-secret\n", encoding="utf-8")

    resp = _setup_token_client(tmp_path).get("/?setup=wrong", follow_redirects=False)

    assert resp.status_code == 401
    assert "set-cookie" not in resp.headers
    assert token_path.exists()


def test_menubar_chats_requires_loopback_or_session() -> None:
    """Titles and workspace names must not be readable from the network."""

    async def stub(request):
        return JSONResponse({"chats": [{"title": "private", "workspace": "personal"}]})

    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/menubar-chats", stub, methods=["GET"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = SimpleNamespace(pwa_auth_token="x")

    local = TestClient(app, base_url="http://localhost:8443", client=("127.0.0.1", 5555))
    assert local.get("/api/menubar-chats").status_code == 200

    remote = TestClient(app, base_url="http://ciao.example", client=("10.0.0.9", 5555))
    assert remote.get("/api/menubar-chats").status_code == 401


# What `tailscale serve` (1.98) actually adds, captured from a live request: it
# connects from 127.0.0.1, so without these the tailnet caller looks local.
_TAILSCALE_SERVE_HEADERS = {
    "Tailscale-Headers-Info": "https://tailscale.com/s/serve-headers",
    "Tailscale-User-Login": "someone@example.com",
    "X-Forwarded-For": "100.101.252.27",
    "X-Forwarded-Host": "mini.tail1.ts.net",
    "X-Forwarded-Proto": "https",
}


@pytest.mark.parametrize(
    "headers",
    [
        _TAILSCALE_SERVE_HEADERS,
        # Each header alone is enough: other local proxies send a subset.
        {"X-Forwarded-For": "100.1.2.3"},
        {"Forwarded": "for=100.1.2.3"},
        {"X-Forwarded-Host": "mini.tail1.ts.net"},
        {"X-Real-IP": "100.1.2.3"},
        {"CF-Connecting-IP": "203.0.113.5"},
        {"Tailscale-Headers-Info": "https://tailscale.com/s/serve-headers"},
        {"X-Forwarded-Port": "443"},
        {"Via": "1.1 proxy"},
        {"True-Client-IP": "203.0.113.5"},
    ],
)
def test_loopback_peer_behind_a_local_proxy_is_not_local(headers) -> None:
    from ciao.web.auth import is_loopback_client

    async def probe(request):
        return JSONResponse({"local": is_loopback_client(request)})

    app = Starlette(routes=[Route("/", probe)])
    client = TestClient(app, base_url="http://localhost:8443", client=("127.0.0.1", 5555))

    assert client.get("/").json() == {"local": True}
    assert client.get("/", headers=headers).json() == {"local": False}


def test_menubar_feed_is_not_readable_through_tailscale_serve() -> None:
    async def stub(request):
        return JSONResponse({"chats": [{"title": "private"}]})

    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/menubar-chats", stub, methods=["GET"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = SimpleNamespace(pwa_auth_token="x")

    proxied = TestClient(
        app, base_url="https://mini.tail1.ts.net", client=("127.0.0.1", 5555)
    )
    resp = proxied.get("/api/menubar-chats", headers=_TAILSCALE_SERVE_HEADERS)
    assert resp.status_code == 401


def test_setup_token_is_not_redeemable_through_tailscale_serve(tmp_path) -> None:
    token_path = tmp_path / ".runtime" / "setup-token"
    token_path.parent.mkdir()
    token_path.write_text("setup-secret\n", encoding="utf-8")

    resp = _setup_token_client(tmp_path, base_url="https://mini.tail1.ts.net").get(
        "/?setup=setup-secret", headers=_TAILSCALE_SERVE_HEADERS, follow_redirects=False
    )

    assert resp.status_code == 403
    assert "set-cookie" not in resp.headers
    assert token_path.exists()


@pytest.mark.parametrize(
    ("host", "local"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),
        ("127.0.0.2", True),
        ("10.0.0.9", False),
        ("localhost", False),
        ("testclient", False),
    ],
)
def test_loopback_client_reads_the_peer_address(host, local) -> None:
    from ciao.web.auth import is_loopback_client

    async def probe(request):
        return JSONResponse({"local": is_loopback_client(request)})

    app = Starlette(routes=[Route("/", probe)])
    client = TestClient(app, base_url="http://localhost:8443", client=(host, 5555))

    assert client.get("/").json() == {"local": local}


_LOOPBACK_ONLY_ROUTES = [
    ("GET", "/api/menubar-chats"),
    ("POST", "/api/admin/drain"),
    ("POST", "/api/admin/drain/cancel"),
    ("GET", "/api/admin/restart-blockers"),
    ("POST", "/api/auth/host-sign-in"),
]


def _loopback_only_client(
    base_url: str, peer: str = "127.0.0.1"
) -> tuple[TestClient, URLSafeTimedSerializer]:
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route(p, _ok, methods=[m]) for m, p in _LOOPBACK_ONLY_ROUTES],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    return TestClient(app, base_url=base_url, client=(peer, 5555)), serializer


def test_loopback_only_set_is_pinned() -> None:
    assert auth._LOOPBACK_ONLY_API == {p for _, p in _LOOPBACK_ONLY_ROUTES}


@pytest.mark.parametrize("method,path", _LOOPBACK_ONLY_ROUTES)
@pytest.mark.parametrize(
    "base_url",
    ["http://localhost:8443", "http://127.0.0.1:8443", "http://[::1]:8443"],
)
def test_loopback_only_routes_allow_a_loopback_host(method, path, base_url) -> None:
    client, _ = _loopback_only_client(base_url)
    assert client.request(method, path).status_code == 200


@pytest.mark.parametrize("method,path", _LOOPBACK_ONLY_ROUTES)
@pytest.mark.parametrize("headers", [{}, {"Origin": "http://evil.example:8443"}])
def test_loopback_only_routes_refuse_a_rebound_host(method, path, headers) -> None:
    client, _ = _loopback_only_client("http://evil.example:8443")
    assert client.request(method, path, headers=headers).status_code == 401


def test_rebound_host_with_a_session_takes_the_session_path() -> None:
    client, serializer = _loopback_only_client("http://mini.local:8443")
    resp = client.request(
        "GET", "/api/admin/restart-blockers", cookies=_auth_cookie(serializer)
    )
    assert resp.status_code == 200


@pytest.mark.parametrize("method,path", _LOOPBACK_ONLY_ROUTES)
@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Origin": "http://localhost:3000"},
        {"Referer": "https://evil.example/page"},
    ],
)
def test_loopback_only_routes_refuse_a_cross_site_origin(method, path, headers) -> None:
    client, _ = _loopback_only_client("http://localhost:8443")
    resp = client.request(method, path, headers=headers)
    assert resp.status_code == 403
    assert resp.json() == {"error": "forbidden origin"}


def test_loopback_only_source_rejects_a_scheme_mismatch() -> None:
    """The loopback-only path compares the scheme too.

    `https://localhost:8443` is a different origin from the engine's
    `http://localhost:8443`, so a loopback-only route refuses a request that
    names it, even though the host and the port are the same.
    """
    req = _origin_req(
        {"host": "localhost:8443", "origin": "https://localhost:8443"}, scheme="http"
    )
    assert auth._same_origin_source(req) is False
    # The matching scheme is this engine's own origin, so the source passes.
    req = _origin_req(
        {"host": "localhost:8443", "origin": "http://localhost:8443"}, scheme="http"
    )
    assert auth._same_origin_source(req) is True

    client, _ = _loopback_only_client("http://localhost:8443")
    resp = client.request(
        "GET",
        "/api/admin/restart-blockers",
        headers={"Origin": "https://localhost:8443"},
    )
    assert resp.status_code == 403
    assert resp.json() == {"error": "forbidden origin"}

    allowed = client.request(
        "GET",
        "/api/admin/restart-blockers",
        headers={"Origin": "http://localhost:8443"},
    )
    assert allowed.status_code == 200


@pytest.mark.parametrize("method,path", _LOOPBACK_ONLY_ROUTES)
def test_loopback_only_routes_refuse_a_local_proxy(method, path) -> None:
    client, _ = _loopback_only_client("http://localhost:8443")
    resp = client.request(method, path, headers={"X-Forwarded-For": "203.0.113.9"})
    assert resp.status_code == 401


def test_cli_restart_gate_request_is_exempt(monkeypatch) -> None:
    seen: dict[str, str] = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self) -> bytes:
            return b'{"restart_blockers": []}'

    def fake_urlopen(*args, **kwargs):
        req = args[0]
        seen["url"] = req if isinstance(req, str) else req.full_url
        return _Resp()

    monkeypatch.setattr(macos_service.urllib.request, "urlopen", fake_urlopen)
    macos_service.restart_blockers(8443)
    parts = urlsplit(seen["url"])
    assert parts.hostname == "localhost"
    client, _ = _loopback_only_client(f"http://{parts.netloc}")
    assert client.get(parts.path).status_code == 200


def test_open_chat_route_is_gone_and_api_is_protected() -> None:
    from ciao.web import routes_api

    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/open-chat/{chat_id}", _ok, methods=["GET"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    client = TestClient(app, base_url="https://ciao.example")
    assert client.get("/api/open-chat/c1").status_code == 401
    assert not hasattr(routes_api, "open_chat_endpoint")
