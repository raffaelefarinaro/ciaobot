"""Scheduled-chat dispatch and completion lifecycle.

``ProjectChatManager`` remains the coordinator for projects, chats, archives, and
persistence.  This collaborator owns the part of a schedule run that turns one
stored :class:`~ciao.schedules.ScheduleEntry` into a stream, waits for any
background work, grades the run, and decides whether the resulting chat can be
auto-archived.  Target-chat preparation and the attention classifier live here
too because they are part of the same dispatch transaction; archive extraction
and the rest of the archive pipeline deliberately remain manager concerns.

The manager keeps delegating methods under their old names.  Calls back into
those seams go through :class:`ScheduleDispatchHost`, rather than reaching into
a second copy of the manager, so existing tests and integrations that patch
``start_stream``, ``prepare_schedule_chat``, the subagent wait, or the classifier
still patch the code that executes.  The protocol is deliberately typed: the
collaborator has no ``Any``-based dependency contract.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from ciao import job_runs, subagent_tracking
from ciao import schedules as schedule_support
from ciao.config import CiaoConfig
from ciao.error_log import clear_error_log, tail_error_log
from ciao.models import BridgeMode, ImageAttachment
from ciao.provider_service import supported_providers
from ciao.providers.opencode import OpencodeProvider, opencode_collab_tree_counts
from ciao.schedules import ScheduleEntry, ScheduleStore
from ciao.web import chat_service
from ciao.web.chat_broker import ChatStream, EventsHub

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ciao.web.project_chats import ArchiveOutcome, ChatInfo, ProjectInfo

logger = logging.getLogger(__name__)


class ScheduleDispatchHost(Protocol):
    """The complete, typed manager surface used by ``ScheduleDispatcher``."""

    _config: CiaoConfig
    _chats: dict[str, ChatInfo]
    _projects: dict[str, ProjectInfo]
    schedule_store: ScheduleStore | None

    @property
    def events(self) -> EventsHub: ...

    def _save(self, *, reason: str = ...) -> None: ...

    def _agent_root_for_chat(self, chat_id: str) -> Path: ...

    def _resolve_schedule_project(
        self, stale_id: str, entry: ScheduleEntry
    ) -> ProjectInfo | None: ...

    def _rehome_interval_chat(
        self, entry: ScheduleEntry, prompt: str
    ) -> ChatInfo | None: ...

    def create_chat(
        self,
        project_id: str,
        title: str = ...,
        *,
        model: str | None = ...,
        mode: str | None = ...,
        provider: str | None = ...,
    ) -> ChatInfo: ...

    def schedule_default_provider(self, project_id: str | None) -> str: ...

    def prepare_schedule_chat(
        self,
        entry: ScheduleEntry,
        prompt: str,
        model: str,
        mode: BridgeMode,
        provider: str = ...,
    ) -> str | None: ...

    def start_stream(
        self,
        chat_id: str,
        prompt: str,
        images: list[ImageAttachment] | None = ...,
        *,
        is_retry: bool = ...,
        unattended: bool = ...,
    ) -> ChatStream: ...

    async def _await_schedule_subagents(
        self, chat_id: str, *, timeout_s: float = ...
    ) -> tuple[bool, bool]: ...

    async def _schedule_run_needs_user(
        self, entry: ScheduleEntry, outcome: chat_service.ScheduleRunOutcome
    ) -> bool: ...

    async def _wait_for_drain_result(
        self, chat_id: str, *, timeout_s: float = ...
    ) -> tuple[str, bool] | None: ...

    def _discard_schedule_drain_result(self, chat_id: str) -> None: ...

    @staticmethod
    def _is_interim_subagent_text(text: str) -> bool: ...

    async def archive_chat(self, chat_id: str) -> ArchiveOutcome | None: ...

    def run_archive_postprocess(
        self,
        chat_id: str,
        outcome: ArchiveOutcome,
        chat_meta: ChatInfo | None,
        project_meta: ProjectInfo | None,
    ) -> None: ...


class ScheduleDispatcher:
    """Own target preparation, dispatch streaming, and run completion."""

    def __init__(self, host: ScheduleDispatchHost) -> None:
        self._host = host

    async def _await_schedule_subagents(
        self, chat_id: str, *, timeout_s: float = 900.0
    ) -> tuple[bool, bool]:
        """Block until the schedule chat's background subagents finish.

        A schedule turn can delegate to background subagents (e.g. memory
        curation dispatches the memory agent) and return before they finish.
        The archive decision must not run against that half-complete state, so
        we poll the parent session JSONL — the reliable running-count signal
        (see ciao/subagent_tracking.py) — until it drains.

        Returns ``(settled, had_async)``: ``settled`` is True when no
        subagents remain running (or none were ever tracked), False when the
        timeout elapses with agents still running; ``had_async`` is True when
        the session ever dispatched a background subagent. Errors resolve to
        ``(True, False)`` so a tracking failure never blocks the pipeline.
        """
        chat = self._host._chats.get(chat_id)
        if chat is None or not chat.session_id:
            return True, False
        if chat.provider == "opencode":
            deadline = time.perf_counter() + timeout_s
            had_async = False
            running = 0
            while time.perf_counter() < deadline:
                tree = await OpencodeProvider.read_collab_tree(
                    self._host._config.workspace_root, chat.session_id
                )
                running, had_now = opencode_collab_tree_counts(tree)
                had_async = had_async or had_now
                if running == 0:
                    return True, had_async
                await asyncio.sleep(3)
            return running == 0, had_async
        if chat.provider != "claude":
            return True, False
        try:
            # Single-shot lookup: a miss reports the agents settled and can
            # archive the chat, so force the lookup past the shared cache's
            # rescan rate limit — see subagent_tracking.find_parent_session_file.
            path = subagent_tracking.find_parent_session_file(
                chat.session_id,
                self._host._config.workspace_root,
                agent_root=self._host._agent_root_for_chat(chat_id),
                force_refresh=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Subagent wait: session file lookup failed for %s", chat_id)
            return True, False
        if path is None:
            return True, False

        deadline = time.perf_counter() + timeout_s
        last_size = -1
        running = 0
        had_async = False
        state: subagent_tracking.SessionSubagentState | None = None
        while time.perf_counter() < deadline:
            try:
                size = path.stat().st_size
            except OSError:
                return True, had_async
            if size != last_size or state is None:
                last_size = size
                try:
                    state = subagent_tracking.parse_session_subagents(path)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Subagent wait: parse failed for %s", chat_id
                    )
                    return True, had_async
                if not had_async:
                    had_async = any(
                        info.is_async and info.kind == "agent"
                        for info in state.subagents.values()
                    )
            # Recomputed every tick, not just when the parent file grows: a
            # completion the CLI never wrote there still shows up as the
            # agent's own transcript going quiet (see the watcher above).
            running = subagent_tracking.running_background_agents(path, state)
            if running == 0:
                return True, had_async
            await asyncio.sleep(3)
        if running:
            logger.warning(
                "Schedule chat %s still has %d background subagent(s) after %.0fs; "
                "keeping chat visible",
                chat_id,
                running,
                timeout_s,
            )
        return running == 0, had_async

    async def _schedule_run_needs_user(
        self, entry: ScheduleEntry, outcome: chat_service.ScheduleRunOutcome
    ) -> bool:
        """Return True when an auto-archive schedule result deserves attention.

        Conservative default: if the classifier cannot produce strict JSON,
        keep the chat visible.
        """
        title = str(getattr(entry, "prompt", "")).split("\n", 1)[0].strip()
        payload: dict[str, object] = {
            "schedule_id": getattr(entry, "schedule_id", ""),
            "title": title,
            "final_output": outcome.final_text[-6000:],
        }
        system_prompt = (
            "You decide whether the user needs to see a scheduled routine result in the chat interface. "
            "Return only JSON: {\"needs_user\": boolean, \"reason\": string}. "
            "needs_user=false when the run is routine maintenance, even if it updated files, triaged proposals, "
            "or created file stubs automatically (e.g. routine memory curation, git syncs, daily logs, baseline bumps). "
            "Set needs_user=true ONLY when there is an actual problem, error, warning, unresolved conflict, "
            "a specific question/decision asked of the user, or a new external finding that requires their direct "
            "intervention or judgment to proceed."
        )
        user_prompt = json.dumps(payload, ensure_ascii=False)
        try:
            from ciao.insights import (
                _resolve_insights_call,
                resolve_insights_model,
            )

            # Route through the shared resolver (same as ciao/insights.py) so an
            # unavailable Apple on-device model is substituted rather than
            # raising -- a raise here keeps the run visible instead of
            # auto-archiving it.
            project_id: str | None = getattr(entry, "web_project_id", None)
            project = self._host._projects.get(project_id) if project_id else None
            workspace = project.workspace if project else None
            fixed_chat_id: str | None = getattr(entry, "web_chat_id", None)
            fixed_chat = self._host._chats.get(fixed_chat_id) if fixed_chat_id else None
            classifier_provider = (
                fixed_chat.provider if fixed_chat is not None
                else getattr(entry, "provider", "")
                or self._host.schedule_default_provider(project_id)
            )
            if classifier_provider not in supported_providers():
                return True
            insights_model = resolve_insights_model(self._host._config, workspace)
            env: dict[str, str] = {}
            model, classifier_provider, note = _resolve_insights_call(
                self._host._config,
                insights_model,
                provider=classifier_provider,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Schedule attention classifier setup failed; keeping chat visible")
            return True
        tracked_provider = classifier_provider
        async with job_runs.track(
            "schedule_attention_classifier",
            "Schedule attention classifier",
            model=model,
            provider=tracked_provider,
            extra={
                "schedule_id": payload["schedule_id"],
                "workspace": workspace or "",
            },
        ) as run:
            if note:
                run.extra["fallback_note"] = note
                logger.info("Schedule attention classifier %s", note)
            try:
                from ciao.providers.oneshot import run_oneshot
                from ciao.insights import _insights_timeout_s, is_context_overflow

                # Same env-tunable budget as the insights job: a slow local
                # model can take minutes on a successful call, so a hard 60s
                # window turned tail latency into a guaranteed TimeoutError
                # and the classifier ran 6/6 in error.
                text = await run_oneshot(
                    user_prompt,
                    system_prompt=system_prompt,
                    model=model,
                    env=env,
                    timeout_s=_insights_timeout_s(),
                    provider=classifier_provider,
                    cwd=self._host._config.workspace_root,
                )
                from ciao.critique import extract_json

                verdict = extract_json(text)
                if verdict is None:
                    # Expected degradation, not a fault: the conservative
                    # default below already handles it, so a full traceback in
                    # server_errors.log only pollutes the triage report.
                    run.status = "error"
                    run.error = "classifier returned no parseable JSON"
                    head = (text or "").strip()[:200]
                    logger.warning(
                        "Schedule attention classifier returned no parseable "
                        "JSON with model %s; keeping chat visible (output head: %r)",
                        model,
                        head,
                    )
                    return True
                needs_user = bool(verdict.get("needs_user", True))
                run.extra["needs_user"] = needs_user
                reason = str(verdict.get("reason", "")).strip()
                if reason:
                    run.extra["reason"] = reason[:500]
                return needs_user
            except Exception as exc:  # noqa: BLE001
                run.status = "error"
                run.error = (str(exc).strip() or type(exc).__name__)[:1000]
                # Distinguish a deterministic 400-style overflow from a
                # transient timeout. The payload is already trimmed to
                # final_text[-6000:] so an overflow here is rare, but
                # recording the class lets the job history tell transient
                # tail-latency from a real context-window problem.
                if is_context_overflow(exc):
                    run.extra["context_overflow"] = True
                    logger.warning(
                        "Schedule attention classifier hit the context window "
                        "with model %s; keeping chat visible",
                        model,
                    )
                else:
                    logger.exception(
                        "Schedule attention classifier failed with model %s; "
                        "keeping chat visible",
                        model,
                    )
                return True

    def prepare_schedule_chat(
        self,
        entry: ScheduleEntry,
        prompt: str,
        model: str,
        mode: BridgeMode,
        provider: str = "",
    ) -> str | None:
        """Create/resolve the target chat for a schedule dispatch.

        Returns the chat_id or None if the target can't be resolved.  This is
        synchronous so callers can get the chat_id before the async stream
        starts.

        ``provider`` applies only when this dispatch creates a new chat
        (web_project_id path). For fixed-chat schedules (web_chat_id), the
        existing chat's provider is honoured.

        A fixed-chat entry is handled differently in two ways. It never has its
        model/mode overwritten — each run uses whatever the user configured on
        the chat, which is the defining property the merged ``interval``
        cadence inherited from loops. And a missing or archived target does not
        end the run, whatever the cadence: the archived transcript is forked
        (``chat_continue`` semantics), or a fresh chat is opened in the entry's
        project, and ``entry.web_chat_id`` is re-pointed at it. Dispatching into
        the archived chat itself would resume a reclaimed provider session and
        fail instantly with a silent ``stream error`` on every future run (see
        issue #407). Only when no project resolves either does this return None,
        which the caller turns into "disable the entry".
        """
        web_project_id: str | None = getattr(entry, "web_project_id", None)
        web_chat_id: str | None = getattr(entry, "web_chat_id", None)
        sched_id = getattr(entry, "schedule_id", "") or ""
        sched_title = (getattr(entry, "title", "") or "").strip()

        def _stamp(chat: ChatInfo) -> None:
            # Record the schedule backlink so the PWA can show a
            # "triggered by schedule X" banner that survives later runs.
            chat.schedule_id = sched_id
            chat.schedule_title = sched_title

        if web_project_id:
            project = self._host._projects.get(web_project_id)
            if project is None:
                project = self._host._resolve_schedule_project(web_project_id, entry)
            if project is None:
                logger.warning("Schedule target project %s not found, skipping", web_project_id)
                return None
            # Prefer the routine's own name ("Workspace care") over a
            # truncated prompt sentence, so schedule chats read cleanly instead
            # of "Run a structural hygiene pass on the... - Jul 15".
            routine_name = (getattr(entry, "title", "") or "").strip()
            if routine_name:
                title_base = routine_name
            else:
                title_base = prompt.split("\n")[0].strip().rstrip(".")
                if len(title_base) > 40:
                    title_base = title_base[:37] + "..."
            date_str = datetime.now(UTC).strftime("%b %d")
            title = f"{title_base} - {date_str}"
            chat = self._host.create_chat(
                project.project_id,
                title=title,
                model=model,
                mode=mode,
                provider=provider or None,
            )
            _stamp(chat)
            self._host._save()
            return chat.chat_id
        if web_chat_id:
            target_chat = self._host._chats.get(web_chat_id)
            if target_chat is None or target_chat.archived:
                replacement = self._host._rehome_interval_chat(entry, prompt)
                if replacement is None:
                    return None
                web_chat_id = replacement.chat_id
                target_chat = replacement
            if target_chat is None:
                logger.warning("Schedule target chat %s not found, skipping", web_chat_id)
                return None
            interval = getattr(entry, "frequency", "") == "interval"
            if not interval:
                # Interval runs inherit the chat's own model/mode instead.
                target_chat.model = model
                target_chat.mode = mode
                # A rehomed replacement was created without a provider arg, so
                # create_chat defaulted it to the workspace's default — which
                # can differ from the engine this dispatch resolved (entry
                # override or workspace default at schedule_effective_routing
                # time). Dispatch runs the replacement's provider, and would
                # then feed it a model chosen for a different engine. Pin all
                # three so the run uses what the schedule asked for.
                if getattr(target_chat, "provider", "") != (provider or ""):
                    target_chat.provider = provider
            _stamp(target_chat)
            self._host._save()
            return cast(str, web_chat_id)
        if getattr(entry, "scope", "") == "system":
            project = self._host._resolve_schedule_project("", entry)
            if project is None:
                logger.warning("System schedule %s has no default project, skipping", getattr(entry, "schedule_id", ""))
                return None
            # Prefer the routine's own name ("Workspace care") over a
            # truncated prompt sentence, so schedule chats read cleanly instead
            # of "Run a structural hygiene pass on the... - Jul 15".
            routine_name = (getattr(entry, "title", "") or "").strip()
            if routine_name:
                title_base = routine_name
            else:
                title_base = prompt.split("\n")[0].strip().rstrip(".")
                if len(title_base) > 40:
                    title_base = title_base[:37] + "..."
            date_str = datetime.now(UTC).strftime("%b %d")
            title = f"{title_base} - {date_str}"
            chat = self._host.create_chat(
                project.project_id,
                title=title,
                model=model,
                mode=mode,
                provider=provider or None,
            )
            _stamp(chat)
            self._host._save()
            return chat.chat_id
        logger.warning("Schedule has no web target, skipping")
        return None

    async def dispatch_schedule(
        self,
        entry: ScheduleEntry,
        prompt: str,
        model: str,
        mode: BridgeMode,
        provider: str = "",
        *,
        target_chat_id: str | None = None,
    ) -> dict[str, str]:
        """Dispatch a schedule and return metadata (chat_id, status, archived_to)."""
        web_project_id: str | None = getattr(entry, "web_project_id", None)
        web_chat_id: str | None = getattr(entry, "web_chat_id", None)

        # Go through the manager seam rather than calling this collaborator's
        # preparation method directly. Existing callers patch the manager's
        # method to inject a target or observe preparation, and that patch must
        # still be the one the dispatch path executes.
        target_id = target_chat_id or self._host.prepare_schedule_chat(
            entry, prompt, model, mode, provider,
        )
        if target_id is None:
            return {}

        result: dict[str, str] = {"chat_id": target_id}
        outcome = chat_service.ScheduleRunOutcome()

        # Job-run recording: this method swallows its own errors (the broad
        # except below sets outcome.stream_error and continues) and has a
        # single exit, so we time it here and record once before returning.
        _sched_perf = time.perf_counter()
        _sched_started = datetime.now(UTC)
        _sched_schedule_id = getattr(entry, "schedule_id", "") or ""
        # Which dispatch this run *is*, sampled from the entry as it stood when
        # the run started. The stored row can have moved on to a newer dispatch
        # by the time the outcome lands (an overlapping manual "Run now"), and
        # that is exactly what the write-back below has to be able to tell.
        _sched_dispatch_id = getattr(entry, "last_dispatch_id", "") or ""

        # Save original model/mode for fixed-chat dispatches. Interval entries
        # are exempt: prepare_schedule_chat leaves the chat's settings alone for
        # them, so there is nothing to restore.
        orig_model: str | None = None
        orig_mode: BridgeMode | None = None
        interval = getattr(entry, "frequency", "") == "interval"
        if not interval and not web_project_id and web_chat_id:
            chat = self._host._chats.get(target_id)
            if chat:
                orig_model, orig_mode = chat.model, chat.mode

        # Substitute error-log placeholder for weekly maintenance schedules
        had_error_placeholder = "{{ERROR_LOG}}" in prompt
        if had_error_placeholder:
            errors = await asyncio.to_thread(
                tail_error_log, self._host._config.workspace_root, 200
            )
            prompt = prompt.replace(
                "{{ERROR_LOG}}",
                errors or "(no errors logged this week)",
            )
        # Richer variant: error log + failed background-job runs
        had_issue_placeholder = "{{ISSUE_REPORT}}" in prompt
        if had_issue_placeholder:
            from ciao.debug_report import build_issue_report
            from ciao.startup_triage import TRIAGE_SCHEDULE_ID

            # Exclude the triage's own past runs so a triage prompt built from
            # {{ISSUE_REPORT}} never re-triages its own recorded summary.
            issue_report = await asyncio.to_thread(
                build_issue_report,
                self._host._config.workspace_root,
                exclude_schedule_ids={TRIAGE_SCHEDULE_ID},
            )
            prompt = prompt.replace(
                "{{ISSUE_REPORT}}", issue_report["report_text"]
            )

        try:
            stream = self._host.start_stream(target_id, prompt, unattended=True)
            async for raw_payload in stream.subscribe():
                if not isinstance(raw_payload, dict):
                    continue
                payload = cast(dict[str, object], raw_payload)
                event_type = payload.get("type")
                if event_type == "permission_request":
                    outcome.permission_requested = True
                elif (
                    event_type == "tool_use"
                    and payload.get("tool_name") == "AskUserQuestion"
                ):
                    outcome.question_requested = True
                elif event_type == "chat_retry":
                    if (payload.get("status") or "") == "pending":
                        outcome.retry_pending = True
                elif event_type == "error":
                    outcome.stream_error = True
                elif event_type == "result":
                    outcome.completed = True
                    outcome.is_error = bool(payload.get("is_error"))
                    outcome.final_text = str(payload.get("text") or "")
            # Clear only after a clean run: a failed triage must not wipe
            # the backlog it never processed.
            if (
                (had_error_placeholder or had_issue_placeholder)
                and chat_service._schedule_run_clean(outcome)
            ):
                await asyncio.to_thread(
                    clear_error_log, self._host._config.workspace_root
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            outcome.stream_error = True
            logger.exception("Schedule dispatch to %s failed", target_id)
        finally:
            if (
                orig_model is not None
                and orig_mode is not None
                and not interval
                and not web_project_id
                and web_chat_id
            ):
                chat = self._host._chats.get(target_id)
                if chat:
                    chat.model = orig_model
                    chat.mode = orig_mode

        chat_state = self._host._chats.get(target_id)
        if chat_state and chat_state.retry_status == "pending":
            outcome.retry_pending = True

        # A clean parent turn may still have live background subagents (e.g.
        # curation delegating to the memory agent). Wait for them to finish
        # before the archive decision so the classifier judges the completed
        # result — not an interim "dispatched, will report later" message. If
        # they don't settle in time, mark the run pending so it stays visible.
        if chat_service._schedule_run_clean(outcome):
            # Drop any stale synthesis result before waiting so we only pick up
            # the turn that runs when *these* subagents finish. The drain that
            # captures it was started by start_stream's completion handler.
            self._host._discard_schedule_drain_result(target_id)
            settled, had_async = await self._host._await_schedule_subagents(target_id)
            if not settled:
                outcome.subagents_pending = True
            elif had_async and chat_state is not None and chat_state.provider == "claude":
                # Background subagents finished: the CLI runs a synthesis turn
                # whose result the between-turns drain records. Feed that real
                # summary to the archive classifier instead of the interim
                # parent message. Bounded; exits as soon as the result lands.
                synth = await self._host._wait_for_drain_result(target_id)
                if synth is not None:
                    synth_text, synth_error = synth
                    if synth_text:
                        outcome.final_text = synth_text
                    if synth_error:
                        outcome.is_error = True
                    elif not synth_text:
                        # A drain result that carries neither text nor error
                        # says nothing; fall through to the interim-text
                        # guard below with whatever the parent last said.
                        pass
                if not outcome.is_error and self._host._is_interim_subagent_text(
                    outcome.final_text
                ):
                    # The synthesis turn never happened (or the CLI died
                    # before writing one — the 2026-08-30 daily-log run: the
                    # nudge and the final task-notification crossed, the SDK
                    # read task was cancelled, and the run "ended" on an
                    # interim message). The parent's data is un-synthesized
                    # and whatever work was supposed to follow — writing the
                    # log, committing — never ran. The run is not done: keep
                    # the chat visible instead of archiving a stub.
                    outcome.subagents_pending = True
                    logger.warning(
                        "Schedule chat %s ended on interim subagent text with "
                        "no synthesis turn; keeping it visible",
                        target_id,
                    )
            self._host._discard_schedule_drain_result(target_id)

        needs_user = False
        if getattr(entry, "archive_policy", "manual") == "auto" and chat_service._schedule_run_clean(outcome):
            needs_user = await self._host._schedule_run_needs_user(entry, outcome)

        if chat_service._should_auto_archive_schedule_run(entry, outcome, needs_user=needs_user):
            chat_meta = self._host._chats.get(target_id)
            project_meta = (
                self._host._projects.get(chat_meta.project_id) if chat_meta else None
            )
            try:
                archive_outcome = await self._host.archive_chat(target_id)
            except Exception:  # noqa: BLE001
                logger.exception("Auto-archive failed for schedule chat %s", target_id)
                archive_outcome = None
            if archive_outcome is not None:
                try:
                    self._host.run_archive_postprocess(
                        target_id, archive_outcome, chat_meta, project_meta
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Auto-archive postprocess failed for schedule chat %s",
                        target_id,
                    )
                outcome.archived_to = str(archive_outcome.path)
                result["archived_to"] = str(archive_outcome.path)
            else:
                logger.warning(
                    "Auto-archive requested but archive_chat returned None for %s",
                    target_id,
                )

        _sched_status, _sched_error = chat_service._schedule_dispatch_status(outcome)
        # Interval entries surface their own last_status in the UI, so hand the
        # classification back. "skipped" (a permission prompt or a deferred
        # retry) is not an error, but it is not a completed run either -- report
        # it as such rather than flattening it to "ok".
        result["status"] = "error" if _sched_status == "error" else _sched_status
        # A failed run is stamped on the stored row, not just in the job log:
        # without this a wall-clock entry failing every run (an archived target
        # resuming a dead provider session, say) produced an endless string of
        # invisible `stream error` records and nothing the operator could see
        # anywhere (issue #407). The stamp makes the PWA sidebar flag the
        # automation for attention on the next schedules refetch. A completed
        # run clears it again, so a one-off failure does not brand the entry
        # unhealthy forever — interval entries get this for free from
        # _run_interval's write-back; wall-clock entries get it here. Re-read
        # the row first: the run streamed for minutes and the user may have
        # edited or retargeted it meanwhile — only the health field is ours to
        # write.
        #
        # `stamp_run_outcome` decides what "ours" means when two dispatches
        # overlap: the health field belongs to the dispatch the row still names,
        # while a completed run credits the occurrence it was dispatched for
        # either way (issue #490).
        if _sched_schedule_id and _sched_status in {"error", "ok", "skipped"}:
            store = cast(
                ScheduleStore | None,
                getattr(self._host, "schedule_store", None),
            )
            if store is not None:
                latest = store.get(_sched_schedule_id)
                if latest is not None and schedule_support.stamp_run_outcome(
                    latest, _sched_dispatch_id, _sched_status
                ):
                    store.replace(latest)
                    # An open sidebar or Automations page only refetches on the
                    # schedules_changed event; without publishing it the newly
                    # stamped health (a failure that needs attention, or a
                    # skipped run waiting on the user) stays invisible until an
                    # unrelated refetch or reload.
                    schedule_support.publish_automations_changed(self._host)
        job_runs.record_run(job_runs.JobRun(
            job="schedule_dispatch",
            label="Scheduled dispatch",
            category="content",
            started_at=_sched_started.isoformat(),
            ended_at=datetime.now(UTC).isoformat(),
            duration_ms=int((time.perf_counter() - _sched_perf) * 1000),
            status=_sched_status,
            model=model,
            provider=provider or "claude",
            error=_sched_error,
            extra={
                "schedule_id": _sched_schedule_id,
                # Which dispatch produced this result. The entry keeps one
                # outcome (the latest dispatch's); the run log keeps every
                # run's, so a superseded one is still attributable to the
                # dispatch it came from instead of being lost.
                "dispatch_id": _sched_dispatch_id,
                "chat_id": target_id,
                "archived_to": outcome.archived_to,
                "permission_requested": outcome.permission_requested,
                "question_requested": outcome.question_requested,
                "retry_pending": outcome.retry_pending,
            },
        ))
        return result


# Keep the shorter name available to callers that describe the lifecycle by
# its noun rather than its verb.  The manager-facing API remains
# ``dispatch_schedule`` for compatibility.
ScheduleDispatch = ScheduleDispatcher
