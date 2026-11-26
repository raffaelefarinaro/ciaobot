"""Chat streaming and between-turn provider-drain collaboration.

``ProjectChatManager`` keeps stream admission, request construction, persistence,
and result-announcement policy. This collaborator owns the actual multi-turn
drive loop, the single provider streaming passes, the crash-journal lifecycle,
the between-turns drain, and the transient state those lifecycles need.

The manager-facing methods remain delegating seams. The collaborator calls those
seams through :class:`ChatStreamingHost`, so tests and integrations that patch
``stream_chat``, ``_drive_stream``, ``_spawn_detached``, or a drain method on the
manager still patch the code that executes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from ciao.cli_envelopes import RELOAD_CONTINUE_PROMPT
from ciao.config import CiaoConfig
from ciao.models import (
    AgentRequest,
    AssistantTextDelta,
    ChatContext,
    ContextCompactedEvent,
    ImageAttachment,
    ModelCapabilityQuestionEvent,
    ModelChangedEvent,
    PermissionRequestEvent,
    PermissionResolvedEvent,
    PromptCancelledEvent,
    LATE_WAKE_NOTICE,
    WakeNoticeEvent,
    ResultEvent,
    SessionTitleEvent,
    ShellFinishedEvent,
    StreamEvent,
    SystemStatusEvent,
    ThinkingEvent,
    ToolUseEvent,
)
from ciao.provider_service import ProviderService, capabilities_for
from ciao.providers.connect_errors import annotate_reauth
from ciao.providers.base import QuestionResponseResult
from ciao.sessions import StateStore
from ciao.transcripts import (
    FinalAnswerTracker,
    TextStepJoiner,
    TranscriptStore,
    TurnJournal,
    _journal_event_record,
)
from ciao.web import chat_service
from ciao.web.chat_broker import (
    ChatStream,
    ChatStreamBroker,
    EventsHub,
    apply_file_touches_to_payload,
    event_to_json,
    public_queue,
)
from ciao.web.file_snapshots import SnapshotStore
from ciao import provider_registry, repo_location

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ciao.web.project_chats import ChatInfo

logger = logging.getLogger(__name__)

CAPABILITY_QUESTION_TIMEOUT_S = 30
CAPABILITY_IMAGE_MESSAGE = (
    "Image input not sent — this model can't see images. "
    "Pick a model that supports images and re-send."
)

# SDK/CLI internals that must never reach a rendered or durable turn. A user
# Stop can make the CLI end the turn on one of these diagnostics (and exit
# non-zero) rather than on a clean partial; the strings are not answers.
_SDK_INTERNAL_DIAGNOSTIC_MARKERS = (
    "ede_diagnostic",
    "resulterror",
    "exit code",
)

# What a non-stop failure reads as once its SDK-internal text is dropped. The
# turn is still an error; it just does not expose an internal diagnostic.
_SDK_INTERNAL_ERROR_MESSAGE = (
    "The model turn failed before it produced an answer. "
    'Send "continue" to retry.'
)


def _is_sdk_internal_diagnostic(text: str) -> bool:
    """True when *text* is an SDK/CLI internal rather than a user answer."""
    lowered = text.lower()
    return any(marker in lowered for marker in _SDK_INTERNAL_DIAGNOSTIC_MARKERS)


def _unreported_usage(usage: dict[str, str]) -> bool:
    """True when *usage* is non-empty and every value parses as int 0.

    An accounted model call always reads more than 0 tokens, so an all-zero
    dict (the CLI's interrupt result) means usage was not reported.
    """
    if not usage:
        return False
    for value in usage.values():
        try:
            if int(value) != 0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _final_answer_text(
    result: str, final_text: str | None, tracker: FinalAnswerTracker
) -> str:
    """The reply text a preview or push may excerpt for this turn (#2139).

    A provider that names its final segment wins, including an explicit empty
    string. Otherwise the text the tracker saw after the turn's last top-level
    tool call is the block the chat renders as the answer. A provider that
    streams no deltas — a recovered turn, or one that only sends its terminal
    frame — leaves the terminal result, which is all the host has.
    """
    if final_text is not None:
        return final_text
    segment = tracker.text
    if tracker.split and segment:
        return segment
    return (result or "").strip() or segment


class ChatStreamingHost(Protocol):
    """The complete manager surface used by :class:`ChatStreaming`."""

    _chats: dict[str, ChatInfo]
    _providers: dict[str, ProviderService]
    _config: CiaoConfig
    _state: StateStore
    _transcripts: TranscriptStore
    _broker: ChatStreamBroker
    _events: EventsHub
    _snapshots: SnapshotStore
    _shutting_down: bool

    def _save(self, *, reason: str = ...) -> None: ...

    async def refresh_repo_location(
        self, chat_id: str, hints: Sequence[str]
    ) -> dict[str, Any] | None: ...

    def _get_provider(self, chat_id: str) -> ProviderService: ...

    def _agent_root_for_chat(self, chat_id: str) -> Path: ...

    def _vault_root_for_chat(self, chat_id: str) -> Path | None: ...

    async def reload_provider(self, chat_id: str) -> bool: ...

    def on_prompt_cancelled(
        self,
        chat_id: str,
        stream: ChatStream,
        prompt: str,
        images: list[ImageAttachment] | None,
        turn_index: int | None,
    ) -> None: ...

    @staticmethod
    def _native_question_request_id(question_json: str) -> str: ...

    @staticmethod
    def _rotate_session_id(chat: ChatInfo, new_session_id: str) -> None: ...

    @staticmethod
    def _commit_context_marker(
        chat: ChatInfo, request: AgentRequest, session_id: str
    ) -> bool: ...

    def _record_agent_tool_use(
        self, chat: ChatInfo, request: AgentRequest, event: ToolUseEvent
    ) -> None: ...

    def build_agent_request(
        self,
        chat: ChatInfo,
        *,
        prompt: str,
        display_prompt: str = "",
        images: list[ImageAttachment] | None = None,
        resume_session: str | None = None,
        unattended: bool = False,
        require_mcp: bool = True,
        compact: bool = False,
    ) -> AgentRequest: ...

    def _agent_accepts_images(self, chat: ChatInfo) -> bool: ...

    async def _capability_candidates(
        self, chat: ChatInfo, model: str
    ) -> list[dict[str, object]]: ...

    async def _await_capability_answer(
        self, chat_id: str, request_id: str, timeout_s: float
    ) -> dict[str, object] | None: ...

    def mark_handover_context_used(self, chat_id: str) -> None: ...

    def resolve_image_ref(self, ref: str) -> ImageAttachment | None: ...

    def _notify_permission(
        self, chat_id: str, event: PermissionRequestEvent
    ) -> None: ...

    def _clear_permission_state(
        self, chat_id: str, request_id: str, session_id: str = ""
    ) -> bool: ...

    def _notify_question(self, chat_id: str, question_json: str) -> None: ...

    async def respond_permission(
        self,
        chat_id: str,
        *,
        request_id: str,
        approved: bool,
        reason: str = "",
        session_id: str = "",
    ) -> QuestionResponseResult: ...

    def _arm_retry(
        self,
        chat_id: str,
        stream: ChatStream,
        *,
        kind: str,
        current_prompt: str,
        current_images: list[ImageAttachment] | None,
        had_progress: bool,
        reason: str,
        unattended: bool,
        quota_reset_at: datetime | None = None,
    ) -> bool: ...

    def _stop_result_payload(
        self, chat_id: str, *, turn_index: int | None, text: str
    ) -> dict[str, object]: ...

    async def _auto_title_and_publish(
        self, chat_id: str, user_text: str, assistant_text: str
    ) -> None: ...

    def apply_adapter_title(self, chat_id: str, title: str) -> bool: ...


    def _clear_chat_retry(self, chat: ChatInfo, *, status: str = "") -> None: ...

    def _release_recovery_claim(self, chat_id: str) -> None: ...
    def _owns_retry(self, chat_id: str, stream: ChatStream) -> bool: ...
    def _settle_schedule_retry(self, chat_id: str, *, succeeded: bool) -> None: ...

    @staticmethod
    def _result_snippet(text: str, limit: int = 280) -> str: ...

    @staticmethod
    def _is_worth_announcing_follow_up(text: str) -> bool: ...

    @staticmethod
    def _is_interim_subagent_text(text: str) -> bool: ...

    def notify_turn_started(self, chat_id: str, stream: ChatStream) -> None: ...

    def _park_result_announce(
        self, chat_id: str, project_id: str, title: str, snippet: str
    ) -> None: ...

    def _announce_result_ready(
        self, chat_id: str, project_id: str, title: str, snippet: str
    ) -> None: ...

    async def _maybe_archive_proposal_helper(self, chat_id: str) -> bool: ...

    async def _memory_pass_turn_finished(self, chat_id: str) -> bool: ...

    def _discard_result_announce(self, chat_id: str) -> None: ...

    def _settle_stop_notice(self, chat_id: str) -> None: ...

    def note_unattended_denial(self, chat_id: str) -> None: ...

    def notify_loop_run_end(self, chat_id: str) -> None: ...

    def _flush_result_announce(self, chat_id: str) -> bool: ...

    def _background_work_outstanding(self, chat_id: str) -> bool: ...
    def _note_shell_finished(self, chat_id: str, event: ShellFinishedEvent) -> None: ...
    def _release_shell_finished_waiting(self, chat_id: str) -> None: ...
    def _take_claimed_notice(self, chat_id: str, *, adopt: bool) -> str | None: ...
    def _keep_unsent_echo(self, chat_id: str, text: str) -> None: ...
    def _take_unsent_echo(self, chat_id: str) -> str | None: ...

    def _spawn_detached(
        self, coro: Coroutine[object, object, object], name: str
    ) -> asyncio.Task[object]: ...

    def stream_chat(
        self,
        chat_id: str,
        prompt: str,
        images: list[ImageAttachment] | None = None,
        *,
        unattended: bool = False,
        compact: bool = False,
    ) -> AsyncGenerator[StreamEvent, None]: ...

    def _drive_stream(
        self,
        *,
        chat_id: str,
        request: AgentRequest,
        outcome: StreamOutcome,
    ) -> AsyncGenerator[StreamEvent, None]: ...

    def _record_stopped_turn(
        self,
        chat_id: str,
        chat: ChatInfo,
        request: AgentRequest,
        outcome: StreamOutcome,
        journal: TurnJournal,
    ) -> None: ...

    async def _await_between_turns_drain(self, chat_id: str) -> None: ...

    def _cancel_between_turns_drain(self, chat_id: str) -> None: ...

    def _start_between_turns_drain(self, chat_id: str, project_id: str) -> None: ...

    async def _drain_between_turns(self, chat_id: str, project_id: str) -> None: ...


@dataclass(slots=True)
class StreamOutcome:
    """Terminal state accumulated by one provider streaming pass."""

    events: list[StreamEvent] = field(default_factory=list)
    response_text: str = ""
    had_error: bool = False
    # True when the terminal result was a user stop normalized from the
    # provider's empty error frame. Distinct from `had_error`: the pass is a
    # cancellation, so transcript, announcement and retry decisions must all
    # treat it as non-error and partial.
    stopped: bool = False
    effective_model: str = ""
    usage: dict[str, str] = field(default_factory=dict)
    quota: dict[str, str] = field(default_factory=dict)
    cost_usd: float = 0.0
    tool_events: list[dict[str, Any]] = field(default_factory=list)


# How long a background Stop waits for the idle drain to unwind before its
# handoff gives up on it and releases the teardown registration.
_STOP_HANDOFF_DRAIN_TIMEOUT_S = 10.0


class ChatStreaming:
    """Own chat drive-loop, provider-stream, and drain lifecycles."""

    def __init__(self, host: ChatStreamingHost) -> None:
        self._host = host
        self._turn_perf_started: dict[tuple[str, int], float] = {}
        self._between_turn_drains: dict[str, asyncio.Task[None]] = {}
        self._drive_tasks: dict[str, tuple[ChatStream, asyncio.Task[None]]] = {}
        # A background Stop's shared teardown: the drain unwind plus the
        # provider abort. A foreground drive joins it before it enters.
        self._stop_handoffs: dict[str, asyncio.Task[None]] = {}
        # A foreground drive that has not entered the provider yet. The event
        # is its withdrawal signal: Stop sets it to wake the wait.
        self._pre_entry: dict[str, tuple[ChatStream, asyncio.Event]] = {}

    @property
    def turn_perf_started(self) -> dict[tuple[str, int], float]:
        return self._turn_perf_started

    @property
    def between_turn_drains(self) -> dict[str, asyncio.Task[None]]:
        return self._between_turn_drains

    def _drain_pending(self, chat_id: str) -> asyncio.Task[None] | None:
        task = self._between_turn_drains.get(chat_id)
        return task if task is not None and not task.done() else None

    def has_live_drain(self, chat_id: str) -> bool:
        return self._drain_pending(chat_id) is not None

    def teardown_unsettled(self, chat_id: str) -> bool:
        """True while a Stop handoff or a not-yet-entered drive is outstanding.

        An idle or cancelling drain never counts: every Claude chat has one
        between turns, and a stuck one must not make the chat undeletable.
        """
        return chat_id in self._stop_handoffs or chat_id in self._pre_entry

    def pending_stop_handoff(self, chat_id: str) -> asyncio.Task[None] | None:
        task = self._stop_handoffs.get(chat_id)
        if task is None or task.done():
            return None
        return task

    async def join_stop_handoff(self, chat_id: str) -> None:
        task = self.pending_stop_handoff(chat_id)
        if task is not None:
            await self._join_task(task)

    def begin_background_stop(
        self, chat_id: str, finish: Callable[[], Awaitable[None]]
    ) -> asyncio.Task[None]:
        """Start (or share) the teardown that follows a background Stop."""
        pending = self.pending_stop_handoff(chat_id)
        if pending is not None:
            return pending

        async def _handoff() -> None:
            try:
                await asyncio.wait_for(
                    self._host._await_between_turns_drain(chat_id),
                    _STOP_HANDOFF_DRAIN_TIMEOUT_S,
                )
            except TimeoutError:
                # A drain that ignores cancellation must not hold the
                # registration forever: it would keep the chat undeletable.
                logger.warning(
                    "Drain for chat %s did not unwind within %.0fs of Stop; "
                    "continuing the handoff",
                    chat_id,
                    _STOP_HANDOFF_DRAIN_TIMEOUT_S,
                )
            try:
                await finish()
            except Exception:
                logger.exception("Background stop teardown failed for chat %s", chat_id)

        task = asyncio.create_task(_handoff())
        self._stop_handoffs[chat_id] = task

        def _settled(finished: asyncio.Task[None]) -> None:
            if self._stop_handoffs.get(chat_id) is finished:
                self._stop_handoffs.pop(chat_id, None)

        task.add_done_callback(_settled)
        return task

    def request_pre_entry_stop(self, chat_id: str, stream: ChatStream) -> bool:
        """Wake a drive still waiting on its predecessor so it withdraws."""
        slot = self._pre_entry.get(chat_id)
        if slot is None or slot[0] is not stream:
            return False
        slot[1].set()
        return True

    async def _join_predecessors(self, chat_id: str) -> None:
        await self._host._await_between_turns_drain(chat_id)
        await self.join_stop_handoff(chat_id)

    async def _await_predecessor(self, chat_id: str, stream: ChatStream) -> bool:
        """Wait for the previous drain and stop handoff; True if Stop withdrew us."""
        slot = self._pre_entry.get(chat_id)
        if self._drain_pending(chat_id) is None and self.pending_stop_handoff(chat_id) is None:
            # Nothing to wait for: stay synchronous, as a drive with no
            # predecessor always was, so a cancel lands in the turn itself.
            await self._host._await_between_turns_drain(chat_id)
            if slot is not None and slot[0] is stream:
                self._pre_entry.pop(chat_id, None)
            return stream.user_stopped
        event = slot[1] if slot is not None and slot[0] is stream else asyncio.Event()
        join = asyncio.create_task(self._join_predecessors(chat_id))
        stopper = asyncio.create_task(event.wait())
        try:
            await asyncio.wait({join, stopper}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            stream.finish()
            self._host._broker.clear(chat_id, stream)
            raise
        finally:
            join.cancel()
            stopper.cancel()
            await asyncio.gather(join, stopper, return_exceptions=True)
            current = self._pre_entry.get(chat_id)
            if current is not None and current[0] is stream:
                self._pre_entry.pop(chat_id, None)
        return stream.user_stopped

    def drive_task(
        self, chat_id: str, stream: ChatStream | None = None
    ) -> asyncio.Task[None] | None:
        current = self._drive_tasks.get(chat_id)
        if current is None or (stream is not None and current[0] is not stream):
            return None
        return current[1]

    async def wait_for_drive_cleanup(
        self, chat_id: str, stream: ChatStream | None, timeout_s: float
    ) -> bool:
        task = self.drive_task(chat_id, stream)
        if task is None:
            return True
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
        except asyncio.TimeoutError:
            return False
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Chat drive cleanup failed for %s", chat_id)
        return True

    def start_turn_perf(self, chat_id: str, turn_index: int) -> None:
        self._turn_perf_started[(chat_id, turn_index)] = time.perf_counter()

    def take_turn_perf(self, chat_id: str, turn_index: int) -> float | None:
        return self._turn_perf_started.pop((chat_id, turn_index), None)

    def discard_turn_perf(self, chat_id: str, turn_index: int) -> None:
        self._turn_perf_started.pop((chat_id, turn_index), None)

    def start_drive(
        self,
        *,
        chat_id: str,
        project_id: str,
        prompt: str,
        images: list[ImageAttachment] | None,
        turn_index: int | None,
        chat_meta: ChatInfo | None,
        stream: ChatStream,
        is_retry: bool,
        unattended: bool,
        compact: bool = False,
    ) -> None:
        self._pre_entry[chat_id] = (stream, asyncio.Event())
        task = asyncio.create_task(
            self.drive(
                chat_id=chat_id,
                project_id=project_id,
                prompt=prompt,
                images=images,
                turn_index=turn_index,
                chat_meta=chat_meta,
                stream=stream,
                is_retry=is_retry,
                unattended=unattended,
                compact=compact,
            )
        )
        self._drive_tasks[chat_id] = (stream, task)

        def _clear(finished: asyncio.Task[None]) -> None:
            current = self._drive_tasks.get(chat_id)
            if current is not None and current[1] is finished:
                self._drive_tasks.pop(chat_id, None)
            # A drive cancelled before its first step never reaches
            # ``_await_predecessor``'s cleanup; the slot is identity-checked on
            # the stream so a newer admission's slot stays.
            slot = self._pre_entry.get(chat_id)
            if slot is not None and slot[0] is stream:
                self._pre_entry.pop(chat_id, None)

        task.add_done_callback(_clear)
        if not unattended:
            try:
                self._host.notify_turn_started(chat_id, stream)
            except Exception:
                logger.exception("Turn-start notification failed for chat %s", chat_id)

    def _clear_stable_context(self, chat_id: str) -> None:
        """Forget the stable-context marker so the next user turn re-sends the stable facts."""
        chat = self._host._chats.get(chat_id)
        if chat is not None:
            chat.context_digest = ""
            chat.context_session_id = ""
            self._host._save()

    def _save_at_turn_end(self, chat_id: str) -> None:
        """Persist the registry at turn end without letting a failed write skip teardown.

        ``_save`` replaces the state file, and on Windows that can fail while another
        process briefly holds it. A raise in ``drive``'s ``finally`` skipped
        ``stream.finish()``, so the chat streamed forever. The in-memory state is
        already right; the next successful save persists it.
        """
        try:
            self._host._save()
        except OSError:
            logger.exception("chat registry save failed at turn end for %s", chat_id)

    async def drive(
        self,
        *,
        chat_id: str,
        project_id: str,
        prompt: str,
        images: list[ImageAttachment] | None,
        turn_index: int | None,
        chat_meta: ChatInfo | None,
        stream: ChatStream,
        is_retry: bool,
        unattended: bool,
        compact: bool = False,
    ) -> None:
        """Drive the admitted stream and every queued follow-up turn.

        Only the first pass can be a compaction: queued follow-ups are user
        messages and run as ordinary turns.
        """
        current_prompt = prompt
        current_images = images
        current_turn_index = turn_index
        # ``prompt`` is the bare command when the drive began as a compaction,
        # so the late title below must not read it as the user's words.
        began_compact = compact
        turn_unattended = unattended
        last_assistant_text = ""
        # The last turn's final answer segment, for the unread preview and the
        # result push. Kept apart from ``last_assistant_text``, which stays the
        # complete reply for the transcript handover and the title (#2139).
        last_answer_text = ""
        had_error = False
        # One-line error text that ended the turn, persisted as chat.last_error.
        last_error_text = ""
        had_provider_progress = False
        repo_hints: list[str] = []
        withdrawn = await self._await_predecessor(chat_id, stream)
        # A compaction is a command, never a chip; only the first pass can be
        # withdrawn, so the flag is consumed there.
        withdraw_pending = withdrawn and not compact

        hidden_turn = False
        retry_armed = False

        def _arm_retry(*args: Any, **kwargs: Any) -> bool:
            # A compaction is a command, and a hidden continuation is not a
            # user prompt: neither is replayed on a retry.
            nonlocal retry_armed
            if compact or hidden_turn:
                return False
            armed = bool(self._host._arm_retry(*args, **kwargs))
            retry_armed = retry_armed or armed
            return armed

        try:
            while True:
                turn_assistant_text = ""
                turn_answer_text = ""
                # True only once this turn produced a successful terminal (or a
                # force-closed partial): the selected answer then replaces the
                # carry-over even when it is empty (#2139 review).
                turn_answer_ready = False
                turn_succeeded = False
                # The turn index the answer is filed under.
                turn_verify_index: int | None = None
                turn_publish_generation = stream.begin_publish_turn(current_turn_index)
                turn_steps = TextStepJoiner()
                turn_segment = FinalAnswerTracker()
                turn_result_published = False
                question_paused = False

                async def _run_turn(
                    run_prompt: str = current_prompt,
                    run_images: list[ImageAttachment] | None = current_images,
                    run_unattended: bool = turn_unattended,
                    run_turn_index: int | None = current_turn_index,
                    run_compact: bool = compact,
                ) -> None:
                    nonlocal turn_assistant_text, turn_answer_text, turn_answer_ready
                    nonlocal turn_succeeded, turn_verify_index
                    nonlocal question_paused, had_error, last_error_text
                    nonlocal had_provider_progress, turn_result_published
                    nonlocal last_assistant_text, last_answer_text
                    # Set once Stop cancelled this turn's prompt: the turn's
                    # bookkeeping was rolled back, so its result must not
                    # recreate any of it.
                    prompt_cancelled = False
                    async for event in self._host.stream_chat(
                        chat_id,
                        run_prompt,
                        images=run_images,
                        unattended=run_unattended,
                        compact=run_compact,
                    ):
                        if isinstance(event, PromptCancelledEvent):
                            self._host.on_prompt_cancelled(
                                chat_id, stream, run_prompt, run_images, run_turn_index
                            )
                            prompt_cancelled = True
                            continue
                        if isinstance(event, PermissionResolvedEvent):
                            self._host._clear_permission_state(
                                chat_id, event.request_id, event.session_id
                            )
                            continue
                        payload = event_to_json(event)
                        if payload and isinstance(
                            event,
                            (PermissionRequestEvent, ToolUseEvent),
                        ):
                            provider_service = self._host._providers.get(chat_id)
                            provider_session = str(
                                getattr(
                                    getattr(provider_service, "provider", None),
                                    "current_session_id",
                                    "",
                                )
                                or ""
                            )
                            # A request from a task/subagent child session keeps its
                            # own session: the reply must be addressed there (#1617).
                            if provider_session and not payload.get("session_id"):
                                payload["session_id"] = provider_session
                        if payload:
                            apply_file_touches_to_payload(
                                payload,
                                workspace_root=self._host._config.workspace_root,
                                agent_root=self._host._agent_root_for_chat(chat_id),
                            )
                        if (
                            payload
                            and isinstance(event, ResultEvent)
                            and run_turn_index is not None
                            and not prompt_cancelled
                        ):
                            completed_at = chat_service._now_iso()
                            started_perf = self.take_turn_perf(chat_id, run_turn_index)
                            duration_ms: int | None = None
                            if started_perf is not None:
                                duration_ms = int(
                                    (time.perf_counter() - started_perf) * 1000
                                )
                            cm = self._host._chats.get(chat_id)
                            if cm is not None:
                                with stream.publish_lock:
                                    rec = cm.user_turn_timings.setdefault(
                                        str(run_turn_index), {}
                                    )
                                    rec["completed_at"] = completed_at
                                    if duration_ms is not None:
                                        rec["duration_ms"] = duration_ms
                                    turn_verify_index = run_turn_index
                                    sent_at_rec = rec.get("sent_at", "")
                                self._host._save()
                            else:
                                sent_at_rec = ""
                            payload["completed_at"] = completed_at
                            if sent_at_rec:
                                payload["sent_at"] = sent_at_rec
                            if duration_ms is not None:
                                payload["duration_ms"] = duration_ms
                        if prompt_cancelled and isinstance(event, ResultEvent):
                            # The stream just committed this turn's context
                            # marker for a prompt that never ran.
                            cancelled_chat = self._host._chats.get(chat_id)
                            if cancelled_chat is not None:
                                cancelled_chat.context_digest = ""
                                self._host._save()
                        if payload:
                            stream.publish(payload)
                            if isinstance(event, ResultEvent):
                                turn_result_published = True
                        if (
                            run_compact
                            and isinstance(event, ResultEvent)
                            and not event.is_error
                        ):
                            # The compacted session no longer holds the stable
                            # context, so the next user turn must re-send it.
                            self._clear_stable_context(chat_id)
                        if isinstance(event, ContextCompactedEvent):
                            # The agent compacted its own session mid-turn.
                            self._clear_stable_context(chat_id)
                        turn_steps.feed(event)
                        turn_segment.feed(event)
                        if isinstance(
                            event,
                            (
                                AssistantTextDelta,
                                ThinkingEvent,
                                ToolUseEvent,
                                PermissionRequestEvent,
                            ),
                        ):
                            had_provider_progress = True
                        if isinstance(event, PermissionRequestEvent):
                            self._host._notify_permission(chat_id, event)
                            if unattended:
                                self._host.note_unattended_denial(chat_id)
                                permission_result = await self._host.respond_permission(
                                    chat_id,
                                    request_id=event.request_id,
                                    approved=False,
                                    reason=(
                                        "Scheduled runs cannot wait for "
                                        "interactive approval."
                                    ),
                                    session_id=event.session_id,
                                )
                                if not permission_result.ok and permission_result.retryable:
                                    logger.warning(
                                        "unattended permission deny was not acknowledged; retrying",
                                        extra={"chat_id": chat_id, "request_id": event.request_id},
                                    )
                                    permission_result = await self._host.respond_permission(
                                        chat_id,
                                        request_id=event.request_id,
                                        approved=False,
                                        reason=(
                                            "Scheduled runs cannot wait for "
                                            "interactive approval."
                                        ),
                                        session_id=event.session_id,
                                    )
                                if not permission_result.ok:
                                    logger.error(
                                        "unattended permission deny failed",
                                        extra={
                                            "chat_id": chat_id,
                                            "request_id": event.request_id,
                                            "error": permission_result.error,
                                        },
                                    )
                        if (
                            isinstance(event, ToolUseEvent)
                            and event.tool_name == "AskUserQuestion"
                            and event.tool_input.strip()
                        ):
                            question_payload = event.tool_input
                            question_session_id = str((payload or {}).get("session_id") or "")
                            if event.request_id or question_session_id:
                                try:
                                    parsed_question = json.loads(event.tool_input)
                                except (TypeError, json.JSONDecodeError):
                                    parsed_question = {"questions": []}
                                if not isinstance(parsed_question, dict):
                                    parsed_question = {"questions": []}
                                if event.request_id:
                                    parsed_question["request_id"] = event.request_id
                                if question_session_id:
                                    parsed_question["session_id"] = question_session_id
                                question_payload = json.dumps(
                                    parsed_question, ensure_ascii=False
                                )
                            self._host._notify_question(chat_id, question_payload)
                            cm_q = self._host._chats.get(chat_id)
                            if cm_q is not None:
                                cm_q.pending_question = question_payload
                                self._host._save()
                            if event.request_id:
                                if unattended:
                                    q_provider = self._host._providers.get(chat_id)
                                    if q_provider is not None:
                                        try:
                                            await q_provider.stop_active()
                                        except Exception:
                                            logger.exception(
                                                "interrupt unattended question failed for chat %s",
                                                chat_id,
                                            )
                                    question_paused = True
                                    return
                                continue
                            q_provider = self._host._providers.get(chat_id)
                            if q_provider is not None:
                                try:
                                    await q_provider.stop_active()
                                except Exception:
                                    logger.exception(
                                        "interrupt after AskUserQuestion failed for chat %s",
                                        chat_id,
                                    )
                            question_paused = True
                            return
                        if isinstance(event, ToolUseEvent):
                            repo_hints.extend(repo_location.tool_event_hints(event))
                            touches: list[dict] = []
                            if payload:
                                multi = payload.get("file_touches")
                                if isinstance(multi, list) and multi:
                                    touches = [t for t in multi if isinstance(t, dict)]
                                elif isinstance(payload.get("file_touch"), dict):
                                    touches = [payload["file_touch"]]
                            for touch in touches:
                                fp = touch.get("file_path") or ""
                                if not fp:
                                    continue
                                try:
                                    self._host._snapshots.schedule_capture(
                                        chat_id=chat_id,
                                        file_path=fp,
                                        action=touch.get("action", "touched"),
                                        tool=event.tool_name,
                                    )
                                except Exception:
                                    logger.exception(
                                        "schedule_capture failed for %s", fp
                                    )
                        if isinstance(event, ResultEvent):
                            if event.stopped:
                                # A normalized user stop settles the turn as a
                                # cancellation: it must not arm a retry, mark
                                # had_error, or become the success-announcement
                                # text. The published partial frame already
                                # carries the text the user saw. Clear the
                                # carry-over so a stopped follow-up cannot
                                # re-announce the previous turn's answer.
                                last_assistant_text = ""
                                last_answer_text = ""
                            elif event.is_error:
                                had_error = True
                                result_text = event.result or ""
                                last_error_text = result_text
                                if event.quota_limited or chat_service._is_retryable_quota_error(result_text):
                                    _arm_retry(
                                        chat_id,
                                        stream,
                                        kind="quota",
                                        current_prompt=run_prompt,
                                        current_images=run_images,
                                        had_progress=had_provider_progress,
                                        reason=result_text or "quota limit",
                                        unattended=run_unattended,
                                        quota_reset_at=event.quota_reset_at if event.quota_limited else None,
                                    )
                                elif chat_service._is_retryable_connection_error(
                                    result_text
                                ):
                                    _arm_retry(
                                        chat_id,
                                        stream,
                                        kind="connection",
                                        current_prompt=run_prompt,
                                        current_images=run_images,
                                        had_progress=had_provider_progress,
                                        reason=result_text or "connection error",
                                        unattended=run_unattended,
                                    )
                                elif chat_service._is_retryable_auth_error(result_text):
                                    _arm_retry(
                                        chat_id,
                                        stream,
                                        kind="auth",
                                        current_prompt=run_prompt,
                                        current_images=run_images,
                                        had_progress=had_provider_progress,
                                        reason=result_text or "auth error",
                                        unattended=run_unattended,
                                    )
                            else:
                                turn_assistant_text = event.result or ""
                                turn_answer_text = _final_answer_text(
                                    event.result or "", event.final_text, turn_segment
                                )
                                turn_answer_ready = True
                                turn_succeeded = True

                turn_task: asyncio.Task | None = None
                if stream.user_stopped:
                    # A Stop landed before the turn task existed (start-up or
                    # between turns). Honour it without creating or running the
                    # provider task (#1109): publish the stopped result and
                    # leave the flag for the bottom-of-loop block below, which
                    # parks the queued follow-ups instead of running them (a
                    # Stop holds the whole queue). Clearing the
                    # carry-over keeps this turn from inheriting and
                    # re-announcing the previous turn's answer.
                    last_assistant_text = ""
                    last_answer_text = ""
                    if withdraw_pending:
                        # Stop landed while the drive waited on its
                        # predecessor: the prompt never reached the provider,
                        # so it returns to the waiting queue as a chip.
                        withdraw_pending = False
                        self._host.on_prompt_cancelled(
                            chat_id,
                            stream,
                            current_prompt,
                            current_images,
                            current_turn_index,
                        )
                        current_turn_index = None
                        stream.publish(
                            self._host._stop_result_payload(
                                chat_id, turn_index=None, text=""
                            )
                        )
                    else:
                        stream.publish(
                            self._host._stop_result_payload(
                                chat_id,
                                turn_index=current_turn_index,
                                text=turn_steps.text,
                            )
                        )
                else:
                    turn_task = stream.create_turn_task(
                        _run_turn,
                        generation=turn_publish_generation,
                        name=f"chat-turn-{chat_id}",
                    )
                stream.turn_task = turn_task
                try:
                    if turn_task is not None:
                        await turn_task
                except asyncio.CancelledError:
                    if not stream.force_closing:
                        raise
                    stream.force_closing = False
                    logger.info("Turn force-closed by user stop for chat %s", chat_id)
                    if not turn_result_published:
                        turn_assistant_text = turn_steps.text
                        # Same contract as a completed result: what the answer
                        # block had streamed, else the partial text the user saw.
                        turn_answer_text = _final_answer_text(
                            turn_steps.text, None, turn_segment
                        )
                        turn_answer_ready = True
                        stream.publish(
                            self._host._stop_result_payload(
                                chat_id,
                                turn_index=current_turn_index,
                                text=turn_steps.text,
                            )
                        )
                except Exception as exc:
                    if stream.user_stopped:
                        logger.info("Stream stopped by user for chat %s", chat_id)
                        # The abort acknowledgement is not an answer: clear any
                        # carry-over so this turn cannot inherit and re-announce
                        # the previous turn's text.
                        last_assistant_text = ""
                        last_answer_text = ""
                        if not turn_result_published:
                            stream.publish(
                                self._host._stop_result_payload(
                                    chat_id,
                                    turn_index=current_turn_index,
                                    text=turn_steps.text,
                                )
                            )
                    elif isinstance(exc, ValueError) and "archived chat" in str(exc):
                        logger.info(
                            "Send to archived chat %s rejected mid-stream", chat_id
                        )
                        stream.publish(
                            {
                                "type": "error",
                                "message": "This chat has been archived.",
                                "archived": True,
                            }
                        )
                        had_error = True
                        last_error_text = "This chat has been archived."
                        break
                    else:
                        logger.exception("Stream error for chat %s", chat_id)
                        error_msg = str(exc).strip() or type(exc).__name__
                        stderr = getattr(exc, "stderr", None)
                        if stderr and str(stderr) not in error_msg:
                            error_msg = f"{error_msg}\n{stderr}"
                        # Retry classification reads the raw message; the
                        # rendered/durable text must never expose an SDK
                        # internal such as ``[ede_diagnostic] ...`` or an exit
                        # code. A user-facing provider error is unchanged.
                        display_error = (
                            _SDK_INTERNAL_ERROR_MESSAGE
                            if _is_sdk_internal_diagnostic(error_msg)
                            else error_msg
                        )
                        error_chat = self._host._chats.get(chat_id)
                        error_model = error_chat.model if error_chat else ""
                        error_session = error_chat.session_id if error_chat else ""
                        stream.publish(
                            {
                                "type": "result",
                                "text": display_error,
                                "is_error": True,
                                "effective_model": error_model,
                                "usage": {},
                                "quota": {},
                                "session_id": error_session,
                            }
                        )
                        had_error = True
                        last_error_text = display_error
                        if error_chat is not None and not compact:
                            try:
                                error_request = self._host.build_agent_request(
                                    error_chat,
                                    prompt=current_prompt,
                                    display_prompt=current_prompt,
                                    images=current_images,
                                    resume_session=error_chat.session_id or None,
                                    unattended=turn_unattended,
                                    require_mcp=False,
                                )
                                self._host._transcripts.record_turn(
                                    error_request,
                                    ctx=ChatContext.for_web(chat_id),
                                    response_text=display_error,
                                    effective_model=error_model,
                                    session_id=error_chat.session_id or None,
                                    usage={},
                                    quota={},
                                    input_kind="text",
                                    context_label=error_chat.title,
                                    provider=error_chat.provider,
                                    is_error=True,
                                )
                            except Exception:
                                logger.exception(
                                    "Failed to persist stream error for chat %s",
                                    chat_id,
                                )
                        if chat_service._is_retryable_quota_error(error_msg):
                            _arm_retry(
                                chat_id,
                                stream,
                                kind="quota",
                                current_prompt=current_prompt,
                                current_images=current_images,
                                had_progress=had_provider_progress,
                                reason=error_msg,
                                unattended=turn_unattended,
                            )
                        elif chat_service._is_retryable_connection_error(error_msg):
                            _arm_retry(
                                chat_id,
                                stream,
                                kind="connection",
                                current_prompt=current_prompt,
                                current_images=current_images,
                                had_progress=had_provider_progress,
                                reason=error_msg,
                                unattended=turn_unattended,
                            )
                        elif chat_service._is_retryable_auth_error(error_msg):
                            _arm_retry(
                                chat_id,
                                stream,
                                kind="auth",
                                current_prompt=current_prompt,
                                current_images=current_images,
                                had_progress=had_provider_progress,
                                reason=error_msg,
                                unattended=turn_unattended,
                            )
                        parked = stream.drain_pending()
                        if parked:
                            cm_park = self._host._chats.get(chat_id)
                            if cm_park is not None:
                                cm_park.pending_queue = [*cm_park.pending_queue, *parked]
                                self._host._save()
                                stream.publish_queue_state(cm_park.pending_queue)
                        break
                finally:
                    stream.end_publish_turn(turn_publish_generation)
                    stream.turn_task = None

                if turn_assistant_text:
                    last_assistant_text = turn_assistant_text
                if turn_answer_ready:
                    # A successful terminal always names this turn's answer,
                    # including an explicitly empty one: keeping the previous
                    # turn's text here would re-announce it (#2139 review).
                    last_answer_text = turn_answer_text
                if question_paused:
                    parked = stream.drain_pending()
                    if parked:
                        cm_park = self._host._chats.get(chat_id)
                        if cm_park is not None:
                            cm_park.pending_queue = [*cm_park.pending_queue, *parked]
                            self._host._save()
                            stream.publish_queue_state(cm_park.pending_queue)
                    break

                next_pending = stream.drain_one()
                skipped_blank = False
                while next_pending is not None and not (
                    str(next_pending.get("text", "")).strip()
                    or next_pending.get("images")
                ):
                    # A follow-up with neither text nor images has nothing to
                    # send. Continuing the turn loop with it would re-run the
                    # previous prompt (#1102), so take the next entry instead.
                    # Blank text alone is sendable when images ride along: the
                    # turn goes out with an empty prompt and those images
                    # rather than dropping them without a word (#1112).
                    skipped_blank = True
                    stream.finish_drain_one(next_pending)
                    next_pending = stream.drain_one()
                if skipped_blank:
                    # Keep the entry we are about to run (or park) in the list:
                    # its chip clears on its own user_echo.
                    remaining = stream.pending
                    if next_pending is not None:
                        remaining = [next_pending, *remaining]
                    waiting = stream.waiting() if stream.waiting is not None else []
                    stream.publish({
                        "type": "queue_state",
                        "queue": public_queue(remaining, waiting),
                    })
                # A user Stop holds the whole queue: what is queued is parked
                # on the chat with its chips, not run. (The task board's Stop
                # and Detach, #1103, are the same rule.)
                park_rest = stream.user_stopped
                if stream.user_stopped:
                    stream.user_stopped = False
                reload_requested = stream.reload_continue
                stream.reload_continue = False
                # `ciao chat reload`: drop the provider so the next turn respawns
                # it and resumes the same session with the changed MCP servers,
                # plugins and skills. Consumed whatever happens; a queued user
                # message runs on the fresh provider instead of a hidden turn.
                if reload_requested and not park_rest:
                    reloaded = await self._host.reload_provider(chat_id)
                    if not reloaded:
                        logger.info(
                            "Provider reload skipped for chat %s: still in use", chat_id
                        )
                    elif next_pending is None and not had_error:
                        current_prompt = RELOAD_CONTINUE_PROMPT
                        current_images = None
                        current_turn_index = None
                        compact = False
                        hidden_turn = True
                        continue
                # Stop can arrive while provider reload is awaiting. The
                # earlier snapshot was consumed before that await, so recheck
                # before deciding whether the drained entry runs or parks.
                if stream.user_stopped:
                    park_rest = True
                    stream.user_stopped = False
                if next_pending is None or had_error or park_rest:
                    stream.accepting_queue = False
                    late = stream.drain_pending()
                    parked = (
                        [next_pending, *late]
                        if (had_error or park_rest) and next_pending is not None
                        else late
                    )
                    if parked:
                        cm_park = self._host._chats.get(chat_id)
                        if cm_park is not None:
                            cm_park.pending_queue = [*cm_park.pending_queue, *parked]
                            if next_pending is not None:
                                stream.finish_drain_one(next_pending)
                            self._host._save()
                            stream.publish_queue_state(cm_park.pending_queue)
                    elif next_pending is not None:
                        stream.finish_drain_one(next_pending)
                    break

                combined_text = next_pending.get("text", "").strip()
                turn_origin = str(next_pending.get("origin") or "")
                merged_image_refs: list[str] = list(next_pending.get("images") or [])
                merged_images: list[ImageAttachment] = []
                for ref in merged_image_refs:
                    attachment = self._host.resolve_image_ref(ref)
                    if attachment:
                        merged_images.append(attachment)

                turn_unattended = False
                turn_index2: int | None = None
                sent_at_iso2 = ""
                queued_send_id = str(next_pending.get("send_id") or "")
                queued_send_ids = [queued_send_id] if queued_send_id else []
                queued_send_ids.extend(
                    str(send_id) for send_id in (next_pending.get("send_ids") or [])
                )
                chat_meta2 = self._host._chats.get(chat_id)
                if (
                    chat_meta2 is not None
                    and self._host._native_question_request_id(
                        chat_meta2.pending_question
                    )
                ):
                    # A native form owns the turn until its response is
                    # acknowledged. Put this queued follow-up back in the
                    # durable queue instead of starting a turn that would
                    # erase the form's pending state.
                    parked = [next_pending, *stream.drain_pending()]
                    chat_meta2.pending_queue = [*chat_meta2.pending_queue, *parked]
                    stream.finish_drain_one(next_pending)
                    self._host._save()
                    stream.publish_queue_state(chat_meta2.pending_queue)
                    break
                if chat_meta2 is not None:
                    chat_meta2.pending_question = ""
                    self._host._release_shell_finished_waiting(chat_id)
                    with stream.publish_lock:
                        turn_index2 = chat_meta2.user_turn_count
                        chat_meta2.user_turn_count = turn_index2 + 1
                        chat_meta2.user_turn_ids[str(turn_index2)] = uuid.uuid4().hex
                        if merged_image_refs:
                            chat_meta2.user_turn_images[str(turn_index2)] = list(
                                merged_image_refs
                            )
                        sent_at_iso2 = chat_service._now_iso()
                        chat_meta2.last_activity_at = sent_at_iso2
                        chat_meta2.last_read_at = sent_at_iso2
                        chat_meta2.user_turn_timings[str(turn_index2)] = {
                            "sent_at": sent_at_iso2,
                        }
                        if turn_origin:
                            chat_meta2.user_turn_origin[str(turn_index2)] = turn_origin
                        if queued_send_ids:
                            chat_meta2.record_send_ids(turn_index2, queued_send_ids)
                    self.start_turn_perf(chat_id, turn_index2)
                    self._host._save()
                stream.finish_drain_one(next_pending)

                followup_echo: dict = {
                    "type": "user_echo",
                    "text": combined_text,
                    "images": merged_image_refs,
                    "entry_id": next_pending.get("id"),
                }
                if turn_index2 is not None:
                    followup_echo["turn_index"] = turn_index2
                if sent_at_iso2:
                    followup_echo["sent_at"] = sent_at_iso2
                if turn_origin:
                    followup_echo["origin"] = turn_origin
                if queued_send_id:
                    followup_echo["send_id"] = queued_send_id
                stream.publish(followup_echo)

                current_prompt = combined_text
                current_images = merged_images or None
                current_turn_index = turn_index2
                compact = False
                hidden_turn = False
        finally:
            _late_fallback = chat_service._fallback_title(prompt) if prompt else None
            if (
                prompt
                and not began_compact
                and chat_meta
                and (
                    chat_meta.title == "New Chat"
                    or (
                        _late_fallback is not None and chat_meta.title == _late_fallback
                    )
                )
            ):
                asyncio.create_task(
                    self._host._auto_title_and_publish(
                        chat_id, prompt, last_assistant_text
                    )
                )
            if current_turn_index is not None:
                self.discard_turn_perf(chat_id, current_turn_index)
            # The turn ended in this process, so it is no longer in flight; during
            # shutdown it did not end, and the stamp stays for the next boot (#2128).
            if chat_meta is not None and chat_meta.turn_owner and not self._host._shutting_down:
                chat_meta.turn_owner = ""
                if compact:
                    self._save_at_turn_end(chat_id)
            # ``compact`` is still set only when the last pass was the
            # compaction, which leaves the previous reply as the last one.
            if chat_meta is not None and not compact:
                chat_meta.last_response = last_assistant_text[
                    -chat_service._PROVIDER_HANDOVER_MAX_CHARS :
                ]
                chat_meta.last_response_status = (
                    "error"
                    if had_error
                    else "question"
                    if chat_meta.pending_question
                    else "permission"
                    if chat_meta.pending_permission
                    else "success"
                    if last_assistant_text.strip()
                    else "empty"
                )
                chat_meta.last_error = (
                    annotate_reauth(
                        self._host._result_snippet(last_error_text or "The turn failed."),
                        chat_meta.provider,
                        chat_meta.model,
                    )
                    if had_error
                    else ""
                )
                chat_meta.failure_id = uuid.uuid4().hex if had_error else ""
                # A terminal result event is not proof that a pending permission
                # request was delivered. Keep the persisted card until the response
                # endpoint returns success or an authoritative stale result.
                self._save_at_turn_end(chat_id)
            repo_loc: dict[str, Any] | None = None
            refresh_cancelled = False
            current = asyncio.current_task()
            if chat_meta is not None and not (
                current is not None and current.cancelling()
            ):
                try:
                    # The whole refresh is bounded, lock wait and git included.
                    repo_loc = await asyncio.wait_for(
                        self._host.refresh_repo_location(chat_id, repo_hints),
                        repo_location.REFRESH_TIMEOUT_S,
                    )
                except TimeoutError:
                    logger.warning("repo location refresh timed out for %s", chat_id)
                except asyncio.CancelledError:
                    # Finish the stream, publish done and run the rest of turn end
                    # first; re-raise at the end of the finally.
                    refresh_cancelled = True
                except Exception:  # noqa: BLE001 — location is advisory; never block turn end
                    logger.exception("repo location refresh failed for %s", chat_id)
            # A reload failure, cancellation, or other exception can interrupt
            # the await after drain_one removed an entry. Park it on teardown so
            # its send id remains owned and the user's message is not lost.
            draining = [*stream.take_draining(), *stream.drain_pending()]
            if draining:
                drain_chat = self._host._chats.get(chat_id)
                if drain_chat is not None:
                    drain_chat.pending_queue = [*drain_chat.pending_queue, *draining]
                    self._save_at_turn_end(chat_id)
                    stream.publish_queue_state(drain_chat.pending_queue)
            stream.finish()
            self._host._broker.clear(chat_id, stream)
            done: dict[str, Any] = {
                "type": "chat_streaming_done",
                "chat_id": chat_id,
                "project_id": project_id,
                "is_error": had_error,
                "last_error": chat_meta.last_error if chat_meta is not None else "",
                "failure_id": chat_meta.failure_id if chat_meta is not None else "",
            }
            if repo_loc is not None:
                done["repo_location"] = repo_loc
            self._host._events.publish(done)
            chat_for_drain = self._host._chats.get(chat_id)
            follow_up_expected = False
            if (
                chat_for_drain is not None
                and chat_for_drain.session_id
                and capabilities_for(chat_for_drain.provider).background_subagents
            ):
                self._host._start_between_turns_drain(chat_id, project_id)
                # A chat with a live drain gets a native follow-up turn when
                # its background work finishes; an interim "I'll report back"
                # announce is parked for it and released when the provider
                # reports no background work, or when the drain ends (see
                # `drain_between_turns`).
                follow_up_expected = True
            self._host._discard_result_announce(chat_id)
            # A loop run that failed, was denied or waits on the owner notifies
            # here; a clean one stays quiet (see ProjectChatManager._loop_run_quiet).
            self._host.notify_loop_run_end(chat_id)
            owns_retry = self._host._owns_retry(chat_id, stream)
            if turn_succeeded and not had_error and is_retry and not stream.network_retry_armed and owns_retry:
                chat_now = self._host._chats.get(chat_id)
                if chat_now is not None and chat_now.retry_status == "pending":
                    self._host._clear_chat_retry(chat_now)
            if not had_error and last_answer_text.strip():
                snippet = self._host._result_snippet(last_answer_text)
                chat_now = self._host._chats.get(chat_id)
                if chat_now is not None:
                    chat_now.last_activity_at = chat_service._now_iso()
                    chat_now.last_snippet = snippet
                    self._save_at_turn_end(chat_id)
                title = chat_now.title if chat_now else "Ciaobot"
                # Interim "I'll report back" text parks the announce for the wake.
                interim = follow_up_expected and self._host._is_interim_subagent_text(
                    last_answer_text
                )
                if interim:
                    self._host._park_result_announce(
                        chat_id, project_id, title, snippet
                    )
                else:
                    self._host._announce_result_ready(
                        chat_id, project_id, title, snippet
                    )
                    self._host._spawn_detached(
                        self._host._maybe_archive_proposal_helper(chat_id),
                        f"archive-proposal-helper-{chat_id}",
                    )
            # The reply to a loop stop note was not announced above; its marker is
            # spent with the turn, so the next reply is announced as usual.
            self._host._settle_stop_notice(chat_id)
            # A retried turn that failed with an error no classifier re-armed would
            # otherwise stay pending and re-fire on its old interval forever, never
            # reaching a cap (#2647). Stop it; the chat's last_error keeps the reason.
            if (
                is_retry
                and had_error
                and not began_compact
                and not retry_armed
                and not stream.network_retry_armed
                and owns_retry
            ):
                chat_unsettled = self._host._chats.get(chat_id)
                if chat_unsettled is not None and chat_unsettled.retry_status == "pending":
                    recovery = chat_unsettled.retry_recovery
                    if recovery == "" or (isinstance(recovery, dict) and recovery.get("manual") is True):
                        logger.info("chat %s retry failed without re-arming; stopping it", chat_id)
                        self._host._clear_chat_retry(chat_unsettled, status="stopped")
            if is_retry and owns_retry:
                current_chat = self._host._chats.get(chat_id)
                marker = current_chat.retry_recovery if current_chat is not None else ""
                if current_chat is not None and isinstance(marker, dict) and marker.get("claim"):
                    if had_error:
                        try:
                            self._host._release_recovery_claim(chat_id)
                        except Exception:
                            logger.exception("Could not release recovery claim for %s", chat_id)
                    else:
                        self._host._clear_chat_retry(current_chat, status="stopped")
            if (stream.network_retry_turn or is_retry) and not began_compact and owns_retry:
                self._host._settle_schedule_retry(
                    chat_id, succeeded=not had_error and bool(last_assistant_text.strip())
                )
            # A memory pass settles on EVERY terminal turn, not only a clean one.
            # An errored or empty pass is exactly what `attention` is for: left
            # `running`, it holds its workspace slot forever and every pass
            # queued behind it never starts. Scheduled once here, below the
            # announce, so a clean turn still runs it once rather than twice.
            self._host._spawn_detached(
                self._host._memory_pass_turn_finished(chat_id),
                f"memory-pass-{chat_id}",
            )
            if refresh_cancelled:
                raise asyncio.CancelledError

    async def drive_stream(
        self,
        *,
        chat_id: str,
        request: AgentRequest,
        outcome: StreamOutcome,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Run one provider streaming pass and aggregate its terminal state."""
        chat = self._host._chats.get(chat_id)
        if chat is None:
            raise ValueError(f"Chat '{chat_id}' not found")
        provider = self._host._get_provider(chat_id)
        # The broker stream that owns this provider pass. Stop sets
        # ``user_stopped`` on the stream before the provider reacts, so an
        # explicit stop is read from the same object the drive loop cancels.
        stream = self._host._broker.get(chat_id)
        # Top-level (non-subagent) text seen so far, so an explicit stop that
        # ends on an empty provider terminal still persists what the user saw.
        # Steps are joined with a paragraph break where a tool call or thinking
        # block separated them, as the live view renders them.
        streamed_steps = TextStepJoiner()
        # A compaction keeps the native session id but drops the stable
        # context from its history, so this turn must not re-commit the marker.
        compacted_this_turn = False

        async for event in provider.execute_streaming(request):
            streamed_steps.feed(event)
            if isinstance(event, SessionTitleEvent):
                # The agent's own title goes to the chat, not to the PWA stream.
                self._host.apply_adapter_title(chat_id, event.title)
                continue
            if (
                isinstance(event, ResultEvent)
                and stream is not None
                and stream.user_stopped
                and not event.is_error
                and streamed_steps.text
            ):
                # A user Stop keeps what the client already rendered. A
                # provider's terminal text can be shorter than the stream: the
                # Claude SDK's result carries only the final text block, and
                # opencode joins its parts with separators. Persisting that
                # tail would drop every earlier block on reload (#1223).
                event = replace(event, result=streamed_steps.text, stopped=True)
            if (
                isinstance(event, ResultEvent)
                and event.stopped
                and _unreported_usage(event.usage)
            ):
                event = replace(event, usage={})
            # Aggregate the terminal state before yielding: a consumer that
            # inspects the outcome the moment this event is delivered (the
            # boundary contract) must already see response/usage/error flags.
            if isinstance(event, ResultEvent):
                outcome.response_text = event.result
                outcome.had_error = bool(event.is_error)
                outcome.stopped = bool(event.stopped)
                outcome.effective_model = event.effective_model or chat.model
                # An ACP agent picks its own model when the chat has none; the chat keeps what it resolved.
                descriptor = provider_registry.get(chat.provider)
                if (
                    descriptor is not None
                    and descriptor.acp_spec_path
                    and not chat.model
                    and outcome.effective_model
                ):
                    chat.model = outcome.effective_model
                    self._host._save()
                outcome.usage = event.usage
                outcome.quota = event.quota
                outcome.cost_usd = event.cost_usd or 0.0
            elif isinstance(event, ToolUseEvent):
                # The archive keeps one row per call, not one per event: Claude
                # streams a call twice (an input-less partial, then the full
                # message) and a result settles the call it answers.
                settled = None
                if event.tool_use_id:
                    settled = next(
                        (
                            entry
                            for entry in outcome.tool_events
                            if entry.get("id") == event.tool_use_id
                        ),
                        None,
                    )
                if settled is not None:
                    if event.type == "tool_result":
                        settled["error"] = bool(event.is_error)
                    else:
                        if event.input_chars is not None:
                            settled["input_chars"] = event.input_chars
                        if event.tool_input:
                            settled["input"] = {"summary": event.tool_input}
                else:
                    entry: dict[str, Any] = {
                        "id": event.tool_use_id or "",
                        "name": event.tool_name,
                        "input": {"summary": event.tool_input},
                    }
                    if event.input_chars is not None:
                        entry["input_chars"] = event.input_chars
                    if event.type == "tool_result":
                        entry["error"] = bool(event.is_error)
                    outcome.tool_events.append(entry)
                self._host._record_agent_tool_use(chat, request, event)
            outcome.events.append(event)
            # Session bookkeeping is aggregated before delivery too, so the
            # consumer that inspects the outcome on the terminal event sees the
            # rotated session and committed context marker.
            sdk_sid = provider.current_session_id
            if sdk_sid:
                changed = False
                if sdk_sid != chat.session_id:
                    self._host._rotate_session_id(chat, sdk_sid)
                    changed = True
                if changed:
                    self._host._save()
            # A compaction marks the turn on its own event, so the marker is not
            # re-committed even when no status line precedes it. The status
            # line is still honoured for providers that report only that.
            if isinstance(event, ContextCompactedEvent) or (
                isinstance(event, SystemStatusEvent)
                and (event.status or "").startswith("Compacted context")
            ):
                compacted_this_turn = True
                if chat.context_digest or chat.context_session_id:
                    chat.context_digest = ""
                    chat.context_session_id = ""
                    self._host._save()
            if isinstance(event, ResultEvent):
                result_sid = event.session_id or provider.current_session_id
                if result_sid:
                    changed = False
                    if event.session_id and event.session_id != chat.session_id:
                        self._host._rotate_session_id(chat, event.session_id)
                        changed = True
                    # Only a turn that completed proves the session holds the
                    # stable context: a rejected prompt never reached it.
                    if not event.is_error and not compacted_this_turn:
                        changed = (
                            self._host._commit_context_marker(
                                chat, request, result_sid
                            )
                            or changed
                        )
                    if changed:
                        self._host._save()
            yield event

    def record_stopped_turn(
        self,
        chat_id: str,
        chat: ChatInfo,
        request: AgentRequest,
        outcome: StreamOutcome,
        journal: TurnJournal,
    ) -> None:
        """Persist a force-stopped provider pass as a partial transcript turn."""
        if request.compact:
            return
        try:
            # Only the top-level turn's deltas are the user-visible answer.
            # Subagent text carries a parent_tool_use_id and belongs to the
            # subagent transcript, not this reply.
            steps = TextStepJoiner()
            for event in outcome.events:
                steps.feed(event)
            streamed = steps.text
            self._host._transcripts.record_turn(
                request,
                ctx=ChatContext.for_web(chat_id),
                response_text=outcome.response_text or streamed,
                effective_model=outcome.effective_model or chat.model,
                session_id=chat.session_id or None,
                usage=outcome.usage,
                quota=outcome.quota,
                input_kind="text",
                context_label=chat.title,
                provider=chat.provider,
                tool_events=outcome.tool_events,
                is_error=False,
                is_partial=True,
            )
            journal.mark_committed()
        except Exception:
            logger.exception(
                "Failed to persist force-stopped turn for chat %s", chat_id
            )

    def _persist_terminal_turn(
        self,
        *,
        chat_id: str,
        chat: ChatInfo,
        request: AgentRequest,
        outcome: StreamOutcome,
        terminal: ResultEvent,
        journal: TurnJournal,
    ) -> None:
        """Persist a terminal that reached the outcome before an abort landed.

        Used on the cancellation and exception paths so the exchange is
        durable exactly once with the terminal's actual flags instead of a
        duplicate stopped-partial turn.
        """
        if request.compact:
            return
        try:
            self._host._transcripts.record_turn(
                request,
                ctx=ChatContext.for_web(chat_id),
                response_text=terminal.result,
                effective_model=outcome.effective_model or chat.model,
                session_id=chat.session_id or None,
                usage=outcome.usage,
                quota=outcome.quota,
                input_kind="text",
                context_label=chat.title,
                provider=chat.provider,
                tool_events=outcome.tool_events,
                is_error=bool(terminal.is_error),
                is_partial=bool(terminal.stopped),
            )
            journal.mark_committed()
        except Exception:
            logger.exception(
                "Failed to persist terminal turn for chat %s", chat_id
            )

    async def stream_chat(
        self,
        chat_id: str,
        prompt: str,
        images: list[ImageAttachment] | None = None,
        *,
        unattended: bool = False,
        compact: bool = False,
        capability_timeout_s: float = CAPABILITY_QUESTION_TIMEOUT_S,
        capability_image_message: str = CAPABILITY_IMAGE_MESSAGE,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Run the pre-flight, journal, provider pass, and transcript write."""
        chat = self._host._chats.get(chat_id)
        if chat is None:
            raise ValueError(f"Chat '{chat_id}' not found")
        if chat.archived:
            raise ValueError("Cannot send messages to an archived chat")

        self._host._get_provider(chat_id)
        # A compaction leaves the last reply in place: it is no exchange of its
        # own, and handover and the control plane read the reply.
        if not compact:
            chat.last_response = ""
            chat.last_response_status = "running"
            self._host._save()
        # A compaction sends no capsule, so a pending handover is still unsent.
        handover_context_sent = not compact and bool(
            chat.handover_context_pending and chat.handover_messages
        )

        request = self._host.build_agent_request(
            chat,
            prompt=prompt,
            display_prompt=prompt,
            images=images,
            resume_session=chat.session_id or None,
            unattended=unattended,
            compact=compact,
        )

        response_text = ""
        effective_model = chat.model
        usage: dict[str, str] = {}
        quota: dict[str, str] = {}
        cost_usd = 0.0
        had_error = False
        tool_events: list[dict[str, Any]] = []

        if images and not self._host._agent_accepts_images(chat):
            if unattended:
                yield SystemStatusEvent(
                    type="system",
                    status=capability_image_message,
                )
                return
            request_id = f"cap-{uuid.uuid4().hex[:12]}"
            stream = self._host._broker.get(chat_id)
            registered = stream is not None and stream.open_capability(request_id)
            if registered:
                candidates = await self._host._capability_candidates(
                    chat, request.model
                )
                yield ModelCapabilityQuestionEvent(
                    type="model_capability_question",
                    request_id=request_id,
                    missing="image_input",
                    current_model=chat.model,
                    candidates=candidates,
                    timeout_s=cast(int, capability_timeout_s),
                )
                answer = await self._host._await_capability_answer(
                    chat_id,
                    request_id,
                    capability_timeout_s,
                )
                if answer is None:
                    yield SystemStatusEvent(
                        type="system",
                        status=capability_image_message,
                    )
                    return
                action = str(answer.get("action") or "")
                if action == "switch":
                    picked = str(answer.get("model_id") or "")
                    offered = {str(entry.get("id") or "") for entry in candidates}
                    if picked and picked not in offered:
                        logger.warning(
                            "Ignoring capability switch to unoffered model %r "
                            "for chat %s",
                            picked,
                            chat_id,
                        )
                        yield SystemStatusEvent(
                            type="system",
                            status=capability_image_message,
                        )
                        return
                    if picked and picked != request.model:
                        chat.model = picked
                        self._host._save()
                        yield ModelChangedEvent(
                            type="model_changed",
                            model=picked,
                        )
                        request = self._host.build_agent_request(
                            chat,
                            prompt=prompt,
                            display_prompt=prompt,
                            images=images,
                            resume_session=chat.session_id or None,
                            unattended=unattended,
                        )
                elif action in {"picker", "cancel"}:
                    yield SystemStatusEvent(
                        type="system",
                        status=capability_image_message,
                    )
                    return

        outcome = StreamOutcome(effective_model=chat.model)
        journal = self._host._transcripts.open_turn_journal(
            ChatContext.for_web(chat_id), chat.provider
        )
        # A compaction has no user turn for crash recovery to restore; an
        # unbegun journal records nothing.
        if not request.compact:
            journal.begin(
                {
                    "provider": chat.provider,
                    "prompt": (request.display_prompt or request.prompt)[:2000],
                    "started_at": chat_service._now_iso(),
                }
            )

        async def _journalled_stream() -> AsyncGenerator[StreamEvent, None]:
            async for event in self._host._drive_stream(
                chat_id=chat_id,
                request=request,
                outcome=outcome,
            ):
                record = _journal_event_record(event)
                if record is not None:
                    journal.append(record)
                yield event

        try:
            try:
                async for event in _journalled_stream():
                    yield event
            except asyncio.CancelledError:
                terminal = next(
                    (
                        event
                        for event in outcome.events
                        if isinstance(event, ResultEvent)
                    ),
                    None,
                )
                if terminal is not None:
                    # The provider's terminal already reached the outcome
                    # before the cancellation landed. Persist that turn with
                    # its actual flags instead of synthesizing a second,
                    # stopped partial turn for the same exchange.
                    self._persist_terminal_turn(
                        chat_id=chat_id,
                        chat=chat,
                        request=request,
                        outcome=outcome,
                        terminal=terminal,
                        journal=journal,
                    )
                else:
                    self._host._record_stopped_turn(
                        chat_id, chat, request, outcome, journal
                    )
                raise
            except Exception:
                owning_stream = self._host._broker.get(chat_id)
                terminal = next(
                    (
                        event
                        for event in outcome.events
                        if isinstance(event, ResultEvent)
                    ),
                    None,
                )
                if owning_stream is not None and owning_stream.user_stopped:
                    # An ordinary exception under an explicit stop is the
                    # provider's abort acknowledgement, not a failure: keep the
                    # partial (or a terminal that already reached the outcome)
                    # durable before re-raising to the drive loop's existing
                    # Stop terminal handler. Non-stop exceptions propagate
                    # unchanged.
                    if terminal is not None:
                        self._persist_terminal_turn(
                            chat_id=chat_id,
                            chat=chat,
                            request=request,
                            outcome=outcome,
                            terminal=terminal,
                            journal=journal,
                        )
                    else:
                        self._host._record_stopped_turn(
                            chat_id, chat, request, outcome, journal
                        )
                raise
            response_text = outcome.response_text
            had_error = outcome.had_error
            stopped = outcome.stopped
            effective_model = outcome.effective_model
            usage = outcome.usage
            quota = outcome.quota
            cost_usd = outcome.cost_usd
            tool_events = outcome.tool_events

            if handover_context_sent and not had_error:
                self._host.mark_handover_context_used(chat_id)

            ctx = ChatContext.for_web(chat_id)
            # A compaction is not a user exchange: it has no user turn to file
            # in the durable transcript, only the provider session's own
            # compaction boundary. A prompt Stop cancelled before it ran is not
            # an exchange either: it waits in the queue and is filed when resent.
            prompt_cancelled = any(
                isinstance(e, PromptCancelledEvent) for e in outcome.events
            )
            if not request.compact and not prompt_cancelled:
                self._host._transcripts.record_turn(
                    request,
                    ctx=ctx,
                    response_text=response_text,
                    effective_model=effective_model,
                    session_id=chat.session_id or None,
                    usage=usage,
                    quota=quota,
                    input_kind="text",
                    context_label=chat.title,
                    provider=chat.provider,
                    tool_events=tool_events,
                    is_error=had_error,
                    # A normalized stopped result is an incomplete reply: it is
                    # durable but flagged partial so a reload renders it as cut
                    # short rather than as a finished answer.
                    is_partial=stopped,
                )
            journal.mark_committed()
        finally:
            journal.finish()

        if cost_usd > 0:
            self._host._state.add_cost(cost_usd)
        if usage:
            self._host._state.set_usage(usage)
        if quota:
            self._host._state.set_quota(quota)
        self._host._state.update_session(chat.session_id or None, ctx)

    @staticmethod
    def _request_drain_cancel(task: asyncio.Task[None]) -> None:
        """Ask a drain to cancel once: a second request would hit its cleanup."""
        if not task.done() and task.cancelling() == 0:
            task.cancel()

    @staticmethod
    async def _join_task(task: asyncio.Task[None]) -> None:
        """Wait for ``task`` without owning it.

        The task's own cancellation or failure is swallowed; the waiter's own
        cancellation still propagates, and never cancels the joined task.
        """
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling() > 0:
                raise
        except Exception:  # noqa: BLE001
            return

    def cancel_between_turns_drain(self, chat_id: str) -> None:
        """Fire-and-forget cancel; pair with await_between_turns_drain."""
        task = self._between_turn_drains.get(chat_id)
        if task is not None:
            self._request_drain_cancel(task)

    async def await_between_turns_drain(self, chat_id: str) -> None:
        """Wait until this chat's drain has fully unwound.

        The task stays registered while it unwinds, so every waiter joins the
        same cleanup instead of the second one returning early.
        """
        task = self._between_turn_drains.get(chat_id)
        if task is None:
            return
        if not task.done():
            self._request_drain_cancel(task)
            await self._join_task(task)
        if self._between_turn_drains.get(chat_id) is task:
            self._between_turn_drains.pop(chat_id, None)

    def start_between_turns_drain(self, chat_id: str, project_id: str) -> None:
        provider_service = self._host._providers.get(chat_id)
        if provider_service is None or not provider_service.can_drain:
            return
        owner = self._host._broker.get(chat_id)
        if owner is not None and not owner.background:
            return
        old = self._between_turn_drains.get(chat_id)
        if old is not None and not old.done() and old.cancelling() == 0:
            # A live, uncancelled drain is already the reader.
            return
        if old is not None and not old.done():
            # A withdrawn Stop cancelled this drain while a foreground turn
            # waited. Chain a replacement behind its unwind so a background wake
            # is not lost and two readers never overlap.
            coro = self._replacement_drain(old, chat_id, project_id)
        else:
            coro = self._host._drain_between_turns(chat_id, project_id)
        task = asyncio.create_task(coro)
        self._between_turn_drains[chat_id] = task

        def _settled(finished: asyncio.Task[None]) -> None:
            if self._between_turn_drains.get(chat_id) is finished:
                self._between_turn_drains.pop(chat_id, None)
            if not finished.cancelled():
                finished.exception()

        task.add_done_callback(_settled)

    async def _replacement_drain(
        self, old: asyncio.Task[None], chat_id: str, project_id: str
    ) -> None:
        try:
            await self._join_task(old)
        except asyncio.CancelledError:
            # A waiter cancelled this replacement while the old drain still
            # unwinds. The replacement is the only registered handle on that
            # cleanup, so it must outlive the old drain before it ends, or the
            # next waiter would return while the old drain is untracked.
            await asyncio.wait({old})
            raise
        provider_service = self._host._providers.get(chat_id)
        if provider_service is None or not provider_service.can_drain:
            return
        owner = self._host._broker.get(chat_id)
        if owner is not None and not owner.background:
            return
        await self._host._drain_between_turns(chat_id, project_id)

    def _record_wake_turn(self, chat_id: str, event: ResultEvent, *, claimed: bool = False) -> None:
        """File a turn the provider ran on its own after background work finished.

        The provider's own session store holds the wake, but the durable transcript
        is the fallback copy a reload reads when that store is unreadable. The
        notice is the turn's prompt; ``origin`` keeps it a system row. A wake
        whose wake item was missed has no notice and records nothing, by design.
        """
        chat = self._host._chats.get(chat_id)
        if (
            chat is None
            or event.origin != "task-notification"
            or not event.wake_notice
            # A claimed notice is already on the card in the journal and the live view: its turn is
            # recorded even when the reply is blank, so the durable transcript shows the same card.
            or not (claimed or (event.result or "").strip())
        ):
            return
        try:
            # `record_turn` reads only the model, prompt and mode, so a bare
            # request is enough; the full builder mints tokens and mutates chat.
            request = AgentRequest(
                prompt=event.wake_notice,
                display_prompt=event.wake_notice,
                model=chat.model,
                mode=chat.mode,
                provider=chat.provider,
                resume_session=chat.session_id or None,
            )
            self._host._transcripts.record_turn(
                request,
                ctx=ChatContext.for_web(chat_id),
                response_text=event.result,
                effective_model=event.effective_model,
                session_id=chat.session_id or None,
                usage=event.usage,
                quota=event.quota,
                input_kind="task-notification",
                context_label=chat.title,
                provider=chat.provider,
                is_error=bool(event.is_error),
                is_partial=bool(event.stopped),
                origin="background_run",
            )
        except Exception:
            logger.exception("Failed to record the wake turn for chat %s", chat_id)

    async def drain_between_turns(self, chat_id: str, project_id: str) -> None:
        """Publish provider events that arrive while no user turn is active."""
        provider_service = self._host._providers.get(chat_id)
        if provider_service is None:
            return
        stream: ChatStream | None = None
        cancelled = False
        superseded = False
        events = provider_service.drain_events()

        def close_stream(had_error: bool, last_error: str = "") -> None:
            nonlocal stream
            if stream is None:
                return
            # ``get`` hides a finished stream, so read ownership first. A
            # drain a foreground admission replaced closes its own subscribers
            # but must not publish a global done over the live turn.
            owned = self._host._broker.get(chat_id) is stream
            stream.finish()
            self._host._broker.clear(chat_id, stream)
            if owned:
                self._host._events.publish(
                    {
                        "type": "chat_streaming_done",
                        "chat_id": chat_id,
                        "project_id": project_id,
                        "is_error": had_error,
                        "last_error": last_error,
                    }
                )
            stream = None

        try:
            # The final text block of the follow-up execution now streaming.
            # Reset per terminal result: each wake is its own turn (#2139).
            drain_segment = FinalAnswerTracker()
            # The live card of a notice a continuation claimed; published before the continuation's text.
            # A claimed card a superseded drain did not publish goes out first, here.
            pending_echo: str | None = self._host._take_unsent_echo(chat_id)
            async for event in events:
                # A shell notice is recorded before the supersede check below: the queue
                # gave it up already, so a drain a foreground turn replaces must not lose it.
                if isinstance(event, ShellFinishedEvent):
                    self._host._note_shell_finished(chat_id, event)
                    continue
                if isinstance(event, PermissionResolvedEvent):
                    self._host._clear_permission_state(
                        chat_id, event.request_id, event.session_id
                    )
                    continue
                if isinstance(event, WakeNoticeEvent):
                    # A claimed notice: its card goes out right before the continuation's reply.
                    pending_echo = event.text
                    continue
                # No await sits in this body, so one check covers every side
                # effect of the iteration: a cancelled drain or one a
                # foreground admission replaced must not touch chat state.
                current_task = asyncio.current_task()
                owner = self._host._broker.get(chat_id)
                if (
                    current_task is not None and current_task.cancelling() > 0
                ) or (owner is not None and not owner.background):
                    superseded = True
                    # The claim stays with the chat; its card waits for the next drain (not lost).
                    if pending_echo is not None:
                        self._host._keep_unsent_echo(chat_id, pending_echo)
                    break
                if isinstance(event, ContextCompactedEvent):
                    self._clear_stable_context(chat_id)
                drain_segment.feed(event)
                if isinstance(event, SessionTitleEvent):
                    self._host.apply_adapter_title(chat_id, event.title)
                    continue
                if (
                    isinstance(event, ResultEvent)
                    and event.origin
                    and stream is None
                    and not event.is_error
                    and not (event.result or "").strip()
                ):
                    # The CLI's empty bookkeeping result (turns=0) for a
                    # notification it already folded into a turn: no events,
                    # nothing to show, record or announce (#1394).
                    logger.debug("Skipped an empty follow-up result for chat %s", chat_id)
                    drain_segment = FinalAnswerTracker()
                    continue
                payload = event_to_json(event)
                if payload is None:
                    continue
                apply_file_touches_to_payload(
                    payload,
                    workspace_root=self._host._config.workspace_root,
                    agent_root=self._host._agent_root_for_chat(chat_id),
                )
                if stream is None:
                    stream = ChatStream(background=True)
                    self._host._broker.register(chat_id, stream)
                    self._host._events.publish(
                        {
                            "type": "chat_streaming_started",
                            "chat_id": chat_id,
                            "project_id": project_id,
                            # A wake: the turn before it is finished, so the
                            # client keeps drawing it (no user row follows).
                            "background": True,
                        }
                    )
                if pending_echo is not None:
                    # The same card a wake draws: the notice, then the reply it produced.
                    stream.publish({"type": "user_echo", "text": pending_echo, "origin": "background_run"})
                    pending_echo = None
                stream.publish(payload)
                if isinstance(event, PermissionRequestEvent):
                    self._host._notify_permission(chat_id, event)
                if isinstance(event, ResultEvent):
                    text = event.result or ""
                    answer_text = _final_answer_text(
                        text, event.final_text, drain_segment
                    )
                    drain_segment = FinalAnswerTracker()
                    is_error = bool(event.is_error)
                    claimed_turn = False
                    if event.origin == "task-notification":
                        # The continuation closes: its turn takes the notice it claimed, if any.
                        # Only the provider's default notice is replaced; a real one is kept.
                        claimed = self._host._take_claimed_notice(
                            chat_id, adopt=event.wake_notice == LATE_WAKE_NOTICE
                        )
                        if claimed:
                            event.wake_notice = claimed
                            claimed_turn = True
                    self._record_wake_turn(chat_id, event, claimed=claimed_turn)
                    settled = not self._host._background_work_outstanding(chat_id)
                    chat_now = self._host._chats.get(chat_id)
                    last_error = (
                        annotate_reauth(
                            self._host._result_snippet(text or "The turn failed."),
                            chat_now.provider if chat_now is not None else "",
                            chat_now.model if chat_now is not None else "",
                        )
                        if is_error
                        else ""
                    )
                    close_stream(is_error, last_error)
                    if (
                        not is_error
                        and answer_text
                        and self._host._is_worth_announcing_follow_up(answer_text)
                    ):
                        snippet = self._host._result_snippet(answer_text)
                        if chat_now is not None:
                            chat_now.last_activity_at = chat_service._now_iso()
                            chat_now.last_snippet = snippet
                            chat_now.last_response = text[
                                -chat_service._PROVIDER_HANDOVER_MAX_CHARS :
                            ]
                            chat_now.last_response_status = "success"
                            chat_now.last_error = ""
                            chat_now.failure_id = ""
                            self._host._save()
                        title = chat_now.title if chat_now else "Ciaobot"
                        if settled:
                            self._host._discard_result_announce(chat_id)
                            self._host._announce_result_ready(
                                chat_id, project_id, title, snippet
                            )
                            self._host._spawn_detached(
                                self._host._maybe_archive_proposal_helper(chat_id),
                                f"archive-proposal-helper-{chat_id}",
                            )
                        else:
                            # Background work still running: this follow-up
                            # replaces the parked announce; the one that lands
                            # once the provider reports none announces once.
                            self._host._park_result_announce(
                                chat_id, project_id, title, snippet
                            )
                    else:
                        if chat_now is not None:
                            # Not worth announcing is a notification decision,
                            # not an outcome one: the result still ended a turn,
                            # so its status is persisted here exactly as the
                            # turn-done path persists it. Without this an
                            # errored drain leaves the chat reading as the clean
                            # success that preceded it, and a memory pass
                            # waiting on this result archives an error.
                            chat_now.last_response = text[
                                -chat_service._PROVIDER_HANDOVER_MAX_CHARS :
                            ]
                            chat_now.last_response_status = (
                                "error"
                                if is_error
                                else "success"
                                if text.strip()
                                else "empty"
                            )
                            chat_now.last_error = last_error
                            chat_now.failure_id = uuid.uuid4().hex if is_error else ""
                            self._host._save()
                        if settled:
                            self._host._flush_result_announce(chat_id)
                    # A pass that was waiting on background work ends here, and
                    # ends once per terminal result whatever that result was.
                    self._host._spawn_detached(
                        self._host._memory_pass_turn_finished(chat_id),
                        f"memory-pass-{chat_id}",
                    )
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            logger.exception("Between-turns drain failed for chat %s", chat_id)
        finally:
            close_stream(False)
            # A ``break`` leaves the generator suspended until the loop's
            # asyncgen hook closes it on a later tick. Close it here so its
            # reader is gone before this task ends and a foreground turn or
            # the next drain starts reading.
            try:
                await events.aclose()
            except Exception:
                logger.warning(
                    "Closing the between-turns reader failed for chat %s",
                    chat_id,
                    exc_info=True,
                )
            if not cancelled and not superseded:
                self._host._flush_result_announce(chat_id)
