"""Tests for Standby Remote Client API proxying."""

from pathlib import Path
import pytest
from unittest.mock import AsyncMock, patch
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.node_proxy import StandbyProxyMiddleware, get_proxy_target_url, is_local_path
from ciao.node_state import NodeStateManager


def test_is_local_path():
    assert is_local_path("/api/node/status") is True
    assert is_local_path("/api/node/handover") is True
    assert is_local_path("/api/auth/login") is True
    assert is_local_path("/api/startup-status") is True
    assert is_local_path("/api/chats") is False
    assert is_local_path("/api/projects") is False


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

    # Unreachable active peer should return 503
    res_chats = client.get("/api/chats")
    assert res_chats.status_code == 503
    assert res_chats.json()["peer_unreachable"] is True
