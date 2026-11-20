"""A provider's own continuation after a background shell: one turn for the shell, no wake of ours.

The fixture is a trimmed capture (``tests/fixtures/acp/shell_opencode_native_continuation.jsonl``):
the shell call, then the agent's own continuation with no prompt from Ciaobot.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ciao.models import AssistantTextDelta, ResultEvent, ShellFinishedEvent, ToolUseEvent, WakeNoticeEvent
from ciao.web import chat_streaming, project_chats
from ciao.web.chat_streaming import ChatStreaming

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


def _shell(call_id: str, command: str, ended_at: float) -> ShellFinishedEvent:
    return ShellFinishedEvent(
        type="shell_finished", tool_use_id=call_id, command=command,
        duration_s=6.0, exit_code=None, ended_at=ended_at,
    )


def _text(text: str) -> AssistantTextDelta:
    return AssistantTextDelta(type="text_delta", text=text)


def _final(text: str, wake_notice: str = WAKE_NOTICE) -> ResultEvent:
    return ResultEvent(type="result", result=text, origin="task-notification", wake_notice=wake_notice)


class _Recorder:
    def __init__(self) -> None:
        self.turns: list[dict[str, Any]] = []

    def record_turn(self, request: Any, *, response_text: str, origin: str, input_kind: str, **_: Any) -> None:
        self.turns.append({"prompt": request.prompt, "response": response_text, "origin": origin})


class _Broker:
    def get(self, chat_id: str) -> None:
        return None

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
    """The drain's host: the real manager's shell hooks, a recording transcript and broker."""

    def __init__(self, manager: Any, script: list[Any], recorder: _Recorder) -> None:
        self._manager = manager
        self._script = script
        self._transcripts = recorder
        self._broker = _Broker()
        self._providers = {"chat-1": SimpleNamespace(drain_events=self._drain)}
        self._chats = manager._chats
        self._config = SimpleNamespace(workspace_root=Path("/tmp"))

    async def _drain(self) -> Any:
        spoke = False
        for item in self._script:
            if isinstance(item, float):
                await asyncio.sleep(item)  # the time between two events of the provider's stream
                continue
            if isinstance(item, AssistantTextDelta) and item.parent_tool_use_id is None and not spoke:
                # What AcpSession does when a late turn starts speaking: ask the host for the notice
                # it answers, and send that notice ahead of the reply.
                spoke = True
                notice = self._manager._continuation_started("chat-1")
                if notice:
                    yield WakeNoticeEvent(type="wake_notice", text=notice)
            if isinstance(item, ResultEvent):
                spoke = False
            yield item

    def _agent_root_for_chat(self, chat_id: str) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        if name in (
            "_note_shell_finished", "_continuation_started", "_take_claimed_notice",
            "_deliver_wake", "_events",
        ):
            return getattr(self._manager, name)
        return lambda *args, **kwargs: None


def _setup(monkeypatch: pytest.MonkeyPatch, window: float, flagged: bool = True) -> tuple[Any, Any]:
    from tests.test_shell_wake import _chat, _manager

    monkeypatch.setattr(project_chats, "SHELL_FINISHED_WINDOW_S", window)
    monkeypatch.setattr(chat_streaming, "ChatStream", _FakeStream)
    _FakeStream.published = []
    chat = _chat(model="m", mode="bypass", provider="opencode", session_id="", title="t")
    manager = _manager(chat)
    manager._providers = {"chat-1": SimpleNamespace(continues_after_shell=flagged)}
    return manager, chat


def _run(manager: Any, *, notes: list[Any] = (), script: list[Any] = (), after: float = 0.0,
         calls: list[Any] = ()) -> _Recorder:
    """Run one scenario in one event loop: the held-notice timers and the provider's drain share it."""
    recorder = _Recorder()

    async def main() -> None:
        for call in calls:  # what happened first on the provider's side
            call()
        for note in notes:
            note()
        if script:
            await ChatStreaming(_Host(manager, list(script), recorder)).drain_between_turns("chat-1", "p")  # type: ignore[arg-type]
        if after:
            await asyncio.sleep(after)

    asyncio.run(main())
    return recorder


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
    # Finding 1: it starts 0.05 s after the shell ends and runs 0.3 s, well past the 0.15 s window.
    manager, _chat_obj = _setup(monkeypatch, window=0.15)
    recorder = _run(
        manager,
        notes=[lambda: manager._note_shell_finished("chat-1", _shell("call-1", "sleep 6; echo done-marker .", time.monotonic()))],
        script=[0.05, _text(NATIVE_TEXT), 0.3, _final(NATIVE_TEXT)],
        after=0.2,
    )
    assert [turn["response"] for turn in recorder.turns] == [NATIVE_TEXT]
    assert recorder.turns[0]["prompt"].splitlines()[0] == "[Ciaobot] 1 background run finished."
    assert recorder.turns[0]["origin"] == "background_run"
    assert manager._recorder.started == [] and manager._recorder.queued == []


def test_after_the_window_with_no_continuation_exactly_one_wake_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _chat_obj = _setup(monkeypatch, window=0.05)
    _run(manager, notes=[lambda: manager._note_shell_finished("chat-1", _shell("call-x", "make", time.monotonic()))], after=0.2)
    assert len(manager._recorder.started) == 1


def test_an_unrelated_subagent_notification_does_not_claim_the_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    # Finding 2: a subagent's completion (a tool result, no reply text) closes at +0.08 s.
    manager, _chat_obj = _setup(monkeypatch, window=0.2)
    subagent = ToolUseEvent(type="tool_result", tool_use_id="task-9", tool_name="task", exit_code=0)
    recorder = _run(
        manager,
        notes=[lambda: manager._note_shell_finished("chat-1", _shell("call-1", "sleep 6", time.monotonic()))],
        script=[0.08, subagent, _final("")],
        after=0.25,
    )
    assert recorder.turns == []  # the subagent turn had no reply to file
    (_chat, prompt, origin), = manager._recorder.started
    assert origin == "background_run" and "sleep 6" in prompt  # the shell's own wake still goes out


def test_a_provider_that_does_not_continue_on_its_own_is_never_claimed(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _chat_obj = _setup(monkeypatch, window=0.1, flagged=False)
    recorder = _run(
        manager,
        notes=[lambda: manager._note_shell_finished("chat-1", _shell("call-1", "make", time.monotonic()))],
        script=[_text(NATIVE_TEXT), _final(NATIVE_TEXT)],
        after=0.2,
    )
    # The provider's own reply is still its own turn, with its own prompt: nothing was claimed.
    assert [turn["prompt"] for turn in recorder.turns] == [WAKE_NOTICE]
    assert len(manager._recorder.started) == 1  # our own wake went out after the window


def test_a_provider_notification_text_is_kept_and_the_claimed_shell_is_still_announced(monkeypatch: pytest.MonkeyPatch) -> None:
    # Finding 2: a real wake_notice from the provider is never overwritten; the shell is not lost.
    manager, _chat_obj = _setup(monkeypatch, window=0.5)
    recorder = _run(
        manager,
        notes=[lambda: manager._note_shell_finished("chat-1", _shell("call-1", "make", time.monotonic()))],
        script=[0.02, _text(NATIVE_TEXT), _final(NATIVE_TEXT, wake_notice="Claude: a real notice.")],
    )
    assert [turn["prompt"] for turn in recorder.turns] == ["Claude: a real notice."]
    (_chat, prompt, _origin), = manager._recorder.started
    assert prompt.splitlines()[0] == "[Ciaobot] 1 background run finished."


def test_a_continuation_before_the_shell_end_answers_it_and_no_wake_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    # Finding 3: the continuation spoke first; the shell end is observed late (deferred behind a prompt).
    manager, _chat_obj = _setup(monkeypatch, window=0.05)
    _run(
        manager,
        calls=[lambda: manager._continuation_started("chat-1")],
        notes=[lambda: manager._note_shell_finished("chat-1", _shell("call-1", "make", time.monotonic() - 5.0))],
        after=0.15,
    )
    assert manager._recorder.started == [] and manager._recorder.queued == []


def test_a_continuation_long_before_a_shell_end_does_not_answer_it(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _chat_obj = _setup(monkeypatch, window=0.05)

    def old_continuation() -> None:
        manager._continuation_started("chat-1")
        manager._continuation_at["chat-1"] -= 600.0  # that continuation was ten minutes earlier

    _run(
        manager,
        calls=[old_continuation],
        notes=[lambda: manager._note_shell_finished("chat-1", _shell("call-1", "make", time.monotonic()))],
        after=0.15,
    )
    assert len(manager._recorder.started) == 1


def test_a_continuation_claims_only_the_shells_that_ended_before_it_started(monkeypatch: pytest.MonkeyPatch) -> None:
    # Finding 5: shell two ends after the continuation started; it keeps its own window.
    manager, _chat_obj = _setup(monkeypatch, window=0.15)
    now = time.monotonic()
    recorder = _run(
        manager,
        notes=[
            lambda: manager._note_shell_finished("chat-1", _shell("call-1", "job one", now)),
            lambda: manager._note_shell_finished("chat-1", _shell("call-2", "job two", now + 0.1)),
        ],
        script=[0.03, _text(NATIVE_TEXT), _final(NATIVE_TEXT)],
        after=0.25,
    )
    assert "job one" in recorder.turns[0]["prompt"] and "job two" not in recorder.turns[0]["prompt"]
    prompts = [p for _, p, _ in manager._recorder.started]
    assert len(prompts) == 1 and "job two" in prompts[0] and "job one" not in prompts[0]


def test_a_second_shell_keeps_its_own_window_and_does_not_restart_the_first(monkeypatch: pytest.MonkeyPatch) -> None:
    # Finding 5: shell one's deadline is not pushed back by shell two ending 0.1 s later.
    manager, _chat_obj = _setup(monkeypatch, window=0.15)
    first_due: list[float] = []

    async def scenario() -> None:
        manager._note_shell_finished("chat-1", _shell("call-1", "job one", time.monotonic()))
        first_due.append(manager._shell_notice_due[("chat-1", "call-1")])
        await asyncio.sleep(0.1)
        manager._note_shell_finished("chat-1", _shell("call-2", "job two", time.monotonic()))
        await asyncio.sleep(0.1)  # shell one's window has closed; shell two's has not

    asyncio.run(scenario())
    assert len(manager._recorder.started) == 1 and "job one" in manager._recorder.started[0][1]
    assert manager._shell_notice_due.get(("chat-1", "call-2"), 0.0) > first_due[0]


def test_a_claimed_continuation_does_not_count_toward_the_wake_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    # Finding 5: a continuation's recorded turn is not one of our wakes.
    manager, chat_obj = _setup(monkeypatch, window=0.1)
    origins_before = dict(chat_obj.user_turn_origin)
    _run(
        manager,
        notes=[lambda: manager._note_shell_finished("chat-1", _shell("call-1", "make", time.monotonic()))],
        script=[0.01, _text(NATIVE_TEXT), _final(NATIVE_TEXT)],
    )
    assert len(manager._shell_wake_times.get("chat-1", ())) == 0
    assert chat_obj.user_turn_origin == origins_before
    assert manager._three_shell_wakes_in_a_row(chat_obj) is False


def test_the_claimed_notice_card_is_published_before_the_continuation_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    # Finding 6: live parity with the reload: the notice row, then the reply.
    manager, _chat_obj = _setup(monkeypatch, window=0.5)
    _run(
        manager,
        notes=[lambda: manager._note_shell_finished("chat-1", _shell("call-1", "make", time.monotonic()))],
        script=[0.01, _text(NATIVE_TEXT), _final(NATIVE_TEXT)],
    )
    echo = _FakeStream.published[0]
    assert echo["type"] == "user_echo" and echo["origin"] == "background_run"
    assert echo["text"].splitlines()[0] == "[Ciaobot] 1 background run finished."
    assert any(p["type"] == "text_delta" for p in _FakeStream.published[1:])


def test_a_shutdown_delivers_held_notices_instead_of_dropping_them(monkeypatch: pytest.MonkeyPatch) -> None:
    # Finding 4: restart resume covers a turn the restart cut off; a held notice must start one.
    manager, _chat_obj = _setup(monkeypatch, window=30.0)
    manager._memory_pass = SimpleNamespace(close=lambda: None)
    _run(
        manager,
        notes=[
            lambda: manager._note_shell_finished("chat-1", _shell("call-1", "make", time.monotonic())),
            manager.begin_shutdown,
        ],
    )
    assert len(manager._recorder.started) == 1
    assert "make" in manager._recorder.started[0][1]
    assert manager._shell_finished_pending == {} and manager._shell_notice_due == {}


def test_a_forgotten_chat_keeps_no_held_notice_or_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    # Archive and delete while a notice is held: nothing left to announce or to claim.
    manager, _chat_obj = _setup(monkeypatch, window=30.0)
    _run(
        manager,
        notes=[
            lambda: manager._note_shell_finished("chat-1", _shell("call-1", "make", time.monotonic())),
            lambda: manager._forget_shell_wake_state("chat-1"),
        ],
    )
    assert manager._continuation_started("chat-1") is None
    assert manager._shell_notice_due == {} and manager._claimed_notices == {}
    assert manager._recorder.started == []

def test_the_session_journals_the_notice_before_the_reply_and_queues_it_ahead_of_the_text() -> None:
    # The live session, not the drain, makes the claim: its first late text asks the hook, and the
    # notice goes into the journal and the late queue before the text it answers.
    from ciao.providers.acp.session import AcpSession
    from ciao.providers.acp.spec import AgentSpec  # noqa: F401 (the spec's type)

    class _Journal:
        def __init__(self) -> None:
            self.rows: list[tuple[str, Any]] = []

        def user(self, text: str, *, turn_origin: str = "") -> None:
            self.rows.append(("user", (text, turn_origin)))

        def event(self, payload: Any) -> None:
            self.rows.append(("event", payload))

    async def run() -> tuple[_Journal, list[Any]]:
        session = AcpSession.__new__(AcpSession)
        session._journal = _Journal()
        session._late_queue = asyncio.Queue()
        session._late_text = session_text()
        session._late_open = False
        session._late_substantive = False
        session._late_speaking = False
        session._quiet_task = None
        session.spec = SimpleNamespace(quiet_period_s=60.0)
        session.continuation_hook = lambda: "[Ciaobot] 1 background run finished.\n\n— make (ran 1 s) (c, exit status unknown)"
        session._touch_late([_text(NATIVE_TEXT)], substantive=True)
        session._touch_late([_text(" more")], substantive=True)  # the same continuation: no second notice
        session._quiet_task.cancel()
        queued = []
        while not session._late_queue.empty():
            queued.append(session._late_queue.get_nowait())
        return session._journal, queued

    journal, queued = asyncio.run(run())
    user_rows = [row for row in journal.rows if row[0] == "user"]
    assert len(user_rows) == 1 and user_rows[0][1][1] == "background_run"
    assert user_rows[0][1][0].startswith("[Ciaobot] 1 background run finished.")
    assert isinstance(queued[0], WakeNoticeEvent)
    assert [getattr(event, "text", None) for event in queued[1:2]] == [NATIVE_TEXT]


def session_text() -> Any:
    from ciao.providers.acp.session import TurnText

    return TurnText()
