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
    StreamEvent,
    SystemStatusEvent,
)
from ciao.providers.acp.connection import AcpConnection
from ciao.providers.acp.normalize import TurnText, UpdateNormalizer, canonical, result_event
from ciao.providers.acp.permissions import PermissionBroker, mode_choices, pick_value
from ciao.providers.acp.spec import AgentSpec
from ciao.redact import redact_text

logger = logging.getLogger(__name__)

_LATE_QUIET = frozenset({"usage_update", "config_option_update", "current_mode_update"})
_LATE_WAKE = "The agent continued after background work finished."
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
        """Enqueue the events of one extension notification. Synchronous, like :meth:`on_update`."""
        if self._replaying or not isinstance(payload, Mapping):
            return
        self._note_update(hook(self._normalizer, method, payload), kind="ext")

    def _note_update(self, events: list[StreamEvent], *, kind: str) -> None:
        if self._prompt_active:
            for event in events:
                self._queue.put_nowait(event)
                self._text.feed(event)
                self._journal_event(event)
            return
        self._touch_late(events, substantive=kind not in _LATE_QUIET)

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
            event.wake_notice = _LATE_WAKE
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

    async def close(self) -> None:
        """``session/close`` when the agent advertises it, then nothing else."""
        self._close_late()
        if self._cap_object("sessionCapabilities", "close") and self.session_id:
            await self._connection.conn.close_session(session_id=self.session_id)
