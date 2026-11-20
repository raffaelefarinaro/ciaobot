"""Background shell liveness: a launched shell settles when its process ends, never on a pid reuse.

The process table is a fake here: nothing is signalled and no real process is started.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ciao.models import ToolUseEvent
from ciao.providers.acp import session as session_module
from ciao.providers.acp.session import AcpSession
from ciao.providers.acp.shell_liveness import MAX_TRACKED, ShellLiveness

STARTED = "Thu Nov 20 10:00:00 2026"
LATER = "Thu Nov 20 11:30:00 2026"


def _ids(settled: list[tuple[str, object]]) -> list[str]:
    return [call_id for call_id, _ in settled]


def test_a_live_pid_with_its_start_time_is_not_settled() -> None:
    shells = ShellLiveness()
    assert shells.track("call-1", 4242, {4242: (1, STARTED)})
    assert shells.settled({4242: (1, STARTED), 1: (0, "boot")}) == []
    assert shells.tracking


def test_a_vanished_pid_settles_once_and_stops_being_tracked() -> None:
    shells = ShellLiveness()
    shells.track("call-1", 4242, {4242: (1, STARTED)})
    assert _ids(shells.settled({1: (0, "boot")})) == ["call-1"]
    assert not shells.tracking
    assert shells.settled({1: (0, "boot")}) == []


def test_a_reused_pid_with_a_different_start_time_counts_as_gone() -> None:
    shells = ShellLiveness()
    shells.track("call-1", 4242, {4242: (1, STARTED)})
    assert _ids(shells.settled({4242: (1, LATER)})) == ["call-1"]


def test_an_empty_table_settles_nothing_so_a_failed_ps_cannot_end_every_shell() -> None:
    shells = ShellLiveness()
    shells.track("call-1", 4242, {4242: (1, STARTED)})
    assert shells.settled({}) == []
    assert shells.tracking


def test_a_pid_absent_at_launch_is_not_tracked() -> None:
    shells = ShellLiveness()
    assert shells.track("call-1", 4242, {1: (0, "boot")}) is False
    assert not shells.tracking


def test_tracking_is_capped_and_a_repeat_is_idempotent() -> None:
    shells = ShellLiveness()
    table = {pid: (1, STARTED) for pid in range(100, 100 + MAX_TRACKED + 5)}
    accepted = [shells.track(f"call-{pid}", pid, table) for pid in table]
    assert accepted.count(True) == MAX_TRACKED
    assert shells.track("call-100", 100, table) is True


def _session_stub() -> AcpSession:
    # Only the state the shell watch touches; the rest of a session is not needed here.
    session = AcpSession.__new__(AcpSession)
    session._shells = ShellLiveness()
    session._shell_task = None
    session._shell_commands = {}
    return session


def _background_ack(call_id: str, pid: int) -> ToolUseEvent:
    return ToolUseEvent(type="tool_result", tool_use_id=call_id, background=True, shell_pid=pid)


@pytest.mark.asyncio
async def test_the_watch_emits_a_settle_through_the_session_path_when_the_process_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table: dict[int, tuple[int, str]] = {4242: (1, STARTED)}
    monkeypatch.setattr(session_module, "process_table", lambda: dict(table))
    monkeypatch.setattr(session_module, "CHECK_INTERVAL_S", 0.01)
    session = _session_stub()
    emitted: list[tuple[list[Any], str]] = []
    session._note_update = lambda events, *, kind: emitted.append((events, kind))  # type: ignore[method-assign]
    ended: list[list[Any]] = []
    session._touch_late = lambda events, *, substantive: ended.append(events)  # type: ignore[method-assign]

    session._track_shells([_background_ack("call-bg", 4242)])
    assert session._shell_task is not None
    await asyncio.sleep(0.03)
    assert emitted == []  # still alive

    table.clear()
    table[1] = (0, "boot")  # launchd still there: a real table is never empty
    await asyncio.wait_for(session._shell_task, timeout=2)
    (events, kind), = emitted
    assert kind == "shell_settle"
    (settle,) = events
    assert (settle.type, settle.tool_use_id, settle.background, settle.is_error, settle.exit_code) == (
        "tool_result", "call-bg", False, False, None,
    )
    assert not session._shells.tracking
    # The end event goes once, on the late path, and names the shell (no output file here).
    (end,) = [e for batch in ended for e in batch]
    assert (end.type, end.tool_use_id, end.output_tail) == ("shell_finished", "call-bg", None)


@pytest.mark.asyncio
async def test_a_pid_reused_during_the_watch_is_settled_not_kept_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    table: dict[int, tuple[int, str]] = {4242: (1, STARTED)}
    monkeypatch.setattr(session_module, "process_table", lambda: dict(table))
    monkeypatch.setattr(session_module, "CHECK_INTERVAL_S", 0.01)
    session = _session_stub()
    emitted: list[str] = []
    session._note_update = lambda events, *, kind: emitted.append(events[0].tool_use_id)  # type: ignore[method-assign,index]
    session._touch_late = lambda events, *, substantive: None  # type: ignore[method-assign]

    session._track_shells([_background_ack("call-bg", 4242)])
    table[4242] = (1, LATER)
    await asyncio.wait_for(session._shell_task, timeout=2)
    assert emitted == ["call-bg"]


def test_a_foreground_result_or_a_pidless_ack_starts_no_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_module, "process_table", lambda: {4242: (1, STARTED)})
    session = _session_stub()
    session._track_shells([
        ToolUseEvent(type="tool_result", tool_use_id="fg", exit_code=0),
        ToolUseEvent(type="tool_result", tool_use_id="bg", background=True, shell_pid=None),
    ])
    assert session._shell_task is None
    assert not session._shells.tracking


@pytest.mark.asyncio
async def test_a_launched_shell_keeps_its_command_and_output_path_for_the_end_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_module, "process_table", lambda: {4242: (1, STARTED)})
    session = _session_stub()
    session._track_shells([
        ToolUseEvent(type="tool_use", tool_use_id="call-bg", tool_input="npm run dev", shell=True, shell_background=True),
    ])
    session._track_shells([
        ToolUseEvent(
            type="tool_result", tool_use_id="call-bg", background=True, shell_pid=4242,
            output_path="/tmp/out.log",
        ),
    ])
    (_, shell), = [(c, s) for c, s in session._shells._tracked.items()]
    assert (shell.command, shell.output_path) == ("npm run dev", "/tmp/out.log")
    assert "call-bg" not in session._shell_commands
    assert session._shell_task is not None
    session._shell_task.cancel()


@pytest.mark.asyncio
async def test_a_settled_shell_is_never_tracked_again_from_a_replayed_acknowledgement() -> None:
    shells = ShellLiveness()
    assert shells.track("call-1", 4242, {4242: (1, STARTED)})
    assert _ids(shells.settled({1: (0, "boot")})) == ["call-1"]
    assert shells.track("call-1", 4242, {4242: (1, STARTED)}) is False
    assert not shells.tracking
