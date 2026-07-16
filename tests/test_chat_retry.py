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


# ── Auto tier-fallback on capability errors (ciao/web/project_chats.py) ──


async def test_capability_error_triggers_tier_fallback(tmp_path: Path) -> None:
    """A 4xx saying 'does not support image input' should auto-retry the next tier.

    This is the screenshot scenario: the chat was sent to a model that
    cannot handle images, the provider returned a 400, and the engine
    silently retried on the next tier in the configured ladder.
    """
    pcm = _make_manager(tmp_path)
    # Pin OllamaSettings to the user's actual config (kimi5.2 = fable,
    # minimax-m3 = opus) so the retry target resolves to a real model.
    pcm._config.ollama = pcm._config.ollama.__class__(
        haiku_model="deepseek-v4-flash:cloud",
        sonnet_model="kimi-k2.7-code:cloud",
        opus_model="minimax-m3:cloud",
        fable_model="kimi5.2:cloud",
    )
    project = pcm.create_project("tier-fallback", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="tier-fallback-test")
    # Pin the chat to the Ollama fable model so intended_backend
    # resolves to "ollama" and the retry path is enabled.
    chat.model = "kimi5.2:cloud"
    pcm._save()

    seen_models: list[str] = []

    from ciao.web.project_chats import _StreamOutcome

    async def fake_drive(*, chat_id, request):
        seen_models.append(request.model)
        if request.model == "kimi5.2:cloud":
            return _StreamOutcome(
                events=[
                    ResultEvent(
                        type="result",
                        result=(
                            "API Error: 400 this model does not support image "
                            "input (ref: test)"
                        ),
                        session_id="sess-1",
                        is_error=True,
                        effective_model="kimi5.2:cloud",
                        usage={},
                        quota={},
                        cost_usd=0.0,
                    )
                ],
                response_text=(
                    "API Error: 400 this model does not support image input"
                ),
                had_error=True,
                effective_model="kimi5.2:cloud",
                usage={},
                quota={},
                cost_usd=0.0,
                tool_events=[],
            )
        # Second attempt on the next tier succeeds.
        return _StreamOutcome(
            events=[
                ResultEvent(
                    type="result",
                    result="Here is my answer about the image.",
                    session_id="sess-2",
                    is_error=False,
                    effective_model=request.model,
                    usage={},
                    quota={},
                    cost_usd=0.0,
                )
            ],
            response_text="Here is my answer about the image.",
            had_error=False,
            effective_model=request.model,
            usage={},
            quota={},
            cost_usd=0.0,
            tool_events=[],
        )

    pcm._drive_stream = fake_drive  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "what is in the image?")
    events = await asyncio.wait_for(_consume(stream), timeout=2.0)

    # First attempt fired on kimi5.2; retry attempted on minimax-m3
    # (configured Ollama opus slot). Both must appear in the seen list.
    assert seen_models[0] == "kimi5.2:cloud"
    assert seen_models[-1] == "minimax-m3:cloud"
    # A status line was emitted between the two attempts so the PWA
    # shows the user what happened.
    status_events = [
        e
        for e in events
        if e.get("type") == "status" and "retrying" in (e.get("message") or "").lower()
    ]
    assert status_events, f"expected a 'retrying' status event, got {events}"
    # The terminal result is the second attempt's success.
    result_events = [e for e in events if e.get("type") == "result"]
    assert result_events
    final = result_events[-1]
    assert final.get("is_error") is False
    assert "Here is my answer" in final.get("text", "")


async def test_rate_limit_does_not_trigger_tier_fallback(tmp_path: Path) -> None:
    """Rate limit errors are NOT capability errors — no retry, error surfaces."""
    pcm = _make_manager(tmp_path)
    pcm._config.ollama = pcm._config.ollama.__class__(
        haiku_model="deepseek-v4-flash:cloud",
        sonnet_model="kimi-k2.7-code:cloud",
        opus_model="minimax-m3:cloud",
        fable_model="kimi5.2:cloud",
    )
    project = pcm.create_project("tier-fallback-no-retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="tier-fallback-no-retry-test")
    chat.model = "kimi5.2:cloud"
    pcm._save()

    drive_calls: list[str] = []

    from ciao.web.project_chats import _StreamOutcome

    async def fake_drive(*, chat_id, request):
        drive_calls.append(request.model)
        return _StreamOutcome(
            events=[
                ResultEvent(
                    type="result",
                    result="API Error: 429 Rate Limit Exceeded",
                    session_id="sess-rl",
                    is_error=True,
                    effective_model=request.model,
                    usage={},
                    quota={},
                    cost_usd=0.0,
                )
            ],
            response_text="API Error: 429 Rate Limit Exceeded",
            had_error=True,
            effective_model=request.model,
            usage={},
            quota={},
            cost_usd=0.0,
            tool_events=[],
        )

    pcm._drive_stream = fake_drive  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    events = await asyncio.wait_for(_consume(stream), timeout=2.0)

    # _drive_stream is only called once — the retry is gated on
    # is_capability_error, which rejects rate limits.
    assert drive_calls == ["kimi5.2:cloud"]
    result_events = [e for e in events if e.get("type") == "result"]
    assert result_events
    assert result_events[-1].get("is_error") is True


async def test_connection_error_marks_turn_for_fast_retry(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")

    async def fake_stream_chat(chat_id, prompt, images=None):
        yield ResultEvent(
            type="result",
            result="API Error: Unable to connect to API (ENOTFOUND)",
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
    assert updated.retry_interval_seconds == 30
    assert any(e.get("type") == "chat_retry" and e.get("status") == "pending" for e in events)
    pcm.stop_chat_retry(chat.chat_id)


async def test_connection_error_exception_marks_turn_for_fast_retry(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")

    async def fake_stream_chat(chat_id, prompt, images=None):
        raise Exception("Unable to connect to API (ENOTFOUND)")
        yield ResultEvent(type="result", result="not reached")  # type: ignore[unreachable]

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    events = await asyncio.wait_for(_consume(stream), timeout=2.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status == "pending"
    assert updated.retry_prompt == "do the thing"
    assert updated.retry_interval_seconds == 30
    pcm.stop_chat_retry(chat.chat_id)
