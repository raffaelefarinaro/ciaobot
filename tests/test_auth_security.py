from __future__ import annotations

from types import SimpleNamespace

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

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


def _origin_req(headers: dict[str, str], allowed: tuple[str, ...] = ()) -> object:
    cfg = SimpleNamespace(pwa_allowed_origins=allowed)
    return SimpleNamespace(
        headers={k.lower(): v for k, v in headers.items()},
        url=SimpleNamespace(hostname=None, port=None),
        app=SimpleNamespace(state=SimpleNamespace(config=cfg)),
    )


def test_same_origin_accepts_matching_host() -> None:
    from ciao.web.auth import _same_origin

    req = _origin_req({"host": "ciao.example"})
    assert _same_origin(req, "https://ciao.example") is True


def test_same_origin_rejects_cross_origin() -> None:
    from ciao.web.auth import _same_origin

    req = _origin_req({"host": "ciao.example"})
    assert _same_origin(req, "https://evil.example") is False


def test_same_origin_accepts_proxy_forwarded_host() -> None:
    """Behind a proxy the bound Host differs from the browser origin; the
    proxy-declared X-Forwarded-Host makes the WS/state-change handshake pass."""
    from ciao.web.auth import _same_origin

    req = _origin_req({"host": "localhost:8765", "x-forwarded-host": "app.example"})
    assert _same_origin(req, "https://app.example") is True
    # A genuine cross-origin still fails even with the forwarded host present.
    assert _same_origin(req, "https://evil.example") is False


def test_same_origin_accepts_configured_allowlist() -> None:
    from ciao.web.auth import _same_origin

    req = _origin_req({"host": "localhost"}, allowed=("app.example",))
    assert _same_origin(req, "https://app.example") is True
    assert _same_origin(req, "https://other.example") is False


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


def _handover_app(host_url: str = "http://100.1.2.3:8443", host_session=None):
    """App exposing /api/node/handover over a stubbed NodeStateManager."""
    from ciao.node_state import NodeStateManager
    from ciao.web.routes_node import node_handover_endpoint

    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/node/handover", node_handover_endpoint, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = SimpleNamespace(pwa_auth_required=True, pwa_auth_token="x")
    node_mgr = NodeStateManager.__new__(NodeStateManager)
    state = {"role": "client", "host_url": host_url, "host_session": host_session}

    def promote():
        state["role"] = "host"
        state["host_url"] = None
        state["host_session"] = None
        return {"role": "host", "host_url": None}

    node_mgr.get_host_url = lambda: state["host_url"]  # type: ignore[method-assign]
    node_mgr.get_host_session = lambda: state["host_session"]  # type: ignore[method-assign]
    node_mgr.promote = promote  # type: ignore[method-assign]
    app.state.node_state_manager = node_mgr
    app.state.local_session_manager = None
    return app, state


def test_node_handover_bailout_allowed_from_loopback_without_session() -> None:
    """Stuck clients on /login must force-promote without a host session."""
    app, state = _handover_app()

    client = TestClient(app, base_url="https://ciao.example", client=("127.0.0.1", 5555))
    resp = client.post(
        "/api/node/handover",
        json={"force": True},
        headers={"Origin": "https://ciao.example"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert state["role"] == "host"


def test_node_handover_rejects_remote_peer_without_session() -> None:
    """Force-promote is identity-less, so the network must not reach it."""
    app, state = _handover_app()

    client = TestClient(app, base_url="https://ciao.example", client=("10.0.0.9", 5555))
    resp = client.post(
        "/api/node/handover",
        json={"force": True},
        headers={"Origin": "https://ciao.example"},
    )
    assert resp.status_code == 401
    assert state["role"] == "client"


def test_node_handover_ignores_a_spoofed_localhost_host_header() -> None:
    """The gate reads the peer address, not the caller-supplied Host header."""
    app, state = _handover_app()

    client = TestClient(app, base_url="http://10.0.0.9:8443", client=("10.0.0.9", 5555))
    resp = client.post("/api/node/handover", json={"force": True}, headers={"Host": "localhost"})
    assert resp.status_code == 401
    assert state["role"] == "client"


def test_node_handover_rejects_target_url_other_than_the_connected_host() -> None:
    """The demote call carries the host session, so the URL must not be free-form."""
    app, state = _handover_app(host_session="host-cookie")

    client = TestClient(app, base_url="https://ciao.example", client=("127.0.0.1", 5555))
    resp = client.post(
        "/api/node/handover",
        json={"target_node_url": "http://attacker.example.com", "force": False},
        headers={"Origin": "https://ciao.example"},
    )
    assert resp.status_code == 400
    assert "connected host" in resp.json()["error"]
    assert state["role"] == "client"


def test_node_handover_bailout_rejects_cross_origin() -> None:
    app, _state = _handover_app()

    client = TestClient(app, base_url="https://ciao.example", client=("127.0.0.1", 5555))
    resp = client.post(
        "/api/node/handover",
        json={"force": True},
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403


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
    app.state.config = SimpleNamespace(pwa_auth_required=True, pwa_auth_token="x")

    local = TestClient(app, base_url="http://localhost:8443", client=("127.0.0.1", 5555))
    assert local.get("/api/menubar-chats").status_code == 200

    remote = TestClient(app, base_url="http://ciao.example", client=("10.0.0.9", 5555))
    assert remote.get("/api/menubar-chats").status_code == 401


def test_menubar_notifications_requires_loopback_or_session() -> None:
    """Notification bodies are message snippets, so the tray's feed is gated the
    same way as the chat list: loopback without a session, session otherwise.

    A client node reaches the host's copy through the proxy, which presents the
    stored host session, so this staying loopback-only does not break it.
    """

    async def stub(request):
        return JSONResponse({"notifications": [{"ts": 1.0, "body": "private"}]})

    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/menubar-notifications", stub, methods=["GET"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = SimpleNamespace(pwa_auth_required=True, pwa_auth_token="x")

    local = TestClient(app, base_url="http://localhost:8443", client=("127.0.0.1", 5555))
    assert local.get("/api/menubar-notifications").status_code == 200

    remote = TestClient(app, base_url="http://ciao.example", client=("10.0.0.9", 5555))
    assert remote.get("/api/menubar-notifications").status_code == 401
