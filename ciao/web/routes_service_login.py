"""`GET`/`PATCH /api/service/login` — the host engine's verified sign-in state.

Two routes over ``ciao/service_login``: a read, and a change of one boolean.

The workspace is the running engine's own ``config.workspace_root`` and nothing
else. There is no ``?workspace=``, no path in the body and no way to name a
different install: the service login control is about the engine serving this
request, so a caller cannot point it at another workspace's service.

Both routes go through the standard ``AuthMiddleware`` — session cookie plus
the unsafe-origin guard on the PATCH — and neither is in the public or
loopback-only allowlist. The service layer owns every refusal; this module
only maps the two outcomes onto status codes, and validates the body before any
OS call happens.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

from ciao import service_login
from ciao.web.routes_helpers import api_error

# One change at a time, for the whole process. Two concurrent PATCHes would
# each re-read the enabled bit, then each write it, and the second write would
# silently win over the first: the serialisation makes the second request
# re-read the first one's result and answer for what is actually registered.
_LOGIN_LOCK = asyncio.Lock()

_BODY_ERROR = 'send exactly {"enabled": true} or {"enabled": false}'


def _workspace(request: Request) -> Path | None:
    """The engine's own workspace root, or None when it is not configured."""
    config = getattr(request.app.state, "config", None)
    workspace = getattr(config, "workspace_root", None)
    if workspace is None:
        return None
    return Path(workspace).expanduser()


async def _requested_enabled(request: Request) -> bool | None:
    """The one boolean the body may carry, or None when the body is not one.

    Strict on purpose: a string, a number, ``null``, an extra key, an empty
    object or unparseable JSON are all rejected before the request is allowed
    near the OS, so nothing can be changed by a body that did not clearly ask
    for it. ``isinstance(1, bool)`` is False and ``isinstance(True, bool)`` is
    True, so ``1`` is not accepted as ``true`` here.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(body, dict) or set(body) != {"enabled"}:
        return None
    value = body["enabled"]
    return value if isinstance(value, bool) else None


async def service_login_status(request: Request) -> JSONResponse:
    """The engine service's start-at-sign-in state, as the machine reports it.

    Always 200: a service that is not installed, one this engine does not own,
    and a query that could not be answered are all states this endpoint
    reports, not failures of the endpoint. ``installed`` and ``enabled`` are
    ``null`` when the machine did not say, and ``can_change`` is false unless
    the enabled bit is proven to be the thing that decides.

    Read-only. It starts, stops, registers and rewrites nothing.
    """
    workspace = _workspace(request)
    if workspace is None:
        return api_error("workspace root is not configured", 500)
    status = await asyncio.to_thread(service_login.login_status, workspace)
    return JSONResponse(status.as_dict())


async def service_login_update(request: Request) -> JSONResponse:
    """Turn start-at-sign-in on or off for the engine serving this request.

    Body: exactly ``{"enabled": true|false}``. Nothing else is accepted, and
    nothing is touched until the body has been read and accepted.

    200 with the re-read status once the machine confirms the new state; 409
    with the same status beside the reason when the change is not this engine's
    to make (no service, another workspace's service, a definition that does
    not start at sign-in, an unanswerable query); 503 when the OS refused the
    change or the re-read did not confirm it — never a 200 that claims a
    position nobody proved.
    """
    enabled = await _requested_enabled(request)
    if enabled is None:
        return api_error(_BODY_ERROR, 400)
    workspace = _workspace(request)
    if workspace is None:
        return api_error("workspace root is not configured", 500)
    async with _LOGIN_LOCK:
        # The service re-reads the machine inside this section, so the
        # ownership and changeability checks it makes describe the state the
        # write is about to act on, not the one a caller read earlier.
        try:
            status = await asyncio.to_thread(
                service_login.set_login_enabled, workspace, enabled
            )
        except service_login.LoginRefused as exc:
            return _failure_response(exc, 409)
        except service_login.LoginUnavailable as exc:
            return _failure_response(exc, 503)
    return JSONResponse(status.as_dict())


def _failure_response(exc: service_login.LoginError, code: int) -> JSONResponse:
    """The error envelope with the freshest status the failure carries."""
    status = exc.status
    payload: dict[str, object] = {"error": str(exc)}
    if status is not None:
        payload.update(status.as_dict())
    return JSONResponse(payload, status_code=code)
