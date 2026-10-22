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
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

from ciao.setup_marker import RUNTIME_DIR_NAME, clear_welcome_pending
from ciao.terminal_auth import TERMINAL_COOKIE, TerminalGrantStore
from ciao.web.auth import SESSION_COOKIE, session_cookie_kwargs
from ciao.web.routes_terminal import _scheme_of

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


def _terminal_grants(request: Request) -> TerminalGrantStore:
    """The app's grant store.

    There is no fallback: the factory always wires one, and a route that
    cannot find it is a wiring bug rather than a request to answer.
    """
    store: TerminalGrantStore = request.app.state.terminal_grants
    return store


def _clear_terminal_cookie(response: JSONResponse, request: Request) -> None:
    """Clear the terminal cookie with the shape the unlock gave it.

    The grant cookie is host-only (no ``Domain``), ``Path=/`` (both
    ``/api/terminal`` and ``/ws/terminal`` need it), ``HttpOnly`` and
    ``SameSite=Strict``, and ``Secure`` when the request arrived over
    HTTPS — so the browser drops the bearer it is holding. A ``Secure``
    cookie would be dropped by a browser on plain HTTP, so the flag
    follows the request's own scheme, read the way the terminal's own
    policy reads it: the effective ASGI scheme, never a raw forwarded
    header.
    """
    response.delete_cookie(
        TERMINAL_COOKIE,
        path="/",
        domain=None,
        secure=_scheme_of(request) == "https",
        httponly=True,
        samesite="strict",
    )


def _revoke_presented_terminal_grant(request: Request) -> None:
    """Revoke the terminal grant this request presents, and only that one.

    The bearer in the terminal cookie names exactly one grant, and the
    cookie belongs to one browser. A logout that presents no cookie, or a
    cookie naming a grant that is unknown, expired or already revoked,
    ends nothing — so a browser that never unlocked a terminal cannot end
    another browser's.
    """
    token = str(request.cookies.get(TERMINAL_COOKIE, "") or "")
    if not token:
        return
    _terminal_grants(request).revoke(token)


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
    """End this browser's session, and this browser's terminal grant.

    The logout presents the grant it is ending. It revokes *that* grant and
    nothing else: the login cookie cannot say which browser is asking —
    two browsers signed in within the same second can hold byte-identical
    cookies — so revoking "the grant for this session" would end another
    browser's terminal too. The grant is named by the bearer in the
    terminal cookie, and the bearer is a secret of one browser.
    """
    response = JSONResponse({"ok": True})
    cookie_kwargs = session_cookie_kwargs(request)
    _revoke_presented_terminal_grant(request)
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        domain=cookie_kwargs.get("domain"),
        secure=bool(cookie_kwargs.get("secure")),
        httponly=True,
        samesite="lax",
    )
    # The terminal cookie is cleared with the shape the unlock gave it, so
    # the browser drops the bearer it is holding.
    _clear_terminal_cookie(response, request)
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

    `host_browser_request` already refuses a browser origin that is not this
    engine's exact origin (scheme, host and effective port); the
    `_same_origin_source` conjunct is kept here as defense in depth. The same
    predicate backs the `on_host` flag, so the UI never offers a path the
    server would refuse.
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

    A successful change ends every terminal grant, on every browser: it
    re-keys the sessions behind them, so every browser has to sign in and
    ask for a new grant. A refused change ends nothing.
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
    # The generated password the welcome card would show is no longer the password.
    clear_welcome_pending(Path(config.workspace_root) / RUNTIME_DIR_NAME)
    config.pwa_auth_token = token_to_store
    request.app.state.serializer = make_serializer(token_to_store)
    _password_change_failures.pop(client_ip, None)
    # A change re-keys the sessions behind the terminal grants, so it ends
    # every grant — on every browser, including this one. A rewrite of the
    # same secret is still a change and still ends them. Nothing is revoked
    # on the failure paths above: a refused change changes nothing.
    _terminal_grants(request).revoke_all()
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
    # The terminal cookie goes with the grants it named: this browser has
    # to acquire a new terminal grant after signing in again.
    _clear_terminal_cookie(response, request)
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
