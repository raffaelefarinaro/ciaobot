"""Authenticated MCP adapter for Ciaobot's application control plane."""

from __future__ import annotations

import contextvars
import functools
import inspect
import json
import logging
import os
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, cast

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.tools.base import Tool
from mcp.types import ToolAnnotations

from ciao.control_plane import (
    CiaoControlPlane,
    ControlPlaneError,
    McpPrincipal,
)
from ciao.web.routes_mcp import (
    _observed_project_mcp_tools,
    _probe_http_mcp_tools,
)


logger = logging.getLogger(__name__)

_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# One telemetry line per tool call with no retention meant a long-lived
# install grew ``mcp_tool_calls.jsonl`` without bound and made every Settings
# usage read reparse the whole history. Same coarse size guard as the job-run
# recorder in ``ciao/job_runs.py`` rather than a second retention scheme.
TELEMETRY_MAX_BYTES = 2 * 1024 * 1024  # trim the log once it passes ~2 MB
TELEMETRY_KEEP_LINES = 2000            # detailed records retained after a trim


@dataclass(frozen=True)
class _UsageAggregate:
    """What one pass over the telemetry state yields.

    ``tools`` is the per-tool aggregate
    (``{tool: {calls, errors, total_ms, providers, last_used}}``) folded from
    the rollup sidecar and then the retained log, so its counters are
    lifetime. The rest describes *where those counters came from*, which is
    what lets :meth:`CiaoMcpService.usage` label its window: retention drops
    detailed records, and a summary that cannot say so invites the reader to
    treat a trimmed log as the whole history.
    """

    tools: dict[str, dict[str, Any]]
    total_calls: int
    total_errors: int
    retained_records: int
    retained_since: str
    rolled_up_calls: int
    rotated_at: str


def _usage_entry() -> dict[str, Any]:
    return {"calls": 0, "errors": 0, "total_ms": 0, "providers": set(), "last_used": ""}


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _fold_telemetry_line(tools: dict[str, dict[str, Any]], line: str) -> str | None:
    """Add one telemetry line to the per-tool aggregate.

    Returns the folded record's timestamp (``""`` when the record carries
    none) or ``None`` when the line was skipped, so callers can count
    retained records and find the start of the retained window without a
    second parse.

    Blank, malformed, and non-object lines are skipped: the log is appended
    to live, so a reader can meet a half-written final record and must not
    turn that into a failed usage read.
    """
    line = line.strip()
    if not line:
        return None
    try:
        record = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(record, dict):
        return None
    name = str(record.get("tool") or "")
    if not name:
        return None
    entry = tools.setdefault(name, _usage_entry())
    entry["calls"] += 1
    if record.get("status") != "ok":
        entry["errors"] += 1
    entry["total_ms"] += _as_int(record.get("duration_ms"))
    provider = str(record.get("provider") or "")
    if provider:
        entry["providers"].add(provider)
    timestamp = str(record.get("timestamp") or "")
    if timestamp > entry["last_used"]:
        entry["last_used"] = timestamp
    return timestamp


def _workspace_env_path(workspace_root: Path) -> Path:
    return workspace_root.resolve() / ".env"


def _read_dotenv_value(env_path: Path, key: str) -> str:
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        env_key, value = line.split("=", 1)
        if env_key.strip() == key:
            return value.strip().strip("'\"")
    return ""


def _env_key_configured(key: str, workspace_root: Path) -> bool:
    if os.environ.get(key, "").strip():
        return True
    return bool(_read_dotenv_value(_workspace_env_path(workspace_root), key))


def _collect_env_refs(value: Any, *, default_source: str = "config") -> list[tuple[str, str]]:
    """Collect ``${VAR}`` references and explicit ``env`` map keys from MCP config."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(key: str, source: str) -> None:
        key = str(key).strip()
        if not key or key in seen:
            return
        seen.add(key)
        found.append((key, source))

    def walk(node: Any, source: str) -> None:
        if isinstance(node, str):
            for match in _ENV_REF_RE.finditer(node):
                add(match.group(1), source)
            return
        if isinstance(node, dict):
            for key, child in node.items():
                if source == "env":
                    # Keys under mcpServers.<name>.env are themselves env vars.
                    add(str(key), "env")
                    walk(child, "env")
                    continue
                child_source = source
                if source == "config" and key in {"headers", "env", "args", "url", "command"}:
                    child_source = str(key)
                walk(child, child_source)
            return
        if isinstance(node, (list, tuple)):
            for child in node:
                walk(child, source)

    walk(value, default_source)
    return found


def _resolve_env_template(value: str, workspace_root: Path) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        return os.environ.get(key, "").strip() or _read_dotenv_value(
            _workspace_env_path(workspace_root), key
        )

    return _ENV_REF_RE.sub(repl, value)

_READ = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
_DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)


# Create-time defaults for the `schedule` tool. Its signature defaults every
# field to None instead, so an "update" can tell a field the caller left out
# from one the caller set to the create default. Encoding the defaults in the
# signature made those two cases identical, and update stripped every field
# equal to a default: daily_time="09:00", frequency="weekly",
# archive_policy="manual" and every ""-clear vanished, so "move the daily
# report to 09:00" called schedule_update with an empty payload and still
# returned ok.
_SCHEDULE_CREATE_DEFAULTS: dict[str, Any] = {
    "prompt": "",
    "daily_time": "09:00",
    "timezone": "UTC",
    "frequency": "weekly",
    "title": "",
    "description": "",
    "provider": "",
    "model": "",
    "archive_policy": "manual",
    "workspace": "",
}


#: Which agent surface produced the current tool call: ``"mcp"`` for the
#: streamable-HTTP adapter, ``"cli"`` when ``ciao.agent_surface`` dispatched it.
#: Read by ``_record_tool_call`` so ``mcp_tool_calls.jsonl`` keeps one schema.
_SURFACE_VAR: contextvars.ContextVar[str] = contextvars.ContextVar("ciao_agent_surface", default="mcp")


@dataclass(slots=True)
class _Session:
    principal: McpPrincipal
    token: str
    expires_at: int


class McpSessionRegistry:
    """In-memory verifier for short-lived managed-process bearer tokens."""

    def __init__(self, ttl_seconds: int = 12 * 60 * 60) -> None:
        self._ttl_seconds = max(60, int(ttl_seconds))
        self._by_token: dict[str, _Session] = {}
        self._by_key: dict[tuple[str, str], str] = {}
        self._lock = threading.RLock()

    def issue(
        self,
        *,
        chat_id: str,
        project_id: str,
        workspace: str,
        provider: str,
    ) -> tuple[str, McpPrincipal]:
        key = (chat_id, provider)
        now = int(time.time())
        with self._lock:
            existing_token = self._by_key.get(key)
            existing = self._by_token.get(existing_token or "")
            if existing is not None and existing.expires_at > now:
                prior = existing.principal
                # Reuse only when scope still matches. A moved project or
                # corrected workspace must not keep minting stale-scoped tools.
                if (
                    prior.project_id == project_id
                    and prior.workspace == workspace
                ):
                    return existing.token, existing.principal
                # revoke() owns the _by_token/_by_key pairing; the lock is an
                # RLock so re-entering it here is safe.
                self.revoke(existing.token)
            token = secrets.token_urlsafe(36)
            principal = McpPrincipal(
                token_id=secrets.token_hex(8),
                chat_id=chat_id,
                project_id=project_id,
                workspace=workspace,
                provider=provider,
            )
            session = _Session(
                principal=principal,
                token=token,
                expires_at=now + self._ttl_seconds,
            )
            self._by_token[token] = session
            self._by_key[key] = token
            return token, principal

    def revoke_chat(self, chat_id: str) -> int:
        with self._lock:
            doomed = [token for token, item in self._by_token.items() if item.principal.chat_id == chat_id]
            for token in doomed:
                item = self._by_token.pop(token)
                self._by_key.pop((item.principal.chat_id, item.principal.provider), None)
            return len(doomed)

    def revoke(self, token: str) -> bool:
        with self._lock:
            item = self._by_token.pop(token, None)
            if item is None:
                return False
            self._by_key.pop((item.principal.chat_id, item.principal.provider), None)
            return True

    async def verify_token(self, token: str) -> AccessToken | None:
        now = int(time.time())
        with self._lock:
            item = self._by_token.get(token)
            if item is None:
                return None
            if item.expires_at <= now:
                self.revoke(token)
                return None
            return AccessToken(
                token=token,
                client_id="ciaobot-managed-provider",
                scopes=["ciaobot"],
                expires_at=item.expires_at,
                subject=item.principal.chat_id,
                claims=item.principal.to_claims(),
            )

    def status(self) -> dict[str, Any]:
        now = int(time.time())
        with self._lock:
            active = [item for item in self._by_token.values() if item.expires_at > now]
            return {
                "active_sessions": len(active),
                "providers": sorted({item.principal.provider for item in active}),
                "chats": sorted({item.principal.chat_id for item in active}),
            }


# ── Module-level operation table ────────────────────────────────────────────
# The control-plane operations are defined once here, shared by both surfaces.
# ``_register_tools`` registers the names in ``MCP_EXPOSED_OPERATIONS`` as MCP
# tools (the set shrinks slice by slice and is deleted in S6); the agent
# dispatcher resolves the same table. Each operation is a function
# ``(service, **kwargs) -> envelope`` that calls ``service._invoke`` with the
# same scoping, mode gate, and telemetry the MCP tools always used. Binding the
# first argument through ``Operation.bind`` (a ``functools.partial``) drops
# ``service`` from the signature, so FastMCP's ``Tool.from_function`` builds the
# exact same argument schema and pydantic validation for the MCP tool and the
# dispatcher alike — argument validation stays identical by construction.


class _NamedPartial(functools.partial):
    """A bound operation carrying the source function's name and docstring."""

    __name__: str
    __doc__: str


@dataclass(slots=True)
class Operation:
    """One control-plane operation shared by the MCP registry and agent CLI."""

    name: str
    annotations: ToolAnnotations
    description: str
    fn: Callable[..., Awaitable[dict[str, Any]]]

    def __post_init__(self) -> None:
        # Normalise module-level docstrings (4-space continuation indent) so
        # both the MCP tool description and the dispatcher's surface read
        # cleanly; the raw `__doc__` keeps the function-body indentation.
        self.description = inspect.cleandoc(self.description)

    def bind(self, service: Any) -> _NamedPartial:
        """A partial with ``service`` bound, so only the tool arguments remain.

        FastMCP needs ``__name__`` (for the Arguments model) and ``__doc__``
        (for the tool description) on the callable it inspects; ``partial``
        exposes neither, so the subclass sets them from the source operation.
        """
        bound = _NamedPartial(self.fn, service)
        bound.__name__ = self.name
        bound.__doc__ = self.description
        return bound


#: Operations still exposed as MCP tools. Shrinks slice by slice as groups
#: migrate to ``ciao <noun> <verb>``; deleted outright in S6. Empty since S5:
#: every operation runs through the agent dispatcher as a ``ciao …`` command.
MCP_EXPOSED_OPERATIONS: frozenset[str] = frozenset()


async def _op_context_get(service: CiaoMcpService) -> dict[str, Any]:
    """Return the active Ciaobot workspace, project, chat, provider, and
    control surface, plus local server/startup/active-chat status folded
    in under the ``system`` key (the former system_status_get)."""

    def _op(cp: CiaoControlPlane, p: McpPrincipal) -> dict[str, Any]:
        result = cp.context_get(p)
        status = cp.system_status_get(p)
        data = result.get("data") if isinstance(result, dict) else None
        if isinstance(data, dict):
            data["system"] = (
                status.get("data") if isinstance(status, dict) else status
            )
        return result

    return await service._invoke("context_get", _op)


async def _op_memory_status(service: CiaoMcpService) -> dict[str, Any]:
    """Report bounded native-guide memory usage and diagnostics."""
    return await service._invoke("memory_status", lambda cp, p: cp.memory_status(p))


async def _op_memory_update(service: CiaoMcpService, region: str, action: str, entry: str = "", match: str = "") -> dict[str, Any]:
    """Add, replace, or remove one entry in the native guide's memory.

    ``region`` is ``memory`` or ``profile``. Use ``match`` for
    replace/remove; use ``entry`` for add/replace. The region cap is
    advisory: the write always goes through, and the result carries
    ``over_cap`` with ``used_chars`` and ``char_limit`` when it exceeds
    the configured limit. Consolidation, not refusal, is what bounds a
    region.
    """
    return await service._invoke(
        "memory_update",
        lambda cp, p: cp.memory_update(
            p,
            region,
            action=action,  # type: ignore[arg-type]
            entry=entry,
            match=match,
        ),
        mutating=True,
    )


async def _op_vault_search(service: CiaoMcpService, query: str, limit: int = 10) -> dict[str, Any]:
    """Full-text search the active workspace vault."""
    return await service._invoke("vault_search", lambda cp, p: cp.vault_search(p, query, limit))


async def _op_vault_review(service: CiaoMcpService, action: str = "list", path: str = "", candidate_id: str = "",
                            disposition: str = "", confirm: str = "") -> dict[str, Any]:
    """List and decide scoped vault-note review candidates.

    The schedule may list candidates only. Trash, restore, and permanent
    deletion require an explicit attended action; permanent deletion
    additionally requires the candidate id as confirmation.
    """
    return await service._invoke(
        "vault_review",
        lambda cp, p: cp.vault_review(
            p, action, path=path, candidate_id=candidate_id,
            disposition=disposition, confirm=confirm,
        ),
        mutating=action != "list" and action != "inspect",
    )


async def _op_gws_status(service: CiaoMcpService) -> dict[str, Any]:
    """Report whether the active workspace's Google Workspace account is
    connected and its token is valid.

    Returns the linked profile name, whether credentials are present,
    the last health-monitor token reading, and whether a re-login is
    needed. Read-only: never runs ``gws auth status``. Use this before
    promising a Google call will work, and to tell the user their Google
    login has expired."""
    return await service._invoke("gws_status", lambda cp, p: cp.gws_status(p))


async def _op_projects_list(service: CiaoMcpService, include_completed: bool = False) -> dict[str, Any]:
    """List projects in the active workspace."""
    return await service._invoke("projects_list", lambda cp, p: cp.projects_list(p, include_completed))


async def _op_project_get(service: CiaoMcpService, project_id: str = "") -> dict[str, Any]:
    """Get one project by ID or name within the active workspace. Omit to get the active project."""
    return await service._invoke("project_get", lambda cp, p: cp.project_get(p, project_id))


async def _op_project(service: CiaoMcpService, action: str, name: str = "", context: str = "",
                      vault_folder: str | None = None, project_id: str = "", stem: str = "") -> dict[str, Any]:
    """Create, update, or restore a project in the active workspace.

    action:
        "create"  — create a project. name is the new project name;
            context is optional. project_id is ignored.
        "update"  — update project metadata or its safe vault-folder
            binding. Omit project_id for the active project. Pass
            name/context/vault_folder as overrides; None means "keep
            current value" (the control plane skips None fields).
        "restore" — restore a completed vault project into the active
            workspace. stem is the completed-project stem to restore.

    Args:
        name: (create) The new project name. (update) The new name, or
            empty to keep the current one.
        context: (create/update) Optional project context string.
        vault_folder: (update) Safe vault-folder binding, or None to
            keep the current value.
        project_id: (update) Project id or case-insensitive name. Omit
            for the active project.
        stem: (restore) The completed-project stem to restore.
    """
    if action == "create":
        return await service._invoke(
            "project",
            lambda cp, p: cp.project_create(p, name, context),
            mutating=True,
        )
    if action == "update":
        return await service._invoke(
            "project",
            lambda cp, p: cp.project_update(
                p, project_id, name=name or None, context=context or None,
                vault_folder=vault_folder,
            ),
            mutating=True,
        )
    if action == "restore":
        if not stem:
            raise ControlPlaneError("invalid_action", "stem is required for restore.")
        return await service._invoke(
            "project",
            lambda cp, p: cp.project_restore(p, stem),
            mutating=True,
        )
    raise ControlPlaneError("invalid_action", "action must be create, update, or restore.")


async def _op_project_action(service: CiaoMcpService, action: str, project_id: str = "") -> dict[str, Any]:
    """Run one lifecycle action on a project.

    action:
        "complete" — move a vault-backed project to completed and
            archive its active project record.
        "delete"   — delete a non-vault-backed project and its chats.
    """
    if action == "complete":
        return await service._invoke(
            "project_action",
            lambda cp, p: cp.project_complete(p, project_id),
            mutating=True,
        )
    if action == "delete":
        return await service._invoke(
            "project_action",
            lambda cp, p: cp.project_delete(p, project_id),
            mutating=True,
        )
    raise ControlPlaneError("invalid_action", "action must be complete or delete.")


async def _op_workspaces_list(service: CiaoMcpService) -> dict[str, Any]:
    """List all configured logical workspaces — names, vault roots, and
    defaults — not just the active one."""
    return await service._invoke("workspaces_list", lambda cp, p: cp.workspaces_list(p))


async def _op_chats_list(service: CiaoMcpService, project_id: str = "") -> dict[str, Any]:
    """List active and archived chats in the active workspace or one project."""
    return await service._invoke("chats_list", lambda cp, p: cp.chats_list(p, project_id))


async def _op_chat_get(service: CiaoMcpService, chat_id: str = "") -> dict[str, Any]:
    """Get one chat by ID within the active workspace. Omit to get the calling chat."""
    return await service._invoke("chat_get", lambda cp, p: cp.chat_get(p, chat_id))


async def _op_chat_create(service: CiaoMcpService, project_id: str | None = None, title: str = "New Chat",
                          provider: str | None = None, model: str | None = None,
                          mode: str | None = None, prompt: str | None = None) -> dict[str, Any]:
    """Create a fresh chat, optionally sending its first prompt in the same call.

    Args:
        project_id: Project id or case-insensitive name. Omit to use the
            calling chat's own project — you don't need to call
            projects_list first for the common case of a sub-topic in
            the current project.
        provider: Provider override. Omit to inherit the target
            project's workspace default.
        model: Model override. Omit to inherit the target project's
            workspace default.
        prompt: If given, immediately starts the new chat's first turn
            with this text — skips a separate chat_send call.
    """
    return await service._invoke(
        "chat_create",
        lambda cp, p: cp.chat_create(
            p,
            project_id,
            title=title,
            provider=provider,
            model=model,
            mode=mode,
            prompt=prompt,
        ),
        mutating=True,
    )


async def _op_chat_update(service: CiaoMcpService, chat_id: str = "", title: str | None = None,
                          provider: str | None = None, model: str | None = None,
                          mode: str | None = None, thinking_level: str | None = None,
                          project_id: str | None = None) -> dict[str, Any]:
    """Update chat metadata and same-backend model settings. Omit chat_id for calling chat."""
    return await service._invoke(
        "chat_update",
        lambda cp, p: cp.chat_update(
            p,
            chat_id,
            title=title,
            provider=provider,
            model=model,
            mode=mode,
            thinking_level=thinking_level,
            project_id=project_id,
        ),
        mutating=True,
    )


async def _op_chat_send(service: CiaoMcpService, chat_id: str, prompt: str) -> dict[str, Any]:
    """Start or queue a user turn in another Ciaobot chat."""
    return await service._invoke("chat_send", lambda cp, p: cp.chat_send(p, chat_id, prompt), mutating=True)


async def _op_chat_continue(service: CiaoMcpService, chat_id: str) -> dict[str, Any]:
    """Continue an archived chat as a new active chat."""
    return await service._invoke("chat_continue", lambda cp, p: cp.chat_continue(p, chat_id), mutating=True)


async def _op_chat_retry(service: CiaoMcpService, chat_id: str = "", action: str = "try_now", prompt: str = "") -> dict[str, Any]:
    """Manage a deferred provider-limit retry: set, stop, or (default)
    immediately try it now."""
    return await service._invoke(
        "chat_retry",
        lambda cp, p: cp.chat_retry_update(
            p, chat_id, action=action, prompt=prompt  # type: ignore[arg-type]
        ),
        mutating=True,
    )


async def _op_chat_handover(service: CiaoMcpService, chat_id: str = "", provider: str = "", model: str = "",
                            messages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Continue a chat on a fresh provider session with optional visible
    history. With provider and model both empty, this just clears the
    current provider session in place (the former chat_new_session)."""

    def _op(cp: CiaoControlPlane, p: McpPrincipal) -> Any:
        if not provider and not model:
            return cp.chat_new_session(p, chat_id)
        return cp.chat_handover(
            p,
            chat_id,
            provider=provider,
            model=model,
            messages=messages,
        )

    return await service._invoke("chat_handover", _op, mutating=True)


async def _op_chat_archive(service: CiaoMcpService, chat_id: str = "") -> dict[str, Any]:
    """Archive a chat to the vault and trigger normal post-archive processing.

    Args:
        chat_id: The ID of the chat to archive. Omit or pass empty to
            archive the calling chat.
    """
    return await service._invoke(
        "chat_archive", lambda cp, p: cp.chat_archive(p, chat_id), mutating=True
    )


async def _op_chat_delete(service: CiaoMcpService, chat_id: str = "") -> dict[str, Any]:
    """Delete a chat; deleting the current caller is deferred until the turn finishes."""
    return await service._invoke(
        "chat_delete", lambda cp, p: cp.chat_delete(p, chat_id), mutating=True
    )


async def _op_chat_stop(service: CiaoMcpService, chat_id: str) -> dict[str, Any]:
    """Stop another chat's active provider turn; the current caller cannot stop itself."""
    return await service._invoke(
        "chat_stop", lambda cp, p: cp.chat_stop(p, chat_id), mutating=True
    )


async def _op_background_run_start(service: CiaoMcpService, cmd: list[str], cwd: str = "",
                                   env: dict[str, str] | None = None,
                                   timeout_s: int = 1800, label: str = "") -> dict[str, Any]:
    """Run one command in a tracked background subprocess and get woken
    when it exits.

    Sits between a plain `nohup` (survives the turn, but you lose track
    of it) and a full agent loop. Use it
    when the work is a single script that takes minutes: a fetch, a
    build, a data enrichment pass. There is no model in the loop and no
    tool access — it runs the command, nothing else.

    This call does NOT block: it returns as soon as the process starts.
    End your turn after starting one. Do not poll; Ciaobot sends you a
    fresh turn with the status, exit code, log tail, and log path when
    it finishes. background_run_status is for the rare case where you
    need the state mid-turn.

    Args:
        cmd: Argv list, e.g. ["./scripts/report.sh", "--full"]. A
            single string is rejected: there is no shell, so nothing
            would split it. For shell features, run
            ["bash", "-lc", "..."] explicitly and own that choice.
        cwd: Directory for the run, relative to THIS chat's workspace
            root — the same directory your own shell commands run in,
            so a path that works in Bash works here unchanged.
            Defaults to that root. Paths outside it are rejected.
        env: Extra environment variables. Loader hooks (LD_PRELOAD and
            friends) and Ciaobot's own session token are rejected.
        timeout_s: Wall-clock ceiling; the process tree is terminated
            past it and the run reports as failed. Default 1800.
        label: Short name for the wake report, e.g. "adoption report".
    """
    return await service._invoke(
        "background_run_start",
        lambda cp, p: cp.background_run_start(
            p, cmd=cmd, cwd=cwd, env=env, timeout_s=timeout_s, label=label
        ),
        mutating=True,
    )


async def _op_background_run_status(service: CiaoMcpService, run_id: str, lines: int = 50) -> dict[str, Any]:
    """Status, exit code, and log tail for a background run this chat
    started.

    Prefer waiting for the wake turn. Only reach for this when you need
    the state inside the current turn — a run started by another chat
    reports as not found.
    """
    return await service._invoke(
        "background_run_status",
        lambda cp, p: cp.background_run_status(p, run_id, lines),
    )


async def _op_background_run_cancel(service: CiaoMcpService, run_id: str) -> dict[str, Any]:
    """Stop a background run: SIGTERM to its process group, then
    SIGKILL after a short grace period.

    Idempotent — cancelling an already-finished run returns its final
    state unchanged.
    """
    return await service._invoke(
        "background_run_cancel",
        lambda cp, p: cp.background_run_cancel(p, run_id),
        mutating=True,
    )


async def _op_schedules_list(service: CiaoMcpService) -> dict[str, Any]:
    """List schedules in the active workspace with their next run."""
    return await service._invoke("schedules_list", lambda cp, p: cp.schedules_list(p))


async def _op_schedule(service: CiaoMcpService, action: str, prompt: str | None = None,
                       daily_time: str | None = None, timezone: str | None = None,
                       frequency: str | None = None, interval_minutes: int | None = None,
                       days_of_week: list[str] | None = None, day_of_month: int | None = None,
                       run_at_date: str | None = None, project_id: str | None = None,
                       chat_id: str | None = None, title: str | None = None,
                       description: str | None = None, provider: str | None = None,
                       model: str | None = None, archive_policy: str | None = None,
                       workspace: str | None = None, schedule_id: str = "") -> dict[str, Any]:
    """Preview, create, or update a Ciaobot schedule (recurring, one-off, or manual-only).

    action:
        "preview" — validate and compute next_run without saving. Call this
            before "create" for a new recurring schedule and show the user
            the resulting next_run, workspace, and project as part of the
            draft. A missing or invalid next_run means the fields don't
            validate as given — don't create it yet.
        "create"  — create a validated schedule. Show the user a concise
            draft and get confirmation before creating it, unless they
            already explicitly asked you to apply it — call "preview"
            first. The draft must include next_run, the target
            workspace, and the target project. Ask if they want a
            different workspace/project when that isn't obvious from
            the request.
        "update"  — update an existing schedule through validated fields.
            Field semantics match "create". System schedules (scope=system)
            only accept enabled/workspace changes — everything else raises
            system_schedule_read_only. Pass schedule_id to target the
            schedule; all other fields are optional overrides.

    Every field but action is optional and unset by default:
    preview/create fall back to the default noted below, update leaves
    an unset field untouched. Pass a field (including "" to clear a
    title, provider, or model) only when you mean to change it.

    Field semantics (identical for preview/create; update treats all but
    schedule_id as optional overrides):

        prompt: The prompt dispatched each run. Start with the goal in
            3-7 words (becomes the chat-title hint); keep only
            schedule-specific logic — a fresh project run already
            inherits canonical docs and skills. Aim for <=1000 chars
            for a simple check, <=4000 for an aggregation/review. For
            routine checks, have it exit early with a one-line no-op
            when there's nothing to report. Supports two placeholders:
            {{ERROR_LOG}} (sanitized server error tail) and
            {{ISSUE_REPORT}} (server errors + failed background jobs);
            Ciaobot clears the consumed error log after a clean run
            that uses one.
        daily_time: Local HH:MM in `timezone`, default "09:00"
            (persisted as the legacy field daily_time_utc). Ignored for
            frequency="interval", which has no time of day.
        timezone: IANA name, e.g. "Europe/Rome", default "UTC". Use
            the user's local timezone unless they ask for UTC.
        frequency: "daily" | "weekly" | "monthly" | "manual" | "once" |
            "interval"; default "weekly". Use "interval" for sub-day
            recurrence ("every 30 minutes") — see interval_minutes.
        interval_minutes: interval only — whole minutes between runs,
            minimum 1, default 10. Combine with project_id for a fresh
            chat per run. Give the prompt a short fixed no-change
            response for a no-op run, so repeated runs stay cheap and
            scannable. A run that comes due while its previous run is
            still streaming is skipped and retried on the next tick,
            not queued; intervals missed while the server was down are
            not replayed.
        days_of_week: weekly only — lowercase "mon".."sun".
        day_of_month: 1-31, monthly only.
        run_at_date: "YYYY-MM-DD", once only, must be in the future.
        project_id: Project id or case-insensitive name — creates a
            fresh chat in that project per run. When omitted, defaults
            to this chat's project and stamps that project's workspace.
            Preferred for vault-aware automation. Schedules bind to a
            project and workspace, never to a chat: a chat-bound run
            posts into one conversation forever (invisible unless you
            watch that chat) and fails silently once the chat is
            archived. There is no chat_id parameter.
        model: Empty inherits the target workspace's default model at
            dispatch time; override only when necessary.
        provider: Empty inherits the target workspace's default
            provider at dispatch time; override only when necessary.
        archive_policy: "manual" (default) | "auto".
        workspace: Omit in almost every case — the schedule is created
            in this chat's workspace. Any other name is refused unless
            it restates this chat's workspace: schedules are
            auto-approved model input and their unattended runs execute
            in bypass, so planting one in another workspace would run
            there with that workspace's guide, integrations, and file
            authority and no operator approval. To automate a second
            workspace, work from a chat scoped to it or ask the
            operator.
        schedule_id: (update only) The schedule to update.

    An enabled schedule with a missed latest occurrence (e.g. the
    server was off, or a run stopped before it finished) runs once on
    startup; older missed intervals are not replayed, a run that
    completed is never repeated, and a slot gets one automatic
    recovery. Interval schedules are excluded from that catch-up:
    their cadence simply resumes.
    """
    # Snapshot the caller's arguments before any other local exists.
    # Doing it first is what keeps helper locals out of the payload: a
    # leaked `_defaults` dict once reached the control plane as
    # `Unknown schedule fields: _defaults`.
    supplied: dict[str, Any] = {
        key: value for key, value in locals().items()
        if key not in {"service", "action", "schedule_id"}
    }
    # Refuse chat bindings at the tool surface, before the control
    # plane, so the caller learns the stance even on an update payload
    # where the control plane would only see chat_id among many fields.
    # The parameter stays in the signature (pydantic silently drops
    # undeclared arguments, which would hide the refusal) but is
    # documented as not existing.
    if supplied.get("chat_id") is not None:
        raise ControlPlaneError(
            "chat_binding_unsupported",
            "Schedules bind to a project, not a chat. Pass project_id "
            "(each run then opens a fresh chat in it, visible in the "
            "sidebar) — chat_id bindings fail silently once their target "
            "chat is archived.",
        )
    if action in {"preview", "create"}:
        values = {
            key: _SCHEDULE_CREATE_DEFAULTS.get(key, value) if value is None else value
            for key, value in supplied.items()
        }
        if action == "preview":
            return await service._invoke("schedule", lambda cp, p: cp.schedule_preview(p, **values))
        return await service._invoke("schedule", lambda cp, p: cp.schedule_create(p, **values), mutating=True)
    if action == "update":
        if not schedule_id:
            raise ControlPlaneError("invalid_action", "schedule_id is required for update.")
        # update applies exactly the fields the caller passed. None (the
        # signature default) is the only "not supplied" marker, so a
        # value that happens to equal a create default still goes
        # through. Filter here rather than leaning on schedule_update's
        # own None-skipping: its system-schedule guard inspects every
        # key it is handed, including ones it would later skip.
        values = {key: value for key, value in supplied.items() if value is not None}
        if not values:
            raise ControlPlaneError(
                "invalid_action",
                "update needs at least one field to change besides schedule_id.",
            )
        # "" now reaches the control plane instead of being dropped,
        # which is the point for title/provider/model. A schedule with
        # no prompt dispatches nothing though, and neither
        # schedule_update nor schedule_preview rejects a blank one, so
        # an accidental prompt="" must fail here rather than quietly
        # wipe a working routine.
        if values.get("prompt") == "":
            raise ControlPlaneError(
                "empty_prompt", "prompt cannot be cleared; pass the new prompt text."
            )
        return await service._invoke(
            "schedule",
            lambda cp, p: cp.schedule_update(p, schedule_id, **values),
            mutating=True,
        )
    raise ControlPlaneError("invalid_action", "action must be preview, create, or update.")


async def _op_schedule_action(service: CiaoMcpService, schedule_id: str, action: str) -> dict[str, Any]:
    """Run one lifecycle action on a schedule.

    action:
        "pause"  — pause without deleting.
        "resume" — resume a paused schedule.
        "run"    — dispatch immediately through the normal chat pipeline.
        "delete" — delete a removable user schedule (destructive). System
            schedules (scope=system) cannot be deleted — this raises
            schedule_not_removable instead.
    """
    dispatch = {
        "pause": lambda cp, p: cp.schedule_update(p, schedule_id, enabled=False),
        "resume": lambda cp, p: cp.schedule_update(p, schedule_id, enabled=True),
        "run": lambda cp, p: cp.schedule_run(p, schedule_id),
        "delete": lambda cp, p: cp.schedule_delete(p, schedule_id),
    }
    op = dispatch.get(action)
    if op is None:
        raise ControlPlaneError(
            "invalid_action", "action must be pause, resume, run, or delete."
        )
    return await service._invoke("schedule_action", op, mutating=True)


async def _op_file_surface(service: CiaoMcpService, path: str) -> dict[str, Any]:
    """Deliberately open a workspace file in the user's pinned preview panel.

    Use this to show the user a file you produced or want to highlight,
    even one you only read, or one a subagent wrote, instead of relying on
    them to notice it. Ordinary Write/Edit calls no longer auto-open the
    panel; call this when a file is worth surfacing.

    The pin happens in the browser: this call only validates the path and
    reports two independent signals. ``viewers`` is how many open chat
    sockets are watching this chat right now; it can be 0 right after a
    successful pin, and nonzero even when the panel did not open.
    ``stream_state`` is "active" or "none" and says whether a turn is
    currently streaming for this chat, nothing about the panel. Never
    read either field as proof the panel opened or failed to open: say
    you called file_surface, and if the user reports nothing happened,
    do not claim you already confirmed it failed."""
    return await service._invoke("file_surface", lambda cp, p: cp.file_surface(p, path))


#: Every operation, one entry per operation, keyed by the MCP-era tool name.
OPERATIONS: tuple[Operation, ...] = (
    Operation("context_get", _READ, _op_context_get.__doc__ or "", _op_context_get),
    Operation("memory_status", _READ, _op_memory_status.__doc__ or "", _op_memory_status),
    Operation("memory_update", _WRITE, _op_memory_update.__doc__ or "", _op_memory_update),
    Operation("vault_search", _READ, _op_vault_search.__doc__ or "", _op_vault_search),
    Operation("vault_review", _DESTRUCTIVE, _op_vault_review.__doc__ or "", _op_vault_review),
    Operation("gws_status", _READ, _op_gws_status.__doc__ or "", _op_gws_status),
    Operation("projects_list", _READ, _op_projects_list.__doc__ or "", _op_projects_list),
    Operation("project_get", _READ, _op_project_get.__doc__ or "", _op_project_get),
    Operation("project", _WRITE, _op_project.__doc__ or "", _op_project),
    Operation("project_action", _DESTRUCTIVE, _op_project_action.__doc__ or "", _op_project_action),
    Operation("workspaces_list", _READ, _op_workspaces_list.__doc__ or "", _op_workspaces_list),
    Operation("chats_list", _READ, _op_chats_list.__doc__ or "", _op_chats_list),
    Operation("chat_get", _READ, _op_chat_get.__doc__ or "", _op_chat_get),
    Operation("chat_create", _WRITE, _op_chat_create.__doc__ or "", _op_chat_create),
    Operation("chat_update", _WRITE, _op_chat_update.__doc__ or "", _op_chat_update),
    Operation("chat_send", _WRITE, _op_chat_send.__doc__ or "", _op_chat_send),
    Operation("chat_continue", _WRITE, _op_chat_continue.__doc__ or "", _op_chat_continue),
    Operation("chat_retry", _WRITE, _op_chat_retry.__doc__ or "", _op_chat_retry),
    Operation("chat_handover", _WRITE, _op_chat_handover.__doc__ or "", _op_chat_handover),
    Operation("chat_archive", _WRITE, _op_chat_archive.__doc__ or "", _op_chat_archive),
    Operation("chat_delete", _DESTRUCTIVE, _op_chat_delete.__doc__ or "", _op_chat_delete),
    Operation("chat_stop", _DESTRUCTIVE, _op_chat_stop.__doc__ or "", _op_chat_stop),
    Operation("background_run_start", _DESTRUCTIVE, _op_background_run_start.__doc__ or "", _op_background_run_start),
    Operation("background_run_status", _READ, _op_background_run_status.__doc__ or "", _op_background_run_status),
    Operation("background_run_cancel", _DESTRUCTIVE, _op_background_run_cancel.__doc__ or "", _op_background_run_cancel),
    Operation("schedules_list", _READ, _op_schedules_list.__doc__ or "", _op_schedules_list),
    Operation("schedule", _WRITE, _op_schedule.__doc__ or "", _op_schedule),
    Operation("schedule_action", _DESTRUCTIVE, _op_schedule_action.__doc__ or "", _op_schedule_action),
    Operation("file_surface", _READ, _op_file_surface.__doc__ or "", _op_file_surface),
)

OPERATIONS_BY_NAME: dict[str, Operation] = {operation.name: operation for operation in OPERATIONS}


class CiaoMcpService:
    """Own the FastMCP server, authentication, tool catalog, and telemetry."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.registry = McpSessionRegistry()
        self.control_plane: CiaoControlPlane | None = None
        self._tool_names: set[str] = set()
        self.operation_table: dict[str, Operation] = dict(OPERATIONS_BY_NAME)
        self._last_error = ""
        self._telemetry_path = Path(config.state_path).parent / "mcp_tool_calls.jsonl"
        # Trimming drops detailed records, so their counters are folded into
        # this sidecar first: without it a rotation would silently reset the
        # lifetime totals the Settings usage table reports.
        self._telemetry_totals_path = Path(config.state_path).parent / "mcp_tool_calls_totals.json"
        self._usage_lock = threading.Lock()
        self._usage_cache: tuple[tuple[Any, ...], _UsageAggregate] | None = None
        issuer = f"http://127.0.0.1:{int(config.pwa_port)}"
        self.server = FastMCP(
            "ciaobot",
            instructions=(
                "Use these tools for Ciaobot memory, vault, and files. "
                "Projects, chats, schedules, background runs, and other "
                "operations are `ciao <noun> <verb>` commands (see the "
                "ciao-cli skill); prefer them over curl or direct .runtime "
                "edits. All paths are relative to the active workspace or "
                "vault."
            ),
            host="127.0.0.1",
            streamable_http_path="/",
            json_response=True,
            stateless_http=True,
            token_verifier=self.registry,
            auth=AuthSettings(
                # pydantic coerces the str to AnyHttpUrl during validation.
                issuer_url=cast(AnyHttpUrl, issuer),
                required_scopes=["ciaobot"],
                resource_server_url=None,
            ),
        )
        self._register_tools()
        self.http_app = self.server.streamable_http_app()

    def bind(self, control_plane: CiaoControlPlane) -> None:
        self.control_plane = control_plane

    @property
    def url(self) -> str:
        # Starlette's Mount canonicalizes the inner root to a trailing slash.
        return f"http://127.0.0.1:{int(self.config.pwa_port)}/mcp/"

    #: Exposed for ``ciao.agent_surface.AgentDispatcher``.
    surface_var = _SURFACE_VAR

    def tool_for(self, operation: Operation) -> Tool:
        """A FastMCP ``Tool`` for one operation, built the same way MCP registers it.

        The dispatcher builds a fresh :class:`Tool` from the shared operation
        entry (via :meth:`Operation.bind`) so its pydantic argument validation
        is byte-for-byte the schema the MCP adapter serves for the same
        operation — the two surfaces cannot drift apart.
        """
        return Tool.from_function(
            operation.bind(self),
            name=operation.name,
            annotations=operation.annotations,
            description=operation.description,
            structured_output=True,
        )

    @property
    def agent_url(self) -> str:
        """Base URL of the agent CLI transport; the operation name is appended."""
        return f"http://127.0.0.1:{int(self.config.pwa_port)}/agent/v1/"

    def credentials_for_chat(self, chat: Any, project: Any) -> tuple[str, str]:
        token, _principal = self.registry.issue(
            chat_id=chat.chat_id,
            project_id=chat.project_id,
            workspace=project.workspace,
            provider=chat.provider,
        )
        return self.url, token

    @asynccontextmanager
    async def lifespan(self):
        async with self.server.session_manager.run():
            yield

    def status(self) -> dict[str, Any]:
        workspace_root = Path(getattr(self.config, "workspace_root", Path.cwd())).resolve()
        return {
            # Retained for the PWA status payload's shape. The control plane is
            # mandatory, so a live service is by definition enabled.
            "enabled": True,
            "url": self.url,
            "bound": self.control_plane is not None,
            "tool_count": len(self._tool_names),
            "tools": sorted(self._tool_names),
            "last_error": self._last_error,
            "env_path": str(_workspace_env_path(workspace_root)),
            "project_servers": self._discover_project_mcp_servers(),
            **self.registry.status(),
        }

    def project_server_env_keys(self) -> set[str]:
        """Env var names referenced by discovered project MCP server configs."""
        keys: set[str] = set()
        for server in self._discover_project_mcp_servers():
            for entry in server.get("env_keys") or []:
                if isinstance(entry, dict) and entry.get("key"):
                    keys.add(str(entry["key"]))
        return keys

    def probe_project_server_tools(self, name: str) -> dict[str, Any]:
        """Lazy tools discovery for one project MCP server.

        HTTP/SSE servers are probed with ``tools/list``. Stdio servers return
        observed telemetry tools only (spawning the command from Settings is
        intentionally avoided).
        """
        servers = {str(s.get("name")): s for s in self._discover_project_mcp_servers()}
        server = servers.get(name)
        if server is None:
            return {"ok": False, "error": f"unknown MCP server '{name}'", "tools": []}
        observed = list(server.get("tools") or [])
        if server.get("transport") != "http":
            return {
                "ok": True,
                "name": name,
                "tools": observed,
                "tools_source": server.get("tools_source") or ("observed" if observed else "none"),
                "tools_note": (
                    "Stdio MCP tools are discovered when a chat loads the server. "
                    f"They surface as {server.get('tool_prefix')}*."
                ),
                "tool_prefix": server.get("tool_prefix"),
            }
        if not server.get("ready", True):
            return {
                "ok": False,
                "name": name,
                "error": "Configure the required .env keys before probing tools.",
                "tools": observed,
                "tools_source": server.get("tools_source") or ("observed" if observed else "none"),
                "tool_prefix": server.get("tool_prefix"),
            }
        workspace_root = Path(getattr(self.config, "workspace_root", Path.cwd())).resolve()
        raw_meta = server.get("_meta")
        meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        raw_headers = meta.get("headers")
        headers_raw: dict[str, Any] = raw_headers if isinstance(raw_headers, dict) else {}
        headers = {
            str(k): _resolve_env_template(str(v), workspace_root)
            for k, v in headers_raw.items()
        }
        tools, error = _probe_http_mcp_tools(str(server.get("url") or ""), headers=headers)
        prefixed = [f"mcp__{name}__{tool}" for tool in tools]
        merged = sorted(set(observed) | set(prefixed) | set(tools))
        if error and not merged:
            return {
                "ok": False,
                "name": name,
                "error": error,
                "tools": observed,
                "tools_source": "observed" if observed else "none",
                "tool_prefix": server.get("tool_prefix"),
            }
        return {
            "ok": True,
            "name": name,
            "tools": merged,
            "tools_source": "probed" if tools else ("observed" if observed else "none"),
            "tools_note": "" if tools else (error or ""),
            "tool_prefix": server.get("tool_prefix"),
        }

    def _discover_project_mcp_servers(self) -> list[dict[str, Any]]:
        servers: list[dict[str, Any]] = []
        workspace_root = Path(getattr(self.config, "workspace_root", Path.cwd())).resolve()
        env_path = _workspace_env_path(workspace_root)
        runtime_root = Path(getattr(self.config, "state_path", workspace_root / ".runtime" / "state.json")).parent
        candidates: list[tuple[str, Path]] = [
            ("project", workspace_root / ".mcp.json"),
            ("project", workspace_root.parent / ".mcp.json"),
            ("project", workspace_root.parent / "ciao" / ".mcp.json"),
        ]

        seen: set[str] = set()
        for source, path in candidates:
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            mcp_dict = data.get("mcpServers") or data.get("mcp_servers") or {}
            if not isinstance(mcp_dict, dict):
                continue
            for name, meta in mcp_dict.items():
                server_name = str(name)
                if server_name in seen:
                    continue
                seen.add(server_name)
                meta_dict = meta if isinstance(meta, dict) else {}
                url = str(meta_dict.get("url", "") or "")
                command = str(meta_dict.get("command", "") or "")
                args_raw = meta_dict.get("args") or []
                args = [str(item) for item in args_raw] if isinstance(args_raw, list) else []
                transport = "http" if url else "stdio"
                env_refs = _collect_env_refs(meta_dict)
                env_keys = [
                    {
                        "key": key,
                        "configured": _env_key_configured(key, workspace_root),
                        "source": ref_source,
                    }
                    for key, ref_source in env_refs
                ]
                ready = all(entry["configured"] for entry in env_keys)
                observed = _observed_project_mcp_tools(runtime_root, server_name)
                tool_prefix = f"mcp__{server_name}__"
                payload: dict[str, Any] = {
                    "name": server_name,
                    "url": url,
                    "command": command,
                    "args": args,
                    "transport": transport,
                    "source": f"{source} ({path.parent.name})",
                    "config_path": str(path.resolve()),
                    "env_path": str(env_path),
                    "env_keys": env_keys,
                    "ready": ready,
                    "tool_prefix": tool_prefix,
                    "tools": observed,
                    "tools_source": "observed" if observed else "none",
                    "tools_note": (
                        ""
                        if observed
                        else (
                            f"Tools load when a chat starts this server "
                            f"(prefix {tool_prefix}*)."
                            if ready
                            else "Add the required .env keys below, then start a chat that uses this server."
                        )
                    ),
                    # Internal probe helper; stripped from status responses.
                    "_meta": meta_dict,
                }
                servers.append(payload)
        return servers

    def status_for_api(self) -> dict[str, Any]:
        """Public status payload without internal probe helpers."""
        payload = self.status()
        servers = []
        for server in payload.get("project_servers") or []:
            if not isinstance(server, dict):
                continue
            public = {k: v for k, v in server.items() if not str(k).startswith("_")}
            # Source is always project-scoped for this list; keep it out of the UI payload.
            public.pop("source", None)
            servers.append(public)
        payload["project_servers"] = servers
        return payload

    def _workspace_root(self) -> Path:
        return Path(getattr(self.config, "workspace_root", Path.cwd())).resolve()

    def _project_mcp_json_candidates(self) -> list[Path]:
        workspace_root = self._workspace_root()
        return [
            workspace_root / ".mcp.json",
            workspace_root.parent / ".mcp.json",
            # Sibling checkout used by some local monorepo layouts.
            workspace_root.parent / "ciao" / ".mcp.json",
        ]

    def _preferred_mcp_json_path(self, *, create: bool = False) -> Path | None:
        """Prefer an existing project ``.mcp.json`` that already has servers."""
        for path in self._project_mcp_json_candidates():
            if not path.is_file():
                continue
            try:
                data = self._read_mcp_json(path)
                servers = data.get("mcpServers") or data.get("mcp_servers") or {}
            except ValueError:
                continue
            if isinstance(servers, dict) and servers:
                return path
        for path in self._project_mcp_json_candidates():
            if path.is_file():
                return path
        if create:
            return self._workspace_root() / ".mcp.json"
        return None

    def _read_mcp_json(self, path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError(f"invalid .mcp.json at {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"invalid .mcp.json at {path}: expected object")
        return data

    def _write_mcp_json(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def _mcp_servers_dict(self, data: dict[str, Any]) -> dict[str, Any]:
        raw = data.get("mcpServers")
        if raw is None:
            raw = data.get("mcp_servers")
        if raw is None:
            servers: dict[str, Any] = {}
            data["mcpServers"] = servers
            return servers
        if not isinstance(raw, dict):
            raise ValueError(".mcp.json mcpServers must be an object")
        if "mcpServers" not in data and "mcp_servers" in data:
            # Normalize legacy key on write.
            data["mcpServers"] = raw
            data.pop("mcp_servers", None)
        return raw

    def _find_server_file(self, name: str) -> tuple[Path, dict[str, Any], dict[str, Any]] | None:
        for path in self._project_mcp_json_candidates():
            if not path.is_file():
                continue
            try:
                data = self._read_mcp_json(path)
                servers = self._mcp_servers_dict(data)
            except ValueError:
                continue
            if name in servers:
                return path, data, servers
        return None

    def upsert_project_server(
        self,
        name: str,
        *,
        url: str = "",
        command: str = "",
        args: list[str] | None = None,
        env_keys: dict[str, str] | None = None,
        bind_env_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create or update a project MCP server in ``.mcp.json``."""
        server_name = str(name or "").strip()
        if not server_name or "/" in server_name or "\\" in server_name:
            raise ValueError("invalid MCP server name")
        url = str(url or "").strip()
        command = str(command or "").strip()
        args_list = [str(item).strip() for item in (args or []) if str(item).strip()]
        if url and command:
            raise ValueError("provide either url or command, not both")
        if not url and not command:
            raise ValueError("url or command is required")

        found = self._find_server_file(server_name)
        if found:
            path, data, servers = found
            meta = servers.get(server_name)
            meta = dict(meta) if isinstance(meta, dict) else {}
        else:
            preferred = self._preferred_mcp_json_path(create=True)
            if preferred is None:
                raise ValueError("no writable .mcp.json location is available")
            path = preferred
            data = self._read_mcp_json(path) if path.is_file() else {}
            servers = self._mcp_servers_dict(data)
            if server_name in servers:
                meta = dict(servers[server_name]) if isinstance(servers[server_name], dict) else {}
            else:
                meta = {}

        if url:
            meta["type"] = "http"
            meta["url"] = url
            meta.pop("command", None)
            meta.pop("args", None)
        else:
            meta.pop("type", None)
            meta.pop("url", None)
            meta["command"] = command
            if args_list:
                meta["args"] = args_list
            else:
                meta.pop("args", None)

        keys_to_bind = [str(k).strip() for k in (bind_env_keys or []) if str(k).strip()]
        if env_keys:
            keys_to_bind.extend(str(k).strip() for k in env_keys if str(k).strip())
        if keys_to_bind:
            raw_env = meta.get("env")
            env_map: dict[str, Any] = dict(raw_env) if isinstance(raw_env, dict) else {}
            for key in keys_to_bind:
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                    raise ValueError(f"invalid env key '{key}'")
                # Keep existing header/env templates; only add missing env bindings.
                existing_refs = {ref for ref, _src in _collect_env_refs(meta)}
                if key not in existing_refs:
                    env_map[key] = f"${{{key}}}"
            if env_map:
                meta["env"] = env_map

        servers[server_name] = meta
        data["mcpServers"] = servers
        self._write_mcp_json(path, data)

        if env_keys:
            updates = {
                str(k).strip(): str(v)
                for k, v in env_keys.items()
                if str(k).strip() and str(v).strip()
            }
            if updates:
                env_path = _workspace_env_path(self._workspace_root())
                _write_mcp_env_values(env_path, updates)
                for key, value in updates.items():
                    os.environ[key] = value.strip()

        return self.status_for_api()

    def delete_project_server(self, name: str) -> dict[str, Any]:
        found = self._find_server_file(name)
        if found is None:
            raise ValueError(f"unknown MCP server '{name}'")
        path, data, servers = found
        servers.pop(name, None)
        data["mcpServers"] = servers
        self._write_mcp_json(path, data)
        return self.status_for_api()

    def save_project_server_env_keys(
        self,
        updates: dict[str, str],
        *,
        server: str | None = None,
        bind_missing: bool = True,
    ) -> dict[str, Any]:
        """Write MCP secrets to ``.env`` and optionally bind new keys into ``.mcp.json``."""
        cleaned = {
            str(k).strip(): str(v)
            for k, v in updates.items()
            if str(k).strip()
        }
        if not cleaned:
            raise ValueError("no keys to save")
        for key in cleaned:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError(f"invalid env key '{key}'")

        allowed = self.project_server_env_keys()
        unknown = sorted(set(cleaned) - allowed)
        if unknown:
            if not server:
                raise ValueError(
                    f"unsupported MCP env key(s): {', '.join(unknown)}. "
                    "Pass server=<name> to bind new keys into that MCP config."
                )
            if not bind_missing:
                raise ValueError(f"unsupported MCP env key(s): {', '.join(unknown)}")
            found = self._find_server_file(server)
            if found is None:
                raise ValueError(f"unknown MCP server '{server}'")
            path, data, servers = found
            meta = dict(servers.get(server) or {})
            env_map = dict(meta.get("env") or {}) if isinstance(meta.get("env"), dict) else {}
            for key in unknown:
                env_map[key] = f"${{{key}}}"
            meta["env"] = env_map
            servers[server] = meta
            data["mcpServers"] = servers
            self._write_mcp_json(path, data)

        env_path = _workspace_env_path(self._workspace_root())
        _write_mcp_env_values(env_path, cleaned)
        for key, value in cleaned.items():
            value = value.strip()
            if value:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        return self.status_for_api()

    def usage(self, *, limit: int | None = None) -> dict[str, Any]:
        """Aggregate per-tool call counts from the telemetry log.

        Counts are lifetime: the retained window of ``mcp_tool_calls.jsonl``
        (written by :meth:`_record_tool_call`) is folded on top of the
        counters the size guard already rolled into the totals sidecar, so
        rotation does not change what a total means. The detailed records
        themselves only cover the retained window, and the ``window`` key
        says so explicitly rather than leaving a reader to assume the log
        still holds every call.
        """
        aggregate = self._usage_totals()
        tools = aggregate.tools
        rows: list[dict[str, Any]] = []
        for name, entry in tools.items():
            calls = entry["calls"]
            rows.append(
                {
                    "tool": name,
                    "calls": calls,
                    "errors": entry["errors"],
                    "avg_ms": int(entry["total_ms"] / calls) if calls else 0,
                    "providers": sorted(entry["providers"]),
                    "last_used": entry["last_used"],
                }
            )
        # Include registered tools that have never been called so the table
        # reflects the full catalog rather than only what has run so far.
        for name in self._tool_names:
            if name not in tools:
                rows.append(
                    {"tool": name, "calls": 0, "errors": 0, "avg_ms": 0, "providers": [], "last_used": ""}
                )
        rows.sort(key=lambda item: (item["calls"], item["tool"]), reverse=True)
        if limit is not None:
            rows = rows[:limit]
        return {
            "total_calls": aggregate.total_calls,
            "total_errors": aggregate.total_errors,
            "tool_count": len(self._tool_names),
            "window": self._usage_window(aggregate),
            "tools": rows,
        }

    @staticmethod
    def _usage_window(aggregate: _UsageAggregate) -> dict[str, Any]:
        """Describe what the counts above cover.

        Retention means the log no longer holds every call, so the summary
        has to say which part of it is still backed by detailed records and
        which part survives only as a rolled-up counter. ``scope`` stays
        ``"lifetime"`` because the sidecar preserves the dropped records'
        counters; without that rollup these numbers would silently become
        "since the last trim".
        """
        retained = aggregate.retained_records
        rolled_up = aggregate.rolled_up_calls
        if rolled_up:
            since = aggregate.retained_since or "the last trim"
            label = (
                f"Lifetime totals. Detailed records cover the newest {retained} "
                f"calls (since {since}); {rolled_up} older calls are counted from "
                "the rolled-up totals only."
            )
        else:
            label = (
                f"Lifetime totals. All {retained} recorded calls are still "
                "retained as detailed records."
            )
        return {
            "scope": "lifetime",
            "label": label,
            "retained_records": retained,
            "retained_since": aggregate.retained_since,
            "rolled_up_calls": rolled_up,
            "max_records": TELEMETRY_KEEP_LINES,
            "rotated_at": aggregate.rotated_at,
        }

    def _telemetry_fingerprint(self) -> tuple[Any, ...]:
        """Identify the on-disk telemetry state.

        Settings polls the usage endpoint, so without a change marker every
        poll reparsed the whole retained window. Size and mtime move on every
        append and on every trim, which is all the cache needs to know.
        """
        marks: list[Any] = []
        for path in (self._telemetry_totals_path, self._telemetry_path):
            try:
                info = path.stat()
            except OSError:
                marks.append(None)
            else:
                marks.append((info.st_mtime_ns, info.st_size))
        return tuple(marks)

    def _usage_totals(self) -> _UsageAggregate:
        fingerprint = self._telemetry_fingerprint()
        with self._usage_lock:
            cached = self._usage_cache
            if cached is not None and cached[0] == fingerprint:
                return cached[1]
        tools, rotated_at = self._load_telemetry_totals()
        rolled_up_calls = sum(_as_int(entry["calls"]) for entry in tools.values())
        retained_records = 0
        retained_since = ""
        try:
            with self._telemetry_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    timestamp = _fold_telemetry_line(tools, line)
                    if timestamp is None:
                        continue
                    retained_records += 1
                    if timestamp and (not retained_since or timestamp < retained_since):
                        retained_since = timestamp
        except OSError:
            pass
        result = _UsageAggregate(
            tools=tools,
            total_calls=sum(_as_int(entry["calls"]) for entry in tools.values()),
            total_errors=sum(_as_int(entry["errors"]) for entry in tools.values()),
            retained_records=retained_records,
            retained_since=retained_since,
            rolled_up_calls=rolled_up_calls,
            rotated_at=rotated_at,
        )
        with self._usage_lock:
            self._usage_cache = (fingerprint, result)
        return result

    def _load_telemetry_totals(self) -> tuple[dict[str, dict[str, Any]], str]:
        """Read the counters for records the size guard already dropped.

        Returns them with the timestamp of the trim that wrote them, which
        the usage window reports so a reader can tell a never-trimmed log
        from one whose detail starts at a rotation.
        """
        try:
            raw = json.loads(self._telemetry_totals_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}, ""
        stored = raw.get("tools") if isinstance(raw, dict) else None
        rotated_at = str(raw.get("rotated_at") or "") if isinstance(raw, dict) else ""
        if not isinstance(stored, dict):
            return {}, rotated_at
        tools: dict[str, dict[str, Any]] = {}
        for name, entry in stored.items():
            if not isinstance(entry, dict):
                continue
            providers = entry.get("providers")
            tools[str(name)] = {
                "calls": _as_int(entry.get("calls")),
                "errors": _as_int(entry.get("errors")),
                "total_ms": _as_int(entry.get("total_ms")),
                "providers": {str(item) for item in providers if item} if isinstance(providers, list) else set(),
                "last_used": str(entry.get("last_used") or ""),
            }
        return tools, rotated_at

    def _trim_telemetry_if_large(self) -> None:
        """Roll the oldest records into the totals sidecar and drop them."""
        try:
            if self._telemetry_path.stat().st_size < TELEMETRY_MAX_BYTES:
                return
        except OSError:
            return
        try:
            with self._telemetry_path.open("r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
            if len(lines) <= TELEMETRY_KEEP_LINES:
                return
            dropped = lines[:-TELEMETRY_KEEP_LINES]
            kept = lines[-TELEMETRY_KEEP_LINES:]
            totals, _previous_rotation = self._load_telemetry_totals()
            for line in dropped:
                _fold_telemetry_line(totals, line)
            payload = {
                "rotated_at": datetime.now(UTC).isoformat(),
                "tools": {
                    name: {
                        "calls": entry["calls"],
                        "errors": entry["errors"],
                        "total_ms": entry["total_ms"],
                        "providers": sorted(entry["providers"]),
                        "last_used": entry["last_used"],
                    }
                    for name, entry in totals.items()
                }
            }
            # Rename the sidecar into place before truncating. The pair is not
            # atomic, so a crash between them double-counts the dropped
            # records; truncating first would instead lose those counts for
            # good, and an inflated total is the recoverable half of that
            # trade.
            staged = self._telemetry_totals_path.with_name(
                self._telemetry_totals_path.name + ".tmp"
            )
            staged.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            staged.replace(self._telemetry_totals_path)
            self._telemetry_path.write_text("".join(kept), encoding="utf-8")
        except OSError:
            logger.debug("Failed to trim the MCP telemetry log", exc_info=True)

    def _principal(self) -> McpPrincipal:
        access = get_access_token()
        if access is None or not isinstance(access.claims, dict):
            raise ControlPlaneError("unauthorized", "A managed Ciaobot MCP session is required.")
        principal = McpPrincipal.from_claims(access.claims)
        if not principal.token_id:
            raise ControlPlaneError("unauthorized", "The MCP session has no principal.")
        return principal

    async def _invoke(
        self,
        name: str,
        operation: Callable[[CiaoControlPlane, McpPrincipal], Any],
        *,
        mutating: bool = False,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        principal: McpPrincipal | None = None
        status = "ok"
        error_code = ""
        value: Any = None
        try:
            if self.control_plane is None:
                raise ControlPlaneError("unavailable", "Ciaobot control plane is not ready.", retryable=True)
            principal = self._principal()
            if mutating and self.control_plane.chat_mode(principal) == "plan":
                raise ControlPlaneError("plan_mode_read_only", "Mutating Ciaobot tools are disabled in plan mode.")
            value = operation(self.control_plane, principal)
            if inspect.isawaitable(value):
                value = await value
            if isinstance(value, dict):
                return value
            return {"ok": True, "data": value}
        except ControlPlaneError as exc:
            status = "error"
            error_code = exc.code
            self._last_error = str(exc)
            return {"ok": False, "error": exc.payload()}
        except (ValueError, KeyError, LookupError) as exc:
            status = "error"
            error_code = "invalid_request"
            self._last_error = str(exc)
            return {
                "ok": False,
                "error": {"code": error_code, "message": str(exc), "retryable": False},
            }
        except Exception as exc:  # noqa: BLE001 - tool boundary must be fail-safe
            status = "error"
            error_code = "internal_error"
            self._last_error = str(exc)
            logger.exception("Ciaobot MCP tool %s failed internally", name)
            return {
                "ok": False,
                "error": {
                    "code": error_code,
                    "message": "Ciaobot could not complete the operation.",
                    "retryable": True,
                },
            }
        finally:
            self._record_tool_call(
                name=name,
                principal=principal,
                status=status,
                error_code=error_code,
                duration_ms=int((time.perf_counter() - started) * 1000),
                value=value,
            )

    def _record_tool_call(
        self,
        *,
        name: str,
        principal: McpPrincipal | None,
        status: str,
        error_code: str,
        duration_ms: int,
        value: Any = None,
    ) -> None:
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "surface": _SURFACE_VAR.get(),
            "tool": name,
            "token_id": principal.token_id if principal else "",
            "chat_id": principal.chat_id if principal else "",
            "provider": principal.provider if principal else "",
            "status": status,
            "error_code": error_code,
            "duration_ms": duration_ms,
        }
        if name == "vault_search" and isinstance(value, dict):
            data = value.get("data")
            if isinstance(data, list):
                paths = [
                    str(row.get("path"))
                    for row in data
                    if isinstance(row, dict) and row.get("path")
                ]
                record["result_count"] = len(paths)
                record["result_paths"] = paths[:50]
        # Telemetry is strictly best-effort: a full disk, a read-only
        # runtime directory, or a value that will not serialise must not
        # turn a successful tool call into a failed one.
        try:
            self._telemetry_path.parent.mkdir(parents=True, exist_ok=True)
            self._trim_telemetry_if_large()
            with self._telemetry_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        except (OSError, TypeError, ValueError):
            logger.debug("Failed to record MCP tool telemetry", exc_info=True)

    def _register_tools(self) -> None:
        """Register exactly the operations in ``MCP_EXPOSED_OPERATIONS``.

        The operation bodies, annotations, and docstrings live in the
        module-level :data:`OPERATIONS` table (shared with the agent
        dispatcher) rather than here as closures. Each entry is bound to this
        service and registered with the same ``Tool.from_function`` metadata
        FastMCP builds for any tool, so the MCP schema and the dispatcher's
        pydantic validation are identical by construction.
        """
        for name in sorted(MCP_EXPOSED_OPERATIONS):
            operation = OPERATIONS_BY_NAME.get(name)
            if operation is None:
                raise ValueError(
                    f"MCP_EXPOSED_OPERATIONS lists unknown operation '{name}'."
                )
            self._tool_names.add(name)
            self.server.add_tool(
                operation.bind(self),
                name=name,
                annotations=operation.annotations,
                description=operation.description,
                structured_output=True,
            )


def _write_mcp_env_values(path: Path, updates: dict[str, str]) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            out.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key not in remaining:
            out.append(line)
            continue
        value = remaining.pop(key).strip()
        if value:
            out.append(f"{key}={value}")
    for key, value in remaining.items():
        value = value.strip()
        if value:
            out.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
