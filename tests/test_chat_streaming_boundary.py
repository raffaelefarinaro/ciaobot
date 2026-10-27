"""Contract and ownership-boundary tests for chat streaming."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Coroutine
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from ciao.models import (
    AgentRequest,
    AssistantTextDelta,
    ImageAttachment,
    PermissionRequestEvent,
    ResultEvent,
    StreamEvent,
    ThinkingEvent,
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
        self._broker = ChatStreamBroker()
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
    def __init__(
        self,
        events: list[StreamEvent],
        workspace_root: Path,
        agent_root: Path | None = None,
    ) -> None:
        self._agent_root = agent_root if agent_root is not None else workspace_root
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

    def _agent_root_for_chat(self, chat_id: str) -> Path:
        del chat_id
        return self._agent_root

    def _fallback_steers(self, chat_id: str, stream: object, reason: str) -> None:
        del chat_id, stream, reason

    def on_steer_delivered(self, chat_id: str, stream: object, steer_id: str) -> None:
        del chat_id, stream, steer_id

    def on_prompt_cancelled(
        self,
        chat_id: str,
        stream: object,
        prompt: str,
        images: object,
        turn_index: int | None,
    ) -> None:
        del chat_id, stream, prompt, images, turn_index

    def _save(self, *, reason: str = "registry_mutation") -> None:
        del reason
        self.saves += 1

    async def refresh_repo_location(self, chat_id, hints):
        return None

    async def _await_between_turns_drain(self, chat_id: str) -> None:
        self.drain_awaits.append(chat_id)

    async def stream_chat(
        self,
        chat_id: str,
        prompt: str,
        images: list[ImageAttachment] | None = None,
        *,
        unattended: bool = False,
        compact: bool = False,
    ) -> AsyncGenerator[StreamEvent, None]:
        del chat_id, prompt, images, unattended, compact
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

    def _settle_schedule_retry(self, chat_id: str, *, succeeded: bool) -> None:
        del chat_id, succeeded

    @staticmethod
    def _result_snippet(text: str, limit: int = 280) -> str:
        return ProjectChatManager._result_snippet(text, limit)

    @staticmethod
    def _is_worth_announcing_follow_up(text: str) -> bool:
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

    def _native_question_request_id(self, pending_question: str) -> str:
        del pending_question
        return ""


class _TurnScriptHost(_DriveHost):
    """Serve one scripted event list per turn of a multi-turn drive."""

    def __init__(
        self, scripts: list[list[StreamEvent]], workspace_root: Path
    ) -> None:
        super().__init__(scripts[0], workspace_root)
        self._scripts = list(scripts)

    async def stream_chat(
        self,
        chat_id: str,
        prompt: str,
        images: list[ImageAttachment] | None = None,
        *,
        unattended: bool = False,
        compact: bool = False,
    ) -> AsyncGenerator[StreamEvent, None]:
        del chat_id, prompt, images, unattended, compact
        for event in self._scripts.pop(0):
            yield event


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
            "last_error": "",
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


_INTERIM = "Interim **working** narration."
_FINAL = "Final **answer** in `file.md`."
_WHOLE_TURN = f"{_INTERIM}\n\n{_FINAL}"


async def _drive_with(host: _DriveHost, stream: ChatStream) -> None:
    host._broker.register("chat-1", stream)
    await ChatStreaming(cast(ChatStreamingHost, host)).drive(
        chat_id="chat-1",
        project_id="project-1",
        prompt=stream.prompt_text,
        images=None,
        turn_index=None,
        chat_meta=host.chat,
        stream=stream,
        is_retry=False,
        unattended=False,
    )


@pytest.mark.asyncio
async def test_turn_end_announces_only_the_final_text_segment(tmp_path: Path) -> None:
    """A multi-step turn ends on its last text block, not the joined turn.

    OpenCode's terminal result is every text part of the turn joined, which is
    what the durable transcript needs; the preview and push must excerpt only
    the block the chat shows as the answer (#2139).
    """
    host = _DriveHost(
        [
            AssistantTextDelta(type="text", text=_INTERIM),
            ToolUseEvent(type="assistant", tool_name="Bash", tool_input="ls"),
            AssistantTextDelta(type="text", text=_FINAL),
            # A call's completion frame does not end the answer bubble.
            ToolUseEvent(
                type="tool_result",
                tool_name="Bash",
                tool_use_id="tool-1",
            ),
            ResultEvent(type="result", result=_WHOLE_TURN, session_id="session-1"),
        ],
        tmp_path,
    )
    stream = ChatStream(prompt_text="practice")

    await _drive_with(host, stream)

    assert host.announcements == [
        ("chat-1", "project-1", "Contract", "Final answer in file.md.")
    ]
    assert host.chat.last_snippet == "Final answer in file.md."
    # The durable reply and the handover keep the complete turn.
    assert host.chat.last_response == _WHOLE_TURN
    assert host.chat.last_response_status == "success"


@pytest.mark.asyncio
async def test_turn_end_prefers_the_streamed_answer_over_a_first_block_result(
    tmp_path: Path,
) -> None:
    """A terminal result that names only the first block does not win.

    The client keeps a defensive merge for exactly this Claude-shaped result,
    so the block streamed after the last tool call is what the user reads.
    """
    host = _DriveHost(
        [
            AssistantTextDelta(type="text", text=_INTERIM),
            ToolUseEvent(type="assistant", tool_name="Bash", tool_input="ls"),
            AssistantTextDelta(type="text", text=_FINAL),
            ResultEvent(type="result", result=_INTERIM, session_id="session-1"),
        ],
        tmp_path,
    )
    stream = ChatStream(prompt_text="practice")

    await _drive_with(host, stream)

    assert host.announcements == [
        ("chat-1", "project-1", "Contract", "Final answer in file.md.")
    ]
    assert host.chat.last_response == _INTERIM


@pytest.mark.asyncio
async def test_turn_end_ignores_subagent_text_in_the_answer(tmp_path: Path) -> None:
    host = _DriveHost(
        [
            AssistantTextDelta(type="text", text=_INTERIM),
            ToolUseEvent(type="assistant", tool_name="Bash", tool_input="ls"),
            AssistantTextDelta(
                type="text",
                text="subagent chatter",
                parent_tool_use_id="tool-1",
            ),
            AssistantTextDelta(type="text", text=_FINAL),
            ResultEvent(type="result", result=_WHOLE_TURN, session_id="session-1"),
        ],
        tmp_path,
    )
    stream = ChatStream(prompt_text="practice")

    await _drive_with(host, stream)

    assert host.announcements == [
        ("chat-1", "project-1", "Contract", "Final answer in file.md.")
    ]


@pytest.mark.asyncio
async def test_turn_end_does_not_reannounce_a_prior_answer_after_stop(
    tmp_path: Path,
) -> None:
    """A stopped follow-up cannot inherit and announce the earlier turn's text."""
    host = _TurnScriptHost(
        [
            [
                AssistantTextDelta(type="text", text="First answer."),
                ResultEvent(type="result", result="First answer."),
            ],
            [ResultEvent(type="result", result="", stopped=True)],
        ],
        tmp_path,
    )
    host.chat.last_snippet = "Earlier drive answer."
    stream = ChatStream(prompt_text="hello")
    stream.enqueue("one more thing")

    await _drive_with(host, stream)

    assert host.announcements == []
    # The persisted preview from the previous drive is not overwritten by a stop.
    assert host.chat.last_snippet == "Earlier drive answer."
    assert host.chat.last_response_status == "empty"


@pytest.mark.asyncio
async def test_turn_end_explicit_empty_answer_does_not_reannounce(
    tmp_path: Path,
) -> None:
    """A later successful turn with no answer block must clear the earlier one.

    OpenCode can finish with text but name an explicitly empty final segment
    (``final_text=""``). The selected answer for that turn is empty, so the
    drive may not fall back to the previous turn's answer and re-announce it.
    """
    host = _TurnScriptHost(
        [
            [
                AssistantTextDelta(type="text", text="First answer."),
                ResultEvent(type="result", result="First answer."),
            ],
            [
                AssistantTextDelta(type="text", text="Second narration."),
                ResultEvent(
                    type="result",
                    result="Second narration.",
                    final_text="",
                ),
            ],
        ],
        tmp_path,
    )
    host.chat.last_snippet = "Earlier drive answer."
    stream = ChatStream(prompt_text="hello")
    stream.enqueue("again")

    await _drive_with(host, stream)

    assert host.announcements == []
    assert host.chat.last_snippet == "Earlier drive answer."
    # The full terminal text is still what the handover keeps.
    assert host.chat.last_response == "Second narration."


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
async def test_terminal_outcome_is_aggregated_before_delivery() -> None:
    """The outcome must be terminal-complete the instant the result is yielded.

    The provider pass used to yield before aggregating, so a consumer that
    inspected ``outcome.response_text``/``had_error`` on delivery saw the
    previous turn's state. A normal completion is the simplest case, but a
    stop normalization relies on the same ordering.
    """
    host = _PassHost(
        _Provider(
            [
                AssistantTextDelta(type="text", text="hello"),
                ResultEvent(
                    type="result",
                    result="done",
                    session_id="s1",
                    effective_model="sonnet",
                    usage={"input_tokens": "1"},
                    quota={"five_hour": "2"},
                    cost_usd=0.5,
                ),
            ]
        )
    )
    streaming = ChatStreaming(cast(ChatStreamingHost, host))
    outcome = StreamOutcome()
    request = AgentRequest(prompt="hello", model="opus", mode="auto")

    seen: list[tuple[bool, str, bool]] = []
    async for event in streaming.drive_stream(
        chat_id="chat-1", request=request, outcome=outcome
    ):
        if isinstance(event, ResultEvent):
            seen.append(
                (
                    outcome.response_text == "done",
                    outcome.effective_model,
                    outcome.had_error,
                )
            )

    assert seen == [(True, "sonnet", False)]
    assert outcome.cost_usd == 0.5
    assert outcome.usage == {"input_tokens": "1"}
    assert outcome.quota == {"five_hour": "2"}


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


@pytest.mark.asyncio
async def test_user_stop_persists_everything_streamed_not_the_terminal_tail() -> None:
    """A Stop keeps the whole streamed reply, not the provider's last block.

    The Claude SDK's terminal ``result`` carries only the final text block of a
    multi-block turn, and opencode joins its parts with separators. The client
    rendered every streamed delta, so the stopped turn must persist exactly
    that text; otherwise a reload loses the earlier blocks (#1223).
    """
    streamed_blocks = ["first block of the reply\n", "second block of the reply"]
    host = _PassHost(
        _Provider(
            [
                *(AssistantTextDelta(type="text", text=t) for t in streamed_blocks),
                ResultEvent(
                    type="result",
                    result="second block of the reply",
                    session_id="s1",
                    effective_model="sonnet",
                    is_error=False,
                ),
            ]
        )
    )
    stream = ChatStream()
    host._broker.register("chat-1", stream)
    stream.user_stopped = True
    streaming = ChatStreaming(cast(ChatStreamingHost, host))
    outcome = StreamOutcome()
    request = AgentRequest(prompt="hello", model="opus", mode="auto")

    received = [
        event
        async for event in streaming.drive_stream(
            chat_id="chat-1", request=request, outcome=outcome
        )
    ]

    full = "".join(streamed_blocks)
    terminal = [event for event in received if isinstance(event, ResultEvent)]
    assert len(terminal) == 1
    assert terminal[0].result == full
    assert terminal[0].stopped is True
    assert outcome.response_text == full
    assert outcome.stopped is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("between", "expected"),
    [
        (
            [ToolUseEvent(type="tool_use", tool_name="Read", tool_use_id="t1")],
            "I'll check the file.\n\nThe file says it works.",
        ),
        (
            [ThinkingEvent(type="thinking", text="hmm")],
            "I'll check the file.\n\nThe file says it works.",
        ),
        (
            [
                ToolUseEvent(type="tool_use", tool_name="Read", tool_use_id="t1"),
                ThinkingEvent(type="thinking", text="hmm"),
            ],
            "I'll check the file.\n\nThe file says it works.",
        ),
    ],
)
async def test_user_stop_keeps_a_paragraph_break_between_text_steps(
    between: list[StreamEvent], expected: str
) -> None:
    """Text steps split by a tool call or thinking reload as separate paragraphs.

    The live view draws each step as its own block; a stop must persist the
    same reply, not the steps run together (#1246).
    """
    host = _PassHost(
        _Provider(
            [
                AssistantTextDelta(type="text", text="I'll check the file."),
                *between,
                AssistantTextDelta(type="text", text="The file says it works."),
                ResultEvent(
                    type="result",
                    result="The file says it works.",
                    session_id="s1",
                    effective_model="sonnet",
                    is_error=False,
                ),
            ]
        )
    )
    stream = ChatStream()
    host._broker.register("chat-1", stream)
    stream.user_stopped = True
    streaming = ChatStreaming(cast(ChatStreamingHost, host))
    outcome = StreamOutcome()
    request = AgentRequest(prompt="hello", model="opus", mode="auto")

    received = [
        event
        async for event in streaming.drive_stream(
            chat_id="chat-1", request=request, outcome=outcome
        )
    ]

    terminal = [event for event in received if isinstance(event, ResultEvent)]
    assert len(terminal) == 1
    assert terminal[0].result == expected
    assert outcome.response_text == expected


@pytest.mark.asyncio
async def test_normal_completion_keeps_the_provider_terminal_text() -> None:
    """Without a Stop the provider's terminal text is the reply, untouched."""
    host = _PassHost(
        _Provider(
            [
                AssistantTextDelta(type="text", text="I'll check the file."),
                ToolUseEvent(type="tool_use", tool_name="Read", tool_use_id="t1"),
                AssistantTextDelta(type="text", text="The file says it works."),
                ResultEvent(
                    type="result",
                    result="The file says it works.",
                    session_id="s1",
                    effective_model="sonnet",
                    is_error=False,
                ),
            ]
        )
    )
    streaming = ChatStreaming(cast(ChatStreamingHost, host))
    outcome = StreamOutcome()
    request = AgentRequest(prompt="hello", model="opus", mode="auto")

    received = [
        event
        async for event in streaming.drive_stream(
            chat_id="chat-1", request=request, outcome=outcome
        )
    ]

    terminal = [event for event in received if isinstance(event, ResultEvent)]
    assert terminal[0].result == "The file says it works."
    assert terminal[0].stopped is False


@pytest.mark.asyncio
async def test_a_call_streamed_twice_and_its_result_are_one_tool_row() -> None:
    """Claude streams a call as an input-less partial, then the full message;
    with its result that is three events and one archived row (#1264)."""
    host = _PassHost(
        _Provider(
            [
                ToolUseEvent(type="tool_use", tool_name="Bash", tool_use_id="t1"),
                ToolUseEvent(
                    type="tool_use",
                    tool_name="Bash",
                    tool_use_id="t1",
                    tool_input="ciao chat list",
                    input_chars=30,
                ),
                ToolUseEvent(
                    type="tool_result", tool_name="Bash", tool_use_id="t1", is_error=True
                ),
                ResultEvent(
                    type="result",
                    result="done",
                    session_id="s1",
                    effective_model="sonnet",
                    is_error=False,
                ),
            ]
        )
    )
    streaming = ChatStreaming(cast(ChatStreamingHost, host))
    outcome = StreamOutcome()
    request = AgentRequest(prompt="hello", model="opus", mode="auto")

    _ = [
        event
        async for event in streaming.drive_stream(
            chat_id="chat-1", request=request, outcome=outcome
        )
    ]

    assert outcome.tool_events == [
        {
            "id": "t1",
            "name": "Bash",
            "input": {"summary": "ciao chat list"},
            "input_chars": 30,
            "error": True,
        }
    ]


class _RefreshHost(_DriveHost):
    """A drive host whose repo-location refresh is slow, to test the bounds."""

    def __init__(self, events: list[StreamEvent], workspace_root: Path) -> None:
        super().__init__(events, workspace_root)
        self.refresh_started = asyncio.Event()

    async def refresh_repo_location(self, chat_id, hints):
        self.refresh_started.set()
        await asyncio.sleep(60)
        return {"path": "/never"}


async def _drive_refresh_host(host: _RefreshHost) -> tuple[ChatStream, asyncio.Task]:
    streaming = ChatStreaming(cast(ChatStreamingHost, host))
    stream = ChatStream(prompt_text="hello")
    host._broker.register("chat-1", stream)
    task = asyncio.create_task(
        streaming.drive(
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
    )
    return stream, task


@pytest.mark.asyncio
async def test_slow_repo_location_refresh_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import repo_location

    monkeypatch.setattr(repo_location, "REFRESH_TIMEOUT_S", 0.05)
    host = _RefreshHost([AssistantTextDelta(type="text", text="hi")], tmp_path)
    stream, task = await _drive_refresh_host(host)
    await asyncio.wait_for(task, 5)

    assert stream.done is True
    done = [e for e in host.published if e["type"] == "chat_streaming_done"]
    assert len(done) == 1
    assert "repo_location" not in done[0]


@pytest.mark.asyncio
async def test_cancel_during_repo_location_refresh_still_finishes_the_turn(
    tmp_path: Path,
) -> None:
    host = _RefreshHost(
        [
            AssistantTextDelta(type="text", text="hello"),
            ResultEvent(type="result", result="done", session_id="session-1"),
        ],
        tmp_path,
    )
    stream, task = await _drive_refresh_host(host)
    await asyncio.wait_for(host.refresh_started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The rest of turn end still ran before the cancellation propagated.
    assert "memory-pass-chat-1" in host.detached_names
    assert host.announcements == [("chat-1", "project-1", "Contract", "done")]

    assert stream.done is True
    assert host._broker.get("chat-1") is None
    done = [e for e in host.published if e["type"] == "chat_streaming_done"]
    assert len(done) == 1


async def _drive_events(
    tmp_path: Path, events: list[StreamEvent], agent_root: Path | None = None
):
    host = _DriveHost(events, tmp_path, agent_root)
    host._providers["chat-1"] = SimpleNamespace(
        provider=SimpleNamespace(current_session_id="ses_parent")
    )
    host._notify_question = lambda *_a, **_k: None  # type: ignore[attr-defined]
    host._notify_permission = lambda *_a, **_k: None  # type: ignore[attr-defined]
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
    return host, stream


@pytest.mark.asyncio
async def test_child_request_frames_keep_their_own_session(tmp_path: Path) -> None:
    host, stream = await _drive_events(
        tmp_path,
        [
            ToolUseEvent(
                type="tool_use",
                tool_name="AskUserQuestion",
                tool_input='{"questions": [{"id": "value", "question": "Pick"}]}',
                tool_use_id="frm_child",
                request_id="frm_child",
                session_id="ses_child",
            ),
            PermissionRequestEvent(
                type="system",
                message="Approve use of shell?",
                tool_name="shell",
                tool_input="ls",
                request_id="per_child",
                session_id="ses_child",
            ),
            ResultEvent(type="result", result="done"),
        ],
    )

    frames = {f["type"]: f for f in stream.buffered_events()}
    assert frames["tool_use"]["session_id"] == "ses_child"
    assert frames["permission_request"]["session_id"] == "ses_child"
    assert json.loads(host.chat.pending_question)["session_id"] == "ses_child"


@pytest.mark.asyncio
async def test_frames_without_a_session_get_the_provider_session(
    tmp_path: Path,
) -> None:
    _host, stream = await _drive_events(
        tmp_path,
        [
            ToolUseEvent(
                type="tool_use",
                tool_name="shell",
                tool_input="ls",
                tool_use_id="call_1",
            ),
            ResultEvent(type="result", result="done"),
        ],
    )

    frames = {f["type"]: f for f in stream.buffered_events()}
    assert frames["tool_use"]["session_id"] == "ses_parent"


@pytest.mark.asyncio
async def test_surfaced_touch_resolves_against_the_chat_agent_root(
    tmp_path: Path,
) -> None:
    install_root = tmp_path / "install"
    agent_root = install_root / "personal"
    (agent_root / "memory-vault").mkdir(parents=True)
    (agent_root / "memory-vault" / "x.html").write_text("<p>hi</p>")
    _host, stream = await _drive_events(
        install_root,
        [
            ToolUseEvent(
                type="tool_use",
                tool_name="shell",
                tool_input="ciao file surface memory-vault/x.html",
                tool_use_id="call_1",
                file_touches=[
                    {"file_path": "memory-vault/x.html", "action": "surfaced"}
                ],
            ),
            ToolUseEvent(
                type="tool_use",
                tool_name="Write",
                tool_input="notes/new.md",
                tool_use_id="call_2",
                file_touches=[{"file_path": "notes/new.md", "action": "written"}],
            ),
            ResultEvent(type="result", result="done"),
        ],
        agent_root,
    )

    tool_frames = [f for f in stream.buffered_events() if f["type"] == "tool_use"]
    assert tool_frames[0]["file_touch"] == {
        "file_path": Path("personal/memory-vault/x.html").as_posix(),
        "action": "surfaced",
    }
    assert tool_frames[1]["file_touch"] == {
        "file_path": Path("personal/notes/new.md").as_posix(),
        "action": "created",
    }
