"""Authentication endpoints for Ciaobot web server."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from datetime import UTC, datetime
from urllib.parse import quote
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from ciao.web.auth import SESSION_COOKIE, is_loopback_client, session_cookie_kwargs
from ciao.web.remote_boundary import (
    content_url,
    is_client_mode,
    is_content_origin,
    is_control_origin,
)

logger = logging.getLogger(__name__)

_login_attempts: dict[str, list[tuple[float, int]]] = {}
_MAX_LOGIN_ATTEMPTS = 10
_LOGIN_WINDOW_SECONDS = 60
_AUTH_BRIDGE_TTL_SECONDS = 60
_AUTH_BRIDGE_MAX_TOKEN_LENGTH = 128


def _auth_bridge_store(app) -> dict[str, tuple[float, str, str, str]]:
    store = getattr(app.state, "auth_bridges", None)
    if not isinstance(store, dict):
        store = {}
        app.state.auth_bridges = store
    return store


def _clear_auth_bridges(app) -> None:
    _auth_bridge_store(app).clear()


def _auth_bridge_binding(app) -> tuple[str, str, str] | None:
    manager = getattr(app.state, "node_state_manager", None)
    if manager is None:
        return None
    try:
        if not manager.is_client():
            return None
        validity = getattr(manager, "is_valid", None)
        if not callable(validity) or not validity():
            return None
        host_url = manager.get_host_url()
        host_session = manager.get_host_session()
        if not host_url or not host_session:
            return None
        digest = hashlib.sha256(host_session.encode("utf-8")).hexdigest()
        return str(host_url), digest, str(getattr(manager, "node_id", ""))
    except Exception:
        return None


def _mint_auth_bridge(app) -> str | None:
    binding = _auth_bridge_binding(app)
    if binding is None:
        return None
    store = _auth_bridge_store(app)
    now = time.monotonic()
    for token, record in list(store.items()):
        if record[0] <= now:
            store.pop(token, None)
    while len(store) >= 32:
        store.pop(next(iter(store)))
    token = secrets.token_urlsafe(32)
    store[hashlib.sha256(token.encode("ascii")).hexdigest()] = (now + _AUTH_BRIDGE_TTL_SECONDS, *binding)
    return token


def _consume_auth_bridge(app, token: str) -> bool:
    if not token or len(token) > _AUTH_BRIDGE_MAX_TOKEN_LENGTH:
        return False
    store = _auth_bridge_store(app)
    key = hashlib.sha256(token.encode("utf-8")).hexdigest()
    record = store.pop(key, None)
    if record is None or record[0] <= time.monotonic():
        return False
    return record[1:] == _auth_bridge_binding(app)


def _auth_bridge_url(request: Request) -> str | None:
    if not is_control_origin(request) or not is_loopback_client(request):
        return None
    token = _mint_auth_bridge(request.app)
    if token is None:
        return None
    return f"{content_url(request, '/api/auth/bridge')}?token={quote(token, safe='')}"


def client_session_response(request: Request, payload: dict) -> JSONResponse:
    bridge_url = _auth_bridge_url(request)
    response_payload = dict(payload)
    if bridge_url:
        response_payload["bridge_url"] = bridge_url
    response = JSONResponse(response_payload)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    serializer = getattr(request.app.state, "serializer", None)
    if serializer is not None:
        signed = serializer.dumps({"user": "owner"})
        response.set_cookie(SESSION_COOKIE, signed, **session_cookie_kwargs(request))
    return response


def _check_login_rate_limit(client_ip: str) -> bool:
    """Return True if the IP is within the rate limit, False if blocked."""
    now = datetime.now(UTC).timestamp()
    window_start = now - _LOGIN_WINDOW_SECONDS
    entries = _login_attempts.get(client_ip, [])
    entries = [(t, c) for (t, c) in entries if t > window_start]
    total = sum(c for (_t, c) in entries)
    if total >= _MAX_LOGIN_ATTEMPTS:
        _login_attempts[client_ip] = entries
        return False
    entries.append((now, 1))
    _login_attempts[client_ip] = entries
    return True


async def auth_login(request: Request) -> JSONResponse:
    app = request.app
    client_ip = request.client.host if request.client else "unknown"
    if not _check_login_rate_limit(client_ip):
        return JSONResponse({"error": "rate limited"}, status_code=429)
    body = await request.json()
    token = str(body.get("token", "") or "")

    node_mgr = getattr(app.state, "node_state_manager", None)
    if node_mgr is not None and node_mgr.is_client():
        return await _client_mode_login(request, token)

    if not hmac.compare_digest(token, app.state.config.pwa_auth_token):
        return JSONResponse({"error": "invalid token"}, status_code=401)
    signed = app.state.serializer.dumps({"user": "owner"})
    response = JSONResponse({"ok": True})
    response.set_cookie(SESSION_COOKIE, signed, **session_cookie_kwargs(request))
    return response


async def _client_mode_login(request: Request, password: str) -> JSONResponse:
    """Authenticate to the remote host and store its session for the tunnel."""
    import httpx

    from ciao.web.routes_helpers import _parse_set_cookie_session

    node_mgr = request.app.state.node_state_manager
    host_url = node_mgr.get_host_url()
    from ciao.node_state import peer_url_is_allowed

    if host_url and not peer_url_is_allowed(host_url, str(request.url.scheme or "")):
        return JSONResponse({"error": "Host transport is not allowed"}, status_code=400)
    if not host_url:
        return JSONResponse(
            {"error": "Client mode has no host URL configured"},
            status_code=400,
        )
    if not password.strip():
        return JSONResponse({"error": "Host password required"}, status_code=400)

    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
            login_res = await client.post(
                f"{host_url}/api/auth",
                json={"token": password},
            )
            if login_res.status_code != 200:
                detail = ""
                try:
                    payload = login_res.json()
                    if isinstance(payload, dict) and payload.get("error"):
                        detail = str(payload["error"])
                except Exception:
                    detail = (login_res.text or "").strip()[:120]
                if login_res.status_code in {401, 403}:
                    return JSONResponse(
                        {"error": "Invalid password for host", "auth_required": True},
                        status_code=401,
                    )
                return JSONResponse(
                    {
                        "error": (
                            f"Host login failed (HTTP {login_res.status_code}"
                            + (f": {detail}" if detail else "")
                            + ")"
                        ),
                        "peer_unreachable": login_res.status_code >= 500,
                    },
                    status_code=400,
                )
            cookies: list[str] = []
            try:
                cookies = login_res.headers.get_list("set-cookie")
            except Exception:
                raw = login_res.headers.get("set-cookie")
                if raw:
                    cookies = [raw]
            host_session = _parse_set_cookie_session(cookies)
            if not host_session:
                host_session = login_res.cookies.get(SESSION_COOKIE)
            if not host_session:
                return JSONResponse(
                    {"error": "Host login succeeded but no session cookie was returned"},
                    status_code=502,
                )
    except Exception as exc:
        return JSONResponse(
            {"error": f"Failed to reach host at {host_url}: {exc}", "peer_unreachable": True},
            status_code=400,
        )

    _clear_auth_bridges(request.app)
    node_mgr.set_host_session(host_session)
    # Keep a local session too so local AuthMiddleware stays happy if enabled.
    return client_session_response(
        request,
        {"ok": True, "mode": "client", "host_url": host_url},
    )


async def auth_bridge(request: Request) -> Response:
    if request.method.upper() != "GET":
        return JSONResponse({"error": "method not allowed"}, status_code=405)
    if not is_loopback_client(request) or not is_content_origin(request):
        return JSONResponse({"error": "client session bridge is local-only"}, status_code=403)
    token = request.query_params.get("token", "")
    if not _consume_auth_bridge(request.app, token):
        return JSONResponse({"error": "invalid or expired session bridge"}, status_code=401)
    response = RedirectResponse(content_url(request, "/"), status_code=302)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    signed = request.app.state.serializer.dumps({"user": "owner"})
    response.set_cookie(SESSION_COOKIE, signed, **session_cookie_kwargs(request))
    return response


async def auth_bridge_issue(request: Request) -> Response:
    if not is_loopback_client(request) or not is_control_origin(request):
        return JSONResponse({"error": "client session bridge is local-only"}, status_code=403)
    bridge_url = _auth_bridge_url(request)
    if bridge_url:
        response = RedirectResponse(bridge_url, status_code=302)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
    if not is_client_mode(request):
        response = RedirectResponse(content_url(request, "/"), status_code=302)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
    return JSONResponse({"error": "host session is required"}, status_code=401)


async def auth_logout(request: Request) -> JSONResponse:
    _clear_auth_bridges(request.app)
    node_mgr = getattr(request.app.state, "node_state_manager", None)
    if node_mgr is not None and node_mgr.is_client():
        node_mgr.set_host_session(None)
    response = JSONResponse({"ok": True})
    cookie_kwargs = session_cookie_kwargs(request)
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        domain=cookie_kwargs.get("domain"),
        secure=bool(cookie_kwargs.get("secure")),
        httponly=True,
        samesite="lax",
    )
    return response


async def auth_check(request: Request) -> JSONResponse:
    # Bootstrap mode must land the browser on the setup wizard. The wizard
    # lives in the login view, and with auth off by default nothing would
    # ever route there — the SPA would open straight into the app on the
    # throwaway bootstrap workspace. Report unauthenticated until setup
    # finishes so the router redirects to /login → first-run wizard.
    config = getattr(request.app.state, "config", None)
    if getattr(config, "bootstrap_mode", False):
        return JSONResponse({"error": "setup required"}, status_code=401)

    node_mgr = getattr(request.app.state, "node_state_manager", None)
    if node_mgr is not None and node_mgr.is_client():
        host_url = node_mgr.get_host_url()
        from ciao.node_state import peer_url_is_allowed

        if host_url and not peer_url_is_allowed(host_url, str(request.url.scheme or "")):
            return JSONResponse({"error": "Host transport is not allowed"}, status_code=400)
        if not host_url:
            return JSONResponse(
                {"error": "client mode missing host", "client": True},
                status_code=401,
            )
        if getattr(config, "pwa_auth_required", False):
            from ciao.web.auth import verify_session

            serializer = getattr(request.app.state, "serializer", None)
            if serializer is None or not verify_session(request, serializer):
                return JSONResponse({"error": "client session required", "client": True}, status_code=401)
        if not node_mgr.get_host_session():
            # Legacy standby→client migrations have no stored session. If the
            # host does not require auth, allow the tunnel; otherwise ask for
            # the host password via the login screen.
            try:
                import httpx

                async with httpx.AsyncClient(timeout=3.0, follow_redirects=False) as client:
                    res = await client.get(f"{host_url}/api/startup-status")
                    if res.status_code == 200:
                        payload = res.json() if res.headers.get("content-type", "").startswith("application/json") else {}
                        if isinstance(payload, dict) and not payload.get("auth_required"):
                            return JSONResponse(
                                {
                                    "ok": True,
                                    "mode": "client",
                                    "host_url": host_url,
                                    "has_host_session": False,
                                }
                            )
            except Exception:
                pass
            return JSONResponse(
                {
                    "error": "host session required",
                    "client": True,
                    "host_url": host_url,
                },
                status_code=401,
            )
        return JSONResponse(
            {
                "ok": True,
                "mode": "client",
                "host_url": host_url,
                "has_host_session": True,
            }
        )

    # Host mode: when a PWA password is required, /api/auth/check must actually
    # verify the session. Returning ok unconditionally let the SPA mount chats
    # while /api/chats and /ws/* correctly 401/403'd — causing a reconnect storm.
    if getattr(config, "pwa_auth_required", False):
        from ciao.web.auth import verify_session

        serializer = getattr(request.app.state, "serializer", None)
        if serializer is None or not verify_session(request, serializer):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

    return JSONResponse({"ok": True})


async def auth_settings_get(request: Request) -> JSONResponse:
    """Return whether PWA password protection is enabled (never the password)."""
    config = request.app.state.config
    return JSONResponse(
        {
            "auth_required": bool(getattr(config, "pwa_auth_required", False)),
            "password_configured": bool(str(getattr(config, "pwa_auth_token", "") or "").strip()),
        }
    )


async def auth_settings_update(request: Request) -> JSONResponse:
    """Set or change the PWA password.

    Body: ``{ "password": str, "current_password"?: str }``.
    When protection is already on, ``current_password`` is required.

    Protection cannot be switched off here — it is the default, and the only
    way out is ``PWA_AUTH_REQUIRED=false`` in the workspace ``.env``, which is
    an explicit operator decision made on the machine itself. A request that
    asks for ``auth_required: false`` is rejected rather than ignored.

    Setting the first password needs no proof of authority: when protection is
    off there is no credential to offer, and a headless host reached over a
    tailnet from a phone (a documented setup — see INTEGRATIONS.md) has no
    localhost browser to fall back to, so requiring a local caller would leave
    that install permanently unprotectable. The exposure this accepts is small
    next to the state it is fixing: an unprotected instance already lets anyone
    who can reach it read and write everything, so a hostile enable costs the
    owner a lockout, recoverable by editing ``PWA_AUTH_TOKEN`` in the workspace
    ``.env``. A caller that is not local is logged with its peer address.
    """
    from ciao.web.auth import (
        MIN_PWA_PASSWORD_LENGTH,
        is_loopback_client,
        make_serializer,
    )
    from ciao.web.routes_api import _env_path, _write_env_values

    config = request.app.state.config
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected object"}, status_code=400)

    currently_required = bool(getattr(config, "pwa_auth_required", False))
    current_token = str(getattr(config, "pwa_auth_token", "") or "")
    if "auth_required" in body and not bool(body.get("auth_required")):
        return JSONResponse(
            {
                "error": (
                    "Password protection cannot be turned off here. Set "
                    "PWA_AUTH_REQUIRED=false in the workspace .env and restart "
                    "if this machine really needs an unprotected dashboard."
                )
            },
            status_code=400,
        )
    new_password = str(body.get("password") or "")
    current_password = str(body.get("current_password") or "")

    if currently_required:
        if not current_password or not hmac.compare_digest(current_password, current_token):
            return JSONResponse(
                {"error": "Current password is required (and must match)"},
                status_code=401,
            )
    elif not is_loopback_client(request):
        client = request.client
        logger.warning(
            "PWA password protection enabled by a non-local caller (peer=%s). "
            "Recover with PWA_AUTH_TOKEN in the workspace .env if this was not you.",
            client.host if client else "unknown",
        )

    token_to_store = new_password.strip() or current_token
    if not token_to_store:
        return JSONResponse({"error": "Set a password"}, status_code=400)
    if len(token_to_store) < MIN_PWA_PASSWORD_LENGTH:
        return JSONResponse(
            {"error": f"Password must be at least {MIN_PWA_PASSWORD_LENGTH} characters"},
            status_code=400,
        )

    updates = {
        "PWA_AUTH_REQUIRED": "true",
        "PWA_AUTH_TOKEN": token_to_store,
    }
    _write_env_values(_env_path(config), updates)
    config.pwa_auth_required = True
    config.pwa_auth_token = token_to_store
    request.app.state.serializer = make_serializer(token_to_store)

    response = JSONResponse(
        {
            "ok": True,
            "auth_required": True,
            "password_configured": bool(token_to_store),
        }
    )
    # Keep this browser logged in after rotating the signing secret.
    signed = request.app.state.serializer.dumps({"user": "owner"})
    response.set_cookie(SESSION_COOKIE, signed, **session_cookie_kwargs(request))
    return response
