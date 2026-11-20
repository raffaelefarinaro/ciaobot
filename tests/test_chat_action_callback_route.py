"""The paste-back route for a needs-you sign-in: POST /api/chats/{id}/actions/{aid}/callback.

The route is the real one behind the real session gate, over a real manager. Only
``gws_auth`` takes a paste-back; every other kind refuses it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web.auth import SESSION_COOKIE, AuthMiddleware
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_chat_actions import (
    chat_action_callback,
    chat_action_continue,
    chat_action_dismiss,
    chat_action_start,
)
from tests.test_chat_actions_runtime import make_manager, make_runtime

_ORIGIN = "https://ciao.example"
_HEADERS = {"Origin": _ORIGIN}
_JSON_HEADERS = {"Origin": _ORIGIN, "Content-Type": "application/json"}
_CALLBACK = "/api/chats/{cid}/actions/{aid}/callback"
_PASTE = {"url": "http://127.0.0.1:5555/?code=a&state=b"}
_AUTH_URL = "https://accounts.google.com/o/oauth2/auth?x"


class _Relogin:
    """The app's GwsReloginManager, as far as the callback route reaches it."""

    def __init__(self) -> None:
        self.completed: list[tuple[str, str]] = []

    def complete_from_redirect(self, profile: str, url: str) -> dict[str, Any]:
        self.completed.append((profile, url))
        return {"status": "completed", "profile": profile}

    def status(self, profile: str) -> dict[str, Any]:
        return {"status": "completed", "profile": profile}

    def cancel(self, profile: str) -> dict[str, Any]:
        return {"ok": True, "cancelled": True, "profile": profile}


def _client(manager: ProjectChatManager, *, signed_in: bool = True) -> TestClient:
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[
            Route("/api/chats/{chat_id}/actions/{action_id}/start", chat_action_start, methods=["POST"]),
            Route("/api/chats/{chat_id}/actions/{action_id}/dismiss", chat_action_dismiss, methods=["POST"]),
            Route("/api/chats/{chat_id}/actions/{action_id}/continue", chat_action_continue, methods=["POST"]),
            Route("/api/chats/{chat_id}/actions/{action_id}/callback", chat_action_callback, methods=["POST"]),
        ],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = manager._config
    app.state.project_chat_manager = manager
    client = TestClient(app, base_url=_ORIGIN, raise_server_exceptions=False)
    if signed_in:
        client.cookies.set(SESSION_COOKIE, serializer.dumps({"user": "owner"}))
    return client


def _post(client: TestClient, cid: str, aid: str, body: dict[str, object]) -> Any:
    return client.post(_CALLBACK.format(cid=cid, aid=aid), json=body, headers=_HEADERS)


def _gws_card(manager: ProjectChatManager, *, waiting: bool = True) -> tuple[str, str, _Relogin]:
    """A chat with a ``gws_auth`` card, ``waiting`` on its consent URL unless ``waiting`` is false."""
    relogin = _Relogin()
    manager.actions_runtime.bind_app_state(SimpleNamespace(gws_relogin_manager=relogin))
    project = manager.create_project("Paste", workspace="work")
    chat = manager.create_chat(project.project_id, title="Paste")
    aid = str(manager.raise_action(chat.chat_id, "gws_auth", {"profile": "work"}, origin="engine")["id"])
    if waiting:
        manager.update_action(chat.chat_id, aid, state="waiting", params_update={"url": _AUTH_URL})
    return chat.chat_id, aid, relogin


@pytest.fixture
def manager(tmp_path: Path) -> ProjectChatManager:
    manager = make_manager(tmp_path)
    make_runtime(manager)
    return manager


def test_callback_hands_the_pasted_url_to_the_gws_manager(manager: ProjectChatManager) -> None:
    cid, aid, relogin = _gws_card(manager)

    response = _post(_client(manager), cid, aid, _PASTE)

    assert response.status_code == 200
    assert response.json()["action"]["state"] == "done"
    assert relogin.completed == [("work", _PASTE["url"])]


def test_not_waiting_is_400_invalid_state(manager: ProjectChatManager) -> None:
    cid, aid, relogin = _gws_card(manager, waiting=False)

    response = _post(_client(manager), cid, aid, _PASTE)

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_state"
    assert body["action"]["state"] == "open"
    assert relogin.completed == []


def test_a_kind_without_a_callback_is_400_unsupported_kind(manager: ProjectChatManager) -> None:
    project = manager.create_project("Command", workspace="work")
    chat = manager.create_chat(project.project_id, title="Command")
    aid = str(manager.raise_action(chat.chat_id, "command", {"command": "ls"}, origin="agent")["id"])
    manager.update_action(chat.chat_id, aid, state="waiting")

    response = _post(_client(manager), chat.chat_id, aid, _PASTE)

    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_kind"


def test_a_retired_mcp_auth_card_cannot_be_raised(manager: ProjectChatManager) -> None:
    from ciao.chat_actions import ChatActionError

    project = manager.create_project("Retired", workspace="work")
    chat = manager.create_chat(project.project_id, title="Retired")

    with pytest.raises(ChatActionError) as refused:
        manager.raise_action(
            chat.chat_id, "mcp_auth", {"server": "notion", "provider": "claude"}, origin="engine"
        )

    assert refused.value.reason == "invalid_kind"


def test_a_stale_id_is_409(manager: ProjectChatManager) -> None:
    cid, _, _ = _gws_card(manager)

    response = _post(_client(manager), cid, "no-such-action", _PASTE)

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "chat_action_conflict"
    assert body["actions"] == manager.chat_action_payloads(cid)


def test_unknown_chat_is_404(manager: ProjectChatManager) -> None:
    response = _post(_client(manager), "no-such-chat", "x", _PASTE)

    assert response.status_code == 404
    assert response.json() == {"error": "not found"}


def test_body_shape(manager: ProjectChatManager) -> None:
    cid, aid, relogin = _gws_card(manager)
    client = _client(manager)
    url = _CALLBACK.format(cid=cid, aid=aid)
    responses = [
        client.post(url, headers=_JSON_HEADERS, json={"url": 1}),
        client.post(url, headers=_JSON_HEADERS, json={"url": "x", "extra": 1}),
        client.post(url, headers=_JSON_HEADERS, content=b"[]"),
        client.post(url, headers=_JSON_HEADERS, content=b"not json"),
    ]
    for response in responses:
        assert response.status_code == 400
        assert response.json() == {"error": "invalid_body", "action": None}
    too_large = client.post(url, headers=_JSON_HEADERS, content=b"x" * 9000)
    assert too_large.status_code == 413
    assert too_large.json() == {"error": "too_large", "action": None}
    assert relogin.completed == []


def test_callback_route_requires_session(manager: ProjectChatManager) -> None:
    cid, aid, relogin = _gws_card(manager)

    response = _post(_client(manager, signed_in=False), cid, aid, _PASTE)

    assert response.status_code == 401
    assert relogin.completed == []


def test_the_pasted_code_is_not_logged(manager: ProjectChatManager, caplog: pytest.LogCaptureFixture) -> None:
    cid, aid, relogin = _gws_card(manager)
    caplog.set_level(logging.DEBUG)

    response = _post(_client(manager), cid, aid, {"url": "http://127.0.0.1:5555/?code=SECRETCODE&state=b"})

    assert response.status_code == 200
    assert relogin.completed == [("work", "http://127.0.0.1:5555/?code=SECRETCODE&state=b")]
    assert "SECRETCODE" not in caplog.text
