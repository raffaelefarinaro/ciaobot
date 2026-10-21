"""Post-setup grace window: system routines must not crowd out onboarding.

A brand-new install runs its first launch with no missed-run history for the
packaged system routine (Workspace care). Because the schedules are withheld
during the setup wizard, the first post-setup startup sees every routine as
past due and `ScheduleManager.catch_up` fires them all in parallel — the user
meets background chats before (or alongside) their onboarding chat. These
tests cover the grace marker and the catch-up suppression it drives.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ciao.schedules import ScheduleManager, ScheduleStore, compute_last_expected_run
from ciao.web.routes_api import _enrich_schedule
from ciao.setup_marker import (
    SETUP_CATCH_UP_GRACE,
    catch_up_grace_active,
    clear_welcome_pending,
    read_setup_marker,
    welcome_pending,
    write_setup_marker,
    write_welcome_pending,
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


def test_welcome_pending_round_trip(tmp_path: Path) -> None:
    runtime_root = tmp_path / ".runtime"
    assert welcome_pending(runtime_root) is False
    write_welcome_pending(runtime_root)
    assert welcome_pending(runtime_root) is True
    clear_welcome_pending(runtime_root)
    assert welcome_pending(runtime_root) is False
    clear_welcome_pending(runtime_root)


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
    store.record_system_first_seen(now_utc - SETUP_CATCH_UP_GRACE - timedelta(hours=1))
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


async def _setup_day_store(tmp_path: Path, t0: datetime):
    runtime_root = tmp_path / ".runtime"
    write_setup_marker(runtime_root, now=t0)
    store = ScheduleStore(
        runtime_root, include_system=True, workspace_names=lambda: ["personal"]
    )
    mgr, dispatched = _make_manager(store)
    await mgr.catch_up(now=t0, skip_system=True)
    return runtime_root, store, dispatched


async def test_fresh_install_does_not_report_system_routines_missed(
    tmp_path: Path,
) -> None:
    t0 = datetime(2026, 1, 19, 14, 0, tzinfo=UTC)
    runtime_root, store, _ = await _setup_day_store(tmp_path, t0)
    later = t0 + timedelta(hours=5)

    system = [e for e in store.list_entries() if e.scope == "system"]
    assert system
    for entry in system:
        assert compute_last_expected_run(entry, now=later) is None
        assert _enrich_schedule(entry, now=later)["missed"] is False

    state = json.loads(
        (runtime_root / "system_schedules_state.json").read_text(encoding="utf-8")
    )
    row = state["schedules"]["system-memory-curation@personal"]
    assert row["first_seen_at"] == "2026-01-19T14:00:00Z"


async def test_a_slot_after_first_seen_is_still_missed(tmp_path: Path) -> None:
    t0 = datetime(2026, 1, 19, 14, 0, tzinfo=UTC)
    _, store, _ = await _setup_day_store(tmp_path, t0)
    now2 = datetime(2026, 1, 21, 14, 0, tzinfo=UTC)

    entry = store.get("system-memory-curation@personal")
    assert entry is not None
    assert _enrich_schedule(entry, now=now2)["missed"] is True


async def _drain() -> None:
    await asyncio.sleep(0.05)