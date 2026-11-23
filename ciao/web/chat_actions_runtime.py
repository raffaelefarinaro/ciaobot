"""The needs-you runtime: the handler registry, the poll loop and cancel.

The chat-action store (``ProjectChatManager.raise_action`` and friends) holds the
cards. This module decides what happens to a card: how a kind starts, how the
engine watches a sign-in until it finishes or the user gives up, and what
Continue wakes.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ciao.chat_actions import ChatAction, ChatActionError, action_payload
from ciao.provider_service import ProviderImpl
from ciao.web.chat_actions_base import ActionContext, ActionHandler
from ciao.web.device_code_auth import DeviceCodeFlow

if TYPE_CHECKING:
    from ciao.terminal_sessions import TerminalSessionManager
    from ciao.web.project_chats import ProjectChatManager

__all__ = [
    "ACTION_HANDLERS",
    "ActionContext",
    "ActionHandler",
    "ChatActionsRuntime",
    "SYSTEM_SETTING_URLS",
    "SystemSettingHandler",
]

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 2.0
POLL_DEADLINE_S = 600.0
CANCEL_TIMEOUT_S = 5.0
TIMEOUT_ERROR = "Timed out waiting for sign-in"

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
    "command": CommandActionHandler(),
    "restart": RestartHandler(),
    "permission_mode": PermissionModeHandler(),
    "system_setting": SystemSettingHandler(),
    "device_code": DeviceCodeFlow(),
}


class ChatActionsRuntime:
    """Starts, watches, dismisses, continues and detects the chat's action cards."""

    def __init__(
        self,
        pcm: ProjectChatManager,
        *,
        poll_interval_s: float = POLL_INTERVAL_S,
        poll_deadline_s: float = POLL_DEADLINE_S,
    ) -> None:
        self._pcm = pcm
        self._poll_interval_s = poll_interval_s
        self._poll_deadline_s = poll_deadline_s
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
