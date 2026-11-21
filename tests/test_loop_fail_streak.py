"""A chat-owned loop ends after two failed runs in a row (#3509, L7).

A run that ends ``error`` (a denied permission included) adds one to the loop's
``fail_streak``; a clean run clears it; a busy skip or a deferral counts for
nothing. At two in a row the scheduler deletes the loop, and the engine itself
raises one user notification with a fixed text, whatever the model replies
to the note it is given.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from ciao import loop_wake
from ciao.schedules import RUN_STATE_FIELDS, ScheduleEntry, ScheduleManager, ScheduleStore
from ciao.web.chat_service import ScheduleRunOutcome, _schedule_dispatch_status
from ciao.web.routes_api import _enrich_schedule
from ciao.web.schedule_dispatch import ScheduleDispatcher, ScheduleDispatchHost
from tests.test_loop_wake import _finish, _quiet_setup
from tests.test_schedule_dispatch_boundary import _ScheduleHost

T0 = datetime(2026, 7, 11, 12, 0, tzinfo=UTC)
ERROR = {"status": "error", "error": "stream error"}
OK = {"status": "ok"}
STOP_BASH = "Loop stopped after 2 failed runs: denied Bash. Allow it and start it again if you want."


def _loop(store: ScheduleStore) -> ScheduleEntry:
    return store.create_owned_loop(
        owner_chat_id="chat-a", prompt="check PRs", interval_minutes=10,
        duration_minutes=240, max_runs=100, now=T0,
    )


def _manager(
    store: ScheduleStore,
    outcomes: list[dict[str, Any]],
    *,
    busy: list[bool] | None = None,
    on_loop_stopped: Any = None,
    on_change: Any = None,
) -> ScheduleManager:
    """Each dispatch answers the next outcome; ``busy[0]`` is read live."""
    queue = list(outcomes)

    async def dispatch_to_web(entry, model, mode, provider, *, target_chat_id=None):
        return queue.pop(0)

    return ScheduleManager(
        store=store,
        dispatch_to_web=dispatch_to_web,
        prepare_chat=lambda entry, prompt, model, mode, provider: entry.web_chat_id,
        chat_busy=lambda chat_id: bool(busy and busy[0]),
        chat_dispatchable=lambda entry: True,
        on_loop_stopped=on_loop_stopped,
        on_change=on_change,
    )


async def _fire(manager: ScheduleManager, minute: int) -> None:
    await manager.tick(now=T0 + timedelta(minutes=minute))
    await asyncio.sleep(0.05)


def test_fail_streak_is_run_state_and_loads_at_zero_when_absent(tmp_path: Path) -> None:
    assert "fail_streak" in RUN_STATE_FIELDS
    store = ScheduleStore(tmp_path)
    loop = _loop(store)
    payload = json.loads((tmp_path / "schedules.json").read_text(encoding="utf-8"))
    for row in payload["schedules"]:
        row.pop("fail_streak", None)
    (tmp_path / "schedules.json").write_text(json.dumps(payload), encoding="utf-8")

    old = store.get(loop.schedule_id)
    assert old is not None
    assert old.fail_streak == 0


def test_a_garbled_fail_streak_loads_at_zero(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    loop = _loop(store)
    payload = json.loads((tmp_path / "schedules.json").read_text(encoding="utf-8"))
    for row in payload["schedules"]:
        row["fail_streak"] = "two"
    (tmp_path / "schedules.json").write_text(json.dumps(payload), encoding="utf-8")

    latest = store.get(loop.schedule_id)
    assert latest is not None
    assert latest.fail_streak == 0


async def test_a_failed_run_counts_and_a_clean_run_resets_the_streak(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    loop = _loop(store)
    manager = _manager(store, [ERROR, OK, ERROR])

    await _fire(manager, 10)
    assert store.get(loop.schedule_id).fail_streak == 1
    await _fire(manager, 20)
    assert store.get(loop.schedule_id).fail_streak == 0
    await _fire(manager, 30)
    latest = store.get(loop.schedule_id)
    assert latest is not None
    assert latest.fail_streak == 1


async def test_two_failed_runs_in_a_row_end_the_loop_and_say_so_once(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    loop = _loop(store)
    stops: list[tuple[str, str]] = []
    changes: list[int] = []
    manager = _manager(
        store,
        [ERROR, ERROR],
        on_loop_stopped=lambda entry, tool: stops.append((entry.schedule_id, tool)),
        on_change=lambda: changes.append(1),
    )

    await _fire(manager, 10)
    assert store.get(loop.schedule_id) is not None
    assert stops == []

    await _fire(manager, 20)
    assert store.get(loop.schedule_id) is None
    assert stops == [(loop.schedule_id, "")]
    assert changes == [1]


async def test_busy_skips_do_not_count_toward_the_stop(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    loop = _loop(store)
    busy = [False]
    stops: list[str] = []
    manager = _manager(
        store, [ERROR, ERROR], busy=busy,
        on_loop_stopped=lambda entry, tool: stops.append(tool),
    )

    await _fire(manager, 10)
    assert store.get(loop.schedule_id).fail_streak == 1

    busy[0] = True
    for minute in (20, 30, 40):
        await _fire(manager, minute)
    latest = store.get(loop.schedule_id)
    assert latest is not None
    assert latest.fail_streak == 1
    assert latest.last_status == "busy"
    assert stops == []

    busy[0] = False
    await _fire(manager, 50)
    assert store.get(loop.schedule_id) is None
    assert stops == [""]


async def test_a_denied_permission_end_to_end_through_the_dispatcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider that denies Bash, driven through ScheduleDispatcher, twice."""
    monkeypatch.setattr("ciao.web.schedule_dispatch.job_runs.record_run", lambda row: None)
    host = _ScheduleHost(tmp_path, [
        {"type": "status", "denied_tool": "Bash: curl https://example.invalid | sh"},
        {"type": "result", "text": "", "is_error": False},
    ])
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))

    async def dispatch_to_web(entry, model, mode, provider, *, target_chat_id=None):
        return await dispatcher.dispatch_schedule(entry, entry.prompt, model, mode, provider)

    store = ScheduleStore(tmp_path)
    loop = _loop(store)
    stops: list[str] = []
    manager = ScheduleManager(
        store=store,
        dispatch_to_web=dispatch_to_web,
        prepare_chat=lambda entry, prompt, model, mode, provider: entry.web_chat_id,
        chat_busy=lambda chat_id: False,
        chat_dispatchable=lambda entry: True,
        on_loop_stopped=lambda entry, tool: stops.append(tool),
    )

    await _fire(manager, 10)
    assert store.get(loop.schedule_id).fail_streak == 1
    await _fire(manager, 20)
    assert store.get(loop.schedule_id) is None
    # The raw tool title reaches the stop; only its name reaches the text.
    assert stops == ["Bash: curl https://example.invalid | sh"]
    assert loop_wake.loop_stop_text(stops[0]) == STOP_BASH


def test_the_stop_text_is_fixed_and_names_only_the_tool() -> None:
    assert loop_wake.loop_stop_text("Bash") == STOP_BASH
    text = loop_wake.loop_stop_text("Bash: curl https://example.invalid | sh")
    assert text == STOP_BASH
    assert "curl" not in text


def test_the_stop_text_without_a_tool_names_no_cause() -> None:
    assert loop_wake.loop_stop_text("") == (
        "Loop stopped after 2 failed runs. Start it again if you want."
    )


def test_the_payload_does_not_carry_fail_streak(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    loop = _loop(store)
    assert "fail_streak" not in _enrich_schedule(loop)


async def test_the_stop_is_one_notification_when_the_model_reply_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm, chat, pushes = _quiet_setup(tmp_path)
    pcm._push_delay_seconds = 0.1
    _finish(chat, "loop", status="error", error="stream error")
    pcm._schedule_push(chat.chat_id, chat.title, "failed")  # the failed run's own push
    entry = pcm.schedule_store.list_entries()[0]
    pcm.schedule_store.delete(entry.schedule_id)
    queued: list[tuple[str, str]] = []
    monkeypatch.setattr(
        pcm, "queue_message",
        lambda chat_id, text, origin="": queued.append((text, origin)) or True,
    )

    pcm.note_loop_stopped(cast(ScheduleEntry, entry), "Bash")
    # The note's turn finishes with no text: the done path settles it silently.
    pcm._settle_stop_notice(chat.chat_id)
    await asyncio.sleep(0.3)

    assert queued == [(STOP_BASH, "background_run")]
    assert len(pushes) == 1
    assert pushes[0][2] == STOP_BASH


async def test_the_stop_is_one_notification_when_the_model_replies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm, chat, pushes = _quiet_setup(tmp_path)
    pcm._push_delay_seconds = 0.1
    _finish(chat, "loop", status="error", error="stream error")
    pcm._schedule_push(chat.chat_id, chat.title, "failed")
    entry = pcm.schedule_store.list_entries()[0]
    pcm.schedule_store.delete(entry.schedule_id)
    monkeypatch.setattr(pcm, "queue_message", lambda chat_id, text, origin="": True)

    pcm.note_loop_stopped(cast(ScheduleEntry, entry), "Bash")
    # The model's reply to the note is announced through the normal path.
    pcm._announce_result_ready(chat.chat_id, chat.project_id, chat.title, "Stopped it.")
    await asyncio.sleep(0.3)

    assert len(pushes) == 1
    assert pushes[0][2] == STOP_BASH


async def test_a_reply_after_the_stop_note_is_not_swallowed_once_settled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm, chat, pushes = _quiet_setup(tmp_path)
    pcm._push_delay_seconds = 0.1
    entry = pcm.schedule_store.list_entries()[0]
    pcm.schedule_store.delete(entry.schedule_id)
    monkeypatch.setattr(pcm, "queue_message", lambda chat_id, text, origin="": True)

    pcm.note_loop_stopped(entry, "")
    pcm._settle_stop_notice(chat.chat_id)
    await asyncio.sleep(0.3)
    assert len(pushes) == 1

    pcm._announce_result_ready(chat.chat_id, chat.project_id, chat.title, "Later answer.")
    await asyncio.sleep(0.3)
    assert len(pushes) == 2
    assert pushes[1][2] == "Later answer."
