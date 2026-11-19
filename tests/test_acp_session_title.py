"""Automatic titles from the agent's own session title (ACP session_info_update, #3138 C2a).

A chat title carries its origin (ChatInfo.title_origin). The host applies an adapter title
over the New Chat placeholder and over Ciaobot's own auto-title, never over a user-owned
title, and a later adapter title replaces an earlier one. Ciaobot's fallback is not written
once an adapter title exists.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao.models import ResultEvent, SessionTitleEvent
from ciao.web.chat_streaming import StreamOutcome
from tests.test_context_capsule import _make_manager


def _chat(manager, title: str = "New Chat"):
    return manager.create_chat(
        manager.create_project("Ciaobot", workspace="work").project_id, title=title
    )


def _published(manager, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    sent: list[dict] = []
    monkeypatch.setattr(manager._events, "publish", sent.append)
    return sent


def _no_llm_title(manager, monkeypatch: pytest.MonkeyPatch) -> None:
    # The LLM fallback would call a model; with no session the native read is skipped,
    # so the deterministic fallback is the title Ciaobot writes.
    async def none(_chat, _user_text, _assistant_text):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(manager, "_llm_chat_title", none)


def _reload(tmp_path: Path):
    return _make_manager(tmp_path)


def test_adapter_title_replaces_the_default_title(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _make_manager(tmp_path)
    chat = _chat(manager)
    sent = _published(manager, monkeypatch)

    assert manager.apply_adapter_title(chat.chat_id, "Fix the login redirect") is True

    assert chat.title == "Fix the login redirect"
    assert chat.title_origin == "adapter"
    assert sent[-1] == {
        "type": "chat_title",
        "chat_id": chat.chat_id,
        "title": "Fix the login redirect",
        "status": "ready",
    }


@pytest.mark.asyncio
async def test_fallback_then_adapter_title_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Ciaobot's own deterministic title is written first; the agent's title arrives later
    # (Claude sends it in the background after the first prompt) and must replace it.
    manager = _make_manager(tmp_path)
    _no_llm_title(manager, monkeypatch)
    chat = _chat(manager)
    assert await manager.auto_title_if_default(chat.chat_id, "Fix the login redirect bug") == (
        "Fix the login redirect bug"
    )
    assert chat.title_origin == "fallback"
    sent = _published(manager, monkeypatch)

    assert manager.apply_adapter_title(chat.chat_id, "Repair login redirect") is True

    assert chat.title == "Repair login redirect"
    assert chat.title_origin == "adapter"
    assert sent[-1]["title"] == "Repair login redirect"


@pytest.mark.asyncio
async def test_adapter_then_fallback_keeps_the_adapter_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first-message pass and the end-of-turn pass both reach the fallback; with an
    # adapter title in place neither may write it, and nothing is published for it.
    manager = _make_manager(tmp_path)
    _no_llm_title(manager, monkeypatch)
    chat = _chat(manager)
    assert manager.apply_adapter_title(chat.chat_id, "Fix the login redirect") is True
    sent = _published(manager, monkeypatch)

    await manager._auto_title_and_publish(chat.chat_id, "Something else entirely", "")

    assert chat.title == "Fix the login redirect"
    assert chat.title_origin == "adapter"
    assert not any(event.get("type") == "chat_title" for event in sent)


def test_user_rename_then_adapter_title_keeps_the_rename(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat = _chat(manager)
    manager.update_chat(chat.chat_id, title="My planning notes")
    assert chat.title_origin == "user"

    assert manager.apply_adapter_title(chat.chat_id, "Fix the login redirect") is False

    assert chat.title == "My planning notes"
    assert chat.title_origin == "user"


@pytest.mark.asyncio
async def test_a_user_rename_of_a_fallback_title_is_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _make_manager(tmp_path)
    _no_llm_title(manager, monkeypatch)
    chat = _chat(manager)
    await manager.auto_title_if_default(chat.chat_id, "Fix the login redirect bug")
    manager.update_chat(chat.chat_id, title="Renamed by hand")

    assert manager.apply_adapter_title(chat.chat_id, "Fix the login redirect") is False
    assert chat.title == "Renamed by hand"


def test_a_later_adapter_title_replaces_the_provisional_one(tmp_path: Path) -> None:
    # Claude's first title can be the raw first prompt; the generated one follows.
    manager = _make_manager(tmp_path)
    chat = _chat(manager)
    manager.apply_adapter_title(chat.chat_id, "please look at the login redirect")

    assert manager.apply_adapter_title(chat.chat_id, "Fix the login redirect") is True

    assert chat.title == "Fix the login redirect"
    assert chat.title_origin == "adapter"


def test_a_rename_after_an_adapter_title_is_kept(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat = _chat(manager)
    manager.apply_adapter_title(chat.chat_id, "Fix the login redirect")
    manager.update_chat(chat.chat_id, title="Renamed by hand")

    assert manager.apply_adapter_title(chat.chat_id, "A newer title") is False

    assert chat.title == "Renamed by hand"


@pytest.mark.asyncio
async def test_adapter_title_equal_to_the_fallback_stops_the_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The agent's title can equal the deterministic fallback text. The text does not change,
    # but the origin does, so the end-of-turn pass must not replace it with the LLM title.
    manager = _make_manager(tmp_path)
    _no_llm_title(manager, monkeypatch)
    chat = _chat(manager)
    await manager.auto_title_if_default(chat.chat_id, "Fix the login redirect bug")

    assert manager.apply_adapter_title(chat.chat_id, "Fix the login redirect bug") is False
    assert chat.title_origin == "adapter"

    assert await manager.auto_title_if_default(chat.chat_id, "x", "y") is None
    assert chat.title == "Fix the login redirect bug"


def test_placeholder_and_blank_titles_are_dropped(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat = _chat(manager)

    assert manager.apply_adapter_title(chat.chat_id, "New session - 2026-11-19T10:00:00.000Z") is False
    assert manager.apply_adapter_title(chat.chat_id, "   ") is False

    assert chat.title == "New Chat"
    assert chat.title_origin == "default"


def test_a_title_that_echoes_the_context_capsule_is_dropped(tmp_path: Path) -> None:
    # An agent's summarizer can echo the first prompt, which carries Ciaobot's capsule.
    manager = _make_manager(tmp_path)
    chat = _chat(manager)
    leaked = '[CIAO_CONTEXT_BEGIN]\n[Chat ID: "chat-1"]\n[CIAO_CONTEXT_END]\n\nWhat is the weather?'

    assert manager.apply_adapter_title(chat.chat_id, leaked) is False
    assert chat.title == "New Chat"


def test_unknown_chat_is_ignored(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    assert manager.apply_adapter_title("no-such-chat", "Fix the login redirect") is False


def test_user_owned_title_survives_a_restart(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat = _chat(manager)
    manager.update_chat(chat.chat_id, title="My planning notes")

    reloaded = _reload(tmp_path)
    again = reloaded._chats[chat.chat_id]

    assert again.title_origin == "user"
    assert reloaded.apply_adapter_title(chat.chat_id, "Fix the login redirect") is False
    assert again.title == "My planning notes"


def test_a_rename_to_new_chat_survives_a_restart(tmp_path: Path) -> None:
    # A title that reads as the placeholder is still user-owned. Only the persisted origin
    # tells it apart from a chat that never got a title, so the reload must keep it.
    manager = _make_manager(tmp_path)
    chat = _chat(manager)
    manager.update_chat(chat.chat_id, title="New Chat")

    reloaded = _reload(tmp_path)

    assert reloaded._chats[chat.chat_id].title_origin == "user"
    assert reloaded.apply_adapter_title(chat.chat_id, "Fix the login redirect") is False


def test_adapter_owned_title_survives_a_restart_and_a_later_adapter_title_replaces_it(
    tmp_path: Path,
) -> None:
    manager = _make_manager(tmp_path)
    chat = _chat(manager)
    manager.apply_adapter_title(chat.chat_id, "please look at the login redirect")

    reloaded = _reload(tmp_path)
    again = reloaded._chats[chat.chat_id]
    assert again.title_origin == "adapter"

    assert reloaded.apply_adapter_title(chat.chat_id, "Fix the login redirect") is True
    assert again.title == "Fix the login redirect"


def test_old_record_with_a_non_default_title_and_no_origin_is_user_owned(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat = _chat(manager)
    manager.update_chat(chat.chat_id, title="Named before the upgrade")
    path = manager._path
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["chats"][chat.chat_id]["title_origin"]
    path.write_text(json.dumps(data), encoding="utf-8")

    reloaded = _reload(tmp_path)
    again = reloaded._chats[chat.chat_id]

    assert again.title_origin == "user"
    assert reloaded.apply_adapter_title(chat.chat_id, "Fix the login redirect") is False
    assert again.title == "Named before the upgrade"


def test_old_record_with_the_default_title_and_no_origin_takes_the_adapter_title(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat = _chat(manager)
    path = manager._path
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["chats"][chat.chat_id]["title_origin"]
    path.write_text(json.dumps(data), encoding="utf-8")

    reloaded = _reload(tmp_path)
    again = reloaded._chats[chat.chat_id]

    assert again.title_origin == "default"
    assert reloaded.apply_adapter_title(chat.chat_id, "Fix the login redirect") is True
    assert again.title == "Fix the login redirect"


@pytest.mark.asyncio
async def test_session_title_event_titles_the_chat_and_is_not_streamed(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat = _chat(manager)
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
    chat = _chat(manager)

    class _Service:
        background_work_outstanding = False

        async def drain_events(self):  # type: ignore[no-untyped-def]
            yield SessionTitleEvent(type="session_title", title="Generated late")

    manager._providers[chat.chat_id] = _Service()  # type: ignore[assignment]
    await manager._streaming.drain_between_turns(chat.chat_id, chat.project_id)

    assert chat.title == "Generated late"


def test_a_long_multiline_agent_title_is_stored_as_one_short_clean_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Claude's fallback title can be the raw first prompt: several paragraphs, terminal escapes
    # and a bidi override. Only one short printable line may reach the store or the broadcast.
    manager = _make_manager(tmp_path)
    chat = _chat(manager)
    sent = _published(manager, monkeypatch)
    raw = "\x1b[2JPlease fix the login redirect ‮gnp.xe\n\n" + "the body of my first prompt " * 180

    assert len(raw) > 5000
    assert manager.apply_adapter_title(chat.chat_id, raw) is True

    assert chat.title == "[2JPlease fix the login redirect gnp.xe"
    assert sent[-1]["title"] == chat.title
    assert _reload(tmp_path)._chats[chat.chat_id].title == chat.title


def test_a_single_huge_agent_title_line_is_truncated_to_the_llm_title_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _make_manager(tmp_path)
    chat = _chat(manager)
    sent = _published(manager, monkeypatch)
    raw = "word " * 1000  # one line, about 5 KB

    assert manager.apply_adapter_title(chat.chat_id, raw) is True

    assert len(chat.title) <= 60
    assert chat.title.endswith("...")
    assert "\n" not in chat.title
    assert sent[-1]["title"] == chat.title


def test_control_and_format_characters_are_dropped_from_an_agent_title(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat = _chat(manager)

    assert manager.apply_adapter_title(chat.chat_id, "Fix\x00 the‮ login​\tredirect") is True

    assert chat.title == "Fix the login redirect"


@pytest.mark.asyncio
async def test_a_claude_chat_takes_no_native_title_poll(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Claude's title arrives as an event; the native poll must not hold back the LLM title by
    # the OpenCode schedule. Checked by the schedule handed to the poll, not by elapsed time.
    manager = _make_manager(tmp_path)
    _no_llm_title(manager, monkeypatch)
    chat = _chat(manager)
    polled: list[tuple[float, ...]] = []

    async def record(chat_id, delays, is_target):  # type: ignore[no-untyped-def]
        polled.append(tuple(delays))
        return False, None

    monkeypatch.setattr(manager, "_poll_native_title", record)
    await manager.auto_title_if_default(chat.chat_id, "Fix the login redirect bug")

    assert polled == [()]
