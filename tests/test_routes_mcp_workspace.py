from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import agent_paths
from ciao.web.auth import AuthMiddleware, SESSION_COOKIE
from ciao.web.routes_mcp import mcp_server_enabled_endpoint, mcp_status_endpoint

_key = agent_paths.claude_config_project_key
_ORIGIN = "https://ciao.example"
_URL = "/api/mcp/servers/enabled"


def test_mcp_corrupt_resolver_keeps_claude_inventory_and_no_500(native_home, monkeypatch):
    import ciao.tool_path as tool_path
    import ciao.providers.opencode as opencode
    import ciao.web.routes_mcp as routes

    home, root = native_home
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"example": {"command": "example"}}}))
    seen = []
    shared_lookups = []
    def resolve_shared(cmd):
        shared_lookups.append(cmd)
        return "/fake/shared-opencode"

    monkeypatch.setattr(tool_path, "resolve_tool", resolve_shared)
    monkeypatch.setattr(opencode, "resolve_tool", resolve_shared)
    inventory = routes.provider_inventory

    async def spy(**kwargs):
        seen.append(kwargs.copy())
        return await inventory(**kwargs)

    def broken():
        raise OSError("private-sentinel")

    monkeypatch.setattr(routes, "provider_inventory", spy)
    monkeypatch.setattr(routes, "resolve_opencode_binary", broken)
    monkeypatch.setattr(routes, "get_bundled_claude_path", lambda: "/claude")
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(routes=[Route("/api/mcp/status", mcp_status_endpoint)],
                    middleware=[Middleware(AuthMiddleware, serializer=serializer)])
    app.state.serializer = serializer
    app.state.config = SimpleNamespace(primary_workspace=lambda: "work", workspace=lambda name: object(),
        agent_root=lambda name: root, disallowed_tools_for_workspace=lambda name: ["mcp__example"])
    client = TestClient(app, base_url=_ORIGIN)
    assert client.get("/api/mcp/status?workspace=work").status_code == 401
    client.cookies.set(SESSION_COOKIE, serializer.dumps({"user": "owner"}))
    response = client.get("/api/mcp/status?workspace=work")
    assert response.status_code == 200
    assert seen[0]["opencode_available"] is False
    assert seen[0]["claude_available"] is True
    assert seen[0]["workspace"] == "work"
    assert seen[0]["explicitly_denied"] == {"example"}
    assert response.json()["workspace"] == "work"
    claude = next(p for p in response.json()["providers"] if p["provider"] == "claude")
    assert any(server["name"] == "example" for server in claude["servers"])
    assert "private-sentinel" not in response.text
    assert "opencode" not in shared_lookups


@pytest.mark.asyncio
async def test_status_includes_native_inventory_and_scoped_live_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ciao.web.routes_mcp as routes

    roots = {"work": tmp_path / "work", "personal": tmp_path / "personal"}
    for root in roots.values():
        root.mkdir()
    (roots["work"] / "opencode.jsonc").write_text(
        '{ // safe config metadata\n "mcp": {"servers": {"notion": {"type":"remote", "url":"https://secret.invalid"},},},}',
        encoding="utf-8",
    )

    class Manager:
        async def provider_mcp_runtime_connections(self, workspace, names):
            assert workspace == "work"
            assert names["opencode"] == ["notion"]
            return {
                "claude": {},
                "opencode": {
                    "notion": [
                        {"state": "failed", "provider": "opencode", "chat_id": "chat-1", "error": "MCP server connection failed. Check the provider logs."}
                    ]
                },
            }

    config = SimpleNamespace(
        primary_workspace=lambda: "personal",
        workspace=lambda name: SimpleNamespace() if name in roots else None,
        agent_root=lambda name: roots[name],
        disallowed_tools_for_workspace=lambda name: ["mcp__notion"] if name == "work" else [],
    )
    monkeypatch.setattr(routes, "get_bundled_claude_path", lambda: "/claude")
    monkeypatch.setattr(routes, "resolve_opencode_binary", lambda: "/opencode")
    app = SimpleNamespace(state=SimpleNamespace(config=config, project_chat_manager=Manager()))
    request = Request({
        "type": "http", "method": "GET", "path": "/api/mcp/status",
        "query_string": b"workspace=work", "headers": [], "app": app,
    })

    response = await mcp_status_endpoint(request)
    body = json.loads(response.body)
    opencode = next(p for p in body["providers"] if p["provider"] == "opencode")
    assert body["workspace"] == "work"
    assert body["agent_root"] == str(roots["work"])
    assert opencode["servers"] == [{
        "name": "notion", "scope": "project", "source": "project", "kind": "remote",
        "enabled": True, "allowed": None, "applies_to": str(roots["work"] / "opencode.jsonc"),
        "connections": [{"state": "failed", "provider": "opencode", "chat_id": "chat-1", "error": "MCP server connection failed. Check the provider logs."}],
    }]
    assert "secret.invalid" not in response.body.decode()


@pytest.mark.asyncio
async def test_status_refuses_unknown_workspace() -> None:
    config = SimpleNamespace(
        primary_workspace=lambda: "personal",
        workspace=lambda _name: None,
    )
    request = Request({
        "type": "http", "method": "GET", "path": "/api/mcp/status",
        "query_string": b"workspace=ghost", "headers": [],
        "app": SimpleNamespace(state=SimpleNamespace(config=config)),
    })
    response = await mcp_status_endpoint(request)
    assert response.status_code == 404
    assert json.loads(response.body) == {"error": "unknown workspace"}


class _Manager:
    def __init__(self) -> None:
        self.reloads: list[tuple[str, str]] = []
        self.forgets: list[tuple[str, str, str]] = []

    async def reload_workspace_providers(self, workspace: str, provider: str) -> dict[str, list[str]]:
        self.reloads.append((workspace, provider))
        return {"reloaded": ["c1"], "pending": ["c2"]}

    def forget_mcp_auth_decline(self, workspace: str, provider: str, server: str) -> None:
        self.forgets.append((workspace, provider, server))


@pytest.fixture
def native_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    for name in ("CLAUDE_CONFIG_DIR", "CLAUDE_CONFIG_PATH", "OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG_CONTENT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    root = tmp_path / "work"
    root.mkdir()
    (home / ".claude.json").write_text(json.dumps({"projects": {}}), encoding="utf-8")
    return home, root


def _enabled_app(root: Path, manager: _Manager, middleware: list[Middleware] | None = None) -> Starlette:
    app = Starlette(
        routes=[Route(_URL, mcp_server_enabled_endpoint, methods=["POST"])],
        middleware=middleware or [],
    )
    app.state.config = SimpleNamespace(
        primary_workspace=lambda: "work",
        workspace=lambda name: SimpleNamespace() if name == "work" else None,
        agent_root=lambda _name: root,
    )
    app.state.project_chat_manager = manager
    return app


def test_enabled_route_turns_a_claude_server_off_and_reloads(native_home: tuple[Path, Path]) -> None:
    _home, root = native_home
    manager = _Manager()
    client = TestClient(_enabled_app(root, manager))

    response = client.post(_URL, json={"provider": "claude", "server": "notion", "enabled": False})

    assert response.status_code == 200
    assert response.json() == {
        "workspace": "work", "provider": "claude", "server": "notion", "enabled": False,
        "changed": True, "applies_to": _key(root), "warning": None,
        "reload": {"reloaded": ["c1"], "pending": ["c2"]},
    }
    assert manager.reloads == [("work", "claude")]
    assert manager.forgets == []


def test_enabled_route_without_change_does_not_reload_and_turning_on_forgets_the_decline(
    native_home: tuple[Path, Path],
) -> None:
    _home, root = native_home
    manager = _Manager()
    client = TestClient(_enabled_app(root, manager))

    response = client.post(_URL, json={"provider": "claude", "server": "notion", "enabled": True})

    body = response.json()
    assert body["changed"] is False
    assert body["reload"] == {"reloaded": [], "pending": []}
    assert manager.reloads == []
    assert manager.forgets == [("work", "claude", "notion")]


def test_enabled_route_refuses_bad_bodies(native_home: tuple[Path, Path]) -> None:
    _home, root = native_home
    client = TestClient(_enabled_app(root, _Manager()))

    for payload in (
        {"provider": "codex", "server": "notion", "enabled": False},
        {"provider": "claude", "server": "notion", "enabled": "no"},
    ):
        response = client.post(_URL, json=payload)
        assert response.status_code == 400
        assert response.json()["reason"] == "invalid_body"

    response = client.post(_URL, content=b"x")
    assert response.status_code == 400
    assert response.json()["reason"] == "invalid_body"

    response = client.post(_URL, json={"workspace": "ghost", "provider": "claude", "server": "notion", "enabled": False})
    assert response.status_code == 404
    assert response.json() == {"error": "unknown workspace"}


def test_enabled_route_maps_toggle_refusals(native_home: tuple[Path, Path]) -> None:
    _home, root = native_home
    client = TestClient(_enabled_app(root, _Manager()))

    response = client.post(_URL, json={"provider": "opencode", "server": "linear", "enabled": False})

    assert response.status_code == 404
    assert response.json()["reason"] == "not_configured"


def test_enabled_route_needs_a_session(native_home: tuple[Path, Path]) -> None:
    home, root = native_home
    serializer = URLSafeTimedSerializer("test-secret")
    app = _enabled_app(root, _Manager(), middleware=[Middleware(AuthMiddleware, serializer=serializer)])
    app.state.serializer = serializer
    client = TestClient(app, base_url=_ORIGIN, raise_server_exceptions=False)
    before = (home / ".claude.json").read_bytes()

    response = client.post(
        _URL, json={"provider": "claude", "server": "notion", "enabled": False}, headers={"Origin": _ORIGIN}
    )

    assert response.status_code == 401
    assert (home / ".claude.json").read_bytes() == before
