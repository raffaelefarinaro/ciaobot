"""``POST /agent/v1/{op}``: the agent CLI's loopback transport.

Bearer-authenticated by the MCP session registry (not the PWA session cookie,
so ``AuthMiddleware`` leaves ``/agent/`` alone the way it leaves ``/mcp/``).
The service is read from ``request.app.state.mcp_service`` like the MCP status
routes do.
"""
from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import JSONResponse

from ciao.agent_surface import AgentDispatcher


async def agent_dispatch_endpoint(request: Request) -> JSONResponse:
    service = getattr(request.app.state, "mcp_service", None)
    if service is None or getattr(service, "control_plane", None) is None:
        return JSONResponse(
            {"ok": False, "error": {"code": "unavailable", "message": "Ciaobot control plane is not ready.", "retryable": True}},
            status_code=503,
        )
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    op = str(request.path_params.get("op") or "")
    body = await request.body()
    if body.strip():
        try:
            arguments = json.loads(body)
        except ValueError:
            return JSONResponse(
                {"ok": False, "error": {"code": "invalid_request", "message": "Body must be a JSON object.", "retryable": False}},
                status_code=400,
            )
    else:
        arguments = {}
    status, envelope = await AgentDispatcher(service).dispatch(token, op, arguments)
    return JSONResponse(envelope, status_code=status)
