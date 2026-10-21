"""Token auth middleware + session cookie signing."""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import secrets
from pathlib import Path

from itsdangerous import URLSafeTimedSerializer, BadSignature
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.websockets import WebSocket

from ciao.os_support.private import open_private


logger = logging.getLogger(__name__)

SESSION_COOKIE = "ciao_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
# Shortest PWA password the first-run wizard and Settings accept.
MIN_PWA_PASSWORD_LENGTH = 4
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
# Headers a reverse proxy adds on the way in. A proxy on this machine
# (`tailscale serve`, Caddy, cloudflared, nginx) connects from loopback, so its
# callers would otherwise look local. uvicorn's own proxy-header handling only
# rewrites the peer from `X-Forwarded-For` when the proxy dials 127.0.0.1 (not
# ::1) and keeps a loopback value a proxy passed through verbatim, so it is no
# substitute. Tailscale Serve always sends `Tailscale-Headers-Info` and appends
# to any `X-Forwarded-For` the caller sent, so a caller behind it cannot strip
# these.
_PROXY_HEADERS = frozenset(
    {"forwarded", "via", "x-real-ip", "cf-connecting-ip", "true-client-ip", "x-client-ip"}
)
_PROXY_HEADER_PREFIXES = ("x-forwarded-", "tailscale-")

# Endpoints reachable with no session at all, from anywhere.
_PUBLIC_API = {
    "/api/auth",
    "/api/auth/check",
    "/api/startup-status",
    "/api/setup-status",
    "/api/setup/finish",
    "/api/setup/list-dirs",
    "/api/setup/inspect-folder",
    "/api/setup/mkdir",
}

# Endpoints usable without a session, but only from a process on this machine.
# `/api/menubar-chats` is how the tray reads chat titles (it holds no cookie).
# `/api/admin/drain` and `/api/admin/drain/cancel` are the update coordinator's own drain handshake,
# driven by `ciao update apply` before it bootstraps the detached updater job.
# `/api/admin/restart-blockers` is the `ciao service` restart/stop gate (reason-tagged).
# `/api/auth/host-sign-in` mints a one-time setup token and opens it in the host's own browser; it returns no credential.
# All are gated on the peer address plus a loopback `Host` and, when present, a same-origin `Origin`/`Referer`: the peer makes a caller local, and the Host check refuses a DNS-rebound page, whose Host is the attacker's name. A request that fails takes the normal session path.
_LOOPBACK_ONLY_API = {
    "/api/menubar-chats",
    "/api/admin/drain",
    "/api/admin/drain/cancel",
    "/api/admin/restart-blockers",
    "/api/auth/host-sign-in",
}

# The names local callers use for the engine (`http://localhost:<port>`).
LOOPBACK_HOST_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})


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

    Covers the reverse-proxy / tunnel case where the browser reaches the app
    under a public hostname while the server binds to (and sees ``Host``)
    something else. ``X-Forwarded-Host`` is the original host a proxy declares.
    Browsers cannot set it on a WebSocket/fetch handshake, so it can't be forged by a
    cross-site page — only a fronting proxy sets it.
    """
    hosts: set[str] = set()
    forwarded = request.headers.get("x-forwarded-host", "")
    for part in forwarded.split(","):
        host, _port = _split_host(part.strip())
        if host:
            hosts.add(host.lower().rstrip("."))
    return hosts


def _effective_port(scheme: str, port: int | None) -> int | None:
    if port is not None:
        return port
    return {"http": 80, "https": 443}.get(scheme.lower())


def _normalise_scheme(scheme: str) -> str:
    """The scheme an origin is compared by.

    ``ws``/``wss`` are the WebSocket spellings of ``http``/``https``: a
    browser reaches the engine over ``wss`` while its own origin is
    ``https``, so the two have to count as the same scheme.
    """
    scheme = scheme.lower()
    return {"ws": "http", "wss": "https"}.get(scheme, scheme)


def _same_origin(request: Request | WebSocket, origin: str) -> bool:
    """True when ``origin`` is the request's own origin.

    A direct request has to present this engine's own origin: the same host,
    the same scheme and the same effective port. A request that carries a
    proxy-declared ``X-Forwarded-Host`` naming the origin's host matches on
    the host alone, because a TLS-terminating proxy legitimately presents
    http to the engine while the browser's origin is https.
    """
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return False

    request_host, request_port = _split_host(request.headers.get("host", ""))
    if not request_host:
        request_host = (request.url.hostname or "").lower()
        request_port = request.url.port

    origin_host = parsed.hostname.lower()
    try:
        origin_port = parsed.port
    except ValueError:
        return False
    # A proxy-declared host comes first. A browser cannot set
    # `X-Forwarded-Host`, so only a fronting proxy does, and a proxy that
    # forwards a client-supplied one is the configuration this boundary
    # excludes (see `docs/REMOTE_BOUNDARY.md`). The host alone carries the
    # match: a TLS-terminating proxy legitimately presents http to the engine
    # while the browser's origin is https, and the port may differ across the
    # proxy hop too.
    if origin_host in _allowed_origin_hosts(request):
        return True
    # A direct request: the origin has to be this engine's own origin, in
    # scheme, host and effective port.
    if origin_host == request_host:
        request_scheme = _normalise_scheme(request.url.scheme)
        if request_scheme != parsed.scheme:
            return False
        return _effective_port(parsed.scheme, origin_port) == _effective_port(
            request_scheme, request_port
        )
    return False


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


def _loopback_host_header(request: Request) -> bool:
    """True when the Host header names this machine; a rebound page carries its own name."""
    host, _port = _split_host(request.headers.get("host", ""))
    if not host:
        host = (request.url.hostname or "").lower()
    return host.rstrip(".") in LOOPBACK_HOST_NAMES


def _same_origin_source(request: Request) -> bool:
    """Origin (else Referer), when sent, must be this exact origin; checked for every method.

    A same-host origin has to match the request's scheme as well as its host
    and effective port.
    """
    source = request.headers.get("origin") or request.headers.get("referer")
    return not source or _same_origin(request, source)


async def authorize_websocket(websocket: WebSocket) -> bool:
    """Handshake gate for `/ws/*`, mirroring the HTTP policy in AuthMiddleware.

    Cross-origin browser connections are always rejected (WebSockets are not
    covered by CORS, so an unchecked handshake allows cross-site hijacking);
    a session cookie is always required, same as `/api/*`.
    Closes the socket and returns False when the connection is not allowed.
    """
    origin = websocket.headers.get("origin")
    if origin and not _same_origin(websocket, origin):
        logger.warning(
            "WebSocket origin rejected: origin=%s host=%s x-forwarded-host=%s "
            "(a reverse proxy must forward X-Forwarded-Host)",
            origin,
            websocket.headers.get("host", ""),
            websocket.headers.get("x-forwarded-host", ""),
        )
        await websocket.close(code=4003, reason="forbidden origin")
        return False
    if not verify_session(websocket, websocket.app.state.serializer):
        await websocket.close(code=4001, reason="unauthorized")
        return False
    return True


def is_loopback_client(request: Request | WebSocket) -> bool:
    """True when the caller is on this machine, not behind a local proxy.

    Reads the connection's source address, never the Host header — a remote
    caller can set `Host: localhost` freely. A loopback peer that carries
    reverse-proxy headers is a proxy relaying someone else, so it is not
    local. This is the only "is it local" check in the codebase; anything that
    grants access must use it.
    """
    client = request.client
    if client is None or not _is_loopback_address(client.host):
        return False
    return not any(
        name in _PROXY_HEADERS or name.startswith(_PROXY_HEADER_PREFIXES)
        for name in request.headers.keys()
    )


def _is_loopback_address(host: str | None) -> bool:
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped.is_loopback
    return address.is_loopback


def _setup_token_path(request: Request) -> Path | None:
    config = getattr(request.app.state, "config", None)
    workspace_root = getattr(config, "workspace_root", None)
    if workspace_root is None:
        return None
    return Path(workspace_root).expanduser() / ".runtime" / "setup-token"


def mint_setup_token(request: Request) -> tuple[str, Path] | None:
    """Write a fresh one-time ``.runtime/setup-token`` (owner-only) and return it with its path; None without a workspace.

    The file is recreated on every mint so an older, looser-mode file is never reused.
    """
    token_path = _setup_token_path(request)
    if token_path is None:
        return None
    token = secrets.token_urlsafe(24)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.unlink(missing_ok=True)
    fd = open_private(token_path, os.O_WRONLY | os.O_EXCL, follow_symlinks=False)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(token + "\n")
    return token, token_path


def _redeem_setup_token(request: Request, token: str):
    if request.method.upper() not in {"GET", "HEAD"}:
        return JSONResponse({"error": "method not allowed"}, status_code=405)
    if not is_loopback_client(request):
        return JSONResponse({"error": "setup token is localhost-only"}, status_code=403)
    token_path = _setup_token_path(request)
    if token_path is None or not token_path.exists():
        return JSONResponse({"error": "invalid setup token"}, status_code=401)
    expected = token_path.read_text(encoding="utf-8").strip()
    # Compare bytes: compare_digest raises TypeError on a non-ASCII str.
    if not expected or not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
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
    ) -> None:
        super().__init__(app)
        self._serializer = serializer

    def _serializer_now(self, request: Request) -> URLSafeTimedSerializer:
        serializer = getattr(request.app.state, "serializer", None)
        return serializer if serializer is not None else self._serializer

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        setup_token = request.query_params.get("setup")
        if path == "/" and setup_token:
            return _redeem_setup_token(request, setup_token)

        protected = (
            path.startswith("/api/") and path not in _PUBLIC_API
        ) or path.startswith("/ws/")
        if not protected:
            return await call_next(request)
        if (
            path in _LOOPBACK_ONLY_API
            and is_loopback_client(request)
            and _loopback_host_header(request)
        ):
            if not _same_origin_source(request):
                return JSONResponse({"error": "forbidden origin"}, status_code=403)
            return await call_next(request)
        if not verify_session(request, self._serializer_now(request)):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if path.startswith("/api/") and not _state_change_origin_allowed(request):
            return JSONResponse({"error": "forbidden origin"}, status_code=403)
        return await call_next(request)
