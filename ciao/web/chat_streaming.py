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
from collections.abc import AsyncGenerator, Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast

from ciao.config import CiaoConfig
from ciao.models import (
    AgentRequest,
    AssistantTextDelta,
    ChatContext,
    ImageAttachment,
    ModelCapabilityQuestionEvent,
    ModelChangedEvent,
    PermissionRequestEvent,
    ResultEvent,
    StreamEvent,
    SystemStatusEvent,
    ThinkingEvent,
    ToolUseEvent,
)
from ciao.provider_service import ProviderService, capabilities_for
from ciao.sessions import StateStore
from ciao.transcripts import (
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
)
from ciao.web.file_snapshots import SnapshotStore

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ciao.web.project_chats import ChatInfo

logger = logging.getLogger(__name__)

CAPABILITY_QUESTION_TIMEOUT_S = 30
CAPABILITY_IMAGE_MESSAGE = (
    "Image input not sent — this model can't see images. "
    "Pick a model that supports images and re-send."
)


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

    def _save(self, *, reason: str = ...) -> None: ...

    def _get_provider(self, chat_id: str) -> ProviderService: ...

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
    ) -> AgentRequest: ...

    async def _model_capable(self, model: str, chat: ChatInfo) -> bool: ...

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

    def _notify_question(self, chat_id: str, question_json: str) -> None: ...

    def respond_permission(
        self,
        chat_id: str,
        *,
        request_id: str,
        approved: bool,
        reason: str = "",
    ) -> bool: ...

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
    ) -> bool: ...

    def _stop_result_payload(
        self, chat_id: str, *, turn_index: int | None, text: str
    ) -> dict[str, object]: ...

    async def _auto_title_and_publish(
        self, chat_id: str, user_text: str, assistant_text: str
    ) -> None: ...

    def _start_subagent_watcher(self, chat_id: str, project_id: str) -> None: ...

    def _clear_chat_retry(self, chat: ChatInfo, *, status: str = "") -> None: ...

    @staticmethod
    def _result_snippet(text: str, limit: int = 280) -> str: ...

    @staticmethod
    def _is_worth_announcing_nudge_reply(text: str) -> bool: ...

    @staticmethod
    def _is_interim_subagent_text(text: str) -> bool: ...

    def _park_result_announce(
        self, chat_id: str, project_id: str, title: str, snippet: str
    ) -> int: ...

    def _announce_result_ready(
        self, chat_id: str, project_id: str, title: str, snippet: str
    ) -> None: ...

    async def _maybe_archive_proposal_helper(self, chat_id: str) -> bool: ...

    def _discard_result_announce(
        self, chat_id: str, token: int | None = None
    ) -> None: ...

    def _flush_result_announce(
        self, chat_id: str, token: int | None = None
    ) -> bool: ...

    def _cancel_parked_announce_deadline(self, chat_id: str) -> None: ...

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
    effective_model: str = ""
    usage: dict[str, str] = field(default_factory=dict)
    quota: dict[str, str] = field(default_factory=dict)
    cost_usd: float = 0.0
    tool_events: list[dict[str, Any]] = field(default_factory=list)


class ChatStreaming:
    """Own chat drive-loop, provider-stream, and drain lifecycles."""

    def __init__(self, host: ChatStreamingHost) -> None:
        self._host = host
        self._turn_perf_started: dict[tuple[str, int], float] = {}
        self._between_turn_drains: dict[str, asyncio.Task[None]] = {}
        self._last_drain_result: dict[str, tuple[str, bool]] = {}
        self._drive_tasks: dict[str, tuple[ChatStream, asyncio.Task[None]]] = {}

    @property
    def turn_perf_started(self) -> dict[tuple[str, int], float]:
        return self._turn_perf_started

    @property
    def between_turn_drains(self) -> dict[str, asyncio.Task[None]]:
        return self._between_turn_drains

    @property
    def last_drain_results(self) -> dict[str, tuple[str, bool]]:
        return self._last_drain_result

    def has_live_drain(self, chat_id: str) -> bool:
        task = self._between_turn_drains.get(chat_id)
        return task is not None and not task.done()

    def drain_for(self, chat_id: str) -> asyncio.Task[None] | None:
        return self._between_turn_drains.get(chat_id)

    def discard_drain_result(self, chat_id: str) -> None:
        self._last_drain_result.pop(chat_id, None)

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
    ) -> None:
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
            )
        )
        self._drive_tasks[chat_id] = (stream, task)

        def _clear(finished: asyncio.Task[None]) -> None:
            current = self._drive_tasks.get(chat_id)
            if current is not None and current[1] is finished:
                self._drive_tasks.pop(chat_id, None)

        task.add_done_callback(_clear)

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
    ) -> None:
        """Drive the admitted stream and every queued follow-up turn."""
        current_prompt = prompt
        current_images = images
        current_turn_index = turn_index
        turn_unattended = unattended
        last_assistant_text = ""
        had_error = False
        had_provider_progress = False
        await self._host._await_between_turns_drain(chat_id)
        try:
            while True:
                turn_assistant_text = ""
                turn_streamed_text = ""
                turn_result_published = False
                question_paused = False

                async def _run_turn(
                    run_prompt: str = current_prompt,
                    run_images: list[ImageAttachment] | None = current_images,
                    run_unattended: bool = turn_unattended,
                    run_turn_index: int | None = current_turn_index,
                ) -> None:
                    nonlocal turn_assistant_text, turn_streamed_text
                    nonlocal question_paused, had_error
                    nonlocal had_provider_progress, turn_result_published
                    async for event in self._host.stream_chat(
                        chat_id,
                        run_prompt,
                        images=run_images,
                        unattended=run_unattended,
                    ):
                        payload = event_to_json(event)
                        if payload:
                            apply_file_touches_to_payload(
                                payload,
                                workspace_root=self._host._config.workspace_root,
                            )
                        if (
                            payload
                            and isinstance(event, ResultEvent)
                            and run_turn_index is not None
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
                                rec = cm.user_turn_timings.setdefault(
                                    str(run_turn_index), {}
                                )
                                rec["completed_at"] = completed_at
                                if duration_ms is not None:
                                    rec["duration_ms"] = duration_ms
                                sent_at_rec = rec.get("sent_at", "")
                                self._host._save()
                            else:
                                sent_at_rec = ""
                            payload["completed_at"] = completed_at
                            if sent_at_rec:
                                payload["sent_at"] = sent_at_rec
                            if duration_ms is not None:
                                payload["duration_ms"] = duration_ms
                        if payload:
                            stream.publish(payload)
                            if isinstance(event, ResultEvent):
                                turn_result_published = True
                        if (
                            isinstance(event, AssistantTextDelta)
                            and event.parent_tool_use_id is None
                        ):
                            turn_streamed_text += event.text
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
                                self._host.respond_permission(
                                    chat_id,
                                    request_id=event.request_id,
                                    approved=False,
                                    reason=(
                                        "Scheduled runs cannot wait for "
                                        "interactive approval."
                                    ),
                                )
                        if (
                            isinstance(event, ToolUseEvent)
                            and event.tool_name == "AskUserQuestion"
                            and event.tool_input.strip()
                        ):
                            question_payload = event.tool_input
                            if event.request_id:
                                try:
                                    parsed_question = json.loads(event.tool_input)
                                except (TypeError, json.JSONDecodeError):
                                    parsed_question = {"questions": []}
                                if not isinstance(parsed_question, dict):
                                    parsed_question = {"questions": []}
                                parsed_question["request_id"] = event.request_id
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
                            if event.is_error:
                                had_error = True
                                result_text = event.result or ""
                                if chat_service._is_retryable_quota_error(result_text):
                                    self._host._arm_retry(
                                        chat_id,
                                        stream,
                                        kind="quota",
                                        current_prompt=run_prompt,
                                        current_images=run_images,
                                        had_progress=had_provider_progress,
                                        reason=result_text or "quota limit",
                                    )
                                elif chat_service._is_retryable_connection_error(
                                    result_text
                                ):
                                    self._host._arm_retry(
                                        chat_id,
                                        stream,
                                        kind="connection",
                                        current_prompt=run_prompt,
                                        current_images=run_images,
                                        had_progress=had_provider_progress,
                                        reason=result_text or "connection error",
                                    )
                                elif chat_service._is_retryable_auth_error(result_text):
                                    self._host._arm_retry(
                                        chat_id,
                                        stream,
                                        kind="auth",
                                        current_prompt=run_prompt,
                                        current_images=run_images,
                                        had_progress=had_provider_progress,
                                        reason=result_text or "auth error",
                                    )
                            else:
                                turn_assistant_text = event.result or ""

                turn_task = asyncio.create_task(
                    _run_turn(), name=f"chat-turn-{chat_id}"
                )
                stream.turn_task = turn_task
                try:
                    await turn_task
                except asyncio.CancelledError:
                    if not stream.force_closing:
                        raise
                    stream.force_closing = False
                    logger.info("Turn force-closed by user stop for chat %s", chat_id)
                    turn_assistant_text = turn_streamed_text
                    stream.publish(
                        self._host._stop_result_payload(
                            chat_id,
                            turn_index=current_turn_index,
                            text=turn_streamed_text,
                        )
                    )
                except Exception as exc:
                    if stream.user_stopped:
                        logger.info("Stream stopped by user for chat %s", chat_id)
                        if not turn_result_published:
                            stream.publish(
                                self._host._stop_result_payload(
                                    chat_id,
                                    turn_index=current_turn_index,
                                    text=turn_streamed_text,
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
                        break
                    else:
                        logger.exception("Stream error for chat %s", chat_id)
                        error_msg = str(exc).strip() or type(exc).__name__
                        stderr = getattr(exc, "stderr", None)
                        if stderr and str(stderr) not in error_msg:
                            error_msg = f"{error_msg}\n{stderr}"
                        error_chat = self._host._chats.get(chat_id)
                        error_model = error_chat.model if error_chat else ""
                        error_session = error_chat.session_id if error_chat else ""
                        stream.publish(
                            {
                                "type": "result",
                                "text": error_msg,
                                "is_error": True,
                                "effective_model": error_model,
                                "usage": {},
                                "quota": {},
                                "session_id": error_session,
                            }
                        )
                        had_error = True
                        if error_chat is not None:
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
                                    response_text=error_msg,
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
                        if chat_service._is_retryable_provider_startup_error(error_msg):
                            self._host._arm_retry(
                                chat_id,
                                stream,
                                kind="startup",
                                current_prompt=current_prompt,
                                current_images=current_images,
                                had_progress=had_provider_progress,
                                reason=error_msg,
                            )
                        elif chat_service._is_retryable_quota_error(error_msg):
                            self._host._arm_retry(
                                chat_id,
                                stream,
                                kind="quota",
                                current_prompt=current_prompt,
                                current_images=current_images,
                                had_progress=had_provider_progress,
                                reason=error_msg,
                            )
                        elif chat_service._is_retryable_connection_error(error_msg):
                            self._host._arm_retry(
                                chat_id,
                                stream,
                                kind="connection",
                                current_prompt=current_prompt,
                                current_images=current_images,
                                had_progress=had_provider_progress,
                                reason=error_msg,
                            )
                        elif chat_service._is_retryable_auth_error(error_msg):
                            self._host._arm_retry(
                                chat_id,
                                stream,
                                kind="auth",
                                current_prompt=current_prompt,
                                current_images=current_images,
                                had_progress=had_provider_progress,
                                reason=error_msg,
                            )
                        parked = stream.drain_pending()
                        if parked:
                            cm_park = self._host._chats.get(chat_id)
                            if cm_park is not None:
                                cm_park.pending_queue = list(parked)
                                self._host._save()
                        break
                finally:
                    stream.turn_task = None

                if turn_assistant_text:
                    last_assistant_text = turn_assistant_text

                if question_paused:
                    parked = stream.drain_pending()
                    if parked:
                        cm_park = self._host._chats.get(chat_id)
                        if cm_park is not None:
                            cm_park.pending_queue = list(parked)
                            self._host._save()
                    break

                next_pending = stream.drain_one()
                if stream.user_stopped:
                    stream.user_stopped = False
                    if next_pending is not None:
                        had_error = False
                if next_pending is None or had_error:
                    stream.accepting_queue = False
                    late = stream.drain_pending()
                    parked = (
                        [next_pending, *late]
                        if had_error and next_pending is not None
                        else late
                    )
                    if parked:
                        cm_park = self._host._chats.get(chat_id)
                        if cm_park is not None:
                            cm_park.pending_queue = list(parked)
                            self._host._save()
                    break

                combined_text = next_pending.get("text", "").strip()
                merged_image_refs: list[str] = list(next_pending.get("images") or [])
                merged_images: list[ImageAttachment] = []
                for ref in merged_image_refs:
                    attachment = self._host.resolve_image_ref(ref)
                    if attachment:
                        merged_images.append(attachment)
                if not combined_text:
                    continue

                turn_unattended = False
                turn_index2: int | None = None
                sent_at_iso2 = ""
                chat_meta2 = self._host._chats.get(chat_id)
                if chat_meta2 is not None:
                    chat_meta2.pending_question = ""
                    turn_index2 = chat_meta2.user_turn_count
                    chat_meta2.user_turn_count = turn_index2 + 1
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
                    self.start_turn_perf(chat_id, turn_index2)
                    self._host._save()

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
                stream.publish(followup_echo)

                current_prompt = combined_text
                current_images = merged_images or None
                current_turn_index = turn_index2
        finally:
            _late_fallback = chat_service._fallback_title(prompt) if prompt else None
            if (
                prompt
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
            if chat_meta is not None:
                permission_pending = bool(chat_meta.pending_permission)
                chat_meta.last_response = last_assistant_text[
                    -chat_service._PROVIDER_HANDOVER_MAX_CHARS :
                ]
                chat_meta.last_response_status = (
                    "error"
                    if had_error
                    else "question"
                    if chat_meta.pending_question
                    else "permission"
                    if permission_pending
                    else "success"
                    if last_assistant_text.strip()
                    else "empty"
                )
                if permission_pending:
                    chat_meta.pending_permission = ""
                self._host._save()
            stream.finish()
            self._host._broker.clear(chat_id, stream)
            self._host._events.publish(
                {
                    "type": "chat_streaming_done",
                    "chat_id": chat_id,
                    "project_id": project_id,
                    "is_error": had_error,
                }
            )
            chat_for_watcher = self._host._chats.get(chat_id)
            nudge_can_reannounce = False
            if (
                chat_for_watcher is not None
                and chat_for_watcher.session_id
                and capabilities_for(chat_for_watcher.provider).background_subagents
            ):
                self._host._start_subagent_watcher(chat_id, project_id)
                if chat_for_watcher.provider == "claude":
                    self._host._start_between_turns_drain(chat_id, project_id)
                    nudge_can_reannounce = True
            self._host._discard_result_announce(chat_id)
            if not had_error and last_assistant_text.strip():
                snippet = self._host._result_snippet(last_assistant_text)
                chat_now = self._host._chats.get(chat_id)
                if chat_now is not None:
                    if chat_now.retry_status == "pending" and is_retry:
                        self._host._clear_chat_retry(chat_now)
                    chat_now.last_activity_at = chat_service._now_iso()
                    chat_now.last_snippet = snippet
                    self._host._save()
                title = chat_now.title if chat_now else "Ciaobot"
                interim = nudge_can_reannounce and self._host._is_interim_subagent_text(
                    last_assistant_text
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

        async for event in provider.execute_streaming(request):
            outcome.events.append(event)
            yield event
            sdk_sid = provider.current_session_id
            if sdk_sid:
                changed = False
                if sdk_sid != chat.session_id:
                    self._host._rotate_session_id(chat, sdk_sid)
                    changed = True
                changed = (
                    self._host._commit_context_marker(chat, request, sdk_sid) or changed
                )
                if changed:
                    self._host._save()
            if isinstance(event, ResultEvent):
                outcome.response_text = event.result
                outcome.had_error = bool(event.is_error)
                outcome.effective_model = event.effective_model or chat.model
                if (
                    chat.provider == "opencode"
                    and not chat.model
                    and outcome.effective_model
                ):
                    chat.model = outcome.effective_model
                    self._host._save()
                outcome.usage = event.usage
                outcome.quota = event.quota
                outcome.cost_usd = event.cost_usd or 0.0
                if event.session_id:
                    changed = False
                    if event.session_id != chat.session_id:
                        self._host._rotate_session_id(chat, event.session_id)
                        changed = True
                    changed = (
                        self._host._commit_context_marker(
                            chat, request, event.session_id
                        )
                        or changed
                    )
                    if changed:
                        self._host._save()
            elif isinstance(event, ToolUseEvent):
                outcome.tool_events.append(
                    {
                        "id": event.tool_use_id or "",
                        "name": event.tool_name,
                        "input": {"summary": event.tool_input},
                    }
                )
                self._host._record_agent_tool_use(chat, request, event)

    def record_stopped_turn(
        self,
        chat_id: str,
        chat: ChatInfo,
        request: AgentRequest,
        outcome: StreamOutcome,
        journal: TurnJournal,
    ) -> None:
        """Persist a force-stopped provider pass as a partial transcript turn."""
        try:
            streamed = "".join(
                getattr(event, "text", "") or ""
                for event in outcome.events
                if type(event).__name__ == "AssistantTextDelta"
            )
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

    async def stream_chat(
        self,
        chat_id: str,
        prompt: str,
        images: list[ImageAttachment] | None = None,
        *,
        unattended: bool = False,
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
        chat.last_response = ""
        chat.last_response_status = "running"
        self._host._save()
        handover_context_sent = bool(
            chat.handover_context_pending and chat.handover_messages
        )

        request = self._host.build_agent_request(
            chat,
            prompt=prompt,
            display_prompt=prompt,
            images=images,
            resume_session=chat.session_id or None,
            unattended=unattended,
        )

        response_text = ""
        effective_model = chat.model
        usage: dict[str, str] = {}
        quota: dict[str, str] = {}
        cost_usd = 0.0
        had_error = False
        tool_events: list[dict[str, Any]] = []

        if images and not await self._host._model_capable(request.model, chat):
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
                self._host._record_stopped_turn(
                    chat_id, chat, request, outcome, journal
                )
                raise
            response_text = outcome.response_text
            had_error = outcome.had_error
            effective_model = outcome.effective_model
            usage = outcome.usage
            quota = outcome.quota
            cost_usd = outcome.cost_usd
            tool_events = outcome.tool_events

            if handover_context_sent and not had_error:
                self._host.mark_handover_context_used(chat_id)

            ctx = ChatContext.for_web(chat_id)
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

    def cancel_between_turns_drain(self, chat_id: str) -> None:
        """Fire-and-forget cancel; pair with await_between_turns_drain."""
        self._host._cancel_parked_announce_deadline(chat_id)
        task = self._between_turn_drains.get(chat_id)
        if task is not None and not task.done():
            task.cancel()

    async def await_between_turns_drain(self, chat_id: str) -> None:
        """Wait until this chat's drain has fully unwound."""
        self._host._cancel_parked_announce_deadline(chat_id)
        task = self._between_turn_drains.pop(chat_id, None)
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            return

    def start_between_turns_drain(self, chat_id: str, project_id: str) -> None:
        provider_service = self._host._providers.get(chat_id)
        if provider_service is None or not provider_service.can_drain:
            return
        self._host._cancel_between_turns_drain(chat_id)
        task = asyncio.create_task(self._host._drain_between_turns(chat_id, project_id))
        self._between_turn_drains[chat_id] = task

    async def drain_between_turns(self, chat_id: str, project_id: str) -> None:
        """Publish provider events that arrive while no user turn is active."""
        provider_service = self._host._providers.get(chat_id)
        if provider_service is None:
            return
        stream: ChatStream | None = None
        cancelled = False

        def close_stream(had_error: bool) -> None:
            nonlocal stream
            if stream is None:
                return
            stream.finish()
            self._host._broker.clear(chat_id, stream)
            self._host._events.publish(
                {
                    "type": "chat_streaming_done",
                    "chat_id": chat_id,
                    "project_id": project_id,
                    "is_error": had_error,
                }
            )
            stream = None

        try:
            async for event in provider_service.drain_events():
                payload = event_to_json(event)
                if payload is None:
                    continue
                apply_file_touches_to_payload(
                    payload,
                    workspace_root=self._host._config.workspace_root,
                )
                if stream is None:
                    stream = ChatStream(background=True)
                    self._host._broker.register(chat_id, stream)
                    self._host._events.publish(
                        {
                            "type": "chat_streaming_started",
                            "chat_id": chat_id,
                            "project_id": project_id,
                        }
                    )
                stream.publish(payload)
                if isinstance(event, PermissionRequestEvent):
                    self._host._notify_permission(chat_id, event)
                if isinstance(event, ResultEvent):
                    text = event.result or ""
                    is_error = bool(event.is_error)
                    self._last_drain_result[chat_id] = (text, is_error)
                    close_stream(is_error)
                    if (
                        not is_error
                        and text
                        and self._host._is_worth_announcing_nudge_reply(text)
                    ):
                        snippet = self._host._result_snippet(text)
                        chat_now = self._host._chats.get(chat_id)
                        if chat_now is not None:
                            chat_now.last_activity_at = chat_service._now_iso()
                            chat_now.last_snippet = snippet
                            chat_now.last_response = text[
                                -chat_service._PROVIDER_HANDOVER_MAX_CHARS :
                            ]
                            chat_now.last_response_status = "success"
                            self._host._save()
                        title = chat_now.title if chat_now else "Ciaobot"
                        self._host._discard_result_announce(chat_id)
                        self._host._announce_result_ready(
                            chat_id, project_id, title, snippet
                        )
                        self._host._spawn_detached(
                            self._host._maybe_archive_proposal_helper(chat_id),
                            f"archive-proposal-helper-{chat_id}",
                        )
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception:
            logger.exception("Between-turns drain failed for chat %s", chat_id)
        finally:
            close_stream(False)
            if not cancelled:
                self._host._flush_result_announce(chat_id)

    async def wait_for_drain_result(
        self, chat_id: str, *, timeout_s: float = 180.0
    ) -> tuple[str, bool] | None:
        """Wait for the drain's next recorded synthesis result."""
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            result = self._last_drain_result.get(chat_id)
            if result is not None:
                return result
            await asyncio.sleep(1)
        return None
