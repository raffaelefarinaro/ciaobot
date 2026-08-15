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
    
    session_id = "12345678-1234-4123-8123-123456789abc"
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
    
    session_id = "12345678-1234-4123-8123-123456789abc"
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


def test_is_session_local_claude_missing_session_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    pcm = _make_manager(tmp_path)
    chat = ChatInfo(
        chat_id="chat-123",
        project_id="proj-123",
        session_id="12345678-1234-4123-8123-123456789abc",
        provider="claude",
    )
    assert pcm.is_session_local(chat) is False


def test_is_session_local_empty_provider_defaults_to_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    pcm = _make_manager(tmp_path)
    chat = ChatInfo(
        chat_id="chat-123",
        project_id="proj-123",
        session_id="12345678-1234-4123-8123-123456789abc",
        provider="",
    )
    assert pcm.is_session_local(chat) is False


@pytest.mark.parametrize(
    ("provider", "session_id"),
    [
        ("opencode", "ses_8aFm2kQx91LpTz"),
        ("pi", "pi-session-1"),
        ("codex", "0195b3c2-aaaa-bbbb-cccc-123456789abc"),
    ],
)
def test_is_session_local_non_claude_providers_are_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    session_id: str,
) -> None:
    """Providers without a local session-file contract are always local.

    Regression for #293: opencode/pi chats were probed for a Claude-style
    ``<session>.jsonl`` that can never exist, got flagged ``local: False``,
    and vanished from the sidebar once closed.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    pcm = _make_manager(tmp_path)
    chat = ChatInfo(
        chat_id="chat-123",
        project_id="proj-123",
        session_id=session_id,
        provider=provider,
    )
    assert pcm.is_session_local(chat) is True


def test_list_chats_dicts_marks_opencode_chats_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    pcm = _make_manager(tmp_path)
    chat = ChatInfo(
        chat_id="chat-oc1",
        project_id="proj-123",
        session_id="ses_8aFm2kQx91LpTz",
        provider="opencode",
    )
    pcm._chats[chat.chat_id] = chat

    dicts = pcm.list_chats_dicts("proj-123")
    by_id = {d["chat_id"]: d for d in dicts}
    assert by_id[chat.chat_id]["local"] is True
