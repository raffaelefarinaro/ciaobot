"""Provider-neutral application control plane for PWA, MCP, and CLI adapters.

The existing managers remain the owners of Ciaobot state and invariants.  This
module supplies a small, typed boundary around them so an agent-facing
transport never needs a browser cookie, a localhost curl command, or direct
knowledge of ``.runtime`` JSON layouts.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ciao import vault_index
from ciao.background import BackgroundRun, BackgroundRunError, TAIL_LINES
from ciao.fts_search import get_db_path, index_vault, init_db, search_vault
from ciao.loops import publish_loops_changed
from ciao.memory_tool import memory_status as memory_status_payload
from ciao.memory_tool import resolve_region, update_region
from ciao.models import ControlSurface
from ciao.web.project_chats import UnknownModelError, _MAX_ACTIVE_DELEGATES
from ciao.schedules import ScheduleEntry, compute_next_run

logger = logging.getLogger(__name__)

# MCP needs to distinguish an omitted optional field from an explicit JSON
# null. ``None`` is a meaningful reset for workspace connector settings and
# denylists, so a normal ``= None`` default loses information before the
# control plane sees it.
_UNSET = object()

# One grammar for a proposal bullet, shared by the list and dismiss paths. The
# trailing `_(from: …)_` source tag is optional and captured when present.
_PROPOSAL_BULLET_RE = re.compile(
    r"^\s*-\s+\[(memory|user|profile)\]\s+(.+?)(?:\s+_\(from:\s*(.+?)\)_)?\s*$"
)


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
        loop_manager: Any,
        local_session_manager: Any | None = None,
        app_settings: Any | None = None,
        startup_tracker: Any | None = None,
        lifecycle_callback: Callable[[int], Any] | None = None,
        connection_tracker: Any | None = None,
        background_runner: Any | None = None,
    ) -> None:
        self.config = config
        self.pcm = project_chat_manager
        self.schedules = schedule_manager
        self.loops = loop_manager
        self.local_sessions = local_session_manager
        self.app_settings = app_settings
        self.startup_tracker = startup_tracker
        self._lifecycle_callback = lifecycle_callback
        self._deferred_actions: dict[str, dict[str, Any]] = {}
        # Optional: the app-wide ConnectionTracker, used by file_surface to
        # report real connected clients instead of a per-turn stream proxy.
        self.connection_tracker = connection_tracker
        # Optional: the BackgroundRunner backing the background_run_* tools.
        # Unset on legacy-only instances and in most tests.
        self.background = background_runner

    def set_lifecycle_callback(self, callback: Callable[[int], Any]) -> None:
        """Attach the server restart callback after uvicorn is constructed."""
        self._lifecycle_callback = callback

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
            "control_surface": getattr(chat, "control_surface", "")
            or getattr(self.config, "control_surface", "legacy"),
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

    def memory_status(self, principal: McpPrincipal) -> dict[str, Any]:
        """Report native guide memory usage without copying its contents."""
        self._workspace(principal)
        guide = Path(self.config.workspace_root) / "CLAUDE.md"
        return _ok(memory_status_payload(
            guide,
            memory_char_limit=int(getattr(self.config, "memory_char_limit", 2200)),
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
        self._workspace(principal)
        if action not in {"add", "replace", "remove"}:
            raise ControlPlaneError("invalid_action", "action must be add, replace, or remove.")
        canonical = resolve_region(region)
        limit = (
            int(getattr(self.config, "memory_char_limit", 2200))
            if canonical == "memory"
            else int(getattr(self.config, "user_char_limit", 1375))
        )
        guide = Path(self.config.workspace_root) / "CLAUDE.md"
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

    def _memory_proposals_path(self, principal: McpPrincipal) -> Path:
        return self._vault_root(principal) / "Workspace" / "Memory-Proposals.md"

    def memory_proposals_list(self, principal: McpPrincipal) -> dict[str, Any]:
        """Return structured pending proposal bullets for the active workspace."""
        path = self._memory_proposals_path(principal)
        if not path.exists():
            return _ok([])
        rows: list[dict[str, str]] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            match = _PROPOSAL_BULLET_RE.match(raw)
            if match:
                rows.append({
                    "target": resolve_region(match.group(1)),
                    "text": match.group(2).strip(),
                    "source": (match.group(3) or "").strip(),
                })
        return _ok(rows)

    def memory_proposal_resolve(
        self,
        principal: McpPrincipal,
        text: str,
        *,
        action: Literal["accept", "reject"],
    ) -> dict[str, Any]:
        """Dismiss exactly one proposal from the queue.

        Promotion into a CLAUDE.md region is an explicit ``Edit`` by the
        agent — this tool never writes memory. Ordering: edit the region
        first, then dismiss the proposal (the reverse loses the fact if the
        turn dies between the two steps).
        """
        if action not in {"accept", "reject"}:
            raise ControlPlaneError("invalid_action", "action must be accept or reject.")
        path = self._memory_proposals_path(principal)
        if not path.exists():
            raise ControlPlaneError("proposal_not_found", "The proposal queue is empty.")
        needle = text.strip()
        if not needle:
            raise ControlPlaneError("proposal_required", "A proposal text or unique substring is required.")
        lines = path.read_text(encoding="utf-8").splitlines()
        candidates = [
            (index, line)
            for index, line in enumerate(lines)
            if line.lstrip().startswith("- [") and needle.casefold() in line.casefold()
        ]
        if not candidates:
            raise ControlPlaneError("proposal_not_found", "No pending proposal matched that text.")
        if len(candidates) > 1:
            raise ControlPlaneError("proposal_ambiguous", "The text matched more than one proposal; use a longer substring.")
        index, line = candidates[0]
        match = _PROPOSAL_BULLET_RE.match(line)
        if match is None:
            raise ControlPlaneError("proposal_invalid", "The matching proposal has an unsupported format.")
        proposal_target = resolve_region(match.group(1))
        proposal_text = match.group(2).strip()
        del lines[index]
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return _ok({
            "action": action,
            "target": proposal_target,
            "text": proposal_text,
            "dismissed": True,
        })

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
        default_model: str = "",
        gws_profile: str = "",
        disallowed_tools: Any = _UNSET,
        claude_ai_mcps: Any = _UNSET,
        color: str = "",
    ) -> dict[str, Any]:
        """Register a new logical workspace under the standard vault folder."""
        from ciao.workspaces import persist_workspaces, workspace_from_request, workspace_to_dict

        data: dict[str, Any] = {
            "name": name,
            "default_provider": default_provider,
            "default_model": default_model,
            "gws_profile": gws_profile,
        }
        if disallowed_tools is not _UNSET:
            data["disallowed_tools"] = disallowed_tools
        if claude_ai_mcps is not _UNSET:
            data["claude_ai_mcps"] = claude_ai_mcps
        if color:
            data["color"] = color
        workspace = workspace_from_request(data, config=self.config)
        self.config.workspaces[workspace.name] = workspace
        persist_workspaces(self.config)
        self._refresh_workspace_registry()
        return _ok(workspace_to_dict(workspace, self.config))

    def workspace_update(
        self,
        principal: McpPrincipal,
        *,
        name: str,
        default_provider: str | None = None,
        default_model: str | None = None,
        gws_profile: str | None = None,
        disallowed_tools: Any = _UNSET,
        claude_ai_mcps: Any = _UNSET,
        color: str = "",
    ) -> dict[str, Any]:
        """Update a registered workspace. Omitted fields keep their values."""
        from ciao.workspaces import persist_workspaces, workspace_from_request, workspace_to_dict

        existing = self.config.workspace(name)
        if existing is None:
            raise ControlPlaneError("workspace_not_found", f"Workspace '{name}' was not found.")
        data: dict[str, Any] = {"name": name}
        if default_provider is not None:
            data["default_provider"] = default_provider
        if default_model is not None:
            data["default_model"] = default_model
        if gws_profile is not None:
            data["gws_profile"] = gws_profile
        if disallowed_tools is not _UNSET:
            data["disallowed_tools"] = disallowed_tools
        if claude_ai_mcps is not _UNSET:
            data["claude_ai_mcps"] = claude_ai_mcps
        if color:
            data["color"] = color
        workspace = workspace_from_request(data, config=self.config, existing=existing)
        self.config.workspaces[workspace.name] = workspace
        persist_workspaces(self.config)
        self._refresh_workspace_registry()
        return _ok(workspace_to_dict(workspace, self.config))

    def workspace_delete(self, principal: McpPrincipal, *, name: str) -> dict[str, Any]:
        """Delete a registered workspace. The last workspace cannot be deleted."""
        if self.config.workspace(name) is None:
            raise ControlPlaneError("workspace_not_found", f"Workspace '{name}' was not found.")
        if len(self.config.workspaces) <= 1:
            raise ControlPlaneError(
                "last_workspace", "Cannot delete the last workspace."
            )
        if name == principal.workspace:
            # The caller lives inside the workspace being deleted; dropping it
            # mid-turn would invalidate the calling scope, so wait until idle.
            return self._defer_until_chat_idle(
                principal,
                "workspace_delete",
                lambda: self._delete_workspace(name),
            )
        return _ok(self._delete_workspace(name))

    def _delete_workspace(self, name: str) -> dict[str, Any]:
        from ciao.workspaces import persist_workspaces

        self.config.workspaces.pop(name, None)
        persist_workspaces(self.config)
        self._refresh_workspace_registry()
        return {"deleted": name}

    def _refresh_workspace_registry(self) -> None:
        """Notify the project-chat manager after a registry mutation."""
        refresh = getattr(self.pcm, "refresh_workspaces", None)
        if callable(refresh):
            refresh()

    # ---- vault ---------------------------------------------------------

    def vault_search(self, principal: McpPrincipal, query: str, limit: int = 10) -> dict[str, Any]:
        root = self._vault_root(principal)
        db_path = get_db_path()
        conn = sqlite3.connect(db_path)
        try:
            init_db(conn)
            index_vault(conn, root)
            rows = search_vault(conn, query, limit=max(1, min(50, int(limit))))
        finally:
            conn.close()
        return _ok(rows)

    def vault_index_refresh(self, principal: McpPrincipal) -> dict[str, Any]:
        root = self._vault_root(principal)
        entries = vault_index.scan_vault(root)
        vault_index.write_index_file(entries, root / "INDEX.md")
        db_path = get_db_path()
        conn = sqlite3.connect(db_path)
        try:
            init_db(conn)
            indexed, removed = index_vault(conn, root)
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

    def project_files_list(self, principal: McpPrincipal, project_id: str = "") -> dict[str, Any]:
        project = self._resolve_project(principal, project_id)
        return _ok(self.pcm.list_project_files(project.project_id))

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
        control_surface: ControlSurface | None = None,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        project = self._resolve_project(principal, project_id)
        chat = self.pcm.create_chat(
            project.project_id, title=title, provider=provider, model=model, mode=mode
        )
        if control_surface is not None:
            chat.control_surface = control_surface
            self.pcm._save()
        result = chat.to_dict(local=True)
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
        control_surface: str | None = None,
    ) -> dict[str, Any]:
        chat_id = self._chat_id(principal, chat_id)
        if project_id is not None:
            self._project(principal, project_id)
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
        if control_surface is not None:
            if control_surface not in {"", "legacy", "mcp", "auto"}:
                raise ControlPlaneError("invalid_control_surface", "Use legacy, mcp, auto, or empty inheritance.")
            old_surface = updated.control_surface
            updated.control_surface = control_surface
            if old_surface != control_surface:
                async def _disconnect_after_turn() -> None:
                    while chat_id in self.pcm.active_chat_ids():
                        await asyncio.sleep(0.25)
                    self.pcm._revoke_mcp_chat(chat_id)
                    provider_service = self.pcm._providers.pop(chat_id, None)
                    if provider_service is not None:
                        await provider_service.disconnect()

                if chat_id == principal.chat_id:
                    asyncio.create_task(_disconnect_after_turn())
                else:
                    self.pcm._revoke_mcp_chat(chat_id)
                    provider_service = self.pcm._providers.pop(chat_id, None)
                    if provider_service is not None:
                        asyncio.create_task(provider_service.disconnect())
            self.pcm._save()
        return _ok(updated.to_dict(local=self.pcm.is_session_local(updated)))

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
            result = await self.pcm.archive_chat(target_id)
            outcome = result.outcome if result is not None else None
            if outcome is not None:
                self.pcm.run_archive_postprocess(target_id, outcome, chat, project)
            payload: dict[str, Any] = {
                "chat_id": target_id,
                "archived_to": str(outcome.path) if outcome else None,
            }
            # Delegate subchats are archived with their supervisor, and a
            # mid-turn one is stopped to get there. Report that to the agent
            # for the same reason the PWA gets it: a discarded turn and a
            # subchat left running are both things the caller must know.
            if result is not None and result.delegates:
                payload["subchats"] = [row.to_dict() for row in result.delegates]
                payload["stopped_chat_ids"] = result.stopped_ids()
                payload["failed_chat_ids"] = result.failed_ids()
            return payload

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

    # ---- delegates -----------------------------------------------------

    def _delegate_payload(self, chat: Any, *, streaming: bool) -> dict[str, Any]:
        return {
            "chat_id": chat.chat_id,
            "title": chat.title,
            "provider": chat.provider,
            "model": chat.model,
            "delegation_id": chat.delegation_id,
            "archived": chat.archived,
            "running": streaming,
            "created_at": chat.created_at,
            "last_activity_at": chat.last_activity_at,
        }

    def delegate_spawn(
        self,
        principal: McpPrincipal,
        *,
        prompt: str,
        title: str = "",
        provider: str | None = None,
        model: str | None = None,
        mode: str | None = None,
        delegation_id: str = "",
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Spawn a writable delegate chat that wakes this chat when it finishes."""
        parent = self._chat(principal, "")
        # Server-side recursion guard. CIAO_DELEGATE_OF tells a delegate what it
        # is, but a child could unset its own env, so the authoritative check is
        # the calling chat's own lineage.
        if parent.spawned_from_chat_id:
            raise ControlPlaneError(
                "nested_delegate_forbidden",
                "A delegate cannot spawn delegates. Report back to your "
                "supervisor instead.",
            )
        if not prompt.strip():
            raise ControlPlaneError("empty_prompt", "prompt is required.")
        active = self.pcm.active_delegate_count(parent.chat_id)
        if active >= _MAX_ACTIVE_DELEGATES:
            raise ControlPlaneError(
                "delegate_limit_reached",
                f"{active} delegates are already running (limit "
                f"{_MAX_ACTIVE_DELEGATES}). Wait for one to report before "
                f"spawning another.",
            )
        project = self._resolve_project(principal, project_id)
        try:
            chat = self.pcm.create_chat(
                project.project_id,
                title=title.strip() or "Delegate",
                provider=provider,
                model=model,
                mode=mode,
                spawned_from_chat_id=parent.chat_id,
                delegation_id=delegation_id.strip(),
            )
        except UnknownModelError as exc:
            # Only the model failure is a model problem; an unknown provider
            # or bucket raises ValueError before model validation and must
            # keep its own identity at the MCP boundary (invalid_request)
            # instead of being relabeled invalid_model (#259).
            raise ControlPlaneError("invalid_model", str(exc)) from exc
        # Same start/queue split as chat_send: a brand-new chat is never
        # streaming, so this normally starts immediately.
        if self.pcm.queue_message(chat.chat_id, prompt.strip()):
            send_status = "queued"
        else:
            self.pcm.start_stream(chat.chat_id, prompt.strip())
            send_status = "started"
        result = chat.to_dict(local=True)
        result["send_status"] = send_status
        result["active_delegates"] = active + 1
        return _ok(result)

    def delegates_list(self, principal: McpPrincipal, chat_id: str = "") -> dict[str, Any]:
        """List delegates spawned by a chat, with which ones are still running."""
        parent_id = self._chat_id(principal, chat_id)
        running = set(self.pcm.active_chat_ids())
        rows = [
            self._delegate_payload(c, streaming=c.chat_id in running)
            for c in self.pcm.delegates_for_chat(parent_id)
        ]
        return _ok({
            "chat_id": parent_id,
            "delegates": rows,
            "active": sum(1 for r in rows if r["running"] and not r["archived"]),
            "limit": _MAX_ACTIVE_DELEGATES,
        })

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
        """
        workspace = self._workspace(principal)
        chat_ref = values.get("chat_id")
        project_ref = values.get("project_id")
        web_chat_id: str | None = None
        project: Any | None = None

        if chat_ref:
            chat = self._chat(principal, str(chat_ref))
            web_chat_id = chat.chat_id
            if project_ref:
                project = self._resolve_project(principal, str(project_ref))
        else:
            # Inherit the active project when omitted — preferred for vault-aware
            # automation and keeps the schedule in the same workspace as this chat.
            project = self._resolve_project(
                principal, None if project_ref is None else str(project_ref)
            )

        web_project_id: str | None = None
        if project is not None:
            web_project_id = project.project_id
            workspace = self._workspace(principal, project.workspace)

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
            frequency=str(values.get("frequency") or "weekly"),
            day_of_month=values.get("day_of_month"),
            run_at_date=values.get("run_at_date"),
            web_chat_id=web_chat_id,
            web_project_id=web_project_id,
            workspace=workspace,
            archive_policy=str(values.get("archive_policy") or "manual"),
            title=str(values.get("title") or ""),
        )
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
            day_of_month=preview["day_of_month"],
            run_at_date=preview["run_at_date"],
            web_chat_id=preview["web_chat_id"],
            web_project_id=preview["web_project_id"],
            workspace=preview["workspace"],
            archive_policy=preview["archive_policy"],
            title=preview["title"],
            description=str(values.get("description") or ""),
        )
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
        if changes.get("project_id"):
            project_id = self._resolve_project_id(principal, str(changes["project_id"]))
            project = self._project(principal, project_id)
            changes["project_id"] = project_id
            # Keep workspace aligned with the new target (same as the HTTP API).
            changes.setdefault("workspace", project.workspace)
        aliases = {"daily_time": "daily_time_utc", "timezone": "timezone_name", "chat_id": "web_chat_id", "project_id": "web_project_id"}
        normalized = {aliases.get(key, key): value for key, value in changes.items() if value is not None}
        known = set(ScheduleEntry.__dataclass_fields__)
        unknown = sorted(set(normalized) - known)
        if unknown:
            raise ControlPlaneError("invalid_fields", f"Unknown schedule fields: {', '.join(unknown)}")
        updated = replace(entry, **normalized)
        self.schedules.replace(updated)
        return _ok(self._schedule_payload(updated))

    async def schedule_run(self, principal: McpPrincipal, schedule_id: str) -> dict[str, Any]:
        self._schedule(principal, schedule_id)
        return _ok(await self.schedules.dispatch_now(schedule_id))

    def schedule_delete(self, principal: McpPrincipal, schedule_id: str) -> dict[str, Any]:
        entry = self._schedule(principal, schedule_id)
        if entry.scope == "system" or not entry.removable:
            raise ControlPlaneError("schedule_not_removable", "This schedule cannot be removed.")
        return _ok({"deleted": self.schedules.delete(schedule_id), "schedule_id": schedule_id})

    def loops_list(self, principal: McpPrincipal) -> dict[str, Any]:
        self._workspace(principal)
        rows = []
        for entry in self.loops.list():
            try:
                self._chat(principal, entry.web_chat_id)
            except ControlPlaneError:
                continue
            row = asdict(entry)
            row["running"] = self.loops.is_running(entry.loop_id)
            rows.append(row)
        return _ok(rows)

    def loop_create(
        self,
        principal: McpPrincipal,
        chat_id: str = "",
        prompt: str = "",
        interval_minutes: int = 10,
        title: str = "",
        autostart: bool = False,
        start: bool = True,
    ) -> dict[str, Any]:
        chat, project = self._chat_scope(principal, chat_id)
        text = prompt.strip()
        if not text:
            raise ControlPlaneError("empty_prompt", "Prompt is required.")
        entry = self.loops.create(
            prompt=text,
            web_chat_id=chat.chat_id,
            interval_minutes=max(1, int(interval_minutes)),
            title=title,
            # `start` runs it now; `autostart` re-arms it on boot. Starting a
            # loop without the latter gives you one that ticks until the next
            # restart and is then silently dead, which is the "model says
            # running, loop isn't" failure one level down — so starting implies
            # both. A caller wanting a one-session loop can stop it.
            autostart=autostart or start,
            web_project_id=chat.project_id,
            workspace=project.workspace,
        )
        # ``autostart`` only governs server boot, so without this a freshly
        # created loop sat at "stopped" while the model cheerfully reported it
        # was running.
        if start:
            self.loops.start_loop(entry.loop_id)
        payload = asdict(entry)
        payload["running"] = self.loops.is_running(entry.loop_id)
        publish_loops_changed(self.pcm)
        return _ok(payload)

    def _loop(self, principal: McpPrincipal, loop_id: str) -> Any:
        entry = self.loops.get(loop_id)
        if entry is None:
            raise ControlPlaneError("loop_not_found", f"Loop '{loop_id}' was not found.")
        self._chat(principal, entry.web_chat_id)
        return entry

    def loop_update(self, principal: McpPrincipal, loop_id: str, **changes: Any) -> dict[str, Any]:
        entry = self._loop(principal, loop_id)
        aliases = {"chat_id": "web_chat_id"}
        normalized = {aliases.get(key, key): value for key, value in changes.items() if value is not None}
        if "web_chat_id" in normalized:
            chat, project = self._chat_scope(principal, str(normalized["web_chat_id"]))
            normalized["web_chat_id"] = chat.chat_id
            normalized["web_project_id"] = chat.project_id
            normalized["workspace"] = project.workspace
        known = set(entry.__dataclass_fields__)
        unknown = sorted(set(normalized) - known)
        if unknown:
            raise ControlPlaneError("invalid_fields", f"Unknown loop fields: {', '.join(unknown)}")
        if "interval_minutes" in normalized:
            normalized["interval_minutes"] = max(1, int(normalized["interval_minutes"]))
        updated = replace(entry, **normalized)
        self.loops.replace(updated)
        publish_loops_changed(self.pcm)
        return _ok(asdict(updated))

    def loop_start(self, principal: McpPrincipal, loop_id: str) -> dict[str, Any]:
        self._loop(principal, loop_id)
        entry = self.loops.start_loop(loop_id)
        publish_loops_changed(self.pcm)
        return _ok(asdict(entry))

    def loop_stop(self, principal: McpPrincipal, loop_id: str) -> dict[str, Any]:
        self._loop(principal, loop_id)
        self.loops.stop_loop(loop_id)
        publish_loops_changed(self.pcm)
        return _ok({"loop_id": loop_id, "running": False})

    async def loop_run(self, principal: McpPrincipal, loop_id: str) -> dict[str, Any]:
        self._loop(principal, loop_id)
        return _ok(await self.loops.run_now(loop_id))

    def loop_delete(self, principal: McpPrincipal, loop_id: str) -> dict[str, Any]:
        self._loop(principal, loop_id)
        deleted = self.loops.delete(loop_id)
        publish_loops_changed(self.pcm)
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

    def file_history_list(
        self, principal: McpPrincipal, chat_id: str, file_path: str
    ) -> dict[str, Any]:
        chat = self._chat(principal, chat_id)
        return _ok(self.pcm.snapshots.list_snapshots(chat_id=chat.chat_id, file_path=file_path))

    def file_snapshot_read(
        self, principal: McpPrincipal, chat_id: str, file_path: str, seq: int
    ) -> dict[str, Any]:
        chat = self._chat(principal, chat_id)
        result = self.pcm.snapshots.read_snapshot(
            chat_id=chat.chat_id, file_path=file_path, seq=max(1, int(seq))
        )
        if result is None:
            raise ControlPlaneError("snapshot_not_found", "The requested snapshot was not found.")
        content, meta = result
        if meta.get("truncated"):
            raise ControlPlaneError("snapshot_truncated", "The snapshot was too large to capture.")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ControlPlaneError("binary_snapshot", "Binary snapshots are not returned through MCP.") from exc
        return _ok({"content": text, "meta": meta})

    async def file_snapshot_restore(
        self, principal: McpPrincipal, chat_id: str, file_path: str, seq: int
    ) -> dict[str, Any]:
        chat_id = self._chat_id(principal, chat_id)
        root = Path(self.config.workspace_root).resolve()
        raw = Path(file_path)
        target = raw.resolve() if raw.is_absolute() else self._safe_relative(root, file_path)
        if not target.is_relative_to(root):
            raise ControlPlaneError("path_forbidden", "Snapshots can only be restored inside the workspace root.")
        result = self.pcm.snapshots.read_snapshot(
            chat_id=chat_id, file_path=file_path, seq=max(1, int(seq))
        )
        if result is None:
            raise ControlPlaneError("snapshot_not_found", "The requested snapshot was not found.")
        content, meta = result
        if meta.get("truncated"):
            raise ControlPlaneError("snapshot_truncated", "A truncated snapshot cannot be restored.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        new_meta = await self.pcm.snapshots.capture(
            chat_id=chat_id,
            file_path=file_path,
            action="restored",
            tool="MCPRestore",
        )
        return _ok({
            "restored_seq": int(seq),
            "new_seq": new_meta.seq if new_meta else 0,
            "path": target.relative_to(root).as_posix(),
        })

    # ---- adversarial review ---------------------------------------------

    async def adversarial_review(
        self,
        principal: McpPrincipal,
        artifact: str,
        *,
        doc_type: str = "document",
        focus: str = "",
        context: str = "",
        models: str = "",
        format: str = "markdown",
    ) -> dict[str, Any]:
        self._workspace(principal)
        text = artifact.strip()
        if not text:
            raise ControlPlaneError("empty_artifact", "Artifact text is required.")
        from ciao.critique import (
            USER_PROMPT_TEMPLATE,
            aggregate,
            render_markdown,
            resolve_critique_panel,
            run_panel,
        )

        panel = resolve_critique_panel(self.config, override=models)
        if not panel:
            raise ControlPlaneError(
                "no_panel", "No critique models are configured (Settings → Models)."
            )
        user_prompt = USER_PROMPT_TEMPLATE.format(
            doc_type=doc_type or "document",
            focus_block=f"Focus area: {focus}\n" if focus else "",
            context_block=f"Author context: {context}\n" if context else "",
            artifact=text,
        )
        results = await run_panel(panel, text, user_prompt, self.config)
        agg = aggregate(results)
        if format == "json":
            from dataclasses import asdict as _asdict

            return _ok({"aggregate": agg, "results": [_asdict(r) for r in results]})
        return _ok({
            "markdown": render_markdown("artifact", results, agg),
            "model_count": agg["model_count"],
            "ok_count": agg["ok_count"],
            "verdicts": agg["verdicts"],
            "total_issues": agg["total_issues"],
        })

    def agent_context_get(self, principal: McpPrincipal) -> dict[str, Any]:
        self._workspace(principal)
        from ciao.web.agent_assets import (
            list_command_assets,
            list_prompt_assets,
            list_subagents,
            workspace_health,
        )

        return _ok({
            "context": [asdict(item) for item in list_prompt_assets(self.config)],
            "subagents": [asdict(item) for item in list_subagents(self.config)],
            "commands": [asdict(item) for item in list_command_assets(self.config)],
            "health": workspace_health(self.config),
        })

    async def skills_sync(self, principal: McpPrincipal, refresh_upstream: bool = False) -> dict[str, Any]:
        self._workspace(principal)
        from ciao.sync_skills import sync_workspace_skills

        result = await asyncio.to_thread(
            sync_workspace_skills,
            self.config.workspace_root,
            refresh_upstream=refresh_upstream,
        )
        return _ok(asdict(result))

    # ---- operations ----------------------------------------------------

    async def local_session_status(self, principal: McpPrincipal) -> dict[str, Any]:
        self._workspace(principal)
        if self.local_sessions is None:
            raise ControlPlaneError("unavailable", "Local session manager is unavailable.")
        return _ok(self.local_sessions.status())

    async def local_session_preflight(self, principal: McpPrincipal) -> dict[str, Any]:
        self._workspace(principal)
        if self.local_sessions is None:
            raise ControlPlaneError("unavailable", "Local session manager is unavailable.")
        return _ok(await self.local_sessions.preflight())

    async def local_session_handback(
        self, principal: McpPrincipal, *, confirm_warnings: bool = False
    ) -> dict[str, Any]:
        self._workspace(principal)
        if self.local_sessions is None:
            raise ControlPlaneError("unavailable", "Local session manager is unavailable.")
        preflight = await self.local_sessions.preflight()
        if preflight.get("blockers"):
            raise ControlPlaneError("secrets_blocked", "The git handback is blocked by the secrets check.")
        if preflight.get("warnings") and not confirm_warnings:
            return {
                "ok": False,
                "error": {
                    "code": "confirmation_required",
                    "message": "Preflight warnings require explicit confirmation.",
                    "retryable": False,
                    "details": preflight.get("warnings"),
                },
            }
        return _ok(await self.local_sessions.commit_and_sync())

    async def local_session_resync(self, principal: McpPrincipal) -> dict[str, Any]:
        self._workspace(principal)
        if self.local_sessions is None:
            raise ControlPlaneError("unavailable", "Local session manager is unavailable.")
        return _ok(await self.local_sessions.resync())

    def lifecycle_actions_list(self, principal: McpPrincipal) -> dict[str, Any]:
        return _ok([
            dict(item)
            for item in self._deferred_actions.values()
            if item.get("token_id") == principal.token_id
        ])

    def lifecycle_action_request(
        self,
        principal: McpPrincipal,
        *,
        action: Literal["restart", "package_update"],
        confirmed: bool = False,
    ) -> dict[str, Any]:
        """Queue a self-affecting action after the requesting turn has drained."""
        self._workspace(principal)
        if action not in {"restart", "package_update"}:
            raise ControlPlaneError("invalid_action", "action must be restart or package_update.")
        if not confirmed:
            raise ControlPlaneError(
                "confirmation_required",
                f"Set confirmed=true only after the user explicitly approved {action}.",
            )
        if self._lifecycle_callback is None:
            raise ControlPlaneError("unavailable", "The server lifecycle callback is not ready.", retryable=True)
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
        asyncio.create_task(self._run_lifecycle_action(record), name=action_id)
        return _ok({key: value for key, value in record.items() if key != "token_id"})

    async def _run_lifecycle_action(self, record: dict[str, Any]) -> None:
        """Wait until the MCP caller's chat is idle before mutating its server."""
        chat_id = str(record.get("chat_id") or "")
        try:
            while chat_id and chat_id in self.pcm.active_chat_ids():
                await asyncio.sleep(0.25)
            record["status"] = "running"
            if record["action"] == "package_update":
                from ciao.package_version import update_package

                result = await asyncio.to_thread(update_package)
                record["result"] = result
                if not result.get("ok"):
                    raise RuntimeError(str(result.get("error") or "package update failed"))
            record["status"] = "restart_requested"
            callback = self._lifecycle_callback
            if callback is None:
                raise RuntimeError("server lifecycle callback became unavailable")
            callback(int(self.config.restart_exit_code))
            record["completed_at"] = datetime.now(UTC).isoformat()
        except Exception as exc:  # noqa: BLE001 - persist a stable deferred result
            record["status"] = "failed"
            record["error"] = str(exc)
            record["completed_at"] = datetime.now(UTC).isoformat()
