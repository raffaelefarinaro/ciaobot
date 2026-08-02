"""Client-mode proxy middleware for Ciaobot host/client deployment.

When a node is in client mode and a host URL is registered, this middleware
tunnels requests to the remote host so the local PWA and macOS tray act as a
thin client.

The client is meant to be a *mirror*: opening it should look exactly like
opening the host's own URL in a browser. So both the API calls and the UI
bundle are tunneled, and only the handful of surfaces that are genuinely about
this machine stay local:

- ``/api/node/*``, ``/api/device/*`` — role, host connection, local install
- ``/api/auth`` login/logout/check — they mint and clear the *local* session
- ``/api/startup-status``, ``/api/setup-status`` — the local engine's own boot
- ``/device`` — the local escape-hatch UI, which must work with the host down

Everything else, including ``/api/auth/settings`` (the password you actually
log in with) and the static bundle, comes from the host.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse

if TYPE_CHECKING:
    from collections.abc import Callable

    from ciao.node_state import NodeStateManager

logger = logging.getLogger(__name__)

# Endpoints that MUST always be handled locally on the client node
EXCLUDED_LOCAL_PATHS: set[str] = {
    "/api/node/status",
    "/api/node/handover",
    "/api/node/demote",
    "/api/node/peers",
    "/api/node/connect",
    "/api/startup-status",
    "/api/setup-status",
    "/api/auth",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/check",
}

# Prefixes handled locally: this machine's role, connection, and own install.
LOCAL_API_PREFIXES: tuple[str, ...] = (
    "/api/node",
    "/api/device",
    "/api/desktop-drop",
)

# Paths that live under a local prefix but must still be mirrored. The PWA
# password managed in Settings is the host's — that is the one the client logs
# in with — while /api/auth login/logout/check stay local because they mint and
# clear this node's own session.
MIRRORED_API_PATHS: set[str] = {
    "/api/auth/settings",
}

# UI routes served from the local bundle even in client mode. The device panel
# is the way out of client mode, so it cannot depend on the host being up.
LOCAL_UI_PREFIXES: tuple[str, ...] = ("/device",)

# Header keys to strip before proxying to prevent host mismatch or double compression
STRIP_HEADERS: set[str] = {
    "host",
    "content-length",
    "connection",
    "transfer-encoding",
}

# The host's session cookie is held server-side in node_state, never handed to
# the browser (whose cookie is this node's own local session).
RESPONSE_STRIP_HEADERS: set[str] = {"set-cookie"}

_NO_CACHE = "no-cache, no-store, must-revalidate"


from starlette.websockets import WebSocket, WebSocketDisconnect


def _static_dir() -> Path:
    """Local Vite build directory (same one ``ciao.web.app`` serves from)."""
    from ciao.web.app import STATIC_DIR

    return STATIC_DIR


def is_local_path(path: str) -> bool:
    """True when an API/WS path must be answered by this node, not the host."""
    cleaned = path.rstrip("/") or path
    if cleaned in MIRRORED_API_PATHS:
        return False
    if cleaned in EXCLUDED_LOCAL_PATHS:
        return True
    return any(
        cleaned == prefix or cleaned.startswith(f"{prefix}/")
        for prefix in LOCAL_API_PREFIXES
    )


def is_local_ui_path(path: str) -> bool:
    """True for UI routes the client renders from its own bundle."""
    cleaned = path.rstrip("/") or path
    return any(
        cleaned == prefix or cleaned.startswith(f"{prefix}/")
        for prefix in LOCAL_UI_PREFIXES
    )


def local_static_file(path: str) -> Path | None:
    """Local build file for ``path``, or None when the host should serve it.

    Hashed ``/assets`` names are content-derived, so a name the local build also
    has is byte-identical to the host's — serving it locally saves a hop without
    breaking the mirror. Names from a different host build are unknown here and
    fall through to the proxy.

    The hash is the whole argument, so only ``/assets`` qualifies. Every other
    static name is stable across builds (``index.html``, ``sw.js``,
    ``manifest.json``, the icons), meaning a version-skewed client would serve
    its own older copy against the host's bundle and quietly stop being a
    mirror. ``index.html`` in particular is what pins which asset hashes the
    browser asks for.
    """
    rel = path.lstrip("/")
    if not rel or not (rel == "assets" or rel.startswith("assets/")):
        return None
    root = _static_dir()
    try:
        resolved = (root / rel).resolve()
        resolved.relative_to(root.resolve())
    except (ValueError, OSError):
        return None
    return resolved if resolved.is_file() else None


def _client_host_url(request: Request | WebSocket) -> str | None:
    """Host URL when this node is a client that should tunnel, else None."""
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


def get_proxy_target_url(request: Request | WebSocket) -> str | None:
    """Return host URL when this node should tunnel the request as a client."""
    # Proxy /api/ and /ws/ requests
    path = request.url.path
    if not (path.startswith("/api/") or path.startswith("/ws/")):
        return None
    if is_local_path(path):
        return None
    return _client_host_url(request)


def get_static_proxy_target(request: Request) -> str | None:
    """Return host URL when the UI bundle for this request should be the host's.

    Serving the host's ``index.html`` and assets is what makes client mode a
    real mirror: without it the client drives a possibly newer host API with its
    own older bundle, and nothing says so.
    """
    path = request.url.path
    if path.startswith("/api/") or path.startswith("/ws/") or path.startswith("/mcp"):
        return None
    if request.method.upper() not in {"GET", "HEAD"}:
        return None
    if is_local_ui_path(path):
        return None
    # Role check before the filesystem probe: on a host this returns None after
    # one state read, so static serving never pays for a stat it cannot use.
    target = _client_host_url(request)
    if target is None:
        return None
    if local_static_file(path) is not None:
        return None
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


def _capture_host_session(request: Request | WebSocket, res: httpx.Response) -> None:
    """Store a session cookie the host just issued as the new tunnel session.

    Without this, changing the host's PWA password from a client would rotate the
    host's signing secret and leave this node holding a session the host no
    longer accepts — the tunnel would die on the next request.
    """
    from ciao.web.routes_api import _parse_set_cookie_session

    # Read the raw headers rather than res.cookies: the latter needs the paired
    # request object, which a streamed or synthesized response may not carry.
    try:
        raw = res.headers.get_list("set-cookie")
    except Exception:
        single = res.headers.get("set-cookie")
        raw = [single] if single else []
    issued = _parse_set_cookie_session(raw)
    if not issued:
        return
    node_mgr = getattr(request.app.state, "node_state_manager", None)
    if node_mgr is None:
        return
    try:
        node_mgr.set_host_session(issued)
    except Exception as exc:  # state file trouble must not fail the response
        logger.warning("Could not store refreshed host session: %s", exc)


def _forwarded_response_headers(res: httpx.Response, *, decoded_body: bool) -> dict[str, str]:
    """Response headers to hand back to the browser.

    ``decoded_body`` marks the non-streaming path, where httpx already
    decompressed the body — forwarding the host's ``content-encoding`` there
    would tell the browser to inflate plain bytes. The streaming path forwards
    raw chunks, so its encoding header stays accurate.
    """
    drop = STRIP_HEADERS | RESPONSE_STRIP_HEADERS
    if decoded_body:
        drop = drop | {"content-encoding"}
    return {k: v for k, v in res.headers.items() if k.lower() not in drop}


async def proxy_http_request(
    request: Request,
    active_peer_url: str,
    *,
    on_unreachable: "Callable[[], Response | None] | None" = None,
) -> Response:
    """Forward an HTTP request to the host node.

    ``on_unreachable`` gets a chance to answer when the host cannot be reached;
    returning None falls back to the standard 503 payload.
    """
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

            _capture_host_session(request, res)
            return StreamingResponse(
                stream_generator(),
                status_code=res.status_code,
                headers=_forwarded_response_headers(res, decoded_body=False),
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
            _capture_host_session(request, res)
            return Response(
                content=res.content,
                status_code=res.status_code,
                headers=_forwarded_response_headers(res, decoded_body=True),
                media_type=res.headers.get("content-type"),
            )
    except httpx.HTTPError as exc:
        logger.warning("Client proxy to host %s failed: %s", target_url, exc)
        if on_unreachable is not None:
            fallback = on_unreachable()
            if fallback is not None:
                return fallback
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


def local_shell_response(request: Request) -> Response | None:
    """Local ``index.html`` for a navigation, so a dead host is not a blank app.

    Only navigations get the shell: answering an asset or icon request with HTML
    would turn a 503 into a confusing parse error. The shell boots the local
    bundle, which can still reach ``/device`` to disconnect.
    """
    if "text/html" not in request.headers.get("accept", ""):
        return None
    index = _static_dir() / "index.html"
    if not index.is_file():
        return None
    return FileResponse(index, headers={"Cache-Control": _NO_CACHE})


async def proxy_static_request(request: Request, active_peer_url: str) -> Response:
    """Serve the host's UI bundle, falling back to the local shell when it is down."""
    return await proxy_http_request(
        request,
        active_peer_url,
        on_unreachable=lambda: local_shell_response(request),
    )


class StandbyProxyMiddleware(BaseHTTPMiddleware):
    """Tunnel API, WS, and UI-bundle requests to the host in client mode."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        target_peer = get_proxy_target_url(request)
        if target_peer:
            return await proxy_http_request(request, target_peer)
        static_peer = get_static_proxy_target(request)
        if static_peer:
            return await proxy_static_request(request, static_peer)
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
                except WebSocketDisconnect:
                    # The local browser went away. Stop the paired host
                    # forwarder without reporting a host failure to a client
                    # that is no longer connected.
                    return

            async def forward_remote_to_client():
                try:
                    async for msg in remote_ws:
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                except WebSocketDisconnect:
                    # The local browser disconnected while the host was still
                    # healthy. As above, this is a normal teardown.
                    return
                raise OSError("host WebSocket closed")

            task1 = asyncio.create_task(forward_client_to_remote())
            task2 = asyncio.create_task(forward_remote_to_client())

            done, pending = await asyncio.wait(
                [task1, task2],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            # Do not swallow a one-way forwarding failure. If the browser can
            # still receive host keepalives but its sends no longer reach the
            # host, leaving this socket open makes the composer paint phantom
            # optimistic messages with no visible connection error.
            for t in done:
                t.result()
    except Exception as exc:
        logger.warning("Client WebSocket proxy to host %s failed: %s", target_ws_url, exc)
        try:
            await websocket.send_json(
                {"type": "host_unreachable"}
            )
            await websocket.close(code=4004)
        except Exception:
            pass
