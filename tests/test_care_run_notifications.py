"""The Workspace care run notifies once, with its outcome.

The care run is an orchestrator chat: its first reply plans, and each subagent
that finishes wakes it for another turn. Those turns are not results for the
person, so none of them is announced (no ``chat_result_ready``, no push); the
dispatcher raises one notification when it records the run's outcome. These
tests pin what each turn hands the notifiers, and that questions, approvals,
typed turns, user routines and ordinary chats are unchanged.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from ciao.models import PermissionRequestEvent
from ciao.schedules import is_care_schedule_id
from ciao.web.chat_broker import ChatStream
from ciao.web.schedule_dispatch import ScheduleDispatchHost, ScheduleDispatcher
from tests.test_chat_messages_interrupted import _make_manager
from tests.test_schedule_dispatch_boundary import (
    _FOLLOW_UP_RESULT,
    _LAUNCH_TURN,
    _archiving_host,
    _capture_run_records,
    _entry,
    _ScheduleHost,
    _until_read,
)

CARE_ID = "system-memory-curation@personal"


def test_the_care_id_rule_matches_bare_and_fanned_out_ids() -> None:
    assert is_care_schedule_id("system-memory-curation")
    assert is_care_schedule_id(CARE_ID)
    assert not is_care_schedule_id("daily-digest")
    assert not is_care_schedule_id("system-memory-curation-extra")
    assert not is_care_schedule_id("")


# ── The manager seam: what a turn end hands the notifiers ────────────────


def _setup(tmp_path: Path, *, schedule_id: str = CARE_ID):
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("Care", workspace="personal")
    chat = pcm.create_chat(project.project_id, title="Workspace care")
    chat.schedule_id = schedule_id
    chat.last_activity_at = "2026-11-28T06:00:00Z"
    chat.last_response_status = "success"
    events: list[dict[str, object]] = []
    pcm._events = SimpleNamespace(publish=events.append)  # type: ignore[assignment]
    pushes: list[tuple[str, str, str]] = []
    pcm.notify_result_cb = lambda cid, title, snippet: pushes.append((cid, title, snippet))
    pcm._push_delay_seconds = 0
    # The dispatcher owns the run until it announces the outcome.
    pcm.begin_care_run(chat.chat_id)
    return pcm, chat, events, pushes


def _turns(chat, *origins: str) -> None:
    chat.user_turn_count = len(origins)
    chat.user_turn_origin = {str(i): o for i, o in enumerate(origins) if o}


def _results(events: list[dict[str, object]]) -> list[dict[str, object]]:
    return [e for e in events if e.get("type") == "chat_result_ready"]


async def _turn_end(pcm, chat) -> None:
    pcm._announce_result_ready(chat.chat_id, chat.project_id, chat.title, "a step finished")
    await asyncio.sleep(0.05)


@pytest.mark.parametrize(
    "origins",
    [
        ("schedule",),  # the run's first reply
        ("schedule", "background_run"),  # a wake for a finished subagent
        ("schedule", "background_run", "background_run"),
    ],
)
async def test_a_care_run_turn_announces_nothing(tmp_path: Path, origins: tuple[str, ...]) -> None:
    pcm, chat, events, pushes = _setup(tmp_path)
    _turns(chat, *origins)
    await _turn_end(pcm, chat)
    assert _results(events) == [] and pushes == []


async def test_a_parked_care_result_released_by_the_drain_announces_nothing(tmp_path: Path) -> None:
    pcm, chat, events, pushes = _setup(tmp_path)
    _turns(chat, "schedule")
    pcm._park_result_announce(chat.chat_id, chat.project_id, chat.title, "interim")
    assert pcm._flush_result_announce(chat.chat_id) is True
    await asyncio.sleep(0.05)
    assert _results(events) == [] and pushes == []


@pytest.mark.parametrize(
    "origins",
    [
        ("schedule", ""),  # the person typed into the care chat
        ("schedule", "", "background_run"),  # and a subagent they asked for woke it
        ("restart_resume",),
    ],
)
async def test_a_turn_the_person_started_in_the_care_chat_is_announced(
    tmp_path: Path, origins: tuple[str, ...]
) -> None:
    pcm, chat, events, pushes = _setup(tmp_path)
    _turns(chat, *origins)
    await _turn_end(pcm, chat)
    assert len(_results(events)) == 1 and len(pushes) == 1


@pytest.mark.parametrize(
    ("schedule_id", "origins"),
    [
        ("daily-digest", ("schedule",)),  # a user routine
        ("daily-digest", ("schedule", "background_run")),
        ("", ("",)),  # an ordinary chat
        ("", ("", "background_run")),
    ],
)
async def test_routines_and_ordinary_chats_still_announce_every_result(
    tmp_path: Path, schedule_id: str, origins: tuple[str, ...]
) -> None:
    pcm, chat, events, pushes = _setup(tmp_path, schedule_id=schedule_id)
    _turns(chat, *origins)
    await _turn_end(pcm, chat)
    assert len(_results(events)) == 1 and len(pushes) == 1


async def test_the_run_end_notice_is_one_event_and_one_push(tmp_path: Path) -> None:
    pcm, chat, events, pushes = _setup(tmp_path)
    _turns(chat, "schedule", "background_run")
    # A clean care run is archived before its outcome is recorded.
    chat.archived = True
    pcm._park_result_announce(chat.chat_id, chat.project_id, chat.title, "interim")

    pcm.announce_schedule_run_end(chat.chat_id, "Workspace care finished: 7 of 7 steps done.")
    # Nothing the run left behind can notify a second time.
    assert pcm._flush_result_announce(chat.chat_id) is False
    await _turn_end(pcm, chat)

    (event,) = _results(events)
    assert event["chat_id"] == chat.chat_id
    assert event["snippet"] == "Workspace care finished: 7 of 7 steps done."
    assert event["memory_pass"] is False and event["quiet_loop_run"] is False
    assert pushes == [(chat.chat_id, "Workspace care", "Workspace care finished: 7 of 7 steps done.")]


@pytest.mark.parametrize(
    "origins",
    [
        ("schedule",),  # a question answered after the dispatcher stopped waiting
        ("schedule", "background_run"),  # a step still running past the cut-off wakes the chat
    ],
)
async def test_a_care_turn_after_the_run_is_over_is_announced(
    tmp_path: Path, origins: tuple[str, ...]
) -> None:
    pcm, chat, events, pushes = _setup(tmp_path)
    pcm.announce_schedule_run_end(chat.chat_id, "Workspace care was cut off.")
    pcm.end_care_run(chat.chat_id)
    _turns(chat, *origins)
    await _turn_end(pcm, chat)
    # The run notice, then this turn as an ordinary result.
    assert [e["snippet"] for e in _results(events)] == ["Workspace care was cut off.", "a step finished"]
    assert len(pushes) == 2


async def test_a_care_run_approval_or_question_still_notifies_once(tmp_path: Path) -> None:
    pcm, chat, _, _ = _setup(tmp_path)
    _turns(chat, "schedule", "background_run")
    approvals: list[tuple[str, str]] = []
    questions: list[str] = []
    pcm.notify_permission_cb = lambda cid, tool, message, rid: approvals.append((cid, tool))
    pcm.notify_question_cb = lambda cid, body: questions.append(cid)

    pcm._notify_permission(
        chat.chat_id, PermissionRequestEvent(
            type="permission_request", tool_name="Bash", message="ls", request_id="r-1"
        )
    )
    pcm._notify_question(chat.chat_id, json.dumps({"questions": [{"question": "Merge these notes?"}]}))

    assert approvals == [(chat.chat_id, "Bash")]
    assert questions == [chat.chat_id]


# ── The dispatcher seam: what the run end hands the notifier ─────────────


@pytest.fixture
def fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ciao.web.schedule_dispatch._FOLLOW_UP_POLL_S", 0.01)


REPORT = """# Workspace care 2026-11-28
- [x] inbox: Inbox triaged
- [x] people: People notes merged
- [ ] audit: Vault audit
## What needs you
1. Decide whether to merge the two Acme notes.
"""


def _write_report(host: _ScheduleHost, text: str = REPORT) -> Path:
    path = host._config.workspace_root / "vault" / "Workspace" / "Care-Report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


async def _dispatch(host: _ScheduleHost, schedule_id: str = CARE_ID) -> dict[str, object]:
    entry = dataclasses.replace(
        _entry(archive_policy="auto"), schedule_id=schedule_id,
        scope="system" if is_care_schedule_id(schedule_id) else "user",
    )
    dispatcher = ScheduleDispatcher(cast(ScheduleDispatchHost, host))
    return await asyncio.wait_for(
        dispatcher.dispatch_schedule(entry, "Run the routine.", "opus", "auto", "opencode"),
        timeout=5,
    )


def _wake_then_finish(host: _ScheduleHost, *, report: bool = True):
    async def wake() -> None:
        await asyncio.sleep(0.05)
        if report:
            _write_report(host)
        chat = host._chats["chat-1"]
        chat.user_turn_origin[str(chat.user_turn_count)] = "background_run"
        chat.user_turn_count += 1
        stream = ChatStream()
        host._broker.register("chat-1", stream)
        host.follow_up_pending = False
        stream.publish(_FOLLOW_UP_RESULT)
        await _until_read(stream)
        stream.finish()

    return asyncio.create_task(wake())


@pytest.mark.usefixtures("fast_poll")
async def test_a_finished_care_run_notifies_once_with_its_step_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _archiving_host(tmp_path, _LAUNCH_TURN)
    host.follow_up_pending = True
    _capture_run_records(monkeypatch)
    task = _wake_then_finish(host)

    result = await _dispatch(host)
    await task

    assert result["status"] == "ok" and host.archives == ["chat-1"]
    assert host.run_end_notices == [
        ("chat-1", "Workspace care finished: 2 of 3 steps done, 1 item needs you.")
    ]


@pytest.mark.usefixtures("fast_poll")
async def test_a_report_from_an_earlier_run_gives_no_step_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _archiving_host(tmp_path, _LAUNCH_TURN)
    host.follow_up_pending = True
    _capture_run_records(monkeypatch)
    old = _write_report(host)
    os.utime(old, (1_000_000_000, 1_000_000_000))
    task = _wake_then_finish(host, report=False)

    await _dispatch(host)
    await task

    assert host.run_end_notices == [("chat-1", "Workspace care finished.")]


@pytest.mark.usefixtures("fast_poll")
async def test_a_cut_off_care_run_notifies_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ciao.web.chat_service.SCHEDULE_FOLLOW_UP_MAX_S", 0.05)
    host = _archiving_host(tmp_path, _LAUNCH_TURN)
    host.follow_up_pending = True  # the subagent never finishes
    _capture_run_records(monkeypatch)

    result = await _dispatch(host)

    assert result["status"] == "error"
    ((chat_id, text),) = host.run_end_notices
    assert chat_id == "chat-1"
    assert text.startswith("Workspace care was cut off: a step was still running after ")
    # The run is no longer in flight, so a step still running wakes the chat audibly.
    assert host.care_calls == [("begin", "chat-1"), ("end", "chat-1")]
    assert host.care_in_flight == set()


@pytest.mark.usefixtures("fast_poll")
async def test_a_stopped_care_run_notifies_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = _archiving_host(tmp_path, _LAUNCH_TURN)
    host.follow_up_pending = True
    _capture_run_records(monkeypatch)

    async def owner_stops() -> None:
        await asyncio.sleep(0.05)
        host.stops += 1
        host.follow_up_pending = False

    task = asyncio.create_task(owner_stops())
    result = await _dispatch(host)
    await task

    assert result["status"] == "skipped"
    assert host.run_end_notices == [("chat-1", "Workspace care was stopped.")]


async def test_a_failed_care_run_notifies_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = _ScheduleHost(tmp_path, [{"type": "result", "text": "usage limit reached", "is_error": True}])
    _capture_run_records(monkeypatch)

    result = await _dispatch(host)

    assert result["status"] == "error"
    assert host.run_end_notices == [("chat-1", "Workspace care failed. Open the chat to see why.")]


@pytest.mark.usefixtures("fast_poll")
async def test_a_care_run_waiting_on_an_approval_adds_no_run_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The approval card notified on its own; the run adds nothing on top of it.
    host = _archiving_host(tmp_path, _LAUNCH_TURN)
    host.follow_up_pending = True
    _capture_run_records(monkeypatch)
    stream = ChatStream(background=True)
    stream.publish({"type": "permission_request", "tool_name": "Bash"})
    host._broker.register("chat-1", stream)

    result = await _dispatch(host)

    assert result["status"] == "skipped"
    assert host.run_end_notices == []


@pytest.mark.usefixtures("fast_poll")
async def test_a_care_run_the_person_took_over_adds_no_run_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _archiving_host(tmp_path, _LAUNCH_TURN)
    host.follow_up_pending = True
    _capture_run_records(monkeypatch)
    owner_turn = ChatStream("what is happening?")

    async def owner_types() -> None:
        await asyncio.sleep(0.05)
        host._chats["chat-1"].user_turn_count += 1
        host._broker.register("chat-1", owner_turn)

    task = asyncio.create_task(owner_types())
    await _dispatch(host)
    await task

    assert host.run_end_notices == []


@pytest.mark.usefixtures("fast_poll")
async def test_a_user_routine_raises_no_run_notice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    host = _archiving_host(tmp_path, _LAUNCH_TURN)
    host.follow_up_pending = True
    _capture_run_records(monkeypatch)
    task = _wake_then_finish(host)

    result = await _dispatch(host, schedule_id="daily-digest")
    await task

    assert result["status"] == "ok"
    assert host.run_end_notices == []
    assert host.care_calls == []


async def test_a_care_run_is_released_when_its_dispatch_is_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _archiving_host(tmp_path, _LAUNCH_TURN)
    host.follow_up_pending = True
    _capture_run_records(monkeypatch)
    entry = dataclasses.replace(_entry(archive_policy="auto"), schedule_id=CARE_ID, scope="system")
    task = asyncio.create_task(
        ScheduleDispatcher(cast(ScheduleDispatchHost, host)).dispatch_schedule(
            entry, "Run the routine.", "opus", "auto", "opencode"
        )
    )
    await asyncio.sleep(0.05)
    assert host.care_in_flight == {"chat-1"}
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert host.care_in_flight == set() and host.run_end_notices == []


def test_a_denied_care_run_names_the_denial() -> None:
    from ciao.web import chat_service
    from ciao.web.schedule_dispatch import care_run_end_text

    outcome = chat_service.ScheduleRunOutcome(completed=True, permission_denied="Bash")
    status, error = chat_service._schedule_dispatch_status(outcome)
    assert status == "error" and error
    assert care_run_end_text(outcome, status, error=error) == f"Workspace care failed. {error}"
