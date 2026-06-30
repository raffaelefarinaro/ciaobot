"""Tests for is_session_local check across all providers."""

from __future__ import annotations

from pathlib import Path
import pytest
from unittest.mock import MagicMock

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


def test_is_session_local_new_chat(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    chat = ChatInfo(
        chat_id="chat-123",
        project_id="proj-123",
        session_id="",
        provider="claude",
    )
    assert pcm.is_session_local(chat) is True


def test_is_session_local_claude_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Patch home directory to tmp_path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    pcm = _make_manager(tmp_path)
    
    # workspace root: tmp_path
    # slug for tmp_path is str(tmp_path).replace("/", "-").lstrip("-")
    slug = str(tmp_path).replace("/", "-").lstrip("-")
    projects_dir = tmp_path / ".claude" / "projects" / f"-{slug}"
    projects_dir.mkdir(parents=True, exist_ok=True)
    
    session_id = "session-uuid-123"
    session_file = projects_dir / f"{session_id}.jsonl"
    session_file.write_text("{}", encoding="utf-8")
    
    chat = ChatInfo(
        chat_id="chat-123",
        project_id="proj-123",
        session_id=session_id,
        provider="claude",
    )
    assert pcm.is_session_local(chat) is True


def test_is_session_local_claude_workspace_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Test fallback across all projects when workspace root slug changes
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    pcm = _make_manager(tmp_path)
    
    # Write to a different project slug dir (e.g. simulating user ran ciao in home dir previously)
    other_projects_dir = tmp_path / ".claude" / "projects" / "-Users-private-user"
    other_projects_dir.mkdir(parents=True, exist_ok=True)
    
    session_id = "session-uuid-123"
    session_file = other_projects_dir / f"{session_id}.jsonl"
    session_file.write_text("{}", encoding="utf-8")
    
    chat = ChatInfo(
        chat_id="chat-123",
        project_id="proj-123",
        session_id=session_id,
        provider="claude",
    )
    # The default projects_dir won't have it, but the fallback projects search will find it
    assert pcm.is_session_local(chat) is True


def test_is_session_local_pi_absolute_path(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    
    session_file = tmp_path / "pi_session.jsonl"
    session_file.write_text("{}", encoding="utf-8")
    
    chat = ChatInfo(
        chat_id="chat-123",
        project_id="proj-123",
        session_id=str(session_file),
        provider="pi",
    )
    assert pcm.is_session_local(chat) is True


def test_is_session_local_pi_home_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    pcm = _make_manager(tmp_path)
    
    pi_chat_dir = tmp_path / ".pi" / "agent" / "sessions" / "chat-123"
    pi_chat_dir.mkdir(parents=True, exist_ok=True)
    
    chat = ChatInfo(
        chat_id="chat-123",
        project_id="proj-123",
        session_id="some-id",
        provider="pi",
    )
    assert pcm.is_session_local(chat) is True
