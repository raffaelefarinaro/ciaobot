"""Authentication endpoints for Ciaobot web server."""

from __future__ import annotations

import asyncio
import hmac
import html
import logging
import os
import secrets
import subprocess
import sys
import time
from datetime import UTC, datetime

from starlette.requests import Request
from starlette.responses import JSONResponse

from ciao.web.auth import SESSION_COOKIE, session_cookie_kwargs

logger = logging.getLogger(__name__)

_login_attempts: dict[str, list[tuple[float, int]]] = {}
_MAX_LOGIN_ATTEMPTS = 10
_LOGIN_WINDOW_SECONDS = 60

_HOST_SIGN_IN_COOLDOWN_SECONDS = 10
_SIGN_IN_PAGE_TTL_SECONDS = 120
_SIGN_IN_PAGE_GLOB = "sign-in-*.html"
_last_host_sign_in: float = 0.0


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


def _desktop_available() -> bool:
    """False on a Linux host with no display (the systemd/VPS install)."""
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


def _open_failed_response() -> JSONResponse:
    return JSONResponse(
        {
            "error": "Could not open a browser on this computer. Run `ciao setup-url` in a terminal instead.",
            "reason": "open_failed",
        },
        status_code=409,
    )


async def auth_host_sign_in(request: Request) -> JSONResponse:
    """Open a one-time sign-in page in the host's default browser.

    Loopback-only and needs no session (see ``_LOOPBACK_ONLY_API``). The fresh
    setup token goes only to owner-only files in ``.runtime`` and into the host
    browser the engine opens; it is never in the response, the logs or a
    process argument list.
    """
    global _last_host_sign_in
    from ciao.os_support.private import open_private
    from ciao.web.auth import _split_host, mint_setup_token
    from ciao.web.routes_api import _open_path_with_default_app, host_browser_request

    if not host_browser_request(request):
        return JSONResponse(
            {
                "error": "Signing in without the password only works in a browser on the computer running Ciaobot.",
                "reason": "not_on_host",
            },
            status_code=403,
        )
    config = getattr(request.app.state, "config", None)
    if getattr(config, "bootstrap_mode", False):
        return JSONResponse(
            {"error": "Finish setup first.", "reason": "setup_required"}, status_code=409
        )
    if not _desktop_available():
        return JSONResponse(
            {
                "error": "Ciaobot runs without a desktop here, so it cannot open a browser. Run `ciao setup-url` in a terminal on this computer.",
                "reason": "no_desktop",
            },
            status_code=409,
        )
    server = request.scope.get("server")
    if not server or not server[1]:
        return JSONResponse(
            {
                "error": "Could not work out which port Ciaobot is listening on. Run `ciao setup-url` in a terminal instead.",
                "reason": "open_failed",
            },
            status_code=409,
        )
    # The redirect port is the socket's own, never the Host header's: that header is caller-controlled.
    name, header_port = _split_host(request.headers.get("host", ""))
    if header_port is not None and header_port != server[1]:
        return JSONResponse(
            {
                "error": "Signing in without the password only works in a browser on the computer running Ciaobot.",
                "reason": "not_on_host",
            },
            status_code=403,
        )
    if name in ("", "0.0.0.0"):
        name = "localhost"
    host = f"[{name}]" if ":" in name else name
    url_base = f"http://{host}:{server[1]}"
    now = time.monotonic()
    if now - _last_host_sign_in < _HOST_SIGN_IN_COOLDOWN_SECONDS:
        return JSONResponse(
            {
                "error": "A sign-in page was just opened. Look for it in your browser.",
                "reason": "cooldown",
            },
            status_code=429,
        )
    _last_host_sign_in = now

    try:
        minted = mint_setup_token(request)
    except FileExistsError:
        return _open_failed_response()
    if minted is None:
        return JSONResponse(
            {"error": "No workspace is configured.", "reason": "no_workspace"}, status_code=409
        )
    token, token_path = minted
    runtime_dir = token_path.parent
    url = f"{url_base}/?setup={token}"

    for stale in runtime_dir.glob(_SIGN_IN_PAGE_GLOB):
        stale.unlink(missing_ok=True)
    page = runtime_dir / f"sign-in-{secrets.token_hex(8)}.html"
    escaped = html.escape(url, quote=True)
    fd = open_private(page, os.O_WRONLY | os.O_EXCL)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(
            '<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer">\n'
            f'<meta http-equiv="refresh" content="0;url={escaped}"><title>Signing in to Ciaobot</title>\n'
            f'<p><a href="{escaped}">Continue to Ciaobot</a></p>\n'
        )
    try:
        await asyncio.to_thread(_open_path_with_default_app, page)
    except (OSError, subprocess.SubprocessError) as exc:
        page.unlink(missing_ok=True)
        token_path.unlink(missing_ok=True)
        logger.warning("host sign-in could not open a browser: %s", exc)
        return _open_failed_response()
    asyncio.get_running_loop().call_later(
        _SIGN_IN_PAGE_TTL_SECONDS, lambda: page.unlink(missing_ok=True)
    )
    logger.info("host sign-in page opened")
    return JSONResponse({"ok": True})
