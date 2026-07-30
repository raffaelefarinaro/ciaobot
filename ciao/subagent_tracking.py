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
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_DISPATCH_TOOL_NAMES = {"Agent", "Task", "agent", "task"}

# How long a background agent's own transcript must sit untouched before we
# treat it as finished without a ``<task-notification>``. See
# ``has_finished_transcript`` for why that fallback exists.
FINISHED_AGENT_IDLE_SECONDS = 60.0

# Bytes read from the tail of an agent transcript to recover its last record.
_TAIL_WINDOW_BYTES = 65536

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

# Prompt the server injects into the parent turn when its background subagents
# all finish, so the chat doesn't sit on the interim "I'll report back" message
# forever (delivered by
# ``ProjectChatManager._nudge_synthesis_after_subagents``). It lives here
# because three call sites need the same string: the sender, the /messages
# renderer that collapses it into a system line instead of a user bubble, and
# the turn counter below — it is machine-generated, so it must not advance
# `turn_index` any more than the CLI's own synthetic user records do.
SUBAGENT_SYNTHESIS_NUDGE = (
    "The background agent(s) you dispatched have now finished. Review their "
    "results (read their transcripts or output as needed) and post your "
    "consolidated final report for this task now. Do not dispatch new "
    "background agents to answer this. If you already posted the final "
    "report, reply with a brief confirmation instead of repeating it."
)

_WHITESPACE_RE = re.compile(r"\s+")


def _collapse_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


_NUDGE_COLLAPSED = _collapse_whitespace(SUBAGENT_SYNTHESIS_NUDGE)


def is_synthesis_nudge(text: str) -> bool:
    """True when ``text`` is the injected subagent-synthesis nudge.

    Matches on the tail: the nudge is sent with the chat's context prefix
    attached, and the /messages prefix stripper only removes the shapes it
    recognizes.
    """
    return _collapse_whitespace(text).endswith(_NUDGE_COLLAPSED)


# Markdown punctuation that can trail a question mark ("**...?**", "(...?)").
_QUESTION_TAIL_CHARS = "*_`~)]}>\"' \t"


def ends_with_user_question(text: str) -> bool:
    """True when the last prose line of ``text`` reads as a question.

    The synthesis nudge is held back in that case: the parent ended its turn
    by asking the user something, and injecting the nudge answers on their
    behalf and buries the question.
    """
    for line in reversed(text.strip().splitlines()):
        stripped = line.strip().rstrip(_QUESTION_TAIL_CHARS)
        if not stripped:
            continue
        return stripped.endswith("?")
    return False


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
    # Last assistant message that carried prose. The synthesis nudge is held
    # back when it ends in a question to the user.
    last_assistant_text: str = ""

    @property
    def awaiting_user_answer(self) -> bool:
        """True when the parent's last word was a question to the user."""
        return ends_with_user_question(self.last_assistant_text)

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


def subagent_transcript_path(parent_path: Path, agent_id: str) -> Path:
    """Where the CLI keeps ``agent_id``'s transcript for this parent session."""
    return (
        parent_path.parent
        / parent_path.stem
        / "subagents"
        / f"agent-{_normalize_agent_id(agent_id)}.jsonl"
    )


def _last_json_record(path: Path) -> dict | None:
    """Last complete JSON record in ``path``, read from the tail only."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            window = min(size, _TAIL_WINDOW_BYTES)
            fh.seek(size - window)
            chunk = fh.read(window)
    except OSError:
        return None
    for raw in reversed(chunk.splitlines()):
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Either the window cut the oldest line in half, or we caught a
            # torn write on the newest one. Fall back to the next record.
            continue
        if isinstance(record, dict):
            return record
    return None


def _is_final_answer_record(record: dict) -> bool:
    """True when ``record`` is an agent's closing prose message.

    A working agent's tail record is a ``tool_use`` assistant message or a
    ``tool_result`` user message; only the final answer is text (optionally
    preceded by thinking) with nothing left to execute.
    """
    if record.get("type") != "assistant":
        return False
    message = record.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return False
    kinds = [b.get("type") for b in content if isinstance(b, dict)]
    return "text" in kinds and all(k in ("text", "thinking") for k in kinds)


def has_finished_transcript(
    parent_path: Path,
    agent_id: str,
    *,
    now: float | None = None,
    idle_seconds: float = FINISHED_AGENT_IDLE_SECONDS,
) -> bool:
    """True when ``agent_id``'s own transcript shows it is done.

    The parent JSONL only learns a background agent finished when the CLI
    writes a ``<task-notification>``, and that record can be deferred to the
    next turn boundary — or replaced, on session resume, by a synthetic
    ``<status>stopped</status>`` "no completion record was found" envelope.
    Either way the parent's running count can sit above zero long after the
    work landed, with nothing left to ever bring it down.

    The agent's transcript is the corroborating signal: it stops growing the
    moment the agent stops. Both conditions are required so a slow tool call
    (minutes of silence mid-run) can't be mistaken for completion.
    """
    path = subagent_transcript_path(parent_path, agent_id)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        # No transcript yet: the agent was dispatched but hasn't written
        # anything, so it is still starting up.
        return False
    if (time.time() if now is None else now) - mtime < idle_seconds:
        return False
    record = _last_json_record(path)
    return record is not None and _is_final_answer_record(record)


def running_background_agents(
    parent_path: Path,
    state: SessionSubagentState,
    *,
    now: float | None = None,
    idle_seconds: float = FINISHED_AGENT_IDLE_SECONDS,
) -> int:
    """Background agents still running, per the parent *and* their transcripts."""
    return sum(
        1
        for info in state.subagents.values()
        if info.is_async
        and info.status == "running"
        and not has_finished_transcript(
            parent_path, info.agent_id, now=now, idle_seconds=idle_seconds
        )
    )


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
    if is_synthesis_nudge(text):
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
                assistant_text = _text_content(message)
                if assistant_text.strip():
                    state.last_assistant_text = assistant_text
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
