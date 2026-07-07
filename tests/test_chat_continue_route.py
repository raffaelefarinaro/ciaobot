from __future__ import annotations

import json
from pathlib import Path
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import chat_continue


def _make_manager(tmp_path: Path) -> tuple[ProjectChatManager, CiaoConfig]:
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    pcm = ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )
    return pcm, config


def _make_client(pcm: ProjectChatManager, config: CiaoConfig) -> TestClient:
    app = Starlette(routes=[
        Route("/api/chats/{chat_id}/continue", chat_continue, methods=["POST"]),
    ])
    app.state.project_chat_manager = pcm
    app.state.config = config
    return TestClient(app)


def test_route_chat_continue(tmp_path: Path) -> None:
    pcm, config = _make_manager(tmp_path)
    client = _make_client(pcm, config)
    
    # 1. Create a project
    proj = pcm.create_project(name="ProjX", workspace="work")
    
    # 2. Write a transcript file
    vault_chats_dir = tmp_path / "memory-vault" / "Logs" / "Chats"
    chat_dir = vault_chats_dir / "chat-arch"
    chat_provider_dir = chat_dir / "claude"
    chat_provider_dir.mkdir(parents=True, exist_ok=True)
    
    chat_md = chat_provider_dir / "2026-06-10T12-00-00Z-sess.md"
    chat_content = (
        "---\n"
        "provider: claude\n"
        "context: Chat to Continue\n"
        "active_model: sonnet\n"
        "session_id: sess1\n"
        "started: 2026-06-10T12:00:00Z\n"
        "ended: 2026-06-10T13:00:00Z\n"
        "---\n\n"
        "## Turn 1\n\n"
        "- Time: 2026-06-10T12:00:00Z\n"
        "### User\n\n"
        "```text\n"
        "[CIAO_CONTEXT_BEGIN]\n"
        "[CONTEXT: work -- Workspace. ...]\n"
        '[Project: "ProjX"]\n'
        "[CIAO_CONTEXT_END]\n\n"
        "What is the capital of Italy?\n"
        "```\n\n"
        "### Assistant\n\n"
        "```text\n"
        "The capital of Italy is Rome.\n"
        "```\n"
    )
    chat_md.write_text(chat_content, encoding="utf-8")
    
    # Run project discovery
    pcm.list_projects()
    
    # Send POST request
    r = client.post("/api/chats/chat-arch/continue")
    assert r.status_code == 200, r.text
    
    data = r.json()
    assert data["chat_id"] != "chat-arch"
    assert data["title"] == "Chat to Continue"
    assert data["project_id"] == proj.project_id
    assert data["model"] == "sonnet"
    assert data["provider"] == "claude"
    assert data["archived"] is False
    
    # Verify the new chat is registered in the manager
    new_chat = pcm.get_chat(data["chat_id"])
    assert new_chat is not None
    assert new_chat.handover_context_pending is True
    assert len(new_chat.handover_messages) == 2
