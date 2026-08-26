from __future__ import annotations

from pathlib import Path

from ciao.config import CiaoConfig
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
    )
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )


def test_mark_read_requests_cross_device_notification_clear(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="Unread")
    chat.last_activity_at = "2026-08-11T10:00:00Z"
    chat.last_read_at = "2026-08-11T09:00:00Z"

    cleared: list[str] = []
    pcm.clear_notifications_cb = cleared.append

    pcm.mark_read(chat.chat_id)

    assert cleared == [chat.chat_id]


def test_mark_all_read_requests_one_clear_per_unread_chat(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    unread = pcm.create_chat(project.project_id, title="Unread")
    unread.last_activity_at = "2026-08-11T10:00:00Z"
    read = pcm.create_chat(project.project_id, title="Read")
    read.last_activity_at = "2026-08-11T09:00:00Z"
    read.last_read_at = "2026-08-11T10:00:00Z"

    cleared: list[str] = []
    pcm.clear_notifications_cb = cleared.append

    assert pcm.mark_all_read() == [unread.chat_id]
    assert cleared == [unread.chat_id]


def test_mark_unread_clears_the_read_stamp(tmp_path: Path) -> None:
    """Marking a chat unread on purpose resets last_read_at so the chat is
    unread again on every device, and publishes a chat_unread event."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("General", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="Read")
    chat.last_activity_at = "2026-08-11T10:00:00Z"
    chat.last_read_at = "2026-08-11T10:00:00Z"

    events: list[dict] = []
    pcm._events.publish = events.append  # type: ignore[method-assign]

    pcm.mark_unread(chat.chat_id)

    assert chat.last_read_at == ""
    assert any(
        e.get("type") == "chat_unread" and e.get("chat_id") == chat.chat_id
        for e in events
    )


def test_mark_unread_unknown_chat_returns_none(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    assert pcm.mark_unread("no-such-chat") is None
