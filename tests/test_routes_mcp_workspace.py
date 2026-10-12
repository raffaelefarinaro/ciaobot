from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from ciao.web.routes_mcp import mcp_status_endpoint


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
        workspace=lambda name: SimpleNamespace(allowed_mcp_servers=[]) if name in roots else None,
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
        "enabled": True, "allowed": None,
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
