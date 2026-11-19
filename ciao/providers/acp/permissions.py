"""ACP permission prompts and the mode value an agent is asked to hold (#3137).

The broker answers ``session/request_permission``. It never approves on its
own: an unattended turn is rejected with no card, a reply for another session
is refused, and cancel answers every pending request with ``cancelled``.
``allow_always`` is chosen only when the agent did not offer ``allow_once``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from ciao.models import BridgeMode, PermissionRequestEvent, StreamEvent, SystemStatusEvent
from ciao.providers.acp.spec import AgentSpec, ModeChoice
from ciao.providers.base import QuestionResponseResult

# Same bound as ``normalize._summary``: a shell command shown on a card.
_COMMAND_SUMMARY_CHARS = 200
_ALLOW_KINDS = ("allow_once", "allow_always")
_REJECT_KINDS = ("reject_once", "reject_always")


def mode_choices(spec: AgentSpec, mode: BridgeMode) -> tuple[ModeChoice, ...]:
    """The config options ``mode`` requires, in the spec's order."""
    return tuple(spec.modes.get(mode, ()))


def pick_value(choice: ModeChoice, offered: Sequence[str]) -> str | None:
    """First of ``choice.values`` the agent offers; None when it offers none of them."""
    for value in choice.values:
        if value in offered:
            return value
    return None


def _title(tool_call: Mapping[str, Any]) -> str:
    title = tool_call.get("title")
    if isinstance(title, str) and title:
        return title
    kind = tool_call.get("kind")
    if isinstance(kind, str) and kind:
        return kind
    return "tool"


def _tool_input(tool_call: Mapping[str, Any], title: str) -> str:
    """The card summary ``normalize._summary`` would show for this tool call."""
    raw = tool_call.get("rawInput")
    if isinstance(raw, Mapping):
        command = raw.get("command")
        if isinstance(command, str) and command:
            return command[:_COMMAND_SUMMARY_CHARS]
    locations = tool_call.get("locations")
    if isinstance(locations, list):
        for item in locations:
            if isinstance(item, Mapping):
                path = item.get("path")
                if isinstance(path, str) and path:
                    return path
    return title


def _selected(options: Sequence[Mapping[str, Any]], kinds: tuple[str, ...]) -> dict[str, Any] | None:
    for kind in kinds:
        for option in options:
            if option.get("kind") == kind:
                return {"outcome": "selected", "optionId": option.get("optionId")}
    return None


def _outcome(options: Sequence[Mapping[str, Any]], *, approved: bool) -> dict[str, Any]:
    kinds = _ALLOW_KINDS if approved else _REJECT_KINDS
    return _selected(options, kinds) or {"outcome": "cancelled"}


@dataclass(slots=True)
class PendingPermission:
    request_id: str
    session_id: str
    options: list[dict[str, Any]]
    future: asyncio.Future[dict[str, Any]]  # resolves to the ACP outcome dict


class PermissionBroker:
    """Holds one session's unanswered permission prompts."""

    def __init__(self, emit: Callable[[StreamEvent], None]) -> None:
        self.emit = emit
        self.unattended = False  # set per prompt from AgentRequest.unattended
        self._pending: dict[str, PendingPermission] = {}

    async def request(
        self,
        session_id: str,
        tool_call: Mapping[str, Any],
        options: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        raw_id = tool_call.get("toolCallId")
        request_id = raw_id if isinstance(raw_id, str) and raw_id else uuid4().hex
        stored = [dict(option) for option in options]
        title = _title(tool_call)
        if self.unattended:
            self.emit(SystemStatusEvent(
                type="system",
                status=f"Denied {title}: scheduled runs cannot wait for approval.",
            ))
            return _outcome(stored, approved=False)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        # A second in-flight prompt with the same toolCallId replaces the
        # pending entry. Cancel the one it replaces so that future cannot
        # stay unanswered, including for cancel_all().
        previous = self._pending.pop(request_id, None)
        if previous is not None and not previous.future.done():
            previous.future.set_result({"outcome": "cancelled"})
        self._pending[request_id] = PendingPermission(request_id, session_id, stored, future)
        self.emit(PermissionRequestEvent(
            type="permission_request",
            message=title,
            tool_name=title,
            tool_input=_tool_input(tool_call, title),
            request_id=request_id,
            session_id=session_id,
        ))
        return await future

    def has(self, request_id: str, session_id: str = "") -> bool:
        item = self._pending.get(request_id)
        if item is None:
            return False
        return not session_id or item.session_id == session_id

    def session_of(self, request_id: str) -> str:
        item = self._pending.get(request_id)
        return item.session_id if item is not None else ""

    def respond(
        self,
        request_id: str,
        approved: bool,
        *,
        session_id: str = "",
    ) -> QuestionResponseResult:
        item = self._pending.get(request_id)
        if item is None:
            return QuestionResponseResult(
                ok=False,
                error="This approval has already been answered.",
                retryable=False,
                already_resolved=True,
            )
        if session_id and item.session_id != session_id:
            return QuestionResponseResult(
                ok=False,
                error="This approval belongs to an older session.",
                retryable=False,
                expired=True,
            )
        self._pending.pop(request_id, None)
        if not item.future.done():
            item.future.set_result(_outcome(item.options, approved=approved))
        return QuestionResponseResult(ok=True)

    def cancel_all(self) -> None:
        """Every pending future gets ``{"outcome": "cancelled"}``."""
        pending = list(self._pending.values())
        self._pending.clear()
        for item in pending:
            if not item.future.done():
                item.future.set_result({"outcome": "cancelled"})
