"""``AcpProvider``: the ``BaseProvider`` for any agent that speaks the Agent Client Protocol (#3137).

A subclass sets ``spec`` and nothing else. The provider owns one agent process
(``AcpConnection``) and at most one live session (``AcpSession``). Hosts reach
it only through the ``BaseProvider`` contract.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import math
import time
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, ClassVar

import acp
from acp.exceptions import RequestError
from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse

from ciao.models import (
    COMPACT_COMMAND,
    AgentRequest,
    ResultEvent,
    StreamEvent,
    linked_repo_of,
    provider_reuse_key,
)
from ciao.providers.acp.connection import AcpConnection, AgentStartError, AgentVersionError
from ciao.providers.acp.normalize import UpdateNormalizer
from ciao.providers.acp.permissions import PermissionBroker
from ciao.providers.acp.session import AcpSession, ModeNotApplied, SessionJournal, journal_file
from ciao.providers.acp.spec import AgentSpec
from ciao.redact import redact_text
from ciao.providers.base import (
    ActiveHandle,
    BaseProvider,
    ProviderCapabilities,
    QuestionResponseResult,
    build_prompt,
    native_slash_prompt,
    prepend_stable_context,
)

logger = logging.getLogger(__name__)

_CATALOG_TTL_S = 300.0
_CATALOG_PROBE_TIMEOUT_S = 25.0
_CATALOG_LOGOUT_WAIT_S = 32.0
# Successful probes only: a failed probe is retried on the next call.
_catalog_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_catalog_locks: dict[tuple[str, str], asyncio.Lock] = {}
_catalog_epochs: dict[str, int] = {}
_pending_catalog_logouts: dict[str, set[asyncio.Task[Any]]] = {}


def acp_catalog_epoch(agent_id: str) -> int:
    return _catalog_epochs.get(agent_id, 0)


def register_catalog_logout(agent_id: str, task: asyncio.Task[Any]) -> None:
    _pending_catalog_logouts.setdefault(agent_id, set()).add(task)


def unregister_catalog_logout(agent_id: str, task: asyncio.Task[Any]) -> None:
    workers = _pending_catalog_logouts.get(agent_id)
    if workers is None:
        return
    workers.discard(task)
    if not workers:
        _pending_catalog_logouts.pop(agent_id, None)


async def wait_for_catalog_logouts(agent_id: str) -> bool:
    """Wait for same-provider logout workers; a timeout must never expose cached rows."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _CATALOG_LOGOUT_WAIT_S
    while workers := tuple(_pending_catalog_logouts.get(agent_id, ())):
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        _done, pending = await asyncio.wait(workers, timeout=remaining)
        if pending:
            return False
    return True


def invalidate_acp_catalog(agent_id: str) -> None:
    """Drop the cached model catalog of one ACP agent, in every workspace."""
    _catalog_epochs[agent_id] = acp_catalog_epoch(agent_id) + 1
    for key in [key for key in _catalog_cache if key[0] == agent_id]:
        _catalog_cache.pop(key, None)


_DESCRIPTION_MAX = 80


def _short_description(description: object) -> str:
    """The adapter's note on one entry, untrusted: the part before " · ", capped.

    Claude Code's adapter writes a model's name there, sometimes followed by a
    price ("Sonnet 4.5 · $3/$15 per Mtok"); the PWA reads only the name.
    """
    if not isinstance(description, str):
        return ""
    return description.split(" · ", 1)[0].strip()[:_DESCRIPTION_MAX].strip()


def _option_items(options: list[dict[str, Any]], config_id: str) -> list[tuple[str, str, str]]:
    """``(value, label, description)`` for every value of one config option, grouped or flat, first wins."""
    items: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for option in options:
        if option.get("id") != config_id:
            continue
        for item in option.get("options") or []:
            if not isinstance(item, Mapping):
                continue
            entries = item["options"] if isinstance(item.get("options"), list) else [item]
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                value = entry.get("value")
                if not isinstance(value, str) or not value or value in seen:
                    continue
                seen.add(value)
                name = entry.get("name")
                description = entry.get("description")
                items.append((
                    value,
                    name if isinstance(name, str) and name else value,
                    _short_description(description),
                ))
        break
    return items


def _catalog_rows(spec: AgentSpec, options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per model: ``{"model", "label", "variants"}``; variants are the thinking levels.

    A row also carries the agent's ``description`` when it gives one. Claude
    Code's ``default`` entry describes the concrete model it runs ("Opus 4.5"),
    which is how the PWA names it instead of showing the word "Default".
    """
    variants = (
        [value for value, _label, _description in _option_items(options, spec.thought_config_id)]
        if spec.thought_config_id else []
    )
    rows: list[dict[str, Any]] = []
    for value, label, description in _option_items(options, spec.model_config_id):
        row: dict[str, Any] = {"model": value, "label": label, "variants": list(variants)}
        if description:
            row["description"] = description
        rows.append(row)
    return rows


@dataclass(slots=True)
class AcpHandle(ActiveHandle):
    session: AcpSession

    async def stop(self) -> None:
        await self.session.cancel()


# acp.Client is a Protocol whose optional methods have no body; the SDK runs without them.
class AgentClient(acp.Client):
    """The SDK client: routes agent calls to the live session and broker."""

    def __init__(self, ext_hook: ExtHook | None = None) -> None:
        self.connection: AcpConnection | None = None
        self.session: AcpSession | None = None
        self.broker: PermissionBroker | None = None
        self.ext_hook = ext_hook

    def _target_session(self) -> AcpSession | None:
        session = self.session
        if session is None and self.connection is not None:
            # ``session/load`` replays updates before ``AcpSession.open`` returns.
            pending = getattr(self.connection, "_pending_session", None)
            if isinstance(pending, AcpSession):
                session = pending
        return session

    async def request_permission(
        self, session_id: str, tool_call: Any, options: Any, **kwargs: Any
    ) -> RequestPermissionResponse:
        broker = self.broker
        if broker is None:
            return RequestPermissionResponse(outcome=DeniedOutcome())
        call = tool_call.model_dump(by_alias=True, exclude_none=True)
        stored = [option.model_dump(by_alias=True, exclude_none=True) for option in options]
        outcome = await broker.request(session_id, call, stored)
        if outcome.get("outcome") == "selected":
            # The broker selects only options with a usable optionId, so this is never None.
            return RequestPermissionResponse(outcome=AllowedOutcome(option_id=outcome["optionId"]))
        return RequestPermissionResponse(outcome=DeniedOutcome())

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        # Synchronous up to ``on_update``: no await before the enqueue.
        payload = update.model_dump(by_alias=True, exclude_none=True)
        payload["sessionId"] = session_id
        session = self._target_session()
        if session is not None:
            session.on_update(payload)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        # Synchronous up to ``on_ext``: no await before the enqueue, as in session_update.
        hook = self.ext_hook
        session = self._target_session()
        if hook is not None and session is not None:
            session.on_ext(method, params, hook)

    async def read_text_file(
        self, session_id: str, path: str, line: int | None = None,
        limit: int | None = None, **kwargs: Any,
    ) -> Any:
        raise RequestError.method_not_found("fs/read_text_file")

    async def write_text_file(
        self, session_id: str, path: str, content: str, **kwargs: Any
    ) -> Any:
        raise RequestError.method_not_found("fs/write_text_file")


def _runtime_dir(config: object | None) -> Path:
    state_path = getattr(config, "state_path", None)
    if state_path is None:
        raise RuntimeError("AcpProvider needs config.state_path to place its session journals.")
    return Path(state_path).parent


ExtHook = Callable[[UpdateNormalizer, str, Mapping[str, Any]], list[StreamEvent]]

_CAPSULE_URI = "ciaobot://context"


def _capsule_block(capsule: str, *, embedded: bool) -> dict[str, Any]:
    """The context capsule as an embedded resource when the agent takes them, else plain text."""
    if embedded:
        return {
            "type": "resource",
            "resource": {"uri": _CAPSULE_URI, "mimeType": "text/plain", "text": capsule},
        }
    return {"type": "text", "text": capsule}


class AcpProvider(BaseProvider):
    spec: ClassVar[AgentSpec]  # set by each subclass
    capabilities = ProviderCapabilities(
        resume=True,
        images=True,
        stop=True,
        permissions=True,
        dynamic_models=True,
        thinking_levels=True,
        usage=True,
        background_subagents=True,
        session_history=True,
        schedule_unattended=True,
    )

    def __init__(self, workspace_root: Path, *, config: object | None = None) -> None:
        super().__init__(workspace_root, config=config)
        self._client = AgentClient(ext_hook=self.ext_update)  # type: ignore[abstract]
        self._connection: AcpConnection | None = None
        self._session: AcpSession | None = None
        self._broker: PermissionBroker | None = None
        self._key: tuple[str, str, str] | None = None
        self._last_activity: float | None = None

    # -- extension hooks (neutral, empty by default) ---------------------------

    def session_meta(self, request: AgentRequest) -> dict[str, Any]:
        """``_meta`` handed to ``session/new``, ``session/load`` and ``session/resume``."""
        return {}

    @classmethod
    def oneshot_meta(cls) -> dict[str, Any]:
        """``_meta`` for a one-shot call of this agent."""
        return {}

    def ext_update(
        self, normalizer: UpdateNormalizer, method: str, payload: Mapping[str, Any]
    ) -> list[StreamEvent]:
        """Events for one extension notification the agent sends (``_<method>`` on the wire).

        ``normalizer`` is the live session's, so tool facts carry over from ``session/update``.
        The default ignores every notification.
        """
        return []

    @classmethod
    def spawn_env(cls) -> dict[str, str]:
        """Env added to the agent process at spawn, under the request's own env."""
        return {}

    @classmethod
    def process_env(cls, request: AgentRequest) -> dict[str, str]:
        """The env the agent process is spawned with for this request's connection."""
        return {**cls.spawn_env(), **request.extra_env}

    # -- turn ------------------------------------------------------------------

    async def run_streaming(
        self,
        request: AgentRequest,
        register_handle: Callable[[ActiveHandle | None], None],
    ) -> AsyncGenerator[StreamEvent, None]:
        key = (provider_reuse_key(request), linked_repo_of(request), str(self.workspace_root))
        try:
            if self._connection is not None and (self._key != key or not self._connection.alive):
                await self.disconnect()
            if self._connection is None:
                try:
                    await self._start(request, key)
                except AgentVersionError as exc:
                    # The gate's refusal is a fixed, user-facing sentence by design.
                    yield ResultEvent(type="result", is_error=True, result=str(exc))
                    return
                except AgentStartError as exc:
                    # The adapter's error text and stderr stay in the log (stderr keyword-redacted);
                    # the PWA gets a fixed sentence built from the agent id only.
                    logger.warning(
                        "%s agent did not start: %s\n%s",
                        self.spec.id, redact_text(str(exc)), redact_text(exc.stderr_tail),
                    )
                    yield ResultEvent(
                        type="result", is_error=True, result=f"{self.spec.id} agent did not start."
                    )
                    return
            connection = self._connection
            assert connection is not None
            session, replaced = await self._session_for(connection, request)
            if replaced:
                prepend_stable_context(request)
            try:
                applied = await session.apply(
                    mode=request.mode, model=request.model, thinking=request.thinking_level
                )
            except ModeNotApplied:
                # The chat asked for a mode the agent would not hold, so nothing runs. The session
                # id still goes out: the retry resumes this session rather than opening another.
                logger.warning("%s did not start the turn: the requested mode was not applied.", self.spec.id)
                yield ResultEvent(
                    type="result",
                    is_error=True,
                    result=f"{self.spec.id} could not set the requested mode; the turn did not run.",
                    session_id=session.session_id,
                )
                return
            for event in applied:
                yield self._seen(event)
            blocks = self._blocks(connection, request)
            register_handle(AcpHandle(session))
            try:
                async with aclosing(session.prompt(
                    blocks,
                    display=request.display_prompt or request.prompt,
                    unattended=request.unattended,
                )) as stream:
                    async for event in stream:
                        yield self._seen(event)
            finally:
                register_handle(None)
        except Exception:
            # Any failure the adapter did not classify (open, config, image read, prompt)
            # is logged with its detail; the PWA gets a fixed sentence, never exception text.
            logger.exception("%s turn failed", self.spec.id)
            yield ResultEvent(
                type="result", is_error=True, result=f"{self.spec.id} could not complete the turn."
            )

    def _seen(self, event: StreamEvent) -> StreamEvent:
        self._last_activity = time.monotonic()
        return event

    async def _start(self, request: AgentRequest, key: tuple[str, str, str]) -> None:
        self._client.connection = None
        self._connection = await AcpConnection.open(
            self.spec,
            cwd=self.workspace_root,
            env=type(self).process_env(request),
            client=self._client,
        )
        self._client.connection = self._connection
        self._key = key

    async def _session_for(
        self, connection: AcpConnection, request: AgentRequest
    ) -> tuple[AcpSession, bool]:
        resume = request.resume_session
        meta = self.session_meta(request)
        session = self._session
        if session is not None and resume and session.session_id == resume:
            # A changed ``_meta`` takes effect by reloading the same session, but
            # never under an open late turn: that waits for the next turn.
            if session.meta_key == json.dumps(meta, sort_keys=True):
                return session, False
            if session.late_turn_open:
                return session, False
            logger.info("Reloading %s session %s: options changed", self.spec.id, resume)
        await self._close_session()
        broker = PermissionBroker(lambda _event: None)
        self._client.broker = broker
        linked = linked_repo_of(request)
        try:
            opened, replaced = await AcpSession.open(
                connection,
                self.spec,
                cwd=self.workspace_root,
                resume=resume,
                additional_dirs=[linked] if linked else [],
                journal_dir=_runtime_dir(self.config),
                broker=broker,
                session_meta=meta,
            )
        except BaseException:
            # The old session is already closed; unwire the broker so the client does
            # not route updates or permission requests to a session that never opened.
            broker.cancel_all()
            self._client.broker = None
            raise
        opened.continuation_hook = self._continuation_notice
        self._session = opened
        self._broker = broker
        self._client.session = opened
        return opened, replaced

    def agent_accepts_images(self) -> bool | None:
        # The agent-level promptCapabilities from its initialize response: it covers the whole
        # session, not a model. Nothing connected, or no boolean stated, is None.
        connection = self._connection
        if connection is None:
            return None
        prompt_caps = connection.agent_capabilities.get("promptCapabilities")
        if not isinstance(prompt_caps, Mapping):
            return None
        value = prompt_caps.get("image")
        return value if isinstance(value, bool) else None

    def _blocks(self, connection: AcpConnection, request: AgentRequest) -> list[dict[str, Any]]:
        prompt_caps = connection.agent_capabilities.get("promptCapabilities")
        slash = native_slash_prompt(request) if self.spec.slash_capsule_as_block and not request.compact else None
        if slash is not None:
            command, capsule = slash
            blocks: list[dict[str, Any]] = [
                {"type": "text", "text": build_prompt(replace(request, prompt=command))},
                _capsule_block(capsule, embedded=isinstance(prompt_caps, dict) and prompt_caps.get("embeddedContext") is True),
            ]
        else:
            text = COMPACT_COMMAND if request.compact else build_prompt(request)
            blocks = [{"type": "text", "text": text}]
        accepts_images = isinstance(prompt_caps, dict) and prompt_caps.get("image") is True
        if request.images and accepts_images and not request.compact:
            for image in request.images:
                blocks.append({
                    "type": "image",
                    "mimeType": image.mime_type,
                    "data": base64.b64encode(image.path.read_bytes()).decode("ascii"),
                })
        return blocks

    # -- host seams --------------------------------------------------------------

    @property
    def current_session_id(self) -> str | None:
        session = self._session
        return session.session_id if session is not None else None

    @property
    def response_session_id(self) -> str:
        return self.current_session_id or ""

    @property
    def is_connected(self) -> bool:
        connection = self._connection
        return connection is not None and connection.alive

    @property
    def cli_connected(self) -> bool:
        connection = self._connection
        return connection is None or connection.alive

    @property
    def can_drain(self) -> bool:
        return self._session is not None and self.is_connected

    # The host's hook for a continuation's notice (set through ProviderService); see AcpSession.
    continuation_hook: Callable[[list[Any]], str | None] | None = None

    def _continuation_notice(self, ended: list[Any]) -> str | None:
        hook = self.continuation_hook
        return hook(ended) if hook is not None else None

    async def abort_session(self, session_id: str) -> ResultEvent | None:
        """Cancel the live session's turn, late turns included (Stop on a background chat)."""
        session = self._session
        if session is None or session.session_id != session_id:
            return None
        return await session.cancel()

    @property
    def background_work_outstanding(self) -> bool:
        session = self._session
        return session is not None and (session.late_turn_open or session.background_shells_running)

    @property
    def seconds_since_activity(self) -> float:
        if self._last_activity is None:
            return math.inf
        return time.monotonic() - self._last_activity

    async def drain_events(self) -> AsyncGenerator[StreamEvent, None]:
        session = self._session
        if session is None:
            return
        async with aclosing(session.late_events()) as events:
            async for event in events:
                yield self._seen(event)

    def has_permission_request(self, request_id: str, session_id: str = "") -> bool:
        broker = self._broker
        return broker is not None and broker.has(request_id, session_id)

    def permission_request_session(self, request_id: str) -> str:
        broker = self._broker
        return broker.session_of(request_id) if broker is not None else ""

    async def respond_permission(
        self, request_id: str, approved: bool, reason: str = "", *,
        session_id: str = "",
    ) -> QuestionResponseResult:
        broker = self._broker
        if broker is None:
            return QuestionResponseResult(ok=True, already_resolved=True)
        return broker.respond(request_id, approved, session_id=session_id)

    async def _close_session(self) -> None:
        # Slots are cleared before any await: a concurrent caller sees no session.
        session, self._session = self._session, None
        broker, self._broker = self._broker, None
        self._client.session = None
        self._client.broker = None
        if broker is not None:
            broker.cancel_all()
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()

    async def disconnect(self) -> None:
        connection, self._connection = self._connection, None
        self._key = None
        self._client.connection = None
        try:
            await self._close_session()
        finally:
            if connection is not None:
                await connection.close()

    # -- persisted sessions ----------------------------------------------------

    @classmethod
    async def delete_session(
        cls, workspace_root: Path, session_id: str, *, runtime_root: Path
    ) -> bool | None:
        """Remove the session journal only; the agent is not spawned here.

        An adapter whose agent keeps its own copy of the session overrides this to ask the
        agent too (OpenCode does, and spawns the agent only when a journal exists to remove).
        """
        path = journal_file(runtime_root, cls.spec.id, session_id)
        try:
            path.unlink()
        except FileNotFoundError:
            return None
        return True

    @classmethod
    async def read_thread(
        cls, workspace_root: Path, session_id: str, *, runtime_root: Path
    ) -> dict[str, Any]:
        # Returned as {"acp_journal": rows}; the transcript renders it through
        # ``transcript_service._render_acp_journal``.
        path = journal_file(runtime_root, cls.spec.id, session_id)
        if not path.exists():
            return {}
        return {"acp_journal": SessionJournal.read(path)}

    # -- model catalog ---------------------------------------------------------------

    @classmethod
    def invalidate_model_catalog(cls) -> None:
        invalidate_acp_catalog(cls.spec.id)

    @classmethod
    def model_catalog_epoch(cls) -> int:
        return acp_catalog_epoch(cls.spec.id)

    @classmethod
    def register_model_catalog_mutation(cls, task: asyncio.Task[Any]) -> None:
        register_catalog_logout(cls.spec.id, task)

    @classmethod
    def unregister_model_catalog_mutation(cls, task: asyncio.Task[Any]) -> None:
        unregister_catalog_logout(cls.spec.id, task)

    @classmethod
    async def wait_for_model_catalog_mutations(cls) -> bool:
        return await wait_for_catalog_logouts(cls.spec.id)

    @classmethod
    async def model_catalog(
        cls, workspace_root: Path, *, force: bool = False
    ) -> list[dict[str, Any]]:
        """Models the agent offers in ``workspace_root``; ``[]`` when the probe fails.

        Runs on the catalog route only, never on status polls. Successful results are
        cached per (agent, workspace) for ``_CATALOG_TTL_S``; ``force`` bypasses the cache.
        """
        key = (cls.spec.id, str(workspace_root))
        if not await wait_for_catalog_logouts(cls.spec.id):
            logger.warning("%s model catalog skipped while logout is still running.", cls.spec.id)
            return []
        lock = _catalog_locks.setdefault(key, asyncio.Lock())
        async with lock:
            epoch = acp_catalog_epoch(cls.spec.id)
            cached = _catalog_cache.get(key)
            if not force and cached is not None and time.monotonic() - cached[0] < _CATALOG_TTL_S:
                return [dict(row) for row in cached[1]]
            try:
                rows = await asyncio.wait_for(
                    cls._probe_catalog(Path(workspace_root)), timeout=_CATALOG_PROBE_TIMEOUT_S
                )
            except TimeoutError:
                # One adapter that answers initialize and then stalls must not hold /api/models.
                logger.warning(
                    "%s model catalog probe timed out after %gs.", cls.spec.id, _CATALOG_PROBE_TIMEOUT_S
                )
                return []
            except Exception as exc:  # noqa: BLE001 — a failed probe is an empty catalog
                # The adapter's error text stays out of the log, as in the turn path.
                logger.warning("%s model catalog probe failed (%s).", cls.spec.id, type(exc).__name__)
                return []
            if epoch != acp_catalog_epoch(cls.spec.id):
                return []
            _catalog_cache[key] = (time.monotonic(), rows)
            return [dict(row) for row in rows]

    @classmethod
    async def _probe_catalog(cls, workspace_root: Path) -> list[dict[str, Any]]:
        connection = await AcpConnection.open(
            cls.spec, cwd=workspace_root, env=cls.spawn_env(), client=AgentClient()  # type: ignore[abstract]
        )
        try:
            # No prompt is sent, so the session's journal file is never written; the
            # journal directory only has to be a path.
            session, _replaced = await AcpSession.open(
                connection,
                cls.spec,
                cwd=workspace_root,
                resume=None,
                additional_dirs=[],
                journal_dir=workspace_root,
                broker=PermissionBroker(lambda _event: None),
                session_meta={},
            )
            try:
                return _catalog_rows(cls.spec, session.config_options)
            finally:
                # The probe session is removed from the user's CLI history; a failure
                # to remove it must not discard the catalog already read.
                await session.discard()
        finally:
            await connection.close()
