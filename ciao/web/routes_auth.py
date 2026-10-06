"""Authentication endpoints for Ciaobot web server."""

from __future__ import annotations

import hmac
import logging
from datetime import UTC, datetime

from starlette.requests import Request
from starlette.responses import JSONResponse

from ciao.web.auth import SESSION_COOKIE, session_cookie_kwargs

logger = logging.getLogger(__name__)

_login_attempts: dict[str, list[tuple[float, int]]] = {}
_MAX_LOGIN_ATTEMPTS = 10
_LOGIN_WINDOW_SECONDS = 60


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

    if not hmac.compare_digest(token, app.state.config.pwa_auth_token):
        return JSONResponse({"error": "invalid token"}, status_code=401)
    signed = app.state.serializer.dumps({"user": "owner"})
    response = JSONResponse({"ok": True})
    response.set_cookie(SESSION_COOKIE, signed, **session_cookie_kwargs(request))
    return response


async def auth_logout(request: Request) -> JSONResponse:
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

    # /api/auth/check must actually verify the session. Returning ok
    # unconditionally let the SPA mount chats while /api/chats and /ws/*
    # correctly 401/403'd — causing a reconnect storm.
    from ciao.web.auth import verify_session

    serializer = getattr(request.app.state, "serializer", None)
    if serializer is None or not verify_session(request, serializer):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    return JSONResponse({"ok": True})


async def auth_settings_get(request: Request) -> JSONResponse:
    """Return whether a PWA password is configured (never the password)."""
    config = request.app.state.config
    return JSONResponse(
        {
            "password_configured": bool(str(getattr(config, "pwa_auth_token", "") or "").strip()),
        }
    )


async def auth_settings_update(request: Request) -> JSONResponse:
    """Set or change the PWA password.

    Body: ``{ "password": str, "current_password": str }``.

    Password protection is always on, so ``current_password`` is always
    required. A forgotten password is recovered from ``PWA_AUTH_TOKEN`` in the
    workspace ``.env``, or with a one-time ``ciao setup-url`` login.
    """
    from ciao.web.auth import MIN_PWA_PASSWORD_LENGTH, make_serializer
    from ciao.web.routes_api import _env_path, _write_env_values

    config = request.app.state.config
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected object"}, status_code=400)

    current_token = str(getattr(config, "pwa_auth_token", "") or "")
    new_password = str(body.get("password") or "")
    current_password = str(body.get("current_password") or "")

    if not current_password or not hmac.compare_digest(current_password, current_token):
        return JSONResponse(
            {"error": "Current password is required (and must match)"},
            status_code=401,
        )

    token_to_store = new_password.strip() or current_token
    if not token_to_store:
        return JSONResponse({"error": "Set a password"}, status_code=400)
    if len(token_to_store) < MIN_PWA_PASSWORD_LENGTH:
        return JSONResponse(
            {"error": f"Password must be at least {MIN_PWA_PASSWORD_LENGTH} characters"},
            status_code=400,
        )

    _write_env_values(_env_path(config), {"PWA_AUTH_TOKEN": token_to_store})
    config.pwa_auth_token = token_to_store
    request.app.state.serializer = make_serializer(token_to_store)

    response = JSONResponse(
        {
            "ok": True,
            "password_configured": bool(token_to_store),
        }
    )
    # Keep this browser logged in after rotating the signing secret.
    signed = request.app.state.serializer.dumps({"user": "owner"})
    response.set_cookie(SESSION_COOKIE, signed, **session_cookie_kwargs(request))
    return response
