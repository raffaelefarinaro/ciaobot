"""Web Push API routes."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from ciao.web.auth import is_loopback_client


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
