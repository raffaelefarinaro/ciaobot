"""The session makes the continuation claim itself, independent of what the drain has dequeued.

Each test drives the real AcpSession paths (``_end_shell``, ``_touch_late``, ``_close_late``) and the
real ``_late_events`` generator the drain reads. Only the chat state and the journal are stand-ins.
"""

from __future__ import annotations

import asyncio
import logging
import time
from functools import partial
from types import SimpleNamespace
from typing import Any

import pytest

from ciao.models import AssistantTextDelta, ShellFinishedEvent, WakeNoticeEvent
from ciao.providers.acp.session import AcpSession, TurnText
from ciao.providers.acp.shell_liveness import ShellLiveness, TrackedShell
from ciao.web import project_chats

NATIVE = "The background command finished and printed `done-marker`."


class _Journal:
    def __init__(self, fail_user: bool = False) -> None:
        self.rows: list[tuple[str, Any]] = []
        self.fail_user = fail_user

    def user(self, text: str, *, turn_origin: str = "") -> None:
        if self.fail_user:
            raise OSError("journal disk full")
        self.rows.append(("user", (text, turn_origin)))

    def event(self, payload: Any) -> None:
        self.rows.append(("event", payload))


def _session(journal: _Journal | None = None) -> AcpSession:
    session = AcpSession.__new__(AcpSession)
    session.shells_hook = None
    session._shells_reported = 0
    session._journal = journal or _Journal()
    session._late_queue = asyncio.Queue()
    session._late_text = TurnText()
    session._late_open = False
    session._late_substantive = False
    session._late_speaking = False
    session._quiet_task = None
    session.spec = SimpleNamespace(quiet_period_s=60.0)
    session.continuation_hook = None
    session._shells = ShellLiveness()
    session._shell_commands = {}
    session._subagent_calls = {}
    session._shell_task = None
    session._prompt_active = False
    session._deferred_ends = []
    session._end_tasks = set()
    session._ended_shells = {}
    session.session_id = "ses_test"
    session._current_model = lambda: ""  # type: ignore[method-assign]
    session._normalizer = SimpleNamespace(usage={})
    session._arm_quiet = lambda: None  # type: ignore[method-assign]  # the quiet timer is not under test
    return session


def _end(session: AcpSession, call_id: str, command: str) -> None:
    """A background shell ends: the session records and emits its end, as the poll or a settle does."""
    shell = TrackedShell(pid=0, started="", command=command, output_path="", launched_at=time.monotonic())
    session._end_shell(call_id, shell, None)


def _text(text: str) -> AssistantTextDelta:
    return AssistantTextDelta(type="text_delta", text=text)


def _queued(session: AcpSession) -> list[Any]:
    out = []
    while not session._late_queue.empty():
        out.append(session._late_queue.get_nowait())
    return out


def _manager(monkeypatch: pytest.MonkeyPatch) -> Any:
    from tests.test_shell_wake import _chat, _manager as make

    monkeypatch.setattr(project_chats, "SHELL_FINISHED_WINDOW_S", 30.0)
    manager = make(_chat(model="m", mode="bypass", provider="opencode", session_id="", title="t"))
    manager._providers = {"chat-1": SimpleNamespace(continues_after_shell=True)}
    return manager


def test_a_continuation_that_speaks_before_the_drain_dequeues_the_shell_end_is_still_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Finding 1: the shell end is recorded and queued, but nothing has dequeued it when the text comes.
    manager = _manager(monkeypatch)
    journal = _Journal()

    async def run() -> list[Any]:
        session = _session(journal)
        session.continuation_hook = partial(manager._continuation_started, "chat-1")
        _end(session, "call-1", "sleep 6; echo done-marker .")
        await asyncio.sleep(0.05)  # the tail read and the end's delivery have run; the drain has not
        session._touch_late([_text(NATIVE)], substantive=True)
        return _queued(session)

    queued = asyncio.run(run())
    assert journal.rows[0][0] == "user" and journal.rows[0][1][1] == "background_run"
    assert journal.rows[0][1][0].startswith("[Ciaobot] 1 background run finished.")
    assert any(isinstance(item, WakeNoticeEvent) for item in queued)
    # The drain dequeues the shell end later: it is answered, so no wake of ours is sent.
    shell_end = next(item for item in queued if isinstance(item, ShellFinishedEvent))
    manager._note_shell_finished("chat-1", shell_end)
    assert manager._recorder.started == [] and manager._recorder.queued == []


def test_with_no_drain_running_the_claim_and_the_journal_still_happen(monkeypatch: pytest.MonkeyPatch) -> None:
    # Finding 1: a foreground turn runs, so no drain consumes the queue; the continuation still claims.
    manager = _manager(monkeypatch)
    journal = _Journal()

    async def run() -> None:
        session = _session(journal)
        session.continuation_hook = partial(manager._continuation_started, "chat-1")
        _end(session, "call-1", "make")
        session._touch_late([_text(NATIVE)], substantive=True)

    asyncio.run(run())
    assert journal.rows[0][0] == "user" and journal.rows[0][1][1] == "background_run"
    assert "call-1" in manager._claimed_shell_ids.get("chat-1", set())


def test_a_raising_hook_keeps_the_reply_journaled_and_queued(caplog: pytest.LogCaptureFixture) -> None:
    # Finding 2: the reply is never lost to the hook's error, and the error is logged.
    journal = _Journal()
    session = _session(journal)

    def broken(ended: Any) -> str:
        raise RuntimeError("hook broke")

    session.continuation_hook = broken

    async def run() -> None:
        _end(session, "call-1", "make")  # a shell end the continuation could answer
        with caplog.at_level(logging.ERROR):
            session._touch_late([_text(NATIVE)], substantive=True)

    asyncio.run(run())
    assert [row[0] for row in journal.rows] == ["event"]
    assert [item.text for item in _queued(session) if isinstance(item, AssistantTextDelta)] == [NATIVE]
    assert any(record.exc_info for record in caplog.records)


def test_a_raising_journal_write_keeps_the_reply_queued(caplog: pytest.LogCaptureFixture) -> None:
    # Finding 2: a failed notice write must not drop the reply, and it is logged.
    session = _session(_Journal(fail_user=True))
    session.continuation_hook = lambda ended: "[Ciaobot] 1 background run finished."

    async def run() -> None:
        _end(session, "call-1", "make")
        with caplog.at_level(logging.ERROR):
            session._touch_late([_text(NATIVE)], substantive=True)

    asyncio.run(run())
    assert [item.text for item in _queued(session) if isinstance(item, AssistantTextDelta)] == [NATIVE]
    assert any(record.exc_info for record in caplog.records)


@pytest.mark.parametrize("notice", [None, ""])
def test_a_hook_with_no_notice_journals_nothing_extra(notice: str | None) -> None:
    journal = _Journal()
    session = _session(journal)
    session.continuation_hook = lambda ended: notice
    session._touch_late([_text(NATIVE)], substantive=True)
    assert [row[0] for row in journal.rows] == ["event"]


def test_the_user_notice_row_precedes_the_first_reply_row(monkeypatch: pytest.MonkeyPatch) -> None:
    # Finding 5: the order in the journal is the order the reader sees.
    manager = _manager(monkeypatch)
    journal = _Journal()

    async def run() -> None:
        session = _session(journal)
        session.continuation_hook = partial(manager._continuation_started, "chat-1")
        _end(session, "call-1", "make")
        session._touch_late([_text(NATIVE)], substantive=True)

    asyncio.run(run())
    kinds = [row[0] for row in journal.rows]
    assert kinds.index("user") < kinds.index("event")


def test_a_second_continuation_after_a_close_is_claimed_for_its_own_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    # Finding 5: after the first continuation closed, the next shell end is answered by the next one.
    manager = _manager(monkeypatch)
    journal = _Journal()

    async def run() -> None:
        session = _session(journal)
        session.continuation_hook = partial(manager._continuation_started, "chat-1")
        _end(session, "call-1", "job one")
        session._touch_late([_text(NATIVE)], substantive=True)
        session._close_late()
        _end(session, "call-2", "job two")
        session._touch_late([_text("again")], substantive=True)

    asyncio.run(run())
    user_rows = [row[1][0] for row in journal.rows if row[0] == "user"]
    assert len(user_rows) == 2
    assert "job one" in user_rows[0] and "job two" in user_rows[1]


def test_a_shell_whose_process_already_ended_is_claimed_when_the_continuation_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The poll has not noticed the end yet, but the process is gone: the continuation claims it.
    from ciao.providers.acp import session as session_module

    manager = _manager(monkeypatch)
    journal = _Journal()
    monkeypatch.setattr(session_module, "process_table", lambda: {1: (0, "boot")})

    async def run() -> None:
        session = _session(journal)
        session.continuation_hook = partial(manager._continuation_started, "chat-1")
        session._shells.track("call-1", 4242, {4242: (1, "Thu Nov 20 10:00:00 2026")}, command="make")
        session._touch_late([_text(NATIVE)], substantive=True)

    asyncio.run(run())
    assert journal.rows[0][0] == "user" and journal.rows[0][1][0].startswith("[Ciaobot] 1 background run finished.")
    assert "call-1" in manager._claimed_shell_ids.get("chat-1", set())
