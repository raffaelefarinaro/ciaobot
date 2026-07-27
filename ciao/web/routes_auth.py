"""Authentication endpoints for Ciaobot web server."""

from __future__ import annotations

import hmac
from datetime import UTC, datetime
from starlette.requests import Request
from starlette.responses import JSONResponse

from ciao.web.auth import SESSION_COOKIE, session_cookie_kwargs

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
    token = body.get("token", "")
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
    """Enable/disable PWA password or change it from Settings.

    Body: ``{ "auth_required": bool, "password"?: str, "current_password"?: str }``.
    When auth is already on, ``current_password`` is required to change it or turn it off.
    """
    from ciao.web.auth import make_serializer
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
    want_required = bool(body.get("auth_required", currently_required))
    new_password = str(body.get("password") or "")
    current_password = str(body.get("current_password") or "")

    if currently_required:
        if not current_password or not hmac.compare_digest(current_password, current_token):
            return JSONResponse(
                {"error": "Current password is required (and must match)"},
                status_code=401,
            )

    if want_required:
        token_to_store = new_password.strip() or current_token
        if not token_to_store:
            return JSONResponse(
                {"error": "Set a password before enabling protection"},
                status_code=400,
            )
        if len(token_to_store) < 4:
            return JSONResponse(
                {"error": "Password must be at least 4 characters"},
                status_code=400,
            )
    else:
        token_to_store = current_token  # keep token in .env even if protection is off

    if new_password.strip():
        token_to_store = new_password.strip()

    updates = {
        "PWA_AUTH_REQUIRED": "true" if want_required else "false",
        "PWA_AUTH_TOKEN": token_to_store,
    }
    _write_env_values(_env_path(config), updates)
    config.pwa_auth_required = want_required
    config.pwa_auth_token = token_to_store
    request.app.state.serializer = make_serializer(token_to_store)

    response = JSONResponse(
        {
            "ok": True,
            "auth_required": want_required,
            "password_configured": bool(token_to_store),
        }
    )
    if want_required:
        # Keep this browser logged in after rotating the signing secret.
        signed = request.app.state.serializer.dumps({"user": "owner"})
        response.set_cookie(SESSION_COOKIE, signed, **session_cookie_kwargs(request))
    return response
