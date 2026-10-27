"""Claude Code provider using the Agent SDK.

Three SDK features worth knowing when you touch this file:

- ``fallback_model``: picked by ``_fallback_model_for(primary)``. Fable →
  Opus → Sonnet → Haiku → none. Keeps schedules alive when the primary tier is
  rate-limited.
- ``hooks={"PreToolUse": ...}``: denies detached shell invocations
  (``nohup … &``) the CLI cannot track; native background Bash and ``Monitor``
  run unchanged. Workspace/project/date/entity context is already supplied by
  the shared request capsule, so Claude does not receive a duplicate
  UserPromptSubmit injection.
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
import math
import os
import sys
import time
import uuid
import warnings
from dataclasses import dataclass, field, replace
import logging
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from collections.abc import Mapping
from typing import Any, cast

from claude_agent_sdk import (
    AssistantMessage,
    CanUseToolShadowedWarning,
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
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    ToolUseBlock,
    UserMessage,
    get_session_info,
)
from claude_agent_sdk.types import TERMINAL_TASK_STATUSES, PermissionMode, SandboxSettings, SystemPromptPreset

from ciao.fs_sandbox import (
    CLAUDE_FILE_TOOL_PATH_KEYS,
    FsSandboxUnavailable,
    claude_file_tool_guard,
    claude_sandbox_settings,
)
from ciao.models import (
    COMPACT_COMMAND,
    AgentRequest,
    AssistantTextDelta,
    BridgeMode,
    PermissionRequestEvent,
    PromptCancelledEvent,
    ResultEvent,
    SteerDeliveredEvent,
    StreamEvent,
    SystemStatusEvent,
    ThinkingEvent,
    TokenUsageEvent,
    ToolUseEvent,
    linked_repo_of,
    provider_reuse_key,
    tool_call_facts,
    tool_work_dirs,
)
from ciao.execution_modes import (
    harness_skill_overrides,
)
from ciao.core_prompt import system_prompt_payload
from ciao.observability.hooks import build_detached_shell_deny_hook
from ciao.providers.permission_gate import PermissionGate
from ciao.providers.base import (
    BACKGROUND_WORK_MAX_S,
    ActiveHandle,
    BaseSDKProvider,
    ProviderCapabilities,
    build_claude_message_stream,
    rate_limit_quota_payload,
)
from ciao.rate_limits import RateLimitStore, default_store_path, is_rate_limit_telemetry
from ciao.setup_status import record_claude_bundled_skills

logger = logging.getLogger(__name__)

# We set ``allowed_tools`` and ``can_use_tool`` together on purpose: the
# allowlist covers Ciaobot's own non-destructive control plane (see
# AUTO_APPROVED_MCP_TOOLS) and the gate handles everything else. The SDK warns
# on every connect that the callback is shadowed for those names, which is
# exactly the intent, so silence that one category instead of printing a
# 35-tool paragraph into the server log per process.
warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)

# CLIConnectionError and ProcessError are both subclasses of ClaudeSDKError, as
# is MessageParseError — they are listed for documentation, not for reach. Do
# not add SDK exceptions here expecting new behaviour: anything deriving from
# ClaudeSDKError is already caught, and reaching into claude_agent_sdk._errors
# for a name the package keeps out of __all__ only buys an ImportError on the
# next SDK bump.
_CLAUDE_OP_ERRORS = (ClaudeSDKError, CLIConnectionError, ProcessError)

# How long a finished CLI turn waits for the replay echo of a steer that is
# still pending before the Ciaobot turn ends anyway (see ``steer_turn``).
_STEER_ECHO_GRACE_S = 30.0
# Bound on cancelling the orphaned steers after that grace expires.
_STEER_CANCEL_TIMEOUT_S = 5.0
# Bound on the CLI's context-usage control request. The first call on a fresh
# CLI measured 0.7-27.8 s and later calls <= 0.44 s (#1402), so the bound only
# ever costs the first turn its exact figure; the local estimate covers it.
_CONTEXT_USAGE_TIMEOUT_S = 3.0

# The SDK sizes its ``initialize`` control-request deadline from this
# process-wide environment variable and exposes no option for it. A cold CLI
# start under load measured 13-53 s on a 10-core Mac, so the SDK's 60 s floor
# is right at the edge: a turn that crosses it dies before the model is asked
# anything ("Control request timeout: initialize"). A CLI that is alive but
# silent still fails, just one turn later than before.
_STREAM_CLOSE_TIMEOUT_ENV = "CLAUDE_CODE_STREAM_CLOSE_TIMEOUT"
_INITIALIZE_TIMEOUT_MS = 180_000
# The SDK owns the initialize deadline (it reads it from
# CLAUDE_CODE_STREAM_CLOSE_TIMEOUT when the client is built) and names a miss
# with "Control request timeout: initialize". Our outer connect deadline must
# outlive it, or it fires first with a bare TimeoutError, the SDK never names
# the cause, and the one retry in run_streaming can never classify the miss.
# The 30 s margin is smaller than a loaded machine's cold spawn (see above) and
# the SDK's 180 s clock starts after it, so the outer deadline can still fire
# first; that is harmless because _connect_with_deadline classifies its own
# timeout as an initialize miss, preserving the retry.
_CONNECT_TIMEOUT_S = _INITIALIZE_TIMEOUT_MS / 1000 + 30
# Ask the CLI to name a refused startup in the message stream. Without it a
# startup failure ends with stderr alone, which the SDK turns into a bare
# control-request timeout instead of an error that says what went wrong.
# Claude Code >= 2.1.274 reads it.
_STARTUP_FAILURE_RESULTS_ENV = "CLAUDE_CODE_STARTUP_FAILURE_RESULTS"
# Every control-request failure is a bare ``Exception``, so the message is the
# only thing that identifies an initialize handshake that missed its deadline.
_INITIALIZE_TIMEOUT_MESSAGE = "Control request timeout: initialize"


class ClaudeInitializeTimeout(RuntimeError):
    """The CLI never answered the SDK's ``initialize`` handshake in time."""


class _SteerEchoTimeout(Exception):
    """The grace wait for a follow-up round expired (private to the turn loop)."""

# Shown when a turn's ``receive_response()`` ends without a terminal
# ResultMessage. That is never a normal end-of-turn: the CLI subprocess
# stream closed mid-turn (server restart/shutdown, subprocess kill, transport
# drop). Without an explicit result the turn would end silently — no error,
# no recorded response — so the chat just appears to "stop" after some tool
# calls. Surfacing this keeps the failure visible and preserves the session
# id so the next turn resumes instead of losing the conversation.
_TURN_INTERRUPTED_MESSAGE = (
    "⚠️ The turn was interrupted before it finished "
    "(the local Ciaobot server or the model subprocess stopped mid-turn — "
    "often a server restart). No output was lost from earlier steps; send "
    "\"continue\" to resume."
)

# The Claude CLI appends this banner as assistant text when the upstream API
# connection drops mid-stream ("API Error: Connection closed mid-response. The
# response above may be incomplete."). The SDK may then report the turn as a
# normal (non-error) terminal result, so nothing downstream would treat it as a
# failure. We detect the banner and re-flag the result as an error the
# orchestration layer can auto-resume. Keep these markers in sync with
# ``_is_retryable_connection_error`` in ``ciao/web/chat_service.py``.
_CONNECTION_DROP_MARKERS = (
    "connection closed mid-response",
    "response above may be incomplete",
)

# Canonical result text used when the drop was seen only in streamed deltas and
# the terminal result field itself is empty. Carries a marker so the
# orchestration layer's connection-error classifier recognises it.
_CONNECTION_DROP_MESSAGE = (
    "API Error: Connection closed mid-response. The response above may be "
    "incomplete."
)


def _is_connection_drop_text(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in _CONNECTION_DROP_MARKERS)


# A name-resolution / connect failure surfaces from the Claude CLI as a generic
# "API Error: Unable to connect to API (ENOTFOUND)" with no host and no error
# category, so an operator reading a failed schedule can't tell which endpoint
# failed to resolve or whether it was DNS, a refused connection, a timeout,
# or auth. We know the endpoint the turn was pointed at, so annotate the error
# with it and classify the failure. See issues #162 and #178. The annotation
# Connection-error annotations are shared with the provider layer, so every
# provider a schedule can dispatch through surfaces the same context.
from ciao.providers.connect_errors import (  # noqa: E402
    annotate_connection_host as _annotate_connection_host,
)


# Hostname named in connection-error annotations. Endpoint routing is Claude
# Code's own configuration, so Ciaobot reports Anthropic's default.
_API_HOST = "api.anthropic.com"

# The claude-agent-sdk reads the CLI subprocess stdout into a bounded decode
# buffer (default 1 MiB, ``_DEFAULT_MAX_BUFFER_SIZE``) and raises a fatal,
# stream-killing error when a single JSON message exceeds it — typically a
# large tool result or assistant content block. Raise the ceiling well above
# the default so legitimately large messages don't abort the turn.
_SDK_MAX_BUFFER_BYTES = 32 * 1024 * 1024

# Shown when even the raised buffer is exceeded (or any decode failure): the
# SDK reader raises a fatal error that would otherwise kill the chat stream.
_OVERSIZED_MESSAGE = (
    "⚠️ The model produced a single message too large to decode "
    "(usually an oversized tool result). The turn was stopped to keep the chat "
    "alive — no earlier output was lost. Retry the turn; if it keeps happening, "
    "narrow the step that produces the huge output (e.g. read less at once)."
)


def _is_oversized_message_error(exc: BaseException) -> bool:
    """True for the SDK reader's fatal 'JSON message exceeded maximum buffer
    size' / 'Failed to decode JSON' error, which otherwise kills the stream."""
    msg = str(exc)
    return "buffer size" in msg or "Failed to decode JSON" in msg


def _is_initialize_timeout(exc: BaseException) -> bool:
    """True for the SDK's control-request deadline on the initialize handshake.

    The SDK raises a bare ``Exception`` for every control-request failure, so
    the message is the only signal. Other subtypes (``get_context_usage``,
    ``set_model``, ``interrupt``) also time out by design and must not be
    mistaken for a CLI that never started.
    """
    return isinstance(exc, Exception) and str(exc).startswith(_INITIALIZE_TIMEOUT_MESSAGE)


def _catalog_account_generation() -> int:
    """Account generation to tag a capture with, read before connecting."""
    from ciao.providers.catalog_evidence import account_generation

    return account_generation()


async def _connect_with_deadline(client: ClaudeSDKClient) -> None:
    """Connect to the CLI, naming a missed initialize as a retryable miss."""
    try:
        await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT_S)
    except asyncio.TimeoutError as exc:
        raise ClaudeInitializeTimeout(
            f"connect deadline {_CONNECT_TIMEOUT_S:.0f}s exceeded"
        ) from exc
    except Exception as exc:
        if _is_initialize_timeout(exc):
            raise ClaudeInitializeTimeout(str(exc)) from exc
        raise


def _cli_process_exited(client: object) -> bool:
    """True once the SDK client's CLI subprocess has exited.

    Reads the SDK's private transport; a client without one (tests) counts
    as alive.
    """
    transport = getattr(client, "_transport", None)
    process = getattr(transport, "_process", None)
    return process is not None and getattr(process, "returncode", None) is not None


# Deliberate limit: only CLAUDE.md and AGENTS.md in the workspace root are
# watched. CLAUDE.local.md, .claude/CLAUDE.md, rules and @imports reach the
# agent at the next natural reconnect.
_GUIDE_FILES = ("CLAUDE.md", "AGENTS.md")


def _guide_fingerprint(root: Path) -> tuple[tuple[int, int] | None, ...]:
    """(mtime_ns, size) of each native guide file in ``root``; None when absent.

    Only ``CLAUDE.md`` and ``AGENTS.md`` in the workspace root are watched.
    ``CLAUDE.local.md``, ``.claude/CLAUDE.md``, rules and ``@imports`` reach
    the agent at the next natural reconnect.
    """
    marks: list[tuple[int, int] | None] = []
    for name in _GUIDE_FILES:
        try:
            st = (root / name).stat()
        except OSError:
            marks.append(None)
            continue
        marks.append((st.st_mtime_ns, st.st_size))
    return tuple(marks)


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


def auth_command(*, device_auth: bool = False) -> list[str]:
    """Interactive login command, for ``ciao auth claude`` and the PWA.

    ``device_auth`` has no Claude equivalent and is ignored.
    """
    from ciao.tool_path import resolve_tool

    binary = get_bundled_claude_path() or resolve_tool("claude")
    if not binary:
        raise FileNotFoundError("Claude CLI not found")
    return [binary, "auth", "login"]


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
        path: str = tool_input.get("file_path", "")
        return path
    if name in ("Edit", "edit"):
        path = tool_input.get("file_path") or tool_input.get("path") or ""
        return path
    if name in ("Write", "write"):
        path = tool_input.get("file_path") or tool_input.get("path") or ""
        return path
    # Search
    if name in ("Grep", "grep"):
        pattern: str = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        return f'"{pattern}"' + (f" in {path}" if path else "")
    if name in ("Glob", "glob"):
        pattern = tool_input.get("pattern", "")
        path = tool_input.get("path", "")
        return pattern + (f" in {path}" if path else "")
    # Shell
    if name in ("Bash", "bash"):
        cmd: str = tool_input.get("command", "")
        desc: str = tool_input.get("description", "")
        if desc:
            return desc
        # Truncate long commands
        return cmd[:120] + ("..." if len(cmd) > 120 else "")
    # Agents
    if name in ("Agent", "agent"):
        desc = tool_input.get("description", "")
        subtype: str = tool_input.get("subagent_type", "")
        if subtype and desc:
            return f"[{subtype}] {desc}"
        return desc or subtype
    # Tasks
    if name in ("TaskCreate",):
        return cast(str, tool_input.get("subject", ""))
    if name in ("TaskUpdate",):
        tid = tool_input.get("taskId", "")
        status = tool_input.get("status", "")
        return f"#{tid} → {status}" if status else f"#{tid}"
    # Skills
    if name in ("Skill",):
        return cast(str, tool_input.get("skill", ""))
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
        return cast(str, tool_input.get("query", ""))
    if name in ("WebFetch",):
        return cast(str, tool_input.get("url", ""))[:100]
    # Notebook
    if name in ("NotebookEdit",):
        return cast(str, tool_input.get("cell_id", ""))
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
    if "fable" in low:
        return "opus"
    if "opus" in low:
        return "sonnet"
    if "sonnet" in low:
        return "haiku"
    return None


def _set_event() -> asyncio.Event:
    event = asyncio.Event()
    event.set()
    return event


@dataclass(slots=True)
class SDKHandle(ActiveHandle):
    """Handle wrapping a live SDK client for interruption."""

    client: ClaudeSDKClient
    # The provider's pending-steer set, cleared before interrupting so the
    # post-interrupt ResultMessage ends the turn instead of awaiting echoes.
    pending_steers: set[str] = field(default_factory=set)
    # Set by ``stop()``: closes the provider's steer gate and wakes the grace
    # wait for a follow-up round. Shared with the provider like the set above.
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    # Set once the CLI echoed the turn's own prompt. Unset means the prompt is
    # still queued behind another CLI turn, so Stop must cancel it too.
    prompt_echoed: asyncio.Event = field(default_factory=_set_event)

    async def stop(self) -> None:
        self.stopped.set()
        had_queued = bool(self.pending_steers) or not self.prompt_echoed.is_set()
        self.pending_steers.clear()
        if had_queued:
            # Stop wins over a steer that is still queued: without
            # cancel_queued the CLI runs it as a new turn after the
            # interrupt. The SDK has no public wrapper, so this uses the
            # private control request. It depends on the bundled CLI's
            # ``interrupt_cancel_queued_v1`` capability (the SDK pins the
            # CLI, so there is no capability fallback).
            try:
                reply = await self.client._query._send_control_request(  # type: ignore[union-attr]
                    {"subtype": "interrupt", "cancel_queued": True}
                )
            except Exception:  # noqa: BLE001 - fall through to a plain interrupt
                logger.warning(
                    "Claude cancel-queued interrupt failed; falling back to a plain "
                    "interrupt",
                    exc_info=True,
                )
            else:
                still_queued = reply.get("still_queued") if isinstance(reply, dict) else None
                if still_queued:
                    logger.warning(
                        "Claude Stop left steers queued in the CLI: %s", still_queued
                    )
                return
        try:
            await self.client.interrupt()
        except Exception:  # noqa: BLE001 — best-effort: a hung CLI or a
            # transport torn down under us (stop escalation disconnects the
            # client while this ack is still pending) must never surface as
            # an un-retrieved task exception.
            logger.debug("Claude interrupt failed; client is likely already idle")


async def _receive_round(
    client: ClaudeSDKClient,
    first_timeout: float | None,
    stopped: asyncio.Event | None = None,
) -> AsyncGenerator[Any, None]:
    """Yield one ``receive_response()`` round.

    ``first_timeout`` bounds the wait for the round's first message only, so a
    follow-up turn that never starts cannot hang the Ciaobot turn. It raises
    ``_SteerEchoTimeout`` on expiry. A set ``stopped`` event ends the wait at
    once with an empty round.
    """
    rounds = client.receive_response().__aiter__()
    try:
        try:
            if first_timeout is None and stopped is None:
                first = await rounds.__anext__()
            else:
                first_task = asyncio.ensure_future(rounds.__anext__())
                stop_task = asyncio.ensure_future((stopped or asyncio.Event()).wait())
                try:
                    done, _ = await asyncio.wait(
                        {first_task, stop_task},
                        timeout=first_timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    stop_task.cancel()
                    if not first_task.done():
                        first_task.cancel()
                if first_task not in done:
                    try:
                        await first_task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                    if stopped is not None and stopped.is_set():
                        return
                    raise _SteerEchoTimeout
                first = first_task.result()
        except StopAsyncIteration:
            return
        yield first
        async for msg in rounds:
            yield msg
    finally:
        aclose = getattr(rounds, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # noqa: BLE001 - best-effort close of a cancelled read
                pass


def is_process_task(info: Mapping[str, Any]) -> bool:
    """True for a shell or Monitor task; background agents have their own rows."""
    return "agent" not in str(info.get("task_type") or "")


class ClaudeProvider(BaseSDKProvider):
    """Claude provider backed by the Agent SDK."""

    capabilities = ProviderCapabilities(
        resume=True,
        fork=True,
        images=True,
        stop=True,
        permissions=True,
        structured_questions=True,
        thinking_levels=True,
        usage=True,
        quota=True,
        subagents=True,
        background_subagents=True,
        subagent_messages=True,
        session_history=True,
        schedule_unattended=True,
    )

    def __init__(self, workspace_root: Path, *, config: object | None = None) -> None:
        super().__init__(workspace_root, config=config)
        self._client: ClaudeSDKClient | None = None
        self._connected = False
        # True while a foreground turn is open, so ``steer_turn`` can refuse
        # once it has ended. ``_steers_pending`` holds the uuids of steers sent
        # but not yet echoed back by the CLI; it is cleared in place, never
        # rebound, because ``SDKHandle`` shares the set.
        self._turn_open = False
        self._steers_pending: set[str] = set()
        # Replaced per turn and shared with that turn's ``SDKHandle``; a set
        # event closes the steer gate (Stop wins over a steer).
        self._stop_event = asyncio.Event()
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
        # Tail of the current turn's streamed assistant text, used to detect
        # the CLI's mid-response connection-drop banner even when it arrives as
        # streamed deltas rather than in the terminal result field. Bounded and
        # reset per turn.
        self._turn_text_tail = ""
        # Set by the stderr handler when the CLI refuses to resume because
        # the session is held by a background agent. The next connect
        # attempt re-resumes with ``--fork-session`` to branch a copy.
        self._fork_resume_next = False
        # Hostname a hostless connection error is annotated with.
        self._api_host = _API_HOST
        # Runtime root: state_path.parent on CiaoConfig; fall back to
        # workspace_root/.runtime when config is absent (tests).
        runtime_root = Path(
            getattr(config, "state_path", workspace_root / ".runtime" / "state.json")
        ).parent
        self._rate_limit_store = RateLimitStore(path=default_store_path(runtime_root))
        # One gate per provider. Each turn rebinds ``emit`` to the turn's
        # merge queue so late approvals can't land in a stale stream.
        self._permission_gate = PermissionGate()
        self._mcp_token = ""
        # Filesystem scope and roots the live CLI process was started with.
        self._fs_scope_key: tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]] = ("machine", (), (), ())
        # Spawn-only options the live CLI process was started with.
        self._spawn_key: tuple[object, ...] = ()
        # Fire-and-forget closes of dead clients, held so they are not collected.
        self._closing_tasks: set[asyncio.Future[None]] = set()
        # Background tasks (native ``run_in_background`` Bash, ``Monitor``,
        # background agents) the live CLI process owns, from the SDK's task
        # messages: task_id -> {"command", "task_type", "started_at"}. They die
        # with the CLI, so the provider is busy while any is tracked.
        self._native_tasks: dict[str, dict[str, Any]] = {}
        # Monotonic time of the last SDK message; None until one arrives.
        self._last_activity: float | None = None

    @property
    def background_work_outstanding(self) -> bool:
        """True while the CLI process owns a tracked background task."""
        self._prune_stale_tasks()
        return bool(self._native_tasks)

    def _prune_stale_tasks(self) -> None:
        """Forget tasks older than the cap: one that never got a terminal message."""
        cutoff = time.monotonic() - BACKGROUND_WORK_MAX_S
        for task_id in [
            tid for tid, info in self._native_tasks.items() if info["started_mono"] < cutoff
        ]:
            logger.warning(
                "Dropping background task %s: no terminal message within %.0f h",
                task_id,
                BACKGROUND_WORK_MAX_S / 3600,
            )
            del self._native_tasks[task_id]

    @property
    def seconds_since_activity(self) -> float:
        """Seconds since the last SDK message, ``math.inf`` if none arrived."""
        if self._last_activity is None:
            return math.inf
        return time.monotonic() - self._last_activity

    def background_shell_rows(self) -> list[dict[str, Any]]:
        """``{id, command, task_type, started_at}`` per tracked task, in id order."""
        self._prune_stale_tasks()
        return [
            {
                "id": task_id,
                **{k: v for k, v in self._native_tasks[task_id].items() if k != "started_mono"},
            }
            for task_id in sorted(self._native_tasks)
        ]

    async def stop_background_task(self, task_id: str) -> bool:
        """Ask the CLI to stop one tracked process task; False if not tracked or an agent.

        The row is not dropped here: the CLI reports the terminal status in the
        message stream and ``_observe_message`` clears it then.
        """
        client = self._client
        info = self._native_tasks.get(task_id)
        if info is None or client is None or not is_process_task(info):
            return False
        await client.stop_task(task_id)
        return True

    def _observe_message(self, msg: Any) -> None:
        """Record SDK activity and track background tasks from lifecycle messages.

        Called for every message from the turn and drain loops, before any
        turn filtering: task messages for a shell that started in an earlier
        turn can arrive before this turn's prompt echo.
        """
        self._last_activity = time.monotonic()
        if isinstance(msg, TaskStartedMessage):
            self._native_tasks[msg.task_id] = {
                "command": msg.description,
                "task_type": msg.task_type or "",
                "started_at": time.time(),
                "started_mono": time.monotonic(),
            }
        elif isinstance(msg, TaskNotificationMessage):
            # Any terminal notification status ends the task.
            self._native_tasks.pop(msg.task_id, None)
        elif isinstance(msg, TaskUpdatedMessage):
            if (msg.patch or {}).get("status") in TERMINAL_TASK_STATUSES:
                self._native_tasks.pop(msg.task_id, None)

    def _stderr_handler(self, line: str) -> None:
        _route_cli_stderr(line)
        if "is currently running as a background agent" in line:
            self._fork_resume_next = True

    @property
    def permission_gate(self) -> PermissionGate:
        """Expose the gate so route handlers can deliver user replies."""
        return self._permission_gate

    @property
    def current_session_id(self) -> str | None:
        """Session id as currently known to the provider (may be set mid-stream)."""
        return self._session_id

    async def read_mcp_status(self) -> dict[str, str]:
        """Read MCP status from this already-connected Claude SDK client."""
        client = self._client
        if client is None or not self._connected:
            return {}
        payload = await asyncio.wait_for(client.get_mcp_status(), timeout=2.0)
        rows = payload.get("mcpServers") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError("Claude MCP status response is malformed")
        allowed = {"connected", "failed", "disabled", "pending", "needs-auth"}
        result: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            state = str(row.get("status") or "")
            if name and state in allowed:
                result[name] = "needs_auth" if state == "needs-auth" else state
        return result

    def _log_client_turn(self, request: AgentRequest, *, reused: bool, reason: str) -> None:
        """One line per turn: whether the live SDK client was reused or replaced, and why."""
        chat = (request.extra_env or {}).get("CIAO_CHAT_ID") or request.resume_session or "-"
        logger.info(
            "claude turn: client=%s reason=%s chat=%s",
            "reused" if reused else "new",
            reason,
            chat,
        )

    def _spawn_key_for(self, request: AgentRequest) -> tuple[object, ...]:
        """Options fixed when the CLI starts that have no in-place setter.

        CIAO_MODEL is left out: a model change is applied with set_model.
        The guide fingerprint makes a guide or memory edit reach the model,
        as it did when every turn started a fresh CLI.
        The linked repo is an add-dir, fixed at spawn.
        """
        env = tuple(sorted(
            (k, v) for k, v in (request.extra_env or {}).items() if k != "CIAO_MODEL"
        ))
        return (
            request.thinking_level,
            tuple(request.disallowed_tools or ()),
            env,
            _guide_fingerprint(Path(self.workspace_root)),
            linked_repo_of(request),
        )

    def _warn_tasks_stopped(self, reason: str) -> None:
        """Log the background tasks a reconnect for ``reason`` is about to stop."""
        if not self._native_tasks:
            return
        logger.warning(
            "Reconnecting the Claude CLI (%s) stops %d background task(s): %s",
            reason,
            len(self._native_tasks),
            "; ".join(
                f"{task_id}: {str(info.get('command') or '')[:80]}"
                for task_id, info in sorted(self._native_tasks.items())
            ),
        )

    async def _ensure_connected(self, request: AgentRequest) -> ClaudeSDKClient:
        requested_model = request.model
        # Whether a client object existed before this turn. Used to tell a
        # client that died (``_connected`` reset) from one never built.
        had_client = self._client is not None
        # Why the live client is being replaced; empty while it is reused.
        reconnect_reason = ""
        if self._client is not None and self._connected and _cli_process_exited(self._client):
            # The CLI exited between turns; writing to it would fail.
            reconnect_reason = "dead_process"
            await self.disconnect()  # also drops the dead CLI's tracked tasks
        if (
            self._client is not None
            and self._connected
            and (
                provider_reuse_key(request) != self._mcp_token
                or (request.agent_fs_scope, request.agent_roots, request.agent_denied, request.agent_denied_except) != self._fs_scope_key
            )
        ):
            # MCP configuration, the shell environment and the filesystem
            # sandbox are fixed when the managed CLI process starts. Reconnect
            # when a chat switches surfaces, receives a refreshed ephemeral
            # token (on either surface), or its workspace scope changes;
            # model/mode alone can still change in place.
            reconnect_reason = "other"
            self._warn_tasks_stopped(reconnect_reason)
            await self.disconnect()
        if (
            self._client is not None
            and self._connected
            and self._spawn_key_for(request) != self._spawn_key
        ):
            if self.background_work_outstanding:
                # A reconnect would kill the background tasks the CLI owns.
                # ``_spawn_key`` is left stale so the change applies on the
                # first turn after they end (same as the memory-prune
                # deferral in ProviderService, #1391).
                logger.info(
                    "Deferring spawn_options reconnect: %d background task(s) running",
                    len(self._native_tasks),
                )
            else:
                reconnect_reason = "spawn_options"
                await self.disconnect()
        if (
            self._client is not None
            and self._connected
            and request.resume_session
            and request.resume_session != self._session_id
        ):
            reconnect_reason = "resume"
            self._warn_tasks_stopped(reconnect_reason)
            await self.disconnect()

        if self._client is not None and self._connected:
            if requested_model != self._current_model:
                try:
                    await self._client.set_model(requested_model)
                    self._current_model = requested_model
                except (*_CLAUDE_OP_ERRORS, asyncio.TimeoutError) as exc:
                    logger.warning("Claude set_model failed (%s); reconnecting", exc)
                    reconnect_reason = "model_change"
                    self._warn_tasks_stopped(reconnect_reason)
                    await self.disconnect()
            if self._client is not None and self._connected and request.mode != self._current_mode:
                try:
                    await self._client.set_permission_mode(_sdk_permission_mode(request.mode))
                    self._current_mode = request.mode
                except Exception as exc:  # noqa: BLE001 - control-request errors are bare Exception
                    logger.warning("Claude set_permission_mode failed (%s); reconnecting", exc)
                    logger.debug("set_permission_mode failure detail", exc_info=True)
                    reconnect_reason = "mode_change"
                    self._warn_tasks_stopped(reconnect_reason)
                    await self.disconnect()
        if self._client is not None and self._connected:
            self._log_client_turn(request, reused=True, reason="none")
            return self._client

        if not reconnect_reason:
            if had_client:
                reconnect_reason = "dead_process"
            elif request.resume_session:
                reconnect_reason = "resume"
            else:
                reconnect_reason = "first_turn"
        self._log_client_turn(request, reused=False, reason=reconnect_reason)

        resume_session = self._validated_resume_session(request.resume_session)
        from ciao.tool_path import resolve_tool
        system_cli = get_bundled_claude_path() or resolve_tool("claude")
        if not system_cli:
            raise FileNotFoundError("Claude Code CLI not found (no bundled binary and not on the login-shell PATH).")
        logger.info("Using Claude Code CLI: %s", system_cli)


        system_prompt = system_prompt_payload("")

        sandbox: SandboxSettings | None = None
        add_dirs: list[str | Path] = []
        file_tool_hooks: list[HookMatcher] = []
        cli_env = dict(request.extra_env or {})
        if request.agent_fs_scope == "workspace":
            if sys.platform == "win32":
                raise FsSandboxUnavailable(
                    "workspace filesystem sandbox is not available on Windows; "
                    "set the workspace to whole machine"
                )
            roots = [Path(root) for root in request.agent_roots]
            sandbox_dict: dict[str, Any] | None = claude_sandbox_settings(
                scope=request.agent_fs_scope,
                mode=request.mode,
                roots=roots,
                read_only=[Path(p) for p in request.agent_read_only],
                denied=[Path(p) for p in request.agent_denied],
                regrant=[Path(p) for p in request.agent_denied_except],
            )
            sandbox = cast(SandboxSettings, sandbox_dict)
            agent_root, *other_roots = request.agent_roots
            add_dirs = [
                root for root in other_roots if not Path(root).is_relative_to(agent_root)
            ]
            # The sandbox covers Bash only; the CLI's own file tools are
            # confined to the same roots by this hook.
            file_tool_hooks = [HookMatcher(
                matcher="|".join(CLAUDE_FILE_TOOL_PATH_KEYS),
                hooks=[claude_file_tool_guard(
                    cwd=Path(self.workspace_root),
                    roots=roots,
                    cli_tmp_base=cli_env.get("CLAUDE_CODE_TMPDIR")
                    or os.environ.get("CLAUDE_CODE_TMPDIR"),
                )],
            )]
        # The linked repository is added in every scope: it is what loads the
        # repo's .claude/skills and .claude/commands. Workspace scope may already
        # list it among the roots above. It is added even inside the workspace
        # root, because the CLI only discovers skills from cwd and its parents.
        linked_repo = linked_repo_of(request)
        if linked_repo and linked_repo not in {str(d) for d in add_dirs}:
            add_dirs.append(linked_repo)
        self._fs_scope_key = (request.agent_fs_scope, request.agent_roots, request.agent_denied, request.agent_denied_except)

        # The SDK reads the initialize deadline from os.environ when it builds
        # the client, so it has to be set before ClaudeSDKClient() below.
        # setdefault: an operator value for the same SDK knob wins.
        os.environ.setdefault(_STREAM_CLOSE_TIMEOUT_ENV, str(_INITIALIZE_TIMEOUT_MS))
        # A startup the CLI refuses should arrive as a result message naming
        # the reason, not as stderr-only silence that the SDK reports as a
        # control-request timeout with no cause.
        cli_env.setdefault(_STARTUP_FAILURE_RESULTS_ENV, "1")

        options = ClaudeAgentOptions(
            model=requested_model,
            fallback_model=_fallback_model_for(requested_model),
            permission_mode=_sdk_permission_mode(request.mode),
            cwd=str(self.workspace_root),
            sandbox=sandbox,
            add_dirs=add_dirs,
            include_partial_messages=True,
            # Raise the CLI-stdout decode buffer above the SDK's 1 MiB default
            # so a single large tool result / content block doesn't kill the
            # stream. See _SDK_MAX_BUFFER_BYTES and issue #137.
            max_buffer_size=_SDK_MAX_BUFFER_BYTES,
            env=cli_env,
            # Per-workspace tool denylist (e.g. the default harness set, or
            # ``Bash``). Empty list = no denylist applied.
            disallowed_tools=list(request.disallowed_tools or []),
            # Agents are discovered from .claude/agents/ via setting_sources
            # below. No manual frontmatter parsing needed.
            setting_sources=["user", "project", "local"],
            # Inline settings layer (highest precedence, additive — it does
            # NOT replace the sources above; verified against the bundled CLI:
            # all 68 workspace skills and 8 agents still resolve with this
            # set). Used to drop the bundled `schedule` / `loop` skills out of
            # the model's context entirely, since Ciaobot's own schedules and
            # automations supersede them. See HARNESS_DISABLED_SKILLS.
            settings=json.dumps({"skillOverrides": harness_skill_overrides()}),
            # The request already carries the provider-neutral context capsule.
            # Keep hooks for process behavior only; injecting context here as
            # well would duplicate it and make prompt caching less effective.
            hooks={
                # Native run_in_background Bash and Monitor are tracked and
                # woken by the CLI, and the provider stays alive while they
                # run. Only detached launches (nohup … &, setsid, disown) are
                # denied: the CLI cannot track them.
                "PreToolUse": [HookMatcher(
                    matcher="Bash",
                    hooks=[build_detached_shell_deny_hook()],
                ), *file_tool_hooks],
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
            # Echo each consumed user message back with the uuid we sent: the
            # delivery signal for ``steer_turn`` (the CLI emits it when the
            # model actually reads the message).
            extra_args={"replay-user-messages": None},
        )
        if request.thinking_level:
            # SDK-native effort knob (low/medium/high/xhigh/max). Unset keeps
            # the SDK's adaptive-thinking default.
            options.effort = request.thinking_level  # type: ignore[assignment]
        if system_prompt is not None:
            # system_prompt_payload builds a SystemPromptPreset-shaped dict.
            options.system_prompt = cast(SystemPromptPreset, system_prompt)
        # Every chat is on the CLI surface since S6: there is no Ciaobot MCP
        # server to attach, and the control-plane capability is carried by
        # `ciao <noun> <verb>` in the shell (CIAO_AGENT_TOKEN/URL in env). No
        # `ciao …` argv prefix is pre-approved: an allow rule is a prefix that a
        # shell suffix (``ciao help >/dev/null; <cmd>``) could ride past, and
        # auto mode must keep a card on shell commands. Users who want no cards
        # switch to `bypass` mode.
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
        self._mcp_token = provider_reuse_key(request)
        self._spawn_key = self._spawn_key_for(request)
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

    def _prompt_payload(self, request: AgentRequest, prompt_uuid: str):
        # An async iterable of message dicts: ``query()`` writes the content
        # blocks and uuid fields per message, which a plain string cannot
        # carry (images, steer uuids). It is sent with ``query()``, never
        # passed to ``connect()`` (#1402).
        if request.compact:
            # The SDK compacts only for the bare command: a context capsule
            # or any other text before it turns it into a normal reply.
            return build_claude_message_stream(
                replace(request, prompt=COMPACT_COMMAND, images=[]), uuid=prompt_uuid
            )
        return build_claude_message_stream(request, uuid=prompt_uuid)

    async def steer_turn(self, request: AgentRequest) -> str | None:
        """Inject a user message into the running foreground turn.

        Returns the delivery id (the uuid the CLI echoes once the model has
        consumed the message), or None when no foreground turn is open. Images
        ride along as the same content blocks a normal prompt carries.
        """
        if (
            self._client is None
            or not self._connected
            or not self._turn_open
            or self._stop_event.is_set()
        ):
            return None
        delivery_id = str(uuid.uuid4())
        # Recorded before the await: the turn loop may read the echo before
        # ``query()`` returns.
        self._steers_pending.add(delivery_id)
        try:
            await self._client.query(build_claude_message_stream(request, uuid=delivery_id))
        except BaseException as exc:
            # Cancelled or failed sends must not leave a uuid the turn would
            # wait the full grace period for.
            self._steers_pending.discard(delivery_id)
            if isinstance(exc, _CLAUDE_OP_ERRORS):
                return None
            raise
        return delivery_id

    async def disconnect(self) -> None:
        # Take the client out of the slot *before* awaiting its close, so
        # exactly one caller owns the close.
        #
        # The SDK's `disconnect()` is not idempotent: it clears its query only
        # after the await, and `Query._close_impl()` has no `_closed` guard, so
        # two callers both run the teardown and both call
        # `transport.close()`. That transport's `close()` guards
        # `if not self._process: return` only on entry, so two concurrent
        # closes both pass the guard; the first then clears `_process` and the
        # second reads `returncode` off None and raises
        # `AttributeError: 'NoneType' object has no attribute 'returncode'`.
        #
        # Two callers met because the engine's teardown disconnects the
        # provider while a fire-and-forget close (the chat-cleanup disconnects
        # in ciao/web/project_chats.py) is still in flight, and because the
        # slot used to be cleared only after the await, so the second caller
        # read the same client out of it.
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except _CLAUDE_OP_ERRORS:
                logger.debug("Claude disconnect failed; dropping client anyway")
        self._connected = False
        self._turn_open = False
        self._steers_pending.clear()
        self._session_id = None
        self._pending_quota = {}
        self._mcp_token = ""
        self._spawn_key = ()
        self._native_tasks.clear()
        self._reset_settings()

    def _forget_dead_cli(self, client: object) -> None:
        """Clear client state after ``client``'s CLI process died.

        disconnect() minus the awaited client call: the drain consumer calls
        this from inside its own task, where awaiting disconnect() deadlocks.
        The dead client is closed in a separate task so its transport and
        query tasks are released. Does nothing when ``client`` is no longer
        the live one: a reconnect already replaced it, and its state must
        survive a stale drain ending late.
        """
        if self._client is not client:
            return
        # Clear the slot before the close starts, so this and disconnect() are
        # both single-owner: whichever takes the client out of the slot owns
        # closing it, and the other finds nothing to close.
        self._client = None
        task = asyncio.ensure_future(self._close_dead_client(client))
        self._closing_tasks.add(task)
        task.add_done_callback(self._closing_tasks.discard)
        self._connected = False
        self._session_id = None
        self._pending_quota = {}
        self._mcp_token = ""
        self._spawn_key = ()
        self._native_tasks.clear()
        self._reset_settings()

    @staticmethod
    async def _close_dead_client(client: object) -> None:
        try:
            await client.disconnect()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - best-effort cleanup of a dead CLI
            logger.debug("Closing the dead Claude client failed", exc_info=True)

    @property
    def can_drain(self) -> bool:
        """True when a connected client exists to drain between turns."""
        return self._client is not None and self._connected

    @property
    def cli_connected(self) -> bool:
        """True while the persistent CLI subprocess this provider owns is alive."""
        return self._client is not None and self._connected

    async def drain_events(self) -> AsyncGenerator[StreamEvent, None]:
        """Yield SDK events arriving *between* turns until cancelled.

        While the client is connected and idle, a finished background `Agent`
        or background Bash makes the CLI start a follow-up turn by itself: a
        task notification, then a fresh assistant turn that ends in a
        `ResultMessage` whose `origin` kind is `task-notification`. There is
        one such turn per completion, sometimes preceded by an empty `turns=0`
        result (verified 2026-10-15 on the CLI bundled with SDK 0.2.165,
        #1394). No prompt is sent for it. Left unread, those messages sit in
        the transport buffer, and the stale `ResultMessage` would end the next
        `receive_response()` immediately, bleeding one turn's output into the
        next. This generator keeps the pipe drained and hands the events to
        the caller for live display; `ResultEvent.origin` marks the follow-up
        results.

        The CLI stays alive between turns (#1402), so this generator normally
        runs until cancelled. It ends by itself only when the CLI exits, and
        then clears the client state so the next turn reconnects.

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
                    self._observe_message(msg)
                    for event in self._convert_message(msg):
                        merged.put_nowait(event)
            except (CLIConnectionError, ProcessError):
                logger.debug("Between-turns drain ended on SDK error", exc_info=True)
                # The transport is gone (the CLI process died between turns).
                # Only these two mean a dead process: ClaudeSDKError also
                # covers MessageParseError, which is a bad payload from a
                # still-alive CLI and must not kill the connection. Mirror
                # disconnect()'s state clearing minus the awaited client
                # call — we are inside the consumer task, so a disconnect()
                # await here would deadlock the drain. _ensure_connected
                # rebuilds the client on the next turn, exactly as after a
                # disconnect(); leaving _connected True would make
                # cli_connected lie to the CLI-task watcher.
                self._forget_dead_cli(client)
            except _CLAUDE_OP_ERRORS:
                # Other typed SDK errors (e.g. MessageParseError) are not
                # transport death; end the drain, keep the client usable.
                logger.debug("Between-turns drain ended on SDK error", exc_info=True)
            except Exception:
                # The SDK raises bare ``Exception`` for some result shapes
                # (e.g. result_type=user, stop_reason=tool_use) that aren't
                # subclasses of the typed SDK errors. Log and end the drain
                # cleanly so the consumer task doesn't finish with an
                # un-retrieved exception (which asyncio logs noisily as
                # "Task exception was never retrieved").
                logger.debug("Between-turns drain ended on unhandled SDK result", exc_info=True)
            else:
                # A normal end of receive_messages() is the transport hitting
                # EOF. The CLI stays alive between turns, so only a dead CLI
                # produces it.
                logger.info("Claude CLI exited between turns; next turn reconnects")
                self._forget_dead_cli(client)
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
            # Always retrieve the consumer's result/exception so asyncio does
            # not warn "Task exception was never retrieved" when the drain
            # ended on an error before the generator was closed.
            try:
                await consumer
            except (asyncio.CancelledError, *_CLAUDE_OP_ERRORS):
                pass
            except Exception:
                logger.debug("Between-turns drain task ended with error", exc_info=True)

    async def _capture_catalog_evidence(
        self, client: ClaudeSDKClient, *, generation: int | None = None
    ) -> None:
        from ciao.providers.catalog_evidence import invalidate_claude_catalog, record_claude_models

        root = Path(self.workspace_root)
        try:
            record_claude_models(
                root, await client.get_server_info(), expected_generation=generation
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            invalidate_claude_catalog(root)

    async def run_streaming(
        self,
        request: AgentRequest,
        register_handle: Callable[[ActiveHandle | None], None],
    ) -> AsyncGenerator[StreamEvent, None]:
        # Pre-stream connect/query failures (e.g. "ProcessTransport is not
        # ready for writing" when the CLI exits in the instant after
        # _ensure_connected checked it, or an initialize handshake that missed
        # its deadline on a cold, loaded CLI) are rare... They are safe to
        # retry once: no events were yielded, the session_id on the chat is
        # preserved, and _ensure_connected will build a fresh client on the
        # retry because _connected was just reset in _run_streaming_once's
        # except block. Both retries share the single ``attempt`` budget, so a
        # machine that is failing every spawn does not loop.
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
            except ClaudeInitializeTimeout as exc:
                if not events_yielded and attempt == 0:
                    # A cold CLI start under load can outrun the initialize
                    # deadline. A second spawn usually lands warm, and no
                    # event has been yielded, so nothing is duplicated.
                    logger.warning(
                        "Claude CLI did not answer initialize (%s); retrying once",
                        exc,
                    )
                    attempt += 1
                    continue
                if request.resume_session:
                    logger.error(
                        "Claude CLI did not answer initialize for session %s after "
                        "the retry; raising instead of silently starting a fresh "
                        "session",
                        request.resume_session,
                    )
                raise
            except CLIConnectionError as exc:
                if not events_yielded and attempt == 0:
                    # The CLI died between the liveness check and the write.
                    # The retry builds a fresh client in _ensure_connected
                    # (no events were yielded). Logged at debug.
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
        prompt_uuid = str(uuid.uuid4())
        payload = self._prompt_payload(request, prompt_uuid)
        prompt_echoed = asyncio.Event()

        # Fresh turn: zero the live token counters so the running total we
        # emit over the wire starts from this turn, not the previous one.
        self._turn_input_tokens = 0
        self._turn_output_committed = 0
        self._cur_msg_output = 0
        self._turn_text_tail = ""

        # Register handle *before* connect/query so /stop can interrupt a
        # hanging SDK call — otherwise the bot locks up with "No active run".
        self._stop_event = asyncio.Event()
        register_handle(SDKHandle(
            client=client,
            pending_steers=self._steers_pending,
            stopped=self._stop_event,
            prompt_echoed=prompt_echoed,
        ))

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
                    # No prompt: the SDK then never starts stream_input, which
                    # closes stdin once the run goes idle and makes the CLI
                    # exit (#1402).
                    # Only an initialize handshake that missed its deadline is
                    # worth a fresh spawn; every other control-request failure
                    # is a real error for this turn.
                    catalog_generation = _catalog_account_generation()
                    await _connect_with_deadline(client)
                    self._connected = True
                    await self._capture_catalog_evidence(client, generation=catalog_generation)
                await asyncio.wait_for(client.query(payload), timeout=120)

                pending_result: ResultEvent | None = None
                last_result_msg: ResultMessage | None = None
                # The main agent's most recent model call. Its usage is the
                # size of the context that call read, which is what the
                # context % must measure. ResultMessage.usage sums every call
                # in the turn and overstates it on tool-heavy turns.
                last_main_msg: AssistantMessage | None = None
                if not request.compact:
                    self._turn_open = True
                first_timeout: float | None = None
                # The first round is a plain read; later rounds can be woken by Stop.
                wake: asyncio.Event | None = None
                while True:
                    got_message = False
                    try:
                        async for msg in _receive_round(client, first_timeout, wake):
                            got_message = True
                            self._observe_message(msg)
                            if isinstance(msg, UserMessage) and msg.uuid == prompt_uuid:
                                prompt_echoed.set()
                                # Output from here on is this turn's: drop whatever
                                # a preceding CLI turn added to the live counters.
                                self._turn_input_tokens = 0
                                self._turn_output_committed = 0
                                self._cur_msg_output = 0
                                self._turn_text_tail = ""
                                continue
                            if not prompt_echoed.is_set():
                                # A CLI turn that ran before ours: a background
                                # follow-up, or one left by a failed orphan cancel or
                                # a Stop. It is not this turn's output (#1406).
                                if isinstance(msg, SystemMessage) and msg.subtype == "init":
                                    await self._record_bundled_skills(client, msg.data or {})
                                elif (
                                    isinstance(msg, SystemMessage)
                                    and msg.subtype in ("status", "compact_boundary")
                                ):
                                    # Session-wide status: a /compact prompt is
                                    # echoed only after the compaction it ran.
                                    for event in self._convert_message(msg):
                                        merged.put_nowait(event)
                                elif isinstance(msg, RateLimitEvent):
                                    self._convert_message(msg)
                                elif isinstance(msg, ResultMessage):
                                    logger.info(
                                        "Claude turn skipped a result that preceded its "
                                        "prompt (origin=%s) for session %s",
                                        (getattr(msg, "origin", None) or {}).get("kind"),
                                        self._session_id,
                                    )
                                continue
                            if (
                                isinstance(msg, UserMessage)
                                and msg.uuid in self._steers_pending
                            ):
                                self._steers_pending.discard(msg.uuid)
                                merged.put_nowait(SteerDeliveredEvent(
                                    type="steer_delivered", delivery_id=msg.uuid or "",
                                ))
                                continue
                            if isinstance(msg, ResultMessage):
                                last_result_msg = msg
                            elif (
                                isinstance(msg, AssistantMessage)
                                and msg.parent_tool_use_id is None
                                and self._call_context_tokens(msg.usage) > 0
                            ):
                                # Zero-usage messages are synthetic (API error or
                                # connection-drop notices, model "<synthetic>"); keep
                                # the last real call rather than clobbering it.
                                last_main_msg = msg
                            elif isinstance(msg, SystemMessage) and msg.subtype == "init":
                                await self._record_bundled_skills(client, msg.data or {})
                            for event in self._convert_message(msg):
                                if isinstance(event, ResultEvent):
                                    # Our prompt ran inside this CLI turn, so it is
                                    # ours even when a follow-up started it.
                                    event.origin = ""
                                    pending_result = event
                                else:
                                    merged.put_nowait(event)
                    except _SteerEchoTimeout:
                        logger.warning(
                            "Claude steer echo not seen within %.0fs for session %s; "
                            "ending the turn",
                            _STEER_ECHO_GRACE_S,
                            self._session_id,
                        )
                        # Close the steer gate first: no steer may be written
                        # while the cancels below are awaited.
                        self._turn_open = False
                        # The steer is still queued in the CLI; cancel it so it
                        # cannot run later as an orphan turn the host also
                        # re-queues. Requires the bundled CLI's
                        # ``cancel_async_message`` control request. Bounded so
                        # a hung CLI cannot hold the turn open.
                        orphans = list(self._steers_pending)

                        async def _cancel_orphans() -> None:
                            for orphan in orphans:
                                try:
                                    reply = await client._query._send_control_request(  # type: ignore[union-attr]
                                        {
                                            "subtype": "cancel_async_message",
                                            "message_uuid": orphan,
                                        }
                                    )
                                except Exception:  # noqa: BLE001 - best effort
                                    logger.warning(
                                        "Could not cancel queued steer %s",
                                        orphan,
                                        exc_info=True,
                                    )
                                    continue
                                if not (isinstance(reply, dict) and reply.get("cancelled") is True):
                                    logger.warning(
                                        "Queued steer %s was not cancelled: %s",
                                        orphan,
                                        reply,
                                    )

                        try:
                            await asyncio.wait_for(
                                _cancel_orphans(), timeout=_STEER_CANCEL_TIMEOUT_S
                            )
                        except asyncio.TimeoutError:
                            logger.warning(
                                "Timed out cancelling queued steers %s", orphans
                            )
                        self._steers_pending.clear()
                    if not got_message:
                        # The CLI stream has ended (or Stop woke the wait):
                        # later rounds would return at once, so stop waiting
                        # for echoes that can no longer arrive.
                        self._steers_pending.clear()
                    elif not prompt_echoed.is_set() and not self._stop_event.is_set():
                        # That round was another CLI turn; ours is still queued.
                        wake = self._stop_event
                        continue
                    if not self._steers_pending:
                        # Closed in the same step that decides to end, before
                        # the post-result awaits, so no steer is written with
                        # nothing left reading.
                        self._turn_open = False
                        break
                    # The CLI runs a steer that lands as the turn finishes as a
                    # separate follow-up turn (a second ResultMessage). Keep
                    # reading so it stays inside this one Ciaobot turn.
                    first_timeout = _STEER_ECHO_GRACE_S
                    wake = self._stop_event

                if pending_result is not None:
                    pending_result.effective_model = self._turn_effective_model(
                        last_result_msg, last_main_msg
                    )
                    await self._augment_with_context_pct(
                        client,
                        pending_result,
                        last_result_msg,
                        last_main_msg,
                        requested_model=request.model,
                    )
                    # A mid-response connection drop is emitted by the CLI as
                    # assistant text and may come back as a *non-error* terminal
                    # result. Re-flag it as an error (and make sure the banner
                    # text is on the result) so the orchestration layer can
                    # auto-resume instead of silently ending the turn.
                    if (
                        _is_connection_drop_text(pending_result.result)
                        or _is_connection_drop_text(self._turn_text_tail)
                    ):
                        if not pending_result.is_error:
                            logger.warning(
                                "Claude turn ended on a mid-response connection "
                                "drop reported as success for session %s; "
                                "re-flagging as a retryable error",
                                self._session_id,
                            )
                        pending_result.is_error = True
                        if not _is_connection_drop_text(pending_result.result):
                            pending_result.result = _CONNECTION_DROP_MESSAGE
                    merged.put_nowait(pending_result)
                elif self._stop_event.is_set() and not prompt_echoed.is_set():
                    # Stopped before our prompt ran: nothing of this turn was
                    # shown. An empty error result is what the host turns
                    # into a stopped turn. Stop cancelled the queued prompt, so
                    # the host keeps it for the user to resend (a compaction is
                    # a command, not a message).
                    if not request.compact:
                        merged.put_nowait(PromptCancelledEvent(type="prompt_cancelled"))
                    merged.put_nowait(ResultEvent(
                        type="result", result="", session_id=self._session_id,
                        is_error=True,
                    ))
                else:
                    # receive_response() completed *normally* but never yielded
                    # a terminal ResultMessage — the stream closed mid-turn.
                    # (An SDK error would have raised instead and be re-raised
                    # via consumer.exception() below.) Emit an explicit error
                    # result so the turn ends visibly and the session id is
                    # preserved for a resume, rather than stopping silently.
                    logger.warning(
                        "Claude turn ended without a ResultMessage (stream closed "
                        "mid-turn); surfacing an interruption error for session %s",
                        self._session_id,
                    )
                    merged.put_nowait(ResultEvent(
                        type="result",
                        result=_TURN_INTERRUPTED_MESSAGE,
                        session_id=self._session_id,
                        is_error=True,
                    ))
            finally:
                self._turn_open = False
                self._steers_pending.clear()
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
                if _is_oversized_message_error(exc):
                    # The SDK reader hit a single JSON message larger than the
                    # decode buffer (or otherwise undecodable) and raised a
                    # fatal error that would kill the chat stream. Surface a
                    # recoverable error result — session id preserved for a
                    # retry/resume — instead of an opaque stack trace. See #137.
                    sid = self._session_id
                    logger.error(
                        "Claude turn hit an oversized/undecodable message for "
                        "session %s; surfacing a recoverable error: %s",
                        sid,
                        exc,
                    )
                    try:
                        await self.disconnect()
                    except Exception:  # noqa: BLE001 — best-effort teardown
                        pass
                    yield ResultEvent(
                        type="result",
                        result=_OVERSIZED_MESSAGE,
                        session_id=sid,
                        is_error=True,
                    )
                    return
                raise exc
        except _CLAUDE_OP_ERRORS:
            try:
                await self.disconnect()
            except Exception:
                pass
            raise
        except asyncio.TimeoutError:
            logger.error(
                "Claude query timed out after 120s; the connect step is "
                "bounded separately by ClaudeInitializeTimeout"
            )
            try:
                await self.disconnect()
            except Exception:
                pass
            raise
        except ClaudeInitializeTimeout:
            # Drop the client so the retry in ``run_streaming`` builds a fresh
            # CLI instead of reusing the one that never answered.
            try:
                await self.disconnect()
            except Exception:  # noqa: BLE001 — best-effort teardown
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
        client: ClaudeSDKClient,
        event: ResultEvent,
        result_msg: ResultMessage | None = None,
        last_main_msg: AssistantMessage | None = None,
        *,
        requested_model: str | None = None,
    ) -> None:
        """Attach the context-window occupancy after the turn to ``event.usage``.

        Prefers the CLI's authoritative ``get_context_usage()``. When that is
        unavailable, estimates from the main agent's last model call over the
        chat model's window (see :meth:`_estimate_context_pct`). When neither
        yields a trustworthy number, ``context_pct`` is left off: the footer
        omits it rather than showing a wrong value.

        The request is bounded to ``_CONTEXT_USAGE_TIMEOUT_S``: the first turn
        after any reconnect can take up to 3 s here and then shows the
        estimated percentage.
        """
        try:
            usage = await asyncio.wait_for(
                client.get_context_usage(), timeout=_CONTEXT_USAGE_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            usage = None
            logger.debug(
                "get_context_usage exceeded %.0fs; using the local estimate",
                _CONTEXT_USAGE_TIMEOUT_S,
            )
        except _CLAUDE_OP_ERRORS:
            usage = None  # fall through to local estimate
            logger.debug("get_context_usage failed; trying local estimate")
        except Exception:  # noqa: BLE001 — defensive against SDK surprises
            usage = None
            logger.debug("get_context_usage raised unexpectedly", exc_info=True)
        if isinstance(usage, dict):
            pct = usage.get("percentage")
            if isinstance(pct, (int, float)) and not isinstance(pct, bool):
                event.usage = {**event.usage, "context_pct": f"{pct:.1f}%"}
                return
            total = usage.get("totalTokens")
            mx = usage.get("maxTokens") or usage.get("rawMaxTokens")
            if isinstance(total, (int, float)) and isinstance(mx, (int, float)) and mx > 0:
                event.usage = {
                    **event.usage,
                    "context_pct": f"{min(100.0, float(total) / float(mx) * 100):.1f}%",
                }
                return
        estimate = ClaudeProvider._estimate_context_pct(
            result_msg, last_main_msg, requested_model=requested_model
        )
        if estimate is not None:
            event.usage = {**event.usage, "context_pct": f"{estimate:.1f}%"}

    @staticmethod
    def _estimate_context_pct(
        result_msg: ResultMessage | None,
        last_main_msg: AssistantMessage | None,
        *,
        requested_model: str | None = None,
    ) -> float | None:
        """Context % from the last model call, or None when it is not reliable.

        The context a call occupies is the prompt it read: ``input_tokens +
        cache_creation_input_tokens + cache_read_input_tokens`` of that one
        call. The same sum the CLI uses for ``get_context_usage``. The turn's
        ``ResultMessage.usage`` cannot stand in for it: it adds up every call
        of the tool loop, each of which re-reads the cached prompt, so a
        tool-heavy turn reports several windows' worth of tokens.

        The window comes from the ``model_usage`` entry of the model that made
        that call. ``model_usage`` also lists side calls (a haiku title or
        classifier call with a 200k window), so the first entry is not the
        chat model. ``requested_model`` is the id the chat asked the CLI for;
        it tells the 200k and ``[1m]`` variants of one model apart.
        """
        call_usage: dict[str, Any] | None = None
        call_model = ""
        if last_main_msg is not None and isinstance(last_main_msg.usage, dict):
            call_usage = last_main_msg.usage
            call_model = last_main_msg.model or ""
        elif result_msg is not None and isinstance(result_msg.usage, dict):
            # ``usage.iterations`` breaks down the final API response; its last
            # entry is that call's own usage, not a turn aggregate.
            iterations = result_msg.usage.get("iterations")
            if isinstance(iterations, list) and iterations and isinstance(iterations[-1], dict):
                call_usage = iterations[-1]
        if call_usage is None:
            return None
        if not call_model and result_msg is not None:
            call_model = ClaudeProvider._extract_effective_model(result_msg)
        context_tokens = ClaudeProvider._call_context_tokens(call_usage)
        if context_tokens <= 0:
            return None
        window = ClaudeProvider._context_window_for_model(
            result_msg.model_usage if result_msg is not None else None,
            call_model,
            requested_model,
        )
        if not window:
            return None
        pct = context_tokens / window * 100
        # One call cannot read more than its window. A larger figure means the
        # window was matched to the wrong model, so the number is not shown.
        if pct > 100.0:
            return None
        return pct

    @staticmethod
    def _call_context_tokens(usage: Any) -> int:
        """Prompt tokens one model call read (input + cache write + cache read)."""
        if not isinstance(usage, dict):
            return 0
        total = 0
        for key in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                total += value
        return total

    @staticmethod
    def _context_window_for_model(
        model_usage: Any, model: str, requested_model: str | None = None
    ) -> int | None:
        """The ``contextWindow`` of ``model``'s entry in ``model_usage``.

        Entries are keyed by the requested id (``claude-opus-5-5[1m]``) while
        the assistant message names the served model (``claude-opus-5-5``), so
        both the key and ``canonicalModel`` are compared with any ``[...]``
        suffix removed. Without a model to match, only an unambiguous single
        entry is used.

        The assistant message drops the suffix, so when the standard and
        ``[1m]`` variants of one model are both listed, the match alone cannot
        say which window the call used. The requested id settles it when it
        is one of the listed keys; otherwise disagreeing windows yield None.
        """
        if not isinstance(model_usage, dict):
            return None

        def _base(name: object) -> str:
            return str(name or "").split("[", 1)[0].strip().lower()

        windows: list[int] = []
        matched: dict[str, int] = {}
        target = _base(model)
        for key, entry in model_usage.items():
            if not isinstance(entry, dict):
                continue
            window = entry.get("contextWindow")
            if not isinstance(window, (int, float)) or isinstance(window, bool) or window <= 0:
                continue
            if target and target in {_base(key), _base(entry.get("canonicalModel"))}:
                windows.append(int(window))
                matched[str(key).strip().lower()] = int(window)
            elif not target:
                windows.append(int(window))
        if not target and len(windows) != 1:
            return None
        if len(set(windows)) > 1:
            requested = (requested_model or "").strip().lower()
            if requested in matched:
                return matched[requested]
            # Both variants were used this turn and the requested id is not
            # one of them (an alias such as "opus"): the window is unknown.
            return None
        return windows[0] if windows else None

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
                    raw_input = block.input or {}
                    if not isinstance(raw_input, dict):
                        raw_input = {}
                    # Compute file touches from the raw SDK input before
                    # summarising. Bash summaries are often a short description
                    # ("Create guest CSV"), which would hide created paths from
                    # the PWA Outputs chips.
                    from ciao.web.chat_broker import extract_file_touches
                    touches = extract_file_touches(block.name, raw_input)
                    summary = _summarize_tool_input(block.name, raw_input)
                    input_chars, surface = tool_call_facts(block.name, raw_input)
                    events.append(ToolUseEvent(
                        type="assistant",
                        tool_name=block.name,
                        tool_input=summary,
                        tool_use_id=getattr(block, "id", None),
                        parent_tool_use_id=parent_id,
                        file_touches=touches or None,
                        work_dirs=tool_work_dirs(block.name, raw_input) or None,
                        input_chars=input_chars,
                        control_surface=surface,
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
            result_text = msg.result or ""
            if msg.is_error and not result_text:
                # A reported SDK failure can carry its diagnostics only in
                # ``errors`` (e.g. subtype "error_during_execution" with an
                # empty ``result``). Join the nonempty string entries so a real
                # error keeps a message instead of reading as an empty result.
                # An empty ``errors`` list stays empty: cancellation is
                # inferred by the caller, not invented here.
                errors = msg.errors or []
                result_text = "\n".join(
                    error
                    for error in errors
                    if isinstance(error, str) and error
                )
            if msg.is_error:
                result_text = _annotate_connection_host(result_text, self._api_host)
            raw_origin = getattr(msg, "origin", None)
            kind = raw_origin.get("kind") if isinstance(raw_origin, dict) else None
            origin = kind if isinstance(kind, str) and kind != "human" else ""
            return [
                ResultEvent(
                    type="result",
                    result=result_text,
                    session_id=self._session_id,
                    is_error=msg.is_error,
                    effective_model=self._extract_effective_model(msg),
                    usage=usage,
                    quota=quota,
                    cost_usd=msg.total_cost_usd,
                    origin=origin,
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
            # Quota is cached and persisted above (Settings shows all tiers).
            # Suppress chat-facing status for rate limit telemetry (allowed,
            # rejected, warnings, etc.). They update usage telemetry in
            # Settings, not conversation. Hard failures surface as error results.
            return []

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
                    # Only the top-level turn's text can carry the CLI's
                    # connection-drop banner; subagent text is unrelated.
                    if parent_id is None:
                        self._turn_text_tail = (self._turn_text_tail + text)[-4096:]
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
            # Each message_start reports the total input tokens for that API
            # call (the full context, including prior messages and tool
            # results). Using the latest value keeps the live counter close to
            # the actual context size; summing across a tool loop inflates it
            # into millions and makes the PWA label misleading.
            if isinstance(input_tokens, int):
                self._turn_input_tokens = input_tokens
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
            status_str = data.get("status") or ""
            # Drop rate-limit status notes (allowed, rejected, warnings). They are
            # transient usage telemetry, not conversation, and clutter the turn.
            # A genuine hard block surfaces as an is_error ResultEvent, not a
            # status line, so nothing actionable is lost by dropping these.
            if is_rate_limit_telemetry(status_str):
                return []
            return [SystemStatusEvent(type="system", status=status_str)]

        if subtype == "compact_boundary":
            # Manual or automatic compaction finished; the session id is kept.
            meta = data.get("compact_metadata") or {}
            pre = meta.get("pre_tokens") if isinstance(meta, dict) else None
            post = meta.get("post_tokens") if isinstance(meta, dict) else None
            if isinstance(pre, int) and isinstance(post, int):
                message = f"Compacted context: {pre:,} → {post:,} tokens"
            else:
                message = "Compacted context"
            return [SystemStatusEvent(type="system", status=message)]

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
    async def _record_bundled_skills(client: Any, init: dict[str, Any]) -> None:
        """Note which of this chat's skills ship with Claude Code itself.

        The init payload lists every skill the chat can load but not where each
        came from; the initialize response flags built-in commands but mixes
        skills with slash commands like /clear. Their intersection is exactly
        the bundled skills, after Ciaobot's own overrides.
        """
        skills = init.get("skills")
        if not isinstance(skills, list):
            return
        try:
            info = await client.get_server_info()
        except Exception:  # noqa: BLE001 — Settings detail, never fail a turn
            logger.debug("get_server_info failed; bundled skills not recorded", exc_info=True)
            return
        commands = (info or {}).get("commands")
        if not isinstance(commands, list):
            return
        if not any(isinstance(c, dict) and "builtin" in c for c in commands):
            # A CLI that predates the flag cannot say; "unknown", not "none".
            return
        builtin = {
            command.get("name")
            for command in commands
            if isinstance(command, dict) and command.get("builtin") is True
        }
        record_claude_bundled_skills(name for name in skills if name in builtin)

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
    def _turn_effective_model(
        result_msg: ResultMessage | None, last_main_msg: AssistantMessage | None
    ) -> str:
        """The model the footer names for a turn.

        The main agent's last model call is the chat model. ``model_usage`` can
        rank a helper call (a haiku title) above a very short reply, so it is
        only used when the turn made no main model call.
        """
        if last_main_msg is not None and last_main_msg.model:
            return last_main_msg.model
        if result_msg is None:
            return ""
        return ClaudeProvider._extract_effective_model(result_msg)

    @staticmethod
    def _extract_effective_model(msg: ResultMessage) -> str:
        """Name the model that did the turn's work.

        ``model_usage`` also lists the helper calls the CLI makes on its small
        fast model, in no guaranteed order, so the entry with the most output
        tokens is the one the user chose — not whichever comes first.
        """
        model_usage = msg.model_usage
        if isinstance(model_usage, dict):

            def _output_tokens(item: tuple[Any, Any]) -> int:
                value = item[1]
                tokens = value.get("outputTokens") if isinstance(value, dict) else None
                return tokens if isinstance(tokens, int) else 0

            for key, value in sorted(model_usage.items(), key=_output_tokens, reverse=True):
                if isinstance(value, dict):
                    for candidate in (
                        value.get("canonicalModel"),
                        value.get("model"),
                        key,
                    ):
                        if isinstance(candidate, str) and candidate.strip():
                            return candidate.strip()
                if isinstance(key, str) and key.strip():
                    return key.strip()
        return ""

    @staticmethod
    def _extract_quota(msg: ResultMessage) -> dict[str, str]:
        return {}
