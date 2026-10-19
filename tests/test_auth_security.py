from __future__ import annotations

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
from ciao.web import auth
from ciao.web.auth import AuthMiddleware, SESSION_COOKIE
from ciao.web.routes_auth import auth_login, auth_logout


async def _ok(_request):
    return JSONResponse({"ok": True})


def _auth_cookie(serializer: URLSafeTimedSerializer) -> dict[str, str]:
    return {SESSION_COOKIE: serializer.dumps({"user": "owner"})}


def _protected_client() -> tuple[TestClient, URLSafeTimedSerializer]:
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/demo", _ok, methods=["GET", "POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    return TestClient(app, base_url="https://ciao.example"), serializer


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


def test_safe_request_does_not_require_origin() -> None:
    client, serializer = _protected_client()
    resp = client.get("/api/demo", cookies=_auth_cookie(serializer))

    assert resp.status_code == 200


def _origin_req(headers: dict[str, str]) -> object:
    return SimpleNamespace(
        headers={k.lower(): v for k, v in headers.items()},
        url=SimpleNamespace(hostname=None, port=None),
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

    req = _origin_req({"host": "localhost:8765", "x-forwarded-host": "app.example"})
    assert _same_origin(req, "https://app.example") is True
    # A genuine cross-origin still fails even with the forwarded host present.
    assert _same_origin(req, "https://evil.example") is False


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
