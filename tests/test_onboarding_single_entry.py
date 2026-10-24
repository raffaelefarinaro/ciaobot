from __future__ import annotations

from pathlib import Path

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.setup_marker import WELCOME_PENDING_FILENAME
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager


def _manager(tmp_path: Path) -> ProjectChatManager:
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


def test_fresh_store_does_not_create_a_chat(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    assert manager.list_chats() == []


def test_existing_chat_is_untouched_on_load(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    project = manager.list_projects()[0]
    chat = manager.create_chat(project.project_id, title="Existing")

    reloaded = _manager(tmp_path)
    restored = reloaded.get_chat(chat.chat_id)
    assert restored is not None
    assert restored.title == "Existing"
    assert len(reloaded.list_chats()) == 1


def test_welcome_pending_flag_is_independent(tmp_path: Path) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    marker = runtime / WELCOME_PENDING_FILENAME
    marker.write_text("2026-10-24T00:00:00+00:00", encoding="utf-8")

    manager = _manager(tmp_path)
    assert marker.is_file()
    assert manager.list_chats() == []


def test_the_interview_is_reachable_on_request(tmp_path: Path) -> None:
    """Nothing creates it automatically; the explicit action still does."""
    manager = _manager(tmp_path)
    assert not [chat for chat in manager._chats.values() if chat.title.endswith("👋")]
    general = next(item for item in manager.list_projects("personal") if item.name == "General")
    chat_id = manager.start_onboarding_interview(general.project_id)
    assert chat_id
    assert any(chat.title.endswith("👋") for chat in manager._chats.values())
