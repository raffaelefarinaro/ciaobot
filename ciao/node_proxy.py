"""Client-mode proxy middleware for Ciaobot host/client deployment.

When a node is in client mode and a host URL is registered, this middleware
intercepts API/WS calls and tunnels them to the remote host so the local PWA
and macOS tray act as a thin client.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

if TYPE_CHECKING:
    from ciao.node_state import NodeStateManager

logger = logging.getLogger(__name__)

# Endpoints that MUST always be handled locally on the standby node
EXCLUDED_LOCAL_PATHS: set[str] = {
    "/api/node/status",
    "/api/node/handover",
    "/api/node/demote",
    "/api/node/peers",
    "/api/node/connect",
    "/api/startup-status",
    "/api/setup-status",
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


from starlette.websockets import WebSocket


def is_local_path(path: str) -> bool:
    cleaned = path.rstrip("/")
    if cleaned in EXCLUDED_LOCAL_PATHS:
        return True
    if cleaned.startswith("/api/desktop-drop"):
        return True
    if cleaned.startswith("/api/auth"):
        return True
    if cleaned.startswith("/api/node"):
        return True
    return False


def get_proxy_target_url(request: Request | WebSocket) -> str | None:
    """Return host URL when this node should tunnel the request as a client."""
    # Proxy /api/ and /ws/ requests
    path = request.url.path
    if not (path.startswith("/api/") or path.startswith("/ws/")):
        return None
    if is_local_path(path):
        return None

    node_mgr: NodeStateManager | None = getattr(request.app.state, "node_state_manager", None)
    if node_mgr is None:
        return None

    # Accept legacy "standby" during rolling upgrades before state migration.
    if node_mgr.get_role() not in {"client", "standby"}:
        return None

    target = node_mgr.get_active_peer_url()
    if not target:
        return None

    # Self-proxy check: avoid proxying to ourselves
    req_host = request.url.hostname or ""
    req_port = request.url.port or 8443

    try:
        from urllib.parse import urlparse
        parsed = urlparse(target)
        target_host = parsed.hostname or ""
        target_port = parsed.port or (443 if parsed.scheme == "https" else 80)

        if (target_host in {"localhost", "127.0.0.1"} and req_host in {"localhost", "127.0.0.1"}) and target_port == req_port:
            return None
        if target_host == req_host and target_port == req_port:
            return None
    except Exception:
        pass

    return target


def _host_auth_headers(request: Request | WebSocket, target_url: str) -> dict[str, str]:
    """Cookie/Authorization to present to the host (not the local client session)."""
    node_mgr = getattr(request.app.state, "node_state_manager", None)
    session = node_mgr.get_host_session() if node_mgr is not None else None
    headers: dict[str, str] = {}
    if session:
        from ciao.web.auth import SESSION_COOKIE

        headers["cookie"] = f"{SESSION_COOKIE}={session}"
    return headers


async def proxy_http_request(request: Request, active_peer_url: str) -> Response:
    """Forward an HTTP request to the host node."""
    path_and_query = request.url.path
    if request.url.query:
        path_and_query += f"?{request.url.query}"
    target_url = f"{active_peer_url.rstrip('/')}{path_and_query}"

    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in STRIP_HEADERS
    }
    # Replace local session cookie with the stored host session when present.
    host_auth = _host_auth_headers(request, active_peer_url)
    if host_auth.get("cookie"):
        headers["cookie"] = host_auth["cookie"]
    elif "cookie" in {k.lower() for k in headers}:
        # Drop local-only session so we don't send a useless/wrong cookie.
        headers = {k: v for k, v in headers.items() if k.lower() != "cookie"}

    clean_peer_url = active_peer_url.rstrip("/")
    if "origin" in headers or request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        headers["origin"] = clean_peer_url
    if "referer" in headers:
        headers["referer"] = f"{clean_peer_url}/"

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
        logger.warning("Client proxy to host %s failed: %s", target_url, exc)
        return JSONResponse(
            {
                "error": f"Host at {active_peer_url} is unreachable: {exc}",
                "peer_unreachable": True,
                "client": True,
                "standby": True,
                "host_url": active_peer_url,
                "active_peer_url": active_peer_url,
            },
            status_code=503,
        )


class StandbyProxyMiddleware(BaseHTTPMiddleware):
    """Tunnel /api requests to the host when this node is in client mode."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        target_peer = get_proxy_target_url(request)
        if target_peer:
            return await proxy_http_request(request, target_peer)
        return await call_next(request)


async def proxy_websocket(websocket: WebSocket, active_peer_url: str) -> None:
    """Proxy a WebSocket connection to the host node."""
    import asyncio
    import websockets

    clean_url = active_peer_url.rstrip("/")
    if clean_url.startswith("https://"):
        target_ws_base = "wss://" + clean_url[8:]
    elif clean_url.startswith("http://"):
        target_ws_base = "ws://" + clean_url[7:]
    else:
        target_ws_base = "ws://" + clean_url

    path_and_query = websocket.url.path
    if websocket.url.query:
        path_and_query += f"?{websocket.url.query}"
    target_ws_url = f"{target_ws_base}{path_and_query}"

    extra_headers = _host_auth_headers(websocket, active_peer_url)

    await websocket.accept()

    try:
        async with websockets.connect(
            target_ws_url,
            additional_headers=extra_headers or None,
        ) as remote_ws:
            async def forward_client_to_remote():
                try:
                    while True:
                        msg = await websocket.receive_text()
                        await remote_ws.send(msg)
                except Exception:
                    pass

            async def forward_remote_to_client():
                try:
                    async for msg in remote_ws:
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                except Exception:
                    pass

            task1 = asyncio.create_task(forward_client_to_remote())
            task2 = asyncio.create_task(forward_remote_to_client())

            done, pending = await asyncio.wait(
                [task1, task2],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
    except Exception as exc:
        logger.warning("Client WebSocket proxy to host %s failed: %s", target_ws_url, exc)
        try:
            await websocket.send_json(
                {"type": "host_unreachable"}
            )
            await websocket.close(code=4004)
        except Exception:
            pass
