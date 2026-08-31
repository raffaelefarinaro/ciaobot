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
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager


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

    # Force closed fast (well under any human-perceivable "not instant").
    await _wait_for(
        lambda: any(
            e.get("type") == "result" and e.get("stopped") for e in captured
        )
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

    abort_issued.set()
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
