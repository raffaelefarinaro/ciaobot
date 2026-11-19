"""Image-capability pre-flight: question, switch, picker, cancel, timeout.

The pre-flight in ``stream_chat`` runs between request build and dispatch
when the user attached images. A model that cannot see images pauses the
turn on a ``model_capability_question``; the PWA answers with
``capability_response`` (switch / picker / cancel). This suite drives the
full flow through ``ProjectChatManager`` exactly like the tier-fallback
tests in ``test_chat_retry.py``. The gate is the agent-level
``promptCapabilities.image`` the agent states at its handshake: only an
explicit False pauses a turn; True or nothing lets it through.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao.config import CiaoConfig
from ciao.models import ImageAttachment, ResultEvent
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager, _CAPABILITY_IMAGE_MSG

from tests.conftest import attach_stub_mcp


# How long a whole fake turn may take before the test fails instead of
# hanging. Only a hang guard: nothing here measures speed, and a healthy turn
# finishes in milliseconds. 2s timed out on a loaded Windows runner (#1862).
_TURN_TIMEOUT_S = 30.0


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


_TEXT_ONLY = "test/text-only"
# A per-model table that says more than the agent does. The gate must ignore it.
_CATALOG = [
    {"model": _TEXT_ONLY, "label": "Text Only (test)", "variants": [], "images": False},
    {"model": "test/sees", "label": "Sees (test)", "variants": [], "images": True},
]


def _stub_agent_images(monkeypatch, value: bool | None) -> None:
    """The opencode agent states ``value`` for image input at its handshake."""
    from ciao.providers.acp.agents import OpencodeAcpProvider

    monkeypatch.setattr(
        OpencodeAcpProvider, "agent_accepts_images", lambda self: value
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

    The current model is the only candidate, disabled: the agent refuses
    images for the whole session, so no other model is offered from the
    per-model table. Cancel closes the turn with the system bubble: no
    dispatch, no result.
    """
    _stub_agent_images(monkeypatch, False)
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
        timeout=_TURN_TIMEOUT_S,
    )

    questions = [e for e in events if e.get("type") == "model_capability_question"]
    assert questions, f"expected a capability question, got {events}"
    q = questions[0]
    assert q["missing"] == "image_input"
    assert q["current_model"] == _TEXT_ONLY
    assert q["timeout_s"] == 30
    assert q["candidates"] == [{"id": _TEXT_ONLY, "label": _TEXT_ONLY, "disabled": True}]
    # No dispatch happened, and the turn ended without a result.
    assert drive_calls == []
    assert not any(e.get("type") == "result" for e in events)
    # Cancel tells the user the images were not sent. SystemStatusEvent
    # serializes as type "status" with the text under "message".
    status = [e for e in events if e.get("type") == "status"]
    assert any(_CAPABILITY_IMAGE_MSG in (e.get("message") or "") for e in status)


async def test_preflight_switch_to_an_unoffered_model_is_refused(tmp_path: Path, monkeypatch) -> None:
    """A switch names a model the question did not offer: it is refused, nothing dispatches.

    With the agent refusing images for the whole session, no model is offered, so
    any switch is unoffered. The turn closes with the system bubble.
    """
    _stub_agent_images(monkeypatch, False)
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
                chat.chat_id,
                request_id=q["request_id"],
                action="switch",
                model_id="test/sees",
            ),
        ),
        timeout=_TURN_TIMEOUT_S,
    )

    assert drive_calls == []
    assert not any(e.get("type") == "model_changed" for e in events)
    assert not any(e.get("type") == "result" for e in events)
    status = [e for e in events if e.get("type") == "status"]
    assert any(_CAPABILITY_IMAGE_MSG in (e.get("message") or "") for e in status)
    assert pcm.get_chat(chat.chat_id).model == _TEXT_ONLY


async def test_preflight_picker_closes_cleanly(tmp_path: Path, monkeypatch) -> None:
    """Picker answers end the turn with no result, but with the system bubble.

    The PWA opens the model selector and the user re-sends through the
    normal path. The bubble is deliberate: a silent end left every *other*
    connected client (flaky mobile socket, second device) with a stuck
    "thinking" state and no transcript row to settle on.
    """
    _stub_agent_images(monkeypatch, False)
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
        timeout=_TURN_TIMEOUT_S,
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
    _stub_agent_images(monkeypatch, False)
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
    events = await asyncio.wait_for(_consume(stream), timeout=_TURN_TIMEOUT_S)

    assert any(e.get("type") == "model_capability_question" for e in events)
    status = [e for e in events if e.get("type") == "status"]
    assert any(_CAPABILITY_IMAGE_MSG in (e.get("message") or "") for e in status)
    assert drive_calls == []
    assert not any(e.get("type") == "result" for e in events)


async def test_preflight_skips_without_images(tmp_path: Path, monkeypatch) -> None:
    """Text-only turns never hit the pre-flight, even when the agent refuses images."""
    _stub_agent_images(monkeypatch, False)
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
    events = await asyncio.wait_for(_consume(stream), timeout=_TURN_TIMEOUT_S)

    assert seen == [_TEXT_ONLY]
    assert not any(e.get("type") == "model_capability_question" for e in events)


async def test_preflight_unattended_never_blocks(tmp_path: Path, monkeypatch) -> None:
    """Scheduled turns skip the question and close with the bubble instead."""
    _stub_agent_images(monkeypatch, False)
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
    events = await asyncio.wait_for(_consume(stream), timeout=_TURN_TIMEOUT_S)

    assert not any(e.get("type") == "model_capability_question" for e in events)
    status = [e for e in events if e.get("type") == "status"]
    assert any(_CAPABILITY_IMAGE_MSG in (e.get("message") or "") for e in status)
    assert drive_calls == []
    assert not any(e.get("type") == "result" for e in events)


# ── Slow-path probes ────────────────────────────────────────────────────


# ── The agent-level gate ────────────────────────────────────────────────


async def _dispatch_image_turn(pcm, chat, tmp_path: Path, *, unattended: bool = False):
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
    stream = pcm.start_stream(
        chat.chat_id, "look", images=[_img(tmp_path)], unattended=unattended
    )
    events = await asyncio.wait_for(_consume(stream), timeout=_TURN_TIMEOUT_S)
    return seen, events


async def test_agent_stating_images_true_is_never_questioned(tmp_path: Path, monkeypatch) -> None:
    """An agent that takes images dispatches the turn; the per-model table is not consulted."""
    _stub_agent_images(monkeypatch, True)
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("cap", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="cap-test")
    _opencode_chat(pcm, chat)

    seen, events = await _dispatch_image_turn(pcm, chat, tmp_path)

    assert seen == [_TEXT_ONLY]
    assert not any(e.get("type") == "model_capability_question" for e in events)


async def test_agent_stating_nothing_is_never_questioned(tmp_path: Path, monkeypatch) -> None:
    """No image statement from the agent (or no connection yet) is not a refusal."""
    _stub_agent_images(monkeypatch, None)
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("cap", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="cap-test")
    _opencode_chat(pcm, chat)

    seen, events = await _dispatch_image_turn(pcm, chat, tmp_path)

    assert seen == [_TEXT_ONLY]
    assert not any(e.get("type") == "model_capability_question" for e in events)


async def test_claude_image_turn_is_never_questioned_and_reads_no_catalog(
    tmp_path: Path, monkeypatch
) -> None:
    """Claude states nothing at the gate, so an image turn dispatches and no catalog is read."""
    from ciao.providers.acp.agents import ClaudeAcpProvider
    from ciao.providers.acp.agents import OpencodeAcpProvider

    catalog_calls: list[str] = []

    async def claude_catalog(cls, workspace_root, *, force=False):
        catalog_calls.append("claude")
        return []

    async def opencode_catalog(cls, workspace_root, *, force=False):
        catalog_calls.append("opencode")
        return [dict(row) for row in _CATALOG]

    monkeypatch.setattr(ClaudeAcpProvider, "model_catalog", classmethod(claude_catalog))
    monkeypatch.setattr(OpencodeAcpProvider, "model_catalog", classmethod(opencode_catalog))
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("cap", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="cap-test")
    chat.provider = "claude"
    chat.model = _TEXT_ONLY
    pcm._save()

    seen, events = await _dispatch_image_turn(pcm, chat, tmp_path)

    assert seen == [_TEXT_ONLY]
    assert not any(e.get("type") == "model_capability_question" for e in events)
    assert catalog_calls == []


async def test_unknown_provider_preflight_is_capable(
    tmp_path: Path, monkeypatch
) -> None:
    """A provider id the registry does not know is treated as capable."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("cap", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="cap-test")
    chat.provider = "no-such-provider"

    assert pcm._agent_accepts_images(chat) is True
    assert await pcm._capability_candidates(chat, _TEXT_ONLY) == [
        {"id": _TEXT_ONLY, "label": _TEXT_ONLY, "disabled": True}
    ]


@pytest.mark.parametrize(
    ("caps", "expected"),
    [
        ({"promptCapabilities": {"image": True}}, True),
        ({"promptCapabilities": {"image": False}}, False),
        ({"promptCapabilities": {}}, None),
        ({"promptCapabilities": {"image": "yes"}}, None),
        ({}, None),
    ],
)
def test_acp_agent_image_statement_is_read_from_the_handshake(
    tmp_path: Path, caps: dict, expected: bool | None
) -> None:
    from ciao.providers.acp.agents import OpencodeAcpProvider

    provider = OpencodeAcpProvider(tmp_path)
    assert provider.agent_accepts_images() is None  # no connection yet
    provider._connection = SimpleNamespace(agent_capabilities=caps)  # type: ignore[assignment]
    assert provider.agent_accepts_images() is expected
