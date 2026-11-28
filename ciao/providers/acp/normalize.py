"""Pure translation of ACP ``session/update`` frames into Ciaobot stream events (#3137).

No I/O and no SDK connection objects: inputs are the plain dicts the SDK gives
``model_dump(by_alias=True)``. The session owner feeds every update of a turn into
one :class:`UpdateNormalizer` and closes the turn with :func:`result_event`.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ciao.models import (
    AssistantTextDelta,
    ModelChangedEvent,
    ResultEvent,
    SessionTitleEvent,
    StreamEvent,
    SystemStatusEvent,
    ThinkingEvent,
    ToolUseEvent,
    tool_call_facts,
    tool_work_dirs,
)
from ciao.tool_summary import summarize_claude_tool_input

logger = logging.getLogger(__name__)

_SNAKE_KEY = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")
# Values under these keys are agent-defined payloads (tool arguments, vendor
# metadata): their keys are data, so they pass through untouched.
_OPAQUE_KEYS = frozenset({"rawInput", "rawOutput", "_meta"})
_COMMAND_SUMMARY_CHARS = 200
# A tool summary, a subagent description and a child title are shown on one line: bounded.
_SUMMARY_CHARS = 200
_SUBAGENT_TYPE_CHARS = 64
_FILE_KINDS = frozenset({"edit", "delete", "move"})
# A child session id names a node in the transcript: bounded, and free of control characters.
_CHILD_ID_CHARS = 128
_LOG_KIND = re.compile(r"[a-z_]{1,40}")
# Claude's subagent tool (`Task` before it was renamed to `Agent`) and OpenCode's `subagent`.
_AGENT_TOOLS = frozenset({"Agent", "Task"})
# A shell call's output kept for the journal: the last lines, bounded, control characters stripped.
# Only this tail is kept; the full output is never stored, and the output file is never read.
_SHELL_TAIL_LINES = 40
_SHELL_TAIL_CHARS = 4096
_SHELL_SCAN_CHARS = 65536
_ANSI = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\^_]|\x1b\][^\n]*"
)
# A launch acknowledgement names the shell (OpenCode: "shell ID: sh_…", Claude: "with ID: …"),
# its pid and the file its output streams to. Each is matched strictly; nothing else is taken.
_SHELL_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_SHELL_ID_TEXT = re.compile(r"(?:shell ID|with ID): ([A-Za-z0-9_-]{1,64})")
# The pid and output path come from structured provider metadata only, never from text the
# command printed (a command may echo "PID: 1234" or "streaming to: ...").
# No ``..`` or ``.`` segment may appear: the path must be the one the shell really wrote to.
_SHELL_OUTPUT_PATH = re.compile(
    r"/(?:(?!\.{1,2}/)[^/\s]+/)*(?:shell/[0-9a-f]{1,64}/sh_[A-Za-z0-9_-]{1,64}\.out|tasks/[A-Za-z0-9_-]{1,64}\.output)"
)
_SHELL_PATH_CHARS = 512


def bounded_text(value: object, limit: int = _SUMMARY_CHARS) -> str:
    """``value`` when it is a string, without Unicode control characters, cut to ``limit``; else ``""``."""
    if not isinstance(value, str):
        return ""
    kept = "".join(ch for ch in value if not unicodedata.category(ch).startswith("C"))
    return kept[:limit]


def child_tool_id(child_id: object) -> str | None:
    """``child:<id>`` for a usable child session id, or None when the id is unusable.

    Unusable: not a string, empty, longer than ``_CHILD_ID_CHARS``, or holding a Unicode
    control character. The id is adapter-controlled, so it is checked before any use.
    """
    if not isinstance(child_id, str) or not child_id or len(child_id) > _CHILD_ID_CHARS:
        return None
    if any(unicodedata.category(ch).startswith("C") for ch in child_id):
        return None
    return f"child:{child_id}"


def loggable_kind(kind: object) -> str:
    """A session/update kind name safe to log: the snake_case name, else a fixed placeholder."""
    if isinstance(kind, str) and _LOG_KIND.fullmatch(kind):
        return kind
    return "<invalid>"

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
        # A tool call between two text segments is a paragraph break: without one the
        # reply reads "...shells.hello". Back-to-back chunks (no call between) join unchanged.
        out = ""
        for segment in self._segments:
            if out and segment and not out.endswith("\n") and not segment.startswith("\n"):
                # Trim only at an inserted break: "Looking at the file. " + break reads as trailing spaces.
                head, tail = out.rstrip(" \t"), segment.lstrip(" \t")
                if head and tail:
                    out = head + "\n\n" + tail
                    continue
            out += segment
        return out

    @property
    def final_text(self) -> str | None:
        return self._segments[-1] if self._split else None


@dataclass(slots=True)
class _Call:
    name: str  # the ToolCall ``name`` field, else its title, else its kind
    kind: str
    raw_input: Any
    locations: list[dict[str, Any]]
    parent: str | None
    claude_tool: str  # ``_meta.claudeCode.toolName``, "" when the adapter sends none
    named: bool  # ``name`` came from the ToolCall ``name`` field, so a title does not replace it
    rendered: tuple[str, str, bool] | None = None  # (tool_name, tool_input, shell_background) last announced
    # A shell call's facts from its frames before the result: Claude's `toolResponse` output and
    # background task id, and an exit code a `terminal_exit` meta carries.
    shell_output: str = ""
    shell_task: str = ""
    exit_code: int | None = None


def _claude_tool(update: Mapping[str, Any]) -> str:
    meta = update.get("_meta")
    claude = meta.get("claudeCode") if isinstance(meta, Mapping) else None
    tool = claude.get("toolName") if isinstance(claude, Mapping) else None
    return tool if isinstance(tool, str) else ""


def _display_name(call: _Call) -> str:
    """The canonical tool name a call is shown under: ``Agent`` and ``Skill`` for the subagent and skill tools."""
    if call.name == "subagent" or (call.kind == "think" and call.claude_tool in _AGENT_TOOLS):
        return "Agent"
    if call.name == "skill" or call.claude_tool == "Skill":
        return "Skill"
    return call.name


def _agent_summary(raw: Any) -> str:
    """``[type] description`` as the live Agent summary formats it. The prompt is never kept."""
    if not isinstance(raw, Mapping):
        return ""
    description = bounded_text(raw.get("description"))
    subtype = bounded_text(raw.get("subagent_type") or raw.get("agent"), _SUBAGENT_TYPE_CHARS)
    return bounded_text(summarize_claude_tool_input("Agent", {"description": description, "subagent_type": subtype}))


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
        # OpenCode subagents: the call id each subagent description belongs to, until a child
        # session claims it, and the subagent call id each child session runs under.
        self._subagent_by_description: dict[str, str] = {}
        self._child_anchors: dict[str, str] = {}
        # Background shells acknowledged but not yet ended, in launch order (see `_remember_acked_shell`).
        self._acked_shells: dict[str, None] = {}

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
            # (`{id, parentID, depth, title}`). Its anchor is the subagent call that started it.
            return self.session_anchor(child.get("id"))
        return None

    def session_anchor(self, session_id: object) -> str | None:
        """The tool id a child session's events nest under: its subagent call, else its own ``child:`` node."""
        if isinstance(session_id, str) and session_id in self._child_anchors:
            return self._child_anchors[session_id]
        return child_tool_id(session_id)

    def linked_call(self, session_id: str) -> str | None:
        """The subagent call id a child session runs under, or None when no call claims it."""
        return self._child_anchors.get(session_id)

    def link_child(self, session_id: str, title: str) -> None:
        """Tie a child session to the subagent call whose description equals its title, once.

        opencode sends the child's ``created`` status before the launch acknowledgement that
        names the session, so the description is the first link. The acknowledgement's
        ``sessionID`` (:meth:`_link_session`) confirms it.
        """
        if session_id in self._child_anchors:
            return
        call_id = self._subagent_by_description.pop(title, None)
        if call_id is not None:
            self._child_anchors[session_id] = call_id

    def _link_session(self, session_id: str, call_id: str) -> None:
        self._child_anchors[session_id] = call_id
        for description, pending in list(self._subagent_by_description.items()):
            if pending == call_id:
                del self._subagent_by_description[description]

    def record_prompt_usage(self, usage: Mapping[str, Any] | None) -> None:
        """Keep the ``usage`` of a ``PromptResponse`` (token counts) as strings."""
        for key, value in (canonical(usage) if usage else {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self.usage[re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()] = str(value)

    def feed(self, update: Mapping[str, Any]) -> list[StreamEvent]:
        u = canonical(update)
        kind = u.get("sessionUpdate")
        if kind == "agent_message_chunk":
            text = chunk_text(u)
            if text is None:
                return []
            return [AssistantTextDelta(type="text", text=text, parent_tool_use_id=self.parent_of(u))]
        if kind == "agent_thought_chunk":
            text = chunk_text(u)
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
        if kind == "session_info_update":
            return self._session_info(u)
        if kind == "compaction_update":
            status = u.get("status")
            if status == "in_progress":
                return [SystemStatusEvent(type="system", status="Compacting context")]
            if status == "completed":
                self.compacted = True
                # The occupancy reported before the compaction no longer holds. Claude reports the
                # new one in the usage_update that follows; opencode reports none until its next turn.
                self.usage.pop("context_used", None)
                self.usage.pop("context_pct", None)
                return [SystemStatusEvent(type="system", status="Compacted context")]
            return []
        if kind in _SILENT_KINDS:
            return []
        label = loggable_kind(kind)
        if label not in self._unknown_logged:
            self._unknown_logged.add(label)
            logger.debug("ignoring unknown ACP update kind %s", label)
        return []

    def _tool_call(self, u: dict[str, Any]) -> list[StreamEvent]:
        call_id = u.get("toolCallId")
        if not isinstance(call_id, str) or not call_id:
            return []
        kind = u.get("kind")
        named = _str_field(u, "name") is not None
        call = _Call(
            name=_wire_name(u),
            kind=kind if isinstance(kind, str) else "",
            raw_input=u.get("rawInput"),
            locations=_locations(u.get("locations")),
            parent=self.parent_of(u),
            claude_tool=_claude_tool(u),
            named=named,
        )
        self._calls[call_id] = call
        self._remember_subagent(call_id, call)
        return self._announce(call_id, call)

    def _tool_call_update(self, u: dict[str, Any]) -> list[StreamEvent]:
        call_id = u.get("toolCallId")
        if not isinstance(call_id, str) or not call_id:
            return []
        status = u.get("status")
        parent = self.parent_of(u)
        # A background shell's acknowledgement already ended its call. Its later `terminal_exit`
        # meta (Claude, with terminal output meta on) is the end of the process: a settle, with
        # the exit code when it has one and None when the adapter sends null.
        if call_id in self._acked_shells and call_id not in self._calls and _has_terminal_exit(u):
            self._acked_shells.pop(call_id, None)
            return [ToolUseEvent(
                type="tool_result",
                tool_use_id=call_id,
                parent_tool_use_id=parent,
                is_error=False,
                exit_code=_terminal_exit(u),
            )]
        if status in ("completed", "failed"):
            known = self._calls.pop(call_id, None)
            if known is not None and known.kind == "execute":
                result = _shell_result(call_id, u, status, known, parent)
                if result.background:
                    self._remember_acked_shell(call_id)
                return [result]
            # A subagent's first result only acknowledges the launch: the child keeps running.
            background = known is not None and _display_name(known) == "Agent" and _is_launch_ack(u)
            if background:
                session = _launched_session(u)
                if session is not None:
                    self._link_session(session, call_id)
            return [
                ToolUseEvent(
                    type="tool_result",
                    tool_use_id=call_id,
                    parent_tool_use_id=parent if parent is not None else (known.parent if known else None),
                    is_error=status == "failed",
                    background=background,
                )
            ]
        call = self._calls.get(call_id)
        if call is None:
            return []
        name_field = _str_field(u, "name")
        title = _str_field(u, "title")
        if name_field is not None:
            call.name = name_field
            call.named = True
        elif title is not None and not call.named:
            call.name = title
        if u.get("rawInput") is not None:
            call.raw_input = u["rawInput"]
        if "locations" in u:
            call.locations = _locations(u["locations"])
        kind = u.get("kind")
        if isinstance(kind, str) and kind:
            call.kind = kind
        call.claude_tool = _claude_tool(u) or call.claude_tool
        if call.kind == "execute":
            _remember_shell_frame(call, u)
        self._remember_subagent(call_id, call)
        # The PWA reconciles a re-announced call by tool_use_id (projects.ts
        # `_pushToolLine`). A re-announce is sent only when the rendered name or input changed.
        return self._announce(call_id, call)

    def _remember_acked_shell(self, call_id: str) -> None:
        """Keep a background shell's call id until its process ends (bounded; the oldest goes first)."""
        self._acked_shells[call_id] = None
        while len(self._acked_shells) > _ACKED_SHELL_LIMIT:
            del self._acked_shells[next(iter(self._acked_shells))]

    def _remember_subagent(self, call_id: str, call: _Call) -> None:
        """Keep the description of a subagent call until a child session claims it."""
        if _display_name(call) != "Agent" or not isinstance(call.raw_input, Mapping):
            return
        if call_id in self._child_anchors.values():
            # A repeated update after the child is linked must not offer the description again.
            return
        description = bounded_text(call.raw_input.get("description"))
        if description:
            self._subagent_by_description[description] = call_id

    def _announce(self, call_id: str, call: _Call) -> list[StreamEvent]:
        """One tool_use per rendered (name, input). Returns nothing when a re-announce renders the same."""
        event = self._tool_use(call_id, call)
        rendered = (event.tool_name, event.tool_input, event.shell_background)
        if rendered == call.rendered:
            return []
        call.rendered = rendered
        return [event]

    def _tool_use(self, call_id: str, call: _Call) -> ToolUseEvent:
        raw = call.raw_input
        name = _display_name(call)
        facts_name = call.claude_tool or ("Bash" if call.kind == "execute" else name)
        input_chars, control_surface = tool_call_facts(facts_name, raw)
        touches = (
            [{"file_path": loc["path"], "action": "edit"} for loc in call.locations]
            if call.kind in _FILE_KINDS and call.locations
            else None
        )
        return ToolUseEvent(
            type="tool_use",
            tool_name=name,
            tool_input=_summary(call, raw, name),
            tool_use_id=call_id,
            parent_tool_use_id=call.parent,
            file_touches=touches,
            work_dirs=tool_work_dirs("Bash", raw) or None if call.kind == "execute" else None,
            input_chars=input_chars,
            control_surface=control_surface,
            shell=call.kind == "execute",
            shell_background=call.kind == "execute" and _shell_background(raw),
        )

    def _session_info(self, u: Mapping[str, Any]) -> list[StreamEvent]:
        # A null or blank title means the agent has none yet: nothing to apply.
        title = u.get("title")
        if not isinstance(title, str) or not title.strip():
            return []
        return [SessionTitleEvent(type="session_title", title=title.strip())]

    def _usage_update(self, u: Mapping[str, Any]) -> None:
        used, size = u.get("used"), u.get("size")
        if used is not None:
            self.usage["context_used"] = str(used)
        if size is not None:
            self.usage["context_size"] = str(size)
        pct = context_pct(self.usage.get("context_used"), self.usage.get("context_size"))
        if pct:
            self.usage["context_pct"] = pct
        else:
            self.usage.pop("context_pct", None)
        # ACP's ``cost.amount`` is the session's cumulative cost, not this turn's: ``cost_usd`` rides
        # each turn's usage as the running total so far (nothing sums it per turn).
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


def context_pct(used: str | None, size: str | None) -> str:
    """The share of the context window in use, as the PWA reads it ("13.2%"), or "" when unknown.

    Both adapters report ``used`` and ``size`` of a ``usage_update`` in tokens (verified against
    claude-agent-acp 0.108.1 and opencode 2.4.2). A window of 0 or a non-number yields "".
    """
    try:
        used_tokens, window = float(used or ""), float(size or "")
    except ValueError:
        return ""
    if not (window > 0 and used_tokens >= 0):
        return ""
    return f"{used_tokens / window * 100:.1f}%"


_SILENT_KINDS = frozenset(
    {
        "plan",
        "available_commands_update",
        "notice",
        "user_message_chunk",
        "compaction_summary_chunk",
    }
)


def chunk_text(u: Mapping[str, Any]) -> str | None:
    """The text of a canonical message chunk, or None when its content is not a text block."""
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


def _summary(call: _Call, raw: Any, name: str) -> str:
    """The one-line input shown for a call: its subagent or skill facts, else command, path or raw input.

    The tool name is never the fallback: a call with no input shows an empty summary.
    """
    if name == "Agent":
        return _agent_summary(raw)
    if name == "Skill":
        skill = (raw.get("name") or raw.get("skill")) if isinstance(raw, Mapping) else None
        return bounded_text(summarize_claude_tool_input("Skill", {"skill": bounded_text(skill)}))
    if call.kind == "execute":
        # A shell call is its command, one line of plain text; its working directory is not its summary.
        command = raw.get("command") if isinstance(raw, Mapping) else None
        return bounded_text(command, _COMMAND_SUMMARY_CHARS) if isinstance(command, str) else ""
    if isinstance(raw, Mapping):
        command = raw.get("command")
        if isinstance(command, str) and command:
            return command[:_COMMAND_SUMMARY_CHARS]
    if call.locations:
        return str(call.locations[0]["path"])
    if isinstance(raw, Mapping) and raw:
        return bounded_text(summarize_claude_tool_input(name, dict(raw)))
    return ""


def _str_field(u: Mapping[str, Any], key: str) -> str | None:
    value = u.get(key)
    return value if isinstance(value, str) and value else None


def _wire_name(u: Mapping[str, Any]) -> str:
    """The ToolCall ``name`` field when the schema sends one, else the title, else the kind, else ``tool``."""
    return _str_field(u, "name") or _str_field(u, "title") or _str_field(u, "kind") or "tool"


def _launch_metadata(u: Mapping[str, Any]) -> Mapping[str, Any] | None:
    raw = u.get("rawOutput")
    meta = raw.get("metadata") if isinstance(raw, Mapping) else None
    return meta if isinstance(meta, Mapping) else None


def _is_launch_ack(u: Mapping[str, Any]) -> bool:
    """opencode's result for a subagent sent in the background: ``metadata.status == "running"``."""
    meta = _launch_metadata(u)
    return meta is not None and meta.get("status") == "running"


def _launched_session(u: Mapping[str, Any]) -> str | None:
    """The child session id a launch acknowledgement names, when it is a usable id."""
    meta = _launch_metadata(u)
    session = meta.get("sessionID") if meta is not None else None
    return session if child_tool_id(session) is not None else None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _shell_background(raw: Any) -> bool:
    """A shell call started in the background: OpenCode's ``background`` or Claude's ``run_in_background``."""
    return isinstance(raw, Mapping) and (raw.get("background") is True or raw.get("run_in_background") is True)


def _content_text(u: Mapping[str, Any]) -> str:
    """The text of a tool call's ``content`` blocks, one block per line."""
    content = u.get("content")
    parts: list[str] = []
    for block in content if isinstance(content, list) else []:
        inner = block.get("content") if isinstance(block, Mapping) else None
        if isinstance(inner, Mapping) and inner.get("type") == "text" and isinstance(inner.get("text"), str):
            parts.append(inner["text"])
    return "\n".join(parts)


def _has_terminal_exit(u: Mapping[str, Any]) -> bool:
    """True when the frame carries a ``terminal_exit`` meta, whatever its exit code."""
    meta = u.get("_meta")
    return isinstance(meta, Mapping) and isinstance(meta.get("terminal_exit"), Mapping)


def _terminal_exit(u: Mapping[str, Any]) -> int | None:
    """The exit code a ``terminal_exit`` meta carries (Claude), None when it sends none or a null."""
    meta = u.get("_meta")
    ended = meta.get("terminal_exit") if isinstance(meta, Mapping) else None
    code = ended.get("exit_code") if isinstance(ended, Mapping) else None
    return code if _is_int(code) else None


# Background shells remembered for their terminal_exit: a session rarely has more than a few.
_ACKED_SHELL_LIMIT = 64


def _remember_shell_frame(call: _Call, u: Mapping[str, Any]) -> None:
    """Keep a shell call's Claude ``toolResponse`` output and task id, and any exit code, for its result."""
    meta = u.get("_meta")
    claude = meta.get("claudeCode") if isinstance(meta, Mapping) else None
    response = claude.get("toolResponse") if isinstance(claude, Mapping) else None
    if isinstance(response, Mapping):
        task = response.get("backgroundTaskId")
        if isinstance(task, str) and _SHELL_ID.fullmatch(task):
            call.shell_task = task
        output = "\n".join(
            text for text in (response.get("stdout"), response.get("stderr")) if isinstance(text, str) and text
        )
        if output:
            call.shell_output = output
    # With terminal output meta, the same text streams as `terminal_output_delta`; it is the
    # output only when no `toolResponse` gave any (a background acknowledgement is that text).
    delta = meta.get("terminal_output_delta") if isinstance(meta, Mapping) else None
    data = delta.get("data") if isinstance(delta, Mapping) else None
    if isinstance(data, str) and data and not call.shell_output:
        call.shell_output = data
    code = _terminal_exit(u)
    if code is not None:
        call.exit_code = code


def sanitize_output(text: str) -> str:
    """``text`` with ANSI and OSC sequences and every non-printing character removed.

    Kept: newlines and tabs. Removed: every Unicode category C character (C0 and C1 controls,
    bidi overrides, zero-width and format characters, tag characters), the line and paragraph
    separators, and an unterminated OSC sequence to the end of its line.
    """
    plain = _ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        ch for ch in plain
        if ch in "\n\t" or (not unicodedata.category(ch).startswith("C") and ch not in _LINE_SEPARATORS)
    )


_LINE_SEPARATORS = frozenset("\u2028\u2029")


def shell_output_tail(text: str) -> str:
    """The last ``_SHELL_TAIL_LINES`` lines and ``_SHELL_TAIL_CHARS`` characters of a shell's output.

    ANSI sequences and control characters (tabs and newlines aside) are removed. Only the final
    ``_SHELL_SCAN_CHARS`` of ``text`` are read, so a huge output costs a bounded scan.
    """
    plain = sanitize_output(text[-_SHELL_SCAN_CHARS:])
    tail = "\n".join(plain.split("\n")[-_SHELL_TAIL_LINES:])
    return tail[-_SHELL_TAIL_CHARS:].strip("\n")


def _shell_id(meta: Mapping[str, Any], task: str, text: str) -> str:
    """The shell id a launch acknowledgement names: OpenCode metadata, Claude's task id, else the text."""
    for candidate in (meta.get("shellID"), task):
        if isinstance(candidate, str) and _SHELL_ID.fullmatch(candidate):
            return candidate
    match = _SHELL_ID_TEXT.search(text)
    return match.group(1) if match else ""


def _shell_pid(meta: Mapping[str, Any]) -> int | None:
    """The shell's pid from structured launch metadata (OpenCode ``metadata.pid``); never from text."""
    pid = meta.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool) and 0 < pid < 2**31:
        return pid
    return None


def _shell_output_path(meta: Mapping[str, Any]) -> str:
    """The output file from structured launch metadata, only when it lies where a shell writes its output.

    No provider metadata carries it today (the fixtures show it only in the acknowledgement text,
    which is never read), so this is empty until an adapter sends ``metadata.outputPath``.
    """
    path = meta.get("outputPath")
    if not isinstance(path, str) or len(path) > _SHELL_PATH_CHARS or ".." in path.split("/"):
        return ""
    return path if _SHELL_OUTPUT_PATH.fullmatch(path) else ""


def _shell_result(
    call_id: str,
    u: Mapping[str, Any],
    status: str,
    known: _Call,
    parent: str | None,
) -> ToolUseEvent:
    """The ``tool_result`` for a shell call.

    A background shell's first result is its launch acknowledgement: ``background`` is set, the
    shell id, pid and output path are read from it, and no output or exit code is taken (the
    shell is still running). Any other result carries the foreground exit code (None when the
    agent sends none) and the output tail.
    """
    meta = _launch_metadata(u) or {}
    text = _content_text(u)
    raw = u.get("rawOutput")
    ack_text = "\n".join(part for part in (text, raw if isinstance(raw, str) else "", known.shell_output) if part)
    is_ack = status == "completed" and _shell_background(known.raw_input) and (
        meta.get("status") == "running" or bool(known.shell_task) or _SHELL_ID_TEXT.search(ack_text) is not None
    )
    parent_id = parent if parent is not None else known.parent
    if is_ack:
        return ToolUseEvent(
            type="tool_result",
            tool_use_id=call_id,
            parent_tool_use_id=parent_id,
            is_error=False,
            background=True,
            shell_id=_shell_id(meta, known.shell_task, ack_text),
            shell_pid=_shell_pid(meta),
            output_path=_shell_output_path(meta),
        )
    code = meta.get("exit")
    if not _is_int(code):
        code = _terminal_exit(u)
    if code is None:
        code = known.exit_code
    output = text or known.shell_output
    return ToolUseEvent(
        type="tool_result",
        tool_use_id=call_id,
        parent_tool_use_id=parent_id,
        is_error=status == "failed",
        exit_code=code,
        output_tail=shell_output_tail(output) if output else "",
    )


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
        # The value is adapter-controlled and unbounded: neither quoted to the PWA nor
        # logged (CodeQL flags it as clear-text logging); only its length is recorded.
        logger.warning("ACP agent ended a turn with an unrecognised stop reason (%d chars)", len(str(stop_reason)))
        body = "The agent ended the turn for an unrecognised reason."
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
