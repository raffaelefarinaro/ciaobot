"""History rows mark the turns that were steered into a running turn (#1392)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_chat_messages_interrupted import _make_manager, _Msg, _request
from ciao.web.routes_api import chat_messages
from ciao.web.transcript_service import _render_opencode_thread


@pytest.mark.asyncio
async def test_steered_flag_on_claude_history_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Steer history", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="steer-history")
    chat.session_id = "sess-steer"
    chat.user_turn_steered = {"1": True}

    fake_sdk = SimpleNamespace(
        get_session_messages=lambda session_id, directory: [
            _Msg("user", "first prompt"),
            _Msg("assistant", "working"),
            _Msg("user", "steered words"),
            _Msg("assistant", "done"),
        ]
    )
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    response = await chat_messages(_request(pcm, pcm._config, chat.chat_id))
    rows = json.loads(response.body)
    users = [r for r in rows if r["role"] == "user"]

    assert [u["content"] for u in users] == ["first prompt", "steered words"]
    assert "steered" not in users[0]
    assert users[1]["steered"] is True


def test_steered_flag_on_opencode_history_rows(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Steer history oc", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="steer-history-oc")
    chat.user_turn_steered = {"1": True}

    def user(text: str) -> dict:
        return {"info": {"role": "user"}, "parts": [{"type": "text", "text": text}]}

    rows = _render_opencode_thread(
        {"info": {}, "messages": [user("one"), user("two")]}, chat
    )
    users = [r for r in rows if r["role"] == "user"]

    assert [u["content"] for u in users] == ["one", "two"]
    assert "steered" not in users[0]
    assert users[1]["steered"] is True


def test_user_turn_steered_survives_save_and_reload(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Steer persist", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="steer-persist")
    chat.user_turn_steered["2"] = True
    pcm._save()

    pcm._chats.clear()
    pcm._load()
    reloaded = pcm.get_chat(chat.chat_id)
    assert reloaded is not None
    assert reloaded.user_turn_steered == {"2": True}


@pytest.mark.asyncio
async def test_steered_row_has_no_unattended_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The steered index inherits the guard flag but is not a scheduled bubble."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Steer unattended", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="steer-unattended-history")
    chat.session_id = "sess-steer-un"
    chat.user_turn_unattended = {"0": True, "1": True}
    chat.user_turn_steered = {"1": True}
    fake_sdk = SimpleNamespace(
        get_session_messages=lambda session_id, directory: [
            _Msg("user", "scheduled tick"),
            _Msg("assistant", "working"),
            _Msg("user", "steered words"),
        ]
    )
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    response = await chat_messages(_request(pcm, pcm._config, chat.chat_id))
    users = [r for r in json.loads(response.body) if r["role"] == "user"]
    assert users[0]["unattended"] is True
    assert users[1]["steered"] is True
    assert "unattended" not in users[1]

    def user(text: str) -> dict:
        return {"info": {"role": "user"}, "parts": [{"type": "text", "text": text}]}

    rows = _render_opencode_thread({"info": {}, "messages": [user("a"), user("b")]}, chat)
    oc_users = [r for r in rows if r["role"] == "user"]
    assert oc_users[0]["unattended"] is True
    assert "unattended" not in oc_users[1]
