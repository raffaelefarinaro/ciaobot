"""Tests for LoopStore and LoopManager (in-chat interval loops)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ciao.loops import LoopEntry, LoopManager, LoopStore


@pytest.fixture
def store(tmp_path: Path) -> LoopStore:
    return LoopStore(tmp_path)


def _make_manager(
    store: LoopStore,
    *,
    busy: bool = False,
    exists: bool = True,
    dispatch_status: str = "ok",
):
    dispatched: list[str] = []

    async def dispatch(entry: LoopEntry) -> dict:
        dispatched.append(entry.loop_id)
        return {"status": dispatch_status, "chat_id": entry.web_chat_id}

    mgr = LoopManager(
        store=store,
        dispatch=dispatch,
        chat_busy=lambda chat_id: busy,
        chat_exists=lambda chat_id: exists,
    )
    return mgr, dispatched


async def _settle() -> None:
    """Let fire-and-forget dispatch tasks run to completion."""
    await asyncio.sleep(0.05)


# ── Store ────────────────────────────────────────────────────────────────


def test_store_create_round_trip(store: LoopStore) -> None:
    entry = store.create(
        prompt="check PRs",
        web_chat_id="chat-abc123",
        interval_minutes=10,
        title="PR watcher",
        autostart=True,
    )
    assert entry.loop_id.startswith("loop-")
    reloaded = store.get(entry.loop_id)
    assert reloaded is not None
    assert reloaded.prompt == "check PRs"
    assert reloaded.web_chat_id == "chat-abc123"
    assert reloaded.interval_minutes == 10
    assert reloaded.title == "PR watcher"
    assert reloaded.autostart is True
    assert reloaded.last_run_at == ""
    assert reloaded.last_status == ""


def test_store_clamps_interval_floor(store: LoopStore) -> None:
    entry = store.create(prompt="p", web_chat_id="chat-x", interval_minutes=0)
    assert entry.interval_minutes == 1
    assert entry.interval() == timedelta(minutes=1)


def test_store_tolerates_unknown_keys(tmp_path: Path) -> None:
    (tmp_path / "loops.json").write_text(
        json.dumps({
            "loops": [{
                "loop_id": "loop-legacy",
                "prompt": "p",
                "web_chat_id": "chat-x",
                "created_at": "2026-07-01T00:00:00+00:00",
                "future_field": "ignored",
            }]
        }),
        encoding="utf-8",
    )
    store = LoopStore(tmp_path)
    entry = store.get("loop-legacy")
    assert entry is not None
    assert entry.interval_minutes == 10  # default
    assert entry.autostart is False


def test_store_scope_defaults_to_user(store: LoopStore) -> None:
    entry = store.create(prompt="p", web_chat_id="chat-x")
    assert entry.scope == "user"
    reloaded = store.get(entry.loop_id)
    assert reloaded is not None
    assert reloaded.scope == "user"


def test_store_delete(store: LoopStore) -> None:
    entry = store.create(prompt="p", web_chat_id="chat-x")
    assert store.delete(entry.loop_id) is True
    assert store.get(entry.loop_id) is None
    assert store.delete(entry.loop_id) is False


# ── Manager: runtime state ───────────────────────────────────────────────


async def test_autostart_marks_only_flagged_loops_running(store: LoopStore) -> None:
    auto = store.create(prompt="p", web_chat_id="chat-x", autostart=True)
    manual = store.create(prompt="p", web_chat_id="chat-y", autostart=False)
    mgr, _ = _make_manager(store)
    mgr.start()
    try:
        assert mgr.is_running(auto.loop_id) is True
        assert mgr.is_running(manual.loop_id) is False
    finally:
        await mgr.stop()


async def test_start_and_stop_loop(store: LoopStore) -> None:
    entry = store.create(prompt="p", web_chat_id="chat-x")
    mgr, dispatched = _make_manager(store)
    mgr.start_loop(entry.loop_id)
    assert mgr.is_running(entry.loop_id)
    await mgr.tick()
    await _settle()
    assert dispatched == [entry.loop_id]
    mgr.stop_loop(entry.loop_id)
    assert not mgr.is_running(entry.loop_id)
    # A stopped loop never fires, even when due.
    latest = store.get(entry.loop_id)
    latest.last_run_at = ""
    store.replace(latest)
    await mgr.tick()
    await _settle()
    assert dispatched == [entry.loop_id]


def test_start_unknown_loop_raises(store: LoopStore) -> None:
    mgr, _ = _make_manager(store)
    with pytest.raises(ValueError):
        mgr.start_loop("loop-nope")


# ── Manager: due logic ───────────────────────────────────────────────────


async def test_tick_respects_interval(store: LoopStore) -> None:
    entry = store.create(prompt="p", web_chat_id="chat-x", interval_minutes=10)
    mgr, dispatched = _make_manager(store)
    mgr.start_loop(entry.loop_id)

    t0 = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)
    await mgr.tick(now=t0)  # never ran -> fires immediately
    await _settle()
    assert dispatched == [entry.loop_id]

    await mgr.tick(now=t0 + timedelta(minutes=5))  # not due yet
    await _settle()
    assert dispatched == [entry.loop_id]

    await mgr.tick(now=t0 + timedelta(minutes=10))  # due again
    await _settle()
    assert dispatched == [entry.loop_id, entry.loop_id]
    latest = store.get(entry.loop_id)
    assert latest.last_status == "ok"
    assert latest.last_run_at == (t0 + timedelta(minutes=10)).isoformat(timespec="seconds")


async def test_busy_chat_skips_without_stamping(store: LoopStore) -> None:
    entry = store.create(prompt="p", web_chat_id="chat-x", interval_minutes=10)
    mgr, dispatched = _make_manager(store, busy=True)
    mgr.start_loop(entry.loop_id)
    t0 = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)
    await mgr.tick(now=t0)
    await _settle()
    assert dispatched == []
    latest = store.get(entry.loop_id)
    assert latest.last_status == "busy"
    # last_run_at untouched: the loop stays due and fires as soon as the
    # chat frees up.
    assert latest.last_run_at == ""


async def test_fires_as_soon_as_chat_frees_up(store: LoopStore) -> None:
    entry = store.create(prompt="p", web_chat_id="chat-x", interval_minutes=10)
    busy_flag = {"busy": True}
    dispatched: list[str] = []

    async def dispatch(e: LoopEntry) -> dict:
        dispatched.append(e.loop_id)
        return {"status": "ok"}

    mgr = LoopManager(
        store=store,
        dispatch=dispatch,
        chat_busy=lambda chat_id: busy_flag["busy"],
        chat_exists=lambda chat_id: True,
    )
    mgr.start_loop(entry.loop_id)
    t0 = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)
    await mgr.tick(now=t0)
    await _settle()
    assert dispatched == []
    busy_flag["busy"] = False
    await mgr.tick(now=t0 + timedelta(seconds=20))
    await _settle()
    assert dispatched == [entry.loop_id]


async def test_missing_chat_auto_stops_loop(store: LoopStore) -> None:
    entry = store.create(prompt="p", web_chat_id="chat-gone", interval_minutes=1)
    mgr, dispatched = _make_manager(store, exists=False)
    mgr.start_loop(entry.loop_id)
    await mgr.tick()
    await _settle()
    assert dispatched == []
    assert not mgr.is_running(entry.loop_id)
    assert store.get(entry.loop_id).last_status == "missing-chat"


async def test_deleted_entry_dropped_from_running(store: LoopStore) -> None:
    entry = store.create(prompt="p", web_chat_id="chat-x")
    mgr, dispatched = _make_manager(store)
    mgr.start_loop(entry.loop_id)
    store.delete(entry.loop_id)
    await mgr.tick()
    await _settle()
    assert dispatched == []
    assert not mgr.is_running(entry.loop_id)


async def test_inflight_iteration_blocks_refire(store: LoopStore) -> None:
    entry = store.create(prompt="p", web_chat_id="chat-x", interval_minutes=1)
    started = asyncio.Event()
    release = asyncio.Event()
    dispatched: list[str] = []

    async def dispatch(e: LoopEntry) -> dict:
        dispatched.append(e.loop_id)
        started.set()
        await release.wait()
        return {"status": "ok"}

    mgr = LoopManager(
        store=store,
        dispatch=dispatch,
        chat_busy=lambda chat_id: False,
        chat_exists=lambda chat_id: True,
    )
    mgr.start_loop(entry.loop_id)
    t0 = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)
    await mgr.tick(now=t0)
    await asyncio.wait_for(started.wait(), timeout=1)
    # Iteration still streaming when the next interval elapses: no double fire.
    await mgr.tick(now=t0 + timedelta(minutes=5))
    assert dispatched == [entry.loop_id]
    release.set()
    await _settle()
    assert store.get(entry.loop_id).last_status == "ok"


async def test_dispatch_error_recorded(store: LoopStore) -> None:
    entry = store.create(prompt="p", web_chat_id="chat-x")
    mgr, dispatched = _make_manager(store, dispatch_status="error")
    mgr.start_loop(entry.loop_id)
    await mgr.tick()
    await _settle()
    assert dispatched == [entry.loop_id]
    assert store.get(entry.loop_id).last_status == "error"


async def test_dispatch_exception_recorded_as_error(store: LoopStore) -> None:
    entry = store.create(prompt="p", web_chat_id="chat-x")

    async def dispatch(e: LoopEntry) -> dict:
        raise RuntimeError("boom")

    mgr = LoopManager(
        store=store,
        dispatch=dispatch,
        chat_busy=lambda chat_id: False,
        chat_exists=lambda chat_id: True,
    )
    mgr.start_loop(entry.loop_id)
    await mgr.tick()
    await _settle()
    assert store.get(entry.loop_id).last_status == "error"


# ── Manager: run now ─────────────────────────────────────────────────────


async def test_run_now_fires_even_when_stopped(store: LoopStore) -> None:
    entry = store.create(prompt="p", web_chat_id="chat-x")
    mgr, dispatched = _make_manager(store)
    result = await mgr.run_now(entry.loop_id)
    await _settle()
    assert result["status"] == "started"
    assert result["chat_id"] == "chat-x"
    assert dispatched == [entry.loop_id]
    assert store.get(entry.loop_id).last_run_at != ""


async def test_run_now_reports_busy(store: LoopStore) -> None:
    entry = store.create(prompt="p", web_chat_id="chat-x")
    mgr, dispatched = _make_manager(store, busy=True)
    result = await mgr.run_now(entry.loop_id)
    assert result["status"] == "busy"
    assert dispatched == []


async def test_run_now_unknown_loop_raises(store: LoopStore) -> None:
    mgr, _ = _make_manager(store)
    with pytest.raises(ValueError):
        await mgr.run_now("loop-nope")
