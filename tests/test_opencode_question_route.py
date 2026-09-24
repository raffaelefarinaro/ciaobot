"""Regression coverage for native question response WebSocket messages."""

from __future__ import annotations

from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient

from ciao.web.routes_chat import ws_chat


class _Manager:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def get_chat(self, _chat_id: str):
        return SimpleNamespace(archived=False)

    def get_active_stream(self, _chat_id: str):
        return None

    async def respond_question_async(self, _chat_id: str, **payload):
        self.calls.append(payload)
        return True


def _app(manager: _Manager) -> Starlette:
    app = Starlette(routes=[WebSocketRoute("/ws/chat/{chat_id}", ws_chat)])
    app.state.project_chat_manager = manager
    app.state.focused_chats = {}
    return app


def test_question_response_accepts_legacy_reply_action() -> None:
    manager = _Manager()
    with TestClient(_app(manager)).websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_json({
            "type": "question_response",
            "request_id": "form-1",
            "action": "reply",
            "answers": {"choice": ["yes"]},
        })
        result = ws.receive_json()

    assert result["ok"] is True
    assert result["state"] == "answered"
    assert manager.calls == [{
        "request_id": "form-1",
        "answers": {"choice": ["yes"]},
        "cancel": False,
        "submitted": True,
    }]


def test_question_response_accepts_legacy_cancel_action() -> None:
    manager = _Manager()
    with TestClient(_app(manager)).websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_json({
            "type": "question_response",
            "request_id": "form-1",
            "action": "cancel",
            "answers": {},
        })
        result = ws.receive_json()

    assert result["ok"] is True
    assert result["state"] == "cancelled"
    assert manager.calls[0]["cancel"] is True
    assert manager.calls[0]["submitted"] is False
