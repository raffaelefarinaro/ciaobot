"""A provider's own continuation after a background shell: one turn for the shell, no wake of ours.

Every scenario drives the real AcpSession (``_end_shell``, ``_touch_late``, ``_close_late``) and the
real ``ChatStreaming.drain_between_turns`` reading the session's late queue. Only the chat's
transcript and stream are stand-ins. The fixture is a trimmed capture of the OpenCode continuation.
"""

from __future__ import annotations

import asyncio
import json
import time
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ciao.models import AssistantTextDelta, ResultEvent, ToolUseEvent
from ciao.web import chat_streaming, project_chats
from ciao.web.chat_streaming import ChatStreaming
from tests.test_continuation_claim_session import _end, _session

FIXTURE = Path(__file__).parent / "fixtures" / "acp" / "shell_opencode_native_continuation.jsonl"
NATIVE_TEXT = "The background command finished and printed `done-marker`."
WAKE_NOTICE = "The agent continued after background work finished."


def _continuation_text() -> str:
    parts = []
    for line in FIXTURE.read_text().splitlines():
        update = json.loads(line)["params"]["update"]
        if update.get("sessionUpdate") == "agent_message_chunk":
            parts.append(update["content"]["text"])
    return "".join(parts)


class _Recorder:
    def __init__(self) -> None:
        self.turns: list[dict[str, Any]] = []

    def record_turn(self, request: Any, *, response_text: str, origin: str, input_kind: str, **_: Any) -> None:
        self.turns.append({"prompt": request.prompt, "response": response_text, "origin": origin})


class _Broker:
    """The chat's stream owner: a foreground turn that takes the drain over is set here."""

    owner: Any = None

    def get(self, chat_id: str) -> Any:
        return self.owner

    def register(self, chat_id: str, stream: Any) -> None:
        return None

    def clear(self, chat_id: str, stream: Any) -> None:
        return None


class _FakeStream:
    """Records what the drain publishes to the live stream, in order."""

    published: list[dict[str, Any]] = []

    def __init__(self, background: bool = False) -> None:
        self.background = background

    def publish(self, payload: dict[str, Any]) -> None:
        _FakeStream.published.append(payload)

    def __getattr__(self, name: str) -> Any:
        return lambda *args, **kwargs: None


class _Host:
    def __init__(self, manager: Any, session: Any, recorder: _Recorder, broker: _Broker) -> None:
        self._manager = manager
        self._transcripts = recorder
        self._broker = broker
        self._providers = {"chat-1": SimpleNamespace(drain_events=session._late_events)}
        self._chats = manager._chats
        self._config = SimpleNamespace(workspace_root=Path("/tmp"))

    def _agent_root_for_chat(self, chat_id: str) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        if name in ("_note_shell_finished", "_take_claimed_notice", "_keep_unsent_echo", "_take_unsent_echo",
                    "_deliver_wake", "_events"):
            return getattr(self._manager, name)
        return lambda *args, **kwargs: None


def _setup(monkeypatch: pytest.MonkeyPatch, window: float, flagged: bool = True) -> tuple[Any, Any, _Recorder, _Broker]:
    from tests.test_shell_wake import _chat, _manager

    monkeypatch.setattr(project_chats, "SHELL_FINISHED_WINDOW_S", window)
    monkeypatch.setattr(chat_streaming, "ChatStream", _FakeStream)
    _FakeStream.published = []
    chat = _chat(model="m", mode="bypass", provider="opencode", session_id="", title="t")
    manager = _manager(chat)
    manager._providers = {"chat-1": SimpleNamespace(continues_after_shell=flagged)}
    session = _session()
    session.continuation_hook = partial(manager._continuation_started, "chat-1")
    return manager, session, _Recorder(), _Broker()


def _text(text: str) -> AssistantTextDelta:
    return AssistantTextDelta(type="text_delta", text=text)


def _final(text: str, wake_notice: str = WAKE_NOTICE) -> ResultEvent:
    return ResultEvent(type="result", result=text, origin="task-notification", wake_notice=wake_notice)


def _run(manager: Any, session: Any, recorder: _Recorder, broker: _Broker, steps: list[Any], after: float = 0.0) -> None:
    """Run the steps in order while the real drain reads the session's queue; the timers share the loop.

    A step is a callable (a session operation), a float (a pause) or an ``("drain",)`` to restart the drain.
    """
    host = _Host(manager, session, recorder, broker)

    async def main() -> None:
        drain = asyncio.create_task(ChatStreaming(host).drain_between_turns("chat-1", "p"))  # type: ignore[arg-type]
        for step in steps:
            if isinstance(step, float):
                await asyncio.sleep(step)
            elif isinstance(step, tuple) and step == ("drain",):
                if drain.done():
                    drain = asyncio.create_task(ChatStreaming(host).drain_between_turns("chat-1", "p"))  # type: ignore[arg-type]
            else:
                step()
                await asyncio.sleep(0)  # let the drain run between two session events
        await asyncio.sleep(after)
        drain.cancel()
        await asyncio.gather(drain, return_exceptions=True)

    asyncio.run(main())


def test_the_fixture_continuation_is_the_native_text() -> None:
    assert _continuation_text() == NATIVE_TEXT


def test_a_turn_boundary_splits_the_reply_text_instead_of_gluing_it() -> None:
    from ciao.web.transcript_service import _render_acp_journal

    records = [
        {"type": "text_delta", "text": "started"},
        {"type": "result", "stop_reason": "end_turn"},
        {"type": "text_delta", "text": "resumed"},
        {"type": "result", "stop_reason": "end_turn"},
    ]
    rows = _render_acp_journal(records, SimpleNamespace(), start_user_idx=0, workspace_root=None, agent_root=None)
    assert [row["content"] for row in rows if row.get("role") == "assistant"] == ["started", "resumed"]


def test_a_continuation_that_runs_past_the_window_is_one_turn_and_no_wake(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, session, recorder, broker = _setup(monkeypatch, window=0.15)
    _run(manager, session, recorder, broker, [
        lambda: _end(session, "call-1", "sleep 6; echo done-marker ."),
        0.05, lambda: session._touch_late([_text(NATIVE_TEXT)], substantive=True),
        0.3, lambda: session._close_late(),
    ], after=0.2)
    assert [turn["response"] for turn in recorder.turns] == [NATIVE_TEXT]
    assert recorder.turns[0]["prompt"].splitlines()[0] == "[Ciaobot] 1 background run finished."
    assert recorder.turns[0]["origin"] == "background_run"
    assert manager._recorder.started == [] and manager._recorder.queued == []


def test_after_the_window_with_no_continuation_exactly_one_wake_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, session, recorder, broker = _setup(monkeypatch, window=0.05)
    _run(manager, session, recorder, broker, [lambda: _end(session, "call-x", "make")], after=0.2)
    assert len(manager._recorder.started) == 1


def test_an_unrelated_subagent_notification_does_not_claim_the_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    # A subagent's completion (a tool result, no reply text) closes its late turn.
    manager, session, recorder, broker = _setup(monkeypatch, window=0.2)
    subagent = ToolUseEvent(type="tool_result", tool_use_id="task-9", tool_name="task", exit_code=0)
    _run(manager, session, recorder, broker, [
        lambda: _end(session, "call-1", "sleep 6"),
        0.05, lambda: session._late_queue.put_nowait(subagent), 0.02, lambda: session._close_late(),
    ], after=0.25)
    assert recorder.turns == []
    (_chat, prompt, origin), = manager._recorder.started
    assert origin == "background_run" and "sleep 6" in prompt


def test_a_provider_that_does_not_continue_on_its_own_is_never_claimed(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, session, recorder, broker = _setup(monkeypatch, window=0.1, flagged=False)
    _run(manager, session, recorder, broker, [
        lambda: _end(session, "call-1", "make"),
        lambda: session._touch_late([_text(NATIVE_TEXT)], substantive=True),
        lambda: session._close_late(),
    ], after=0.2)
    assert [turn["prompt"] for turn in recorder.turns] == [WAKE_NOTICE]  # the reply keeps its own prompt
    assert len(manager._recorder.started) == 1  # our own wake went out after the window


def test_a_provider_notification_text_is_kept_and_the_claimed_shell_is_still_announced(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, session, recorder, broker = _setup(monkeypatch, window=0.5)
    _run(manager, session, recorder, broker, [
        lambda: _end(session, "call-1", "make"),
        lambda: session._touch_late([_text(NATIVE_TEXT)], substantive=True),
        lambda: session._late_queue.put_nowait(_final(NATIVE_TEXT, wake_notice="Claude: a real notice.")),
    ])
    assert [turn["prompt"] for turn in recorder.turns] == ["Claude: a real notice."]
    (_chat, prompt, _origin), = manager._recorder.started
    assert prompt.splitlines()[0] == "[Ciaobot] 1 background run finished."


def test_a_second_shell_ending_later_is_not_claimed_by_the_first_continuation(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, session, recorder, broker = _setup(monkeypatch, window=0.15)
    _run(manager, session, recorder, broker, [
        lambda: _end(session, "call-1", "job one"),
        0.02, lambda: session._touch_late([_text(NATIVE_TEXT)], substantive=True),
        lambda: _end(session, "call-2", "job two"),
        0.01, lambda: session._close_late(),
    ], after=0.3)
    assert "job one" in recorder.turns[0]["prompt"] and "job two" not in recorder.turns[0]["prompt"]
    prompts = [p for _, p, _ in manager._recorder.started]
    assert len(prompts) == 1 and "job two" in prompts[0] and "job one" not in prompts[0]


def test_each_held_notice_keeps_its_own_window(monkeypatch: pytest.MonkeyPatch) -> None:
    # Finding 5: job two ends 0.1 s after job one; job one's window closes first, job two keeps its own.
    manager, session, recorder, broker = _setup(monkeypatch, window=0.15)
    _run(manager, session, recorder, broker, [
        lambda: _end(session, "call-1", "job one"),
        0.1, lambda: _end(session, "call-2", "job two"),
    ], after=0.35)
    prompts = [p for _, p, _ in manager._recorder.started]
    assert len(prompts) == 2
    assert "job one" in prompts[0] and "job two" not in prompts[0]
    assert "job two" in prompts[1] and "job one" not in prompts[1]


def test_a_claimed_continuation_does_not_count_toward_the_wake_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, session, recorder, broker = _setup(monkeypatch, window=0.1)
    chat = manager._chats["chat-1"]
    origins_before = dict(chat.user_turn_origin)
    _run(manager, session, recorder, broker, [
        lambda: _end(session, "call-1", "make"),
        lambda: session._touch_late([_text(NATIVE_TEXT)], substantive=True),
        lambda: session._close_late(),
    ])
    assert len(manager._shell_wake_times.get("chat-1", ())) == 0
    assert chat.user_turn_origin == origins_before
    assert manager._three_shell_wakes_in_a_row(chat) is False


def test_the_claimed_card_is_published_before_the_continuation_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, session, recorder, broker = _setup(monkeypatch, window=0.5)
    _run(manager, session, recorder, broker, [
        lambda: _end(session, "call-1", "make"),
        lambda: session._touch_late([_text(NATIVE_TEXT)], substantive=True),
        lambda: session._close_late(),
    ])
    echo = _FakeStream.published[0]
    assert echo["type"] == "user_echo" and echo["origin"] == "background_run"
    assert echo["text"].splitlines()[0] == "[Ciaobot] 1 background run finished."
    assert any(p["type"] == "text_delta" for p in _FakeStream.published[1:])


def test_a_whitespace_only_continuation_still_records_the_card_it_was_claimed_for(monkeypatch: pytest.MonkeyPatch) -> None:
    # Finding 3: the card is in the journal and the live view, so the durable transcript records it too.
    manager, session, recorder, broker = _setup(monkeypatch, window=0.5)
    _run(manager, session, recorder, broker, [
        lambda: _end(session, "call-1", "make"),
        lambda: session._touch_late([_text("   ")], substantive=True),
        lambda: session._close_late(),
    ])
    assert len(recorder.turns) == 1
    assert recorder.turns[0]["prompt"].splitlines()[0] == "[Ciaobot] 1 background run finished."
    assert recorder.turns[0]["response"].strip() == ""


def test_a_superseded_drain_keeps_the_claim_and_the_card_for_the_next_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    # Finding 4: a foreground turn takes the drain over between the notice and the reply.
    manager, session, recorder, broker = _setup(monkeypatch, window=0.5)

    def foreground_takes_over() -> None:
        broker.owner = SimpleNamespace(background=False)

    def foreground_done() -> None:
        broker.owner = None

    _run(manager, session, recorder, broker, [
        lambda: _end(session, "call-1", "make"),
        0.02, lambda: session._touch_late([_text("first")], substantive=True),
        foreground_takes_over, 0.02,
        lambda: session._late_queue.put_nowait(_text(NATIVE_TEXT)),  # consumed by the superseded drain
        0.02, lambda: foreground_done(), ("drain",), 0.05,
        lambda: session._close_late(),
    ])
    assert len(recorder.turns) == 1 and recorder.turns[0]["prompt"].splitlines()[0] == "[Ciaobot] 1 background run finished."
    cards = [p for p in _FakeStream.published if p["type"] == "user_echo"]
    assert len(cards) == 1  # the card was published once, by the drain that could publish it
    assert manager._claimed_notices.get("chat-1") in (None, [])
