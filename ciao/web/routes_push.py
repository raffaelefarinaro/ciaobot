"""Web Push API routes."""

from __future__ import annotations

import asyncio
import time

from starlette.requests import Request
from starlette.responses import JSONResponse

from ciao.web.auth import is_loopback_client

# One test per subscription per cooldown: the button is for a person, and a
# loop hammering a push service would get this server's VAPID key throttled.
_TEST_COOLDOWN_SECONDS = 10.0
_last_test_at: dict[str, float] = {}


async def push_public_key(request: Request) -> JSONResponse:
    pm = request.app.state.push_manager
    return JSONResponse({"public_key": pm.public_key})


# Distinguishes a subscription created by the local browser/PWA from one on a
# remote device (a phone reaching the server over LAN/tunnel), so the menu bar
# only stands down for a subscription that actually covers this Mac.
_is_loopback = is_loopback_client


async def push_subscribe(request: Request) -> JSONResponse:
    pm = request.app.state.push_manager
    data = await request.json()
    sub = data.get("subscription") or data
    try:
        pm.add(sub, local=_is_loopback(request))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "count": pm.count()})


async def push_unsubscribe(request: Request) -> JSONResponse:
    pm = request.app.state.push_manager
    data = await request.json()
    endpoint = data.get("endpoint", "")
    if endpoint:
        pm.remove(endpoint)
    return JSONResponse({"ok": True, "count": pm.count()})


async def push_status(request: Request) -> JSONResponse:
    pm = request.app.state.push_manager
    return JSONResponse({"count": pm.count(), "public_key": pm.public_key})


async def push_notification_feed(request: Request) -> JSONResponse:
    """Recent notification entries for the macOS menu bar.

    The tray used to read ``.runtime/notifications.jsonl`` off its own disk,
    which only the machine that ran the chat ever writes. On a client node that
    file stays empty forever, so the reliable native banner never fired there
    and best-effort Web Push was the only channel left. Reading through the API
    instead means the client proxy tunnels this to the host, and both machines
    show the same banners.

    Session-free but loopback-only (see ``_LOOPBACK_ONLY_API`` in
    ``ciao.web.auth``): the bodies are message snippets, so like
    ``/api/menubar-chats`` this must not be reachable from the network.
    """
    pm = request.app.state.push_manager
    try:
        after = float(request.query_params.get("after", "0") or 0.0)
    except ValueError:
        after = 0.0
    return JSONResponse({"notifications": pm.read_log(after=after)})


async def push_subscription_check(request: Request) -> JSONResponse:
    """Confirm whether a given endpoint is registered server-side.

    Used by the frontend on boot: if the browser still has a subscription but
    the server forgot it (state file moved, fresh deployment), re-register
    silently instead of asking the user to grant permission again.
    """
    pm = request.app.state.push_manager
    endpoint = request.query_params.get("endpoint", "")
    return JSONResponse({
        "registered": bool(endpoint) and pm.has(endpoint),
        "count": pm.count(),
    })


async def push_test(request: Request) -> JSONResponse:
    """Send a test notification to the caller's own push subscription."""
    pm = request.app.state.push_manager
    try:
        data = await request.json()
    except ValueError:
        data = {}
    endpoint = str((data or {}).get("endpoint") or "").strip() if isinstance(data, dict) else ""
    if not endpoint:
        return JSONResponse({"error": "endpoint is required"}, status_code=400)
    if not pm.has(endpoint):
        return JSONResponse({"error": "This device is not subscribed on the server. Turn notifications off and on again."}, status_code=404)
    now = time.monotonic()
    last = _last_test_at.get(endpoint)
    if last is not None and now - last < _TEST_COOLDOWN_SECONDS:
        return JSONResponse({"error": "Wait a few seconds before sending another test."}, status_code=429)
    _last_test_at[endpoint] = now
    if not pm.configured:
        return JSONResponse({"error": "Web Push is not configured on this server."}, status_code=502)
    accepted = await asyncio.to_thread(pm.send_test, endpoint)
    if not accepted:
        return JSONResponse({"error": "The push service did not accept the test notification."}, status_code=502)
    return JSONResponse({"ok": True, "accepted": True})
