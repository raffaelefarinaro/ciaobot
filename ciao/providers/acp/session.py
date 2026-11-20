"""One ACP agent session: open, one prompt at a time, config, late turns, journal (#3137).

``session_update`` is delivered to :meth:`AcpSession.on_update`, which only
enqueues. The SDK runs each notification in its own task, so the order the
client observes is the order of those enqueues, and there is no ``await``
before ``put_nowait``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from acp.exceptions import RequestError
from acp.schema import (
    AudioContentBlock,
    EmbeddedResourceContentBlock,
    ImageContentBlock,
    ResourceContentBlock,
    TextContentBlock,
)
from pydantic import TypeAdapter

from ciao.model_tiers import resolve_tier
from ciao.models import (
    BridgeMode,
    ContextCompactedEvent,
    ResultEvent,
    LATE_WAKE_NOTICE,
    ShellFinishedEvent,
    StreamEvent,
    SystemStatusEvent,
    ToolUseEvent,
)
from ciao.os_support.processes import process_table
from ciao.providers.acp.connection import AcpConnection
from ciao.providers.acp.normalize import TurnText, UpdateNormalizer, canonical, result_event
from ciao.providers.acp.permissions import PermissionBroker, mode_choices, pick_value
from ciao.providers.acp.shell_liveness import CHECK_INTERVAL_S, ShellLiveness, TrackedShell
from ciao.providers.acp.shell_output import read_tail
from ciao.providers.acp.spec import AgentSpec
from ciao.redact import redact_text

logger = logging.getLogger(__name__)

_LATE_QUIET = frozenset({"usage_update", "config_option_update", "current_mode_update", "shell_settle"})
# Background shell commands held between a launch and its acknowledgement; a session has few.
_SHELL_COMMANDS_LIMIT = 64
# Longest a shell's output tail may take to read; a slow read gives up rather than hold the notice.
SHELL_TAIL_READ_S = 5.0
# A chat's model string is user-controlled, so a status line echoes at most this many characters.
_STATUS_MODEL_CHARS = 80

_CONTENT_BLOCK: TypeAdapter[Any] = TypeAdapter(
    TextContentBlock
    | ImageContentBlock
    | AudioContentBlock
    | ResourceContentBlock
    | EmbeddedResourceContentBlock
)


def journal_file(runtime_dir: Path, agent_id: str, session_id: str) -> Path:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return runtime_dir / "acp-journal" / agent_id / f"{digest}.jsonl"


def _prepare_journal_file(path: Path) -> None:
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(directory, 0o700)
        if directory.parent.name == "acp-journal":
            os.chmod(directory.parent, 0o700)
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
    if os.name != "nt":
        os.chmod(path, 0o600)


def journal_path(runtime_dir: Path, agent_id: str, session_id: str) -> Path:
    """``<runtime_dir>/acp-journal/<agent_id>/<sha256(session_id)[:32]>.jsonl`` (0600 file, 0700 dirs)."""
    path = journal_file(runtime_dir, agent_id, session_id)
    _prepare_journal_file(path)
    return path


class SessionJournal:
    """Append-only JSONL of one ACP session's user prompts and normalized events, for history."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def user(self, text: str, *, turn_origin: str = "") -> None:
        self._append({"type": "user", "text": text, "turn_origin": turn_origin})

    def event(self, payload: Mapping[str, Any]) -> None:
        """Append one ``event_to_json``-shaped dict."""
        self._append(dict(payload))

    def _append(self, payload: Mapping[str, Any]) -> None:
        _prepare_journal_file(self.path)
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        fd = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        if os.name != "nt":
            os.chmod(self.path, 0o600)

    @staticmethod
    def read(path: Path) -> list[dict[str, Any]]:
        """Every complete JSON line. A file that does not end in a newline loses its torn tail."""
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return []
        if not data:
            return []
        if not data.endswith(b"\n"):
            if b"\n" not in data:
                return []
            data = data.rsplit(b"\n", 1)[0]
        rows: list[dict[str, Any]] = []
        for line in data.splitlines():
            if not line.strip():
                continue
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                rows.append(parsed)
        return rows


def _config_options(response: Any) -> list[dict[str, Any]]:
    raw = getattr(response, "config_options", None) or []
    options: list[dict[str, Any]] = []
    for item in raw:
        if hasattr(item, "model_dump"):
            dumped = item.model_dump(by_alias=True, exclude_none=True)
            if isinstance(dumped, dict):
                options.append(dumped)
        elif isinstance(item, Mapping):
            options.append(dict(item))
    return options


class ModeNotApplied(Exception):
    """A mode other than bypass could not be set, so the turn must not run.

    The message is fixed and carries no adapter text: the caller reports a fixed sentence.
    """

    def __init__(self, mode: str) -> None:
        super().__init__(mode)
        self.mode = mode


def _update_body(update: Mapping[str, Any]) -> dict[str, Any]:
    inner = update.get("update")
    if (
        "sessionUpdate" not in update
        and "session_update" not in update
        and isinstance(inner, Mapping)
    ):
        return dict(inner)
    return dict(update)


class AcpSession:
    def __init__(
        self,
        connection: AcpConnection,
        spec: AgentSpec,
        *,
        session_id: str,
        config_options: list[dict[str, Any]],
        journal: SessionJournal,
        broker: PermissionBroker,
        session_meta: Mapping[str, Any] | None = None,
    ) -> None:
        self._connection = connection
        self.spec = spec
        self.session_id = session_id
        self.config_options = config_options
        self._journal = journal
        self._broker = broker
        meta = dict(session_meta or {})
        self.meta_key = json.dumps(meta, sort_keys=True)
        self._meta = meta
        self._normalizer = UpdateNormalizer(spec, session_id)
        self._seed_values()
        self._lock = asyncio.Lock()
        self._queue: asyncio.Queue[StreamEvent] = asyncio.Queue()
        self._late_queue: asyncio.Queue[StreamEvent] = asyncio.Queue()
        self._prompt_active = False
        self._replaying = False
        self._replay: list[dict[str, Any]] = []
        self._text = TurnText()
        self._late_text = TurnText()
        self._late_open = False
        self._late_substantive = False
        self._quiet_task: asyncio.Task[None] | None = None
        # Background shells whose process ends silently (no terminal meta): settled by a liveness check.
        self._shells = ShellLiveness()
        # End notices waiting for the prompt to finish, and the tasks reading their output tails.
        self._deferred_ends: list[ShellFinishedEvent] = []
        self._end_tasks: set[asyncio.Task[None]] = set()
        # The command each background shell was launched with, by call id, until it settles.
        self._shell_commands: dict[str, str] = {}
        self._shell_task: asyncio.Task[None] | None = None
        # The not-offered model status is reported once per session.
        self._model_reported = False
        self._outer_emit = broker.emit
        broker.emit = self._emit

    @classmethod
    async def open(
        cls,
        connection: AcpConnection,
        spec: AgentSpec,
        *,
        cwd: Path,
        resume: str | None,
        additional_dirs: Sequence[str],
        journal_dir: Path,
        broker: PermissionBroker,
        session_meta: Mapping[str, Any] | None = None,
    ) -> tuple[AcpSession, bool]:
        """Returns ``(session, replaced)``. ``replaced`` when ``resume`` was given but a new session had to be created."""
        meta = dict(session_meta or {})
        provisional = resume or "_pending"
        session = cls(
            connection,
            spec,
            session_id=resume or "",
            config_options=[],
            journal=SessionJournal(journal_file(journal_dir, spec.id, provisional)),
            broker=broker,
            session_meta=meta,
        )
        # The client forwards updates here while load replays, before ``open`` returns.
        # Cleared on every exit: after open, routing belongs to the provider's client slot,
        # and a closed session must never keep receiving updates through this fallback.
        setattr(connection, "_pending_session", session)
        try:
            session_id, replaced, options, replay = await session._establish(
                cwd, resume, additional_dirs, meta
            )
        finally:
            setattr(connection, "_pending_session", None)
        session.session_id = session_id
        session._normalizer.session_id = session_id
        session.config_options = options
        session._seed_values()
        path = journal_file(journal_dir, spec.id, session_id)
        existed = path.exists()
        session._journal = SessionJournal(path)
        if replay and not existed:
            session._rebuild(replay)
        return session, replaced

    async def _establish(
        self,
        cwd: Path,
        resume: str | None,
        additional_dirs: Sequence[str],
        meta: Mapping[str, Any],
    ) -> tuple[str, bool, list[dict[str, Any]], list[dict[str, Any]]]:
        arguments: dict[str, Any] = dict(meta)
        if self._cap_object("sessionCapabilities", "additionalDirectories"):
            arguments["additional_directories"] = [str(path) for path in additional_dirs]
        if resume and self._cap_flag("loadSession"):
            response, replay = await self._try_load(cwd, resume, arguments)
            if response is not None:
                return resume, False, _config_options(response), replay
        elif resume and self._cap_object("sessionCapabilities", "resume"):
            try:
                response = await self._connection.conn.resume_session(
                    session_id=resume, cwd=str(cwd), **arguments
                )
            except RequestError:
                response = None
            if response is not None:
                return resume, False, _config_options(response), []
        if resume:
            created = await self._connection.conn.new_session(cwd=str(cwd), **arguments)
            return created.session_id, True, _config_options(created), []
        created = await self._connection.conn.new_session(cwd=str(cwd), **arguments)
        return created.session_id, False, _config_options(created), []

    async def _try_load(
        self,
        cwd: Path,
        resume: str,
        arguments: Mapping[str, Any],
    ) -> tuple[Any, list[dict[str, Any]]]:
        self._replaying = True
        try:
            response = await self._connection.conn.load_session(
                cwd=str(cwd), session_id=resume, **arguments
            )
        except RequestError:
            response = None
        finally:
            self._replaying = False
            replay = list(self._replay)
            self._replay.clear()
        if response is None:
            return None, []
        return response, replay

    def _cap_flag(self, key: str) -> bool:
        return self._connection.agent_capabilities.get(key) is True

    def _cap_object(self, *keys: str) -> bool:
        node: Any = self._connection.agent_capabilities
        for key in keys:
            if not isinstance(node, Mapping):
                return False
            value = node.get(key)
            if value is None:
                return False
            node = value
        return True

    def _seed_values(self) -> None:
        for option in self.config_options:
            option_id = option.get("id")
            current = option.get("currentValue")
            if isinstance(option_id, str) and current is not None:
                self._normalizer.current_values[option_id] = str(current)

    def _offered(self, config_id: str) -> list[str]:
        for option in self.config_options:
            if option.get("id") != config_id:
                continue
            values: list[str] = []
            for item in option.get("options") or []:
                if not isinstance(item, Mapping):
                    continue
                if isinstance(item.get("value"), str):
                    values.append(item["value"])
                for sub in item.get("options") or []:
                    if isinstance(sub, Mapping) and isinstance(sub.get("value"), str):
                        values.append(sub["value"])
            return values
        return []

    def _emit(self, event: StreamEvent) -> None:
        self._note_update([event], kind="permission_request")
        self._outer_emit(event)

    def on_update(self, update: Mapping[str, Any]) -> None:
        """Enqueue one update. Synchronous: no await before the queue put."""
        session_id = update.get("sessionId")
        if isinstance(session_id, str) and self.session_id and session_id != self.session_id:
            return
        if self._replaying:
            self._replay.append(dict(update))
            return
        body = _update_body(update)
        events = self._normalizer.feed(body)
        if self._normalizer.compacted:
            self._normalizer.compacted = False
            events.append(ContextCompactedEvent(type="context_compacted"))
        kind = canonical(body).get("sessionUpdate")
        self._note_update(events, kind=kind if isinstance(kind, str) else "")

    def on_ext(
        self,
        method: str,
        payload: Any,
        hook: Callable[[UpdateNormalizer, str, Mapping[str, Any]], list[StreamEvent]],
    ) -> None:
        """Enqueue the events of one extension notification. Synchronous, like :meth:`on_update`.

        Only a notification that yields events counts as agent output. One that yields none (an
        auth status, a child of another session) must not touch the late turn: an empty late turn
        would otherwise open, close as substantive, and wake the chat for nothing.
        """
        if self._replaying or not isinstance(payload, Mapping):
            return
        events = hook(self._normalizer, method, payload)
        if events:
            self._note_update(events, kind="ext")

    def _note_update(self, events: list[StreamEvent], *, kind: str) -> None:
        self._finish_shells(events)
        self._track_shells(events)
        if self._prompt_active:
            for event in events:
                self._queue.put_nowait(event)
                self._text.feed(event)
                self._journal_event(event)
            return
        self._touch_late(events, substantive=kind not in _LATE_QUIET)

    def _finish_shells(self, events: list[StreamEvent]) -> None:
        """End tracking of each background shell the provider has settled, with its exit code when sent.

        A Claude ``terminal_exit`` or a normalizer result is the provider's own settle. It is the
        one place a tracked shell ends from the events, so the poll that follows finds it gone.
        """
        for event in events:
            if (
                isinstance(event, ToolUseEvent) and event.type == "tool_result" and not event.background
                and event.tool_use_id
            ):
                shell = self._shells.release(event.tool_use_id)
                if shell is not None:
                    self._end_shell(event.tool_use_id, shell, event.exit_code)

    def _end_shell(self, call_id: str, shell: TrackedShell, exit_code: int | None) -> None:
        """Announce that a background shell ended, once: the end event, with its output tail read off the loop."""
        event = ShellFinishedEvent(
            type="shell_finished",
            tool_use_id=call_id,
            command=shell.command,
            output_path=shell.output_path,
            duration_s=round(time.monotonic() - shell.launched_at, 1),
            exit_code=exit_code,
            ended_at=time.monotonic(),
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._deliver_end(event)
            return
        # A task per shell: one slow read never holds up another shell's settle.
        task = loop.create_task(self._read_tail_then_deliver(event, shell.output_path))
        self._end_tasks.add(task)
        task.add_done_callback(self._end_tasks.discard)

    async def _read_tail_then_deliver(self, event: ShellFinishedEvent, path: str) -> None:
        try:
            event.output_tail = await asyncio.wait_for(asyncio.to_thread(read_tail, path), SHELL_TAIL_READ_S)
        except (TimeoutError, OSError):
            event.output_tail = None
        self._deliver_end(event)

    def _deliver_end(self, event: ShellFinishedEvent) -> None:
        """Queue an end notice on the late path, or hold it until the running prompt finishes.

        A late turn must not open while a prompt is running: the event waits here and
        ``_flush_deferred_ends`` delivers it when the prompt ends.
        """
        if self._prompt_active:
            self._deferred_ends.append(event)
            return
        self._touch_late([event], substantive=False)

    def _flush_deferred_ends(self) -> None:
        pending, self._deferred_ends = self._deferred_ends, []
        for event in pending:
            self._touch_late([event], substantive=False)

    def _track_shells(self, events: list[StreamEvent]) -> None:
        """Start watching each background shell whose launch acknowledgement names a pid."""
        for event in events:
            if (
                isinstance(event, ToolUseEvent) and event.type == "tool_use"
                and event.shell_background and event.tool_use_id
            ):
                # Kept until the launch acknowledgement arrives, so the end notice can name it.
                self._shell_commands[event.tool_use_id] = event.tool_input
                while len(self._shell_commands) > _SHELL_COMMANDS_LIMIT:
                    del self._shell_commands[next(iter(self._shell_commands))]
                continue
            if not (isinstance(event, ToolUseEvent) and event.type == "tool_result" and event.background and event.tool_use_id):
                continue
            command = self._shell_commands.pop(event.tool_use_id, "")
            if event.shell_pid is None:
                # No pid on the wire (Claude): the shell ends only by the provider's own settle.
                tracked = self._shells.track_unpolled(event.tool_use_id, command=command, output_path=event.output_path)
            else:
                # One ``ps`` snapshot, taken once per launch: the start time is what identifies the pid.
                tracked = self._shells.track(
                    event.tool_use_id, event.shell_pid, process_table(),
                    command=command, output_path=event.output_path,
                )
            if not tracked or event.shell_pid is None:
                continue  # a pid-less shell is never polled
            if self._shell_task is None or self._shell_task.done():
                self._shell_task = asyncio.create_task(self._watch_shells())

    async def _watch_shells(self) -> None:
        """Settle each tracked shell as its process ends; stops when none with a pid is left."""
        while self._shells.polling:
            await asyncio.sleep(CHECK_INTERVAL_S)
            table = await asyncio.to_thread(process_table)
            for call_id, shell in self._shells.settled(table):
                # The same path a tool_result takes: the journal (history) and the live queue.
                # The shell is already released, so this settle does not end it a second time.
                self._note_update(
                    [ToolUseEvent(type="tool_result", tool_use_id=call_id, is_error=False, exit_code=None)],
                    kind="shell_settle",
                )
                # The poll cannot tell how it ended: no exit code.
                self._end_shell(call_id, shell, None)

    def _touch_late(self, events: list[StreamEvent], *, substantive: bool) -> None:
        if not self._late_open:
            self._late_open = True
            self._late_text = TurnText()
            self._late_substantive = False
        if substantive:
            self._late_substantive = True
        for event in events:
            self._late_queue.put_nowait(event)
            self._late_text.feed(event)
            self._journal_event(event)
        self._arm_quiet()

    def _arm_quiet(self) -> None:
        previous = self._quiet_task
        self._quiet_task = asyncio.create_task(self._quiet_wait())
        if previous is not None and previous is not asyncio.current_task():
            previous.cancel()

    async def _quiet_wait(self) -> None:
        try:
            await asyncio.sleep(self.spec.quiet_period_s)
        except asyncio.CancelledError:
            return
        self._close_late()

    def _close_late(self) -> None:
        if not self._late_open:
            return
        self._late_open = False
        task = self._quiet_task
        self._quiet_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        if self._late_substantive:
            event = result_event(
                "end_turn",
                text=self._late_text,
                session_id=self.session_id,
                model=self._current_model(),
                usage=dict(self._normalizer.usage),
                origin="task-notification",
            )
            event.wake_notice = LATE_WAKE_NOTICE
            self._late_queue.put_nowait(event)
            self._journal_event(event)
        self._late_text = TurnText()
        self._late_substantive = False

    def _journal_event(self, event: StreamEvent) -> None:
        # Lazy: the providers package must not import ciao.web at module load.
        from ciao.web.chat_broker import event_to_json

        payload = event_to_json(event)
        if isinstance(payload, dict):
            self._journal.event(payload)

    def _rebuild(self, updates: Sequence[Mapping[str, Any]]) -> None:
        normalizer = UpdateNormalizer(self.spec, self.session_id)
        for update in updates:
            for event in normalizer.feed(_update_body(update)):
                self._journal_event(event)

    async def apply(self, *, mode: BridgeMode, model: str, thinking: str) -> list[StreamEvent]:
        """Hold the agent at the chat's mode, model and thinking level before a turn.

        A mode other than ``bypass`` is fail-closed: when the agent offers no value for it,
        or the switch fails, ``ModeNotApplied`` is raised and the turn must not run. Bypass
        is the one mode whose failure is only reported, since it is the looser setting.
        """
        strict = mode != "bypass"
        events: list[StreamEvent] = []
        for choice in mode_choices(self.spec, mode):
            picked = pick_value(choice, self._offered(choice.config_id))
            if picked is None:
                if strict:
                    raise ModeNotApplied(mode)
                events.append(SystemStatusEvent(
                    type="system",
                    status=f"{self.spec.id} does not offer a mode for '{mode}'.",
                ))
                continue
            events.extend(await self._set_option(choice.config_id, picked, strict=strict))
        if model:
            # The model is set exactly when the agent offers it. A bare tier name (opus) is the
            # one alias mapped: to the offered model that has that word. Anything else is not
            # guessed at and not sent, so the agent keeps its default; one status line per
            # session says so.
            offered = self._offered(self.spec.model_config_id)
            target = model if model in offered else resolve_tier(model, offered)
            if target is not None:
                events.extend(await self._set_option(self.spec.model_config_id, target))
            elif offered and not self._model_reported:
                self._model_reported = True
                events.append(SystemStatusEvent(
                    type="system",
                    status=f"{self.spec.id} does not offer the model '{model[:_STATUS_MODEL_CHARS]}'.",
                ))
        if thinking and self.spec.thought_config_id:
            offered = self._offered(self.spec.thought_config_id)
            if not offered or thinking in offered:
                events.extend(await self._set_option(self.spec.thought_config_id, thinking))
        return events

    async def _set_option(self, config_id: str, value: str, *, strict: bool = False) -> list[StreamEvent]:
        if self._normalizer.current_values.get(config_id) == value:
            return []
        failed = False
        try:
            await self._connection.conn.set_config_option(config_id, self.session_id, value)
        except RequestError as exc:
            # The adapter's error text can carry paths and settings, so only its type is logged.
            logger.warning("%s could not set %s (%s).", self.spec.id, config_id, type(exc).__name__)
            failed = True
        # Raised outside the handler: no adapter exception is chained to the turn's error.
        if failed:
            if strict:
                raise ModeNotApplied(config_id)
            return [SystemStatusEvent(
                type="system",
                status=f"{self.spec.id} could not set {config_id}.",
            )]
        self._normalizer.current_values[config_id] = value
        return []

    def _current_model(self) -> str:
        value = self._normalizer.current_values.get(self.spec.model_config_id, "")
        return value if isinstance(value, str) else str(value)

    def prompt(
        self,
        blocks: list[dict[str, Any]],
        *,
        display: str,
        unattended: bool,
    ) -> AsyncGenerator[StreamEvent, None]:
        return self._prompt(blocks, display=display, unattended=unattended)

    async def _prompt(
        self,
        blocks: list[dict[str, Any]],
        *,
        display: str,
        unattended: bool,
    ) -> AsyncGenerator[StreamEvent, None]:
        async with self._lock:
            self._prompt_active = True
            try:
                if self._late_open:
                    self._close_late()
                self._broker.unattended = unattended
                self._journal.user(display)
                self._text = TurnText()
                task = asyncio.create_task(self._run_prompt(blocks))
                try:
                    while True:
                        while True:
                            try:
                                yield self._queue.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                        if task.done():
                            await asyncio.sleep(0)
                            if self._queue.empty():
                                # The result yield suspends this generator. Clear
                                # the flag first, with no await after the empty
                                # check, so an update that arrives while the
                                # caller consumes the result opens a late turn
                                # instead of remaining unread on this queue.
                                self._prompt_active = False
                                self._flush_deferred_ends()
                                break
                            continue
                        incoming = asyncio.create_task(self._queue.get())
                        done, _pending = await asyncio.wait(
                            {incoming, task}, return_when=asyncio.FIRST_COMPLETED
                        )
                        if incoming in done:
                            yield incoming.result()
                        else:
                            await self._requeue(incoming)
                    result = self._turn_result(task)
                    self._journal_event(result)
                    yield result
                finally:
                    if not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
            finally:
                self._prompt_active = False
                self._flush_deferred_ends()

    async def _requeue(self, incoming: asyncio.Task[StreamEvent]) -> None:
        if not incoming.done():
            incoming.cancel()
        try:
            event = await incoming
        except asyncio.CancelledError:
            return
        self._queue.put_nowait(event)

    async def _run_prompt(self, blocks: list[dict[str, Any]]) -> Any:
        parsed = [_CONTENT_BLOCK.validate_python(block) for block in blocks]
        return await self._connection.conn.prompt(self.session_id, parsed)

    def _turn_result(self, task: asyncio.Task[Any]) -> StreamEvent:
        if task.cancelled():
            return self._unexpected_stop()
        exc = task.exception()
        if isinstance(exc, ConnectionError) or not self._connection.alive:
            return self._unexpected_stop()
        if exc is not None:
            raise exc
        response = task.result()
        stop = getattr(response, "stop_reason", None) or "end_turn"
        return result_event(
            str(stop),
            text=self._text,
            session_id=self.session_id,
            model=self._current_model(),
            usage=self._usage_for(response),
        )

    def _usage_for(self, response: Any) -> dict[str, str]:
        usage = getattr(response, "usage", None)
        if usage is None:
            return dict(self._normalizer.usage)
        dumped = usage.model_dump(by_alias=True, exclude_none=True) if hasattr(usage, "model_dump") else dict(usage)
        fresh = UpdateNormalizer(self.spec, self.session_id)
        fresh.record_prompt_usage(dumped if isinstance(dumped, Mapping) else {})
        return dict(fresh.usage)

    def _unexpected_stop(self) -> ResultEvent:
        # Raw adapter stderr is logged for diagnosis and never placed in the event.
        logger.warning(
            "%s stopped unexpectedly; stderr tail:\n%s",
            self.spec.id,
            redact_text(self._connection.stderr_tail(5)),
        )
        return ResultEvent(
            type="result",
            is_error=True,
            result=f"{self.spec.id} stopped unexpectedly.",
            session_id=self.session_id,
        )

    async def cancel(self) -> None:
        # The permission response is written by the connection task after the
        # handler returns. ACP requires that cancelled outcome to be on the
        # wire before session/cancel, so wait until those writes have flushed.
        answered = self._broker.cancel_all()
        if answered:
            await self._connection.wait_until_responses_flushed(answered)
        await self._connection.conn.cancel(self.session_id)

    def late_events(self) -> AsyncGenerator[StreamEvent, None]:
        return self._late_events()

    async def _late_events(self) -> AsyncGenerator[StreamEvent, None]:
        while True:
            yield await self._late_queue.get()

    @property
    def turn_running(self) -> bool:
        return self._prompt_active

    @property
    def late_turn_open(self) -> bool:
        return self._late_open

    @property
    def background_shells_running(self) -> bool:
        """True while a tracked background shell runs, or its end notice is still on its way."""
        return self._shells.tracking or bool(self._deferred_ends) or bool(self._end_tasks)

    async def close(self) -> None:
        """``session/close`` when the agent advertises it, then nothing else."""
        self._close_late()
        if self._shell_task is not None:
            self._shell_task.cancel()
            self._shell_task = None
        for task in list(self._end_tasks):
            task.cancel()
        self._deferred_ends = []
        if self._cap_object("sessionCapabilities", "close") and self.session_id:
            await self._connection.conn.close_session(session_id=self.session_id)

    async def discard(self) -> None:
        """Remove a throwaway session the agent keeps (a probe or a one-shot), best effort.

        ``session/delete`` when the agent advertises it, so the session does not stay in the
        user's own CLI history; otherwise ``session/close``. A failure is logged with its
        exception type only, and never raised: the caller's result does not depend on it.
        """
        self._close_late()
        if not self.session_id:
            return
        try:
            if self._cap_object("sessionCapabilities", "delete"):
                await self._connection.conn.delete_session(session_id=self.session_id)
            else:
                await self.close()
        except Exception as exc:  # noqa: BLE001 — the adapter's error text is never logged
            logger.warning("%s could not remove a throwaway session (%s).", self.spec.id, type(exc).__name__)
