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
from ciao.models import ImageAttachment, ResultEvent, ToolUseEvent
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


def _write_media(tmp_path: Path, filename: str = "shot.png") -> Path:
    """Write a real file under the manager's media_root so refs resolve."""
    media = tmp_path / ".runtime" / "media"
    media.mkdir(parents=True, exist_ok=True)
    (media / filename).write_bytes(b"\x89PNG\r\n\x1a\n")
    return media / filename


async def test_queued_messages_flush_one_at_a_time(tmp_path: Path) -> None:
    """Two messages queued mid-turn must flush as two separate turns."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-queue", workspace="work")
    # Non-default title so the auto-title side-effect task doesn't spawn.
    chat = pcm.create_chat(project.project_id, title="queue-test")

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
    assert pcm.queue_message(chat.chat_id, "msg A", entry_id="q-a") is True
    assert pcm.queue_message(chat.chat_id, "msg B", entry_id="q-b") is True

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
    assert echoes[1].get("entry_id") == "q-a"
    assert echoes[2]["text"] == "msg B"
    assert echoes[2].get("turn_index") == 2
    assert echoes[2].get("entry_id") == "q-b"


async def test_a_blank_queued_follow_up_does_not_rerun_the_previous_prompt(
    tmp_path: Path,
) -> None:
    """A whitespace-only queued follow-up must be skipped, not re-run (#1102)."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-blank", workspace="work")
    chat = pcm.create_chat(project.project_id, title="blank-followup-test")

    first_turn_ready = asyncio.Event()
    turn_calls: list[str] = []

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
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

    # A blank follow-up, then a real one.
    assert pcm.queue_message(chat.chat_id, "   ", entry_id="q-blank") is True
    assert pcm.queue_message(chat.chat_id, "msg A", entry_id="q-a") is True

    first_turn_ready.set()

    await asyncio.wait_for(consumer, timeout=5.0)

    # The blank follow-up must not re-run "initial"; only "msg A" runs next.
    assert turn_calls == ["initial", "msg A"], f"got {turn_calls!r}"

    echoes = [e for e in captured if e.get("type") == "user_echo"]
    assert len(echoes) == 2, f"expected 2 user_echo events, got {echoes!r}"

    queue_states = [e for e in captured if e.get("type") == "queue_state"]
    assert queue_states, "expected a queue_state event for the skipped blank entry"
    assert all(
        entry.get("id") != "q-blank"
        for state in queue_states
        for entry in state.get("queue", [])
    ), f"skipped entry still present in {queue_states!r}"
    assert [e.get("id") for e in queue_states[-1]["queue"]] == ["q-a"]


async def test_a_blank_last_follow_up_ends_the_stream_without_another_turn(
    tmp_path: Path,
) -> None:
    """A trailing whitespace-only follow-up must end the stream (#1102)."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-blank-last", workspace="work")
    chat = pcm.create_chat(project.project_id, title="blank-last-test")

    first_turn_ready = asyncio.Event()
    turn_calls: list[str] = []

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
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

    assert pcm.queue_message(chat.chat_id, "   ", entry_id="q-blank") is True

    first_turn_ready.set()

    await asyncio.wait_for(consumer, timeout=5.0)

    assert turn_calls == ["initial"], f"got {turn_calls!r}"
    results = [e for e in captured if e.get("type") == "result"]
    assert len(results) == 1, f"expected 1 result event, got {results!r}"


async def test_a_blank_follow_up_before_an_errored_turn_keeps_the_real_chip(
    tmp_path: Path,
) -> None:
    """A blank skipped before an errored turn must not wipe the parked chip.

    When the turn ends in a non-retryable error, the real follow-up taken off
    the stream is parked onto `chat.pending_queue` and runs on the next send.
    The `queue_state` published for the skipped blank must still list it, or the
    PWA's whole-list replacement drops its chip while it is still queued.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-blank-error", workspace="work")
    chat = pcm.create_chat(project.project_id, title="blank-error-test")

    first_turn_ready = asyncio.Event()
    turn_calls: list[str] = []

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        turn_calls.append(prompt)
        if len(turn_calls) == 1:
            await first_turn_ready.wait()
        yield ResultEvent(
            type="result",
            result="boom",
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

    assert pcm.queue_message(chat.chat_id, "   ", entry_id="q-blank") is True
    assert pcm.queue_message(chat.chat_id, "msg A", entry_id="q-a") is True

    first_turn_ready.set()

    await asyncio.wait_for(consumer, timeout=5.0)

    # The errored turn stopped the loop; the real follow-up is parked for the
    # next send.
    assert turn_calls == ["initial"], f"got {turn_calls!r}"
    parked = pcm._chats[chat.chat_id].pending_queue
    assert [e["id"] for e in parked] == ["q-a"], parked

    queue_states = [e for e in captured if e.get("type") == "queue_state"]
    assert queue_states, "expected a queue_state event for the skipped blank entry"
    assert [e.get("id") for e in queue_states[-1]["queue"]] == ["q-a"], (
        f"parked follow-up lost its chip: {queue_states[-1]!r}"
    )


async def test_image_only_queued_follow_up_flushes_with_images(
    tmp_path: Path,
) -> None:
    """A queued follow-up with blank text but images must reach the provider.

    The drive loop's blank skip read "no text" as "nothing to send" and dropped
    the entry without a word, so the images the user attached never went out
    (#1112). Image-only is sendable: the turn goes out with an empty prompt and
    the resolved images.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q4-image-only", workspace="work")
    chat = pcm.create_chat(project.project_id, title="image-only-followup-test")
    shot = _write_media(tmp_path)

    first_turn_ready = asyncio.Event()
    turn_calls: list[tuple[str, list[str]]] = []

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        turn_calls.append(
            (prompt, [img.original_filename for img in images or []])
        )
        if len(turn_calls) == 1:
            await first_turn_ready.wait()
        yield ResultEvent(
            type="result",
            result="assistant answer",
            session_id="sess-img",
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

    # A follow-up whose text is blank and whose images are the content.
    assert (
        pcm.queue_message(
            chat.chat_id,
            "   ",
            images=[
                ImageAttachment(
                    path=shot,
                    mime_type="image/png",
                    original_filename="shot.png",
                )
            ],
            entry_id="q-img",
        )
        is True
    )

    first_turn_ready.set()

    await asyncio.wait_for(consumer, timeout=5.0)

    # It ran: as its own turn, with the images attached and no invented
    # placeholder prompt text.
    assert [prompt for prompt, _ in turn_calls] == ["initial", ""], turn_calls
    assert turn_calls[1][1] == ["shot.png"], turn_calls

    echoes = [e for e in captured if e.get("type") == "user_echo"]
    assert [e["text"] for e in echoes] == ["initial", ""], echoes
    assert echoes[1]["images"] == ["shot.png"], echoes
    assert echoes[1].get("entry_id") == "q-img", echoes


async def test_whitespace_text_without_images_is_ignored(tmp_path: Path) -> None:
    """Blank text with no images is the one case that stays unsendable (#1112).

    Nothing is lost when it is skipped: the entry carries no images, so there
    is nothing to silently drop, and no provider call may happen for it.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q4-blank-no-images", workspace="work")
    chat = pcm.create_chat(project.project_id, title="blank-no-images-test")

    first_turn_ready = asyncio.Event()
    turn_calls: list[tuple[str, list[str]]] = []

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        turn_calls.append(
            (prompt, [img.original_filename for img in images or []])
        )
        if len(turn_calls) == 1:
            await first_turn_ready.wait()
        yield ResultEvent(
            type="result",
            result="assistant answer",
            session_id="sess-blank",
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

    assert pcm.queue_message(chat.chat_id, "   ", entry_id="q-blank") is True

    first_turn_ready.set()

    await asyncio.wait_for(consumer, timeout=5.0)

    # No second turn for an entry with neither text nor images...
    assert [prompt for prompt, _ in turn_calls] == ["initial"], turn_calls
    # ...and no turn carries images that would have been lost with it.
    assert all(not images for _, images in turn_calls), turn_calls

    echoes = [e for e in captured if e.get("type") == "user_echo"]
    assert [e["text"] for e in echoes] == ["initial"], echoes
    assert all(not e.get("images") for e in echoes), echoes


async def test_reseed_preserves_image_only_entries(tmp_path: Path) -> None:
    """The `pending_queue` re-seed must keep an image-only parked entry.

    The re-seed skipped every entry with no text, so a follow-up parked by a
    question-pause with images but blank text lost them for good when the user
    answered (#1112). An entry with neither text nor images is still dropped —
    there is nothing to send.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q4-reseed-images", workspace="work")
    chat = pcm.create_chat(project.project_id, title="reseed-image-only-test")
    _write_media(tmp_path)

    pcm._chats[chat.chat_id].pending_queue = [
        {"id": "q-img", "text": "   ", "images": ["shot.png"]},
        {"id": "q-empty", "text": "", "images": []},
    ]

    turn_calls: list[tuple[str, list[str]]] = []

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        turn_calls.append(
            (prompt, [img.original_filename for img in images or []])
        )
        yield ResultEvent(
            type="result",
            result="assistant answer",
            session_id="sess-reseed",
            is_error=False,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "answer")
    # The re-seed is synchronous inside `start_stream` — the drive task only
    # runs at the next await point.
    assert [e["id"] for e in stream.pending] == ["q-img"], stream.pending

    captured: list[dict] = []

    async def consume(s) -> None:
        async for ev in s.subscribe():
            captured.append(ev)

    consumer = asyncio.create_task(consume(stream))
    await asyncio.wait_for(consumer, timeout=5.0)

    # The answer turn, then the image-only follow-up with its image attached.
    assert turn_calls == [("answer", []), ("", ["shot.png"])], turn_calls
    assert pcm._chats[chat.chat_id].pending_queue == []


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

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
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

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
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

    # The park must tell a connected client. An empty queue_state here would
    # wipe the chips the PWA is about to keep across the torn-down stream.
    queue_states = [e for e in captured if e.get("type") == "queue_state"]
    assert queue_states, "parking follow-ups must publish queue_state"
    assert [entry.get("text") for entry in queue_states[-1]["queue"]] == [
        "msg A",
        "msg B",
    ], queue_states[-1]
    assert queue_states[-1]["queue"], "an empty queue_state would wipe the chips"

    public = pcm._chats[chat.chat_id].to_dict()["pending_queue"]
    assert [entry["text"] for entry in public] == ["msg A", "msg B"]
    assert [entry["id"] for entry in public] == [entry["id"] for entry in parked]
    assert all(set(entry) == {"id", "text", "images"} for entry in public)
    # A caller must not be able to mutate the live queue through the payload.
    assert public is not parked
    assert public[0] is not parked[0]
    assert public[0]["images"] is not parked[0]["images"]
    public[0]["text"] = "changed"
    public.append({"id": "extra", "text": "nope", "images": []})
    assert [entry["text"] for entry in pcm._chats[chat.chat_id].pending_queue] == [
        "msg A",
        "msg B",
    ]

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

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
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

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
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

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
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

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
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

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
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

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
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


# ── the close-the-queue race ────────────────────────────────────────────────


def test_a_finished_stream_stops_accepting_queued_messages() -> None:
    """Once the drive loop is done, queueing is a message with no reader.

    `drain_one()` and the stream teardown are not one atomic step. A send
    landing in that window was accepted into `_pending` that nothing would
    ever read — the loop had already looked — so the user kept a QUEUED chip
    for a message that was never going to be sent, and `queue_message`
    returning True stopped the caller starting a real turn for it.
    """
    stream = ChatStream("chat-1")
    assert stream.accepting_queue is True

    stream.accepting_queue = False

    # The stream itself still holds whatever was queued before it closed —
    # refusing new work must not discard the old.
    stream.accepting_queue = True
    stream.enqueue("earlier", [])
    stream.accepting_queue = False
    assert [entry["text"] for entry in stream.pending] == ["earlier"]


@pytest.mark.asyncio
async def test_queue_message_refuses_a_closed_stream(tmp_path: Path) -> None:
    """`queue_message` must report False so the caller starts a real turn.

    Returning True for a stream whose loop has finished is what turned this
    from a dropped message into a silent one: the caller trusted the queue
    and never fell through to `start_stream`.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("queue-close", workspace="work")
    chat = pcm.create_chat(project.project_id, title="queue-close-test")

    stream = ChatStream(chat.chat_id)
    pcm._broker.register(chat.chat_id, stream)
    assert pcm.queue_message(chat.chat_id, "while running") is True

    stream.accepting_queue = False
    assert pcm.queue_message(chat.chat_id, "after the loop gave up") is False

    # Refusing new work must not discard what was queued before the close.
    assert [entry["text"] for entry in stream.pending] == ["while running"]


@pytest.mark.asyncio
async def test_a_message_queued_as_the_turn_ends_is_parked_not_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clean-completion path parked nothing, so a late send vanished.

    Every other exit — error, question-pause, the defensive branch — parked the
    queue onto `chat.pending_queue`. The common path, a turn that finished with
    an empty-looking queue, broke straight out and let `finally` tear the
    stream down with the message still in it.

    The race is simulated at exactly its real boundary: `drain_one` reports an
    empty queue, and a send lands immediately afterwards.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("queue-race", workspace="work")
    chat = pcm.create_chat(project.project_id, title="queue-race-test")

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield ResultEvent(
            type="result", result="done", session_id="sess-race", is_error=False,
            effective_model=chat.model, usage={}, quota={}, cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "initial")

    # `ChatStream` uses __slots__, so the hook goes on the class. Patching at
    # exactly `drain_one` puts the simulated send at the real boundary: the
    # loop has just looked and found nothing.
    raced: list[str] = []
    real_drain_one = ChatStream.drain_one

    def drain_one_then_race(self):
        entry = real_drain_one(self)
        if entry is None and not raced:
            raced.append("sent")
            self.enqueue("landed in the race window", [], entry_id="q-race")
        return entry

    monkeypatch.setattr(ChatStream, "drain_one", drain_one_then_race)

    captured: list[dict] = []

    async def consume(s) -> None:
        async for ev in s.subscribe():
            captured.append(ev)

    consumer = asyncio.create_task(consume(stream))
    await asyncio.wait_for(consumer, timeout=5.0)

    assert raced, "fixture must queue inside the race window"
    parked = [entry["text"] for entry in pcm._chats[chat.chat_id].pending_queue]
    assert parked == ["landed in the race window"], (
        "a message queued as the turn ended must survive to the next turn"
    )


def test_park_pending_for_retry_publishes_the_parked_queue(tmp_path: Path) -> None:
    """A retry park publishes the parked entries and `to_dict` copies them.

    Publishing `stream.pending` after the drain would send an empty
    `queue_state` and wipe the chips. A stream that has already finished
    cannot deliver the event, so that case stays on the chat record only.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q4-retry-park", workspace="work")
    chat = pcm.create_chat(project.project_id, title="retry-park")

    stream = ChatStream("prompt")
    stream.enqueue("and then summarise it", ["shot.png"], entry_id="q-1")
    pcm._park_pending_for_retry(chat.chat_id, stream)

    states = [e for e in stream.buffered_events() if e.get("type") == "queue_state"]
    assert states == [
        {
            "type": "queue_state",
            "queue": [
                {"id": "q-1", "text": "and then summarise it", "images": ["shot.png"]},
            ],
        }
    ]
    public = pcm._chats[chat.chat_id].to_dict()["pending_queue"]
    assert public == states[0]["queue"]
    assert public is not pcm._chats[chat.chat_id].pending_queue
    assert public[0] is not pcm._chats[chat.chat_id].pending_queue[0]

    # Nothing queued: do not publish an empty queue.
    quiet = ChatStream("prompt")
    pcm._park_pending_for_retry(chat.chat_id, quiet)
    assert not any(e.get("type") == "queue_state" for e in quiet.buffered_events())

    # Already finished: the list is stored, but the sentinel has gone out.
    done = ChatStream("prompt")
    done.enqueue("too late", [], entry_id="q-late")
    done.finish()
    pcm._park_pending_for_retry(chat.chat_id, done)
    assert pcm._chats[chat.chat_id].pending_queue == [
        {"id": "q-late", "text": "too late", "images": []}
    ]
    assert not any(e.get("type") == "queue_state" for e in done.buffered_events())


# ── Steer delivery (#1392 A2) ────────────────────────────────────────────


class _SteerProvider:
    """Stands in for a ProviderService: records steer requests."""

    def __init__(self, *, delivery_id: str | None = "d1", on_stop=None) -> None:
        self.delivery_id = delivery_id
        self.requests: list = []
        self._on_stop = on_stop

    async def steer_turn(self, request):
        self.requests.append(request)
        return self.delivery_id

    def active_handle(self):
        on_stop = self._on_stop
        if on_stop is None:
            return None

        class _Handle:
            async def stop(self) -> None:
                on_stop()

        return _Handle()

    async def stop_active(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None


def _result(chat) -> ResultEvent:
    return ResultEvent(
        type="result",
        result="answer",
        session_id="sess-steer",
        is_error=False,
        effective_model=chat.model,
        usage={},
        quota={},
        cost_usd=0.0,
    )


async def _start_blocked_turn(tmp_path: Path, provider: _SteerProvider, *, events=()):
    """Run a first turn that blocks until released; returns the moving parts."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q4-steer", workspace="work")
    chat = pcm.create_chat(project.project_id, title="steer-test")
    pcm._providers[chat.chat_id] = provider  # type: ignore[assignment]
    release = asyncio.Event()
    turn_calls: list[str] = []

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        turn_calls.append(prompt)
        if len(turn_calls) == 1:
            await release.wait()
            for event in events:
                yield event
        yield _result(chat)

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]
    captured: list[dict] = []

    async def consume(stream) -> None:
        async for ev in stream.subscribe():
            captured.append(ev)

    stream = pcm.start_stream(chat.chat_id, "initial")
    consumer = asyncio.create_task(consume(stream))
    await _wait_for(lambda: any(e.get("type") == "user_echo" for e in captured))
    return pcm, chat, stream, release, turn_calls, captured, consumer


def _queue_states(captured: list[dict]) -> list[list[dict]]:
    return [e["queue"] for e in captured if e.get("type") == "queue_state"]


def test_chat_stream_steer_candidates_and_take() -> None:
    stream = ChatStream("chat-1")
    stream.enqueue("plain", entry_id="a")
    stream.enqueue("steer me", entry_id="b", delivery="steer")
    stream.enqueue("with image", ["shot.png"], entry_id="c", delivery="steer")

    assert [e["id"] for e in stream.steer_candidates()] == ["b", "c"]

    stream.mark_steering(["b", "c"], "d1")
    assert stream.steer_candidates() == []
    # In flight: locked against edit and remove.
    assert stream.edit_pending("b", "changed") is False
    assert stream.remove_pending("b") is False
    # And refuses to go back to queue by hand.
    assert stream.set_delivery(["b"], "queue") is False

    taken = stream.take_steered("d1")
    assert [e["id"] for e in taken] == ["b", "c"]
    assert [e["id"] for e in stream.pending] == ["a"]


def test_chat_stream_set_delivery_and_revert() -> None:
    stream = ChatStream("chat-1")
    stream.enqueue("a", entry_id="a")
    stream.enqueue("b", entry_id="b")
    assert stream.set_delivery(["a", "b"], "steer") is True
    assert [e["delivery"] for e in stream.pending] == ["steer", "steer"]
    stream.mark_steering(["a"], "d1")
    assert stream.revert_steers() == ["a", "b"]
    assert [(e["delivery"], e["steer_id"]) for e in stream._pending] == [
        ("queue", ""),
        ("queue", ""),
    ]
    assert stream.set_delivery(["missing"], "steer") is False


def test_pending_public_hides_steer_id_and_flags_steering() -> None:
    stream = ChatStream("chat-1")
    stream.enqueue("a", entry_id="a", delivery="steer")
    stream.mark_steering(["a"], "d1")
    stream.enqueue("b", entry_id="b")
    assert stream.pending_public() == [
        {"id": "a", "text": "a", "images": [], "delivery": "steer", "steering": True},
        {"id": "b", "text": "b", "images": [], "delivery": "queue", "steering": False},
    ]


async def test_steer_queue_combines_in_list_order(tmp_path: Path) -> None:
    provider = _SteerProvider()
    pcm, chat, stream, release, turn_calls, captured, consumer = (
        await _start_blocked_turn(tmp_path, provider)
    )
    assert pcm.queue_message(chat.chat_id, "a", entry_id="q-a", delivery="steer")
    assert pcm.queue_message(chat.chat_id, "b", entry_id="q-b")
    # Reorder b before a, then set both to steer: one dispatch, list order.
    assert pcm.reorder_queue(chat.chat_id, "q-b", "q-a")
    ok = await pcm.set_queue_delivery(chat.chat_id, ["q-a", "q-b"], "steer")
    assert ok is True

    assert [r.prompt for r in provider.requests] == ["b\n\na"]
    assert provider.requests[0].images == []
    await _wait_for(lambda: bool(_queue_states(captured)))
    last = _queue_states(captured)[-1]
    assert [(e["id"], e["steering"]) for e in last] == [("q-b", True), ("q-a", True)]

    release.set()
    await asyncio.wait_for(consumer, timeout=5.0)


async def test_steer_queue_combines_images_in_list_order(tmp_path: Path) -> None:
    provider = _SteerProvider()
    pcm, chat, stream, release, _calls, captured, consumer = await _start_blocked_turn(
        tmp_path, provider
    )
    one = _write_media(tmp_path, "one.png")
    two = _write_media(tmp_path, "two.png")

    def _img(path: Path) -> ImageAttachment:
        return ImageAttachment(path=path, mime_type="image/png", original_filename=path.name)

    assert pcm.queue_message(
        chat.chat_id, "first", images=[_img(one)], entry_id="q-1", delivery="steer"
    )
    # Dispatch only happens in steer_queue, so both are still undispatched.
    assert pcm.queue_message(
        chat.chat_id, "", images=[_img(two)], entry_id="q-2", delivery="steer"
    )
    await pcm.steer_queue(chat.chat_id)

    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.prompt == "first"
    assert [img.original_filename for img in request.images] == ["one.png", "two.png"]

    release.set()
    await asyncio.wait_for(consumer, timeout=5.0)


async def test_steer_delivered_publishes_steered_and_bumps_turn(tmp_path: Path) -> None:
    from ciao.models import SteerDeliveredEvent

    provider = _SteerProvider()
    delivered = SteerDeliveredEvent(type="steer_delivered", delivery_id="d1")
    pcm, chat, stream, release, turn_calls, captured, consumer = (
        await _start_blocked_turn(tmp_path, provider, events=[delivered])
    )
    before = pcm.get_chat(chat.chat_id).user_turn_count
    pcm.queue_message(chat.chat_id, "a", entry_id="q-a", delivery="steer")
    pcm.queue_message(chat.chat_id, "b", entry_id="q-b", delivery="steer")
    await pcm.steer_queue(chat.chat_id)
    release.set()
    await asyncio.wait_for(consumer, timeout=5.0)

    steered = [e for e in captured if e.get("type") == "steered"]
    assert len(steered) == 1
    event = steered[0]
    assert event["entry_ids"] == ["q-a", "q-b"]
    assert event["text"] == "a\n\nb"
    assert event["turn_index"] == before
    assert event["delivery_id"] == "d1"
    assert event["sent_at"]
    meta = pcm.get_chat(chat.chat_id)
    assert meta.user_turn_count == before + 1
    assert meta.user_turn_steered[str(before)] is True
    assert meta.user_turn_timings[str(before)]["sent_at"] == event["sent_at"]
    # The steered turn gets the turn's completion stamp, and nothing flushed as
    # a second turn.
    assert "completed_at" in meta.user_turn_timings[str(before)]
    assert turn_calls == ["initial"]
    assert _queue_states(captured)[-1] == []
    assert not [e for e in captured if e.get("type") == "steer_fallback"]


async def test_steer_delivered_before_dispatch_returns_still_lands(
    tmp_path: Path,
) -> None:
    """The echo can beat steer_turn's return; the entries must still clear."""
    provider = _SteerProvider()
    pcm, chat, stream, release, _calls, captured, consumer = await _start_blocked_turn(
        tmp_path, provider
    )

    async def echo_first(request):
        pcm.on_steer_delivered(chat.chat_id, stream, "d9")
        return "d9"

    provider.steer_turn = echo_first  # type: ignore[method-assign]
    pcm.queue_message(chat.chat_id, "a", entry_id="q-a", delivery="steer")
    await pcm.steer_queue(chat.chat_id)

    await _wait_for(lambda: any(e.get("type") == "steered" for e in captured))
    assert stream.pending == []
    release.set()
    await asyncio.wait_for(consumer, timeout=5.0)


async def test_steer_without_live_turn_falls_back_with_notice(tmp_path: Path) -> None:
    provider = _SteerProvider(delivery_id=None)
    pcm, chat, stream, release, turn_calls, captured, consumer = (
        await _start_blocked_turn(tmp_path, provider)
    )
    pcm.queue_message(chat.chat_id, "later", entry_id="q-a", delivery="steer")
    await pcm.steer_queue(chat.chat_id)

    await _wait_for(lambda: any(e.get("type") == "steer_fallback" for e in captured))
    fallback = [e for e in captured if e.get("type") == "steer_fallback"][0]
    assert fallback == {
        "type": "steer_fallback",
        "entry_ids": ["q-a"],
        "reason": "not_running",
    }
    assert [(e["delivery"], e["steering"]) for e in _queue_states(captured)[-1]] == [
        ("queue", False)
    ]

    release.set()
    await asyncio.wait_for(consumer, timeout=5.0)
    # It flushed as the next turn.
    assert turn_calls == ["initial", "later"]


async def test_steer_queue_without_a_stream_falls_back_quietly(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    # No stream, no provider: nothing to publish to and nothing to raise.
    await pcm.steer_queue("no-such-chat")


async def test_undelivered_steer_reverts_at_turn_end(tmp_path: Path) -> None:
    provider = _SteerProvider()
    pcm, chat, stream, release, turn_calls, captured, consumer = (
        await _start_blocked_turn(tmp_path, provider)
    )
    pcm.queue_message(chat.chat_id, "late", entry_id="q-a", delivery="steer")
    await pcm.steer_queue(chat.chat_id)
    assert len(provider.requests) == 1

    release.set()  # the turn ends without a SteerDeliveredEvent
    await asyncio.wait_for(consumer, timeout=5.0)

    fallback = [e for e in captured if e.get("type") == "steer_fallback"]
    assert [(e["entry_ids"], e["reason"]) for e in fallback] == [(["q-a"], "finished")]
    assert turn_calls == ["initial", "late"]
    echoes = [e for e in captured if e.get("type") == "user_echo"]
    assert echoes[-1]["entry_id"] == "q-a"
    assert not [e for e in captured if e.get("type") == "steered"]


async def test_stop_reverts_steers(tmp_path: Path) -> None:
    release_holder: dict[str, asyncio.Event] = {}
    provider = _SteerProvider(on_stop=lambda: release_holder["e"].set())
    pcm, chat, stream, release, turn_calls, captured, consumer = (
        await _start_blocked_turn(tmp_path, provider)
    )
    release_holder["e"] = release
    pcm.queue_message(chat.chat_id, "after stop", entry_id="q-a", delivery="steer")
    await pcm.steer_queue(chat.chat_id)

    await pcm.stop_chat(chat.chat_id)
    await asyncio.wait_for(consumer, timeout=5.0)

    fallback = [e for e in captured if e.get("type") == "steer_fallback"]
    assert [(e["entry_ids"], e["reason"]) for e in fallback] == [(["q-a"], "stopped")]
    # Stop wins: the steer is not delivered, then runs as the next turn.
    assert turn_calls == ["initial", "after stop"]


async def test_set_queue_delivery_rejects_bad_values(tmp_path: Path) -> None:
    provider = _SteerProvider()
    pcm, chat, stream, release, _calls, captured, consumer = await _start_blocked_turn(
        tmp_path, provider
    )
    pcm.queue_message(chat.chat_id, "a", entry_id="q-a")
    assert await pcm.set_queue_delivery(chat.chat_id, ["q-a"], "sometimes") is False
    assert await pcm.set_queue_delivery(chat.chat_id, ["nope"], "steer") is False
    assert stream.pending[0]["delivery"] == "queue"
    assert provider.requests == []
    # Back to queue is fine for an undispatched entry.
    assert await pcm.set_queue_delivery(chat.chat_id, ["q-a"], "queue") is True
    release.set()
    await asyncio.wait_for(consumer, timeout=5.0)


async def test_set_queue_delivery_on_parked_queue(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q4-steer-parked", workspace="work")
    chat = pcm.create_chat(project.project_id, title="parked-steer")
    chat.pending_queue = [{"id": "p1", "text": "x", "images": []}]
    assert await pcm.set_queue_delivery(chat.chat_id, ["p1"], "steer") is True
    assert chat.pending_queue[0]["delivery"] == "steer"
