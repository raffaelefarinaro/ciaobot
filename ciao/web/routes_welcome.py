"""`/api/welcome*` — the one-time welcome card on Home.

`ciao setup` writes `.runtime/welcome-pending` only when it generated the PWA
password itself (see `ciao.setup_marker`). While that marker exists Home can
show the password, open the workspace folder, and be dismissed.

Every route takes the normal session path (signed session cookie, same-origin
check on POSTs) and appears in neither the public nor the loopback-only
allowlist: the latter lets a loopback peer skip the session. The password read
and the folder open then add ``host_browser_request``: loopback TCP peer with
no proxy headers, local Host, exact same-origin Origin/Referer. `tailscale serve`
dials the engine from 127.0.0.1 but always adds ``Tailscale-*`` and
``X-Forwarded-For`` headers and a ``*.ts.net`` Host, so a tailnet visitor fails
that check twice. An SSH port-forward still looks local; anyone with SSH to the
host can already read `.env`.

Dismissing deletes the marker for every device, and the password route is
gated on the marker, so once it is gone nothing returns the password. The
password is never logged or put in an error message.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from ciao import provider_registry
from ciao.setup_marker import (
    RUNTIME_DIR_NAME,
    clear_welcome_pending,
    welcome_pending,
)
from ciao.web.auth import _same_origin_source
from ciao.web.routes_api import _open_path_with_default_app, host_browser_request

_NO_STORE = {"Cache-Control": "no-store"}
NOT_ON_HOST = "The password is shown on the computer running Ciaobot."


def _host_request(request: Request) -> bool:
    # host_browser_request ignores ports; the exact-origin check keeps another
    # localhost app (a dev server on :3000) from reading the password.
    return host_browser_request(request) and _same_origin_source(request)


def _runtime_root(config: Any) -> Path:
    return Path(config.workspace_root) / RUNTIME_DIR_NAME


async def welcome_status(request: Request) -> JSONResponse:
    config = request.app.state.config
    if not welcome_pending(_runtime_root(config)):
        return JSONResponse({"pending": False}, headers=_NO_STORE)
    provider: str = config.default_provider_for_workspace(config.primary_workspace())
    descriptor = provider_registry.get(provider)
    return JSONResponse(
        {
            "pending": True,
            "workspace_root": str(Path(config.workspace_root).resolve()),
            "provider": provider,
            "provider_label": descriptor.cli_label if descriptor else provider,
            "on_host": _host_request(request),
            "platform": sys.platform,
        },
        headers=_NO_STORE,
    )


async def welcome_password(request: Request) -> JSONResponse:
    config = request.app.state.config
    if not _host_request(request):
        return JSONResponse(
            {"error": NOT_ON_HOST, "reason": "not_on_host"},
            status_code=403,
            headers=_NO_STORE,
        )
    if not welcome_pending(_runtime_root(config)):
        return JSONResponse({"error": "nothing to show"}, status_code=404, headers=_NO_STORE)
    password: str = str(config.pwa_auth_token or "")
    return JSONResponse({"password": password}, headers=_NO_STORE)


async def welcome_open_folder(request: Request) -> JSONResponse:
    config = request.app.state.config
    if not _host_request(request):
        return JSONResponse(
            {
                "error": "Opening the folder is only available on the computer running Ciaobot.",
                "reason": "not_on_host",
            },
            status_code=403,
            headers=_NO_STORE,
        )
    root = Path(config.workspace_root).resolve()
    if not root.is_dir():
        return JSONResponse({"error": "folder not found"}, status_code=404, headers=_NO_STORE)
    try:
        await asyncio.to_thread(_open_path_with_default_app, root)
    except (OSError, subprocess.SubprocessError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=500, headers=_NO_STORE)
    return JSONResponse({"ok": True}, headers=_NO_STORE)


async def welcome_dismiss(request: Request) -> JSONResponse:
    await asyncio.to_thread(clear_welcome_pending, _runtime_root(request.app.state.config))
    return JSONResponse({"ok": True}, headers=_NO_STORE)
