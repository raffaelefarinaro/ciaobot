"""Tests for the `interval` schedule cadence (formerly in-chat loops).

Loops merged into schedules as one more frequency. These cover the properties
that were specific to that primitive and had to survive the merge: cadence
measured from the last dispatch, skip-not-queue against a busy chat,
self-disabling when the target is unrecoverable, exclusion from the catch-up
pass, and inheritance of the target chat's model/mode.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ciao.web.project_chats import ProjectChatManager
from ciao.schedules import (
    INTERVAL_FREQUENCY,
    MIN_INTERVAL_MINUTES,
    ScheduleEntry,
    ScheduleManager,
    ScheduleStore,
    compute_last_expected_run,
    compute_next_run,
    interval_delta,
    is_interval,
    migrate_loops,
    normalize_interval_minutes,
)


@pytest.fixture
def store(tmp_path: Path) -> ScheduleStore:
    return ScheduleStore(tmp_path)


def _create(store: ScheduleStore, **overrides) -> ScheduleEntry:
    params: dict = {
        "daily_time_utc": "",
        "prompt": "check PRs",
        "model": "",
        "mode": "auto",
        "chat_id": 0,
        "frequency": INTERVAL_FREQUENCY,
        "interval_minutes": 10,
        "web_chat_id": "chat-x",
    }
    params.update(overrides)
    return store.create(**params)


def _make_manager(
    store: ScheduleStore,
    *,
    busy: bool = False,
    dispatchable: bool = True,
    status: str = "ok",
):
    dispatched: list[str] = []

    async def dispatch_to_web(entry, model, mode, provider, *, target_chat_id=None):
        dispatched.append(entry.schedule_id)
        return {"status": status, "chat_id": target_chat_id}

    manager = ScheduleManager(
        store=store,
        dispatch_to_web=dispatch_to_web,
        # Mirrors prepare_schedule_chat: the bound chat, or a fresh one for a
        # project-bound entry.
        prepare_chat=lambda entry, prompt, model, mode, provider: (
            entry.web_chat_id or f"chat-new-for-{entry.web_project_id}"
        ),
        chat_busy=lambda chat_id: busy,
        chat_dispatchable=lambda entry: dispatchable,
    )
    return manager, dispatched


async def _settle() -> None:
    """Let the fire-and-forget run task finish."""
    await asyncio.sleep(0.05)


# ── Field handling ───────────────────────────────────────────────────────


def test_create_round_trip(store: ScheduleStore) -> None:
    entry = _create(store, title="PR watcher")
    reloaded = store.get(entry.schedule_id)
    assert reloaded is not None
    assert is_interval(reloaded)
    assert reloaded.interval_minutes == 10
    assert reloaded.title == "PR watcher"
    assert reloaded.enabled is True
    assert reloaded.last_dispatched_at == ""
    assert reloaded.last_status == ""


def test_interval_minutes_rejects_values_below_the_floor() -> None:
    assert normalize_interval_minutes("7") == 7
    for bad in (0, -1, "x", None):
        with pytest.raises(ValueError):
            normalize_interval_minutes(bad)


def test_stored_zero_is_floored_not_treated_as_no_wait(store: ScheduleStore) -> None:
    """A hand-edited or legacy 0 must not become a per-tick hot loop."""
    entry = _create(store)
    entry.interval_minutes = 0
    store.replace(entry)
    assert interval_delta(store.get(entry.schedule_id)) == timedelta(
        minutes=MIN_INTERVAL_MINUTES
    )


def test_interval_entries_sort_after_timed_ones(store: ScheduleStore) -> None:
    """An empty daily_time_utc must not float interval rows to the top."""
    _create(store, interval_minutes=30)
    store.create(
        daily_time_utc="09:00", prompt="p", model="", mode="auto", chat_id=0,
        frequency="daily",
    )
    frequencies = [entry.frequency for entry in store.list_entries()]
    assert frequencies == ["daily", INTERVAL_FREQUENCY]


# ── Next run / missed detection ──────────────────────────────────────────


def test_next_run_is_now_before_the_first_dispatch(store: ScheduleStore) -> None:
    entry = _create(store)
    now = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)
    assert compute_next_run(entry, now=now) == now.astimezone(
        compute_next_run(entry, now=now).tzinfo
    )


def test_next_run_counts_from_the_last_dispatch(store: ScheduleStore) -> None:
    entry = _create(store, interval_minutes=30)
    entry.last_dispatched_at = "2026-07-11T12:00:00+00:00"
    assert compute_next_run(entry) == datetime(
        2026, 7, 11, 12, 30, tzinfo=UTC
    ).astimezone(compute_next_run(entry).tzinfo)


def test_next_run_reads_a_naive_local_stamp(store: ScheduleStore) -> None:
    """The wall-clock tick path writes naive local time; interval math must
    localize it rather than treat it as UTC and fire an hour early or late."""
    entry = _create(store, interval_minutes=30, timezone_name="Europe/Zurich")
    entry.last_dispatched_at = "2026-07-11T12:00:00"  # 12:00 Zurich = 10:00 UTC
    assert compute_next_run(entry).astimezone(UTC) == datetime(
        2026, 7, 11, 10, 30, tzinfo=UTC
    )


def test_disabled_interval_has_no_next_run(store: ScheduleStore) -> None:
    entry = _create(store)
    entry.enabled = False
    assert compute_next_run(entry) is None


def test_interval_never_reports_a_missed_run(store: ScheduleStore) -> None:
    """Relative cadence has no expected slot, so every skipped tick would
    otherwise read as missed — and be replayed by catch_up."""
    entry = _create(store)
    entry.last_dispatched_at = "2020-01-01T00:00:00+00:00"
    assert compute_last_expected_run(entry) is None


# ── Ticking ──────────────────────────────────────────────────────────────


async def test_tick_respects_the_interval(store: ScheduleStore) -> None:
    entry = _create(store, interval_minutes=10)
    manager, dispatched = _make_manager(store)

    t0 = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)
    await manager.tick(now=t0)  # never ran -> fires immediately
    await _settle()
    assert dispatched == [entry.schedule_id]

    await manager.tick(now=t0 + timedelta(minutes=5))  # not due yet
    await _settle()
    assert dispatched == [entry.schedule_id]

    await manager.tick(now=t0 + timedelta(minutes=10))  # due again
    await _settle()
    assert dispatched == [entry.schedule_id, entry.schedule_id]

    latest = store.get(entry.schedule_id)
    assert latest.last_status == "ok"
    assert latest.last_dispatched_at == (
        t0 + timedelta(minutes=10)
    ).isoformat(timespec="seconds")


async def test_disabled_interval_never_fires(store: ScheduleStore) -> None:
    entry = _create(store)
    entry.enabled = False
    store.replace(entry)
    manager, dispatched = _make_manager(store)
    await manager.tick()
    await _settle()
    assert dispatched == []


async def test_busy_chat_skips_without_stamping(store: ScheduleStore) -> None:
    entry = _create(store)
    manager, dispatched = _make_manager(store, busy=True)
    await manager.tick(now=datetime(2026, 7, 11, 12, 0, tzinfo=UTC))
    await _settle()
    assert dispatched == []
    latest = store.get(entry.schedule_id)
    assert latest.last_status == "busy"
    # last_dispatched_at untouched: still due, so it fires the moment the chat
    # frees up rather than waiting out another full interval.
    assert latest.last_dispatched_at == ""


async def test_fires_as_soon_as_the_chat_frees_up(store: ScheduleStore) -> None:
    entry = _create(store)
    busy = {"value": True}
    dispatched: list[str] = []

    async def dispatch_to_web(e, model, mode, provider, *, target_chat_id=None):
        dispatched.append(e.schedule_id)
        return {"status": "ok"}

    manager = ScheduleManager(
        store=store,
        dispatch_to_web=dispatch_to_web,
        prepare_chat=lambda e, prompt, model, mode, provider: e.web_chat_id,
        chat_busy=lambda chat_id: busy["value"],
    )
    t0 = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)
    await manager.tick(now=t0)
    await _settle()
    assert dispatched == []

    busy["value"] = False
    await manager.tick(now=t0 + timedelta(seconds=20))
    await _settle()
    assert dispatched == [entry.schedule_id]


async def test_project_bound_interval_ignores_chat_busy(store: ScheduleStore) -> None:
    """A new chat per run has nothing to collide with, so busy must not gate it."""
    entry = _create(store, web_chat_id=None, web_project_id="proj-1")
    manager, dispatched = _make_manager(store, busy=True)
    await manager.tick()
    await _settle()
    assert dispatched == [entry.schedule_id]


async def test_unrecoverable_target_disables_the_entry(store: ScheduleStore) -> None:
    entry = _create(store, web_chat_id="chat-gone")
    manager, dispatched = _make_manager(store, dispatchable=False)
    await manager.tick()
    await _settle()
    assert dispatched == []
    latest = store.get(entry.schedule_id)
    assert latest.enabled is False
    assert latest.last_status == "missing-chat"


async def test_prepare_chat_returning_none_disables_the_entry(
    store: ScheduleStore,
) -> None:
    entry = _create(store)
    manager = ScheduleManager(
        store=store,
        dispatch_to_web=None,
        prepare_chat=lambda *args: None,
    )
    await manager.tick()
    await _settle()
    latest = store.get(entry.schedule_id)
    assert latest.enabled is False
    assert latest.last_status == "missing-chat"


async def test_inflight_run_blocks_a_second_fire(store: ScheduleStore) -> None:
    entry = _create(store, interval_minutes=1)
    started = asyncio.Event()
    release = asyncio.Event()
    dispatched: list[str] = []

    async def dispatch_to_web(e, model, mode, provider, *, target_chat_id=None):
        dispatched.append(e.schedule_id)
        started.set()
        await release.wait()
        return {"status": "ok"}

    manager = ScheduleManager(
        store=store,
        dispatch_to_web=dispatch_to_web,
        prepare_chat=lambda e, prompt, model, mode, provider: e.web_chat_id,
    )
    t0 = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)
    await manager.tick(now=t0)
    await asyncio.wait_for(started.wait(), timeout=1)
    await manager.tick(now=t0 + timedelta(minutes=5))
    assert dispatched == [entry.schedule_id]
    release.set()
    await _settle()
    assert store.get(entry.schedule_id).last_status == "ok"


async def test_rehomed_chat_is_persisted(store: ScheduleStore) -> None:
    """prepare_chat re-points a dead target at a replacement chat. Without
    carrying that onto the status write-back the entry forgets it and builds a
    new chat every interval."""
    entry = _create(store, web_chat_id="dead-chat")

    def prepare(e, prompt, model, mode, provider):
        e.web_chat_id = "fresh-chat"
        return "fresh-chat"

    manager = ScheduleManager(
        store=store,
        dispatch_to_web=None,
        prepare_chat=prepare,
    )
    await manager.tick()
    await _settle()
    assert store.get(entry.schedule_id).web_chat_id == "fresh-chat"


async def test_a_concurrent_user_edit_is_not_clobbered(store: ScheduleStore) -> None:
    entry = _create(store)

    async def dispatch_to_web(e, model, mode, provider, *, target_chat_id=None):
        edited = store.get(e.schedule_id)
        edited.prompt = "user rewrote this mid-run"
        store.replace(edited)
        return {"status": "ok"}

    manager = ScheduleManager(
        store=store,
        dispatch_to_web=dispatch_to_web,
        prepare_chat=lambda e, prompt, model, mode, provider: e.web_chat_id,
    )
    await manager.tick()
    await _settle()
    assert store.get(entry.schedule_id).prompt == "user rewrote this mid-run"


async def test_dispatch_error_is_recorded(store: ScheduleStore) -> None:
    entry = _create(store)
    manager, _ = _make_manager(store, status="error")
    await manager.tick()
    await _settle()
    assert store.get(entry.schedule_id).last_status == "error"


async def test_dispatch_exception_is_recorded_as_error(store: ScheduleStore) -> None:
    entry = _create(store)

    async def dispatch_to_web(e, model, mode, provider, *, target_chat_id=None):
        raise RuntimeError("boom")

    manager = ScheduleManager(
        store=store,
        dispatch_to_web=dispatch_to_web,
        prepare_chat=lambda e, prompt, model, mode, provider: e.web_chat_id,
    )
    await manager.tick()
    await _settle()
    assert store.get(entry.schedule_id).last_status == "error"


async def test_interval_entries_inherit_the_chat_rather_than_override_it(
    store: ScheduleStore,
) -> None:
    """An empty model is what makes resolve_target hand back the chat's own."""
    entry = _create(store)
    assert entry.model == ""
    seen: list[str] = []

    async def dispatch_to_web(e, model, mode, provider, *, target_chat_id=None):
        seen.append(model)
        return {"status": "ok"}

    manager = ScheduleManager(
        store=store,
        resolve_target=lambda e: ("claude", e.model or "chat-model", "auto", ""),
        dispatch_to_web=dispatch_to_web,
        prepare_chat=lambda e, prompt, model, mode, provider: e.web_chat_id,
    )
    await manager.tick()
    await _settle()
    assert seen == ["chat-model"]


# ── Catch-up ─────────────────────────────────────────────────────────────


async def test_catch_up_skips_interval_entries(store: ScheduleStore) -> None:
    """Resuming the cadence is the right recovery; replaying every interval
    missed during downtime is not."""
    entry = _create(store)
    entry.last_dispatched_at = "2020-01-01T00:00:00+00:00"
    store.replace(entry)
    manager, dispatched = _make_manager(store)
    assert await manager.catch_up() == []
    await _settle()
    assert dispatched == []


# ── Manual run ───────────────────────────────────────────────────────────


async def test_dispatch_now_fires_a_disabled_entry(store: ScheduleStore) -> None:
    entry = _create(store)
    entry.enabled = False
    store.replace(entry)
    manager, dispatched = _make_manager(store)
    result = await manager.dispatch_now(entry.schedule_id)
    await _settle()
    assert result["status"] == "started"
    assert result["chat_id"] == "chat-x"
    assert dispatched == [entry.schedule_id]
    assert store.get(entry.schedule_id).last_dispatched_at != ""


async def test_dispatch_now_refuses_a_busy_chat(store: ScheduleStore) -> None:
    entry = _create(store)
    manager, dispatched = _make_manager(store, busy=True)
    result = await manager.dispatch_now(entry.schedule_id)
    assert result["status"] == "busy"
    assert dispatched == []


async def test_dispatch_now_reports_an_unrecoverable_target(
    store: ScheduleStore,
) -> None:
    entry = _create(store)
    manager, dispatched = _make_manager(store, dispatchable=False)
    result = await manager.dispatch_now(entry.schedule_id)
    assert result["status"] == "missing-chat"
    assert dispatched == []


async def test_dispatch_now_unknown_id_raises(store: ScheduleStore) -> None:
    manager, _ = _make_manager(store)
    with pytest.raises(ValueError):
        await manager.dispatch_now("sched-nope")


# ── Migration from loops.json ────────────────────────────────────────────


def _write_loops(runtime: Path, loops: list[dict]) -> None:
    (runtime / "loops.json").write_text(
        json.dumps({"loops": loops}), encoding="utf-8"
    )


def test_migrate_loops_imports_as_interval_schedules(tmp_path: Path) -> None:
    _write_loops(tmp_path, [{
        "loop_id": "loop-a1b2c3d4",
        "prompt": "check PRs",
        "web_chat_id": "chat-x",
        "web_project_id": "proj-1",
        "workspace": "work",
        "created_at": "2026-07-01T00:00:00+00:00",
        "interval_minutes": 15,
        "title": "PR watcher",
        "autostart": True,
        "last_run_at": "2026-07-02T09:00:00+00:00",
        "last_status": "ok",
    }])
    assert migrate_loops(tmp_path) == 1

    entry = ScheduleStore(tmp_path).get("loop-a1b2c3d4")
    assert entry is not None
    assert entry.frequency == INTERVAL_FREQUENCY
    assert entry.interval_minutes == 15
    assert entry.web_chat_id == "chat-x"
    # Legacy loops reused their existing chat; the old project hint must not
    # turn the migrated interval into a new-chat-per-run schedule.
    assert entry.web_project_id is None
    # ...but it is still the project the loop would have been re-homed into if
    # its chat went away, so it is kept as the fallback rather than dropped.
    assert entry.fallback_project_id == "proj-1"
    assert entry.workspace == "work"
    assert entry.title == "PR watcher"
    assert entry.enabled is True
    assert entry.last_dispatched_at == "2026-07-02T09:00:00+00:00"
    assert entry.last_status == "ok"
    # Empty model keeps the run inheriting the chat, as loops always did.
    assert entry.model == ""
    # The old file is moved aside so a later boot cannot re-import it.
    assert not (tmp_path / "loops.json").exists()
    assert (tmp_path / "loops.json.migrated").exists()


def test_migrate_loops_carries_autostart_as_enabled(tmp_path: Path) -> None:
    _write_loops(tmp_path, [
        {"loop_id": "loop-on", "prompt": "p", "web_chat_id": "c", "autostart": True},
        {"loop_id": "loop-off", "prompt": "p", "web_chat_id": "c", "autostart": False},
    ])
    migrate_loops(tmp_path)
    store = ScheduleStore(tmp_path)
    assert store.get("loop-on").enabled is True
    assert store.get("loop-off").enabled is False


def test_migrate_loops_is_idempotent_and_keeps_existing_entries(
    tmp_path: Path,
) -> None:
    store = ScheduleStore(tmp_path)
    store.create(
        daily_time_utc="09:00", prompt="daily", model="", mode="auto", chat_id=0,
        frequency="daily",
    )
    _write_loops(tmp_path, [
        {"loop_id": "loop-a", "prompt": "p", "web_chat_id": "c", "autostart": True},
    ])
    assert migrate_loops(tmp_path) == 1
    assert migrate_loops(tmp_path) == 0
    assert len(store.list_entries()) == 2


def test_migrate_loops_skips_a_loop_with_no_target(tmp_path: Path) -> None:
    _write_loops(tmp_path, [{"loop_id": "loop-a", "prompt": "p", "web_chat_id": ""}])
    assert migrate_loops(tmp_path) == 0
    assert ScheduleStore(tmp_path).list_entries() == []


def test_migrate_loops_no_file_is_a_no_op(tmp_path: Path) -> None:
    assert migrate_loops(tmp_path) == 0


def test_migrated_loop_rehomes_into_its_own_project_not_general(tmp_path: Path) -> None:
    """A migrated loop whose chat is deleted must resume where it lived.

    The legacy resolver used the loop's own ``web_project_id`` for this. The
    interval schema cannot reuse that field (there it means "new chat per run"
    and outranks the fixed chat), so the value is carried as
    ``fallback_project_id`` — without it ``resolve_automation_project`` falls
    straight through to the workspace's General and the unattended prompt runs
    in the wrong project context.
    """
    _write_loops(tmp_path, [{
        "loop_id": "loop-rehome",
        "prompt": "p",
        "web_chat_id": "chat-gone",
        "web_project_id": "proj-original",
        "workspace": "work",
    }])
    migrate_loops(tmp_path)
    entry = ScheduleStore(tmp_path).get("loop-rehome")

    class _Project:
        def __init__(self, pid: str, name: str) -> None:
            self.project_id, self.name, self.workspace = pid, name, "work"

    class _PCM:
        _projects = {
            "proj-original": _Project("proj-original", "Original"),
            "proj-general": _Project("proj-general", "General"),
        }
        resolve_automation_project = (
            ProjectChatManager.resolve_automation_project
        )

    assert _PCM().resolve_automation_project(entry).project_id == "proj-original"


def test_rehome_falls_back_to_general_when_the_project_is_gone(tmp_path: Path) -> None:
    _write_loops(tmp_path, [{
        "loop_id": "loop-orphan",
        "prompt": "p",
        "web_chat_id": "chat-gone",
        "web_project_id": "proj-deleted",
        "workspace": "work",
    }])
    migrate_loops(tmp_path)
    entry = ScheduleStore(tmp_path).get("loop-orphan")

    class _Project:
        def __init__(self, pid: str, name: str) -> None:
            self.project_id, self.name, self.workspace = pid, name, "work"

    class _PCM:
        _projects = {"proj-general": _Project("proj-general", "General")}
        resolve_automation_project = (
            ProjectChatManager.resolve_automation_project
        )

    assert _PCM().resolve_automation_project(entry).project_id == "proj-general"
