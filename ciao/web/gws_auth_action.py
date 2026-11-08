"""The `gws_auth` needs-you card: sign a workspace's expired Google login in again.

The card reuses the app's ``GwsReloginManager`` (the same instance Settings uses), so
the loopback listener lives in the engine process. A pasted redirect from another
device is finished through ``GwsReloginManager.complete_from_redirect``, never over
HTTP. Nothing here logs the URL, code or state.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from ciao.chat_actions import ChatAction, ChatActionError
from ciao.gws_auth import RELOGIN_BAD_REDIRECT, RELOGIN_NO_PENDING, RELOGIN_STATE_MISMATCH
from ciao.web.chat_actions_base import ActionContext, ActionHandler

logger = logging.getLogger(__name__)

UNAVAILABLE = "Google sign-in is not available in this engine."
TIMED_OUT = "The sign-in timed out before you finished it. Try again."
FAILED_DEFAULT = "The sign-in did not finish."

_REFUSALS: dict[str, str] = {
    RELOGIN_NO_PENDING: "no_pending_sign_in",
    RELOGIN_BAD_REDIRECT: "bad_callback_url",
    RELOGIN_STATE_MISMATCH: "state_mismatch",
}


class GwsAuthHandler(ActionHandler):
    def __init__(self) -> None:
        self._refresh_tasks: set[asyncio.Task[None]] = set()

    @staticmethod
    def _manager(ctx: ActionContext) -> Any:
        return getattr(ctx.app_state, "gws_relogin_manager", None)

    async def start(self, ctx: ActionContext, action: ChatAction, body: Mapping[str, object]) -> None:
        manager = self._manager(ctx)
        if manager is None:
            ctx.pcm.update_action(ctx.chat.chat_id, action.id, state="failed", error=UNAVAILABLE)
            return
        profile = action.params["profile"]
        try:
            result: dict[str, Any] = await asyncio.to_thread(manager.start, profile)
        except ValueError as exc:
            # The manager's messages are already secret-free.
            ctx.pcm.update_action(ctx.chat.chat_id, action.id, state="failed", error=str(exc))
            return
        try:
            ctx.pcm.update_action(
                ctx.chat.chat_id,
                action.id,
                state="waiting",
                params_update={"url": str(result.get("auth_url", ""))},
            )
        except ChatActionError:
            # A URL the validator refuses must not leave a listener behind.
            manager.cancel(profile)
            ctx.pcm.update_action(ctx.chat.chat_id, action.id, state="failed", error=FAILED_DEFAULT)
            return
        logger.info("gws_auth %s: waiting", action.id)

    async def poll(self, ctx: ActionContext, action: ChatAction) -> None:
        manager = self._manager(ctx)
        if manager is None:
            return
        status: dict[str, Any] = manager.status(action.params["profile"])
        word = status.get("status")
        if word == "pending":
            return
        if word == "completed":
            ctx.pcm.update_action(ctx.chat.chat_id, action.id, state="done")
            logger.info("gws_auth %s: done", action.id)
            self._refresh_health(ctx)
            return
        if word == "error":
            error = str(status.get("error") or FAILED_DEFAULT)
            ctx.pcm.update_action(ctx.chat.chat_id, action.id, state="failed", error=error)
            logger.info("gws_auth %s: failed", action.id)
            return
        # "none": the session's TTL expired and the manager dropped it.
        ctx.pcm.update_action(ctx.chat.chat_id, action.id, state="failed", error=TIMED_OUT)
        logger.info("gws_auth %s: timed out", action.id)

    def _refresh_health(self, ctx: ActionContext) -> None:
        """Refresh the gws health cache in the background; the probes can take 30 s each."""
        monitor = getattr(ctx.app_state, "gws_health_monitor", None)
        if monitor is None:
            return

        async def _run() -> None:
            try:
                await asyncio.to_thread(monitor.check_once)
            except Exception:
                logger.exception("Post-relogin health refresh failed")

        task = asyncio.get_running_loop().create_task(_run())
        self._refresh_tasks.add(task)
        task.add_done_callback(self._refresh_tasks.discard)

    async def callback(self, ctx: ActionContext, action: ChatAction, url: str) -> None:
        manager = self._manager(ctx)
        if manager is None:
            raise ChatActionError("no_pending_sign_in")
        try:
            await asyncio.to_thread(manager.complete_from_redirect, action.params["profile"], url)
        except ValueError as exc:
            # A refusal leaves the sign-in pending: the user may paste again.
            raise ChatActionError(_REFUSALS.get(str(exc), "bad_callback_url")) from None
        await self.poll(ctx, action)

    def wake_prompt(self, action: ChatAction) -> str:
        profile = action.params["profile"]
        return f"The user signed in to the '{profile}' Google account again. Retry the Google call that failed."
