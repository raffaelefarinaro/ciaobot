from __future__ import annotations

import yaml
from pathlib import Path
import pytest

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager, ChatInfo


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    """Build a ProjectChatManager backed by tmp_path-only stores."""
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


def test_discover_archived_chats_happy_path(tmp_path: Path) -> None:
    # Set up active projects
    pcm = _make_manager(tmp_path)
    
    # We will create projects
    work_proj = pcm.create_project("Work Project A", workspace="work")
    personal_proj = pcm.create_project("Personal Project B", workspace="personal")
    
    # Create vault structure for transcripts
    vault_chats_dir = tmp_path / "memory-vault" / "Logs" / "Chats"
    vault_chats_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. Create a work chat transcript
    chat1_dir = vault_chats_dir / "chat-11111111"
    chat1_provider_dir = chat1_dir / "claude"
    chat1_provider_dir.mkdir(parents=True, exist_ok=True)
    
    chat1_md = chat1_provider_dir / "2026-06-10T12-00-00Z-sess1.md"
    chat1_content = (
        "---\n"
        "type: telegram-transcript\n"
        "provider: claude\n"
        "context: Work Chat Title\n"
        "active_model: sonnet\n"
        "session_id: sess1\n"
        "started: 2026-06-10T12:00:00Z\n"
        "ended: 2026-06-10T13:00:00Z\n"
        "---\n\n"
        "## Turn 1\n\n"
        "### User\n\n"
        "```text\n"
        "[CIAO_CONTEXT_BEGIN]\n"
        "[CONTEXT: work -- Workspace. ...]\n"
        '[Project: "Work Project A"]\n'
        "[CIAO_CONTEXT_END]\n\n"
        "Hello!\n"
        "```\n"
    )
    chat1_md.write_text(chat1_content, encoding="utf-8")
    
    # 2. Create a personal chat transcript in a specific project
    chat2_dir = vault_chats_dir / "chat-22222222"
    chat2_provider_dir = chat2_dir / "claude"
    chat2_provider_dir.mkdir(parents=True, exist_ok=True)
    
    chat2_md = chat2_provider_dir / "2026-06-10T14-00-00Z-sess2.md"
    chat2_content = (
        "---\n"
        "type: telegram-transcript\n"
        "provider: pi\n"
        "context: Personal Chat Title\n"
        "selected_model: qwen3\n"
        "session_id: sess2\n"
        "started: 2026-06-10T14:00:00Z\n"
        "ended: 2026-06-10T15:00:00Z\n"
        "---\n\n"
        "## Turn 1\n\n"
        "### User\n\n"
        "```text\n"
        "[CIAO_CONTEXT_BEGIN]\n"
        '[Project: "Personal Project B"]\n'
        "[CIAO_CONTEXT_END]\n\n"
        "Hi!\n"
        "```\n"
    )
    chat2_md.write_text(chat2_content, encoding="utf-8")

    # 3. Create a chat transcript that falls back to General work
    chat3_dir = vault_chats_dir / "chat-33333333"
    chat3_provider_dir = chat3_dir / "claude"
    chat3_provider_dir.mkdir(parents=True, exist_ok=True)
    
    chat3_md = chat3_provider_dir / "2026-06-10T16-00-00Z-sess3.md"
    chat3_content = (
        "---\n"
        "type: telegram-transcript\n"
        "provider: claude\n"
        "context: Work General Chat Title\n"
        "active_model: opus\n"
        "session_id: sess3\n"
        "started: 2026-06-10T16:00:00Z\n"
        "ended: 2026-06-10T17:00:00Z\n"
        "---\n\n"
        "## Turn 1\n\n"
        "### User\n\n"
        "```text\n"
        "[CIAO_CONTEXT_BEGIN]\n"
        "[CONTEXT: work -- Workspace. ...]\n"
        "[CIAO_CONTEXT_END]\n\n"
        "How to do X?\n"
        "```\n"
    )
    chat3_md.write_text(chat3_content, encoding="utf-8")

    # Force discovery
    pcm.list_projects()
    
    # Assert chats discovered and correctly mapped
    c1 = pcm.get_chat("chat-11111111")
    assert c1 is not None
    assert c1.title == "Work Chat Title"
    assert c1.project_id == work_proj.project_id
    assert c1.model == "sonnet"
    assert c1.provider == "claude"
    assert c1.archived is True
    assert c1.created_at == "2026-06-10T12:00:00Z"
    assert c1.last_activity_at == "2026-06-10T13:00:00Z"
    assert c1.archive_path == "memory-vault/Logs/Chats/chat-11111111/claude/2026-06-10T12-00-00Z-sess1.md"
    
    c2 = pcm.get_chat("chat-22222222")
    assert c2 is not None
    assert c2.title == "Personal Chat Title"
    assert c2.project_id == personal_proj.project_id
    assert c2.model == "qwen3"
    assert c2.provider == "pi"
    assert c2.archived is True
    assert c2.created_at == "2026-06-10T14:00:00Z"
    assert c2.last_activity_at == "2026-06-10T15:00:00Z"
    assert c2.archive_path == "memory-vault/Logs/Chats/chat-22222222/claude/2026-06-10T14-00-00Z-sess2.md"

    # Find work General ID
    work_gen = next(p for p in pcm.list_projects() if p.workspace == "work" and p.name == "General")
    c3 = pcm.get_chat("chat-33333333")
    assert c3 is not None
    assert c3.title == "Work General Chat Title"
    assert c3.project_id == work_gen.project_id
    assert c3.archived is True


def test_discover_archived_chats_pruning(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    
    # Seed an archived chat in the manager's db manually
    proj = pcm.create_project("Work Project A", workspace="work")
    
    # Create the chats root directory so pruning check runs
    vault_chats_dir = tmp_path / "memory-vault" / "Logs" / "Chats"
    vault_chats_dir.mkdir(parents=True, exist_ok=True)

    chat = ChatInfo(
        chat_id="chat-old",
        project_id=proj.project_id,
        title="Old Chat",
        archived=True,
        archive_path="memory-vault/Logs/Chats/chat-old/claude/2026-06-10T12-00-00Z-sessold.md",
    )
    pcm._chats["chat-old"] = chat
    pcm._save()
    
    # Verify it exists initially
    assert pcm.get_chat("chat-old") is not None
    
    # Run discovery (mock directories are empty, so transcript file doesn't exist)
    pcm.list_projects()
    
    # Assert it was pruned
    assert pcm.get_chat("chat-old") is None


def test_discover_archived_chats_pruning_ignores_active(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    
    # Seed an active (non-archived) chat in the manager's db
    proj = pcm.create_project("Work Project A", workspace="work")
    chat = pcm.create_chat(proj.project_id, title="Active Chat")
    
    # Verify it exists initially
    assert pcm.get_chat(chat.chat_id) is not None
    assert chat.archived is False
    
    # Run discovery (no transcripts exist)
    pcm.list_projects()
    
    # Assert it was NOT pruned (since it's active and has no archive_path)
    assert pcm.get_chat(chat.chat_id) is not None


def test_continue_archived_chat(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    
    # 1. Create a project and active project list
    proj = pcm.create_project("Work Project A", workspace="work")
    
    # 2. Write a transcript file
    vault_chats_dir = tmp_path / "memory-vault" / "Logs" / "Chats"
    chat_dir = vault_chats_dir / "chat-tobecontinued"
    chat_provider_dir = chat_dir / "claude"
    chat_provider_dir.mkdir(parents=True, exist_ok=True)
    
    chat_md = chat_provider_dir / "2026-06-10T12-00-00Z-sess.md"
    chat_content = (
        "---\n"
        "type: telegram-transcript\n"
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
        '[Project: "Work Project A"]\n'
        "[CIAO_CONTEXT_END]\n\n"
        "What is the capital of Italy?\n"
        "```\n\n"
        "### Assistant\n\n"
        "```text\n"
        "The capital of Italy is Rome.\n"
        "```\n"
    )
    chat_md.write_text(chat_content, encoding="utf-8")
    
    # Run discovery so pcm knows about this archived chat
    pcm.list_projects()
    
    # Continue it
    new_chat = pcm.continue_archived_chat("chat-tobecontinued")
    
    assert new_chat is not None
    assert new_chat.chat_id != "chat-tobecontinued"
    assert new_chat.title == "Chat to Continue"
    assert new_chat.project_id == proj.project_id
    assert new_chat.model == "sonnet"
    assert new_chat.provider == "claude"
    assert new_chat.archived is False
    assert new_chat.handover_context_pending is True
    
    # Verify handover messages
    msgs = new_chat.handover_messages
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "What is the capital of Italy?"
    assert msgs[0]["timestamp"] == "2026-06-10T12:00:00Z"
    
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"] == "The capital of Italy is Rome."
    assert msgs[1]["timestamp"] == "2026-06-10T12:00:00Z"
