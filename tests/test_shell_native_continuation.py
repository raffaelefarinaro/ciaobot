"""OpenCode continues a session on its own when a background shell ends; the reply must be recorded once.

The fixture is a trimmed capture (``tests/fixtures/acp/shell_opencode_native_continuation.jsonl``):
the shell call, then the agent's own continuation with no prompt from Ciaobot.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ciao import shell_wake
from ciao.models import ResultEvent, ShellFinishedEvent
from ciao.providers.acp import session as session_module
from ciao.providers.acp.agents import OPENCODE
from ciao.providers.acp.normalize import UpdateNormalizer
from ciao.providers.acp.session import AcpSession
from ciao.providers.acp.shell_liveness import ShellLiveness
from ciao.web import project_chats
from ciao.web.chat_streaming import ChatStreaming
from ciao.web.project_chats import ProjectChatManager

FIXTURE = Path(__file__).parent / "fixtures" / "acp" / "shell_opencode_native_continuation.jsonl"
NATIVE_TEXT = "The background command finished and printed `done-marker`."


def _frames() -> list[dict[str, Any]]:
    return [json.loads(line)["params"] for line in FIXTURE.read_text().splitlines() if line.strip()]


def _continuation_text() -> str:
    parts = []
    for params in _frames():
        update = params["update"]
        if update.get("sessionUpdate") == "agent_message_chunk":
            parts.append(update["content"]["text"])
    return "".join(parts)


# -- the transcript: a continuation is its own turn ---------------------------------------------


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
    rows = _render_acp_journal(
        records, SimpleNamespace(), start_user_idx=0,
        workspace_root=None, agent_root=None,
    )
    texts = [row["content"] for row in rows if row.get("role") == "assistant"]
    assert texts == ["started", "resumed"]


# -- the chat: the continuation claims the notice; no second wake -------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.turns: list[dict[str, Any]] = []

    def record_turn(self, request: Any, *, response_text: str, origin: str, input_kind: str, **_: Any) -> None:
        self.turns.append({"prompt": request.prompt, "response": response_text, "origin": origin, "input_kind": input_kind})


class _Broker:
    def get(self, chat_id: str) -> None:
        return None

    def register(self, chat_id: str, stream: Any) -> None:
        return None

    def clear(self, chat_id: str, stream: Any) -> None:
        return None


class _Host:
    """The drain's host: the real manager's shell hooks and wake delivery, a recording transcript."""

    def __init__(self, manager: ProjectChatManager, events: list[Any], recorder: _Recorder) -> None:
        self._manager = manager
        self._script = events
        self._transcripts = recorder
        self._broker = _Broker()
        self._providers = {"chat-1": SimpleNamespace(drain_events=self._drain)}
        self._chats = manager._chats
        self._config = SimpleNamespace(workspace_root=Path("/tmp"))

    def _agent_root_for_chat(self, chat_id: str) -> None:
        return None

    async def _drain(self) -> Any:
        for event in self._script:
            yield event

    def __getattr__(self, name: str) -> Any:
        if name in ("_note_shell_finished", "_claim_shell_notices", "_deliver_wake", "_events"):
            return getattr(self._manager, name)
        return lambda *args, **kwargs: None


@pytest.mark.asyncio
async def test_a_native_continuation_claims_the_held_notice_and_is_recorded_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_shell_wake import _chat, _manager

    monkeypatch.setattr(project_chats, "SHELL_FINISHED_WINDOW_S", 30.0)  # the notice is held
    chat = _chat(model="m", mode="bypass", provider="opencode", session_id="", title="t")
    manager = _manager(chat)
    shell_end = ShellFinishedEvent(
        type="shell_finished", tool_use_id="call_6f8e2205ea724dc29fab5fcc",
        command="sleep 6; echo done-marker .", duration_s=6.0, exit_code=None,
    )
    manager._note_shell_finished("chat-1", shell_end)
    native = ResultEvent(
        type="result", result=NATIVE_TEXT, origin="task-notification",
        wake_notice="The agent continued after background work finished.",
    )
    recorder = _Recorder()
    host = _Host(manager, [native], recorder)
    await ChatStreaming(host).drain_between_turns("chat-1", "project-1")  # type: ignore[arg-type]
    await asyncio.sleep(0)

    # One turn: the continuation's reply, filed with the shell notice as its prompt.
    assert [turn["response"] for turn in recorder.turns] == [NATIVE_TEXT]
    assert recorder.turns[0]["prompt"].splitlines()[0] == "[Ciaobot] 1 background run finished."
    assert recorder.turns[0]["origin"] == "background_run"
    # And no wake of Ciaobot's own started a second reply for the same shell.
    assert manager._recorder.started == [] and manager._recorder.queued == []
    assert manager._shell_finished_flushes == {}


@pytest.mark.asyncio
async def test_a_shell_with_no_native_continuation_is_still_announced_by_ciaobot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_shell_wake import _chat, _manager

    monkeypatch.setattr(project_chats, "SHELL_FINISHED_WINDOW_S", 0.01)
    manager = _manager(_chat())
    manager._note_shell_finished("chat-1", ShellFinishedEvent(
        type="shell_finished", tool_use_id="call-x", command="make", exit_code=0,
    ))
    await asyncio.sleep(0.1)
    assert len(manager._recorder.started) == 1
