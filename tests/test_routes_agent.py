"""``GET /api/mcp/usage``: the operator-only telemetry route of the agent surface."""
from __future__ import annotations

from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.web.app import create_app
from ciao.web.auth import SESSION_COOKIE
from ciao.web.routes_agent import mcp_usage_endpoint

_DELETED_MCP_ROUTES = ("/api/mcp/status", "/api/mcp/servers/enabled")


def _usage_client(service) -> TestClient:
    app = Starlette(routes=[Route("/api/mcp/usage", mcp_usage_endpoint, methods=["GET"])])
    app.state.mcp_service = service
    return TestClient(app)


def _real_app(tmp_path):
    config = CiaoConfig(
        pwa_auth_token="test-secret",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime",
        media_root=tmp_path / "media",
        vault_root=tmp_path / "memory-vault",
        workspaces={"personal": WorkspaceConfig(name="personal", vault_root="memory-vault/personal")},
    )
    return create_app(config)


def test_mcp_usage_route_still_answers_without_a_service() -> None:
    with _usage_client(None) as client:
        body = client.get("/api/mcp/usage").json()

    assert body["total_calls"] == 0
    assert body["total_errors"] == 0
    assert body["tool_count"] == 0
    assert body["tools"] == []
    assert body["window"]["scope"] == "lifetime"
    assert body["window"]["label"] == "No MCP telemetry recorded yet."


def test_mcp_usage_route_returns_the_service_payload() -> None:
    payload = {"total_calls": 3, "total_errors": 1, "tool_count": 1, "tools": [{"name": "chat_list"}]}
    service = SimpleNamespace(usage=lambda: payload)
    with _usage_client(service) as client:
        assert client.get("/api/mcp/usage").json() == payload


def test_the_factory_app_keeps_usage_behind_the_session_and_drops_the_mcp_management_routes(tmp_path) -> None:
    app = _real_app(tmp_path)
    paths = {route.path for route in app.routes if isinstance(route, Route)}
    assert "/api/mcp/usage" in paths
    for gone in _DELETED_MCP_ROUTES:
        assert gone not in paths

    anonymous = TestClient(app, base_url="https://ciao.example")
    assert anonymous.get("/api/mcp/usage").status_code == 401

    signed_in = TestClient(app, base_url="https://ciao.example")
    signed_in.cookies.set(SESSION_COOKIE, app.state.serializer.dumps({"user": "owner"}))
    response = signed_in.get("/api/mcp/usage")
    assert response.status_code == 200
    assert response.json()["total_calls"] == 0
