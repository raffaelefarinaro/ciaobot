"""Provider-neutral application control plane for PWA, MCP, and CLI adapters.

The existing managers remain the owners of Ciaobot state and invariants.  This
module supplies a small, typed boundary around them so an agent-facing
transport never needs a browser cookie, a localhost curl command, or direct
knowledge of ``.runtime`` JSON layouts.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ciao import vault_index
from ciao.background import BackgroundRun, BackgroundRunError, TAIL_LINES
from ciao.fts_search import (
    get_db_path,
    index_vault,
    init_db,
    search_vault,
    vault_key_prefix,
)
from ciao.memory_tool import memory_status as memory_status_payload
from ciao.memory_tool import resolve_region, update_region
from ciao.web.project_chats import UnknownModelError
from ciao.schedules import (
    DEFAULT_INTERVAL_MINUTES,
    FREQUENCIES,
    INTERVAL_FREQUENCY,
    ScheduleEntry,
    compute_next_run,
    is_interval,
    normalize_interval_minutes,
    publish_automations_changed,
    stamp_fallback_project,
    wall_clock_time_error,
)

logger = logging.getLogger(__name__)

# MCP needs to distinguish an omitted optional field from an explicit JSON
# null. ``None`` is a meaningful reset for workspace denylist settings, so a
# normal ``= None`` default loses information before the control plane sees
# it.
_UNSET = object()

# A GWS health reading older than this is treated as stale: the monitor
# preserves prior state when probes are unavailable or checks are disabled, so
# a cached "valid" reading can outlive the token it described. Mirrors the
# default CIAO_GWS_HEALTH_INTERVAL (900s) plus a small grace period.
_GWS_HEALTH_STALE_AFTER = 1200.0

@dataclass(frozen=True, slots=True)
class McpPrincipal:
    """Identity and scope attached to one managed provider process."""

    token_id: str
    chat_id: str
    project_id: str
    workspace: str
    provider: str
    # Only ``chat`` is ever issued. The type used to also allow ``automation``,
    # which nothing minted and nothing branched on, and a third value
    # ``handoff`` was smuggled past this annotation by a cast so a gate could
    # test for it. That gate never fired, because the sole issuing call site
    # hardcodes ``chat``; the handoff primitive has since been deleted
    # entirely. Keeping this a one-value Literal means any future restricted
    # role has to change the issuing path to type-check, instead of adding a
    # check that silently never runs.
    role: Literal["chat"] = "chat"

    def to_claims(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_claims(cls, claims: dict[str, Any]) -> "McpPrincipal":
        """Rebuild a principal from token claims.

        Anything other than ``chat`` in the claim is normalised away rather
        than trusted: a token is the one input here that does not come from
        our own call sites, so an unrecognised role must not become a
        privilege the rest of the code then reasons about.
        """
        return cls(
            token_id=str(claims.get("token_id") or ""),
            chat_id=str(claims.get("chat_id") or ""),
            project_id=str(claims.get("project_id") or ""),
            workspace=str(claims.get("workspace") or ""),
            provider=str(claims.get("provider") or ""),
        )


# Permission modes ordered weakest to strongest, so a child chat's requested
# mode can be compared against its ceiling instead of being overwritten by it.
# ``plan`` is read-only, ``normal`` asks before acting, ``auto`` acts with safer
# defaults, ``bypass`` skips approvals. Mirrors ``BridgeMode`` in ciao.models
# and the SDK mapping in ciao.providers.claude.
_MODE_RANK: dict[str, int] = {"plan": 0, "normal": 1, "auto": 2, "bypass": 3}


class ControlPlaneError(ValueError):
    """Stable application error returned by MCP adapters."""

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable

    def payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
        }


def _ok(data: Any = None, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": True}
    if data is not None:
        payload["data"] = data
    payload.update(extra)
    return payload


class CiaoControlPlane:
    """Application operations shared by agent-facing transports."""

    def __init__(
        self,
        config: Any,
        *,
        project_chat_manager: Any,
        schedule_manager: Any,
        app_settings: Any | None = None,
        startup_tracker: Any | None = None,
        connection_tracker: Any | None = None,
        background_runner: Any | None = None,
    ) -> None:
        self.config = config
        self.pcm = project_chat_manager
        self.schedules = schedule_manager
        self.app_settings = app_settings
        self.startup_tracker = startup_tracker
        self._deferred_actions: dict[str, dict[str, Any]] = {}
        # Optional: the app-wide ConnectionTracker, used by file_surface to
        # report real connected clients instead of a per-turn stream proxy.
        self.connection_tracker = connection_tracker
        # Optional: the BackgroundRunner backing the background_run_* tools.
        # Unset on legacy-only instances and in most tests.
        self.background = background_runner

    def _defer_until_chat_idle(
        self,
        principal: McpPrincipal,
        action: str,
        operation: Callable[[], Any],
    ) -> dict[str, Any]:
        """Return before applying a mutation that would tear down its own tool caller."""
        action_id = f"action-{uuid.uuid4().hex[:8]}"
        record = {
            "action_id": action_id,
            "action": action,
            "chat_id": principal.chat_id,
            "token_id": principal.token_id,
            "status": "queued",
            "requested_at": datetime.now(UTC).isoformat(),
            "completed_at": "",
            "error": "",
        }
        self._deferred_actions[action_id] = record

        async def _run() -> None:
            try:
                while principal.chat_id in self.pcm.active_chat_ids():
                    await asyncio.sleep(0.25)
                record["status"] = "running"
                value = operation()
                if hasattr(value, "__await__"):
                    value = await value
                if hasattr(value, "to_dict"):
                    value = value.to_dict()
                record["result"] = value
                record["status"] = "completed"
            except Exception as exc:  # noqa: BLE001 - deferred boundary is fail-safe
                record["status"] = "failed"
                record["error"] = str(exc)
            finally:
                record["completed_at"] = datetime.now(UTC).isoformat()

        asyncio.create_task(_run(), name=action_id)
        return _ok({
            "deferred": True,
            **{key: value for key, value in record.items() if key != "token_id"},
        })

    # ---- scope ---------------------------------------------------------

    def _workspace(self, principal: McpPrincipal, requested: str = "") -> str:
        workspace = requested.strip() or principal.workspace
        if not workspace:
            raise ControlPlaneError("workspace_required", "No active workspace is available.")
        if workspace != principal.workspace:
            raise ControlPlaneError(
                "workspace_forbidden",
                f"This provider process is scoped to workspace '{principal.workspace}'.",
            )
        if self.config.workspace(workspace) is None:
            raise ControlPlaneError("workspace_not_found", f"Workspace '{workspace}' was not found.")
        return workspace

    def _project(self, principal: McpPrincipal, project_id: str) -> Any:
        project = self.pcm.get_project(project_id)
        if project is None:
            raise ControlPlaneError("project_not_found", f"Project '{project_id}' was not found.")
        self._workspace(principal, project.workspace)
        return project

    def _resolve_project_id(self, principal: McpPrincipal, ref: str) -> str:
        """Resolve a non-empty project id-or-case-insensitive-name to an exact id.

        Shared by any tool that accepts a project reference, so a caller never
        has to pre-resolve an id via ``projects_list`` for the common case."""
        exact = self.pcm.get_project(ref)
        if exact is not None:
            exact_id: str = exact.project_id
            return exact_id
        matches = [
            p for p in self.pcm.list_projects(principal.workspace)
            if p.name.casefold() == ref.casefold()
        ]
        if len(matches) == 1:
            match_id: str = matches[0].project_id
            return match_id
        if len(matches) > 1:
            raise ControlPlaneError(
                "project_ambiguous",
                f"'{ref}' matches more than one project; use its exact id instead.",
            )
        raise ControlPlaneError("project_not_found", f"Project '{ref}' was not found.")

    def _resolve_project(self, principal: McpPrincipal, ref: str | None) -> Any:
        """Resolve a project by exact id, case-insensitive name, or the
        caller's current project when ``ref`` is omitted or self-referential."""
        value = (ref or "").strip()
        if not value or value.lower() in {"this", "this project", "current", "self"}:
            if not principal.project_id:
                raise ControlPlaneError(
                    "project_required",
                    "No project given and no active project to default to; pass a project id or name.",
                )
            return self._project(principal, principal.project_id)
        return self._project(principal, self._resolve_project_id(principal, value))

    def _resolve_chat_id(self, principal: McpPrincipal, ref: str | None) -> str:
        """Resolve a chat ID, defaulting to principal.chat_id when ref is omitted,
        empty, or self-referential ('this', 'this chat', 'current', 'self')."""
        value = (ref or "").strip()
        if not value or value.lower() in {"this", "this chat", "current", "self"}:
            if not principal.chat_id:
                raise ControlPlaneError(
                    "chat_required",
                    "No chat ID given and no active chat to default to.",
                )
            return principal.chat_id
        return value

    def _resolve_project_in_workspace(
        self, principal: McpPrincipal, ref: str, workspace: str
    ) -> Any:
        """Resolve a project reference against one explicit workspace.

        Like ``_resolve_project`` but both the name lookup and the ownership
        check target ``workspace`` instead of the caller's own (used by
        ``schedule_update`` to resolve a project inside its settled
        destination workspace).
        """
        exact = self.pcm.get_project(ref)
        if exact is not None:
            if exact.workspace != workspace:
                raise ControlPlaneError(
                    "workspace_mismatch",
                    f"Project '{ref}' lives in workspace '{exact.workspace}', not '{workspace}'.",
                )
            return exact
        matches = [
            p for p in self.pcm.list_projects(workspace)
            if p.name.casefold() == ref.casefold()
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ControlPlaneError(
                "project_ambiguous",
                f"'{ref}' matches more than one project; use its exact id instead.",
            )
        raise ControlPlaneError("project_not_found", f"Project '{ref}' was not found.")

    def _chat_scope(self, principal: McpPrincipal, chat_id: str | None = None) -> tuple[Any, Any]:
        """Resolve a chat plus the project that owns it, in one authorization pass.

        Callers that need the workspace should take the project from here rather
        than looking it up again: ``_chat`` already resolves and authorizes it.
        """
        resolved_id = self._resolve_chat_id(principal, chat_id)
        chat = self.pcm.get_chat(resolved_id)
        if chat is None:
            raise ControlPlaneError("chat_not_found", f"Chat '{resolved_id}' was not found.")
        return chat, self._project(principal, chat.project_id)

    def _chat(self, principal: McpPrincipal, chat_id: str | None = None) -> Any:
        return self._chat_scope(principal, chat_id)[0]

    def _chat_id(self, principal: McpPrincipal, chat_id: str | None = None) -> str:
        """Authorize a chat reference and return its real id.

        ``_chat`` resolves ``""``/``"this"``/``"self"`` internally but returns
        only the chat, so callers that echo the id back had to remember a second
        line to recover it. Use this when the chat object itself is not needed.
        """
        return str(self._chat(principal, chat_id).chat_id)

    def chat_mode(self, principal: McpPrincipal) -> str:
        chat = self.pcm.get_chat(principal.chat_id) if principal.chat_id else None
        return str(getattr(chat, "mode", "auto") or "auto")

    def _child_mode(self, principal: McpPrincipal, requested: str | None) -> str:
        """Hold an MCP-created child at or below its caller's permission ceiling.

        The child starts its first turn immediately, so accepting a *stronger*
        mode from model-authored tool arguments would let a normal/auto chat
        manufacture a bypass session without an operator approval. The provider
        enforces the returned mode as its session permission rules; keeping the
        clamp here also covers provider child chats.

        A ceiling, not a pin. This used to return ``parent_mode`` outright and
        ignore ``requested``, which blocked *de-escalation* as well as
        escalation: from a ``bypass`` chat, ``chat_update(chat_id=<other>,
        mode="normal")`` raised the target to ``bypass`` instead of clamping it,
        and in an ``auto`` chat a ``mode="plan"`` downgrade was written back as
        ``auto`` while the response still reported success — a requested
        restriction silently discarded. A weaker request is always honoured;
        only an upward one is clamped.
        """
        parent_mode = self.chat_mode(principal)
        if parent_mode not in _MODE_RANK:
            parent_mode = "normal"
        # An unrecognised request carries no rank to compare, so it cannot be
        # honoured safely; fall back to the ceiling rather than guessing.
        if not requested or requested not in _MODE_RANK:
            return parent_mode
        if _MODE_RANK[requested] <= _MODE_RANK[parent_mode]:
            return requested
        logger.warning(
            "Clamping child chat mode %r to ceiling %r for %s",
            requested,
            parent_mode,
            principal.chat_id or "unscoped MCP session",
        )
        return parent_mode

    def _vault_root(self, principal: McpPrincipal) -> Path:
        workspace = self._workspace(principal)
        resolver = getattr(self.pcm, "_workspace_vault_root", None)
        if callable(resolver):
            return Path(resolver(workspace)).resolve()
        return Path(self.config.vault_root).resolve()

    @staticmethod
    def _safe_relative(root: Path, relative_path: str, *, must_exist: bool = False) -> Path:
        raw = Path(relative_path)
        if raw.is_absolute() or "\x00" in relative_path:
            raise ControlPlaneError("invalid_path", "Use a relative path inside the active root.")
        target = (root / raw).resolve()
        if not target.is_relative_to(root.resolve()):
            raise ControlPlaneError("path_forbidden", "The path resolves outside the active root.")
        if must_exist and not target.exists():
            raise ControlPlaneError("file_not_found", f"'{relative_path}' was not found.")
        return target

    # ---- context/status -----------------------------------------------

    def context_get(self, principal: McpPrincipal) -> dict[str, Any]:
        chat = self.pcm.get_chat(principal.chat_id) if principal.chat_id else None
        project = self.pcm.get_project(principal.project_id) if principal.project_id else None
        return _ok({
            "workspace": principal.workspace,
            "project": project.to_dict() if project else None,
            "chat": chat.to_dict(local=self.pcm.is_session_local(chat)) if chat else None,
            "provider": principal.provider,
            "role": principal.role,
        })

    def system_status_get(self, principal: McpPrincipal) -> dict[str, Any]:
        self._workspace(principal)
        return _ok({
            "version": __import__("ciao").__version__,
            "workspace_root": str(self.config.workspace_root),
            "vault_root": str(self._vault_root(principal)),
            "active_chat_ids": self.pcm.active_chat_ids(),
            "startup": self.startup_tracker.to_dict() if self.startup_tracker else None,
        })

    def gws_status(self, principal: McpPrincipal) -> dict[str, Any]:
        """Report Google Workspace connection status for the active workspace.

        Resolves the workspace's linked ``gws_profile`` the same way the runtime
        does (the operator-level default only counts when it names an account
        that actually exists), then reports whether credentials are present and
        whether the periodic health monitor's last reading says the token is
        valid. Read-only and cheap: it never runs ``gws auth status`` itself, so
        the agent can answer "is Google connected?" without a subprocess.
        """
        workspace = self._workspace(principal)
        from ciao import gws_auth

        profile = str(
            getattr(self.config.workspace(workspace), "gws_profile", "") or ""
        ).strip()
        if not profile:
            default = str(getattr(self.config, "gws_default_profile", "") or "").strip()
            profile = default if default in gws_auth.known_profiles(self.config) else ""
        if not profile:
            return _ok(
                {
                    "profile": "",
                    "configured": False,
                    "connected": False,
                    "needs_relogin": False,
                }
            )

        config_dir = gws_auth.profile_config_dir(self.config, profile)
        credentials_present = False
        if config_dir is not None:
            credentials_present = any(
                (config_dir / name).is_file()
                for name in ("credentials.json", "credentials.enc")
            )
        health = gws_auth.read_health_cache(
            Path(self.config.state_path).parent
        ).get(profile, {})
        token_valid = (
            bool(health.get("token_valid")) if "token_valid" in health else None
        )
        # The health monitor preserves prior state when a probe is unavailable or
        # checks are disabled (CIAO_GWS_HEALTH_INTERVAL=0), so a cached reading
        # can outlive the token it described. Only a fresh, confirmed valid
        # reading establishes a connection; a stale one is reported as unknown
        # rather than assumed good.
        checked_at = health.get("checked_at")
        fresh = (
            isinstance(checked_at, (int, float))
            and (time.time() - float(checked_at)) <= _GWS_HEALTH_STALE_AFTER
        )
        connected = bool(credentials_present and token_valid is True and fresh)
        # The health monitor debounces a single invalid reading (notify_threshold
        # consecutive invalid runs) before treating a login as dead. Mirror that:
        # only a confirmed invalid state (notified_invalid) surfaces as
        # needs_relogin, so a transient reading does not trigger a false
        # re-authentication prompt.
        needs_relogin = bool(
            credentials_present
            and token_valid is False
            and health.get("notified_invalid")
        )
        return _ok(
            {
                "profile": profile,
                "configured": credentials_present,
                "connected": connected,
                "token_valid": token_valid,
                "needs_relogin": needs_relogin,
                "token_error": str(health.get("token_error") or ""),
                "checked_at": checked_at,
                "stale": bool(credentials_present and not fresh),
            }
        )

    def memory_status(self, principal: McpPrincipal) -> dict[str, Any]:
        """Report native guide memory usage without copying its contents."""
        workspace = self._workspace(principal)
        guide = Path(self.config.agent_root(workspace)) / "CLAUDE.md"
        return _ok(memory_status_payload(
            guide,
            memory_char_limit=int(getattr(self.config, "memory_char_limit", 3000)),
            user_char_limit=int(getattr(self.config, "user_char_limit", 1375)),
        ))

    def memory_update(
        self,
        principal: McpPrincipal,
        region: str,
        *,
        action: Literal["add", "replace", "remove"],
        entry: str = "",
        match: str = "",
    ) -> dict[str, Any]:
        """Apply one bounded edit to the native ``CLAUDE.md`` memory region."""
        workspace = self._workspace(principal)
        if action not in {"add", "replace", "remove"}:
            raise ControlPlaneError("invalid_action", "action must be add, replace, or remove.")
        canonical = resolve_region(region)
        limit = (
            int(getattr(self.config, "memory_char_limit", 3000))
            if canonical == "memory"
            else int(getattr(self.config, "user_char_limit", 1375))
        )
        guide = Path(self.config.agent_root(workspace)) / "CLAUDE.md"
        try:
            result = update_region(
                guide,
                canonical,
                action=action,
                entry=entry,
                match=match,
                char_limit=limit,
            )
        except ValueError as exc:
            raise ControlPlaneError("memory_update_invalid", str(exc)) from exc
        return _ok(result)

    # ---- memory proposals ----------------------------------------------

    # Memory-proposal list/dismiss moved out of the MCP surface to the CLI
    # (`ciao memory-proposals`, `ciao memory-proposal-dismiss`) so the nightly
    # curation agent can review and resolve the queue through one tool instead
    # of a synchronous MCP round-trip.

    # ---- workspaces ----------------------------------------------------

    def workspaces_list(self, principal: McpPrincipal) -> dict[str, Any]:
        """All configured logical workspaces, not just the active one."""
        from ciao.workspaces import workspace_to_dict

        return _ok(
            {"workspaces": [workspace_to_dict(item, self.config) for item in self.config.workspaces.values()]}
        )

    def workspace_create(
        self,
        principal: McpPrincipal,
        *,
        name: str,
        default_provider: str = "claude",
        gws_profile: str = "",
        disallowed_tools: Any = _UNSET,
        color: str = "",
    ) -> dict[str, Any]:
        """Register a new logical workspace under the standard vault folder."""
        from ciao.workspaces import persist_workspaces, workspace_from_request, workspace_to_dict

        data: dict[str, Any] = {
            "name": name,
            "default_provider": default_provider,
            "gws_profile": gws_profile,
        }
        if disallowed_tools is not _UNSET:
            data["disallowed_tools"] = disallowed_tools
        if color:
            data["color"] = color
        workspace = workspace_from_request(data, config=self.config)
        self.config.workspaces[workspace.name] = workspace
        persist_workspaces(self.config)
        self._refresh_workspace_registry()
        return _ok(workspace_to_dict(workspace, self.config))


    def _refresh_workspace_registry(self) -> None:
        """Notify the project-chat manager after a registry mutation."""
        refresh = getattr(self.pcm, "refresh_workspaces", None)
        if callable(refresh):
            refresh()

    # ---- vault ---------------------------------------------------------

    def _entity_index_root(self, principal: McpPrincipal) -> Path:
        """The vault whose INDEX.md covers this chat. See vault_index_refresh."""
        workspace = self._workspace(principal)
        if workspace:
            try:
                return Path(self.config.agent_vault_root(workspace))
            except (AttributeError, ValueError):
                pass
        return Path(self.config.vault_root)

    def _index_stamp(self, principal: McpPrincipal) -> str:
        """Workspace to stamp on scanned entries, or "" to infer from the path.

        Empty before the re-rooting: the shared vault holds every workspace, so
        the first-path-segment inference is what labels them. The workspace name
        afterwards, because a root's vault holds exactly one workspace and its
        first segment is a folder name.
        """
        workspace = self._workspace(principal)
        if not workspace:
            return ""
        try:
            rooted = Path(self.config.agent_vault_root(workspace)) != Path(
                self.config.vault_root
            )
        except (AttributeError, ValueError):
            return ""
        return workspace if rooted else ""

    def _search_key_base(self) -> Path:
        """The install root, which every stored search key is relative to.

        Falls back to the configured vault root's parent, which is the install
        root in both layouts: before the re-rooting the vault sits directly under
        it, and after it ``config.vault_root`` still names that same path even
        though each root now owns its own vault. The fallback exists because
        several call sites build a minimal config stub.
        """
        base = getattr(self.config, "workspace_root", None)
        if base:
            return Path(base)
        return Path(self.config.vault_root).parent

    def vault_search(self, principal: McpPrincipal, query: str, limit: int = 10) -> dict[str, Any]:
        """Search this workspace's notes, and only this workspace's notes.

        Keys are stored relative to the install root so two agent roots holding
        a vault of the same name cannot overwrite each other's rows, and the
        result set is filtered to this vault's prefix. Both halves are needed:
        the isolation used to be a side effect of the index prune deleting every
        other root's rows on each pass, which also meant switching workspace
        re-indexed the whole vault.
        """
        root = self._vault_root(principal)
        base = self._search_key_base()
        db_path = get_db_path()
        conn = sqlite3.connect(db_path)
        try:
            init_db(conn)
            index_vault(conn, root, path_base=base)
            rows = search_vault(
                conn,
                query,
                limit=max(1, min(50, int(limit))),
                path_prefix=vault_key_prefix(root, base),
            )
        finally:
            conn.close()
        return _ok(rows)

    def vault_index_refresh(self, principal: McpPrincipal) -> dict[str, Any]:
        """Rebuild the entity index covering this chat, and its search index.

        The index root is ``agent_vault_root(workspace)``, which is correct in
        both layouts and for the same reason each time: it is the vault whose
        INDEX.md this chat's entity lookup reads. Before the re-rooting that is
        the ONE shared index, holding every workspace's prefixed paths and
        filtered per workspace at read time; after it, this root's own index,
        which needs no prefix because the root holds one vault.

        Deliberately not ``_workspace_vault_root``: before the migration that is
        a subtree of the shared vault, and writing an index there produced one
        whose paths no filter recognised while leaving the real one stale.

        The FTS index stays workspace-scoped: it backs ``vault_search``, whose
        isolation boundary is ``_vault_root(principal)``.
        """
        search_root = self._vault_root(principal)
        index_root = self._entity_index_root(principal)
        entries = vault_index.scan_vault(
            index_root, workspace=self._index_stamp(principal)
        )
        vault_index.write_index_file(entries, index_root / "INDEX.md")
        db_path = get_db_path()
        conn = sqlite3.connect(db_path)
        try:
            init_db(conn)
            indexed, removed = index_vault(
                conn, search_root, path_base=self._search_key_base()
            )
        finally:
            conn.close()
        return _ok({"notes": len(entries), "fts_indexed": indexed, "fts_removed": removed})

    # ---- projects/chats ------------------------------------------------

    def projects_list(self, principal: McpPrincipal, include_completed: bool = False) -> dict[str, Any]:
        workspace = self._workspace(principal)
        data: dict[str, Any] = {
            "active": [item.to_dict() for item in self.pcm.list_projects(workspace)]
        }
        if include_completed:
            data["completed"] = self.pcm.list_completed_projects(workspace)
        return _ok(data)

    def project_get(self, principal: McpPrincipal, project_id: str = "") -> dict[str, Any]:
        project = self._resolve_project(principal, project_id)
        return _ok(project.to_dict())

    def project_create(self, principal: McpPrincipal, name: str, context: str = "") -> dict[str, Any]:
        workspace = self._workspace(principal)
        clean_name = name.strip()
        if not clean_name:
            raise ControlPlaneError("invalid_name", "Project name is required.")
        return _ok(self.pcm.create_project(clean_name, workspace, context).to_dict())

    def project_update(
        self,
        principal: McpPrincipal,
        project_id: str = "",
        *,
        name: str | None = None,
        context: str | None = None,
        vault_folder: str | None = None,
    ) -> dict[str, Any]:
        project = self._resolve_project(principal, project_id)
        item = self.pcm.update_project(
            project.project_id, name=name, context=context, vault_folder=vault_folder
        )
        if item is None:
            raise ControlPlaneError("project_not_found", f"Project '{project.project_id}' was not found.")
        return _ok(item.to_dict())

    def project_complete(self, principal: McpPrincipal, project_id: str = "") -> dict[str, Any]:
        project = self._resolve_project(principal, project_id)
        pid = project.project_id
        current_chat = self.pcm.get_chat(principal.chat_id) if principal.chat_id else None
        if current_chat is not None and current_chat.project_id == pid:
            return self._defer_until_chat_idle(
                principal,
                "project_complete",
                lambda: self.pcm.complete_project(pid),
            )
        return _ok(self.pcm.complete_project(pid))

    def project_restore(self, principal: McpPrincipal, stem: str) -> dict[str, Any]:
        workspace = self._workspace(principal)
        return _ok(self.pcm.restore_project(workspace, stem))

    def project_delete(self, principal: McpPrincipal, project_id: str = "") -> dict[str, Any]:
        project = self._resolve_project(principal, project_id)
        pid = project.project_id
        current_chat = self.pcm.get_chat(principal.chat_id) if principal.chat_id else None
        if current_chat is not None and current_chat.project_id == pid:
            return self._defer_until_chat_idle(
                principal,
                "project_delete",
                lambda: {
                    "deleted": self.pcm.delete_project(pid),
                    "project_id": pid,
                },
            )
        return _ok({"deleted": self.pcm.delete_project(pid), "project_id": pid})

    def chats_list(self, principal: McpPrincipal, project_id: str = "") -> dict[str, Any]:
        if project_id:
            self._project(principal, project_id)
            chats = self.pcm.list_chats(project_id)
        else:
            chats = [
                chat for chat in self.pcm.list_chats()
                if self.pcm.get_project(chat.project_id)
                and self.pcm.get_project(chat.project_id).workspace == principal.workspace
            ]
        return _ok([chat.to_dict(local=self.pcm.is_session_local(chat)) for chat in chats])

    def chat_get(self, principal: McpPrincipal, chat_id: str) -> dict[str, Any]:
        chat = self._chat(principal, chat_id)
        return _ok(chat.to_dict(local=self.pcm.is_session_local(chat)))

    def chat_create(
        self,
        principal: McpPrincipal,
        project_id: str | None = None,
        *,
        title: str = "New Chat",
        provider: str | None = None,
        model: str | None = None,
        mode: str | None = None,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        project = self._resolve_project(principal, project_id)
        requested_mode = mode
        mode = self._child_mode(principal, mode)
        chat = self.pcm.create_chat(
            project.project_id, title=title, provider=provider, model=model, mode=mode
        )
        result = chat.to_dict(local=True)
        if requested_mode and requested_mode != mode:
            result["mode_clamped"] = True
            result["requested_mode"] = requested_mode
        text = (prompt or "").strip()
        if text:
            if self.pcm.queue_message(chat.chat_id, text):
                result["send_status"] = "queued"
            else:
                self.pcm.start_stream(chat.chat_id, text)
                result["send_status"] = "started"
        return _ok(result)

    def chat_update(
        self,
        principal: McpPrincipal,
        chat_id: str,
        *,
        title: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        mode: str | None = None,
        thinking_level: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        chat_id = self._chat_id(principal, chat_id)
        if project_id is not None:
            self._project(principal, project_id)
        requested_mode = mode
        if mode is not None:
            # A normal/auto MCP caller must not upgrade its own or another
            # chat to bypass through the auto-approved metadata tool. Keep the
            # same ceiling used for newly-created child chats.
            mode = self._child_mode(principal, mode)
        updated = self.pcm.update_chat(
            chat_id,
            title=title,
            provider=provider,
            model=model,
            mode=mode,
            thinking_level=thinking_level,
            project_id=project_id,
        )
        if updated is None:
            raise ControlPlaneError("chat_not_found", f"Chat '{chat_id}' was not found.")
        result = updated.to_dict(local=self.pcm.is_session_local(updated))
        if requested_mode and requested_mode != mode:
            result["mode_clamped"] = True
            result["requested_mode"] = requested_mode
        return _ok(result)

    def chat_send(self, principal: McpPrincipal, chat_id: str, prompt: str) -> dict[str, Any]:
        chat = self._chat(principal, chat_id)
        chat_id = chat.chat_id
        if chat.archived:
            raise ControlPlaneError("chat_archived", "Cannot send to an archived chat.")
        text = prompt.strip()
        if not text:
            raise ControlPlaneError("empty_prompt", "Prompt is required.")
        if self.pcm.queue_message(chat_id, text):
            return _ok({"chat_id": chat_id, "status": "queued"})
        self.pcm.start_stream(chat_id, text)
        return _ok({"chat_id": chat_id, "status": "started"})

    def chat_continue(self, principal: McpPrincipal, chat_id: str) -> dict[str, Any]:
        chat = self._chat(principal, chat_id)
        chat = self.pcm.continue_archived_chat(chat.chat_id)
        return _ok(chat.to_dict(local=True))

    def chat_retry(self, principal: McpPrincipal, chat_id: str) -> dict[str, Any]:
        chat_id = self._chat_id(principal, chat_id)
        stream = self.pcm.try_chat_retry_now(chat_id)
        return _ok({"chat_id": chat_id, "status": "started" if stream else "not_pending"})

    def chat_retry_update(
        self,
        principal: McpPrincipal,
        chat_id: str,
        action: Literal["set", "stop", "try_now"],
        prompt: str = "",
    ) -> dict[str, Any]:
        chat_id = self._chat_id(principal, chat_id)
        if action == "set":
            chat = self.pcm.set_chat_retry(chat_id, prompt, image_refs=[], reason="mcp")
            if chat is None:
                raise ControlPlaneError("chat_not_found", f"Chat '{chat_id}' was not found.")
            return _ok(chat.to_dict(local=self.pcm.is_session_local(chat)))
        if action == "stop":
            chat = self.pcm.stop_chat_retry(chat_id)
            if chat is None:
                raise ControlPlaneError("chat_not_found", f"Chat '{chat_id}' was not found.")
            return _ok(chat.to_dict(local=self.pcm.is_session_local(chat)))
        if action == "try_now":
            return self.chat_retry(principal, chat_id)
        raise ControlPlaneError("invalid_action", "action must be set, stop, or try_now.")

    def chat_new_session(self, principal: McpPrincipal, chat_id: str) -> dict[str, Any]:
        chat_id = self._chat_id(principal, chat_id)
        if chat_id == principal.chat_id:
            return self._defer_until_chat_idle(
                principal, "chat_new_session", lambda: self.pcm.new_session(chat_id)
            )
        chat = self.pcm.new_session(chat_id)
        if chat is None:
            raise ControlPlaneError("chat_not_found", f"Chat '{chat_id}' was not found.")
        return _ok(chat.to_dict(local=True))

    def chat_handover(
        self,
        principal: McpPrincipal,
        chat_id: str,
        *,
        provider: str,
        model: str,
        messages: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        chat_id = self._chat_id(principal, chat_id)
        if chat_id == principal.chat_id:
            return self._defer_until_chat_idle(
                principal,
                "chat_handover",
                lambda: self.pcm.handover_chat(
                    chat_id,
                    provider=provider.strip(),
                    model=model.strip(),
                    messages=[row for row in (messages or []) if isinstance(row, dict)],
                ),
            )
        chat = self.pcm.handover_chat(
            chat_id,
            provider=provider.strip(),
            model=model.strip(),
            messages=[row for row in (messages or []) if isinstance(row, dict)],
        )
        if chat is None:
            raise ControlPlaneError("chat_not_found", f"Chat '{chat_id}' was not found.")
        return _ok(chat.to_dict(local=self.pcm.is_session_local(chat)))

    def chat_fork(
        self,
        principal: McpPrincipal,
        chat_id: str,
        *,
        messages: list[dict[str, Any]],
        turn_index: int,
    ) -> dict[str, Any]:
        chat_id = self._chat_id(principal, chat_id)
        if turn_index < 0:
            raise ControlPlaneError("invalid_turn", "turn_index must be non-negative.")
        fork = self.pcm.fork_chat(
            chat_id,
            messages=[row for row in messages if isinstance(row, dict)],
            turn_index=turn_index,
        )
        return _ok(fork.to_dict(local=True))

    async def chat_archive(self, principal: McpPrincipal, chat_id: str = "") -> dict[str, Any]:
        target_id = chat_id.strip()
        if not target_id or target_id.lower() in {"this", "this chat", "current", "self"}:
            target_id = principal.chat_id
        chat = self._chat(principal, target_id)
        project = self._project(principal, chat.project_id)

        async def _archive() -> dict[str, Any]:
            outcome = await self.pcm.archive_chat(target_id)
            if outcome is not None:
                self.pcm.run_archive_postprocess(target_id, outcome, chat, project)
            return {
                "chat_id": target_id,
                "archived_to": str(outcome.path) if outcome else None,
            }

        if target_id == principal.chat_id:
            # Archiving the calling chat tears down its own tool caller, so it
            # still waits for this turn to finish before anything happens.
            return self._defer_until_chat_idle(principal, "chat_archive", _archive)
        return _ok(await _archive())

    def chat_delete(self, principal: McpPrincipal, chat_id: str) -> dict[str, Any]:
        chat_id = self._chat_id(principal, chat_id)
        if chat_id == principal.chat_id:
            return self._defer_until_chat_idle(
                principal,
                "chat_delete",
                lambda: {"chat_id": chat_id, "deleted": self.pcm.delete_chat(chat_id)},
            )
        return _ok({"chat_id": chat_id, "deleted": self.pcm.delete_chat(chat_id)})

    async def chat_stop(self, principal: McpPrincipal, chat_id: str) -> dict[str, Any]:
        chat_id = self._chat_id(principal, chat_id)
        if chat_id == principal.chat_id:
            raise ControlPlaneError(
                "self_stop_forbidden",
                "The current turn cannot stop itself through MCP; use the PWA stop control.",
            )
        return _ok({"chat_id": chat_id, "stopped": await self.pcm.stop_chat(chat_id)})

    # ---- background command runs ----------------------------------------

    def _background_runner(self) -> Any:
        if self.background is None:
            raise ControlPlaneError(
                "unavailable",
                "Background command runs are not available on this server.",
                retryable=True,
            )
        return self.background

    def _background_payload(
        self, run: BackgroundRun, *, tail: list[str] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "run_id": run.run_id,
            "label": run.label,
            "status": run.status,
            "exit_code": run.exit_code,
            "pid": run.pid,
            "cmd": list(run.cmd),
            "cwd": run.cwd,
            "timeout_s": run.timeout_s,
            "started_at": run.started_at,
            "ended_at": run.ended_at,
            "error": run.error,
            "log_path": str(self._background_runner().log_path(run.run_id)),
        }
        if tail is not None:
            payload["last_lines"] = tail
        return payload

    def _owned_run(self, principal: McpPrincipal, run_id: str) -> BackgroundRun:
        """Resolve a run the calling chat owns, or raise ``run_not_found``.

        A run belongs to exactly the chat that started it. A run owned by
        another chat reports as *not found* rather than *forbidden*: the run id
        is otherwise an existence oracle for work in chats the caller cannot
        see. The workspace check is defence in depth for a stale record whose
        chat has since moved.
        """
        value = (run_id or "").strip()
        if not value:
            raise ControlPlaneError("run_required", "run_id is required.")
        run: BackgroundRun | None = self._background_runner().get(value)
        if (
            run is None
            or run.parent_chat_id != principal.chat_id
            or (run.workspace and run.workspace != principal.workspace)
        ):
            raise ControlPlaneError("run_not_found", f"Run '{value}' was not found.")
        return run

    async def background_run_start(
        self,
        principal: McpPrincipal,
        *,
        cmd: Any,
        cwd: str = "",
        env: dict[str, Any] | None = None,
        timeout_s: int = 1800,
        label: str = "",
    ) -> dict[str, Any]:
        """Start a command in a tracked background subprocess.

        The run is attributed to the calling chat and inherits its workspace,
        so ``background_run_status``/``_cancel`` from any other chat cannot see
        or touch it. Nothing here builds a shell command line: ``cmd`` is an
        argv list handed to ``create_subprocess_exec``.
        """
        chat, project = self._chat_scope(principal, "")
        workspace = self._workspace(principal, project.workspace)
        try:
            run: BackgroundRun = await self._background_runner().start_run(
                parent_chat_id=chat.chat_id,
                project_id=project.project_id,
                workspace=workspace,
                cmd=cmd,
                cwd=cwd,
                env=env,
                timeout_s=timeout_s,
                label=label,
            )
        except BackgroundRunError as exc:
            raise ControlPlaneError(exc.code, str(exc)) from exc
        return _ok(self._background_payload(run))

    def background_run_status(
        self, principal: McpPrincipal, run_id: str, lines: int = TAIL_LINES
    ) -> dict[str, Any]:
        run = self._owned_run(principal, run_id)
        tail = self._background_runner().tail(run.run_id, max(1, min(int(lines), 500)))
        return _ok(self._background_payload(run, tail=tail))

    async def background_run_cancel(
        self, principal: McpPrincipal, run_id: str
    ) -> dict[str, Any]:
        run = self._owned_run(principal, run_id)
        try:
            updated: BackgroundRun = await self._background_runner().cancel(run.run_id)
        except BackgroundRunError as exc:
            raise ControlPlaneError(exc.code, str(exc)) from exc
        return _ok(
            self._background_payload(
                updated, tail=self._background_runner().tail(updated.run_id)
            )
        )

    # ---- schedules/loops ----------------------------------------------

    def _schedule_payload(self, entry: ScheduleEntry) -> dict[str, Any]:
        data = asdict(entry)
        next_run = compute_next_run(entry)
        data["next_run"] = next_run.isoformat() if next_run else None
        # Name the target project here so every schedule tool reports it the
        # same way; enriching at the call site left it missing from list/update.
        if entry.web_project_id:
            project = self.pcm.get_project(entry.web_project_id)
            data["project_name"] = getattr(project, "name", "") or ""
        return data

    def schedules_list(self, principal: McpPrincipal) -> dict[str, Any]:
        workspace = self._workspace(principal)
        rows = [
            self._schedule_payload(entry)
            for entry in self.schedules.list_entries()
            if not entry.workspace or entry.workspace == workspace
        ]
        return _ok(rows)

    def schedule_preview(self, principal: McpPrincipal, **values: Any) -> dict[str, Any]:
        """Validate schedule fields and resolve workspace/project targets.

        When ``chat_id`` is omitted, ``project_id`` defaults to the caller's
        active project (same as ``chat_create``) so MCP-created schedules land
        in the chat's workspace+project instead of an unscoped Personal fallback.

        An explicit ``workspace`` may only restate the principal's own. Unlike a
        chat turn, a schedule is auto-approved model input that persists a
        prompt and runs unattended — and unattended dispatch forces the target
        chat into bypass — so honoring a foreign workspace would let an
        injected or compromised managed chat execute there with that
        workspace's guide, integrations, and filesystem authority, without
        operator approval. Giving a second workspace an automation is operator
        business, done from a chat scoped to it.
        """
        # One boundary for explicit and omitted targets alike: ``_workspace``
        # refuses any name other than the principal's own and still validates
        # that the scoped workspace exists.
        workspace = self._workspace(principal, str(values.get("workspace") or ""))

        chat_ref = values.get("chat_id")
        project_ref = values.get("project_id")
        web_chat_id: str | None = None
        project: Any | None = None

        if chat_ref:
            chat = self._chat(principal, str(chat_ref))
            web_chat_id = chat.chat_id

        if project_ref:
            project = self._resolve_project(principal, str(project_ref))
        elif not web_chat_id:
            # Inherit the active project when omitted — preferred for vault-aware
            # automation and keeps the schedule in the same workspace as this chat.
            project = self._resolve_project(principal, None)

        web_project_id: str | None = None
        if project is not None:
            web_project_id = project.project_id
            # Re-stamp from the resolved project (same as the HTTP route).
            workspace = self._workspace(principal, project.workspace)

        frequency = str(values.get("frequency") or "weekly")
        if frequency not in FREQUENCIES:
            raise ControlPlaneError(
                "invalid_frequency",
                f"frequency must be one of {', '.join(sorted(FREQUENCIES))}.",
            )
        # Interval cadence needs minutes, not a time of day.
        interval_minutes = 0
        if frequency == INTERVAL_FREQUENCY:
            # `or DEFAULT` would rescue a rejected 0 into a valid cadence, so
            # only an absent value falls back.
            supplied_interval = values.get("interval_minutes")
            if supplied_interval is None:
                supplied_interval = DEFAULT_INTERVAL_MINUTES
            try:
                interval_minutes = normalize_interval_minutes(supplied_interval)
            except ValueError as exc:
                raise ControlPlaneError("invalid_interval", str(exc)) from exc

        now = datetime.now(UTC).isoformat(timespec="seconds")
        entry = ScheduleEntry(
            schedule_id="preview",
            daily_time_utc=str(values.get("daily_time") or values.get("daily_time_utc") or "09:00"),
            prompt=str(values.get("prompt") or "preview"),
            chat_id=0,
            created_at=now,
            model=str(values.get("model") or ""),
            provider=str(values.get("provider") or ""),
            mode=str(values.get("mode") or "auto"),  # type: ignore[arg-type]
            timezone_name=str(values.get("timezone") or values.get("timezone_name") or "UTC"),
            days_of_week=list(values.get("days_of_week") or []),
            frequency=frequency,
            interval_minutes=interval_minutes,
            day_of_month=values.get("day_of_month"),
            run_at_date=values.get("run_at_date"),
            web_chat_id=web_chat_id,
            web_project_id=web_project_id,
            web_project_name=getattr(project, "name", "") if project is not None else "",
            workspace=workspace,
            archive_policy=str(values.get("archive_policy") or "manual"),
            title=str(values.get("title") or ""),
        )
        # Same gate `schedule_update` applies, on the create door. A model
        # emitting `daily_time: "9:30"` (no leading zero) or "25:00" otherwise
        # got a stored entry that `tick()` compares against "%H:%M" and never
        # matches — reported as healthy by `next_run` and silently dead.
        time_error = wall_clock_time_error(entry)
        if time_error:
            raise ControlPlaneError("invalid_time", time_error)
        return _ok(self._schedule_payload(entry))

    def schedule_create(self, principal: McpPrincipal, **values: Any) -> dict[str, Any]:
        preview = self.schedule_preview(principal, **values)["data"]
        entry = self.schedules.create(
            daily_time_utc=preview["daily_time_utc"],
            prompt=preview["prompt"],
            model=preview["model"],
            provider=preview["provider"],
            mode=preview["mode"],
            chat_id=0,
            timezone_name=preview["timezone_name"],
            days_of_week=preview["days_of_week"],
            frequency=preview["frequency"],
            interval_minutes=preview["interval_minutes"],
            day_of_month=preview["day_of_month"],
            run_at_date=preview["run_at_date"],
            web_chat_id=preview["web_chat_id"],
            web_project_id=preview["web_project_id"],
            web_project_name=preview["web_project_name"],
            workspace=preview["workspace"],
            archive_policy=preview["archive_policy"],
            title=preview["title"],
            description=str(values.get("description") or ""),
        )
        # Where a chat-bound entry re-homes once its chat is deleted; only
        # capturable while that chat still exists. See stamp_fallback_project.
        if stamp_fallback_project(entry, self.pcm):
            self.schedules.replace(entry)
        publish_automations_changed(self.pcm)
        return _ok(self._schedule_payload(entry))

    def _schedule(self, principal: McpPrincipal, schedule_id: str) -> ScheduleEntry:
        entry = next((item for item in self.schedules.list_entries() if item.schedule_id == schedule_id), None)
        if entry is None:
            raise ControlPlaneError("schedule_not_found", f"Schedule '{schedule_id}' was not found.")
        if entry.workspace and entry.workspace != principal.workspace:
            raise ControlPlaneError("workspace_forbidden", "Schedule belongs to another workspace.")
        resolved: ScheduleEntry = entry
        return resolved

    def schedule_update(self, principal: McpPrincipal, schedule_id: str, **changes: Any) -> dict[str, Any]:
        entry = self._schedule(principal, schedule_id)
        if entry.scope == "system" and any(key not in {"enabled", "workspace"} for key in changes):
            raise ControlPlaneError("system_schedule_read_only", "System schedules only allow enabled/workspace changes.")
        # Where this schedule's run bindings actually live: the bound project's
        # own workspace when there is one (an entry written before the workspace
        # field existed carries none of its own), else the stored field.
        bound = self.pcm.get_project(entry.web_project_id) if entry.web_project_id else None
        origin = str(getattr(bound, "workspace", "") or entry.workspace or principal.workspace)
        # Settle the destination workspace *before* resolving the project, and
        # validate it here: dispatch-time routing and provider/model inheritance
        # both hang off this field, so silently storing garbage would strand the
        # schedule. The destination is scoped like every other tool too: an
        # unattended run executes its prompt in bypass inside the target
        # workspace, so a move there is operator business, not something a
        # managed chat can talk a token into.
        if changes.get("workspace") is not None:
            target = self._workspace(principal, str(changes["workspace"]).strip().lower())
            changes["workspace"] = target
        else:
            target = origin

        project: Any | None = None
        if changes.get("project_id"):
            # Resolve the reference inside the *destination* workspace, exactly
            # as ``schedule_preview`` does. Going through
            # ``_resolve_project_id``/``_project`` searched and authorized
            # against the caller's own workspace instead, so
            # ``workspace="work"`` plus a project that lives in `work` failed
            # with project_not_found/workspace_forbidden before the new
            # workspace was ever considered — cross-workspace targeting worked
            # on create but never on update.
            project = self._resolve_project_in_workspace(
                principal, str(changes["project_id"]), target
            )
            changes["project_id"] = project.project_id
            # Keep workspace aligned with the new target (same as the HTTP API).
            changes.setdefault("workspace", project.workspace)
        aliases = {"daily_time": "daily_time_utc", "timezone": "timezone_name", "chat_id": "web_chat_id", "project_id": "web_project_id"}
        normalized = {aliases.get(key, key): value for key, value in changes.items() if value is not None}
        if project is not None:
            # The recorded name is what survives per-instance project-id
            # regeneration (dispatch re-homes by it), so leaving the previous
            # project's name behind would let a moved schedule silently re-home
            # onto the wrong project.
            normalized["web_project_name"] = project.name
        if target != origin:
            # Reassigning the workspace has to re-point the run target too:
            # dispatch prioritises web_chat_id/web_project_id and never checks
            # them against entry.workspace, so a bare ``workspace="work"``
            # update left the schedule listed under `work` while every
            # unattended run still created chats in — or posted into — the old
            # workspace's project/chat: a silent cross-workspace write.
            if normalized.get("web_chat_id"):
                # Same boundary ``schedule_create`` draws: a chat binding cannot
                # cross workspaces, and it resolves against the caller's own.
                raise ControlPlaneError(
                    "workspace_mismatch",
                    "chat_id binds the schedule to a chat in its current workspace; "
                    "omit it when moving the schedule to another workspace, and pass "
                    "project_id to choose where its runs land.",
                )
            if entry.web_chat_id or entry.web_project_id:
                normalized["web_chat_id"] = None
                if "web_project_id" not in normalized:
                    if entry.scope == "system":
                        # System rows persist only SYSTEM_STATE_FIELDS and
                        # resolve their project from the packaged definition
                        # plus the workspace at dispatch, so clearing suffices.
                        normalized["web_project_id"] = None
                        normalized["web_project_name"] = ""
                    else:
                        # A user schedule with neither binding is skipped
                        # outright at dispatch ("Schedule has no web target"),
                        # so a bare move still has to name a destination: the
                        # target workspace's General project, which is what an
                        # unqualified cross-workspace target means.
                        general = next(
                            (
                                item for item in self.pcm.list_projects(target)
                                if item.name == "General"
                            ),
                            None,
                        )
                        if general is None:
                            raise ControlPlaneError(
                                "project_required",
                                f"Workspace '{target}' has no General project to move this "
                                "schedule into; pass project_id naming a project in it.",
                            )
                        normalized["web_project_id"] = general.project_id
                        normalized["web_project_name"] = general.name
        known = set(ScheduleEntry.__dataclass_fields__)
        unknown = sorted(set(normalized) - known)
        if unknown:
            raise ControlPlaneError("invalid_fields", f"Unknown schedule fields: {', '.join(unknown)}")
        if "frequency" in normalized and normalized["frequency"] not in FREQUENCIES:
            raise ControlPlaneError(
                "invalid_frequency",
                f"frequency must be one of {', '.join(sorted(FREQUENCIES))}.",
            )
        if "interval_minutes" in normalized:
            try:
                normalized["interval_minutes"] = normalize_interval_minutes(
                    normalized["interval_minutes"]
                )
            except ValueError as exc:
                raise ControlPlaneError("invalid_interval", str(exc)) from exc
        updated = replace(entry, **normalized)
        # Switching to interval without naming a cadence would leave 0 stored,
        # which interval_delta floors to one minute -- far faster than asked.
        if is_interval(updated) and not updated.interval_minutes:
            updated.interval_minutes = DEFAULT_INTERVAL_MINUTES
        # The mirror of the REST route's guard. Moving an interval entry (or a
        # migrated loop) to a wall-clock cadence leaves daily_time_utc empty,
        # and compute_next_run cannot parse it -- the automation would report
        # as enabled and never dispatch.
        time_error = wall_clock_time_error(updated)
        if time_error:
            raise ControlPlaneError("invalid_time", time_error)
        # A changed chat/project binding moves where this entry re-homes.
        stamp_fallback_project(updated, self.pcm)
        self.schedules.replace(updated)
        publish_automations_changed(self.pcm)
        return _ok(self._schedule_payload(updated))

    _RUN_REFUSALS: dict[str, tuple[str, str]] = {
        "busy": (
            "schedule_busy",
            "The target chat has a turn in flight; retry when it finishes.",
        ),
        "missing-chat": (
            "schedule_target_missing",
            "The target chat no longer exists and could not be re-homed.",
        ),
    }

    def _raise_if_run_refused(self, result: dict[str, Any]) -> dict[str, Any]:
        """Turn a non-dispatching ``dispatch_now`` outcome into an error.

        An interval entry refuses rather than queues, and reports that through
        ``status`` rather than by raising. Wrapping it in ``_ok`` told the
        model the run had started when nothing was dispatched — the REST twin
        answers 409 for exactly these two, so the tool surface has to agree.
        """
        refusal = self._RUN_REFUSALS.get(str(result.get("status") or ""))
        if refusal is not None:
            raise ControlPlaneError(refusal[0], refusal[1])
        return result

    async def schedule_run(self, principal: McpPrincipal, schedule_id: str) -> dict[str, Any]:
        self._schedule(principal, schedule_id)
        result = self._raise_if_run_refused(await self.schedules.dispatch_now(schedule_id))
        publish_automations_changed(self.pcm)
        return _ok(result)

    def schedule_delete(self, principal: McpPrincipal, schedule_id: str) -> dict[str, Any]:
        entry = self._schedule(principal, schedule_id)
        if entry.scope == "system" or not entry.removable:
            raise ControlPlaneError("schedule_not_removable", "This schedule cannot be removed.")
        deleted = self.schedules.delete(schedule_id)
        publish_automations_changed(self.pcm)
        return _ok({"deleted": deleted, "schedule_id": schedule_id})

    # ---- loops (compatibility) -----------------------------------------
    # Loops became the `interval` cadence of a schedule. These methods keep the
    # retired tool surface working for one release by translating to and from
    # interval schedules; the `schedule` tool with frequency="interval" is the
    # real API. Remove them, and the `loop*` MCP tools, in the release after
    # next.

    def _loop_payload(self, entry: ScheduleEntry) -> dict[str, Any]:
        """An interval schedule rendered in the retired Loop shape."""
        return {
            "loop_id": entry.schedule_id,
            "schedule_id": entry.schedule_id,
            "prompt": entry.prompt,
            "web_chat_id": entry.web_chat_id or "",
            "web_project_id": entry.web_project_id or "",
            "workspace": entry.workspace,
            "created_at": entry.created_at,
            "interval_minutes": entry.interval_minutes,
            "title": entry.title,
            # One flag replaced two: a stopped entry neither ticks now nor
            # resumes on the next boot, so both legacy fields report it.
            "autostart": entry.enabled,
            "running": entry.enabled,
            "last_run_at": entry.last_dispatched_at,
            "last_status": entry.last_status,
            "scope": entry.scope,
        }

    def _interval_entries(self) -> list[ScheduleEntry]:
        return [entry for entry in self.schedules.list_entries() if is_interval(entry)]

    def loops_list(self, principal: McpPrincipal) -> dict[str, Any]:
        self._workspace(principal)
        rows = []
        for entry in self._interval_entries():
            if not entry.web_chat_id:
                continue
            try:
                self._chat(principal, entry.web_chat_id)
            except ControlPlaneError:
                continue
            rows.append(self._loop_payload(entry))
        return _ok(rows)

    def loop_create(
        self,
        principal: McpPrincipal,
        chat_id: str = "",
        prompt: str = "",
        interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
        title: str = "",
        autostart: bool = False,
        start: bool = True,
    ) -> dict[str, Any]:
        chat, project = self._chat_scope(principal, chat_id)
        text = prompt.strip()
        if not text:
            raise ControlPlaneError("empty_prompt", "Prompt is required.")
        try:
            minutes = normalize_interval_minutes(interval_minutes)
        except ValueError as exc:
            raise ControlPlaneError("invalid_interval", str(exc)) from exc
        entry = self.schedules.create(
            daily_time_utc="",
            prompt=text,
            # Empty model/mode is what makes each run inherit the target chat's
            # own settings, which is how loops always behaved.
            model="",
            mode="auto",
            chat_id=0,
            frequency=INTERVAL_FREQUENCY,
            interval_minutes=minutes,
            web_chat_id=chat.chat_id,
            title=title,
            workspace=project.workspace,
        )
        # `start` and `autostart` collapsed into `enabled`: the split existed so
        # a loop could tick until the next restart and then be silently dead,
        # which is the "model says running, loop isn't" failure it was meant to
        # prevent. Either flag now means "run it".
        stamp_fallback_project(entry, self.pcm)
        if not (start or autostart):
            entry.enabled = False
        self.schedules.replace(entry)
        publish_automations_changed(self.pcm)
        return _ok(self._loop_payload(entry))

    def _loop(self, principal: McpPrincipal, loop_id: str) -> ScheduleEntry:
        entry = next(
            (item for item in self._interval_entries() if item.schedule_id == loop_id),
            None,
        )
        if entry is None:
            raise ControlPlaneError("loop_not_found", f"Loop '{loop_id}' was not found.")
        # Same workspace boundary `_schedule` enforces. Loops always carried a
        # `web_chat_id`, so the chat check below was the only scope check they
        # ever needed; interval schedules can be project-bound and carry none,
        # which left this deprecated surface as an unguarded second door onto
        # another workspace's automations — and both `loop` and `loop_action`
        # are auto-approved, so no card would have been raised either.
        if entry.workspace and entry.workspace != principal.workspace:
            raise ControlPlaneError(
                "workspace_forbidden", "Loop belongs to another workspace."
            )
        # Packaged routines are read-only through `schedule`/`schedule_action`
        # (`system_schedule_read_only` / `schedule_not_removable`). Every
        # `_loop` caller mutates, runs, or deletes, so refuse them outright
        # here rather than let the deprecated shape reach a row the supported
        # surface protects. No packaged routine is interval-cadenced today;
        # this keeps that from silently becoming a hole if one ever is.
        if entry.scope == "system" or not entry.removable:
            raise ControlPlaneError(
                "system_schedule_read_only",
                "This is a system routine; manage it with `schedule`, not `loop`.",
            )
        if entry.web_chat_id:
            self._chat(principal, entry.web_chat_id)
        return entry

    def loop_update(self, principal: McpPrincipal, loop_id: str, **changes: Any) -> dict[str, Any]:
        entry = self._loop(principal, loop_id)
        supplied = {key: value for key, value in changes.items() if value is not None}
        unknown = sorted(
            set(supplied)
            - {"chat_id", "web_chat_id", "prompt", "title", "interval_minutes", "autostart"}
        )
        if unknown:
            raise ControlPlaneError("invalid_fields", f"Unknown loop fields: {', '.join(unknown)}")
        target_chat = supplied.pop("chat_id", None) or supplied.pop("web_chat_id", None)
        if target_chat is not None:
            chat, project = self._chat_scope(principal, str(target_chat))
            entry.web_chat_id = chat.chat_id
            entry.workspace = project.workspace
            # Retargeting moves where this entry re-homes.
            stamp_fallback_project(entry, self.pcm)
        if "prompt" in supplied:
            entry.prompt = str(supplied["prompt"])
        if "title" in supplied:
            entry.title = str(supplied["title"])
        if "interval_minutes" in supplied:
            try:
                entry.interval_minutes = normalize_interval_minutes(
                    supplied["interval_minutes"]
                )
            except ValueError as exc:
                raise ControlPlaneError("invalid_interval", str(exc)) from exc
        if "autostart" in supplied:
            entry.enabled = bool(supplied["autostart"])
        self.schedules.replace(entry)
        publish_automations_changed(self.pcm)
        return _ok(self._loop_payload(entry))

    def loop_start(self, principal: McpPrincipal, loop_id: str) -> dict[str, Any]:
        entry = self._loop(principal, loop_id)
        entry.enabled = True
        self.schedules.replace(entry)
        publish_automations_changed(self.pcm)
        return _ok(self._loop_payload(entry))

    def loop_stop(self, principal: McpPrincipal, loop_id: str) -> dict[str, Any]:
        entry = self._loop(principal, loop_id)
        entry.enabled = False
        self.schedules.replace(entry)
        publish_automations_changed(self.pcm)
        return _ok({"loop_id": loop_id, "running": False})

    async def loop_run(self, principal: McpPrincipal, loop_id: str) -> dict[str, Any]:
        self._loop(principal, loop_id)
        result = self._raise_if_run_refused(await self.schedules.dispatch_now(loop_id))
        publish_automations_changed(self.pcm)
        return _ok({**result, "loop_id": loop_id})

    def loop_delete(self, principal: McpPrincipal, loop_id: str) -> dict[str, Any]:
        self._loop(principal, loop_id)
        deleted = self.schedules.delete(loop_id)
        publish_automations_changed(self.pcm)
        return _ok({"deleted": deleted, "loop_id": loop_id})


    # ---- workspace files/assets ---------------------------------------

    def workspace_file_read(self, principal: McpPrincipal, path: str) -> dict[str, Any]:
        root = Path(self.config.workspace_root).resolve()
        target = self._safe_relative(root, path, must_exist=True)
        if not target.is_file() or target.stat().st_size > 2 * 1024 * 1024:
            raise ControlPlaneError("unsupported_file", "File must be a text file no larger than 2 MiB.")
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ControlPlaneError("binary_file", "Binary files are not returned through MCP.") from exc
        return _ok({"path": target.relative_to(root).as_posix(), "content": content})

    def workspace_file_write(self, principal: McpPrincipal, path: str, content: str) -> dict[str, Any]:
        root = Path(self.config.workspace_root).resolve()
        target = self._safe_relative(root, path)
        if target.is_relative_to(Path(self.config.state_path).parent.resolve()):
            raise ControlPlaneError("runtime_file_forbidden", "Runtime stores must be changed through their domain tools.")
        if len(content.encode("utf-8")) > 2 * 1024 * 1024:
            raise ControlPlaneError("file_too_large", "File exceeds the 2 MiB MCP write limit.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return _ok({"path": target.relative_to(root).as_posix(), "size": len(content.encode('utf-8'))})

    def file_surface(self, principal: McpPrincipal, path: str) -> dict[str, Any]:
        """Validate a workspace file exists so the PWA can open it in the pinned
        preview panel. The actual surfacing happens client-side, keyed off this
        tool call showing up in the turn's trace — see extract_file_touches in
        ciao/web/chat_broker.py. Pin delivery does not read either field below;
        neither one proves the panel opened or failed to open.

        ``viewers`` is how many `/ws/chat/{chat_id}` sockets are open for this
        chat right now, from the connection tracker. It reflects real client
        presence and is independent of whether a turn is streaming.

        ``stream_state`` is ``"active"`` when a turn is currently streaming for
        this chat, or ``"none"`` otherwise. It says nothing about whether a
        client is attached to that turn.
        """
        root = Path(self.config.workspace_root).resolve()
        target = self._safe_relative(root, path, must_exist=True)
        if not target.is_file():
            raise ControlPlaneError("unsupported_file", "Only an existing file can be surfaced.")
        viewers, stream_state = self._file_surface_signal(principal.chat_id)
        return _ok(
            {
                "path": target.relative_to(root).as_posix(),
                "viewers": viewers,
                "stream_state": stream_state,
            }
        )

    def _file_surface_signal(self, chat_id: str) -> tuple[int, str]:
        """Client-presence and stream-state signal for ``file_surface``.

        ``viewers`` used to be ``ChatStream.subscriber_count``, a value
        scoped to one turn, sampled once, to answer a question scoped to one
        connection ("is a client attached to this chat"). That mismatch made
        it flaky by construction, not just wrong on one code path:

        - Every turn boundary has a real gap. `_attach_streams`
          (ciao/web/routes_chat.py) polls the broker for a new stream every
          `_ATTACH_POLL_SECONDS` (0.5s, routes_chat.py:57), so a healthy,
          fully-attached client reads 0 subscribers on the new stream for up
          to half a second after it is registered, before flipping to 1 with
          no state change on the client's end. Observed in production: two
          `file_surface` calls minutes apart on one unbroken connection
          returned 0, 0, then a third returned 1 — consistent with sampling
          landing in that gap twice, then past it.
        - A worse, longer-lived version of the same mismatch: a client can be
          stuck relaying a superseded `ChatStream` that was replaced in the
          broker without `finish()` being called on it, because
          `_attach_streams` only re-polls after its current stream forward
          returns (see the orphaned-stream note on `ChatStreamBroker.register`,
          ciao/web/chat_broker.py). That client shows 0 subscribers on the
          new stream indefinitely, not just for one poll window.

        Both are symptoms of the same design error: a per-turn object cannot
        answer a per-connection question. Counting live `/ws/chat/{chat_id}`
        sockets via the connection tracker fixes this structurally — the
        socket's lifetime already matches the question being asked, so
        neither turn boundaries nor broker replacement can make it flicker.
        """
        viewers = 0
        if chat_id and self.connection_tracker is not None:
            viewers = self.connection_tracker.chat_client_count(chat_id)
        stream_state = "none"
        if chat_id:
            try:
                stream = self.pcm.get_active_stream(chat_id)
            except Exception:
                stream = None
            if stream is not None:
                stream_state = "active"
        return viewers, stream_state

    # ---- adversarial review ---------------------------------------------

    async def sync_skills(self, principal: McpPrincipal) -> dict[str, Any]:
        self._workspace(principal)
        from ciao.sync_skills import sync_workspace_skills

        result = await asyncio.to_thread(
            sync_workspace_skills,
            self.config.workspace_root,
        )
        return _ok(asdict(result))
