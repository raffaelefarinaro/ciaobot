from __future__ import annotations

import json
from pathlib import Path

from ciao.config import CiaoConfig
from ciao.providers.pi import PiSettings
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        pi=PiSettings(models=("qwen3-coder",)),
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )


def test_handover_chat_switches_provider_and_persists_messages(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("handover", workspace="personal")
    chat = pcm.create_chat(project.project_id, model="opus", provider="claude")
    chat.session_id = "sess-old"
    chat.user_turn_count = 2
    pcm._save()

    messages = [
        {"role": "user", "content": "Build the handover feature", "timestamp": "2026-06-07T10:00:00Z"},
        {"role": "system", "tool_name": "_activity", "content": "$ Edit ciao/web/project_chats.py"},
        {"role": "assistant", "content": "I changed the backend."},
        {"role": "assistant", "content": "quota hit", "is_error": True},
    ]

    updated = pcm.handover_chat(
        chat.chat_id,
        provider="pi",
        model="qwen3-coder",
        messages=messages,
    )

    assert updated is not None
    assert updated.provider == "pi"
    assert updated.model == "qwen3-coder"
    assert updated.session_id == ""
    assert updated.handover_context_pending is True
    assert updated.handover_messages[0]["content"] == "Build the handover feature"
    assert updated.handover_messages[-1]["role"] == "system"
    assert "Handed over from Claude / opus to Pi / qwen3-coder" in updated.handover_messages[-1]["content"]

    persisted = json.loads((tmp_path / ".runtime" / "web_projects.json").read_text(encoding="utf-8"))
    saved = persisted["chats"][chat.chat_id]
    assert saved["provider"] == "pi"
    assert saved["model"] == "qwen3-coder"
    assert saved["session_id"] == ""
    assert saved["handover_context_pending"] is True
    assert saved["handover_messages"][-1]["role"] == "system"

    reloaded = _make_manager(tmp_path).get_chat(chat.chat_id)
    assert reloaded is not None
    assert reloaded.handover_messages == updated.handover_messages
    assert reloaded.handover_context_pending is True


def test_handover_messages_are_injected_into_next_prompt_once(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("handover", workspace="personal")
    chat = pcm.create_chat(project.project_id, model="opus", provider="claude")
    pcm.handover_chat(
        chat.chat_id,
        provider="pi",
        model="qwen3-coder",
        messages=[
            {"role": "user", "content": "First request"},
            {"role": "assistant", "content": "First answer"},
        ],
    )

    handed = pcm.get_chat(chat.chat_id)
    assert handed is not None
    prefix = pcm._build_prompt_prefix(handed)
    assert "[Provider handover messages]" in prefix
    assert "User: First request" in prefix
    assert "Assistant: First answer" in prefix

    pcm.mark_handover_context_used(chat.chat_id)
    after = pcm.get_chat(chat.chat_id)
    assert after is not None
    assert after.handover_context_pending is False
    assert "[Provider handover messages]" not in pcm._build_prompt_prefix(after)
    assert after.handover_messages
