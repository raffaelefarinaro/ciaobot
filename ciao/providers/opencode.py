"""opencode provider over the local HTTP + SSE server.

Unlike Claude (in-process SDK) and Codex (stdio JSON-RPC), opencode ships a
real multi-session HTTP server. Ciaobot runs ``opencode serve`` on an ephemeral
loopback port and drives it over ``httpx``, consuming the ``/event`` SSE stream.

**One server process per active chat.** A shared server is tempting because
opencode isolates chats as sessions, but Ciaobot scopes its control-plane MCP
token per chat, and opencode's MCP configuration is server-wide (``/mcp`` and
``opencode.json``) rather than per-session. A shared server would force one
long-lived token across every chat and lose failure isolation. Per-session
*permission* and *model* are supported and are set on the session instead.
Permission changes rotate to a newly-created session, because the installed
API does not apply a patched ruleset to an existing session.

The wire contract is verified against the server's own OpenAPI document at
``/doc`` on startup, so an incompatible build fails closed with a readable
message rather than half-working.

Capability note: opencode has no method that injects a message into a running
turn, so ``steer`` is False and ``ProviderService`` keeps a mid-turn message in
its next-turn queue. Everything else Ciaobot needs — fork, abort, permissions,
structured questions, background subagents as child sessions — is native.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import socket
import time
from collections import deque
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ciao.model_tiers import MODEL_TIERS, canonical_tier
from ciao.memory_injector import system_prompt_payload
from ciao.models import (
    AgentRequest,
    AssistantTextDelta,
    BridgeMode,
    PermissionRequestEvent,
    ResultEvent,
    StreamEvent,
    ThinkingEvent,
    TokenUsageEvent,
    ToolUseEvent,
)
from ciao.providers.base import (
    ActiveHandle,
    BaseSDKProvider,
    ProviderCapabilities,
    build_prompt,
    build_runtime_context,
)
from ciao.execution_modes import AUTO_APPROVED_MCP_TOOLS, MCP_SERVER_NAME
from ciao.providers.safe_commands import is_read_only_command
from ciao.tool_path import resolve_tool

logger = logging.getLogger(__name__)

# Operations Ciaobot cannot work without. Checked against the server's own
# OpenAPI paths at connect time so an incompatible build fails closed. This is
# the machine-checkable equivalent of Codex's hand-maintained
# ``_REQUIRED_PROTOCOL_TOKENS``.
REQUIRED_PATHS: frozenset[str] = frozenset({
    "/global/health",
    "/event",
    "/session",
    "/session/{sessionID}",
    "/session/{sessionID}/abort",
    "/session/{sessionID}/children",
    "/session/{sessionID}/fork",
    "/session/{sessionID}/message",
    "/session/{sessionID}/prompt_async",
    "/permission/{requestID}/reply",
    "/question/{requestID}/reply",
    "/question/{requestID}/reject",
})

# The catalog needs a throwaway `opencode serve` (~1-2s), and /api/models is
# hit on every model-picker open. Cache it like Codex does rather than paying
# a server spawn per request.
_MODEL_CACHE_TTL = 300.0
# An empty catalog is cached far more briefly: it usually means "nothing
# authenticated yet" or "the server did not come up", and holding that for five
# minutes would hide the models for five minutes after opencode starts working.
# It is still cached, because /api/models is on the PWA's load path and an
# uncached empty result means a throwaway server spawn — up to
# `_SERVER_START_TIMEOUT` of it when the binary exists but never gets healthy —
# on every single request.
_EMPTY_MODEL_CACHE_TTL = 20.0
_MODEL_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}

# Session reads (`read_thread` / `read_collab_tree`) also cost a throwaway
# `opencode serve`, and the PWA polls the routes they back on short intervals
# (15s status sync plus a post-turn retry ladder for /messages, 4s for
# /subagents while a turn streams). A TTL shorter than every poll interval
# collapses the bursts to roughly one spawn per tick without making replays
# feel stale.
_READ_CACHE_TTL = 3.0
_THREAD_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_COLLAB_CACHE: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}

_SERVER_START_TIMEOUT = 30.0
_SERVER_START_ATTEMPTS = 3
_SERVER_START_RETRY_DELAYS = (0.25, 0.75)
_REQUEST_TIMEOUT = 30.0
_SHUTDOWN_TIMEOUT = 5.0
_SERVER_START_LOCKS: dict[str, asyncio.Lock] = {}
# Lines of the server's stderr kept for error messages. The pipe must be read
# continuously (a full 64K pipe buffer blocks the child's next write and wedges
# the server mid-turn), so the reader keeps a bounded tail rather than the lot.
_STDERR_TAIL_LINES = 20

# opencode's built-in primary agents, keyed by Ciaobot's BridgeMode. `plan` is
# opencode's own read-only agent; everything else runs `build` and differs only
# in the permission ruleset below.
_MODE_AGENTS: dict[str, str] = {
    "plan": "plan",
    "normal": "build",
    "auto": "build",
    "bypass": "build",
}

# Per-session permission rulesets, as `POST /session` wants them: a *list* of
# {permission, pattern, action} rules, not the `{"*": "ask"}` map used in
# `opencode.json`. The two shapes are different and the API rejects the map
# with a bare 400, so keep this in rule form.
#
# Resolution is last-match-wins, so the wildcard goes first and the specific
# grants follow. Read-only tools are always allowed; `edit` is what separates
# auto from normal. Shell stays on the wildcard in every non-bypass mode, so a
# command reaches `_permission_event` even in auto, where the read-only
# classifier decides whether the operator has to see it.
_READ_ONLY_TOOLS = ("read", "glob", "grep", "list")


def _rules(*entries: tuple[str, str]) -> list[dict[str, str]]:
    return [
        {"permission": permission, "pattern": "*", "action": action}
        for permission, action in entries
    ]


_MODE_PERMISSIONS: dict[str, list[dict[str, str]]] = {
    "plan": _rules(("*", "ask"), *((tool, "allow") for tool in _READ_ONLY_TOOLS)),
    "normal": _rules(("*", "ask")),
    "auto": _rules(
        ("*", "ask"),
        *((tool, "allow") for tool in _READ_ONLY_TOOLS),
        ("edit", "allow"),
    ),
    "bypass": _rules(("*", "allow")),
}


@dataclass(frozen=True, slots=True)
class OpencodeSettings:
    """Operator overrides for the opencode tier aliases.

    Empty string means "no pin": the tier falls through to whatever model the
    session's configured provider resolves. Mirrors ``CodexSettings`` so
    ``AppSettings.provider_routing`` can drive both the same way.
    """

    haiku_model: str = ""
    sonnet_model: str = ""
    opus_model: str = ""
    fable_model: str = ""

    def tier_overrides(self) -> dict[str, str]:
        return {tier: getattr(self, f"{tier}_model") for tier in MODEL_TIERS}


def opencode_tier_overrides(config: object) -> dict[str, str]:
    """Extract the per-tier opencode pins from a (duck-typed) config object."""
    settings = getattr(config, "opencode", None)
    if settings is None:
        return {}
    return {tier: model for tier, model in settings.tier_overrides().items() if model}


def resolve_opencode_binary(env: Mapping[str, str] | None = None) -> str | None:
    """Absolute path to the opencode CLI, or None when it is not installed."""
    source = env if env is not None else os.environ
    explicit = str(source.get("CIAO_OPENCODE_BIN", "")).strip()
    if explicit:
        path = Path(explicit).expanduser()
        return str(path.resolve()) if path.is_file() else None
    return resolve_tool("opencode")


def auth_command(*, device_auth: bool = False) -> list[str]:
    """Interactive login command, for ``ciao auth opencode`` and the PWA.

    ``device_auth`` has no opencode equivalent and is ignored.
    """
    binary = resolve_opencode_binary()
    if not binary:
        raise FileNotFoundError("opencode CLI not found")
    return [binary, "auth", "login"]


def _free_port() -> int:
    """Reserve an ephemeral loopback port and hand back the number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _server_start_lock(workspace_root: Path) -> asyncio.Lock:
    """Serialize per-workspace server startup inside this Ciaobot process.

    opencode keeps its state in a shared SQLite database even though Ciaobot
    gives each chat its own server process. Serializing startup avoids two
    Ciaobot chats racing through opencode's migrations at the same time.
    """
    key = str(workspace_root.resolve())
    lock = _SERVER_START_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _SERVER_START_LOCKS[key] = lock
    return lock


def _is_transient_startup_error(exc: BaseException) -> bool:
    """Whether a failed server launch is likely to recover on retry."""
    text = str(exc).lower()
    return "database is locked" in text or "database is busy" in text


def missing_required_paths(spec: Mapping[str, Any]) -> tuple[str, ...]:
    """Required operations absent from a served OpenAPI document."""
    paths = spec.get("paths")
    available = set(paths) if isinstance(paths, Mapping) else set()
    return tuple(sorted(REQUIRED_PATHS - available))


_ENV_PLACEHOLDER_RE = re.compile(r"\{env:([^}]+)\}")
_SHELL_PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")


def _config_strings(node: object) -> list[str]:
    """Every string in a config tree, for placeholder scanning."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, Mapping):
        return [s for value in node.values() for s in _config_strings(value)]
    if isinstance(node, (list, tuple)):
        return [s for value in node for s in _config_strings(value)]
    return []


def unresolved_placeholders(config: object) -> tuple[str, ...]:
    """Every ``{env:VAR}`` or ``${VAR}`` placeholder in a config tree.

    Used for configs registered through the API, where opencode performs no
    interpolation at all, so any placeholder is a bug regardless of the
    environment.
    """
    found: list[str] = []
    for text in _config_strings(config):
        found.extend(f"{{env:{name}}}" for name in _ENV_PLACEHOLDER_RE.findall(text))
        found.extend(f"${{{name}}}" for name in _SHELL_PLACEHOLDER_RE.findall(text))
    return tuple(dict.fromkeys(found))


def config_placeholder_problems(
    config: object, env: Mapping[str, str]
) -> tuple[str, ...]:
    """Placeholders in an ``opencode.json`` that will not resolve under ``env``.

    opencode substitutes ``{env:VAR}`` and ``{file:path}`` when it reads a
    config *file*, falling back to an empty string when ``VAR`` is absent from
    the server process environment. A missing token therefore reaches the MCP
    server as ``""`` and only surfaces much later as a 401 on the first tool
    call, which is near-undebuggable from the chat. ``${VAR}`` is not opencode
    syntax and is passed through verbatim, which fails the same way.

    Both are reported here so the spawn logs say what is wrong while the
    process is starting, rather than leaving a silent empty credential.
    """
    problems: list[str] = []
    for text in _config_strings(config):
        for name in _SHELL_PLACEHOLDER_RE.findall(text):
            problems.append(
                f"opencode.json uses ${{{name}}}, which opencode does not "
                f"interpolate; use {{env:{name}}} instead"
            )
        for name in _ENV_PLACEHOLDER_RE.findall(text):
            if not env.get(name.strip()):
                problems.append(
                    f"opencode.json references {{env:{name}}} but {name} is not "
                    "set in the environment; it will resolve to an empty string"
                )
    return tuple(dict.fromkeys(problems))


def workspace_config_placeholder_problems(
    workspace_root: Path, env: Mapping[str, str]
) -> tuple[str, ...]:
    """Placeholder problems in the workspace's own ``opencode.json``.

    opencode discovers config by walking up from its cwd, which is always the
    workspace root, so this is the file the server will actually load.
    """
    for name in ("opencode.json", "opencode.jsonc"):
        path = workspace_root / name
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            config = json.loads(raw)
        except ValueError:
            # jsonc comments, or a half-written file; not ours to diagnose.
            continue
        return config_placeholder_problems(config, env)
    return ()


def _sanitize_error(message: object) -> str:
    """First line only: opencode error payloads carry a bundler stack trace."""
    text = str(message or "").strip()
    return text.split("\n", 1)[0].strip()


def error_text(error: Mapping[str, Any] | None) -> str:
    """Human-readable text from a ``session.error`` payload."""
    if not isinstance(error, Mapping):
        return "opencode reported an error"
    data = error.get("data")
    message = data.get("message") if isinstance(data, Mapping) else None
    return _sanitize_error(message) or str(error.get("name") or "opencode error")


def _summarize_tool_input(tool: str, raw: object) -> str:
    """Short, non-secret one-liner describing a tool call for the UI."""
    if not isinstance(raw, Mapping):
        return ""
    for key in ("filePath", "path", "file", "pattern", "query", "command", "description"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:400]
    # An empty map has no summary; returning its json printed a literal "{}"
    # next to the tool name in the activity row.
    return json.dumps(raw, ensure_ascii=False)[:200] if raw else ""


def _file_touches(tool: str, raw: object) -> list[dict[str, str]] | None:
    """Paths a tool call writes, for the PWA's file-change cards."""
    if not isinstance(raw, Mapping):
        return None
    action = {"write": "write", "edit": "edit", "patch": "edit"}.get(tool.lower())
    if action is None:
        return None
    path = raw.get("filePath") or raw.get("path")
    if not isinstance(path, str) or not path.strip():
        return None
    return [{"file_path": path.strip(), "action": action}]


def _token_count(raw: object) -> int:
    """A wire token count as an int, or 0 for anything that is not a number.

    Coercing with a bare ``int()`` would raise on a string or an object, and
    this runs inside the SSE loop where only ``httpx`` errors are handled — a
    surprising payload would kill the turn with a traceback.
    """
    return int(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 0


def usage_payload(tokens: Mapping[str, Any] | None) -> dict[str, str]:
    """Normalize opencode token counts into Ciaobot's usage fields."""
    if not isinstance(tokens, Mapping):
        return {}
    raw_cache = tokens.get("cache")
    cache: Mapping[str, Any] = raw_cache if isinstance(raw_cache, Mapping) else {}
    usage: dict[str, str] = {}
    for key, source in (
        ("inputTokens", tokens.get("input")),
        ("outputTokens", tokens.get("output")),
        ("reasoningTokens", tokens.get("reasoning")),
        ("cacheReadTokens", cache.get("read")),
        ("cacheWriteTokens", cache.get("write")),
    ):
        count = _token_count(source)
        if count:
            usage[key] = str(count)
    return usage


def _token_usage_events(tokens: object) -> list[StreamEvent]:
    """A live token-count event, when the payload carries real counts."""
    if not isinstance(tokens, Mapping):
        return []
    read_in = _token_count(tokens.get("input"))
    read_out = _token_count(tokens.get("output"))
    if not read_in and not read_out:
        return []
    return [TokenUsageEvent(type="token_usage", input_tokens=read_in, output_tokens=read_out)]


def control_plane_permission_rules() -> list[dict[str, str]]:
    """Allow rules for the auto-approved half of Ciaobot's own MCP tools.

    Ciaobot's control plane is not a third-party tool the operator should have
    to approve call by call: reading a project's files or listing chats is the
    app doing its own bookkeeping. Claude gets this through
    ``options.allowed_tools``; opencode needs it as session permission rules,
    or every ``ciaobot_*`` call raises a card even in auto mode.

    Enumerated rather than globbed on purpose. ``ciaobot_*`` would also allow
    the destructive tools deliberately kept out of AUTO_APPROVED_MCP_TOOLS —
    chat_delete, project_delete, chat_stop, background_run_start — which must
    keep prompting. opencode names an MCP tool ``<server>_<tool>``.
    """
    return [
        {"permission": f"{MCP_SERVER_NAME}_{tool}", "pattern": "*", "action": "allow"}
        for tool in AUTO_APPROVED_MCP_TOOLS
    ]


def mode_settings(mode: BridgeMode) -> tuple[str, list[dict[str, str]]]:
    """Map a Ciaobot mode onto an opencode (agent, permission ruleset)."""
    key = mode if mode in _MODE_AGENTS else "normal"
    rules = [dict(rule) for rule in _MODE_PERMISSIONS[key]]
    # Plan mode is excluded: its contract is "propose, don't act", and an allow
    # rule would punch a hole in it — same carve-out as the Claude provider.
    if key != "plan":
        rules.extend(control_plane_permission_rules())
    return _MODE_AGENTS[key], rules


def _session_permission_matches(
    payload: object, expected: list[dict[str, str]]
) -> bool:
    """Return whether a session exposes exactly the rules for this turn."""
    if not isinstance(payload, Mapping):
        return False
    info = payload.get("info")
    if isinstance(info, Mapping):
        payload = info
    actual = payload.get("permission")
    return isinstance(actual, list) and actual == expected


def auto_approves_permission(mode: BridgeMode, permission: str, command: str) -> bool:
    """Whether a surfaced permission request is answerable without the operator.

    A session's permission ruleset is fixed at creation and `PATCH` does not
    apply (see ``_ensure_session``); mode changes rotate the session before the
    prompt runs. Even a fresh auto session deliberately keeps shell on the
    wildcard. Deciding here, on the *current* mode of the turn, is what makes
    Auto automatic: bypass approves everything, auto approves verifiably
    read-only work, and every other mode (or anything the classifier cannot
    verify) still puts a card in front of the operator.
    """
    if mode == "bypass":
        return True
    if mode != "auto":
        return False
    if permission == "bash":
        return bool(command) and is_read_only_command(command)
    return permission in _READ_ONLY_TOOLS


def split_model(model: str) -> tuple[str, str]:
    """Split ``providerID/modelID`` into its parts.

    opencode addresses models as ``provider/model`` (e.g.
    ``anthropic/claude-sonnet-4-6``). A bare id has no provider, and the
    caller lets opencode fall back to its configured default.
    """
    value = (model or "").strip()
    if not value:
        return "", ""
    provider, sep, rest = value.partition("/")
    if not sep or not rest:
        return "", value
    return provider, rest


def compose_system(developer_instructions: str, runtime: str) -> str:
    """Build the prompt body's ``system`` field from its two halves.

    Instructions first, runtime facts after: the caller's system prompt is what
    defines the call, and the date/workspace lines are context it may refer to.
    Either half may be empty -- a chat supplies no instructions (opencode's own
    agent config owns the system prompt) and a bare environment yields no
    runtime lines -- and an empty result means "send no ``system`` at all".
    """
    return "\n\n".join(
        part for part in (developer_instructions.strip(), runtime.strip()) if part
    )


class OpencodeActiveHandle(ActiveHandle):
    """Stops the in-flight turn by aborting its session."""

    def __init__(self, provider: "OpencodeProvider", session_id: str) -> None:
        self._provider = provider
        self._session_id = session_id

    async def stop(self) -> None:
        await self._provider.abort_session(self._session_id)


@dataclass(slots=True)
class _PendingRequest:
    """A permission or question request awaiting the operator's reply."""

    request_id: str
    session_id: str
    tool_use_id: str = ""
    question_ids: tuple[str, ...] = ()


class OpencodeProvider(BaseSDKProvider):
    """Runs a chat turn against a per-chat ``opencode serve`` process."""

    capabilities = ProviderCapabilities(
        resume=True,
        fork=True,
        images=True,
        stop=True,
        # No upstream method injects into a running turn; a mid-turn message
        # queues for the next one instead.
        steer=False,
        permissions=True,
        structured_questions=True,
        dynamic_models=True,
        # Reasoning effort is per model (opencode calls it a model `variant`),
        # so the level list is narrowed per model from the catalog rather than
        # being a fixed ladder — same arrangement as Codex.
        thinking_levels=True,
        usage=True,
        # opencode is bring-your-own-provider: there is no unified quota or
        # reset-time snapshot to report.
        quota=False,
        subagents=True,
        background_subagents=True,
        subagent_messages=True,
        session_history=True,
        schedule_unattended=True,
    )

    def __init__(
        self,
        workspace_root: Path,
        *,
        config: object | None = None,
        developer_instructions: str | None = None,
    ) -> None:
        super().__init__(workspace_root, config=config)
        # ``None`` means a normal Ciaobot chat and receives the compact shared
        # core below. A supplied string is an explicit one-shot instruction
        # (titles, insights, critique) and remains isolated from chat policy.
        self._developer_instructions = (
            None if developer_instructions is None else developer_instructions.strip()
        )
        self._process: asyncio.subprocess.Process | None = None
        # Reads the server's stderr for its whole life; see
        # `_start_stderr_reader` for why leaving the pipe unread is not an option.
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        self._client: httpx.AsyncClient | None = None
        self._base_url: str = ""
        self._password: str = ""
        self._session_id: str = ""
        self._permission_requests: dict[str, _PendingRequest] = {}
        # Auto-approval reply tasks. asyncio holds tasks only weakly, so a
        # fire-and-forget task can be garbage-collected before the reply is
        # posted; the set keeps each alive until its done-callback removes it.
        self._auto_approve_tasks: set[asyncio.Task[None]] = set()
        self._question_requests: dict[str, _PendingRequest] = {}
        self._tool_calls: dict[str, str] = {}
        self._mcp_token: str = ""
        # Per-turn stream state, reset by `_reset_turn_state`.
        self._emitted: dict[str, int] = {}
        # partID -> part type. `message.part.delta` reports the *field* it is
        # filling, and a ReasoningPart stores its content in `text` just like a
        # TextPart — so `field` alone cannot tell reasoning from prose. The
        # part is always announced by `message.part.updated` before its deltas
        # arrive, which is what makes this lookup reliable.
        self._part_types: dict[str, str] = {}
        self._user_message_id: str = ""
        self._usage: dict[str, str] = {}
        self._cost: float | None = None
        # Visible assistant text, accumulated per part so the terminal
        # ResultEvent can carry the turn's answer (codex-style). `record_turn`
        # persists that as the durable transcript's response, so leaving it
        # empty made replayed opencode chats render blank turns (#295).
        # Dict insertion order doubles as the part order.
        self._answer_parts: dict[str, list[str]] = {}
        # What opencode actually ran, as `providerID/modelID`. A workspace may
        # pin opencode without naming a model, in which case the request carries
        # none and only the assistant message says what was used.
        self._effective_model: str = ""

    def _reset_turn_state(self) -> None:
        self._emitted.clear()
        self._part_types.clear()
        self._tool_calls.clear()
        self._user_message_id = ""
        self._usage = {}
        self._cost = None
        self._answer_parts.clear()
        self._effective_model = ""

    # ---------------------------------------------------------------- server

    @property
    def current_session_id(self) -> str | None:
        return self._session_id or None

    @property
    def can_drain(self) -> bool:
        """opencode has no between-turns event source to drain."""
        return False

    def _chat_system_instructions(self, request: AgentRequest) -> str:
        """Return the compact core for normal chats, never bounded memory."""
        payload = system_prompt_payload(
            "", control_surface=request.control_surface
        ) or {}
        return str(payload.get("append") or "")

    async def _ensure_server(self, request: AgentRequest) -> httpx.AsyncClient:
        """Start (or reuse) this chat's server and return its HTTP client.

        A changed MCP token forces a full restart: opencode reads MCP
        configuration at server scope, so the running process cannot be
        re-pointed at a new token.
        """
        if self._client is not None and self._process is not None:
            if self._process.returncode is None and request.mcp_token == self._mcp_token:
                return self._client
            await self.disconnect()

        binary = resolve_opencode_binary(request.extra_env or None)
        if not binary:
            raise FileNotFoundError(
                "opencode CLI not found. Install it, or set CIAO_OPENCODE_BIN."
            )

        lock = _server_start_lock(self.workspace_root)
        for attempt in range(_SERVER_START_ATTEMPTS):
            try:
                async with lock:
                    return await self._start_server_once(request, binary)
            except BaseException as exc:
                if (
                    not _is_transient_startup_error(exc)
                    or attempt + 1 >= _SERVER_START_ATTEMPTS
                ):
                    raise
                delay = _SERVER_START_RETRY_DELAYS[attempt]
                logger.warning(
                    "opencode startup hit shared database contention for %s "
                    "(attempt %d/%d); retrying in %.2fs",
                    self.workspace_root,
                    attempt + 1,
                    _SERVER_START_ATTEMPTS,
                    delay,
                )
                await asyncio.sleep(delay)

        raise AssertionError("unreachable opencode startup retry state")

    async def _start_server_once(
        self, request: AgentRequest, binary: str
    ) -> httpx.AsyncClient:
        """Start, validate, and register one opencode server process."""
        port = _free_port()
        self._password = secrets.token_urlsafe(24)
        env = {
            **os.environ,
            **(request.extra_env or {}),
            "OPENCODE_SERVER_PASSWORD": self._password,
        }
        # The control-plane token is registered as a literal Authorization
        # header below. Never put it in the server environment: opencode passes
        # that environment to model-launched shell commands, where `env` (or a
        # malicious workspace script) could steal the token and call `/mcp`
        # without going through provider permission prompts.
        env.pop("CIAO_MCP_SESSION_TOKEN", None)
        self._mcp_token = request.mcp_token

        # Say it now, while the environment we are about to hand over is in
        # hand: an unresolved placeholder becomes an empty credential and only
        # shows up as a 401 from some MCP server mid-turn.
        for problem in workspace_config_placeholder_problems(self.workspace_root, env):
            logger.warning("opencode: %s", problem)

        self._process = await asyncio.create_subprocess_exec(
            binary, "serve", "--port", str(port), "--hostname", "127.0.0.1",
            cwd=str(self.workspace_root),
            env=env,
            # stdout is discarded rather than piped: nothing consumes it, and an
            # unread pipe blocks the child once the OS buffer fills. stderr is
            # kept for diagnostics but drained continuously for the same reason.
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._start_stderr_reader()
        self._base_url = f"http://127.0.0.1:{port}"
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            auth=("opencode", self._password),
            # No read timeout: the SSE stream is idle between turns and must
            # not be torn down for being quiet.
            timeout=httpx.Timeout(_REQUEST_TIMEOUT, read=None),
        )
        try:
            await self._await_health()
            await self._verify_contract()
            await self._register_control_plane(request)
        except BaseException:
            # A server we could not validate is a server nobody will ever
            # shut down; reap it here rather than leaking it for the life of
            # the app.
            await self.disconnect()
            raise
        return self._client

    def _start_stderr_reader(self) -> None:
        """Drain the server's stderr into a bounded tail.

        Not optional bookkeeping: a piped stream nobody reads fills its 64K OS
        buffer and then blocks the child's next write, which wedges the server
        mid-turn. Keeping a tail also gives startup failures a readable cause.
        """
        stream = getattr(self._process, "stderr", None)
        self._stderr_tail.clear()
        if stream is None:
            return

        async def drain() -> None:
            try:
                while True:
                    line = await stream.readline()
                    if not line:
                        return
                    text = line.decode("utf-8", "replace").rstrip()
                    if text:
                        self._stderr_tail.append(text)
                        logger.debug("opencode serve: %s", text)
            except (asyncio.CancelledError, OSError, ValueError):
                return

        self._stderr_task = asyncio.create_task(drain())

    async def _stderr_detail(self) -> str:
        """Last line of the server's stderr, for an error message.

        Waits briefly for the reader first: a server that dies immediately does
        so before the drain task has had a turn, and the reason it printed is
        exactly what the caller needs. The pipe is at EOF once the process is
        gone, so the task finishes on its own.
        """
        reader = self._stderr_task
        if reader is not None and not reader.done():
            try:
                await asyncio.wait_for(asyncio.shield(reader), timeout=1.0)
            except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
                pass
        return self._stderr_tail[-1] if self._stderr_tail else ""

    async def _await_health(self) -> None:
        """Poll ``/global/health`` until the server answers or we give up."""
        assert self._client is not None
        deadline = asyncio.get_running_loop().time() + _SERVER_START_TIMEOUT
        last_error: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            if self._process is not None and self._process.returncode is not None:
                detail = await self._stderr_detail()
                raise RuntimeError(
                    f"opencode serve exited with code {self._process.returncode}"
                    + (f": {detail}" if detail else "")
                )
            try:
                response = await self._client.get("/global/health", timeout=2.0)
                if response.status_code == 200:
                    return
            except httpx.HTTPError as exc:  # not up yet
                last_error = exc
            await asyncio.sleep(0.2)
        raise TimeoutError(f"opencode serve did not become healthy: {last_error}")

    async def _verify_contract(self) -> None:
        """Fail closed when the installed build is missing required operations."""
        assert self._client is not None
        try:
            response = await self._client.get("/doc", timeout=10.0)
            response.raise_for_status()
            spec = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"could not read the opencode API document: {exc}") from exc
        missing = missing_required_paths(spec)
        if missing:
            raise RuntimeError(
                "this opencode build is missing operations Ciaobot needs: "
                + ", ".join(missing)
            )

    async def _register_control_plane(self, request: AgentRequest) -> None:
        """Attach Ciaobot's own MCP server to this chat's opencode process.

        Claude gets this through ``options.mcp_servers`` and Codex through
        ``-c mcp_servers.ciaobot.*``. opencode takes it over the running
        server's API, which is what makes the per-chat process worth having:
        the token is scoped to this chat and never written to
        ``opencode.json``, where it would be workspace-wide and on disk.

        The token goes in the header literally: opencode's ``{env:VAR}``
        interpolation is a config-*file* feature and is not applied to configs
        registered through the API (verified — the placeholder was sent
        through verbatim, which the control plane would reject as a bad
        token). The call is loopback and password-authenticated, and the
        server already holds the token in its environment, so this adds no
        exposure — and unlike ``opencode.json`` it never reaches disk.
        """
        client = self._client
        if client is None or not request.mcp_url or not request.mcp_token:
            return
        config: dict[str, Any] = {
            "type": "remote",
            "url": request.mcp_url,
            "enabled": True,
            "headers": {"Authorization": f"Bearer {request.mcp_token}"},
        }
        # Nothing here is interpolated (see above), so a placeholder that slipped
        # in would be sent verbatim and rejected as a bad token. Fail with the
        # cause rather than a downstream 401.
        placeholders = unresolved_placeholders(config)
        if placeholders:
            raise RuntimeError(
                "refusing to register the Ciaobot MCP server with unresolved "
                f"placeholders {', '.join(placeholders)}: opencode does not "
                "interpolate configs registered over the API"
            )
        try:
            response = await client.post(
                "/mcp", json={"name": MCP_SERVER_NAME, "config": config}
            )
        except httpx.HTTPError as exc:
            if request.mcp_required:
                raise RuntimeError(f"could not attach the Ciaobot MCP server: {exc}") from exc
            logger.warning("opencode: Ciaobot MCP server not attached: %s", exc)
            return
        if response.status_code >= 400:
            detail = _sanitize_error(response.text)
            if request.mcp_required:
                raise RuntimeError(f"opencode refused the Ciaobot MCP server: {detail}")
            logger.warning("opencode: Ciaobot MCP server refused: %s", detail)

    async def disconnect(self) -> None:
        """Tear down the server, denying anything still awaiting a reply."""
        for pending in list(self._permission_requests.values()):
            await self._reply_permission(pending, "reject")
        for pending in list(self._question_requests.values()):
            await self._reject_question(pending)
        self._permission_requests.clear()
        self._question_requests.clear()
        self._tool_calls.clear()

        if self._client is not None:
            await self._client.aclose()
            self._client = None
        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=_SHUTDOWN_TIMEOUT)
            except (TimeoutError, asyncio.TimeoutError):
                process.kill()
                await process.wait()
        # After the process is gone, so the tail still explains a crash above.
        reader = self._stderr_task
        self._stderr_task = None
        if reader is not None:
            reader.cancel()
        self._base_url = ""
        self._mcp_token = ""
        self._reset_settings()

    # --------------------------------------------------------------- session

    async def _ensure_session(self, request: AgentRequest) -> str:
        """Resume, fork, or create the session this turn runs in.

        The permission ruleset is fixed at creation. `agent` is re-sent on every
        prompt, but a mode switch must not reuse a session whose rules differ:
        `PATCH /session/{id}` accepts `permission` and returns 200, but verified
        against opencode 1.18 it does not apply. When the session's permission
        is missing or differs, create a fresh session with the current rules
        rather than run with a stale (possibly broader) grant.
        """
        client = self._client
        assert client is not None
        agent, permission = mode_settings(request.mode)
        provider_id, model_id = split_model(request.model)

        resume = (request.resume_session or "").strip()
        if resume:
            response = await client.get(f"/session/{resume}")
            if response.status_code < 400:
                try:
                    session_payload = response.json()
                except (TypeError, ValueError):
                    session_payload = None
                if _session_permission_matches(session_payload, permission):
                    if request.fork_session:
                        fork_response = await client.post(
                            f"/session/{resume}/fork", json={}
                        )
                        if fork_response.status_code < 400:
                            self._session_id = str(
                                fork_response.json().get("id") or ""
                            )
                            if self._session_id:
                                return self._session_id
                        logger.warning(
                            "opencode fork failed (%s); starting a new session",
                            fork_response.status_code,
                        )
                    else:
                        self._session_id = resume
                        return resume
                else:
                    logger.warning(
                        "opencode session %s permission rules do not match %s; "
                        "starting a fresh session",
                        resume,
                        request.mode,
                    )
            else:
                logger.info("opencode session %s is gone; starting a new one", resume)

        payload: dict[str, Any] = {"agent": agent, "permission": permission}
        if model_id:
            model: dict[str, Any] = {"id": model_id, "providerID": provider_id}
            if request.thinking_level:
                model["variant"] = request.thinking_level
            payload["model"] = model
        response = await client.post("/session", json=payload)
        response.raise_for_status()
        self._session_id = str(response.json().get("id") or "")
        return self._session_id

    async def abort_session(self, session_id: str) -> None:
        client = self._client
        if client is None or not session_id:
            return
        try:
            await client.post(f"/session/{session_id}/abort")
        except httpx.HTTPError:
            logger.debug("opencode abort failed for %s", session_id, exc_info=True)

    def _prompt_parts(self, request: AgentRequest) -> list[dict[str, Any]]:
        """Text plus any attached images, in opencode's part shape."""
        parts: list[dict[str, Any]] = [{"type": "text", "text": build_prompt(request)}]
        for image in request.images:
            parts.append({
                "type": "file",
                "mime": image.mime_type,
                "filename": image.original_filename,
                "url": image.path.resolve().as_uri(),
            })
        return parts

    async def steer(self, request: AgentRequest) -> bool:
        """Always False: opencode cannot inject into a running turn.

        Returning False (rather than sending a second prompt) is deliberate —
        a second prompt would be queued or would abort the active turn, and
        neither is what steering means. ``ProviderService`` keeps the message
        for the next turn instead.
        """
        return False

    # ------------------------------------------------------------ permissions

    def tool_use_id_for_request(self, request_id: str) -> str:
        pending = self._permission_requests.get(request_id)
        return pending.tool_use_id if pending is not None else ""

    async def _reply_permission(self, pending: _PendingRequest, reply: str) -> bool:
        client = self._client
        if client is None:
            return False
        try:
            response = await client.post(
                f"/permission/{pending.request_id}/reply", json={"reply": reply}
            )
            return response.status_code < 400
        except httpx.HTTPError:
            logger.debug("opencode permission reply failed", exc_info=True)
            return False

    async def _auto_approve(self, pending: _PendingRequest) -> None:
        """Approve a request the mode has already decided, without a card.

        Replies "once" rather than "always": "always" whitelists opencode's
        suggested pattern for the rest of the session, which could over-approve
        (e.g. `git *` from one `git status`). Each request is re-judged.
        """
        replied = await self._reply_permission(pending, "once")
        if replied:
            self._permission_requests.pop(pending.request_id, None)
        else:
            logger.warning(
                "opencode auto-approval reply failed for %s", pending.request_id
            )

    async def _deliver_permission_reply(
        self, pending: _PendingRequest, reply: str
    ) -> None:
        replied = await self._reply_permission(pending, reply)
        if replied:
            self._permission_requests.pop(pending.request_id, None)
        else:
            logger.warning(
                "opencode permission reply failed for %s", pending.request_id
            )

    def send_permission_response(self, request_id: str, approved: bool) -> bool:
        pending = self._permission_requests.get(request_id)
        if pending is None or self._client is None:
            return False
        asyncio.create_task(
            self._deliver_permission_reply(
                pending, "once" if approved else "reject"
            )
        )
        return True

    async def _reject_question(self, pending: _PendingRequest) -> bool:
        client = self._client
        if client is None:
            return False
        try:
            response = await client.post(f"/question/{pending.request_id}/reject", json={})
            return response.status_code < 400
        except httpx.HTTPError:
            logger.debug("opencode question reject failed", exc_info=True)
            return False

    async def _reply_question(
        self, pending: _PendingRequest, payload: dict[str, Any]
    ) -> bool:
        client = self._client
        if client is None:
            return False
        try:
            response = await client.post(
                f"/question/{pending.request_id}/reply", json=payload
            )
            return response.status_code < 400
        except httpx.HTTPError:
            logger.debug("opencode question reply failed", exc_info=True)
            return False

    async def _deliver_question_reply(
        self, pending: _PendingRequest, payload: dict[str, Any]
    ) -> None:
        replied = (
            await self._reject_question(pending)
            if not payload["answers"]
            else await self._reply_question(pending, payload)
        )
        if replied:
            self._question_requests.pop(pending.request_id, None)
        else:
            logger.warning(
                "opencode question reply failed for %s", pending.request_id
            )

    def send_question_response(
        self, request_id: str, answers: Mapping[str, Sequence[str]]
    ) -> bool:
        pending = self._question_requests.get(request_id)
        if pending is None or self._client is None:
            return False
        payload = {
            "answers": [
                [str(value) for value in answers.get(question_id, ())]
                for question_id in pending.question_ids
            ]
        }
        asyncio.create_task(self._deliver_question_reply(pending, payload))
        return True

    # -------------------------------------------------------------- streaming

    def _emit_suffix(self, part_id: str, text: str) -> str:
        """Return only the not-yet-emitted tail of a cumulative part.

        opencode streams the same text twice: incrementally via
        ``message.part.delta`` and cumulatively via ``message.part.updated``
        (which restates the whole part each time). Emitting both would double
        every token, and consuming only one is not safe either — which of the
        two a model produces varies. Tracking how much of each part has already
        been emitted makes either source, or both, come out right.
        """
        already = self._emitted.get(part_id, 0)
        if len(text) <= already:
            return ""
        self._emitted[part_id] = len(text)
        return text[already:]

    def _note_answer(self, part_id: str, text: str) -> None:
        """Accumulate one emitted fragment of the visible reply."""
        self._answer_parts.setdefault(part_id, []).append(text)

    def _answer_text(self) -> str:
        """The turn's visible reply, joined across text parts codex-style."""
        parts = (
            "".join(chunks).strip() for chunks in self._answer_parts.values()
        )
        return "\n\n".join(part for part in parts if part)

    def _event_to_stream(self, event: Mapping[str, Any]) -> list[StreamEvent]:
        """Translate one SSE event into zero or more Ciaobot stream events.

        The `message.part.*` family is the live one. The `session.next.*`
        events in the schema are a separate, newer stream that this build does
        not emit for ordinary turns; they are handled too so that a future
        opencode which switches over keeps working.
        """
        kind = str(event.get("type") or "")
        props = event.get("properties")
        props = props if isinstance(props, Mapping) else {}

        if kind == "message.part.delta":
            return self._part_delta(props)
        if kind == "message.part.updated":
            return self._part_updated(props)
        if kind == "message.updated":
            return self._message_updated(props)

        # Newer `session.next.*` stream, kept as a forward-compatible path.
        if kind == "session.next.text.delta":
            text = str(props.get("delta") or "")
            if not text:
                return []
            self._note_answer(str(props.get("partID") or ""), text)
            return [AssistantTextDelta(type="text", text=text)]

        if kind == "session.next.reasoning.delta":
            text = str(props.get("delta") or "")
            return [ThinkingEvent(type="thinking", text=text)] if text else []

        if kind == "session.next.tool.called":
            call_id = str(props.get("callID") or "")
            tool = str(props.get("tool") or "")
            self._tool_calls[call_id] = tool
            return [ToolUseEvent(
                type="tool_use",
                tool_name=tool,
                tool_input=_summarize_tool_input(tool, props.get("input")),
                tool_use_id=call_id or None,
                file_touches=_file_touches(tool, props.get("input")),
            )]

        if kind in {"session.next.tool.success", "session.next.tool.failed"}:
            call_id = str(props.get("callID") or "")
            tool = self._tool_calls.pop(call_id, "")
            detail = error_text(props.get("error")) if kind.endswith("failed") else ""
            return [ToolUseEvent(
                type="tool_result",
                tool_name=tool,
                tool_input=detail,
                tool_use_id=call_id or None,
            )]

        if kind == "session.next.step.ended":
            return _token_usage_events(props.get("tokens"))

        if kind in {"permission.v2.asked", "permission.asked"}:
            return self._permission_event(props)

        if kind in {"question.v2.asked", "question.asked"}:
            return self._question_event(props)

        if kind == "session.status":
            # Deliberately not surfaced. opencode repeats `busy` throughout a
            # turn and the PWA renders a SystemStatusEvent as a visible row, so
            # forwarding it printed a column of "busy" lines above the reply.
            # The streaming events already show the turn is running.
            return []

        return []

    def _part_delta(self, props: Mapping[str, Any]) -> list[StreamEvent]:
        """Incremental text/reasoning for one part."""
        delta = str(props.get("delta") or "")
        if not delta:
            return []
        part_id = str(props.get("partID") or "")
        field = str(props.get("field") or "text")
        if field not in {"text", "reasoning"}:
            return []
        # Count it against the part so the cumulative update that follows does
        # not replay the same characters.
        self._emitted[part_id] = self._emitted.get(part_id, 0) + len(delta)
        # The part's own type decides, not the field name: reasoning content
        # also arrives in a field called `text`.
        if self._part_types.get(part_id, "text") == "reasoning" or field == "reasoning":
            return [ThinkingEvent(type="thinking", text=delta)]
        self._note_answer(part_id, delta)
        return [AssistantTextDelta(type="text", text=delta)]

    def _part_updated(self, props: Mapping[str, Any]) -> list[StreamEvent]:
        """A settled part: text, reasoning, a tool call, or a step boundary."""
        part = props.get("part")
        if not isinstance(part, Mapping):
            return []
        part_type = str(part.get("type") or "")
        part_id = str(part.get("id") or "")
        if part_id and part_type:
            self._part_types[part_id] = part_type

        if part_type in {"text", "reasoning"}:
            # A user part is echoed back on submit; the visible user bubble
            # already exists, so replaying it would duplicate the prompt.
            if part.get("messageID") and part.get("messageID") == self._user_message_id:
                return []
            suffix = self._emit_suffix(part_id, str(part.get("text") or ""))
            if not suffix:
                return []
            if part_type == "reasoning":
                return [ThinkingEvent(type="thinking", text=suffix)]
            self._note_answer(part_id, suffix)
            return [AssistantTextDelta(type="text", text=suffix)]

        if part_type == "tool":
            return self._tool_part(part)

        if part_type == "step-finish":
            return _token_usage_events(part.get("tokens"))

        return []

    def _tool_part(self, part: Mapping[str, Any]) -> list[StreamEvent]:
        """One tool call, emitted once on start and once on settle."""
        call_id = str(part.get("callID") or part.get("id") or "")
        tool = str(part.get("tool") or "")
        state = part.get("state")
        state = state if isinstance(state, Mapping) else {}
        status = str(state.get("status") or "")
        raw_input = state.get("input")

        if status in {"pending", "running"}:
            if call_id in self._tool_calls:
                return []  # already announced; a running update is not news
            # A `pending` part carries `input={}` — the arguments stream in and
            # only land by `running`. Announcing at pending showed the tool
            # with no detail at all ("bash" with an empty argument line).
            if status == "pending" and not raw_input:
                return []
            self._tool_calls[call_id] = tool
            return [ToolUseEvent(
                type="tool_use",
                tool_name=tool,
                tool_input=_summarize_tool_input(tool, raw_input),
                tool_use_id=call_id or None,
                file_touches=_file_touches(tool, raw_input),
            )]

        if status in {"completed", "error"}:
            events: list[StreamEvent] = []
            if call_id not in self._tool_calls:
                # A fast tool can settle before any running update arrives, so
                # the call would otherwise never be shown at all.
                events.append(ToolUseEvent(
                    type="tool_use",
                    tool_name=tool,
                    tool_input=_summarize_tool_input(tool, raw_input),
                    tool_use_id=call_id or None,
                    file_touches=_file_touches(tool, raw_input),
                ))
            self._tool_calls.pop(call_id, None)
            detail = _sanitize_error(state.get("error")) if status == "error" else ""
            events.append(ToolUseEvent(
                type="tool_result",
                tool_name=tool,
                tool_input=detail,
                tool_use_id=call_id or None,
            ))
            return events

        return []

    def _message_updated(self, props: Mapping[str, Any]) -> list[StreamEvent]:
        """Track the turn's user message id and its final usage/cost."""
        info = props.get("info")
        if not isinstance(info, Mapping):
            return []
        if info.get("role") == "user":
            self._user_message_id = str(info.get("id") or "")
            return []
        if info.get("role") != "assistant":
            return []
        # The only place the resolved model is reported. Without this a chat
        # that let opencode pick (no model on the request) records an empty
        # model forever, and the header has nothing to show.
        model_id = str(info.get("modelID") or "")
        if model_id:
            provider_id = str(info.get("providerID") or "")
            self._effective_model = f"{provider_id}/{model_id}" if provider_id else model_id
        cost = info.get("cost")
        if isinstance(cost, (int, float)):
            self._cost = float(cost)
        tokens = info.get("tokens")
        if isinstance(tokens, Mapping):
            self._usage = usage_payload(tokens) or self._usage
        return _token_usage_events(tokens)

    def _permission_event(self, props: Mapping[str, Any]) -> list[StreamEvent]:
        """Surface an approval prompt, naming what is actually being approved.

        The live event is `permission.asked`, whose payload is
        ``{permission, patterns, metadata, always, tool:{callID}}``. The
        schema's newer `permission.v2.asked` uses ``{action, resources}``
        instead, so both are read — an approval card that cannot say *what* it
        is approving is worse than useless.
        """
        request_id = str(props.get("id") or "")
        if not request_id:
            return []

        # v1 names the tool in `permission`; v2 names it in `action`.
        tool_name = str(props.get("permission") or props.get("action") or "").strip()
        detail = ""
        command = ""
        metadata = props.get("metadata")
        if isinstance(metadata, Mapping):
            raw_command = metadata.get("command")
            if isinstance(raw_command, str):
                command = raw_command.strip()
            for key in ("command", "filePath", "path", "url", "pattern"):
                value = metadata.get(key)
                if isinstance(value, str) and value.strip():
                    detail = value.strip()
                    break
        if not detail:
            # `patterns` (v1) / `resources` (v2) hold the concrete targets.
            targets = props.get("patterns")
            if not isinstance(targets, list):
                targets = props.get("resources")
            if isinstance(targets, list):
                detail = ", ".join(str(item) for item in targets if str(item).strip())

        tool = props.get("tool")
        call_id = str(tool.get("callID") or "") if isinstance(tool, Mapping) else ""
        pending = _PendingRequest(
            request_id=request_id,
            session_id=str(props.get("sessionID") or ""),
            tool_use_id=call_id,
        )
        label = tool_name or "a tool"
        # Without a client the approval could not be posted, so fall through
        # to the card (only reachable in tests and teardown races).
        if self._client is not None and auto_approves_permission(
            self._current_mode, tool_name, command
        ):
            logger.info(
                "opencode auto-approved %s in %s mode: %s",
                label, self._current_mode, detail or "(no detail)",
            )
            # Registered until the reply lands so the disconnect sweep can
            # still reject it if the turn is torn down first; _auto_approve
            # removes the entry once opencode has an answer.
            self._permission_requests[request_id] = pending
            task = asyncio.create_task(self._auto_approve(pending))
            self._auto_approve_tasks.add(task)
            task.add_done_callback(self._auto_approve_tasks.discard)
            return []
        self._permission_requests[request_id] = pending
        return [PermissionRequestEvent(
            # `system`, and "Approve use of X?", to match the Claude and Codex
            # providers. A different type and a restated "opencode wants to
            # use bash" rendered as an extra transcript line beside the card.
            type="system",
            message=f"Approve use of {label}?",
            tool_name=label,
            tool_input=detail[:400],
            request_id=request_id,
        )]

    def _question_event(self, props: Mapping[str, Any]) -> list[StreamEvent]:
        """Surface a structured question as the PWA's question card."""
        request_id = str(props.get("id") or "")
        questions = props.get("questions")
        if not request_id or not isinstance(questions, list) or not questions:
            return []
        question_ids = tuple(
            str(item.get("id") or index)
            for index, item in enumerate(questions)
            if isinstance(item, Mapping)
        )
        self._question_requests[request_id] = _PendingRequest(
            request_id=request_id,
            session_id=str(props.get("sessionID") or ""),
            question_ids=question_ids,
        )
        payload = {
            "questions": [
                {
                    "id": str(item.get("id") or index),
                    "question": str(item.get("question") or ""),
                    "header": str(item.get("header") or ""),
                    "multiSelect": bool(item.get("multiple")),
                    "isOther": bool(item.get("custom")),
                    "options": [
                        {
                            "label": str(option.get("label") or option.get("value") or ""),
                            "description": str(option.get("description") or ""),
                        }
                        for option in (item.get("options") or [])
                        if isinstance(option, Mapping)
                    ],
                }
                for index, item in enumerate(questions)
                if isinstance(item, Mapping)
            ]
        }
        return [ToolUseEvent(
            type="tool_use",
            tool_name="AskUserQuestion",
            tool_input=json.dumps(payload, ensure_ascii=False),
            tool_use_id=request_id,
            request_id=request_id,
        )]

    async def _resolve_model(
        self, client: httpx.AsyncClient, model: str
    ) -> tuple[str, str]:
        """Resolve a requested model to ``(providerID, modelID)`` for the prompt.

        A qualified ``provider/model`` id passes through. An unqualified one --
        in practice a tier alias like ``sonnet``, which is what schedules,
        routines and the critique panel speak -- is not something opencode can
        serve: sending it verbatim asks for modelID ``sonnet`` under an empty
        provider, which opencode rejects. Resolve it against the live catalog
        instead.

        Returns ``("", "")`` when nothing matches, which makes the caller omit
        the model and let opencode apply its own default. That is the honest
        outcome for a catalog with no equivalent of the requested tier, and it
        keeps a bring-your-own-provider install working out of the box.
        """
        provider_id, model_id = split_model(model)
        if provider_id or not model_id:
            return provider_id, model_id
        try:
            response = await client.get("/provider")
            response.raise_for_status()
            catalog = _catalog_from_providers(response.json())
        except (httpx.HTTPError, ValueError):
            logger.info("opencode catalog unavailable; letting it pick a model")
            return "", ""
        resolved = opencode_tier_models(
            catalog, opencode_tier_overrides(self.config)
        ).get(canonical_tier(model_id), "")
        if not resolved:
            logger.info(
                "opencode has no model for tier %r; using its configured default",
                model_id,
            )
            return "", ""
        return split_model(resolved)

    async def run_streaming(
        self,
        request: AgentRequest,
        register_handle: Callable[[ActiveHandle | None], None],
    ) -> AsyncGenerator[StreamEvent, None]:
        client = await self._ensure_server(request)
        # Permission requests can arrive while session setup is in flight.
        # Keep the live mode aligned with this turn before any setup work so a
        # resumed session cannot consult the previous turn's mode (#291).
        self._remember_settings(request)
        session_id = await self._ensure_session(request)
        self._reset_turn_state()
        register_handle(OpencodeActiveHandle(self, session_id))

        agent, _permission = mode_settings(request.mode)
        provider_id, model_id = await self._resolve_model(client, request.model)
        body: dict[str, Any] = {"agent": agent, "parts": self._prompt_parts(request)}
        if model_id:
            body["model"] = {"providerID": provider_id, "modelID": model_id}
        # Reasoning effort rides beside the model, not inside it, on prompts.
        if request.thinking_level:
            body["variant"] = request.thinking_level
        if self._developer_instructions is None:
            instructions = self._chat_system_instructions(request)
            runtime = ""
        else:
            instructions = self._developer_instructions
            runtime = build_runtime_context(request)
        system = compose_system(instructions, runtime)
        if system:
            body["system"] = system

        error: str = ""
        saw_output = False

        try:
            async with client.stream("GET", "/event") as stream:
                stream.raise_for_status()
                # Subscribe before prompting: opencode starts emitting as soon
                # as the prompt is accepted, and a late subscriber loses the
                # opening deltas.
                response = await client.post(
                    f"/session/{session_id}/prompt_async", json=body
                )
                if response.status_code >= 400:
                    detail = _sanitize_error(response.text)
                    yield ResultEvent(
                        type="result",
                        result=f"opencode rejected the prompt: {detail}",
                        session_id=session_id,
                        is_error=True,
                    )
                    return

                async for raw in stream.aiter_lines():
                    if not raw.startswith("data: "):
                        continue
                    try:
                        event = json.loads(raw[6:])
                    except ValueError:
                        continue
                    if not isinstance(event, Mapping):
                        continue
                    props = event.get("properties")
                    props = props if isinstance(props, Mapping) else {}
                    # The stream is server-wide; ignore other sessions. Child
                    # sessions (subagents) are read separately via /children.
                    event_session = str(props.get("sessionID") or "")
                    if event_session and event_session != session_id:
                        continue

                    kind = str(event.get("type") or "")
                    if kind == "session.error":
                        # Keep the first error of the turn. opencode emits the
                        # failure once before `session.idle` and often repeats
                        # it afterwards with a bundler stack appended; the
                        # first one is both authoritative and the cleaner text,
                        # and the repeat lands after we have already stopped.
                        error = error or error_text(props.get("error"))
                        continue
                    if kind == "session.idle":
                        break

                    for converted in self._event_to_stream(event):
                        saw_output = saw_output or converted.type in {"text", "tool_use"}
                        yield converted
        except httpx.HTTPError as exc:
            yield ResultEvent(
                type="result",
                result=f"opencode connection failed: {exc}",
                session_id=session_id,
                is_error=True,
            )
            return
        finally:
            register_handle(None)

        yield ResultEvent(
            type="result",
            # A successful turn carries the accumulated answer (codex-style):
            # `record_turn` persists it as the durable transcript's response,
            # which is what the PWA replays when the session is unreadable.
            result=error or self._answer_text(),
            session_id=session_id,
            is_error=bool(error),
            effective_model=self._effective_model or request.model,
            # Accumulated from the assistant message's own totals rather than
            # summed per step, so a retried step cannot double-count.
            usage=self._usage,
            cost_usd=self._cost,
            fallback_final=bool(error) and saw_output,
        )

    # ----------------------------------------------------------- provider API

    @classmethod
    async def model_catalog(
        cls, workspace_root: Path, *, force: bool = False
    ) -> list[dict[str, Any]]:
        """Models the signed-in opencode account can currently reach.

        Runs a short-lived server rather than reusing a chat's: the catalog is
        queried from Settings, where no chat is necessarily open.
        """
        key = str(workspace_root)
        cached = _MODEL_CACHE.get(key)
        if cached and not force:
            ttl = _MODEL_CACHE_TTL if cached[1] else _EMPTY_MODEL_CACHE_TTL
            if time.monotonic() - cached[0] < ttl:
                return [dict(item) for item in cached[1]]
        payload: object = None
        async with _EphemeralServer(workspace_root) as client:
            if client is not None:
                try:
                    response = await client.get("/provider")
                    response.raise_for_status()
                    payload = response.json()
                except (httpx.HTTPError, ValueError):
                    payload = None
        catalog = _catalog_from_providers(payload)
        _log_catalog_change(key, cached[1] if cached else None, catalog)
        # Every outcome is cached, empties included — an empty result is the
        # expensive one to recompute (a server spawn, or the full health-poll
        # deadline when the binary exists but never answers), and /api/models is
        # on the PWA's load path. `_EMPTY_MODEL_CACHE_TTL` keeps that short so
        # models appear seconds after opencode starts working, not minutes.
        _MODEL_CACHE[key] = (time.monotonic(), catalog)
        return catalog

    @classmethod
    async def read_thread(
        cls, workspace_root: Path, session_id: str
    ) -> dict[str, Any]:
        """Session metadata plus its message history, for transcript replay."""
        if not session_id:
            return {}
        key = (str(workspace_root), session_id)
        cached = _THREAD_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < _READ_CACHE_TTL:
            return cached[1]
        thread: dict[str, Any] = {}
        async with _EphemeralServer(workspace_root) as client:
            if client is None:
                return {}
            try:
                info = await client.get(f"/session/{session_id}")
                info.raise_for_status()
                messages = await client.get(f"/session/{session_id}/message")
                messages.raise_for_status()
                thread = {"info": info.json(), "messages": messages.json()}
            except (httpx.HTTPError, ValueError):
                thread = {}
        _THREAD_CACHE[key] = (time.monotonic(), thread)
        return thread

    @classmethod
    async def read_collab_tree(
        cls, workspace_root: Path, session_id: str
    ) -> list[dict[str, Any]]:
        """Child sessions — opencode's background subagents — with their history.

        Each entry is ``{"info": <child session>, "messages": [...]}`` — the
        same shape :meth:`read_thread` returns — fetched over the one ephemeral
        server rather than a server spawn per child.
        """
        if not session_id:
            return []
        key = (str(workspace_root), session_id)
        cached = _COLLAB_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < _READ_CACHE_TTL:
            return cached[1]

        async def _child_messages(client: Any, child_id: str) -> list[Any]:
            try:
                messages = await client.get(f"/session/{child_id}/message")
                messages.raise_for_status()
                payload = messages.json()
            except (httpx.HTTPError, ValueError):
                return []
            return payload if isinstance(payload, list) else []

        result: list[dict[str, Any]] = []
        async with _EphemeralServer(workspace_root) as client:
            if client is None:
                return []
            try:
                response = await client.get(f"/session/{session_id}/children")
                response.raise_for_status()
                children = response.json()
            except (httpx.HTTPError, ValueError):
                return []
            if not isinstance(children, list):
                return []
            children = [
                child for child in children
                if isinstance(child, dict) and child.get("id")
            ]
            histories = await asyncio.gather(
                *(_child_messages(client, str(child["id"])) for child in children)
            )
            result = [
                {"info": child, "messages": messages}
                for child, messages in zip(children, histories)
            ]
        _COLLAB_CACHE[key] = (time.monotonic(), result)
        return result

    @classmethod
    async def delete_thread(cls, workspace_root: Path, session_id: str) -> bool:
        async with _EphemeralServer(workspace_root) as client:
            if client is None or not session_id:
                return False
            try:
                response = await client.delete(f"/session/{session_id}")
                return response.status_code < 400
            except httpx.HTTPError:
                return False


def opencode_tier_models(
    catalog: Sequence[Mapping[str, Any]],
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Map tier aliases onto concrete ``providerID/modelID`` ids from the catalog.

    opencode addresses every model as ``providerID/modelID``, so a bare tier
    alias is not a model id it can serve -- it would be sent as modelID
    ``sonnet`` under an empty provider. This is what turns the alias into
    something opencode recognises.

    An operator pin (Settings -> Models -> model routing) wins, but only while
    that model is still in the catalog: a pin left behind by a disconnected
    provider must not outvote a model the user can actually run. Otherwise the
    tier name is matched inside the model id, which works because opencode's
    catalog is mostly vendor-named models (``anthropic/claude-sonnet-4-6``).

    A tier with no pin and no match is **absent from the result**, deliberately.
    opencode is bring-your-own-provider and a catalog of Llama or Qwen models
    has no "opus"; guessing one would silently run a model the user did not ask
    for, so the caller omits the model instead and lets opencode pick its own
    default.
    """
    available = [str(row.get("model") or "") for row in catalog]
    in_catalog = set(available)
    resolved: dict[str, str] = {}
    for tier in MODEL_TIERS:
        pinned = (overrides or {}).get(tier, "")
        if pinned and pinned in in_catalog:
            resolved[tier] = pinned
            continue
        match = next((m for m in available if tier in m.lower()), "")
        if match:
            resolved[tier] = match
    return resolved


def catalog_providers(catalog: Sequence[Mapping[str, Any]]) -> set[str]:
    """The provider ids represented in a catalog, from its ``provider/model`` rows."""
    return {
        str(row.get("model", "")).split("/", 1)[0]
        for row in catalog
        if "/" in str(row.get("model", ""))
    }


def _log_catalog_change(
    key: str,
    previous: Sequence[Mapping[str, Any]] | None,
    catalog: Sequence[Mapping[str, Any]],
) -> None:
    """Record which opencode providers came or went.

    opencode is bring-your-own-provider and its catalog is read-through -- there
    is no stored list to inspect after the fact -- so without this a user who
    connects or loses a provider has nothing in the log explaining why their
    model list changed. Logged only on a real change, so a healthy install is
    quiet across the 5-minute refresh.
    """
    now = catalog_providers(catalog)
    if previous is None:
        if now:
            logger.info(
                "opencode catalog: %d model(s) from %s",
                len(catalog),
                ", ".join(sorted(now)),
            )
        return
    before = catalog_providers(previous)
    if now == before:
        return
    added = sorted(now - before)
    removed = sorted(before - now)
    parts = []
    if added:
        parts.append(f"connected {', '.join(added)}")
    if removed:
        parts.append(f"lost {', '.join(removed)}")
    logger.info(
        "opencode providers changed: %s (%d model(s) now reachable)",
        "; ".join(parts),
        len(catalog),
    )


def model_accepts_images(model: Mapping[str, Any]) -> bool | None:
    """Whether an opencode catalog entry accepts image input.

    Reads the entry's own ``capabilities`` block, which opencode sources from
    models.dev::

        "capabilities": {"attachment": false, "toolcall": true,
                         "input": {"text": true, "image": false, ...}}

    ``capabilities.input.image`` is the authoritative per-model answer, so this
    replaces guessing from model-name families. ``None`` means "the build did
    not say": older opencode versions omit the block, and an unknown answer must
    not be reported as a refusal -- the caller treats it as capable and lets the
    upstream reject the attachment itself if it really cannot take one.
    """
    capabilities = model.get("capabilities")
    if not isinstance(capabilities, Mapping):
        return None
    inputs = capabilities.get("input")
    if isinstance(inputs, Mapping) and isinstance(inputs.get("image"), bool):
        return bool(inputs["image"])
    # Some entries carry only the coarser `attachment` flag. It covers any
    # non-text input (pdf, audio, image), so it can only rule vision *out*:
    # attachment=false is a reliable no, attachment=true is not a reliable yes.
    if capabilities.get("attachment") is False:
        return False
    return None


def _catalog_from_providers(payload: object) -> list[dict[str, Any]]:
    """Flatten ``GET /provider`` into Ciaobot's ``{model, label}`` rows.

    Only *connected* providers contribute: opencode's ``all`` list enumerates
    every backend it knows how to talk to (hundreds), which is a catalog of
    possibilities rather than of models the user can actually run.
    """
    if not isinstance(payload, Mapping):
        return []
    connected = payload.get("connected")
    # An empty `connected` list means "nothing authenticated yet", which is a
    # real state (a fresh install reports exactly that) and must yield no
    # models. Only a *missing* key means the build does not report the
    # distinction, in which case fall back to listing everything.
    filter_connected = isinstance(connected, list)
    connected_ids = {str(item) for item in connected} if isinstance(connected, list) else set()
    rows: list[dict[str, Any]] = []
    for provider in payload.get("all") or []:
        if not isinstance(provider, Mapping):
            continue
        provider_id = str(provider.get("id") or "")
        if filter_connected and provider_id not in connected_ids:
            continue
        models = provider.get("models")
        entries = models.values() if isinstance(models, Mapping) else (models or [])
        for model in entries:
            if not isinstance(model, Mapping):
                continue
            model_id = str(model.get("id") or "")
            if not model_id:
                continue
            variants = model.get("variants")
            row: dict[str, Any] = {
                "model": f"{provider_id}/{model_id}",
                "label": f"{model.get('name') or model_id} ({provider_id})",
                # Reasoning-effort variants this model accepts, e.g.
                # ["low", "medium", "high"]. Empty for models with no effort
                # control; opencode calls the parameter `variant`.
                "variants": sorted(variants) if isinstance(variants, Mapping) else [],
            }
            # Only stated when opencode said so. Omitting the key rather than
            # defaulting it keeps "unknown" distinguishable from "no" for the
            # image pre-flight, which must not refuse an attachment on a guess.
            accepts_images = model_accepts_images(model)
            if accepts_images is not None:
                row["images"] = accepts_images
            rows.append(row)
    return rows


class _EphemeralServer:
    """Async context manager running a throwaway ``opencode serve``.

    Used by the classmethod read paths (model catalog, history, child
    sessions), which run outside any chat and so have no long-lived server.
    Yields ``None`` when opencode is not installed, so callers degrade to an
    empty result instead of raising into a settings route.
    """

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root
        self._process: asyncio.subprocess.Process | None = None
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> httpx.AsyncClient | None:
        binary = resolve_opencode_binary()
        if not binary:
            return None
        port = _free_port()
        password = secrets.token_urlsafe(24)
        try:
            self._process = await asyncio.create_subprocess_exec(
                binary, "serve", "--port", str(port), "--hostname", "127.0.0.1",
                cwd=str(self._workspace_root),
                env={**os.environ, "OPENCODE_SERVER_PASSWORD": password},
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return None
        self._client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            auth=("opencode", password),
            timeout=httpx.Timeout(_REQUEST_TIMEOUT),
        )
        deadline = asyncio.get_running_loop().time() + _SERVER_START_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            if self._process.returncode is not None:
                return None
            try:
                response = await self._client.get("/global/health", timeout=2.0)
                if response.status_code == 200:
                    return self._client
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
        return None

    async def __aexit__(self, *_exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=_SHUTDOWN_TIMEOUT)
            except (TimeoutError, asyncio.TimeoutError):
                process.kill()
                await process.wait()


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CREDENTIAL_COUNT_RE = re.compile(r"(\d+)\s+credentials?\b", re.IGNORECASE)


def _credential_count(binary: str, *, timeout: float) -> int | None:
    """How many provider credentials opencode has stored, or None if unknown.

    Reads the count opencode itself prints (`0 credentials`). The output is a
    decorated TUI box — ANSI codes and box-drawing characters — so counting
    non-empty lines counts the decoration, which is how this once reported
    "10 provider(s) authenticated" against an empty store.

    `~/.local/share/opencode/auth.json` is deliberately not read: parsing a
    provider's cached credential file to determine identity is out of bounds
    (see docs/plans/GEMINI_CLI_PROVIDER_PLAN.md).
    """
    import subprocess

    try:
        listed = subprocess.run(
            [binary, "auth", "list"], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _CREDENTIAL_COUNT_RE.search(_ANSI_RE.sub("", listed.stdout))
    return int(match.group(1)) if match else None


def opencode_login_status(
    env: Mapping[str, str] | None = None, *, timeout: float = 5.0
) -> dict[str, Any]:
    """Bounded, credential-free opencode install/auth status for Settings."""
    import subprocess

    from ciao.setup_status import _provider

    binary = resolve_opencode_binary(env)
    if not binary:
        return _provider(
            name="opencode",
            ok=False,
            auth="missing",
            command="opencode",
            detail="not installed",
            version="not installed",
        )
    version = ""
    try:
        result = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=timeout
        )
        version = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    except (OSError, subprocess.SubprocessError):
        version = ""
    credentials = _credential_count(binary, timeout=timeout)
    # opencode's own free tier serves models with no credentials at all, so an
    # empty credential store still means "usable" — it does not mean "not set
    # up". Say which it is rather than implying the user has connected
    # something they have not.
    if credentials is None:
        detail = "installed; credential state unknown"
        auth = "unknown"
    elif credentials > 0:
        detail = f"{credentials} provider credential(s)"
        auth = "oauth"
    else:
        detail = "no credentials — free models only"
        auth = "free"
    return _provider(
        name="opencode",
        ok=True,
        auth=auth,
        command="opencode auth login",
        detail=detail,
        version=version or "unknown",
    )


def opencode_system_skills(env: Mapping[str, str] | None = None) -> list[str]:
    """Skills opencode's own CLI loads. It has no separate bundled catalog."""
    return []
