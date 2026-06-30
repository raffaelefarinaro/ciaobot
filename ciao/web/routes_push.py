"""Web Push API routes."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse


async def push_public_key(request: Request) -> JSONResponse:
    pm = request.app.state.push_manager
    return JSONResponse({"public_key": pm.public_key})


async def push_subscribe(request: Request) -> JSONResponse:
    pm = request.app.state.push_manager
    data = await request.json()
    sub = data.get("subscription") or data
    try:
        pm.add(sub)
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
