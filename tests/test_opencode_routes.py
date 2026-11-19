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
from ciao.providers.acp.agents import OpencodeAcpProvider
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
    """An opencode chat's history is its ACP session journal, read with the engine's runtime root."""
    pcm = _manager(tmp_path)
    chat = _opencode_chat(pcm, "ses_003133027ffe")
    (pcm._agent_root_for_chat(chat.chat_id) / "notes.md").write_text("hi")
    chat.user_turn_images["0"] = ["image.png"]
    pcm._save()
    # The shape ``OpencodeAcpProvider.read_thread`` returns: the journal rows, in order.
    journal = {"acp_journal": [
        {"type": "user", "text": f"{_ENVELOPE}hello", "turn_origin": ""},
        {"type": "thinking", "text": "quick think"},
        {"type": "tool_use", "tool_name": "bash", "tool_input": "pwd", "tool_use_id": "t1"},
        {"type": "tool_use", "tool_name": "write", "tool_input": "notes.md", "tool_use_id": "t2",
         "file_touches": [{"file_path": "notes.md", "action": "edit"}]},
        {"type": "text_delta", "text": "world"},
        {"type": "result", "result": "world", "is_error": False},
    ]}
    read_thread = AsyncMock(return_value=journal)
    monkeypatch.setattr(OpencodeAcpProvider, "read_thread", read_thread)

    response = asyncio.run(chat_messages(_request(
        f"/api/chats/{chat.chat_id}/messages",
        _app(pcm),
        chat_id=chat.chat_id,
    )))
    rows = json.loads(response.body)

    # The replay reads the session with the engine's runtime root, not the workspace's.
    assert read_thread.await_args.kwargs["runtime_root"] == pcm._config.state_path.parent
    assert rows[0]["role"] == "user"
    assert rows[0]["content"] == "hello"
    assert rows[0]["images"] == ["image.png"]
    assert any(
        row.get("tool_name") == "_thinking" and row["content"] == "quick think"
        for row in rows
    )
    assert any(
        row.get("tool_name") == "_filecard" and row["file_path"] == "notes.md"
        for row in rows
    )
    assert rows[-1] == {"role": "assistant", "content": "world"}


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
        OpencodeAcpProvider, "read_thread", AsyncMock(return_value={})
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
            return {"acp_journal": [
                {"type": "user", "text": f"q {session_id}", "turn_origin": ""},
                {"type": "text_delta", "text": f"a {session_id}"},
                {"type": "result", "result": f"a {session_id}", "is_error": False},
            ]}

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
    # Exactly one read per lineage session, oldest first: the ACP path answers, so no second read.
    assert calls == [(root, "ses_old"), (root, "ses_mid"), (root, "ses_new")]
    assert [r["content"] for r in rows if r["role"] == "assistant"] == [
        "a ses_old", "a ses_mid", "a ses_new",
    ]
