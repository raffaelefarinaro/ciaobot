"""Track subagent dispatches and completions from a Claude session JSONL.

The Claude Code CLI records everything needed to know which subagents a
session spawned and whether they are still running:

- The Agent/Task ``tool_use`` block on an assistant record carries the
  dispatch's ``id``, ``description``, ``subagent_type``, and
  ``run_in_background`` input.
- The paired ``tool_result`` user record carries ``toolUseResult.agentId``
  (linking the dispatch to the subagent transcript file) and
  ``toolUseResult.isAsync`` for background dispatches.
- When a background subagent finishes, the CLI enqueues a
  ``<task-notification>`` envelope naming the ``<task-id>`` (the agent id)
  and a ``<status>``.

``list_subagents`` in the SDK only enumerates transcript *files*, which
persist after completion, so it can never answer "how many are still
running". Parsing the parent JSONL is the reliable signal, and it also
yields the dispatch → user-turn association the PWA needs to anchor
subagent panels to the turn that spawned them.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_DISPATCH_TOOL_NAMES = {"Agent", "Task", "agent", "task"}

_TASK_NOTIFICATION_RE = re.compile(
    r"<task-notification>(.*?)</task-notification>", re.DOTALL
)
_INNER_TAG_RE = re.compile(r"<([a-z-]+)>(.*?)</\1>", re.DOTALL)

# User-turn skip rules mirrored from the /messages renderer
# (ciao/web/routes_api.py): records matching these never render as user
# bubbles there, so they must not advance the turn counter here either or
# `turn_index` anchoring drifts.
_CONTROL_SLASH_PREFIXES = ("/model", "/mode")
_NO_RESPONSE_SENTINEL = "No response requested."
_INTERRUPTED_REQUEST_SENTINEL = "[Request interrupted by user]"
_CLI_ENVELOPE_TAGS = (
    "task-notification",
    "bash-input",
    "bash-stdout",
    "bash-stderr",
    "bash-exit-code",
    "local-command-stdout",
    "local-command-stderr",
    "local-command-caveat",
    "command-name",
    "command-message",
    "command-args",
    "remote-review",
    "remote-review-progress",
    "teammate-message",
    "cross-session-message",
    "fork-boilerplate",
)
_CLI_ENVELOPE_RE = re.compile(
    r"^\s*<(?:" + "|".join(re.escape(t) for t in _CLI_ENVELOPE_TAGS) + r")(?:\s[^>]*)?>"
)


@dataclass
class SubagentInfo:
    """One subagent dispatched by the parent session."""

    agent_id: str
    tool_use_id: str = ""
    description: str = ""
    subagent_type: str = ""
    is_async: bool = False
    # "running" | "completed" | "failed" | "" (unknown)
    status: str = ""
    # 0-based index of the user turn that dispatched this agent, aligned with
    # the `turn_index` the /messages endpoint stamps on user bubbles. None
    # when the dispatch happened before any countable user turn.
    turn_index: int | None = None


@dataclass
class SessionSubagentState:
    """Aggregate subagent state parsed from a parent session JSONL."""

    subagents: dict[str, SubagentInfo] = field(default_factory=dict)

    @property
    def running_background(self) -> int:
        return sum(
            1
            for info in self.subagents.values()
            if info.is_async and info.status == "running"
        )


def find_parent_session_file(session_id: str, workspace_root: Path | str) -> Path | None:
    """Locate the parent session JSONL for ``session_id`` on this machine."""
    if not session_id:
        return None
    try:
        from ciao.transcripts import _claude_projects_dir

        preferred = _claude_projects_dir(Path(workspace_root)) / f"{session_id}.jsonl"
        if preferred.exists():
            return preferred
    except Exception:  # noqa: BLE001 — fall through to the glob scan
        pass
    projects_root = Path.home() / ".claude" / "projects"
    try:
        for path in projects_root.glob(f"*/{session_id}.jsonl"):
            return path
    except OSError:
        pass
    return None


def _text_content(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def _is_countable_user_turn(content: str) -> bool:
    text = content.strip()
    if not text:
        return False
    head = text.split(None, 1)[0]
    if head in _CONTROL_SLASH_PREFIXES:
        return False
    if text in (_NO_RESPONSE_SENTINEL, _INTERRUPTED_REQUEST_SENTINEL):
        return False
    if _CLI_ENVELOPE_RE.match(text):
        return False
    return True


def _notification_fields(content: str) -> dict[str, str] | None:
    m = _TASK_NOTIFICATION_RE.search(content)
    if not m:
        return None
    return {tag: text.strip() for tag, text in _INNER_TAG_RE.findall(m.group(1))}


def _normalize_agent_id(agent_id: str) -> str:
    return agent_id.removeprefix("agent-")


def parse_session_subagents(path: Path) -> SessionSubagentState:
    """Parse subagent dispatch/completion state out of a session JSONL."""
    state = SessionSubagentState()
    # Dispatch metadata keyed by the Agent tool_use id, joined to an agent_id
    # when the tool_result record lands.
    dispatch_inputs: dict[str, dict[str, str]] = {}
    user_idx = 0

    try:
        fh = path.open(encoding="utf-8")
    except OSError:
        return state

    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue

            rtype = record.get("type")

            if rtype == "queue-operation":
                # A completion notification can be enqueued and later removed
                # without ever becoming a user record (e.g. the process exits
                # first), so the enqueue itself must count as completion.
                content = record.get("content")
                if record.get("operation") == "enqueue" and isinstance(content, str):
                    _apply_notification(state, content)
                continue

            message = record.get("message")

            if rtype == "assistant" and isinstance(message, dict):
                blocks = message.get("content")
                if not isinstance(blocks, list):
                    continue
                for block in blocks:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_use"
                        and block.get("name") in _DISPATCH_TOOL_NAMES
                        and block.get("id")
                    ):
                        tool_input = block.get("input") or {}
                        if not isinstance(tool_input, dict):
                            tool_input = {}
                        dispatch_inputs[str(block["id"])] = {
                            "description": str(tool_input.get("description") or ""),
                            "subagent_type": str(tool_input.get("subagent_type") or ""),
                        }
                continue

            if rtype != "user":
                continue

            tool_use_result = record.get("toolUseResult")
            if isinstance(tool_use_result, dict) and tool_use_result.get("agentId"):
                agent_id = _normalize_agent_id(str(tool_use_result["agentId"]))
                tool_use_id = _tool_result_use_id(message)
                dispatched = dispatch_inputs.get(tool_use_id, {})
                is_async = bool(tool_use_result.get("isAsync"))
                existing = state.subagents.get(agent_id)
                # A completion notification can precede the tool_result in
                # rare orderings; never downgrade a completed agent back to
                # running.
                status = "running" if is_async else "completed"
                if existing is not None and existing.status not in ("", "running"):
                    status = existing.status
                state.subagents[agent_id] = SubagentInfo(
                    agent_id=agent_id,
                    tool_use_id=tool_use_id,
                    description=str(
                        tool_use_result.get("description")
                        or dispatched.get("description")
                        or ""
                    ),
                    subagent_type=dispatched.get("subagent_type", ""),
                    is_async=is_async,
                    status=status,
                    turn_index=user_idx - 1 if user_idx > 0 else None,
                )
                continue

            content = _text_content(message)
            if _notification_fields(content) is not None:
                _apply_notification(state, content)
                continue
            if _is_countable_user_turn(content):
                user_idx += 1

    return state


def _tool_result_use_id(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            return str(block.get("tool_use_id") or "")
    return ""


def _apply_notification(state: SessionSubagentState, content: str) -> None:
    fields = _notification_fields(content)
    if not fields:
        return
    task_id = _normalize_agent_id(fields.get("task-id", ""))
    if not task_id:
        return
    status = fields.get("status", "") or "completed"
    if status not in ("completed", "failed"):
        # The CLI's vocabulary may grow; anything non-failed counts as done
        # for "is it still running" purposes.
        status = "failed" if "fail" in status or "error" in status else "completed"
    info = state.subagents.get(task_id)
    if info is None:
        # Notification for an agent we never saw dispatched at parent level
        # (e.g. an agent spawned by another subagent). Record it so the
        # transcript endpoint can still attach a status.
        state.subagents[task_id] = SubagentInfo(
            agent_id=task_id, is_async=True, status=status
        )
    else:
        info.status = status
