"""Tests for is_session_local check across all providers."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ciao.agent_paths import claude_project_slug
from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao import transcripts
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
    # Claude Code's slug for the workspace root (tmp_path).
    projects_dir = tmp_path / ".claude" / "projects" / claude_project_slug(tmp_path)
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


def test_list_chats_dicts_batches_fresh_claude_presence_by_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    pcm = _make_manager(tmp_path)
    root = tmp_path / ".claude" / "projects"
    slugs = [root / f"slug-{i}" for i in range(3)]
    for slug in slugs:
        slug.mkdir(parents=True)

    ids = [f"00000000-0000-4000-8000-{i:012d}" for i in range(30)]
    for index, session_id in enumerate(ids[:10]):
        (slugs[index % len(slugs)] / f"{session_id}.jsonl").write_text("{}", encoding="utf-8")
    for index, session_id in enumerate(ids):
        chat = ChatInfo(
            chat_id=f"chat-{index}", project_id="proj-123",
            session_id=session_id, provider="claude",
        )
        pcm._chats[chat.chat_id] = chat
    pcm._chats["invalid"] = ChatInfo(
        chat_id="invalid", project_id="proj-123", session_id="ses_bad", provider="claude"
    )
    pcm._chats["new"] = ChatInfo(
        chat_id="new", project_id="proj-123", session_id="", provider="claude"
    )
    pcm._chats["other-provider"] = ChatInfo(
        chat_id="other-provider", project_id="proj-123", session_id="ses_x", provider="opencode"
    )

    real_scandir = os.scandir
    scanned: list[str] = []

    def counting_scandir(path: str | os.PathLike[str]):
        scanned.append(os.fspath(path))
        return real_scandir(path)

    monkeypatch.setattr(transcripts.os, "scandir", counting_scandir)
    first = {row["chat_id"]: row["local"] for row in pcm.list_chats_dicts("proj-123")}
    for i in range(30):
        assert first[f"chat-{i}"] is (i < 10)
    assert first["invalid"] is False
    assert first["new"] is True
    assert first["other-provider"] is True
    assert len(scanned) == 1 + len(slugs)

    scanned.clear()
    (slugs[0] / f"{ids[0]}.jsonl").unlink()
    (slugs[1] / f"{ids[10]}.jsonl").write_text("{}", encoding="utf-8")
    second = {row["chat_id"]: row["local"] for row in pcm.list_chats_dicts("proj-123")}
    assert second["chat-0"] is False
    assert second["chat-10"] is True
    assert sum(second[f"chat-{i}"] for i in range(30)) == 10
    assert len(scanned) == 1 + len(slugs)

    # A newly used cwd must be visible on the very next list request too.
    late_slug = root / "late-cwd"
    late_slug.mkdir()
    (late_slug / f"{ids[11]}.jsonl").write_text("{}", encoding="utf-8")
    third = {row["chat_id"]: row["local"] for row in pcm.list_chats_dicts("proj-123")}
    assert third["chat-11"] is True


def test_batched_session_presence_ignores_broken_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    session_id = "00000000-0000-4000-8000-000000000001"
    assert transcripts._global_sessions_present({session_id}) == set()
    slug = tmp_path / ".claude" / "projects" / "cwd"
    slug.mkdir(parents=True)
    target = tmp_path / "session.jsonl"
    try:
        (slug / f"{session_id}.jsonl").symlink_to(target)
    except OSError:
        pytest.skip("This host cannot create symlinks")
    assert transcripts._global_sessions_present({session_id}) == set()
    target.write_text("{}", encoding="utf-8")
    assert transcripts._global_sessions_present({session_id}) == {session_id}
