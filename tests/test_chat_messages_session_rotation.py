"""Tests for stitching /messages history across a mid-conversation SDK
session rotation (autocompact, or a resume-failure fallback that forks a
new session).

Context: the CLI can start a brand-new SDK session file mid-conversation
without any explicit user action. Each session file only holds the turns
written after it started, so once ciaobot's tracked `session_id` rotates,
the earlier turns become invisible in the PWA even though they're still on
disk under the old session_id — unless `/messages` walks the full
`previous_session_ids` lineage instead of reading only the current one.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import chat_messages


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


class _Msg:
    def __init__(self, type_: str, content: str) -> None:
        self.type = type_
        self.message = {"role": type_, "content": [{"type": "text", "text": content}]}


def _request(pcm: ProjectChatManager, config: CiaoConfig, chat_id: str) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/api/chats/{chat_id}/messages",
        "headers": [],
        "path_params": {"chat_id": chat_id},
        "app": SimpleNamespace(
            state=SimpleNamespace(project_chat_manager=pcm, config=config),
        ),
    }
    return Request(scope)


_SEGMENTS = {
    "sess-old": [
        _Msg("user", "what should we build first"),
        _Msg("assistant", "let's start with the backend"),
    ],
    "sess-new": [
        _Msg("user", "This session is being continued from a previous conversation"),
        _Msg("assistant", "continuing: backend is done, testing now"),
    ],
}


@pytest.mark.asyncio
async def test_messages_stitches_history_across_session_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chat that rotated through an older session_id must still render the
    older segment's turns ahead of the current session's, in order.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Rotation test", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="rotation-test")
    chat.previous_session_ids = ["sess-old"]
    chat.session_id = "sess-new"
    pcm._save()

    fake_sdk = SimpleNamespace(
        get_session_messages=lambda session_id, directory: _SEGMENTS[session_id]
    )
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    response = await chat_messages(_request(pcm, pcm._config, chat.chat_id))
    payload = response.body.decode()

    old_pos = payload.index("what should we build first")
    new_pos = payload.index("continuing: backend is done")
    assert old_pos < new_pos, payload
    assert '"content":"let\'s start with the backend"' in payload


@pytest.mark.asyncio
async def test_messages_skips_missing_rotated_segment_without_blanking_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If an older rotated-away session file is gone (pruned, or a remote
    chat opened on a different machine), the current segment must still
    render instead of the whole history going blank.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Rotation missing", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="rotation-missing-test")
    chat.previous_session_ids = ["sess-gone"]
    chat.session_id = "sess-new"
    pcm._save()

    def _fake_get(session_id: str, directory: str):
        if session_id == "sess-gone":
            raise FileNotFoundError("gone")
        return _SEGMENTS[session_id]

    fake_sdk = SimpleNamespace(get_session_messages=_fake_get)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)

    response = await chat_messages(_request(pcm, pcm._config, chat.chat_id))
    payload = response.body.decode()

    assert '"content":"continuing: backend is done, testing now"' in payload
    assert "what should we build first" not in payload


@pytest.mark.asyncio
async def test_new_session_clears_previous_session_ids(tmp_path: Path) -> None:
    """Starting a fresh session severs the old lineage — it must not leak
    into the brand-new conversation.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Rotation reset", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="rotation-reset-test")
    chat.previous_session_ids = ["sess-old"]
    chat.session_id = "sess-new"
    pcm._save()

    pcm.new_session(chat.chat_id)

    assert pcm._chats[chat.chat_id].previous_session_ids == []
    assert pcm._chats[chat.chat_id].session_id == ""
