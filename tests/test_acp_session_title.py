"""Automatic titles from the agent's own session title (ACP session_info_update, #3138 C2a).

The host applies an adapter title only while the chat shows the default title or the
adapter title it wrote before. A user rename is never replaced.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao.models import ResultEvent, SessionTitleEvent
from ciao.web.chat_streaming import StreamOutcome
from tests.test_context_capsule import _make_manager


def _chat(manager, title: str):
    chat = manager.create_chat(manager.create_project("Ciaobot", workspace="work").project_id)
    chat.title = title
    return chat


def _published(manager, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    sent: list[dict] = []
    monkeypatch.setattr(manager._events, "publish", sent.append)
    return sent


def test_adapter_title_replaces_the_default_title(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _make_manager(tmp_path)
    chat = _chat(manager, "New Chat")
    sent = _published(manager, monkeypatch)

    assert manager.apply_adapter_title(chat.chat_id, "Fix the login redirect") is True

    assert chat.title == "Fix the login redirect"
    assert chat.adapter_title == "Fix the login redirect"
    assert sent[-1] == {
        "type": "chat_title",
        "chat_id": chat.chat_id,
        "title": "Fix the login redirect",
        "status": "ready",
    }


def test_adapter_title_never_replaces_a_user_rename(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat = _chat(manager, "My planning notes")

    assert manager.apply_adapter_title(chat.chat_id, "Fix the login redirect") is False

    assert chat.title == "My planning notes"
    assert chat.adapter_title == ""


def test_a_later_adapter_title_replaces_the_provisional_one(tmp_path: Path) -> None:
    # Claude's first title can be the raw first prompt; the generated one follows.
    manager = _make_manager(tmp_path)
    chat = _chat(manager, "New Chat")
    manager.apply_adapter_title(chat.chat_id, "please look at the login redirect")

    assert manager.apply_adapter_title(chat.chat_id, "Fix the login redirect") is True

    assert chat.title == "Fix the login redirect"


def test_a_rename_after_an_adapter_title_is_kept(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat = _chat(manager, "New Chat")
    manager.apply_adapter_title(chat.chat_id, "Fix the login redirect")
    chat.title = "Renamed by hand"

    assert manager.apply_adapter_title(chat.chat_id, "A newer title") is False

    assert chat.title == "Renamed by hand"


def test_placeholder_and_blank_titles_are_dropped(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat = _chat(manager, "New Chat")

    assert manager.apply_adapter_title(chat.chat_id, "New session - 2026-11-19T10:00:00.000Z") is False
    assert manager.apply_adapter_title(chat.chat_id, "   ") is False

    assert chat.title == "New Chat"


def test_a_title_that_echoes_the_context_capsule_is_dropped(tmp_path: Path) -> None:
    # An agent's summarizer can echo the first prompt, which carries Ciaobot's capsule.
    manager = _make_manager(tmp_path)
    chat = _chat(manager, "New Chat")
    leaked = '[CIAO_CONTEXT_BEGIN]\n[Chat ID: "chat-1"]\n[CIAO_CONTEXT_END]\n\nWhat is the weather?'

    assert manager.apply_adapter_title(chat.chat_id, leaked) is False
    assert chat.title == "New Chat"


def test_unknown_chat_is_ignored(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    assert manager.apply_adapter_title("no-such-chat", "Fix the login redirect") is False


@pytest.mark.asyncio
async def test_session_title_event_titles_the_chat_and_is_not_streamed(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat = _chat(manager, "New Chat")
    request = SimpleNamespace(context_digest="", compact=False)

    class _Service:
        provider = SimpleNamespace(current_session_id="ses_1")
        current_session_id = "ses_1"
        background_work_outstanding = False

        async def execute_streaming(self, request):  # type: ignore[no-untyped-def]
            del request
            yield SessionTitleEvent(type="session_title", title="Fix the login redirect")
            yield ResultEvent(type="result", result="done", is_error=False)

    manager._providers[chat.chat_id] = _Service()  # type: ignore[assignment]
    seen = []
    async for event in manager._streaming.drive_stream(
        chat_id=chat.chat_id, request=request, outcome=StreamOutcome()  # type: ignore[arg-type]
    ):
        seen.append(event)

    assert chat.title == "Fix the login redirect"
    assert not any(isinstance(e, SessionTitleEvent) for e in seen)
    assert any(isinstance(e, ResultEvent) for e in seen)


@pytest.mark.asyncio
async def test_session_title_event_between_turns_titles_the_chat(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat = _chat(manager, "New Chat")

    class _Service:
        background_work_outstanding = False

        async def drain_events(self):  # type: ignore[no-untyped-def]
            yield SessionTitleEvent(type="session_title", title="Generated late")

    manager._providers[chat.chat_id] = _Service()  # type: ignore[assignment]
    await manager._streaming.drain_between_turns(chat.chat_id, chat.project_id)

    assert chat.title == "Generated late"
