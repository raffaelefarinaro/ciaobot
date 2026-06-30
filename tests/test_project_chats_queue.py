"""Coverage for the queued-message flush path in `_drive()`.

Context: while a turn is streaming, a user can type more messages — they get
queued via `ProjectChatManager.queue_message()` and flushed together as a
single follow-up turn once the first one finishes. The UI bug reported on
2026-04-21 was that the flushed follow-up rendered as a "Thinking" spinner
with no user bubble, suggesting either a missing/garbled `user_echo` or a
turn_index off-by-one.

These tests pin the server-side contract so any regression on the publish
side is caught before it reaches the PWA.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ciao.config import CiaoConfig
from ciao.models import ResultEvent, ToolUseEvent
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
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


async def test_queued_messages_flush_as_single_user_echo(tmp_path: Path) -> None:
    """Two messages queued mid-turn must flush as one `user_echo` payload.

    Contract under test:
      - `stream_chat` is invoked twice (initial prompt + combined follow-up)
      - the combined follow-up prompt is the queued texts joined by "\\n\\n"
      - exactly one additional `user_echo` event is published for the flush
      - it carries the joined text AND a `turn_index` equal to the pre-flush
        `user_turn_count` (so the client can dedup against any optimistic
        bubble it rendered locally)
    """
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

    # Release the first turn; _drive will drain pending and kick the follow-up.
    first_turn_ready.set()

    await asyncio.wait_for(consumer, timeout=5.0)

    # Two stream_chat invocations expected: initial prompt + combined flush.
    assert len(turn_calls) == 2, f"expected 2 turns, got {turn_calls!r}"
    assert turn_calls[0] == "initial"
    assert turn_calls[1] == "msg A\n\nmsg B", (
        f"combined prompt mismatch: {turn_calls[1]!r}"
    )

    echoes = [e for e in captured if e.get("type") == "user_echo"]
    assert len(echoes) == 2, (
        f"expected 2 user_echo events (initial + flush), got {echoes!r}"
    )

    # Initial echo carries the initial prompt and turn_index 0.
    assert echoes[0]["text"] == "initial"
    assert echoes[0].get("turn_index") == 0

    # The flushed echo is the bug-prone one: this is what must render as a
    # user bubble for the combined follow-up turn.
    flushed = echoes[1]
    assert flushed["text"] == "msg A\n\nmsg B", (
        f"flushed echo text mismatch: {flushed!r}"
    )
    assert flushed.get("turn_index") == 1, (
        f"flushed echo turn_index mismatch: {flushed!r}"
    )
    assert flushed.get("images") == []


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


async def test_queue_flush_bumps_user_turn_count(tmp_path: Path) -> None:
    """The flush turn must bump `user_turn_count` exactly once so history
    replay (GET /messages) lines up with what the client rendered live."""
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

    # Start: 0. Initial bump: 1. Flush bump: 2. No extra bumps for the
    # individual queued messages (they merge into one turn).
    assert pcm.get_chat(chat.chat_id).user_turn_count == 2
