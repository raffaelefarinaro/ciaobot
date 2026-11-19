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
from ciao.web.routes_api import chat_messages

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
    (pcm._agent_root_for_chat(chat.chat_id) / "notes.md").write_text("hi")
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
                    {
                        "type": "tool",
                        "tool": "shell",
                        "state": {
                            "status": "completed",
                            "input": {"command": "ciao file surface notes.md"},
                        },
                    },
                    {
                        "type": "tool",
                        "tool": "shell",
                        "state": {
                            "status": "completed",
                            "input": {"command": "ciao file surface missing.md"},
                        },
                    },
                    {"type": "text", "text": "world"},
                ],
            },
        ],
    }
    read_thread = AsyncMock(return_value=thread)
    monkeypatch.setattr(OpencodeProvider, "read_thread", read_thread)

    response = asyncio.run(chat_messages(_request(
        f"/api/chats/{chat.chat_id}/messages",
        _app(pcm),
        chat_id=chat.chat_id,
    )))
    rows = json.loads(response.body)

    # The replay reads the session with the engine's runtime root, not the workspace's.
    assert read_thread.await_args.kwargs["runtime_root"] == pcm._config.state_path.parent
    assert any(
        row.get("tool_name") == "_filecard"
        and row["file_path"] == "notes.md"
        and row.get("action") == "surfaced"
        for row in rows
    )
    assert not any(row.get("file_path") == "missing.md" for row in rows)
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


def test_opencode_chat_messages_render_shell_wakes_as_a_notice_before_the_reply(
    tmp_path: Path, monkeypatch,
) -> None:
    pcm = _manager(tmp_path)
    chat = _opencode_chat(pcm, "ses_shell_wake")

    def shell(shell_id: str, exit_code: int, output: str, command: str) -> dict:
        return {
            "info": {
                "role": "synthetic",
                "metadata": {
                    "source": "shell", "shellID": shell_id, "jobID": shell_id,
                    "state": "completed", "truncated": False, "exit": exit_code,
                },
                "description": command,
            },
            "parts": [{
                "type": "text", "synthetic": True,
                "text": (
                    f'<shell id="{shell_id}" state="completed" command="{command}">\n'
                    f"{output}\n\n\nExited with code {exit_code}\n</shell>"
                ),
            }],
        }

    thread = {
        "info": {"id": "ses_shell_wake"},
        "messages": [
            {"info": {"role": "user"}, "parts": [{"type": "text", "text": "go"}]},
            {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "started"}]},
            shell("sh_a", 3, "PROBE_A_DONE", "probe a"),
            shell("sh_b", 0, "B_DONE", "probe b"),
            {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "both done"}]},
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

    assert [row["role"] for row in rows] == ["user", "assistant", "system", "assistant"]
    notice = rows[2]
    assert notice["origin"] == "background_run"
    assert "turn_index" not in notice
    assert "2 background runs finished" in notice["content"]
    assert "FAILED, exit 3" in notice["content"]
    assert "PROBE_A_DONE" in notice["content"]
    assert rows[3]["content"] == "both done"


def test_opencode_chat_messages_split_a_subagent_wake_from_the_turn_before(
    tmp_path: Path, monkeypatch,
) -> None:
    """The wake's reply must not join the earlier turn and demote its answer."""
    pcm = _manager(tmp_path)
    chat = _opencode_chat(pcm, "ses_sub_wake")
    thread = {
        "info": {"id": "ses_sub_wake"},
        "messages": [
            {"info": {"role": "user"}, "parts": [{"type": "text", "text": "go"}]},
            {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "first answer"}]},
            {
                "info": {
                    "role": "synthetic",
                    "metadata": {
                        "source": "subagent", "childID": "ses_child",
                        "agent": "General", "state": "completed",
                    },
                    "description": "Review the client",
                },
                "parts": [{
                    "type": "text", "synthetic": True,
                    "text": (
                        '<subagent sessionID="ses_child" state="completed" '
                        'description="Review the client">\nCHILD_REPORT\n</subagent>'
                    ),
                }],
            },
            {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "follow-up"}]},
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

    assert [row["role"] for row in rows] == ["user", "assistant", "system", "assistant"]
    assert rows[1]["content"] == "first answer"
    assert rows[2]["origin"] == "background_run"
    assert "— Review the client (ses_child, ok)" in rows[2]["content"]
    assert "CHILD_REPORT" in rows[2]["content"]
    assert rows[3]["content"] == "follow-up"


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


def test_opencode_chat_messages_read_each_lineage_session_through_the_registry(
    tmp_path: Path, monkeypatch,
) -> None:
    """The host reaches ``read_thread`` via the descriptor factory, not the adapter."""
    from ciao.provider_registry import ProviderDescriptor
    from ciao.providers.base import BaseProvider

    calls: list[tuple[Path, str]] = []

    class StubProvider(BaseProvider):
        @classmethod
        async def read_thread(cls, workspace_root, session_id, **_kwargs):
            calls.append((workspace_root, session_id))
            return {
                "info": {"id": session_id},
                "messages": [
                    {
                        "info": {"role": "user"},
                        "parts": [{"type": "text", "text": f"q {session_id}"}],
                    },
                    {
                        "info": {"role": "assistant"},
                        "parts": [{"type": "text", "text": f"a {session_id}"}],
                    },
                ],
            }

    real_factory = ProviderDescriptor.factory

    def factory(self):
        return StubProvider if self.id == "opencode" else real_factory(self)

    pcm = _manager(tmp_path)
    chat = _opencode_chat(pcm, "ses_new")
    chat.previous_session_ids = ["ses_old", "ses_mid"]
    pcm._save()
    monkeypatch.setattr(ProviderDescriptor, "factory", factory)

    response = asyncio.run(chat_messages(_request(
        f"/api/chats/{chat.chat_id}/messages",
        _app(pcm),
        chat_id=chat.chat_id,
    )))
    rows = json.loads(response.body)

    root = pcm._agent_root_for_chat(chat.chat_id)
    assert calls == [(root, "ses_old"), (root, "ses_mid"), (root, "ses_new")]
    assert [r["content"] for r in rows if r["role"] == "assistant"] == [
        "a ses_old", "a ses_mid", "a ses_new",
    ]
