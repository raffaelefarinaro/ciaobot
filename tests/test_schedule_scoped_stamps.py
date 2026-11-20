"""Scheduler stamps are scoped writes (#3068, slice 4)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path

from ciao.schedules import ScheduleManager, ScheduleStore

_NOW = datetime(2026, 6, 15, 8, 0, tzinfo=UTC)


def _due(store: ScheduleStore):
    entry = store.create(
        daily_time_utc="08:00", prompt="p", model="sonnet", chat_id=0,
        frequency="daily", timezone_name="UTC", title="t",
    )
    # An old created_at so the slot counts as due rather than before first seen.
    path = store._path
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schedules"][0]["created_at"] = "2026-01-01T00:00:00Z"
    path.write_text(json.dumps(data), encoding="utf-8")
    return entry


def _manager(store: ScheduleStore, on_prepare) -> tuple[ScheduleManager, list[str]]:
    dispatched: list[str] = []

    async def dispatch(entry, model, mode, provider, *, target_chat_id=None):
        dispatched.append(entry.schedule_id)

    manager = ScheduleManager(
        store=store,
        dispatch_to_web=dispatch,
        prepare_chat=lambda entry, prompt, model, mode, provider: on_prepare(entry),
    )
    return manager, dispatched


def _edit(root: Path, schedule_id: str, **changes) -> None:
    other = ScheduleStore(root)
    current = other.get(schedule_id)
    assert current is not None
    base = dataclasses.replace(current)
    for key, value in changes.items():
        setattr(current, key, value)
    other.replace(current, base=base)


async def test_tick_stamp_keeps_an_edit_made_while_the_run_was_prepared(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    entry = _due(store)

    def edit_during_prepare(_entry):
        _edit(tmp_path, entry.schedule_id, title="edited meanwhile", prompt="new prompt")
        return None

    manager, dispatched = _manager(store, edit_during_prepare)
    await manager.tick(now=_NOW)
    await asyncio.sleep(0.05)
    final = store.get(entry.schedule_id)
    assert dispatched == [entry.schedule_id]
    assert final is not None
    assert final.title == "edited meanwhile" and final.prompt == "new prompt"
    assert final.last_status == "running" and final.last_triggered_on == "2026-06-15"


async def test_tick_stamp_does_not_resurrect_a_row_deleted_while_the_run_was_prepared(
    tmp_path: Path,
) -> None:
    store = ScheduleStore(tmp_path)
    entry = _due(store)

    def delete_during_prepare(_entry):
        ScheduleStore(tmp_path).delete(entry.schedule_id)
        return None

    manager, _dispatched = _manager(store, delete_during_prepare)
    await manager.tick(now=_NOW)
    await asyncio.sleep(0.05)
    assert store.get(entry.schedule_id) is None


async def test_run_now_stamp_keeps_a_concurrent_edit(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    entry = _due(store)

    def edit_during_prepare(_entry):
        _edit(tmp_path, entry.schedule_id, title="edited meanwhile")
        return None

    manager, _dispatched = _manager(store, edit_during_prepare)
    await manager.dispatch_now(entry.schedule_id, now=_NOW)
    await asyncio.sleep(0.05)
    final = store.get(entry.schedule_id)
    assert final is not None and final.title == "edited meanwhile" and final.last_status == "running"


async def test_interval_start_refuses_a_row_deleted_before_the_stamp(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    entry = store.create(
        daily_time_utc="", prompt="p", model="", chat_id=0,
        frequency="interval", interval_minutes=5, web_chat_id="chat-1", title="t",
    )
    dispatched: list[str] = []

    async def dispatch(e, model, mode, provider, *, target_chat_id=None):
        dispatched.append(e.schedule_id)

    def prepare(e, prompt, model, mode, provider):
        ScheduleStore(tmp_path).delete(entry.schedule_id)
        return "chat-1"

    manager = ScheduleManager(store=store, dispatch_to_web=dispatch, prepare_chat=prepare)
    result = await manager.dispatch_now(entry.schedule_id, now=_NOW)
    await asyncio.sleep(0.05)
    assert dispatched == []
    assert result["status"] == "refused"
    assert store.get(entry.schedule_id) is None


async def test_stop_cancels_and_joins_tracked_run_tasks(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    started = asyncio.Event()
    finished: list[str] = []

    async def dispatch(entry, model, mode, provider, *, target_chat_id=None):
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            finished.append(entry.schedule_id)

    manager = ScheduleManager(store=store, dispatch_to_web=dispatch)
    entry = store.create(
        daily_time_utc="", prompt="p", model="", chat_id=0,
        frequency="manual", title="t",
    )
    await manager.dispatch_now(entry.schedule_id, now=_NOW)
    await asyncio.wait_for(started.wait(), 2)
    assert await manager.stop() == 0
    assert finished == [entry.schedule_id]


async def test_stop_reports_a_run_that_ignores_cancellation(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def dispatch(entry, model, mode, provider, *, target_chat_id=None):
        started.set()
        while not release.is_set():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                continue

    manager = ScheduleManager(store=store, dispatch_to_web=dispatch)
    entry = store.create(
        daily_time_utc="", prompt="p", model="", chat_id=0,
        frequency="manual", title="t",
    )
    await manager.dispatch_now(entry.schedule_id, now=_NOW)
    await asyncio.wait_for(started.wait(), 2)
    assert await manager.stop(settle_timeout=0.1) == 1
    release.set()
    await asyncio.sleep(0.05)


async def test_work_count_covers_inflight_runs_and_live_tasks(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def dispatch(entry, model, mode, provider, *, target_chat_id=None):
        started.set()
        await release.wait()

    manager = ScheduleManager(store=store, dispatch_to_web=dispatch)
    assert manager.work_count() == 0
    entry = store.create(
        daily_time_utc="", prompt="p", model="", chat_id=0,
        frequency="manual", title="t",
    )
    await manager.dispatch_now(entry.schedule_id, now=_NOW)
    await asyncio.wait_for(started.wait(), 2)
    assert manager.work_count() >= 1
    release.set()
    await asyncio.sleep(0.05)
    assert manager.work_count() == 0


async def test_command_interval_deleted_mid_fire_reports_nothing_started(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    entry = store.create(
        daily_time_utc="", prompt="p", model="", chat_id=0,
        frequency="interval", interval_minutes=5, command="true", title="t",
    )
    dispatched: list[str] = []

    async def dispatch(e, model, mode, provider, *, target_chat_id=None):
        dispatched.append(e.schedule_id)

    original = store.replace

    def delete_then_replace(*args, **kwargs):
        ScheduleStore(tmp_path).delete(entry.schedule_id)
        return original(*args, **kwargs)

    store.replace = delete_then_replace  # type: ignore[method-assign]
    manager = ScheduleManager(store=store, dispatch_to_web=dispatch)
    result = await manager.dispatch_now(entry.schedule_id, now=_NOW)
    await asyncio.sleep(0.05)
    assert dispatched == []
    assert result["status"] == "refused"
    assert store.get(entry.schedule_id) is None
