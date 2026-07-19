"""Authenticated MCP adapter for Ciaobot's application control plane."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import secrets
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

from ciao.control_plane import CiaoControlPlane, ControlPlaneError, McpPrincipal


logger = logging.getLogger(__name__)


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
        self._by_key: dict[tuple[str, str, str], str] = {}
        self._lock = threading.RLock()

    def issue(
        self,
        *,
        chat_id: str,
        project_id: str,
        workspace: str,
        provider: str,
        role: str = "chat",
        consultation_depth: int = 0,
    ) -> tuple[str, McpPrincipal]:
        key = (chat_id, provider, role)
        now = int(time.time())
        with self._lock:
            existing_token = self._by_key.get(key)
            existing = self._by_token.get(existing_token or "")
            if existing is not None and existing.expires_at > now:
                return existing.token, existing.principal
            token = secrets.token_urlsafe(36)
            principal = McpPrincipal(
                token_id=secrets.token_hex(8),
                chat_id=chat_id,
                project_id=project_id,
                workspace=workspace,
                provider=provider,
                role=role,  # type: ignore[arg-type]
                consultation_depth=consultation_depth,
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
                self._by_key.pop((item.principal.chat_id, item.principal.provider, item.principal.role), None)
            return len(doomed)

    def revoke(self, token: str) -> bool:
        with self._lock:
            item = self._by_token.pop(token, None)
            if item is None:
                return False
            self._by_key.pop((item.principal.chat_id, item.principal.provider, item.principal.role), None)
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


class CiaoMcpService:
    """Own the FastMCP server, authentication, tool catalog, and telemetry."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.registry = McpSessionRegistry()
        self.control_plane: CiaoControlPlane | None = None
        self._tool_names: set[str] = set()
        self._last_error = ""
        self._telemetry_path = Path(config.state_path).parent / "mcp_tool_calls.jsonl"
        issuer = f"http://127.0.0.1:{int(config.pwa_port)}"
        self.server = FastMCP(
            "ciaobot",
            instructions=(
                "Use these tools for Ciaobot memory, vault, projects, chats, "
                "schedules, loops, files, and application state. Prefer them "
                "over curl, the ciao CLI, or direct .runtime edits. All paths "
                "are relative to the active workspace or vault."
            ),
            host="127.0.0.1",
            streamable_http_path="/",
            json_response=True,
            stateless_http=True,
            token_verifier=self.registry,
            auth=AuthSettings(
                issuer_url=issuer,
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

    def credentials_for_chat(self, chat: Any, project: Any, *, role: str = "chat") -> tuple[str, str]:
        token, _principal = self.registry.issue(
            chat_id=chat.chat_id,
            project_id=chat.project_id,
            workspace=project.workspace,
            provider=chat.provider,
            role=role,
            consultation_depth=1 if role == "consultation" else 0,
        )
        return self.url, token

    @asynccontextmanager
    async def lifespan(self):
        async with self.server.session_manager.run():
            yield

    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(getattr(self.config, "mcp_enabled", True)),
            "url": self.url,
            "bound": self.control_plane is not None,
            "tool_count": len(self._tool_names),
            "tools": sorted(self._tool_names),
            "last_error": self._last_error,
            **self.registry.status(),
        }

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
        try:
            if self.control_plane is None:
                raise ControlPlaneError("unavailable", "Ciaobot control plane is not ready.", retryable=True)
            principal = self._principal()
            if mutating and principal.role == "consultation":
                raise ControlPlaneError(
                    "consultation_read_only",
                    "Provider consultation participants have read-only Ciaobot access.",
                )
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
            )

    def _record_tool_call(
        self,
        *,
        name: str,
        principal: McpPrincipal | None,
        status: str,
        error_code: str,
        duration_ms: int,
    ) -> None:
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "surface": "mcp",
            "tool": name,
            "token_id": principal.token_id if principal else "",
            "chat_id": principal.chat_id if principal else "",
            "provider": principal.provider if principal else "",
            "status": status,
            "error_code": error_code,
            "duration_ms": duration_ms,
        }
        try:
            self._telemetry_path.parent.mkdir(parents=True, exist_ok=True)
            with self._telemetry_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError:
            pass

    def _tool(self, *args: Any, **kwargs: Any):
        name = str(kwargs.get("name") or (args[0] if args else ""))
        if name:
            self._tool_names.add(name)
        return self.server.tool(*args, **kwargs)

    def _register_tools(self) -> None:  # noqa: C901 - catalog is intentionally explicit
        tool = self._tool

        @tool(name="context_get", annotations=_READ, structured_output=True)
        async def context_get() -> dict[str, Any]:
            """Return the active Ciaobot workspace, project, chat, provider, and control surface."""
            return await self._invoke("context_get", lambda cp, p: cp.context_get(p))

        @tool(name="capabilities_get", annotations=_READ, structured_output=True)
        async def capabilities_get() -> dict[str, Any]:
            """List the Ciaobot MCP tools available to this managed provider process."""
            return await self._invoke(
                "capabilities_get",
                lambda _cp, _p: {"ok": True, "data": sorted(self._tool_names)},
            )

        @tool(name="system_status_get", annotations=_READ, structured_output=True)
        async def system_status_get() -> dict[str, Any]:
            """Return local Ciaobot server, workspace, startup, and active-chat status."""
            return await self._invoke("system_status_get", lambda cp, p: cp.system_status_get(p))

        @tool(name="automation_runs_list", annotations=_READ, structured_output=True)
        async def automation_runs_list(limit_per_job: int = 10) -> dict[str, Any]:
            """List recent background automation runs and their durations/errors."""
            return await self._invoke(
                "automation_runs_list",
                lambda cp, p: cp.automation_runs_list(p, limit_per_job),
            )

        @tool(name="debug_issues_get", annotations=_READ, structured_output=True)
        async def debug_issues_get() -> dict[str, Any]:
            """Return a sanitized local Ciaobot runtime issue report."""
            return await self._invoke("debug_issues_get", lambda cp, p: cp.debug_issues_get(p))

        @tool(name="memory_read", annotations=_READ, structured_output=True)
        async def memory_read(target: str = "memory") -> dict[str, Any]:
            """Read bounded cross-session memory or the bounded user profile."""
            return await self._invoke("memory_read", lambda cp, p: cp.memory_read(p, target))

        @tool(name="memory_add", annotations=_WRITE, structured_output=True)
        async def memory_add(text: str, target: str = "memory") -> dict[str, Any]:
            """Add one validated durable entry to bounded memory or the user profile."""
            return await self._invoke("memory_add", lambda cp, p: cp.memory_add(p, target, text), mutating=True)

        @tool(name="memory_replace", annotations=_WRITE, structured_output=True)
        async def memory_replace(old_text: str, new_text: str, target: str = "memory") -> dict[str, Any]:
            """Replace exactly one bounded-memory entry selected by a unique substring."""
            return await self._invoke(
                "memory_replace",
                lambda cp, p: cp.memory_replace(p, target, old_text, new_text),
                mutating=True,
            )

        @tool(name="memory_remove", annotations=_DESTRUCTIVE, structured_output=True)
        async def memory_remove(text: str, target: str = "memory") -> dict[str, Any]:
            """Remove exactly one bounded-memory entry selected by a unique substring."""
            return await self._invoke("memory_remove", lambda cp, p: cp.memory_remove(p, target, text), mutating=True)

        @tool(name="memory_proposals_list", annotations=_READ, structured_output=True)
        async def memory_proposals_list() -> dict[str, Any]:
            """List reviewable memory proposals produced from archived chats."""
            return await self._invoke(
                "memory_proposals_list", lambda cp, p: cp.memory_proposals_list(p)
            )

        @tool(name="memory_proposal_resolve", annotations=_WRITE, structured_output=True)
        async def memory_proposal_resolve(
            text: str,
            action: str,
            target: str = "",
        ) -> dict[str, Any]:
            """Accept or reject one proposal selected by a unique text substring."""
            return await self._invoke(
                "memory_proposal_resolve",
                lambda cp, p: cp.memory_proposal_resolve(
                    p, text, action=action, target=target  # type: ignore[arg-type]
                ),
                mutating=True,
            )

        @tool(name="vault_notes_list", annotations=_READ, structured_output=True)
        async def vault_notes_list(limit: int = 100) -> dict[str, Any]:
            """List markdown note paths in the active workspace vault."""
            return await self._invoke("vault_notes_list", lambda cp, p: cp.vault_notes_list(p, limit))

        @tool(name="vault_search", annotations=_READ, structured_output=True)
        async def vault_search(query: str, limit: int = 10) -> dict[str, Any]:
            """Full-text search the active workspace vault."""
            return await self._invoke("vault_search", lambda cp, p: cp.vault_search(p, query, limit))

        @tool(name="vault_note_read", annotations=_READ, structured_output=True)
        async def vault_note_read(path: str) -> dict[str, Any]:
            """Read one vault-relative markdown note."""
            return await self._invoke("vault_note_read", lambda cp, p: cp.vault_note_read(p, path))

        @tool(name="vault_note_write", annotations=_WRITE, structured_output=True)
        async def vault_note_write(path: str, content: str) -> dict[str, Any]:
            """Write one vault-relative markdown note."""
            return await self._invoke("vault_note_write", lambda cp, p: cp.vault_note_write(p, path, content), mutating=True)

        @tool(name="vault_index_refresh", annotations=_WRITE, structured_output=True)
        async def vault_index_refresh() -> dict[str, Any]:
            """Rebuild the active vault's markdown and FTS indexes."""
            return await self._invoke("vault_index_refresh", lambda cp, p: cp.vault_index_refresh(p), mutating=True)

        @tool(name="vault_lint", annotations=_READ, structured_output=True)
        async def vault_lint_tool() -> dict[str, Any]:
            """Check the active vault for broken links, orphans, and near-duplicates."""
            return await self._invoke("vault_lint", lambda cp, p: cp.vault_lint(p))

        @tool(name="projects_list", annotations=_READ, structured_output=True)
        async def projects_list(include_completed: bool = False) -> dict[str, Any]:
            """List projects in the active workspace."""
            return await self._invoke("projects_list", lambda cp, p: cp.projects_list(p, include_completed))

        @tool(name="project_get", annotations=_READ, structured_output=True)
        async def project_get(project_id: str) -> dict[str, Any]:
            """Get one project by ID within the active workspace."""
            return await self._invoke("project_get", lambda cp, p: cp.project_get(p, project_id))

        @tool(name="project_create", annotations=_WRITE, structured_output=True)
        async def project_create(name: str, context: str = "") -> dict[str, Any]:
            """Create a project in the active workspace."""
            return await self._invoke("project_create", lambda cp, p: cp.project_create(p, name, context), mutating=True)

        @tool(name="project_update", annotations=_WRITE, structured_output=True)
        async def project_update(
            project_id: str,
            name: str | None = None,
            context: str | None = None,
            vault_folder: str | None = None,
        ) -> dict[str, Any]:
            """Update project metadata or its safe vault-folder binding."""
            return await self._invoke(
                "project_update",
                lambda cp, p: cp.project_update(
                    p, project_id, name=name, context=context, vault_folder=vault_folder
                ),
                mutating=True,
            )

        @tool(name="project_complete", annotations=_DESTRUCTIVE, structured_output=True)
        async def project_complete(project_id: str) -> dict[str, Any]:
            """Move a vault-backed project to completed and archive its active project record."""
            return await self._invoke("project_complete", lambda cp, p: cp.project_complete(p, project_id), mutating=True)

        @tool(name="project_restore", annotations=_WRITE, structured_output=True)
        async def project_restore(stem: str) -> dict[str, Any]:
            """Restore a completed vault project into the active workspace."""
            return await self._invoke("project_restore", lambda cp, p: cp.project_restore(p, stem), mutating=True)

        @tool(name="project_delete", annotations=_DESTRUCTIVE, structured_output=True)
        async def project_delete(project_id: str) -> dict[str, Any]:
            """Delete a non-vault-backed project and its chats."""
            return await self._invoke("project_delete", lambda cp, p: cp.project_delete(p, project_id), mutating=True)

        @tool(name="project_files_list", annotations=_READ, structured_output=True)
        async def project_files_list(project_id: str) -> dict[str, Any]:
            """List files inside a project's vault folder."""
            return await self._invoke("project_files_list", lambda cp, p: cp.project_files_list(p, project_id))

        @tool(name="chats_list", annotations=_READ, structured_output=True)
        async def chats_list(project_id: str = "") -> dict[str, Any]:
            """List active and archived chats in the active workspace or one project."""
            return await self._invoke("chats_list", lambda cp, p: cp.chats_list(p, project_id))

        @tool(name="chat_get", annotations=_READ, structured_output=True)
        async def chat_get(chat_id: str) -> dict[str, Any]:
            """Get one chat by ID within the active workspace."""
            return await self._invoke("chat_get", lambda cp, p: cp.chat_get(p, chat_id))

        @tool(name="chat_create", annotations=_WRITE, structured_output=True)
        async def chat_create(
            project_id: str,
            title: str = "New Chat",
            provider: str | None = None,
            model: str | None = None,
            mode: str | None = None,
            control_surface: str | None = None,
        ) -> dict[str, Any]:
            """Create a fresh chat in a project."""
            return await self._invoke(
                "chat_create",
                lambda cp, p: cp.chat_create(
                    p,
                    project_id,
                    title=title,
                    provider=provider,
                    model=model,
                    mode=mode,
                    control_surface=control_surface,  # type: ignore[arg-type]
                ),
                mutating=True,
            )

        @tool(name="chat_update", annotations=_WRITE, structured_output=True)
        async def chat_update(
            chat_id: str,
            title: str | None = None,
            provider: str | None = None,
            model: str | None = None,
            mode: str | None = None,
            thinking_level: str | None = None,
            project_id: str | None = None,
            model_bucket: str | None = None,
            control_surface: str | None = None,
        ) -> dict[str, Any]:
            """Update chat metadata and same-backend model settings."""
            return await self._invoke(
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
                    model_bucket=model_bucket,
                    control_surface=control_surface,
                ),
                mutating=True,
            )

        @tool(name="chat_send", annotations=_WRITE, structured_output=True)
        async def chat_send(chat_id: str, prompt: str) -> dict[str, Any]:
            """Start or queue a user turn in another Ciaobot chat."""
            return await self._invoke("chat_send", lambda cp, p: cp.chat_send(p, chat_id, prompt), mutating=True)

        @tool(name="chat_continue", annotations=_WRITE, structured_output=True)
        async def chat_continue(chat_id: str) -> dict[str, Any]:
            """Continue an archived chat as a new active chat."""
            return await self._invoke("chat_continue", lambda cp, p: cp.chat_continue(p, chat_id), mutating=True)

        @tool(name="chat_retry", annotations=_WRITE, structured_output=True)
        async def chat_retry(chat_id: str) -> dict[str, Any]:
            """Run a pending provider-limit retry immediately."""
            return await self._invoke("chat_retry", lambda cp, p: cp.chat_retry(p, chat_id), mutating=True)

        @tool(name="chat_retry_update", annotations=_WRITE, structured_output=True)
        async def chat_retry_update(
            chat_id: str,
            action: str = "try_now",
            prompt: str = "",
        ) -> dict[str, Any]:
            """Set, stop, or immediately try a deferred provider-limit retry."""
            return await self._invoke(
                "chat_retry_update",
                lambda cp, p: cp.chat_retry_update(
                    p, chat_id, action=action, prompt=prompt  # type: ignore[arg-type]
                ),
                mutating=True,
            )

        @tool(name="chat_new_session", annotations=_WRITE, structured_output=True)
        async def chat_new_session(chat_id: str) -> dict[str, Any]:
            """Clear provider session state while retaining the chat record."""
            return await self._invoke(
                "chat_new_session", lambda cp, p: cp.chat_new_session(p, chat_id), mutating=True
            )

        @tool(name="chat_handover", annotations=_WRITE, structured_output=True)
        async def chat_handover(
            chat_id: str,
            provider: str,
            model: str,
            messages: list[dict[str, Any]] | None = None,
            model_bucket: str = "",
        ) -> dict[str, Any]:
            """Continue a chat on a fresh provider session with optional visible history."""
            return await self._invoke(
                "chat_handover",
                lambda cp, p: cp.chat_handover(
                    p,
                    chat_id,
                    provider=provider,
                    model=model,
                    messages=messages,
                    model_bucket=model_bucket,
                ),
                mutating=True,
            )

        @tool(name="chat_fork", annotations=_WRITE, structured_output=True)
        async def chat_fork(
            chat_id: str,
            messages: list[dict[str, Any]],
            turn_index: int,
        ) -> dict[str, Any]:
            """Create an independent chat from visible history through one turn."""
            return await self._invoke(
                "chat_fork",
                lambda cp, p: cp.chat_fork(
                    p, chat_id, messages=messages, turn_index=turn_index
                ),
                mutating=True,
            )

        @tool(name="chat_archive", annotations=_WRITE, structured_output=True)
        async def chat_archive(chat_id: str) -> dict[str, Any]:
            """Archive a chat to the vault and trigger normal post-archive processing."""
            return await self._invoke(
                "chat_archive", lambda cp, p: cp.chat_archive(p, chat_id), mutating=True
            )

        @tool(name="chat_delete", annotations=_DESTRUCTIVE, structured_output=True)
        async def chat_delete(chat_id: str) -> dict[str, Any]:
            """Delete a chat; deleting the current caller is deferred until the turn finishes."""
            return await self._invoke(
                "chat_delete", lambda cp, p: cp.chat_delete(p, chat_id), mutating=True
            )

        @tool(name="chat_mark_read", annotations=_WRITE, structured_output=True)
        async def chat_mark_read(chat_id: str) -> dict[str, Any]:
            """Mark a chat read across connected PWA clients."""
            return await self._invoke(
                "chat_mark_read", lambda cp, p: cp.chat_mark_read(p, chat_id), mutating=True
            )

        @tool(name="chat_stop", annotations=_DESTRUCTIVE, structured_output=True)
        async def chat_stop(chat_id: str) -> dict[str, Any]:
            """Stop another chat's active provider turn; the current caller cannot stop itself."""
            return await self._invoke(
                "chat_stop", lambda cp, p: cp.chat_stop(p, chat_id), mutating=True
            )

        @tool(name="consultations_list", annotations=_READ, structured_output=True)
        async def consultations_list(chat_id: str = "") -> dict[str, Any]:
            """List cross-provider consultations attached to a chat."""
            return await self._invoke(
                "consultations_list", lambda cp, p: cp.consultations_list(p, chat_id)
            )

        @tool(name="consultation_start", annotations=_WRITE, structured_output=True)
        async def consultation_start(
            provider: str,
            model: str,
            message: str,
            chat_id: str = "",
            model_bucket: str = "",
            user_authorized: bool = False,
        ) -> dict[str, Any]:
            """Start a bounded cross-provider consultation and return its first reply."""
            return await self._invoke(
                "consultation_start",
                lambda cp, p: cp.consultation_start(
                    p,
                    provider=provider,
                    model=model,
                    message=message,
                    chat_id=chat_id,
                    model_bucket=model_bucket,
                    user_authorized=user_authorized,
                ),
                mutating=True,
            )

        @tool(name="consultation_send", annotations=_WRITE, structured_output=True)
        async def consultation_send(
            subchat_id: str,
            message: str,
            user_authorized: bool = False,
        ) -> dict[str, Any]:
            """Send a follow-up message to a provider consultation."""
            return await self._invoke(
                "consultation_send",
                lambda cp, p: cp.consultation_send(
                    p, subchat_id, message, user_authorized=user_authorized
                ),
                mutating=True,
            )

        @tool(name="consultation_events", annotations=_READ, structured_output=True)
        async def consultation_events(subchat_id: str) -> dict[str, Any]:
            """Read the event transcript for a provider consultation."""
            return await self._invoke(
                "consultation_events", lambda cp, p: cp.consultation_events(p, subchat_id)
            )

        @tool(name="consultation_close", annotations=_WRITE, structured_output=True)
        async def consultation_close(subchat_id: str) -> dict[str, Any]:
            """Close a provider consultation that no longer needs follow-up."""
            return await self._invoke(
                "consultation_close", lambda cp, p: cp.consultation_close(p, subchat_id), mutating=True
            )

        @tool(name="consultation_cancel", annotations=_DESTRUCTIVE, structured_output=True)
        async def consultation_cancel(subchat_id: str) -> dict[str, Any]:
            """Cancel active work in a provider consultation."""
            return await self._invoke(
                "consultation_cancel", lambda cp, p: cp.consultation_cancel(p, subchat_id), mutating=True
            )

        @tool(name="consultation_extend", annotations=_WRITE, structured_output=True)
        async def consultation_extend(
            subchat_id: str,
            user_authorized: bool = False,
        ) -> dict[str, Any]:
            """Extend consultation quotas only after explicit user authorization."""
            return await self._invoke(
                "consultation_extend",
                lambda cp, p: cp.consultation_extend(
                    p, subchat_id, user_authorized=user_authorized
                ),
                mutating=True,
            )

        @tool(name="schedules_list", annotations=_READ, structured_output=True)
        async def schedules_list() -> dict[str, Any]:
            """List schedules in the active workspace with their next run."""
            return await self._invoke("schedules_list", lambda cp, p: cp.schedules_list(p))

        @tool(name="schedule_preview", annotations=_READ, structured_output=True)
        async def schedule_preview(
            prompt: str,
            daily_time: str = "09:00",
            timezone: str = "UTC",
            frequency: str = "weekly",
            days_of_week: list[str] | None = None,
            day_of_month: int | None = None,
            run_at_date: str | None = None,
            project_id: str | None = None,
            chat_id: str | None = None,
            title: str = "",
            provider: str = "",
            model: str = "",
            archive_policy: str = "manual",
        ) -> dict[str, Any]:
            """Validate a schedule and compute its next run without saving it."""
            values = {key: value for key, value in locals().items() if key != "self"}
            return await self._invoke("schedule_preview", lambda cp, p: cp.schedule_preview(p, **values))

        @tool(name="schedule_create", annotations=_WRITE, structured_output=True)
        async def schedule_create(
            prompt: str,
            daily_time: str = "09:00",
            timezone: str = "UTC",
            frequency: str = "weekly",
            days_of_week: list[str] | None = None,
            day_of_month: int | None = None,
            run_at_date: str | None = None,
            project_id: str | None = None,
            chat_id: str | None = None,
            title: str = "",
            description: str = "",
            provider: str = "",
            model: str = "",
            archive_policy: str = "manual",
        ) -> dict[str, Any]:
            """Create a validated Ciaobot schedule without editing runtime JSON."""
            values = {key: value for key, value in locals().items() if key != "self"}
            return await self._invoke("schedule_create", lambda cp, p: cp.schedule_create(p, **values), mutating=True)

        @tool(name="schedule_update", annotations=_WRITE, structured_output=True)
        async def schedule_update(
            schedule_id: str,
            prompt: str | None = None,
            daily_time: str | None = None,
            timezone: str | None = None,
            frequency: str | None = None,
            days_of_week: list[str] | None = None,
            day_of_month: int | None = None,
            run_at_date: str | None = None,
            project_id: str | None = None,
            chat_id: str | None = None,
            title: str | None = None,
            description: str | None = None,
            provider: str | None = None,
            model: str | None = None,
            archive_policy: str | None = None,
        ) -> dict[str, Any]:
            """Update a Ciaobot schedule through validated fields."""
            values = {
                key: value
                for key, value in locals().items()
                if key not in {"self", "schedule_id"}
            }
            return await self._invoke(
                "schedule_update",
                lambda cp, p: cp.schedule_update(p, schedule_id, **values),
                mutating=True,
            )

        @tool(name="schedule_pause", annotations=_WRITE, structured_output=True)
        async def schedule_pause(schedule_id: str) -> dict[str, Any]:
            """Pause a schedule without deleting it."""
            return await self._invoke("schedule_pause", lambda cp, p: cp.schedule_pause(p, schedule_id), mutating=True)

        @tool(name="schedule_resume", annotations=_WRITE, structured_output=True)
        async def schedule_resume(schedule_id: str) -> dict[str, Any]:
            """Resume a paused schedule."""
            return await self._invoke("schedule_resume", lambda cp, p: cp.schedule_resume(p, schedule_id), mutating=True)

        @tool(name="schedule_run", annotations=_WRITE, structured_output=True)
        async def schedule_run(schedule_id: str) -> dict[str, Any]:
            """Dispatch a schedule immediately through the normal chat pipeline."""
            return await self._invoke("schedule_run", lambda cp, p: cp.schedule_run(p, schedule_id), mutating=True)

        @tool(name="schedule_delete", annotations=_DESTRUCTIVE, structured_output=True)
        async def schedule_delete(schedule_id: str) -> dict[str, Any]:
            """Delete a removable user schedule."""
            return await self._invoke("schedule_delete", lambda cp, p: cp.schedule_delete(p, schedule_id), mutating=True)

        @tool(name="loops_list", annotations=_READ, structured_output=True)
        async def loops_list() -> dict[str, Any]:
            """List in-chat loops in the active workspace."""
            return await self._invoke("loops_list", lambda cp, p: cp.loops_list(p))

        @tool(name="loop_create", annotations=_WRITE, structured_output=True)
        async def loop_create(
            chat_id: str,
            prompt: str,
            interval_minutes: int = 10,
            title: str = "",
            autostart: bool = False,
        ) -> dict[str, Any]:
            """Create an interval loop bound to an existing chat."""
            return await self._invoke(
                "loop_create",
                lambda cp, p: cp.loop_create(p, chat_id, prompt, interval_minutes, title, autostart),
                mutating=True,
            )

        @tool(name="loop_update", annotations=_WRITE, structured_output=True)
        async def loop_update(
            loop_id: str,
            prompt: str | None = None,
            chat_id: str | None = None,
            interval_minutes: int | None = None,
            title: str | None = None,
            autostart: bool | None = None,
        ) -> dict[str, Any]:
            """Update an in-chat loop."""
            values = {
                key: value
                for key, value in locals().items()
                if key not in {"self", "loop_id"}
            }
            return await self._invoke("loop_update", lambda cp, p: cp.loop_update(p, loop_id, **values), mutating=True)

        @tool(name="loop_start", annotations=_WRITE, structured_output=True)
        async def loop_start(loop_id: str) -> dict[str, Any]:
            """Start a loop's runtime cadence."""
            return await self._invoke("loop_start", lambda cp, p: cp.loop_start(p, loop_id), mutating=True)

        @tool(name="loop_stop", annotations=_WRITE, structured_output=True)
        async def loop_stop(loop_id: str) -> dict[str, Any]:
            """Stop a loop's runtime cadence without deleting it."""
            return await self._invoke("loop_stop", lambda cp, p: cp.loop_stop(p, loop_id), mutating=True)

        @tool(name="loop_run", annotations=_WRITE, structured_output=True)
        async def loop_run(loop_id: str) -> dict[str, Any]:
            """Run one loop iteration immediately."""
            return await self._invoke("loop_run", lambda cp, p: cp.loop_run(p, loop_id), mutating=True)

        @tool(name="loop_delete", annotations=_DESTRUCTIVE, structured_output=True)
        async def loop_delete(loop_id: str) -> dict[str, Any]:
            """Delete an in-chat loop."""
            return await self._invoke("loop_delete", lambda cp, p: cp.loop_delete(p, loop_id), mutating=True)

        @tool(name="workspace_file_read", annotations=_READ, structured_output=True)
        async def workspace_file_read(path: str) -> dict[str, Any]:
            """Read a workspace-relative UTF-8 text file up to 2 MiB."""
            return await self._invoke("workspace_file_read", lambda cp, p: cp.workspace_file_read(p, path))

        @tool(name="workspace_file_write", annotations=_WRITE, structured_output=True)
        async def workspace_file_write(path: str, content: str) -> dict[str, Any]:
            """Write a workspace-relative UTF-8 file; runtime stores are forbidden."""
            return await self._invoke("workspace_file_write", lambda cp, p: cp.workspace_file_write(p, path, content), mutating=True)

        @tool(name="file_history_list", annotations=_READ, structured_output=True)
        async def file_history_list(chat_id: str, file_path: str) -> dict[str, Any]:
            """List captured file snapshots for a chat and file path."""
            return await self._invoke(
                "file_history_list",
                lambda cp, p: cp.file_history_list(p, chat_id, file_path),
            )

        @tool(name="file_snapshot_read", annotations=_READ, structured_output=True)
        async def file_snapshot_read(
            chat_id: str, file_path: str, seq: int
        ) -> dict[str, Any]:
            """Read one UTF-8 file snapshot from append-only chat history."""
            return await self._invoke(
                "file_snapshot_read",
                lambda cp, p: cp.file_snapshot_read(p, chat_id, file_path, seq),
            )

        @tool(name="file_snapshot_restore", annotations=_DESTRUCTIVE, structured_output=True)
        async def file_snapshot_restore(
            chat_id: str, file_path: str, seq: int
        ) -> dict[str, Any]:
            """Restore a workspace-contained snapshot and capture the restoration as a new snapshot."""
            return await self._invoke(
                "file_snapshot_restore",
                lambda cp, p: cp.file_snapshot_restore(p, chat_id, file_path, seq),
                mutating=True,
            )

        @tool(name="agent_context_get", annotations=_READ, structured_output=True)
        async def agent_context_get() -> dict[str, Any]:
            """List context assets, subagents, commands, and workspace health."""
            return await self._invoke("agent_context_get", lambda cp, p: cp.agent_context_get(p))

        @tool(name="workspace_health_get", annotations=_READ, structured_output=True)
        async def workspace_health_get() -> dict[str, Any]:
            """Check canonical agent assets and generated provider mirrors."""
            return await self._invoke("workspace_health_get", lambda cp, p: cp.workspace_health_get(p))

        @tool(name="workspace_health_fix", annotations=_WRITE, structured_output=True)
        async def workspace_health_fix() -> dict[str, Any]:
            """Repair missing workspace scaffolding and provider asset mirrors."""
            return await self._invoke("workspace_health_fix", lambda cp, p: cp.workspace_health_fix(p), mutating=True)

        @tool(name="skills_list", annotations=_READ, structured_output=True)
        async def skills_list() -> dict[str, Any]:
            """List stock, custom, and installed skills with provider availability."""
            return await self._invoke("skills_list", lambda cp, p: cp.skills_list(p))

        @tool(name="skills_sync", annotations=_WRITE, structured_output=True)
        async def skills_sync(refresh_upstream: bool = False) -> dict[str, Any]:
            """Synchronize canonical skills, commands, and subagents to provider mirrors."""
            return await self._invoke("skills_sync", lambda cp, p: cp.skills_sync(p, refresh_upstream), mutating=True)

        @tool(name="local_session_status", annotations=_READ, structured_output=True)
        async def local_session_status() -> dict[str, Any]:
            """Return local git-session synchronization status."""
            return await self._invoke("local_session_status", lambda cp, p: cp.local_session_status(p))

        @tool(name="local_session_preflight", annotations=_READ, structured_output=True)
        async def local_session_preflight() -> dict[str, Any]:
            """Run the local-session safety preflight without committing or pushing."""
            return await self._invoke("local_session_preflight", lambda cp, p: cp.local_session_preflight(p))

        @tool(name="local_session_handback", annotations=_DESTRUCTIVE, structured_output=True)
        async def local_session_handback(confirm_warnings: bool = False) -> dict[str, Any]:
            """Commit and synchronize the current branch after secrets preflight."""
            return await self._invoke(
                "local_session_handback",
                lambda cp, p: cp.local_session_handback(
                    p, confirm_warnings=confirm_warnings
                ),
                mutating=True,
            )

        @tool(name="local_session_resync", annotations=_WRITE, structured_output=True)
        async def local_session_resync() -> dict[str, Any]:
            """Finish local synchronization after an interactive conflict resolution."""
            return await self._invoke(
                "local_session_resync", lambda cp, p: cp.local_session_resync(p), mutating=True
            )

        @tool(name="package_status_get", annotations=_READ, structured_output=True)
        async def package_status_get() -> dict[str, Any]:
            """Return installed package version and best-effort update availability."""
            return await self._invoke(
                "package_status_get", lambda cp, p: cp.package_status_get(p)
            )

        @tool(name="lifecycle_actions_list", annotations=_READ, structured_output=True)
        async def lifecycle_actions_list() -> dict[str, Any]:
            """List this managed process's deferred restart/update actions."""
            return await self._invoke(
                "lifecycle_actions_list", lambda cp, p: cp.lifecycle_actions_list(p)
            )

        @tool(name="lifecycle_action_request", annotations=_DESTRUCTIVE, structured_output=True)
        async def lifecycle_action_request(
            action: str,
            confirmed: bool = False,
        ) -> dict[str, Any]:
            """Queue a confirmed restart or package update after the current turn drains."""
            return await self._invoke(
                "lifecycle_action_request",
                lambda cp, p: cp.lifecycle_action_request(
                    p, action=action, confirmed=confirmed  # type: ignore[arg-type]
                ),
                mutating=True,
            )


async def mcp_status_endpoint(request: Request) -> JSONResponse:
    service = getattr(request.app.state, "mcp_service", None)
    if service is None:
        return JSONResponse({"enabled": False, "bound": False, "tool_count": 0})
    return JSONResponse(service.status())
