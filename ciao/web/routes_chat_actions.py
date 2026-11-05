"""Browser-only routes for a chat's needs-you cards: start, dismiss and continue.

These routes sit behind the signed session cookie and are not loopback-only. An
agent can raise an action (``ciao chat needs-action``) but can never start,
dismiss or continue one; only the card's buttons do that.

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
from ciao.web.auth import is_loopback_client
from ciao.web.project_chats import ChatActionConflictError, ProjectChatManager


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
