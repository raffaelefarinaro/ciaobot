"""V2-only regression coverage for native question response WebSockets."""

from __future__ import annotations

from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient

from ciao.providers.opencode import QuestionResponseResult
from ciao.web.routes_chat import ws_chat
from itsdangerous import URLSafeTimedSerializer
from tests.session import signed_in


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

    async def respond_permission(self, _chat_id: str, **payload):
        self.calls.append(payload)
        return QuestionResponseResult(True)


def _app(manager: _Manager) -> Starlette:
    app = Starlette(routes=[WebSocketRoute("/ws/chat/{chat_id}", ws_chat)])
    app.state.project_chat_manager = manager
    app.state.focused_chats = {}
    app.state.serializer = URLSafeTimedSerializer("test-secret")
    return app


def test_question_response_forwards_the_v2_reply_shape() -> None:
    manager = _Manager()
    with signed_in(TestClient(_app(manager))).websocket_connect("/ws/chat/chat-1") as ws:
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
    with signed_in(TestClient(_app(manager))).websocket_connect("/ws/chat/chat-1") as ws:
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
    with signed_in(TestClient(_app(manager))).websocket_connect("/ws/chat/chat-1") as ws:
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


def test_permission_response_requires_a_json_boolean() -> None:
    manager = _Manager()
    with signed_in(TestClient(_app(manager))).websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_json({
            "type": "permission_response",
            "request_id": "permission-1",
            "approved": "false",
        })
        result = ws.receive_json()

    assert result["ok"] is False
    assert result["retryable"] is False
    assert "JSON boolean" in result["error"]
    assert manager.calls == []


def test_permission_response_forwards_a_boolean_verdict_and_session() -> None:
    manager = _Manager()
    with signed_in(TestClient(_app(manager))).websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_json({
            "type": "permission_response",
            "request_id": "permission-1",
            "session_id": "ses_1",
            "approved": False,
            "reason": "User denied",
        })
        result = ws.receive_json()

    assert result["ok"] is True
    assert result["session_id"] == "ses_1"
    assert manager.calls == [{
        "request_id": "permission-1",
        "session_id": "ses_1",
        "approved": False,
        "reason": "User denied",
    }]


def test_answering_a_native_card_asks_the_control_plane_to_resettle() -> None:
    """The answer path re-settles a held attempt (#1110).

    Answering a native card starts no turn, so the route itself has to hand the
    chat to the control plane once the card is acknowledged. The plane is reached
    off ``app.state.mcp_service``; a fake records the one call.
    """
    manager = _Manager()
    resettled: list[str] = []
    app = _app(manager)
    app.state.mcp_service = SimpleNamespace(
        control_plane=SimpleNamespace(
            resettle_after_question_answer=lambda chat_id: resettled.append(chat_id) or True
        )
    )
    with signed_in(TestClient(app)).websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_json({
            "type": "question_response",
            "request_id": "form-1",
            "action": "reply",
            "answers": {"choice": ["yes"]},
        })
        ws.receive_json()

    assert resettled == ["chat-1"]


def test_question_response_still_answers_when_no_control_plane_is_bound() -> None:
    """Bootstrap has no control plane; the answer must not fail on its absence."""
    manager = _Manager()
    with signed_in(TestClient(_app(manager))).websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_json({
            "type": "question_response",
            "request_id": "form-1",
            "action": "reply",
            "answers": {"choice": ["yes"]},
        })
        result = ws.receive_json()

    assert result["ok"] is True
