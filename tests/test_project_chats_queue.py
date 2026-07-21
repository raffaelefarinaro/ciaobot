"""Coverage for the queued-message flush path in `_drive()`.

Context: while a turn is streaming, a user can type more messages — they get
queued via `ProjectChatManager.queue_message()` and flushed one at a time as
separate follow-up turns once the first one finishes. This matches the PWA
request to send queued messages individually and to be able to reorder or edit
them before they go out.

These tests pin the server-side contract so any regression on the publish
side is caught before it reaches the PWA.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ciao.config import CiaoConfig
from ciao.models import ResultEvent, ToolUseEvent
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.chat_broker import ChatStream
from ciao.web.project_chats import ProjectChatManager


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    """Build a ProjectChatManager backed by tmp_path-only stores."""
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
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


async def test_queued_messages_flush_one_at_a_time(tmp_path: Path) -> None:
    """Two messages queued mid-turn must flush as two separate turns."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-queue", workspace="work")
    # Non-default title so the auto-title side-effect task doesn't spawn.
    chat = pcm.create_chat(project.project_id, title="queue-test")

    first_turn_ready = asyncio.Event()
    turn_calls: list[str] = []

    async def fake_stream_chat(chat_id, prompt, images=None):
        """Fake provider: block the first turn until the test releases it."""
        turn_calls.append(prompt)
        if len(turn_calls) == 1:
            await first_turn_ready.wait()
        yield ResultEvent(
            type="result",
            result="assistant answer",
            session_id="sess-x",
            is_error=False,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    # Monkeypatch the instance-level stream_chat so `_drive()` consumes the fake.
    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    captured: list[dict] = []

    async def consume(stream) -> None:
        async for ev in stream.subscribe():
            captured.append(ev)

    stream = pcm.start_stream(chat.chat_id, "initial")
    consumer = asyncio.create_task(consume(stream))

    # Let _drive() kick off and the subscriber register + replay the buffer.
    await _wait_for(
        lambda: any(e.get("type") == "user_echo" for e in captured),
        timeout=2.0,
    )

    # Queue two messages while the first turn is still blocked. This must
    # succeed (broker.get() returns non-None) — otherwise the flush path
    # doesn't even get exercised.
    assert pcm.queue_message(chat.chat_id, "msg A") is True
    assert pcm.queue_message(chat.chat_id, "msg B") is True

    # Release the first turn; _drive will drain pending one by one.
    first_turn_ready.set()

    await asyncio.wait_for(consumer, timeout=5.0)

    # Three stream_chat invocations expected: initial prompt + two follow-ups.
    assert len(turn_calls) == 3, f"expected 3 turns, got {turn_calls!r}"
    assert turn_calls[0] == "initial"
    assert turn_calls[1] == "msg A"
    assert turn_calls[2] == "msg B"

    echoes = [e for e in captured if e.get("type") == "user_echo"]
    assert len(echoes) == 3, (
        f"expected 3 user_echo events (initial + two follow-ups), got {echoes!r}"
    )

    # Initial echo carries the initial prompt and turn_index 0.
    assert echoes[0]["text"] == "initial"
    assert echoes[0].get("turn_index") == 0

    # Each queued follow-up gets its own echo and its own turn_index.
    assert echoes[1]["text"] == "msg A"
    assert echoes[1].get("turn_index") == 1
    assert echoes[2]["text"] == "msg B"
    assert echoes[2].get("turn_index") == 2


def test_question_notification_prefers_text_prompt_alias(tmp_path: Path) -> None:
    """Alternate AskUserQuestion shape uses `text` instead of `question`.

    MiniMax (Claude path) has been observed to emit
    ``{"text": "...", "type": "single_select", "options": [...]}``. Without
    accepting ``text``, the push body falls through to the raw JSON blob.
    """
    pcm = _make_manager(tmp_path)
    bodies: list[str] = []
    pcm.notify_question_cb = lambda _chat_id, body: bodies.append(body)

    payload = json.dumps(
        {
            "questions": [
                {
                    "text": "How do you want to handle the booking form?",
                    "type": "single_select",
                    "options": [{"label": "A. Link manually", "value": "manual"}],
                },
                {
                    "question": "Which guests first?",
                    "options": [{"label": "All Yes"}],
                },
            ]
        },
        ensure_ascii=False,
    )
    pcm._notify_question("chat-x", payload)

    assert bodies == [
        "How do you want to handle the booking form?\nWhich guests first?"
    ]


async def test_ask_user_question_pauses_turn_without_draining_as_queued(tmp_path: Path) -> None:
    """AskUserQuestion should stop the active turn so the user's answer starts
    the next turn immediately instead of sitting in the queued-message buffer.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-question", workspace="work")
    chat = pcm.create_chat(project.project_id, title="question-test")

    async def fake_stream_chat(chat_id, prompt, images=None):
        yield ToolUseEvent(
            type="assistant",
            tool_name="AskUserQuestion",
            tool_input="",
        )
        yield ToolUseEvent(
            type="assistant",
            tool_name="AskUserQuestion",
            tool_input='{"questions":[{"question":"Which email?"}]}',
        )
        yield ResultEvent(
            type="result",
            result="kept working after the question",
            session_id="sess-q",
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
    await asyncio.wait_for(consume(stream), timeout=5.0)

    assert any(
        e.get("type") == "tool_use" and e.get("tool_name") == "AskUserQuestion"
        for e in captured
    )
    assert not any(e.get("type") == "result" for e in captured), captured
    assert pcm.get_active_stream(chat.chat_id) is None
    assert pcm.queue_message(chat.chat_id, "answer") is False
    # The question is persisted on the chat so a reloaded PWA can rebuild the
    # picker (surfaced via to_dict.pending_question).
    assert pcm._chats[chat.chat_id].pending_question == (
        '{"questions":[{"question":"Which email?"}]}'
    )
    assert pcm._chats[chat.chat_id].to_dict()["pending_question"] == (
        '{"questions":[{"question":"Which email?"}]}'
    )


async def test_queued_messages_survive_question_pause_and_flush_after_answer(
    tmp_path: Path,
) -> None:
    """Messages queued mid-turn must not be dropped when the turn pauses on an
    AskUserQuestion. They get parked on the chat and re-seeded into the answer
    turn so they still flush as follow-ups (regression: queued messages
    disappeared and were never sent after answering a question via the PWA).
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q3-park", workspace="work")
    chat = pcm.create_chat(project.project_id, title="park-test")

    queued_ready = asyncio.Event()
    turn_calls: list[str] = []

    async def fake_stream_chat(chat_id, prompt, images=None):
        turn_calls.append(prompt)
        if prompt == "initial":
            # Hold the first turn open until the test has queued follow-ups,
            # then pause on a question — which tears the stream down.
            await queued_ready.wait()
            yield ToolUseEvent(
                type="assistant",
                tool_name="AskUserQuestion",
                tool_input='{"questions":[{"question":"Which email?"}]}',
            )
            return
        yield ResultEvent(
            type="result",
            result=f"done: {prompt}",
            session_id="sess-park",
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

    # Queue two follow-ups while the first turn is still running.
    assert pcm.queue_message(chat.chat_id, "msg A") is True
    assert pcm.queue_message(chat.chat_id, "msg B") is True

    # Release the turn so it reaches the question and pauses.
    queued_ready.set()
    await asyncio.wait_for(consumer, timeout=5.0)

    # The paused turn produced no result and the stream is gone...
    assert not any(e.get("type") == "result" for e in captured), captured
    assert pcm.get_active_stream(chat.chat_id) is None
    # ...but the queued follow-ups were parked on the chat, not dropped.
    parked = pcm._chats[chat.chat_id].pending_queue
    assert [p["text"] for p in parked] == ["msg A", "msg B"], parked

    # The user answers, starting a fresh turn. The parked follow-ups must
    # re-seed and flush after it, in order.
    captured2: list[dict] = []

    async def consume2(stream) -> None:
        async for ev in stream.subscribe():
            captured2.append(ev)

    stream2 = pcm.start_stream(chat.chat_id, "answer")
    await asyncio.wait_for(consume2(stream2), timeout=5.0)

    # answer + two flushed follow-ups.
    assert turn_calls == ["initial", "answer", "msg A", "msg B"], turn_calls
    echoes = [e["text"] for e in captured2 if e.get("type") == "user_echo"]
    assert echoes == ["answer", "msg A", "msg B"], echoes
    # The park buffer is consumed exactly once.
    assert pcm._chats[chat.chat_id].pending_queue == []


async def test_queue_edit_remove_reorder_operate_on_parked_queue(tmp_path: Path) -> None:
    """reorder_queue/edit_queue/remove_queue must not silently no-op once the
    stream that queued them has torn down (error, question-pause, retry-armed
    — anything that parks onto `chat.pending_queue`). Regression: they only
    ever touched the live ChatStream's in-memory queue via `self._broker.get`,
    so once that returned None they returned False without checking the
    persisted parked queue — the client's QUEUED chip would show an edit or
    removal as successful (optimistic UI) while the original, untouched
    message still flushed on the next turn.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q3-parked-ops", workspace="work")
    chat = pcm.create_chat(project.project_id, title="parked-ops-test")

    # No live stream for this chat — simulates a turn that already tore down
    # after parking its queue (e.g. a non-retryable error, or question-pause).
    assert pcm.get_active_stream(chat.chat_id) is None
    pcm._chats[chat.chat_id].pending_queue = [
        {"id": "q-1", "text": "msg A", "images": []},
        {"id": "q-2", "text": "msg B", "images": []},
        {"id": "q-3", "text": "msg C", "images": []},
    ]

    # Edit operates on the parked queue, not a no-op.
    assert pcm.edit_queue(chat.chat_id, "q-2", "msg B edited") is True
    parked = pcm._chats[chat.chat_id].pending_queue
    assert [p["text"] for p in parked] == ["msg A", "msg B edited", "msg C"], parked

    # Reorder likewise.
    assert pcm.reorder_queue(chat.chat_id, "q-3", before_id="q-1") is True
    parked = pcm._chats[chat.chat_id].pending_queue
    assert [p["id"] for p in parked] == ["q-3", "q-1", "q-2"], parked

    # Remove likewise.
    assert pcm.remove_queue(chat.chat_id, "q-1") is True
    parked = pcm._chats[chat.chat_id].pending_queue
    assert [p["id"] for p in parked] == ["q-3", "q-2"], parked

    # Unknown entry id still correctly fails.
    assert pcm.remove_queue(chat.chat_id, "nope") is False


async def test_new_session_clears_parked_question_queue(tmp_path: Path) -> None:
    """Starting a fresh session abandons a paused question and any follow-ups
    parked for its answer turn — they must not leak into the new conversation.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q3-reset", workspace="work")
    chat = pcm.create_chat(project.project_id, title="reset-test")
    pcm._chats[chat.chat_id].pending_question = '{"questions":[{"question":"x?"}]}'
    pcm._chats[chat.chat_id].pending_queue = [
        {"id": "q-1", "text": "stale follow-up", "images": []}
    ]

    pcm.new_session(chat.chat_id)

    assert pcm._chats[chat.chat_id].pending_question == ""
    assert pcm._chats[chat.chat_id].pending_queue == []


async def test_ask_user_question_interrupts_live_provider(tmp_path: Path) -> None:
    """When a live provider is attached, AskUserQuestion must interrupt the
    CLI turn. A live probe (claude-agent-sdk 0.2.93) confirmed interrupt is the
    only clean stop: a PreToolUse "defer" hook makes the CLI surface the tool
    as an internal error and the model chatters a fallback instead of stopping.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-interrupt", workspace="work")
    chat = pcm.create_chat(project.project_id, title="interrupt-test")

    stop_calls: list[str] = []

    class _FakeProvider:
        async def stop_active(self) -> bool:
            stop_calls.append(chat.chat_id)
            return True

    # Register a fake provider so _drive's interrupt branch fires.
    pcm._providers[chat.chat_id] = _FakeProvider()  # type: ignore[assignment]

    async def fake_stream_chat(chat_id, prompt, images=None):
        yield ToolUseEvent(
            type="assistant",
            tool_name="AskUserQuestion",
            tool_input="",
        )
        yield ToolUseEvent(
            type="assistant",
            tool_name="AskUserQuestion",
            tool_input='{"questions":[{"question":"Q3 or Q4?"}]}',
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    async def consume(stream) -> None:
        async for _ev in stream.subscribe():
            pass

    stream = pcm.start_stream(chat.chat_id, "initial")
    await asyncio.wait_for(consume(stream), timeout=5.0)

    assert stop_calls == [chat.chat_id], "expected one interrupt on AskUserQuestion"
    assert pcm._chats[chat.chat_id].pending_question == (
        '{"questions":[{"question":"Q3 or Q4?"}]}'
    )


async def test_user_send_clears_pending_question(tmp_path: Path) -> None:
    """A new user turn answers/supersedes the paused question, so the persisted
    picker state must be cleared at send time."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-clear", workspace="work")
    chat = pcm.create_chat(project.project_id, title="clear-test")
    pcm._chats[chat.chat_id].pending_question = '{"questions":[{"question":"x?"}]}'

    async def fake_stream_chat(chat_id, prompt, images=None):
        yield ResultEvent(
            type="result",
            result="answered",
            session_id="sess-c",
            is_error=False,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    async def consume(stream) -> None:
        async for _ev in stream.subscribe():
            pass

    stream = pcm.start_stream(chat.chat_id, "Q3 please")
    await asyncio.wait_for(consume(stream), timeout=5.0)

    assert pcm._chats[chat.chat_id].pending_question == ""


async def test_queue_flush_bumps_user_turn_count_per_message(tmp_path: Path) -> None:
    """Each flushed queued message bumps `user_turn_count` exactly once so
    history replay (GET /messages) lines up with what the client rendered live.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-count", workspace="work")
    chat = pcm.create_chat(project.project_id, title="count-test")

    first_turn_ready = asyncio.Event()
    turn_calls: list[str] = []

    async def fake_stream_chat(chat_id, prompt, images=None):
        turn_calls.append(prompt)
        if len(turn_calls) == 1:
            await first_turn_ready.wait()
        yield ResultEvent(
            type="result",
            result="ok",
            session_id="sess-y",
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

    pcm.queue_message(chat.chat_id, "follow-up one")
    pcm.queue_message(chat.chat_id, "follow-up two")
    first_turn_ready.set()
    await asyncio.wait_for(consumer, timeout=5.0)

    # Start: 0. Initial bump: 1. Each queued follow-up bumps once more: 3.
    assert pcm.get_chat(chat.chat_id).user_turn_count == 3


async def test_queued_message_survives_non_retryable_error(tmp_path: Path) -> None:
    """A follow-up turn that ends in a non-retryable error (not a quota or
    connection error eligible for auto-retry) must not silently drop whatever
    was still queued behind it. Regression: `_drive()` popped the next queued
    message off the stream before checking `had_error`, then discarded it on
    `break` without parking it — so with two messages queued, the first got
    sent as a follow-up turn and the second vanished when that turn errored.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q3-error-drop", workspace="work")
    chat = pcm.create_chat(project.project_id, title="error-drop-test")

    first_turn_ready = asyncio.Event()
    turn_calls: list[str] = []

    async def fake_stream_chat(chat_id, prompt, images=None):
        turn_calls.append(prompt)
        if prompt == "initial":
            await first_turn_ready.wait()
            yield ResultEvent(
                type="result",
                result="assistant answer",
                session_id="sess-x",
                is_error=False,
                effective_model=chat.model,
                usage={},
                quota={},
                cost_usd=0.0,
            )
            return
        # The follow-up turn (msg A) fails with a non-retryable error: not a
        # quota or connection error, so it must not auto-retry, and any
        # message still queued behind it must be parked, not dropped.
        yield ResultEvent(
            type="result",
            result="permission denied: tool X is not allowed",
            session_id="sess-x",
            is_error=True,
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

    assert pcm.queue_message(chat.chat_id, "msg A") is True
    assert pcm.queue_message(chat.chat_id, "msg B") is True

    first_turn_ready.set()
    await asyncio.wait_for(consumer, timeout=5.0)

    # msg A was sent as a follow-up turn and errored; msg B must be parked on
    # the chat rather than lost.
    assert turn_calls == ["initial", "msg A"], turn_calls
    parked = pcm._chats[chat.chat_id].pending_queue
    assert [p["text"] for p in parked] == ["msg B"], parked


def test_chat_stream_pending_reorder_edit_remove() -> None:
    """ChatStream exposes stable ids and supports reorder/edit/remove."""
    stream = ChatStream("hello")
    id_a = stream.enqueue("A")
    id_b = stream.enqueue("B")
    id_c = stream.enqueue("C")

    assert [p["text"] for p in stream.pending] == ["A", "B", "C"]

    # Move B before A.
    assert stream.reorder_pending(id_b, before_id=id_a) is True
    assert [p["text"] for p in stream.pending] == ["B", "A", "C"]

    # Move A to the end.
    assert stream.reorder_pending(id_a, before_id=None) is True
    assert [p["text"] for p in stream.pending] == ["B", "C", "A"]

    # Edit C.
    assert stream.edit_pending(id_c, "C-edited", images=["img.png"]) is True
    assert [p["text"] for p in stream.pending] == ["B", "C-edited", "A"]
    assert stream.pending[1]["images"] == ["img.png"]

    # Remove B.
    assert stream.remove_pending(id_b) is True
    assert [p["text"] for p in stream.pending] == ["C-edited", "A"]

    # Drain one at a time preserves the new order.
    first = stream.drain_one()
    assert first is not None and first["text"] == "C-edited"
    second = stream.drain_one()
    assert second is not None and second["text"] == "A"
    assert stream.drain_one() is None


def test_chat_stream_enqueue_accepts_client_id() -> None:
    """If the client supplies an entry id, the backend uses it."""
    stream = ChatStream("hello")
    client_id = "client-abc-123"
    resolved = stream.enqueue("hello", entry_id=client_id)
    assert resolved == client_id
    assert stream.pending[0]["id"] == client_id


async def test_queue_reorder_changes_flush_order(tmp_path: Path) -> None:
    """Reordering the backend queue changes the order in which queued messages
    are flushed as individual turns."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-reorder", workspace="work")
    chat = pcm.create_chat(project.project_id, title="reorder-test")

    first_turn_ready = asyncio.Event()
    turn_calls: list[str] = []

    async def fake_stream_chat(chat_id, prompt, images=None):
        turn_calls.append(prompt)
        if len(turn_calls) == 1:
            await first_turn_ready.wait()
        yield ResultEvent(
            type="result",
            result="ok",
            session_id="sess-r",
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

    id_a = pcm.get_active_stream(chat.chat_id).enqueue("A")
    pcm.get_active_stream(chat.chat_id).enqueue("B")

    # Swap order so B goes first.
    pcm.reorder_queue(chat.chat_id, id_a, before_id=None)

    first_turn_ready.set()
    await asyncio.wait_for(consumer, timeout=5.0)

    assert turn_calls == ["initial", "B", "A"]


async def test_queue_edit_updates_flushed_text(tmp_path: Path) -> None:
    """Editing a queued message changes what is sent in its follow-up turn."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-edit", workspace="work")
    chat = pcm.create_chat(project.project_id, title="edit-test")

    first_turn_ready = asyncio.Event()
    turn_calls: list[str] = []

    async def fake_stream_chat(chat_id, prompt, images=None):
        turn_calls.append(prompt)
        if len(turn_calls) == 1:
            await first_turn_ready.wait()
        yield ResultEvent(
            type="result",
            result="ok",
            session_id="sess-e",
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

    entry_id = pcm.get_active_stream(chat.chat_id).enqueue("old")
    pcm.edit_queue(chat.chat_id, entry_id, "edited")

    first_turn_ready.set()
    await asyncio.wait_for(consumer, timeout=5.0)

    assert turn_calls == ["initial", "edited"]
