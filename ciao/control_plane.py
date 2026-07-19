"""Provider-neutral application control plane for PWA, MCP, and CLI adapters.

The existing managers remain the owners of Ciaobot state and invariants.  This
module supplies a small, typed boundary around them so an agent-facing
transport never needs a browser cookie, a localhost curl command, or direct
knowledge of ``.runtime`` JSON layouts.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ciao import job_runs, vault_index, vault_lint
from ciao.fts_search import get_db_path, index_vault, init_db, search_vault
from ciao.memory_tool import (
    add_entry,
    path_for_target,
    read_entries,
    remove_entry,
    replace_entry,
)
from ciao.models import ControlSurface
from ciao.schedules import ScheduleEntry, compute_next_run


@dataclass(frozen=True, slots=True)
class McpPrincipal:
    """Identity and scope attached to one managed provider process."""

    token_id: str
    chat_id: str
    project_id: str
    workspace: str
    provider: str
    role: Literal["chat", "automation", "consultation"] = "chat"
    consultation_depth: int = 0

    def to_claims(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_claims(cls, claims: dict[str, Any]) -> "McpPrincipal":
        return cls(
            token_id=str(claims.get("token_id") or ""),
            chat_id=str(claims.get("chat_id") or ""),
            project_id=str(claims.get("project_id") or ""),
            workspace=str(claims.get("workspace") or ""),
            provider=str(claims.get("provider") or ""),
            role=str(claims.get("role") or "chat"),  # type: ignore[arg-type]
            consultation_depth=int(claims.get("consultation_depth") or 0),
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
        provider_subchat_manager: Any | None = None,
        local_session_manager: Any | None = None,
        app_settings: Any | None = None,
        startup_tracker: Any | None = None,
        lifecycle_callback: Callable[[int], Any] | None = None,
    ) -> None:
        self.config = config
        self.pcm = project_chat_manager
        self.schedules = schedule_manager
        self.loops = loop_manager
        self.consultations = provider_subchat_manager
        self.local_sessions = local_session_manager
        self.app_settings = app_settings
        self.startup_tracker = startup_tracker
        self._lifecycle_callback = lifecycle_callback
        self._deferred_actions: dict[str, dict[str, Any]] = {}

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

    def _chat(self, principal: McpPrincipal, chat_id: str) -> Any:
        chat = self.pcm.get_chat(chat_id)
        if chat is None:
            raise ControlPlaneError("chat_not_found", f"Chat '{chat_id}' was not found.")
        self._project(principal, chat.project_id)
        return chat

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
            "consultation_depth": principal.consultation_depth,
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

    def automation_runs_list(self, principal: McpPrincipal, limit_per_job: int = 10) -> dict[str, Any]:
        self._workspace(principal)
        limit = max(1, min(50, int(limit_per_job)))
        return _ok(job_runs.automation_summary(limit_per_job=limit))

    # ---- memory --------------------------------------------------------

    def _memory_path_limit(self, target: str) -> tuple[Path, int]:
        if target not in {"memory", "user"}:
            raise ControlPlaneError("invalid_target", "target must be 'memory' or 'user'.")
        limit = (
            int(getattr(self.config, "memory_char_limit", 2200))
            if target == "memory"
            else int(getattr(self.config, "user_char_limit", 1375))
        )
        return path_for_target(target), limit  # type: ignore[arg-type]

    def memory_read(self, principal: McpPrincipal, target: str) -> dict[str, Any]:
        self._workspace(principal)
        path, limit = self._memory_path_limit(target)
        return read_entries(path, char_limit=limit)

    def memory_add(self, principal: McpPrincipal, target: str, text: str) -> dict[str, Any]:
        self._workspace(principal)
        path, limit = self._memory_path_limit(target)
        return add_entry(path, text, char_limit=limit)

    def memory_replace(
        self, principal: McpPrincipal, target: str, old_text: str, new_text: str
    ) -> dict[str, Any]:
        self._workspace(principal)
        path, limit = self._memory_path_limit(target)
        return replace_entry(path, old_text, new_text, char_limit=limit)

    def memory_remove(self, principal: McpPrincipal, target: str, text: str) -> dict[str, Any]:
        self._workspace(principal)
        path, limit = self._memory_path_limit(target)
        return remove_entry(path, text, char_limit=limit)

    def _memory_proposals_path(self, principal: McpPrincipal) -> Path:
        return self._vault_root(principal) / "Workspace" / "Memory-Proposals.md"

    def memory_proposals_list(self, principal: McpPrincipal) -> dict[str, Any]:
        """Return structured pending proposal bullets for the active workspace."""
        path = self._memory_proposals_path(principal)
        if not path.exists():
            return _ok([])
        rows: list[dict[str, str]] = []
        pattern = re.compile(
            r"^\s*-\s+\[(memory|user)\]\s+(.+?)(?:\s+_\(from:\s*(.+?)\)_)?\s*$"
        )
        for raw in path.read_text(encoding="utf-8").splitlines():
            match = pattern.match(raw)
            if match:
                rows.append({
                    "target": match.group(1),
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
        target: str = "",
    ) -> dict[str, Any]:
        """Accept or reject exactly one proposal while keeping the queue auditable."""
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
        match = re.match(r"^\s*-\s+\[(memory|user)\]\s+(.+?)(?:\s+_\(from:.*\)_)?\s*$", line)
        if match is None:
            raise ControlPlaneError("proposal_invalid", "The matching proposal has an unsupported format.")
        proposal_target = target.strip() or match.group(1)
        proposal_text = match.group(2).strip()
        if proposal_target not in {"memory", "user"}:
            raise ControlPlaneError("invalid_target", "target must be memory or user.")
        memory_result: dict[str, Any] | None = None
        if action == "accept":
            memory_result = self.memory_add(principal, proposal_target, proposal_text)
            if not memory_result.get("ok") and "duplicate" not in str(memory_result.get("error", "")).lower():
                return memory_result
        del lines[index]
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return _ok({
            "action": action,
            "target": proposal_target,
            "text": proposal_text,
            "memory": memory_result,
        })

    # ---- vault ---------------------------------------------------------

    def vault_notes_list(self, principal: McpPrincipal, limit: int = 100) -> dict[str, Any]:
        root = self._vault_root(principal)
        rows = []
        for path in sorted(root.rglob("*.md")):
            resolved = path.resolve()
            if not resolved.is_relative_to(root) or any(part.startswith(".") for part in path.relative_to(root).parts):
                continue
            rows.append(path.relative_to(root).as_posix())
            if len(rows) >= max(1, min(500, int(limit))):
                break
        return _ok(rows)

    def vault_note_read(self, principal: McpPrincipal, path: str) -> dict[str, Any]:
        root = self._vault_root(principal)
        target = self._safe_relative(root, path, must_exist=True)
        if target.suffix.lower() != ".md" or not target.is_file():
            raise ControlPlaneError("unsupported_file", "Vault note reads require a markdown file.")
        text = target.read_text(encoding="utf-8")
        if len(text) > 200_000:
            raise ControlPlaneError("file_too_large", "Vault note exceeds the 200000 character MCP limit.")
        return _ok({"path": path, "content": text})

    def vault_note_write(self, principal: McpPrincipal, path: str, content: str) -> dict[str, Any]:
        root = self._vault_root(principal)
        target = self._safe_relative(root, path)
        if target.suffix.lower() != ".md":
            raise ControlPlaneError("unsupported_file", "Vault note writes require a .md path.")
        if len(content) > 200_000:
            raise ControlPlaneError("file_too_large", "Vault note exceeds the 200000 character MCP limit.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return _ok({"path": target.relative_to(root).as_posix(), "size": len(content)})

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

    def vault_lint(self, principal: McpPrincipal) -> dict[str, Any]:
        return _ok(vault_lint.run_validation(self._vault_root(principal)))

    # ---- projects/chats ------------------------------------------------

    def projects_list(self, principal: McpPrincipal, include_completed: bool = False) -> dict[str, Any]:
        workspace = self._workspace(principal)
        data: dict[str, Any] = {
            "active": [item.to_dict() for item in self.pcm.list_projects(workspace)]
        }
        if include_completed:
            data["completed"] = self.pcm.list_completed_projects(workspace)
        return _ok(data)

    def project_get(self, principal: McpPrincipal, project_id: str) -> dict[str, Any]:
        return _ok(self._project(principal, project_id).to_dict())

    def project_create(self, principal: McpPrincipal, name: str, context: str = "") -> dict[str, Any]:
        workspace = self._workspace(principal)
        clean_name = name.strip()
        if not clean_name:
            raise ControlPlaneError("invalid_name", "Project name is required.")
        return _ok(self.pcm.create_project(clean_name, workspace, context).to_dict())

    def project_update(
        self,
        principal: McpPrincipal,
        project_id: str,
        *,
        name: str | None = None,
        context: str | None = None,
        vault_folder: str | None = None,
    ) -> dict[str, Any]:
        self._project(principal, project_id)
        item = self.pcm.update_project(
            project_id, name=name, context=context, vault_folder=vault_folder
        )
        if item is None:
            raise ControlPlaneError("project_not_found", f"Project '{project_id}' was not found.")
        return _ok(item.to_dict())

    def project_complete(self, principal: McpPrincipal, project_id: str) -> dict[str, Any]:
        self._project(principal, project_id)
        current_chat = self.pcm.get_chat(principal.chat_id) if principal.chat_id else None
        if current_chat is not None and current_chat.project_id == project_id:
            return self._defer_until_chat_idle(
                principal,
                "project_complete",
                lambda: self.pcm.complete_project(project_id),
            )
        return _ok(self.pcm.complete_project(project_id))

    def project_restore(self, principal: McpPrincipal, stem: str) -> dict[str, Any]:
        workspace = self._workspace(principal)
        return _ok(self.pcm.restore_project(workspace, stem))

    def project_delete(self, principal: McpPrincipal, project_id: str) -> dict[str, Any]:
        self._project(principal, project_id)
        current_chat = self.pcm.get_chat(principal.chat_id) if principal.chat_id else None
        if current_chat is not None and current_chat.project_id == project_id:
            return self._defer_until_chat_idle(
                principal,
                "project_delete",
                lambda: {
                    "deleted": self.pcm.delete_project(project_id),
                    "project_id": project_id,
                },
            )
        return _ok({"deleted": self.pcm.delete_project(project_id), "project_id": project_id})

    def project_files_list(self, principal: McpPrincipal, project_id: str) -> dict[str, Any]:
        self._project(principal, project_id)
        return _ok(self.pcm.list_project_files(project_id))

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
        project_id: str,
        *,
        title: str = "New Chat",
        provider: str | None = None,
        model: str | None = None,
        mode: str | None = None,
        control_surface: ControlSurface | None = None,
    ) -> dict[str, Any]:
        self._project(principal, project_id)
        chat = self.pcm.create_chat(
            project_id, title=title, provider=provider, model=model, mode=mode
        )
        if control_surface is not None:
            chat.control_surface = control_surface
            self.pcm._save()
        return _ok(chat.to_dict(local=True))

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
        model_bucket: str | None = None,
        control_surface: str | None = None,
    ) -> dict[str, Any]:
        chat = self._chat(principal, chat_id)
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
            model_bucket=model_bucket,
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
        self._chat(principal, chat_id)
        chat = self.pcm.continue_archived_chat(chat_id)
        return _ok(chat.to_dict(local=True))

    def chat_retry(self, principal: McpPrincipal, chat_id: str) -> dict[str, Any]:
        self._chat(principal, chat_id)
        stream = self.pcm.try_chat_retry_now(chat_id)
        return _ok({"chat_id": chat_id, "status": "started" if stream else "not_pending"})

    def chat_retry_update(
        self,
        principal: McpPrincipal,
        chat_id: str,
        action: Literal["set", "stop", "try_now"],
        prompt: str = "",
    ) -> dict[str, Any]:
        self._chat(principal, chat_id)
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
        self._chat(principal, chat_id)
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
        model_bucket: str = "",
    ) -> dict[str, Any]:
        self._chat(principal, chat_id)
        if chat_id == principal.chat_id:
            return self._defer_until_chat_idle(
                principal,
                "chat_handover",
                lambda: self.pcm.handover_chat(
                    chat_id,
                    provider=provider.strip(),
                    model=model.strip(),
                    messages=[row for row in (messages or []) if isinstance(row, dict)],
                    model_bucket=model_bucket.strip(),
                ),
            )
        chat = self.pcm.handover_chat(
            chat_id,
            provider=provider.strip(),
            model=model.strip(),
            messages=[row for row in (messages or []) if isinstance(row, dict)],
            model_bucket=model_bucket.strip(),
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
        self._chat(principal, chat_id)
        if turn_index < 0:
            raise ControlPlaneError("invalid_turn", "turn_index must be non-negative.")
        fork = self.pcm.fork_chat(
            chat_id,
            messages=[row for row in messages if isinstance(row, dict)],
            turn_index=turn_index,
        )
        return _ok(fork.to_dict(local=True))

    def chat_archive(self, principal: McpPrincipal, chat_id: str) -> dict[str, Any]:
        chat = self._chat(principal, chat_id)
        project = self._project(principal, chat.project_id)
        if chat_id == principal.chat_id:
            def _archive() -> dict[str, Any]:
                outcome = self.pcm.archive_chat(chat_id)
                if outcome is not None:
                    self.pcm.run_archive_postprocess(chat_id, outcome, chat, project)
                return {"chat_id": chat_id, "archived_to": str(outcome.path) if outcome else None}

            return self._defer_until_chat_idle(principal, "chat_archive", _archive)
        outcome = self.pcm.archive_chat(chat_id)
        if outcome is not None:
            self.pcm.run_archive_postprocess(chat_id, outcome, chat, project)
        return _ok({"chat_id": chat_id, "archived_to": str(outcome.path) if outcome else None})

    def chat_delete(self, principal: McpPrincipal, chat_id: str) -> dict[str, Any]:
        self._chat(principal, chat_id)
        if chat_id == principal.chat_id:
            return self._defer_until_chat_idle(
                principal,
                "chat_delete",
                lambda: {"chat_id": chat_id, "deleted": self.pcm.delete_chat(chat_id)},
            )
        return _ok({"chat_id": chat_id, "deleted": self.pcm.delete_chat(chat_id)})

    def chat_mark_read(self, principal: McpPrincipal, chat_id: str) -> dict[str, Any]:
        self._chat(principal, chat_id)
        chat = self.pcm.mark_read(chat_id)
        return _ok({"chat_id": chat_id, "last_read_at": chat.last_read_at if chat else ""})

    async def chat_stop(self, principal: McpPrincipal, chat_id: str) -> dict[str, Any]:
        self._chat(principal, chat_id)
        if chat_id == principal.chat_id:
            raise ControlPlaneError(
                "self_stop_forbidden",
                "The current turn cannot stop itself through MCP; use the PWA stop control.",
            )
        return _ok({"chat_id": chat_id, "stopped": await self.pcm.stop_chat(chat_id)})

    # ---- provider consultations --------------------------------------

    def _consultation_manager(self) -> Any:
        if self.consultations is None:
            raise ControlPlaneError("unavailable", "Provider consultation manager is unavailable.")
        return self.consultations

    def _consultation_record(self, principal: McpPrincipal, subchat_id: str) -> Any:
        record = self._consultation_manager().get_record(subchat_id)
        if record is None:
            raise ControlPlaneError("consultation_not_found", f"Consultation '{subchat_id}' was not found.")
        self._chat(principal, record.parent_chat_id)
        return record

    def consultations_list(self, principal: McpPrincipal, chat_id: str = "") -> dict[str, Any]:
        parent_id = chat_id or principal.chat_id
        self._chat(principal, parent_id)
        return _ok([item.to_dict() for item in self._consultation_manager().list_records(parent_id)])

    async def consultation_start(
        self,
        principal: McpPrincipal,
        *,
        provider: str,
        model: str,
        message: str,
        chat_id: str = "",
        model_bucket: str = "",
        user_authorized: bool = False,
    ) -> dict[str, Any]:
        if principal.role == "consultation":
            raise ControlPlaneError("nested_consultation_forbidden", "A consultation cannot start another consultation.")
        parent = self._chat(principal, chat_id or principal.chat_id)
        if not provider.strip() or not model.strip() or not message.strip():
            raise ControlPlaneError("invalid_consultation", "provider, model, and message are required.")
        from ciao.provider_subchats import ProviderRoute

        owner = ProviderRoute(
            provider=parent.provider,
            model=parent.model,
            model_bucket=parent.model_bucket,
            label="owner",
        )
        participant = ProviderRoute(
            provider=provider.strip(),
            model=model.strip(),
            model_bucket=model_bucket.strip(),
            label="participant",
        )
        record = self._consultation_manager().create_subchat(
            parent_chat_id=parent.chat_id,
            parent_turn_index=max(0, int(parent.user_turn_count) - 1),
            owner=owner,
            participant=participant,
        )
        result = await self._consultation_manager().run_consultation_turn(
            record.subchat_id,
            message.strip(),
            user_authorized=user_authorized,
        )
        return _ok({"record": record.to_dict(), "result": result})

    async def consultation_send(
        self,
        principal: McpPrincipal,
        subchat_id: str,
        message: str,
        *,
        user_authorized: bool = False,
    ) -> dict[str, Any]:
        self._consultation_record(principal, subchat_id)
        if not message.strip():
            raise ControlPlaneError("empty_prompt", "message is required.")
        return _ok(await self._consultation_manager().run_consultation_turn(
            subchat_id, message.strip(), user_authorized=user_authorized
        ))

    def consultation_events(self, principal: McpPrincipal, subchat_id: str) -> dict[str, Any]:
        self._consultation_record(principal, subchat_id)
        return _ok(self._consultation_manager().get_events(subchat_id))

    def consultation_close(self, principal: McpPrincipal, subchat_id: str) -> dict[str, Any]:
        self._consultation_record(principal, subchat_id)
        self._consultation_manager().close_subchat(subchat_id)
        return _ok(self._consultation_manager().get_record(subchat_id).to_dict())

    async def consultation_cancel(self, principal: McpPrincipal, subchat_id: str) -> dict[str, Any]:
        self._consultation_record(principal, subchat_id)
        await self._consultation_manager().cancel_subchat(subchat_id)
        return _ok(self._consultation_manager().get_record(subchat_id).to_dict())

    def consultation_extend(
        self, principal: McpPrincipal, subchat_id: str, *, user_authorized: bool
    ) -> dict[str, Any]:
        self._consultation_record(principal, subchat_id)
        self._consultation_manager().extend_subchat(subchat_id, user_authorized=user_authorized)
        return _ok(self._consultation_manager().get_record(subchat_id).to_dict())

    # ---- schedules/loops ----------------------------------------------

    @staticmethod
    def _schedule_payload(entry: ScheduleEntry) -> dict[str, Any]:
        data = asdict(entry)
        next_run = compute_next_run(entry)
        data["next_run"] = next_run.isoformat() if next_run else None
        return data

    def schedules_list(self, principal: McpPrincipal) -> dict[str, Any]:
        workspace = self._workspace(principal)
        rows = [
            self._schedule_payload(entry)
            for entry in self.schedules.list()
            if not entry.workspace or entry.workspace == workspace
        ]
        return _ok(rows)

    def schedule_preview(self, principal: McpPrincipal, **values: Any) -> dict[str, Any]:
        workspace = self._workspace(principal)
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
            web_chat_id=values.get("chat_id"),
            web_project_id=values.get("project_id"),
            workspace=workspace,
            archive_policy=str(values.get("archive_policy") or "manual"),
            title=str(values.get("title") or ""),
        )
        if entry.web_chat_id:
            self._chat(principal, entry.web_chat_id)
        if entry.web_project_id:
            self._project(principal, entry.web_project_id)
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
        entry = next((item for item in self.schedules.list() if item.schedule_id == schedule_id), None)
        if entry is None:
            raise ControlPlaneError("schedule_not_found", f"Schedule '{schedule_id}' was not found.")
        if entry.workspace and entry.workspace != principal.workspace:
            raise ControlPlaneError("workspace_forbidden", "Schedule belongs to another workspace.")
        return entry

    def schedule_update(self, principal: McpPrincipal, schedule_id: str, **changes: Any) -> dict[str, Any]:
        entry = self._schedule(principal, schedule_id)
        if entry.scope == "system" and any(key not in {"enabled", "workspace"} for key in changes):
            raise ControlPlaneError("system_schedule_read_only", "System schedules only allow enabled/workspace changes.")
        aliases = {"daily_time": "daily_time_utc", "timezone": "timezone_name", "chat_id": "web_chat_id", "project_id": "web_project_id"}
        normalized = {aliases.get(key, key): value for key, value in changes.items() if value is not None}
        known = set(ScheduleEntry.__dataclass_fields__)
        unknown = sorted(set(normalized) - known)
        if unknown:
            raise ControlPlaneError("invalid_fields", f"Unknown schedule fields: {', '.join(unknown)}")
        updated = replace(entry, **normalized)
        self.schedules.replace(updated)
        return _ok(self._schedule_payload(updated))

    def schedule_pause(self, principal: McpPrincipal, schedule_id: str) -> dict[str, Any]:
        return self.schedule_update(principal, schedule_id, enabled=False)

    def schedule_resume(self, principal: McpPrincipal, schedule_id: str) -> dict[str, Any]:
        return self.schedule_update(principal, schedule_id, enabled=True)

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
        chat_id: str,
        prompt: str,
        interval_minutes: int = 10,
        title: str = "",
        autostart: bool = False,
    ) -> dict[str, Any]:
        self._chat(principal, chat_id)
        entry = self.loops.create(
            prompt=prompt,
            web_chat_id=chat_id,
            interval_minutes=max(1, int(interval_minutes)),
            title=title,
            autostart=autostart,
        )
        return _ok(asdict(entry))

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
            self._chat(principal, str(normalized["web_chat_id"]))
        known = set(entry.__dataclass_fields__)
        unknown = sorted(set(normalized) - known)
        if unknown:
            raise ControlPlaneError("invalid_fields", f"Unknown loop fields: {', '.join(unknown)}")
        if "interval_minutes" in normalized:
            normalized["interval_minutes"] = max(1, int(normalized["interval_minutes"]))
        updated = replace(entry, **normalized)
        self.loops.replace(updated)
        return _ok(asdict(updated))

    def loop_start(self, principal: McpPrincipal, loop_id: str) -> dict[str, Any]:
        self._loop(principal, loop_id)
        return _ok(asdict(self.loops.start_loop(loop_id)))

    def loop_stop(self, principal: McpPrincipal, loop_id: str) -> dict[str, Any]:
        self._loop(principal, loop_id)
        self.loops.stop_loop(loop_id)
        return _ok({"loop_id": loop_id, "running": False})

    async def loop_run(self, principal: McpPrincipal, loop_id: str) -> dict[str, Any]:
        self._loop(principal, loop_id)
        return _ok(await self.loops.run_now(loop_id))

    def loop_delete(self, principal: McpPrincipal, loop_id: str) -> dict[str, Any]:
        self._loop(principal, loop_id)
        return _ok({"deleted": self.loops.delete(loop_id), "loop_id": loop_id})

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

    def file_history_list(
        self, principal: McpPrincipal, chat_id: str, file_path: str
    ) -> dict[str, Any]:
        self._chat(principal, chat_id)
        return _ok(self.pcm.snapshots.list_snapshots(chat_id=chat_id, file_path=file_path))

    def file_snapshot_read(
        self, principal: McpPrincipal, chat_id: str, file_path: str, seq: int
    ) -> dict[str, Any]:
        self._chat(principal, chat_id)
        result = self.pcm.snapshots.read_snapshot(
            chat_id=chat_id, file_path=file_path, seq=max(1, int(seq))
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
        self._chat(principal, chat_id)
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

    def workspace_health_get(self, principal: McpPrincipal) -> dict[str, Any]:
        self._workspace(principal)
        from ciao.web.agent_assets import workspace_health

        return _ok(workspace_health(self.config))

    def workspace_health_fix(self, principal: McpPrincipal) -> dict[str, Any]:
        self._workspace(principal)
        from ciao.web.agent_assets import repair_workspace_health

        return _ok(repair_workspace_health(self.config))

    def skills_list(self, principal: McpPrincipal) -> dict[str, Any]:
        self._workspace(principal)
        from ciao.skills_inventory import build_skill_inventory

        return _ok(build_skill_inventory(self.config.workspace_root))

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

    async def package_status_get(self, principal: McpPrincipal) -> dict[str, Any]:
        self._workspace(principal)
        from ciao.package_version import package_status

        return _ok(await asyncio.to_thread(package_status))

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

    def debug_issues_get(self, principal: McpPrincipal) -> dict[str, Any]:
        self._workspace(principal)
        from ciao.debug_report import build_issue_report

        return _ok(build_issue_report(Path(self.config.workspace_root)))

    def serialize_for_report(self, value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
