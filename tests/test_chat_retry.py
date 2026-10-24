from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ciao.config import CiaoConfig
from ciao.models import ChatContext, ResultEvent
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web import chat_service
from ciao.web.project_chats import ProjectChatManager

from tests.conftest import attach_stub_mcp


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
    return attach_stub_mcp(ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    ))


async def _consume(stream) -> list[dict]:
    events: list[dict] = []
    async for event in stream.subscribe():
        events.append(event)
    return events


async def test_quota_error_marks_turn_for_hourly_retry(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
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


def test_manual_retry_is_hourly_after_a_daily_automatic_retry(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    chat.retry_interval_seconds = 24 * 60 * 60

    updated = pcm.set_chat_retry(chat.chat_id, "retry this hourly", reason="manual")

    assert updated is not None
    assert updated.retry_interval_seconds == 60 * 60


async def test_try_retry_now_starts_stream_when_chat_is_idle(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    pcm.set_chat_retry(chat.chat_id, "run me", reason="manual")

    calls: list[str] = []

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
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

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
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
    """A 4xx saying 'does not support image input' surfaces as an error result.

    The auto tier-fallback ladder was removed; a capability error is now
    reported to the PWA as the single result of the turn.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("tier-fallback", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="tier-fallback-test")
    chat.model = "fable"
    pcm._save()
    # This test exercises the dispatch path, not the pre-flight, so let the
    # turn through as if the pre-flight approved the model.
    async def _always_capable(model, chat):
        return True

    pcm._model_capable = _always_capable  # type: ignore[assignment]

    seen_models: list[str] = []

    async def fake_drive(*, chat_id, request, outcome):
        seen_models.append(request.model)
        outcome.response_text = "API Error: 400 this model does not support image input"
        outcome.had_error = True
        outcome.effective_model = request.model
        evt = ResultEvent(
            type="result",
            result=(
                "API Error: 400 this model does not support image "
                "input (ref: test)"
            ),
            session_id="sess-1",
            is_error=True,
            effective_model=request.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )
        outcome.events.append(evt)
        yield evt

    pcm._drive_stream = fake_drive  # type: ignore[assignment]

    from ciao.models import ImageAttachment

    stream = pcm.start_stream(
        chat.chat_id,
        "what is in the image?",
        images=[
            ImageAttachment(
                path=tmp_path / "shot.png",
                mime_type="image/png",
                original_filename="shot.png",
            )
        ],
    )
    events = await asyncio.wait_for(_consume(stream), timeout=2.0)

    # Only the original model is dispatched; no silent retry.
    assert seen_models == ["fable"]
    # Exactly one result — the error, surfaced to the PWA.
    result_events = [e for e in events if e.get("type") == "result"]
    assert len(result_events) == 1, f"expected exactly one result, got {result_events}"
    final = result_events[0]
    assert final.get("is_error") is True
    assert "does not support image" in final.get("text", "")
    # No model_changed event: the model was not swapped.
    model_changed = [e for e in events if e.get("type") == "model_changed"]
    assert model_changed == []
    # The chat keeps its original model.
    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.model == "fable"


async def test_rate_limit_does_not_trigger_tier_fallback(tmp_path: Path) -> None:
    """Rate limit errors are NOT capability errors — no retry, error surfaces."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("tier-fallback-no-retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="tier-fallback-no-retry-test")
    chat.model = "fable"
    pcm._save()

    drive_calls: list[str] = []

    async def fake_drive(*, chat_id, request, outcome):
        drive_calls.append(request.model)
        outcome.response_text = "API Error: 429 Rate Limit Exceeded"
        outcome.had_error = True
        outcome.effective_model = request.model
        evt = ResultEvent(
            type="result",
            result="API Error: 429 Rate Limit Exceeded",
            session_id="sess-rl",
            is_error=True,
            effective_model=request.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )
        outcome.events.append(evt)
        yield evt

    pcm._drive_stream = fake_drive  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    events = await asyncio.wait_for(_consume(stream), timeout=2.0)

    # _drive_stream is only called once — the retry is gated on
    # is_capability_error, which rejects rate limits.
    assert drive_calls == ["fable"]
    result_events = [e for e in events if e.get("type") == "result"]
    assert result_events
    assert result_events[-1].get("is_error") is True
    # No model_changed when fallback does not run.
    assert not any(e.get("type") == "model_changed" for e in events)
    # Chat model stays on the original.
    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.model == "fable"


async def test_connection_error_marks_turn_for_fast_retry(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
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

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
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
    assert any(
        event.get("type") == "chat_retry" and event.get("status") == "pending"
        for event in events
    )
    pcm.stop_chat_retry(chat.chat_id)


def test_connection_drop_banner_classified_as_connection_error() -> None:
    """The CLI mid-response drop banner must be a retryable connection error."""
    from ciao.web.chat_service import (
        _is_retryable_connection_error,
        _is_retryable_provider_startup_error,
        _is_retryable_quota_error,
    )

    banner = (
        "API Error: Connection closed mid-response. The response above may "
        "be incomplete."
    )
    assert _is_retryable_connection_error(banner) is True
    assert _is_retryable_provider_startup_error(
        "opencode serve exited with code 1: database is locked"
    ) is True
    assert _is_retryable_provider_startup_error(
        "the model said database is locked"
    ) is False
    # A server that stayed alive but never answered /api/info is the
    # same transient startup wedge (shared SQLite contention) and gets the
    # same bounded auto-retry.
    assert _is_retryable_provider_startup_error(
        "opencode serve did not become healthy: "
        "server stayed alive but never answered /api/info"
    ) is True
    # It is NOT a quota error — must not be routed to the hourly retry path.
    assert _is_retryable_quota_error(banner) is False

    # Out of credits / spend limits must be classified as retryable quota errors
    credits_err = "Title generation failed: Your workspace is out of credits. Ask your workspace owner to refill in order to continue."
    spend_limit_err = "You've hit your org's monthly spend limit"
    rate_limit_err = "API Error: Request rejected (429): rate limit exceeded"
    claude_code_session_limit = (
        "You've hit your session limit - resets 2:20pm (Europe/Zurich)"
    )
    assert _is_retryable_quota_error(credits_err) is True
    assert _is_retryable_quota_error(spend_limit_err) is True
    assert _is_retryable_quota_error(rate_limit_err) is True
    assert _is_retryable_quota_error(claude_code_session_limit) is True


async def test_opencode_startup_error_is_persisted_and_retried(tmp_path: Path) -> None:
    """Pre-session provider failures must not leave an empty scheduled chat."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(
        project.project_id,
        title="retry-test",
        provider="opencode",
    )

    async def fail_before_provider_session(*_args, **_kwargs):
        if False:  # pragma: no cover - keeps this a minimal async generator
            yield None
        raise RuntimeError("opencode serve exited with code 1: database is locked")

    pcm.stream_chat = fail_before_provider_session  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "run the scheduled review")
    events = await asyncio.wait_for(_consume(stream), timeout=2.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status == "pending"
    assert updated.retry_interval_seconds == 30
    assert any(
        event.get("type") == "result"
        and event.get("is_error") is True
        and "database is locked" in event.get("text", "")
        for event in events
    )

    rows = pcm._transcripts.current_messages(
        ChatContext.for_web(chat.chat_id), "opencode"
    )
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert rows[-1]["is_error"] is True
    assert "database is locked" in rows[-1]["content"]
    pcm.stop_chat_retry(chat.chat_id)



def test_provider_detects_connection_drop_text() -> None:
    from ciao.providers.claude import _is_connection_drop_text

    assert _is_connection_drop_text(
        "API Error: Connection closed mid-response. The response above may "
        "be incomplete."
    ) is True
    assert _is_connection_drop_text("here is a normal answer") is False


async def test_midresponse_drop_resumes_with_continue_not_replay(tmp_path: Path) -> None:
    """A drop AFTER streaming output resumes with "continue", not the prompt.

    Replaying the original prompt could re-run tool calls the partial turn
    already executed, so with provider progress the retry must ask the
    resumed session to continue instead. It must also fire on the fast (30s)
    interval and preserve any queued follow-ups.
    """
    from ciao.models import AssistantTextDelta

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    # A mid-response drop implies a live session to resume. In production
    # _drive_stream persists this from the event; the fake here bypasses it.
    chat.session_id = "sess-drop"
    pcm._save()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        # Stream some output first so had_provider_progress flips True.
        yield AssistantTextDelta(type="assistant", text="Working on it… ")
        yield ResultEvent(
            type="result",
            result=(
                "API Error: Connection closed mid-response. The response "
                "above may be incomplete."
            ),
            session_id="sess-drop",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the big multi-step task")
    # Queue a follow-up while the turn is (about to be) in flight.
    stream.enqueue("and then summarise it", entry_id="q-follow")
    await asyncio.wait_for(_consume(stream), timeout=2.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status == "pending"
    # Resume-continue, NOT a replay of the original prompt.
    assert updated.retry_prompt == "continue"
    assert updated.retry_interval_seconds == 30
    # The queued follow-up survived the errored turn (parked for the retry).
    assert [e.get("text") for e in updated.pending_queue] == ["and then summarise it"]
    pcm.stop_chat_retry(chat.chat_id)


async def test_quota_error_with_progress_resumes_continue(tmp_path: Path) -> None:
    """A usage-limit error that lands mid-turn (after output streamed) must
    resume the live session with "continue" rather than replaying the prompt,
    which would re-run any tool calls the partial turn already executed."""
    from ciao.models import AssistantTextDelta

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    # A mid-turn limit implies a live session to resume (see the drop test).
    chat.session_id = "sess-live"
    pcm._save()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        # A tool ran / text streamed before the limit hit → had progress.
        yield AssistantTextDelta(type="assistant", text="sending the email… ")
        yield ResultEvent(
            type="result",
            result="API Error: Request rejected (429): reached your session usage limit",
            session_id="sess-live",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "send the email then reply")
    await asyncio.wait_for(_consume(stream), timeout=2.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status == "pending"
    # Resume-continue, NOT a replay that would re-send the email.
    assert updated.retry_prompt == "continue"
    # Still gated by the hourly quota interval, not the fast connection one.
    assert updated.retry_interval_seconds == 3600
    pcm.stop_chat_retry(chat.chat_id)


async def test_non_billing_rate_limit_with_progress_resumes_continue(tmp_path: Path) -> None:
    """A plain (non-billing) rate-limit 429, e.g. a provider's weekly usage
    limit, landing mid-turn with a live session must also resume with
    "continue" rather than being dropped without any retry armed."""
    from ciao.models import AssistantTextDelta

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    chat.session_id = "sess-live"
    pcm._save()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield AssistantTextDelta(type="assistant", text="editing files… ")
        yield ResultEvent(
            type="result",
            result="API Error: Request rejected (429) · rate limit exceeded, try again later",
            session_id="sess-live",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "fix the bug and run the tests")
    await asyncio.wait_for(_consume(stream), timeout=2.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status == "pending"
    assert updated.retry_prompt == "continue"
    assert updated.retry_interval_seconds == 3600
    pcm.stop_chat_retry(chat.chat_id)


async def test_midresponse_drop_without_session_falls_back_to_replay(tmp_path: Path) -> None:
    """With progress but no session to resume, replay the prompt, not "continue"."""
    from ciao.models import AssistantTextDelta

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    # No session_id set — nothing to resume.

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield AssistantTextDelta(type="assistant", text="starting… ")
        yield ResultEvent(
            type="result",
            result="API Error: Connection closed mid-response.",
            session_id="",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    await asyncio.wait_for(_consume(stream), timeout=2.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status == "pending"
    assert updated.retry_prompt == "do the thing"
    assert updated.retry_interval_seconds == 30
    pcm.stop_chat_retry(chat.chat_id)


async def test_midresponse_drop_stops_after_retry_cap(tmp_path: Path) -> None:
    """Once the connection-drop retry cap is hit, no further retry is armed."""
    from ciao.models import AssistantTextDelta
    from ciao.web.project_chats import _MAX_CONNECTION_DROP_RETRIES

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    # Simulate a live session plus an already-exhausted retry budget.
    chat.session_id = "sess-drop"
    chat.retry_attempts = _MAX_CONNECTION_DROP_RETRIES
    chat.retry_kind = "connection"
    pcm._save()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield AssistantTextDelta(type="assistant", text="partial… ")
        yield ResultEvent(
            type="result",
            result="API Error: Connection closed mid-response.",
            session_id="sess-drop",
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
    # No retry armed past the cap.
    assert updated.retry_status != "pending"
    assert not any(
        e.get("type") == "chat_retry" and e.get("status") == "pending" for e in events
    )


async def test_billing_spend_limit_error_with_progress_arms_retry(tmp_path: Path) -> None:
    """Billing and spend limit errors must trigger auto-retry even after provider progress."""
    from ciao.models import AssistantTextDelta

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield AssistantTextDelta(type="assistant", text="some progress... ")
        yield ResultEvent(
            type="result",
            result="You've hit your org's monthly spend limit · run /usage-credits to ask your admin for a higher limit",
            session_id="sess-billing",
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
    assert any(
        e.get("type") == "chat_retry" and e.get("status") == "pending" for e in events
    )
    pcm.stop_chat_retry(chat.chat_id)


async def test_rate_limit_error_with_progress_arms_retry(tmp_path: Path) -> None:
    """A plain rate-limit 429 must auto-retry after progress too, same as a
    billing/spend-limit error — the earlier carve-out that skipped arming
    for non-billing quota errors once anything had streamed left users with
    a bare error and no retry affordance for the common "usage limit" case.
    Without a session to resume it falls back to replaying the prompt, same
    as the no-session connection-drop case."""
    from ciao.models import AssistantTextDelta

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield AssistantTextDelta(type="assistant", text="some progress... ")
        yield ResultEvent(
            type="result",
            result="API Error: Request rejected (429): rate limit exceeded",
            session_id="sess-rate",
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
    assert any(
        e.get("type") == "chat_retry" and e.get("status") == "pending" for e in events
    )
    pcm.stop_chat_retry(chat.chat_id)


def test_a_plainly_stated_usage_limit_arms_the_hourly_retry() -> None:
    """opencode/OpenAI states the limit without a 429 or a vendor phrasing.

    "The usage limit has been reached" contains "usage limit", but that needle
    used to sit behind a `429`/`too many requests` gate, so the turn fell
    through to unknown: no hourly retry armed, and the user had to sit on the
    chat and press Retry by hand.
    """
    from ciao.web.chat_service import _is_retryable_quota_error

    assert _is_retryable_quota_error("The usage limit has been reached") is True
    assert _is_retryable_quota_error("Usage limit reached") is True
    assert _is_retryable_quota_error("You have exceeded your quota") is True
    assert _is_retryable_quota_error("Token limit exhausted for this key") is True


def test_a_tool_quota_error_does_not_arm_the_provider_retry() -> None:
    """"quota" alone is a noun tools and the OS use too, so it is not a provider limit.

    "disk quota exceeded" ended a turn and armed the hourly provider retry,
    re-running the turn against a problem that is not a provider limit. A
    quota counts only when it is the caller's own (exceeded your quota) or a
    named provider's (OpenAI quota exceeded).
    """
    from ciao.web.chat_service import _is_retryable_quota_error

    assert _is_retryable_quota_error("OSError: [Errno 122] Disk quota exceeded") is False
    assert (
        _is_retryable_quota_error(
            "Google API quota exceeded for quota metric 'Read requests' and limit 'Read requests per minute'"
        )
        is False
    )
    assert _is_retryable_quota_error("storage quota reached") is False
    assert _is_retryable_quota_error("You have exceeded your daily request quota for this API") is False
    assert (
        _is_retryable_quota_error(
            "You exceeded your current quota, please check your plan and billing details"
        )
        is True
    )
    assert _is_retryable_quota_error("OpenAI quota exceeded") is True
    assert _is_retryable_quota_error("API Error: 429 Too Many Requests: quota") is True


def test_a_limit_noun_alone_still_needs_the_429_marker() -> None:
    """The gate exists so ordinary prose mentioning a quota is not a quota error.

    Only pairing the noun with an exhaustion verb bypasses it; the bare noun
    must still carry the 429 / too-many-requests marker.
    """
    from ciao.web.chat_service import _is_retryable_quota_error

    assert _is_retryable_quota_error("I checked the quota settings for you") is False
    assert _is_retryable_quota_error("the session was restored") is False
    assert _is_retryable_quota_error("explain how rate limit headers work") is False


OLLAMA_WEEKLY_LIMIT = (
    "API Error: Request rejected (429) · you (x) have reached your weekly usage "
    "limit, upgrade for higher limits: https://ollama.com/upgrade"
)


def test_weekly_usage_limit_is_terminal_not_hourly() -> None:
    """A weekly limit resets days out; it takes the daily retry, never the hourly one."""
    from ciao.web.chat_service import (
        _is_retryable_quota_error,
        _is_terminal_quota_error,
    )

    assert _is_terminal_quota_error(OLLAMA_WEEKLY_LIMIT) is True
    assert _is_retryable_quota_error(OLLAMA_WEEKLY_LIMIT) is False
    assert _is_terminal_quota_error("You've hit your monthly limit") is True
    assert _is_retryable_quota_error("You've hit your monthly limit") is False


def test_session_limit_and_capacity_stay_retryable() -> None:
    from ciao.web.chat_service import (
        _is_terminal_quota_error,
        _is_retryable_quota_error,
    )

    session = "API Error: Request rejected (429) · you have reached your session usage limit"
    capacity = "Model is at capacity, try again shortly"
    for text in (session, capacity, "You've hit your session limit - resets 2:20pm"):
        assert _is_terminal_quota_error(text) is False
        assert _is_retryable_quota_error(text) is True


async def test_weekly_limit_error_arms_daily_retry(tmp_path: Path) -> None:
    """A weekly limit surfaces as an error and arms the once-a-day retry."""
    from ciao.models import AssistantTextDelta

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield AssistantTextDelta(type="assistant", text="partial… ")
        yield ResultEvent(
            type="result",
            result=OLLAMA_WEEKLY_LIMIT,
            session_id="",
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
    assert updated.retry_kind == "quota_terminal"
    assert updated.retry_interval_seconds == 86400
    assert any(e.get("type") == "chat_retry" and e.get("status") == "pending" for e in events)
    assert any(
        e.get("type") == "result" and e.get("is_error") for e in events
    )
    pcm.stop_chat_retry(chat.chat_id)


async def test_terminal_quota_retries_stop_after_cap(tmp_path: Path) -> None:
    """After _MAX_TERMINAL_QUOTA_RETRIES daily retries, a further weekly limit arms nothing."""
    from ciao.web.project_chats import _MAX_TERMINAL_QUOTA_RETRIES

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    chat.retry_attempts = _MAX_TERMINAL_QUOTA_RETRIES
    chat.retry_kind = "quota_terminal"
    pcm._save()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield ResultEvent(
            type="result",
            result=OLLAMA_WEEKLY_LIMIT,
            session_id="",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    await asyncio.wait_for(_consume(stream), timeout=2.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status != "pending"


def test_terminal_quota_reset_at_uses_claude_weekly_bucket(tmp_path: Path) -> None:
    """The daily retry lands just after a rejected Claude weekly bucket resets."""
    import json
    import time

    from ciao.rate_limits import default_store_path

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test", provider="claude")
    store = default_store_path(pcm._runtime_root)
    store.parent.mkdir(parents=True, exist_ok=True)
    resets_at = int(time.time()) + 3 * 86400
    store.write_text(
        json.dumps({
            "buckets": {
                "seven_day": {"status": "rejected", "resets_at": resets_at},
                "five_hour": {"status": "allowed", "resets_at": resets_at + 60},
            },
        }),
        encoding="utf-8",
    )

    from datetime import UTC, datetime

    expected = datetime.fromtimestamp(resets_at + 60, UTC).replace(microsecond=0)
    assert pcm._quota_reset_at(chat, "quota_terminal") == (
        expected.isoformat().replace("+00:00", "Z")
    )

    chat.provider = "opencode"
    assert pcm._quota_reset_at(chat, "quota_terminal") is None
    chat.provider = "claude"

    store.write_text(
        json.dumps({"buckets": {"seven_day": {"status": "allowed", "resets_at": resets_at}}}),
        encoding="utf-8",
    )
    assert pcm._quota_reset_at(chat, "quota_terminal") is None

    store.write_text(
        json.dumps({"buckets": {"seven_day": {"status": "rejected", "resets_at": 1}}}),
        encoding="utf-8",
    )
    assert pcm._quota_reset_at(chat, "quota_terminal") is None

    store.write_text("{not json", encoding="utf-8")
    assert pcm._quota_reset_at(chat, "quota_terminal") is None


def test_quota_reset_at_uses_claude_five_hour_bucket_for_session_limits(tmp_path: Path) -> None:
    """An hourly-kind quota retry lands just after a rejected five-hour bucket resets."""
    import json
    import time
    from datetime import UTC, datetime

    from ciao.rate_limits import default_store_path

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test", provider="claude")
    store = default_store_path(pcm._runtime_root)
    store.parent.mkdir(parents=True, exist_ok=True)
    resets_at = int(time.time()) + 2 * 3600
    store.write_text(
        json.dumps({
            "buckets": {
                "five_hour": {"status": "rejected", "resets_at": resets_at},
                "seven_day": {"status": "allowed", "resets_at": resets_at + 86400},
            },
        }),
        encoding="utf-8",
    )

    expected = datetime.fromtimestamp(resets_at + 60, UTC).replace(microsecond=0)
    assert pcm._quota_reset_at(chat, "quota") == expected.isoformat().replace("+00:00", "Z")
    # The weekly bucket is not rejected, and other kinds read no bucket at all.
    assert pcm._quota_reset_at(chat, "quota_terminal") is None
    assert pcm._quota_reset_at(chat, "connection") is None


def test_quota_retry_interval_backs_off_and_caps() -> None:
    from ciao.web.project_chats import _MAX_QUOTA_RETRIES, _quota_retry_interval

    schedule = [_quota_retry_interval(n) for n in range(_MAX_QUOTA_RETRIES + 4)]
    hour = 3600
    assert schedule[:5] == [hour, 2 * hour, 4 * hour, 8 * hour, 8 * hour]
    assert max(schedule) == 8 * hour


async def test_consecutive_quota_retries_back_off(tmp_path: Path) -> None:
    """Each further session-limit failure waits twice as long, up to the cap."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield ResultEvent(
            type="result",
            result="API Error: Request rejected (429): reached your session usage limit",
            session_id="",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    await asyncio.wait_for(_consume(stream), timeout=2.0)
    intervals = [pcm.get_chat(chat.chat_id).retry_interval_seconds]  # type: ignore[union-attr]
    for _ in range(4):
        retried = pcm.try_chat_retry_now(chat.chat_id)
        assert retried is not None
        await asyncio.wait_for(_consume(retried), timeout=2.0)
        intervals.append(pcm.get_chat(chat.chat_id).retry_interval_seconds)  # type: ignore[union-attr]

    assert intervals == [3600, 7200, 14400, 28800, 28800]
    pcm.stop_chat_retry(chat.chat_id)


async def test_unattended_quota_turn_is_replayed_unattended(tmp_path: Path) -> None:
    """A scheduled turn that hits a usage limit keeps its unattended flag on replay,
    including across a restart that reloads the retry state from disk."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")

    seen: list[bool] = []

    async def failing_stream_chat(chat_id, prompt, images=None, **kwargs):
        seen.append(bool(kwargs.get("unattended")))
        yield ResultEvent(
            type="result",
            result="usage limit reached",
            session_id="",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = failing_stream_chat  # type: ignore[assignment]
    stream = pcm.start_stream(chat.chat_id, "scheduled care", unattended=True)
    await asyncio.wait_for(_consume(stream), timeout=2.0)

    failed = pcm.get_chat(chat.chat_id)
    assert failed is not None
    assert failed.retry_status == "pending"
    assert failed.retry_kind == "quota"
    assert failed.retry_unattended is True

    reloaded = _make_manager(tmp_path)
    restored = reloaded.get_chat(chat.chat_id)
    assert restored is not None
    assert restored.retry_unattended is True

    async def ok_stream_chat(chat_id, prompt, images=None, **kwargs):
        seen.append(bool(kwargs.get("unattended")))
        yield ResultEvent(
            type="result",
            result="done",
            session_id="",
            is_error=False,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    reloaded.stream_chat = ok_stream_chat  # type: ignore[assignment]
    retried = reloaded.try_chat_retry_now(chat.chat_id)
    assert retried is not None
    await asyncio.wait_for(_consume(retried), timeout=2.0)

    assert seen == [True, True]
    after = reloaded.get_chat(chat.chat_id)
    assert after is not None
    replay_index = str(after.user_turn_count - 1)
    assert after.user_turn_unattended.get(replay_index) is True
    assert after.retry_status == ""
    assert after.retry_unattended is False


async def test_attended_quota_turn_is_replayed_attended(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    seen: list[bool] = []

    async def fake_stream_chat(chat_id, prompt, images=None, **kwargs):
        seen.append(bool(kwargs.get("unattended")))
        yield ResultEvent(
            type="result",
            result="usage limit reached",
            session_id="",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]
    stream = pcm.start_stream(chat.chat_id, "typed by a person")
    await asyncio.wait_for(_consume(stream), timeout=2.0)
    assert pcm.get_chat(chat.chat_id).retry_unattended is False  # type: ignore[union-attr]

    retried = pcm.try_chat_retry_now(chat.chat_id)
    assert retried is not None
    await asyncio.wait_for(_consume(retried), timeout=2.0)
    assert seen == [False, False]
    pcm.stop_chat_retry(chat.chat_id)


async def test_failed_turn_sets_last_error_and_next_turn_clears_it(tmp_path: Path) -> None:
    """A failed turn persists its error on the chat and in the done event; a clean turn clears it."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")

    published: list[dict] = []
    original_publish = pcm._events.publish

    def capture_publish(payload: dict) -> None:
        published.append(payload)
        original_publish(payload)

    pcm._events.publish = capture_publish  # type: ignore[method-assign]

    outcomes = iter([
        ResultEvent(
            type="result",
            result="API Error: Request rejected (400): bad request body",
            session_id="",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        ),
        ResultEvent(
            type="result",
            result="all good",
            session_id="",
            is_error=False,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        ),
    ])

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield next(outcomes)

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "first")
    await asyncio.wait_for(_consume(stream), timeout=2.0)
    await asyncio.sleep(0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert "bad request body" in updated.last_error
    assert updated.to_dict()["last_error"] == updated.last_error
    done = [e for e in published if e.get("type") == "chat_streaming_done"]
    assert done and done[-1]["last_error"] == updated.last_error

    stream = pcm.start_stream(chat.chat_id, "second")
    await asyncio.wait_for(_consume(stream), timeout=2.0)
    await asyncio.sleep(0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.last_error == ""
    done = [e for e in published if e.get("type") == "chat_streaming_done"]
    assert done[-1]["last_error"] == ""


async def test_quota_retries_stop_after_cap(tmp_path: Path) -> None:
    """After _MAX_QUOTA_RETRIES hourly retries, a further quota error arms nothing."""
    from ciao.web.project_chats import _MAX_QUOTA_RETRIES

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    chat.retry_attempts = _MAX_QUOTA_RETRIES
    chat.retry_kind = "quota"
    pcm._save()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield ResultEvent(
            type="result",
            result="API Error: Request rejected (429): rate limit exceeded",
            session_id="",
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
    assert updated.retry_status != "pending"
    assert not any(
        e.get("type") == "chat_retry" and e.get("status") == "pending" for e in events
    )


async def test_quota_retry_armed_below_cap(tmp_path: Path) -> None:
    """One short of the quota cap, a rate-limit error still arms the hourly retry."""
    from ciao.web.project_chats import _MAX_QUOTA_RETRIES

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    chat.retry_attempts = _MAX_QUOTA_RETRIES - 1
    chat.retry_kind = "quota"
    pcm._save()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield ResultEvent(
            type="result",
            result="API Error: Request rejected (429): rate limit exceeded",
            session_id="",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    await asyncio.wait_for(_consume(stream), timeout=2.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status == "pending"
    pcm.stop_chat_retry(chat.chat_id)


async def test_five_hour_session_limit_gets_six_hourly_attempts(tmp_path: Path) -> None:
    """A session limit that clears inside the 5-hour window is retried six times."""
    from ciao.web.project_chats import _MAX_QUOTA_RETRIES

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield ResultEvent(
            type="result",
            result="API Error: Request rejected (429): reached your session usage limit",
            session_id="",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    await asyncio.wait_for(_consume(stream), timeout=2.0)

    # Each hourly fire fails again; the first failure arms the ladder.
    for _ in range(_MAX_QUOTA_RETRIES):
        updated = pcm.get_chat(chat.chat_id)
        assert updated is not None
        assert updated.retry_status == "pending"
        retry_stream = pcm.try_chat_retry_now(chat.chat_id)
        assert retry_stream is not None
        await asyncio.wait_for(_consume(retry_stream), timeout=2.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status != "pending"


async def test_connection_retries_do_not_consume_quota_budget(tmp_path: Path) -> None:
    """Earlier connection retries leave the full quota budget for a later limit."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    chat.retry_attempts = 3
    chat.retry_kind = "connection"
    pcm._save()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield ResultEvent(
            type="result",
            result="API Error: Request rejected (429): rate limit exceeded",
            session_id="",
            is_error=True,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "do the thing")
    await asyncio.wait_for(_consume(stream), timeout=2.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.retry_status == "pending"
    assert updated.retry_kind == "quota"
    assert updated.retry_attempts == 0
    pcm.stop_chat_retry(chat.chat_id)


async def test_retry_loop_wakes_at_a_rearmed_retry_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed daily retry that re-arms a near time is retried then, not a day later."""
    from ciao.web import project_chats

    monkeypatch.setattr(project_chats, "_RETRY_STREAM_POLL_SECONDS", 0.01)
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    near_at = chat_service._iso_after(1)
    calls: list[str] = []
    second_call = asyncio.Event()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        calls.append(prompt)
        if len(calls) == 1:
            # The first daily retry fails and re-arms a retry one second out.
            pcm.set_chat_retry(
                chat_id,
                prompt,
                reason="connection",
                next_at=near_at,
                interval_seconds=24 * 60 * 60,
            )
            return
        second_call.set()
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

    pcm.set_chat_retry(
        chat.chat_id,
        "run me",
        reason="quota",
        next_at="2000-01-01T00:00:00Z",
        interval_seconds=24 * 60 * 60,
    )
    await asyncio.wait_for(second_call.wait(), timeout=5.0)
    while (current := pcm.get_chat(chat.chat_id)) is not None and current.retry_status == "pending":
        await asyncio.sleep(0.01)

    assert calls == ["run me", "run me"]
    assert current is not None
    assert current.retry_status == ""


def _ok_result(chat) -> ResultEvent:
    return ResultEvent(
        type="result", result="network looks down", session_id="sess-net",
        is_error=False, effective_model=chat.model, usage={}, quota={}, cost_usd=0.0,
    )


async def test_network_retry_arms_with_ten_minute_backoff_and_in_flight_prompt(
    tmp_path: Path,
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="net")

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        assert pcm.arm_network_retry(chat.chat_id) == "armed"
        yield _ok_result(chat)

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]
    stream = pcm.start_stream(chat.chat_id, "do the scheduled thing", unattended=True)
    await asyncio.wait_for(_consume(stream), timeout=2.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated.retry_status == "pending"
    assert updated.retry_kind == "network"
    assert updated.retry_prompt == "do the scheduled thing"
    assert updated.retry_attempts == 0
    assert updated.retry_interval_seconds == 600
    pcm.stop_chat_retry(chat.chat_id)


async def test_network_retry_turn_that_rearms_survives_its_own_turn_end(
    tmp_path: Path,
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="net")
    assert pcm.arm_network_retry(chat.chat_id, "job") == "armed"
    seen: list[object] = []

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        seen.append(_kwargs.get("unattended"))
        assert pcm.arm_network_retry(chat.chat_id) == "armed"
        yield _ok_result(chat)

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]
    stream = pcm.try_chat_retry_now(chat.chat_id)
    assert stream is not None
    await asyncio.wait_for(_consume(stream), timeout=2.0)

    assert seen == [True]
    updated = pcm.get_chat(chat.chat_id)
    assert updated.retry_status == "pending"
    assert updated.retry_attempts == 1
    assert updated.retry_interval_seconds == 1200
    pcm.stop_chat_retry(chat.chat_id)


async def test_network_retry_turn_that_succeeds_clears_the_retry(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="net")
    assert pcm.arm_network_retry(chat.chat_id, "job") == "armed"

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        yield _ok_result(chat)

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]
    stream = pcm.try_chat_retry_now(chat.chat_id)
    assert stream is not None
    await asyncio.wait_for(_consume(stream), timeout=2.0)

    assert pcm.get_chat(chat.chat_id).retry_status == ""


async def test_network_retry_backoff_doubles_and_caps_at_three(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="net")

    for attempts, interval in ((0, 600), (1, 1200), (2, 2400)):
        chat.retry_attempts = attempts
        assert pcm.arm_network_retry(chat.chat_id, "job") == "armed"
        assert chat.retry_interval_seconds == interval
    chat.retry_attempts = 3
    assert pcm.arm_network_retry(chat.chat_id, "job") == "exhausted"
    assert chat.retry_status == "stopped"
    assert chat.retry_kind == ""


async def test_try_now_past_the_network_cap_exhausts_instead_of_firing(
    tmp_path: Path,
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="net")
    assert pcm.arm_network_retry(chat.chat_id, "job") == "armed"
    chat.retry_attempts = 3
    calls: list[str] = []

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        calls.append(prompt)
        yield _ok_result(chat)

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]

    assert pcm.try_chat_retry_now(chat.chat_id) is None
    assert calls == []
    assert chat.retry_status == "stopped"


async def test_arm_network_retry_without_prompt_or_turn_refuses(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="net")

    assert pcm.arm_network_retry(chat.chat_id) == "no_prompt"
    assert chat.retry_status == ""


async def test_network_retry_holds_the_result_push(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    pcm._push_delay_seconds = 0
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="net")
    pushed: list[tuple] = []
    pcm.notify_result_cb = lambda cid, title, snippet: pushed.append((cid, title, snippet))

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        pcm.arm_network_retry(chat.chat_id)
        yield _ok_result(chat)

    pcm.stream_chat = fake_stream_chat  # type: ignore[assignment]
    stream = pcm.start_stream(chat.chat_id, "job", unattended=True)
    await asyncio.wait_for(_consume(stream), timeout=2.0)
    await asyncio.sleep(0.05)

    assert pushed == []
    pcm.stop_chat_retry(chat.chat_id)


async def test_control_plane_network_kind_maps_outcomes(tmp_path: Path) -> None:
    from ciao.control_plane import CiaoControlPlane, ControlPlaneError, McpPrincipal

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="net")
    cp = CiaoControlPlane(pcm._config, project_chat_manager=pcm, schedule_manager=None)
    principal = McpPrincipal(
        token_id="t", chat_id=chat.chat_id, project_id="p",
        workspace="personal", provider="opencode",
    )

    out = cp.chat_retry_update(principal, chat.chat_id, "set", prompt="job", kind="network")
    assert out["data"]["retry"]["interval_seconds"] == 600

    with pytest.raises(ControlPlaneError) as bad_kind:
        cp.chat_retry_update(principal, chat.chat_id, "set", prompt="job", kind="bogus")
    assert bad_kind.value.code == "invalid_kind"
    with pytest.raises(ControlPlaneError) as bad_action:
        cp.chat_retry_update(principal, chat.chat_id, "stop", kind="network")
    assert bad_action.value.code == "invalid_action"

    chat.retry_attempts = 3
    with pytest.raises(ControlPlaneError) as capped:
        cp.chat_retry_update(principal, chat.chat_id, "set", prompt="job", kind="network")
    assert capped.value.code == "retry_exhausted"
    pcm.stop_chat_retry(chat.chat_id)
