"""Recorded provider frames of one background shell, through a real AcpSession, to the end notice.

The fixtures are trimmed captures (``tests/fixtures/acp/shell_*_wire_background.jsonl``): the shell
call's own frames, with the local user name and project path replaced. The process table is the
only fake: OpenCode's end is the pid leaving it, Claude's end is its ``terminal_exit``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from ciao import shell_wake
from ciao.models import ShellFinishedEvent
from ciao.providers.acp import session as session_module
from ciao.providers.acp.agents import CLAUDE, OPENCODE
from ciao.providers.acp.normalize import UpdateNormalizer
from ciao.providers.acp.session import AcpSession
from ciao.providers.acp.shell_liveness import ShellLiveness

FIXTURES = Path(__file__).parent / "fixtures" / "acp"
STARTED = "Thu Nov 20 10:00:00 2026"


def _frames(name: str) -> list[dict[str, Any]]:
    return [json.loads(line)["params"] for line in (FIXTURES / name).read_text().splitlines() if line.strip()]


def _session(spec: Any, monkeypatch: pytest.MonkeyPatch, table: dict[int, tuple[int, str]], ended: list[Any]) -> AcpSession:
    monkeypatch.setattr(session_module, "process_table", lambda: dict(table))
    monkeypatch.setattr(session_module, "CHECK_INTERVAL_S", 0.01)
    session = AcpSession.__new__(AcpSession)
    session.spec = spec
    session.session_id = ""  # the frames' session check is skipped
    session._normalizer = UpdateNormalizer(spec, "")
    session._replaying = False
    session._prompt_active = False
    session._late_open = False
    session._shells = ShellLiveness()
    session._shell_task = None
    session._shell_commands = {}
    session._deferred_ends = []
    session._end_tasks = set()
    session._ended_shells = {}
    session.continuation_hook = None
    session._late_speaking = False
    # What reaches the late queue is what the chat's drain turns into _note_shell_finished.
    session._touch_late = lambda events, *, substantive: ended.extend(  # type: ignore[method-assign]
        event for event in events if isinstance(event, ShellFinishedEvent)
    )
    return session


async def _settle(session: AcpSession) -> None:
    while session._end_tasks:
        await asyncio.wait(list(session._end_tasks), timeout=2)


def _notice_text(events: list[ShellFinishedEvent]) -> str:
    return shell_wake.shell_finished_prompt(events)


@pytest.mark.asyncio
async def test_opencode_wire_pid_ends_the_shell_with_no_exit_code_and_no_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    table = {22660: (1, STARTED)}
    ended: list[ShellFinishedEvent] = []
    session = _session(OPENCODE, monkeypatch, table, ended)
    frames = _frames("shell_opencode_wire_background.jsonl")
    for params in frames:
        session.on_update(params)
    # The pid came from the acknowledgement's structured metadata, so the shell is being watched.
    assert session._shells.polling
    # The process ends; launchd (pid 1) is still there, as it is in a real table (an empty one is a failed ps).
    del table[22660]
    table[1] = (0, "boot")
    await asyncio.wait_for(session._shell_task, timeout=2)
    await _settle(session)
    (notice,) = ended
    assert notice.tool_use_id == "call_fd55161349d04c0cacb10e22"
    assert notice.command == "sleep 6; echo done-marker ."  # raw; the notice label is the redacted form
    assert notice.exit_code is None  # the process table cannot tell how a shell ended
    assert notice.output_tail is None  # OpenCode sends no structured output path: no tail
    text = _notice_text(ended)
    assert "exit status unknown" in text and "log: None" in text
    assert "(no output)" not in text


@pytest.mark.asyncio
async def test_claude_wire_terminal_exit_ends_the_shell_once_and_is_never_polled(monkeypatch: pytest.MonkeyPatch) -> None:
    table = {1: (0, "boot")}
    ended: list[ShellFinishedEvent] = []
    session = _session(CLAUDE, monkeypatch, table, ended)
    for params in _frames("shell_claude_wire_background.jsonl"):
        session.on_update(params)
    await _settle(session)
    (notice,) = ended
    assert notice.tool_use_id == "toolu_01Y1rtNLi2z9mzENhKthat2r"
    assert notice.command == "sleep 6; echo done-marker"
    # The terminal_exit carries a null exit code: the notice says the status is unknown.
    assert notice.exit_code is None
    assert notice.output_tail is None  # Claude sends the path only as text: never read
    assert session._shell_task is None and not session._shells.polling  # never polled
    assert not session._shells.tracking  # settled by the terminal_exit, not left running
    assert "exit status unknown" in _notice_text(ended)
