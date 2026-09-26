"""OpenCode 2.x provider over the local HTTP + SSE server.

Unlike Claude (in-process SDK), OpenCode ships a real multi-session HTTP
server. Ciaobot runs ``opencode serve`` on an ephemeral loopback port and
drives it over ``httpx``, consuming the ``/api/event`` SSE stream. OpenCode 1
is intentionally unsupported: V2 replaced the server API, response envelopes,
permission rules, event payloads, and model catalog, so carrying both wire
contracts would duplicate every provider operation.

**One server process per active chat.** A shared server is tempting because
OpenCode isolates chats as sessions, but Ciaobot scopes its control-plane MCP
token per chat, while OpenCode configures MCP at server scope. Per-session
agent, model, and permission rules keep chat behavior isolated.

The V2 wire contract is verified against the server's own OpenAPI document at
``/openapi.json`` on startup. Readiness and the installed major version come
from ``/api/info``; V1 and unknown server shapes fail closed with an explicit
upgrade message.

Capability note: OpenCode has no method that injects a message into a running
turn; Ciaobot keeps a mid-turn message in the next-turn queue. Everything else
Ciaobot needs — fork, interrupt, permissions, forms, and background subagents
as child sessions — is native.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import secrets
import socket
import time
from collections import deque
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from ciao.core_prompt import system_prompt_payload
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
    provider_reuse_key,
)
from ciao.providers.base import (
    ActiveHandle,
    BaseSDKProvider,
    ProviderCapabilities,
    build_prompt,
    build_runtime_context,
    prepend_stable_context,
)
from ciao.execution_modes import (
    opencode_credential_deny_rules,
)
from ciao.providers._sse import SSEDecoder
from ciao.tool_path import resolve_tool

logger = logging.getLogger(__name__)

# Operations Ciaobot cannot work without. Checked against the server's own
# OpenAPI paths at connect time so an incompatible build fails closed. This is
# the machine-checkable equivalent of the provider's protocol requirements.
REQUIRED_PATHS: frozenset[str] = frozenset({
    "/api/info",
    "/api/event",
    "/api/session",
    "/api/session/active",
    "/api/session/{sessionID}",
    "/api/session/{sessionID}/agent",
    "/api/session/{sessionID}/fork",
    "/api/session/{sessionID}/interrupt",
    "/api/session/{sessionID}/message",
    "/api/session/{sessionID}/model",
    "/api/session/{sessionID}/permission",
    "/api/session/{sessionID}/permission/{requestID}/reply",
    "/api/session/{sessionID}/prompt",
    "/api/session/{sessionID}/form",
    "/api/session/{sessionID}/form/{formID}",
    "/api/session/{sessionID}/form/{formID}/reply",
    "/api/provider",
    "/api/model",
    "/api/model/default",
})

OPENCODE_V2_REQUIRED = (
    "Ciaobot requires OpenCode 2.0.16 or newer 2.x. "
    "Update OpenCode, then retry this chat."
)

# The catalog needs a throwaway `opencode serve` (~1-2s), and /api/models is
# hit on every model-picker open. Cache it rather than paying
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
# How long the catalog keeps polling a fresh server whose model list is still
# empty. Loading took about 0.5s against three connected providers; an account
# with genuinely no models pays this once per `_EMPTY_MODEL_CACHE_TTL`.
_CATALOG_WARMUP_TIMEOUT = 5.0
_CATALOG_WARMUP_POLL = 0.25

# Session reads (`read_thread` / `read_collab_tree`) also cost a throwaway
# `opencode serve`. A chat with a live provider attached reuses that server
# instead (see `has_live_server` / `read_live_collab_tree`), so this classmethod
# path and its cache now mainly cover reads with nothing attached (an archived
# chat, or a chat viewed from another device). The PWA still polls the routes
# they back on short intervals (15s status sync, 4s for /subagents while a
# turn streams), so the TTL stays above that cadence to collapse bursts to
# roughly one spawn per tick rather than one per poll.
_READ_CACHE_TTL = 6.0
# A spawn that could not start at all (binary missing, process died, health
# never answered) is cached longer than a read: while opencode is in this
# state, every uncached read path spawns another doomed `opencode serve` and
# holds it for up to `_SERVER_START_TIMEOUT`, so an un-negative-cached
# `/subagents` poll stacks dying servers on every tick. The TTL stays well
# under a minute so reads recover promptly once opencode starts working.
_READ_FAILURE_CACHE_TTL = 20.0
# Cache entries carry their own TTL as ``(stamp, ttl, value)``: a failed
# spawn lives longer than a successful read (see `_READ_FAILURE_CACHE_TTL`).
_THREAD_CACHE: dict[tuple[str, str], tuple[float, float, dict[str, Any]]] = {}
_COLLAB_CACHE: dict[tuple[str, str], tuple[float, float, list[dict[str, Any]]]] = {}

_SERVER_START_TIMEOUT = 30.0
_SERVER_START_ATTEMPTS = 3
_SERVER_START_RETRY_DELAYS = (0.25, 0.75)
# `/api/info` can become healthy a moment before OpenCode finishes serving its
# generated OpenAPI document. Retry that narrow readiness race, not an
# incompatible (valid JSON) contract.
_OPENAPI_READY_ATTEMPTS = 5
_OPENAPI_READY_DELAY_S = 0.2
_REQUEST_TIMEOUT = 30.0
# Mid-turn SSE recovery: re-subscribe attempts after a dropped /event stream,
# then a bounded message-poll window that replays settled parts idempotently.
_OPENCODE_SSE_RECONNECTS = 3
_OPENCODE_RECOVERY_WINDOW_S = 60.0
_OPENCODE_RECOVERY_POLL_S = 2.5


def _opencode_messages_signature(messages: list[Any]) -> str:
    """Change detector for this turn's projected assistant messages.

    A user-only projection is deliberately an empty signature: two matching
    polls before the first assistant row must not look like a finished turn.
    Tool state and assistant completion are part of the signature so a running
    tool cannot be mistaken for settled output.
    """
    pieces: list[str] = []
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        info = message.get("info")
        if not isinstance(info, Mapping) or info.get("role") != "assistant":
            continue
        pieces.append(
            f"message={info.get('id')}/completed="
            f"{((info.get('time') or {}).get('completed') if isinstance(info.get('time'), Mapping) else None)}"
        )
        parts = message.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, Mapping):
                continue
            state = part.get("state")
            state = state if isinstance(state, Mapping) else {}
            text = part.get("text")
            pieces.append(
                f"{part.get('id')}/{part.get('type')}/"
                f"{len(text) if isinstance(text, str) else 0}/"
                f"{state.get('status')}"
            )
    return "|".join(pieces)
_SHUTDOWN_TIMEOUT = 5.0
_SERVER_START_LOCKS: dict[str, asyncio.Lock] = {}
_CATALOG_LOCKS: dict[str, asyncio.Lock] = {}
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

# Per-session permission rulesets use OpenCode 2's ordered
# ``{action, resource, effect}`` shape. Resolution is last-match-wins, so the
# wildcard goes first and specific grants follow. ``auto`` allows routine work
# while keeping every shell command behind an operator approval card.
_READ_ONLY_TOOLS = ("read", "glob", "grep")
# V2 sends the glob pattern/regex itself as the permission resource; it does
# not send the search root or result paths. Require an explicit operator card
# for both search actions in every mode, including bypass, so a broad search
# cannot silently enumerate credential-bearing files. Path-shaped deny rules
# below still hard-deny direct protected glob patterns after this ask rule.
_SEARCH_PERMISSION_RULES: tuple[dict[str, str], ...] = (
    {"action": "glob", "resource": "*", "effect": "ask"},
    {"action": "grep", "resource": "*", "effect": "ask"},
)

# Permission changes cannot be patched onto an existing opencode session.
# Keep the replacement-session handover bounded so a long-running chat does
# not turn one mode switch into an unbounded prompt.
_SESSION_HANDOVER_MAX_MESSAGES = 40
_SESSION_HANDOVER_MAX_CHARS = 24_000


def _rules(*entries: tuple[str, str]) -> list[dict[str, str]]:
    return [
        {"action": action, "resource": "*", "effect": effect}
        for action, effect in entries
    ]


def _permissive_auto_rules() -> list[dict[str, str]]:
    """Allow routine work while routing every shell command to the operator."""
    return _rules(
        ("*", "allow"),
        ("shell", "ask"),
    )


_MODE_PERMISSIONS: dict[str, list[dict[str, str]]] = {
    "plan": _rules(("*", "ask"), *((tool, "allow") for tool in _READ_ONLY_TOOLS)),
    "normal": _rules(("*", "ask")),
    "auto": _permissive_auto_rules(),
    "bypass": _rules(("*", "allow")),
}


@dataclass(frozen=True, slots=True)
class OpencodeSettings:
    """Operator override for the opencode default model.

    Empty string means "no override": the default falls through to whatever
    model the session's configured provider resolves. Mirrors
    so ``AppSettings.provider_default_models`` can drive it the same way.
    """

    default_model: str = ""


def opencode_default_model(config: object) -> str:
    """The operator's opencode default model, or ``""`` when there is none."""
    settings = getattr(config, "opencode", None)
    if settings is None:
        return ""
    return str(getattr(settings, "default_model", "") or "")


def resolve_opencode_binary(env: Mapping[str, str] | None = None) -> str | None:
    """Absolute path to the opencode CLI, or None when it is not installed."""
    source: Mapping[str, str] = {**os.environ, **env} if env else os.environ
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


def _catalog_lock(workspace_root: Path) -> asyncio.Lock:
    key = str(workspace_root.resolve())
    lock = _CATALOG_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _CATALOG_LOCKS[key] = lock
    return lock


def _version_number(value: object) -> tuple[int, ...] | None:
    """Parse ``2.0.16`` or CLI output ``opencode v2.0.16``."""
    match = re.search(r"(?<!\d)v?(\d+(?:\.\d+)+)", str(value or ""))
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def _server_version_error(payload: object) -> str | None:
    """Return the user-facing error when ``/api/info`` is too old or not V2."""
    if not isinstance(payload, Mapping):
        return OPENCODE_V2_REQUIRED
    version = str(payload.get("version") or "").strip()
    parsed = _version_number(version)
    if parsed is None or parsed < (2, 0, 16) or parsed[0] != 2:
        detail = f" Installed server version: {version}." if version else ""
        return f"{OPENCODE_V2_REQUIRED}{detail}"
    return None


def _health_failure_reason(
    last_status: int | None, last_error: Exception | None
) -> str:
    """A human-readable cause for a server that never became healthy."""
    if last_status is not None:
        return f"health returned HTTP {last_status}"
    if last_error is not None:
        return str(last_error)
    return "server stayed alive but never answered /api/info"


def _is_transient_startup_error(exc: BaseException) -> bool:
    """Whether a failed server launch is likely to recover on retry.

    A server that wedges on startup (exits, or never becomes healthy) can
    clear once the shared database contention it hit settles, so both the
    database-lock exit and the never-healthy timeout are treated as
    retriable. Everything else — a missing binary, a contract mismatch — is
    terminal.
    """
    text = str(exc).lower()
    if "database is locked" in text or "database is busy" in text:
        return True
    return "did not become healthy" in text


def missing_required_paths(spec: Mapping[str, Any]) -> tuple[str, ...]:
    """Required operations absent from a served OpenAPI document."""
    paths = spec.get("paths")
    available = set(paths) if isinstance(paths, Mapping) else set()
    return tuple(sorted(REQUIRED_PATHS - available))


_CIAO_CONTEXT_BLOCK_RE = re.compile(
    r"^\[CIAO_CONTEXT_BEGIN\]\n.*?\n\[CIAO_CONTEXT_END\]\n\n",
    re.DOTALL,
)
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
    """Human-readable text from an OpenCode 2 structured error."""
    if not isinstance(error, Mapping):
        return "OpenCode reported an error"
    return _sanitize_error(error.get("message")) or str(
        error.get("type") or "OpenCode error"
    )


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


def _context_window_for(payload: object, provider_id: str, model_id: str) -> int | None:
    """The model's ``limit.context`` from V2's flat model snapshot."""
    models = _data(payload)
    if not isinstance(models, list):
        return None
    for model in models:
        if not isinstance(model, Mapping):
            continue
        if (
            str(model.get("providerID") or "") != provider_id
            or str(model.get("modelID") or "") != model_id
        ):
            continue
        limit = model.get("limit")
        context = limit.get("context") if isinstance(limit, Mapping) else None
        if (
            isinstance(context, (int, float))
            and not isinstance(context, bool)
            and context > 0
        ):
            return int(context)
        return None
    return None


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
        # OpenCode writes one assistant message per model call and sets its
        # tokens from that call alone (not a turn sum), so the last message's
        # total (input + output + reasoning + cache) is the current context
        # size: the figure OpenCode's own UI puts over the model's window.
        ("totalTokens", tokens.get("total")),
    ):
        count = _token_count(source)
        if count:
            usage[key] = str(count)
    if "totalTokens" not in usage:
        total = sum(_token_count(tokens.get(key)) for key in ("input", "output", "reasoning"))
        total += _token_count(cache.get("read")) + _token_count(cache.get("write"))
        if total:
            usage["totalTokens"] = str(total)
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


def mode_settings(
    mode: BridgeMode,
    *,
    tools_enabled: bool = True,
    runtime_root: object = None,
    workspace_root: object = None,
) -> tuple[str, list[dict[str, str]]]:
    """Map a Ciaobot mode onto an opencode (agent, permission ruleset).

    One-shot routines set ``tools_enabled=False``. A deny-all session rule is
    the opencode API's tool-disable mechanism: unlike plan mode it does not
    allow read/glob/grep/list to reach the provider at all.

    ``runtime_root`` is the resolved runtime directory, when the caller can
    reach it, so the credential denies cover a relocated
    ``CIAO_RUNTIME_ROOT`` and not only the default ``.runtime`` name. When a
    workspace location is known, relative V2 aliases are included as well.

    Since S6 every chat is on the CLI surface. Auto mode does not pre-approve
    any ``ciao …`` argv prefix: an allow rule is a prefix a shell suffix
    (``ciao help >/dev/null; <cmd>``) could ride past, so bash stays ``ask``
    and every shell command, including ``ciao …``, keeps a card. Users who want
    no routine cards switch to ``bypass``; V2 glob/grep search actions still
    require an explicit card because their resources are not paths.
    """
    key = mode if mode in _MODE_AGENTS else "normal"
    if not tools_enabled:
        return _MODE_AGENTS[key], _rules(("*", "deny"))
    rules = [dict(rule) for rule in _MODE_PERMISSIONS[key]]
    # Search resources in V2 are patterns/regexes rather than paths. Keep
    # search operations behind an explicit card even in bypass mode; otherwise
    # a broad glob/grep can enumerate secrets without any operator decision.
    rules.extend(dict(rule) for rule in _SEARCH_PERMISSION_RULES)
    # Last, and for every mode including `bypass`: resolution is
    # last-match-wins, and this is the one carve-out no mode may buy its way
    # out of. See `opencode_credential_deny_rules`.
    rules.extend(opencode_credential_deny_rules(runtime_root, workspace_root))
    return _MODE_AGENTS[key], rules


# Actions a memory pass needs: it reads and edits the vault and shells out
# to `ciao`/`ciao vault search`. Everything else is denied by the leading
# wildcard, which is also what blocks MCP (opencode's permission union has
# no `mcp` action) and the search actions the pass must not use.
_MEMORY_PASS_ALLOWED_ACTIONS: tuple[str, ...] = (
    "read", "edit", "write", "shell", "bash",
    "external_directory", "list", "question", "skill",
)


def memory_pass_guardrail_rules(
    runtime_root: object = None, workspace_root: object = None
) -> list[dict[str, str]]:
    """The session ruleset the end-of-conversation memory pass runs under.

    An allow-list, not the ``bypass`` mode's blanket allow: the pass reads and
    edits the vault and shells out to ``ciao``, and nothing else. A leading
    wildcard ``deny`` plus explicit grants is what denies MCP here — opencode's
    permission union has no ``mcp`` action to name.

    ``glob`` and ``grep`` are denied outright rather than left at ``ask``:
    opencode always asks for them even in ``bypass`` (7 of 12 agent chats in
    the #594 experiment stopped on that card), and V2 sends the pattern as the
    resource, so a search cannot be scoped to the vault anyway. The pass uses
    ``ciao vault search``.

    Same order as :func:`mode_settings`: wildcard first, specific grants after,
    and the credential denies last because opencode resolves last-match-wins.
    """
    rules = _rules(("*", "deny"))
    rules.extend(
        {"action": action, "resource": "*", "effect": "allow"}
        for action in _MEMORY_PASS_ALLOWED_ACTIONS
    )
    # Search stays denied even though `read` is allowed: V2 sends the
    # pattern as the resource, so it cannot be scoped to the vault.
    rules.append({"action": "glob", "resource": "*", "effect": "deny"})
    rules.append({"action": "grep", "resource": "*", "effect": "deny"})
    rules.append({"action": "skill", "resource": "gws-*", "effect": "deny"})
    rules.extend(opencode_credential_deny_rules(runtime_root, workspace_root))
    return rules


def readonly_agent_rules(roots: Sequence[Path]) -> list[dict[str, str]]:
    """Deny everything except reading inside ``roots`` (read-only memory agent).

    The opencode counterpart of the Claude read-only tool gate
    (``ciao.providers.oneshot.path_allowed``): same question, answered by the
    server's own ruleset. ``glob`` and ``grep`` stay denied even with the
    roots allowed, because V2 sends the *search pattern* as the resource
    rather than the search root, so a search cannot be scoped to a directory
    at all. The agent reads notes by path instead.
    """
    rules = _rules(("*", "deny"))
    for root in roots:
        base = str(Path(root).resolve())
        for action in ("read", "external_directory"):
            rules.append({"action": action, "resource": base, "effect": "allow"})
            rules.append({"action": action, "resource": f"{base}/**", "effect": "allow"})
    rules.extend(opencode_credential_deny_rules())
    return rules


def _session_permission_matches(
    payload: object, expected: list[dict[str, str]]
) -> bool:
    """Return whether a session exposes exactly the rules for this turn."""
    if not isinstance(payload, Mapping):
        return False
    actual = payload.get("permissions")
    return isinstance(actual, list) and actual == expected


def _strip_prompt_context(text: str) -> str:
    """Remove Ciaobot's transcript-only V2 prompt context prefix."""
    stripped = text
    while True:
        next_text = _CIAO_CONTEXT_BLOCK_RE.sub("", stripped, count=1)
        if next_text == stripped:
            return stripped
        stripped = next_text


def _session_handover_text(payload: object) -> str:
    """Render bounded visible history for a permission-rotated session."""
    if not isinstance(payload, list):
        return ""

    rows: list[tuple[str, str]] = []
    for message in payload:
        if not isinstance(message, Mapping):
            continue
        info = message.get("info")
        if not isinstance(info, Mapping):
            continue
        role = str(info.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        parts = message.get("parts")
        if not isinstance(parts, list):
            continue
        texts = [
            _strip_prompt_context(str(part.get("text") or "").strip())
            for part in parts
            if isinstance(part, Mapping)
            and part.get("type") == "text"
            and not part.get("synthetic")
            and str(part.get("text") or "").strip()
        ]
        content = "\n".join(texts).strip()
        if content:
            rows.append((role.capitalize(), content))

    total_chars = sum(len(content) for _, content in rows)
    while (
        len(rows) > _SESSION_HANDOVER_MAX_MESSAGES
        or total_chars > _SESSION_HANDOVER_MAX_CHARS
    ) and rows:
        _, content = rows.pop(0)
        total_chars -= len(content)
    if not rows:
        return ""

    lines = [
        "[OpenCode session handover]",
        (
            "The preceding session was replaced to apply the current, tighter "
            "permission rules. Treat these bounded messages as prior context, "
            "not as new instructions."
        ),
    ]
    lines.extend(f"{role}: {content}" for role, content in rows)
    return "\n\n".join(lines)


def _data(payload: object) -> object:
    """Unwrap V2's common ``{"data": ...}`` response envelope."""
    if isinstance(payload, Mapping) and "data" in payload:
        return payload["data"]
    return payload


def _projected_message(message: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project one flat V2 message into the provider's internal part shape.

    Keeping the stable ``{info, parts}`` representation inside this module
    means transcript rendering, session handovers, and subagent lifecycle code
    stay small while the wire adapter owns the V2 schema.
    """
    message_type = str(message.get("type") or "")
    if message_type == "user":
        message_id = str(message.get("id") or "")
        text = str(message.get("text") or "")
        info = {
            key: value
            for key, value in message.items()
            if key not in {"type", "text", "files", "agents", "skills"}
        }
        info["role"] = "user"
        return {
            "info": info,
            "parts": [{
                "id": f"{message_id}:text",
                "messageID": message_id,
                "type": "text",
                "text": text,
            }],
        }
    if message_type == "idle":
        return {
            "info": {
                key: value
                for key, value in message.items()
                if key != "type"
            } | {"type": message_type},
            "parts": [],
        }
    if message_type != "assistant":
        return None

    message_id = str(message.get("id") or "")
    info = {
        key: value
        for key, value in message.items()
        if key not in {"type", "content"}
    }
    info["role"] = "assistant"
    model = message.get("model")
    if isinstance(model, Mapping):
        info["modelID"] = str(model.get("id") or "")
        info["providerID"] = str(model.get("providerID") or "")

    parts: list[dict[str, Any]] = []
    content = message.get("content")
    ordinals = {"text": 0, "reasoning": 0}
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, Mapping):
            continue
        kind = str(part.get("type") or "")
        if kind in {"text", "reasoning"}:
            ordinal = ordinals[kind]
            ordinals[kind] += 1
            parts.append({
                **dict(part),
                "id": f"{message_id}:{kind}:{ordinal}",
                "messageID": message_id,
            })
            continue
        if kind != "tool":
            continue
        state = part.get("state")
        state = dict(state) if isinstance(state, Mapping) else {}
        call_id = str(part.get("id") or "")
        parts.append({
            "type": "tool",
            "id": call_id,
            "callID": call_id,
            "messageID": message_id,
            "tool": str(part.get("name") or "tool"),
            "state": state,
        })
    return {"info": info, "parts": parts}


async def _read_v2_messages(
    client: httpx.AsyncClient, session_id: str
) -> list[dict[str, Any]]:
    """Read every V2 message in chronological order and normalize each row."""
    messages: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        params: dict[str, str] = {"limit": "100"}
        if cursor:
            # V2 rejects combining cursor with order; the cursor already
            # encodes the direction chosen by the first page.
            params["cursor"] = cursor
        else:
            params["order"] = "asc"
        response = await client.get(
            f"/api/session/{session_id}/message", params=params
        )
        response.raise_for_status()
        body = response.json()
        page = _data(body)
        if not isinstance(page, list):
            break
        for message in page:
            if not isinstance(message, Mapping):
                continue
            normalized = _projected_message(message)
            if normalized is not None:
                messages.append(normalized)
        cursor_body = body.get("cursor") if isinstance(body, Mapping) else None
        next_cursor = (
            str(cursor_body.get("next") or "")
            if isinstance(cursor_body, Mapping)
            else ""
        )
        if not next_cursor:
            break
        if next_cursor in seen_cursors:
            raise RuntimeError("OpenCode pagination returned a repeated cursor")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return messages


async def _read_v2_children(
    client: httpx.AsyncClient, parent_id: str
) -> list[dict[str, Any]]:
    """Read every V2 child session for a parent, following cursors."""
    children: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        params: dict[str, str] = {
            "parentID": parent_id,
            "limit": "100",
        }
        if cursor:
            params["cursor"] = cursor
        response = await client.get("/api/session", params=params)
        response.raise_for_status()
        body = response.json()
        page = _data(body)
        if not isinstance(page, list):
            break
        children.extend(
            dict(child)
            for child in page
            if isinstance(child, Mapping) and child.get("id")
        )
        cursor_body = body.get("cursor") if isinstance(body, Mapping) else None
        next_cursor = (
            str(cursor_body.get("next") or "")
            if isinstance(cursor_body, Mapping)
            else ""
        )
        if not next_cursor:
            break
        if next_cursor in seen_cursors:
            raise RuntimeError("OpenCode pagination returned a repeated cursor")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return children


async def _read_active_sessions(client: httpx.AsyncClient) -> set[str]:
    """Session IDs currently executing in this OpenCode server process."""
    response = await client.get("/api/session/active")
    raise_for_status = getattr(response, "raise_for_status", None)
    if callable(raise_for_status):
        raise_for_status()
    active = _data(response.json())
    if not isinstance(active, Mapping):
        return set()
    return {
        str(session_id)
        for session_id, state in active.items()
        if isinstance(state, Mapping) and state.get("type") == "running"
    }


def _v2_part_key(props: Mapping[str, Any], kind: str = "") -> str:
    return (
        f"{props.get('assistantMessageID') or ''}:{kind}:"
        f"{props.get('ordinal') or 0}"
    )


def split_model(model: str) -> tuple[str, str]:
    """Split ``providerID/modelID`` into its parts.

    opencode addresses models as ``provider/model`` (e.g.
    ``anthropic/claude-sonnet-4-6``). A bare id has no provider; the caller
    resolves it against the V2 catalog before sending a model switch.
    """
    value = (model or "").strip()
    if not value:
        return "", ""
    provider, sep, rest = value.partition("/")
    if not sep or not rest:
        return "", value
    return provider, rest


def compose_system(developer_instructions: str, runtime: str) -> str:
    """Build the context prefix carried in V2's text-only prompt body.

    Instructions come first, runtime facts after. The provider wraps the result
    in transcript-only context markers because V2 has no stable per-prompt system
    field.
    """
    return "\n\n".join(
        part for part in (developer_instructions.strip(), runtime.strip()) if part
    )


class OpencodeActiveHandle(ActiveHandle):
    """Stops the in-flight turn by interrupting its V2 session."""

    def __init__(self, provider: "OpencodeProvider", session_id: str) -> None:
        self._provider = provider
        self._session_id = session_id

    async def stop(self) -> None:
        # Flag first: the streaming pump checks it on every event and at
        # every reconnect, so the local turn ends as soon as the interrupt is
        # issued rather than waiting for a terminal execution event over a
        # half-healthy SSE subscription.
        # Guarded: an empty id would put the sentinel back to a value the
        # pump's `== session_id` guard can match by accident (see
        # `_stop_requested`), and there is nothing to abort either way.
        if self._session_id:
            self._provider._stop_requested = self._session_id
            await self._provider.abort_session(self._session_id)


@dataclass(slots=True)
class _PendingRequest:
    """A permission or form awaiting the operator's reply."""

    request_id: str
    session_id: str
    tool_use_id: str = ""
    question_ids: tuple[str, ...] = ()
    question_fields: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class QuestionResponseResult:
    """Outcome of one native-form reply/cancel operation."""

    ok: bool
    error: str = ""
    retryable: bool = True


class _FormValidationError(ValueError):
    """The PWA answer cannot satisfy the V2 form schema."""


FormValue = str | int | float | bool | list[str]


def _form_option_values(field: Mapping[str, Any]) -> list[str]:
    options = field.get("options")
    if not isinstance(options, list):
        return []
    return [
        str(option.get("value"))
        for option in options
        if isinstance(option, Mapping) and option.get("value") is not None
    ]


def _form_answer_value(
    kind: str, values: Sequence[str]
) -> FormValue | None:
    """Coerce one PWA selection to the primitive expected by a V2 field."""
    items = [str(value) for value in values]
    if kind == "multiselect":
        return items
    if not items:
        return None
    value = items[0]
    if kind == "external":
        return True if value == "true" else None
    if kind == "boolean":
        if value in {"true", "1", "yes", "on"}:
            return True
        if value in {"false", "0", "no", "off"}:
            return False
        return None
    if kind == "integer":
        try:
            parsed_int = int(value)
        except ValueError:
            return None
        return parsed_int if math.isfinite(float(parsed_int)) else None
    if kind == "number":
        try:
            parsed_float = float(value)
        except ValueError:
            return None
        return parsed_float if math.isfinite(parsed_float) else None
    return value


def _form_field_active(
    field: Mapping[str, Any], answer: Mapping[str, object]
) -> bool:
    """Apply V2's strict, ordered ``when`` semantics."""
    conditions = field.get("when")
    if not isinstance(conditions, list):
        return True
    for condition in conditions:
        if not isinstance(condition, Mapping):
            return False
        key = str(condition.get("key") or "")
        if key not in answer:
            # V2 treats an unanswered reference as neither eq nor neq.
            return False
        actual = answer[key]
        expected = condition.get("value")
        if isinstance(actual, list):
            equal = expected in actual
        else:
            equal = actual == expected
        op = str(condition.get("op") or "eq")
        if op == "eq" and not equal:
            return False
        if op == "neq" and equal:
            return False
        if op not in {"eq", "neq"}:
            return False
    return True


def _validate_form_field(field: Mapping[str, Any], values: Sequence[str]) -> FormValue:
    kind = str(field.get("type") or "string")
    if kind != "multiselect" and len(values) > 1:
        raise _FormValidationError("Choose one answer")
    value = _form_answer_value(kind, values)
    if value is None:
        raise _FormValidationError("The answer has an invalid value")

    if kind == "multiselect":
        if not isinstance(value, list):
            raise _FormValidationError("Choose one or more options")
        items = [str(item) for item in value]
        minimum = field.get("minItems")
        maximum = field.get("maxItems")
        if isinstance(minimum, (int, float)) and len(items) < minimum:
            raise _FormValidationError("Choose more options")
        if isinstance(maximum, (int, float)) and len(items) > maximum:
            raise _FormValidationError("Choose fewer options")
        if not field.get("custom"):
            allowed = set(_form_option_values(field))
            if any(item not in allowed for item in items):
                raise _FormValidationError("Choose one of the available options")
        return items

    if kind == "external":
        if value is not True:
            raise _FormValidationError("Complete the external step first")
        return True

    if kind == "boolean":
        if not isinstance(value, bool):
            raise _FormValidationError("Choose yes or no")
        return value

    if kind in {"integer", "number"}:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise _FormValidationError("Enter a valid number")
        if kind == "integer" and not float(value).is_integer():
            raise _FormValidationError("Enter a whole number")
        minimum = field.get("minimum")
        maximum = field.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise _FormValidationError("Enter a number within the allowed range")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise _FormValidationError("Enter a number within the allowed range")
        return value

    if not isinstance(value, str):
        raise _FormValidationError("Enter a valid value")
    if field.get("required") and not value:
        raise _FormValidationError("This field is required")
    minimum = field.get("minLength")
    maximum = field.get("maxLength")
    if isinstance(minimum, int) and len(value) < minimum:
        raise _FormValidationError("Enter a longer value")
    if isinstance(maximum, int) and len(value) > maximum:
        raise _FormValidationError("Enter a shorter value")
    pattern = field.get("pattern")
    if isinstance(pattern, str) and pattern:
        try:
            if re.search(pattern, value) is None:
                raise _FormValidationError("Enter a value in the requested format")
        except re.error as exc:
            raise _FormValidationError("The form has an invalid validation rule") from exc
    fmt = str(field.get("format") or "")
    if fmt == "email" and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value) is None:
        raise _FormValidationError("Enter a valid email address")
    if fmt == "uri" and not urlparse(value).scheme:
        raise _FormValidationError("Enter a valid absolute URI")
    if fmt == "date":
        try:
            parsed_date = datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise _FormValidationError("Enter a valid date") from exc
        if parsed_date.strftime("%Y-%m-%d") != value:
            raise _FormValidationError("Enter a valid date")
    if fmt == "date-time":
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise _FormValidationError("Enter a valid date and time") from exc
    if not field.get("custom") and _form_option_values(field):
        if value not in set(_form_option_values(field)):
            raise _FormValidationError("Choose one of the available options")
    return value


def _validate_form_answer(
    pending: _PendingRequest, answers: Mapping[str, Sequence[str]]
) -> dict[str, FormValue]:
    known = set(pending.question_fields)
    unknown = set(answers) - known
    if unknown:
        raise _FormValidationError("The answer contains an unknown field")

    answer: dict[str, FormValue] = {}
    for question_id in pending.question_ids:
        field = pending.question_fields[question_id]
        active = _form_field_active(field, answer)
        if not active:
            if question_id in answers:
                raise _FormValidationError("An inactive field cannot be answered")
            continue
        values = answers.get(question_id, ())
        if not values:
            if field.get("required"):
                raise _FormValidationError("This field is required")
            continue
        # The PWA sends display labels for legacy cards and wire values for V2.
        # Prefer an exact option value, then map a legacy label to its value.
        option_map = {
            str(option.get("label") or ""): str(option.get("value"))
            for option in (field.get("options") or [])
            if isinstance(option, Mapping)
        }
        option_values = set(_form_option_values(field))
        # A wire value may also be another option's display label. Prefer an
        # exact value match before applying the legacy label-to-value map.
        mapped_values = [
            item if item in option_values else option_map.get(item, item)
            for item in values
        ]
        answer[question_id] = _validate_form_field(field, mapped_values)
    return answer


class OpencodeProvider(BaseSDKProvider):
    """Runs a chat turn against a per-chat ``opencode serve`` process."""

    capabilities = ProviderCapabilities(
        resume=True,
        fork=True,
        images=True,
        stop=True,
        permissions=True,
        structured_questions=True,
        dynamic_models=True,
        # Reasoning effort is per model (opencode calls it a model `variant`),
        # so the level list is narrowed per model from the catalog rather than
        # being a fixed ladder.
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
        tools_enabled: bool = True,
        permission_rules: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(workspace_root, config=config)
        # ``None`` means a normal Ciaobot chat and receives the compact shared
        # core below. A supplied string is an explicit one-shot instruction
        # (titles, insights, critique) and remains isolated from chat policy.
        self._developer_instructions = (
            None if developer_instructions is None else developer_instructions.strip()
        )
        self._tools_enabled = tools_enabled
        # A custom ruleset replaces the mode's one entirely. The read-only
        # memory agent needs exactly one shape, and no Ciaobot mode is it; see
        # `readonly_agent_rules`. `None` keeps `mode_settings`.
        self._permission_rules = permission_rules
        self._process: asyncio.subprocess.Process | None = None
        # Reads the server's stderr for its whole life; see
        # `_start_stderr_reader` for why leaving the pipe unread is not an option.
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        self._client: httpx.AsyncClient | None = None
        self._base_url: str = ""
        self._password: str = ""
        self._session_id: str = ""
        self._session_handover_context: str = ""
        self._permission_requests: dict[str, _PendingRequest] = {}
        self._question_requests: dict[str, _PendingRequest] = {}
        self._tool_calls: dict[str, str] = {}
        self._settled_tool_ids: set[str] = set()
        self._mcp_token: str = ""
        # Per-turn stream state, reset by `_reset_turn_state`.
        self._emitted: dict[str, int] = {}
        # `assistantMessageID:ordinal` -> reasoning/text. V2 uses separate
        # reasoning/text event families, but recovery replays normalized parts
        # through the same accumulator, so both paths share this lookup.
        self._part_types: dict[str, str] = {}
        self._user_message_id: str = ""
        self._usage: dict[str, str] = {}
        # ``session.usage.updated`` is a cumulative session snapshot, while
        # ``session.step.ended`` describes the latest model call. Keep the
        # latter separately for context-window occupancy.
        self._context_usage: dict[str, str] = {}
        self._cost: float | None = None
        # Visible assistant text, accumulated per part so the terminal
        # ResultEvent can carry the turn's answer. `record_turn`
        # persists that as the durable transcript's response, so leaving it
        # empty made replayed opencode chats render blank turns (#295).
        # Dict insertion order doubles as the part order.
        self._answer_parts: dict[str, list[str]] = {}
        # What opencode actually ran, as `providerID/modelID`. A workspace may
        # pin opencode without naming a model, in which case the request carries
        # none and only the assistant message says what was used.
        self._effective_model: str = ""
        # Qualified model selected for this turn; an empty pair means V2 should
        # use the session/server default.
        self._turn_model: tuple[str, str] = ("", "")
        # Session id whose turn the user asked to stop, or None. Set by the
        # active handle's stop() and consumed by the streaming pump so the
        # turn ends as soon as the interrupt is issued instead of waiting for
        # a terminal execution event that may never arrive over a flaky stream.
        #
        # None, not "": `_ensure_session` can hand back an empty id when the
        # server's response carries none, and with "" as the sentinel the
        # pump's `self._stop_requested == session_id` guard then read as
        # "stopped" on the first SSE event of a turn nobody stopped —
        # returning with `terminal_seen` set, which also skips the reconcile
        # backstop, for a silently empty turn.
        self._stop_requested: str | None = None

    def _reset_turn_state(self) -> None:
        self._emitted.clear()
        self._part_types.clear()
        self._tool_calls.clear()
        self._settled_tool_ids.clear()
        self._user_message_id = ""
        self._usage = {}
        self._context_usage = {}
        self._cost = None
        self._answer_parts.clear()
        self._effective_model = ""
        self._turn_recovered_via_poll = False
        self._poll_idle_outcome: str = ""
        self._poll_error: str = ""
        self._stop_requested = None

    # ---------------------------------------------------------------- server

    @property
    def current_session_id(self) -> str | None:
        return self._session_id or None

    @property
    def has_live_server(self) -> bool:
        """True while this chat's own opencode server is still running.

        Lets read paths (the subagents poll) reuse this connection instead of
        paying to spawn a throwaway server via ``_EphemeralServer`` every time
        the 3-second read cache misses.
        """
        return (
            self._client is not None
            and self._process is not None
            and self._process.returncode is None
        )

    async def read_live_collab_tree(self) -> list[dict[str, Any]]:
        """``read_collab_tree`` read over this chat's already-running server.

        Same shape as the classmethod, but skips ``_EphemeralServer`` entirely:
        while a chat is attached, its own server is already up, so spawning a
        second one just to poll subagent transcripts every few seconds wastes
        a process start each time. Callers should check ``has_live_server``
        first and fall back to the classmethod otherwise (e.g. a chat with no
        attached provider, viewed from another device or after a restart).
        """
        client = self._client
        if client is None or not self._session_id:
            return []

        async def _child_messages(child_id: str) -> list[dict[str, Any]]:
            try:
                return await _read_v2_messages(client, child_id)
            except (httpx.HTTPError, TypeError, ValueError):
                return []

        children_result, active_result = await asyncio.gather(
            _read_v2_children(client, self._session_id),
            _read_active_sessions(client),
            return_exceptions=True,
        )
        if isinstance(children_result, Exception):
            return []
        # A transient failure of the auxiliary activity map must not discard
        # successfully fetched children. ``None`` means activity is unknown;
        # the conservative counter will fall back to message timing.
        active_ids = (
            None
            if isinstance(active_result, Exception)
            else active_result
        )
        children = [child for child in children_result if child.get("id")]
        histories = await asyncio.gather(
            *(_child_messages(str(child["id"])) for child in children)
        )
        return [
            {
                "info": child,
                "messages": messages,
                "active": (
                    None
                    if active_ids is None
                    else str(child["id"]) in active_ids
                ),
            }
            for child, messages in zip(children, histories)
        ]

    @property
    def can_drain(self) -> bool:
        """opencode has no between-turns event source to drain."""
        return False

    def _chat_system_instructions(self) -> str:
        """Return the compact core for normal chats, never bounded memory."""
        payload = system_prompt_payload("") or {}
        return str(payload.get("append") or "")

    def _runtime_root(self) -> str:
        """The resolved runtime directory, or "" when it cannot be reached.

        Feeds the credential denies in ``mode_settings`` so a relocated
        ``CIAO_RUNTIME_ROOT`` is covered by path, not just by the default
        ``.runtime`` name. ``self.config`` is optional on this base class and
        unset in many tests, so a missing one degrades to the name-based
        patterns rather than raising.
        """
        state_path = getattr(self.config, "state_path", None)
        if not state_path:
            return ""
        try:
            return str(Path(state_path).parent.resolve())
        except (OSError, ValueError):
            return ""

    def _session_settings(
        self, request: AgentRequest
    ) -> tuple[str, list[dict[str, str]]]:
        """The (agent, permission rules) this turn's session runs under.

        A caller that supplied ``permission_rules`` is not running a Ciaobot
        mode — it is the read-only memory agent — so its ruleset is used
        verbatim and the mode only picks the agent. A memory pass runs under
        :func:`memory_pass_guardrail_rules` rather than its ``bypass`` mode's
        blanket allow. Everyone else gets :func:`mode_settings`, unchanged.
        """
        if self._permission_rules is not None:
            return "build", [dict(rule) for rule in self._permission_rules]
        if request.memory_pass:
            return "build", memory_pass_guardrail_rules(
                self._runtime_root(), self.workspace_root
            )
        return mode_settings(
            request.mode,
            tools_enabled=self._tools_enabled,
            runtime_root=self._runtime_root(),
            workspace_root=self.workspace_root,
        )

    async def _ensure_server(self, request: AgentRequest) -> httpx.AsyncClient:
        """Start (or reuse) this chat's server and return its HTTP client.

        A changed MCP token forces a full restart: opencode reads MCP
        configuration at server scope, so the running process cannot be
        re-pointed at a new token.
        """
        if self._client is not None and self._process is not None:
            if self._process.returncode is None and provider_reuse_key(request) == self._mcp_token:
                return self._client
            await self.disconnect()

        binary = resolve_opencode_binary(request.extra_env)
        if not binary:
            raise FileNotFoundError(
                "opencode CLI not found. Install it, make sure it is on your login shell PATH, "
                "or set CIAO_OPENCODE_BIN."
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
        self._mcp_token = provider_reuse_key(request)

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
        """Poll ``/api/info`` and require an OpenCode 2.x server."""
        assert self._client is not None
        deadline = asyncio.get_running_loop().time() + _SERVER_START_TIMEOUT
        last_error: Exception | None = None
        last_status: int | None = None
        while asyncio.get_running_loop().time() < deadline:
            if self._process is not None and self._process.returncode is not None:
                detail = await self._stderr_detail()
                raise RuntimeError(
                    f"OpenCode serve exited with code {self._process.returncode}"
                    + (f": {detail}" if detail else "")
                )
            try:
                response = await self._client.get("/api/info", timeout=2.0)
                last_status = response.status_code
                if response.status_code == 401:
                    raise RuntimeError("OpenCode server authentication failed")
                if response.status_code == 200:
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise RuntimeError(OPENCODE_V2_REQUIRED) from exc
                    version_error = _server_version_error(payload)
                    if version_error:
                        raise RuntimeError(version_error)
                    return
                if response.status_code == 404:
                    raise RuntimeError(OPENCODE_V2_REQUIRED)
            except httpx.HTTPError as exc:  # not up yet
                last_error = exc
                last_status = None
            await asyncio.sleep(0.2)
        reason = _health_failure_reason(last_status, last_error)
        raise TimeoutError(f"OpenCode serve did not become healthy: {reason}")

    async def _verify_contract(self) -> None:
        """Fail closed when the installed V2 build lacks required operations."""
        assert self._client is not None
        last_error: Exception | None = None
        spec: Any = None
        for attempt in range(_OPENAPI_READY_ATTEMPTS):
            try:
                response = await self._client.get("/openapi.json", timeout=10.0)
                response.raise_for_status()
                spec = response.json()
                break
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt + 1 < _OPENAPI_READY_ATTEMPTS:
                    await asyncio.sleep(_OPENAPI_READY_DELAY_S)
        else:
            raise RuntimeError(
                f"could not read the OpenCode API document: {last_error}"
            ) from last_error
        missing = missing_required_paths(spec)
        if missing:
            raise RuntimeError(
                "This OpenCode 2.x build is missing operations Ciaobot needs: "
                + ", ".join(missing)
            )

    async def disconnect(self) -> None:
        """Tear down the server, denying anything still awaiting a reply."""
        for pending in list(self._permission_requests.values()):
            await self._reply_permission(pending, "reject")
        for pending in list(self._question_requests.values()):
            await self._reject_question(pending)
        self._permission_requests.clear()
        self._question_requests.clear()
        self._tool_calls.clear()
        self._settled_tool_ids.clear()

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
                try:
                    process.kill()
                    await process.wait()
                except (ProcessLookupError, FileNotFoundError):
                    # The process already exited between the timeout and the
                    # kill; treat it as cleanly gone rather than surfacing a
                    # spurious task exception.
                    pass
        # After the process is gone, so the tail still explains a crash above.
        reader = self._stderr_task
        self._stderr_task = None
        if reader is not None:
            reader.cancel()
        self._base_url = ""
        self._mcp_token = ""
        self._session_handover_context = ""
        self._reset_settings()

    async def delete_current_session(self) -> bool:
        """Delete this provider's current session while the server is alive."""
        client = self._client
        session_id = self._session_id
        if client is None or not session_id:
            return False
        try:
            response = await client.delete(f"/api/session/{session_id}")
        except httpx.HTTPError:
            logger.debug("opencode session deletion failed for %s", session_id, exc_info=True)
            return False
        deleted = response.status_code < 400
        if deleted:
            self._session_id = ""
        else:
            logger.debug(
                "opencode refused to delete session %s (status %s)",
                session_id,
                response.status_code,
            )
        return deleted

    # --------------------------------------------------------------- session

    async def _default_model_ref(
        self, client: httpx.AsyncClient, thinking_level: str = ""
    ) -> dict[str, str] | None:
        response = await client.get("/api/model/default")
        response.raise_for_status()
        model = _data(response.json())
        if not isinstance(model, Mapping):
            return None
        model_id = str(model.get("modelID") or "")
        provider_id = str(model.get("providerID") or "")
        if not model_id or not provider_id:
            return None
        ref = {"id": model_id, "providerID": provider_id}
        if thinking_level:
            ref["variant"] = thinking_level
        return ref

    async def _ensure_session(self, request: AgentRequest) -> str:
        """Resume, fork, or create the V2 session this turn runs in."""
        client = self._client
        assert client is not None
        agent, permissions = self._session_settings(request)
        provider_id, model_id = split_model(request.model)
        if model_id and not provider_id:
            self._turn_model = await self._resolve_model(client, request.model)
            provider_id, model_id = self._turn_model
        else:
            self._turn_model = (provider_id, model_id)

        desired_model: dict[str, str] | None = None
        if model_id:
            desired_model = {"id": model_id, "providerID": provider_id}
            if request.thinking_level:
                desired_model["variant"] = request.thinking_level
        default_model: dict[str, str] | None = None
        if not request.model:
            try:
                default_model = await self._default_model_ref(
                    client, request.thinking_level
                )
            except httpx.HTTPError:
                logger.debug("OpenCode default-model lookup failed", exc_info=True)

        async def _configure(
            session_id: str, current: Mapping[str, Any]
        ) -> bool:
            if str(current.get("agent") or "") != agent:
                response = await client.post(
                    f"/api/session/{session_id}/agent", json={"agent": agent}
                )
                if response.status_code >= 400:
                    return False
            target_model = desired_model or default_model
            if (
                not request.model
                and target_model is None
                and isinstance(current.get("model"), Mapping)
                and current["model"].get("id")
            ):
                # An empty request means "use OpenCode's configured default";
                # silently retaining a resumed session's old model would make
                # the request setting ineffective when /api/model/default is
                # unavailable. Let the caller rotate to a fresh session.
                return False
            if target_model is not None:
                current_model = current.get("model")
                current_model = current_model if isinstance(current_model, Mapping) else {}
                same_ref = (
                    str(current_model.get("id") or "") == target_model["id"]
                    and str(current_model.get("providerID") or "")
                    == target_model["providerID"]
                )
                current_variant = str(current_model.get("variant") or "")
                desired_variant = target_model.get("variant", "")
                if current_variant == "default" and not desired_variant:
                    current_variant = ""
                if not same_ref or current_variant != desired_variant:
                    response = await client.post(
                        f"/api/session/{session_id}/model",
                        json={"model": target_model},
                    )
                    if response.status_code >= 400:
                        return False
            return True

        replacement_metadata: Mapping[str, Any] | None = None
        resume = (request.resume_session or "").strip()
        if resume:
            response = await client.get(f"/api/session/{resume}")
            if response.status_code < 400:
                try:
                    session_payload = _data(response.json())
                except (TypeError, ValueError):
                    session_payload = None
                if isinstance(session_payload, Mapping):
                    metadata = session_payload.get("metadata")
                    if isinstance(metadata, Mapping):
                        replacement_metadata = metadata
                if (
                    isinstance(session_payload, Mapping)
                    and _session_permission_matches(session_payload, permissions)
                ):
                    if request.fork_session:
                        fork_response = await client.post(
                            f"/api/session/{resume}/fork", json={}
                        )
                        if fork_response.status_code < 400:
                            try:
                                forked = _data(fork_response.json())
                            except (TypeError, ValueError):
                                forked = None
                            if isinstance(forked, Mapping):
                                forked_id = str(forked.get("id") or "")
                                if forked_id and await _configure(forked_id, forked):
                                    self._session_id = forked_id
                                    return forked_id
                        logger.warning(
                            "OpenCode fork failed (%s); starting a new session",
                            fork_response.status_code,
                        )
                    elif await _configure(resume, session_payload):
                        self._session_id = resume
                        return resume
                else:
                    logger.warning(
                        "OpenCode session %s permission rules do not match %s; "
                        "starting a fresh session",
                        resume,
                        request.mode,
                    )
                    try:
                        history = await _read_v2_messages(client, resume)
                        self._session_handover_context = _session_handover_text(history)
                    except (httpx.HTTPError, TypeError, ValueError, RuntimeError):
                        logger.info(
                            "OpenCode session %s history unavailable during "
                            "permission rotation",
                            resume,
                        )
            else:
                logger.info("OpenCode session %s is gone; starting a new one", resume)

            # A replacement session does not inherit stable workspace/project facts.
            prepend_stable_context(request)

        payload: dict[str, Any] = {"agent": agent, "permissions": permissions}
        if replacement_metadata is not None:
            payload["metadata"] = dict(replacement_metadata)
        new_session_model = desired_model or default_model
        if new_session_model is not None:
            payload["model"] = dict(new_session_model)
        response = await client.post("/api/session", json=payload)
        response.raise_for_status()
        session_payload = _data(response.json())
        if not isinstance(session_payload, Mapping):
            raise RuntimeError("OpenCode returned an invalid session payload")
        self._session_id = str(session_payload.get("id") or "")
        if not self._session_id:
            raise RuntimeError("OpenCode returned a session without an id")
        return self._session_id

    async def abort_session(self, session_id: str) -> None:
        client = self._client
        if client is None or not session_id:
            return
        try:
            await client.post(f"/api/session/{session_id}/interrupt")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a failed interrupt must never wedge Stop
            # This runs detached from the Stop request; observe every failure so
            # it can never become an unretrieved task exception.
            logger.debug("OpenCode interrupt failed for %s", session_id, exc_info=True)

    def _prompt_body(
        self, request: AgentRequest, *, system: str = ""
    ) -> dict[str, Any]:
        """V2 prompt text plus native file attachments.

        V2 removed the per-prompt ``system`` field. Wrap Ciaobot's core/runtime
        context in the existing transcript-only context markers so the model
        receives it while replay strips it from the user bubble.
        """
        text = build_prompt(request)
        if system:
            text = f"[CIAO_CONTEXT_BEGIN]\n{system}\n[CIAO_CONTEXT_END]\n\n{text}"
        body: dict[str, Any] = {
            "text": text,
            "delivery": "queue",
            "resume": True,
        }
        files = [
            {
                "uri": image.path.resolve().as_uri(),
                "name": image.original_filename,
            }
            for image in request.images
        ]
        if files:
            body["files"] = files
        return body

    async def steer(self, request: AgentRequest) -> bool:
        """Always False: opencode cannot inject into a running turn.

        Returning False (rather than sending a second prompt) is deliberate —
        a second prompt would be queued or would abort the active turn, and
        neither is what steering means. The caller keeps the message for the
        next turn instead.
        """
        return False

    # ------------------------------------------------------------ permissions

    def tool_use_id_for_request(
        self, request_id: str, session_id: str = ""
    ) -> str:
        pending = self._permission_requests.get(request_id)
        if pending is None or (session_id and pending.session_id != session_id):
            return ""
        return pending.tool_use_id

    async def _reply_permission(
        self, pending: _PendingRequest, reply: str, message: str = ""
    ) -> QuestionResponseResult:
        client = self._client
        if client is None or not pending.session_id:
            return QuestionResponseResult(False, "OpenCode is not connected", False)
        try:
            body: dict[str, str] = {"decision": reply}
            if message:
                body["message"] = message
            response = await client.post(
                f"/api/session/{pending.session_id}/permission/"
                f"{pending.request_id}/reply",
                json=body,
            )
        except httpx.HTTPError as exc:
            logger.debug("OpenCode permission reply failed", exc_info=True)
            return QuestionResponseResult(False, str(exc), True)
        if response.status_code in {200, 204, 409}:
            return QuestionResponseResult(True)
        return QuestionResponseResult(
            False,
            _sanitize_error(getattr(response, "text", ""))
            or f"OpenCode returned HTTP {response.status_code}",
            response.status_code >= 500 or response.status_code in {408, 429},
        )

    async def send_permission_response(
        self,
        request_id: str,
        approved: bool,
        message: str = "",
        *,
        session_id: str = "",
    ) -> QuestionResponseResult:
        pending = self._permission_requests.get(request_id)
        if pending is not None and session_id and pending.session_id != session_id:
            return QuestionResponseResult(
                False, "Permission request belongs to another session", False
            )
        if pending is None or self._client is None:
            return QuestionResponseResult(False, "Permission request is no longer active", False)
        result = await self._reply_permission(
            pending, "once" if approved else "reject", message
        )
        if result.ok:
            self._permission_requests.pop(request_id, None)
        return result

    async def _reject_question(self, pending: _PendingRequest) -> QuestionResponseResult:
        client = self._client
        if client is None or not pending.session_id:
            return QuestionResponseResult(False, "OpenCode is not connected", False)
        try:
            response = await client.delete(
                f"/api/session/{pending.session_id}/form/{pending.request_id}"
            )
        except httpx.HTTPError as exc:
            logger.debug("OpenCode form cancellation failed", exc_info=True)
            return QuestionResponseResult(False, str(exc), True)
        if response.status_code in {200, 204, 409}:
            return QuestionResponseResult(True)
        return QuestionResponseResult(
            False,
            _sanitize_error(getattr(response, "text", ""))
            or f"OpenCode returned HTTP {response.status_code}",
            response.status_code >= 500 or response.status_code in {408, 429},
        )

    async def _reply_question(
        self,
        pending: _PendingRequest,
        answer: Mapping[str, FormValue],
    ) -> QuestionResponseResult:
        client = self._client
        if client is None or not pending.session_id:
            return QuestionResponseResult(False, "OpenCode is not connected", False)
        try:
            response = await client.post(
                f"/api/session/{pending.session_id}/form/{pending.request_id}/reply",
                json={"answer": dict(answer)},
            )
        except httpx.HTTPError as exc:
            logger.debug("OpenCode form reply failed", exc_info=True)
            return QuestionResponseResult(False, str(exc), True)
        if response.status_code in {200, 204, 409}:
            return QuestionResponseResult(True)
        return QuestionResponseResult(
            False,
            _sanitize_error(getattr(response, "text", ""))
            or f"OpenCode returned HTTP {response.status_code}",
            response.status_code >= 500 or response.status_code in {408, 429},
        )

    async def send_question_response(
        self,
        request_id: str,
        answers: Mapping[str, Sequence[str]],
        *,
        cancel: bool = False,
        session_id: str = "",
    ) -> QuestionResponseResult:
        """Reply to or explicitly cancel a V2 form.

        ``answers={}`` is a valid answer for an all-optional form; only the
        explicit ``cancel`` flag maps to DELETE. The HTTP operation is awaited
        so the caller can keep the card retryable when V2 rejects it.
        """
        pending = self._question_requests.get(request_id)
        if pending is not None and session_id and pending.session_id != session_id:
            return QuestionResponseResult(
                False, "Question request belongs to another session", False
            )
        if pending is None:
            # A duplicate reply after the SSE form.replied/cancelled event is
            # idempotently successful; there is nothing left to mutate.
            return QuestionResponseResult(True)
        if self._client is None:
            return QuestionResponseResult(False, "OpenCode is not connected", False)
        if cancel:
            result = await self._reject_question(pending)
        else:
            try:
                answer = _validate_form_answer(pending, answers)
            except _FormValidationError as exc:
                return QuestionResponseResult(False, str(exc), False)
            result = await self._reply_question(pending, answer)
        if result.ok:
            self._question_requests.pop(request_id, None)
        return result

    # -------------------------------------------------------------- streaming

    def _emit_suffix(self, part_id: str, text: str) -> str:
        """Return the unseen tail of V2 delta/settled or recovered text."""
        already = self._emitted.get(part_id, 0)
        if len(text) <= already:
            return ""
        self._emitted[part_id] = len(text)
        return text[already:]

    def _note_answer(self, part_id: str, text: str) -> None:
        """Accumulate one emitted fragment of the visible reply."""
        self._answer_parts.setdefault(part_id, []).append(text)

    def _turn_scope(self, messages: list[Any]) -> list[Mapping[str, Any]]:
        """Return this turn's projected rows through its first idle boundary.

        V2 can append another user/assistant turn to the same session while a
        recovery poll is in flight. The first idle message after our admitted
        user row is the authoritative end of this execution; everything after
        it belongs to a later turn and must not leak into the current result.
        """
        anchor = -1
        for index, message in enumerate(messages):
            if not isinstance(message, Mapping):
                continue
            info = message.get("info")
            if not isinstance(info, Mapping) or info.get("role") != "user":
                continue
            if self._user_message_id:
                if str(info.get("id") or "") == self._user_message_id:
                    anchor = index
                    break
                continue
            anchor = index
        if self._user_message_id and anchor < 0:
            return []
        scoped: list[Mapping[str, Any]] = []
        for message in messages[anchor + 1:]:
            if not isinstance(message, Mapping):
                continue
            scoped.append(message)
            info = message.get("info")
            if isinstance(info, Mapping) and info.get("type") == "idle":
                break
        return scoped

    def _turn_messages(self, messages: list[Any]) -> list[Mapping[str, Any]]:
        """Return assistant messages projected after this turn's user row."""
        return [
            message
            for message in self._turn_scope(messages)
            if isinstance(message.get("info"), Mapping)
            and message["info"].get("role") == "assistant"
        ]

    def _turn_assistant_parts(
        self, messages: list[Any]
    ) -> list[Mapping[str, Any]]:
        """Settled parts of *this* turn's assistant messages, in order."""
        parts: list[Mapping[str, Any]] = []
        for message in self._turn_messages(messages):
            message_parts = message.get("parts")
            if isinstance(message_parts, list):
                parts.extend(
                    part for part in message_parts if isinstance(part, Mapping)
                )
        return parts

    def _restore_turn_metadata(self, messages: list[Any]) -> None:
        """Restore model, usage, cost, and assistant error after SSE loss."""
        total_tokens: dict[str, int] = {}
        last_context_usage: dict[str, str] = {}
        total_cost = 0.0
        for message in self._turn_scope(messages):
            info = message.get("info")
            if isinstance(info, Mapping) and info.get("type") == "idle":
                outcome = str(info.get("outcome") or "")
                if outcome in {"succeeded", "failed", "interrupted"}:
                    self._poll_idle_outcome = outcome
                if outcome == "failed":
                    self._poll_error = self._poll_error or "OpenCode execution failed"
                elif outcome == "interrupted":
                    self._poll_error = (
                        self._poll_error or "OpenCode execution was interrupted"
                    )
        for message in self._turn_messages(messages):
            info = message.get("info")
            if not isinstance(info, Mapping):
                continue
            model_id = str(info.get("modelID") or "")
            provider_id = str(info.get("providerID") or "")
            if model_id:
                self._effective_model = (
                    f"{provider_id}/{model_id}" if provider_id else model_id
                )
            tokens = info.get("tokens")
            if isinstance(tokens, Mapping):
                message_usage = usage_payload(tokens)
                if message_usage:
                    last_context_usage = message_usage
                for key, value in message_usage.items():
                    try:
                        total_tokens[key] = total_tokens.get(key, 0) + int(value)
                    except (TypeError, ValueError):
                        continue
            cost = info.get("cost")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                total_cost += float(cost)
            message_error = info.get("error")
            if isinstance(message_error, Mapping):
                self._poll_error = self._poll_error or error_text(message_error)
        if total_tokens:
            self._usage = {key: str(value) for key, value in total_tokens.items()}
        if last_context_usage:
            self._context_usage = last_context_usage
        if total_cost:
            self._cost = total_cost

    def _turn_has_running_tools(self, messages: list[Any]) -> bool:
        return any(
            isinstance(part.get("state"), Mapping)
            and part["state"].get("status") in {"pending", "running", "streaming"}
            for message in self._turn_messages(messages)
            for part in (message.get("parts") or [])
            if isinstance(part, Mapping)
        )

    async def _reconcile_interrupted_turn(
        self, client: httpx.AsyncClient, session_id: str
    ) -> AsyncGenerator[StreamEvent, None]:
        """Recover a turn whose SSE died after the prompt was accepted."""
        deadline = time.monotonic() + _OPENCODE_RECOVERY_WINDOW_S
        signature = ""
        while True:
            messages: list[dict[str, Any]] | None = None
            active_ids: set[str] | None = None
            try:
                messages = await _read_v2_messages(client, session_id)
                active_ids = await _read_active_sessions(client)
            except (httpx.HTTPError, ValueError, AttributeError, RuntimeError):
                # Best effort: an unreadable endpoint makes the turn degraded.
                messages = messages if messages is not None else None

            if messages is not None:
                turn_messages = self._turn_messages(messages)
                current = _opencode_messages_signature(turn_messages)
                user_seen = not self._user_message_id or any(
                    isinstance(message.get("info"), Mapping)
                    and message["info"].get("role") == "user"
                    and str(message["info"].get("id") or "") == self._user_message_id
                    for message in messages
                )
                if user_seen:
                    self._session_handover_context = ""
                running = self._turn_has_running_tools(messages)
                idle_outcome = next(
                    (
                        str(message["info"].get("outcome") or "")
                        for message in reversed(self._turn_scope(messages))
                        if isinstance(message.get("info"), Mapping)
                        and message["info"].get("type") == "idle"
                        and str(message["info"].get("outcome") or "")
                        in {"succeeded", "failed", "interrupted"}
                    ),
                    "",
                )
                quiesced = (
                    user_seen
                    and not running
                    and (
                        idle_outcome in {"succeeded", "failed", "interrupted"}
                        or (
                            active_ids is not None
                            and session_id not in active_ids
                            and bool(current)
                            and current == signature
                        )
                    )
                )
                signature = current or signature
                self._restore_turn_metadata(messages)
                for part in self._turn_assistant_parts(messages):
                    for converted in self._part_updated({"part": dict(part)}):
                        yield converted
                if quiesced:
                    self._turn_recovered_via_poll = True
                    return
            if time.monotonic() >= deadline:
                self._poll_error = (
                    self._poll_error
                    or "OpenCode turn recovery timed out before a terminal result"
                )
                return
            await asyncio.sleep(_OPENCODE_RECOVERY_POLL_S)

    def _answer_text(self) -> str:
        """The turn's visible reply, joined across text parts."""
        parts = (
            "".join(chunks).strip() for chunks in self._answer_parts.values()
        )
        return "\n\n".join(part for part in parts if part)

    async def _reload_pending_requests(
        self, client: httpx.AsyncClient, session_id: str
    ) -> list[StreamEvent]:
        """Recover V2 permission/form requests missed during an SSE outage."""
        events: list[StreamEvent] = []
        permission_seen: set[str] = set()
        permission_loaded = False
        for path in (f"/api/session/{session_id}/permission",):
            try:
                response = await client.get(path)
                raise_for_status = getattr(response, "raise_for_status", None)
                if callable(raise_for_status):
                    raise_for_status()
                requests = _data(response.json())
            except (httpx.HTTPError, TypeError, ValueError, AttributeError):
                continue
            if not isinstance(requests, list):
                continue
            permission_loaded = True
            for request in requests:
                if not isinstance(request, Mapping):
                    continue
                request_id = str(request.get("id") or "")
                if not request_id or request.get("state") not in (
                    None,
                    "pending",
                    "asked",
                ):
                    continue
                permission_seen.add(request_id)
                if request_id in self._permission_requests:
                    continue
                events.extend(self._permission_event(request))
        if permission_loaded:
            for request_id, pending in list(self._permission_requests.items()):
                if pending.session_id == session_id and request_id not in permission_seen:
                    self._permission_requests.pop(request_id, None)

        form_seen: set[str] = set()
        form_loaded = False
        for path in (f"/api/session/{session_id}/form",):
            try:
                response = await client.get(path)
                raise_for_status = getattr(response, "raise_for_status", None)
                if callable(raise_for_status):
                    raise_for_status()
                forms = _data(response.json())
            except (httpx.HTTPError, TypeError, ValueError, AttributeError):
                continue
            if not isinstance(forms, list):
                continue
            form_loaded = True
            for form in forms:
                if not isinstance(form, Mapping):
                    continue
                if isinstance(form.get("form"), Mapping):
                    form = form["form"]
                form_session = str(form.get("sessionID") or "")
                if form_session != session_id:
                    continue
                form_id = str(form.get("id") or "")
                metadata = form.get("metadata")
                if isinstance(metadata, Mapping) and metadata.get("kind") not in {
                    None,
                    "question",
                }:
                    continue
                state = form.get("state")
                if isinstance(state, Mapping) and state.get("status") not in {
                    None,
                    "pending",
                }:
                    continue
                if form_id:
                    form_seen.add(form_id)
                if form_id and form_id not in self._question_requests:
                    events.extend(self._question_event(form))
        if form_loaded:
            for form_id, pending in list(self._question_requests.items()):
                if pending.session_id == session_id and form_id not in form_seen:
                    self._question_requests.pop(form_id, None)
        return events

    def _event_to_stream(self, event: Mapping[str, Any]) -> list[StreamEvent]:
        """Translate one OpenCode 2.x SSE event into Ciaobot stream events."""
        kind = str(event.get("type") or "")
        props = event.get("data")
        props = props if isinstance(props, Mapping) else {}

        if kind in {"session.reasoning.started", "session.text.started"}:
            part_kind = "reasoning" if kind.startswith("session.reasoning.") else "text"
            self._part_types[_v2_part_key(props, part_kind)] = part_kind
            return []
        if kind in {"session.reasoning.delta", "session.text.delta"}:
            part_kind = "reasoning" if kind.startswith("session.reasoning.") else "text"
            key = _v2_part_key(props, part_kind)
            self._part_types[key] = part_kind
            return self._part_delta({
                "partID": key,
                "field": "text",
                "delta": props.get("delta"),
            })
        if kind in {"session.reasoning.ended", "session.text.ended"}:
            part_kind = "reasoning" if kind.startswith("session.reasoning.") else "text"
            key = _v2_part_key(props, part_kind)
            suffix = self._emit_suffix(key, str(props.get("text") or ""))
            if not suffix:
                return []
            if part_kind == "reasoning":
                return [ThinkingEvent(type="thinking", text=suffix)]
            self._note_answer(key, suffix)
            return [AssistantTextDelta(type="text", text=suffix)]

        if kind == "session.tool.input.started":
            call_id = str(props.get("id") or "")
            if call_id:
                self._tool_calls[call_id] = str(props.get("name") or "tool")
            return []
        if kind == "session.tool.called":
            call_id = str(props.get("id") or "")
            if call_id in self._settled_tool_ids:
                return []
            tool = str(props.get("name") or self._tool_calls.get(call_id) or "tool")
            self._tool_calls[call_id] = tool
            raw_input = props.get("input")
            return [ToolUseEvent(
                type="tool_use",
                tool_name=tool,
                tool_input=_summarize_tool_input(tool, raw_input),
                tool_use_id=call_id or None,
                file_touches=_file_touches(tool, raw_input),
            )]
        if kind in {"session.tool.success", "session.tool.failed"}:
            call_id = str(props.get("id") or "")
            if call_id in self._settled_tool_ids:
                return []
            tool = self._tool_calls.pop(call_id, "")
            if call_id:
                self._settled_tool_ids.add(call_id)
            error = props.get("error")
            detail = error_text(error) if kind.endswith("failed") and isinstance(error, Mapping) else ""
            return [ToolUseEvent(
                type="tool_result",
                tool_name=tool,
                tool_input=detail,
                tool_use_id=call_id or None,
            )]

        if kind == "session.step.started":
            model = props.get("model")
            if isinstance(model, Mapping):
                model_id = str(model.get("id") or "")
                provider_id = str(model.get("providerID") or "")
                if model_id:
                    self._effective_model = (
                        f"{provider_id}/{model_id}" if provider_id else model_id
                    )
            return []
        if kind in {"session.step.ended", "session.usage.updated"}:
            cost = props.get("cost")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                self._cost = float(cost)
            tokens = props.get("tokens")
            if isinstance(tokens, Mapping):
                event_usage = usage_payload(tokens)
                if event_usage:
                    if kind == "session.step.ended":
                        self._context_usage = event_usage
                    self._usage = event_usage or self._usage
            return _token_usage_events(tokens)

        if kind == "permission.asked":
            return self._permission_event(props)
        if kind == "form.created":
            form = props.get("form")
            if not isinstance(form, Mapping):
                return []
            metadata = form.get("metadata")
            if isinstance(metadata, Mapping) and metadata.get("kind") not in {
                None,
                "question",
            }:
                return []
            return self._question_event(form)
        if kind in {"form.replied", "form.cancelled"}:
            form_id = str(props.get("formID") or props.get("id") or "")
            if form_id:
                self._question_requests.pop(form_id, None)
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

        if call_id and call_id in self._settled_tool_ids:
            return []
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
            if call_id:
                self._settled_tool_ids.add(call_id)
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
            raw_error = state.get("error")
            detail = (
                error_text(raw_error)
                if status == "error" and isinstance(raw_error, Mapping)
                else ""
            )
            events.append(ToolUseEvent(
                type="tool_result",
                tool_name=tool,
                tool_input=detail,
                tool_use_id=call_id or None,
            ))
            return events

        return []

    def _permission_event(self, props: Mapping[str, Any]) -> list[StreamEvent]:
        """Surface one V2 permission request as the PWA approval card."""
        request_id = str(props.get("id") or "")
        session_id = str(props.get("sessionID") or "")
        if not request_id or not session_id:
            return []
        existing = self._permission_requests.get(request_id)
        if existing is not None and existing.session_id == session_id:
            return []
        action = str(props.get("action") or "tool").strip()
        detail = str(props.get("message") or "").strip()
        resources = props.get("resources")
        resource_detail = ""
        if isinstance(resources, list):
            resource_detail = ", ".join(
                str(item) for item in resources if str(item).strip()
            )
        if action in {"glob", "grep"} and resource_detail:
            # V2 uses the pattern/regex as the permission resource; metadata.path
            # is only the search root and must not hide the query the operator is
            # approving.
            detail = resource_detail
        else:
            metadata = props.get("metadata")
            if isinstance(metadata, Mapping):
                metadata_detail = ""
                for key in ("command", "filePath", "path", "url", "pattern"):
                    value = metadata.get(key)
                    if isinstance(value, str) and value.strip():
                        metadata_detail = value.strip()
                        break
                if metadata_detail:
                    detail = (
                        f"{detail} ({metadata_detail})"
                        if detail
                        else metadata_detail
                    )
            if not detail and resource_detail:
                detail = resource_detail
        source = props.get("source")
        call_id = (
            str(source.get("id") or "") if isinstance(source, Mapping) else ""
        )
        self._permission_requests[request_id] = _PendingRequest(
            request_id=request_id,
            session_id=session_id,
            tool_use_id=call_id,
        )
        label = {
            "glob": "glob search",
            "grep": "content search",
        }.get(action, action or "a tool")
        return [PermissionRequestEvent(
            type="system",
            message=f"Approve use of {label}?",
            tool_name=label,
            tool_input=detail[:400],
            request_id=request_id,
            session_id=session_id,
        )]

    def _question_event(self, form: Mapping[str, Any]) -> list[StreamEvent]:
        """Surface one V2 form as the PWA's AskUserQuestion card."""
        request_id = str(form.get("id") or "")
        session_id = str(form.get("sessionID") or "")
        fields = form.get("fields")
        if not request_id or not session_id or not isinstance(fields, list):
            return []
        existing = self._question_requests.get(request_id)
        if existing is not None and existing.session_id == session_id:
            return []
        visible = [
            item for item in fields
            if isinstance(item, Mapping) and not item.get("hidden")
        ]
        if not visible:
            return []

        question_ids: list[str] = []
        question_fields: dict[str, dict[str, Any]] = {}
        questions: list[dict[str, Any]] = []
        for index, item in enumerate(visible):
            key = str(item.get("key") or index)
            kind = str(item.get("type") or "string")
            options = [
                dict(option) for option in (item.get("options") or [])
                if isinstance(option, Mapping)
            ]
            if kind == "boolean" and not options:
                options = [
                    {"value": "true", "label": "Yes"},
                    {"value": "false", "label": "No"},
                ]
            if kind == "external":
                options = [{
                    "value": "true",
                    "label": "Done",
                    "description": str(item.get("url") or ""),
                }]
            field = {**dict(item), "options": options}
            question_ids.append(key)
            question_fields[key] = field
            questions.append({
                "id": key,
                "question": str(item.get("description") or item.get("title") or key),
                "header": str(item.get("title") or form.get("title") or "")[:80],
                "type": kind,
                "required": bool(item.get("required")),
                "when": item.get("when") if isinstance(item.get("when"), list) else [],
                "format": item.get("format"),
                "pattern": item.get("pattern"),
                "minLength": item.get("minLength"),
                "maxLength": item.get("maxLength"),
                "minimum": item.get("minimum"),
                "maximum": item.get("maximum"),
                "minItems": item.get("minItems"),
                "maxItems": item.get("maxItems"),
                "custom": bool(item.get("custom")),
                "multiSelect": kind == "multiselect",
                "isOther": kind != "external" and (bool(item.get("custom")) or not options),
                "options": [
                    {
                        "label": str(option.get("label") or option.get("value") or ""),
                        "value": str(option.get("value") or ""),
                        "description": str(option.get("description") or ""),
                    }
                    for option in options
                ],
            })

        self._question_requests[request_id] = _PendingRequest(
            request_id=request_id,
            session_id=session_id,
            question_ids=tuple(question_ids),
            question_fields=question_fields,
        )
        return [ToolUseEvent(
            type="tool_use",
            tool_name="AskUserQuestion",
            tool_input=json.dumps({"questions": questions}, ensure_ascii=False),
            tool_use_id=request_id,
            request_id=request_id,
            session_id=session_id,
        )]

    async def _resolve_model(
        self, client: httpx.AsyncClient, model: str
    ) -> tuple[str, str]:
        """Resolve a bare model id against V2's flat catalog."""
        provider_id, model_id = split_model(model)
        if provider_id or not model_id:
            return provider_id, model_id
        # A chat's server can be seconds old here, and a fresh server lists no
        # models until its providers load (see `model_catalog`), which would
        # reject a valid bare id as not found.
        loop = asyncio.get_running_loop()
        warm_deadline = loop.time() + _CATALOG_WARMUP_TIMEOUT
        while True:
            response = await client.get("/api/model")
            response.raise_for_status()
            models = _data(response.json())
            if models or loop.time() >= warm_deadline:
                break
            await asyncio.sleep(_CATALOG_WARMUP_POLL)
        if not isinstance(models, list):
            return "", ""
        matches = [
            entry
            for entry in models
            if isinstance(entry, Mapping)
            and entry.get("enabled") is not False
            and str(entry.get("modelID") or entry.get("id") or "") == model_id
            and str(entry.get("providerID") or "")
        ]
        providers = {str(entry.get("providerID")) for entry in matches}
        if not matches:
            raise ValueError(
                f"OpenCode model {model!r} was not found; use provider/model"
            )
        if len(providers) != 1:
            raise ValueError(
                f"OpenCode model {model!r} is ambiguous; use provider/model"
            )
        entry = matches[0]
        return str(entry.get("providerID") or ""), str(
            entry.get("modelID") or entry.get("id") or ""
        )

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
        self._turn_model = split_model(request.model)
        session_id = await self._ensure_session(request)
        self._reset_turn_state()
        register_handle(OpencodeActiveHandle(self, session_id))

        if self._developer_instructions is None:
            instructions = self._chat_system_instructions()
            runtime = ""
        else:
            instructions = self._developer_instructions
            runtime = build_runtime_context(request)
        system = compose_system(instructions, runtime)
        if self._session_handover_context:
            system = compose_system(system, self._session_handover_context)
        message_id = f"msg_{secrets.token_hex(12)}"
        body = {**self._prompt_body(request, system=system), "id": message_id}
        # Anchor recovery to our own id before the request leaves the process.
        # V2 reconciles duplicate prompt ids, so a retry after an ambiguous
        # transport failure cannot create a second execution.
        self._user_message_id = message_id

        error: str = ""
        saw_output = False

        # The SSE subscription can drop mid-turn (network blip, server hiccup)
        # before `session.execution.*` arrives. Rather than failing the whole turn,
        # re-subscribe a bounded number of times; if the stream still will not
        # hold, poll the message list until output quiesces and replay settled
        # parts through the same accumulator (its `_emitted` bookkeeping makes
        # the replay idempotent). Mirrors conduit's poll-backstop design.
        prompt_attempted = False
        prompt_accepted = False
        prompt_rejected = False
        prompt_receipt_error = ""
        terminal_seen = False

        async def _pump_once() -> AsyncGenerator[StreamEvent, None]:
            """One SSE subscription, pumped until idle or premature close."""
            nonlocal prompt_attempted, prompt_accepted, prompt_rejected
            nonlocal prompt_receipt_error, error, saw_output, terminal_seen
            async with client.stream("GET", "/api/event") as stream:
                stream.raise_for_status()
                # Subscribe before prompting: opencode starts emitting as soon
                # as the prompt is accepted, and a late subscriber loses the
                # opening deltas.
                if not prompt_accepted:
                    prompt_attempted = True
                    response = await client.post(
                        f"/api/session/{session_id}/prompt", json=body
                    )
                    if response.status_code >= 400:
                        detail = _sanitize_error(response.text)
                        prompt_rejected = True
                        error = error or f"OpenCode rejected the prompt: {detail}"
                        return
                    try:
                        admitted = _data(response.json())
                    except (TypeError, ValueError) as exc:
                        prompt_receipt_error = (
                            f"OpenCode returned an invalid prompt receipt: {exc}"
                        )
                        return
                    admitted_id = (
                        str(admitted.get("id") or "")
                        if isinstance(admitted, Mapping)
                        else ""
                    )
                    if admitted_id != message_id:
                        prompt_receipt_error = (
                            "OpenCode returned an invalid prompt receipt: "
                            f"expected message id {message_id}"
                        )
                        return
                    # Once accepted, the replacement session owns the handover.
                    self._session_handover_context = ""
                    prompt_accepted = True
                if prompt_accepted:
                    for converted in await self._reload_pending_requests(
                        client, session_id
                    ):
                        saw_output = True
                        yield converted
                decoder = SSEDecoder()
                async for sse in decoder.aiter_bytes(stream.aiter_bytes()):
                    if self._stop_requested == session_id:
                        # The user stopped the turn and the abort has been
                        # issued: end locally now. Waiting for the server's
                        # `session.execution.*` can drag through SSE reconnects and
                        # the poll backstop, leaving Stop feeling dead.
                        terminal_seen = True
                        return
                    try:
                        event = sse.json()
                    except ValueError:
                        continue
                    if not isinstance(event, Mapping):
                        continue
                    props = event.get("data")
                    props = props if isinstance(props, Mapping) else {}
                    event_session = str(props.get("sessionID") or "")
                    if not event_session:
                        form = props.get("form")
                        if isinstance(form, Mapping):
                            event_session = str(form.get("sessionID") or "")
                    if event_session and event_session != session_id:
                        continue

                    kind = str(event.get("type") or "")
                    if kind == "session.execution.failed":
                        error = error or error_text(props.get("error"))
                        terminal_seen = True
                        break
                    if kind == "session.execution.interrupted":
                        error = error or "OpenCode execution was interrupted"
                        terminal_seen = True
                        break
                    if kind == "session.execution.succeeded":
                        terminal_seen = True
                        break

                    for converted in self._event_to_stream(event):
                        saw_output = saw_output or converted.type in {"text", "tool_use"}
                        yield converted

        # The SSE subscription can drop mid-turn (network blip, server hiccup)
        # before `session.execution.*` arrives. Rather than failing the whole turn,
        # re-subscribe a bounded number of times; if the stream still will not
        # hold, poll the message list until output quiesces and replay settled
        # parts through the same accumulator — its `_emitted` bookkeeping makes
        # the replay idempotent. Poll-backstop design borrowed from conduit.
        reconnects = 0
        try:
            while True:
                try:
                    async for converted in _pump_once():
                        yield converted
                except httpx.HTTPError as exc:
                    if not prompt_attempted:
                        # The event subscription failed before a prompt could be
                        # admitted; there is no ambiguous server-side work.
                        yield ResultEvent(
                            type="result",
                            result=f"OpenCode connection failed: {exc}",
                            session_id=session_id,
                            is_error=True,
                        )
                        return
                    # A prompt POST may have committed before its response was
                    # lost. Retry the same V2 message id, then reconcile by id.
                if prompt_rejected or terminal_seen:
                    break
                if self._stop_requested == session_id:
                    # Stopped: no reconnects, no recovery — just finish.
                    break
                if reconnects >= _OPENCODE_SSE_RECONNECTS - 1:
                    break
                reconnects += 1
                await asyncio.sleep(0.5 * reconnects)

            degraded_final = False
            if (
                not terminal_seen
                and prompt_attempted
                and not prompt_rejected
                and self._stop_requested != session_id
            ):
                for converted in await self._reload_pending_requests(client, session_id):
                    saw_output = True
                    yield converted
                self._turn_recovered_via_poll = False
                async for converted in self._reconcile_interrupted_turn(
                    client, session_id
                ):
                    saw_output = saw_output or converted.type in {"text", "tool_use"}
                    yield converted
                degraded_final = not self._turn_recovered_via_poll
                error = error or self._poll_error
                if not self._turn_recovered_via_poll:
                    error = error or prompt_receipt_error
        finally:
            register_handle(None)

        await self._augment_context_pct(client, self._turn_model)

        yield ResultEvent(
            type="result",
            # A successful turn carries the accumulated answer:
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
            fallback_final=(bool(error) and saw_output) or degraded_final,
        )

    async def _augment_context_pct(
        self, client: httpx.AsyncClient, model: tuple[str, str]
    ) -> None:
        """Attach the turn's context-window occupancy to ``self._usage``.

        Mirrors OpenCode's own UI: the last model call's total tokens over the
        model's declared ``limit.context`` from ``GET /api/model``. Silent on
        failure — the field is simply left off the usage payload when the CLI
        cannot answer.
        """
        usage = self._context_usage or self._usage
        if not usage:
            return
        total = usage.get("totalTokens")
        if not total:
            return
        provider_id, model_id = model
        if not provider_id or not model_id:
            # A chat may let opencode choose the model; `_effective_model` then
            # carries the resolved `providerID/modelID` from the assistant
            # message.
            provider_id, model_id = split_model(self._effective_model)
        if not provider_id or not model_id:
            return
        context_window: int | None = None
        try:
            response = await client.get("/api/model")
            if response.status_code < 400:
                context_window = _context_window_for(
                    response.json(), provider_id, model_id
                )
        except (httpx.HTTPError, ValueError):
            context_window = None
        if not context_window:
            return
        try:
            total_tokens = int(total)
        except (TypeError, ValueError):
            return
        if total_tokens <= 0:
            return
        self._usage = {
            **self._usage,
            "context_window": str(context_window),
            "context_pct": f"{min(100.0, total_tokens / context_window * 100):.1f}%",
        }

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

        async with _catalog_lock(workspace_root):
            # Recheck after acquiring the single-flight lock: another settings
            # request may have filled the cache while this one was waiting.
            cached = _MODEL_CACHE.get(key)
            if cached and not force:
                ttl = _MODEL_CACHE_TTL if cached[1] else _EMPTY_MODEL_CACHE_TTL
                if time.monotonic() - cached[0] < ttl:
                    return [dict(item) for item in cached[1]]
            models_payload: object = {"data": []}
            async with _EphemeralServer(workspace_root) as client:
                if client is not None:
                    # A fresh server answers /api/info before it has loaded
                    # its providers, and its first /api/model is an empty
                    # list for the half-second or so that takes. Keep asking
                    # while the list is empty, or that empty list is cached
                    # and the picker shows no opencode models (2.0.16).
                    loop = asyncio.get_running_loop()
                    warm_deadline = loop.time() + _CATALOG_WARMUP_TIMEOUT
                    attempt = 0
                    while True:
                        try:
                            models = await client.get("/api/model")
                            if (
                                getattr(models, "status_code", 200) in {502, 503, 504}
                                and attempt < 2
                            ):
                                attempt += 1
                                await asyncio.sleep(0.25 * attempt)
                                continue
                            models.raise_for_status()
                            models_payload = models.json()
                        except (httpx.HTTPError, ValueError, AttributeError):
                            if attempt < 2:
                                attempt += 1
                                await asyncio.sleep(0.25 * attempt)
                                continue
                            models_payload = {"data": []}
                            break
                        if _data(models_payload) or loop.time() >= warm_deadline:
                            break
                        await asyncio.sleep(_CATALOG_WARMUP_POLL)
            # V2's /api/model snapshot is already filtered to enabled models;
            # do not combine it with a separately timed provider snapshot.
            catalog = _catalog_from_api({"data": None}, models_payload)
            _log_catalog_change(key, cached[1] if cached else None, catalog)
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
        if cached and time.monotonic() - cached[0] < cached[1]:
            return cached[2]
        thread: dict[str, Any] = {}
        ttl = _READ_CACHE_TTL
        async with _EphemeralServer(workspace_root) as client:
            if client is None:
                # Cache the failure too, briefly longer than a read: an
                # uncached miss here means another doomed server spawn on
                # the next poll.
                ttl = _READ_FAILURE_CACHE_TTL
            else:
                try:
                    info = await client.get(f"/api/session/{session_id}")
                    info.raise_for_status()
                    session_info = _data(info.json())
                    messages = await _read_v2_messages(client, session_id)
                    if isinstance(session_info, Mapping):
                        thread = {"info": dict(session_info), "messages": messages}
                except (httpx.HTTPError, TypeError, ValueError, RuntimeError):
                    thread = {}
        _THREAD_CACHE[key] = (time.monotonic(), ttl, thread)
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
        if cached and time.monotonic() - cached[0] < cached[1]:
            return cached[2]

        async def _child_messages(client: Any, child_id: str) -> list[dict[str, Any]]:
            try:
                return await _read_v2_messages(client, child_id)
            except (httpx.HTTPError, TypeError, ValueError):
                return []

        result: list[dict[str, Any]] = []
        ttl = _READ_CACHE_TTL
        async with _EphemeralServer(workspace_root) as client:
            if client is None:
                # Negative-cache the failed spawn (see read_thread).
                ttl = _READ_FAILURE_CACHE_TTL
            else:
                try:
                    children = await _read_v2_children(client, session_id)
                except (httpx.HTTPError, TypeError, ValueError, RuntimeError):
                    children = []
                histories = await asyncio.gather(
                    *(_child_messages(client, str(child["id"])) for child in children)
                )
                # This server is a different process from the chat's live
                # server; its /api/session/active map cannot prove that a
                # child is idle. Leave activity unknown and let message timing
                # provide the conservative fallback.
                result = [
                    {"info": child, "messages": messages, "active": None}
                    for child, messages in zip(children, histories)
                ]
        _COLLAB_CACHE[key] = (time.monotonic(), ttl, result)
        return result

    @classmethod
    async def delete_thread(cls, workspace_root: Path, session_id: str) -> bool:
        async with _EphemeralServer(workspace_root) as client:
            if client is None or not session_id:
                return False
            try:
                response = await client.delete(f"/api/session/{session_id}")
                return response.status_code < 400
            except httpx.HTTPError:
                return False


def opencode_collab_tree_counts(tree: Sequence[Mapping[str, Any]]) -> tuple[int, bool]:
    """Return running and observed counts, treating unknown activity safely."""
    running = 0
    for item in tree:
        if not isinstance(item, Mapping):
            continue
        if "active" not in item:
            continue
        active = item.get("active")
        if active is True:
            running += 1
            continue
        if active is False:
            continue
        messages = item.get("messages")
        if not isinstance(messages, list):
            # A different process cannot prove that a child with no projected
            # assistant row is idle. Keep it conservatively running.
            running += 1
            continue
        last: Mapping[str, Any] | None = None
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            info = message.get("info")
            if isinstance(info, Mapping) and info.get("role") == "assistant":
                last = info
        if last is None:
            running += 1
            continue
        time_info = last.get("time") if isinstance(last, Mapping) else None
        if (
            isinstance(time_info, Mapping)
            and time_info.get("created")
            and not time_info.get("completed")
        ):
            running += 1
    return running, bool(tree)


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
    """Read image support from V2's ``capabilities.input`` modality list."""
    capabilities = model.get("capabilities")
    inputs = capabilities.get("input") if isinstance(capabilities, Mapping) else None
    if isinstance(inputs, list) and "image" in inputs:
        return True
    if isinstance(inputs, list) and "text" in inputs:
        return False
    return None


def _catalog_from_api(
    providers_payload: object, models_payload: object
) -> list[dict[str, Any]]:
    """Flatten V2's active-provider and flat-model snapshots."""
    providers = _data(providers_payload)
    if providers is None:
        active_providers: set[str] | None = None
    elif isinstance(providers, list):
        active_providers = {
            str(provider.get("id") or "")
            for provider in providers if isinstance(provider, Mapping)
            if provider.get("id") and provider.get("activation") != "disabled"
        }
        # The free OpenCode provider is usable before provider discovery settles.
        if not active_providers:
            active_providers = {"opencode"}
    else:
        active_providers = None

    models = _data(models_payload)
    if not isinstance(models, list):
        return []
    rows: list[dict[str, Any]] = []
    for model in models:
        if not isinstance(model, Mapping) or model.get("enabled") is False:
            continue
        provider_id = str(model.get("providerID") or "")
        model_id = str(model.get("modelID") or model.get("id") or "")
        if (
            not provider_id
            or not model_id
            or (active_providers is not None and provider_id not in active_providers)
        ):
            continue
        variants = [
            str(variant.get("id") or "")
            for variant in (model.get("variants") or [])
            if isinstance(variant, Mapping) and variant.get("id")
        ]
        row: dict[str, Any] = {
            "model": f"{provider_id}/{model_id}",
            "label": f"{model.get('name') or model_id} ({provider_id})",
            "variants": sorted(variants),
        }
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
        async with _server_start_lock(self._workspace_root):
            return await self._enter_unlocked()

    async def _enter_unlocked(self) -> httpx.AsyncClient | None:
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
                response = await self._client.get("/api/info", timeout=2.0)
                if response.status_code == 200:
                    try:
                        payload = response.json()
                    except ValueError:
                        return None
                    return self._client if _server_version_error(payload) is None else None
                if response.status_code in {401, 404}:
                    return None
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
                try:
                    process.kill()
                    await process.wait()
                except (ProcessLookupError, FileNotFoundError):
                    # The process already exited between the timeout and the
                    # kill; treat it as cleanly gone rather than surfacing a
                    # spurious task exception.
                    pass


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CREDENTIAL_COUNT_RE = re.compile(r"(\d+)\s+credentials?\b", re.IGNORECASE)


def _credential_count(binary: str, *, timeout: float) -> int | None:
    """How many providers opencode holds credentials for, or None if unknown.

    2.0.16 prints a plain table (and "No authenticated integrations" when
    empty), so ask for `--format json` and count providers with at least one
    connection. Earlier 2.x builds printed a decorated TUI box ending in
    `N credentials`; that count is the fallback. Counting non-empty lines
    counts the decoration, which is how this once reported "10 provider(s)
    authenticated" against an empty store.

    `~/.local/share/opencode/auth.json` is deliberately not read: parsing a
    provider's cached credential file to determine identity is out of bounds.
    """
    import subprocess

    try:
        listed = subprocess.run(
            [binary, "auth", "list", "--format", "json"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        providers = json.loads(listed.stdout)
    except (TypeError, ValueError):
        providers = None
    if isinstance(providers, list):
        return sum(
            1 for item in providers
            if isinstance(item, dict) and item.get("connections")
        )
    try:
        listed = subprocess.run(
            [binary, "auth", "list"], capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _CREDENTIAL_COUNT_RE.search(_ANSI_RE.sub("", listed.stdout))
    return int(match.group(1)) if match else None


def _server_list(binary: str, path: str, *, timeout: float) -> list[dict[str, Any]]:
    """`data` rows of a V2 list route, fetched through `opencode api`.

    Run from the home directory so the result is the global configuration,
    not whatever project the engine happens to be started in.
    """
    import subprocess

    try:
        result = subprocess.run(
            [binary, "api", "GET", path],
            capture_output=True, text=True, timeout=timeout, cwd=str(Path.home()),
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return []
    rows = payload.get("data") if isinstance(payload, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _row_names(rows: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for row in rows:
        name = str(row.get("id") or row.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def _opencode_inventory(binary: str, *, timeout: float) -> tuple[list[str], list[str]]:
    """Skills and plugins, and MCP servers, the opencode CLI loads globally.

    Built-in plugins are opencode internals, and a plugin that failed to load
    (a duplicate ID, say) brings nothing, so both are left out.
    """
    skills = _row_names(_server_list(binary, "/api/skill", timeout=timeout))
    plugins = _row_names([
        row for row in _server_list(binary, "/api/plugin", timeout=timeout)
        if (row.get("source") or {}).get("type") != "builtin"
        and (row.get("state") or {}).get("status", "active") == "active"
    ])
    mcps = _row_names(_server_list(binary, "/api/mcp", timeout=timeout))
    return skills + [name for name in plugins if name not in skills], mcps


def opencode_login_status(*, timeout: float = 5.0) -> dict[str, Any]:
    """Bounded, credential-free opencode install/auth status for Settings."""
    import subprocess

    from ciao.setup_status import _provider

    binary = resolve_opencode_binary()
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
    if _server_version_error({"version": version}) is not None:
        return _provider(
            name="opencode",
            ok=False,
            auth="unsupported_version",
            command="opencode --version",
            detail=OPENCODE_V2_REQUIRED,
            version=version or "unknown",
        )
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
    skills, mcps = _opencode_inventory(binary, timeout=timeout)
    return _provider(
        name="opencode",
        ok=True,
        auth=auth,
        command="opencode auth login",
        detail=detail,
        version=version or "unknown",
        skills=skills,
        mcps=mcps,
    )


def opencode_system_skills(env: Mapping[str, str] | None = None) -> list[str]:
    """Skills opencode's own CLI loads. It has no separate bundled catalog."""
    return []
