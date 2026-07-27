"""Standby node proxy middleware for Ciaobot multi-device deployment.

When a node is in 'standby' mode and an active peer URL is registered,
this middleware intercepts API calls and proxies them to the active leader node,
providing a seamless remote-client experience for the PWA and macOS tray.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import httpx
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

logger = logging.getLogger(__name__)

# Endpoints that MUST always be handled locally on the standby node
EXCLUDED_LOCAL_PATHS: set[str] = {
    "/api/node/status",
    "/api/node/handover",
    "/api/node/demote",
    "/api/node/peers",
    "/api/startup-status",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/check",
}

# Header keys to strip before proxying to prevent host mismatch or double compression
STRIP_HEADERS: set[str] = {
    "host",
    "content-length",
    "connection",
    "transfer-encoding",
}


def is_local_path(path: str) -> bool:
    cleaned = path.rstrip("/")
    if cleaned in EXCLUDED_LOCAL_PATHS:
        return True
    if cleaned.startswith("/api/auth"):
        return True
    if cleaned.startswith("/api/node"):
        return True
    return False


def get_proxy_target_url(request: Request) -> str | None:
    """Returns target active peer URL if request should be proxied in standby mode."""
    # Only proxy /api/ requests
    path = request.url.path
    if not path.startswith("/api/"):
        return None
    if is_local_path(path):
        return None

    node_mgr = getattr(request.app.state, "node_state_manager", None)
    if node_mgr is None:
        return None

    if node_mgr.get_role() != "standby":
        return None

    return node_mgr.get_active_peer_url()


async def proxy_http_request(request: Request, active_peer_url: str) -> Response:
    """Forward an HTTP request to the active peer node."""
    path_and_query = request.url.path
    if request.url.query:
        path_and_query += f"?{request.url.query}"
    target_url = f"{active_peer_url.rstrip('/')}{path_and_query}"

    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in STRIP_HEADERS
    }

    try:
        body = await request.body()
    except Exception:
        body = b""

    timeout = httpx.Timeout(60.0, connect=5.0)

    try:
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)
        # Check if client requested event stream / SSE
        is_sse = "text/event-stream" in request.headers.get("accept", "")

        if is_sse:
            req = client.build_request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
            )
            res = await client.send(req, stream=True)

            async def stream_generator():
                try:
                    async for chunk in res.aiter_raw():
                        yield chunk
                finally:
                    await res.aclose()
                    await client.aclose()

            res_headers = {
                k: v for k, v in res.headers.items() if k.lower() not in STRIP_HEADERS
            }
            return StreamingResponse(
                stream_generator(),
                status_code=res.status_code,
                headers=res_headers,
                media_type=res.headers.get("content-type", "text/event-stream"),
            )

        # Standard HTTP proxy call
        async with client:
            res = await client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
            )
            res_headers = {
                k: v for k, v in res.headers.items() if k.lower() not in STRIP_HEADERS
            }
            return Response(
                content=res.content,
                status_code=res.status_code,
                headers=res_headers,
                media_type=res.headers.get("content-type"),
            )
    except httpx.HTTPError as exc:
        logger.warning("Standby proxy to active peer %s failed: %s", target_url, exc)
        return JSONResponse(
            {
                "error": f"Active leader at {active_peer_url} is unreachable: {exc}",
                "peer_unreachable": True,
                "standby": True,
                "active_peer_url": active_peer_url,
            },
            status_code=503,
        )


class StandbyProxyMiddleware(BaseHTTPMiddleware):
    """Starlette middleware to proxy API calls to active peer when in standby mode."""

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        target_peer = get_proxy_target_url(request)
        if target_peer:
            return await proxy_http_request(request, target_peer)
        return await call_next(request)
