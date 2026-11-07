"""The device_code needs-you card: a gh or az sign-in run as a helper terminal for the chat.

The agent raises the card when a CLI it uses is not logged in. On Start the
engine runs the tool from a constant table (fixed arguments, a code regex and an
allowlisted verification URL) as a helper terminal session, scrapes the one-time
code from its output so the card can show it, presses Enter once for gh (which
waits for Enter before it polls), and ends in ``done`` on exit 0 or ``failed``
otherwise. The argv is constants plus the resolved executable; no model text
reaches the command line. Nothing here logs the code, the URL or the output.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from ciao.chat_actions import ActionState, ChatAction
from ciao.terminal_scrape import last_error_line, strip_terminal_controls
from ciao.terminal_sessions import TerminalSessionError
from ciao.tool_path import resolve_command
from ciao.web.chat_actions_base import ActionContext, ActionHandler
from ciao.web.mcp_auth_claude import CAPACITY, CLOSED, NO_TERMINAL

__all__ = [
    "CAPACITY",
    "CLOSED",
    "DEVICE_TOOLS",
    "NO_TERMINAL",
    "DeviceCodeFlow",
    "DeviceToolSpec",
]

logger = logging.getLogger(__name__)

# Error copy. The engine composes these; no model text and no terminal output.
UNKNOWN_TOOL = "That tool has no sign-in card."
NOT_INSTALLED = "{tool} is not installed on this computer. Ask the agent for the install command."
CANNOT_RUN = "{tool} cannot be started from the sign-in terminal on this computer. Ask the agent for the command instead."
ENDED = "{tool} sign-in ended with exit code {code}."


@dataclass(frozen=True)
class DeviceToolSpec:
    """One tool the card may sign in: its binary, fixed arguments and output scrapers."""

    binary: str  # name resolved on the terminal PATH
    args: tuple[str, ...]  # fixed arguments
    code_re: re.Pattern[str]  # group 1 = the one-time code
    url_re: re.Pattern[str]  # group 0 = an allowlisted verification URL
    default_url: str  # used when the output shows none
    press_enter_after_code: bool


DEVICE_TOOLS: Mapping[str, DeviceToolSpec] = MappingProxyType(
    {
        "gh": DeviceToolSpec(
            binary="gh",
            args=("auth", "login", "--web", "--hostname", "github.com", "--git-protocol", "https"),
            code_re=re.compile(r"(?i:one-time code)\W{0,4}([A-Z0-9]{4}-[A-Z0-9]{4})\b"),
            url_re=re.compile(r"https://github\.com/login/device\b"),
            default_url="https://github.com/login/device",
            press_enter_after_code=True,
        ),
        "az": DeviceToolSpec(
            binary="az",
            args=("login", "--use-device-code"),
            code_re=re.compile(r"(?i:enter the code)\s+([A-Z0-9]{8,12})\b"),
            url_re=re.compile(r"https://(?:microsoft\.com/devicelogin|login\.microsoft\.com/device)\b"),
            default_url="https://microsoft.com/devicelogin",
            press_enter_after_code=False,
        ),
    }
)


class DeviceCodeFlow(ActionHandler):
    """The device-code sign-in: a helper terminal running a fixed gh or az login."""

    poll_states = frozenset({"working", "waiting"})

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
        logger.info("device_code %s: %s", action.id, state)

    async def _end(self, ctx: ActionContext, session_id: str) -> None:
        if ctx.terminals is None:
            return
        try:
            await ctx.terminals.end(session_id)
        except TerminalSessionError:
            pass

    @staticmethod
    def _spec(action: ChatAction) -> DeviceToolSpec | None:
        return DEVICE_TOOLS.get(action.params.get("tool", ""))

    async def start(self, ctx: ActionContext, action: ChatAction, body: Mapping[str, object]) -> None:
        spec = self._spec(action)
        if spec is None:
            self._set(ctx, action, "failed", error=UNKNOWN_TOOL)
            return
        try:
            prefix = resolve_command(spec.binary)
        except OSError:
            self._set(ctx, action, "failed", error=CANNOT_RUN.format(tool=spec.binary))
            return
        if not prefix:
            self._set(ctx, action, "failed", error=NOT_INSTALLED.format(tool=spec.binary))
            return
        # A bare executable must be the tool itself. A Windows wrapper that unwraps to
        # an unrelated interpreter (az.cmd -> python.exe) would run the wrong program.
        # The [node, script] form from an npm shim is accepted as resolved.
        if len(prefix) == 1 and Path(prefix[0]).stem.lower() != spec.binary:
            self._set(ctx, action, "failed", error=CANNOT_RUN.format(tool=spec.binary))
            return
        if len(prefix) not in (1, 2):
            self._set(ctx, action, "failed", error=CANNOT_RUN.format(tool=spec.binary))
            return
        folder = ctx.agent_root
        if folder is None or ctx.terminals is None:
            self._set(ctx, action, "failed", error=NO_TERMINAL)
            return
        try:
            info = await ctx.terminals.create(
                folder,
                "chat",
                ctx.chat.chat_id,
                argv=[*prefix, *spec.args],
            )
        except TerminalSessionError as exc:
            self._set(ctx, action, "failed", error=CAPACITY if exc.reason == "capacity" else NO_TERMINAL)
            return
        if ctx.pcm.get_action(ctx.chat.chat_id, action.id) is None:
            # Dismissed while the helper was starting: nothing will poll or cancel it, so end it here.
            await self._end(ctx, info.session_id)
            return
        # Empty values clear a previous attempt's code, URL and Enter on a restart from failed.
        self._set(
            ctx,
            action,
            "working",
            params_update={"session_id": info.session_id, "code": "", "url": "", "entered": ""},
        )

    async def poll(self, ctx: ActionContext, action: ChatAction) -> None:
        session_id = action.params.get("session_id")
        if not session_id:
            return
        spec = self._spec(action)
        if spec is None:
            return
        if ctx.terminals is None:
            self._set(ctx, action, "failed", error=CLOSED)
            return
        try:
            info = ctx.terminals.get(session_id)
            raw = ctx.terminals.helper_output(session_id)
        except TerminalSessionError:
            self._set(ctx, action, "failed", error=CLOSED)
            return
        text = strip_terminal_controls(raw)
        if not action.params.get("code"):
            match = spec.code_re.search(text)
            if match:
                url_match = spec.url_re.search(text)
                params_update: dict[str, object] = {
                    "code": match.group(1),
                    "url": url_match.group(0) if url_match else spec.default_url,
                }
                if spec.press_enter_after_code and not info.exited:
                    try:
                        ctx.terminals.helper_input(session_id, b"\r")
                    except TerminalSessionError:
                        pass
                    else:
                        params_update["entered"] = "1"
                self._set(ctx, action, "waiting", params_update=params_update)
        if not info.exited:
            return
        if info.exit_code == 0:
            self._set(ctx, action, "done")
        else:
            error = last_error_line(text) or ENDED.format(tool=spec.binary, code=info.exit_code)
            self._set(ctx, action, "failed", error=error)
        await self._end(ctx, session_id)

    async def cancel(self, ctx: ActionContext, action: ChatAction) -> None:
        session_id = action.params.get("session_id")
        if session_id:
            await self._end(ctx, session_id)

    def wake_prompt(self, action: ChatAction) -> str:
        return (
            f"[Ciaobot] The user finished signing in to {action.params.get('tool', '')}. "
            "Continue where you left off."
        )
