"""Post-setup grace window: system routines must not crowd out onboarding.

A brand-new install runs its first launch with no missed-run history for the
packaged system routines (Workspace care, Skill reflection). Because the
schedules are withheld during the setup wizard, the first post-setup startup
sees every routine as past due and `ScheduleManager.catch_up` fires them all
in parallel — the user meets background chats before (or alongside) their
onboarding chat. These tests cover the grace marker and the catch-up
suppression it drives.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ciao.schedules import ScheduleManager, ScheduleStore
from ciao.setup_marker import (
    SETUP_CATCH_UP_GRACE,
    catch_up_grace_active,
    read_setup_marker,
    write_setup_marker,
)


def _make_manager(store: ScheduleStore):
    dispatched: list[str] = []

    async def dispatch(entry, model, mode, provider, *, target_chat_id=None):
        dispatched.append(entry.schedule_id)

    return ScheduleManager(store=store, dispatch_to_web=dispatch), dispatched


def _fresh_install(tmp_path: Path) -> tuple[ScheduleStore, ScheduleManager, list[str]]:
    runtime_root = tmp_path / ".runtime"
    write_setup_marker(runtime_root)
    store = ScheduleStore(runtime_root, include_system=True)
    mgr, dispatched = _make_manager(store)
    return store, mgr, dispatched


def test_setup_marker_round_trip(tmp_path: Path) -> None:
    runtime_root = tmp_path / ".runtime"
    assert read_setup_marker(runtime_root) is None

    path = write_setup_marker(runtime_root, now=datetime(2026, 8, 28, 12, 0, tzinfo=UTC))

    assert path.exists()
    assert read_setup_marker(runtime_root) == datetime(
        2026, 8, 28, 12, 0, tzinfo=UTC
    )


def test_setup_marker_ignores_corrupt_content(tmp_path: Path) -> None:
    runtime_root = tmp_path / ".runtime"
    runtime_root.mkdir()
    (runtime_root / "setup-completed-at").write_text("not-a-timestamp\n", encoding="utf-8")

    assert read_setup_marker(runtime_root) is None
    assert catch_up_grace_active(runtime_root) is False


def test_grace_active_within_window(tmp_path: Path) -> None:
    runtime_root = tmp_path / ".runtime"
    write_setup_marker(runtime_root, now=datetime.now(UTC) - timedelta(hours=1))

    assert catch_up_grace_active(runtime_root) is True


def test_grace_expired_after_window(tmp_path: Path) -> None:
    runtime_root = tmp_path / ".runtime"
    write_setup_marker(
        runtime_root,
        now=datetime.now(UTC) - SETUP_CATCH_UP_GRACE - timedelta(minutes=1),
    )

    assert catch_up_grace_active(runtime_root) is False


def test_no_marker_means_no_grace(tmp_path: Path) -> None:
    # A rerun of setup over an existing workspace never writes the marker, so
    # established installs keep the normal catch-up behavior.
    assert catch_up_grace_active(tmp_path) is False


async def test_catch_up_skip_system_leaves_user_schedules_firing(tmp_path: Path) -> None:
    runtime_root = tmp_path / ".runtime"
    write_setup_marker(runtime_root)
    store = ScheduleStore(runtime_root, include_system=True)
    entry = store.create(
        daily_time_utc="08:00",
        prompt="user routine",
        model="sonnet",
        mode="bypass",
        chat_id=0,
        frequency="daily",
    )
    entry.created_at = "2026-01-01T00:00:00Z"
    store.replace(entry)
    mgr, dispatched = _make_manager(store)

    now_utc = datetime(2026, 1, 19, 14, 0, tzinfo=UTC)
    fired = await mgr.catch_up(now=now_utc, skip_system=True)

    await _drain()
    assert fired == [entry.schedule_id]
    assert dispatched == [entry.schedule_id]
    assert store.get(entry.schedule_id).last_triggered_on == "2026-01-19"


async def test_first_launch_within_grace_skips_system_routines(tmp_path: Path) -> None:
    # Setup just finished; every system routine looks past due (their packaged
    # times are after the wizard's restart). Without the grace window the
    # first catch-up would dispatch all of them in parallel.
    store, mgr, dispatched = _fresh_install(tmp_path)

    now_utc = datetime(2026, 1, 19, 14, 0, tzinfo=UTC)
    grace_active = catch_up_grace_active(
        tmp_path / ".runtime", now=now_utc
    )
    fired = await mgr.catch_up(now=now_utc, skip_system=grace_active)

    await _drain()
    assert grace_active is True
    assert fired == []
    assert dispatched == []
    for entry in store.list_entries():
        if entry.scope == "system":
            assert entry.last_triggered_on == ""


async def test_after_grace_system_routines_catch_up_normally(tmp_path: Path) -> None:
    runtime_root = tmp_path / ".runtime"
    now_utc = datetime(2026, 1, 19, 14, 0, tzinfo=UTC)
    write_setup_marker(
        runtime_root,
        now=now_utc - SETUP_CATCH_UP_GRACE - timedelta(hours=1),
    )
    store = ScheduleStore(runtime_root, include_system=True)
    mgr, dispatched = _make_manager(store)

    grace_active = catch_up_grace_active(runtime_root, now=now_utc)
    fired = await mgr.catch_up(now=now_utc, skip_system=grace_active)

    await _drain()
    assert grace_active is False
    system_ids = [
        entry.schedule_id
        for entry in store.list_entries()
        if entry.scope == "system"
    ]
    assert set(fired) == set(system_ids)
    assert set(dispatched) == set(system_ids)


async def _drain() -> None:
    await asyncio.sleep(0.05)