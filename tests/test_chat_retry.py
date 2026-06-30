from __future__ import annotations

import asyncio
from pathlib import Path

from ciao.config import CiaoConfig
from ciao.models import ResultEvent
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
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )


async def _consume(stream) -> list[dict]:
    events: list[dict] = []
    async for event in stream.subscribe():
        events.append(event)
    return events


async def test_quota_error_marks_turn_for_hourly_retry(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")

    async def fake_stream_chat(chat_id, prompt, images=None):
        yield ResultEvent(
            type="result",
            result="API Error: Request rejected (429): reached your session usage limit",
            session_id="sess-retry",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    events = await asyncio.wait_for(_consume(stream), timeout=2.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status == "pending"
    assert updated.retry_prompt == "do the thing"
    assert updated.retry_attempts == 0
    assert updated.retry_next_at
    assert updated.retry_interval_seconds == 3600
    assert any(e.get("type") == "chat_retry" and e.get("status") == "pending" for e in events)
    pcm.stop_chat_retry(chat.chat_id)


def test_manual_retry_can_be_set_and_stopped(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")

    updated = pcm.set_chat_retry(chat.chat_id, "retry this manually", reason="manual")
    assert updated is not None
    assert updated.retry_status == "pending"
    assert updated.retry_prompt == "retry this manually"
    assert updated.retry_last_error == "manual"

    stopped = pcm.stop_chat_retry(chat.chat_id)
    assert stopped is not None
    assert stopped.retry_status == "stopped"
    assert stopped.retry_prompt == ""


async def test_try_retry_now_starts_stream_when_chat_is_idle(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    pcm.set_chat_retry(chat.chat_id, "run me", reason="manual")

    calls: list[str] = []

    async def fake_stream_chat(chat_id, prompt, images=None):
        calls.append(prompt)
        yield ResultEvent(
            type="result",
            result="done",
            session_id="sess-ok",
            is_error=False,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.try_chat_retry_now(chat.chat_id)
    assert stream is not None
    await asyncio.wait_for(_consume(stream), timeout=2.0)

    assert calls == ["run me"]
    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status == ""
    assert updated.retry_prompt == ""


async def test_try_retry_now_refuses_active_chat(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    pcm.set_chat_retry(chat.chat_id, "run later", reason="manual")

    release = asyncio.Event()

    async def fake_stream_chat(chat_id, prompt, images=None):
        await release.wait()
        yield ResultEvent(
            type="result",
            result="done",
            session_id="sess-active",
            is_error=False,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    active_stream = pcm.start_stream(chat.chat_id, "already running")
    consumer = asyncio.create_task(_consume(active_stream))
    await asyncio.sleep(0.01)

    assert pcm.try_chat_retry_now(chat.chat_id) is None

    release.set()
    await asyncio.wait_for(consumer, timeout=2.0)
