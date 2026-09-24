"""Background-subagent completion watching, lifted out of ProjectChatManager.

A turn that dispatches background ``Agent`` calls ends immediately; the agents
outlive it and the CLI does not resume the parent when they finish. Something
has to keep looking, publish the falling count, poke the parent for a
consolidated report once the last one lands, and wake a chat whose CLI-owned
tasks (Monitor, background Bash) died with their CLI. That is this module.

Ownership boundary — the three pieces of state this owns, and nothing else
mutates:

* ``_pending_subagent_watchers`` — at most one live watcher task per chat.
  The registry is also the identity check every cleanup path makes: a watcher
  only tidies up, flushes a parked announce or publishes a zero count while it
  is still the chat's registered watcher, because ``start`` cancels and
  replaces the previous one and cancellation is delivered asynchronously.
* ``_background_agents_last`` — the last count published per chat, which is
  what ``chat_subagents_ready`` diffs against and what the sidebar badge and
  ``active_chat_ids`` read.
* ``_cli_task_wakes_sent`` — the per-process guard that bounds CLI-task wake
  redelivery to once per ``(chat, task)``.

Everything else it needs is the host's, reached through
:class:`SubagentWatcherHost`: the chat registry and config, the event hub, the
parked-announce machinery, the synthesis nudge (which is built from provider
and drain state the manager owns) and wake delivery. The protocol is the whole
list, so what this collaborator asks of the manager is readable without
reading the manager — and a test can drive the watcher against a stub host
instead of building a ProjectChatManager.

The manager keeps thin delegating methods under the old names: they are the
seams the suite patches (``_cli_owner_alive``,
``_nudge_synthesis_after_subagents``, ``_watch_subagent_completion_inner``),
and the watcher calls them back through the host so a patch on the manager is
still the patch this code sees.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from ciao import subagent_tracking
from ciao.providers.opencode import (
    OpencodeProvider,
    opencode_collab_tree_counts,
)
from ciao.subagent_tracking import SubagentInfo
from ciao.web import chat_service

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ciao.web.project_chats import ChatInfo

logger = logging.getLogger(__name__)

# Why the synthesis nudge did or did not go out. The caller has to tell
# "nothing will ever announce for this turn" (release the parked announce)
# apart from "a user turn took over" (say nothing — that turn announces for
# itself, and pushing here would deliver the interim non-answer mid-turn).
#
# It lives with the watcher rather than with the nudge: the nudge is the
# manager's (it is assembled from provider and drain state), but the watcher
# is the only caller and these outcomes are exactly the contract it needs back.
# ``project_chats`` re-exports them.
NudgeOutcome = Literal["sent", "reported", "superseded", "declined"]
NUDGE_SENT: NudgeOutcome = "sent"
NUDGE_REPORTED: NudgeOutcome = "reported"
NUDGE_SUPERSEDED: NudgeOutcome = "superseded"
NUDGE_DECLINED: NudgeOutcome = "declined"

# The orphaned-CLI-task startup sweep only wakes chats active within this
# window: the first upgrade after the sweep shipped must not wake every chat
# that ever left a Monitor running months ago, and a Monitor worth checking
# is one from this week.
_ORPHANED_CLI_TASK_SWEEP_MAX_AGE = timedelta(days=7)

# How long the startup sweep lets the app settle before delivering a wake, so
# it does not fire mid-startup. Same value as the background-run coalescing
# window in ``project_chats`` and deliberately a separate constant: that one
# batches a burst of finished scripts into one turn, this one waits out
# startup, and they are free to diverge.
_STARTUP_WAKE_COALESCE_SECONDS = 5.0


class SubagentWatcherHost(Protocol):
    """What :class:`SubagentWatchers` needs from the chat manager.

    Deliberately exhaustive: if it is not here, the watcher does not touch it.
    The names keep their manager-side spelling because these are the manager's
    existing seams and the suite patches several of them by name.
    """

    _chats: dict[str, ChatInfo]
    _providers: dict[str, Any]
    _config: Any
    _events: Any
    _restart_draining: bool

    def _save(self, *, reason: str = ...) -> None: ...

    def _agent_root_for_chat(self, chat_id: str) -> Path: ...

    def _parked_announce_token(self, chat_id: str) -> int | None: ...

    def _flush_result_announce(
        self, chat_id: str, token: int | None = ...
    ) -> bool: ...

    def _arm_parked_announce_deadline(self, chat_id: str, token: int) -> None: ...

    def _cli_owner_alive(self, chat_id: str) -> bool: ...

    def _is_interim_subagent_text(self, text: str) -> bool: ...

    async def _nudge_synthesis_after_subagents(
        self, chat_id: str, awaiting_user_answer: bool = ...,
        already_reported: bool = ...,
    ) -> NudgeOutcome: ...

    def _deliver_wake(self, parent: ChatInfo, prompt: str, *, count: int) -> str: ...

    async def _watch_subagent_completion(
        self, chat_id: str, project_id: str
    ) -> None: ...

    async def _watch_subagent_completion_inner(
        self, chat_id: str, project_id: str, handed_to_drain: list[bool]
    ) -> None: ...


class SubagentWatchers:
    """One live watcher per chat, plus the CLI-task wake bookkeeping."""

    def __init__(self, host: SubagentWatcherHost) -> None:
        self._host = host
        # Per-chat background subagent completion watchers. Each active turn
        # may spawn subagents; we keep at most one watcher per chat so rapid
        # turns cannot stack pollers on the same session file.
        self._pending_subagent_watchers: dict[str, asyncio.Task] = {}
        # Last announced running-background-subagent count per chat. Feeds
        # the sidebar badge and the /ws/events connect snapshot, so a reload
        # heals a count a dropped socket left stale.
        self._background_agents_last: dict[str, int] = {}
        # (chat_id, task_id) pairs this process has already woken for.
        self._cli_task_wakes_sent: set[tuple[str, str]] = set()

    # ── what the manager reads back ──────────────────────────────────────

    @property
    def watchers(self) -> dict[str, asyncio.Task]:
        """The live watcher task per chat. Mutable: callers register into it."""
        return self._pending_subagent_watchers

    @property
    def last_counts(self) -> dict[str, int]:
        """Last published running count per chat, including zeros."""
        return self._background_agents_last

    @property
    def wakes_sent(self) -> set[tuple[str, str]]:
        """``(chat_id, task_id)`` pairs already woken this process."""
        return self._cli_task_wakes_sent

    def running_counts(self) -> dict[str, int]:
        """Last announced running-background-subagent count per chat (>0 only)."""
        return {cid: n for cid, n in self._background_agents_last.items() if n > 0}

    def running_count(self, chat_id: str) -> int:
        """Last announced count for one chat, 0 when nothing was published."""
        return self._background_agents_last.get(chat_id, 0)

    def watching_chat_ids(self) -> set[str]:
        """Chats with a live watcher, counted as active work during a drain.

        Included even before the first poll publishes a running count:
        without the slot, a parent stream can finish and briefly make a chat
        look idle while its background agents still run.
        """
        return {
            chat_id
            for chat_id, task in self._pending_subagent_watchers.items()
            if not task.done()
        }

    # ── the watcher itself (moved verbatim from ProjectChatManager) ──────

    def start(self, chat_id: str, project_id: str) -> None:
        """Replace any existing subagent watcher for this chat with a new one."""
        old = self._pending_subagent_watchers.get(chat_id)
        if old is not None and not old.done():
            old.cancel()
        task = asyncio.create_task(self._host._watch_subagent_completion(chat_id, project_id))
        self._pending_subagent_watchers[chat_id] = task

    def publish_count(self, chat_id: str, project_id: str, count: int, nudged: bool = False) -> None:
        self._background_agents_last[chat_id] = count
        self._host._events.publish({
            "type": "chat_subagents_ready",
            "chat_id": chat_id,
            "project_id": project_id,
            "remaining": count,
            "nudged": nudged,
        })

    async def watch(self, chat_id: str, project_id: str) -> None:
        """Watch the session JSONL until background subagents finish.

        The SDK's ``list_subagents`` enumerates transcript *files*, which
        persist after completion, so its count never drops. The parent
        session JSONL carries the dispatches (``toolUseResult.isAsync``) and,
        usually, a ``<task-notification>`` envelope per completion.

        "Usually" is why every tick also consults the agents' own transcripts
        (``subagent_tracking.running_background_agents``): the CLI can defer
        that notification to the next turn boundary, so an agent that finished
        while the parent turn was still running leaves the count pinned at N
        with nothing left in the parent file to ever bring it down. Recheck on
        every tick, not just when the parent file grows, for the same reason.

        Emits ``chat_subagents_ready`` whenever the running count changes and
        schedules a delayed push when the last one completes.
        """
        # Every `return` below is a path where no nudge will ever fire, so the
        # parked announce has to go out or the chat completes in silence. The
        # finally is the backstop for all of them, including an exception.
        #
        # Guarded on still being the registered watcher: `_start_subagent_watcher`
        # cancels and replaces the previous one, and cancellation is delivered
        # asynchronously, so a superseded watcher's finally can run *after* the
        # next turn has parked its own announce. Flushing there would push a
        # result while its synthesis nudge was still pending. The chat's newest
        # watcher is the only one entitled to release the chat's parked entry;
        # the superseded turn's entry was already overwritten by that park.
        # A box rather than a return value: the handoff has to survive the
        # inner watcher raising *after* the nudge landed (a publish callback
        # that throws, a bad JSONL line on the next tick). A lost return value
        # would send this finally down the flush path while the drain is still
        # going to announce the synthesis reply — two pushes for one turn.
        handed_to_drain: list[bool] = []
        try:
            await self._host._watch_subagent_completion_inner(
                chat_id, project_id, handed_to_drain
            )
        finally:
            # A non-empty box means a nudge landed and the between-turns
            # drain now owns the release — it is the only code that learns
            # whether a synthesis reply actually arrived and was worth
            # announcing. Flushing here would push the interim non-answer
            # alongside it.
            #
            # A live foreground turn owns it for the same reason: the nudge
            # returning NUDGE_SUPERSEDED breaks the loop straight into this
            # `finally`, so without the check the very flush that path
            # declines to do happens here a moment later — the "I'll report
            # back once the agents finish" push landing mid-turn. That turn's
            # own turn-done handling discards this entry and announces for
            # itself, so nothing is lost by staying quiet.
            if not handed_to_drain:
                current = self._pending_subagent_watchers.get(chat_id)
                if current is None or current is asyncio.current_task():
                    self._host._flush_result_announce(chat_id)

    async def watch_inner(
        self, chat_id: str, project_id: str, handed_to_drain: list[bool]
    ) -> None:
        """Appends to ``handed_to_drain`` once the drain owns the parked announce."""
        chat = self._host._chats.get(chat_id)
        if chat is None or not chat.session_id:
            return
        if chat.provider == "opencode":
            # opencode has no nudge path, so nothing can take the park from us.
            await self._watch_opencode(chat_id, project_id)
            return
        # This watcher looks the file up exactly once; a miss here is final
        # (the loop below would never run), so force the cross-cwd fallback
        # past the shared cache's rescan rate limit — see subagent_tracking.
        path = subagent_tracking.find_parent_session_file(
            chat.session_id,
            self._host._config.workspace_root,
            agent_root=self._host._agent_root_for_chat(chat_id),
            force_refresh=True,
        )
        if path is None:
            return

        last_count = -1
        last_size = -1
        # Pending-notification handling. The completion watcher delays the
        # synthesis nudge while the CLI is still processing a
        # <task-notification>: steering into that exact window is the race
        # that killed the 2026-08-30 daily-log run (the two prompts crossed
        # on the transport, the SDK read task was cancelled, and the run
        # ended on an interim message with the agents' data never
        # synthesized). The hold is bounded at two ticks (~6s): a CLI that
        # died before answering — the other half of that same failure —
        # must not leave the chat parked on the interim message until the
        # deadline, and steer() queues on the persistent client, so firing
        # after the grace still lands after whatever turn the CLI is
        # running instead of interleaving with it. "Nudged" is reported to
        # clients only once, with the zero-count publish it belongs to.
        held_ticks = 0
        nudged = False
        # One attempt per watcher, outcome regardless. Every exit path but the
        # CLI-task one breaks right after the nudge, so a *failed* nudge got a
        # retry only there — and that retry was harmful twice over: the failed
        # attempt already flushed the parked interim announce, so a later
        # success handed the drain a turn that would announce again (two pushes
        # for one turn, the interim non-answer among them), and while the nudge
        # kept failing — a parent that ended on a question never stops failing —
        # every 3s tick re-ran `last_activity_at = now` + `_save()` and another
        # steer attempt for the watcher's full hour.
        nudge_attempted = False
        task_wake_sent = False
        cli_gone_ticks = 0
        state: subagent_tracking.SessionSubagentState | None = None
        # Background agents can run for a long while; poll cheaply (a stat
        # per tick, a re-parse only when the file grew) with a wide horizon.
        deadline = time.perf_counter() + 3600
        try:
            while time.perf_counter() < deadline:
                try:
                    size = path.stat().st_size
                except OSError:
                    break
                if size != last_size or state is None:
                    last_size = size
                    state = subagent_tracking.parse_session_subagents(path)
                count = subagent_tracking.running_background_agents(path, state)
                pending = state.notification_pending
                # CLI-owned tasks (Monitor / background Bash): when the CLI
                # subprocess that owns them is gone, their completion will
                # never be delivered. Two consecutive disconnected ticks give
                # a normal between-turns reconnect time to come back before
                # the wake fires.
                tasks = subagent_tracking.running_tasks(state)
                if tasks and not task_wake_sent:
                    if not self.unwoken_tasks(chat_id, tasks):
                        # Already woken this process; a wake whose turn never
                        # persisted is retried at most once per restart, not
                        # every tick.
                        task_wake_sent = True
                    else:
                        cli_gone_ticks = 0 if self._host._cli_owner_alive(chat_id) else cli_gone_ticks + 1
                        if cli_gone_ticks >= 2:
                            self.wake_for_dead_cli_tasks(chat, project_id, tasks)
                            task_wake_sent = True
                if pending:
                    held_ticks += 1
                else:
                    # A closed window must not spend its grace on the next
                    # one: an earlier notification leaves held_ticks at
                    # whatever it climbed to, and without the reset a later
                    # notification would inherit "grace expired" on its first
                    # tick and steer into the CLI's processing window — the
                    # exact prompt-crossing race the hold exists to prevent.
                    held_ticks = 0
                grace_expired = pending and held_ticks > 2
                ready_to_nudge = (
                    count == 0
                    and not nudge_attempted
                    and (not pending or grace_expired)
                )
                if count != last_count or ready_to_nudge:
                    if ready_to_nudge:
                        chat_now = self._host._chats.get(chat_id)
                        if chat_now is not None:
                            chat_now.last_activity_at = chat_service._now_iso()
                            self._host._save()
                        # Poke the parent to synthesize a final report. The
                        # CLI won't auto-continue the turn on its own, so
                        # without this the chat sits on the interim
                        # "I'll report back" message forever. The
                        # unprocessed-notification hold above decides *when*:
                        # not inside the CLI's own window (the race that
                        # killed the 2026-08-30 daily-log run), or, if the
                        # window never closes, after the bounded grace. When
                        # the nudge lands on the live client the between-turns
                        # drain publishes the reply (and its own push); we
                        # only fall back to a bare push if the nudge could
                        # not be delivered. We intentionally do NOT send a
                        # separate generic "Background agents finished" push
                        # — it stacked a second, content-free notification
                        # on top of the chat's own result push (user
                        # feedback). The in-app subagent count below still
                        # updates the UI.
                        # The question hold stays absolute (inside the
                        # nudge call); only the notification hold above
                        # carries the bounded grace.
                        nudge_attempted = True
                        already_reported = (
                            state.notification_answered
                            and not state.notification_pending
                            and not self._host._is_interim_subagent_text(
                                state.last_assistant_text
                            )
                        )
                        outcome = await self._host._nudge_synthesis_after_subagents(
                            chat_id,
                            awaiting_user_answer=state.awaiting_user_answer,
                            already_reported=already_reported,
                        )
                        nudged = outcome == NUDGE_SENT
                        if outcome in (NUDGE_SENT, NUDGE_REPORTED):
                            handed_to_drain.append(True)
                            parked_token = self._host._parked_announce_token(chat_id)
                            if parked_token is not None:
                                self._host._arm_parked_announce_deadline(
                                    chat_id, parked_token
                                )
                        elif outcome == NUDGE_DECLINED:
                            current = self._pending_subagent_watchers.get(chat_id)
                            if current is None or current is asyncio.current_task():
                                self._host._flush_result_announce(
                                    chat_id,
                                    self._host._parked_announce_token(chat_id),
                                )
                    if count != last_count or (ready_to_nudge and nudged):
                        self.publish_count(chat_id, project_id, count, nudged=nudged)
                    last_count = count
                # Keep the watcher alive while tracked CLI tasks are still
                # running and their wake has not gone out: once the owning
                # CLI dies, nothing else would ever deliver the completion.
                # After the wake (or once the tasks complete via
                # notification) the normal exit rules apply — the CLI
                # answers its own task-notifications on resume.
                if count == 0 and tasks and not task_wake_sent:
                    if self._host._restart_draining:
                        # A restart must not wait an hour on this watcher:
                        # active_chat_ids() counts it and the restart drain
                        # has no timeout. sweep_orphaned_cli_tasks wakes the
                        # chat after the restart, so there is nothing left
                        # to guard here.
                        break
                    if time.perf_counter() >= deadline:
                        break
                    await asyncio.sleep(3)
                    continue
                if count == 0 and (not pending or nudged or grace_expired):
                    break
                await asyncio.sleep(3)
        finally:
            # Clean up our slot when the watcher exits.
            current = self._pending_subagent_watchers.get(chat_id)
            if current is asyncio.current_task():
                self._pending_subagent_watchers.pop(chat_id, None)
                if last_count > 0:
                    # Exiting on the deadline, a vanished session file, or a
                    # crash while the count is still positive would leave every
                    # connected client showing a badge that can never clear
                    # (the events snapshot only heals it on reconnect). We are
                    # no longer watching, so announce zero. Cancellation by a
                    # replacement watcher skips this: it already owns the slot
                    # and will publish the real count on its first tick.
                    self.publish_count(chat_id, project_id, 0)
                self._background_agents_last.pop(chat_id, None)
            # A superseded watcher leaves the replacement's count alone: it
            # already published on its first tick, and wiping the baseline
            # here would make the next tick republish a duplicate event.

    async def _watch_opencode(
        self, chat_id: str, project_id: str
    ) -> None:
        """Poll the opencode session tree while background children run."""
        last_count = -1
        deadline = time.perf_counter() + 3600
        try:
            while time.perf_counter() < deadline:
                chat = self._host._chats.get(chat_id)
                if chat is None or chat.provider != "opencode" or not chat.session_id:
                    break
                provider_service = self._host._providers.get(chat_id)
                live_provider = (
                    provider_service.provider
                    if provider_service is not None
                    else None
                )
                if (
                    isinstance(live_provider, OpencodeProvider)
                    and live_provider.has_live_server
                    and live_provider.current_session_id == chat.session_id
                ):
                    # /api/session/active is process-local. A throwaway read
                    # server cannot see children still executing in this chat's
                    # server, so use the live connection whenever it exists.
                    tree = await live_provider.read_live_collab_tree()
                else:
                    tree = await OpencodeProvider.read_collab_tree(
                        self._host._agent_root_for_chat(chat_id), chat.session_id
                    )
                count, had_subagents = opencode_collab_tree_counts(tree)
                if count != last_count:
                    if count == 0 and last_count > 0:
                        chat.last_activity_at = chat_service._now_iso()
                        self._host._save()
                        # No separate "Background agents finished" push — the
                        # chat's own result notification covers it; the extra
                        # generic ping was redundant (user feedback).
                    self.publish_count(chat_id, project_id, count, nudged=False)
                    last_count = count
                if not had_subagents or count == 0:
                    break
                await asyncio.sleep(3)
        finally:
            current = self._pending_subagent_watchers.get(chat_id)
            if current is asyncio.current_task():
                self._pending_subagent_watchers.pop(chat_id, None)
                if last_count > 0:
                    # See the Claude watcher: never leave clients holding a
                    # count we have stopped maintaining.
                    self.publish_count(chat_id, project_id, 0)
                self._background_agents_last.pop(chat_id, None)
            # A superseded watcher must not wipe the replacement's baseline
            # (see the Claude watcher above).

    def unwoken_tasks(
        self, chat_id: str, tasks: list[SubagentInfo]
    ) -> list[SubagentInfo]:
        """CLI tasks in *tasks* this process has not already woken for.

        ``_cli_task_wakes_sent`` bounds redelivery to once per process
        lifetime: a wake whose turn never reached the JSONL (CLI cannot
        reconnect, auth down) would otherwise re-arm on every watcher tick
        or restart sweep, because nothing marks the tasks lost. Across a
        restart the JSONL "lost" marker from a persisted wake is the durable
        guard, so a wake that never persisted is retried at most once per
        restart.
        """
        return [
            task
            for task in tasks
            if (chat_id, task.agent_id) not in self._cli_task_wakes_sent
        ]

    def wake_for_dead_cli_tasks(
        self, parent: ChatInfo, project_id: str, tasks: list[SubagentInfo]
    ) -> None:
        """Deliver one wake turn for CLI tasks whose owning CLI is gone.

        Tasks already woken this process are filtered out first; if none
        remain, nothing is delivered and the (chat_id, task_id) pairs are
        recorded *before* delivery so a failed wake is not re-armed.
        """
        tasks = self.unwoken_tasks(parent.chat_id, tasks)
        if not tasks:
            return
        for task in tasks:
            self._cli_task_wakes_sent.add((parent.chat_id, task.agent_id))
        prompt = self.build_cli_task_wake_prompt(tasks)
        self._host._deliver_wake(parent, prompt, count=len(tasks))

    def sweep_orphaned_cli_tasks(self) -> int:
        """After a restart, wake chats whose CLI tasks (Monitor / background Bash)
        were still running: the CLI that owned them died with the old server.

        No watcher survives a restart (one is only armed when a turn finishes),
        so without this sweep a task that was running at shutdown would never
        produce a wake. Only chats active within
        ``_ORPHANED_CLI_TASK_SWEEP_MAX_AGE`` are woken: the first upgrade after
        the sweep shipped must not wake every chat that ever left a Monitor
        running months ago. An unparseable or empty ``last_activity_at`` never
        skips a chat — the wake is worth more than the risk of missing one.
        Delivery goes through the same deferred path as
        ``queue_background_wake`` — a bounded coalescing sleep, then
        ``_deliver_wake`` — so the sweep does not fire mid-startup and never
        raises into the caller. Tasks already woken this process
        (``_cli_task_wakes_sent``) are skipped; across a restart the JSONL
        "lost" marker from a persisted wake is the durable guard, so a wake
        that never persisted is retried at most once per restart. Returns
        the number of chats armed for a wake.
        """
        woken = 0
        for chat in list(self._host._chats.values()):
            try:
                if chat.archived or not chat.session_id:
                    continue
                if chat.provider != "claude":
                    continue
                last_active = chat_service._parse_iso(chat.last_activity_at)
                if (
                    last_active is not None
                    and datetime.now(UTC) - last_active
                    > _ORPHANED_CLI_TASK_SWEEP_MAX_AGE
                ):
                    continue
                path = subagent_tracking.find_parent_session_file(
                    chat.session_id,
                    self._host._config.workspace_root,
                    agent_root=self._host._agent_root_for_chat(chat.chat_id),
                )
                if path is None:
                    continue
                state = subagent_tracking.parse_session_subagents(path)
                tasks = self.unwoken_tasks(
                    chat.chat_id, subagent_tracking.running_tasks(state)
                )
                if not tasks:
                    continue
                woken += 1
                try:
                    asyncio.create_task(self._deferred_cli_task_wake(chat))
                except RuntimeError:
                    # No running loop (e.g. a sync startup path). Dropping the
                    # wake beats raising into the caller; the tasks stay
                    # "running" in the JSONL, so nothing is lost by trying
                    # again on the next sweep.
                    logger.debug(
                        "No event loop for orphaned CLI task wake of %s",
                        chat.chat_id,
                    )
            except Exception:  # noqa: BLE001 — a sweep failure must not kill startup
                logger.exception(
                    "Orphaned CLI task sweep failed for chat %s",
                    getattr(chat, "chat_id", "?"),
                )
        return woken

    async def _deferred_cli_task_wake(self, parent: ChatInfo) -> None:
        """Wait out the startup coalescing window, then wake for dead-CLI tasks."""
        try:
            await asyncio.sleep(_STARTUP_WAKE_COALESCE_SECONDS)
            tasks = self.unwoken_tasks(
                parent.chat_id,
                subagent_tracking.running_tasks(
                    subagent_tracking.parse_session_subagents(
                        subagent_tracking.find_parent_session_file(
                            parent.session_id,
                            self._host._config.workspace_root,
                            agent_root=self._host._agent_root_for_chat(parent.chat_id),
                        )
                        or Path("nonexistent")
                    )
                ),
            )
            if not tasks:
                return
            self.wake_for_dead_cli_tasks(parent, parent.project_id, tasks)
        except Exception:  # noqa: BLE001 — a failed wake must not kill the app
            logger.exception(
                "Orphaned CLI task wake failed for chat %s", parent.chat_id
            )

    @staticmethod
    def build_cli_task_wake_prompt(tasks: list[SubagentInfo]) -> str:
        """Compose the wake turn for CLI tasks orphaned by a dead CLI.

        Mirrors the background-run wake: name the log/output the command was
        writing and tell the chat to verify rather than assume. The first line
        carries ``subagent_tracking.CLI_TASK_WAKE_PREFIX`` so the parser can
        recognise this prompt in the JSONL later and mark the tasks lost —
        the wake must never be sent twice.
        """
        lines = [
            f"{subagent_tracking.CLI_TASK_WAKE_PREFIX} {len(tasks)} CLI task"
            f"{'s' if len(tasks) != 1 else ''} you started "
            "(Monitor / background shell) were lost: the Claude CLI process "
            "that owned them has exited, so their completion will never be "
            "delivered to this chat."
        ]
        for task in tasks:
            lines.append("")
            lines.append(f"— {task.subagent_type}: {task.description} (task {task.agent_id})")
            if task.command:
                lines.append(f"command: {task.command}")
        lines.append("")
        lines.append(
            "Check the real state yourself now: read the log or output file "
            "the command was writing, and inspect the process (pgrep/ps) "
            "rather than assuming it finished or that the last lines tell the "
            "whole story. For future long-running commands use the "
            "`ciao run start -- <cmd>` command, which survives CLI restarts "
            "and wakes this chat with the exit code, log tail and log path."
        )
        return "\n".join(lines)
