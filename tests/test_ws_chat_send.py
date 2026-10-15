"""ws_chat message handling: a dying socket must never drop the turn.

Regression test for the reported "page refreshed and removed my message"
bug. The handler used to write a `thinking` status to the socket BEFORE
starting the stream, so a client that disconnected right after sending its
message frame (WebKit suspension closes the socket in the same instant the
frame arrives) took the early `break` and `start_stream` was never called —
the turn silently vanished and the reconnecting client's history reload
wiped the optimistic bubble.

Also covers the idle keepalive: an open chat with no active stream used to
get no frames at all, so the PWA watchdog closed and reconnected the socket
every ~14 s. `_attach_streams` now sends `{"type": "keepalive"}` on the
STREAM_KEEPALIVE_SECONDS cadence.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from itsdangerous import URLSafeTimedSerializer

from ciao.web import routes_chat
from ciao.web.chat_broker import ChatStream
from ciao.web.routes_chat import ws_chat
from tests.session import signed_in


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
    app.state.serializer = URLSafeTimedSerializer("test-secret")
    return app


def test_message_starts_turn_even_when_socket_dies_immediately() -> None:
    started: list[tuple[str, str]] = []
    client = signed_in(TestClient(_app(started)))
    with client.websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_text('{"type":"message","text":"hello there"}')
        # The client dies right after the frame (suspension). Exiting the
        # context closes the socket before the handler can write anything.
    # The turn must still have been started server-side; the reconnecting
    # client replays the buffered user_echo from the broker stream.
    assert started == [("chat-1", "hello there")]


def test_message_acks_thinking_when_socket_stays_open() -> None:
    started: list[tuple[str, str]] = []
    client = signed_in(TestClient(_app(started)))
    with client.websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_text('{"type":"message","text":"hello there"}')
        assert ws.receive_json() == {"type": "status", "message": "thinking"}
    assert started == [("chat-1", "hello there")]


def test_empty_message_is_ignored_without_starting_a_stream() -> None:
    started: list[tuple[str, str]] = []
    client = signed_in(TestClient(_app(started)))
    with client.websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_text('{"type":"message","text":""}')
    assert started == []


def test_ws_message_queues_image_only_follow_up() -> None:
    """Blank text with images is a sendable message, not a silent drop.

    The blank-text check ran before the image refs were resolved, so an
    image-only frame never reached the queue or `start_stream` and the user's
    images were lost without a word (#1112).
    """
    queued: list[tuple[str, list[str]]] = []

    def resolve_image_ref(ref: str) -> SimpleNamespace:
        return SimpleNamespace(original_filename=ref)

    def queue_message(
        chat_id: str, text: str, images=None, entry_id=None, delivery="queue"
    ) -> bool:
        queued.append((text, [img.original_filename for img in images or []]))
        return True

    # A turn is in flight, so the send buffers into the pending queue.
    stream = ChatStream("chat-1")
    manager = SimpleNamespace(
        get_chat=lambda _cid: SimpleNamespace(archived=False),
        get_active_stream=lambda _cid: stream,
        resolve_image_ref=resolve_image_ref,
        queue_message=queue_message,
        start_stream=lambda *_a, **_k: None,
    )
    app = Starlette(routes=[WebSocketRoute("/ws/chat/{chat_id}", ws_chat)])
    app.state.project_chat_manager = manager
    app.state.focused_chats = {}
    app.state.serializer = URLSafeTimedSerializer("test-secret")

    client = signed_in(TestClient(app))
    with client.websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_text('{"type":"message","text":"","images":["shot.png"]}')

    assert queued == [("", ["shot.png"])], queued


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
    app.state.serializer = URLSafeTimedSerializer("test-secret")

    client = signed_in(TestClient(app))
    with client.websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_text('{"type":"message","text":"hello"}')
        assert ws.receive_json()["archived"] is True
    assert started == []


def test_idle_chat_socket_receives_keepalive(monkeypatch) -> None:
    """An idle chat must get frames, or the PWA watchdog calls it half-open."""
    monkeypatch.setattr(routes_chat, "STREAM_KEEPALIVE_SECONDS", 0.05)
    monkeypatch.setattr(routes_chat, "_ATTACH_POLL_SECONDS", 0.01)
    client = signed_in(TestClient(_app([])))
    with client.websocket_connect("/ws/chat/chat-1") as ws:
        assert ws.receive_json() == {"type": "keepalive"}
        # It repeats: the client stamps liveness on every keepalive.
        assert ws.receive_json() == {"type": "keepalive"}


async def test_attach_streams_ends_when_keepalive_send_fails(monkeypatch) -> None:
    """A dead socket ends the attach loop instead of spinning forever."""
    monkeypatch.setattr(routes_chat, "STREAM_KEEPALIVE_SECONDS", 0.05)
    monkeypatch.setattr(routes_chat, "_ATTACH_POLL_SECONDS", 0.01)

    class _DeadWebSocket:
        async def send_json(self, payload) -> None:
            raise WebSocketDisconnect()

    pcm = SimpleNamespace(get_active_stream=lambda _cid: None)
    await asyncio.wait_for(
        routes_chat._attach_streams(_DeadWebSocket(), pcm, "chat-1"), 2
    )


def _steer_app(calls: list, stream: ChatStream | None) -> Starlette:
    async def steer_queue(chat_id: str) -> None:
        calls.append(("steer_queue", chat_id))

    async def set_queue_delivery(chat_id: str, entry_ids, delivery) -> bool:
        calls.append(("set_queue_delivery", chat_id, entry_ids, delivery))
        return True

    def queue_message(chat_id, text, images=None, entry_id=None, delivery="queue") -> bool:
        calls.append(("queue_message", text, entry_id, delivery))
        return True

    manager = SimpleNamespace(
        get_chat=lambda _cid: SimpleNamespace(archived=False),
        get_active_stream=lambda _cid: stream,
        resolve_image_ref=lambda ref: None,
        queue_message=queue_message,
        steer_queue=steer_queue,
        set_queue_delivery=set_queue_delivery,
        start_stream=lambda *_a, **_k: None,
    )
    app = Starlette(routes=[WebSocketRoute("/ws/chat/{chat_id}", ws_chat)])
    app.state.project_chat_manager = manager
    app.state.focused_chats = {}
    app.state.serializer = URLSafeTimedSerializer("test-secret")
    return app


def test_ws_message_with_steer_delivery_steers() -> None:
    calls: list = []
    client = signed_in(TestClient(_steer_app(calls, ChatStream("chat-1"))))
    with client.websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_text('{"type":"message","text":"x","delivery":"steer","entry_id":"e1"}')
        ws.send_text('{"type":"message","text":"y","entry_id":"e2"}')
        ws.send_text('{"type":"message","text":"z","delivery":"bogus","entry_id":"e3"}')
    assert calls == [
        ("queue_message", "x", "e1", "steer"),
        ("steer_queue", "chat-1"),
        ("queue_message", "y", "e2", "queue"),
        ("queue_message", "z", "e3", "queue"),
    ]


def test_queue_set_delivery_frame() -> None:
    calls: list = []
    client = signed_in(TestClient(_steer_app(calls, None)))
    with client.websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_text('{"type":"queue_set_delivery","entry_ids":["e1"],"delivery":"steer"}')
    assert calls == [("set_queue_delivery", "chat-1", ["e1"], "steer")]


def test_queue_set_delivery_frame_rejects_malformed_input() -> None:
    calls: list = []
    client = signed_in(TestClient(_steer_app(calls, None)))
    frames = [
        '{"type":"queue_set_delivery","entry_ids":"e1","delivery":"steer"}',
        '{"type":"queue_set_delivery","entry_ids":[1,{"a":1}],"delivery":"steer"}',
        '{"type":"queue_set_delivery","entry_ids":["e1"],"delivery":"sometimes"}',
        '{"type":"queue_set_delivery","entry_ids":[],"delivery":"steer"}',
        '{"type":"queue_set_delivery","entry_ids":["' + "x" * 300 + '"],"delivery":"steer"}',
        '{"type":"queue_set_delivery","entry_ids":' + str(["e"] * 500).replace("'", '"') + ',"delivery":"steer"}',
        '{"type":"queue_set_delivery","delivery":"steer"}',
    ]
    with client.websocket_connect("/ws/chat/chat-1") as ws:
        for frame in frames:
            ws.send_text(frame)
    assert calls == []
