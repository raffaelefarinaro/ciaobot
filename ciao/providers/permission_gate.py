"""Route SDK ``can_use_tool`` callbacks through the PWA's approve/deny UI.

When the Claude Agent SDK runs in ``permission_mode="auto"``, the classifier
handles most tool calls silently; only escalations land here. Flow:

1. SDK invokes :meth:`PermissionGate.handle` with a pending tool call.
2. Gate publishes a :class:`PermissionRequestEvent` into the active stream
   (fan-out: per-chat WebSocket + push notification).
3. Gate awaits an :class:`asyncio.Future` keyed by ``request_id``.
4. The PWA client sends ``{type: "permission_response", request_id, approved,
   reason}``; the route handler calls :meth:`answer`, which completes the
   future and lets the SDK proceed.

The gate is intentionally stateless across turns: the provider instantiates
one per ``ClaudeProvider`` and relies on :meth:`cancel_all` to drain pending
prompts when a turn ends (user stop, error, disconnect) so stale answers from
a previous turn cannot leak into a new one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from ciao.models import PermissionRequestEvent

logger = logging.getLogger(__name__)

EmitCallback = Callable[[PermissionRequestEvent], None]


def _summarize_input(tool_input: dict[str, Any]) -> str:
    """Best-effort one-line rendering of the tool input for the UI.

    We avoid the provider's rich summarizer here to keep the gate independent
    of Claude-specific heuristics; the client can show the full JSON if it
    wants.
    """
    if not tool_input:
        return ""
    try:
        return json.dumps(tool_input, ensure_ascii=False, default=str)[:2000]
    except (TypeError, ValueError):
        return str(tool_input)[:2000]


class PermissionGate:
    """One-callback-per-permission async gate."""

    __slots__ = ("_emit", "_pending", "_loop")

    def __init__(self, emit: EmitCallback | None = None) -> None:
        self._emit: EmitCallback = emit or (lambda _ev: None)
        self._pending: dict[str, asyncio.Future[PermissionResultAllow | PermissionResultDeny]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_emit(self, emit: EmitCallback | None) -> None:
        """Rebind the publish callback.

        The provider pins one ``PermissionGate`` per ``ClaudeProvider`` but
        each streaming turn owns its own output queue. Call this at turn
        start (bind to the turn's queue) and turn teardown (bind to no-op)
        so late gate events can't leak into a stale queue.
        """
        self._emit = emit or (lambda _ev: None)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def has_pending(self, request_id: str) -> bool:
        future = self._pending.get(request_id)
        return future is not None and not future.done()

    async def handle(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        """SDK ``can_use_tool`` callback. Blocks until the UI answers."""
        # AskUserQuestion should never require a permission prompt: the model
        # is simply asking the user a question in-band. Auto-approve so the
        # questions flow as normal text instead of a blocking Approve/Deny card.
        if tool_name == "AskUserQuestion":
            return PermissionResultAllow()

        request_id = context.tool_use_id or f"gen-{uuid.uuid4().hex}"
        loop = asyncio.get_running_loop()
        self._loop = loop
        future: asyncio.Future[PermissionResultAllow | PermissionResultDeny] = loop.create_future()

        # Collision with a stale id is theoretical (SDK generates fresh ids)
        # but we still guard: leaving an old future stranded would deadlock
        # every future prompt with the same id.
        old = self._pending.pop(request_id, None)
        if old is not None and not old.done():
            old.cancel()

        self._pending[request_id] = future

        event = PermissionRequestEvent(
            type="system",
            message=f"Approve use of {tool_name}?",
            tool_name=tool_name,
            tool_input=_summarize_input(tool_input),
            request_id=request_id,
        )
        try:
            self._emit(event)
        except Exception:  # noqa: BLE001 — publish must never kill the turn
            logger.exception("Permission event publish failed for %s", request_id)

        try:
            return await future
        finally:
            # ``pop(..., None)`` handles the race where ``answer`` already
            # removed the entry (the normal path) as well as the cancel
            # path where we never resolved it.
            self._pending.pop(request_id, None)

    def answer(
        self,
        request_id: str,
        *,
        approved: bool,
        reason: str = "",
    ) -> bool:
        """Resolve a pending request. Returns True if a match was found."""
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            return False

        if approved:
            future.set_result(PermissionResultAllow())
        else:
            message = reason or "User denied this action"
            future.set_result(PermissionResultDeny(message=message, interrupt=False))
        return True

    def cancel_all(self, reason: str = "Turn ended before approval") -> None:
        """Resolve every pending request as a deny. Used on turn teardown."""
        if not self._pending:
            return
        for request_id, future in list(self._pending.items()):
            if not future.done():
                future.set_result(
                    PermissionResultDeny(message=reason, interrupt=False)
                )
            self._pending.pop(request_id, None)
