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
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import httpx

from ciao.chat_actions import ChatAction, ChatActionError, action_key, action_payload
from ciao.provider_service import ProviderImpl
from ciao.providers.opencode import McpOAuthUnavailable, OpencodeProvider
from ciao.web.chat_actions_base import ActionContext, ActionHandler

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
]

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 2.0
POLL_DEADLINE_S = 600.0
DETECT_TIMEOUT_S = 3.0
START_TIMEOUT_S = 20.0
CANCEL_TIMEOUT_S = 5.0
TIMEOUT_ERROR = "Timed out waiting for sign-in"

# Error copy. The engine composes these; the PWA shows them as they are.
NOT_RUNNING_ERROR = "This chat's OpenCode server is not running. Send a message in the chat, then try again."
NO_SIGN_IN_ERROR = "OpenCode does not offer a sign-in for this server. Sign in with `opencode mcp auth` instead."
CODE_MODE_ERROR = "This server asks for a pasted sign-in code. Sign in with `opencode mcp auth` instead."
START_FAILED_ERROR = "OpenCode could not start the sign-in. Try again."
BAD_URL_ERROR = "OpenCode returned a sign-in address Ciao cannot open."
STOPPED_ERROR = "This chat's OpenCode server stopped before the sign-in finished. Try again."
EXPIRED_ERROR = "The sign-in page expired. Try again."
FAILED_DEFAULT_ERROR = "The sign-in did not finish."


class McpAuthHandler(ActionHandler):
    """Dispatches an ``mcp_auth`` action to the flow for its provider."""

    poll_states = frozenset({"working", "waiting"})

    def _flow(self, action: ChatAction) -> ActionHandler | None:
        return MCP_AUTH_FLOWS.get(action.params.get("provider", ""))

    async def start(self, ctx: ActionContext, action: ChatAction, body: Mapping[str, object]) -> None:
        flow = self._flow(action)
        if flow is None:
            # No flow for this provider: refused before any state change. C2 removes this branch.
            raise ChatActionError("unsupported_provider")
        await flow.start(ctx, action, body)

    async def poll(self, ctx: ActionContext, action: ChatAction) -> None:
        flow = self._flow(action)
        if flow is not None:
            await flow.poll(ctx, action)

    async def cancel(self, ctx: ActionContext, action: ChatAction) -> None:
        flow = self._flow(action)
        if flow is not None:
            await flow.cancel(ctx, action)

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
        provider = ctx.provider
        if not isinstance(provider, OpencodeProvider) or not provider.has_live_server:
            ctx.pcm.update_action(cid, action.id, state="failed", error=NOT_RUNNING_ERROR)
            return
        try:
            attempt = await asyncio.wait_for(
                provider.start_mcp_oauth(action.params["server"]), START_TIMEOUT_S
            )
        except McpOAuthUnavailable as exc:
            error = NOT_RUNNING_ERROR if exc.reason == "not_running" else NO_SIGN_IN_ERROR
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


#: The flow for each provider. C2 adds "claude".
MCP_AUTH_FLOWS: dict[str, ActionHandler] = {"opencode": OpencodeMcpAuthFlow()}

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


#: The handler for each kind. Kinds with a handler register here.
ACTION_HANDLERS: dict[str, ActionHandler] = {
    "mcp_auth": McpAuthHandler(),
    "command": CommandActionHandler(),
    "restart": RestartHandler(),
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

    def detection_provider(self, chat_id: str) -> OpencodeProvider | None:
        """The provider detection may ask: an OpenCode chat with a live server. B1 covers OpenCode only."""
        chat = self._pcm.get_chat(chat_id)
        if chat is None or chat.archived or chat.provider != "opencode":
            return None
        service = self._pcm._providers.get(chat_id)
        provider: ProviderImpl | None = service.provider if service is not None else None
        if isinstance(provider, OpencodeProvider) and provider.has_live_server:
            return provider
        return None

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
                if action_key("mcp_auth", {"server": server}) in chat.dismissed_action_keys:
                    continue
                try:
                    self._pcm.raise_action(
                        chat_id,
                        "mcp_auth",
                        {"server": server, "provider": "opencode"},
                        origin="engine",
                    )
                except (ChatActionError, ValueError):
                    logger.debug("could not raise a sign-in card for chat %s", chat_id, exc_info=True)
                    continue
            elif state == "connected":
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
