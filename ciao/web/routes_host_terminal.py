"""`GET`/`POST /api/host/terminal` — open a terminal on the engine's machine.

Both routes take the normal session path (signed session cookie plus the
same-origin check on the POST) and appear in neither the public nor the
loopback-only allowlist: that last set lets a loopback peer skip the session,
which would let any local process open terminals. The handlers then add
``host_browser_request``: loopback TCP peer, loopback Host, same-host
Origin/Referer.

The body carries an action id or a chat/project id, never a command or a path;
the command and the folder are resolved server-side in ``ciao.host_terminal``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from ciao import host_terminal
from ciao.web.routes_api import host_browser_request


def _fail(reason: str, error: str, status: int) -> JSONResponse:
    return JSONResponse({"ok": False, "reason": reason, "error": error}, status_code=status)


async def host_terminal_status(request: Request) -> JSONResponse:
    if not host_browser_request(request):
        return JSONResponse(
            {
                "available": False,
                "platform": host_terminal.host_platform(),
                "reason": "not_on_host",
                "terminal": "",
                "actions": [],
            }
        )
    caps = await asyncio.to_thread(host_terminal.capabilities)
    return JSONResponse(caps.as_dict())


async def host_terminal_launch(request: Request) -> JSONResponse:
    if not host_browser_request(request):
        return _fail("not_on_host", host_terminal.REASON_MESSAGES["not_on_host"], 403)
    try:
        body: Any = await request.json()
    except (json.JSONDecodeError, ValueError):
        return _fail("bad_request", "invalid json", 400)
    if not isinstance(body, dict):
        return _fail("bad_request", "invalid json", 400)
    keys = set(body)
    config = request.app.state.config
    action: str | None = None
    if keys == {"action"}:
        value = body["action"]
        if not isinstance(value, str) or value not in host_terminal.ACTION_IDS:
            return _fail("unknown_action", "unknown action", 400)
        action = value
        workspace = Path(config.workspace_root)
        folder = workspace if workspace.is_dir() else Path.home()
    elif (
        "open" in keys
        and keys <= {"open", "chat_id", "project_id"}
        and keys & {"chat_id", "project_id"}
        and body["open"] == "folder"
    ):
        for key in ("chat_id", "project_id"):
            if key in body and not isinstance(body[key], str):
                return _fail("bad_request", f"{key} must be a string", 400)
        try:
            folder = host_terminal.resolve_folder(
                config,
                request.app.state.project_chat_manager,
                chat_id=str(body.get("chat_id") or ""),
                project_id=str(body.get("project_id") or ""),
            )
        except ValueError as exc:
            return _fail("bad_request", str(exc), 400)
        except LookupError as exc:
            return _fail("not_found", str(exc), 404)
        except FileNotFoundError as exc:
            return _fail("folder_missing", f"folder not found: {exc}", 409)
    else:
        return _fail("bad_request", 'send {"action": id} or {"open": "folder", "chat_id"|"project_id": id}', 400)
    try:
        launched = await asyncio.to_thread(
            host_terminal.launch,
            folder,
            action=action,
            runtime_dir=Path(config.state_path).parent,
        )
    except host_terminal.TerminalUnavailable as exc:
        return JSONResponse(
            {"ok": False, "platform": host_terminal.host_platform(), "reason": exc.reason, "error": str(exc)},
            status_code=409,
        )
    except host_terminal.NothingToUpdate as exc:
        return _fail("not_applicable", str(exc), 409)
    except OSError as exc:
        return _fail("launch_failed", str(exc), 500)
    return JSONResponse({"ok": True, "platform": host_terminal.host_platform(), "launched": launched})
