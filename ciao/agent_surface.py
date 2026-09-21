"""Agent CLI surface: Ciaobot operations over an authenticated loopback route.

The ``ciao`` command line running inside a managed provider shell posts
``{op, arguments}`` to ``POST /agent/v1/{op}`` with the chat's bearer token.
The dispatcher verifies the token with the same :class:`McpSessionRegistry`
the MCP adapter uses, then runs the *same* operation the MCP adapter serves —
resolved from the shared module-level operation table in ``ciao.mcp_server``
and validated through the same ``Tool.from_function`` metadata — inside the
same auth context, so every control-plane guard (workspace confinement,
plan-mode gate, unattended checks), the response envelope, argument validation
and telemetry are shared byte-for-byte. The only observable difference is
``surface: "cli"`` in ``mcp_tool_calls.jsonl``.

Identity never comes from arguments: ``--chat`` and friends are ordinary tool
parameters authorised by the control plane against the token's principal.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Bearer capability for the agent CLI, injected into the managed provider's
#: foreground shell only. Background runs strip it (see ``ciao/background.py``).
AGENT_TOKEN_ENV = "CIAO_AGENT_TOKEN"
#: Base URL the CLI posts to; ``{op}`` is appended.
AGENT_URL_ENV = "CIAO_AGENT_URL"
#: Per-chat surface selection for the MCP-versus-CLI migration:
#: ``{"<chat_id>": "cli"}`` under the runtime dir. Absent chat means ``mcp``:
#: the default surface stays MCP so the harness argv allow-rules (which are
#: bypassable through shell operators — ``ciao x >/dev/null; <cmd>``) only
#: activate for a chat explicitly pinned to ``cli``, never by default. There is
#: deliberately no wildcard — the file lives in ``.runtime``, which the model's
#: shell can write, so one entry must never flip every chat. Migrated
#: operations are reachable on both surfaces because the CLI token+URL are
#: injected regardless of surface (see ``build_agent_request``).
SURFACE_FILE_NAME = "agent_surface.json"
SURFACES = ("mcp", "cli")


def surface_for_chat(runtime_dir: Path, chat_id: str, default: str = "mcp") -> str:
    """Return ``"cli"`` or ``"mcp"`` for ``chat_id`` from the surface file."""
    try:
        raw = json.loads((runtime_dir / SURFACE_FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    if not isinstance(raw, dict):
        return default
    value = str(raw.get(chat_id) or default)
    return value if value in SURFACES else default


def _envelope_error(code: str, message: str, *, retryable: bool = False) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message, "retryable": retryable}}


class AgentDispatcher:
    """Run one registered control-plane operation for a bearer token."""

    def __init__(self, service: Any) -> None:
        self._service = service

    def operations(self) -> tuple[str, ...]:
        """Every operation the dispatcher can run, independent of the MCP set."""
        return tuple(sorted(self._service.operation_table))

    async def dispatch(self, token: str, op: str, arguments: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Return ``(http_status, envelope)``. Never raises for caller errors."""
        # Imported here: ``ciao.control_plane`` imports the chat manager, which
        # imports this module for the env constants, and the CLI must stay light.
        from mcp.server.auth.middleware.auth_context import auth_context_var
        from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

        from ciao.control_plane import McpPrincipal

        service = self._service
        if not token:
            return 401, _envelope_error("unauthorized", "A Ciaobot agent token is required.")
        access = await service.registry.verify_token(token)
        if access is None:
            return 401, _envelope_error("unauthorized", "The agent token is invalid or expired.")
        # Same gate the MCP mount applies through RequireAuthMiddleware
        # (``required_scopes=["ciaobot"]``); every token the registry mints
        # carries it today, so this only matters if a second token type ever
        # appears, and then it must not become a bypass.
        if "ciaobot" not in (access.scopes or []):
            return 403, _envelope_error("forbidden", "The agent token lacks the ciaobot scope.")
        operation = service.operation_table.get(op)
        if operation is None:
            return 404, _envelope_error("unknown_operation", f"Unknown Ciaobot operation '{op}'.")
        if not isinstance(arguments, dict):
            return 400, _envelope_error("invalid_request", "Arguments must be a JSON object.")

        started = time.perf_counter()
        auth_token = auth_context_var.set(AuthenticatedUser(access))
        surface_token = service.surface_var.set("cli")
        # Build the FastMCP Tool from the shared operation entry so argument
        # validation (pydantic) is byte-for-byte the schema the MCP adapter
        # serves for the same operation — the two surfaces cannot drift. This is
        # schema construction from the operation table, independent of the
        # caller's arguments; a failure here is a server fault, not a client 400,
        # so it stays outside the argument-validation try below.
        try:
            tool = service.tool_for(operation)
        except Exception:  # noqa: BLE001 - internal fault, not a caller error
            logger.exception("agent surface: failed to build tool for %s", op)
            return 500, _envelope_error("internal_error", "Ciaobot could not build the operation.")
        try:
            result = await tool.run(arguments)
        except Exception as exc:  # noqa: BLE001 - argument validation is the tool boundary
            # ``tool.run`` validates arguments with pydantic before the tool
            # body (and therefore ``_invoke`` and its telemetry) ever runs, so
            # a bad flag would otherwise leave no record. Mirror ``_invoke``.
            principal = McpPrincipal.from_claims(access.claims or {})
            service._record_tool_call(
                name=op,
                principal=principal,
                status="error",
                error_code="invalid_request",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            # First line only: pydantic's full text echoes ``input_value=…``,
            # which would put prompt or memory text into the server log.
            logger.info("agent surface: %s rejected arguments: %s", op, _validation_message(exc))
            return 400, _envelope_error("invalid_request", _validation_message(exc))
        finally:
            service.surface_var.reset(surface_token)
            auth_context_var.reset(auth_token)
        if isinstance(result, dict):
            status = 200 if result.get("ok", True) else 422
            return status, result
        return 200, {"ok": True, "data": result}


def _validation_message(exc: Exception) -> str:
    text = str(exc).strip().splitlines()
    return text[0][:300] if text else exc.__class__.__name__
