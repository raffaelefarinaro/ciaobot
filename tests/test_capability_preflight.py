"""Image-capability pre-flight: question, switch, picker, cancel, timeout.

The pre-flight in ``stream_chat`` runs between request build and dispatch
when the user attached images. A model that cannot see images pauses the
turn on a ``model_capability_question``; the PWA answers with
``capability_response`` (switch / picker / cancel). This suite drives the
full flow through ``ProjectChatManager`` exactly like the tier-fallback
tests in ``test_chat_retry.py``, plus unit tests for the two slow-path
capability source (opencode's own model catalog).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Literal

from ciao.config import CiaoConfig
from ciao.models import ImageAttachment, ResultEvent
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager, _CAPABILITY_IMAGE_MSG

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


# opencode is bring-your-own-provider, so it is the only provider whose catalog
# can contain a model that cannot accept an image. `images` mirrors what
# opencode reports in `capabilities.input.image`; an absent key means "opencode
# did not say", which must read as capable rather than as a refusal.
_TEXT_ONLY = "test/text-only"
_CATALOG = [
    {"model": _TEXT_ONLY, "label": "Text Only (test)", "variants": [], "images": False},
    {"model": "test/sees", "label": "Sees (test)", "variants": [], "images": True},
    {"model": "test/also-sees", "label": "Also Sees (test)", "variants": [], "images": True},
    {"model": "test/third", "label": "Third (test)", "variants": [], "images": True},
    {"model": "test/unstated", "label": "Unstated (test)", "variants": []},
]


def _stub_catalog(monkeypatch, catalog=None) -> None:
    async def fake_catalog(cls, workspace_root, *, force=False):
        return [dict(row) for row in (_CATALOG if catalog is None else catalog)]

    from ciao.providers.opencode import OpencodeProvider

    monkeypatch.setattr(
        OpencodeProvider, "model_catalog", classmethod(fake_catalog)
    )


def _opencode_chat(pcm: ProjectChatManager, chat, model: str = _TEXT_ONLY) -> None:
    """Point a chat at an opencode model, bypassing the picker's validation."""
    chat.provider = "opencode"
    chat.model = model
    pcm._save()


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


async def test_preflight_question_shape_and_cancel(tmp_path: Path, monkeypatch) -> None:
    """A non-vision model with images pauses on a well-formed question.

    The current model leads the candidates as the disabled entry, followed by
    every model opencode states accepts images. Cancel closes the turn with
    the system bubble: no dispatch, no result.
    """
    _stub_catalog(monkeypatch)
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("cap", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="cap-test")
    _opencode_chat(pcm, chat)

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
    assert q["current_model"] == _TEXT_ONLY
    assert q["timeout_s"] == 30
    assert q["candidates"][0] == {
        "id": _TEXT_ONLY,
        "label": _TEXT_ONLY,
        "disabled": True,
    }
    # Only models opencode positively states accept images are offered.
    # `test/unstated` is omitted: an unknown answer is good enough to
    # let the current turn through, but not to recommend a switch.
    assert [c["id"] for c in q["candidates"][1:]] == [
        "test/sees",
        "test/also-sees",
        "test/third",
    ]
    assert all(c["supports_vision"] is True for c in q["candidates"][1:])
    # No dispatch happened, and the turn ended without a result.
    assert drive_calls == []
    assert not any(e.get("type") == "result" for e in events)
    # Cancel tells the user the images were not sent. SystemStatusEvent
    # serializes as type "status" with the text under "message".
    status = [e for e in events if e.get("type") == "status"]
    assert any(_CAPABILITY_IMAGE_MSG in (e.get("message") or "") for e in status)


async def test_preflight_switch_redispatches_on_picked_model(tmp_path: Path, monkeypatch) -> None:
    """Switch answers re-dispatch the turn on the picked model.

    The chat model is persisted, a ``model_changed`` event is emitted, and
    the request is rebuilt against the new model before dispatch.
    """
    _stub_catalog(monkeypatch)
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("cap", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="cap-test")
    _opencode_chat(pcm, chat)

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
                model_id="test/sees",
            ),
        ),
        timeout=2.0,
    )

    # Dispatched exactly once, on the picked model.
    assert seen == ["test/sees"]
    model_changed = [e for e in events if e.get("type") == "model_changed"]
    assert model_changed == [{"type": "model_changed", "model": "test/sees"}]
    results = [e for e in events if e.get("type") == "result"]
    assert len(results) == 1
    assert results[0].get("is_error") is False
    # The switch persisted on the chat for the next turn.
    updated = pcm.get_chat(chat.chat_id)
    assert updated is not None
    assert updated.model == "test/sees"


async def test_preflight_picker_closes_cleanly(tmp_path: Path, monkeypatch) -> None:
    """Picker answers end the turn with no result, but with the system bubble.

    The PWA opens the model selector and the user re-sends through the
    normal path. The bubble is deliberate: a silent end left every *other*
    connected client (flaky mobile socket, second device) with a stuck
    "thinking" state and no transcript row to settle on.
    """
    _stub_catalog(monkeypatch)
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("cap", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="cap-test")
    _opencode_chat(pcm, chat)

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
    # The system bubble explains the empty transcript on every client.
    status = [e for e in events if e.get("type") == "status"]
    assert any(_CAPABILITY_IMAGE_MSG in (e.get("message") or "") for e in status)


async def test_preflight_timeout_closes_turn_with_bubble(tmp_path: Path, monkeypatch) -> None:
    """No answer within the window closes the turn with the system bubble.

    The 30s window is monkeypatched down so the test does not wait. The
    turn ends with no dispatch and no result event.
    """
    monkeypatch.setattr("ciao.web.project_chats.CAPABILITY_QUESTION_TIMEOUT_S", 0.1)
    _stub_catalog(monkeypatch)
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("cap", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="cap-test")
    _opencode_chat(pcm, chat)

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


async def test_preflight_skips_without_images(tmp_path: Path, monkeypatch) -> None:
    """Text-only turns never hit the pre-flight, even on a non-vision model."""
    _stub_catalog(monkeypatch)
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("cap", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="cap-test")
    _opencode_chat(pcm, chat)

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

    assert seen == [_TEXT_ONLY]
    assert not any(e.get("type") == "model_capability_question" for e in events)


async def test_preflight_unattended_never_blocks(tmp_path: Path, monkeypatch) -> None:
    """Scheduled turns skip the question and close with the bubble instead."""
    _stub_catalog(monkeypatch)
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("cap", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="cap-test")
    _opencode_chat(pcm, chat)

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
