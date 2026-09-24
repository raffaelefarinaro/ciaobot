"""Origin and capability boundary for a client-mode node.

Client content is rendered through the node's content origin. Machine controls
are only accepted from the separate loopback control origin.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.websockets import WebSocket

LOCAL_CONTROL_HOST = "127.0.0.1"
CONTENT_HOST = "localhost"
LOCAL_CONTROL_HEADER = "X-Ciao-Local-Control"

_LOCAL_CONTROL_API_PREFIXES = (
    "/api/node",
    "/api/device",
    "/api/desktop-drop",
    "/api/native/sessions",
)
_LOCAL_CONTROL_UI_PREFIXES = ("/device",)
_LOCAL_STATIC_PATHS = {
    "/assets",
    "/favicon.ico",
    "/index.html",
    "/manifest.json",
    "/sw.js",
}
_LOCAL_STATIC_PREFIXES = ("/assets",)
_LOCAL_CLIENT_READ_PATHS = {
    "/api/auth",
    "/api/auth/bridge",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/check",
    "/api/startup-status",
    "/api/setup-status",
}


def _http_scheme(value: object) -> str:
    scheme = str(value or "http").lower()
    return {"ws": "http", "wss": "https"}.get(scheme, scheme)


def _clean_path(path: str) -> str:
    return path.rstrip("/") or path


def _matches_prefix(path: str, prefixes: tuple[str, ...]) -> bool:
    cleaned = _clean_path(path)
    return any(
        cleaned == prefix or cleaned.startswith(f"{prefix}/")
        for prefix in prefixes
    )


def is_local_control_api_path(path: str) -> bool:
    return _matches_prefix(path, _LOCAL_CONTROL_API_PREFIXES)


def is_local_control_ui_path(path: str) -> bool:
    return _matches_prefix(path, _LOCAL_CONTROL_UI_PREFIXES)


def is_local_static_path(path: str) -> bool:
    cleaned = _clean_path(path)
    return cleaned in _LOCAL_STATIC_PATHS or _matches_prefix(
        cleaned, _LOCAL_STATIC_PREFIXES
    )


def is_invalid_node_state(request: Request | WebSocket) -> bool:
    manager = getattr(request.app.state, "node_state_manager", None)
    if manager is None:
        return False
    validity = getattr(manager, "is_valid", None)
    if not callable(validity):
        return True
    try:
        return not bool(validity())
    except Exception:
        return True


def is_client_mode(request: Request | WebSocket) -> bool:
    manager = getattr(request.app.state, "node_state_manager", None)
    if manager is None:
        return False
    checker = getattr(manager, "is_client", None)
    if not callable(checker):
        return True
    try:
        if bool(checker()):
            return True
        return is_invalid_node_state(request)
    except Exception:
        return True


def _configured_port(request: Request | WebSocket) -> int | None:
    config = getattr(request.app.state, "config", None)
    raw = getattr(config, "pwa_port", None)
    if raw is None:
        port = request.url.port
        return int(port) if port is not None else None
    try:
        return int(raw)
    except (TypeError, ValueError):
        port = request.url.port
        return int(port) if port is not None else None


def _default_port(scheme: str) -> int | None:
    return {"http": 80, "https": 443}.get(scheme.lower())


def _effective_port(scheme: str, port: int | None) -> int | None:
    return port if port is not None else _default_port(scheme)


def _ports_match(scheme: str, actual: int | None, expected: int | None) -> bool:
    return _effective_port(scheme, actual) == _effective_port(scheme, expected)


def _host_parts(request: Request | WebSocket) -> tuple[str | None, int | None]:
    raw = request.headers.get("host", "").strip()
    if not raw:
        raw = request.url.netloc
    if "://" in raw:
        try:
            raw = urlsplit(raw).netloc
        except ValueError:
            return None, None
    try:
        parsed = urlsplit(f"//{raw}")
        return parsed.hostname, parsed.port
    except ValueError:
        return None, None


def _is_loopback_peer(request: Request) -> bool:
    client = request.client
    return bool(
        client
        and client.host in {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}
    )


def is_control_origin(request: Request | WebSocket) -> bool:
    hostname, port = _host_parts(request)
    if hostname != LOCAL_CONTROL_HOST:
        return False
    expected = _configured_port(request)
    return _ports_match(_http_scheme(request.url.scheme), port, expected)


def is_content_origin(request: Request) -> bool:
    hostname, port = _host_parts(request)
    if hostname != CONTENT_HOST:
        return False
    expected = _configured_port(request)
    return _ports_match(str(request.url.scheme or "http"), port, expected)


def local_control_origin_allowed(
    request: Request, *, require_header: bool = True
) -> bool:
    if not is_client_mode(request):
        return True
    if not is_control_origin(request) or not _is_loopback_peer(request):
        return False
    if require_header and request.headers.get(LOCAL_CONTROL_HEADER.lower()) != "1":
        return False
    origin = request.headers.get("origin", "").strip()
    if not origin:
        return True
    expected = _configured_port(request)
    try:
        parsed_origin = urlsplit(origin)
        origin_scheme = parsed_origin.scheme
        hostname = parsed_origin.hostname
        port = parsed_origin.port
    except ValueError:
        return False
    if (
        parsed_origin.username
        or parsed_origin.password
        or parsed_origin.path not in {"", "/"}
        or parsed_origin.query
        or parsed_origin.fragment
    ):
        return False
    return (
        hostname == LOCAL_CONTROL_HOST
        and origin_scheme == _http_scheme(request.url.scheme)
        and _ports_match(origin_scheme, port, expected)
    )


def _switch_origin(request: Request, host: str, path: str | None = None) -> str:
    scheme = str(request.url.scheme or "http")
    port = _configured_port(request)
    netloc = f"{host}:{port}" if port is not None else host
    target_path = str(request.url.path) if path is None else str(path)
    query = str(request.url.query) if path is None else ""
    return str(urlunsplit((scheme, netloc, target_path, query, "")))


def local_control_url(request: Request, path: str | None = None) -> str:
    return _switch_origin(request, LOCAL_CONTROL_HOST, path)


def content_url(request: Request, path: str | None = None) -> str:
    return _switch_origin(request, CONTENT_HOST, path)


def invalid_state_api_guard(request: Request) -> Response | None:
    if not is_invalid_node_state(request):
        return None
    path = request.url.path
    if path in {
        "/api/node/status",
        "/api/node/connect",
        "/api/node/handover",
        "/api/auth",
        "/api/auth/check",
        "/api/auth/bridge",
        "/api/auth/logout",
        "/api/startup-status",
        "/device",
    } or path == "/api/device" or path.startswith("/api/device/"):
        return None
    if path.startswith(("/api/", "/ws/")):
        return JSONResponse(
            {"error": "node state is invalid", "client": True},
            status_code=503,
        )
    return None


def local_control_api_guard(request: Request) -> Response | None:
    if not is_local_control_api_path(request.url.path):
        return None
    if local_control_origin_allowed(request):
        return None
    return JSONResponse(
        {"error": "local controls are only available on the device origin"},
        status_code=403,
    )


def local_control_ui_guard(request: Request) -> Response | None:
    if not is_local_control_ui_path(request.url.path):
        return None
    if local_control_origin_allowed(request, require_header=False):
        return None
    if not _is_loopback_peer(request):
        return JSONResponse(
            {"error": "local controls are only available on this device"},
            status_code=403,
        )
    if request.method.upper() in {"GET", "HEAD"}:
        return RedirectResponse(
            local_control_url(request),
            status_code=307,
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(
        {"error": "local controls are only available on the device origin"},
        status_code=403,
    )


def content_origin_guard(request: Request) -> Response | None:
    if (
        not is_client_mode(request)
        or not is_control_origin(request)
        or not _is_loopback_peer(request)
    ):
        return None
    path = request.url.path
    if (
        is_local_control_api_path(path)
        or is_local_control_ui_path(path)
        or is_local_static_path(path)
    ):
        return None
    if _clean_path(path) in _LOCAL_CLIENT_READ_PATHS:
        return None
    if request.method.upper() in {"GET", "HEAD"} and not path.startswith(("/api/", "/ws/")):
        return RedirectResponse(
            content_url(request),
            status_code=307,
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(
        {"error": "remote content must use the content origin"},
        status_code=421,
    )
