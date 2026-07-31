"""`prepare_schedule_chat` stamps a schedule backlink on the target chat.

A schedule is 1:many with chats (each project-schedule run spawns a new
chat), so unlike loops the link can't live only on the automation side.
The chat carries `schedule_id` / `schedule_title` so the PWA banner survives
later runs. This covers both branches: project-schedule (new chat) and
fixed-chat (reuse).
"""

from __future__ import annotations

from pathlib import Path

from ciao.config import CiaoConfig
from ciao.schedules import ScheduleEntry
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
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )


def _entry(*, schedule_id: str, web_project_id: str | None = None,
           web_chat_id: str | None = None, title: str = "") -> ScheduleEntry:
    return ScheduleEntry(
        schedule_id=schedule_id,
        daily_time_utc="08:00",
        prompt="Morning briefing.",
        chat_id=0,
        created_at="2026-07-31T00:00:00Z",
        web_project_id=web_project_id,
        web_chat_id=web_chat_id,
        workspace="personal",
        title=title,
    )


def test_project_schedule_stamps_backlink_on_new_chat(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)

    # Stale web_project_id resolves to the seeded personal General project,
    # so a fresh chat is created for this run.
    entry = _entry(schedule_id="sched-abc", web_project_id="proj-stale00",
                   title="Morning briefing")
    chat_id = pcm.prepare_schedule_chat(entry, entry.prompt, "opus", "auto", "claude")

    assert chat_id is not None
    chat = pcm.get_chat(chat_id)
    assert chat is not None
    assert chat.schedule_id == "sched-abc"
    assert chat.schedule_title == "Morning briefing"


def test_fixed_chat_schedule_stamps_backlink_on_existing_chat(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)

    # Create a chat the user owns, then bind a schedule to it.
    proj = pcm.create_project("Holder", workspace="personal")
    target = pcm.create_chat(proj.project_id, title="My chat",
                             model="opus", provider="claude")
    entry = _entry(schedule_id="sched-xyz", web_chat_id=target.chat_id, title="Evening review")
    chat_id = pcm.prepare_schedule_chat(entry, entry.prompt, "opus", "auto", "claude")

    assert chat_id == target.chat_id
    chat = pcm.get_chat(chat_id)
    assert chat is not None
    assert chat.schedule_id == "sched-xyz"
    assert chat.schedule_title == "Evening review"


def test_backlink_persists_across_reload(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    entry = _entry(schedule_id="sched-persist", web_project_id="proj-stale00",
                   title="Daily curation")
    chat_id = pcm.prepare_schedule_chat(entry, entry.prompt, "opus", "auto", "claude")
    assert chat_id is not None

    # Re-load from disk the same way the server does on boot.
    pcm2 = _make_manager(tmp_path)
    chat = pcm2.get_chat(chat_id)
    assert chat is not None
    assert chat.schedule_id == "sched-persist"
    assert chat.schedule_title == "Daily curation"