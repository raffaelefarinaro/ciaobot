"""Transcript replay routes for opencode chats (#295)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from starlette.requests import Request

from ciao.config import CiaoConfig
from ciao.models import AgentRequest, ChatContext
from ciao.providers.opencode import OpencodeProvider
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import chat_messages, chat_subagents

_ENVELOPE = "[CIAO_CONTEXT_BEGIN]\nproject=x\n[CIAO_CONTEXT_END]\n\n"


def _manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    config = CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "archives"),
        path=runtime / "web_projects.json",
    )


def _request(path: str, app, **path_params: str) -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [],
        "app": app,
        "path_params": path_params,
        "query_string": b"",
    })


def _opencode_chat(pcm: ProjectChatManager, session_id: str):
    project = pcm.create_project("opencode", workspace="personal")
    chat = pcm.create_chat(
        project.project_id, model="opencode/big-pickle", provider="opencode"
    )
    chat.session_id = session_id
    pcm._save()
    return chat


def _app(pcm: ProjectChatManager) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(
        config=pcm._config,
        project_chat_manager=pcm,
    ))


def test_opencode_chat_messages_render_session_history(
    tmp_path: Path, monkeypatch,
) -> None:
    """An opencode `ses_*` id must replay via read_thread, not the Claude path."""
    pcm = _manager(tmp_path)
    chat = _opencode_chat(pcm, "ses_003133027ffe")
    chat.user_turn_images["0"] = ["image.png"]
    pcm._save()
    thread = {
        "info": {"id": "ses_003133027ffe"},
        "messages": [
            {
                "info": {"role": "user"},
                "parts": [{"type": "text", "text": f"{_ENVELOPE}hello"}],
            },
            {
                "info": {"role": "assistant"},
                "parts": [
                    {"type": "reasoning", "text": "quick think"},
                    {"type": "step-start"},
                    {
                        "type": "tool",
                        "tool": "bash",
                        "state": {"status": "completed", "input": {"command": "pwd"}},
                    },
                    {
                        "type": "tool",
                        "tool": "write",
                        "state": {"status": "completed", "input": {"filePath": "notes.md"}},
                    },
                    {"type": "text", "text": "world"},
                ],
            },
        ],
    }
    monkeypatch.setattr(
        OpencodeProvider, "read_thread", AsyncMock(return_value=thread)
    )

    response = asyncio.run(chat_messages(_request(
        f"/api/chats/{chat.chat_id}/messages",
        _app(pcm),
        chat_id=chat.chat_id,
    )))
    rows = json.loads(response.body)

    assert rows[0]["role"] == "user"
    assert rows[0]["content"] == "hello"
    assert rows[0]["images"] == ["image.png"]
    assert any(
        row.get("tool_name") == "_thinking" and row["content"] == "quick think"
        for row in rows
    )
    assert any(
        row.get("tool_name") == "_activity" and "bash pwd" in row["content"]
        for row in rows
    )
    assert any(
        row.get("tool_name") == "_filecard" and row["file_path"] == "notes.md"
        for row in rows
    )
    assert rows[-1] == {"role": "assistant", "content": "world"}


def test_opencode_chat_messages_skip_synthetic_user_parts(
    tmp_path: Path, monkeypatch,
) -> None:
    pcm = _manager(tmp_path)
    chat = _opencode_chat(pcm, "ses_synthetic")
    thread = {
        "info": {"id": "ses_synthetic"},
        "messages": [
            {
                "info": {"role": "user"},
                "parts": [
                    {"type": "text", "text": "compaction summary", "synthetic": True},
                    {"type": "text", "text": "typed by hand"},
                ],
            },
        ],
    }
    monkeypatch.setattr(
        OpencodeProvider, "read_thread", AsyncMock(return_value=thread)
    )

    response = asyncio.run(chat_messages(_request(
        f"/api/chats/{chat.chat_id}/messages",
        _app(pcm),
        chat_id=chat.chat_id,
    )))
    rows = json.loads(response.body)

    assert rows[0]["content"] == "typed by hand"


def test_opencode_chat_messages_fall_back_to_the_durable_transcript(
    tmp_path: Path, monkeypatch,
) -> None:
    """When the opencode server/session is unreadable, replay `.runtime`.

    The rendered rows must show the visible prompt, not the injected context
    envelope the recorded turn keeps on disk for chat recovery.
    """
    pcm = _manager(tmp_path)
    chat = _opencode_chat(pcm, "ses_gone")
    request = AgentRequest(
        prompt=f"{_ENVELOPE}hello",
        model="opencode/big-pickle",
        mode="auto",
        provider="opencode",
        display_prompt=f"{_ENVELOPE}hello",
    )
    pcm._transcripts.record_turn(
        request,
        ctx=ChatContext.for_web(chat.chat_id),
        response_text="world",
        effective_model="opencode/big-pickle",
        session_id="ses_gone",
        usage={},
        quota={},
        input_kind="text",
        provider="opencode",
    )
    monkeypatch.setattr(
        OpencodeProvider, "read_thread", AsyncMock(return_value={})
    )

    response = asyncio.run(chat_messages(_request(
        f"/api/chats/{chat.chat_id}/messages",
        _app(pcm),
        chat_id=chat.chat_id,
    )))
    rows = json.loads(response.body)

    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert rows[0]["content"] == "hello"
    assert rows[1]["content"] == "world"


def test_sessionless_opencode_chat_messages_show_durable_startup_error(
    tmp_path: Path,
) -> None:
    """A failed server launch still has visible history without a session id."""
    pcm = _manager(tmp_path)
    chat = _opencode_chat(pcm, "")
    request = AgentRequest(
        prompt="review the architecture",
        model="opencode/big-pickle",
        mode="auto",
        provider="opencode",
        display_prompt="review the architecture",
    )
    pcm._transcripts.record_turn(
        request,
        ctx=ChatContext.for_web(chat.chat_id),
        response_text="opencode serve exited with code 1: database is locked",
        effective_model="opencode/big-pickle",
        session_id=None,
        usage={},
        quota={},
        input_kind="text",
        provider="opencode",
        is_error=True,
    )

    response = asyncio.run(chat_messages(_request(
        f"/api/chats/{chat.chat_id}/messages",
        _app(pcm),
        chat_id=chat.chat_id,
    )))
    rows = json.loads(response.body)

    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert rows[-1]["is_error"] is True
    assert "database is locked" in rows[-1]["content"]


def test_opencode_subagents_read_child_sessions(
    tmp_path: Path, monkeypatch,
) -> None:
    pcm = _manager(tmp_path)
    chat = _opencode_chat(pcm, "ses_parent")
    monkeypatch.setattr(
        OpencodeProvider,
        "read_collab_tree",
        AsyncMock(return_value=[{
            "info": {
                "id": "ses_child",
                "parentID": "ses_parent",
                "title": "Research it",
            },
            "messages": [{
                "info": {"role": "assistant"},
                "parts": [{"type": "text", "text": "Findings"}],
            }],
        }]),
    )

    response = asyncio.run(chat_subagents(_request(
        f"/api/chats/{chat.chat_id}/subagents",
        _app(pcm),
        chat_id=chat.chat_id,
    )))
    rows = json.loads(response.body)

    assert rows == [{
        "agent_id": "ses_child",
        "parent_agent_id": "ses_parent",
        "messages": [{"role": "assistant", "content": "Findings"}],
        "tool_use_id": "",
        "description": "Research it",
        "subagent_type": "opencode",
        "is_async": True,
        "status": "completed",
        "turn_index": 0,
    }]


def test_opencode_subagents_prefer_live_server_over_ephemeral_spawn(
    tmp_path: Path, monkeypatch,
) -> None:
    """A chat with a running provider must not pay for `_EphemeralServer`.

    The subagents poll fires every 4s while a turn streams. If the read cache
    (`_READ_CACHE_TTL`) has already expired, falling through to the
    classmethod spawns a whole new `opencode serve` process just to answer
    this poll -- expensive enough, repeated often enough across concurrent
    chats, to starve the event loop and destabilize the browser's per-chat
    websocket. When the chat's own server is already live, its connection
    must be reused instead.
    """
    pcm = _manager(tmp_path)
    chat = _opencode_chat(pcm, "ses_parent")

    live_provider = OpencodeProvider(tmp_path, config=pcm._config)
    live_provider._client = object()
    live_provider._process = SimpleNamespace(returncode=None)
    live_provider._session_id = "ses_parent"
    monkeypatch.setattr(
        live_provider,
        "read_live_collab_tree",
        AsyncMock(return_value=[{
            "info": {"id": "ses_child", "parentID": "ses_parent", "title": "Live"},
            "messages": [],
        }]),
    )
    service = SimpleNamespace(provider=live_provider)
    pcm._providers[chat.chat_id] = service

    ephemeral_spawn = AsyncMock(side_effect=AssertionError(
        "must not spawn an ephemeral server while a live one is attached"
    ))
    monkeypatch.setattr(OpencodeProvider, "read_collab_tree", ephemeral_spawn)

    response = asyncio.run(chat_subagents(_request(
        f"/api/chats/{chat.chat_id}/subagents",
        _app(pcm),
        chat_id=chat.chat_id,
    )))
    rows = json.loads(response.body)

    assert [row["agent_id"] for row in rows] == ["ses_child"]
    ephemeral_spawn.assert_not_called()


def test_opencode_failed_write_renders_as_activity_not_filecard(
    tmp_path: Path, monkeypatch,
) -> None:
    """A denied/errored write reached nothing on disk: no file card, but the
    attempt stays visible in the Activity trace (matches the live path)."""
    pcm = _manager(tmp_path)
    chat = _opencode_chat(pcm, "ses_failed_write")
    thread = {
        "info": {"id": "ses_failed_write"},
        "messages": [
            {
                "info": {"role": "user"},
                "parts": [{"type": "text", "text": "write it"}],
            },
            {
                "info": {"role": "assistant"},
                "parts": [
                    {
                        "type": "tool",
                        "tool": "write",
                        "state": {
                            "status": "error",
                            "input": {"filePath": "notes.md"},
                            "error": "permission denied",
                        },
                    },
                    {"type": "text", "text": "could not write"},
                ],
            },
        ],
    }
    monkeypatch.setattr(
        OpencodeProvider, "read_thread", AsyncMock(return_value=thread)
    )

    response = asyncio.run(chat_messages(_request(
        f"/api/chats/{chat.chat_id}/messages",
        _app(pcm),
        chat_id=chat.chat_id,
    )))
    rows = json.loads(response.body)

    assert not any(row.get("tool_name") == "_filecard" for row in rows)
    assert any(
        row.get("tool_name") == "_activity" and "write" in row["content"]
        for row in rows
    )


def test_opencode_subagents_derive_status_and_turn_index(
    tmp_path: Path, monkeypatch,
) -> None:
    """A child mid-turn shows as running, anchored to the parent turn that
    was sent before the child session was created."""
    pcm = _manager(tmp_path)
    chat = _opencode_chat(pcm, "ses_parent")
    chat.user_turn_timings["0"] = {"sent_at": "2026-08-15T10:00:00Z"}
    chat.user_turn_timings["1"] = {"sent_at": "2026-08-15T10:05:00Z"}
    pcm._save()
    created_ms = 1786788330000  # 2026-08-15T10:05:30Z, after turn 1
    monkeypatch.setattr(
        OpencodeProvider,
        "read_collab_tree",
        AsyncMock(return_value=[{
            "info": {
                "id": "ses_running_child",
                "parentID": "ses_parent",
                "title": "Long task",
                "time": {"created": created_ms},
            },
            "messages": [{
                "info": {
                    "role": "assistant",
                    "time": {"created": created_ms + 1000},
                },
                "parts": [],
            }],
        }, {
            "info": {
                "id": "ses_failed_child",
                "parentID": "ses_parent",
                "title": "Broken task",
            },
            "messages": [{
                "info": {
                    "role": "assistant",
                    "error": {"name": "UnknownError"},
                },
                "parts": [],
            }],
        }]),
    )

    response = asyncio.run(chat_subagents(_request(
        f"/api/chats/{chat.chat_id}/subagents",
        _app(pcm),
        chat_id=chat.chat_id,
    )))
    rows = json.loads(response.body)

    assert rows[0]["status"] == "running"
    assert rows[0]["turn_index"] == 1
    assert rows[1]["status"] == "failed"
    assert rows[1]["turn_index"] == 0
