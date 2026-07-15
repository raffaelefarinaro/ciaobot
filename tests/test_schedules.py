"""Tests for schedule persistence, dispatch, and startup catch-up."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ciao.schedules import ScheduleManager, ScheduleStore


def _zurich(dt_utc: datetime) -> datetime:
    return dt_utc.astimezone(ZoneInfo("Europe/Zurich"))


@pytest.fixture
def store(tmp_path: Path) -> ScheduleStore:
    return ScheduleStore(tmp_path)


async def _make_manager(store: ScheduleStore):
    dispatched: list[str] = []

    async def dispatch(entry, model, mode, provider, *, target_chat_id=None):
        dispatched.append(entry.schedule_id)

    mgr = ScheduleManager(
        store=store,
        dispatch_to_web=dispatch,
    )
    return mgr, dispatched


def _set_created_at(
    store: ScheduleStore,
    entry,
    created_at: str = "2026-01-01T00:00:00Z",
):
    """Give time-travel tests a deterministic creation floor."""
    entry.created_at = created_at
    store.replace(entry)
    return entry


def test_schedule_policy_fields_default_for_legacy_entries(tmp_path: Path) -> None:
    path = tmp_path / "schedules.json"
    path.write_text(
        (
            '{"schedules":[{'
            '"schedule_id":"sched-legacy",'
            '"daily_time_utc":"01:00",'
            '"prompt":"legacy",'
            '"chat_id":0,'
            '"created_at":"2026-06-06T00:00:00Z"'
            '}]}'
        ),
        encoding="utf-8",
    )
    store = ScheduleStore(tmp_path)
    entry = store.get("sched-legacy")
    assert entry is not None
    assert entry.archive_policy == "manual"


def test_schedule_policy_fields_round_trip(store: ScheduleStore) -> None:
    entry = store.create(
        daily_time_utc="01:00",
        prompt="curate",
        model="sonnet",
        mode="auto",
        chat_id=0,
        frequency="daily",
        archive_policy="auto",
    )
    reloaded = store.get(entry.schedule_id)
    assert reloaded is not None
    assert reloaded.archive_policy == "auto"


def test_system_schedules_load_from_stock_not_runtime(tmp_path: Path) -> None:
    (tmp_path / "schedules.json").write_text(
        json.dumps(
            {
                "schedules": [
                    {
                        "schedule_id": "sched-user",
                        "daily_time_utc": "08:00",
                        "prompt": "user routine",
                        "chat_id": 0,
                        "created_at": "2026-06-06T00:00:00Z",
                    },
                    {
                        "schedule_id": "system-old-copy",
                        "scope": "system",
                        "daily_time_utc": "09:00",
                        "prompt": "stale copied system routine",
                        "chat_id": 0,
                        "created_at": "2026-06-06T00:00:00Z",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    store = ScheduleStore(tmp_path, include_system=True)

    schedules = store.list()

    assert store.get("sched-user") is not None
    assert store.get("system-old-copy") is None
    assert any(item.schedule_id == "system-memory-curation" for item in schedules)


def test_stock_memory_curation_schedule_updates_project_canonical_docs(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path, include_system=True)
    entry = store.get("system-memory-curation")
    assert entry is not None
    assert "canonical doc" in entry.prompt.lower()
    assert "session-insights" in entry.prompt.lower()


def test_system_schedule_state_persists_separately(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path, include_system=True)
    entry = store.get("system-memory-curation")
    assert entry is not None

    entry.last_triggered_on = "2026-07-05"
    entry.last_dispatched_at = "2026-07-05T01:00:00"
    entry.enabled = False
    entry.workspace = "work"
    store.replace(entry)

    runtime = json.loads((tmp_path / "schedules.json").read_text(encoding="utf-8")) if (tmp_path / "schedules.json").exists() else {"schedules": []}
    state = json.loads((tmp_path / "system_schedules_state.json").read_text(encoding="utf-8"))
    reloaded = ScheduleStore(tmp_path, include_system=True).get("system-memory-curation")

    assert runtime == {"schedules": []}
    assert state["schedules"]["system-memory-curation"]["last_triggered_on"] == "2026-07-05"
    assert state["schedules"]["system-memory-curation"]["workspace"] == "work"
    assert reloaded is not None
    assert reloaded.enabled is False
    assert reloaded.last_triggered_on == "2026-07-05"
    assert reloaded.workspace == "work"


async def test_catch_up_fires_past_due_schedule(store: ScheduleStore):
    # Monday 15:00 Zurich = 13:00 UTC (CET) / 14:00 UTC (CEST, summer).
    # Use a plain winter date so offset is well-defined: Monday 2026-01-19.
    entry = store.create(
        daily_time_utc="08:00",
        prompt="morning",
        model="sonnet",
        mode="bypass",
        chat_id=0,
        frequency="weekly",
        days_of_week=["mon", "tue", "wed", "thu", "fri"],
    )
    _set_created_at(store, entry)
    mgr, dispatched = await _make_manager(store)
    # "Now" = Monday 2026-01-19 15:00 Zurich (= 14:00 UTC in winter)
    now_utc = datetime(2026, 1, 19, 14, 0, tzinfo=UTC)
    fired = await mgr.catch_up(now=now_utc)
    # Dispatch is fire-and-forget via create_task; let the task run.
    await asyncio.sleep(0.05)
    assert fired == [entry.schedule_id]
    assert dispatched == [entry.schedule_id]
    # Marked as triggered today
    reloaded = store.get(entry.schedule_id)
    assert reloaded.last_triggered_on == "2026-01-19"


async def test_catch_up_skips_new_schedule_before_first_run(store: ScheduleStore):
    entry = store.create(
        daily_time_utc="18:00",
        prompt="evening",
        model="sonnet",
        mode="bypass",
        chat_id=0,
        frequency="daily",
    )
    _set_created_at(store, entry, "2026-01-19T05:00:00Z")
    mgr, dispatched = await _make_manager(store)
    # 15:00 Zurich (before 18:00) — should NOT fire
    now_utc = datetime(2026, 1, 19, 14, 0, tzinfo=UTC)
    fired = await mgr.catch_up(now=now_utc)
    assert fired == []
    assert dispatched == []
    assert store.get(entry.schedule_id).last_triggered_on == ""


async def test_catch_up_skips_already_triggered_today(store: ScheduleStore):
    entry = store.create(
        daily_time_utc="08:00",
        prompt="morning",
        model="sonnet",
        mode="bypass",
        chat_id=0,
        frequency="daily",
    )
    _set_created_at(store, entry)
    entry.last_triggered_on = "2026-01-19"
    store.replace(entry)
    mgr, dispatched = await _make_manager(store)
    now_utc = datetime(2026, 1, 19, 14, 0, tzinfo=UTC)
    fired = await mgr.catch_up(now=now_utc)
    assert fired == []
    assert dispatched == []


async def test_catch_up_fires_latest_weekly_slot_from_previous_day(
    store: ScheduleStore,
):
    # Schedule only fires on Sundays; the server returns Monday.
    entry = store.create(
        daily_time_utc="08:00",
        prompt="sunday-only",
        model="sonnet",
        mode="bypass",
        chat_id=0,
        frequency="weekly",
        days_of_week=["sun"],
    )
    _set_created_at(store, entry)
    mgr, dispatched = await _make_manager(store)
    # Monday 2026-01-19 15:00 Zurich — catch up Sunday's missed slot.
    now_utc = datetime(2026, 1, 19, 14, 0, tzinfo=UTC)
    fired = await mgr.catch_up(now=now_utc)
    await asyncio.sleep(0.05)
    assert fired == [entry.schedule_id]
    assert dispatched == [entry.schedule_id]
    assert store.get(entry.schedule_id).last_triggered_on == "2026-01-18"


async def test_catch_up_monthly_matches_day(store: ScheduleStore):
    entry = store.create(
        daily_time_utc="10:00",
        prompt="monthly",
        model="sonnet",
        mode="bypass",
        chat_id=0,
        frequency="monthly",
        day_of_month=19,
    )
    _set_created_at(store, entry)
    mgr, dispatched = await _make_manager(store)
    # 2026-01-19 at 15:00 Zurich: day matches, time past
    now_utc = datetime(2026, 1, 19, 14, 0, tzinfo=UTC)
    fired = await mgr.catch_up(now=now_utc)
    await asyncio.sleep(0.05)
    assert fired == [entry.schedule_id]
    assert dispatched == [entry.schedule_id]


async def test_catch_up_monthly_skips_other_days(store: ScheduleStore):
    entry = store.create(
        daily_time_utc="10:00",
        prompt="monthly",
        model="sonnet",
        mode="bypass",
        chat_id=0,
        frequency="monthly",
        day_of_month=25,
    )
    _set_created_at(store, entry)
    mgr, dispatched = await _make_manager(store)
    # 2026-01-19: day does NOT match (only day 25)
    now_utc = datetime(2026, 1, 19, 14, 0, tzinfo=UTC)
    fired = await mgr.catch_up(now=now_utc)
    assert fired == []


async def test_catch_up_runs_only_latest_missed_occurrence_once(
    store: ScheduleStore,
):
    entry = store.create(
        daily_time_utc="08:00",
        prompt="daily summary",
        model="sonnet",
        mode="bypass",
        chat_id=0,
        frequency="daily",
    )
    _set_created_at(store, entry)
    mgr, dispatched = await _make_manager(store)
    now_utc = datetime(2026, 1, 19, 14, 0, tzinfo=UTC)

    first = await mgr.catch_up(now=now_utc)
    second = await mgr.catch_up(now=now_utc)
    await asyncio.sleep(0.05)

    assert first == [entry.schedule_id]
    assert second == []
    assert dispatched == [entry.schedule_id]


async def test_previous_day_catch_up_does_not_suppress_todays_tick(
    store: ScheduleStore,
):
    entry = store.create(
        daily_time_utc="08:00",
        prompt="daily summary",
        model="sonnet",
        mode="bypass",
        chat_id=0,
        frequency="daily",
    )
    _set_created_at(store, entry)
    mgr, dispatched = await _make_manager(store)

    # Server returns Monday at 07:00 local, before today's slot. Catch up the
    # latest missed occurrence (Sunday), then allow Monday's 08:00 tick.
    caught_up = await mgr.catch_up(
        now=datetime(2026, 1, 19, 6, 0, tzinfo=UTC)
    )
    assert store.get(entry.schedule_id).last_triggered_on == "2026-01-18"
    await mgr.tick(now=datetime(2026, 1, 19, 7, 0, tzinfo=UTC))
    await asyncio.sleep(0.05)

    assert caught_up == [entry.schedule_id]
    assert dispatched == [entry.schedule_id, entry.schedule_id]
    assert store.get(entry.schedule_id).last_triggered_on == "2026-01-19"


# ── Manual schedules ────────────────────────────────────────────────────
# Manual schedules never auto-fire. They exist only so the "Run now" button
# has a saved prompt to dispatch on click.


async def test_manual_schedule_skipped_by_tick(store: ScheduleStore):
    entry = store.create(
        daily_time_utc="",  # manual entries don't need a time
        prompt="monthly usage report",
        model="sonnet",
        mode="bypass",
        chat_id=0,
        frequency="manual",
    )
    mgr, dispatched = await _make_manager(store)
    # Even at an hour that *would* match a 00:00 daily schedule, manual must not fire.
    now_utc = datetime(2026, 1, 19, 0, 0, tzinfo=UTC)
    await mgr.tick(now=now_utc)
    await asyncio.sleep(0.05)
    assert dispatched == []
    assert store.get(entry.schedule_id).last_triggered_on == ""


async def test_manual_schedule_skipped_by_catch_up(store: ScheduleStore):
    store.create(
        daily_time_utc="",
        prompt="manual-only",
        model="sonnet",
        mode="bypass",
        chat_id=0,
        frequency="manual",
    )
    mgr, dispatched = await _make_manager(store)
    now_utc = datetime(2026, 1, 19, 14, 0, tzinfo=UTC)
    fired = await mgr.catch_up(now=now_utc)
    assert fired == []
    assert dispatched == []


async def test_manual_schedule_runs_via_dispatch_now(store: ScheduleStore):
    entry = store.create(
        daily_time_utc="",
        prompt="manual-only",
        model="sonnet",
        mode="bypass",
        chat_id=0,
        frequency="manual",
    )
    mgr, dispatched = await _make_manager(store)
    result = await mgr.dispatch_now(entry.schedule_id)
    await asyncio.sleep(0.05)
    assert result["schedule_id"] == entry.schedule_id
    assert dispatched == [entry.schedule_id]


async def test_dispatch_now_interactive_run_returns_immediately(store: ScheduleStore) -> None:
    entry = store.create(
        daily_time_utc="",
        prompt="manual interactive",
        model="sonnet",
        mode="auto",
        chat_id=0,
        frequency="manual",
        archive_policy="manual",
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def dispatch(entry, model, mode, provider, *, target_chat_id=None):
        started.set()
        await release.wait()
        return {"archived_to": "should-not-block"}

    mgr = ScheduleManager(store=store, dispatch_to_web=dispatch)
    result = await mgr.dispatch_now(entry.schedule_id)
    await asyncio.wait_for(started.wait(), timeout=1)
    assert result["schedule_id"] == entry.schedule_id
    assert result["archive_policy"] == "manual"
    assert "archived_to" not in result
    release.set()
    await asyncio.sleep(0.05)


async def test_dispatch_now_returns_chat_id_immediately_for_auto_archive(
    store: ScheduleStore,
) -> None:
    entry = store.create(
        daily_time_utc="",
        prompt="manual background",
        model="sonnet",
        mode="auto",
        chat_id=0,
        frequency="manual",
        archive_policy="auto",
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def dispatch(entry, model, mode, provider, *, target_chat_id=None):
        assert target_chat_id == "chat-sched"
        started.set()
        await release.wait()
        return {
            "chat_id": target_chat_id,
            "archived_to": "memory-vault/Logs/Chats/chat-sched/run.md",
        }

    def prepare_chat(entry, prompt, model, mode, provider):
        return "chat-sched"

    mgr = ScheduleManager(
        store=store,
        dispatch_to_web=dispatch,
        prepare_chat=prepare_chat,
    )
    result = await mgr.dispatch_now(entry.schedule_id)
    await asyncio.wait_for(started.wait(), timeout=1)
    assert result["chat_id"] == "chat-sched"
    assert "archived_to" not in result
    assert result["archive_policy"] == "auto"
    release.set()
    await asyncio.sleep(0.05)


def test_compute_next_run_returns_none_for_manual(store: ScheduleStore):
    from ciao.schedules import compute_next_run
    entry = store.create(
        daily_time_utc="",
        prompt="manual-only",
        model="sonnet",
        mode="bypass",
        chat_id=0,
        frequency="manual",
    )
    assert compute_next_run(entry) is None


# ── One-off schedules ──────────────────────────────────────────────────
# `once` fires exactly once at run_at_date + daily_time_utc (in the entry's
# timezone), then the entry is deleted from the store. Different from
# `manual`, which never auto-fires; different from recurring frequencies,
# which keep firing on schedule.


async def test_once_fires_on_exact_date_then_deletes(store: ScheduleStore):
    # Mon 2026-01-19 10:00 Zurich (= 09:00 UTC in winter)
    entry = store.create(
        daily_time_utc="10:00",
        prompt="one-off",
        model="sonnet",
        mode="bypass",
        chat_id=0,
        frequency="once",
        run_at_date="2026-01-19",
    )
    mgr, dispatched = await _make_manager(store)
    now_utc = datetime(2026, 1, 19, 9, 0, tzinfo=UTC)
    await mgr.tick(now=now_utc)
    await asyncio.sleep(0.05)
    assert dispatched == [entry.schedule_id]
    # Entry should be gone after firing
    assert store.get(entry.schedule_id) is None


async def test_once_does_not_fire_before_date(store: ScheduleStore):
    entry = store.create(
        daily_time_utc="10:00",
        prompt="one-off",
        model="sonnet",
        mode="bypass",
        chat_id=0,
        frequency="once",
        run_at_date="2026-01-20",
    )
    mgr, dispatched = await _make_manager(store)
    # Day before
    now_utc = datetime(2026, 1, 19, 9, 0, tzinfo=UTC)
    await mgr.tick(now=now_utc)
    await asyncio.sleep(0.05)
    assert dispatched == []
    assert store.get(entry.schedule_id) is not None


async def test_once_does_not_fire_at_wrong_time_same_day(store: ScheduleStore):
    entry = store.create(
        daily_time_utc="10:00",
        prompt="one-off",
        model="sonnet",
        mode="bypass",
        chat_id=0,
        frequency="once",
        run_at_date="2026-01-19",
    )
    mgr, dispatched = await _make_manager(store)
    # Same day, but 09:00 Zurich (= 08:00 UTC) — too early
    now_utc = datetime(2026, 1, 19, 8, 0, tzinfo=UTC)
    await mgr.tick(now=now_utc)
    await asyncio.sleep(0.05)
    assert dispatched == []
    assert store.get(entry.schedule_id) is not None


async def test_once_catch_up_fires_past_due_from_yesterday(store: ScheduleStore):
    # Server was down across the scheduled fire and is restarting now.
    entry = store.create(
        daily_time_utc="10:00",
        prompt="one-off",
        model="sonnet",
        mode="bypass",
        chat_id=0,
        frequency="once",
        run_at_date="2026-01-18",  # yesterday
    )
    mgr, dispatched = await _make_manager(store)
    # "Now" = 2026-01-19 15:00 Zurich (= 14:00 UTC)
    now_utc = datetime(2026, 1, 19, 14, 0, tzinfo=UTC)
    fired = await mgr.catch_up(now=now_utc)
    await asyncio.sleep(0.05)
    assert fired == [entry.schedule_id]
    assert dispatched == [entry.schedule_id]
    assert store.get(entry.schedule_id) is None


async def test_once_dispatch_now_deletes_entry(store: ScheduleStore):
    entry = store.create(
        daily_time_utc="10:00",
        prompt="one-off",
        model="sonnet",
        mode="bypass",
        chat_id=0,
        frequency="once",
        run_at_date="2026-05-09",
    )
    mgr, dispatched = await _make_manager(store)
    result = await mgr.dispatch_now(entry.schedule_id)
    await asyncio.sleep(0.05)
    assert result["schedule_id"] == entry.schedule_id
    assert dispatched == [entry.schedule_id]
    # `Run now` on a once schedule should consume it
    assert store.get(entry.schedule_id) is None


def test_once_compute_next_run_returns_target_datetime(store: ScheduleStore):
    from ciao.schedules import compute_next_run
    entry = store.create(
        daily_time_utc="10:00",
        prompt="one-off",
        model="sonnet",
        mode="bypass",
        chat_id=0,
        frequency="once",
        run_at_date="2026-01-19",
    )
    # Compute relative to a "now" before the target
    now_utc = datetime(2026, 1, 18, 0, 0, tzinfo=UTC)
    next_run = compute_next_run(entry, now=now_utc)
    assert next_run is not None
    assert next_run.date().isoformat() == "2026-01-19"
    assert next_run.strftime("%H:%M") == "10:00"


def test_once_compute_next_run_returns_none_if_past(store: ScheduleStore):
    from ciao.schedules import compute_next_run
    entry = store.create(
        daily_time_utc="10:00",
        prompt="one-off",
        model="sonnet",
        mode="bypass",
        chat_id=0,
        frequency="once",
        run_at_date="2026-01-10",
    )
    now_utc = datetime(2026, 1, 19, 0, 0, tzinfo=UTC)
    assert compute_next_run(entry, now=now_utc) is None


# ── Missed-run detection (compute_last_expected_run) ────────────────────
# A schedule is "missed" when its most recent expected fire has passed but no
# trigger was recorded for that day. compute_last_expected_run is the mirror of
# compute_next_run, walking backwards and bounded by created_at.


def _entry(**overrides):
    from ciao.schedules import ScheduleEntry
    base = dict(
        schedule_id="sched-test",
        daily_time_utc="08:00",
        prompt="p",
        chat_id=0,
        created_at="2026-01-01T00:00:00Z",
        frequency="daily",
        timezone_name="Europe/Zurich",
        last_triggered_on="",
    )
    base.update(overrides)
    return ScheduleEntry(**base)


def test_last_expected_run_daily_uses_today_after_time():
    from ciao.schedules import compute_last_expected_run
    # Now = Mon 2026-01-19 15:00 Zurich (= 14:00 UTC, winter), after 08:00.
    now = datetime(2026, 1, 19, 14, 0, tzinfo=UTC)
    last = compute_last_expected_run(_entry(), now=now)
    assert last is not None
    assert last.date().isoformat() == "2026-01-19"
    assert last.strftime("%H:%M") == "08:00"


def test_last_expected_run_daily_uses_yesterday_before_time():
    from ciao.schedules import compute_last_expected_run
    # Now = 07:00 Zurich (= 06:00 UTC winter), before today's 08:00 fire.
    now = datetime(2026, 1, 19, 6, 0, tzinfo=UTC)
    last = compute_last_expected_run(_entry(), now=now)
    assert last is not None
    assert last.date().isoformat() == "2026-01-18"


def test_last_expected_run_respects_created_floor():
    from ciao.schedules import compute_last_expected_run
    # Created today at 09:00 UTC; the only past candidate (today 08:00 Zurich
    # = 07:00 UTC winter) predates creation, so there's no expected run yet.
    now = datetime(2026, 1, 19, 14, 0, tzinfo=UTC)
    entry = _entry(created_at="2026-01-19T09:00:00Z")
    assert compute_last_expected_run(entry, now=now) is None


def test_last_expected_run_none_for_manual_and_disabled():
    from ciao.schedules import compute_last_expected_run
    now = datetime(2026, 1, 19, 14, 0, tzinfo=UTC)
    assert compute_last_expected_run(_entry(frequency="manual"), now=now) is None
    assert compute_last_expected_run(_entry(enabled=False), now=now) is None


def test_last_expected_run_weekly_skips_offdays():
    from ciao.schedules import compute_last_expected_run
    # Now = Sun 2026-01-18 14:00 UTC. Weekly on weekdays only → last expected
    # fire is Fri 2026-01-16.
    now = datetime(2026, 1, 18, 14, 0, tzinfo=UTC)
    entry = _entry(frequency="weekly", days_of_week=["mon", "tue", "wed", "thu", "fri"])
    last = compute_last_expected_run(entry, now=now)
    assert last is not None
    assert last.date().isoformat() == "2026-01-16"


def test_last_expected_run_once_past_and_future():
    from ciao.schedules import compute_last_expected_run
    now = datetime(2026, 1, 19, 14, 0, tzinfo=UTC)
    past = _entry(frequency="once", run_at_date="2026-01-10", daily_time_utc="10:00")
    assert compute_last_expected_run(past, now=now) is not None
    future = _entry(frequency="once", run_at_date="2026-01-25", daily_time_utc="10:00")
    assert compute_last_expected_run(future, now=now) is None


def test_enrich_schedule_flags_missed():
    from ciao.web.routes_api import _enrich_schedule
    # Daily schedule created months ago, never triggered → today's fire is
    # overdue and unrecorded, so it must be flagged missed.
    enriched = _enrich_schedule(_entry())
    assert enriched["missed"] is True
    assert enriched["last_expected_run"] is not None


def test_enrich_schedule_manual_not_missed():
    from ciao.web.routes_api import _enrich_schedule
    enriched = _enrich_schedule(_entry(frequency="manual", daily_time_utc=""))
    assert enriched["missed"] is False
    assert enriched["last_expected_run"] is None


async def test_dispatch_now_stamps_last_dispatched_at(
    store: ScheduleStore,
) -> None:
    """Manual "Run now" must stamp ``last_dispatched_at`` so schedule health
    checks know the schedule ran today, but it must NOT stamp
    ``last_triggered_on`` -- a manual run must not suppress the next scheduled
    fire (the daily-idempotency key for ``tick()``/``catch_up()``).
    """
    entry = store.create(
        daily_time_utc="",
        prompt="manual run",
        model="sonnet",
        mode="bypass",
        chat_id=0,
        frequency="manual",
    )
    mgr, dispatched = await _make_manager(store)
    assert entry.last_triggered_on == ""
    assert entry.last_dispatched_at == ""

    result = await mgr.dispatch_now(entry.schedule_id)
    await asyncio.sleep(0.05)
    assert result["schedule_id"] == entry.schedule_id

    reloaded = store.get(entry.schedule_id)
    assert reloaded is not None
    assert reloaded.last_dispatched_at != ""
    # last_triggered_on is intentionally not stamped on manual runs.
    assert reloaded.last_triggered_on == ""


async def test_enrich_schedule_manual_run_today_not_missed(
    store: ScheduleStore,
) -> None:
    """A schedule that was run manually today must not be reported as missed,
    even though its ``last_triggered_on`` is empty (manual runs do not stamp
    the daily-idempotency field).
    """
    from datetime import UTC, datetime
    from ciao.web.routes_api import _enrich_schedule

    entry = store.create(
        daily_time_utc="14:30",
        prompt="daily kr update",
        model="sonnet",
        mode="bypass",
        chat_id=0,
        frequency="daily",
        timezone_name="UTC",
    )
    # Simulate a manual run that happened today.
    entry.last_dispatched_at = datetime.now(UTC).isoformat(timespec="seconds")
    enriched = _enrich_schedule(entry)
    assert enriched["missed"] is False
    assert enriched["last_dispatched_at"] is not None


def test_enrich_schedule_late_manual_run_clears_missed():
    """A nightly slot that the cron path missed, then run manually the next
    morning, must not still read as missed: the manual dispatch happened after
    the expected fire, so the work was attended to (late). This is the bug the
    fix targets -- the old date-equality check kept the badge because the
    manual run's date differed from the missed slot's date.
    """
    from ciao.web.routes_api import _enrich_schedule
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)  # Monday noon
    entry = _entry(
        daily_time_utc="23:59",
        frequency="daily",
        timezone_name="UTC",
        last_triggered_on="",  # cron never stamped last night's 23:59 slot
        last_dispatched_at="2026-06-08T08:00:00+00:00",  # manual "Run now" this morning
    )
    enriched = _enrich_schedule(entry, now=now)
    # last_expected is Sun 2026-06-07 23:59; the manual run at Mon 08:00 is after it.
    assert enriched["last_expected_run"][:10] == "2026-06-07"
    assert enriched["missed"] is False


def test_enrich_schedule_dispatch_before_expected_stays_missed():
    """A dispatch that predates the most recent expected fire does not clear
    the badge -- that slot is genuinely missed.
    """
    from ciao.web.routes_api import _enrich_schedule
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    entry = _entry(
        daily_time_utc="23:59",
        frequency="daily",
        timezone_name="UTC",
        last_triggered_on="",
        last_dispatched_at="2026-06-06T23:59:00+00:00",  # two days before the missed slot
    )
    enriched = _enrich_schedule(entry, now=now)
    assert enriched["missed"] is True


def test_enrich_schedule_naive_dispatch_stamp_does_not_crash():
    """``tick``/``catch_up`` write a naive local-time stamp; comparing it
    against the tz-aware expected fire must not raise TypeError.
    """
    from ciao.web.routes_api import _enrich_schedule
    now = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
    entry = _entry(
        daily_time_utc="23:59",
        frequency="daily",
        timezone_name="UTC",
        last_triggered_on="",
        last_dispatched_at="2026-06-08T08:00:00",  # naive, no offset
    )
    enriched = _enrich_schedule(entry, now=now)
    # Localized to UTC, 2026-06-08T08:00 is after Sun 23:59 → not missed.
    assert enriched["missed"] is False


def test_was_dispatched_since_handles_both_stamp_formats():
    from ciao.schedules import was_dispatched_since
    when = datetime(2026, 6, 7, 23, 59, tzinfo=UTC)
    # Aware UTC stamp (dispatch_now format).
    assert was_dispatched_since(
        _entry(timezone_name="UTC", last_dispatched_at="2026-06-08T08:00:00+00:00"), when
    ) is True
    assert was_dispatched_since(
        _entry(timezone_name="UTC", last_dispatched_at="2026-06-06T08:00:00+00:00"), when
    ) is False
    # Naive local stamp (tick/catch_up format), localized to the entry's tz.
    assert was_dispatched_since(
        _entry(timezone_name="UTC", last_dispatched_at="2026-06-08T08:00:00"), when
    ) is True
    # Empty / malformed stamps are treated as "no dispatch".
    assert was_dispatched_since(_entry(last_dispatched_at=""), when) is False
    assert was_dispatched_since(_entry(last_dispatched_at="garbage"), when) is False


def test_system_routines_ship_descriptions_and_set(tmp_path: Path) -> None:
    """Packaged system routines must carry a user-facing description, and the
    shipped set is memory-curation + workspace-hygiene + skill-evolution
    (the operator-only self-improvement review is no longer shipped)."""
    store = ScheduleStore(tmp_path, include_system=True)
    system = {e.schedule_id: e for e in store.list() if e.scope == "system"}
    assert set(system) == {
        "system-memory-curation",
        "system-workspace-hygiene",
        "system-skill-evolution",
    }
    assert "system-weekly-review" not in system
    for entry in system.values():
        assert entry.description, f"{entry.schedule_id} missing a description"


def test_user_schedule_description_round_trips(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    entry = store.create(
        daily_time_utc="09:00",
        prompt="do a thing",
        model="",
        mode="auto",
        chat_id=0,
        frequency="daily",
    )
    entry.description = "Human-friendly summary"
    store.replace(entry)
    reloaded = ScheduleStore(tmp_path).get(entry.schedule_id)
    assert reloaded is not None
    assert reloaded.description == "Human-friendly summary"
