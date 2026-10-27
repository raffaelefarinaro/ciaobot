"""Background-subagent completion watching, lifted out of ProjectChatManager.

A turn that dispatches background ``Agent`` calls ends immediately; the agents
outlive it. While the CLI stays connected it runs a follow-up turn by itself
for each completion, and the between-turns drain streams that turn (#1394).
This module keeps looking at the session file to publish the falling count,
releases a parked announce when no follow-up will come, and wakes a chat whose
CLI-owned tasks (Monitor, background Bash) died with their CLI.

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
parked-announce machinery, the drain state the manager owns and wake delivery. The protocol is the whole
list, so what this collaborator asks of the manager is readable without
reading the manager — and a test can drive the watcher against a stub host
instead of building a ProjectChatManager.

The manager keeps thin delegating methods under the old names: they are the
seams the suite patches (``_cli_owner_alive`` and
``_watch_subagent_completion_inner``),
and the watcher calls them back through the host so a patch on the manager is
still the patch this code sees.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ciao import subagent_tracking
from ciao.providers.base import BACKGROUND_WORK_MAX_S as _BACKGROUND_WORK_MAX_S
from ciao.providers.opencode import (
    OpencodeProvider,
    opencode_collab_tree_counts,
)
from ciao.subagent_tracking import SubagentInfo
from ciao.web import chat_service

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ciao.web.project_chats import ChatInfo

logger = logging.getLogger(__name__)

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

# The cap lives in providers.base (the providers enforce it too); re-exported
# here as the watcher horizon.
BACKGROUND_WORK_MAX_S = _BACKGROUND_WORK_MAX_S


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

    def _flush_result_announce(self, chat_id: str) -> bool: ...

    def _has_live_drain(self, chat_id: str) -> bool: ...

    def _cli_owner_alive(self, chat_id: str) -> bool: ...

    def _deliver_wake(self, parent: ChatInfo, prompt: str, *, count: int, origin: str) -> str: ...

    async def _watch_subagent_completion(
        self, chat_id: str, project_id: str
    ) -> None: ...

    async def _watch_subagent_completion_inner(
        self, chat_id: str, project_id: str, saw_work: list[bool]
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
        # Any background work (agents plus opencode shells) per chat, for
        # lifecycle decisions; the published count is agents only.
        self._background_work_last: dict[str, int] = {}
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

    def work_count(self, chat_id: str) -> int:
        """Last announced background work for one chat, shells included."""
        return self._background_work_last.get(chat_id, 0)

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

    def current_count(self, chat_id: str) -> int:
        """Running background agents for *chat_id*, read from the session file now.

        The drain asks this when a follow-up result lands: the CLI writes the
        completion into the JSONL before it runs the follow-up turn, so a fresh
        read says whether this was the last one, where the published count can
        lag a poll behind. Chats without a Claude session file report the last
        published count.
        """
        chat = self._host._chats.get(chat_id)
        if chat is not None and chat.provider == "opencode":
            service = self._host._providers.get(chat_id)
            live = service.provider if service is not None else None
            if isinstance(live, OpencodeProvider) and live.has_live_server:
                # The provider tracks shells and children from the event
                # stream, so this is fresher than the published count.
                return live.background_count
            return self.running_count(chat_id)
        if chat is None or not chat.session_id or chat.provider != "claude":
            return self.running_count(chat_id)
        path = subagent_tracking.find_parent_session_file(
            chat.session_id,
            self._host._config.workspace_root,
            agent_root=self._host._agent_root_for_chat(chat_id),
        )
        if path is None:
            return 0
        state = subagent_tracking.parse_session_subagents(path)
        return subagent_tracking.running_background_agents(path, state)

    # ── the watcher itself (moved verbatim from ProjectChatManager) ──────

    def start(self, chat_id: str, project_id: str) -> None:
        """Replace any existing subagent watcher for this chat with a new one."""
        old = self._pending_subagent_watchers.get(chat_id)
        if old is not None and not old.done():
            old.cancel()
        task = asyncio.create_task(self._host._watch_subagent_completion(chat_id, project_id))
        self._pending_subagent_watchers[chat_id] = task

    def publish_count(
        self, chat_id: str, project_id: str, count: int, *, work: int | None = None
    ) -> None:
        """Announce *count* agents; *work* is the lifecycle count when it differs."""
        self._background_agents_last[chat_id] = count
        self._background_work_last[chat_id] = count if work is None else work
        self._host._events.publish({
            "type": "chat_subagents_ready",
            "chat_id": chat_id,
            "project_id": project_id,
            "remaining": count,
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

        Emits ``chat_subagents_ready`` whenever the running count changes.
        """
        # A watcher that saw background work leaves the park to a live drain:
        # each finished task brings a native follow-up turn, and the drain
        # announces the one that lands at count zero. Every other exit releases
        # it here. Guarded on still being the registered watcher, because
        # `start` cancels and replaces asynchronously.
        saw_work: list[bool] = []
        try:
            await self._host._watch_subagent_completion_inner(
                chat_id, project_id, saw_work
            )
        finally:
            current = self._pending_subagent_watchers.get(chat_id)
            if current is None or current is asyncio.current_task():
                if not (saw_work and self._host._has_live_drain(chat_id)):
                    self._host._flush_result_announce(chat_id)

    async def watch_inner(
        self, chat_id: str, project_id: str, saw_work: list[bool]
    ) -> None:
        """Appends to ``saw_work`` once it sees background agents or CLI tasks running."""
        chat = self._host._chats.get(chat_id)
        if chat is None or not chat.session_id:
            return
        if chat.provider == "opencode":
            await self._watch_opencode(chat_id, project_id, saw_work)
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
        task_wake_sent = False
        cli_gone_ticks = 0
        state: subagent_tracking.SessionSubagentState | None = None
        # Background agents can run for a long while; poll cheaply (a stat
        # per tick, a re-parse only when the file grew) up to the shared cap.
        deadline = time.perf_counter() + BACKGROUND_WORK_MAX_S
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
                tasks = subagent_tracking.running_tasks(state)
                if (count > 0 or tasks) and not saw_work:
                    saw_work.append(True)
                # CLI-owned tasks (Monitor / background Bash): when the CLI
                # subprocess that owns them is gone, their completion will
                # never be delivered. Two consecutive disconnected ticks give
                # a normal between-turns reconnect time to come back before
                # the wake fires.
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
                if count != last_count:
                    if count == 0 and last_count > 0:
                        chat_now = self._host._chats.get(chat_id)
                        if chat_now is not None:
                            chat_now.last_activity_at = chat_service._now_iso()
                            self._host._save()
                    self.publish_count(chat_id, project_id, count)
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
                if count == 0:
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
                self._background_work_last.pop(chat_id, None)
            # A superseded watcher leaves the replacement's count alone: it
            # already published on its first tick, and wiping the baseline
            # here would make the next tick republish a duplicate event.

    async def _watch_opencode(
        self, chat_id: str, project_id: str, saw_work: list[bool]
    ) -> None:
        """Poll the opencode session tree and its background shells while background work runs."""
        last_count = -1
        last_work = -1
        deadline = time.perf_counter() + BACKGROUND_WORK_MAX_S
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
                live = (
                    live_provider
                    if isinstance(live_provider, OpencodeProvider)
                    and live_provider.has_live_server
                    and live_provider.current_session_id == chat.session_id
                    else None
                )
                if live is not None:
                    # /api/session/active is process-local. A throwaway read
                    # server cannot see children still executing in this chat's
                    # server, so use the live connection whenever it exists.
                    tree = await live.read_live_collab_tree()
                    shells = await live.refresh_background_shells()
                else:
                    tree = await OpencodeProvider.read_collab_tree(
                        self._host._agent_root_for_chat(chat_id), chat.session_id
                    )
                    # Background shells are child processes of the chat's
                    # server: with no live server there are none.
                    shells = 0
                children, had_subagents = opencode_collab_tree_counts(tree)
                # Shells are background work, not agents: they keep the watcher
                # alive but are not published as the agent count.
                work = children + shells
                if work > 0 and not saw_work:
                    # Tells `watch` a live drain owns the parked announce.
                    saw_work.append(True)
                if work != last_work:
                    if work == 0 and last_work > 0:
                        chat.last_activity_at = chat_service._now_iso()
                        self._host._save()
                        # No separate "Background agents finished" push — the
                        # chat's own result notification covers it; the extra
                        # generic ping was redundant (user feedback).
                    last_work = work
                if children != last_count:
                    self.publish_count(chat_id, project_id, children, work=work)
                    last_count = children
                else:
                    self._background_work_last[chat_id] = work
                if (not had_subagents and shells == 0) or work == 0:
                    break
                if self._host._restart_draining:
                    # A restart must not wait an hour on this watcher:
                    # active_chat_ids() counts it and the restart drain has no
                    # timeout. Engine shutdown ends the background work anyway.
                    break
                if live is not None:
                    # Republish as soon as the drain/pump sees a shell exit or
                    # a child end, so the count reaches zero before the
                    # follow-up result arrives.
                    await live.wait_background_change(3.0)
                else:
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
                self._background_work_last.pop(chat_id, None)
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

    def cli_task_candidates(self, chat: ChatInfo) -> list[SubagentInfo]:
        """*chat*'s running CLI tasks that this process has not woken for.

        The one resolution step both wake paths need: the boot-time
        ``sweep_orphaned_cli_tasks`` and the cancelled-drain replay. Empty when
        the session file is gone, when nothing is still running in it, or when
        every task in it was already woken this process.
        """
        path = subagent_tracking.find_parent_session_file(
            chat.session_id,
            self._host._config.workspace_root,
            agent_root=self._host._agent_root_for_chat(chat.chat_id),
        )
        if path is None:
            return []
        state = subagent_tracking.parse_session_subagents(path)
        return self.unwoken_tasks(chat.chat_id, subagent_tracking.running_tasks(state))

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
        self._host._deliver_wake(parent, prompt, count=len(tasks), origin="cli_task_wake")

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
                tasks = self.cli_task_candidates(chat)
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
