"""ws_chat message handling: a dying socket must never drop the turn.

Regression test for the reported "page refreshed and removed my message"
bug. The handler used to write a `thinking` status to the socket BEFORE
starting the stream, so a client that disconnected right after sending its
message frame (WebKit suspension closes the socket in the same instant the
frame arrives) took the early `break` and `start_stream` was never called —
the turn silently vanished and the reconnecting client's history reload
wiped the optimistic bubble.
"""

from __future__ import annotations

from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient

from ciao.web.routes_chat import ws_chat


def _app(started: list[tuple[str, str]]) -> Starlette:
    """A minimal app around ws_chat with a recording manager."""

    def start_stream(chat_id: str, text: str, images=None) -> None:
        started.append((chat_id, text))

    manager = SimpleNamespace(
        get_chat=lambda _cid: SimpleNamespace(archived=False),
        get_active_stream=lambda _cid: None,
        start_stream=start_stream,
    )
    app = Starlette(routes=[WebSocketRoute("/ws/chat/{chat_id}", ws_chat)])
    app.state.project_chat_manager = manager
    app.state.focused_chats = {}
    return app


def test_message_starts_turn_even_when_socket_dies_immediately() -> None:
    started: list[tuple[str, str]] = []
    client = TestClient(_app(started))
    with client.websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_text('{"type":"message","text":"hello there"}')
        # The client dies right after the frame (suspension). Exiting the
        # context closes the socket before the handler can write anything.
    # The turn must still have been started server-side; the reconnecting
    # client replays the buffered user_echo from the broker stream.
    assert started == [("chat-1", "hello there")]


def test_message_acks_thinking_when_socket_stays_open() -> None:
    started: list[tuple[str, str]] = []
    client = TestClient(_app(started))
    with client.websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_text('{"type":"message","text":"hello there"}')
        assert ws.receive_json() == {"type": "status", "message": "thinking"}
    assert started == [("chat-1", "hello there")]


def test_empty_message_is_ignored_without_starting_a_stream() -> None:
    started: list[tuple[str, str]] = []
    client = TestClient(_app(started))
    with client.websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_text('{"type":"message","text":""}')
    assert started == []


def test_archived_chat_is_rejected_without_starting_a_stream() ->  None:
    started: list[tuple[str, str]] = []

    def start_stream(chat_id: str, text: str, images=None) -> None:
        started.append((chat_id, text))

    manager = SimpleNamespace(
        get_chat=lambda _cid: SimpleNamespace(archived=True),
        get_active_stream=lambda _cid: None,
        start_stream=start_stream,
    )
    app = Starlette(routes=[WebSocketRoute("/ws/chat/{chat_id}", ws_chat)])
    app.state.project_chat_manager = manager
    app.state.focused_chats = {}

    client = TestClient(app)
    with client.websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_text('{"type":"message","text":"hello"}')
        assert ws.receive_json()["archived"] is True
    assert started == []
