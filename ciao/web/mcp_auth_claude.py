"""The Claude half of the ``mcp_auth`` needs-you card.

The SDK session has no ``/mcp`` panel, so a Claude chat's sign-in runs the
CLI's own non-interactive login, ``claude mcp login --no-browser <server>``,
as a helper terminal for the chat. The engine scrapes the authorize URL from
its output, reconnects the server on the live session when the helper exits 0,
and ends the helper in every terminal state. The argv is constants plus a
validated server name; no model text reaches the command line.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Mapping
from pathlib import Path

from ciao.chat_actions import ActionState, ChatAction
from ciao.providers.claude import ClaudeProvider, get_bundled_claude_path, is_claudeai_connector
from ciao.terminal_scrape import first_https_url, last_error_line, strip_terminal_controls
from ciao.terminal_sessions import TerminalSessionError
from ciao.tool_path import resolve_tool
from ciao.web.chat_actions_base import ActionContext, ActionHandler

__all__ = ["ClaudeMcpAuthFlow", "claude_login_argv", "valid_server_name"]

logger = logging.getLogger(__name__)

CLAUDE_MCP_LOGIN_ARGS: tuple[str, ...] = ("mcp", "login", "--no-browser")

_NAME_MAX = 128
_CONTROL_CHARS = re.compile("[\x00-\x1f\x7f-\x9f]")

# Error copy. The engine composes these; no model text and no terminal output.
NOT_RUNNING = "This chat's Claude session is not running. Send any message in the chat, then try again."
NOT_CONFIGURED = "That MCP server is not configured for this chat."
BAD_NAME = "That server name cannot be signed in from here."
NO_CLI = "Claude Code CLI not found."
CAPACITY = "Too many terminal sessions are open. Close one in the terminal pane and try again."
NO_TERMINAL = "The sign-in terminal could not be started."
CLOSED = "The sign-in terminal was closed before sign-in finished."
CLAUDEAI_CONNECTOR = "This is a claude.ai connector. Connect it on claude.ai in Settings, Connectors."
NOT_CONNECTED = "Signed in, but the server did not reconnect. Ask the agent to run ciao chat reload."


def valid_server_name(name: object) -> bool:
    """A server name the engine may pass as an argv element: text, bounded, never option-like."""
    if not isinstance(name, str) or not 1 <= len(name) <= _NAME_MAX:
        return False
    if name.startswith("-") or name != name.strip():
        return False
    return _CONTROL_CHARS.search(name) is None


def claude_login_argv(binary: str, server: str) -> list[str]:
    return [binary, *CLAUDE_MCP_LOGIN_ARGS, server]


def _claude_binary() -> str | None:
    try:
        return get_bundled_claude_path() or resolve_tool("claude")
    except OSError:
        return None


class ClaudeMcpAuthFlow(ActionHandler):
    """The Claude MCP sign-in: a helper terminal running ``claude mcp login``."""

    def _set(
        self,
        ctx: ActionContext,
        action: ChatAction,
        state: ActionState,
        *,
        error: str = "",
        params_update: dict[str, object] | None = None,
    ) -> None:
        ctx.pcm.update_action(
            ctx.chat.chat_id, action.id, state=state, params_update=params_update, error=error
        )
        logger.info("mcp_auth claude %s: %s", action.id, state)

    async def _end(self, ctx: ActionContext, session_id: str) -> None:
        if ctx.terminals is None:
            return
        try:
            await ctx.terminals.end(session_id)
        except TerminalSessionError:
            pass

    async def start(self, ctx: ActionContext, action: ChatAction, body: Mapping[str, object]) -> None:
        server = action.params.get("server", "")
        if not valid_server_name(server):
            self._set(ctx, action, "failed", error=BAD_NAME)
            return
        if is_claudeai_connector(server):
            # Connected on claude.ai, not with ``claude mcp login``.
            self._set(ctx, action, "failed", error=CLAUDEAI_CONNECTOR)
            return
        provider = ctx.provider
        if not isinstance(provider, ClaudeProvider) or not provider._connected:
            self._set(ctx, action, "failed", error=NOT_RUNNING)
            return
        try:
            states = await provider.read_mcp_status()
        except Exception:  # noqa: BLE001 - a failed status read is the same refusal as no session
            logger.debug("Claude MCP status read failed for chat %s", ctx.chat.chat_id, exc_info=True)
            self._set(ctx, action, "failed", error=NOT_RUNNING)
            return
        if server not in states:
            self._set(ctx, action, "failed", error=NOT_CONFIGURED)
            return
        if states[server] == "connected":
            self._set(ctx, action, "done")
            return
        binary = _claude_binary()
        if binary is None:
            self._set(ctx, action, "failed", error=NO_CLI)
            return
        if ctx.terminals is None:
            self._set(ctx, action, "failed", error=NO_TERMINAL)
            return
        try:
            info = await ctx.terminals.create(
                Path(provider.workspace_root),
                "chat",
                ctx.chat.chat_id,
                argv=claude_login_argv(binary, server),
            )
        except TerminalSessionError as exc:
            self._set(ctx, action, "failed", error=CAPACITY if exc.reason == "capacity" else NO_TERMINAL)
            return
        if ctx.pcm.get_action(ctx.chat.chat_id, action.id) is None:
            # Dismissed while the helper was starting: nothing will poll or cancel it, so end it here.
            await self._end(ctx, info.session_id)
            return
        self._set(ctx, action, "working", params_update={"session_id": info.session_id})

    async def poll(self, ctx: ActionContext, action: ChatAction) -> None:
        session_id = action.params.get("session_id")
        if not session_id:
            return
        server = action.params.get("server", "")
        if ctx.terminals is None:
            self._set(ctx, action, "failed", error=CLOSED)
            return
        try:
            info = ctx.terminals.get(session_id)
            raw = ctx.terminals.helper_output(session_id)
        except TerminalSessionError:
            self._set(ctx, action, "failed", error=CLOSED)
            return
        if "url" not in action.params:
            url = first_https_url(raw)
            if url:
                self._set(ctx, action, "waiting", params_update={"url": url})
        if not info.exited:
            return
        if info.exit_code == 0:
            ok = True
            provider = ctx.provider
            if isinstance(provider, ClaudeProvider):
                try:
                    reconnected = await provider.reconnect_mcp_server(server)
                except Exception:  # noqa: BLE001 - the status read below decides
                    logger.debug("Claude MCP reconnect failed for chat %s", ctx.chat.chat_id, exc_info=True)
                    reconnected = True
                if reconnected:
                    try:
                        states = await provider.read_mcp_status()
                    except Exception:  # noqa: BLE001
                        states = {}
                    ok = states.get(server) == "connected"
            # With no live session the next spawn reads the new token, so the sign-in is done.
            self._set(ctx, action, "done" if ok else "failed", error="" if ok else NOT_CONNECTED)
        else:
            error = last_error_line(strip_terminal_controls(raw))
            error = error or f"Sign-in ended with exit code {info.exit_code}."
            self._set(ctx, action, "failed", error=error)
        await self._end(ctx, session_id)

    async def cancel(self, ctx: ActionContext, action: ChatAction) -> None:
        session_id = action.params.get("session_id")
        if session_id:
            await self._end(ctx, session_id)

    def wake_prompt(self, action: ChatAction) -> str:
        return (
            f"[Ciaobot] The user signed in to the MCP server {json.dumps(action.params.get('server', ''))}. "
            "Its tools are available in this chat now; continue the task you were working on."
        )

