"""Stale ends, late exits, teardown and bounded claims, on the real session and drain.

Each scenario runs the real AcpSession (``_end_shell``, ``_touch_late``, ``_close_late``, the late
queue the drain reads) and ``ChatStreaming.drain_between_turns``. The helpers are the same ones the
continuation tests use.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from ciao.models import ShellFinishedEvent
from ciao.web import project_chats
from tests.test_continuation_claim_session import _end, _session, _text
from tests.test_shell_native_continuation import NATIVE_TEXT, _run, _setup


def _prompts(manager: Any) -> list[str]:
    return [prompt for _, prompt, _ in manager._recorder.started]


def test_a_shell_woken_by_the_host_is_not_announced_again_by_a_later_continuation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Finding 1: A is woken by the host after its window; a later continuation must claim only B.
    manager, session, recorder, broker = _setup(monkeypatch, window=0.1)
    _run(manager, session, recorder, broker, [
        lambda: _end(session, "call-a", "job a"),
        0.25,  # A's window closes; the host wakes for A
        lambda: _end(session, "call-b", "job b"),
        lambda: session._touch_late([_text(NATIVE_TEXT)], substantive=True),
        lambda: session._close_late(),
    ], after=0.2)
    assert len(_prompts(manager)) == 1 and "job a" in _prompts(manager)[0]  # A's own wake, once
    assert len(recorder.turns) == 1
    assert "job b" in recorder.turns[0]["prompt"] and "job a" not in recorder.turns[0]["prompt"]


def test_a_shell_that_exits_while_its_continuation_is_open_gets_no_second_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Finding 2: the continuation already reported A; C exits 300 ms after the continuation began.
    manager, session, recorder, broker = _setup(monkeypatch, window=0.1)
    _run(manager, session, recorder, broker, [
        lambda: _end(session, "call-a", "job a"),
        lambda: session._touch_late([_text(NATIVE_TEXT)], substantive=True),  # claims A: one card
        0.3,
        lambda: _end(session, "call-c", "job c"),  # exits while the continuation is still open
        lambda: session._close_late(),
    ], after=0.3)
    assert _prompts(manager) == []  # no wake prompt: the agent already answered
    assert len(recorder.turns) == 1  # one reply, carrying one card for A
    assert "job a" in recorder.turns[0]["prompt"] and "job c" not in recorder.turns[0]["prompt"]


def test_closing_or_discarding_a_session_forgets_its_unclaimed_ends() -> None:
    # Finding 3: the session's own record does not outlive it (the chat-side state is cleared elsewhere).
    session = _session()
    session._ended_shells = {"call-a": ShellFinishedEvent(type="shell_finished", tool_use_id="call-a")}
    session._cap_object = lambda *args: False  # type: ignore[method-assign]
    session._connection = SimpleNamespace()
    asyncio.run(session.close())
    assert session._ended_shells == {}

    session._ended_shells = {"call-b": ShellFinishedEvent(type="shell_finished", tool_use_id="call-b")}
    asyncio.run(session.discard())
    assert session._ended_shells == {}


def test_a_claimed_id_is_dropped_once_its_end_is_dequeued_and_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    # Finding 4: the claimed set holds an id only until its end has been seen and skipped.
    from tests.test_shell_wake import _chat, _manager

    monkeypatch.setattr(project_chats, "SHELL_FINISHED_WINDOW_S", 30.0)
    manager = _manager(_chat(model="m", mode="bypass", provider="opencode", session_id="", title="t"))
    manager._claimed_shell_ids = {"chat-1": {"call-a"}}
    manager._note_shell_finished("chat-1", ShellFinishedEvent(type="shell_finished", tool_use_id="call-a"))
    assert "call-a" not in manager._claimed_shell_ids.get("chat-1", set())
    assert manager._shell_finished_pending == {}


def test_the_claimed_id_set_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_shell_wake import _chat, _manager

    monkeypatch.setattr(project_chats, "SHELL_CLAIMED_IDS_CAP", 4)
    manager = _manager(_chat())
    manager._providers = {"chat-1": SimpleNamespace(continues_after_shell=True)}
    for index in range(10):
        manager._claimed_shell_ids.setdefault("chat-1", set()).add(f"call-{index}")
        manager._trim_claimed_shell_ids("chat-1")
    assert len(manager._claimed_shell_ids["chat-1"]) <= 4
