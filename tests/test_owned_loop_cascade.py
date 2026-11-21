"""A chat-owned loop ends with its owner chat and is never re-homed (#3499).

L1 made a loop live in one chat. These tests pin the other half of that
rule: deleting, archiving or removing the chat (or its project or workspace)
deletes the chat's loops in the same synchronous section, and nothing that
handles a dead target may move a loop to another chat. Ordinary chat-bound
schedules on the same chat are untouched.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.schedules import ScheduleEntry, ScheduleManager, ScheduleStore, ScheduleStoreCorrupt
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.chat_broker import ChatStream
from ciao.web.project_chats import ChatTeardownConflict, ProjectChatManager
from ciao.web.routes_api import _enrich_schedule

from .conftest import attach_stub_mcp
from .test_workspace_archive import _app, _isolated  # noqa: F401 - autouse isolation

T0 = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)


def _make_manager(tmp_path: Path) -> tuple[ProjectChatManager, ScheduleStore]:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={
            "personal": WorkspaceConfig(name="personal", vault_root="memory-vault/personal")
        },
    )
    pcm = attach_stub_mcp(
        ProjectChatManager(
            config,
            state_store=StateStore(config.state_path, tmp_path, config.media_root),
            transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
            path=runtime / "web_projects.json",
        )
    )
    store = ScheduleStore(runtime)
    pcm.schedule_store = store
    return pcm, store


def _loop(store: ScheduleStore, owner: str, prompt: str = "check PRs") -> ScheduleEntry:
    return store.create_owned_loop(
        owner_chat_id=owner, prompt=prompt, interval_minutes=10, now=T0
    )


def _routine(store: ScheduleStore, chat_id: str) -> ScheduleEntry:
    """An ordinary chat-bound schedule (not a loop) on the given chat."""
    return store.create(
        daily_time_utc="08:00", prompt="morning brief", model="",
        chat_id=0, web_chat_id=chat_id, workspace="personal",
    )


def _ids(store: ScheduleStore) -> set[str]:
    return {entry.schedule_id for entry in store.list_entries()}


def _record_events(pcm: ProjectChatManager, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    seen: list[dict] = []
    monkeypatch.setattr(pcm._events, "publish", lambda event: seen.append(event))
    return seen


def _changed(events: list[dict]) -> list[dict]:
    return [event for event in events if event.get("type") == "schedules_changed"]


async def _handoff(pcm: ProjectChatManager, chat_id: str):
    """A background Stop whose provider abort is still running."""
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def finish() -> None:
        entered.set()
        await gate.wait()

    task = pcm._streaming.begin_background_stop(chat_id, finish)
    await asyncio.wait_for(entered.wait(), 5)
    return gate, task


# ── store door ──────────────────────────────────────────────────────────


def test_delete_owned_by_removes_only_that_owners_loops(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    mine = [_loop(store, "chat-a"), _loop(store, "chat-a", "second")]
    other = _loop(store, "chat-b")
    routine = _routine(store, "chat-a")

    removed = store.delete_owned_by("chat-a")

    assert sorted(removed) == sorted(e.schedule_id for e in mine)
    assert _ids(store) == {other.schedule_id, routine.schedule_id}


def test_delete_owned_by_refuses_an_unreadable_store(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    _loop(store, "chat-a")
    (tmp_path / "schedules.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(ScheduleStoreCorrupt):
        store.delete_owned_by("chat-a")


# ── chat delete ─────────────────────────────────────────────────────────


def test_deleting_a_chat_ends_its_loops_and_keeps_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="Watch PRs")
    other = pcm.create_chat(project.project_id, title="Other")
    mine = _loop(store, chat.chat_id)
    theirs = _loop(store, other.chat_id)
    routine = _routine(store, chat.chat_id)

    assert pcm.delete_chat(chat.chat_id) is True

    assert mine.schedule_id not in _ids(store)
    assert _ids(store) == {theirs.schedule_id, routine.schedule_id}


def test_deleting_a_chat_publishes_schedules_changed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="Watch PRs")
    _loop(store, chat.chat_id)
    _loop(store, chat.chat_id, "second")
    events = _record_events(pcm, monkeypatch)

    pcm.delete_chat(chat.chat_id)

    assert len(_changed(events)) == 1


def test_deleting_a_chat_without_loops_publishes_no_schedules_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="Plain")
    _loop(store, "chat-someone-else")
    events = _record_events(pcm, monkeypatch)

    pcm.delete_chat(chat.chat_id)

    assert _changed(events) == []


async def test_a_refused_delete_keeps_the_loops(tmp_path: Path) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="Stopping", provider="claude")
    mine = _loop(store, chat.chat_id)
    gate, task = await _handoff(pcm, chat.chat_id)
    try:
        with pytest.raises(ChatTeardownConflict):
            pcm.delete_chat(chat.chat_id)
        assert pcm.get_chat(chat.chat_id) is not None
        assert mine.schedule_id in _ids(store)
    finally:
        gate.set()
        await asyncio.wait_for(task, 5)


def test_an_unreadable_store_does_not_fail_the_delete(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="Plain")
    (tmp_path / ".runtime" / "schedules.json").write_text("{broken", encoding="utf-8")

    with caplog.at_level(logging.ERROR):
        assert pcm.delete_chat(chat.chat_id) is True

    assert pcm.get_chat(chat.chat_id) is None
    assert "loop" in caplog.text.lower()


# ── chat archive ────────────────────────────────────────────────────────


async def test_archiving_a_chat_ends_its_loops_and_keeps_the_rest(tmp_path: Path) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="Watch PRs")
    other = pcm.create_chat(project.project_id, title="Other")
    mine = _loop(store, chat.chat_id)
    theirs = _loop(store, other.chat_id)
    routine = _routine(store, chat.chat_id)

    await pcm.archive_chat(chat.chat_id)

    assert pcm.get_chat(chat.chat_id).archived is True
    assert mine.schedule_id not in _ids(store)
    assert _ids(store) == {theirs.schedule_id, routine.schedule_id}


async def test_a_refused_archive_keeps_the_loops(tmp_path: Path) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="Stopping", provider="claude")
    mine = _loop(store, chat.chat_id)
    gate, task = await _handoff(pcm, chat.chat_id)
    try:
        with pytest.raises(ChatTeardownConflict):
            await pcm.archive_chat(chat.chat_id)
        assert pcm.get_chat(chat.chat_id).archived is False
        assert mine.schedule_id in _ids(store)
    finally:
        gate.set()
        await asyncio.wait_for(task, 5)


# ── project and workspace removal ───────────────────────────────────────


def test_deleting_a_project_ends_the_loops_of_its_chats(tmp_path: Path) -> None:
    pcm, store = _make_manager(tmp_path)
    doomed = pcm.create_project("Doomed", workspace="personal")
    kept = pcm.create_project("Kept", workspace="personal")
    chat = pcm.create_chat(doomed.project_id, title="Doomed chat")
    survivor = pcm.create_chat(kept.project_id, title="Kept chat")
    mine = _loop(store, chat.chat_id)
    theirs = _loop(store, survivor.chat_id)

    pcm.delete_project(doomed.project_id)

    assert mine.schedule_id not in _ids(store)
    assert theirs.schedule_id in _ids(store)


def test_completing_a_project_ends_the_loops_of_its_chats(tmp_path: Path) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Finished", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="Finished chat")
    mine = _loop(store, chat.chat_id)

    pcm.complete_project(project.project_id)

    assert mine.schedule_id not in _ids(store)


def test_archiving_a_workspace_ends_its_loops_and_archives_no_loop_row(
    tmp_path: Path,
) -> None:
    client, config, pcm, store = _app(tmp_path, rerooted=True)
    pcm.schedule_store = store
    general = next(p for p in pcm.list_projects("work") if p.is_auto)
    chat = pcm.create_chat(general.project_id, title="work loop chat")
    mine = _loop(store, chat.chat_id)
    routine = store.create(
        daily_time_utc="08:00", prompt="standup", model="",
        chat_id=0, workspace="work",
    )

    response = client.post("/api/workspaces/work/archive")

    assert response.status_code == 200, response.json()
    assert response.json()["archived"]["schedules"] == 1
    assert mine.schedule_id not in _ids(store)
    assert routine.schedule_id not in _ids(store)
    folder = Path(config.workspace_root) / ".archived-workspaces"
    metadata_files = list(folder.rglob("archive.json"))
    assert metadata_files, "the workspace archive should carry its metadata"
    archived_ids = {
        s["schedule_id"]
        for path in metadata_files
        for s in json.loads(path.read_text(encoding="utf-8")).get("schedules", [])
    }
    assert mine.schedule_id not in archived_ids

    restored = client.post(
        "/api/workspaces/archived/restore",
        json={"id": response.json()["archived"]["id"]},
    )

    assert restored.status_code == 200, restored.json()
    assert mine.schedule_id not in _ids(store)
    assert not any(e.owner_chat_id for e in store.list_entries())


def test_an_empty_chat_with_a_stray_loop_takes_the_loop_with_it(tmp_path: Path) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="New Chat")
    mine = _loop(store, chat.chat_id)

    assert chat.chat_id in pcm._cleanup_empty_chats()

    assert mine.schedule_id not in _ids(store)


# ── dispatch never re-homes a loop ──────────────────────────────────────


def _no_chat_changes(pcm: ProjectChatManager, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def refuse(name: str):
        def _inner(*_args: Any, **_kwargs: Any) -> None:
            calls.append(name)
            raise AssertionError(f"{name} must not run for a chat-owned loop")

        return _inner

    for name in ("continue_archived_chat", "create_chat", "_rehome_interval_chat"):
        monkeypatch.setattr(pcm, name, refuse(name))
    return calls


def test_a_loop_whose_owner_is_archived_is_never_forked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    owner = pcm.create_chat(project.project_id, title="Owner")
    owner.archived = True
    entry = _loop(store, owner.chat_id)
    calls = _no_chat_changes(pcm, monkeypatch)
    before = len(pcm.list_chats())

    assert pcm.prepare_schedule_chat(entry, entry.prompt, "opus", "auto", "claude") is None

    assert calls == []
    assert len(pcm.list_chats()) == before


def test_a_loop_whose_owner_is_missing_is_never_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    owner = pcm.create_chat(project.project_id, title="Owner")
    entry = _loop(store, owner.chat_id)
    del pcm._chats[owner.chat_id]
    entry.fallback_project_id = project.project_id
    calls = _no_chat_changes(pcm, monkeypatch)
    before = len(pcm.list_chats())

    assert pcm.prepare_schedule_chat(entry, entry.prompt, "opus", "auto", "claude") is None

    assert calls == []
    assert len(pcm.list_chats()) == before
    assert entry.web_chat_id == owner.chat_id


def test_rehoming_refuses_a_loop_outright(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    owner = pcm.create_chat(project.project_id, title="Owner")
    owner.archived = True
    entry = _loop(store, owner.chat_id)
    # A project that resolves: without the guard the loop would be forked or
    # given a fresh chat here, so the refusal is what keeps it on its owner.
    entry.fallback_project_id = project.project_id
    before = len(pcm.list_chats())

    assert pcm._rehome_interval_chat(entry, entry.prompt) is None

    assert len(pcm.list_chats()) == before
    assert entry.web_chat_id == owner.chat_id


def test_a_loop_run_does_not_stamp_its_owner_chat(tmp_path: Path) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    owner = pcm.create_chat(project.project_id, title="Owner")
    entry = _loop(store, owner.chat_id)

    chat_id = pcm.prepare_schedule_chat(entry, entry.prompt, "opus", "auto", "claude")

    assert chat_id == owner.chat_id
    assert owner.schedule_id == ""
    assert owner.schedule_title == ""


def test_an_ordinary_chat_bound_entry_still_stamps_its_chat(tmp_path: Path) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="Briefed")
    entry = _routine(store, chat.chat_id)

    assert pcm.prepare_schedule_chat(entry, entry.prompt, "opus", "auto", "claude") == chat.chat_id
    assert chat.schedule_id == entry.schedule_id


def test_the_owner_check_is_missing_for_archived_and_deleted_owners(tmp_path: Path) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    owner = pcm.create_chat(project.project_id, title="Owner")
    assert pcm.loop_owner_alive(owner.chat_id) is True
    owner.archived = True
    assert pcm.loop_owner_alive(owner.chat_id) is False
    owner.archived = False
    del pcm._chats[owner.chat_id]
    assert pcm.loop_owner_alive(owner.chat_id) is False


def test_the_owner_check_reports_busy_while_the_owner_is_mid_archive(tmp_path: Path) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    owner = pcm.create_chat(project.project_id, title="Owner")
    pcm._archive_locks[owner.chat_id] = asyncio.Lock()
    assert pcm.loop_owner_alive(owner.chat_id) is None


# ── sweep ───────────────────────────────────────────────────────────────


def _sweep_manager(store: ScheduleStore, owner_alive, dispatched: list[str]) -> ScheduleManager:
    async def dispatch_to_web(entry, model, mode, provider, *, target_chat_id=None):
        dispatched.append(entry.schedule_id)
        return {"status": "ok", "chat_id": target_chat_id}

    return ScheduleManager(
        store=store,
        dispatch_to_web=dispatch_to_web,
        prepare_chat=lambda entry, prompt, model, mode, provider: entry.web_chat_id,
        chat_busy=lambda chat_id: False,
        chat_dispatchable=lambda entry: True,
        owner_alive=owner_alive,
    )


async def test_the_sweep_ends_a_loop_whose_owner_is_gone(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    mine = _loop(store, "chat-gone")
    dispatched: list[str] = []
    manager = _sweep_manager(store, lambda chat_id: False, dispatched)

    await manager.tick(now=T0 + timedelta(minutes=11))

    assert mine.schedule_id not in _ids(store)
    assert dispatched == []


async def test_the_sweep_skips_a_loop_whose_owner_is_mid_archive(tmp_path: Path) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    owner = pcm.create_chat(project.project_id, title="Archiving")
    mine = _loop(store, owner.chat_id)
    pcm._archive_locks[owner.chat_id] = asyncio.Lock()
    dispatched: list[str] = []
    manager = _sweep_manager(store, pcm.loop_owner_alive, dispatched)

    await manager.tick(now=T0 + timedelta(minutes=11))

    assert mine.schedule_id in _ids(store)
    assert dispatched == []


async def test_a_refused_archive_leaves_the_loop_for_the_next_sweep(tmp_path: Path) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    owner = pcm.create_chat(project.project_id, title="Stopping", provider="claude")
    mine = _loop(store, owner.chat_id)
    gate, task = await _handoff(pcm, owner.chat_id)
    try:
        with pytest.raises(ChatTeardownConflict):
            await pcm.archive_chat(owner.chat_id)
        assert pcm.loop_owner_alive(owner.chat_id) is True
        dispatched: list[str] = []
        manager = _sweep_manager(store, pcm.loop_owner_alive, dispatched)
        await manager.tick(now=T0 + timedelta(minutes=11))
        assert mine.schedule_id in _ids(store)
    finally:
        gate.set()
        await asyncio.wait_for(task, 5)


async def test_the_sweep_ends_a_loop_once_its_owner_is_archived(tmp_path: Path) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    owner = pcm.create_chat(project.project_id, title="Archived by hand")
    mine = _loop(store, owner.chat_id)
    owner.archived = True
    manager = _sweep_manager(store, pcm.loop_owner_alive, [])

    await manager.tick(now=T0 + timedelta(minutes=11))

    assert mine.schedule_id not in _ids(store)


# ── in-flight run and enrichment ────────────────────────────────────────


async def test_deleting_the_owner_mid_run_resurrects_nothing(tmp_path: Path) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    owner = pcm.create_chat(project.project_id, title="Running")
    mine = _loop(store, owner.chat_id)
    gate = asyncio.Event()
    dispatched: list[str] = []

    async def dispatch_to_web(entry, model, mode, provider, *, target_chat_id=None):
        dispatched.append(entry.schedule_id)
        await gate.wait()
        return {"status": "ok", "chat_id": target_chat_id}

    manager = ScheduleManager(
        store=store,
        dispatch_to_web=dispatch_to_web,
        prepare_chat=lambda entry, prompt, model, mode, provider: entry.web_chat_id,
        chat_busy=lambda chat_id: False,
        chat_dispatchable=lambda entry: True,
        owner_alive=pcm.loop_owner_alive,
    )
    await manager.tick(now=T0 + timedelta(minutes=11))
    await asyncio.sleep(0.05)  # the run is a task: let it reach the gate
    assert dispatched == [mine.schedule_id]
    assert mine.schedule_id in manager._inflight

    assert pcm.delete_chat(owner.chat_id) is True
    gate.set()
    for task in list(manager._run_tasks):
        await asyncio.wait_for(task, 5)

    assert store.get(mine.schedule_id) is None
    assert _ids(store) == set()
    assert manager._inflight == set()


def test_enrichment_reports_an_owned_loop_unavailable_once_its_owner_is_archived(
    tmp_path: Path,
) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    owner = pcm.create_chat(project.project_id, title="Owner")
    entry = _loop(store, owner.chat_id)
    assert _enrich_schedule(entry, pcm)["context_available"] is True
    owner.archived = True

    assert _enrich_schedule(entry, pcm)["context_available"] is False


# ── review follow-ups: store failures, archive window, prune, carry-over, reset ──


def _broken_store(store: ScheduleStore, monkeypatch: pytest.MonkeyPatch) -> None:
    def disk_full(owner_chat_id: str) -> list[str]:
        raise OSError("disk full")

    monkeypatch.setattr(store, "delete_owned_by", disk_full)


def test_a_disk_error_does_not_abort_a_chat_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="Plain")
    _broken_store(store, monkeypatch)

    assert pcm.delete_chat(chat.chat_id) is True

    assert pcm.get_chat(chat.chat_id) is None


def test_a_disk_error_does_not_abort_the_empty_chat_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="New Chat")
    _broken_store(store, monkeypatch)

    assert chat.chat_id in pcm._cleanup_empty_chats()

    assert pcm.get_chat(chat.chat_id) is None


def test_a_disk_error_does_not_abort_the_archived_transcript_prune(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="Gone")
    chat.archived = True
    chat.archive_path = "memory-vault/personal/Logs/Chats/gone/claude/none.md"
    for root in pcm._config.chats_roots():
        root.mkdir(parents=True, exist_ok=True)
    _broken_store(store, monkeypatch)

    pcm._discover_archived_chats()

    assert pcm.get_chat(chat.chat_id) is None


async def test_a_cancelled_archive_after_the_provider_step_keeps_the_loops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="Cancelled")
    mine = _loop(store, chat.chat_id)

    async def cancelled(*_args: Any, **_kwargs: Any) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(pcm, "_disconnect_provider", cancelled)

    with pytest.raises(asyncio.CancelledError):
        await pcm.archive_chat(chat.chat_id)

    assert chat.archived is False
    assert mine.schedule_id in _ids(store)


async def test_a_cancelled_save_during_archive_keeps_the_loops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="Cancelled save")
    mine = _loop(store, chat.chat_id)
    real_save = pcm._save

    def save_then_cancel(*args: Any, **kwargs: Any) -> None:
        if chat.archived:
            raise asyncio.CancelledError
        real_save(*args, **kwargs)

    monkeypatch.setattr(pcm, "_save", save_then_cancel)

    with pytest.raises(asyncio.CancelledError):
        await pcm.archive_chat(chat.chat_id)

    assert mine.schedule_id in _ids(store)


async def test_a_completed_archive_drops_the_loops_only_after_it_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="Completed")
    mine = _loop(store, chat.chat_id)
    seen_saved_archived: list[bool] = []
    real_save = pcm._save

    def record_save(*args: Any, **kwargs: Any) -> None:
        real_save(*args, **kwargs)
        if chat.archived:
            seen_saved_archived.append(mine.schedule_id in _ids(store))

    monkeypatch.setattr(pcm, "_save", record_save)

    await pcm.archive_chat(chat.chat_id)

    assert mine.schedule_id not in _ids(store)
    # The loop was still present at the save that recorded the archive.
    assert seen_saved_archived == [True]


def test_the_archived_transcript_prune_ends_the_loop_of_a_chat_whose_transcript_is_gone(
    tmp_path: Path,
) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="Pruned")
    mine = _loop(store, chat.chat_id)
    other = _loop(store, "chat-kept")
    chat.archived = True
    chat.archive_path = "memory-vault/personal/Logs/Chats/gone/claude/none.md"
    for root in pcm._config.chats_roots():
        root.mkdir(parents=True, exist_ok=True)

    pcm._discover_archived_chats()

    assert pcm.get_chat(chat.chat_id) is None
    assert mine.schedule_id not in _ids(store)
    assert other.schedule_id in _ids(store)


async def test_a_dispatch_that_repoints_a_loop_leaves_the_stored_row_on_its_owner(
    tmp_path: Path,
) -> None:
    store = ScheduleStore(tmp_path)
    mine = _loop(store, "chat-owner")

    async def repoint(entry, model, mode, provider, *, target_chat_id=None):
        entry.web_chat_id = "chat-replacement"
        return {"status": "ok"}

    manager = ScheduleManager(
        store=store,
        dispatch_to_web=repoint,
        prepare_chat=lambda entry, prompt, model, mode, provider: entry.web_chat_id,
        chat_busy=lambda chat_id: False,
        chat_dispatchable=lambda entry: True,
        owner_alive=lambda chat_id: True,
    )
    await manager.tick(now=T0 + timedelta(minutes=11))
    for task in list(manager._run_tasks):
        await asyncio.wait_for(task, 5)

    stored = store.get(mine.schedule_id)
    assert stored is not None
    assert stored.web_chat_id == "chat-owner"


def test_a_chat_reset_keeps_its_loop_running_into_the_fresh_session(
    tmp_path: Path,
) -> None:
    pcm, store = _make_manager(tmp_path)
    project = pcm.create_project("Holder", workspace="personal")
    owner = pcm.create_chat(project.project_id, title="Reset me", provider="claude")
    mine = _loop(store, owner.chat_id)

    assert pcm.new_session(owner.chat_id) is not None

    assert pcm.get_chat(owner.chat_id) is not None
    assert pcm.loop_owner_alive(owner.chat_id) is True
    assert mine.schedule_id in _ids(store)
    entry = store.get(mine.schedule_id)
    assert entry is not None
    assert pcm.prepare_schedule_chat(entry, entry.prompt, "opus", "auto", "claude") == owner.chat_id
