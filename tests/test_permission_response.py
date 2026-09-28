"""PWA permission-response round-trip.

Validates the full chain from the client-facing route down to the provider's
PermissionGate:

    ws client  →  respond_permission(chat_id, request_id, approved, reason, session_id)
               →  ProjectChatManager.respond_permission
               →  ProviderService.provider.permission_gate.answer
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.providers.opencode import OpencodeProvider, QuestionResponseResult
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


def test_a_native_question_blocks_a_new_ordinary_turn(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="t")
    chat.pending_question = json.dumps({
        "request_id": "form-1",
        "questions": [{"id": "choice", "question": "Continue?"}],
    })

    with pytest.raises(ValueError, match="Answer the open question"):
        pcm.start_stream(chat.chat_id, "send around the form")

    assert json.loads(chat.pending_question)["request_id"] == "form-1"


@pytest.mark.asyncio
async def test_respond_permission_forwards_to_provider_gate(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="t")

    # Force a provider to exist so we can simulate a pending permission.
    provider_service = pcm._get_provider(chat.chat_id)
    gate = provider_service.provider.permission_gate

    from claude_agent_sdk.types import (
        PermissionResultAllow,
        ToolPermissionContext,
    )

    ctx = ToolPermissionContext(
        signal=None, suggestions=[], tool_use_id="tool-1", agent_id=None
    )
    pending = asyncio.create_task(gate.handle("Bash", {"command": "ls"}, ctx))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    ok = await pcm.respond_permission(
        chat.chat_id, request_id="tool-1", approved=True, reason=""
    )
    assert ok.ok is True

    result = await pending
    assert isinstance(result, PermissionResultAllow)


@pytest.mark.asyncio
async def test_respond_permission_keeps_matching_pending_without_ack(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="t")
    chat.pending_permission = json.dumps({
        "request_id": "req-1", "tool_name": "Bash", "message": "Approve use of Bash?", "tool_input": "",
    })

    await pcm.respond_permission(chat.chat_id, request_id="req-1", approved=True, reason="")

    # No provider acknowledgement means the persisted card stays retryable.
    assert pcm._chats[chat.chat_id].pending_permission != ""


@pytest.mark.asyncio
async def test_respond_permission_ignores_stale_reply_for_a_superseded_request(
    tmp_path: Path,
) -> None:
    """A late reply for an already-superseded prompt must not wipe a newer one."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="t")
    chat.pending_permission = json.dumps({
        "request_id": "req-2", "tool_name": "Bash", "message": "Approve use of Bash?", "tool_input": "",
    })

    await pcm.respond_permission(chat.chat_id, request_id="req-1", approved=True, reason="")

    assert json.loads(pcm._chats[chat.chat_id].pending_permission)["request_id"] == "req-2"


@pytest.mark.asyncio
async def test_persisted_question_without_a_provider_is_retryable_and_retained(
    tmp_path: Path,
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="persisted form", provider="opencode")
    chat.pending_question = json.dumps({
        "request_id": "form-1",
        "session_id": "ses_form",
        "questions": [{"id": "q1", "question": "Continue?"}],
    })

    result = await pcm.respond_question(
        chat.chat_id,
        request_id="form-1",
        answers={},
        action="cancel",
    )

    assert result.ok is False
    assert result.retryable is True
    assert json.loads(chat.pending_question)["request_id"] == "form-1"


@pytest.mark.asyncio
async def test_opencode_disconnect_is_not_authoritative_question_stale(
    tmp_path: Path,
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(
        project.project_id, title="connected form", provider="opencode"
    )
    chat.pending_question = json.dumps({
        "request_id": "form-1",
        "session_id": "ses_form",
        "questions": [{"id": "q1", "question": "Continue?"}],
    })
    provider = OpencodeProvider(tmp_path)
    provider._session_id = "ses_form"

    async def disconnected(
        _request_id, _answers, *, cancel=False, session_id=""
    ):
        return QuestionResponseResult(False, "OpenCode is not connected", False)

    provider.send_question_response = disconnected  # type: ignore[method-assign]
    pcm._providers[chat.chat_id] = SimpleNamespace(provider=provider)

    result = await pcm.respond_question(
        chat.chat_id,
        request_id="form-1",
        answers={},
        action="cancel",
    )

    assert result.ok is False
    assert json.loads(chat.pending_question)["request_id"] == "form-1"


@pytest.mark.asyncio
async def test_stale_question_reply_does_not_clear_newer_persisted_form(
    tmp_path: Path,
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(
        project.project_id, title="two forms", provider="opencode"
    )
    chat.pending_question = json.dumps({
        "request_id": "form-2",
        "session_id": "ses_two",
        "questions": [{"id": "q2", "question": "Newest?"}],
    })
    provider = OpencodeProvider(tmp_path)
    provider._session_id = "ses_two"
    pcm._providers[chat.chat_id] = SimpleNamespace(provider=provider)

    result = await pcm.respond_question(
        chat.chat_id,
        request_id="form-1",
        answers={},
        action="cancel",
    )

    assert result.ok is True
    assert json.loads(chat.pending_question)["request_id"] == "form-2"


@pytest.mark.asyncio
async def test_stale_session_reply_does_not_clear_a_reused_request_id(
    tmp_path: Path,
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(
        project.project_id, title="reused permission", provider="opencode"
    )
    chat.pending_permission = json.dumps({
        "request_id": "permission-reused",
        "session_id": "ses_new",
        "tool_name": "shell",
        "message": "Approve the new session?",
    })
    provider = OpencodeProvider(tmp_path)
    provider._session_id = "ses_new"
    pcm._providers[chat.chat_id] = SimpleNamespace(provider=provider)

    result = await pcm.respond_permission(
        chat.chat_id,
        request_id="permission-reused",
        approved=True,
        session_id="ses_old",
    )

    assert result.ok is True
    assert json.loads(chat.pending_permission)["session_id"] == "ses_new"


@pytest.mark.asyncio
async def test_stale_form_session_does_not_clear_a_reused_request_id(
    tmp_path: Path,
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(
        project.project_id, title="reused form", provider="opencode"
    )
    chat.pending_question = json.dumps({
        "request_id": "form-reused",
        "session_id": "ses_new",
        "questions": [{"id": "q1", "question": "Continue?"}],
    })
    provider = OpencodeProvider(tmp_path)
    provider._session_id = "ses_new"
    pcm._providers[chat.chat_id] = SimpleNamespace(provider=provider)

    result = await pcm.respond_question(
        chat.chat_id,
        request_id="form-reused",
        answers={},
        action="cancel",
        session_id="ses_old",
    )

    assert result.ok is True
    assert json.loads(chat.pending_question)["session_id"] == "ses_new"


@pytest.mark.asyncio
async def test_respond_permission_returns_false_when_no_provider(tmp_path: Path) -> None:
    """A permission reply for a chat with no provider yet must be a no-op."""
    pcm = _make_manager(tmp_path)
    ok = await pcm.respond_permission("no-such-chat", request_id="x", approved=True, reason="")
    assert ok.ok is False


@pytest.mark.asyncio
async def test_respond_permission_unknown_request_id_returns_false(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="t")
    pcm._get_provider(chat.chat_id)  # instantiate gate

    ok = await pcm.respond_permission(
        chat.chat_id, request_id="never-asked", approved=True, reason=""
    )
    # A stale id is already settled; acknowledge it so clients clear the card.
    assert ok.ok is True


@pytest.mark.asyncio
async def test_respond_permission_strips_buffered_event_from_active_stream(
    tmp_path: Path,
) -> None:
    """An answered permission must not replay on the next subscribe.

    Repro: the user opens a chat, the SDK asks for Bash approval, the
    user taps Approve, then later reopens the chat. Without buffer
    cleanup the broker replays the original ``permission_request`` and
    the PWA renders a phantom Approve/Deny card for a request that's
    already been answered.
    """
    from ciao.web.chat_broker import ChatStream

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="t")

    # Simulate an in-flight stream that already published a permission_request.
    stream = ChatStream("hi")
    pcm._broker.register(chat.chat_id, stream)
    stream.publish({
        "type": "permission_request",
        "tool_name": "Bash",
        "tool_input": "ls",
        "message": "Approve use of Bash?",
        "request_id": "tool-99",
    })

    # Ensure a provider exists so respond_permission's gate hop is exercised.
    pcm._get_provider(chat.chat_id)

    # Stale reply (gate has nothing pending) — should still strip the buffer.
    await pcm.respond_permission(
        chat.chat_id, request_id="tool-99", approved=True, reason=""
    )

    replay = stream.buffered_events()
    assert all(
        ev.get("type") != "permission_request" for ev in replay
    ), f"buffered permission_request leaked into replay: {replay}"


@pytest.mark.asyncio
async def test_denial_retracts_the_tool_card_before_v2_consumes_the_request(
    tmp_path: Path,
) -> None:
    """A V2 permission id can differ from the tool-call id it gates."""
    from ciao.web.chat_broker import ChatStream

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="t")

    class _Provider:
        def tool_use_id_for_request(self, _request_id: str) -> str:
            return "call-1"

        async def send_permission_response(
            self, _request_id: str, _approved: bool, _reason: str = ""
        ) -> bool:
            # The real adapter removes its pending map before returning.
            return True

    pcm._providers[chat.chat_id] = SimpleNamespace(provider=_Provider())
    stream = ChatStream("hi")
    pcm._broker.register(chat.chat_id, stream)
    stream.publish({
        "type": "tool_use",
        "tool_name": "write",
        "tool_use_id": "call-1",
        "file_touch": {"file_path": "notes.md", "action": "created"},
    })

    result = await pcm.respond_permission(
        chat.chat_id, request_id="permission-1", approved=False
    )
    assert result.ok is True
    events = stream.buffered_events()
    assert any(
        event.get("type") == "tool_denied"
        and event.get("tool_use_id") == "call-1"
        for event in events
    )
    tool_event = next(event for event in events if event.get("type") == "tool_use")
    assert "file_touch" not in tool_event
