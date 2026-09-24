from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.requests import Request
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.node_proxy import (
    StandbyProxyMiddleware,
    get_static_proxy_target,
    is_local_path,
)
from ciao.node_state import NodeStateManager
from ciao.web.auth import AuthMiddleware, make_serializer


async def _local_status(request):
    return JSONResponse({"ok": True, "origin": "local"})


async def _local_handover(request):
    return JSONResponse({"ok": True, "forced": True})


async def _local_drop(request):
    return JSONResponse({"ok": True})


def _client_app(tmp_path: Path) -> Starlette:
    serializer = make_serializer("host-secret")
    app = Starlette(
        routes=[
            Route("/api/node/status", _local_status, methods=["GET"]),
            Route("/api/node/handover", _local_handover, methods=["POST"]),
            Route("/api/device/update", _local_status, methods=["POST"]),
            Route("/api/desktop-drop", _local_drop, methods=["POST"]),
            Route("/device", _local_status, methods=["GET"]),
        ],
        middleware=[
            Middleware(AuthMiddleware, serializer=serializer),
            Middleware(StandbyProxyMiddleware),
        ],
    )
    app.state.serializer = serializer
    app.state.config = SimpleNamespace(
        pwa_auth_required=True,
        pwa_auth_token="host-secret",
        pwa_port=8443,
    )
    manager = NodeStateManager(tmp_path / "state")
    manager.connect_as_client("http://host.example:8443", host_session="host-session")
    app.state.node_state_manager = manager
    return app


def test_remote_content_cannot_reach_local_controls(tmp_path: Path) -> None:
    app = _client_app(tmp_path)
    client = TestClient(
        app, base_url="http://localhost:8443", client=("127.0.0.1", 5555)
    )

    assert client.get("/api/node/status").status_code == 403
    assert client.post("/api/node/handover", json={"force": True}).status_code == 403
    assert client.post("/api/device/update").status_code == 403
    assert client.post("/api/desktop-drop", json={"grant_id": "x"}).status_code == 403
    assert client.post(
        "/api/device/update",
        headers={"X-Ciao-Local-Control": "1"},
    ).status_code == 403


def test_native_sessions_stays_on_the_local_control_boundary(tmp_path: Path) -> None:
    app = _client_app(tmp_path)
    assert is_local_path("/api/native/sessions")
    client = TestClient(
        app, base_url="http://localhost:8443", client=("127.0.0.1", 5555)
    )
    assert client.get("/api/native/sessions").status_code == 403


def test_device_origin_is_the_only_local_control_surface(tmp_path: Path) -> None:
    app = _client_app(tmp_path)
    content = TestClient(
        app, base_url="http://localhost:8443", client=("127.0.0.1", 5555)
    )
    control = TestClient(
        app, base_url="http://127.0.0.1:8443", client=("127.0.0.1", 5555)
    )

    redirected = content.get("/device", follow_redirects=False)
    assert redirected.status_code == 307
    assert redirected.headers["location"] == "http://127.0.0.1:8443/device"

    assert control.get(
        "/api/node/status", headers={"X-Ciao-Local-Control": "1"}
    ).status_code == 200
    assert control.post(
        "/api/node/handover",
        json={"force": True},
        headers={"Origin": "http://localhost:8443", "X-Ciao-Local-Control": "1"},
    ).status_code == 403
    assert control.post("/api/node/handover", json={"force": True}).status_code == 403
    assert control.post(
        "/api/node/handover",
        json={"force": True},
        headers={
            "Origin": "http://user@127.0.0.1:8443",
            "X-Ciao-Local-Control": "1",
        },
    ).status_code == 403
    assert control.post(
        "/api/node/handover",
        json={"force": True},
        headers={
            "Origin": "http://127.0.0.1:8443",
            "X-Ciao-Local-Control": "1",
        },
    ).status_code == 200
    assert control.post(
        "/api/device/update", headers={"X-Ciao-Local-Control": "1"}
    ).status_code == 200
    assert control.post(
        "/api/desktop-drop",
        json={"grant_id": "x"},
        headers={"X-Ciao-Local-Control": "1"},
    ).status_code == 200

    remote_peer = TestClient(
        app, base_url="http://127.0.0.1:8443", client=("10.0.0.9", 5555)
    )
    assert remote_peer.get(
        "/api/node/status", headers={"X-Ciao-Local-Control": "1"}
    ).status_code == 403


def test_default_http_port_is_normalized_for_local_controls(tmp_path: Path) -> None:
    app = _client_app(tmp_path)
    app.state.config.pwa_port = 80
    control = TestClient(
        app, base_url="http://127.0.0.1", client=("127.0.0.1", 5555)
    )

    response = control.post(
        "/api/node/handover",
        json={"force": True},
        headers={
            "Origin": "http://127.0.0.1",
            "X-Ciao-Local-Control": "1",
        },
    )
    assert response.status_code == 200


def test_control_origin_redirects_remote_content_to_content_origin(tmp_path: Path) -> None:
    app = _client_app(tmp_path)
    client = TestClient(
        app, base_url="http://127.0.0.1:8443", client=("127.0.0.1", 5555)
    )

    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "http://localhost:8443/"


def test_control_origin_serves_its_own_static_files(tmp_path: Path) -> None:
    app = _client_app(tmp_path)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/assets/index-local.js",
            "headers": [(b"host", b"127.0.0.1:8443")],
        }
    )
    request.scope["app"] = app
    assert get_static_proxy_target(request) is None


def test_invalid_node_state_blocks_content_api_fallback(tmp_path: Path) -> None:
    app = _client_app(tmp_path)
    state_file = tmp_path / "state" / "node_state.json"
    state_file.write_text("{", encoding="utf-8")
    content = TestClient(
        app,
        base_url="http://localhost:8443",
        client=("127.0.0.1", 5555),
    )
    content.cookies.set("ciao_session", app.state.serializer.dumps({"user": "owner"}))

    response = content.get("/api/chats")

    assert response.status_code == 503
    assert response.json()["client"] is True


def test_host_redirect_is_not_forwarded(tmp_path: Path) -> None:
    app = _client_app(tmp_path)
    client = TestClient(
        app, base_url="http://localhost:8443", client=("127.0.0.1", 5555)
    )
    client.cookies.set("ciao_session", app.state.serializer.dumps({"user": "owner"}))
    response = httpx.Response(
        302,
        headers={"location": "http://attacker.example/steal"},
        request=httpx.Request("GET", "http://host.example:8443/api/chats"),
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "httpx.AsyncClient.request",
            lambda *args, **kwargs: _async_value(response),
        )
        result = client.get("/api/chats")
    assert result.status_code == 502
    assert "location" not in result.headers
    assert result.json()["error"] == "Host redirect refused"


async def _async_value(value):
    return value


def test_peer_redirect_without_location_is_refused(tmp_path: Path) -> None:
    app = _client_app(tmp_path)
    content = TestClient(
        app,
        base_url="http://localhost:8443",
        client=("127.0.0.1", 5555),
    )
    content.cookies.set("ciao_session", app.state.serializer.dumps({"user": "owner"}))
    response = httpx.Response(302, request=httpx.Request("GET", "http://host.example:8443/api/chats"))
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "httpx.AsyncClient.request",
            lambda *args, **kwargs: _async_value(response),
        )
        result = content.get("/api/chats")
    assert result.status_code == 502
    assert "location" not in result.headers


def test_tauri_capability_does_not_grant_remote_content() -> None:
    root = Path(__file__).parents[1]
    capability = json.loads(
        (root / "desktop/src-tauri/capabilities/main.json").read_text(encoding="utf-8")
    )
    assert "remote" not in capability
    assert capability["windows"] == ["update"]
    assert "trigger-app-update" not in capability["permissions"]
    assert not (root / "desktop/src-tauri/permissions/trigger-app-update.toml").exists()


def test_remote_control_routes_are_listed_as_local() -> None:
    root = Path(__file__).parents[1]
    source = (root / "ciao/web/remote_boundary.py").read_text(encoding="utf-8")
    for route in ("/api/node", "/api/device", "/api/desktop-drop"):
        assert route in source
