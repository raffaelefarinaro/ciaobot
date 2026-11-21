"""Real ACP agent specs and the provider classes that run them (#3138, #3139).

Every agent Ciaobot runs over ACP has one ``AgentSpec`` here and one ``AcpProvider`` subclass that
sets it. The ``claude`` provider runs ``claude-agent-acp``. Ciaobot's disable options (the trim
layer, the workspace's extra denies and the claude.ai connectors switch) reach the adapter as
``_meta.claudeCode.options``, which the adapter applies to the CLI. Everything else
the CLI owns, so nothing else is sent.

Login stays in the CLI: ``codex_auth_command`` only builds the command the Settings terminal
runs, and Ciaobot never reads or stores the credentials it writes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import aclosing
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from acp.exceptions import RequestError

from ciao import claude_sessions
from ciao.claude_trim import claude_settings_layer
from ciao.models import AgentRequest, AssistantTextDelta, StreamEvent, ThinkingEvent, ToolUseEvent
from ciao.providers.acp.connection import AcpConnection, AgentStartError, AgentVersionError
from ciao.providers.acp.normalize import UpdateNormalizer, bounded_text, child_tool_id, loggable_kind
from ciao.providers.acp.provider import AcpProvider, AgentClient
from ciao.providers.acp.spec import AgentSpec, ModeChoice, VersionGate
from ciao.providers.base import ActiveHandle, CleanupTimedOut
from ciao.opencode_cli import (
    OPENCODE_V2_REQUIRED,
    cached_opencode_version,
    opencode_start_supported,
)
from ciao.tool_path import resolve_tool

logger = logging.getLogger(__name__)

CLAUDE = AgentSpec(
    id="claude",
    binary="claude-agent-acp",
    # CLAUDE_CODE_DISABLE_CRON removes the cron tools and stops durable jobs in a project's
    # .claude/scheduled_tasks.json from firing; the loop family is denied in claude_trim.py.
    env={
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_ARTIFACT": "1",
        "CLAUDE_CODE_DISABLE_CRON": "1",
    },
    # The claude.ai connectors switch travels only in _meta.claudeCode.options.env (see below).
    unset_env=("ENABLE_CLAUDEAI_MCP_SERVERS",),
    # Background shells: the adapter then sends `terminal_exit` when a background Bash ends.
    client_meta={"terminal_output_delta": True},
    continues_after_shell=True,
    modes={
        "plan": (ModeChoice("mode", ("plan",)),),
        "normal": (ModeChoice("mode", ("default",)),),
        "auto": (ModeChoice("mode", ("auto", "acceptEdits")),),
        "bypass": (ModeChoice("mode", ("bypassPermissions",)),),
    },
    model_config_id="model",
    thought_config_id="effort",
    slash_capsule_as_block=True,
)

# A one-shot call has no tools, no skills, no setting sources and no MCP servers.
CLAUDE_ONESHOT_OPTIONS: Mapping[str, Any] = MappingProxyType({
    "tools": [],
    "skills": [],
    "settingSources": [],
    "strictMcpConfig": True,
})

_CONNECTORS_ENV_KEY = "ENABLE_CLAUDEAI_MCP_SERVERS"
_CONNECTORS_OFF_ENV = {_CONNECTORS_ENV_KEY: "false"}


def claude_session_options(request: AgentRequest) -> dict[str, Any]:
    """The ``_meta.claudeCode.options`` dict for a chat: its disable options only.

    - ``disallowedTools``: the request's denies (trim tool and skill denies, then the
      workspace's extra denies, in that order).
    - ``settings``: the trim layer as an object. A string would be read by the adapter as a
      file path relative to the working directory.
    - ``env``: ``ENABLE_CLAUDEAI_MCP_SERVERS=false`` only when the chat's connectors are off.

    An option with no value is left out, so an unchanged chat sends the same dict every turn.
    """
    options: dict[str, Any] = {}
    if request.disallowed_tools:
        options["disallowedTools"] = list(request.disallowed_tools)
    settings = (
        request.claude_settings
        if request.claude_settings is not None
        else claude_settings_layer({})
    )
    if settings:
        options["settings"] = dict(settings)
    if request.extra_env.get("ENABLE_CLAUDEAI_MCP_SERVERS") == "false":
        options["env"] = dict(_CONNECTORS_OFF_ENV)
    return options


def _claude_cli_env() -> dict[str, str]:
    """Point the adapter at the user's own ``claude``, and its login, when one resolves."""
    from ciao.adapter_install import absolute_executable
    from ciao.setup_status import claude_cli_path

    path = absolute_executable(claude_cli_path() or None)
    return {"CLAUDE_CODE_EXECUTABLE": path} if path else {}


class ClaudeAcpProvider(AcpProvider):
    spec = CLAUDE

    def session_meta(self, request: AgentRequest) -> dict[str, Any]:
        """Ciaobot's disable options for this chat, as ``_meta`` on session/new, load and resume."""
        return {"claudeCode": {"options": claude_session_options(request)}}

    @classmethod
    def oneshot_meta(cls) -> dict[str, Any]:
        return {"claudeCode": {"options": dict(CLAUDE_ONESHOT_OPTIONS)}}

    @classmethod
    def spawn_env(cls) -> dict[str, str]:
        """The user's own ``claude`` as ``CLAUDE_CODE_EXECUTABLE``, when one resolves.

        Credentials are not added here: the engine environment already reaches the adapter, and
        the CLI handles its own login.
        """
        return _claude_cli_env()

    @classmethod
    def process_env(cls, request: AgentRequest) -> dict[str, str]:
        # The connectors switch travels only in _meta.claudeCode.options.env. A process
        # env value would outlive a reload, since the connection is reused across it.
        env = super().process_env(request)
        env.pop(_CONNECTORS_ENV_KEY, None)
        return env

    @classmethod
    async def delete_session(
        cls, workspace_root: Path, session_id: str, *, runtime_root: Path
    ) -> bool | None:
        # The ACP journal and the CLI's own session file both go. Either one deleting counts.
        journal = await super().delete_session(workspace_root, session_id, runtime_root=runtime_root)
        try:
            # A git subprocess and file removal: run in a worker thread, not on the event loop.
            cli_file = await asyncio.to_thread(
                claude_sessions.delete_session_file, session_id, str(workspace_root)
            )
        except OSError:
            # Neither the id nor the OSError text is logged: the id comes from the adapter, and
            # the error text carries the path.
            logger.warning("Could not delete a Claude session file; it was left in place.")
            cli_file = False
        return True if (journal or cli_file) else None


def claude_auth_command(*, device_auth: bool = False) -> list[str]:
    """The interactive Claude Code login, as argv. ``device_auth`` has no Claude equivalent."""
    del device_auth
    from ciao.setup_status import claude_cli_path

    binary = claude_cli_path()
    if not binary:
        raise FileNotFoundError("Claude CLI not found")
    return [binary, "auth", "login"]


CODEX = AgentSpec(
    id="codex",
    binary="codex-acp",
    env={},  # CODEX_PATH is the user's own codex, added by CodexAcpProvider.spawn_env.
    client_meta={},  # No subagents opt-in: a Codex subagent is a plain tool call (plan Q6).
    # codex-acp reports its sign-in state as an unprefixed ``auth/status_update`` (see CodexAcpProvider.ext_update).
    extension_notifications=("auth/status_update",),
    modes={
        "plan": (
            ModeChoice("mode", ("read-only",)),
            ModeChoice("collaboration_mode", ("plan",)),
        ),
        "normal": (
            ModeChoice("mode", ("workspace-write",)),
            ModeChoice("collaboration_mode", ("default",)),
        ),
        "auto": (
            ModeChoice("mode", ("agent",)),
            ModeChoice("collaboration_mode", ("default",)),
        ),
        "bypass": (
            ModeChoice("mode", ("agent-full-access",)),
            ModeChoice("collaboration_mode", ("default",)),
        ),
    },
    model_config_id="model",
    thought_config_id="reasoning_effort",
)


class CodexAcpProvider(AcpProvider):
    spec = CODEX

    def ext_update(
        self, normalizer: UpdateNormalizer, method: str, payload: Mapping[str, Any]
    ) -> list[StreamEvent]:
        """``auth/status_update`` is debug-logged only: the payload's state, never its account labels.

        ``payload`` is always a mapping (the connection passes ``{}`` for a non-object), so each
        field is checked before use and anything unexpected is ignored. No events are produced.
        """
        if method == "auth/status_update":
            state = payload.get("state")
            accounts = payload.get("accounts")
            logger.debug(
                "codex auth status: state=%s accounts=%d",
                state if isinstance(state, str) else type(state).__name__,
                len(accounts) if isinstance(accounts, list) else 0,
            )
        return []

    @classmethod
    def spawn_env(cls) -> dict[str, str]:
        """The user's own ``codex`` as ``CODEX_PATH``, when one resolves.

        Without it codex-acp would run the codex it bundles, which Ciaobot does not install. A
        relative or non-executable result is left unset, so the variable never names a path the
        adapter would resolve against its own working directory.
        """
        from ciao.adapter_install import absolute_executable

        path = absolute_executable(codex_binary())
        return {"CODEX_PATH": path} if path else {}


_OPENCODE_CHILD_UPDATE = "opencode/session/child_update"

OPENCODE = AgentSpec(
    id="opencode",
    binary="opencode",
    args=("acp",),
    # Child sessions arrive as ``opencode/session/child_update`` instead of folded into the parent.
    client_meta={"opencode/child-session-updates": True},
    continues_after_shell=True,
    extension_notifications=(_OPENCODE_CHILD_UPDATE,),
    # opencode has no bypass mode: its permission cards follow the user's opencode config (plan Q7).
    modes={
        "plan": (ModeChoice("mode", ("plan",)),),
        "normal": (ModeChoice("mode", ("build",)),),
        "auto": (ModeChoice("mode", ("build",)),),
        "bypass": (ModeChoice("mode", ("build",)),),
    },
    model_config_id="model",
    thought_config_id="effort",
    # The floor is the one in ciao.opencode_cli (shared with the Settings status row).
    version_gate=VersionGate(
        supported=opencode_start_supported,
        refusal=OPENCODE_V2_REQUIRED,
        probe=cached_opencode_version,
    ),
)

# Terminal child statuses, and whether each one failed.
_OPENCODE_CHILD_END = {"completed": False, "failed": True, "interrupted": True}
# The only child updates that reach the parent transcript. Everything else would rewrite the
# chat's own state: usage_update the context and cost gauges, config_option_update the tracked
# model, current_mode_update the mode, compaction_update the parent's compaction status.
_CHILD_UPDATE_KINDS = frozenset({"agent_message_chunk", "agent_thought_chunk", "tool_call", "tool_call_update"})
# A child's title is shown on its node.
_CHILD_TITLE_CHARS = 200
# Live children (opened, no terminal status yet) per turn. A child leaves the set when its
# terminal status arrives and when the turn ends, so the cap bounds only children running at once.
_CHILD_LIMIT = 256
# The ceiling on one session's store cleanup (open, session/delete, close). A healthy agent
# answers in well under a second; past this the chat's cleanup gives up rather than hold the caller.
_DELETE_TIMEOUT_S = 10.0
# ACP's "resource not found" (JSON-RPC -32002): the session is not in the agent's store.
_RESOURCE_NOT_FOUND = -32002


def _safe_title(raw: object) -> str:
    return bounded_text(raw, _CHILD_TITLE_CHARS)


def _update_kind(body: Mapping[str, Any]) -> object:
    kind = body.get("sessionUpdate")
    return kind if kind is not None else body.get("session_update")


class OpencodeAcpProvider(AcpProvider):
    """``opencode acp``: a subagent's child session nests under the subagent's tool call.

    The notification is ``opencode/session/child_update``, sent with no leading underscore, from
    ``packages/cli/src/acp/child.ts`` in opencode v2.3.2 (routed by ``connection.connect_client``).
    Its payload is ``{rootSessionId, childSessionId, parentSessionId, depth, title?, type}``, where:

    - ``type: "update"`` carries ``update``, a session/update body. Only its message and thought
      chunks and its tool calls are kept (see ``_CHILD_UPDATE_KINDS``); its tool ids are prefixed
      ``<childSessionId>:<id>``.
    - ``type: "status"`` carries ``status`` (``created``, ``running``, ``completed``, ``failed``,
      ``interrupted``) and an optional ``error``.

    A child that is linked to a subagent call (see ``UpdateNormalizer.link_child``) nests under
    that call's tool_use_id, and its terminal status closes that call with a ``tool_result``
    carrying ``subagent_status``. A child with no subagent call behind it keeps its own
    ``child:<childSessionId>`` node (tool name ``Subagent``), and its terminal status closes that
    node. A child whose parent is another child nests under that child's anchor.
    """

    spec = OPENCODE

    def __init__(self, workspace_root: Path, *, config: object | None = None) -> None:
        super().__init__(workspace_root, config=config)
        self._children: set[str] = set()
        self._children_root: object = None
        self._children_limit_logged = False
        self._dropped_kinds: set[str] = set()

    async def run_streaming(
        self,
        request: AgentRequest,
        register_handle: Callable[[ActiveHandle | None], None],
    ) -> AsyncGenerator[StreamEvent, None]:
        try:
            async with aclosing(super().run_streaming(request, register_handle)) as events:
                async for event in events:
                    yield event
        finally:
            # A child still running when the turn ends is not counted against the next turn.
            self._forget_children()

    def _forget_children(self) -> None:
        self._children.clear()
        self._children_limit_logged = False

    def _log_children_limit(self) -> None:
        # Once per turn; the line names no child id and no adapter text.
        if not self._children_limit_logged:
            self._children_limit_logged = True
            logger.debug("OpenCode child sessions over %d live at once are not shown", _CHILD_LIMIT)

    @classmethod
    async def delete_session(
        cls, workspace_root: Path, session_id: str, *, runtime_root: Path
    ) -> bool | None:
        """Remove the chat's session from opencode's own store, then its journal.

        opencode keeps each session in its store under the agent root, so the agent is asked to
        delete it when it advertises ``session/delete``. The agent is spawned only when this
        chat's journal exists: no journal means the chat never reached a prompt, or the session
        is already gone. The whole store cleanup is bounded by ``_DELETE_TIMEOUT_S``; past it,
        :class:`CleanupTimedOut` tells the caller to skip the chat's remaining session ids.
        Start and delete failures are logged by type only, and the journal result stands.
        """
        journal = await super().delete_session(workspace_root, session_id, runtime_root=runtime_root)
        if not journal:
            return None
        if not session_id:
            return True
        try:
            await asyncio.wait_for(
                cls._delete_from_store(workspace_root, session_id), _DELETE_TIMEOUT_S
            )
        except TimeoutError as exc:
            logger.warning(
                "opencode did not finish deleting a session in time (%s); its store kept it.",
                type(exc).__name__,
            )
            raise CleanupTimedOut(type(exc).__name__) from None
        return True

    @classmethod
    async def _delete_from_store(cls, workspace_root: Path, session_id: str) -> None:
        try:
            connection = await AcpConnection.open(
                cls.spec, cwd=workspace_root, env=cls.spawn_env(), client=AgentClient()  # type: ignore[abstract]
            )
        except AgentVersionError:
            return  # the version gate has already logged its one refusal
        except AgentStartError:
            logger.warning("opencode could not start to delete a session; its store kept it.")
            return
        try:
            caps = connection.agent_capabilities.get("sessionCapabilities")
            if isinstance(caps, Mapping) and caps.get("delete") is not None:
                await connection.conn.delete_session(session_id=session_id)
        except RequestError as exc:
            if exc.code == _RESOURCE_NOT_FOUND:
                logger.debug("opencode's store has no such session; nothing to delete.")
            else:
                logger.warning("opencode could not delete a session (%s).", type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 — the adapter's error text is never logged
            logger.warning("opencode could not delete a session (%s).", type(exc).__name__)
        finally:
            await connection.close()

    def ext_update(
        self, normalizer: UpdateNormalizer, method: str, payload: Mapping[str, Any]
    ) -> list[StreamEvent]:
        if method != _OPENCODE_CHILD_UPDATE:
            return []
        root = payload.get("rootSessionId")
        child = payload.get("childSessionId")
        tool_id = child_tool_id(child)
        # A child of another session is not this chat's: its events are dropped.
        if root != normalizer.session_id or tool_id is None:
            return []
        # Validated by child_tool_id, so the id is a plain string here.
        child_id = tool_id.removeprefix("child:")
        if root != self._children_root:
            self._children_root = root
            self._forget_children()
        title = _safe_title(payload.get("title"))
        normalizer.link_child(child_id, title)
        # The subagent call this child runs under, or None for a child nobody launched.
        call_id = normalizer.linked_call(child_id)
        parent_session = payload.get("parentSessionId")
        parent = None if parent_session == root else normalizer.session_anchor(parent_session)
        events: list[StreamEvent] = []
        if child_id not in self._children:
            # The first event for a child opens its node, whether a status or an update came first.
            if len(self._children) >= _CHILD_LIMIT:
                self._log_children_limit()
                return []
            self._children.add(child_id)
            if call_id is None:
                events.append(ToolUseEvent(
                    type="tool_use",
                    tool_name="Subagent",
                    tool_input=title,
                    tool_use_id=tool_id,
                    parent_tool_use_id=parent,
                ))
        # Everything the child says nests under its subagent call, or under its own node.
        nest = call_id or tool_id
        kind = payload.get("type")
        if kind == "update":
            body = payload.get("update")
            if isinstance(body, Mapping):
                body_kind = _update_kind(body)
                if body_kind not in _CHILD_UPDATE_KINDS:
                    self._drop_kind(body_kind)
                    return events
                for event in normalizer.feed(dict(body)):
                    if isinstance(event, (AssistantTextDelta, ThinkingEvent, ToolUseEvent)):
                        event = replace(event, parent_tool_use_id=nest)
                    events.append(event)
        elif kind == "status":
            status = payload.get("status")
            if isinstance(status, str) and status in _OPENCODE_CHILD_END:
                # The child is over: its slot is free for the next one.
                self._children.discard(child_id)
                is_error = _OPENCODE_CHILD_END[status]
                if call_id is not None:
                    # The subagent call's own result, closing the launch acknowledgement.
                    events.append(ToolUseEvent(
                        type="tool_result",
                        tool_use_id=call_id,
                        is_error=is_error,
                        subagent_status=status,
                    ))
                else:
                    events.append(ToolUseEvent(
                        type="tool_result",
                        tool_use_id=tool_id,
                        parent_tool_use_id=parent,
                        is_error=is_error,
                    ))
        return events

    def _drop_kind(self, kind: object) -> None:
        label = loggable_kind(kind)
        if label not in self._dropped_kinds:
            self._dropped_kinds.add(label)
            logger.debug("dropped child session update kind %s", label)


def codex_binary() -> str | None:
    """The ``codex`` CLI on the terminal PATH, or ``None``. The one lookup Codex code uses."""
    return resolve_tool("codex")


def codex_auth_command(*, device_auth: bool = False) -> list[str]:
    """``codex login`` with the resolved binary, for the Settings terminal.

    Raises ``FileNotFoundError`` when ``codex`` is not on the terminal PATH.
    """
    codex = codex_binary()
    if codex is None:
        raise FileNotFoundError("codex was not found on PATH.")
    argv = [codex, "login"]
    if device_auth:
        argv.append("--device-auth")
    return argv
