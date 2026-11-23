"""ACP permission prompts and the mode value an agent is asked to hold (#3137).

The broker answers ``session/request_permission``. It never approves on its
own: attended cards require an unambiguous ``allow_once``; only one-time
approval and rejection options are selected. Unattended turns never get a card,
a reply for another session is refused, and cancel answers every pending
request with ``cancelled``.
"""

from __future__ import annotations

import asyncio
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from ciao.models import (
    BridgeMode,
    PermissionRequestEvent,
    PermissionResolvedEvent,
    StreamEvent,
    SystemStatusEvent,
)
from ciao.providers.acp.spec import AgentSpec, ModeChoice
from ciao.providers.base import QuestionResponseResult

_COMMAND_SUMMARY_CHARS = 200
_SHELL_COMMAND_CHARS = 4096


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


def _denied_kind(tool_call: Mapping[str, Any]) -> str:
    """The ACP tool kind (``execute``, ``edit``, ...), or ``tool`` when none is given."""
    kind = tool_call.get("kind")
    if isinstance(kind, str) and kind.isidentifier():
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


def _usable_option_id(option: Mapping[str, Any]) -> str | None:
    option_id = option.get("optionId")
    if isinstance(option_id, str) and option_id.strip():
        return option_id
    return None


def _selected(options: Sequence[Mapping[str, Any]], kinds: tuple[str, ...]) -> dict[str, Any] | None:
    """Select one matching choice only when its usable ``optionId`` is unique.

    Missing, blank, or duplicate IDs cannot be answered safely.
    """
    matches = [
        (option, _usable_option_id(option))
        for option in options
        if option.get("kind") in kinds
    ]
    matches = [(option, option_id) for option, option_id in matches if option_id is not None]
    if len(matches) != 1:
        return None
    option_id = matches[0][1]
    if sum(_usable_option_id(option) == option_id for option in options) != 1:
        return None
    return {"outcome": "selected", "optionId": option_id}


def _shell_command(tool_call: Mapping[str, Any]) -> tuple[bool, str | None, str | None]:
    raw = tool_call.get("rawInput")
    name = tool_call.get("toolName", tool_call.get("tool_name", tool_call.get("name", "")))
    metadata = tool_call.get("_meta")
    claude = metadata.get("claudeCode") if isinstance(metadata, Mapping) else None
    claude_name = claude.get("toolName") if isinstance(claude, Mapping) else ""
    title = tool_call.get("title")
    shell_names = {"shell", "bash", "terminal"}
    named_shell = any(
        isinstance(candidate, str) and candidate.strip().casefold() in shell_names
        for candidate in (name, claude_name, title)
    )
    if isinstance(title, str):
        title_lower = title.strip().casefold()
        named_shell = named_shell or any(
            title_lower.startswith(f"{shell_name}:") for shell_name in shell_names
        )
    is_shell = tool_call.get("kind") in {"execute", "shell"} or named_shell or (
        isinstance(raw, Mapping) and "command" in raw
    )
    if not is_shell:
        return False, None, None
    command = raw.get("command") if isinstance(raw, Mapping) else None
    if not isinstance(command, str) or not command.strip() or len(command) > _SHELL_COMMAND_CHARS:
        return True, None, None
    if any(unicodedata.category(char) in {"Cc", "Cf"} and char != "\n" for char in command):
        return True, None, None
    cwd = raw.get("cwd") if isinstance(raw, Mapping) else None
    if isinstance(raw, Mapping) and "cwd" in raw:
        if (
            not isinstance(cwd, str)
            or not cwd.strip()
            or len(cwd) > _SHELL_COMMAND_CHARS
            or any(unicodedata.category(char).startswith("C") for char in cwd)
        ):
            return True, None, None
    return True, command, cwd if isinstance(cwd, str) else None


def _usable_allow_once(options: Sequence[Mapping[str, Any]]) -> str | None:
    """Only show an approval when its exact one-time choice is unambiguous."""
    matches = [option for option in options if option.get("kind") == "allow_once"]
    if len(matches) != 1:
        return None
    option_id = _usable_option_id(matches[0])
    if option_id is None:
        return None
    if sum(_usable_option_id(option) == option_id for option in options) != 1:
        return None
    return option_id


@dataclass(slots=True)
class PendingPermission:
    request_id: str
    session_id: str
    options: list[dict[str, Any]]
    future: asyncio.Future[dict[str, Any]]  # resolves to the ACP outcome dict
    tool_call_id: str


class PermissionBroker:
    """Holds one session's unanswered permission prompts."""

    def __init__(self, emit: Callable[[StreamEvent], None]) -> None:
        self.emit = emit
        self.unattended = False  # set per prompt from AgentRequest.unattended
        self._pending: dict[str, PendingPermission] = {}
        self._tool_call_pending: dict[tuple[str, str], str] = {}

    async def request(
        self,
        session_id: str,
        tool_call: Mapping[str, Any],
        options: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        raw_id = tool_call.get("toolCallId")
        tool_call_id = raw_id if isinstance(raw_id, str) and raw_id else ""
        request_id = uuid4().hex
        stored = [dict(option) for option in options]
        tool_call_key = (session_id, tool_call_id)
        previous_id = self._tool_call_pending.pop(tool_call_key, "") if tool_call_id else ""
        previous = self._pending.pop(previous_id, None) if previous_id else None
        if previous is not None:
            self.emit(PermissionResolvedEvent(
                type="permission_resolved",
                request_id=previous.request_id,
                session_id=previous.session_id,
            ))
            if not previous.future.done():
                previous.future.set_result({"outcome": "cancelled"})
        shell_command, command, shell_cwd = _shell_command(tool_call)
        title = _title(tool_call)
        if self.unattended:
            self.emit(SystemStatusEvent(
                type="system",
                status=f"Denied {'shell tool' if shell_command else title}: scheduled runs cannot wait for approval.",
                # The kind, never the title: adapters often put the full shell
                # command (tokens, URLs, newlines) in the title, and this name
                # is copied into the run's recorded error.
                denied_tool="shell" if shell_command else _denied_kind(tool_call),
            ))
            return _selected(stored, ("reject_once",)) or {"outcome": "cancelled"}
        if shell_command and command is None:
            return {"outcome": "cancelled"}
        if _usable_allow_once(stored) is None:
            return {"outcome": "cancelled"}
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        # A second in-flight prompt with the same toolCallId replaces the
        # pending entry. Cancel the one it replaces so that future cannot
        # stay unanswered, including for cancel_all().
        self._pending[request_id] = PendingPermission(request_id, session_id, stored, future, tool_call_id)
        if tool_call_id:
            self._tool_call_pending[tool_call_key] = request_id
        self.emit(PermissionRequestEvent(
            type="permission_request",
            message=title,
            tool_name=title,
            tool_input=command if shell_command and command is not None else _tool_input(tool_call, title),
            shell_command=shell_command,
            shell_cwd=shell_cwd or "",
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
            # Settled already (another tab, or cancelled): acknowledged so the caller clears its card.
            return QuestionResponseResult(ok=True, already_resolved=True)
        if session_id and item.session_id != session_id:
            return QuestionResponseResult(
                ok=False,
                error="This approval belongs to an older session.",
                retryable=False,
                expired=True,
            )
        self._pending.pop(request_id, None)
        tool_call_key = (item.session_id, item.tool_call_id)
        if item.tool_call_id and self._tool_call_pending.get(tool_call_key) == request_id:
            self._tool_call_pending.pop(tool_call_key, None)
        if not item.future.done():
            kinds = ("allow_once",) if approved else ("reject_once",)
            item.future.set_result(_selected(item.options, kinds) or {"outcome": "cancelled"})
        return QuestionResponseResult(ok=True)

    def cancel_all(self) -> int:
        """Answer every pending future with ``{"outcome": "cancelled"}``.

        Returns how many were still unanswered. The JSON-RPC write of each
        answer happens later, on the connection task that was awaiting it.
        """
        pending = list(self._pending.values())
        self._pending.clear()
        self._tool_call_pending.clear()
        answered = 0
        for item in pending:
            if not item.future.done():
                item.future.set_result({"outcome": "cancelled"})
                answered += 1
        return answered
