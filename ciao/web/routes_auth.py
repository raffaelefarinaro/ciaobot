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

# Wrong current-password attempts on POST /api/auth/settings, per client IP.
# Separate from the login counter: a signed-in session can still be used to
# guess the password, and a successful login must not reset this budget.
_password_change_failures: dict[str, list[float]] = {}
_MAX_PASSWORD_CHANGE_FAILURES = 5
_PASSWORD_CHANGE_WINDOW_SECONDS = 15 * 60


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


def _token_matches(supplied: str, expected: str) -> bool:
    # compare_digest raises TypeError on non-ASCII str, so compare bytes.
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _recent_password_change_failures(client_ip: str) -> list[float]:
    window_start = datetime.now(UTC).timestamp() - _PASSWORD_CHANGE_WINDOW_SECONDS
    recent = [t for t in _password_change_failures.get(client_ip, []) if t > window_start]
    if recent:
        _password_change_failures[client_ip] = recent
    else:
        _password_change_failures.pop(client_ip, None)
    return recent


async def auth_login(request: Request) -> JSONResponse:
    app = request.app
    client_ip = request.client.host if request.client else "unknown"
    if not _check_login_rate_limit(client_ip):
        return JSONResponse({"error": "rate limited"}, status_code=429)
    body = await request.json()
    token = str(body.get("token", "") or "")

    if not _token_matches(token, app.state.config.pwa_auth_token):
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


def _on_host(request: Request) -> bool:
    """A browser on the computer running Ciaobot, with an exact same-origin source.

    `host_browser_request` compares host names only, so the port-exact
    Origin/Referer check is added here. The same predicate backs the `on_host`
    flag, so the UI never offers a path the server would refuse.
    """
    from ciao.web.auth import _same_origin_source
    from ciao.web.routes_api import host_browser_request

    return host_browser_request(request) and _same_origin_source(request)


async def auth_settings_get(request: Request) -> JSONResponse:
    """Return whether a PWA password is configured (never the password) and whether this browser is on the host."""
    config = request.app.state.config
    return JSONResponse(
        {
            "password_configured": bool(str(getattr(config, "pwa_auth_token", "") or "").strip()),
            "on_host": _on_host(request),
        }
    )


async def auth_settings_update(request: Request) -> JSONResponse:
    """Set or change the PWA password.

    Body: ``{ "password": str, "current_password"?: str }``.

    Password protection is always on. ``current_password`` is required unless
    the request comes from a browser on the host (``host_browser_request`` plus
    an exact same-origin source), which still needs the session. A request
    relayed by ``tailscale serve`` or any local proxy is not the host. A
    forgotten password is recovered from ``PWA_AUTH_TOKEN`` in the workspace
    ``.env``, or with a one-time ``ciao setup-url`` login. A missing
    or wrong ``current_password`` is 400/403, never 401: 401 means the session
    is gone, and the PWA sends the browser to /login on it.
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
    on_host = _on_host(request)
    client_ip = request.client.host if request.client else "unknown"

    if on_host:
        if not new_password.strip():
            return JSONResponse(
                {"error": "Enter a new password.", "code": "new_password_required"},
                status_code=400,
            )
    else:
        if not current_password:
            return JSONResponse(
                {"error": "Enter your current password.", "code": "current_password_required"},
                status_code=400,
            )
        recent = _recent_password_change_failures(client_ip)
        if len(recent) >= _MAX_PASSWORD_CHANGE_FAILURES:
            return JSONResponse(
                {
                    "error": "Too many wrong attempts. Wait a few minutes and try again.",
                    "code": "rate_limited",
                },
                status_code=429,
            )
        if not _token_matches(current_password, current_token):
            recent.append(datetime.now(UTC).timestamp())
            _password_change_failures[client_ip] = recent
            return JSONResponse(
                {"error": "Current password is incorrect.", "code": "wrong_current_password"},
                status_code=403,
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
    _password_change_failures.pop(client_ip, None)
    logger.info("PWA password changed (on_host=%s)", on_host)

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
