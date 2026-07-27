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

    from ciao.web.routes_api import _parse_set_cookie_session

    node_mgr = request.app.state.node_state_manager
    host_url = node_mgr.get_host_url()
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
                f"{host_url}/api/auth/login",
                json={"token": password},
            )
            if login_res.status_code != 200:
                return JSONResponse(
                    {"error": "Invalid password for host", "auth_required": True},
                    status_code=401,
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

    node_mgr.set_host_session(host_session)
    # Keep a local session too so local AuthMiddleware stays happy if enabled.
    signed = request.app.state.serializer.dumps({"user": "owner"})
    response = JSONResponse(
        {"ok": True, "mode": "client", "host_url": host_url},
    )
    response.set_cookie(SESSION_COOKIE, signed, **session_cookie_kwargs(request))
    return response


async def auth_logout(request: Request) -> JSONResponse:
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
        if not host_url:
            return JSONResponse(
                {"error": "client mode missing host", "client": True},
                status_code=401,
            )
        if not node_mgr.get_host_session():
            # Legacy standby→client migrations have no stored session. If the
            # host does not require auth, allow the tunnel; otherwise ask for
            # the host password via the login screen.
            try:
                import httpx

                async with httpx.AsyncClient(timeout=3.0) as client:
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
