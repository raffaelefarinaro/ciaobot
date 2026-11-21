"""Contract and ownership-boundary tests for chat streaming."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
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
from ciao.provider_service import ProviderService
from ciao.web.chat_broker import ChatStream, ChatStreamBroker
from ciao.web.chat_streaming import ChatStreaming, ChatStreamingHost, StreamOutcome
from ciao.web.project_chats import ChatInfo, ProjectChatManager
from tests.test_start_stream_enospc_repro import admission_case, consume, driver_probe
from tests.test_memory_pass import _admission_pass, passes_enabled


@pytest.mark.parametrize("unattended", [False, True])
async def test_start_drive_registers_task_before_notifying_observers(admission_case, driver_probe, unattended):
    manager, chat, _ = admission_case
    probe = driver_probe(manager, chat)
    observations = []
    def observer(chat_id, stream):
        task = manager._streaming.drive_task(chat_id, stream)
        assert task is not None and not task.done()
        assert not probe.calls
        observations.append(stream)
    manager.on_turn_started(observer)
    stream = manager.start_stream(chat.chat_id, "owned first", unattended=unattended)
    assert observations == ([] if unattended else [stream])
    probe.release.set()
    await consume(stream)
    assert len(probe.calls) == 1


@pytest.mark.parametrize("host_throws", [False, True])
async def test_start_drive_notification_failure_cannot_orphan_committed_turn(
    admission_case, driver_probe, monkeypatch, host_throws,
):
    manager, chat, _ = admission_case
    probe = driver_probe(manager, chat)
    observed = []
    def fail(*args):
        raise RuntimeError("synthetic observer failure")
    manager.on_turn_started(fail)
    manager.on_turn_started(lambda *args: observed.append(args))
    if host_throws:
        monkeypatch.setattr(manager, "notify_turn_started", fail)
    stream = manager.start_stream(chat.chat_id, "accepted")
    assert manager._streaming.drive_task(chat.chat_id, stream) is not None
    if not host_throws:
        assert len(observed) == 1  # manager isolates each subscriber
    probe.release.set()
    events = await consume(stream)
    assert stream.done and any(event["type"] == "result" for event in events)
    assert len(probe.calls) == 1


@pytest.mark.parametrize("event_type", ["chat_postprocess", "chat_streaming_started"])
async def test_postcommit_publication_failure_does_not_skip_driver_or_change_pending_retry(
    tmp_path, passes_enabled, driver_probe, monkeypatch, event_type,
):
    manager, [(chat, source)], registry = _admission_pass(tmp_path)
    chat.retry_status, chat.retry_prompt = "pending", "previous"
    chat.pending_queue = [{"id": "parked", "text": "later", "images": []}]
    manager._save()
    task = asyncio.create_task(asyncio.Event().wait())
    manager._retry_tasks[chat.chat_id] = task
    probe = driver_probe(manager, chat)
    attempted = []
    publish = manager._events.publish
    def fail_one(event):
        attempted.append(event["type"])
        if event["type"] == event_type:
            assert manager._streaming.drive_task(chat.chat_id) is not None
            raise RuntimeError("synthetic publication failure")
        publish(event)
    monkeypatch.setattr(manager._events, "publish", fail_one)
    try:
        stream = manager.start_stream(chat.chat_id, "continue")
        assert task.cancelling() == 0 and manager._retry_tasks[chat.chat_id] is task
        assert stream.retry_generation == -1
        assert chat.retry_status == "pending" and chat.retry_prompt == "previous"
        assert attempted == ["chat_postprocess", "chat_streaming_started"]
        assert chat.helper["state"] == "running" and chat.user_turn_count == 1
        assert source.postprocess["steps"]["memory_pass"]["status"] == "running"
        assert json.loads(registry.read_bytes())["chats"][chat.chat_id]["user_turn_count"] == 1
        monkeypatch.setattr(manager._events, "publish", publish)
        probe.release.set()
        events = await consume(stream)
        assert any(event["type"] == "queue_state" for event in events)
        assert len(probe.calls) == 1
        assert task.cancelling() == 0 and manager._retry_tasks[chat.chat_id] is task
        assert chat.retry_status == "pending" and chat.retry_prompt == "previous"
    finally:
        monkeypatch.setattr(manager._events, "publish", publish)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def test_explicit_retry_clear_publication_failure_still_invalidates_owner_and_cancels_timer(
    admission_case, monkeypatch,
):
    manager, chat, _ = admission_case
    chat.retry_status, chat.retry_prompt = "pending", "previous"
    manager._save()
    task = asyncio.create_task(asyncio.Event().wait())
    manager._retry_tasks[chat.chat_id] = task
    generation = manager._retry_generations.get(chat.chat_id, 0)

    def fail_publish(event):
        assert event["type"] == "chat_retry"
        raise RuntimeError("synthetic retry publication failure")

    monkeypatch.setattr(manager._events, "publish", fail_publish)
    try:
        with pytest.raises(RuntimeError, match="synthetic retry publication failure"):
            manager._clear_chat_retry(chat)
        assert manager._retry_generations[chat.chat_id] == generation + 1
        assert task.cancelling() == 1 and chat.chat_id not in manager._retry_tasks
        assert chat.retry_status == "" and chat.retry_prompt == ""
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


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
    def _owns_retry(self, chat_id: str, stream: ChatStream) -> bool:
        return True

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
        self.denials: list[str] = []
        self.loop_run_ends: list[str] = []
        self.detached_names: list[str] = []
        self.drain_awaits: list[str] = []

    def _agent_root_for_chat(self, chat_id: str) -> Path:
        del chat_id
        return self._agent_root

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

    def _settle_stop_notice(self, chat_id: str) -> None:
        del chat_id

    def note_unattended_denial(self, chat_id: str) -> None:
        self.denials.append(chat_id)

    def notify_loop_run_end(self, chat_id: str) -> None:
        self.loop_run_ends.append(chat_id)

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

    def _release_shell_finished_waiting(self, chat_id: str) -> None:
        """A held shell notice is released when a pending question clears; this host holds none."""

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
            "failure_id": "",
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
    manager._stop_notice_chats = set()
    manager._held_stop_notes = {}
    manager.schedule_store = None
    manager._streaming = ChatStreaming(cast(ChatStreamingHost, manager))

    manager._turn_perf_started[("chat-1", 0)] = 1.5
    assert manager._streaming.turn_perf_started[("chat-1", 0)] == 1.5

    manager._turn_perf_started = {("chat-2", 1): 2.5}
    assert manager._streaming.turn_perf_started == {("chat-2", 1): 2.5}

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


@pytest.mark.asyncio
async def test_stop_then_send_foreground_must_wait_for_the_drain_cleanup(
    admission_case, driver_probe, monkeypatch,
):
    """A send racing Stop's own drain await must still wait for the cleanup.

    Stop on a background stream awaits the drain "until it has fully unwound"
    (``await_between_turns_drain`` pops the task, cancels, awaits). A send that
    arrives while that drain's cancellation cleanup is still running is an
    ordinary sequence (stop_chat's comment: "Stop followed straight by a send"),
    but the send's drive loop also waits on ``_await_between_turns_drain``
    before its first turn — and the drain task is no longer in the registry
    (it used to pop the slot), so the foreground waiter must not pass its wait until the
    still-blocked drain task has actually completed. ``start_stream`` cancels
    the drain fire-and-forget, and ``cancel_between_turns_drain``'s docstring
    says to "pair with await_between_turns_drain" — that pairing is what this
    race must not break.
    """
    manager, chat, _registry = admission_case
    probe = driver_probe(manager, chat)

    timeline: list[str] = []

    def mark(label: str) -> None:
        timeline.append(label)

    # Evidence anchor: the background drain holds a background broker stream
    # (what drain_between_turns registers while it runs) and its cancellation
    # cleanup unwinds like the real one's `finally`: finish, clear, publish.
    background = ChatStream(prompt_text="background")
    background.background = True
    manager._broker.register(chat.chat_id, background)

    cleanup_entered = asyncio.Event()
    cleanup_gate = asyncio.Event()

    async def drain() -> None:
        try:
            # Blocked while background work streams, exactly like the real
            # drain between turns.
            await asyncio.Event().wait()
        finally:
            # Cancellation cleanup. Blocks on the gate so the Stop-then-send
            # overlap window is deterministic; repeated cancellation must not
            # abort the cleanup mid-way (each retry re-awaits the gate behind
            # a shield and keeps the cleanup contract).
            mark("drain_cleanup_started")
            cleanup_entered.set()
            while not cleanup_gate.is_set():
                try:
                    await asyncio.shield(cleanup_gate.wait())
                except asyncio.CancelledError:
                    pass
            mark("drain_cleanup_finished")
            background.finish()
            manager._broker.clear(chat.chat_id, background)
            manager._events.publish({
                "type": "chat_streaming_done",
                "chat_id": chat.chat_id,
                "project_id": chat.project_id,
                "is_error": False,
                "last_error": "",
            })

    drain_task = asyncio.create_task(drain())
    manager._between_turn_drains[chat.chat_id] = drain_task
    drain_done = asyncio.Event()
    drain_task.add_done_callback(
        lambda _task: (mark("drain_done"), drain_done.set())
    )

    # Instrument the real waiter seam for both waiters: stop's (entered before
    # the send) and the foreground drive's (entered after it).
    waiter_slot = {"n": 0}
    real_await_drain = manager._await_between_turns_drain

    async def traced_await_drain(chat_id: str) -> None:
        waiter_slot["n"] += 1
        enter_label = f"waiter_enter#{waiter_slot['n']}"
        return_label = f"waiter_return#{waiter_slot['n']}"
        mark(enter_label)
        try:
            await real_await_drain(chat_id)
        finally:
            mark(return_label)

    monkeypatch.setattr(manager, "_await_between_turns_drain", traced_await_drain)

    real_publish = manager._events.publish

    def recording_publish(payload: dict) -> None:
        mark(f"publish:{payload.get('type')}")
        real_publish(payload)

    monkeypatch.setattr(manager._events, "publish", recording_publish)

    # Probe the foreground turn's provider entry on top of the driver probe.
    fg_entry = asyncio.Event()
    probed_stream_chat = manager.stream_chat

    async def traced_stream_chat(chat_id, prompt, images=None, **kwargs):
        mark("stream_chat_entered")
        fg_entry.set()
        async for event in probed_stream_chat(chat_id, prompt, images=images, **kwargs):
            yield event

    monkeypatch.setattr(manager, "stream_chat", traced_stream_chat)

    def pos_of(label: str) -> int | None:
        return timeline.index(label) if label in timeline else None

    def evidence() -> str:
        return " -> ".join(timeline)

    async def settle() -> None:
        """Release every held gate and join the tasks this test owns."""
        cleanup_gate.set()
        probe.release.set()
        background.finish()
        for held in (stop_task, drain_task):
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(held, 5)
        for _ in range(10):
            driving = [
                task
                for _, task in list(manager._streaming._drive_tasks.values())
                if not task.done()
            ]
            if not driving:
                break
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.gather(*driving), 5)
        for _ in range(10):
            detached = [
                task for task in manager._detached_tasks if not task.done()
            ]
            if not detached:
                break
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.gather(*detached), 5)

    stop_task = asyncio.create_task(manager.stop_chat(chat.chat_id))
    try:
        # 1. Stop enters the background branch and blocks inside the real
        # await_between_turns_drain: the drain was cancelled and its cleanup
        # is entered but still running. The slot stays registered so a second
        # waiter joins the same cleanup.
        await asyncio.wait_for(cleanup_entered.wait(), 5)
        assert cleanup_entered.is_set() and not cleanup_gate.is_set()
        assert not drain_task.done()
        assert manager._broker.get(chat.chat_id) is background
        assert manager._streaming.between_turn_drains.get(chat.chat_id) is drain_task
        assert not probe.calls  # the send has not started yet

        # 2. A send arrives while the drain cleanup is still pending.
        mark("foreground_send")
        foreground = manager.start_stream(chat.chat_id, "send during stop")
        mark("foreground_registered")
        assert foreground is not background
        assert manager._broker.get(chat.chat_id) is foreground
        assert chat.user_turn_count == 1  # admission committed over the cleanup

        # 3. Let the loop run with the gate still closed (bounded call_soon
        # hops), then watch — briefly — for the foreground turn entering the
        # provider while the drain cleanup is still pending.
        loop = asyncio.get_running_loop()
        for _ in range(8):
            tick = loop.create_future()
            loop.call_soon(tick.set_result, None)
            await tick
        assert not drain_task.done()  # still blocked on the gate
        entered_during_cleanup = False
        try:
            await asyncio.wait_for(fg_entry.wait(), 0.5)
            entered_during_cleanup = True
        except asyncio.TimeoutError:
            entered_during_cleanup = False

        # 4. The ownership contract, evaluated while the drain is still
        # pending (gate closed): a waiter may only return once the drain task
        # has completed.
        def drain_completed() -> bool:
            return drain_done.is_set()

        # Anchor 1 (instrument+mechanism sanity): stop's waiter is the only
        # one entered before the send.
        assert pos_of("waiter_enter#1") is not None
        assert (pos_of("waiter_enter#1") or 0) < (
            pos_of("foreground_registered") or 0
        )
        registered_pos = pos_of("foreground_registered") or 0
        waiters_before_send = [
            entry
            for entry in timeline[:registered_pos]
            if entry.startswith("waiter_enter#")
        ]
        assert waiters_before_send == ["waiter_enter#1"], evidence()
        stop_return_pos = pos_of("waiter_return#1")
        assert stop_return_pos is None or (
            drain_completed() and (pos_of("drain_done") or 0) < stop_return_pos
        ), (
            "stop's drain wait returned while the drain was still pending: "
            f"{evidence()}"
        )

        # Anchor 2 (the suspected gap): the foreground drive's waiter — the
        # first waiter entered after the send — must not return before the
        # drain task (cleanup included) completed.
        fg_waits = [
            (index, entry)
            for index, entry in enumerate(timeline)
            if index > registered_pos and entry.startswith("waiter_enter#")
        ]
        assert fg_waits, (
            "the foreground drive never reached its predecessor-drain wait: "
            f"entered_during_cleanup={entered_during_cleanup}, timeline={evidence()}"
        )
        fg_enter_label = fg_waits[0][1]
        fg_return_label = fg_enter_label.replace("enter", "return")
        assert pos_of(fg_return_label) is None, (
            "the foreground turn's predecessor-drain wait returned before the "
            f"drain completed its cancellation cleanup: {evidence()}"
        )

        # Anchor 3 (the seam): the foreground turn must not enter the provider
        # before the drain cleanup completed either.
        entered_pos = pos_of("stream_chat_entered")
        assert not entered_during_cleanup or (
            drain_completed() and (pos_of("drain_done") or 0) < (entered_pos or 0)
        ), (
            "the foreground turn entered stream_chat while the previous "
            f"drain's cancellation cleanup was still pending: {evidence()}"
        )

        # 5. Release and join; then the surviving end-state invariants.
        await settle()
        assert (
            (pos_of("drain_done") or -1)
            < (pos_of(fg_return_label) or -1)
            < (pos_of("stream_chat_entered") or -1)
        ), evidence()

        # Control (evaluable now that the gate opened): stop's own waiter
        # returned only after the drain — instrument sanity for Anchor 2.
        assert (
            (pos_of("drain_done") or 0) < (pos_of("waiter_return#1") or 0)
        ), f"stop's drain wait returned before the drain completed: {evidence()}"
        assert stop_task.result() is True
        events = await consume(foreground)
        assert any(event["type"] == "result" for event in events)
        assert foreground.done and background.done
        assert manager._broker.get(chat.chat_id) is None
        # The drain's late cleanup unwound only after the foreground was
        # registered (identity-guarded clear keeps the new owner, but the
        # stale chat_streaming_done publish lands after streaming started).
        cleanup_finished_pos = pos_of("drain_cleanup_finished")
        assert cleanup_finished_pos is not None and cleanup_finished_pos > registered_pos
        assert timeline[cleanup_finished_pos + 1] == "publish:chat_streaming_done"
    finally:
        await settle()


# ---------------------------------------------------------------------------
# #3172: a foreground drive waits for every predecessor and can be withdrawn.
# ---------------------------------------------------------------------------


class _StubProviderService:
    """The provider_service surface the stop and drain paths touch."""

    provider = None

    def __init__(self, *, can_drain: bool = False) -> None:
        self.can_drain = can_drain
        self.background_work_outstanding = False
        self.stop_active_calls = 0
        self.drain_opened = 0
        self.drain_opened_event = asyncio.Event()

    def active_handle(self):
        return None

    async def stop_active(self) -> bool:
        self.stop_active_calls += 1
        return True

    async def disconnect(self) -> None:
        return None

    async def drain_events(self):
        self.drain_opened += 1
        self.drain_opened_event.set()
        await asyncio.Event().wait()
        yield  # pragma: no cover - makes this an async generator


def _hold_background_drain(manager, chat):
    """A background stream plus a drain whose cancellation cleanup is gated."""
    background = ChatStream(prompt_text="background")
    background.background = True
    manager._broker.register(chat.chat_id, background)
    hold = SimpleNamespace(
        background=background,
        gate=asyncio.Event(),
        entered=asyncio.Event(),
        task=None,
    )

    async def drain() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            hold.entered.set()
            while not hold.gate.is_set():
                try:
                    await asyncio.shield(hold.gate.wait())
                except asyncio.CancelledError:
                    pass
            background.finish()
            manager._broker.clear(chat.chat_id, background)

    hold.task = asyncio.create_task(drain())
    manager._between_turn_drains[chat.chat_id] = hold.task
    return hold


async def _ticks(count: int = 12) -> None:
    loop = asyncio.get_running_loop()
    for _ in range(count):
        tick = loop.create_future()
        loop.call_soon(tick.set_result, None)
        await tick


async def _join(*tasks: asyncio.Task) -> None:
    for held in tasks:
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(held, 5)


async def _join_drives(manager) -> None:
    for _ in range(10):
        driving = [
            task
            for _, task in list(manager._streaming._drive_tasks.values())
            if not task.done()
        ]
        detached = [task for task in manager._detached_tasks if not task.done()]
        if not driving and not detached:
            return
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.gather(*driving, *detached), 5)


async def _send_behind_held_drain(manager, chat, **kwargs):
    """Stop's handoff is blocked on a gated drain; a foreground send then waits."""
    hold = _hold_background_drain(manager, chat)
    await _ticks(2)
    stop_bg = asyncio.create_task(manager.stop_chat(chat.chat_id))
    await asyncio.wait_for(hold.entered.wait(), 5)
    foreground = manager.start_stream(chat.chat_id, "send during stop", **kwargs)
    await _ticks()
    return hold, stop_bg, foreground


async def test_stop_while_waiting_withdraws_prompt_to_a_waiting_chip(
    admission_case, driver_probe
):
    manager, chat, _ = admission_case
    probe = driver_probe(manager, chat)
    manager._providers[chat.chat_id] = _StubProviderService()
    hold, stop_bg, foreground = await _send_behind_held_drain(
        manager, chat, send_id="S"
    )
    try:
        assert chat.user_turn_count == 1
        assert await asyncio.wait_for(manager.stop_chat(chat.chat_id), 5) is True
        assert hold.task.done() is False  # the gate is still shut
        assert not probe.calls
        events = await consume(foreground)
        kinds = [
            event["type"]
            for event in events
            if event["type"]
            in {"user_echo", "prompt_cancelled", "queue_state", "result"}
        ]
        assert kinds == ["user_echo", "prompt_cancelled", "queue_state", "result"]
        cancelled = next(e for e in events if e["type"] == "prompt_cancelled")
        assert cancelled["turn_index"] == 0
        results = [e for e in events if e["type"] == "result"]
        assert len(results) == 1 and results[0]["stopped"] is True
        assert chat.user_turn_count == 0
        assert [
            {key: entry[key] for key in ("text", "images", "send_ids")}
            for entry in chat.pending_queue
        ] == [{"text": "send during stop", "images": [], "send_ids": ["S"]}]
        assert manager.has_pending_send(chat.chat_id, "S") is True
        assert manager.find_turn_by_send_id(chat.chat_id, "S") is None
        hold.gate.set()
        await _join(stop_bg, hold.task)
        assert not probe.calls

        entry_id = chat.pending_queue[0]["id"]
        probe.release.set()
        assert await manager.send_queued_now(chat.chat_id, [entry_id])
        await _join_drives(manager)
        assert probe.calls == [(chat.chat_id, "send during stop")]
        assert manager.find_turn_by_send_id(chat.chat_id, "S") is not None
        assert chat.pending_queue == []
        assert "S" in chat.user_turn_send_ids["0"]
    finally:
        hold.gate.set()
        probe.release.set()
        await _join(stop_bg, hold.task)
        await _join_drives(manager)


async def test_unattended_send_withdrawn_while_waiting_is_the_same_chip(
    admission_case, driver_probe
):
    manager, chat, _ = admission_case
    probe = driver_probe(manager, chat)
    manager._providers[chat.chat_id] = _StubProviderService()
    hold, stop_bg, foreground = await _send_behind_held_drain(
        manager, chat, unattended=True
    )
    try:
        assert await asyncio.wait_for(manager.stop_chat(chat.chat_id), 5) is True
        await consume(foreground)
        assert [e["text"] for e in chat.pending_queue] == ["send during stop"]
        assert chat.user_turn_count == 0
        assert chat.user_turn_unattended == {}
        assert not probe.calls
    finally:
        hold.gate.set()
        probe.release.set()
        await _join(stop_bg, hold.task)
        await _join_drives(manager)


async def test_compact_stopped_while_waiting_publishes_a_stopped_result_without_a_chip(
    admission_case, driver_probe
):
    manager, chat, _ = admission_case
    probe = driver_probe(manager, chat)
    manager._providers[chat.chat_id] = _StubProviderService()
    hold, stop_bg, foreground = await _send_behind_held_drain(
        manager, chat, compact=True
    )
    try:
        before = list(chat.pending_queue)
        assert await asyncio.wait_for(manager.stop_chat(chat.chat_id), 5) is True
        events = await consume(foreground)
        assert not [e for e in events if e["type"] == "prompt_cancelled"]
        assert chat.pending_queue == before
        results = [e for e in events if e["type"] == "result"]
        assert len(results) == 1 and results[0]["stopped"] is True
        assert not probe.calls
    finally:
        hold.gate.set()
        probe.release.set()
        await _join(stop_bg, hold.task)
        await _join_drives(manager)


async def test_foreground_waits_for_the_background_stop_abort(
    admission_case, driver_probe, monkeypatch
):
    manager, chat, _ = admission_case
    probe = driver_probe(manager, chat)
    manager._providers[chat.chat_id] = _StubProviderService()
    finish_entered = asyncio.Event()
    finish_gate = asyncio.Event()
    finish_calls: list[str] = []

    async def gated_finish(chat_id: str) -> None:
        finish_calls.append(chat_id)
        finish_entered.set()
        await finish_gate.wait()

    monkeypatch.setattr(manager, "_finish_background_stop", gated_finish)
    hold = _hold_background_drain(manager, chat)
    await _ticks(2)
    stop_task = asyncio.create_task(manager.stop_chat(chat.chat_id))
    try:
        await asyncio.wait_for(hold.entered.wait(), 5)
        hold.gate.set()
        await asyncio.wait_for(finish_entered.wait(), 5)
        assert hold.task.done()
        foreground = manager.start_stream(chat.chat_id, "after the abort")
        await _ticks()
        assert not probe.calls
        finish_gate.set()
        probe.release.set()
        events = await consume(foreground)
        assert any(event["type"] == "result" for event in events)
        assert len(probe.calls) == 1
        assert finish_calls == [chat.chat_id]
        assert await asyncio.wait_for(stop_task, 5) is True
    finally:
        finish_gate.set()
        hold.gate.set()
        probe.release.set()
        await _join(stop_task, hold.task)
        await _join_drives(manager)


async def test_repeated_ownerless_stop_joins_the_same_handoff(
    admission_case, monkeypatch
):
    manager, chat, _ = admission_case
    service = _StubProviderService()
    manager._providers[chat.chat_id] = service
    finish_entered = asyncio.Event()
    finish_gate = asyncio.Event()
    finish_calls: list[str] = []

    async def gated_finish(chat_id: str) -> None:
        finish_calls.append(chat_id)
        finish_entered.set()
        await finish_gate.wait()

    monkeypatch.setattr(manager, "_finish_background_stop", gated_finish)
    hold = _hold_background_drain(manager, chat)
    await _ticks(2)
    first = asyncio.create_task(manager.stop_chat(chat.chat_id))
    second: asyncio.Task | None = None
    try:
        await asyncio.wait_for(hold.entered.wait(), 5)
        hold.gate.set()
        await asyncio.wait_for(finish_entered.wait(), 5)
        assert manager._broker.get(chat.chat_id) is None
        second = asyncio.create_task(manager.stop_chat(chat.chat_id))
        await _ticks()
        assert not second.done() and not first.done()
        assert service.stop_active_calls == 0
        finish_gate.set()
        assert await asyncio.wait_for(first, 5) is True
        assert await asyncio.wait_for(second, 5) is True
        assert finish_calls == [chat.chat_id]
        assert service.stop_active_calls == 0
    finally:
        finish_gate.set()
        hold.gate.set()
        await _join(first, hold.task, *([second] if second else []))


def _drain_case(manager, chat, monkeypatch, script):
    """Real ``drain_between_turns`` over a provider whose events a script yields."""
    chat.session_id = "sess-drain"
    release = asyncio.Event()
    service = _StubProviderService(can_drain=True)

    async def drain_events():
        service.drain_opened += 1
        service.drain_opened_event.set()
        async for event in script(release):
            yield event

    service.drain_events = drain_events  # type: ignore[method-assign]
    manager._providers[chat.chat_id] = service
    seen = SimpleNamespace(published=[], registered=[], memory=[])
    real_publish = manager._events.publish
    real_register = manager._broker.register

    def publish(payload: dict) -> None:
        seen.published.append(payload)
        real_publish(payload)

    def register(chat_id: str, stream: ChatStream) -> None:
        seen.registered.append(stream)
        real_register(chat_id, stream)

    async def memory(chat_id: str) -> None:
        seen.memory.append(chat_id)

    monkeypatch.setattr(manager._events, "publish", publish)
    monkeypatch.setattr(manager._broker, "register", register)
    monkeypatch.setattr(manager, "_memory_pass_turn_finished", memory)
    return service, release, seen


def _late_result(chat, text="late wake"):
    return ResultEvent(
        type="result",
        result=text,
        session_id="",
        is_error=False,
        effective_model=chat.model,
        usage={},
        quota={},
    )


async def test_superseded_drain_event_has_no_side_effects(
    admission_case, driver_probe, monkeypatch
):
    manager, chat, _ = admission_case
    probe = driver_probe(manager, chat)
    chat.last_response = "before"
    chat.last_response_status = "success"

    async def script(release):
        try:
            await release.wait()
        except asyncio.CancelledError:
            pass
        yield _late_result(chat)

    _service, _release, seen = _drain_case(manager, chat, monkeypatch, script)
    manager._streaming.start_between_turns_drain(chat.chat_id, chat.project_id)
    drain_task = manager._between_turn_drains[chat.chat_id]
    await _ticks()
    try:
        foreground = manager.start_stream(chat.chat_id, "now")
        await _join(drain_task)
        assert not [s for s in seen.registered if s.background]
        assert not [
            e for e in seen.published if e["type"] == "chat_streaming_done"
        ]
        assert not [
            e
            for e in seen.published
            if e["type"] == "chat_streaming_started" and e.get("background")
        ]
        assert chat.last_response == "before"
        assert chat.last_response_status == "success"
        await _ticks()
        assert seen.memory == []
        probe.release.set()
        await consume(foreground)
    finally:
        probe.release.set()
        await _join_drives(manager)


async def test_superseded_drain_with_a_background_stream_closes_it_silently(
    admission_case, driver_probe, monkeypatch
):
    manager, chat, _ = admission_case
    probe = driver_probe(manager, chat)

    async def script(release):
        yield AssistantTextDelta(type="text", text="waking")
        try:
            await release.wait()
        except asyncio.CancelledError:
            pass
        yield _late_result(chat)

    _service, _release, seen = _drain_case(manager, chat, monkeypatch, script)
    manager._streaming.start_between_turns_drain(chat.chat_id, chat.project_id)
    drain_task = manager._between_turn_drains[chat.chat_id]
    await _ticks()
    try:
        assert len([s for s in seen.registered if s.background]) == 1
        bg = seen.registered[0]
        foreground = manager.start_stream(chat.chat_id, "now")
        await _join(drain_task)
        assert bg.done
        types = [e["type"] for e in seen.published]
        started = len(types) - 1 - types[::-1].index("chat_streaming_started")
        assert "chat_streaming_done" not in types[started:]
        assert [e for e in seen.published if e["type"] == "chat_streaming_started"][
            -1
        ].get("background") is not True
        probe.release.set()
        await consume(foreground)
    finally:
        probe.release.set()
        await _join_drives(manager)


def _assert_drain_left_no_trace(manager, chat, seen) -> None:
    assert seen.registered == []
    assert seen.published == []
    assert chat.last_response == "before"
    assert chat.last_response_status == "success"


async def test_a_cancelled_drain_with_no_foreground_owner_has_no_side_effects(
    admission_case, monkeypatch
):
    manager, chat, _ = admission_case
    chat.last_response = "before"
    chat.last_response_status = "success"

    async def script(release):
        try:
            await release.wait()
        except asyncio.CancelledError:
            pass
        yield _late_result(chat)

    _service, _release, seen = _drain_case(manager, chat, monkeypatch, script)
    manager._streaming.start_between_turns_drain(chat.chat_id, chat.project_id)
    drain_task = manager._between_turn_drains[chat.chat_id]
    await _ticks()
    assert manager._broker.get(chat.chat_id) is None
    drain_task.cancel()
    await _join(drain_task)
    assert drain_task.done()
    _assert_drain_left_no_trace(manager, chat, seen)
    await _ticks()
    assert seen.memory == []


async def test_a_drain_under_a_foreground_owner_has_no_side_effects_unless_cancelled(
    admission_case, monkeypatch
):
    manager, chat, _ = admission_case
    chat.last_response = "before"
    chat.last_response_status = "success"

    async def script(release):
        await release.wait()
        yield _late_result(chat)

    _service, release, seen = _drain_case(manager, chat, monkeypatch, script)
    manager._streaming.start_between_turns_drain(chat.chat_id, chat.project_id)
    drain_task = manager._between_turn_drains[chat.chat_id]
    await _ticks()
    foreground = ChatStream(prompt_text="live")
    manager._broker.register(chat.chat_id, foreground)
    seen.registered.clear()
    try:
        assert drain_task.cancelling() == 0
        release.set()
        await asyncio.wait_for(drain_task, 5)
        _assert_drain_left_no_trace(manager, chat, seen)
        assert manager._broker.get(chat.chat_id) is foreground
        await _ticks()
        assert seen.memory == []
    finally:
        manager._broker.clear(chat.chat_id, foreground)
        await _join(drain_task)


async def test_owned_background_drain_close_publishes_exactly_one_done(
    admission_case, monkeypatch
):
    manager, chat, _ = admission_case

    async def script(release):
        yield AssistantTextDelta(type="text", text="waking")
        yield _late_result(chat, "all done")
        await release.wait()

    _service, release, seen = _drain_case(manager, chat, monkeypatch, script)
    manager._streaming.start_between_turns_drain(chat.chat_id, chat.project_id)
    drain_task = manager._between_turn_drains[chat.chat_id]
    try:
        await _ticks(20)
        assert (
            len([e for e in seen.published if e["type"] == "chat_streaming_done"])
            == 1
        )
    finally:
        drain_task.cancel()
        release.set()
        await _join(drain_task)
        await _join_drives(manager)
    assert (
        len([e for e in seen.published if e["type"] == "chat_streaming_done"]) == 1
    )


async def test_drain_waiters_share_one_cancel_and_survive_waiter_cancellation(
    admission_case,
):
    manager, chat, _ = admission_case
    hold = _hold_background_drain(manager, chat)
    await _ticks(2)
    streaming = manager._streaming
    first = asyncio.create_task(streaming.await_between_turns_drain(chat.chat_id))
    second = asyncio.create_task(streaming.await_between_turns_drain(chat.chat_id))
    try:
        await asyncio.wait_for(hold.entered.wait(), 5)
        await _ticks()
        assert not first.done() and not second.done()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not second.done()
        assert not hold.task.done()
        assert streaming.between_turn_drains.get(chat.chat_id) is hold.task
        assert hold.task.cancelling() == 1
        hold.gate.set()
        await asyncio.wait_for(second, 5)
        assert hold.task.done()
        assert chat.chat_id not in streaming.between_turn_drains
    finally:
        hold.gate.set()
        await _join(first, second, hold.task)


async def test_withdrawn_stop_leaves_a_replacement_drain_for_background_wakes(
    admission_case, driver_probe, monkeypatch
):
    manager, chat, _ = admission_case
    probe = driver_probe(manager, chat)
    chat.session_id = "sess-drain"
    service = _StubProviderService(can_drain=True)
    manager._providers[chat.chat_id] = service
    hold = _hold_background_drain(manager, chat)
    await _ticks(2)
    replacement = None
    try:
        foreground = manager.start_stream(chat.chat_id, "send behind the drain")
        await asyncio.wait_for(hold.entered.wait(), 5)
        await _ticks()
        assert hold.task.cancelling() == 1 and not hold.task.done()
        assert await asyncio.wait_for(manager.stop_chat(chat.chat_id), 5) is True
        await consume(foreground)
        replacement = manager._between_turn_drains[chat.chat_id]
        assert replacement is not hold.task
        await _ticks()
        assert service.drain_opened == 0
        hold.gate.set()
        await asyncio.wait_for(service.drain_opened_event.wait(), 5)
        assert hold.task.done()
        assert service.drain_opened == 1
        assert manager._between_turn_drains[chat.chat_id] is replacement
        assert not replacement.done()
        # Control: a live, uncancelled drain keeps its task (one reader only).
        manager._streaming.start_between_turns_drain(chat.chat_id, chat.project_id)
        assert manager._between_turn_drains[chat.chat_id] is replacement
        assert service.drain_opened == 1
    finally:
        hold.gate.set()
        if replacement is not None:
            replacement.cancel()
            await _join(replacement)
        await _join(hold.task)
        await _join_drives(manager)


async def test_send_after_a_withdrawn_stop_still_waits_for_the_old_drain(
    admission_case, driver_probe, monkeypatch
):
    manager, chat, _ = admission_case
    probe = driver_probe(manager, chat)
    chat.session_id = "sess-drain"
    service = _StubProviderService(can_drain=True)
    manager._providers[chat.chat_id] = service
    hold = _hold_background_drain(manager, chat)
    await _ticks(2)
    try:
        first = manager.start_stream(chat.chat_id, "send behind the drain")
        await asyncio.wait_for(hold.entered.wait(), 5)
        await _ticks()
        assert await asyncio.wait_for(manager.stop_chat(chat.chat_id), 5) is True
        await consume(first)
        replacement = manager._between_turn_drains[chat.chat_id]
        assert replacement is not hold.task
        # The old drain's cleanup is still held: send again.
        second = manager.start_stream(chat.chat_id, "send again")
        await _ticks()
        assert not probe.calls
        assert not hold.task.done()
        assert not replacement.done()
        assert manager._between_turn_drains[chat.chat_id] is replacement
        hold.gate.set()
        probe.release.set()
        await consume(second)
        assert len(probe.calls) == 1
        assert hold.task.done()
    finally:
        hold.gate.set()
        probe.release.set()
        await _join(hold.task)
        await _join_drives(manager)


async def test_replacement_drain_exits_without_draining_after_teardown(
    admission_case, driver_probe, monkeypatch
):
    manager, chat, _ = admission_case
    probe = driver_probe(manager, chat)
    chat.session_id = "sess-drain"
    service = _StubProviderService(can_drain=True)
    manager._providers[chat.chat_id] = service
    hold = _hold_background_drain(manager, chat)
    await _ticks(2)
    try:
        foreground = manager.start_stream(chat.chat_id, "send behind the drain")
        await asyncio.wait_for(hold.entered.wait(), 5)
        await _ticks()
        assert await asyncio.wait_for(manager.stop_chat(chat.chat_id), 5) is True
        await consume(foreground)
        replacement = manager._between_turn_drains[chat.chat_id]
        assert replacement is not hold.task
        manager._providers.pop(chat.chat_id)
        hold.gate.set()
        await _join(hold.task, replacement)
        assert replacement.done()
        assert service.drain_opened == 0
        assert chat.chat_id not in manager._between_turn_drains
    finally:
        hold.gate.set()
        await _join(hold.task)
        await _join_drives(manager)


async def test_drive_cancelled_while_waiting_finishes_its_stream(
    admission_case, driver_probe
):
    manager, chat, _ = admission_case
    probe = driver_probe(manager, chat)
    hold = _hold_background_drain(manager, chat)
    await _ticks(2)
    try:
        foreground = manager.start_stream(chat.chat_id, "will be cancelled")
        await asyncio.wait_for(hold.entered.wait(), 5)
        await _ticks()
        drive = manager._streaming.drive_task(chat.chat_id, foreground)
        assert drive is not None and not drive.done()
        drive.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drive
        assert foreground.done
        assert manager._broker.get(chat.chat_id) is None
        assert not probe.calls
        assert manager._streaming.teardown_unsettled(chat.chat_id) is False
    finally:
        hold.gate.set()
        probe.release.set()
        await _join(hold.task)
        await _join_drives(manager)


async def test_pre_entry_slot_is_released_when_the_drive_is_cancelled_before_its_first_step(
    admission_case, driver_probe
):
    manager, chat, _ = admission_case
    probe = driver_probe(manager, chat)
    # A held predecessor keeps both drives parked, so the slots stay observable.
    hold = _hold_background_drain(manager, chat)
    await _ticks(2)
    streaming = manager._streaming
    first_stream = ChatStream(prompt_text="first")
    second_stream = ChatStream(prompt_text="second")

    def start(stream: ChatStream) -> None:
        streaming.start_drive(
            chat_id=chat.chat_id,
            project_id=chat.project_id,
            prompt="p",
            images=None,
            turn_index=None,
            chat_meta=chat,
            stream=stream,
            is_retry=False,
            unattended=True,
        )

    start(first_stream)
    first_task = streaming.drive_task(chat.chat_id, first_stream)
    assert first_task is not None
    assert streaming.teardown_unsettled(chat.chat_id) is True
    first_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_task
    assert chat.chat_id not in streaming._pre_entry
    assert streaming.teardown_unsettled(chat.chat_id) is False

    # Identity: a late clear from the first task never removes a newer slot.
    start(first_stream)
    first_task = streaming.drive_task(chat.chat_id, first_stream)
    assert first_task is not None
    start(second_stream)
    second_task = streaming.drive_task(chat.chat_id, second_stream)
    assert second_task is not None
    first_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_task
    assert streaming._pre_entry[chat.chat_id][0] is second_stream
    second_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second_task
    assert chat.chat_id not in streaming._pre_entry
    hold.gate.set()
    probe.release.set()
    await _join(hold.task)
    await _join_drives(manager)


async def test_stop_without_a_cached_provider_still_withdraws_the_waiting_prompt(
    admission_case, driver_probe
):
    manager, chat, _ = admission_case
    probe = driver_probe(manager, chat)
    assert chat.chat_id not in manager._providers
    hold, stop_bg, foreground = await _send_behind_held_drain(manager, chat)
    try:
        slot_event = manager._streaming._pre_entry[chat.chat_id][1]
        assert not slot_event.is_set()
        # No cached provider: the existing contract answers False.
        assert await asyncio.wait_for(manager.stop_chat(chat.chat_id), 5) is False
        assert slot_event.is_set()
        events = await consume(foreground)
        assert len([e for e in events if e["type"] == "prompt_cancelled"]) == 1
        assert not probe.calls
    finally:
        hold.gate.set()
        probe.release.set()
        await _join(stop_bg, hold.task)
        await _join_drives(manager)


# ── the drain reader is closed deterministically (#3172 review) ──────────


class _RecordingProvider:
    """A provider whose drain generator records when it is finalised."""

    can_drain = True
    background_work_outstanding = False

    def __init__(self, chat) -> None:
        self.chat = chat
        self.release = asyncio.Event()
        self.closed = False

    async def drain_events(self):
        try:
            await self.release.wait()
            yield _late_result(self.chat)
            await asyncio.Event().wait()
        finally:
            self.closed = True


def _through_provider_service(manager, chat, monkeypatch):
    provider = _RecordingProvider(chat)
    service = ProviderService(manager._config)
    service._provider = provider  # type: ignore[assignment]
    manager._providers[chat.chat_id] = service
    return provider


async def test_a_superseded_drain_closes_the_provider_reader_before_it_ends(
    admission_case, monkeypatch
):
    manager, chat, _ = admission_case
    provider = _through_provider_service(manager, chat, monkeypatch)
    manager._streaming.start_between_turns_drain(chat.chat_id, chat.project_id)
    drain_task = manager._between_turn_drains[chat.chat_id]
    await _ticks()
    # A foreground owner supersedes the drain: its loop breaks on the next event.
    manager._broker.register(chat.chat_id, ChatStream())
    provider.release.set()
    await _join(drain_task)
    assert drain_task.done()
    assert provider.closed is True


async def test_a_cancelled_drain_closes_the_provider_reader_before_it_ends(
    admission_case, monkeypatch
):
    manager, chat, _ = admission_case
    provider = _through_provider_service(manager, chat, monkeypatch)
    manager._streaming.start_between_turns_drain(chat.chat_id, chat.project_id)
    drain_task = manager._between_turn_drains[chat.chat_id]
    await _ticks()
    drain_task.cancel()
    await _join(drain_task)
    assert drain_task.done()
    assert provider.closed is True


class _FailingCloseEvents:
    """An event iterator whose ``aclose`` raises."""

    def __init__(self, events, *, hold: bool) -> None:
        self._events = list(events)
        self._hold = hold

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._events:
            return self._events.pop(0)
        if self._hold:
            await asyncio.Event().wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        raise RuntimeError("close failed")


async def _idle(release):
    return
    yield  # pragma: no cover


async def test_a_failing_reader_close_still_flushes_the_result_announce(
    admission_case, monkeypatch
):
    manager, chat, _ = admission_case
    service, _release, _seen = _drain_case(manager, chat, monkeypatch, _idle)
    service.drain_events = lambda: _FailingCloseEvents(  # type: ignore[method-assign]
        [_late_result(chat)], hold=False
    )
    flushed: list[str] = []
    monkeypatch.setattr(manager, "_flush_result_announce", flushed.append)
    manager._streaming.start_between_turns_drain(chat.chat_id, chat.project_id)
    drain_task = manager._between_turn_drains[chat.chat_id]
    await _join(drain_task)
    assert drain_task.done() and drain_task.exception() is None
    assert chat.chat_id in flushed


async def test_a_failing_reader_close_does_not_replace_a_cancellation(
    admission_case, monkeypatch
):
    manager, chat, _ = admission_case
    service, _release, _seen = _drain_case(manager, chat, monkeypatch, _idle)
    service.drain_events = lambda: _FailingCloseEvents([], hold=True)  # type: ignore[method-assign]
    manager._streaming.start_between_turns_drain(chat.chat_id, chat.project_id)
    drain_task = manager._between_turn_drains[chat.chat_id]
    await _ticks()
    drain_task.cancel()
    await _join(drain_task)
    assert drain_task.cancelled()


async def test_a_drive_with_no_predecessor_does_not_suspend_before_the_turn(
    admission_case,
):
    manager, chat, _ = admission_case
    stream = ChatStream()
    manager._broker.register(chat.chat_id, stream)
    coro = manager._streaming._await_predecessor(chat.chat_id, stream)
    with pytest.raises(StopIteration) as done:
        coro.send(None)  # a suspension would yield a future instead
    assert done.value.value is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_id", "is_acp", "chat_model", "expected_model"),
    [
        # An ACP agent that picked its own model: the chat keeps it when it had none.
        ("opencode", True, "", "vendor/picked"),
        ("codex", True, "", "vendor/picked"),
        # A chat that already names its model keeps it, whatever the agent reports.
        ("opencode", True, "opus", "opus"),
        # A non-ACP provider is never written back.
        ("claude", False, "", ""),
    ],
)
async def test_empty_model_is_written_back_for_any_acp_agent_only(
    monkeypatch: pytest.MonkeyPatch,
    provider_id: str,
    is_acp: bool,
    chat_model: str,
    expected_model: str,
) -> None:
    from ciao import provider_registry

    descriptor = SimpleNamespace(id=provider_id, acp_spec_path="pkg.mod:SPEC" if is_acp else "")
    monkeypatch.setattr(provider_registry, "get", lambda pid: descriptor if pid == provider_id else None)
    events: list[StreamEvent] = [
        AssistantTextDelta(type="text", text="hi"),
        ResultEvent(type="result", result="hi", session_id="s1", effective_model="vendor/picked"),
    ]
    host = _PassHost(_Provider(events))
    host.chat.provider = provider_id
    host.chat.model = chat_model
    streaming = ChatStreaming(cast(ChatStreamingHost, host))
    outcome = StreamOutcome()

    [_ async for _ in streaming.drive_stream(
        chat_id="chat-1",
        request=AgentRequest(prompt="hello", model=chat_model, mode="auto"),
        outcome=outcome,
    )]

    assert host.chat.model == expected_model
    assert outcome.effective_model == "vendor/picked"
