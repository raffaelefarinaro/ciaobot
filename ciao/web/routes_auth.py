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
