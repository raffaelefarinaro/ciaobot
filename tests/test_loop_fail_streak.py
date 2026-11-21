"""A chat-owned loop ends after two failed runs in a row (#3509, L7).

A run that ends ``error`` (a denied permission included) adds one to the loop's
``fail_streak``; a clean run clears it; a busy skip, a deferral or a question
counts for nothing. At two in a row the scheduler deletes the loop, and the
engine itself raises one user notification with a fixed text, whatever the
model replies to the note it is given.
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
from ciao.web import chat_streaming
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
    admission_closed: list[bool] | None = None,
    on_loop_stopped: Any = None,
    on_change: Any = None,
) -> ScheduleManager:
    """Each dispatch answers the next outcome; ``busy[0]`` and ``admission_closed[0]`` are read live."""
    queue = list(outcomes)

    async def dispatch_to_web(entry, model, mode, provider, *, target_chat_id=None):
        return queue.pop(0)

    return ScheduleManager(
        store=store,
        dispatch_to_web=dispatch_to_web,
        prepare_chat=lambda entry, prompt, model, mode, provider: entry.web_chat_id,
        chat_busy=lambda chat_id: bool(busy and busy[0]),
        chat_dispatchable=lambda entry: True,
        admission_closed=(lambda: admission_closed[0]) if admission_closed is not None else None,
        on_loop_stopped=on_loop_stopped,
        on_change=on_change,
    )


async def _fire(manager: ScheduleManager, minute: int) -> None:
    await manager.tick(now=T0 + timedelta(minutes=minute))
    await asyncio.sleep(0.05)


def _fake_delivery(pcm: Any, monkeypatch: pytest.MonkeyPatch, result: str) -> list[tuple[str, str]]:
    """Replace the wake delivery: record each (prompt, origin) and answer ``result``."""
    calls: list[tuple[str, str]] = []

    def deliver(parent, prompt, *, count, origin, report=True):
        calls.append((prompt, origin))
        return result

    monkeypatch.setattr(pcm, "_deliver_wake", deliver)
    return calls


def _record_events(pcm: Any, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(pcm._events, "publish", events.append)
    return events


def _ready(events: list[dict[str, Any]]) -> int:
    return sum(1 for event in events if event.get("type") == "chat_result_ready")


def _stop_entry(pcm: Any, chat_id: str) -> ScheduleEntry:
    """The loop of a test chat, deleted as ``_run_interval`` does before the stop."""
    entry = pcm.schedule_store.list_entries()[0]
    assert entry.owner_chat_id == chat_id
    pcm.schedule_store.delete(entry.schedule_id)
    return cast(ScheduleEntry, entry)


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


async def test_skipped_retrying_and_question_runs_leave_the_streak_alone(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    loop = _loop(store)
    # The real status of a question: the dispatch classifies it as "skipped".
    question_status, _ = _schedule_dispatch_status(
        ScheduleRunOutcome(completed=True, question_requested=True)
    )
    assert question_status == "skipped"
    manager = _manager(
        store,
        [ERROR, {"status": "skipped"}, {"status": "retrying"}, {"status": question_status}],
    )

    await _fire(manager, 10)
    for minute in (20, 30, 40):
        await _fire(manager, minute)
    latest = store.get(loop.schedule_id)
    assert latest is not None
    assert latest.fail_streak == 1
    assert latest.last_status == question_status


async def test_a_deferred_run_leaves_the_streak_alone(tmp_path: Path) -> None:
    store = ScheduleStore(tmp_path)
    loop = _loop(store)
    closed = [False]
    manager = _manager(store, [ERROR, ERROR], admission_closed=closed)

    await _fire(manager, 10)
    closed[0] = True
    for minute in (20, 30):
        await _fire(manager, minute)
    latest = store.get(loop.schedule_id)
    assert latest is not None
    assert latest.fail_streak == 1
    assert latest.run_count == 1

    closed[0] = False
    await _fire(manager, 40)
    assert store.get(loop.schedule_id) is None


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
    entry = _stop_entry(pcm, chat.chat_id)
    queued = _fake_delivery(pcm, monkeypatch, "started")

    pcm.note_loop_stopped(entry, "Bash")
    # The note's turn finishes with no text: the done path settles it silently.
    pcm._settle_stop_notice(chat.chat_id)
    await asyncio.sleep(0.3)

    assert queued == [(STOP_BASH, "loop_stop")]
    assert len(pushes) == 1
    assert pushes[0][2] == STOP_BASH


async def test_the_stop_is_one_notification_when_the_model_replies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm, chat, pushes = _quiet_setup(tmp_path)
    pcm._push_delay_seconds = 0.1
    _finish(chat, "loop", status="error", error="stream error")
    pcm._schedule_push(chat.chat_id, chat.title, "failed")
    entry = _stop_entry(pcm, chat.chat_id)
    _fake_delivery(pcm, monkeypatch, "started")

    pcm.note_loop_stopped(entry, "Bash")
    # The model's reply to the note is not announced: the stop already was.
    pcm._announce_result_ready(chat.chat_id, chat.project_id, chat.title, "Stopped it.")
    await asyncio.sleep(0.3)

    assert len(pushes) == 1
    assert pushes[0][2] == STOP_BASH


async def test_a_reply_after_the_stop_note_is_announced_once_settled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm, chat, pushes = _quiet_setup(tmp_path)
    pcm._push_delay_seconds = 0.1
    entry = _stop_entry(pcm, chat.chat_id)
    _fake_delivery(pcm, monkeypatch, "started")

    pcm.note_loop_stopped(entry, "")
    pcm._settle_stop_notice(chat.chat_id)
    await asyncio.sleep(0.3)
    assert len(pushes) == 1

    pcm._announce_result_ready(chat.chat_id, chat.project_id, chat.title, "Later answer.")
    await asyncio.sleep(0.3)
    assert len(pushes) == 2
    assert pushes[1][2] == "Later answer."


async def test_a_failing_delivery_still_leaves_one_notification_and_no_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm, chat, pushes = _quiet_setup(tmp_path)
    events = _record_events(pcm, monkeypatch)
    entry = _stop_entry(pcm, chat.chat_id)

    def broken(*args: object, **kwargs: object) -> str:
        raise RuntimeError("queue is down")

    monkeypatch.setattr(pcm, "_deliver_wake", broken)
    pcm.note_loop_stopped(entry, "Bash")  # must not raise
    await asyncio.sleep(0)

    assert len(pushes) == 1
    assert _ready(events) == 1
    assert chat.chat_id not in pcm._stop_notice_chats


async def test_a_deferred_note_still_notifies_and_the_next_reply_is_announced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm, chat, pushes = _quiet_setup(tmp_path)
    pcm._push_delay_seconds = 0.1
    entry = _stop_entry(pcm, chat.chat_id)
    _fake_delivery(pcm, monkeypatch, "deferred")

    pcm.note_loop_stopped(entry, "Bash")
    await asyncio.sleep(0)
    assert len(pushes) == 1
    assert chat.chat_id not in pcm._stop_notice_chats

    pcm._announce_result_ready(chat.chat_id, chat.project_id, chat.title, "Next answer.")
    await asyncio.sleep(0.3)
    assert len(pushes) == 2
    assert pushes[1][2] == "Next answer."


async def test_the_note_turn_settling_clears_its_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm, chat, _pushes = _quiet_setup(tmp_path)
    entry = _stop_entry(pcm, chat.chat_id)
    _fake_delivery(pcm, monkeypatch, "started")

    pcm.note_loop_stopped(entry, "Bash")
    assert chat.chat_id in pcm._stop_notice_chats
    pcm._settle_stop_notice(chat.chat_id)
    assert chat.chat_id not in pcm._stop_notice_chats


def test_forgetting_a_chat_drops_its_stop_marker_and_held_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm, chat, _pushes = _quiet_setup(tmp_path)
    started = _fake_delivery(pcm, monkeypatch, "started")
    entry = _stop_entry(pcm, chat.chat_id)
    pcm.note_loop_stopped(entry, "Bash")
    assert started and chat.chat_id in pcm._stop_notice_chats

    pcm._forget_shell_wake_state(chat.chat_id)  # the archive and delete path

    assert chat.chat_id not in pcm._stop_notice_chats
    assert chat.chat_id not in pcm._held_stop_notes


async def test_a_note_waits_while_the_owner_has_an_open_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm, chat, pushes = _quiet_setup(tmp_path)
    chat.pending_question = '{"request_id": "q-1"}'
    entry = _stop_entry(pcm, chat.chat_id)
    delivered = _fake_delivery(pcm, monkeypatch, "started")

    pcm.note_loop_stopped(entry, "Bash")
    await asyncio.sleep(0.05)
    assert delivered == []
    assert len(pushes) == 1  # the notification is not held

    chat.pending_question = ""
    pcm._settle_stop_notice(chat.chat_id)
    await asyncio.sleep(0.05)
    assert delivered == [(STOP_BASH, "loop_stop")]


async def test_the_stop_cancels_the_failed_runs_pending_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm, chat, pushes = _quiet_setup(tmp_path)
    pcm._push_delay_seconds = 5
    _finish(chat, "loop", status="error", error="stream error")
    pcm._schedule_push(chat.chat_id, chat.title, "failed")
    pending = pcm._pending_push[chat.chat_id]
    entry = _stop_entry(pcm, chat.chat_id)
    _fake_delivery(pcm, monkeypatch, "queued")

    pcm.note_loop_stopped(entry, "Bash")
    await asyncio.sleep(0)

    assert pending.cancelled()
    assert len(pushes) == 1
    assert pushes[0][2] == STOP_BASH


async def test_a_denied_final_run_leaves_exactly_one_banner_and_one_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm, chat, pushes = _quiet_setup(tmp_path)
    pcm._push_delay_seconds = 0.1
    events = _record_events(pcm, monkeypatch)
    store = pcm.schedule_store
    store.update_fields(store.list_entries()[0].schedule_id, {"fail_streak": 1})
    _finish(chat, "loop")
    pcm.note_unattended_denial(chat.chat_id)
    # The done path of the stopping run: its quiet check, then its reply.
    pcm.notify_loop_run_end(chat.chat_id)
    pcm._announce_result_ready(chat.chat_id, chat.project_id, chat.title, "Could not run it.")
    entry = _stop_entry(pcm, chat.chat_id)
    _fake_delivery(pcm, monkeypatch, "queued")

    pcm.note_loop_stopped(entry, "Bash")
    await asyncio.sleep(0.3)

    assert _ready(events) == 1
    assert len(pushes) == 1
    assert pushes[0][2] == STOP_BASH


async def test_a_first_denied_run_still_notifies_normally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm, chat, pushes = _quiet_setup(tmp_path)
    pcm._push_delay_seconds = 0.1
    events = _record_events(pcm, monkeypatch)
    _finish(chat, "loop")
    pcm.note_unattended_denial(chat.chat_id)

    pcm._announce_result_ready(chat.chat_id, chat.project_id, chat.title, "Denied once.")
    await asyncio.sleep(0.3)

    assert _ready(events) == 1
    assert len(pushes) == 1
    assert pushes[0][2] == "Denied once."


def test_the_done_path_settles_the_stop_marker_after_the_announce() -> None:
    """Seam: the reply announce is judged first, then the marker is spent."""
    text = Path(chat_streaming.__file__).read_text(encoding="utf-8")
    start = text.index("if not had_error and last_answer_text.strip():")
    announce = text.index("self._host._announce_result_ready(", start)
    settle = text.index("self._host._settle_stop_notice(chat_id)", start)
    assert start < announce < settle
