"""A native form with no provider and no running turn cannot be answered natively."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao.models import ResultEvent
from ciao.providers.opencode import QuestionResponseResult
from ciao.web.chat_streaming import ChatStream
from ciao.web.project_chats import ProjectChatManager
from tests.test_project_chat_persistence import _make_manager

_FORM = {
    "request_id": "form-1",
    "session_id": "ses_1",
    "questions": [
        {
            "id": "q1",
            "header": "Next step",
            "question": "What next?",
            "options": [
                {"label": "Ship it", "value": "ship"},
                {"label": "Hold", "value": "hold"},
            ],
        }
    ],
}

_PERMISSION = {
    "request_id": "perm-1",
    "session_id": "ses_1",
    "tool_name": "Bash",
    "message": "Run ls?",
    "tool_input": "{\"command\": \"ls\"}",
}


def _chat_with_permission(tmp_path: Path):
    manager = _make_manager(tmp_path)
    project = manager.create_project("Dead permission", workspace="personal")
    chat = manager.create_chat(project.project_id, provider="opencode")
    chat.pending_permission = json.dumps(_PERMISSION)
    return manager, chat


def _chat_with_form(tmp_path: Path):
    manager = _make_manager(tmp_path)
    project = manager.create_project("Dead form", workspace="personal")
    chat = manager.create_chat(project.project_id, provider="opencode")
    chat.pending_question = json.dumps(_FORM)
    return manager, chat


def _install_fake_drive(manager: ProjectChatManager, chat) -> None:
    async def fake_drive(*, chat_id, request, outcome):
        event = ResultEvent(
            type="result",
            result="",
            session_id="ses_1",
            is_error=False,
            effective_model=chat.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )
        outcome.events.append(event)
        yield event

    manager._drive_stream = fake_drive  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_cancel_with_no_provider_clears_the_form(tmp_path: Path) -> None:
    manager, chat = _chat_with_form(tmp_path)
    result = await manager.respond_question(
        chat.chat_id, request_id="form-1", answers={}, action="cancel"
    )
    assert result.ok is True
    assert result.delivered_as_message is False
    assert chat.pending_question == ""


@pytest.mark.asyncio
async def test_reply_with_no_provider_starts_a_turn_carrying_the_answer(
    tmp_path: Path,
) -> None:
    manager, chat = _chat_with_form(tmp_path)
    _install_fake_drive(manager, chat)
    result = await manager.respond_question(
        chat.chat_id, request_id="form-1", answers={"q1": ["ship", "after lunch"]}
    )
    assert result.ok is True
    assert result.delivered_as_message is True
    assert chat.pending_question == ""
    stream = manager.get_active_stream(chat.chat_id)
    assert stream is not None
    assert 'Answer to your question "Next step": Ship it, after lunch' in stream.prompt_text
    _ = [e async for e in stream.subscribe()]


@pytest.mark.asyncio
async def test_reply_with_no_provider_while_draining_keeps_the_form(tmp_path: Path) -> None:
    manager, chat = _chat_with_form(tmp_path)
    manager.begin_restart_drain()
    result = await manager.respond_question(
        chat.chat_id, request_id="form-1", answers={"q1": ["ship"]}
    )
    assert result.ok is False
    assert result.retryable is True
    assert json.loads(chat.pending_question)["request_id"] == "form-1"


@pytest.mark.asyncio
async def test_reply_while_a_turn_is_starting_stays_retryable(tmp_path: Path) -> None:
    manager, chat = _chat_with_form(tmp_path)
    manager._broker.register(chat.chat_id, ChatStream(prompt_text="x"))
    result = await manager.respond_question(
        chat.chat_id, request_id="form-1", answers={"q1": ["ship"]}
    )
    assert result.ok is False
    assert result.retryable is True
    assert json.loads(chat.pending_question)["request_id"] == "form-1"


@pytest.mark.asyncio
async def test_live_provider_reply_is_still_forwarded(tmp_path: Path) -> None:
    manager, chat = _chat_with_form(tmp_path)
    calls: list[tuple] = []

    async def send_question_response(request_id, answers, *, cancel, session_id):
        calls.append((request_id, answers, cancel, session_id))
        return QuestionResponseResult(True)

    manager._providers[chat.chat_id] = SimpleNamespace(  # type: ignore[assignment]
        provider=SimpleNamespace(
            current_session_id="ses_1", send_question_response=send_question_response
        )
    )
    result = await manager.respond_question(
        chat.chat_id, request_id="form-1", answers={"q1": ["ship"]}
    )
    assert result.ok is True
    assert result.delivered_as_message is False
    assert len(calls) == 1
    assert manager.get_active_stream(chat.chat_id) is None


@pytest.mark.asyncio
async def test_start_stream_supersedes_a_dead_form(tmp_path: Path) -> None:
    manager, chat = _chat_with_form(tmp_path)
    _install_fake_drive(manager, chat)
    stream = manager.start_stream(chat.chat_id, "never mind, do this instead")
    assert stream is not None
    assert chat.pending_question == ""
    _ = [e async for e in stream.subscribe()]


def test_start_stream_still_refuses_while_a_live_provider_holds_the_form(
    tmp_path: Path,
) -> None:
    manager, chat = _chat_with_form(tmp_path)
    manager._providers[chat.chat_id] = SimpleNamespace(provider=object())  # type: ignore[assignment]
    with pytest.raises(ValueError, match="Answer the open question"):
        manager.start_stream(chat.chat_id, "hello")
    assert json.loads(chat.pending_question)["request_id"] == "form-1"


@pytest.mark.asyncio
async def test_deny_with_no_provider_clears_the_permission(tmp_path: Path) -> None:
    manager, chat = _chat_with_permission(tmp_path)
    result = await manager.respond_permission(
        chat.chat_id, request_id="perm-1", approved=False
    )
    assert result.ok is True
    assert result.expired is True
    assert chat.pending_permission == ""


@pytest.mark.asyncio
async def test_allow_with_no_provider_clears_it_and_reports_expired(
    tmp_path: Path,
) -> None:
    manager, chat = _chat_with_permission(tmp_path)
    result = await manager.respond_permission(
        chat.chat_id, request_id="perm-1", approved=True
    )
    assert result.ok is True
    assert result.expired is True
    assert chat.pending_permission == ""
    assert manager.get_active_stream(chat.chat_id) is None


@pytest.mark.asyncio
async def test_dead_reply_for_an_already_settled_permission_is_plain_ok(
    tmp_path: Path,
) -> None:
    manager, chat = _chat_with_permission(tmp_path)
    chat.pending_permission = ""
    result = await manager.respond_permission(
        chat.chat_id, request_id="perm-1", approved=True
    )
    assert result.ok is True
    assert result.expired is False


@pytest.mark.asyncio
async def test_permission_reply_while_a_turn_is_starting_stays_retryable(
    tmp_path: Path,
) -> None:
    manager, chat = _chat_with_permission(tmp_path)
    manager._broker.register(chat.chat_id, ChatStream(prompt_text="x"))
    result = await manager.respond_permission(
        chat.chat_id, request_id="perm-1", approved=True
    )
    assert result.ok is False
    assert result.retryable is True
    assert json.loads(chat.pending_permission)["request_id"] == "perm-1"


@pytest.mark.asyncio
async def test_permission_reply_for_an_unknown_chat_is_not_retryable(
    tmp_path: Path,
) -> None:
    manager = _make_manager(tmp_path)
    result = await manager.respond_permission(
        "missing-chat", request_id="perm-1", approved=True
    )
    assert result.ok is False
    assert result.retryable is False


@pytest.mark.asyncio
async def test_live_provider_permission_reply_is_still_forwarded(
    tmp_path: Path,
) -> None:
    manager, chat = _chat_with_permission(tmp_path)
    calls: list[tuple] = []

    async def send_permission_response(*args, **kwargs):
        calls.append((args, kwargs))
        return QuestionResponseResult(True)

    manager._providers[chat.chat_id] = SimpleNamespace(  # type: ignore[assignment]
        provider=SimpleNamespace(
            current_session_id="ses_1",
            send_permission_response=send_permission_response,
        )
    )
    result = await manager.respond_permission(
        chat.chat_id, request_id="perm-1", approved=True
    )
    assert result.ok is True
    assert result.expired is False
    assert len(calls) == 1
    assert chat.pending_permission == ""


@pytest.mark.asyncio
async def test_start_stream_supersedes_a_dead_permission(tmp_path: Path) -> None:
    manager, chat = _chat_with_permission(tmp_path)
    _install_fake_drive(manager, chat)
    stream = manager.start_stream(chat.chat_id, "never mind, do this instead")
    assert stream is not None
    assert chat.pending_permission == ""
    _ = [e async for e in stream.subscribe()]


@pytest.mark.asyncio
async def test_start_stream_keeps_a_permission_held_by_a_live_provider(
    tmp_path: Path,
) -> None:
    manager, chat = _chat_with_permission(tmp_path)
    manager._providers[chat.chat_id] = SimpleNamespace(provider=object())  # type: ignore[assignment]
    _install_fake_drive(manager, chat)
    stream = manager.start_stream(chat.chat_id, "hello")
    assert stream is not None
    assert json.loads(chat.pending_permission)["request_id"] == "perm-1"
    _ = [e async for e in stream.subscribe()]


def test_startup_marks_persisted_native_forms_stale(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Stale", workspace="personal")
    a = manager.create_chat(project.project_id, provider="opencode")
    b = manager.create_chat(project.project_id)
    permission = json.dumps({"request_id": "perm-1", "tool_name": "Bash", "message": "ok?"})
    a.pending_permission = permission
    a.pending_question = json.dumps(_FORM)
    legacy = {"questions": [{"id": "q1", "question": "Continue?"}]}
    b.pending_question = json.dumps(legacy)
    manager._save()

    reloaded = _make_manager(tmp_path)
    assert json.loads(reloaded._chats[a.chat_id].pending_question)["stale"] is True
    assert json.loads(reloaded._chats[b.chat_id].pending_question) == legacy
    assert reloaded._chats[a.chat_id].pending_permission == permission


def test_native_answer_prompt_maps_option_values_to_labels() -> None:
    form = json.dumps(_FORM)
    prompt = ProjectChatManager._native_answer_prompt(
        form, {"q1": ["hold", "typed"], "__freeform__": ["extra"]}
    )
    assert 'Answer to your question "Next step": Hold, typed' in prompt
    assert "Answer to your question: extra" in prompt
    assert prompt.startswith("(The question form is no longer open")
    empty = ProjectChatManager._native_answer_prompt(form, {})
    assert "I submitted your question form without answers." in empty
