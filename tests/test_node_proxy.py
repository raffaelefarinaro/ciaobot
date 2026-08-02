"""Tests for Standby Remote Client API proxying."""

from pathlib import Path
import httpx
import pytest
from unittest.mock import AsyncMock, patch
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.node_proxy import (
    StandbyProxyMiddleware,
    get_proxy_target_url,
    get_static_proxy_target,
    is_local_path,
    is_local_ui_path,
    proxy_websocket,
)
from ciao.node_state import NodeStateManager


def test_is_local_path():
    assert is_local_path("/api/node/status") is True
    assert is_local_path("/api/node/handover") is True
    assert is_local_path("/api/auth/login") is True
    assert is_local_path("/api/startup-status") is True
    assert is_local_path("/api/setup-status") is True
    assert is_local_path("/api/desktop-drop") is True
    assert is_local_path("/api/chats") is False
    assert is_local_path("/api/projects") is False


def test_device_scoped_api_stays_local():
    # The Device panel must report and update *this* machine even though
    # /api/package/* right next to it reports the host's install.
    assert is_local_path("/api/device/package-status") is True
    assert is_local_path("/api/device/update") is True
    assert is_local_path("/api/device") is True


def test_password_settings_are_mirrored_not_local():
    # The password a client logs in with is the host's, so Settings must edit
    # the host's — while login/logout/check stay local, they mint and clear
    # this node's own session.
    assert is_local_path("/api/auth/settings") is False
    assert is_local_path("/api/auth") is True
    assert is_local_path("/api/auth/check") is True
    assert is_local_path("/api/auth/logout") is True


def test_is_local_ui_path():
    assert is_local_ui_path("/device") is True
    assert is_local_ui_path("/device/") is True
    assert is_local_ui_path("/settings") is False
    assert is_local_ui_path("/") is False


def test_get_proxy_target_url(tmp_path: Path):
    mgr = NodeStateManager(tmp_path)
    mgr.add_peer("http://192.168.1.50:8543", peer_id="home-server")

    # In active mode, should not proxy
    app = Starlette()
    app.state.node_state_manager = mgr

    request = Request({"type": "http", "path": "/api/chats", "headers": []})
    request.scope["app"] = app

    assert get_proxy_target_url(request) is None

    # Demote to standby
    mgr.demote()
    assert get_proxy_target_url(request) == "http://192.168.1.50:8543"

    # Local paths should not be proxied even in standby
    request_local = Request({"type": "http", "path": "/api/node/status", "headers": []})
    request_local.scope["app"] = app
    assert get_proxy_target_url(request_local) is None


def test_standby_proxy_middleware_routing(tmp_path: Path):
    mgr = NodeStateManager(tmp_path)
    mgr.add_peer("http://active-peer.local:8443", peer_id="active-peer")
    mgr.demote()

    async def local_chats(request):
        return JSONResponse({"source": "local"})

    async def local_node_status(request):
        return JSONResponse({"source": "local_node_status"})

    routes = [
        Route("/api/chats", local_chats),
        Route("/api/node/status", local_node_status),
    ]

    middleware = [Middleware(StandbyProxyMiddleware)]
    app = Starlette(routes=routes, middleware=middleware)
    app.state.node_state_manager = mgr

    client = TestClient(app)

    # Local endpoint should pass through to local handler
    res_status = client.get("/api/node/status")
    assert res_status.status_code == 200
    assert res_status.json()["source"] == "local_node_status"

    # Unreachable host should return 503 (mock connect failure; sandbox may
    # otherwise answer with a policy 403 instead of raising).
    import httpx

    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_request:
        mock_request.side_effect = httpx.ConnectError("offline")
        res_chats = client.get("/api/chats")
    assert res_chats.status_code == 503
    assert res_chats.json()["peer_unreachable"] is True
    assert res_chats.json().get("client") is True


def _client_app(tmp_path: Path, static_dir: Path) -> tuple[Starlette, NodeStateManager]:
    """App in client mode whose local bundle lives in ``static_dir``."""
    mgr = NodeStateManager(tmp_path)
    mgr.connect_as_client("http://host.local:8443")

    async def local_device(request):
        return JSONResponse({"source": "local_device_page"})

    app = Starlette(
        routes=[Route("/device", local_device)],
        middleware=[Middleware(StandbyProxyMiddleware)],
    )
    app.state.node_state_manager = mgr
    return app, mgr


def test_static_proxy_target_mirrors_host_bundle(tmp_path: Path, monkeypatch):
    import ciao.web.app as web_app

    static_dir = tmp_path / "static"
    (static_dir / "assets").mkdir(parents=True)
    (static_dir / "index.html").write_text("<html>local shell</html>")
    (static_dir / "assets" / "index-local.js").write_text("// local build")
    monkeypatch.setattr(web_app, "STATIC_DIR", static_dir)

    app, _mgr = _client_app(tmp_path / "state", static_dir)

    def probe(path: str, method: str = "GET"):
        request = Request({"type": "http", "method": method, "path": path, "headers": []})
        request.scope["app"] = app
        return get_static_proxy_target(request)

    # Navigations and unknown (host-built) asset hashes come from the host.
    assert probe("/") == "http://host.local:8443"
    assert probe("/settings") == "http://host.local:8443"
    assert probe("/assets/index-abc123.js") == "http://host.local:8443"
    # Hashed names the local build also has are byte-identical: serve locally.
    assert probe("/assets/index-local.js") is None
    # The escape hatch and the API are never static-proxied.
    assert probe("/device") is None
    assert probe("/api/chats") is None
    # Only reads are mirrored; a POST to an SPA path is not a bundle fetch.
    assert probe("/", method="POST") is None


def test_unhashed_static_names_always_come_from_the_host(tmp_path: Path, monkeypatch):
    """Only `/assets` is content-hashed, so only `/assets` is safe to serve locally.

    Every other static name is stable across builds. Serving the local copy of
    one would let a version-skewed client run its own service worker or manifest
    against the host's bundle, which is exactly the skew client mode hides.
    """
    import ciao.web.app as web_app

    static_dir = tmp_path / "static"
    (static_dir / "assets").mkdir(parents=True)
    for name in ("index.html", "sw.js", "manifest.json", "favicon.ico"):
        (static_dir / name).write_text("local")
    (static_dir / "assets" / "index-local.js").write_text("// local build")
    monkeypatch.setattr(web_app, "STATIC_DIR", static_dir)

    app, _mgr = _client_app(tmp_path / "state", static_dir)

    def probe(path: str):
        request = Request({"type": "http", "method": "GET", "path": path, "headers": []})
        request.scope["app"] = app
        return get_static_proxy_target(request)

    for path in ("/index.html", "/sw.js", "/manifest.json", "/favicon.ico"):
        assert probe(path) == "http://host.local:8443", path
    # The hashed asset is still the local shortcut.
    assert probe("/assets/index-local.js") is None


def test_client_serves_host_index_and_local_device_page(tmp_path: Path, monkeypatch):
    import ciao.web.app as web_app

    static_dir = tmp_path / "static"
    static_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text("<html>local shell</html>")
    monkeypatch.setattr(web_app, "STATIC_DIR", static_dir)

    app, _mgr = _client_app(tmp_path / "state", static_dir)
    client = TestClient(app)

    host_response = httpx.Response(
        200,
        headers={"content-type": "text/html; charset=utf-8"},
        content=b"<html>host bundle</html>",
    )
    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_request:
        mock_request.return_value = host_response
        res = client.get("/", headers={"accept": "text/html"})
    assert res.status_code == 200
    assert "host bundle" in res.text

    # The device panel is answered locally, host or no host.
    res_device = client.get("/device")
    assert res_device.status_code == 200
    assert res_device.json()["source"] == "local_device_page"


def test_unreachable_host_falls_back_to_local_shell(tmp_path: Path, monkeypatch):
    import ciao.web.app as web_app

    static_dir = tmp_path / "static"
    static_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text("<html>local shell</html>")
    monkeypatch.setattr(web_app, "STATIC_DIR", static_dir)

    app, _mgr = _client_app(tmp_path / "state", static_dir)
    client = TestClient(app)

    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_request:
        mock_request.side_effect = httpx.ConnectError("offline")
        navigation = client.get("/", headers={"accept": "text/html"})
        asset = client.get("/assets/index-abc123.js", headers={"accept": "*/*"})

    # A dead host must not mean a blank app: the local shell boots and can
    # still reach /device to disconnect.
    assert navigation.status_code == 200
    assert "local shell" in navigation.text
    # Non-navigations get an honest error instead of HTML pretending to be JS.
    assert asset.status_code == 503
    assert asset.json()["peer_unreachable"] is True


def test_host_issued_session_cookie_is_captured_not_forwarded(tmp_path: Path, monkeypatch):
    """Changing the host password from a client keeps the tunnel alive."""
    import ciao.web.app as web_app
    from ciao.web.auth import SESSION_COOKIE

    static_dir = tmp_path / "static"
    static_dir.mkdir(parents=True)
    monkeypatch.setattr(web_app, "STATIC_DIR", static_dir)

    app, mgr = _client_app(tmp_path / "state", static_dir)
    client = TestClient(app)

    rotated = httpx.Response(
        200,
        headers={
            "content-type": "application/json",
            "set-cookie": f"{SESSION_COOKIE}=rotated-host-session; Path=/",
        },
        json={"ok": True},
    )
    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_request:
        mock_request.return_value = rotated
        res = client.post("/api/auth/settings", json={"auth_required": True})

    assert res.status_code == 200
    assert mgr.get_host_session() == "rotated-host-session"
    # The browser keeps this node's own session; the host's never leaks through.
    assert "set-cookie" not in {k.lower() for k in res.headers}


@pytest.mark.asyncio
async def test_websocket_proxy_reports_host_connection_state() -> None:
    class FailingConnection:
        async def __aenter__(self):
            raise OSError("host offline")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    websocket = AsyncMock()
    websocket.url.path = "/ws/chat/chat-1"
    websocket.url.query = ""
    websocket.app.state.node_state_manager = None

    with patch("websockets.connect", return_value=FailingConnection()):
        await proxy_websocket(websocket, "http://10.0.0.5:8443")

    websocket.accept.assert_awaited_once()
    websocket.send_json.assert_awaited_once_with({"type": "host_unreachable"})
    websocket.close.assert_awaited_once_with(code=4004)


@pytest.mark.asyncio
async def test_websocket_proxy_reports_a_client_to_host_forwarding_failure() -> None:
    import asyncio

    class BrokenRemote:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def send(self, message: str) -> None:
            raise OSError("host connection dropped")

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()
            raise StopAsyncIteration

    websocket = AsyncMock()
    websocket.url.path = "/ws/chat/chat-1"
    websocket.url.query = ""
    websocket.app.state.node_state_manager = None
    websocket.receive_text.return_value = '{"type":"message","text":"continue"}'

    with patch("websockets.connect", return_value=BrokenRemote()):
        await proxy_websocket(websocket, "http://10.0.0.5:8443")

    websocket.send_json.assert_awaited_once_with({"type": "host_unreachable"})
    websocket.close.assert_awaited_once_with(code=4004)
