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

#: Upper bound on one agent request body. ``chat_handover``/``chat_fork`` with
#: carried history are the largest legitimate callers and stay far below this.
MAX_BODY_BYTES = 1_048_576


def _too_large() -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": {"code": "payload_too_large", "message": f"Request body exceeds {MAX_BODY_BYTES} bytes.", "retryable": False}},
        status_code=413,
    )


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
    # Authenticate before touching the body: with PWA_HOST=0.0.0.0 this route
    # is reachable from the LAN, and buffering an unauthenticated upload first
    # would let anyone fill memory before the 401. Then read the body in
    # bounded chunks; a control-plane call is a few hundred bytes, the largest
    # legitimate payload (a handover with visible history) well under the cap.
    if not token or await service.registry.verify_token(token) is None:
        return JSONResponse(
            {"ok": False, "error": {"code": "unauthorized", "message": "A valid Ciaobot agent token is required.", "retryable": False}},
            status_code=401,
        )
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return _too_large()
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_BODY_BYTES:
            return _too_large()
        chunks.append(chunk)
    body = b"".join(chunks)
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
