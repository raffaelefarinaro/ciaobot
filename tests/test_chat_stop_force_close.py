"""Force-close path for the Stop button (ProjectChatManager.stop_chat).

A provider-level stop (Claude interrupt / opencode abort) is the clean path,
but it only ends the turn when the provider cooperates. When it doesn't — a
hung CLI, a dead SSE subscription — the drive loop used to sit blocked in the
event iterator forever with the UI stuck on "streaming". These tests pin the
bounded grace + force-close contract: the turn always closes promptly, every
client gets a terminal result event, and queued follow-ups still flush.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ciao.config import CiaoConfig
from ciao.models import AssistantTextDelta, ResultEvent
from ciao.sessions import ChatContext, StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ChatInfo, ProjectChatManager

from .conftest import attach_stub_mcp


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


async def _wait_for(predicate, timeout: float = 3.0, step: float = 0.01) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(step)
    raise AssertionError(f"timed out waiting for predicate {predicate!r}")


class _HangingHandle:
    """A provider handle whose stop() never gets acked (wedged CLI)."""

    def __init__(self, acked: asyncio.Event) -> None:
        self._acked = acked

    async def stop(self) -> None:
        await self._acked.wait()


def _fake_provider_service(acked: asyncio.Event, disconnects: list[int]):
    class _FakeProviderService:
        can_drain = False

        def active_handle(self):
            return _HangingHandle(acked)

        async def stop_active(self) -> bool:
            await _HangingHandle(acked).stop()
            return True

        async def disconnect(self) -> None:
            disconnects.append(1)
            acked.set()

    return _FakeProviderService()


async def test_stop_force_closes_a_hung_turn_and_flushes_queue(
    tmp_path: Path,
) -> None:
    """Provider never ends the turn: force close + synthetic result + flush."""
    pcm = _make_manager(tmp_path)
    # Keep the grace window tiny so the test is fast.
    pcm._STOP_GRACE_S = 0.05
    project = pcm.create_project("stop-force", workspace="work")
    chat = pcm.create_chat(project.project_id, title="stop-test", provider="claude")

    acked = asyncio.Event()
    disconnects: list[int] = []
    pcm._providers[chat.chat_id] = _fake_provider_service(acked, disconnects)

    turn_calls: list[str] = []
    first_turn_blocked = asyncio.Event()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        turn_calls.append(prompt)
        if len(turn_calls) == 1:
            # Hung CLI: stream a partial answer, then never terminate.
            yield AssistantTextDelta(type="text", text="partial answer")
            await first_turn_blocked.wait()
        else:
            yield ResultEvent(
                type="result",
                result="post-stop answer",
                session_id="sess-x",
                is_error=False,
                effective_model=chat.model,
                usage={},
                quota={},
            )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    captured: list[dict] = []

    async def consume(stream) -> None:
        async for ev in stream.subscribe():
            captured.append(ev)

    stream = pcm.start_stream(chat.chat_id, "initial")
    consumer = asyncio.create_task(consume(stream))

    await _wait_for(
        lambda: any(e.get("type") == "text_delta" for e in captured),
    )
    assert pcm.queue_message(chat.chat_id, "follow-up") is True

    stopped = await asyncio.wait_for(pcm.stop_chat(chat.chat_id), timeout=2.0)
    assert stopped is True

    # Force closed fast (well under any human-perceivable "not instant"), then
    # wait for the queued follow-up to publish its own result.
    await _wait_for(
        lambda: len([
            event for event in captured if event.get("type") == "result"
        ]) == 2
    )
    await _wait_for(
        lambda: len([e for e in captured if e.get("type") == "result"]) >= 2
    )
    results = [e for e in captured if e.get("type") == "result"]
    # Synthetic stop result, then the flushed follow-up's own result.
    assert len(results) == 2
    assert results[0].get("stopped") is True
    assert results[0].get("is_error") is False
    assert results[0].get("text") == "partial answer"
    assert "stopped" not in results[1]
    assert results[1].get("text") == "post-stop answer"
    # Claude escalation dropped the wedged client.
    assert disconnects == [1]
    # The queued follow-up still flushes as its own turn.
    await _wait_for(lambda: len(turn_calls) == 2)
    await _wait_for(lambda: stream.done)
    assert turn_calls == ["initial", "follow-up"]

    consumer.cancel()
    first_turn_blocked.set()


async def test_a_board_stop_parks_the_queue_instead_of_running_it(
    tmp_path: Path,
) -> None:
    """A board Stop ends the delegated work: the queued follow-up is parked.

    ``park_queue=True`` moves the stream's queued follow-ups onto the chat
    before the turn ends, so the drive loop runs no next turn and the parked
    message survives (re-seeded by the next user turn) rather than running
    over an attempt the board has already settled ``stopped`` (#1103).
    """
    pcm = _make_manager(tmp_path)
    pcm._STOP_GRACE_S = 0.05
    project = pcm.create_project("stop-park", workspace="work")
    chat = pcm.create_chat(project.project_id, title="stop-park", provider="claude")

    acked = asyncio.Event()
    disconnects: list[int] = []
    pcm._providers[chat.chat_id] = _fake_provider_service(acked, disconnects)

    turn_calls: list[str] = []
    first_turn_blocked = asyncio.Event()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        turn_calls.append(prompt)
        if len(turn_calls) == 1:
            yield AssistantTextDelta(type="text", text="partial answer")
            await first_turn_blocked.wait()
        else:
            yield ResultEvent(
                type="result",
                result="post-stop answer",
                session_id="sess-x",
                is_error=False,
                effective_model=chat.model,
                usage={},
                quota={},
            )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    captured: list[dict] = []

    async def consume(stream) -> None:
        async for ev in stream.subscribe():
            captured.append(ev)

    stream = pcm.start_stream(chat.chat_id, "initial")
    consumer = asyncio.create_task(consume(stream))

    await _wait_for(
        lambda: any(e.get("type") == "text_delta" for e in captured),
    )
    assert pcm.queue_message(chat.chat_id, "follow-up") is True

    stopped = await asyncio.wait_for(
        pcm.stop_chat(chat.chat_id, park_queue=True), timeout=2.0
    )
    assert stopped is True

    await _wait_for(
        lambda: len([e for e in captured if e.get("type") == "result"]) == 1
    )
    await _wait_for(lambda: stream.done)
    # No follow-up turn ran, and the queue is empty: the message was parked.
    assert turn_calls == ["initial"]
    results = [e for e in captured if e.get("type") == "result"]
    assert len(results) == 1
    assert results[0].get("stopped") is True
    assert [e["text"] for e in pcm.get_chat(chat.chat_id).pending_queue] == [
        "follow-up"
    ]

    consumer.cancel()
    first_turn_blocked.set()


async def test_a_message_queued_during_a_board_stop_is_parked_too(
    tmp_path: Path,
) -> None:
    """A message queued while the board Stop is in flight is parked, not run.

    The park decision happens at the drive loop's drain point, so anything
    ``queue_message`` accepts during the stop window (provider grace, force
    close, drive cleanup) is parked with the rest instead of running as a
    follow-up over an attempt the board has settled ``stopped`` (#1103).
    """
    pcm = _make_manager(tmp_path)
    pcm._STOP_GRACE_S = 0.05
    project = pcm.create_project("stop-late", workspace="work")
    chat = pcm.create_chat(project.project_id, title="stop-late", provider="claude")

    acked = asyncio.Event()
    disconnects: list[int] = []
    pcm._providers[chat.chat_id] = _fake_provider_service(acked, disconnects)

    turn_calls: list[str] = []
    first_turn_blocked = asyncio.Event()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        turn_calls.append(prompt)
        if len(turn_calls) == 1:
            yield AssistantTextDelta(type="text", text="partial answer")
            await first_turn_blocked.wait()
        else:
            yield ResultEvent(
                type="result",
                result="post-stop answer",
                session_id="sess-x",
                is_error=False,
                effective_model=chat.model,
                usage={},
                quota={},
            )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    captured: list[dict] = []

    async def consume(stream) -> None:
        async for ev in stream.subscribe():
            captured.append(ev)

    stream = pcm.start_stream(chat.chat_id, "initial")
    consumer = asyncio.create_task(consume(stream))

    await _wait_for(
        lambda: any(e.get("type") == "text_delta" for e in captured),
    )
    assert pcm.queue_message(chat.chat_id, "follow-up") is True

    stop_task = asyncio.create_task(pcm.stop_chat(chat.chat_id, park_queue=True))
    await asyncio.sleep(0.01)
    assert pcm.queue_message(chat.chat_id, "late") is True
    stopped = await asyncio.wait_for(stop_task, timeout=2.0)
    assert stopped is True

    await _wait_for(
        lambda: len([e for e in captured if e.get("type") == "result"]) == 1
    )
    await _wait_for(lambda: stream.done)
    # No follow-up turn ran, and both messages are parked.
    assert turn_calls == ["initial"]
    results = [e for e in captured if e.get("type") == "result"]
    assert len(results) == 1
    assert results[0].get("stopped") is True
    assert [e["text"] for e in pcm.get_chat(chat.chat_id).pending_queue] == [
        "follow-up",
        "late",
    ]

    consumer.cancel()
    first_turn_blocked.set()


async def test_stop_prefers_the_clean_provider_level_end(tmp_path: Path) -> None:
    """Provider reacts to the stop inside the grace window: no force close."""
    pcm = _make_manager(tmp_path)
    pcm._STOP_GRACE_S = 2.0
    project = pcm.create_project("stop-clean", workspace="work")
    chat = pcm.create_chat(project.project_id, title="stop-clean", provider="opencode")

    acked = asyncio.Event()
    disconnects: list[int] = []
    pcm._providers[chat.chat_id] = _fake_provider_service(acked, disconnects)

    turn_calls: list[str] = []
    abort_issued = asyncio.Event()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        turn_calls.append(prompt)
        yield AssistantTextDelta(type="text", text="partial answer")
        # The provider processes the abort and ends the turn cleanly.
        await abort_issued.wait()
        yield ResultEvent(
            type="result",
            result="partial answer",
            session_id="sess-x",
            is_error=False,
            effective_model=chat.model,
            usage={},
            quota={},
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    captured: list[dict] = []

    async def consume(stream) -> None:
        async for ev in stream.subscribe():
            captured.append(ev)

    stream = pcm.start_stream(chat.chat_id, "initial")
    consumer = asyncio.create_task(consume(stream))

    await _wait_for(
        lambda: any(e.get("type") == "text_delta" for e in captured),
    )

    # Let the provider's clean terminal event win the stop race, while also
    # acknowledging the detached provider-stop handle so it cannot remain
    # pending after the turn completes.
    asyncio.get_running_loop().call_soon(abort_issued.set)
    acked.set()
    stopped = await asyncio.wait_for(pcm.stop_chat(chat.chat_id), timeout=2.0)
    assert stopped is True

    # The real terminal event ended the turn: no synthetic result, no
    # escalation disconnect.
    await _wait_for(lambda: any(e.get("type") == "result" for e in captured))
    results = [e for e in captured if e.get("type") == "result"]
    assert len(results) == 1
    assert "stopped" not in results[0]
    assert results[0].get("text") == "partial answer"
    assert disconnects == []
    await _wait_for(lambda: stream.done)
    assert turn_calls == ["initial"]

    consumer.cancel()


async def test_stop_without_an_active_turn_is_bounded_and_false(
    tmp_path: Path,
) -> None:
    """No turn running: stop reports False instead of raising or hanging."""
    pcm = _make_manager(tmp_path)
    pcm._STOP_GRACE_S = 0.05
    project = pcm.create_project("stop-idle", workspace="work")
    chat = pcm.create_chat(project.project_id, title="stop-idle", provider="claude")

    acked = asyncio.Event()
    disconnects: list[int] = []
    pcm._providers[chat.chat_id] = _fake_provider_service(acked, disconnects)

    stopped = await asyncio.wait_for(pcm.stop_chat(chat.chat_id), timeout=2.0)
    assert stopped is False
    assert disconnects == []


@pytest.mark.parametrize("provider", ["claude", "opencode"])
async def test_stop_reaches_both_providers(tmp_path: Path, provider: str) -> None:
    """The same stop_chat path serves every provider (regression guard)."""
    pcm = _make_manager(tmp_path)
    pcm._STOP_GRACE_S = 0.05
    project = pcm.create_project("stop-both", workspace="work")
    chat = pcm.create_chat(project.project_id, title="stop-both", provider=provider)

    acked = asyncio.Event()
    disconnects: list[int] = []
    pcm._providers[chat.chat_id] = _fake_provider_service(acked, disconnects)

    stop_calls: list[str] = []

    class _EndingHandle:
        async def stop(self) -> None:
            stop_calls.append(chat.chat_id)
            # The provider "acks" but the turn still never ends on its own,
            # so the force close has to kick in for both providers alike.

    class _EndingProviderService:
        can_drain = False

        def active_handle(self):
            return _EndingHandle()

        async def stop_active(self) -> bool:
            await _EndingHandle().stop()
            return True

        async def disconnect(self) -> None:
            disconnects.append(1)
            acked.set()

    pcm._providers[chat.chat_id] = _EndingProviderService()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield AssistantTextDelta(type="text", text="working")
        await asyncio.Event().wait()

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    captured: list[dict] = []

    async def consume(stream) -> None:
        async for ev in stream.subscribe():
            captured.append(ev)

    stream = pcm.start_stream(chat.chat_id, "initial")
    consumer = asyncio.create_task(consume(stream))
    await _wait_for(
        lambda: any(e.get("type") == "text_delta" for e in captured),
    )

    stopped = await asyncio.wait_for(pcm.stop_chat(chat.chat_id), timeout=2.0)
    assert stopped is True
    assert stop_calls == [chat.chat_id]
    await _wait_for(
        lambda: any(
            e.get("type") == "result" and e.get("stopped") for e in captured
        )
    )
    await _wait_for(lambda: stream.done)

    consumer.cancel()


async def test_force_close_persists_the_partial_turn_to_the_transcript(
    tmp_path: Path,
) -> None:
    """A force-stopped turn survives a reload, flagged partial.

    Cancelling the turn task unwound `stream_chat` before `record_turn`, and
    its `finally` then deleted the crash journal — so the exchange existed
    only as live WS events. Reopening the chat showed neither the prompt nor
    the partial answer, and no startup recovery could bring it back, because
    for opencode chats the durable transcript IS what a reload renders.
    """
    pcm = attach_stub_mcp(_make_manager(tmp_path))
    pcm._STOP_GRACE_S = 0.05
    project = pcm.create_project("stop-persist", workspace="work")
    chat = pcm.create_chat(project.project_id, title="stop-test", provider="opencode")

    acked = asyncio.Event()
    disconnects: list[int] = []
    pcm._providers[chat.chat_id] = _fake_provider_service(acked, disconnects)

    hung = asyncio.Event()

    # Patched below `stream_chat`, so the real journal + record_turn path runs.
    async def fake_drive_stream(*, chat_id, request, outcome):
        event = AssistantTextDelta(type="text", text="partial answer")
        outcome.events.append(event)
        yield event
        await hung.wait()

    pcm._drive_stream = fake_drive_stream  # type: ignore[assignment]

    captured: list[dict] = []

    async def consume(stream) -> None:
        async for ev in stream.subscribe():
            captured.append(ev)

    stream = pcm.start_stream(chat.chat_id, "please answer")
    consumer = asyncio.create_task(consume(stream))
    await _wait_for(lambda: any(e.get("type") == "text_delta" for e in captured))

    assert await asyncio.wait_for(pcm.stop_chat(chat.chat_id), timeout=2.0) is True
    await _wait_for(
        lambda: any(e.get("type") == "result" and e.get("stopped") for e in captured)
    )

    ctx = ChatContext.for_web(chat.chat_id)
    await _wait_for(
        lambda: bool(pcm._transcripts.current_messages(ctx, "opencode"))
    )
    rows = pcm._transcripts.current_messages(ctx, "opencode")
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert rows[0]["content"] == "please answer"
    assert rows[1]["content"] == "partial answer"

    hung.set()
    consumer.cancel()


async def test_a_clean_turn_is_not_flagged_partial(tmp_path: Path) -> None:
    """The partial flag belongs to force-close only, not to every turn."""
    pcm = attach_stub_mcp(_make_manager(tmp_path))
    project = pcm.create_project("stop-clean", workspace="work")
    chat = pcm.create_chat(project.project_id, title="clean", provider="opencode")
    pcm._providers[chat.chat_id] = _fake_provider_service(asyncio.Event(), [])

    async def fake_drive_stream(*, chat_id, request, outcome):
        event = ResultEvent(
            type="result",
            result="whole answer",
            session_id="sess-1",
            is_error=False,
            effective_model=chat.model,
            usage={},
            quota={},
        )
        outcome.events.append(event)
        outcome.response_text = event.result
        yield event

    pcm._drive_stream = fake_drive_stream  # type: ignore[assignment]

    async for _ in pcm.stream_chat(chat.chat_id, "hello"):
        pass

    ctx = ChatContext.for_web(chat.chat_id)
    stored = pcm._transcripts._load_current(ctx, "opencode")
    turns = stored["turns"]
    assert len(turns) == 1
    assert turns[0]["response"] == "whole answer"
    assert "is_partial" not in turns[0]


async def test_stop_that_raises_still_publishes_a_terminal_result(
    tmp_path: Path,
) -> None:
    """Provider raises on the stop instead of yielding a terminal event.

    Claude's SDK ends an interrupted turn this way. The drive loop treated the
    raise as intentional and fell straight through to the queue drain without
    publishing anything, so clients kept their streaming spinner on a turn the
    server had already finished — it only went away when the *next* send
    replaced it, which is exactly what the turn looked like from the composer.
    """
    pcm = _make_manager(tmp_path)
    pcm._STOP_GRACE_S = 2.0
    project = pcm.create_project("stop-raises", workspace="work")
    chat = pcm.create_chat(project.project_id, title="stop-raises", provider="claude")

    acked = asyncio.Event()
    disconnects: list[int] = []
    pcm._providers[chat.chat_id] = _fake_provider_service(acked, disconnects)

    abort_issued = asyncio.Event()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield AssistantTextDelta(type="text", text="partial answer")
        await abort_issued.wait()
        raise RuntimeError("request was aborted")

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    captured: list[dict] = []

    async def consume(stream) -> None:
        async for ev in stream.subscribe():
            captured.append(ev)

    stream = pcm.start_stream(chat.chat_id, "initial")
    consumer = asyncio.create_task(consume(stream))

    await _wait_for(
        lambda: any(e.get("type") == "text_delta" for e in captured),
    )

    abort_issued.set()
    await asyncio.wait_for(pcm.stop_chat(chat.chat_id), timeout=3.0)

    await _wait_for(lambda: any(e.get("type") == "result" for e in captured))
    results = [e for e in captured if e.get("type") == "result"]
    assert len(results) == 1
    # A stop is not a failure: the frame must not paint an error bubble.
    assert results[0].get("stopped") is True
    assert results[0].get("is_error") is False
    # The partial answer the user already saw streaming is what it carries.
    assert results[0].get("text") == "partial answer"
    assert results[0].get("completed_at")
    await _wait_for(lambda: stream.done)

    # A cancelled turn is not a result: it must not raise an unread badge, an
    # in-app toast or a push carrying the half sentence the user just stopped.
    assert pcm._chats[chat.chat_id].last_response_status == "empty"
    assert pcm._chats[chat.chat_id].last_snippet == ""

    consumer.cancel()


class _CleanStopHandle:
    """Provider handle whose stop() releases the clean empty-error terminal."""

    def __init__(self, release: asyncio.Event) -> None:
        self._release = release

    async def stop(self) -> None:
        self._release.set()


class _ScriptedProvider:
    """Provider fake with a real ``execute_streaming`` below drive_stream."""

    def __init__(self, chat: ChatInfo, script) -> None:
        self._chat = chat
        self._script = script
        self.current_session_id = "sess-native"

    async def execute_streaming(self, request):
        async for event in self._script(request):
            yield event


class _RealPathProviderService:
    """ProviderService fake exposing the shape ``drive_stream`` reads.

    ``execute_streaming`` is a real async generator, so ``drive_stream``'s
    normalization, the journal and ``record_turn`` all execute for real.
    """

    can_drain = False

    def __init__(self, chat: ChatInfo, script, release: asyncio.Event) -> None:
        self._provider = _ScriptedProvider(chat, script)
        self._release = release

    @property
    def provider(self) -> _ScriptedProvider:
        return self._provider

    @property
    def current_session_id(self) -> str:
        return self._provider.current_session_id

    def active_handle(self):
        return _CleanStopHandle(self._release)

    async def execute_streaming(self, request):
        async for event in self._provider.execute_streaming(request):
            yield event

    async def stop_active(self) -> bool:
        self._release.set()
        return True

    async def disconnect(self) -> None:
        self._release.set()


async def _start_real_path_stream(
    tmp_path: Path,
    *,
    provider: str,
    user_text: str,
    release: asyncio.Event,
    script,
    project_name: str,
    title: str = "stop-test",
):
    pcm = attach_stub_mcp(_make_manager(tmp_path))
    pcm._STOP_GRACE_S = 2.0
    project = pcm.create_project(project_name, workspace="work")
    chat = pcm.create_chat(project.project_id, title=title, provider=provider)
    pcm._providers[chat.chat_id] = _RealPathProviderService(chat, script, release)

    captured: list[dict] = []

    async def consume(stream) -> None:
        async for ev in stream.subscribe():
            captured.append(ev)

    stream = pcm.start_stream(chat.chat_id, user_text)
    consumer = asyncio.create_task(consume(stream))
    # The drive loop assigns `stream.turn_task` only once the provider pass is
    # actually running. `stop_chat` uses that task to bound its clean stop, so
    # a test that stops without a turn in flight only exercises cleanup.
    await _wait_for(lambda: stream.turn_task is not None or stream.done)
    return pcm, chat, stream, consumer, captured


@pytest.mark.parametrize("stop_before_text", [False, True])
async def test_clean_claude_stop_persists_partial_to_archive(
    tmp_path: Path, stop_before_text: bool
) -> None:
    """A clean provider-level Claude stop survives reload and archive."""
    release = asyncio.Event()

    def script(request):
        del request

        async def gen():
            if not stop_before_text:
                yield AssistantTextDelta(type="text", text="partial answer")
                # Subagent text must never be persisted as the main answer.
                yield AssistantTextDelta(
                    type="text",
                    text="subagent aside",
                    parent_tool_use_id="task-1",
                )
            await release.wait()
            yield ResultEvent(
                type="result",
                result="",
                session_id="sess-native",
                is_error=True,
                effective_model="opus",
                usage={"input_tokens": "3", "output_tokens": "5"},
                quota={"five_hour": "7"},
                cost_usd=0.25,
            )

        return gen()

    (
        pcm,
        chat,
        stream,
        consumer,
        captured,
    ) = await _start_real_path_stream(
        tmp_path,
        provider="claude",
        user_text="please answer",
        release=release,
        script=script,
        project_name="stop-clean-partial",
        title="stop-test",
    )

    if not stop_before_text:
        await _wait_for(
            lambda: any(e.get("type") == "text_delta" for e in captured)
        )

    assert await asyncio.wait_for(pcm.stop_chat(chat.chat_id), timeout=3.0) is True
    await _wait_for(lambda: stream.done)
    await _wait_for(lambda: any(e.get("type") == "result" for e in captured))

    results = [e for e in captured if e.get("type") == "result"]
    # Exactly one terminal frame, normalized to a non-error stop.
    assert len(results) == 1
    assert results[0].get("stopped") is True
    assert results[0].get("is_error") is False
    assert results[0].get("text") == ("" if stop_before_text else "partial answer")
    # No completed-answer announcement for a stopped partial.
    assert pcm._chats[chat.chat_id].last_snippet == ""
    assert pcm._chats[chat.chat_id].last_response_status == "empty"

    ctx = ChatContext.for_web(chat.chat_id)
    await _wait_for(lambda: bool(pcm._transcripts.current_messages(ctx, "claude")))
    rows = pcm._transcripts.current_messages(ctx, "claude")
    stored = pcm._transcripts._load_current(ctx, "claude")
    assert len(stored["turns"]) == 1
    turn = stored["turns"][-1]
    assert turn["is_partial"] is True
    assert turn["is_error"] is False
    assert turn["usage"] == {"input_tokens": "3", "output_tokens": "5"}
    assert turn["quota"] == {"five_hour": "7"}
    assert turn["effective_model"] == chat.model
    assert stored["session_id"] == "sess-native"
    if stop_before_text:
        # No invented assistant text: only the user row is durable.
        assert [row["role"] for row in rows] == ["user"]
        assert turn["response"] == ""
    else:
        assert [row["role"] for row in rows] == ["user", "assistant"]
        assert rows[1]["content"] == "partial answer"
        assert rows[1].get("partial") is True
        assert "is_error" not in rows[1]
        assert turn["response"] == "partial answer"
    # The archive contains the partial exactly once and no subagent text.
    archived = await pcm.archive_chat(chat.chat_id)
    assert archived is not None
    archive_text = archived.path.read_text(encoding="utf-8")
    assert archive_text.count("partial answer") == (0 if stop_before_text else 1)
    assert "subagent aside" not in archive_text

    consumer.cancel()
    release.set()


async def test_stop_does_not_hide_substantive_provider_error(
    tmp_path: Path,
) -> None:
    """A stop must not launder a real provider error into a clean partial."""
    release = asyncio.Event()

    def script(request):
        del request

        async def gen():
            yield AssistantTextDelta(type="text", text="partial answer")
            await release.wait()
            yield ResultEvent(
                type="result",
                result="Model refused the request.",
                session_id="sess-native",
                is_error=True,
                effective_model="opus",
            )

        return gen()

    (
        pcm,
        chat,
        stream,
        consumer,
        captured,
    ) = await _start_real_path_stream(
        tmp_path,
        provider="claude",
        user_text="please answer",
        release=release,
        script=script,
        project_name="stop-substantive",
        title="stop-substantive",
    )
    await _wait_for(lambda: any(e.get("type") == "text_delta" for e in captured))
    # The user stops, but the provider's terminal is a substantive error. The
    # stop must not rewrite it into a clean partial.
    assert await asyncio.wait_for(pcm.stop_chat(chat.chat_id), timeout=3.0) is True
    await _wait_for(lambda: stream.done)

    results = [e for e in captured if e.get("type") == "result"]
    assert len(results) == 1
    assert results[0].get("is_error") is True
    assert "stopped" not in results[0]
    assert results[0].get("text") == "Model refused the request."

    ctx = ChatContext.for_web(chat.chat_id)
    await _wait_for(lambda: bool(pcm._transcripts.current_messages(ctx, "claude")))
    rows = pcm._transcripts.current_messages(ctx, "claude")
    assert rows[-1]["content"] == "Model refused the request."
    assert rows[-1].get("is_error") is True
    assert pcm._chats[chat.chat_id].last_response_status == "error"
    consumer.cancel()


async def test_empty_provider_error_without_stop_stays_error(
    tmp_path: Path,
) -> None:
    """An empty provider error with no user stop is a failure, not a stop."""
    pcm = attach_stub_mcp(_make_manager(tmp_path))
    project = pcm.create_project("stop-no-stop", workspace="work")
    chat = pcm.create_chat(project.project_id, title="empty-error", provider="claude")
    release = asyncio.Event()
    release.set()

    def script(request):
        del request

        async def gen():
            yield ResultEvent(
                type="result",
                result="",
                session_id="sess-native",
                is_error=True,
                effective_model="opus",
            )

        return gen()

    pcm._providers[chat.chat_id] = _RealPathProviderService(chat, script, release)

    received = [event async for event in pcm.stream_chat(chat.chat_id, "hello")]
    assert len(received) == 1
    assert received[0].is_error is True
    assert received[0].stopped is False

    ctx = ChatContext.for_web(chat.chat_id)
    stored = pcm._transcripts._load_current(ctx, "claude")
    assert stored["turns"][-1]["is_error"] is True
    assert "is_partial" not in stored["turns"][-1]


async def test_stop_exception_persists_partial_before_archive(
    tmp_path: Path,
) -> None:
    """An abort-ack exception under a stop persists the partial; the drive
    loop's existing Stop handler publishes the one terminal frame."""
    release = asyncio.Event()

    def script(request):
        del request

        async def gen():
            yield AssistantTextDelta(type="text", text="partial answer")
            await release.wait()
            raise RuntimeError("request was aborted")

        return gen()

    (
        pcm,
        chat,
        stream,
        consumer,
        captured,
    ) = await _start_real_path_stream(
        tmp_path,
        provider="claude",
        user_text="please answer",
        release=release,
        script=script,
        project_name="stop-exc",
        title="stop-exc",
    )
    await _wait_for(lambda: any(e.get("type") == "text_delta" for e in captured))

    assert await asyncio.wait_for(pcm.stop_chat(chat.chat_id), timeout=3.0) is True
    await _wait_for(lambda: stream.done)

    results = [e for e in captured if e.get("type") == "result"]
    assert len(results) == 1
    assert results[0].get("stopped") is True
    assert results[0].get("is_error") is False
    assert results[0].get("text") == "partial answer"

    ctx = ChatContext.for_web(chat.chat_id)
    await _wait_for(lambda: bool(pcm._transcripts.current_messages(ctx, "claude")))
    rows = pcm._transcripts.current_messages(ctx, "claude")
    # One durable turn: the partial persisted once, not duplicated.
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert rows[1]["content"] == "partial answer"
    assert rows[1].get("partial") is True
    consumer.cancel()


async def test_stop_with_sdk_error_diagnostic_persists_streamed_partial(
    tmp_path: Path,
) -> None:
    """A stop whose provider terminal is an SDK diagnostic keeps the partial.

    The real SDK does not end an interrupted Claude turn on an *empty* error
    frame: the CLI exits non-zero after reporting a diagnostic, and the SDK
    surfaces it as a ``ResultError`` carrying ``[ede_diagnostic] ...``. #952
    only normalized the empty-error shape, so the internal string replaced the
    streamed partial in the durable turn. The streamed partial must win and no
    SDK internal may reach the transcript or the archive.
    """
    from claude_agent_sdk._errors import ResultError

    diagnostic = (
        "Claude Code returned an error result: [ede_diagnostic] "
        "result_type=user last_content_type=n/a stop_reason=null "
        "(exit code: 1)"
    )
    release = asyncio.Event()

    def script(request):
        del request

        async def gen():
            yield AssistantTextDelta(type="text", text="partial answer")
            await release.wait()
            # The CLI reports the terminal diagnostic as a non-empty error
            # result frame (``_convert_message`` joins ``ResultMessage.errors``
            # into ``result``) and then exits non-zero, which the SDK surfaces
            # as a ``ResultError``.
            yield ResultEvent(
                type="result",
                result=(
                    "[ede_diagnostic] result_type=user "
                    "last_content_type=n/a stop_reason=null"
                ),
                session_id="sess-native",
                is_error=True,
                effective_model="opus",
            )
            raise ResultError(diagnostic, exit_code=1)

        return gen()

    (
        pcm,
        chat,
        stream,
        consumer,
        captured,
    ) = await _start_real_path_stream(
        tmp_path,
        provider="claude",
        user_text="please answer",
        release=release,
        script=script,
        project_name="stop-sdk-diagnostic",
        title="stop-sdk-diagnostic",
    )
    await _wait_for(lambda: any(e.get("type") == "text_delta" for e in captured))

    assert await asyncio.wait_for(pcm.stop_chat(chat.chat_id), timeout=3.0) is True
    await _wait_for(lambda: stream.done)

    results = [e for e in captured if e.get("type") == "result"]
    assert len(results) == 1
    assert results[0].get("stopped") is True
    assert results[0].get("is_error") is False
    assert results[0].get("text") == "partial answer"

    ctx = ChatContext.for_web(chat.chat_id)
    await _wait_for(lambda: bool(pcm._transcripts.current_messages(ctx, "claude")))
    stored = pcm._transcripts._load_current(ctx, "claude")
    assert len(stored["turns"]) == 1
    turn = stored["turns"][-1]
    assert turn["is_partial"] is True
    assert turn["is_error"] is False
    assert turn["response"] == "partial answer"
    rows = pcm._transcripts.current_messages(ctx, "claude")
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert rows[1]["content"] == "partial answer"
    assert rows[1].get("partial") is True
    assert "is_error" not in rows[1]
    assert "ede_diagnostic" not in rows[1]["content"]

    archived = await pcm.archive_chat(chat.chat_id)
    assert archived is not None
    archive_text = archived.path.read_text(encoding="utf-8")
    assert "partial answer" in archive_text
    assert "ede_diagnostic" not in archive_text
    assert "ResultError" not in archive_text

    consumer.cancel()


async def test_stop_followup_does_not_inherit_stopped_state(
    tmp_path: Path,
) -> None:
    """After a stopped turn, the queued follow-up is a normal success."""
    release = asyncio.Event()

    def script(request):
        prompt = request.prompt

        async def gen():
            if "follow-up" in prompt:
                yield ResultEvent(
                    type="result",
                    result="follow-up answer",
                    session_id="sess-native",
                    is_error=False,
                    effective_model="opus",
                )
                return
            yield AssistantTextDelta(type="text", text="partial answer")
            await release.wait()
            yield ResultEvent(
                type="result",
                result="",
                session_id="sess-native",
                is_error=True,
                effective_model="opus",
            )

        return gen()

    (
        pcm,
        chat,
        stream,
        consumer,
        captured,
    ) = await _start_real_path_stream(
        tmp_path,
        provider="claude",
        user_text="please answer",
        release=release,
        script=script,
        project_name="stop-followup",
        title="stop-followup",
    )
    await _wait_for(lambda: any(e.get("type") == "text_delta" for e in captured))
    assert pcm.queue_message(chat.chat_id, "follow-up") is True

    assert await asyncio.wait_for(pcm.stop_chat(chat.chat_id), timeout=3.0) is True
    await _wait_for(
        lambda: len([e for e in captured if e.get("type") == "result"]) == 2
    )
    await _wait_for(lambda: stream.done)

    results = [e for e in captured if e.get("type") == "result"]
    assert results[0].get("stopped") is True
    assert "stopped" not in results[1]
    assert results[1].get("is_error") is False
    assert results[1].get("text") == "follow-up answer"
    assert pcm._chats[chat.chat_id].last_response_status == "success"
    consumer.cancel()


async def test_stopped_followup_does_not_reannounce_previous_answer(
    tmp_path: Path, monkeypatch
) -> None:
    """A stopped follow-up must not re-announce the previous turn's answer.

    ``last_assistant_text`` lives for the whole ``drive()`` session, so a
    stopped follow-up that produced no answer of its own used to leave it
    pointing at the prior turn's reply. The turn-done announce then fired
    ``chat_result_ready`` plus a push for the already-seen answer the user had
    just cancelled, and rewrote the chat's snippet and ``last_response_status``
    as a fresh success.
    """
    release = asyncio.Event()
    first_turn_release = asyncio.Event()

    def script(request):
        prompt = request.prompt

        async def gen():
            if "follow-up" in prompt:
                yield AssistantTextDelta(type="text", text="partial answer")
                await release.wait()
                yield ResultEvent(
                    type="result",
                    result="",
                    session_id="sess-native",
                    is_error=True,
                    effective_model="opus",
                )
                return
            # Held open so the follow-up queues against a live stream.
            await first_turn_release.wait()
            yield ResultEvent(
                type="result",
                result="ok",
                session_id="sess-native",
                is_error=False,
                effective_model="opus",
            )

        return gen()

    (
        pcm,
        chat,
        stream,
        consumer,
        captured,
    ) = await _start_real_path_stream(
        tmp_path,
        provider="claude",
        user_text="please answer",
        release=release,
        script=script,
        project_name="stop-reannounce",
        title="stop-reannounce",
    )
    assert pcm.queue_message(chat.chat_id, "follow-up") is True

    published: list[dict] = []
    pushes: list = []
    monkeypatch.setattr(pcm._events, "publish", published.append)
    monkeypatch.setattr(pcm, "_schedule_push", lambda *a, **k: pushes.append(a))

    def spawn(coro, name: str):
        if name.startswith("archive-proposal-helper") or name.startswith(
            "memory-pass-"
        ):
            coro.close()
            return None
        return asyncio.create_task(coro, name=name)

    monkeypatch.setattr(pcm, "_spawn_detached", spawn)

    first_turn_release.set()
    await _wait_for(
        lambda: any(
            e.get("type") == "text_delta" and e.get("text") == "partial answer"
            for e in captured
        )
    )
    assert await asyncio.wait_for(pcm.stop_chat(chat.chat_id), timeout=3.0) is True
    await _wait_for(lambda: stream.done)

    results = [e for e in captured if e.get("type") == "result"]
    assert len(results) == 2
    assert results[0].get("text") == "ok"
    assert "stopped" not in results[0]
    assert results[1].get("stopped") is True
    assert results[1].get("is_error") is False
    assert results[1].get("text") == "partial answer"

    # No announcement or push for the cancelled follow-up.
    assert pushes == []
    assert not any(ev.get("type") == "chat_result_ready" for ev in published)

    # The cancelled turn is not recorded as this chat's fresh answer.
    state = pcm._chats[chat.chat_id]
    assert state.last_response_status == "empty"
    assert state.last_snippet == ""

    ctx = ChatContext.for_web(chat.chat_id)
    stored = pcm._transcripts._load_current(ctx, "claude")
    turns = stored["turns"]
    assert len(turns) == 2
    assert turns[0]["response"] == "ok"
    assert "is_partial" not in turns[0]
    assert turns[1]["response"] == "partial answer"
    assert turns[1]["is_partial"] is True
    assert turns[1]["is_error"] is False
    rows = pcm._transcripts.current_messages(ctx, "claude")
    assert [row["role"] for row in rows] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert rows[-1]["content"] == "partial answer"
    assert rows[-1].get("partial") is True
    assert "is_error" not in rows[-1]

    consumer.cancel()


async def test_cancel_after_terminal_does_not_duplicate_result_or_transcript(
    tmp_path: Path,
) -> None:
    """A force-cancel landing after the terminal published must not add a
    second stopped frame or a duplicate transcript turn."""
    release = asyncio.Event()
    publish_terminal = asyncio.Event()
    hold_open = asyncio.Event()

    def script(request):
        del request

        async def gen():
            yield AssistantTextDelta(type="text", text="partial answer")
            await publish_terminal.wait()
            yield ResultEvent(
                type="result",
                result="",
                session_id="sess-native",
                is_error=True,
                effective_model="opus",
            )
            await hold_open.wait()

        return gen()

    (
        pcm,
        chat,
        stream,
        consumer,
        captured,
    ) = await _start_real_path_stream(
        tmp_path,
        provider="claude",
        user_text="please answer",
        release=release,
        script=script,
        project_name="stop-after-terminal",
        title="stop-after-terminal",
    )
    await _wait_for(lambda: any(e.get("type") == "text_delta" for e in captured))

    # Let the provider publish its terminal, then force-close the still-open
    # generator. The drive loop's synthetic result must not fire again.
    stream.user_stopped = True
    publish_terminal.set()
    await _wait_for(lambda: any(e.get("type") == "result" for e in captured))
    stream.force_closing = True
    if stream.turn_task is not None:
        stream.turn_task.cancel()
    hold_open.set()
    await _wait_for(lambda: stream.done)

    results = [e for e in captured if e.get("type") == "result"]
    assert len(results) == 1
    assert results[0].get("stopped") is True

    ctx = ChatContext.for_web(chat.chat_id)
    await _wait_for(lambda: bool(pcm._transcripts.current_messages(ctx, "claude")))
    stored = pcm._transcripts._load_current(ctx, "claude")
    assert len(stored["turns"]) == 1
    assert stored["turns"][0]["is_partial"] is True
    rows = pcm._transcripts.current_messages(ctx, "claude")
    assert [row["role"] for row in rows] == ["user", "assistant"]
    consumer.cancel()


async def test_stop_before_turn_task_prevents_turn(tmp_path: Path) -> None:
    """A Stop that lands before the turn task exists must still prevent the turn.

    The drive loop used to create the turn task unconditionally, so a Stop
    pressed in the start-up window set ``user_stopped`` and then watched the
    turn run normally. The pre-task check now consumes the flag and publishes a
    stopped result without ever invoking the provider (#1109).
    """
    pcm = _make_manager(tmp_path)
    # Same window as the board-stop pre-task test: wait_for_drive_cleanup does
    # a real state save, and 50ms returns False on a loaded Windows runner
    # before the drive has published the stopped result.
    pcm._STOP_GRACE_S = 2.0
    project = pcm.create_project("stop-early", workspace="work")
    chat = pcm.create_chat(project.project_id, title="stop-early", provider="claude")

    acked = asyncio.Event()
    disconnects: list[int] = []
    pcm._providers[chat.chat_id] = _fake_provider_service(acked, disconnects)

    turn_calls: list[str] = []

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        turn_calls.append(prompt)
        yield ResultEvent(
            type="result",
            result="should not run",
            session_id="sess-x",
            is_error=False,
            effective_model=chat.model,
            usage={},
            quota={},
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    captured: list[dict] = []

    async def consume(stream) -> None:
        async for ev in stream.subscribe():
            captured.append(ev)

    stream = pcm.start_stream(chat.chat_id, "initial")
    consumer = asyncio.create_task(consume(stream))

    # The Stop lands before the drive loop has created the turn task.
    assert stream.turn_task is None
    stopped = await asyncio.wait_for(pcm.stop_chat(chat.chat_id), timeout=2.0)
    assert stopped is True

    await _wait_for(lambda: stream.done)
    # The provider was never invoked and the stop flag was consumed.
    assert turn_calls == []
    assert stream.user_stopped is False
    results = [e for e in captured if e.get("type") == "result"]
    assert len(results) == 1
    assert results[0].get("stopped") is True
    assert results[0].get("is_error") is False

    consumer.cancel()


async def test_stop_cancels_armed_retry(tmp_path: Path) -> None:
    """A Stop cancels a retry armed for the chat, so the prompt cannot replay.

    A turn that errored with a retryable failure leaves no live stream, so the
    retry cancel cannot ride on the live-stream branch. ``stop_chat`` must
    clear the armed retry unconditionally (#1109).
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("stop-retry", workspace="work")
    chat = pcm.create_chat(project.project_id, title="stop-retry", provider="claude")

    pcm.set_chat_retry(chat.chat_id, "replay me", reason="quota limit")
    assert pcm.get_chat(chat.chat_id).retry_status == "pending"

    stopped = await asyncio.wait_for(pcm.stop_chat(chat.chat_id), timeout=2.0)
    assert stopped is False  # no turn was running

    updated = pcm.get_chat(chat.chat_id)
    assert updated.retry_status == "stopped"
    assert updated.retry_prompt == ""
    # The cleared retry will not replay: the idle-replay entrypoint refuses it.
    assert pcm.try_chat_retry_now(chat.chat_id) is None


async def test_plain_stop_without_armed_retry_leaves_retry_untouched(
    tmp_path: Path, monkeypatch
) -> None:
    """A plain Stop with no armed retry must not stamp ``retry_status="stopped"``.

    ``stop_chat`` used to call ``stop_chat_retry`` unconditionally, which
    clears the retry with ``status="stopped"``, saves, and publishes a
    ``chat_retry`` event even when nothing was pending. Nothing resets
    ``"stopped"`` back to ``""``, so the archive-proposal helper and the
    memory pass refused forever after any plain Stop.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("stop-noretry", workspace="work")
    chat = pcm.create_chat(project.project_id, title="stop-noretry", provider="claude")

    published: list[dict] = []
    monkeypatch.setattr(pcm._events, "publish", published.append)

    stopped = await asyncio.wait_for(pcm.stop_chat(chat.chat_id), timeout=2.0)
    assert stopped is False  # no turn was running

    updated = pcm.get_chat(chat.chat_id)
    assert updated.retry_status == ""
    assert updated.retry_prompt == ""
    # No retry was armed, so nothing may have published a chat_retry event.
    assert not any(ev.get("type") == "chat_retry" for ev in published)


async def test_a_board_stop_in_the_pre_task_window_parks_the_queue(
    tmp_path: Path,
) -> None:
    """A board Stop landing before the turn task exists must park the queue.

    ``park_queue=True`` sets both ``user_stopped`` and ``park_on_stop``. The
    pre-task guard used to consume ``user_stopped`` before the bottom-of-loop
    ``park_rest`` read it, so the first queued follow-up ran as a normal turn
    instead of being parked (#1103). The guard must publish the stopped result
    but leave the flag for the loop to honour the park.
    """
    pcm = _make_manager(tmp_path)
    # A realistic grace: the pre-task path returns wait_for_drive_cleanup, which
    # does a real state save, so a sub-100ms window races the drive task on a
    # loaded runner (notably Windows) and returns False spuriously. There is no
    # hung provider to force-close here, so the production default costs nothing.
    pcm._STOP_GRACE_S = 2.0
    project = pcm.create_project("stop-early-park", workspace="work")
    chat = pcm.create_chat(
        project.project_id, title="stop-early-park", provider="claude"
    )

    acked = asyncio.Event()
    disconnects: list[int] = []
    pcm._providers[chat.chat_id] = _fake_provider_service(acked, disconnects)

    turn_calls: list[str] = []

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        turn_calls.append(prompt)
        yield ResultEvent(
            type="result",
            result="should not run",
            session_id="sess-x",
            is_error=False,
            effective_model=chat.model,
            usage={},
            quota={},
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    captured: list[dict] = []

    async def consume(stream) -> None:
        async for ev in stream.subscribe():
            captured.append(ev)

    stream = pcm.start_stream(chat.chat_id, "initial")
    consumer = asyncio.create_task(consume(stream))

    # The Stop lands before the drive loop has created the turn task.
    assert stream.turn_task is None
    assert pcm.queue_message(chat.chat_id, "follow-up") is True
    stopped = await asyncio.wait_for(
        pcm.stop_chat(chat.chat_id, park_queue=True), timeout=2.0
    )
    assert stopped is True

    await _wait_for(lambda: stream.done)
    # The provider was never invoked: the queued follow-up was parked, not run.
    assert turn_calls == []
    results = [e for e in captured if e.get("type") == "result"]
    assert len(results) == 1
    assert results[0].get("stopped") is True
    assert [e["text"] for e in pcm.get_chat(chat.chat_id).pending_queue] == [
        "follow-up"
    ]

    consumer.cancel()
