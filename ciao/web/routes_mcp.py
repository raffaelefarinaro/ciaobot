"""MCP provider inventory, server on/off, and usage routes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from ciao.providers.claude import get_bundled_claude_path
from ciao.providers.opencode import resolve_opencode_binary
from ciao.web.mcp_inventory import provider_inventory
from ciao.web.mcp_toggle import McpToggleError, set_mcp_server_enabled


def _workspace_of(request: Request) -> str:
    return str(request.query_params.get("workspace", "") or "").strip()


async def mcp_status_endpoint(request: Request) -> JSONResponse:
    config = request.app.state.config
    requested = _workspace_of(request)
    workspace = requested or config.primary_workspace()
    if config.workspace(workspace) is None:
        return JSONResponse({"error": "unknown workspace"}, status_code=404)
    agent_root = config.agent_root(workspace)
    manager = getattr(request.app.state, "project_chat_manager", None)
    denied = {
        tool[len("mcp__") :]
        for tool in config.disallowed_tools_for_workspace(workspace)
        if isinstance(tool, str) and tool.startswith("mcp__") and len(tool) > len("mcp__")
    }
    payload = await provider_inventory(
        workspace=workspace,
        agent_root=Path(agent_root),
        explicitly_denied=denied,
        manager=manager,
        claude_available=get_bundled_claude_path() is not None,
        opencode_available=resolve_opencode_binary() is not None,
    )
    return JSONResponse(payload)


# Session-cookie gated like `PATCH /api/workspaces/{name}`, which edits the same kind of workspace policy; no host-only gate.
async def mcp_server_enabled_endpoint(request: Request) -> JSONResponse:
    try:
        body: Any = await request.json()
    except ValueError:
        body = None
    if (
        not isinstance(body, dict)
        or body.get("provider") not in ("claude", "opencode")
        or not isinstance(body.get("server"), str)
        or not isinstance(body.get("enabled"), bool)
    ):
        return JSONResponse(
            {"error": "Body must be a JSON object with provider, server and enabled.", "reason": "invalid_body"},
            status_code=400,
        )
    config = request.app.state.config
    workspace = str(body.get("workspace") or "").strip() or config.primary_workspace()
    if config.workspace(workspace) is None:
        return JSONResponse({"error": "unknown workspace"}, status_code=404)
    provider: str = body["provider"]
    server: str = body["server"]
    enabled: bool = body["enabled"]
    try:
        result = await asyncio.to_thread(
            set_mcp_server_enabled,
            provider=provider,
            server=server,
            agent_root=Path(config.agent_root(workspace)),
            enabled=enabled,
        )
    except McpToggleError as exc:
        return JSONResponse({"error": exc.message, "reason": exc.reason}, status_code=exc.status)
    manager = getattr(request.app.state, "project_chat_manager", None)
    reload: dict[str, list[str]] = {"reloaded": [], "pending": []}
    if manager is not None:
        if result.enabled:
            manager.forget_mcp_auth_decline(workspace, provider, server)
        if result.changed:
            reload = await manager.reload_workspace_providers(workspace, provider)
    return JSONResponse({"workspace": workspace, **result.to_dict(), "reload": reload})


async def mcp_usage_endpoint(request: Request) -> JSONResponse:
    service = getattr(request.app.state, "mcp_service", None)
    if service is None:
        return JSONResponse(
            {
                "total_calls": 0,
                "total_errors": 0,
                "tool_count": 0,
                "window": {
                    "scope": "lifetime",
                    "label": "No MCP telemetry recorded yet.",
                    "retained_records": 0,
                    "retained_since": "",
                    "rolled_up_calls": 0,
                    "max_records": 0,
                    "rotated_at": "",
                },
                "tools": [],
            }
        )
    return JSONResponse(service.usage())
