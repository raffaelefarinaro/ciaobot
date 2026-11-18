"""Pure translation of ACP ``session/update`` frames into Ciaobot stream events (#3137).

No I/O and no SDK connection objects: inputs are the plain dicts the SDK gives
``model_dump(by_alias=True)``. The session owner feeds every update of a turn into
one :class:`UpdateNormalizer` and closes the turn with :func:`result_event`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ciao.models import (
    AssistantTextDelta,
    ModelChangedEvent,
    ResultEvent,
    StreamEvent,
    SystemStatusEvent,
    ThinkingEvent,
    ToolUseEvent,
    tool_call_facts,
    tool_work_dirs,
)

logger = logging.getLogger(__name__)

_SNAKE_KEY = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
# Values under these keys are agent-defined payloads (tool arguments, vendor
# metadata): their keys are data, so they pass through untouched.
_OPAQUE_KEYS = frozenset({"rawInput", "rawOutput", "_meta"})
_COMMAND_SUMMARY_CHARS = 200
_FILE_KINDS = frozenset({"edit", "delete", "move"})

_OUTPUT_LIMIT_TEXT = "The model hit its output limit before finishing."
_TOO_MANY_STEPS_TEXT = "The agent stopped after too many steps in one turn."
_REFUSAL_TEXT = "The model declined to continue this request."


class _ModelSpec(Protocol):
    """The slice of ``AgentSpec`` the normalizer reads."""

    @property
    def model_config_id(self) -> str: ...


def _camel(key: str) -> str:
    head, *rest = key.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def canonical(update: Mapping[str, Any]) -> dict[str, Any]:
    """camelCase keys win over snake_case duplicates, recursively; snake-only keys are camel-cased.

    Opaque payloads (``rawInput``, ``rawOutput``, ``_meta``) keep their keys.
    """
    return {k: v for k, v in _canonical_items(update)}


def _canonical_items(node: Mapping[str, Any]) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for key, value in node.items():
        name = _camel(key) if _SNAKE_KEY.match(key) else key
        if name != key and name in node:
            continue  # the camelCase twin wins
        out.append((name, value if name in _OPAQUE_KEYS else _canonical_value(value)))
    return out


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return canonical(value)
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    return value


@dataclass(slots=True)
class TurnText:
    """Top-level text of one turn.

    ``result`` is all of it; ``final_text`` the last segment after the last
    top-level tool call. Subagent text (events with a parent) is excluded.
    """

    _segments: list[str] = field(default_factory=lambda: [""])
    _split: bool = False
    _seen_tools: set[str] = field(default_factory=set)

    def feed(self, event: StreamEvent) -> None:
        if isinstance(event, AssistantTextDelta):
            if event.parent_tool_use_id is None:
                self._segments[-1] += event.text
        elif isinstance(event, ToolUseEvent) and event.type == "tool_use":
            if event.parent_tool_use_id is not None:
                return
            # A refresh re-announces a call already seen: it does not split the text.
            if event.tool_use_id is not None:
                if event.tool_use_id in self._seen_tools:
                    return
                self._seen_tools.add(event.tool_use_id)
            self._split = True
            self._segments.append("")

    @property
    def result(self) -> str:
        return "".join(self._segments)

    @property
    def final_text(self) -> str | None:
        return self._segments[-1] if self._split else None


@dataclass(slots=True)
class _Call:
    name: str
    kind: str
    raw_input: Any
    locations: list[dict[str, Any]]
    parent: str | None


class UpdateNormalizer:
    """Stateful per-session translator: one instance carries tool-call facts across updates."""

    def __init__(self, spec: _ModelSpec, session_id: str) -> None:
        self._model_config_id = spec.model_config_id
        self.session_id = session_id
        self.usage: dict[str, str] = {}
        self.current_values: dict[str, str] = {}
        self.compacted = False
        self._calls: dict[str, _Call] = {}
        self._unknown_logged: set[str] = set()

    def parent_of(self, update: Mapping[str, Any]) -> str | None:
        meta = update.get("_meta")
        if not isinstance(meta, Mapping):
            return None
        claude = meta.get("claudeCode")
        if isinstance(claude, Mapping):
            parent = claude.get("parentToolUseId")
            if isinstance(parent, str) and parent:
                return parent
        child = meta.get("opencode/child-session")
        if isinstance(child, Mapping):
            # opencode tags a child's frames with the child session itself
            # (`{id, parentID, depth, title}`); it carries no parent tool-call id.
            child_id = child.get("id")
            if isinstance(child_id, str) and child_id:
                return child_id
        return None

    def record_prompt_usage(self, usage: Mapping[str, Any] | None) -> None:
        """Keep the ``usage`` of a ``PromptResponse`` (token counts) as strings."""
        for key, value in (canonical(usage) if usage else {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self.usage[re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()] = str(value)

    def feed(self, update: Mapping[str, Any]) -> list[StreamEvent]:
        u = canonical(update)
        kind = u.get("sessionUpdate")
        if kind == "agent_message_chunk":
            text = _chunk_text(u)
            if text is None:
                return []
            return [AssistantTextDelta(type="text", text=text, parent_tool_use_id=self.parent_of(u))]
        if kind == "agent_thought_chunk":
            text = _chunk_text(u)
            if text is None:
                return []
            return [ThinkingEvent(type="thinking", text=text, parent_tool_use_id=self.parent_of(u))]
        if kind == "tool_call":
            return self._tool_call(u)
        if kind == "tool_call_update":
            return self._tool_call_update(u)
        if kind == "usage_update":
            self._usage_update(u)
            return []
        if kind == "config_option_update":
            return self._config_options(u)
        if kind == "current_mode_update":
            mode = u.get("currentModeId")
            if isinstance(mode, str):
                self.current_values["mode"] = mode
            return []
        if kind == "compaction_update":
            if u.get("status") == "completed":
                self.compacted = True
                return [SystemStatusEvent(type="system", status="Compacted context")]
            return []
        if kind in _SILENT_KINDS:
            return []
        label = str(kind)
        if label not in self._unknown_logged:
            self._unknown_logged.add(label)
            logger.debug("ignoring unknown ACP update kind %r", label)
        return []

    def _tool_call(self, u: dict[str, Any]) -> list[StreamEvent]:
        call_id = u.get("toolCallId")
        if not isinstance(call_id, str) or not call_id:
            return []
        title = u.get("title")
        kind = u.get("kind")
        call = _Call(
            name=(title if isinstance(title, str) and title else "") or (kind if isinstance(kind, str) and kind else "") or "tool",
            kind=kind if isinstance(kind, str) else "",
            raw_input=u.get("rawInput"),
            locations=_locations(u.get("locations")),
            parent=self.parent_of(u),
        )
        self._calls[call_id] = call
        return [self._tool_use(call_id, call, u)]

    def _tool_call_update(self, u: dict[str, Any]) -> list[StreamEvent]:
        call_id = u.get("toolCallId")
        if not isinstance(call_id, str) or not call_id:
            return []
        status = u.get("status")
        parent = self.parent_of(u)
        if status in ("completed", "failed"):
            known = self._calls.pop(call_id, None)
            return [
                ToolUseEvent(
                    type="tool_result",
                    tool_use_id=call_id,
                    parent_tool_use_id=parent if parent is not None else (known.parent if known else None),
                    is_error=status == "failed",
                )
            ]
        call = self._calls.get(call_id)
        if call is None:
            return []
        changed = False
        title = u.get("title")
        if isinstance(title, str) and title and title != call.name:
            call.name = title
            changed = True
        if u.get("rawInput") is not None and u.get("rawInput") != call.raw_input:
            call.raw_input = u["rawInput"]
            changed = True
        if "locations" in u:
            locations = _locations(u["locations"])
            if locations != call.locations:
                call.locations = locations
                changed = True
        kind = u.get("kind")
        if isinstance(kind, str) and kind and kind != call.kind:
            call.kind = kind
            changed = True
        # The PWA reconciles a re-announced call by tool_use_id (projects.ts
        # `_pushToolLine`), so a fuller title or input replaces the first line.
        return [self._tool_use(call_id, call, u)] if changed else []

    def _tool_use(self, call_id: str, call: _Call, u: Mapping[str, Any]) -> ToolUseEvent:
        raw = call.raw_input
        facts_name = call.name
        meta = u.get("_meta")
        claude = meta.get("claudeCode") if isinstance(meta, Mapping) else None
        if isinstance(claude, Mapping) and isinstance(claude.get("toolName"), str):
            facts_name = claude["toolName"]
        elif call.kind == "execute":
            facts_name = "Bash"
        input_chars, control_surface = tool_call_facts(facts_name, raw)
        touches = (
            [{"file_path": loc["path"], "action": "edit"} for loc in call.locations]
            if call.kind in _FILE_KINDS and call.locations
            else None
        )
        return ToolUseEvent(
            type="tool_use",
            tool_name=call.name,
            tool_input=_summary(call, raw),
            tool_use_id=call_id,
            parent_tool_use_id=call.parent,
            file_touches=touches,
            work_dirs=tool_work_dirs("Bash", raw) or None if call.kind == "execute" else None,
            input_chars=input_chars,
            control_surface=control_surface,
        )

    def _usage_update(self, u: Mapping[str, Any]) -> None:
        used, size = u.get("used"), u.get("size")
        if used is not None:
            self.usage["context_used"] = str(used)
        if size is not None:
            self.usage["context_size"] = str(size)
        cost = u.get("cost")
        if isinstance(cost, Mapping) and cost.get("amount") is not None:
            self.usage["cost_usd"] = str(cost["amount"])

    def _config_options(self, u: Mapping[str, Any]) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        options = u.get("configOptions")
        for option in options if isinstance(options, list) else []:
            if not isinstance(option, Mapping):
                continue
            option_id, value = option.get("id"), option.get("currentValue")
            if not isinstance(option_id, str) or value is None:
                continue
            value = str(value)
            previous = self.current_values.get(option_id)
            self.current_values[option_id] = value
            if option_id == self._model_config_id and value != previous:
                events.append(ModelChangedEvent(type="model_changed", model=value))
        return events


_SILENT_KINDS = frozenset(
    {
        "plan",
        "available_commands_update",
        "session_info_update",
        "notice",
        "user_message_chunk",
        "compaction_summary_chunk",
    }
)


def _chunk_text(u: Mapping[str, Any]) -> str | None:
    content = u.get("content")
    if isinstance(content, Mapping) and content.get("type") == "text":
        text = content.get("text")
        if isinstance(text, str):
            return text
    return None


def _locations(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, Mapping) and isinstance(item.get("path"), str) and item["path"]:
            out.append(dict(item))
    return out


def _summary(call: _Call, raw: Any) -> str:
    if isinstance(raw, Mapping):
        command = raw.get("command")
        if isinstance(command, str) and command:
            return command[:_COMMAND_SUMMARY_CHARS]
    if call.locations:
        return str(call.locations[0]["path"])
    return call.name


def result_event(
    stop_reason: str,
    *,
    text: TurnText,
    session_id: str,
    model: str,
    usage: Mapping[str, str],
    origin: str = "",
) -> ResultEvent:
    """The terminal event for one ``session/prompt`` response."""
    is_error, stopped = True, False
    if stop_reason == "end_turn":
        is_error, body = False, text.result
    elif stop_reason == "cancelled":
        is_error, stopped, body = False, True, text.result
    elif stop_reason == "max_tokens":
        body = text.result or _OUTPUT_LIMIT_TEXT
    elif stop_reason == "max_turn_requests":
        body = _TOO_MANY_STEPS_TEXT
    elif stop_reason == "refusal":
        body = _REFUSAL_TEXT
    else:
        body = f"The agent ended the turn with an unknown reason: {stop_reason}"
    return ResultEvent(
        type="result",
        result=body,
        session_id=session_id,
        is_error=is_error,
        effective_model=model,
        usage=dict(usage),
        stopped=stopped,
        origin=origin,
        final_text=text.final_text,
    )
