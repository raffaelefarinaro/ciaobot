"""Browser-only routes for a chat's needs-you cards: start, dismiss, continue, callback, turn-off, and raising an MCP sign-in from Settings.

These routes sit behind the signed session cookie and are not loopback-only. An
agent can raise an action (``ciao chat needs-action``) but can never start,
dismiss, continue or turn one off; only the card's buttons do that. Settings' MCP **Sign in** raises an `mcp_auth` card through `chat_mcp_auth_request`, then starts it through the start route.

Every route answers 200 JSON and never 204, because the PWA treats an empty body
as "route not available on the running server". Refusals map the same way in
every route: an unknown chat is 404, an unknown or stale action id is 409 with the
chat's current list, and any ``ChatActionError`` is 400 with the action as it now
stands.
"""

from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import JSONResponse

from ciao.chat_actions import ChatActionError, action_payload
from ciao.oauth_callback import CallbackRefused
from ciao.web.auth import is_loopback_client
from ciao.web.mcp_toggle import McpToggleError
from ciao.web.project_chats import ChatActionConflictError, ProjectChatManager

CALLBACK_BODY_MAX_BYTES = 8192


def _action_error_response(
    pcm: ProjectChatManager,
    chat_id: str,
    action_id: str,
    exc: ChatActionConflictError | ChatActionError,
) -> JSONResponse:
    if isinstance(exc, ChatActionConflictError):
        return JSONResponse(
            {"error": "chat_action_conflict", "actions": exc.actions},
            status_code=409,
        )
    current = pcm.get_action(chat_id, action_id)
    return JSONResponse(
        {
            "error": exc.reason,
            "action": action_payload(current) if current is not None else None,
        },
        status_code=400,
    )


async def chat_action_start(request: Request) -> JSONResponse:
    pcm: ProjectChatManager = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    action_id = request.path_params["action_id"]
    if pcm.get_chat(chat_id) is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    raw = await request.body()
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        return JSONResponse({"error": "invalid_body", "action": None}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid_body", "action": None}, status_code=400)
    try:
        payload = await pcm.actions_runtime.start(
            chat_id, action_id, body, is_loopback=is_loopback_client(request)
        )
    except (ChatActionConflictError, ChatActionError) as exc:
        return _action_error_response(pcm, chat_id, action_id, exc)
    return JSONResponse({"action": payload})


async def chat_action_dismiss(request: Request) -> JSONResponse:
    pcm: ProjectChatManager = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    action_id = request.path_params["action_id"]
    if pcm.get_chat(chat_id) is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        remaining = await pcm.actions_runtime.dismiss(chat_id, action_id)
    except (ChatActionConflictError, ChatActionError) as exc:
        return _action_error_response(pcm, chat_id, action_id, exc)
    return JSONResponse({"actions": remaining})


async def chat_action_continue(request: Request) -> JSONResponse:
    pcm: ProjectChatManager = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    action_id = request.path_params["action_id"]
    if pcm.get_chat(chat_id) is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        remaining, delivery = await pcm.actions_runtime.continue_action(chat_id, action_id)
    except (ChatActionConflictError, ChatActionError) as exc:
        return _action_error_response(pcm, chat_id, action_id, exc)
    return JSONResponse({"actions": remaining, "delivery": delivery})


async def chat_action_turn_off(request: Request) -> JSONResponse:
    """Turn off an mcp_auth card's server for this chat's workspace and provider, and remove the card.

    A config refusal is 400 (never 409, which the PWA reads as a stale card) and keeps the card.
    """
    pcm: ProjectChatManager = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    action_id = request.path_params["action_id"]
    if pcm.get_chat(chat_id) is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        remaining, reload = await pcm.actions_runtime.turn_off_server(chat_id, action_id)
    except McpToggleError as exc:
        current = pcm.get_action(chat_id, action_id)
        return JSONResponse(
            {"error": exc.reason, "action": action_payload(current) if current is not None else None},
            status_code=400,
        )
    except (ChatActionConflictError, ChatActionError) as exc:
        return _action_error_response(pcm, chat_id, action_id, exc)
    return JSONResponse({"actions": remaining, "reload": reload})


async def chat_action_callback(request: Request) -> JSONResponse:
    """Replay a pasted sign-in address. Body exactly ``{"url": "<pasted address>"}``.

    A refused address is 400 with a closed refusal word, and the action is untouched.
    """
    pcm: ProjectChatManager = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    action_id = request.path_params["action_id"]
    if pcm.get_chat(chat_id) is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    raw = await request.body()
    if len(raw) > CALLBACK_BODY_MAX_BYTES:
        return JSONResponse({"error": "too_large", "action": None}, status_code=413)
    try:
        body = json.loads(raw)
    except ValueError:
        return JSONResponse({"error": "invalid_body", "action": None}, status_code=400)
    if not isinstance(body, dict) or set(body) != {"url"} or not isinstance(body["url"], str):
        return JSONResponse({"error": "invalid_body", "action": None}, status_code=400)
    try:
        payload = await pcm.actions_runtime.callback(
            chat_id, action_id, body["url"], is_loopback=is_loopback_client(request)
        )
    except CallbackRefused as exc:
        current = pcm.get_action(chat_id, action_id)
        return JSONResponse(
            {
                "error": exc.reason,
                "action": action_payload(current) if current is not None else None,
            },
            status_code=400,
        )
    except (ChatActionConflictError, ChatActionError) as exc:
        return _action_error_response(pcm, chat_id, action_id, exc)
    return JSONResponse({"action": payload})


async def chat_mcp_auth_request(request: Request) -> JSONResponse:
    """Raise (or return) the chat's MCP sign-in card for one server. Body exactly ``{"server": "<name>"}``."""
    pcm: ProjectChatManager = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    if pcm.get_chat(chat_id) is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    raw = await request.body()
    try:
        body = json.loads(raw) if raw else None
    except ValueError:
        return JSONResponse({"error": "invalid_body", "action": None}, status_code=400)
    if not isinstance(body, dict) or set(body) != {"server"} or not isinstance(body["server"], str):
        return JSONResponse({"error": "invalid_body", "action": None}, status_code=400)
    try:
        payload = pcm.actions_runtime.request_mcp_auth(chat_id, body["server"])
    except ChatActionError as exc:
        return JSONResponse({"error": exc.reason, "action": None}, status_code=400)
    return JSONResponse({"action": payload})
