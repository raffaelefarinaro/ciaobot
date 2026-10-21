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
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from itsdangerous import URLSafeTimedSerializer

from ciao.config import CiaoConfig
from ciao.models import ResultEvent
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web import routes_chat
from ciao.web.chat_broker import ChatStream
from ciao.web.project_chats import SEND_ID_HISTORY_TURNS, ProjectChatManager
from ciao.web.routes_chat import ws_chat
from tests.session import signed_in


def _app(started: list[tuple[str, str]]) -> Starlette:
    """A minimal app around ws_chat with a recording manager."""

    def start_stream(
        chat_id: str, text: str, images=None, *, send_id: str = ""
    ) -> None:
        started.append((chat_id, text))

    manager = SimpleNamespace(
        get_chat=lambda _cid: SimpleNamespace(archived=False),
        get_active_stream=lambda _cid: None,
        find_turn_by_send_id=lambda _cid, _sid: None,
        has_pending_send=lambda _cid, _sid: False,
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
        chat_id: str, text: str, images=None, entry_id=None, delivery="queue",
        send_id: str = "",
    ) -> bool:
        queued.append((text, [img.original_filename for img in images or []]))
        return True

    # A turn is in flight, so the send buffers into the pending queue.
    stream = ChatStream("chat-1")
    manager = SimpleNamespace(
        get_chat=lambda _cid: SimpleNamespace(archived=False),
        get_active_stream=lambda _cid: stream,
        resolve_image_ref=resolve_image_ref,
        find_turn_by_send_id=lambda _cid, _sid: None,
        has_pending_send=lambda _cid, _sid: False,
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

    def start_stream(chat_id: str, text: str, images=None, **kwargs) -> None:
        started.append((chat_id, text))

    manager = SimpleNamespace(
        get_chat=lambda _cid: SimpleNamespace(archived=True),
        get_active_stream=lambda _cid: None,
        find_turn_by_send_id=lambda _cid, _sid: None,
        has_pending_send=lambda _cid, _sid: False,
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

    def queue_message(chat_id, text, images=None, entry_id=None, delivery="queue", **kwargs) -> bool:
        calls.append(("queue_message", text, entry_id, delivery))
        return True

    manager = SimpleNamespace(
        get_chat=lambda _cid: SimpleNamespace(archived=False),
        get_active_stream=lambda _cid: stream,
        resolve_image_ref=lambda ref: None,
        find_turn_by_send_id=lambda _cid, _sid: None,
        has_pending_send=lambda _cid, _sid: False,
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


def test_queue_set_delivery_send_now_during_restart_drain_answers_server_restarting() -> None:
    from ciao.web.project_chats import RestartDrainingError

    calls: list = []
    app = _steer_app(calls, None)

    async def draining(chat_id, entry_ids, delivery):
        raise RestartDrainingError()

    app.state.project_chat_manager.set_queue_delivery = draining
    client = signed_in(TestClient(app))
    with client.websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_text('{"type":"queue_set_delivery","entry_ids":["e1"],"delivery":"steer"}')
        assert ws.receive_json()["type"] == "server_restarting"


def test_queue_set_delivery_failure_is_an_error_frame_not_a_crash() -> None:
    calls: list = []
    app = _steer_app(calls, None)

    async def boom(chat_id, entry_ids, delivery):
        raise ValueError("This chat has been archived.")

    app.state.project_chat_manager.set_queue_delivery = boom
    client = signed_in(TestClient(app))
    with client.websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_text('{"type":"queue_set_delivery","entry_ids":["e1"],"delivery":"steer"}')
        assert ws.receive_json() == {"type": "error", "message": "This chat has been archived."}


# ── Idempotent sends: one logical send, one turn ─────────────────────────
#
# A recovery resend of a message the engine already owns used to start a
# second turn: the client could not prove delivery, and nothing on the wire
# told the server the frame was a repeat. The client now mints a send id per
# logical send and puts it on the frame; the engine records it per user turn
# and answers a repeat with a duplicate echo.


def _duplicate_app(started: list[dict]) -> Starlette:
    """A manager that records the sends and owns one recorded turn."""

    turns: dict[str, dict] = {}

    def find_turn_by_send_id(chat_id: str, send_id: str):
        return turns.get(send_id)

    def start_stream(chat_id: str, text: str, images=None, send_id: str = "") -> None:
        started.append({"chat_id": chat_id, "text": text, "send_id": send_id})
        if send_id:
            turns[send_id] = {"turn_index": 7, "sent_at": "2026-10-20T12:20:40Z"}

    manager = SimpleNamespace(
        get_chat=lambda _cid: SimpleNamespace(archived=False, user_turn_origin={}),
        get_active_stream=lambda _cid: None,
        find_turn_by_send_id=find_turn_by_send_id,
        has_pending_send=lambda _cid, _sid: False,
        start_stream=start_stream,
    )
    app = Starlette(routes=[WebSocketRoute("/ws/chat/{chat_id}", ws_chat)])
    app.state.project_chat_manager = manager
    app.state.focused_chats = {}
    app.state.serializer = URLSafeTimedSerializer("test-secret")
    return app


def test_duplicate_send_id_does_not_start_a_second_turn() -> None:
    started: list[dict] = []
    client = signed_in(TestClient(_duplicate_app(started)))
    with client.websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_text('{"type":"message","text":"hello","send_id":"s-1"}')
        assert ws.receive_json() == {"type": "status", "message": "thinking"}
        # The same logical send arrives a second time (a recovery resend).
        ws.send_text('{"type":"message","text":"hello","send_id":"s-1"}')
        echo = ws.receive_json()

    # Only the first frame started a turn.
    assert len(started) == 1
    assert started[0]["send_id"] == "s-1"
    # The second one is answered with a duplicate echo naming the turn the
    # send id already owns.
    assert echo["type"] == "user_echo"
    assert echo["duplicate"] is True
    assert echo["send_id"] == "s-1"
    assert echo["turn_index"] == 7


def test_every_send_frame_is_logged_with_its_send_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A send frame leaves a trace: who sent what, and was it a duplicate.

    Without it, a duplicated turn cannot be attributed to a frame afterwards:
    the ws frames were never written down.
    """
    started: list[dict] = []
    with caplog.at_level(logging.DEBUG, logger="ciao.web.routes_chat"):
        client = signed_in(TestClient(_duplicate_app(started)))
        with client.websocket_connect("/ws/chat/chat-1") as ws:
            ws.send_text('{"type":"message","text":"hello there","send_id":"s-1"}')
            assert ws.receive_json() == {"type": "status", "message": "thinking"}

    send_lines = [
        rec.getMessage() for rec in caplog.records
        if rec.name == "ciao.web.routes_chat" and "ws send" in rec.getMessage()
    ]
    assert len(send_lines) == 1, caplog.records
    line = send_lines[0]
    assert "chat-1" in line, line
    assert "s-1" in line, line
    # The text length, not the text: the message itself never reaches a log.
    assert "len=11" in line, line
    assert "duplicate=False" in line, line
    assert "hello there" not in line, line


def test_a_send_id_with_unsafe_characters_is_sanitized(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A send id is written to a log line and to chat metadata.

    It is therefore reduced to a token alphabet: whitespace and control
    characters are stripped, so no log record can be forged with a newline
    and the id the engine records is the sanitized one.
    """
    started: list[dict] = []
    with caplog.at_level(logging.DEBUG, logger="ciao.web.routes_chat"):
        client = signed_in(TestClient(_duplicate_app(started)))
        with client.websocket_connect("/ws/chat/chat-1") as ws:
            ws.send_text('{"type":"message","text":"hello","send_id":"a b\\nc"}')
            assert ws.receive_json() == {"type": "status", "message": "thinking"}

    assert started[0]["send_id"] == "abc", started
    # No log record carries a newline out of the id.
    for rec in caplog.records:
        assert "\n" not in rec.getMessage(), rec.getMessage()
    send_lines = [
        rec.getMessage() for rec in caplog.records
        if rec.name == "ciao.web.routes_chat" and "ws send" in rec.getMessage()
    ]
    assert len(send_lines) == 1, caplog.records
    assert "abc" in send_lines[0], send_lines[0]


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )


async def _wait_for(predicate, timeout: float = 2.0, step: float = 0.01) -> None:
    """Poll `predicate()` until True or timeout (fails the test on timeout)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(step)
    raise AssertionError(f"timed out waiting for predicate {predicate!r}")


async def test_send_id_survives_a_metadata_reload(tmp_path: Path) -> None:
    """The recorded send id is engine state, not request state.

    A recovery resend arrives after the engine wrote its metadata to disk, so
    the id has to be found again from the reloaded registry — otherwise the
    repeat starts a second turn.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("sendids", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="send-id")

    pcm.start_stream(chat.chat_id, "hello", send_id="s-1")
    found = pcm.find_turn_by_send_id(chat.chat_id, "s-1")
    assert found is not None
    assert found["turn_index"] == 0

    # A fresh manager over the same registry, as an engine restart would.
    config = pcm._config
    reloaded = ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, config.workspace_root, config.media_root),
        transcript_store=TranscriptStore(
            Path(config.state_path).parent,
            Path(config.state_path).parent / "transcripts",
        ),
        path=pcm._path,
    )
    found_again = reloaded.find_turn_by_send_id(chat.chat_id, "s-1")
    assert found_again is not None
    assert found_again["turn_index"] == 0
    # An unknown id owns no turn.
    assert reloaded.find_turn_by_send_id(chat.chat_id, "s-unknown") is None


async def test_the_send_id_history_is_bounded(tmp_path: Path) -> None:
    """The recorded send ids are a recent window, not the whole chat.

    The map exists to recognise a duplicate resend, and the PWA stops
    resending a send older than 30 s, so dedup only needs the recent
    sends. Without a bound the map grew with every turn of every chat,
    and it is persisted with the chat metadata.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("sendids", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="send-id-window")
    # The turn is recorded by the send path; the provider drive is not what
    # this test is about.
    pcm._streaming.start_drive = lambda **_kwargs: None

    total = SEND_ID_HISTORY_TURNS + 6
    for index in range(total):
        stream = pcm.start_stream(chat.chat_id, f"turn {index}", send_id=f"s-{index}")
        # The next send must open a new turn, so the finished stream is not
        # reused by the broker.
        stream.finish()

    chat_info = pcm.get_chat(chat.chat_id)
    assert chat_info is not None
    assert len(chat_info.user_turn_send_ids) == SEND_ID_HISTORY_TURNS
    # The oldest turns are gone; the newest is still found.
    assert pcm.find_turn_by_send_id(chat.chat_id, "s-0") is None
    found = pcm.find_turn_by_send_id(chat.chat_id, f"s-{total - 1}")
    assert found is not None
    assert found["turn_index"] == total - 1


async def test_a_queued_flush_prunes_the_send_id_history(tmp_path: Path) -> None:
    """The queued follow-up path prunes the send-id map as well.

    A queued message flushes into a turn of its own, and that turn records
    the client's send id. The write goes through the same bounded map a
    direct send uses, so a chat driven by queued messages is bounded too —
    and a key that names no turn is dropped on the way.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("sendids", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="send-id-queued")
    chat_info = pcm.get_chat(chat.chat_id)
    assert chat_info is not None
    # A map at its bound: 64 recorded ids, plus a key that names no turn.
    chat_info.user_turn_send_ids = {
        **{str(index): f"s-{index}" for index in range(SEND_ID_HISTORY_TURNS)},
        "not-a-turn": "s-bad",
    }
    # The next turn is the one after the recorded ones.
    chat_info.user_turn_count = SEND_ID_HISTORY_TURNS

    first_turn_ready = asyncio.Event()
    turn_calls: list[str] = []

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        """Fake provider: block the first turn until the test releases it."""
        turn_calls.append(prompt)
        if len(turn_calls) == 1:
            await first_turn_ready.wait()
        yield ResultEvent(
            type="result",
            result="assistant answer",
            session_id="sess-sid",
            is_error=False,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    captured: list[dict] = []

    async def consume(stream) -> None:
        async for ev in stream.subscribe():
            captured.append(ev)

    stream = pcm.start_stream(chat.chat_id, "initial")
    consumer = asyncio.create_task(consume(stream))
    await _wait_for(
        lambda: any(e.get("type") == "user_echo" for e in captured),
        timeout=2.0,
    )
    assert pcm.queue_message(chat.chat_id, "queued one", send_id="s-queued") is True
    first_turn_ready.set()
    await asyncio.wait_for(consumer, timeout=5.0)

    # The flushed turn owns the send id: a repeat of that frame is a
    # duplicate, not a new turn.
    found = pcm.find_turn_by_send_id(chat.chat_id, "s-queued")
    assert found is not None, "the flushed turn must record the queued send id"

    # And the map is still the newest 64 turns: the flushed turn's id is
    # in, the oldest recorded one is out, and the malformed key is gone.
    assert set(chat_info.user_turn_send_ids) == {
        *(str(index) for index in range(1, SEND_ID_HISTORY_TURNS)),
        str(SEND_ID_HISTORY_TURNS + 1),
    }


async def test_a_key_that_is_not_a_turn_owns_no_send(tmp_path: Path) -> None:
    """Only a decimal turn index names a turn.

    The engine writes decimal turn indices, and a key that is not one does
    not name a turn, so it cannot own a send id — and looking it up must
    not raise either. The persisted metadata can carry such a key, and the
    next send prunes it away.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("sendids", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="send-id-keys")
    chat_info = pcm.get_chat(chat.chat_id)
    assert chat_info is not None
    chat_info.user_turn_send_ids = {"not-a-turn": "s-1", "5": "s-5"}
    pcm._save()

    # A fresh manager over the same registry, as an engine restart would:
    # the malformed key is persisted metadata, not just in-memory state.
    config = pcm._config
    reloaded = ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, config.workspace_root, config.media_root),
        transcript_store=TranscriptStore(
            Path(config.state_path).parent,
            Path(config.state_path).parent / "transcripts",
        ),
        path=pcm._path,
    )
    reloaded_chat = reloaded.get_chat(chat.chat_id)
    assert reloaded_chat is not None
    assert "not-a-turn" in reloaded_chat.user_turn_send_ids

    # The lookup ignores a key that names no turn.
    assert reloaded.find_turn_by_send_id(chat.chat_id, "s-1") is None
    found = reloaded.find_turn_by_send_id(chat.chat_id, "s-5")
    assert found is not None
    assert found["turn_index"] == 5

    # One real send records its own id and prunes the malformed key.
    reloaded.start_stream(chat.chat_id, "hello", send_id="s-2")
    assert "not-a-turn" not in reloaded_chat.user_turn_send_ids
    assert reloaded_chat.user_turn_send_ids == {"0": "s-2", "5": "s-5"}


async def test_the_newest_turn_wins_when_a_send_id_is_recorded_twice(
    tmp_path: Path,
) -> None:
    """A send id on two turns is owned by the newer one.

    The engine records the id per turn, so the last turn carrying it is the
    one a repeated frame would duplicate. The older turn comes first in the
    map, so a scan that stopped at the first match would answer with it —
    and with the wrong send time.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("sendids", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="send-id-twice")
    chat_info = pcm.get_chat(chat.chat_id)
    assert chat_info is not None
    chat_info.user_turn_send_ids = {"0": "s-1", "3": "s-1"}
    chat_info.user_turn_timings = {
        "0": {"sent_at": "2026-10-22T09:00:00Z"},
        "3": {"sent_at": "2026-10-22T10:00:00Z"},
    }

    found = pcm.find_turn_by_send_id(chat.chat_id, "s-1")
    assert found is not None
    assert found["turn_index"] == 3
    # The send time is the newer turn's, not the first match's.
    assert found["sent_at"] == "2026-10-22T10:00:00Z"


async def test_the_history_row_does_not_carry_the_send_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The history row must not claim the client's send id.

    The id marks the client's OWN optimistic bubble, so a recovery can tell
    it from the server's copy of the same turn. A history row that carried
    the id would be indistinguishable from the optimistic copy, and the
    delivery proof would never fire.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("sendids", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="send-id")
    pcm.start_stream(chat.chat_id, "hello", send_id="s-1")
    chat.session_id = "sess-a"
    pcm._save()
    monkeypatch.setitem(
        sys.modules,
        "claude_agent_sdk",
        SimpleNamespace(
            get_session_messages=lambda session_id, directory: [
                SimpleNamespace(
                    type="user",
                    message={"role": "user", "content": "hello"},
                ),
            ]
        ),
    )

    from ciao.web import transcript_service

    rows = await transcript_service._assemble_chat_messages(
        pcm, pcm._config, pcm.get_chat(chat.chat_id)
    )
    user_rows = [r for r in rows if r.get("role") == "user"]
    assert user_rows, rows
    assert not any(r.get("send_id") for r in user_rows), rows


def test_a_duplicate_send_id_is_not_queued_twice() -> None:
    """With a turn in flight, two frames with one send id leave one entry.

    The duplicate check belongs to the route, not to the queue: the route
    sees the pending id before it queues anything, so the second frame is
    answered with a duplicate echo and never reaches the queue. `start_stream`
    is never reached either — a second turn for one logical send is the
    duplicate this issue is about.
    """
    queued_ids: list[str] = []
    started: list[dict] = []

    def queue_message(
        chat_id: str, text: str, images=None, entry_id=None, delivery="queue",
        send_id: str = "",
    ) -> bool:
        queued_ids.append(send_id)
        return True

    def start_stream(chat_id: str, text: str, images=None, send_id: str = "") -> None:
        started.append({"chat_id": chat_id, "text": text, "send_id": send_id})

    manager = SimpleNamespace(
        get_chat=lambda _cid: SimpleNamespace(archived=False),
        get_active_stream=lambda _cid: ChatStream("chat-1"),
        resolve_image_ref=lambda ref: None,
        find_turn_by_send_id=lambda _cid, _sid: None,
        has_pending_send=lambda _cid, send_id: send_id in queued_ids,
        queue_message=queue_message,
        start_stream=start_stream,
    )
    app = Starlette(routes=[WebSocketRoute("/ws/chat/{chat_id}", ws_chat)])
    app.state.project_chat_manager = manager
    app.state.focused_chats = {}
    app.state.serializer = URLSafeTimedSerializer("test-secret")

    client = signed_in(TestClient(app))
    with client.websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_text('{"type":"message","text":"hello","send_id":"s-1"}')
        # The same logical send arrives a second time (a recovery resend).
        ws.send_text('{"type":"message","text":"hello","send_id":"s-1"}')
        echo = ws.receive_json()

    # Only one queue entry: the second frame was refused at the route.
    assert queued_ids == ["s-1"], queued_ids
    # The second frame is answered with the duplicate echo.
    assert echo["type"] == "user_echo"
    assert echo["duplicate"] is True
    assert echo["send_id"] == "s-1"
    # And no turn was started for it.
    assert started == [], started


# ── Retry origin: the client may tag its own retry ───────────────────────
#
# A deliberate retry of a failed turn is still the user's message, but the
# PWA draws it as a notice instead of a bubble. The tag rides on the frame as
# `origin: "retry"`: it is presentation metadata, and no engine origin
# (schedule, background_run, …) can be forged from the wire.


def _origin_app(calls: list, stream: ChatStream | None = None) -> Starlette:
    """A manager that records what the send path was handed."""

    def start_stream(chat_id: str, text: str, images=None, **kwargs) -> None:
        calls.append(("start_stream", text, dict(kwargs)))

    def queue_message(chat_id: str, text: str, images=None, **kwargs) -> bool:
        calls.append(("queue_message", text, dict(kwargs)))
        return True

    async def steer_queue(chat_id: str) -> None:
        calls.append(("steer_queue", chat_id, {}))

    manager = SimpleNamespace(
        get_chat=lambda _cid: SimpleNamespace(archived=False, user_turn_origin={}),
        get_active_stream=lambda _cid: stream,
        resolve_image_ref=lambda ref: None,
        find_turn_by_send_id=lambda _cid, _sid: None,
        has_pending_send=lambda _cid, _sid: False,
        queue_message=queue_message,
        steer_queue=steer_queue,
        start_stream=start_stream,
    )
    app = Starlette(routes=[WebSocketRoute("/ws/chat/{chat_id}", ws_chat)])
    app.state.project_chat_manager = manager
    app.state.focused_chats = {}
    app.state.serializer = URLSafeTimedSerializer("test-secret")
    return app


def test_a_retry_tagged_send_starts_a_retry_tagged_turn() -> None:
    calls: list = []
    client = signed_in(TestClient(_origin_app(calls)))
    with client.websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_text('{"type":"message","text":"try again","origin":"retry"}')
    assert calls == [
        ("start_stream", "try again", {"send_id": "", "origin": "retry"}),
    ], calls


def test_an_ordinary_send_passes_no_origin() -> None:
    """An untagged send is untouched: the route adds nothing to the call."""
    calls: list = []
    client = signed_in(TestClient(_origin_app(calls)))
    with client.websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_text('{"type":"message","text":"hello"}')
    assert calls == [("start_stream", "hello", {"send_id": ""})], calls


def test_no_origin_other_than_retry_is_accepted_from_the_wire() -> None:
    """The client may not forge an engine origin.

    ``schedule``/``background_run`` and friends say the ENGINE wrote the turn;
    a client that could set them could make its own message look like an
    automation notice. Only the exact "retry" value is honoured; every other
    value (including a near miss) is ignored, as any other extra field is.
    """
    forged_values = [
        "schedule", "background_run", "cli_task_wake", "task_delegation",
        "task_resume", "webhook", "update_task", "cron", "RETRY", "Retry",
        "retry ", 5, True, ["retry"], {"origin": "retry"},
    ]
    for forged in forged_values:
        calls: list = []
        client = signed_in(TestClient(_origin_app(calls)))
        with client.websocket_connect("/ws/chat/chat-1") as ws:
            ws.send_text(
                json.dumps({"type": "message", "text": "hello", "origin": forged})
            )
        assert calls == [("start_stream", "hello", {"send_id": ""})], forged


def test_a_retry_tagged_send_is_queued_with_its_origin() -> None:
    """A retry sent while a turn runs keeps its tag in the queue."""
    calls: list = []
    client = signed_in(TestClient(_origin_app(calls, ChatStream("chat-1"))))
    with client.websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_text('{"type":"message","text":"try again","origin":"retry"}')
    assert calls == [
        (
            "queue_message",
            "try again",
            {"entry_id": None, "delivery": "queue", "send_id": "", "origin": "retry"},
        ),
    ], calls


def test_a_retry_tagged_steer_is_queued_with_its_origin() -> None:
    calls: list = []
    client = signed_in(TestClient(_origin_app(calls, ChatStream("chat-1"))))
    with client.websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_text(
            '{"type":"message","text":"try again","origin":"retry","delivery":"steer"}'
        )
    assert calls == [
        (
            "queue_message",
            "try again",
            {"entry_id": None, "delivery": "steer", "send_id": "", "origin": "retry"},
        ),
        ("steer_queue", "chat-1", {}),
    ], calls


def test_an_archived_chat_refuses_a_retry_tagged_send() -> None:
    """The tag buys nothing: an archived chat is still refused."""
    calls: list = []
    app = _origin_app(calls)
    app.state.project_chat_manager.get_chat = lambda _cid: SimpleNamespace(
        archived=True, user_turn_origin={}
    )
    client = signed_in(TestClient(app))
    with client.websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_text('{"type":"message","text":"try again","origin":"retry"}')
        frame = ws.receive_json()
    assert frame["type"] == "error"
    assert frame["archived"] is True
    assert calls == [], calls


def _duplicate_origin_app(
    calls: list,
    recorded_origin: str = "",
    owned: bool = True,
    pending: bool = False,
) -> Starlette:
    """A manager that owns a recorded turn, and the chat meta that records it."""
    turns: dict[str, dict] = {
        "s-1": {"turn_index": 7, "sent_at": "2026-10-20T12:20:40Z"}
    }
    origins = {"7": recorded_origin} if recorded_origin else {}

    def find_turn_by_send_id(chat_id: str, send_id: str):
        return turns.get(send_id) if owned else None

    def start_stream(chat_id: str, text: str, images=None, **kwargs) -> None:
        calls.append(("start_stream", text, dict(kwargs)))

    manager = SimpleNamespace(
        get_chat=lambda _cid: SimpleNamespace(archived=False, user_turn_origin=origins),
        get_active_stream=lambda _cid: None,
        find_turn_by_send_id=find_turn_by_send_id,
        has_pending_send=lambda _cid, _sid: pending,
        start_stream=start_stream,
    )
    app = Starlette(routes=[WebSocketRoute("/ws/chat/{chat_id}", ws_chat)])
    app.state.project_chat_manager = manager
    app.state.focused_chats = {}
    app.state.serializer = URLSafeTimedSerializer("test-secret")
    return app


@pytest.mark.parametrize("recorded_origin", ["retry", "background_run"])
def test_a_duplicate_send_echoes_the_recorded_origin(recorded_origin: str) -> None:
    """A repeat of a retry turn repeats the tag the engine recorded.

    The client that reconnects after a recovery resend must not lose the
    notice it was told about.
    """
    calls: list = []
    client = signed_in(TestClient(_duplicate_origin_app(calls, recorded_origin)))
    with client.websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_text('{"type":"message","text":"try again","send_id":"s-1","origin":"retry"}')
        echo = ws.receive_json()
    assert echo["type"] == "user_echo"
    assert echo["duplicate"] is True
    assert echo["turn_index"] == 7
    assert echo["origin"] == recorded_origin
    # And the repeat started no second turn.
    assert calls == [], calls


def test_a_duplicate_send_does_not_take_the_origin_from_the_frame() -> None:
    """A repeat cannot tag a turn the engine never tagged."""
    calls: list = []
    client = signed_in(TestClient(_duplicate_origin_app(calls, "")))
    with client.websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_text(
            '{"type":"message","text":"try again","send_id":"s-1","origin":"retry"}'
        )
        echo = ws.receive_json()
    assert echo["duplicate"] is True
    assert "origin" not in echo
    assert calls == [], calls


def test_a_pending_duplicate_is_not_tagged_as_a_retry() -> None:
    """A queued send owns no turn yet, so it has no tag to repeat.

    The eventual echo and the history carry the authoritative one.
    """
    calls: list = []
    client = signed_in(TestClient(_duplicate_origin_app(calls, "", owned=False, pending=True)))
    with client.websocket_connect("/ws/chat/chat-1") as ws:
        ws.send_text(
            '{"type":"message","text":"try again","send_id":"s-1","origin":"retry"}'
        )
        echo = ws.receive_json()
    assert echo["duplicate"] is True
    assert "origin" not in echo
    assert calls == [], calls
