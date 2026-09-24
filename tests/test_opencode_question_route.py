"""V2-only regression coverage for native question response WebSockets."""

from __future__ import annotations

from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient

from ciao.providers.opencode import QuestionResponseResult
from ciao.web.routes_chat import ws_chat


class _Manager:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def get_chat(self, _chat_id: str):
        return SimpleNamespace(archived=False)

    def get_active_stream(self, _chat_id: str):
        return None

    async def respond_question(self, _chat_id: str, **payload):
        self.calls.append(payload)
        return QuestionResponseResult(True)


def _app(manager: _Manager) -> Starlette:
    app = Starlette(routes=[WebSocketRoute("/ws/chat/{chat_id}", ws_chat)])
    app.state.project_chat_manager = manager
    app.state.focused_chats = {}
    return app


def test_question_response_forwards_the_v2_reply_shape() -> None:
    manager = _Manager()
    with TestClient(_app(manager)).websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_json({
            "type": "question_response",
            "request_id": "form-1",
            "action": "reply",
            "answers": {"choice": ["yes"], "count": [3]},
        })
        result = ws.receive_json()

    assert result == {
        "type": "question_response_result",
        "request_id": "form-1",
        "ok": True,
        "state": "answered",
        "error": "",
        "retryable": True,
    }
    assert manager.calls == [{
        "request_id": "form-1",
        "answers": {"choice": ["yes"], "count": ["3"]},
        "action": "reply",
    }]


def test_question_response_forwards_the_v2_cancel_shape() -> None:
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
    assert manager.calls == [{
        "request_id": "form-1",
        "answers": {},
        "action": "cancel",
    }]


def test_question_response_rejects_a_missing_or_unknown_action() -> None:
    manager = _Manager()
    with TestClient(_app(manager)).websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_json({
            "type": "question_response",
            "request_id": "form-1",
            "answers": {"choice": ["yes"]},
        })
        result = ws.receive_json()

    assert result["ok"] is False
    assert result["state"] == "rejected"
    assert result["retryable"] is False
    assert manager.calls == []
