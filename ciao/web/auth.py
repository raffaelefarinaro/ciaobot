"""Token auth middleware + session cookie signing."""

from __future__ import annotations

import hmac
import logging
from pathlib import Path

from itsdangerous import URLSafeTimedSerializer, BadSignature
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.websockets import WebSocket

logger = logging.getLogger(__name__)

SESSION_COOKIE = "ciao_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
_LOOPBACK_ADDRESSES = {"127.0.0.1", "::1", "localhost"}

# Endpoints reachable with no session at all, from anywhere.
_PUBLIC_API = {
    "/api/auth",
    "/api/startup-status",
    "/api/active-chats",
    "/api/setup-status",
    "/api/setup/finish",
    "/api/setup/list-dirs",
    "/api/setup/mkdir",
}

# Endpoints usable without a session, but only from a process on this machine.
# `/api/menubar-chats` is how the tray reads chat titles (it holds no cookie),
# and `/api/node/handover` is the client-mode escape hatch offered on the login
# screen, which by definition runs before any session exists. Both are gated on
# the peer address rather than the Host header, which a caller controls.
_LOOPBACK_ONLY_API = {
    "/api/menubar-chats",
    "/api/node/handover",
}


def make_serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret)


def verify_session(request: Request | WebSocket, serializer: URLSafeTimedSerializer) -> bool:
    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return False
    try:
        serializer.loads(cookie, max_age=SESSION_MAX_AGE)
        return True
    except BadSignature:
        return False


def session_cookie_kwargs(request: Request) -> dict:
    # Host-only cookie: scoped to the exact host that served it.
    return dict(
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
        secure=request.url.scheme == "https",
    )


def _split_host(value: str) -> tuple[str, int | None]:
    host = value.strip().lower()
    if not host:
        return "", None
    if host.startswith("["):
        end = host.find("]")
        if end != -1:
            port = None
            if host[end + 1:].startswith(":"):
                try:
                    port = int(host[end + 2:])
                except ValueError:
                    port = None
            return host[1:end], port
    if ":" not in host:
        return host, None
    name, raw_port = host.rsplit(":", 1)
    try:
        return name, int(raw_port)
    except ValueError:
        return host, None


def _allowed_origin_hosts(request: Request | WebSocket) -> set[str]:
    """Hostnames — beyond the bound ``Host`` — an origin may legitimately match.

    Covers the reverse-proxy / tunnel / host-alias case where the browser
    reaches the app under a public hostname while the server binds to (and sees
    ``Host``) something else:

    - ``X-Forwarded-Host``: the original host a proxy declares. Browsers cannot
      set this on a WebSocket/fetch handshake, so it can't be forged by a
      cross-site page — only a fronting proxy sets it.
    - ``CIAO_ALLOWED_ORIGINS``: an explicit operator allowlist of hosts/origins.
    """
    from urllib.parse import urlsplit

    hosts: set[str] = set()
    forwarded = request.headers.get("x-forwarded-host", "")
    for part in forwarded.split(","):
        host, _port = _split_host(part.strip())
        if host:
            hosts.add(host.lower().rstrip("."))
    state = getattr(getattr(request, "app", None), "state", None)
    cfg = getattr(state, "config", None)
    for entry in getattr(cfg, "pwa_allowed_origins", ()) or ():
        host = urlsplit(entry).hostname if "//" in entry else _split_host(entry)[0]
        if host:
            hosts.add(host.lower().rstrip("."))
    return hosts


def _same_origin(request: Request | WebSocket, origin: str) -> bool:
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False

    request_host, request_port = _split_host(request.headers.get("host", ""))
    if not request_host:
        request_host = (request.url.hostname or "").lower()
        request_port = request.url.port

    origin_host = parsed.hostname.lower()
    origin_port = parsed.port
    if origin_host == request_host:
        if origin_port is not None and request_port is not None and origin_port != request_port:
            return False
        return True
    # Reached under a proxy-declared or operator-allowed host (port may differ
    # across the proxy hop, so it isn't compared here).
    return origin_host in _allowed_origin_hosts(request)


def _state_change_origin_allowed(request: Request) -> bool:
    if request.method.upper() in _SAFE_METHODS:
        return True
    origin = request.headers.get("origin")
    if origin:
        return _same_origin(request, origin)
    referer = request.headers.get("referer")
    if referer:
        return _same_origin(request, referer)
    return True


async def authorize_websocket(websocket: WebSocket) -> bool:
    """Handshake gate for `/ws/*`, mirroring the HTTP policy in AuthMiddleware.

    Cross-origin browser connections are always rejected (WebSockets are not
    covered by CORS, so an unchecked handshake allows cross-site hijacking);
    a session cookie is required only when auth is enabled, same as `/api/*`.
    Closes the socket and returns False when the connection is not allowed.
    """
    origin = websocket.headers.get("origin")
    if origin and not _same_origin(websocket, origin):
        logger.warning(
            "WebSocket origin rejected: origin=%s host=%s x-forwarded-host=%s "
            "(set CIAO_ALLOWED_ORIGINS if reaching Ciaobot under a proxy host)",
            origin,
            websocket.headers.get("host", ""),
            websocket.headers.get("x-forwarded-host", ""),
        )
        await websocket.close(code=4003, reason="forbidden origin")
        return False
    config = getattr(websocket.app.state, "config", None)
    if getattr(config, "pwa_auth_required", False) and not verify_session(
        websocket, websocket.app.state.serializer
    ):
        await websocket.close(code=4001, reason="unauthorized")
        return False
    return True


def is_loopback_client(request: Request) -> bool:
    """True when the TCP peer is on this machine.

    Reads the connection's source address, never the Host header — a remote
    caller can set `Host: localhost` freely. This is the only "is it local"
    check in the codebase; anything that grants access must use it.
    """
    client = request.client
    if client is None or not client.host:
        return False
    return client.host in _LOOPBACK_ADDRESSES


def _setup_token_path(request: Request) -> Path | None:
    config = getattr(request.app.state, "config", None)
    workspace_root = getattr(config, "workspace_root", None)
    if workspace_root is None:
        return None
    return Path(workspace_root).expanduser() / ".runtime" / "setup-token"


def _redeem_setup_token(request: Request, token: str):
    if request.method.upper() not in {"GET", "HEAD"}:
        return JSONResponse({"error": "method not allowed"}, status_code=405)
    if not is_loopback_client(request):
        return JSONResponse({"error": "setup token is localhost-only"}, status_code=403)
    token_path = _setup_token_path(request)
    if token_path is None or not token_path.exists():
        return JSONResponse({"error": "invalid setup token"}, status_code=401)
    expected = token_path.read_text(encoding="utf-8").strip()
    if not expected or not hmac.compare_digest(token, expected):
        return JSONResponse({"error": "invalid setup token"}, status_code=401)

    signed = request.app.state.serializer.dumps({"user": "owner"})
    response = RedirectResponse("/", status_code=302)
    response.set_cookie(SESSION_COOKIE, signed, **session_cookie_kwargs(request))
    token_path.unlink(missing_ok=True)
    return response


class AuthMiddleware(BaseHTTPMiddleware):
    """Reject unauthenticated requests.

    Only `/api/*` (except bootstrap/status endpoints) and `/ws/*` are
    protected; everything else falls through to the SPA shell so the frontend
    can handle login.
    """

    def __init__(
        self,
        app,
        *,
        serializer: URLSafeTimedSerializer,
        auth_required: bool = False,
    ) -> None:
        super().__init__(app)
        self._serializer = serializer
        self._auth_required = auth_required

    def _auth_required_now(self, request: Request) -> bool:
        config = getattr(request.app.state, "config", None)
        if config is not None and hasattr(config, "pwa_auth_required"):
            return bool(config.pwa_auth_required)
        return self._auth_required

    def _serializer_now(self, request: Request) -> URLSafeTimedSerializer:
        serializer = getattr(request.app.state, "serializer", None)
        return serializer if serializer is not None else self._serializer

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        setup_token = request.query_params.get("setup")
        if path == "/" and setup_token:
            return _redeem_setup_token(request, setup_token)

        protected = (
            (
                path.startswith("/api/")
                and path not in _PUBLIC_API
                and not path.startswith("/api/open-chat/")
            )
            or path.startswith("/ws/")
        )
        if not protected:
            return await call_next(request)
        if path in _LOOPBACK_ONLY_API and is_loopback_client(request):
            if not _state_change_origin_allowed(request):
                return JSONResponse({"error": "forbidden origin"}, status_code=403)
            return await call_next(request)
        if self._auth_required_now(request) and not verify_session(
            request, self._serializer_now(request)
        ):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if path.startswith("/api/") and not _state_change_origin_allowed(request):
            return JSONResponse({"error": "forbidden origin"}, status_code=403)
        return await call_next(request)
