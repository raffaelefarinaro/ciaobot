"""Tests for moving chats between projects via update_chat,
plus event-broadcast coverage for project CRUD."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.schedules import ScheduleStore
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ArchiveOutcome, ProjectChatManager


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


async def _release_after(
    blockers: list[threading.Event], delay: float
) -> None:
    """Release saturated pool workers after *delay* seconds.

    Used by the #942 regression test so the old fixed-budget poll would expire
    while the pool is still saturated, independently of the waiter under test.
    """
    await asyncio.sleep(delay)
    for event in blockers:
        event.set()


class _EventCapture:
    """Test helper: attaches an EventsHub subscription so synchronous
    publishes land directly in its queue for assertion."""

    def __init__(self, pcm: ProjectChatManager) -> None:
        self._pcm = pcm
        self._subscription = pcm._events.attach()
        self.queue = self._subscription._queue

    def drain(self) -> list[dict]:
        out: list[dict] = []
        while True:
            try:
                out.append(self.queue.get_nowait())
            except asyncio.QueueEmpty:
                return out

    def close(self) -> None:
        self._pcm._events._subs.discard(self.queue)


def test_move_chat_happy_path(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    src = pcm.create_project("2026-q2-source", workspace="work")
    dst = pcm.create_project("2026-q2-dest", workspace="work")
    chat = pcm.create_chat(src.project_id, title="movable")

    cap = _EventCapture(pcm)
    moved = pcm.update_chat(chat.chat_id, project_id=dst.project_id)
    assert moved is not None
    assert moved.project_id == dst.project_id

    events = cap.drain()
    move_events = [e for e in events if e.get("type") == "chat_moved"]
    assert len(move_events) == 1
    assert move_events[0]["chat_id"] == chat.chat_id
    assert move_events[0]["project_id"] == dst.project_id
    assert move_events[0]["old_project_id"] == src.project_id


def test_move_chat_same_project_is_noop(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    src = pcm.create_project("2026-q2-source", workspace="work")
    chat = pcm.create_chat(src.project_id, title="stationary")

    cap = _EventCapture(pcm)
    result = pcm.update_chat(chat.chat_id, project_id=src.project_id)
    assert result is not None
    assert result.project_id == src.project_id

    events = cap.drain()
    assert not [e for e in events if e.get("type") == "chat_moved"]


def test_move_chat_rejects_cross_workspace(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    src = pcm.create_project("2026-q2-work", workspace="work")
    dst = pcm.create_project("2026-q2-personal", workspace="personal")
    chat = pcm.create_chat(src.project_id, title="cross-ws")

    with pytest.raises(ValueError, match="workspace"):
        pcm.update_chat(chat.chat_id, project_id=dst.project_id)

    # Chat should remain in the original project.
    assert pcm.get_chat(chat.chat_id).project_id == src.project_id


def test_move_chat_rejects_unknown_project(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    src = pcm.create_project("2026-q2-source", workspace="work")
    chat = pcm.create_chat(src.project_id, title="orphaning")

    with pytest.raises(ValueError, match="not found"):
        pcm.update_chat(chat.chat_id, project_id="proj-doesnotexist")

    assert pcm.get_chat(chat.chat_id).project_id == src.project_id


def test_move_chat_rejects_archived(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    src = pcm.create_project("2026-q2-source", workspace="work")
    dst = pcm.create_project("2026-q2-dest", workspace="work")
    chat = pcm.create_chat(src.project_id, title="archived")
    # Mark archived directly to avoid the full archive_chat side effects.
    pcm._chats[chat.chat_id].archived = True

    with pytest.raises(ValueError, match="archived"):
        pcm.update_chat(chat.chat_id, project_id=dst.project_id)


def test_update_chat_other_fields_still_work(tmp_path: Path) -> None:
    """Adding project_id support must not regress title/model/mode updates."""
    pcm = _make_manager(tmp_path)
    src = pcm.create_project("2026-q2-source", workspace="work")
    chat = pcm.create_chat(src.project_id, title="orig")

    updated = pcm.update_chat(
        chat.chat_id, title="renamed", model="opus", mode="auto"
    )
    assert updated is not None
    assert updated.title == "renamed"
    assert updated.model == "opus"
    assert updated.mode == "auto"
    # project_id unchanged when not provided.
    assert updated.project_id == src.project_id


def test_create_project_publishes_event(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    cap = _EventCapture(pcm)

    p = pcm.create_project("2026-q2-broadcast", workspace="work")
    events = cap.drain()
    created = [e for e in events if e.get("type") == "project_created"]
    assert len(created) == 1
    assert created[0]["project"]["project_id"] == p.project_id
    assert created[0]["project"]["name"] == "2026-q2-broadcast"


def test_update_project_publishes_event(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    p = pcm.create_project("2026-q2-renameable", workspace="work")
    cap = _EventCapture(pcm)

    pcm.update_project(p.project_id, name="2026-q2-renamed")
    events = cap.drain()
    updated = [e for e in events if e.get("type") == "project_updated"]
    assert len(updated) == 1
    assert updated[0]["project"]["name"] == "2026-q2-renamed"


def test_delete_project_publishes_event(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    p = pcm.create_project("2026-q2-doomed", workspace="work")
    cap = _EventCapture(pcm)

    pcm.delete_project(p.project_id)
    events = cap.drain()
    deleted = [e for e in events if e.get("type") == "project_deleted"]
    assert len(deleted) == 1
    assert deleted[0]["project_id"] == p.project_id


def test_delete_project_rejects_vault_backed(tmp_path: Path) -> None:
    """Deleting a vault-backed project must fail: otherwise auto-discovery
    re-creates the project on the next list_projects() call. The user must
    use complete_project (which moves the vault entry) or remove the vault
    entry directly."""
    parent = tmp_path / "memory-vault" / "personal" / "projects" / "active"
    folder = parent / "Stuck"
    folder.mkdir(parents=True)
    (folder / "Stuck.md").write_text(
        "---\nname: Stuck\nstatus: active\n---\n# Stuck\n",
        encoding="utf-8",
    )

    pcm = _make_manager(tmp_path)
    pcm.list_projects()  # triggers auto-discovery
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "Stuck")

    with pytest.raises(ValueError, match="vault entry"):
        pcm.delete_project(proj.project_id)

    # Project must remain in state — the guard fires before any mutation.
    assert proj.project_id in pcm._projects
    # Vault folder must remain untouched.
    assert (folder / "Stuck.md").exists()


def test_delete_project_allows_manual_project_without_vault_folder(tmp_path: Path) -> None:
    """Manually-created projects (no vault_folder) can be deleted normally."""
    pcm = _make_manager(tmp_path)
    p = pcm.create_project("Manual", workspace="personal")
    assert p.vault_folder == ""

    ok = pcm.delete_project(p.project_id)
    assert ok is True
    assert p.project_id not in pcm._projects


# ── Empty-chat cleanup ──────────────────────────────────────────────────


def test_create_chat_preserves_prior_empty_chat(tmp_path: Path) -> None:
    """Creating a second chat does NOT drop the first, even if it is empty.

    The automatic empty-chat sweep was removed: it raced the just-created
    chat on every new-chat POST (deleting the chat the user had just opened),
    which closed the panel and caused the "new chat flashes then opens" bug.
    Empty chats now live until the user deletes them explicitly.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-sweep", workspace="work")

    empty = pcm.create_chat(project.project_id)  # default title, no turns
    assert empty.chat_id in pcm._chats

    cap = _EventCapture(pcm)
    fresh = pcm.create_chat(project.project_id)

    assert empty.chat_id in pcm._chats, "empty chat must NOT be swept"
    assert fresh.chat_id in pcm._chats

    deleted = [e for e in cap.drain() if e.get("type") == "chat_deleted"]
    assert len(deleted) == 0, "no chat_deleted event expected"


def test_create_chat_preserves_non_empty_chats(tmp_path: Path) -> None:
    """Chats that have user turns or a session are kept when a new one opens."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-keep", workspace="work")

    used = pcm.create_chat(project.project_id)
    pcm._chats[used.chat_id].user_turn_count = 1  # simulate a sent message

    renamed = pcm.create_chat(project.project_id)
    renamed.title = "Planning next quarter"
    pcm._chats[renamed.chat_id].title = "Planning next quarter"

    pcm.create_chat(project.project_id)  # triggers sweep

    assert used.chat_id in pcm._chats
    assert renamed.chat_id in pcm._chats


def test_startup_preserves_empty_chats(tmp_path: Path) -> None:
    """An empty chat saved to disk survives a manager restart.

    The automatic empty-chat sweep was removed (it raced new-chat creation
    and closed the panel). Empty chats are cleaned up by hand instead.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-startup", workspace="work")
    orphan = pcm.create_chat(project.project_id)
    assert orphan.chat_id in pcm._chats

    # Simulate restart by building a fresh manager against the same state dir.
    pcm2 = _make_manager(tmp_path)
    assert orphan.chat_id in pcm2._chats


def test_delete_chat_publishes_event(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-delete", workspace="work")
    chat = pcm.create_chat(project.project_id)
    pcm._chats[chat.chat_id].user_turn_count = 1  # keep it out of the sweep

    cap = _EventCapture(pcm)
    assert pcm.delete_chat(chat.chat_id) is True

    deleted = [e for e in cap.drain() if e.get("type") == "chat_deleted"]
    assert len(deleted) == 1
    assert deleted[0]["chat_id"] == chat.chat_id
    assert deleted[0]["reason"] == "user"


async def test_archive_chat_publishes_event(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-archive", workspace="work")
    chat = pcm.create_chat(project.project_id)

    cap = _EventCapture(pcm)
    await pcm.archive_chat(chat.chat_id)

    archived = [e for e in cap.drain() if e.get("type") == "chat_archived"]
    assert len(archived) == 1
    assert archived[0]["chat_id"] == chat.chat_id
    assert archived[0]["project_id"] == project.project_id


async def test_archive_and_delete_tell_chat_ended_subscribers(tmp_path: Path) -> None:
    """The delegation service learns a chat can no longer go on (#1064)."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-ended", workspace="work")
    archived = pcm.create_chat(project.project_id)
    deleted = pcm.create_chat(project.project_id)
    pcm._chats[deleted.chat_id].user_turn_count = 1
    seen: list[tuple[str, str, str]] = []

    def boom(_chat_id: str, _chat: object, _how: str) -> None:
        raise RuntimeError("a subscriber cannot fail the archive")

    pcm.on_chat_ended(boom)
    pcm.on_chat_ended(lambda chat_id, chat, how: seen.append((chat_id, chat.chat_id, how)))
    await pcm.archive_chat(archived.chat_id)
    assert pcm.delete_chat(deleted.chat_id) is True
    assert seen == [
        (archived.chat_id, archived.chat_id, "archived"),
        (deleted.chat_id, deleted.chat_id, "deleted"),
    ]


async def test_archiving_a_run_chat_clears_its_needs_you_flag(tmp_path: Path) -> None:
    """A "skipped" run points the operator at its chat; archiving it answers that.

    Left up, the Automations page kept saying "last run needs you — check the
    chat" about a chat that was archived, until the next run (a week later for
    a weekly entry). Only the entry whose last run was this chat is touched.
    """
    pcm = _make_manager(tmp_path)
    store = ScheduleStore(tmp_path / ".runtime")
    pcm.schedule_store = store
    project = pcm.create_project("2026-q3-sched", workspace="work")
    chat = pcm.create_chat(project.project_id)
    other = pcm.create_chat(project.project_id)

    def _entry(run_chat: str) -> str:
        entry = store.create(
            daily_time_utc="06:00", prompt="Brief.", model="", mode="auto", chat_id=0,
            web_project_id=project.project_id, workspace="work",
        )
        entry.last_run_chat_id = run_chat
        entry.last_status = "skipped"
        store.replace(entry)
        return entry.schedule_id

    archived_run = _entry(chat.chat_id)
    other_run = _entry(other.chat_id)

    cap = _EventCapture(pcm)
    await pcm.archive_chat(chat.chat_id)

    assert store.get(archived_run).last_status == "ok"
    assert store.get(other_run).last_status == "skipped"
    assert any(e.get("type") == "schedules_changed" for e in cap.drain())


async def test_archive_route_returns_the_postprocess_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The response carries the pipeline's opening state, not just a bare ok.

    The client that archived clears its pane as soon as this resolves, so a
    `chat_postprocess` event emitted a moment later would be missed and the
    chat would settle showing nothing about what Ciaobot took from it.
    """
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("2026-q2-archive-report", workspace="work")
    chat = pcm.create_chat(project.project_id, title="chat")

    # An empty chat archives to None, which would skip post-processing
    # entirely; stub the transcript write so it yields a real outcome.
    def fake_archive_session(*, ctx: object, **_kwargs: object) -> Path:
        return tmp_path / f"{ctx.key}.md"  # type: ignore[attr-defined]

    monkeypatch.setattr(pcm._transcripts, "archive_session", fake_archive_session)

    def fake_postprocess(chat_id: str, *_args: object, **_kwargs: object) -> None:
        target = pcm.get_chat(chat_id)
        assert target is not None
        target.postprocess = {
            "state": "running",
            "step": "insights",
            "expected": ["insights"],
        }

    monkeypatch.setattr(pcm, "run_archive_postprocess", fake_postprocess)

    from starlette.requests import Request

    from ciao.web.routes_api import chat_archive

    app = SimpleNamespace(state=SimpleNamespace(project_chat_manager=pcm))
    request = Request({
        "type": "http",
        "method": "POST",
        "path": f"/api/chats/{chat.chat_id}/archive",
        "headers": [],
        "path_params": {"chat_id": chat.chat_id},
        "app": app,
    })
    response = await chat_archive(request)
    payload = json.loads(response.body)

    assert payload["ok"] is True
    assert payload["archived_to"] is not None
    assert payload["postprocess"]["state"] == "running"
    assert payload["postprocess"]["step"] == "insights"
    assert pcm.get_chat(chat.chat_id).archived is True


async def test_delete_and_archive_reclaim_opencode_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("provider-reclaim", workspace="work")
    deleted: list[tuple[str, str]] = []

    async def _fake_opencode_delete(_workspace, session_id: str) -> bool:
        deleted.append(("opencode", session_id))
        return True

    monkeypatch.setattr(
        "ciao.web.project_chats.OpencodeProvider.delete_thread",
        _fake_opencode_delete,
    )
    opencode = pcm.create_chat(project.project_id, title="opencode-to-delete")
    opencode.provider = "opencode"
    opencode.session_id = "opencode-session"
    opencode.user_turn_count = 1
    assert pcm.delete_chat(opencode.chat_id) is True
    await asyncio.sleep(0)
    assert deleted[-1] == ("opencode", "opencode-session")

    opencode_archived = pcm.create_chat(
        project.project_id, title="opencode-to-archive"
    )
    opencode_archived.provider = "opencode"
    opencode_archived.session_id = "opencode-archive-session"
    await pcm.archive_chat(opencode_archived.chat_id)
    assert deleted[-1] == ("opencode", "opencode-archive-session")


@pytest.mark.asyncio
async def test_archive_postprocess_indexes_under_the_shared_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR #467 review: archive indexing must share the control-plane writer lock.

    The control plane now runs index passes in bounded workers, so this
    formerly loop-serialized archive writer can overlap them. It must take the
    same per-database ``keyed_lock`` (and, since ``run_archive_postprocess`` is
    called from async callers, run off the loop).
    """
    from ciao import async_reads, fts_search

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("archive-index", workspace="work")
    chat = pcm.create_chat(project.project_id, title="archive index chat")

    locks: list[str] = []
    indexed: list[Path] = []
    real_keyed_lock = async_reads.keyed_lock

    def _recording_keyed_lock(key: str):
        locks.append(key)
        return real_keyed_lock(key)

    def _recording_index_file(conn, vault_root, file_path, *, path_base=None):
        indexed.append(Path(file_path))
        return True

    monkeypatch.setattr(async_reads, "keyed_lock", _recording_keyed_lock)
    monkeypatch.setattr(fts_search, "index_file", _recording_index_file)

    archive_path = tmp_path / "archive.md"
    archive_path.write_text("# chat\n\nfindme archive body\n", encoding="utf-8")
    pcm.run_archive_postprocess(
        chat.chat_id,
        ArchiveOutcome(
            path=archive_path,
            turn_count=1,
        ),
        chat,
        project,
    )
    # The index write is dispatched as a tracked background task. Wait on that
    # task itself (not a fixed sleep budget): under a loaded runner the read can
    # be admitted late, and the old 500 ms poll expired before it ran (#942).
    pending = [
        t
        for t in pcm._detached_tasks
        if t.get_name() == f"archive-index-{chat.chat_id}"
    ]
    assert pending, "postprocess did not schedule the index task"
    await asyncio.gather(*pending)

    assert indexed == [archive_path], "archive indexing did not run"
    assert locks, "archive indexing did not take the shared FTS write lock"
    assert locks[0] == f"fts-index:{fts_search.get_db_path()}"


@pytest.mark.asyncio
async def test_archive_index_waits_for_the_detached_task_under_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#942: the wait is on the detached task, not a fixed time budget.

    Reproduces the Windows CI flake on a fast machine: saturate the shared,
    bounded vault-read executor so the archive index read cannot be admitted
    within the old 500 ms poll, then run the real postprocess body. The fixed
    budget would assert while indexing is merely late; awaiting the tracked
    ``archive-index-<chat>`` task completes once the pool drains.
    """
    from ciao import async_reads, fts_search

    async_reads.reset_vault_read_executor()
    blockers: list[threading.Event] = []
    submitted: list[concurrent.futures.Future[None]] = []
    release_task: asyncio.Task[None] | None = None
    try:
        pcm = _make_manager(tmp_path)
        project = pcm.create_project("archive-index-load", workspace="work")
        chat = pcm.create_chat(project.project_id, title="archive index load chat")

        locks: list[str] = []
        indexed: list[Path] = []
        real_keyed_lock = async_reads.keyed_lock

        def _recording_keyed_lock(key: str):
            locks.append(key)
            return real_keyed_lock(key)

        def _recording_index_file(conn, vault_root, file_path, *, path_base=None):
            indexed.append(Path(file_path))
            return True

        monkeypatch.setattr(async_reads, "keyed_lock", _recording_keyed_lock)
        monkeypatch.setattr(fts_search, "index_file", _recording_index_file)

        executor = async_reads.vault_read_executor()
        for i in range(executor.max_backlog):
            event = threading.Event()
            submitted.append(
                executor.submit(
                    f"block-{i}",
                    lambda e=event: e.wait(),
                    coalesce=False,
                )
            )
            blockers.append(event)

        archive_path = tmp_path / "archive.md"
        archive_path.write_text("# chat\n\nfindme archive body\n", encoding="utf-8")
        pcm.run_archive_postprocess(
            chat.chat_id,
            ArchiveOutcome(
                path=archive_path,
                turn_count=1,
            ),
            chat,
            project,
        )
        pending = [
            t
            for t in pcm._detached_tasks
            if t.get_name() == f"archive-index-{chat.chat_id}"
        ]
        assert pending, "postprocess did not schedule the index task"

        # Keep the pool saturated for longer than the old 500 ms budget.
        # Releasing from a background coroutine (not before the wait) means a
        # regression to the old 50 x 10 ms poll asserts on an empty ``indexed``
        # here instead of passing by accident.
        release_task = asyncio.ensure_future(_release_after(blockers, 0.75))

        # The pool is saturated, so the index read cannot have run yet.
        await asyncio.sleep(0.05)
        assert indexed == [], "indexed before the saturated pool was released"

        # The wait is on the tracked index task, never the clock.
        await asyncio.gather(*pending)

        assert indexed == [archive_path], "archive indexing did not run"
        assert locks, "archive indexing did not take the shared FTS write lock"
        assert locks[0] == f"fts-index:{fts_search.get_db_path()}"
    finally:
        if release_task is not None:
            if not release_task.done():
                release_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await release_task
        # Always release every worker before tearing down, then join them:
        # ``reset_vault_read_executor`` only cancels queued jobs and waits up
        # to two seconds, so a still-running blocker would survive it.
        for event in blockers:
            event.set()
        for future in submitted:
            with contextlib.suppress(Exception):
                future.result()
        async_reads.reset_vault_read_executor()


def test_synchronous_archive_indexing_is_best_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR #467 review: a failed FTS update must not fail the archive.

    A synchronous caller (CLI/test with no running loop) takes the inline
    branch. Previously the indexing error propagated after the archive had
    already succeeded; it must be swallowed and logged like the async branch.
    """
    from ciao import fts_search

    pcm = _make_manager(tmp_path)
    project = pcm.create_project("best-effort", workspace="work")
    chat = pcm.create_chat(project.project_id, title="best effort chat")

    def _explode(*_args: object, **_kwargs: object) -> bool:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(fts_search, "index_file", _explode)

    archive_path = tmp_path / "archive.md"
    archive_path.write_text("# chat\n\nbody\n", encoding="utf-8")

    # No running loop here: this forces the synchronous inline branch, which
    # must not raise even though the optional index write fails.
    pcm.run_archive_postprocess(
        chat.chat_id,
        ArchiveOutcome(
            path=archive_path,
            turn_count=1,
        ),
        chat,
        project,
    )
