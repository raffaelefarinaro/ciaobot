"""Contract and ownership-boundary tests for chat streaming."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Coroutine
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from ciao.models import (
    AgentRequest,
    AssistantTextDelta,
    ImageAttachment,
    ResultEvent,
    StreamEvent,
    ToolUseEvent,
)
from ciao.web.chat_broker import ChatStream, ChatStreamBroker
from ciao.web.chat_streaming import ChatStreaming, ChatStreamingHost, StreamOutcome
from ciao.web.project_chats import ChatInfo, ProjectChatManager


class _Provider:
    current_session_id = "native-session"

    def __init__(self, events: list[StreamEvent]) -> None:
        self._events = events

    async def execute_streaming(
        self, request: AgentRequest
    ) -> AsyncGenerator[StreamEvent, None]:
        del request
        for event in self._events:
            yield event


class _PassHost:
    def __init__(self, provider: _Provider) -> None:
        self.chat = ChatInfo(
            chat_id="chat-1",
            project_id="project-1",
            title="Contract",
            model="opus",
        )
        self._chats = {"chat-1": self.chat}
        self._provider = provider
        self.saves = 0
        self.tool_events: list[ToolUseEvent] = []

    def _get_provider(self, chat_id: str) -> _Provider:
        assert chat_id == "chat-1"
        return self._provider

    def _rotate_session_id(self, chat: ChatInfo, new_session_id: str) -> None:
        chat.session_id = new_session_id

    def _commit_context_marker(
        self, chat: ChatInfo, request: AgentRequest, session_id: str
    ) -> bool:
        del chat, request, session_id
        return False

    def _record_agent_tool_use(
        self, chat: ChatInfo, request: AgentRequest, event: ToolUseEvent
    ) -> None:
        del chat, request
        self.tool_events.append(event)

    def _save(self, *, reason: str = "registry_mutation") -> None:
        del reason
        self.saves += 1


@pytest.mark.asyncio
async def test_provider_pass_keeps_stream_events_and_terminal_outcome() -> None:
    events: list[StreamEvent] = [
        AssistantTextDelta(type="text", text="hello"),
        ResultEvent(
            type="result",
            result="done",
            session_id="event-session",
            effective_model="sonnet",
            usage={"input_tokens": "3", "output_tokens": "5"},
            quota={"five_hour": "7"},
            cost_usd=0.25,
        ),
    ]
    host = _PassHost(_Provider(events))
    streaming = ChatStreaming(cast(ChatStreamingHost, host))
    outcome = StreamOutcome()
    request = AgentRequest(prompt="hello", model="opus", mode="auto")

    received = [
        event
        async for event in streaming.drive_stream(
            chat_id="chat-1",
            request=request,
            outcome=outcome,
        )
    ]

    assert received == events
    assert outcome.events == events
    assert outcome.response_text == "done"
    assert outcome.had_error is False
    assert outcome.effective_model == "sonnet"
    assert outcome.usage == {"input_tokens": "3", "output_tokens": "5"}
    assert outcome.quota == {"five_hour": "7"}
    assert outcome.cost_usd == 0.25
    assert host.chat.session_id == "event-session"
    assert host.saves == 2


class _DriveHost:
    def __init__(self, events: list[StreamEvent], workspace_root: Path) -> None:
        self.chat = ChatInfo(
            chat_id="chat-1",
            project_id="project-1",
            title="Contract",
            model="opus",
        )
        self._chats = {"chat-1": self.chat}
        self._providers: dict[str, object] = {}
        self._config = SimpleNamespace(workspace_root=workspace_root)
        self._state = SimpleNamespace()
        self._transcripts = SimpleNamespace()
        self._broker = ChatStreamBroker()
        self.published: list[dict] = []
        self._events = SimpleNamespace(publish=self.published.append)
        self._snapshots = SimpleNamespace(schedule_capture=lambda **_: None)
        self._events_to_stream = events
        self.saves = 0
        self.announcements: list[tuple[str, str, str, str]] = []
        self.discarded: list[str] = []
        self.detached_names: list[str] = []
        self.drain_awaits: list[str] = []

    def _save(self, *, reason: str = "registry_mutation") -> None:
        del reason
        self.saves += 1

    async def _await_between_turns_drain(self, chat_id: str) -> None:
        self.drain_awaits.append(chat_id)

    async def stream_chat(
        self,
        chat_id: str,
        prompt: str,
        images: list[ImageAttachment] | None = None,
        *,
        unattended: bool = False,
    ) -> AsyncGenerator[StreamEvent, None]:
        del chat_id, prompt, images, unattended
        for event in self._events_to_stream:
            yield event

    def _stop_result_payload(
        self, chat_id: str, *, turn_index: int | None, text: str
    ) -> dict[str, object]:
        return {
            "type": "result",
            "chat_id": chat_id,
            "turn_index": turn_index,
            "text": text,
        }

    async def _auto_title_and_publish(
        self, chat_id: str, user_text: str, assistant_text: str
    ) -> None:
        del chat_id, user_text, assistant_text

    def _start_subagent_watcher(self, chat_id: str, project_id: str) -> None:
        del chat_id, project_id

    def _clear_chat_retry(self, chat: ChatInfo, *, status: str = "") -> None:
        del chat, status

    @staticmethod
    def _result_snippet(text: str, limit: int = 280) -> str:
        return text[:limit]

    @staticmethod
    def _is_worth_announcing_nudge_reply(text: str) -> bool:
        return len(text) >= 4

    @staticmethod
    def _is_interim_subagent_text(text: str) -> bool:
        return "waiting" in text

    def _park_result_announce(
        self, chat_id: str, project_id: str, title: str, snippet: str
    ) -> int:
        del chat_id, project_id, title, snippet
        return 1

    def _announce_result_ready(
        self, chat_id: str, project_id: str, title: str, snippet: str
    ) -> None:
        self.announcements.append((chat_id, project_id, title, snippet))

    async def _maybe_archive_proposal_helper(self, chat_id: str) -> bool:
        del chat_id
        return False

    async def _memory_pass_turn_finished(self, chat_id: str) -> bool:
        del chat_id
        return False

    def _discard_result_announce(self, chat_id: str, token: int | None = None) -> None:
        del token
        self.discarded.append(chat_id)

    def _flush_result_announce(self, chat_id: str, token: int | None = None) -> bool:
        del chat_id, token
        return False

    def _cancel_parked_announce_deadline(self, chat_id: str) -> None:
        del chat_id

    def _spawn_detached(
        self, coro: Coroutine[object, object, object], name: str
    ) -> asyncio.Task[object]:
        coro.close()
        self.detached_names.append(name)
        return asyncio.create_task(asyncio.sleep(0))

    def _start_between_turns_drain(self, chat_id: str, project_id: str) -> None:
        del chat_id, project_id


@pytest.mark.asyncio
async def test_drive_keeps_websocket_payloads_and_lifecycle_events(
    tmp_path: Path,
) -> None:
    host = _DriveHost(
        [
            AssistantTextDelta(type="text", text="hello"),
            ResultEvent(
                type="result",
                result="done",
                session_id="session-1",
                effective_model="opus",
                usage={"input_tokens": "3", "output_tokens": "5"},
                quota={"five_hour": "7"},
            ),
        ],
        tmp_path,
    )
    streaming = ChatStreaming(cast(ChatStreamingHost, host))
    stream = ChatStream(prompt_text="hello")
    host._broker.register("chat-1", stream)

    await streaming.drive(
        chat_id="chat-1",
        project_id="project-1",
        prompt="hello",
        images=None,
        turn_index=None,
        chat_meta=host.chat,
        stream=stream,
        is_retry=False,
        unattended=False,
    )

    assert stream.buffered_events() == [
        {"type": "text_delta", "text": "hello"},
        {
            "type": "result",
            "text": "done",
            "is_error": False,
            "effective_model": "opus",
            "usage": {"input_tokens": "3", "output_tokens": "5"},
            "session_id": "session-1",
            "quota": {"five_hour": "7"},
        },
    ]
    assert host.published == [
        {
            "type": "chat_streaming_done",
            "chat_id": "chat-1",
            "project_id": "project-1",
            "is_error": False,
        }
    ]
    assert host.drain_awaits == ["chat-1"]
    assert host.discarded == ["chat-1"]
    assert host.announcements == [("chat-1", "project-1", "Contract", "done")]
    assert host.detached_names == [
        "archive-proposal-helper-chat-1",
        "memory-pass-chat-1",
    ]
    assert stream.done is True
    assert host._broker.get("chat-1") is None
    assert host.chat.last_response == "done"
    assert host.chat.last_response_status == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("events", "status"),
    [
        (
            [ResultEvent(type="result", result="the vault is locked", is_error=True)],
            "error",
        ),
        ([ResultEvent(type="result", result="")], "empty"),
    ],
)
async def test_an_unclean_terminal_turn_still_settles_a_memory_pass(
    tmp_path: Path,
    events: list[StreamEvent],
    status: str,
) -> None:
    """The memory-pass hook fires on every terminal turn, not only a clean one.

    Gating it on a clean result is what left an errored pass recorded as
    `running` forever, holding its workspace slot and every pass queued behind
    it. The proposal helper stays success-gated: a failed turn must not propose
    anything, and an error must never be archived as a clean success.
    """
    host = _DriveHost(events, tmp_path)
    streaming = ChatStreaming(cast(ChatStreamingHost, host))
    stream = ChatStream(prompt_text="hello")
    host._broker.register("chat-1", stream)

    await streaming.drive(
        chat_id="chat-1",
        project_id="project-1",
        prompt="hello",
        images=None,
        turn_index=None,
        chat_meta=host.chat,
        stream=stream,
        is_retry=False,
        unattended=False,
    )

    assert host.detached_names == ["memory-pass-chat-1"]
    assert host.announcements == []
    # The terminal status is what the pass reads, so it has to say what happened.
    assert host.chat.last_response_status == status
    assert host.chat.last_response == ""
    assert stream.done is True
    assert host._broker.get("chat-1") is None


@pytest.mark.asyncio
async def test_manager_private_streaming_state_is_collaborator_views() -> None:
    manager = object.__new__(ProjectChatManager)
    manager._streaming = ChatStreaming(cast(ChatStreamingHost, manager))

    manager._turn_perf_started[("chat-1", 0)] = 1.5
    assert manager._streaming.turn_perf_started[("chat-1", 0)] == 1.5

    manager._turn_perf_started = {("chat-2", 1): 2.5}
    assert manager._streaming.turn_perf_started == {("chat-2", 1): 2.5}

    manager._last_drain_result["chat-1"] = ("summary", False)
    assert manager._streaming.last_drain_results["chat-1"] == ("summary", False)

    manager._last_drain_result = {"chat-2": ("error", True)}
    assert manager._streaming.last_drain_results == {"chat-2": ("error", True)}

    task = asyncio.create_task(asyncio.sleep(0))
    manager._between_turn_drains["chat-1"] = task
    assert manager._streaming.between_turn_drains["chat-1"] is task
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    manager._detached_tasks = set()
    manager._detached_tasks.add(task)
    assert task in manager._detached_tasks
    manager._detached_tasks.clear()
