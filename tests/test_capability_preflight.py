"""Image-capability pre-flight: question, switch, picker, cancel, timeout.

The pre-flight in ``stream_chat`` runs between request build and dispatch
when the user attached images. A model that cannot see images pauses the
turn on a ``model_capability_question``; the PWA answers with
``capability_response`` (switch / picker / cancel). This suite drives the
full flow through ``ProjectChatManager`` exactly like the tier-fallback
tests in ``test_chat_retry.py``, plus unit tests for the two slow-path
probes (Ollama ``/api/show`` with its 24h cache, OpenRouter catalog).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ciao.config import CiaoConfig
from ciao.models import ImageAttachment, ResultEvent
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager, _CAPABILITY_IMAGE_MSG


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


def _pin_ollama(pcm: ProjectChatManager) -> None:
    # The user's actual config: fable=kimi5.2, opus=minimax-m3,
    # sonnet=kimi-k2.7-code, haiku=deepseek-v4-flash. Only minimax-m3 is
    # vision-capable, so it is the sole candidate for a non-vision chat.
    pcm._config.ollama = pcm._config.ollama.__class__(
        haiku_model="deepseek-v4-flash:cloud",
        sonnet_model="kimi-k2.7-code:cloud",
        opus_model="minimax-m3:cloud",
        fable_model="kimi5.2:cloud",
    )


def _img(tmp_path: Path) -> ImageAttachment:
    return ImageAttachment(
        path=tmp_path / "shot.png",
        mime_type="image/png",
        original_filename="shot.png",
    )


def _ok_result(model: str) -> ResultEvent:
    return ResultEvent(
        type="result",
        result="ok",
        session_id="sess-ok",
        is_error=False,
        effective_model=model,
        usage={},
        quota={},
        cost_usd=0.0,
    )


async def _consume(stream) -> list[dict]:
    events: list[dict] = []
    async for event in stream.subscribe():
        events.append(event)
    return events


async def _consume_answering(stream, pcm, chat_id, responder) -> list[dict]:
    """Consume, answering the capability question the moment it appears."""
    events: list[dict] = []
    async for event in stream.subscribe():
        events.append(event)
        if event.get("type") == "model_capability_question":
            responder(event)
    return events


# ── Pre-flight flow through ProjectChatManager ─────────────────────────


async def test_preflight_question_shape_and_cancel(tmp_path: Path) -> None:
    """A non-vision model with images pauses on a well-formed question.

    The current model leads the candidates as the disabled entry; the only
    same-backend vision neighbor is the opus slot (minimax-m3). Cancel
    closes the turn with the system bubble, no dispatch, no result.
    """
    pcm = _make_manager(tmp_path)
    _pin_ollama(pcm)
    project = pcm.create_project("cap", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="cap-test")
    chat.model = "deepseek-v4-flash:cloud"  # known non-vision (haiku slot)
    pcm._save()

    drive_calls: list[str] = []

    async def fake_drive(*, chat_id, request, outcome):
        drive_calls.append(request.model)
        yield  # pragma: no cover - never reached in this test

    pcm._drive_stream = fake_drive  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "what is this?", images=[_img(tmp_path)])
    events = await asyncio.wait_for(
        _consume_answering(
            stream,
            pcm,
            chat.chat_id,
            lambda q: pcm.respond_capability(
                chat.chat_id, request_id=q["request_id"], action="cancel"
            ),
        ),
        timeout=2.0,
    )

    questions = [e for e in events if e.get("type") == "model_capability_question"]
    assert questions, f"expected a capability question, got {events}"
    q = questions[0]
    assert q["missing"] == "image_input"
    assert q["current_model"] == "deepseek-v4-flash:cloud"
    assert q["timeout_s"] == 30
    assert q["candidates"][0] == {
        "id": "deepseek-v4-flash:cloud",
        "label": "deepseek-v4-flash:cloud",
        "disabled": True,
    }
    # The fable slot (kimi5.2) is not in the known-bad prefix set ("kimi-"
    # needs the dash), so it fast-paths as vision-capable and is offered;
    # the sonnet slot (kimi-k2.7-code) is known non-vision and skipped. A
    # wrong guess here is caught at dispatch time by the capability-error
    # ladder, exactly like the tier-fallback tests exercise.
    assert [c["id"] for c in q["candidates"][1:]] == [
        "kimi5.2:cloud",
        "minimax-m3:cloud",
    ]
    assert all(c["supports_vision"] is True for c in q["candidates"][1:])
    # No dispatch happened, and the turn ended without a result.
    assert drive_calls == []
    assert not any(e.get("type") == "result" for e in events)
    # Cancel tells the user the images were not sent. SystemStatusEvent
    # serializes as type "status" with the text under "message".
    status = [e for e in events if e.get("type") == "status"]
    assert any(_CAPABILITY_IMAGE_MSG in (e.get("message") or "") for e in status)


async def test_preflight_switch_redispatches_on_picked_model(tmp_path: Path) -> None:
    """Switch answers re-dispatch the turn on the picked model.

    The chat model is persisted, a ``model_changed`` event is emitted, and
    the request is rebuilt against the new model before dispatch.
    """
    pcm = _make_manager(tmp_path)
    _pin_ollama(pcm)
    project = pcm.create_project("cap", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="cap-test")
    chat.model = "deepseek-v4-flash:cloud"
    pcm._save()

    seen: list[str] = []

    async def fake_drive(*, chat_id, request, outcome):
        seen.append(request.model)
        outcome.response_text = "ok"
        outcome.had_error = False
        outcome.effective_model = request.model
        evt = _ok_result(request.model)
        outcome.events.append(evt)
        yield evt

    pcm._drive_stream = fake_drive  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "what is this?", images=[_img(tmp_path)])
    events = await asyncio.wait_for(
        _consume_answering(
            stream,
            pcm,
            chat.chat_id,
            lambda q: pcm.respond_capability(
                chat.chat_id,
                request_id=q["request_id"],
                action="switch",
                model_id="minimax-m3:cloud",
            ),
        ),
        timeout=2.0,
    )

    # Dispatched exactly once, on the picked model.
    assert seen == ["minimax-m3:cloud"]
    model_changed = [e for e in events if e.get("type") == "model_changed"]
    assert model_changed == [{"type": "model_changed", "model": "minimax-m3:cloud"}]
    results = [e for e in events if e.get("type") == "result"]
    assert len(results) == 1
    assert results[0].get("is_error") is False
    # The switch persisted on the chat for the next turn.
    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.model == "minimax-m3:cloud"


async def test_preflight_picker_closes_cleanly(tmp_path: Path) -> None:
    """Picker answers end the turn with no result and no bubble.

    The PWA opens the model selector and the user re-sends through the
    normal path, so this turn just stops.
    """
    pcm = _make_manager(tmp_path)
    _pin_ollama(pcm)
    project = pcm.create_project("cap", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="cap-test")
    chat.model = "deepseek-v4-flash:cloud"
    pcm._save()

    drive_calls: list[str] = []

    async def fake_drive(*, chat_id, request, outcome):
        drive_calls.append(request.model)
        yield  # pragma: no cover - never reached in this test

    pcm._drive_stream = fake_drive  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "what is this?", images=[_img(tmp_path)])
    events = await asyncio.wait_for(
        _consume_answering(
            stream,
            pcm,
            chat.chat_id,
            lambda q: pcm.respond_capability(
                chat.chat_id, request_id=q["request_id"], action="picker"
            ),
        ),
        timeout=2.0,
    )

    assert drive_calls == []
    assert not any(e.get("type") == "result" for e in events)
    # No status bubble: the user is mid-flow, not abandoned.
    assert not any(e.get("type") == "status" for e in events)


async def test_preflight_timeout_closes_turn_with_bubble(tmp_path: Path, monkeypatch) -> None:
    """No answer within the window closes the turn with the system bubble.

    The 30s window is monkeypatched down so the test does not wait. The
    turn ends with no dispatch and no result event.
    """
    monkeypatch.setattr("ciao.web.project_chats.CAPABILITY_QUESTION_TIMEOUT_S", 0.1)
    pcm = _make_manager(tmp_path)
    _pin_ollama(pcm)
    project = pcm.create_project("cap", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="cap-test")
    chat.model = "deepseek-v4-flash:cloud"
    pcm._save()

    drive_calls: list[str] = []

    async def fake_drive(*, chat_id, request, outcome):
        drive_calls.append(request.model)
        yield  # pragma: no cover - never reached in this test

    pcm._drive_stream = fake_drive  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "what is this?", images=[_img(tmp_path)])
    events = await asyncio.wait_for(_consume(stream), timeout=2.0)

    assert any(e.get("type") == "model_capability_question" for e in events)
    status = [e for e in events if e.get("type") == "status"]
    assert any(_CAPABILITY_IMAGE_MSG in (e.get("message") or "") for e in status)
    assert drive_calls == []
    assert not any(e.get("type") == "result" for e in events)


async def test_preflight_skips_without_images(tmp_path: Path) -> None:
    """Text-only turns never hit the pre-flight, even on a non-vision model."""
    pcm = _make_manager(tmp_path)
    _pin_ollama(pcm)
    project = pcm.create_project("cap", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="cap-test")
    chat.model = "deepseek-v4-flash:cloud"
    pcm._save()

    seen: list[str] = []

    async def fake_drive(*, chat_id, request, outcome):
        seen.append(request.model)
        outcome.response_text = "ok"
        outcome.had_error = False
        outcome.effective_model = request.model
        evt = _ok_result(request.model)
        outcome.events.append(evt)
        yield evt

    pcm._drive_stream = fake_drive  # type: ignore[assignment]

    stream = pcm.start_stream(chat.chat_id, "hello there")
    events = await asyncio.wait_for(_consume(stream), timeout=2.0)

    assert seen == ["deepseek-v4-flash:cloud"]
    assert not any(e.get("type") == "model_capability_question" for e in events)


async def test_preflight_unattended_never_blocks(tmp_path: Path) -> None:
    """Scheduled turns skip the question and close with the bubble instead."""
    pcm = _make_manager(tmp_path)
    _pin_ollama(pcm)
    project = pcm.create_project("cap", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="cap-test")
    chat.model = "deepseek-v4-flash:cloud"
    pcm._save()

    drive_calls: list[str] = []

    async def fake_drive(*, chat_id, request, outcome):
        drive_calls.append(request.model)
        yield  # pragma: no cover - never reached in this test

    pcm._drive_stream = fake_drive  # type: ignore[assignment]

    stream = pcm.start_stream(
        chat.chat_id, "what is this?", images=[_img(tmp_path)], unattended=True
    )
    events = await asyncio.wait_for(_consume(stream), timeout=2.0)

    assert not any(e.get("type") == "model_capability_question" for e in events)
    status = [e for e in events if e.get("type") == "status"]
    assert any(_CAPABILITY_IMAGE_MSG in (e.get("message") or "") for e in status)
    assert drive_calls == []
    assert not any(e.get("type") == "result" for e in events)


# ── Slow-path probes ────────────────────────────────────────────────────


def test_ollama_vision_probe_cached_24h(monkeypatch) -> None:
    """The /api/show probe runs once; the answer is cached 24h per model."""
    from ciao.providers import ollama as ollama_mod
    from ciao.providers.ollama import vision_support_ollama

    calls: list[str] = []

    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return json.dumps(self._payload).encode("utf-8")

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *exc) -> bool:
            return False

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return FakeResponse({"model_info": {"vision.clip": 1}})

    monkeypatch.setattr(ollama_mod.urllib.request, "urlopen", fake_urlopen)
    # A cloud-routed id needs a real cloud key to be routable.
    settings = ollama_mod.OllamaSettings(
        base_url="https://ollama.com",
        api_key="sk-cloud",
        haiku_model="probe-model:cloud",
        sonnet_model="probe-model:cloud",
        opus_model="probe-model:cloud",
        fable_model="probe-model:cloud",
    )
    try:
        assert vision_support_ollama("probe-model:cloud", settings) is True
        assert vision_support_ollama("probe-model:cloud", settings) is True
    finally:
        ollama_mod._VISION_CACHE.clear()
    assert len(calls) == 1, "second call must be served from the 24h cache"
    assert calls[0].endswith("/api/show")


def test_ollama_vision_probe_unreachable_defaults_to_capable(monkeypatch) -> None:
    """A failed probe logs and returns None; the pre-flight treats None as
    capable so an image turn is never blocked on a dead daemon."""
    from ciao.providers import ollama as ollama_mod
    from ciao.providers.ollama import vision_support_ollama

    def fake_urlopen(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(ollama_mod.urllib.request, "urlopen", fake_urlopen)
    settings = ollama_mod.OllamaSettings(
        base_url="https://ollama.com",
        api_key="sk-cloud",
        haiku_model="probe-model:cloud",
        sonnet_model="probe-model:cloud",
        opus_model="probe-model:cloud",
        fable_model="probe-model:cloud",
    )
    try:
        assert vision_support_ollama("probe-model:cloud", settings) is None
    finally:
        ollama_mod._VISION_CACHE.clear()


def test_openrouter_catalog_flags_vision(monkeypatch) -> None:
    """discover_models extracts vision support from the catalog, and
    vision_support_openrouter reads it back without a refresh of its own."""
    from ciao.providers import openrouter as or_mod
    from ciao.providers.openrouter import (
        discover_models,
        vision_support_openrouter,
    )

    payload = {
        "data": [
            {
                "id": "anthropic/claude-sonnet-4-6",
                "architecture": {
                    "modality": "text+image",
                    "input_modalities": ["text", "image"],
                },
            },
            {
                "id": "deepseek/deepseek-chat",
                "architecture": {"modality": "text"},
            },
            {"id": "no-arch-model"},
        ]
    }

    class FakeResponse:
        def read(self) -> bytes:
            return json.dumps(payload).encode("utf-8")

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *exc) -> bool:
            return False

    def fake_urlopen(req, timeout=None):
        return FakeResponse()

    monkeypatch.setattr(or_mod.urllib.request, "urlopen", fake_urlopen)
    settings = or_mod.OpenRouterSettings(api_key="sk-test")
    ids, vision = discover_models(settings)
    assert "anthropic/claude-sonnet-4-6" in ids
    assert vision["anthropic/claude-sonnet-4-6"] is True
    assert vision["deepseek/deepseek-chat"] is False
    assert vision["no-arch-model"] is False
    assert vision_support_openrouter("anthropic/claude-sonnet-4-6", settings) is True
    assert vision_support_openrouter("deepseek/deepseek-chat", settings) is False
    # Unknown ids (not in the last catalog fetch) are None, not False.
    assert vision_support_openrouter("unknown/model", settings) is None
