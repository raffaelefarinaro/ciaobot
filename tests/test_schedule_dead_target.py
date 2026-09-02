"""Schedules never dispatch into a dead chat, and failures are observable.

Issue #407: a chat-bound schedule whose target chat was archived resumed the
reclaimed provider session and failed instantly with `stream error` — recorded
only in the job log, invisible everywhere a user looks, and repeated forever.
Two fixes are covered here:

1. `prepare_schedule_chat` re-homes a fixed-chat entry whose target is
   archived or deleted (any cadence, not just interval): the archived
   transcript is forked, or a fresh chat opens in the entry's project, and
   `web_chat_id` is re-pointed on the entry (which the tick paths persist).
2. A failed dispatch stamps `last_status: "error"` on the stored row, which
   the PWA sidebar reads as needs-attention.
"""

from __future__ import annotations

from pathlib import Path

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.schedules import ScheduleEntry, ScheduleStore
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
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=f"memory-vault/{name}")
            for name in ("personal",)
        },
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )


def _entry(*, web_chat_id: str | None, frequency: str = "daily") -> ScheduleEntry:
    return ScheduleEntry(
        schedule_id="sched-deadtarget",
        daily_time_utc="08:00" if frequency != "interval" else "",
        prompt="Morning briefing.",
        chat_id=0,
        created_at="2026-09-01T00:00:00Z",
        frequency=frequency,
        web_chat_id=web_chat_id,
        workspace="personal",
        title="Morning briefing",
    )


def _archived_transcript(tmp_path: Path, chat_id: str) -> str:
    rel = f"memory-vault/personal/Logs/Chats/{chat_id}/claude/2026-06-10T12-00-00Z-sess.md"
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "provider: claude\n"
        "context: Archived Briefing\n"
        "active_model: sonnet\n"
        "session_id: sess1\n"
        "started: 2026-06-10T12:00:00Z\n"
        "ended: 2026-06-10T13:00:00Z\n"
        "---\n\n"
        "## Turn 1\n\n"
        "- Time: 2026-06-10T12:00:00Z\n"
        "### User\n\n"
        "```text\n"
        "What is the capital of Italy?\n"
        "```\n\n"
        "### Assistant\n\n"
        "```text\n"
        "Rome.\n"
        "```\n",
        encoding="utf-8",
    )
    return rel


def test_archived_target_forks_a_replacement_for_a_wall_clock_entry(
    tmp_path: Path,
) -> None:
    """The issue's repro: daily schedule, target chat archived afterwards.

    Before the fix the next run resumed the dead provider session and failed
    in ~100ms with an invisible `stream error`, every run, forever. Now the
    archived transcript is forked into a fresh chat and the entry re-points.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    target = pcm.create_chat(project.project_id, title="Daily brief", model="opus")
    target.archived = True
    target.archive_path = _archived_transcript(tmp_path, target.chat_id)
    pcm._save()

    entry = _entry(web_chat_id=target.chat_id)
    chat_id = pcm.prepare_schedule_chat(entry, entry.prompt, "opus", "auto", "claude")

    assert chat_id is not None
    assert chat_id != target.chat_id
    replacement = pcm.get_chat(chat_id)
    assert replacement is not None
    assert replacement.archived is False
    assert replacement.handover_context_pending is True
    assert pcm.get_chat(target.chat_id).archived is True
    # The re-point lands on the entry so the next run does not fork again.
    assert entry.web_chat_id == chat_id


def test_missing_target_opens_a_fresh_chat_for_a_wall_clock_entry(
    tmp_path: Path,
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    target = pcm.create_chat(project.project_id, title="Daily brief", model="opus")
    del pcm._chats[target.chat_id]

    entry = _entry(web_chat_id=target.chat_id)
    # Real write paths stamp the chat's project as the re-home fallback while
    # the chat exists (see stamp_fallback_project); mirror that here.
    entry.fallback_project_id = project.project_id
    chat_id = pcm.prepare_schedule_chat(entry, entry.prompt, "opus", "auto", "claude")

    assert chat_id is not None
    assert chat_id != target.chat_id
    replacement = pcm.get_chat(chat_id)
    assert replacement is not None
    assert replacement.project_id == project.project_id
    assert entry.web_chat_id == chat_id


def test_missing_target_without_a_fallback_rehomes_into_general(
    tmp_path: Path,
) -> None:
    """A weaker claim than the stamped fallback, but still better than
    failing every run: the workspace's General project hosts the run."""
    pcm = _make_manager(tmp_path)
    pcm.create_project("Holder", workspace="personal")
    entry = _entry(web_chat_id="chat-gone")
    chat_id = pcm.prepare_schedule_chat(entry, entry.prompt, "opus", "auto", "claude")

    assert chat_id is not None
    replacement = pcm.get_chat(chat_id)
    assert replacement is not None
    general = next(
        p for p in pcm.list_projects("personal") if p.name == "General"
    )
    assert replacement.project_id == general.project_id
    assert entry.web_chat_id == chat_id


def test_unrecoverable_target_still_returns_none(tmp_path: Path) -> None:
    """No archived transcript to fork and no project/workspace we know about:
    the caller disables the entry rather than erroring every run."""
    pcm = _make_manager(tmp_path)
    entry = _entry(web_chat_id="chat-gone")
    entry.workspace = "no-such-workspace"

    assert pcm.prepare_schedule_chat(entry, entry.prompt, "opus", "auto", "claude") is None


def _dispatch_manager(tmp_path: Path):
    """A real manager wired to a store, with `start_stream` stubbed per-test."""
    pcm = _make_manager(tmp_path)
    runtime = tmp_path / ".runtime"
    store = ScheduleStore(runtime)
    pcm.schedule_store = store
    return pcm, store


def _stream_stub(events: list[dict]):
    """Replace `start_stream` with one that replays `events` then ends."""

    def _fake_start_stream(chat_id, prompt, *args, **kwargs):
        class _Stream:
            def subscribe(self):
                async def _gen():
                    for event in events:
                        yield event
                return _gen()
        return _Stream()

    return _fake_start_stream


def test_failed_dispatch_stamps_last_status_on_the_row(tmp_path: Path) -> None:
    """An endless string of invisible `stream error` job records is not an
    observable failure. The stored row must say so where the sidebar reads."""
    import asyncio

    pcm, store = _dispatch_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="Daily brief", model="opus")
    entry = store.create(
        daily_time_utc="08:00",
        prompt="brief",
        model="opus",
        mode="auto",
        chat_id=0,
        frequency="daily",
        web_chat_id=chat.chat_id,
        workspace="personal",
    )
    pcm.start_stream = _stream_stub([{"type": "error"}])  # type: ignore[method-assign]

    result = asyncio.run(
        pcm.dispatch_schedule(entry, entry.prompt, "opus", "auto", "claude")
    )

    assert result["status"] == "error"
    stored = store.get(entry.schedule_id)
    assert stored is not None
    assert stored.last_status == "error"


def test_later_clean_run_clears_the_error_stamp(tmp_path: Path) -> None:
    """A one-off failure must not brand the entry unhealthy forever.

    Interval entries self-heal through _run_interval's write-back; a
    wall-clock entry has no such write-back, so the dispatch stamps "ok" on
    a completed run and the sidebar warning disappears with it.
    """
    import asyncio

    pcm, store = _dispatch_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="Daily brief", model="opus")
    entry = store.create(
        daily_time_utc="08:00",
        prompt="brief",
        model="opus",
        mode="auto",
        chat_id=0,
        frequency="daily",
        web_chat_id=chat.chat_id,
        workspace="personal",
    )
    entry.last_status = "error"
    store.replace(entry)
    pcm.start_stream = _stream_stub(
        [{"type": "result", "text": "done", "is_error": False}]
    )  # type: ignore[method-assign]

    result = asyncio.run(
        pcm.dispatch_schedule(entry, entry.prompt, "opus", "auto", "claude")
    )

    assert result["status"] == "ok"
    assert store.get(entry.schedule_id).last_status == "ok"


def test_successful_dispatch_does_not_stamp_last_status(tmp_path: Path) -> None:
    import asyncio

    pcm, store = _dispatch_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="Daily brief", model="opus")
    entry = store.create(
        daily_time_utc="08:00",
        prompt="brief",
        model="opus",
        mode="auto",
        chat_id=0,
        frequency="daily",
        web_chat_id=chat.chat_id,
        workspace="personal",
    )
    pcm.start_stream = _stream_stub(
        [{"type": "result", "text": "done", "is_error": False}]
    )  # type: ignore[method-assign]

    result = asyncio.run(
        pcm.dispatch_schedule(entry, entry.prompt, "opus", "auto", "claude")
    )

    assert result["status"] == "ok"
    stored = store.get(entry.schedule_id)
    assert stored is not None
    # A completed run records its own health ("ok"), which is what clears a
    # stale error stamp from an earlier failed run.
    assert stored.last_status == "ok"