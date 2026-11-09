"""The needs-you runtime: the handler registry, the poll loop, cancel and detection.

The chat-action store (``ProjectChatManager.raise_action`` and friends) holds the
cards. This module decides what happens to a card: how a kind starts, how the
engine watches a sign-in until the server connects or the user gives up, what
Continue wakes, and what turn-end detection raises. The OpenCode sign-in lives
here too, behind ``OpencodeMcpAuthFlow``; all provider HTTP stays in
``ciao/providers/opencode.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ciao.chat_actions import ChatAction, ChatActionError, action_key, action_payload
from ciao.oauth_callback import CallbackTarget, validate_pasted_callback
from ciao.provider_service import ProviderImpl
from ciao.providers.claude import ClaudeProvider, is_claudeai_connector
from ciao.providers.opencode import McpOAuthUnavailable, OpencodeProvider
from ciao.web.chat_actions_base import ActionContext, ActionHandler
from ciao.web.device_code_auth import DeviceCodeFlow
from ciao.web.gws_auth_action import GwsAuthHandler
from ciao.web.mcp_auth_claude import ClaudeMcpAuthFlow
from ciao.web.mcp_toggle import set_mcp_server_enabled

if TYPE_CHECKING:
    from ciao.terminal_sessions import TerminalSessionManager
    from ciao.web.project_chats import ProjectChatManager

__all__ = [
    "ACTION_HANDLERS",
    "ActionContext",
    "ActionHandler",
    "ChatActionsRuntime",
    "MCP_AUTH_FLOWS",
    "McpAuthHandler",
    "OpencodeMcpAuthFlow",
    "SYSTEM_SETTING_URLS",
    "SystemSettingHandler",
]

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 2.0
POLL_DEADLINE_S = 600.0
DETECT_TIMEOUT_S = 3.0
START_TIMEOUT_S = 20.0
CANCEL_TIMEOUT_S = 5.0
CALLBACK_TIMEOUT_S = 5.0
TIMEOUT_ERROR = "Timed out waiting for sign-in"
UNREACHABLE_ERROR = "Could not reach the sign-in listener on this computer. Start the sign-in again."
REFUSED_ERROR = "The sign-in listener did not accept that address. Start the sign-in again."

# Error copy. The engine composes these; the PWA shows them as they are.
NO_SIGN_IN_ERROR = "OpenCode does not offer a sign-in for this server. Sign in with `opencode mcp auth` instead."
CODE_MODE_ERROR = "This server asks for a pasted sign-in code. Sign in with `opencode mcp auth` instead."
OPENCODE_MISSING_ERROR = "OpenCode is not installed, so the sign-in cannot start. Install OpenCode, then try again."
SERVER_START_FAILED_ERROR = "OpenCode could not start for this chat. Try again."
START_FAILED_ERROR = "OpenCode could not start the sign-in. Try again."
BAD_URL_ERROR = "OpenCode returned a sign-in address Ciao cannot open."
STOPPED_ERROR = "This chat's OpenCode server stopped before the sign-in finished. Try again."
EXPIRED_ERROR = "The sign-in page expired. Try again."
FAILED_DEFAULT_ERROR = "The sign-in did not finish."


async def replay_callback(target: CallbackTarget) -> int:
    """GET the pending flow's own loopback listener once. Returns the HTTP status, 0 when unreachable.

    A bare transport, not an AsyncClient: the client logs every request URL at INFO
    (the code would reach the server log), follows redirects when asked and reads
    proxy settings from the environment. A transport does none of the three.
    """
    timeout = httpx.Timeout(CALLBACK_TIMEOUT_S).as_dict()
    request = httpx.Request("GET", target.replay_url(), extensions={"timeout": timeout})
    try:
        async with httpx.AsyncHTTPTransport(retries=0) as transport:
            response = await asyncio.wait_for(
                transport.handle_async_request(request), CALLBACK_TIMEOUT_S
            )
            try:
                return response.status_code
            finally:
                await response.aclose()
    except (httpx.HTTPError, OSError, asyncio.TimeoutError):
        return 0


class McpAuthHandler(ActionHandler):
    """Dispatches an ``mcp_auth`` action to the flow for its provider."""

    poll_states = frozenset({"working", "waiting"})

    def _flow(self, action: ChatAction) -> ActionHandler | None:
        return MCP_AUTH_FLOWS.get(action.params.get("provider", ""))

    async def start(self, ctx: ActionContext, action: ChatAction, body: Mapping[str, object]) -> None:
        # A new sign-in has a new authorize URL, so a paste-back mark from the last attempt no longer applies.
        if action.params.get("callback_used") == "1":
            ctx.pcm.update_action(
                ctx.chat.chat_id, action.id, state=action.state, params_update={"callback_used": "0"}
            )
        await MCP_AUTH_FLOWS[action.params["provider"]].start(ctx, action, body)

    async def poll(self, ctx: ActionContext, action: ChatAction) -> None:
        flow = self._flow(action)
        if flow is not None:
            await flow.poll(ctx, action)

    async def cancel(self, ctx: ActionContext, action: ChatAction) -> None:
        flow = self._flow(action)
        if flow is not None:
            await flow.cancel(ctx, action)

    async def callback(self, ctx: ActionContext, action: ChatAction, url: str) -> None:
        """Replay a pasted sign-in callback to the pending flow's listener, once.

        Steps up to the mark are synchronous, so nothing can interleave between the
        single-use check and the mark. A refusal from ``validate_pasted_callback``
        changes nothing, so a typo can be fixed and sent again.
        """
        authorize = action.params.get("url", "")
        if not authorize:
            raise ChatActionError("invalid_state")
        if action.params.get("callback_used") == "1":
            raise ChatActionError("already_used")
        target = validate_pasted_callback(authorize, url)
        cid = ctx.chat.chat_id
        attempt_url = action.params.get("url", "")
        ctx.pcm.update_action(cid, action.id, state="waiting", params_update={"callback_used": "1"})
        status = await replay_callback(target)
        # The flow's poll may have finished the sign-in, or the user restarted it (a new authorize URL),
        # while the replay was in flight: only fail the same attempt that is still waiting.
        current = ctx.pcm.get_action(cid, action.id)
        if (
            current is not None
            and current.state == "waiting"
            and current.params.get("url", "") == attempt_url
        ):
            if status == 0:
                ctx.pcm.update_action(cid, action.id, state="failed", error=UNREACHABLE_ERROR)
            elif status >= 400:
                ctx.pcm.update_action(cid, action.id, state="failed", error=REFUSED_ERROR)
        logger.info("chat action %s callback replayed (status %d)", action.id, status)

    def wake_prompt(self, action: ChatAction) -> str:
        flow = self._flow(action)
        return flow.wake_prompt(action) if flow is not None else ""


class OpencodeMcpAuthFlow(ActionHandler):
    """The OpenCode MCP sign-in, through OpenCode's V2 integration API."""

    def __init__(self) -> None:
        # Action id -> (the provider that opened it, integration id, attempt id).
        # The provider is kept so a cancel after a lifecycle clear still reaches OpenCode.
        self._attempts: dict[str, tuple[OpencodeProvider, str, str]] = {}

    async def start(self, ctx: ActionContext, action: ChatAction, body: Mapping[str, object]) -> None:
        cid = ctx.chat.chat_id
        ctx.pcm.update_action(cid, action.id, state="working")
        # The idle sweep may have stopped the server since the card was raised: start it.
        try:
            provider = await ctx.pcm.ensure_opencode_server(cid)
        except FileNotFoundError:
            ctx.pcm.update_action(cid, action.id, state="failed", error=OPENCODE_MISSING_ERROR)
            return
        except Exception as exc:  # noqa: BLE001 - any startup failure fails the card
            # The message may carry server stderr; log only the class.
            logger.warning("OpenCode server could not start for chat %s: %s", cid, type(exc).__name__)
            ctx.pcm.update_action(cid, action.id, state="failed", error=SERVER_START_FAILED_ERROR)
            return
        try:
            attempt = await asyncio.wait_for(
                provider.start_mcp_oauth(action.params["server"]), START_TIMEOUT_S
            )
        except McpOAuthUnavailable as exc:
            error = STOPPED_ERROR if exc.reason == "not_running" else NO_SIGN_IN_ERROR
            ctx.pcm.update_action(cid, action.id, state="failed", error=error)
            return
        except (httpx.HTTPError, ValueError, OSError, asyncio.TimeoutError) as exc:
            # The message may carry upstream detail; log only the class.
            logger.warning("OpenCode MCP sign-in could not start for chat %s: %s", cid, type(exc).__name__)
            ctx.pcm.update_action(cid, action.id, state="failed", error=START_FAILED_ERROR)
            return
        if attempt.mode == "code":
            with contextlib.suppress(Exception):
                await provider.cancel_mcp_oauth(attempt.integration_id, attempt.attempt_id)
            ctx.pcm.update_action(cid, action.id, state="failed", error=CODE_MODE_ERROR)
            return
        if ctx.pcm.get_action(cid, action.id) is None:
            # Dismissed while the attempt was being opened: nothing will poll or cancel it, so cancel it here.
            with contextlib.suppress(Exception):
                await provider.cancel_mcp_oauth(attempt.integration_id, attempt.attempt_id)
            return
        self._attempts[action.id] = (provider, attempt.integration_id, attempt.attempt_id)
        try:
            ctx.pcm.update_action(
                cid, action.id, state="waiting", params_update={"url": attempt.url}
            )
        except ChatActionError:
            self._attempts.pop(action.id, None)
            with contextlib.suppress(Exception):
                await provider.cancel_mcp_oauth(attempt.integration_id, attempt.attempt_id)
            ctx.pcm.update_action(cid, action.id, state="failed", error=BAD_URL_ERROR)

    async def poll(self, ctx: ActionContext, action: ChatAction) -> None:
        cid = ctx.chat.chat_id
        provider = ctx.provider
        if not isinstance(provider, OpencodeProvider) or not provider.has_live_server:
            self._attempts.pop(action.id, None)
            ctx.pcm.update_action(cid, action.id, state="failed", error=STOPPED_ERROR)
            return
        server = action.params["server"]
        states = await provider.read_mcp_status()
        if states.get(server) == "connected":
            self._attempts.pop(action.id, None)
            ctx.pcm.update_action(cid, action.id, state="done")
            return
        attempt = self._attempts.get(action.id)
        if attempt is None:
            return
        _, integration_id, attempt_id = attempt
        status, message = await provider.read_mcp_oauth_attempt(integration_id, attempt_id)
        if status == "failed":
            self._attempts.pop(action.id, None)
            ctx.pcm.update_action(
                cid, action.id, state="failed", error=message or FAILED_DEFAULT_ERROR
            )
        elif status == "expired":
            self._attempts.pop(action.id, None)
            ctx.pcm.update_action(cid, action.id, state="failed", error=EXPIRED_ERROR)

    async def cancel(self, ctx: ActionContext, action: ChatAction) -> None:
        attempt = self._attempts.pop(action.id, None)
        if attempt is not None:
            owner, integration_id, attempt_id = attempt
            await owner.cancel_mcp_oauth(integration_id, attempt_id)

    def wake_prompt(self, action: ChatAction) -> str:
        return (
            f"[Ciaobot] The user signed in to the MCP server {json.dumps(action.params['server'])}. "
            "Its tools are available in this chat now; continue the task you were working on."
        )


#: The flow for each provider.
MCP_AUTH_FLOWS: dict[str, ActionHandler] = {
    "opencode": OpencodeMcpAuthFlow(),
    "claude": ClaudeMcpAuthFlow(),
}

COMMAND_WAKE_PROMPT = (
    "The user ran the suggested command in the terminal (exit status unknown). "
    "Check the result with your own tools: you cannot see the terminal's output."
)


class CommandActionHandler(ActionHandler):
    """The `command` kind. The PWA types the text into the chat's terminal
    through the terminal's own door. The engine only records the user's
    progress. It never opens a terminal, writes to a PTY or runs anything."""

    # "ran" may follow "typed"; a failed command card does not exist.
    start_states = frozenset({"open", "working"})
    # poll_states stays the base default ({"waiting"}): this kind never waits,
    # so it is never polled and never times out.

    async def start(self, ctx: ActionContext, action: ChatAction, body: Mapping[str, object]) -> None:
        step = body.get("step")
        if step not in ("typed", "ran") or set(body) - {"step"}:
            raise ChatActionError("invalid_body")
        ctx.pcm.update_action(
            ctx.chat.chat_id, action.id, state="working" if step == "typed" else "done"
        )

    def wake_prompt(self, action: ChatAction) -> str:
        return COMMAND_WAKE_PROMPT


class RestartHandler(ActionHandler):
    """The card only navigates to Settings; the engine never restarts from it."""

    async def start(self, ctx: ActionContext, action: ChatAction, body: Mapping[str, object]) -> None:
        return None

    def wake_prompt(self, action: ChatAction) -> str:
        return "The user saw your restart request."


_PRIVACY_PANE = "x-apple.systempreferences:com.apple.preference.security"
#: The constant URL each privacy pane opens. The model names a pane; it never supplies a URL.
SYSTEM_SETTING_URLS: dict[str, str] = {
    "full_disk_access": f"{_PRIVACY_PANE}?Privacy_AllFiles",
    "calendars": f"{_PRIVACY_PANE}?Privacy_Calendars",
    "contacts": f"{_PRIVACY_PANE}?Privacy_Contacts",
    "reminders": f"{_PRIVACY_PANE}?Privacy_Reminders",
    "photos": f"{_PRIVACY_PANE}?Privacy_Photos",
    "screen_recording": f"{_PRIVACY_PANE}?Privacy_ScreenCapture",
    "accessibility": f"{_PRIVACY_PANE}?Privacy_Accessibility",
    "automation": f"{_PRIVACY_PANE}?Privacy_Automation",
    "camera": f"{_PRIVACY_PANE}?Privacy_Camera",
    "microphone": f"{_PRIVACY_PANE}?Privacy_Microphone",
}
SYSTEM_SETTING_TITLES: dict[str, str] = {
    "full_disk_access": "Full Disk Access", "calendars": "Calendars", "contacts": "Contacts",
    "reminders": "Reminders", "photos": "Photos", "screen_recording": "Screen & System Audio Recording",
    "accessibility": "Accessibility", "automation": "Automation", "camera": "Camera", "microphone": "Microphone",
}
SYSTEM_SETTING_OFF_MAC_ERROR = "System Settings is only available when Ciaobot runs on macOS."
SYSTEM_SETTING_OPEN_ERROR = "System Settings could not be opened. Open it from the Apple menu."


def _open_system_settings(url: str) -> None:
    subprocess.run(["open", url], check=True, timeout=10)


class SystemSettingHandler(ActionHandler):
    """Opens one macOS Privacy & Security pane on the engine's own Mac, from a constant URL."""

    async def start(self, ctx: ActionContext, action: ChatAction, body: Mapping[str, object]) -> None:
        if not ctx.is_loopback:
            raise ChatActionError("not_local")  # action unchanged; the route answers 400
        cid = ctx.chat.chat_id
        url = SYSTEM_SETTING_URLS.get(action.params.get("setting", ""))
        if sys.platform != "darwin" or url is None:
            ctx.pcm.update_action(cid, action.id, state="failed", error=SYSTEM_SETTING_OFF_MAC_ERROR)
            return
        try:
            await asyncio.to_thread(_open_system_settings, url)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("System Settings could not be opened for chat %s: %s", cid, type(exc).__name__)
            ctx.pcm.update_action(cid, action.id, state="failed", error=SYSTEM_SETTING_OPEN_ERROR)
            return
        ctx.pcm.update_action(cid, action.id, state="done")

    def wake_prompt(self, action: ChatAction) -> str:
        title = SYSTEM_SETTING_TITLES.get(action.params.get("setting", ""), "the requested pane")
        return (
            f"The user opened System Settings → Privacy & Security → {title}. "
            "Try the step that was blocked again; if it still fails, the app may need to be restarted "
            "for the change to apply."
        )


MODE_WAKE_DESCRIPTIONS: dict[str, str] = {
    "plan": "read-only, propose changes",
    "normal": "ask before every action",
    "auto": "safe work runs, risky actions ask",
    "bypass": "every action runs without asking",
}
ARCHIVED_MODE_ERROR = "This chat is archived; its mode cannot change."
MODE_FAILED_ERROR = "The mode could not be changed."


class PermissionModeHandler(ActionHandler):
    """The `permission_mode` kind. The agent asks for a mode; the user's Start applies it.

    The mode is read from the raised params, never from the start body, so the
    user approves exactly what the card showed. It is applied through
    ``update_chat``, the validated path ``PATCH /api/chats/{id}`` uses. Nothing the
    agent can reach calls this: only the PWA's Start does.
    """

    async def start(self, ctx: ActionContext, action: ChatAction, body: Mapping[str, object]) -> None:
        mode = action.params["mode"]
        chat_id = ctx.chat.chat_id
        chat = ctx.pcm.get_chat(chat_id)
        if chat is None or chat.archived:
            ctx.pcm.update_action(chat_id, action.id, state="failed", error=ARCHIVED_MODE_ERROR)
            return
        try:
            updated = ctx.pcm.update_chat(chat_id, mode=mode)
        except ValueError:
            updated = None
        if updated is None:
            ctx.pcm.update_action(chat_id, action.id, state="failed", error=MODE_FAILED_ERROR)
            return
        logger.info("chat %s permission mode -> %s (user-approved request)", chat_id, mode)
        ctx.pcm.update_action(chat_id, action.id, state="done")

    def wake_prompt(self, action: ChatAction) -> str:
        mode = action.params["mode"]
        return (
            f"The user switched this chat's permission mode to {mode} "
            f"({MODE_WAKE_DESCRIPTIONS[mode]}). It applies from this turn."
        )


#: The handler for each kind. Kinds with a handler register here.
ACTION_HANDLERS: dict[str, ActionHandler] = {
    "mcp_auth": McpAuthHandler(),
    "command": CommandActionHandler(),
    "restart": RestartHandler(),
    "permission_mode": PermissionModeHandler(),
    "system_setting": SystemSettingHandler(),
    "device_code": DeviceCodeFlow(),
    "gws_auth": GwsAuthHandler(),
}


class ChatActionsRuntime:
    """Starts, watches, dismisses, continues and detects the chat's action cards."""

    def __init__(
        self,
        pcm: ProjectChatManager,
        *,
        poll_interval_s: float = POLL_INTERVAL_S,
        poll_deadline_s: float = POLL_DEADLINE_S,
        detect_timeout_s: float = DETECT_TIMEOUT_S,
    ) -> None:
        self._pcm = pcm
        self._poll_interval_s = poll_interval_s
        self._poll_deadline_s = poll_deadline_s
        self._detect_timeout_s = detect_timeout_s
        self._app_state: Any = None
        self._polls: dict[str, asyncio.Task[object]] = {}

    def bind_app_state(self, app_state: Any) -> None:
        """Keep the Starlette app state, so handlers can reach app singletons from the poll loop."""
        self._app_state = app_state

    def context(self, chat_id: str, *, is_loopback: bool = True) -> ActionContext | None:
        chat = self._pcm.get_chat(chat_id)
        if chat is None:
            return None
        service = self._pcm._providers.get(chat_id)
        provider: ProviderImpl | None = service.provider if service is not None else None
        terminals: TerminalSessionManager | None = getattr(self._app_state, "terminal_sessions", None)
        return ActionContext(
            pcm=self._pcm,
            chat=chat,
            provider=provider,
            is_loopback=is_loopback,
            app_state=self._app_state,
            terminals=terminals,
            agent_root=self._pcm._agent_root_for_chat(chat_id),
        )

    def _conflict(self, chat_id: str) -> Exception:
        from ciao.web.project_chats import ChatActionConflictError

        return ChatActionConflictError(self._pcm.chat_action_payloads(chat_id))

    async def start(
        self, chat_id: str, action_id: str, body: Mapping[str, object], *, is_loopback: bool
    ) -> dict[str, object]:
        action = self._pcm.get_action(chat_id, action_id)
        if action is None:
            raise self._conflict(chat_id)
        handler = ACTION_HANDLERS.get(action.kind)
        if handler is None:
            raise ChatActionError("unsupported_kind")
        if action.state not in handler.start_states:
            raise ChatActionError("invalid_state")
        ctx = self.context(chat_id, is_loopback=is_loopback)
        if ctx is None:
            raise self._conflict(chat_id)
        await handler.start(ctx, action, body)
        current = self._pcm.get_action(chat_id, action_id)
        if current is None:
            raise self._conflict(chat_id)
        if current.state in handler.poll_states:
            self.start_polling(chat_id, action_id)
        return action_payload(current)

    async def dismiss(self, chat_id: str, action_id: str) -> list[dict[str, object]]:
        action = self._pcm.get_action(chat_id, action_id)
        if action is not None:
            self._stop_poll(action_id)
            handler = ACTION_HANDLERS.get(action.kind)
            ctx = self.context(chat_id)
            if handler is not None and ctx is not None:
                await self._cancel_quietly(handler, ctx, action)
        self._pcm.dismiss_action(chat_id, action_id)
        return self._pcm.chat_action_payloads(chat_id)

    async def turn_off_server(
        self, chat_id: str, action_id: str
    ) -> tuple[list[dict[str, object]], dict[str, list[str]]]:
        """Turn an mcp_auth card's server off for the chat's workspace and provider, then dismiss the card.

        The provider's own config is changed (``ciao.web.mcp_toggle``); a refusal raises
        ``McpToggleError`` and leaves the card as it is. The chat itself is reloaded even when
        its project names a workspace the engine no longer knows (``_workspace_for_chat``
        then answers the primary workspace, whose scope does not contain this chat).
        """
        action = self._pcm.get_action(chat_id, action_id)
        if action is None:
            raise self._conflict(chat_id)
        if action.kind != "mcp_auth":
            raise ChatActionError("unsupported_kind")
        provider = str(action.params.get("provider", ""))
        server = str(action.params.get("server", ""))
        workspace = self._pcm._workspace_for_chat(chat_id)
        agent_root = Path(self._pcm._agent_root_for_chat(chat_id))
        await asyncio.to_thread(
            set_mcp_server_enabled,
            provider=provider, server=server, agent_root=agent_root, enabled=False,
        )
        remaining = await self.dismiss(chat_id, action_id)
        reload = await self._pcm.reload_workspace_providers(workspace, provider)
        covered = set(reload["reloaded"]) | set(reload["pending"])
        if chat_id not in covered and chat_id in self._pcm._providers:
            if chat_id in self._pcm.active_chat_ids() or not await self._pcm.reload_provider(chat_id):
                reload["pending"] = sorted([*reload["pending"], chat_id])
            else:
                reload["reloaded"] = sorted([*reload["reloaded"], chat_id])
        return remaining, reload

    async def continue_action(
        self, chat_id: str, action_id: str
    ) -> tuple[list[dict[str, object]], str]:
        chat = self._pcm.get_chat(chat_id)
        action = self._pcm.get_action(chat_id, action_id)
        if chat is None or action is None:
            raise self._conflict(chat_id)
        if action.state != "done":
            raise ChatActionError("invalid_state")
        handler = ACTION_HANDLERS.get(action.kind)
        if handler is None:
            raise ChatActionError("unsupported_kind")
        prompt = handler.wake_prompt(action)
        self._stop_poll(action_id)
        ctx = self.context(chat_id)
        if ctx is not None:
            await self._cancel_quietly(handler, ctx, action)
        self._pcm.remove_action(chat_id, action_id)
        delivery = self._pcm._deliver_wake(chat, prompt, count=1, origin="chat_action")
        return self._pcm.chat_action_payloads(chat_id), delivery

    async def callback(
        self, chat_id: str, action_id: str, url: str, *, is_loopback: bool
    ) -> dict[str, object]:
        """Hand a pasted sign-in address to the card kind's handler. Does not start or stop the poll:
        the card is already ``waiting``, so its poll is running from ``start``."""
        action = self._pcm.get_action(chat_id, action_id)
        if action is None:
            raise self._conflict(chat_id)
        if action.state != "waiting":
            raise ChatActionError("invalid_state")
        handler = ACTION_HANDLERS.get(action.kind)
        if handler is None:
            raise ChatActionError("unsupported_kind")
        ctx = self.context(chat_id, is_loopback=is_loopback)
        if ctx is None:
            raise self._conflict(chat_id)
        await handler.callback(ctx, action, url)
        current = self._pcm.get_action(chat_id, action_id)
        if current is None:
            raise self._conflict(chat_id)
        return action_payload(current)

    def cancel_removed(self, chat_id: str, actions: list[ChatAction]) -> None:
        """Stop the polls of, and cancel, actions a lifecycle clear dropped. Synchronous."""
        for action in actions:
            self._stop_poll(action.id)
            handler = ACTION_HANDLERS.get(action.kind)
            if handler is None:
                continue
            ctx = self.context(chat_id)
            if ctx is None:
                continue
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                logger.debug("no running loop; skipping cancel for action %s", action.id)
                continue
            self._pcm._spawn_detached(
                self._cancel_quietly(handler, ctx, action),
                f"chat-action-cancel-{action.id}",
            )

    def start_polling(self, chat_id: str, action_id: str) -> None:
        existing = self._polls.get(action_id)
        if existing is not None and not existing.done():
            return
        task = self._pcm._spawn_detached(
            self._poll_loop(chat_id, action_id), f"chat-action-poll-{action_id}"
        )
        self._polls[action_id] = task

        def _forget(finished: asyncio.Task[object]) -> None:
            if self._polls.get(action_id) is finished:
                self._polls.pop(action_id, None)

        task.add_done_callback(_forget)

    def detection_provider(self, chat_id: str) -> OpencodeProvider | ClaudeProvider | None:
        """The provider detection may ask: a live OpenCode server, or a connected Claude session."""
        chat = self._pcm.get_chat(chat_id)
        if chat is None or chat.archived:
            return None
        service = self._pcm._providers.get(chat_id)
        provider: ProviderImpl | None = service.provider if service is not None else None
        if isinstance(provider, OpencodeProvider) and provider.has_live_server:
            return provider
        if isinstance(provider, ClaudeProvider) and provider._connected:
            return provider
        return None

    def request_mcp_auth(self, chat_id: str, server: str) -> dict[str, object]:
        """Put the chat's sign-in card for ``server`` on the chat, or return the one already there.

        The user asked for this sign-in in Settings, so an earlier Not now for this
        server no longer applies to this chat or its workspace. A ``done`` card for
        the server is dropped first: once connected, detection marks the card done,
        and after a later expiry that card would come back unchanged. The card is
        started by the usual start route; nothing here talks to a provider.
        """
        server = server.strip()
        chat = self._pcm.get_chat(chat_id)
        if chat is None or chat.archived:
            raise ChatActionError("invalid_state")
        if chat.provider == "claude" and is_claudeai_connector(server):
            raise ChatActionError("invalid_param")
        key = action_key("mcp_auth", {"server": server})
        chat.dismissed_action_keys.discard(key)
        self._pcm.declined_mcp_auth.discard(self._pcm.mcp_auth_decline_key(chat, key))
        for existing in list(chat.actions):
            if existing.state == "done" and action_key(existing.kind, existing.params) == key:
                self._stop_poll(existing.id)
                self._pcm.remove_action(chat_id, existing.id)
        payload = self._pcm.raise_action(
            chat_id, "mcp_auth", {"server": server, "provider": chat.provider}, origin="engine"
        )
        if payload is None:
            raise ChatActionError("invalid_state")
        return payload

    async def detect_mcp_auth(self, chat_id: str) -> None:
        """Raise a sign-in card for each server that needs one, and finish cards whose server connected."""
        provider = self.detection_provider(chat_id)
        if provider is None:
            return
        try:
            states = await asyncio.wait_for(provider.read_mcp_status(), self._detect_timeout_s)
        except Exception:  # noqa: BLE001 - detection is best effort
            logger.debug("MCP status read failed during detection for chat %s", chat_id, exc_info=True)
            return
        chat = self._pcm.get_chat(chat_id)
        if chat is None or chat.archived:
            return
        for server, state in sorted(states.items()):
            if state == "needs_auth":
                if isinstance(provider, ClaudeProvider) and is_claudeai_connector(server):
                    # A claude.ai connector is in the account's (or the
                    # organization's) catalog whether or not the user ever
                    # connected it, and it is connected on claude.ai, not with
                    # ``claude mcp login``. Raising a card for each one asked
                    # for sign-ins to services the user does not use.
                    continue
                key = action_key("mcp_auth", {"server": server})
                declined = self._pcm.mcp_auth_decline_key(chat, key)
                if key in chat.dismissed_action_keys or declined in self._pcm.declined_mcp_auth:
                    continue
                try:
                    self._pcm.raise_action(
                        chat_id,
                        "mcp_auth",
                        {"server": server, "provider": chat.provider},
                        origin="engine",
                    )
                except (ChatActionError, ValueError):
                    logger.debug("could not raise a sign-in card for chat %s", chat_id, exc_info=True)
                    continue
            elif state == "connected":
                # Signed in after all: a later expiry should ask again.
                self._pcm.declined_mcp_auth.discard(
                    self._pcm.mcp_auth_decline_key(chat, action_key("mcp_auth", {"server": server}))
                )
                for action in list(chat.actions):
                    if (
                        action.kind == "mcp_auth"
                        and action.params.get("server") == server
                        and action.state != "done"
                    ):
                        self._pcm.update_action(chat_id, action.id, state="done")

    def _stop_poll(self, action_id: str) -> None:
        task = self._polls.pop(action_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _poll_loop(self, chat_id: str, action_id: str) -> None:
        deadline = time.monotonic() + self._poll_deadline_s
        while True:
            await asyncio.sleep(self._poll_interval_s)
            action = self._pcm.get_action(chat_id, action_id)
            handler = ACTION_HANDLERS.get(action.kind) if action is not None else None
            if action is None or handler is None or action.state not in handler.poll_states:
                return
            ctx = self.context(chat_id)
            if ctx is None:
                return
            if time.monotonic() >= deadline:
                self._pcm.update_action(chat_id, action_id, state="failed", error=TIMEOUT_ERROR)
                await self._cancel_quietly(handler, ctx, action)
                return
            try:
                await handler.poll(ctx, action)
            except Exception:  # noqa: BLE001 - one failed tick must not end the wait
                logger.debug("chat action poll tick failed for %s", chat_id, exc_info=True)

    async def _cancel_quietly(self, handler: ActionHandler, ctx: ActionContext, action: ChatAction) -> None:
        try:
            await asyncio.wait_for(handler.cancel(ctx, action), CANCEL_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - cleanup is best effort
            logger.debug("chat action cancel failed for %s", action.id, exc_info=True)
