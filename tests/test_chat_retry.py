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

    The PWA must see exactly one ``result`` (the success), a ``status``
    note about the retry, and a ``model_changed`` event after success.
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
    seen_image_counts: list[int] = []

    async def fake_drive(*, chat_id, request, outcome):
        seen_models.append(request.model)
        seen_image_counts.append(len(request.images or []))
        if request.model == "kimi5.2:cloud":
            outcome.response_text = "API Error: 400 this model does not support image input"
            outcome.had_error = True
            outcome.effective_model = "kimi5.2:cloud"
            evt = ResultEvent(
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
            outcome.events.append(evt)
            yield evt
            return
        # Second attempt on the next tier succeeds.
        outcome.response_text = "Here is my answer about the image."
        outcome.had_error = False
        outcome.effective_model = request.model
        evt = ResultEvent(
            type="result",
            result="Here is my answer about the image.",
            session_id="sess-2",
            is_error=False,
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

    # First attempt fired on kimi5.2; retry attempted on minimax-m3
    # (configured Ollama opus slot). Both must appear in the seen list.
    assert seen_models[0] == "kimi5.2:cloud"
    assert seen_models[-1] == "minimax-m3:cloud"
    # Images must be preserved on the retry (not stripped).
    assert seen_image_counts == [1, 1]
    # A status line was emitted between the two attempts so the PWA
    # shows the user what happened.
    status_events = [
        e
        for e in events
        if e.get("type") == "status" and "retrying" in (e.get("message") or "").lower()
    ]
    assert status_events, f"expected a 'retrying' status event, got {events}"
    # Exactly one result — the error from the first model is suppressed.
    result_events = [e for e in events if e.get("type") == "result"]
    assert len(result_events) == 1, f"expected exactly one result, got {result_events}"
    final = result_events[0]
    assert final.get("is_error") is False
    assert "Here is my answer" in final.get("text", "")
    # Successful fallback notifies the PWA of the model switch.
    model_changed = [e for e in events if e.get("type") == "model_changed"]
    assert model_changed == [{"type": "model_changed", "model": "minimax-m3:cloud"}]
    # And persists it on the chat so the next turn uses the working model.
    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.model == "minimax-m3:cloud"


async def test_capability_fallback_persists_model_change(tmp_path: Path) -> None:
    """After a successful capability fallback, chat.model must stick."""
    pcm = _make_manager(tmp_path)
    pcm._config.ollama = pcm._config.ollama.__class__(
        haiku_model="deepseek-v4-flash:cloud",
        sonnet_model="kimi-k2.7-code:cloud",
        opus_model="minimax-m3:cloud",
        fable_model="kimi5.2:cloud",
    )
    project = pcm.create_project("tier-fallback-persist", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="persist-model")
    chat.model = "kimi5.2:cloud"
    pcm._save()

    async def fake_drive(*, chat_id, request, outcome):
        if request.model == "kimi5.2:cloud":
            outcome.response_text = "API Error: 400 this model does not support image input"
            outcome.had_error = True
            outcome.effective_model = request.model
            evt = ResultEvent(
                type="result",
                result=outcome.response_text,
                session_id="sess-1",
                is_error=True,
                effective_model=request.model,
                usage={},
                quota={},
                cost_usd=0.0,
            )
            outcome.events.append(evt)
            yield evt
            return
        outcome.response_text = "ok"
        outcome.had_error = False
        outcome.effective_model = request.model
        evt = ResultEvent(
            type="result",
            result="ok",
            session_id="sess-2",
            is_error=False,
            effective_model=request.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )
        outcome.events.append(evt)
        yield evt

    pcm._drive_stream = fake_drive  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "describe this")
    await asyncio.wait_for(_consume(stream), timeout=2.0)

    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.model == "minimax-m3:cloud"
    # Reloading from disk must keep the persisted model.
    pcm2 = _make_manager(tmp_path)
    reloaded = pcm2.get_chat(chat.chat_id)
    assert reloaded is not None
    assert reloaded.model == "minimax-m3:cloud"


async def test_capability_fallback_preserves_images(tmp_path: Path) -> None:
    """Capability fallback must retry with the original images intact."""
    pcm = _make_manager(tmp_path)
    pcm._config.ollama = pcm._config.ollama.__class__(
        haiku_model="deepseek-v4-flash:cloud",
        sonnet_model="kimi-k2.7-code:cloud",
        opus_model="minimax-m3:cloud",
        fable_model="kimi5.2:cloud",
    )
    project = pcm.create_project("tier-fallback-images", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="preserve-images")
    chat.model = "kimi5.2:cloud"
    pcm._save()

    retry_images: list | None = None

    async def fake_drive(*, chat_id, request, outcome):
        nonlocal retry_images
        if request.model == "kimi5.2:cloud":
            outcome.response_text = "API Error: 400 this model does not support image input"
            outcome.had_error = True
            outcome.effective_model = request.model
            evt = ResultEvent(
                type="result",
                result=outcome.response_text,
                session_id="sess-1",
                is_error=True,
                effective_model=request.model,
                usage={},
                quota={},
                cost_usd=0.0,
            )
            outcome.events.append(evt)
            yield evt
            return
        retry_images = list(request.images or [])
        outcome.response_text = "I see a cat."
        outcome.had_error = False
        outcome.effective_model = request.model
        evt = ResultEvent(
            type="result",
            result="I see a cat.",
            session_id="sess-2",
            is_error=False,
            effective_model=request.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )
        outcome.events.append(evt)
        yield evt

    pcm._drive_stream = fake_drive  # type: ignore[assignment]

    from ciao.models import ImageAttachment

    img = ImageAttachment(
        path=tmp_path / "photo.jpg",
        mime_type="image/jpeg",
        original_filename="photo.jpg",
    )
    stream = pcm.start_stream(chat.chat_id, "what is this?", images=[img])
    await asyncio.wait_for(_consume(stream), timeout=2.0)

    assert retry_images is not None
    assert len(retry_images) == 1
    assert retry_images[0].path == img.path
    assert retry_images[0].mime_type == "image/jpeg"
    assert retry_images[0].original_filename == "photo.jpg"


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
    assert drive_calls == ["kimi5.2:cloud"]
    result_events = [e for e in events if e.get("type") == "result"]
    assert result_events
    assert result_events[-1].get("is_error") is True
    # No model_changed when fallback does not run.
    assert not any(e.get("type") == "model_changed" for e in events)
    # Chat model stays on the original.
    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.model == "kimi5.2:cloud"


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


def test_connection_drop_banner_classified_as_connection_error() -> None:
    """The CLI mid-response drop banner must be a retryable connection error."""
    from ciao.web.project_chats import (
        _is_retryable_connection_error,
        _is_retryable_quota_error,
        _is_billing_or_spend_limit_error,
    )

    banner = (
        "API Error: Connection closed mid-response. The response above may "
        "be incomplete."
    )
    assert _is_retryable_connection_error(banner) is True
    # It is NOT a quota error — must not be routed to the hourly retry path.
    assert _is_retryable_quota_error(banner) is False

    # Out of credits / spend limits must be classified as retryable quota errors
    credits_err = "Title generation via codex gpt-5.6-terra failed: Your workspace is out of credits. Ask your workspace owner to refill in order to continue."
    spend_limit_err = "You've hit your org's monthly spend limit"
    rate_limit_err = "API Error: Request rejected (429): rate limit exceeded"
    assert _is_retryable_quota_error(credits_err) is True
    assert _is_retryable_quota_error(spend_limit_err) is True
    assert _is_retryable_quota_error(rate_limit_err) is True

    assert _is_billing_or_spend_limit_error(credits_err) is True
    assert _is_billing_or_spend_limit_error(spend_limit_err) is True
    assert _is_billing_or_spend_limit_error(rate_limit_err) is False



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

    async def fake_stream_chat(chat_id, prompt, images=None):
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

    async def fake_stream_chat(chat_id, prompt, images=None):
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


async def test_midresponse_drop_without_session_falls_back_to_replay(tmp_path: Path) -> None:
    """With progress but no session to resume, replay the prompt, not "continue"."""
    from ciao.models import AssistantTextDelta

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")
    # No session_id set — nothing to resume.

    async def fake_stream_chat(chat_id, prompt, images=None):
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
    pcm._save()

    async def fake_stream_chat(chat_id, prompt, images=None):
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

    async def fake_stream_chat(chat_id, prompt, images=None):
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


async def test_rate_limit_error_with_progress_does_not_arm_retry(tmp_path: Path) -> None:
    """Rate limit errors must NOT trigger auto-retry after provider progress."""
    from ciao.models import AssistantTextDelta

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("retry", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="retry-test")

    async def fake_stream_chat(chat_id, prompt, images=None):
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
    assert updated.retry_status != "pending"
    assert not any(
        e.get("type") == "chat_retry" and e.get("status") == "pending" for e in events
    )

