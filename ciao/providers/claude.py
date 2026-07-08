"""Claude Code provider using the Agent SDK.

Three SDK features worth knowing when you touch this file:

- ``fallback_model``: picked by ``_fallback_model_for(primary)``. Opus → Sonnet,
  Sonnet → Haiku, Haiku → none. Keeps schedules alive when the primary tier is
  rate-limited.
- ``hooks={"UserPromptSubmit": ...}``: registers a Python callback
  (``ciao/observability/hooks.py``) that fires before every user turn. Injects
  today's date, workspace, GWS profile, and any vault entities mentioned in the
  prompt. Entity matching is index-backed via ``memory-vault/INDEX.md``; no
  full-text scan. Env vars the hook reads per turn: ``CIAO_ACTIVE_WORKSPACE``,
  ``CIAO_ACTIVE_PROJECT``, ``GWS_PROFILE``. ``CIAO_WORKSPACE`` remains the
  filesystem workspace root.
- ``setting_sources=["user", "project", "local"]``: makes the CLI auto-discover
  ``.claude/skills/``, ``.claude/agents/``, and ``.claude/commands/`` (including
  ciao-native commands like ``/remember``).

Bg-agent resume recovery: when a chat's session id is still held by a CLI
``bg`` spare worker (auto-imported CLI sessions), ``--resume`` exits 1 with
``"currently running as a background agent"`` on stderr. ``_stderr_handler``
flips ``_fork_resume_next`` on that line; the next connect attempt sets
``options.fork_session=True`` so the CLI branches a fresh session id off the
existing transcript. ``run_streaming`` retries once when the flag is set.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
import logging
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    CLIConnectionError,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    HookMatcher,
    ProcessError,
    RateLimitEvent,
    ResultMessage,
    StreamEvent as SDKStreamEvent,
    SystemMessage,
    ToolUseBlock,
    get_session_info,
)
from claude_agent_sdk.types import PermissionMode

from ciao.models import (
    AgentRequest,
    AssistantTextDelta,
    BridgeMode,
    PermissionRequestEvent,
    ResultEvent,
    StreamEvent,
    SystemStatusEvent,
    ThinkingEvent,
    TokenUsageEvent,
    ToolUseEvent,
)
from ciao.memory_injector import build_memory_block, system_prompt_payload
from ciao.observability.hooks import (
    build_user_prompt_submit_hook,
    build_web_search_post_tooluse_hook,
)
from ciao.providers.permission_gate import PermissionGate
from ciao.providers.base import (
    ActiveHandle,
    BaseSDKProvider,
    build_claude_message_stream,
    rate_limit_quota_payload,
    rate_limit_status_text,
)
from ciao.rate_limits import RateLimitStore, default_store_path

logger = logging.getLogger(__name__)

_CLAUDE_OP_ERRORS = (ClaudeSDKError, CLIConnectionError, ProcessError)


def get_bundled_claude_path() -> str | None:
    """Find the bundled Claude CLI inside the installed claude-agent-sdk package."""
    try:
        import claude_agent_sdk
        from pathlib import Path
        import platform
        cli_name = "claude.exe" if platform.system() == "Windows" else "claude"
        bundled_path = Path(claude_agent_sdk.__file__).parent / "_bundled" / cli_name
        if bundled_path.exists() and bundled_path.is_file():
            return str(bundled_path)
    except Exception:
        pass
    return None


# Patterns from the bundled Claude Code CLI's stderr that are harmless noise
# (the CLI process losing a race with its own teardown, mostly). Without
# pattern-matching we either flood the server log on every turn or hide
# genuine failures, so these are demoted to DEBUG while everything else
# stays at WARNING.
#
# - "Error in hook callback ... Stream closed": CLI tries to fire a final
#   end-of-turn system-reminder hook (open-tasks reminder) after the SDK
#   transport has already closed. The hook just doesn't run; nothing else
#   breaks.
# - "error: Stream closed at sendRequest": same root cause, surfaces as a
#   bare JS error without the "hook callback" preamble.
_KNOWN_BENIGN_CLI_STDERR = (
    "Error in hook callback",
    "error: Stream closed",
    " at sendRequest (",
    " at <anonymous> (",
)


def _route_cli_stderr(line: str) -> None:
    """Forward a CLI-subprocess stderr line into our logger.

    Without a callback the SDK inherits stderr to the parent process, so
    the CLI's internal warnings end up in uvicorn's stdout (e.g. a
    `Error in hook callback hook_0: ... Stream closed` warning). Routing
    to our logger lets us demote known-benign
    noise without losing genuine errors.
    """
    stripped = line.rstrip()
    if not stripped:
        return
    if any(needle in stripped for needle in _KNOWN_BENIGN_CLI_STDERR):
        logger.debug("claude-cli stderr (benign): %s", stripped)
        return
    logger.warning("claude-cli stderr: %s", stripped)


def _summarize_tool_input(name: str, tool_input: dict) -> str:
    """Extract the most relevant piece from tool input for display."""
    if not tool_input:
        return ""
    # File operations
    if name in ("Read", "read"):
        path = tool_input.get("file_path", "")
        return path
    if name in ("Edit", "edit"):
        path = tool_input.get("file_path", "")
        return path
    if name in ("Write", "write"):
        path = tool_input.get("file_path", "")
        return path
    # Search
    if name in ("Grep", "grep"):
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        return f'"{pattern}"' + (f" in {path}" if path else "")
    if name in ("Glob", "glob"):
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        return pattern + (f" in {path}" if path else "")
    # Shell
    if name in ("Bash", "bash"):
        cmd = tool_input.get("command", "")
        desc = tool_input.get("description", "")
        if desc:
            return desc
        # Truncate long commands
        return cmd[:120] + ("..." if len(cmd) > 120 else "")
    # Agents
    if name in ("Agent", "agent"):
        desc = tool_input.get("description", "")
        subtype = tool_input.get("subagent_type", "")
        if subtype and desc:
            return f"[{subtype}] {desc}"
        return desc or subtype
    # Tasks
    if name in ("TaskCreate",):
        return tool_input.get("subject", "")
    if name in ("TaskUpdate",):
        tid = tool_input.get("taskId", "")
        status = tool_input.get("status", "")
        return f"#{tid} → {status}" if status else f"#{tid}"
    # Skills
    if name in ("Skill",):
        return tool_input.get("skill", "")
    # Interactive question prompt. Headless CLI auto-cancels with empty
    # answers, so the PWA needs the full questions payload to render its
    # own picker. Returning the JSON here keeps every downstream consumer
    # (live stream, /messages replay, schedule logs) on one source of truth.
    if name in ("AskUserQuestion",):
        questions = tool_input.get("questions")
        if questions:
            try:
                return json.dumps({"questions": questions}, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                return ""
        return ""
    # Web
    if name in ("WebSearch",):
        return tool_input.get("query", "")
    if name in ("WebFetch",):
        return tool_input.get("url", "")[:100]
    # Notebook
    if name in ("NotebookEdit",):
        return tool_input.get("cell_id", "")
    # MCP tools (tool name contains __)
    if "__" in name:
        # Try to find the most interesting field
        for key in ("query", "text", "message", "url", "path", "name", "title"):
            val = tool_input.get(key, "")
            if val:
                return str(val)[:100]
    # Fallback: first string value
    for val in tool_input.values():
        if isinstance(val, str) and val:
            return val[:100]
    return ""


_BRIDGE_TO_SDK_MODE: dict[BridgeMode, PermissionMode] = {
    "normal": "default",
    "plan": "plan",
    # "auto" → SDK's real classifier-backed permission mode (announced
    # 2026-04 on claude.com/blog/auto-mode). Safe actions run silently,
    # risky ones are blocked or escalated to a manual prompt. The manual
    # prompt flows through the ``can_use_tool`` callback wired below.
    "auto": "auto",
    "bypass": "bypassPermissions",
}


def _sdk_permission_mode(bridge_mode: BridgeMode) -> PermissionMode:
    return _BRIDGE_TO_SDK_MODE.get(bridge_mode, "bypassPermissions")


def _fallback_model_for(primary: str) -> str | None:
    """Pick a cheaper fallback so rate-limited schedules downgrade instead of failing.

    Returns None when the primary is already the cheapest tier, so the SDK
    doesn't fall back to itself.
    """
    low = (primary or "").lower()
    if "opus" in low:
        return "sonnet"
    if "sonnet" in low:
        return "haiku"
    return None


@dataclass(slots=True)
class SDKHandle(ActiveHandle):
    """Handle wrapping a live SDK client for interruption."""

    client: ClaudeSDKClient

    async def stop(self) -> None:
        try:
            await self.client.interrupt()
        except _CLAUDE_OP_ERRORS:
            logger.debug("Claude interrupt failed; client is likely already idle")


class ClaudeProvider(BaseSDKProvider):
    """Claude provider backed by the Agent SDK."""

    def __init__(self, workspace_root: Path, *, config: object | None = None) -> None:
        super().__init__(workspace_root, config=config)
        self._client: ClaudeSDKClient | None = None
        self._connected = False
        self._session_id: str | None = None
        self._pending_quota: dict[str, str] = {}
        # Running token counters for the in-flight turn, fed by partial
        # stream events. ``_turn_input_tokens`` and ``_turn_output_committed``
        # accumulate across assistant messages in a tool loop; ``_cur_msg_output``
        # holds the current message's cumulative output so the live total is
        # committed + current. Reset at the start of each streaming turn.
        self._turn_input_tokens = 0
        self._turn_output_committed = 0
        self._cur_msg_output = 0
        # Set by the stderr handler when the CLI refuses to resume because
        # the session is held by a background agent. The next connect
        # attempt re-resumes with ``--fork-session`` to branch a copy.
        self._fork_resume_next = False
        # Runtime root: state_path.parent on CiaoConfig; fall back to
        # workspace_root/.runtime when config is absent (tests).
        runtime_root = Path(
            getattr(config, "state_path", workspace_root / ".runtime" / "state.json")
        ).parent
        self._rate_limit_store = RateLimitStore(path=default_store_path(runtime_root))
        self._vault_root = Path(
            getattr(config, "vault_root", workspace_root / "memory-vault")
        )
        # One gate per provider. Each turn rebinds ``emit`` to the turn's
        # merge queue so late approvals can't land in a stale stream.
        self._permission_gate = PermissionGate()

    def _stderr_handler(self, line: str) -> None:
        _route_cli_stderr(line)
        if "is currently running as a background agent" in line:
            self._fork_resume_next = True

    @property
    def permission_gate(self) -> PermissionGate:
        """Expose the gate so route handlers can deliver user replies."""
        return self._permission_gate

    def _memory_config(self) -> dict[str, Any]:
        """Pull memory knobs off CiaoConfig with safe fallbacks for tests."""
        cfg = getattr(self, "config", None)
        return {
            "enabled": bool(getattr(cfg, "memory_enabled", True)),
            "memory_limit": int(getattr(cfg, "memory_char_limit", 2200)),
            "user_limit": int(getattr(cfg, "user_char_limit", 1375)),
        }

    @property
    def current_session_id(self) -> str | None:
        """Session id as currently known to the provider (may be set mid-stream)."""
        return self._session_id

    async def _ensure_connected(self, request: AgentRequest) -> ClaudeSDKClient:
        if (
            self._client is not None
            and self._connected
            and request.resume_session
            and request.resume_session != self._session_id
        ):
            await self.disconnect()

        if self._client is not None and self._connected:
            if request.model != self._current_model:
                try:
                    await self._client.set_model(request.model)
                    self._current_model = request.model
                except _CLAUDE_OP_ERRORS:
                    logger.warning("Claude set_model failed; reconnecting")
                    await self.disconnect()
            if self._client is not None and self._connected and request.mode != self._current_mode:
                try:
                    await self._client.set_permission_mode(_sdk_permission_mode(request.mode))
                    self._current_mode = request.mode
                except _CLAUDE_OP_ERRORS:
                    logger.warning("Claude set_permission_mode failed; reconnecting")
                    await self.disconnect()
        if self._client is not None and self._connected:
            return self._client

        resume_session = self._validated_resume_session(request.resume_session)
        system_cli = get_bundled_claude_path()
        if not system_cli:
            raise FileNotFoundError("Bundled Claude Code CLI not found in the installed claude-agent-sdk package.")
        logger.info("Using bundled Claude Code CLI: %s", system_cli)


        # Bounded agent-managed memory: frozen snapshot of ~/.ciao/memory.md
        # and ~/.ciao/user.md appended to Claude Code's default system prompt.
        # Edits go through `ciao memory` (same path Pi uses, via the script
        # wrapper) instead of an MCP tool, so the write path stays in sync.
        # Edits persist immediately but only appear in this block on the next
        # session, which keeps the prefix cache stable.
        memory_cfg = self._memory_config()
        memory_block = ""
        if memory_cfg["enabled"]:
            try:
                memory_block = build_memory_block(
                    memory_char_limit=memory_cfg["memory_limit"],
                    user_char_limit=memory_cfg["user_limit"],
                )
            except Exception:  # noqa: BLE001 — never block a chat on memory wiring
                logger.exception("memory block failed; continuing without it")
                memory_block = ""
        system_prompt = system_prompt_payload(memory_block)

        options = ClaudeAgentOptions(
            model=request.model,
            fallback_model=_fallback_model_for(request.model),
            permission_mode=_sdk_permission_mode(request.mode),
            cwd=str(self.workspace_root),
            include_partial_messages=True,
            env=request.extra_env or {},
            # Per-workspace tool denylist (e.g. block claude.ai connector
            # MCPs for personal chats). Empty list = no denylist applied.
            disallowed_tools=list(request.disallowed_tools or []),
            # Agents are discovered from .claude/agents/ via setting_sources
            # below. No manual frontmatter parsing needed.
            setting_sources=["user", "project", "local"],
            # Per-turn runtime context + vault entity tags. Fires before
            # each user prompt reaches the model. See ciao/observability/hooks.py.
            hooks={
                "UserPromptSubmit": [HookMatcher(
                    hooks=[build_user_prompt_submit_hook(
                        self._vault_root,
                        request.extra_env or {},
                    )],
                )],
                # Backfill WebSearch on Ollama- and OpenRouter-routed chats.
                # Their Anthropic-compat layers don't execute the server-side
                # web_search tool, so WebSearch returns an empty boilerplate;
                # this PostToolUse hook reruns the query against the backend's
                # own search surface (Ollama /api/web_search, OpenRouter web
                # plugin) and injects the real results. No-op on the Anthropic
                # path and when WebSearch already returned results. See
                # ciao/observability/hooks.py.
                "PostToolUse": [HookMatcher(
                    matcher="WebSearch",
                    hooks=[build_web_search_post_tooluse_hook(
                        request.extra_env or {},
                    )],
                )],
            },
            # Auto mode's classifier handles most tool calls silently, but
            # escalations (blocked actions the model keeps insisting on)
            # surface here. The gate publishes a PermissionRequestEvent into
            # the PWA and awaits the user's allow/deny. Wired for every mode
            # so CLI "default" prompts also route through the UI instead of
            # hanging the CLI subprocess.
            can_use_tool=self._permission_gate.handle,
            # Capture CLI-subprocess stderr so it lands in our logger
            # instead of bleeding into uvicorn's output. _stderr_handler
            # demotes the harmless end-of-turn "hook callback Stream closed"
            # races to DEBUG; anything else is logged at WARNING. It also
            # flips ``_fork_resume_next`` when the CLI refuses to resume a
            # session held by a background agent, so the retry can fork.
            stderr=self._stderr_handler,
        )
        if request.thinking_level:
            # SDK-native effort knob (low/medium/high/xhigh/max). Unset keeps
            # the SDK's adaptive-thinking default.
            options.effort = request.thinking_level  # type: ignore[assignment]
        if system_prompt is not None:
            options.system_prompt = system_prompt
        if system_cli:
            options.cli_path = system_cli
        if resume_session:
            options.resume = resume_session
            # If the previous attempt was blocked by a background agent
            # holding this session, fork off a copy instead of refusing.
            # The CLI assigns a fresh session id, which gets persisted
            # back to the chat from the first AssistantMessage.
            if self._fork_resume_next:
                options.fork_session = True
                logger.info(
                    "Forking resumed session %s (previous attempt blocked by background agent)",
                    resume_session,
                )
            self._fork_resume_next = False
        else:
            # Force a brand-new session so the CLI cannot auto-resume a
            # previous session from project state files.  Without this the
            # same session gets reused across independent tasks, causing
            # cross-task result bleed (e.g. curation output delivered as
            # the morning briefing).
            options.session_id = str(uuid.uuid4())

        self._client = ClaudeSDKClient(options=options)
        self._remember_settings(request)
        return self._client

    def _validated_resume_session(self, session_id: str | None) -> str | None:
        if not session_id:
            return None
        try:
            info = get_session_info(session_id, directory=str(self.workspace_root))
        except ValueError:
            return None
        if info is None:
            logger.warning("Claude session %s is stale locally; starting fresh", session_id)
            return None
        return session_id

    def _prompt_payload(self, request: AgentRequest):
        # Always stream: SDK's can_use_tool gate requires an AsyncIterable
        # prompt at connect() time (claude_agent_sdk>=0.1.63). A plain
        # string raises "can_use_tool callback requires streaming mode".
        return build_claude_message_stream(request)

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except _CLAUDE_OP_ERRORS:
                logger.debug("Claude disconnect failed; dropping client anyway")
            self._client = None
        self._connected = False
        self._session_id = None
        self._pending_quota = {}
        self._reset_settings()

    async def steer(self, request: AgentRequest) -> bool:
        """Inject a follow-up user message into the currently-active turn.

        Returns True if the message was pushed to the live SDK client, False
        if there's no active client (caller should fall back to queuing).

        The caller owns the lifecycle of the drive loop consuming
        ``receive_response()`` — this method just writes to the transport.
        The SDK's ``query()`` is not mutually exclusive with a pending
        ``receive_response()`` iterator, so the new message lands as an
        additional user turn the CLI processes in-line.
        """
        if self._client is None or not self._connected:
            return False
        try:
            payload = self._prompt_payload(request)
            await self._client.query(payload)
            return True
        except _CLAUDE_OP_ERRORS:
            logger.warning("Claude steer query failed; caller should fall back to queue")
            return False

    @property
    def can_drain(self) -> bool:
        """True when a connected client exists to drain between turns."""
        return self._client is not None and self._connected

    async def drain_events(self) -> AsyncGenerator[StreamEvent, None]:
        """Yield SDK events arriving *between* turns until cancelled.

        Background subagents outlive the parent turn: when one completes, the
        CLI injects a task-notification, and a fresh parent turn follows —
        run by the CLI on its own (CLI-version dependent, observed not to
        happen reliably) or requested via ``steer()`` by the completion
        watcher's synthesis nudge. Its messages land on stdout with nobody
        consuming them. Left unread they
        sit in the transport buffer and the stale ResultMessage would
        terminate the *next* ``receive_response()`` immediately, bleeding one
        turn's output into the next. This generator keeps the pipe drained
        and hands the events to the caller for live display.

        The caller owns the lifecycle: it MUST cancel this generator before
        starting a new turn (``receive_response`` and this iterator consume
        from the same underlying stream and must not run concurrently).
        """
        client = self._client
        if client is None or not self._connected:
            return

        merged: asyncio.Queue[object] = asyncio.Queue()
        _DONE = object()
        # Background turns can hit Auto-mode tool gating too; surface the
        # permission request instead of leaving the can_use_tool await to
        # silently time out.
        self._permission_gate.set_emit(lambda ev: merged.put_nowait(ev))

        async def consume() -> None:
            try:
                async for msg in client.receive_messages():
                    for event in self._convert_message(msg):
                        merged.put_nowait(event)
            except _CLAUDE_OP_ERRORS:
                logger.debug("Between-turns drain ended on SDK error", exc_info=True)
            finally:
                merged.put_nowait(_DONE)

        consumer = asyncio.create_task(consume())
        try:
            while True:
                item = await merged.get()
                if item is _DONE:
                    return
                yield item  # type: ignore[misc]
        finally:
            self._permission_gate.set_emit(None)
            self._permission_gate.cancel_all("Background drain ended before approval")
            if not consumer.done():
                consumer.cancel()
                try:
                    await consumer
                except (asyncio.CancelledError, *_CLAUDE_OP_ERRORS):
                    pass

    async def run_streaming(
        self,
        request: AgentRequest,
        register_handle: Callable[[ActiveHandle | None], None],
    ) -> AsyncGenerator[StreamEvent, None]:
        # Pre-stream connect/query failures (e.g. "ProcessTransport is not
        # ready for writing" when a prior client died but wasn't reaped yet)
        # are transient and safe to retry: no events were yielded, the
        # session_id on the chat is preserved, and _ensure_connected will
        # build a fresh client on the retry because _connected was just
        # reset in _run_streaming_once's except block.
        #
        # If ANY event was yielded before the failure, we must not retry —
        # the partial output is already on the wire and a second pass would
        # duplicate content or double-run tool calls.
        attempt = 0
        while True:
            events_yielded = False
            try:
                async for event in self._run_streaming_once(request, register_handle):
                    events_yielded = True
                    yield event
                return
            except CLIConnectionError as exc:
                if not events_yielded and attempt == 0:
                    # Transient stdin race: the prior client's ProcessTransport
                    # hasn't fully released the write fd when the new one tries
                    # to attach. The retry always succeeds (no events were
                    # yielded, fresh client is built in _ensure_connected).
                    # Log at debug so the stderr log doesn't accumulate
                    # ~15 warnings/day per chat.
                    logger.debug(
                        "Claude pre-stream connection failed (%s); auto-retrying once",
                        exc,
                    )
                    attempt += 1
                    continue
                if request.resume_session:
                    logger.error(
                        "Claude resume failed for session %s after retry; raising "
                        "instead of silently starting a fresh session",
                        request.resume_session,
                    )
                raise
            except _CLAUDE_OP_ERRORS:
                # Non-connection SDK errors (ProcessError, generic
                # ClaudeSDKError) are not the transient stdin race; surface
                # them without a retry. Exception: the CLI refuses to
                # resume a session held by a background agent (auto-imported
                # CLI sessions running as `bg`). The stderr handler sets
                # ``_fork_resume_next`` so we can retry once with
                # ``--fork-session`` and keep the conversation alive.
                if (
                    not events_yielded
                    and attempt == 0
                    and self._fork_resume_next
                    and request.resume_session
                ):
                    logger.warning(
                        "Claude resume blocked by background agent for session %s; "
                        "retrying with --fork-session",
                        request.resume_session,
                    )
                    attempt += 1
                    continue
                if request.resume_session:
                    logger.error(
                        "Claude resume failed for session %s; raising instead of "
                        "silently starting a fresh session (which would lose context)",
                        request.resume_session,
                    )
                raise

    async def _run_streaming_once(
        self,
        request: AgentRequest,
        register_handle: Callable[[ActiveHandle | None], None],
    ) -> AsyncGenerator[StreamEvent, None]:
        client = await self._ensure_connected(request)
        payload = self._prompt_payload(request)

        # Fresh turn: zero the live token counters so the running total we
        # emit over the wire starts from this turn, not the previous one.
        self._turn_input_tokens = 0
        self._turn_output_committed = 0
        self._cur_msg_output = 0

        # Register handle *before* connect/query so /stop can interrupt a
        # hanging SDK call — otherwise the bot locks up with "No active run".
        register_handle(SDKHandle(client=client))

        # Merge queue: both the SDK consumer task AND the permission gate
        # push here. One producer per source keeps the yield order causal
        # (e.g. tool_use → permission_request → tool result).
        merged: asyncio.Queue[object] = asyncio.Queue()
        _DONE = object()

        # Rebind the gate for this turn so permission requests land in this
        # turn's merge queue. On finally we restore a noop so a delayed gate
        # callback from a prior turn can't publish into a new turn.
        self._permission_gate.set_emit(lambda ev: merged.put_nowait(ev))

        async def consume_sdk() -> None:
            """Drive connect/query then fan SDK events into ``merged``."""
            try:
                if not self._connected:
                    await asyncio.wait_for(client.connect(payload), timeout=120)
                    self._connected = True
                else:
                    await asyncio.wait_for(client.query(payload), timeout=120)

                pending_result: ResultEvent | None = None
                async for msg in client.receive_response():
                    for event in self._convert_message(msg):
                        if isinstance(event, ResultEvent):
                            pending_result = event
                        else:
                            merged.put_nowait(event)

                if pending_result is not None:
                    await self._augment_with_context_pct(client, pending_result)
                    merged.put_nowait(pending_result)
            finally:
                merged.put_nowait(_DONE)

        consumer = asyncio.create_task(consume_sdk())
        try:
            while True:
                item = await merged.get()
                if item is _DONE:
                    break
                # StreamEvent subclasses are the only non-sentinel items that
                # reach this queue (SDK conversions + gate publishes).
                yield item  # type: ignore[misc]

            # Re-raise any exception from the SDK consumer now that we've
            # drained the queue. Keeps the retry logic in ``run_streaming``
            # working (CLIConnectionError triggers one retry).
            exc = consumer.exception()
            if exc is not None:
                raise exc
        except _CLAUDE_OP_ERRORS:
            try:
                await self.disconnect()
            except Exception:
                pass
            raise
        except asyncio.TimeoutError:
            logger.error("Claude connect/query timed out after 120s")
            try:
                await self.disconnect()
            except Exception:
                pass
            raise
        finally:
            # Unbind the gate so a late callback can't land on a stale queue.
            self._permission_gate.set_emit(None)
            # Resolve any still-pending approvals as deny — otherwise the
            # SDK's can_use_tool await would hang after the turn ends.
            self._permission_gate.cancel_all("Turn ended before approval")
            if not consumer.done():
                consumer.cancel()
                try:
                    await consumer
                except (asyncio.CancelledError, *_CLAUDE_OP_ERRORS):
                    pass
            register_handle(None)

    @staticmethod
    async def _augment_with_context_pct(
        client: ClaudeSDKClient, event: ResultEvent
    ) -> None:
        """Fetch accurate context-window % from the CLI and attach to usage.

        Silent on failure: if the CLI cannot answer, the field is simply
        absent from the event. We deliberately do not fall back to a
        hand-rolled estimate — only the CLI has the authoritative number.
        """
        try:
            usage = await client.get_context_usage()
        except _CLAUDE_OP_ERRORS:
            logger.debug("get_context_usage failed; dropping context_pct")
            return
        except Exception:  # noqa: BLE001 — defensive against SDK surprises
            logger.debug("get_context_usage raised unexpectedly", exc_info=True)
            return
        pct = usage.get("percentage") if isinstance(usage, dict) else None
        if isinstance(pct, (int, float)):
            event.usage = {**event.usage, "context_pct": f"{pct:.1f}%"}

    def _convert_message(self, msg: Any) -> list[StreamEvent]:
        if isinstance(msg, SDKStreamEvent):
            return self._convert_stream_event(msg)

        if isinstance(msg, AssistantMessage):
            events: list[StreamEvent] = []
            # When this AssistantMessage came from inside a Task subagent the
            # SDK sets ``parent_tool_use_id`` to the parent-level Task's
            # tool_use_id. Forward that so the client can attribute the tool
            # call to the right agent in the trace.
            parent_id = getattr(msg, "parent_tool_use_id", None)
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    summary = _summarize_tool_input(block.name, block.input or {})
                    events.append(ToolUseEvent(
                        type="assistant",
                        tool_name=block.name,
                        tool_input=summary,
                        tool_use_id=getattr(block, "id", None),
                        parent_tool_use_id=parent_id,
                    ))
            if msg.session_id:
                self._session_id = msg.session_id
            return events

        if isinstance(msg, SystemMessage):
            return self._convert_system_message(msg)

        if isinstance(msg, ResultMessage):
            self._session_id = msg.session_id or self._session_id
            usage = self._extract_usage(msg)
            quota = self._pending_quota or self._extract_quota(msg)
            self._pending_quota = {}
            return [
                ResultEvent(
                    type="result",
                    result=msg.result or "",
                    session_id=self._session_id,
                    is_error=msg.is_error,
                    effective_model=self._extract_effective_model(msg),
                    usage=usage,
                    quota=quota,
                    cost_usd=msg.total_cost_usd,
                )
            ]

        if isinstance(msg, RateLimitEvent):
            self._pending_quota = rate_limit_quota_payload(msg.rate_limit_info)
            # Persist per-bucket so the Settings card can show all five
            # tiers (5h + weekly variants + overage) independently.
            try:
                self._rate_limit_store.update(msg.rate_limit_info)
            except Exception:  # noqa: BLE001 — never fail a turn on telemetry
                logger.debug("rate_limit persistence failed", exc_info=True)
            return [
                SystemStatusEvent(
                    type="system",
                    status=rate_limit_status_text(self._pending_quota),
                )
            ]

        return []

    def _convert_stream_event(self, event: SDKStreamEvent) -> list[StreamEvent]:
        raw = event.event
        if not isinstance(raw, dict):
            return []

        # Stream events fired from inside a Task subagent carry the parent
        # Task's tool_use_id. Threading it through lets the client label
        # subagent activity in the trace ("[Explore] $ Bash …") instead of
        # blending it with parent's own work.
        parent_id = getattr(event, "parent_tool_use_id", None)

        event_type = raw.get("type", "")
        if event_type == "content_block_delta":
            delta = raw.get("delta", {})
            delta_type = delta.get("type", "")
            if delta_type == "text_delta":
                text = delta.get("text", "")
                if text:
                    return [AssistantTextDelta(
                        type="assistant",
                        text=text,
                        parent_tool_use_id=parent_id,
                    )]
            if delta_type == "thinking_delta":
                return [ThinkingEvent(
                    type="assistant",
                    text=delta.get("thinking", ""),
                    parent_tool_use_id=parent_id,
                )]

        if event_type == "content_block_start":
            block = raw.get("content_block", {})
            if block.get("type") == "tool_use" and block.get("name"):
                return [ToolUseEvent(
                    type="assistant",
                    tool_name=block["name"],
                    tool_use_id=block.get("id"),
                    parent_tool_use_id=parent_id,
                )]
            if block.get("type") == "thinking":
                return [ThinkingEvent(
                    type="assistant",
                    text="",
                    parent_tool_use_id=parent_id,
                )]

        # Running token usage. Only the top-level turn is counted (subagents
        # run their own sessions and aren't rolled into the parent's usage),
        # so live numbers track the parent turn's own totals. A tool loop emits
        # one message_start / …message_delta pair per assistant message; we
        # commit the finished message's output before starting the next.
        if parent_id is None and event_type in ("message_start", "message_delta"):
            usage_ev = self._track_stream_usage(event_type, raw)
            if usage_ev is not None:
                return [usage_ev]

        return []

    def _track_stream_usage(
        self, event_type: str, raw: dict
    ) -> TokenUsageEvent | None:
        """Accumulate partial-message usage into a cumulative turn total."""
        if event_type == "message_start":
            usage = (raw.get("message") or {}).get("usage") or {}
            # A new assistant message began: fold the previous message's
            # output into the committed total before it gets overwritten.
            self._turn_output_committed += self._cur_msg_output
            self._cur_msg_output = 0
            input_tokens = usage.get("input_tokens")
            if isinstance(input_tokens, int):
                self._turn_input_tokens += input_tokens
            out = usage.get("output_tokens")
            if isinstance(out, int):
                self._cur_msg_output = out
        else:  # message_delta
            usage = raw.get("usage") or {}
            out = usage.get("output_tokens")
            if isinstance(out, int):
                self._cur_msg_output = out
        return TokenUsageEvent(
            type="assistant",
            input_tokens=self._turn_input_tokens,
            output_tokens=self._turn_output_committed + self._cur_msg_output,
        )

    def _convert_system_message(self, msg: SystemMessage) -> list[StreamEvent]:
        subtype = msg.subtype
        data = msg.data or {}

        if subtype == "status":
            return [SystemStatusEvent(type="system", status=data.get("status"))]

        if subtype == "input_required":
            tool_name = data.get("tool_name", "") or data.get("tool", "")
            tool_input = data.get("tool_input", "") or ""
            if isinstance(tool_input, dict):
                import json

                tool_input = json.dumps(tool_input)
            return [
                PermissionRequestEvent(
                    type="system",
                    message=data.get("message", "Permission required"),
                    tool_name=str(tool_name),
                    tool_input=str(tool_input),
                )
            ]

        return []

    @staticmethod
    def _extract_usage(msg: ResultMessage) -> dict[str, str]:
        """Per-turn token counts from the ResultMessage.

        Context-window occupancy is intentionally NOT computed here. We ask
        the CLI for that number via ``get_context_usage()`` after the turn
        drains (see :meth:`_run_streaming_once`) because the CLI is the only
        place that can count system prompt + tool defs + memory files +
        autocompact buffer correctly.
        """
        summary: dict[str, str] = {}
        usage = msg.usage
        if isinstance(usage, dict):
            for key in (
                "input_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
                "output_tokens",
            ):
                value = usage.get(key)
                if isinstance(value, int):
                    summary[key] = str(value)
        return summary

    @staticmethod
    def _extract_effective_model(msg: ResultMessage) -> str:
        model_usage = msg.model_usage
        if isinstance(model_usage, dict):
            for value in model_usage.values():
                if isinstance(value, dict):
                    model = value.get("model")
                    if isinstance(model, str) and model:
                        return model
        return ""

    @staticmethod
    def _extract_quota(msg: ResultMessage) -> dict[str, str]:
        return {}
