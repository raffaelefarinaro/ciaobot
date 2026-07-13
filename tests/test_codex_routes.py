from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from starlette.requests import Request

from ciao.config import CiaoConfig
from ciao.providers.codex import CodexProvider
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import chat_messages, chat_subagents, list_models


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
    })


def test_models_endpoint_exposes_codex_catalog_and_per_model_effort(
    tmp_path: Path, monkeypatch,
) -> None:
    config = CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
    )
    catalog = [{
        "id": "gpt-test",
        "model": "gpt-test",
        "displayName": "GPT Test",
        "description": "Test model",
        "hidden": False,
        "isDefault": True,
        "defaultReasoningEffort": "high",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low", "description": "Low"},
            {"reasoningEffort": "high", "description": "High"},
        ],
        "inputModalities": ["text", "image"],
    }]
    monkeypatch.setattr(
        CodexProvider, "model_catalog", AsyncMock(return_value=catalog)
    )
    app = SimpleNamespace(state=SimpleNamespace(config=config))

    response = asyncio.run(list_models(_request("/api/models", app)))
    data = json.loads(response.body)

    assert data["provider_models"]["codex"] == ["gpt-test"]
    assert data["provider_defaults"]["codex"] == "gpt-test"
    assert data["alias_tiers"]["codex"] == {
        "haiku": "gpt-test",
        "sonnet": "gpt-test",
        "opus": "gpt-test",
        "fable": "gpt-test",
    }
    assert data["model_reasoning_levels"]["gpt-test"] == ["low", "high"]
    assert data["codex_model_metadata"]["gpt-test"]["display_name"] == "GPT Test"
    assert data["backends"]["codex"] is True


def test_codex_chat_messages_render_thread_items(
    tmp_path: Path, monkeypatch,
) -> None:
    pcm = _manager(tmp_path)
    project = pcm.create_project("codex", workspace="personal")
    chat = pcm.create_chat(
        project.project_id, model="gpt-test", provider="codex"
    )
    chat.session_id = "thread-1"
    chat.user_turn_images["0"] = ["image.png"]
    pcm._save()
    thread = {
        "id": "thread-1",
        "turns": [{
            "id": "turn-1",
            "items": [
                {"type": "userMessage", "id": "u1", "content": [{
                    "type": "text",
                    "text": "[CIAO_CONTEXT_BEGIN]\nproject=x\n[CIAO_CONTEXT_END]\n\nhello",
                }]},
                {"type": "commandExecution", "id": "cmd", "command": "pwd"},
                {"type": "fileChange", "id": "patch", "changes": [{
                    "path": "notes.md", "kind": "update",
                }]},
                {"type": "agentMessage", "id": "a1", "text": "world"},
            ],
        }],
    }
    monkeypatch.setattr(
        CodexProvider, "read_thread", AsyncMock(return_value=thread)
    )
    app = SimpleNamespace(state=SimpleNamespace(
        config=pcm._config,
        project_chat_manager=pcm,
    ))

    response = asyncio.run(chat_messages(_request(
        f"/api/chats/{chat.chat_id}/messages",
        app,
        chat_id=chat.chat_id,
    )))
    rows = json.loads(response.body)

    assert rows[0]["role"] == "user"
    assert rows[0]["content"] == "hello"
    assert rows[0]["images"] == ["image.png"]
    assert any(row.get("tool_name") == "_activity" and "pwd" in row["content"] for row in rows)
    assert any(row.get("tool_name") == "_filecard" and row["file_path"] == "notes.md" for row in rows)
    assert rows[-1] == {"role": "assistant", "content": "world"}


def test_codex_chat_messages_preserve_commentary_and_final_phases(
    tmp_path: Path, monkeypatch,
) -> None:
    pcm = _manager(tmp_path)
    project = pcm.create_project("codex-phases", workspace="personal")
    chat = pcm.create_chat(
        project.project_id, model="gpt-test", provider="codex"
    )
    chat.session_id = "thread-phases"
    pcm._save()
    thread = {
        "id": "thread-phases",
        "turns": [{
            "id": "turn-1",
            "items": [
                {"type": "userMessage", "id": "u1", "content": [{
                    "type": "text", "text": "check it",
                }]},
                {
                    "type": "agentMessage",
                    "id": "a1",
                    "text": "I'll check that now.",
                    "phase": "commentary",
                },
                {
                    "type": "agentMessage",
                    "id": "a2",
                    "text": "Done.",
                    "phase": "final_answer",
                },
            ],
        }],
    }
    monkeypatch.setattr(
        CodexProvider, "read_thread", AsyncMock(return_value=thread)
    )
    app = SimpleNamespace(state=SimpleNamespace(
        config=pcm._config,
        project_chat_manager=pcm,
    ))

    response = asyncio.run(chat_messages(_request(
        f"/api/chats/{chat.chat_id}/messages",
        app,
        chat_id=chat.chat_id,
    )))
    rows = json.loads(response.body)

    assert rows[1] == {
        "role": "assistant",
        "content": "I'll check that now.",
        "phase": "commentary",
    }
    assert rows[2] == {
        "role": "assistant",
        "content": "Done.",
        "phase": "final_answer",
    }


def test_codex_subagents_read_receiver_threads(
    tmp_path: Path, monkeypatch,
) -> None:
    pcm = _manager(tmp_path)
    project = pcm.create_project("codex", workspace="personal")
    chat = pcm.create_chat(
        project.project_id, model="gpt-test", provider="codex"
    )
    chat.session_id = "parent-thread"
    pcm._save()
    parent = {
        "id": "parent-thread",
        "turns": [{"items": [{
            "type": "collabAgentToolCall",
            "id": "agent-call",
            "receiverThreadIds": ["child-thread"],
            "prompt": "Research it",
            "agentsStates": {"child-thread": {"status": "completed"}},
            "tool": "spawnAgent",
            "status": "completed",
        }]}],
    }
    child = {
        "id": "child-thread",
        "turns": [{"items": [
            {"type": "agentMessage", "id": "a1", "text": "Findings"},
            {
                "type": "collabAgentToolCall",
                "id": "nested-call",
                "tool": "spawnAgent",
                "receiverThreadIds": ["grandchild-thread"],
                "agentsStates": {"grandchild-thread": {"status": "completed"}},
                "prompt": "Verify it",
                "status": "completed",
            },
        ]}],
    }
    grandchild = {
        "id": "grandchild-thread",
        "turns": [{"items": [
            {"type": "agentMessage", "id": "a2", "text": "Verified"},
        ]}],
    }
    monkeypatch.setattr(
        CodexProvider,
        "read_thread",
        AsyncMock(side_effect=[parent, child, grandchild]),
    )
    app = SimpleNamespace(state=SimpleNamespace(
        config=pcm._config,
        project_chat_manager=pcm,
    ))

    response = asyncio.run(chat_subagents(_request(
        f"/api/chats/{chat.chat_id}/subagents",
        app,
        chat_id=chat.chat_id,
    )))
    rows = json.loads(response.body)

    assert rows == [{
        "agent_id": "child-thread",
        "parent_agent_id": "",
        "messages": [
            {"role": "assistant", "content": "Findings"},
            {
                "role": "system",
                "content": "⚙️ Agent completed Verify it",
                "tool_name": "_activity",
            },
        ],
        "tool_use_id": "agent-call",
        "description": "Research it",
        "subagent_type": "codex",
        "is_async": True,
        "status": "completed",
        "turn_index": 0,
    }, {
        "agent_id": "grandchild-thread",
        "parent_agent_id": "child-thread",
        "messages": [{"role": "assistant", "content": "Verified"}],
        "tool_use_id": "nested-call",
        "description": "Verify it",
        "subagent_type": "codex",
        "is_async": True,
        "status": "completed",
        "turn_index": 0,
    }]
