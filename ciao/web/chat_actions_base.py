"""The handler base for a needs-you action kind.

Every kind subclasses :class:`ActionHandler`. A kind module that lives in its own
file imports only this module, never ``ciao.web.chat_actions_runtime``, so the
runtime can import it for registration without an import cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from ciao.chat_actions import ChatAction, ChatActionError
from ciao.provider_service import ProviderImpl

if TYPE_CHECKING:
    from ciao.terminal_sessions import TerminalSessionManager
    from ciao.web.project_chats import ChatInfo, ProjectChatManager


@dataclass(frozen=True)
class ActionContext:
    """What a handler may reach while it works one action."""

    pcm: ProjectChatManager
    chat: ChatInfo
    provider: ProviderImpl | None
    is_loopback: bool
    app_state: Any = None  # Starlette app.state (None in unit tests)
    terminals: TerminalSessionManager | None = None  # app_state.terminal_sessions
    agent_root: Path | None = None  # pcm._agent_root_for_chat(chat_id)


class ActionHandler:
    """Subclass this for each kind.

    ``start`` raises :class:`ChatActionError` before changing any state; ``cancel``
    is the one cleanup hook. ``start_states`` and ``poll_states`` say which states
    a start may begin from and which states the poll loop keeps ticking in.
    """

    start_states: ClassVar[frozenset[str]] = frozenset({"open", "failed"})
    poll_states: ClassVar[frozenset[str]] = frozenset({"waiting"})

    async def start(self, ctx: ActionContext, action: ChatAction, body: Mapping[str, object]) -> None:
        raise NotImplementedError

    async def poll(self, ctx: ActionContext, action: ChatAction) -> None:
        """One tick of the wait. The runtime owns the loop; the default does nothing."""
        return None

    async def cancel(self, ctx: ActionContext, action: ChatAction) -> None:
        """Release what the action holds: dismiss, continue, timeout or a lifecycle clear."""
        return None

    async def callback(self, ctx: ActionContext, action: ChatAction, url: str) -> None:
        raise ChatActionError("unsupported_kind")

    def wake_prompt(self, action: ChatAction) -> str:
        raise NotImplementedError
