"""MCP provider inventory and usage routes."""

from __future__ import annotations

from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

from ciao.providers.claude import get_bundled_claude_path
from ciao.providers.opencode import resolve_opencode_binary
from ciao.web.mcp_inventory import provider_inventory


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
