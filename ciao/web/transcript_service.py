"""The chat transcript's read path: stored provider messages -> PWA rows.

``ciao/web/routes_api.py`` owns the HTTP surface of a chat - parsing the
request, resolving the chat off ``app.state``, and shaping the JSON envelope.
Turning what a provider actually stored into the rows the PWA transcript
renders is a separate job, and it is this module's.

It covers the whole read path:

* the per-message rules - which blocks an assistant message contributes,
  which tool calls earn a file card, which user turns are the CLI talking to
  itself rather than the human, and which injected context is stripped back
  off a prompt before it is shown;
* the per-session renderers - the Claude session JSONL, an opencode thread,
  and the nested subagent transcripts;
* the assembly that stitches a chat's sessions, handover messages and durable
  transcript metadata into one chronological list
  (:func:`_assemble_chat_messages`);
* and the last shaping step before the wire, :func:`_prune_rows_for_wire`.

Nothing here touches Starlette: no ``Request``, no response objects, no app
state. Callers pass the manager, the config and the chat, so a test can
exercise any of it without building a request - and ``routes_api`` imports
this as a module rather than by name, so a test that patches one helper has
the handlers see the patch.

What stays with the callers: reading query params, mapping rows onto a
pagination envelope or an HTTP status, and the part-cache that serves one
expanded row.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from ciao import cli_envelopes, subagent_tracking
from ciao.models import ChatContext
from ciao.providers.claude import _summarize_tool_input
from ciao.providers.opencode import (
    OpencodeProvider,
    _file_touches as _opencode_file_touches,
    _summarize_tool_input as _summarize_opencode_tool_input,
)
from ciao.rate_limits import is_rate_limit_telemetry
from ciao.web import chat_service
from ciao.web.chat_broker import extract_file_touches, normalize_file_touch_paths

logger = logging.getLogger(__name__)


_CONTEXT_BLOCK_RE = re.compile(
    r"^\[CIAO_CONTEXT_BEGIN\]\n.*?\n\[CIAO_CONTEXT_END\]\n\n",
    re.DOTALL,
)

# `build_prompt()` in ciao/providers/base.py appends an image manifest block
# (`[INCOMING IMAGES]\n1. filename.png\n2. other.jpg - caption: ...`) to the
# user's text before sending to the Claude SDK, so the SDK has filenames and
# captions alongside the native image blocks. The SDK persists that text
# verbatim in the session file. On replay we re-emit the images separately
# from `chat.user_turn_images`, so the manifest is redundant in the UI and
# shows up as literal text in the user bubble. Strip it here.
_IMAGE_MANIFEST_RE = re.compile(
    r"\n{0,2}\[INCOMING IMAGES\]\n(?:\d+\. [^\n]*(?:\n|$))+\s*$",
)


def _extract_text_content(raw: object) -> str:
    content = ""
    if isinstance(raw, dict):
        content_blocks = raw.get("content", "")
        if isinstance(content_blocks, str):
            content = content_blocks
        elif isinstance(content_blocks, list):
            parts = []
            for block in content_blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            content = "\n".join(parts)
    return content


def _extract_inline_images(raw: object) -> list[str]:
    """Extract inline base64 images from SDK message content blocks.

    Returns a list of data URIs (``data:<mime>;base64,<data>``).
    """
    images: list[str] = []
    if not isinstance(raw, dict):
        return images
    content_blocks = raw.get("content", "")
    if not isinstance(content_blocks, list):
        return images
    for block in content_blocks:
        if not isinstance(block, dict) or block.get("type") != "image":
            continue
        source = block.get("source", {})
        if source.get("type") == "base64":
            media_type = source.get("media_type", "image/jpeg")
            data = source.get("data", "")
            if data:
                images.append(f"data:{media_type};base64,{data}")
    return images


_TOOL_ICONS = {
    "Read": "\U0001F4D6",
    "Edit": "\u270F\uFE0F",
    "Write": "\U0001F4DD",
    "Bash": "$",
    "Grep": "\U0001F50D",
    "Glob": "\U0001F4C2",
    "Agent": "\U0001F916",
    "Skill": "\u26A1",
    "WebSearch": "\U0001F310",
    "WebFetch": "\U0001F310",
    "TaskCreate": "\u2611\uFE0F",
    "TaskUpdate": "\u2611\uFE0F",
    "grep_search": "\U0001F50D",
    "view_file": "\U0001F4D6",
    "run_command": "$",
    "list_dir": "\U0001F4C2",
    "exec_command": "$",
}


def _tool_icon(name: str) -> str:
    return _TOOL_ICONS.get(name, "\u2699\uFE0F")


# Tools whose failure invalidates their file card. A refused or errored `Write`
# either wrote the file or did not run at all; a failed `file_surface` did not
# select an artifact. A `Bash` non-zero exit says no such thing — `printf x > f
# && exit 1` leaves the file behind — so its card stands, or history would hide
# a file the agent really created.
_FAILURE_DROPS_FILE_CARD_TOOLS = frozenset({
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "mcp__ciaobot__file_surface",
})


def _touches_survive_failure(tool_name: str) -> bool:
    """Whether a failed call's file cards should still render."""
    return tool_name not in _FAILURE_DROPS_FILE_CARD_TOOLS


def _failed_tool_use_ids(msgs: list) -> set[str]:
    """Tool-call ids whose ``tool_result`` came back as an error.

    A denied or failed ``Write``/``Edit`` never touched the file, but the file
    card is emitted from the *request*, so history would show an Outputs chip
    for a file that was never created (this is what made a permission-denied
    `skills-monitor.md` look written). Results live on the following user
    message, so they can only be matched in a pre-pass over the whole session.

    Which ids actually suppress a card is decided per tool — see
    ``_touches_survive_failure``.
    """
    failed: set[str] = set()
    for m in msgs:
        # Both SDK objects and raw JSONL dicts flow through here (the subagent
        # renderer accepts either).
        mtype = m.get("type") if isinstance(m, dict) else getattr(m, "type", None)
        if mtype != "user":
            continue
        message = m.get("message") if isinstance(m, dict) else getattr(m, "message", None)
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            failed_result = bool(block.get("is_error"))
            content = block.get("content")
            if not failed_result and isinstance(content, str):
                try:
                    content = json.loads(content)
                except (TypeError, ValueError):
                    content = None
            if (
                not failed_result
                and isinstance(content, dict)
                and content.get("ok") is False
            ):
                # MCP tools return structured envelopes. Claude records these
                # as a successful transport-level tool_result even when the
                # application operation failed, e.g. file_surface returning
                # {"ok": false, "error": ...}.
                failed_result = True
            if failed_result and block.get("tool_use_id"):
                failed.add(str(block["tool_use_id"]))
    return failed


def _extract_assistant_blocks(
    raw: object,
    workspace_root: Path | None = None,
) -> list[dict]:
    """Return ordered text/tool_use blocks for an assistant message.

    Items: {"kind": "text", "text": str},
           {"kind": "thinking", "text": str}, or
           {"kind": "tool_use", "name": str, "summary": str,
            "file_touch": {file_path, action} | None}.
    ``file_touch`` is populated when the tool mutates a file on disk so the
    PWA can render an inline file card on reload instead of the generic
    activity row. ``thinking`` mirrors the live stream's ThinkingEvent so
    reasoning is tagged as reasoning on reload instead of being dropped or
    (for providers that persist reasoning as a text block) promoted into the
    final answer bubble.
    """
    items: list[dict] = []
    if not isinstance(raw, dict):
        return items
    content_blocks = raw.get("content", "")
    if isinstance(content_blocks, str):
        if content_blocks.strip():
            items.append({"kind": "text", "text": content_blocks})
        return items
    if not isinstance(content_blocks, list):
        return items
    for block in content_blocks:
        if isinstance(block, str):
            if block.strip():
                items.append({"kind": "text", "text": block})
            continue
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text = block.get("text", "")
            if text.strip():
                items.append({"kind": "text", "text": text})
        elif btype in ("thinking", "redacted_thinking"):
            # Extended-thinking / reasoning blocks. Anthropic stores these as
            # {"type": "thinking", "thinking": "..."}; the redacted variant is
            # encrypted and carries no readable text (skip it). Surfacing them
            # as their own kind lets the history renderer tag them `_thinking`
            # — matching the live path — so reasoning stays collapsed in the
            # Activity trace instead of rendering as a normal answer bubble.
            thought = block.get("thinking") or block.get("text") or ""
            if isinstance(thought, str) and thought.strip():
                items.append({"kind": "thinking", "text": thought})
        elif btype == "tool_use":
            name = block.get("name", "")
            tinput = block.get("input") or {}
            if not isinstance(tinput, dict):
                tinput = {}
            summary = _summarize_tool_input(name, tinput)
            touches = normalize_file_touch_paths(
                extract_file_touches(name, tinput),
                workspace_root,
            )
            entry = {"kind": "tool_use", "name": name, "summary": summary}
            # Kept so the history builder can match this call against its
            # tool_result and drop the file card when the call failed.
            if block.get("id"):
                entry["id"] = str(block["id"])
            if touches:
                entry["file_touch"] = touches[0]
                if len(touches) > 1:
                    entry["file_touches"] = touches
            items.append(entry)
    return items


def _strip_legacy_context_prefix(content: str) -> str:
    lines = content.splitlines()
    idx = 0
    seen_context = False

    while idx < len(lines):
        line = lines[idx]
        if not line.strip():
            if seen_context:
                remainder = "\n".join(lines[idx + 1 :]).strip()
                return remainder or content
            idx += 1
            continue
        if line.startswith("[CONTEXT: ") or line.startswith("[Project context: ") or line.startswith('[Project: "') or line.startswith('[Chat: "'):
            seen_context = True
            idx += 1
            continue
        if line.startswith("[PWA interface: "):
            seen_context = True
            idx += 1
            while idx < len(lines):
                if lines[idx].endswith("space.]"):
                    idx += 1
                    break
                idx += 1
            continue
        break

    if seen_context:
        while idx < len(lines) and not lines[idx].strip():
            idx += 1
        remainder = "\n".join(lines[idx:]).strip()
        return remainder or content

    return content


def _strip_image_manifest(content: str) -> str:
    stripped = _IMAGE_MANIFEST_RE.sub("", content)
    return stripped if stripped else content


def _strip_injected_context(content: str) -> str:
    # A continuation / handover turn can stack two [CIAO_CONTEXT_BEGIN] blocks
    # (e.g. stable context + today). Strip them all, not just the first one.
    stripped = content
    while True:
        nxt = _CONTEXT_BLOCK_RE.sub("", stripped, count=1)
        if nxt == stripped:
            break
        stripped = nxt
    if stripped != content:
        return _strip_image_manifest(stripped).strip() or content
    legacy = _strip_legacy_context_prefix(content)
    legacy = _strip_image_manifest(legacy)
    return legacy.strip() or content


# The user-record skip rules (/model and /mode echoes, the interrupted-turn
# sentinel, the interrupt marker, the CLI envelope tags) and the
# task-notification grammar live in ciao/cli_envelopes.py. They are shared
# with ciao/subagent_tracking.py, whose turn counter must skip exactly the
# records this renderer hides or `turn_index` anchoring drifts. The assistant
# acknowledgement of a control slash command ("Set model to ..." / "Set mode
# to ...") is collapsed into a system bubble by _classify_control_ack below.
_is_control_slash_command = cli_envelopes.is_control_slash_command
_is_no_response_sentinel = cli_envelopes.is_no_response_sentinel
_is_interrupted_request_sentinel = cli_envelopes.is_interrupted_request_sentinel


def _classify_control_ack(text: str) -> str | None:
    """Return a user-facing label if `text` is an SDK control ack, else None."""
    t = text.strip()
    if t.startswith("Set model to "):
        return f"\U0001F504 {t}"  # 🔄
    if t.startswith("Set mode to "):
        return f"\U0001F504 {t}"
    return None


# The subagent's own final message often self-reports its sign-off ("Agent
# "X" completed", "...finished", "...done", ...) rather than a fixed CLI
# string, so the "already shaped, pass through as-is" check has to tolerate
# whatever terminal-status verb the model picked instead of matching only
# "completed" — otherwise it doubles up with the generic wrapper below (e.g.
# "Subagent completed: Agent "X" finished").
_AGENT_SELF_STATUS_RE = re.compile(
    r'^Agent "[^"]+" (?:completed|finished|done|succeeded|failed)\b', re.IGNORECASE
)

_is_cli_internal_envelope = cli_envelopes.is_cli_envelope


# Stands in for the injected subagent-synthesis nudge in the transcript. Same
# icon as the subagent-completion lines above so the pair reads as one story.
_SYNTHESIS_NUDGE_LABEL = "\U0001F916 Background agents finished — asked for a consolidated report"


def _summarize_task_notification(content: str) -> str | None:
    """Render a <task-notification> envelope as a one-line system bubble.

    Returns None if `content` carries no task-notification. The CLI emits this
    XML as a user-role message after a Task subagent finishes. We surface it
    as a system status bubble so the user retains visibility into subagent
    completions without seeing the raw envelope.

    Only the first notification is summarised when the CLI concatenates
    several into one record; the rest stay hidden with the envelope. Callers
    must have established that `content` is a CLI envelope — a notification
    quoted inside the human's own prose is their text, not a completion.
    """
    fields = cli_envelopes.task_notification_fields(content)
    if fields is None:
        return None
    status = fields.get("status", "completed")
    summary = fields.get("summary", "")
    first_line = summary.splitlines()[0].strip() if summary else ""
    icon = "\U0001F916"  # 🤖
    if _AGENT_SELF_STATUS_RE.match(first_line):
        # Already shaped like 'Agent "X" completed'; pass it through.
        return f"{icon} {first_line}"
    if first_line:
        # Trim aggressively so the bubble stays one line; full output lives in
        # the subagent transcript fetchable via /api/chats/{id}/subagents.
        snippet = first_line if len(first_line) <= 120 else first_line[:117] + "..."
        return f"{icon} Subagent {status}: {snippet}"
    return f"{icon} Subagent {status}"


def _render_subagent_messages(msgs: Iterable[object]) -> list[dict]:
    """Render SDK or JSONL message objects for the subagent transcript UI."""
    rendered: list[dict] = []
    # Materialised because the failed-tool pre-pass has to see the results,
    # which arrive after the calls they belong to.
    msgs = list(msgs)
    failed_tool_ids = _failed_tool_use_ids(msgs)
    for m in msgs:
        mtype = getattr(m, "type", None)
        message = getattr(m, "message", None)
        if isinstance(m, dict):
            mtype = m.get("type", mtype)
            message = m.get("message", message)
        if mtype == "assistant":
            blocks = _extract_assistant_blocks(message)
            blocks = [
                b for b in blocks
                if not (b["kind"] == "text" and _is_no_response_sentinel(b["text"]))
            ]
            if not blocks:
                continue
            pending_tools: list[str] = []

            def flush_tools() -> None:
                if pending_tools:
                    rendered.append({
                        "role": "system",
                        "content": "\n".join(pending_tools),
                        "tool_name": "_activity",
                    })
                    pending_tools.clear()

            for blk in blocks:
                if blk["kind"] == "tool_use":
                    name = blk["name"] or "tool"
                    summary = blk.get("summary") or ""
                    touches = blk.get("file_touches")
                    if not isinstance(touches, list) or not touches:
                        touch = blk.get("file_touch")
                        touches = [touch] if touch else []
                    if (
                        touches
                        and blk.get("id") in failed_tool_ids
                        and not _touches_survive_failure(name)
                    ):
                        # Denied or errored write: nothing reached disk, so
                        # render a plain activity row instead of a file card
                        # that implies the write happened.
                        touches = []
                    if touches:
                        flush_tools()
                        for touch in touches:
                            if not isinstance(touch, dict) or not touch.get("file_path"):
                                continue
                            rendered.append({
                                "role": "system",
                                "tool_name": "_filecard",
                                "content": touch["file_path"],
                                "file_path": touch["file_path"],
                                "action": touch.get("action") or "touched",
                                "tool": name,
                            })
                        continue
                    line = f"{_tool_icon(name)} {name}"
                    if summary:
                        line += f" {summary}"
                    pending_tools.append(line)
                elif blk["kind"] == "thinking":
                    # Subagent reasoning is not surfaced in the transcript
                    # panel (it was dropped before thinking blocks were
                    # extracted; skip to keep that behavior).
                    continue
                else:
                    flush_tools()
                    text = blk["text"].strip()
                    if text:
                        rendered.append({"role": "assistant", "content": text})
            flush_tools()
            continue

        content = _extract_text_content(message).strip()
        if not content:
            continue
        if _is_no_response_sentinel(content):
            continue
        rendered.append({"role": str(mtype or "system"), "content": content})
    return rendered


def _local_session_jsonl_paths(
    session_id: str, workspace_root: Path, *, agent_root: Path | None = None
) -> list[Path]:
    """Find local Claude Code JSONL files for ``session_id``."""
    try:
        from ciao.transcripts import (
            _claude_projects_dir,
            _global_session_matches,
        )
    except ImportError:
        return []
    paths: list[Path] = []
    root = agent_root if agent_root is not None else workspace_root
    preferred = _claude_projects_dir(root) / f"{session_id}.jsonl"
    if preferred.exists():
        paths.append(preferred)
    # When an agent root is supplied, the preferred path already scopes to
    # that root's own projects dir, so a session under another root stays
    # invisible (the re-rooting isolation). Without a root, keep the global
    # scan so callers that supply nothing behave exactly as today.
    if agent_root is not None:
        return paths
    # Sweep every cross-cwd match, not just the first: a session resumed or
    # copied under another cwd can have subagent progress records spread
    # across the files, and _local_subagent_transcripts reads them all. The
    # listing is cached (transcripts._global_session_matches) so this costs
    # one walk per TTL window, not one per poll — and a miss re-scans
    # (rate-limited) so a session created mid-window is not missed.
    try:
        for path in _global_session_matches(session_id):
            if path not in paths:
                paths.append(path)
    except OSError:
        pass
    return paths


def _jsonl_message_from_entry(entry: dict) -> dict | None:
    etype = entry.get("type")
    message = entry.get("message")
    if etype in {"assistant", "user"} and isinstance(message, dict):
        return {"type": etype, "message": message}
    if etype == "progress":
        nested = entry.get("data", {}).get("message")
        if isinstance(nested, dict):
            ntype = nested.get("type")
            nmessage = nested.get("message")
            if ntype in {"assistant", "user"} and isinstance(nmessage, dict):
                return {"type": ntype, "message": nmessage}
    return None


def _read_jsonl_messages(path: Path) -> list[dict]:
    messages: list[dict] = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                msg = _jsonl_message_from_entry(entry)
                if msg is not None:
                    messages.append(msg)
    except OSError:
        return []
    return messages


def _local_subagent_transcripts(
    session_id: str, workspace_root: Path, *, agent_root: Path | None = None
) -> list[dict]:
    """Fallback parser for nested subagent JSONL files and progress entries."""
    projects_root = Path.home() / ".claude" / "projects"
    grouped: dict[str, list[dict]] = {}

    try:
        # The projects dir holds one slug folder per cwd; a bare glob over
        # "*/<sid>/subagents/*.jsonl" descends every slug. Restrict to the
        # dirs the cached listing already knows, so a stale-slug pileup
        # cannot turn this fallback into a multi-second scandir storm.
        candidate_dirs = [
            entry
            for entry in projects_root.iterdir()
            if entry.is_dir() and (entry / session_id / "subagents").is_dir()
        ]
    except OSError:
        candidate_dirs = []
    nested_paths: list[Path] = []
    for entry in candidate_dirs:
        subagents_dir = entry / session_id / "subagents"
        try:
            nested_paths.extend(sorted(subagents_dir.glob("*.jsonl")))
        except OSError:
            continue
    for path in nested_paths:
        msgs = _read_jsonl_messages(path)
        if msgs:
            grouped.setdefault(path.stem, []).extend(msgs)

    for path in _local_session_jsonl_paths(session_id, workspace_root, agent_root=agent_root):
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(entry, dict) or entry.get("type") != "progress":
                        continue
                    msg = _jsonl_message_from_entry(entry)
                    if msg is None:
                        continue
                    data = entry.get("data", {})
                    agent_id = (
                        data.get("agent_id")
                        or data.get("subagent_id")
                        or data.get("task_id")
                        or data.get("parent_tool_use_id")
                        or "progress"
                    )
                    grouped.setdefault(str(agent_id), []).append(msg)
        except OSError:
            continue

    return [
        {"agent_id": agent_id, "messages": _render_subagent_messages(messages)}
        for agent_id, messages in sorted(grouped.items())
        if messages
    ]


def _overlay_assistant_timings(
    entries: list[dict], timings: dict
) -> None:
    """Attach sent_at + duration_ms to the LAST assistant text per turn.

    ``timings`` is ``ChatInfo.user_turn_timings`` keyed by turn_index (as str).
    Walks the chronological message list, tracks which turn each assistant
    text belongs to (the most recent user msg's turn_index), then overlays
    timings from the corresponding record. The user entries themselves get
    their own ``sent_at`` set inline at append time; this helper only handles
    the assistant side, where multiple text/tool blocks share a single turn.
    """
    if not timings:
        return
    current_turn: int | None = None
    last_assistant_idx_in_turn: dict[int, int] = {}
    for i, entry in enumerate(entries):
        role = entry.get("role")
        if role == "user":
            ti = entry.get("turn_index")
            current_turn = ti if isinstance(ti, int) else None
        elif role == "assistant" and current_turn is not None:
            last_assistant_idx_in_turn[current_turn] = i
    for turn, idx in last_assistant_idx_in_turn.items():
        rec = timings.get(str(turn)) or timings.get(turn)
        if not isinstance(rec, dict):
            continue
        completed = rec.get("completed_at")
        if completed:
            entries[idx]["sent_at"] = completed
        duration = rec.get("duration_ms")
        if isinstance(duration, (int, float)):
            entries[idx]["duration_ms"] = int(duration)


def _render_opencode_thread(
    thread: dict, chat, *, metadata: bool = True, start_user_idx: int = 0
) -> list[dict]:
    """Render opencode session messages into the provider-neutral PWA row shape.

    ``thread`` is :meth:`OpencodeProvider.read_thread`'s ``{"info", "messages"}``
    payload; each message is ``{"info": {role, ...}, "parts": [...]}``.

    ``metadata`` overlays ``chat``'s per-turn images/timings/unattended flags
    onto the rows. Pass ``False`` when ``thread`` is a *child* session: its
    turn numbering restarts at 0, so the parent chat's turn metadata does not
    apply to it.

    ``start_user_idx`` offsets per-session user numbering when stitching
    ``previous_session_ids``.
    """
    messages = thread.get("messages")
    if not isinstance(messages, list):
        messages = []
    result: list[dict] = []
    user_idx = int(start_user_idx) if isinstance(start_user_idx, int) else 0
    pending_tools: list[str] = []

    def flush_tools() -> None:
        if pending_tools:
            result.append({
                "role": "system",
                "content": "\n".join(pending_tools),
                "tool_name": "_activity",
            })
            pending_tools.clear()

    for message in messages:
        if not isinstance(message, dict):
            continue
        info = message.get("info")
        info = info if isinstance(info, dict) else {}
        role = str(info.get("role") or "")
        parts = message.get("parts")
        parts = [part for part in parts if isinstance(part, dict)] if isinstance(parts, list) else []
        if role == "user":
            flush_tools()
            # A prompt can span several text parts; synthetic ones are
            # opencode's own injections (compaction summaries), not something
            # the user typed.
            texts = [
                str(part.get("text") or "")
                for part in parts
                if part.get("type") == "text" and not part.get("synthetic")
            ]
            content = _strip_injected_context("\n".join(texts)).strip()
            if not content:
                continue
            entry: dict = {
                "role": "user",
                "content": content,
                "turn_index": user_idx,
            }
            if metadata:
                refs = chat.user_turn_images.get(str(user_idx))
                if refs:
                    entry["images"] = list(refs)
                timing = chat.user_turn_timings.get(str(user_idx)) or {}
                if timing.get("sent_at"):
                    entry["sent_at"] = timing["sent_at"]
                if chat.user_turn_unattended.get(str(user_idx)):
                    entry["unattended"] = True
            result.append(entry)
            user_idx += 1
            continue
        if role != "assistant":
            continue
        for part in parts:
            kind = str(part.get("type") or "")
            if kind == "text":
                flush_tools()
                text = str(part.get("text") or "").strip()
                if text:
                    result.append({"role": "assistant", "content": text})
                continue
            if kind == "reasoning":
                # Same `_thinking` tag as the Claude replay path: the PWA
                # folds it into the collapsed Activity trace.
                flush_tools()
                text = str(part.get("text") or "").strip()
                if text:
                    result.append({
                        "role": "system",
                        "content": text,
                        "tool_name": "_thinking",
                    })
                continue
            if kind == "tool":
                tool = str(part.get("tool") or "tool")
                state = part.get("state")
                state = state if isinstance(state, dict) else {}
                raw_input = state.get("input")
                touches = _opencode_file_touches(tool, raw_input)
                if str(state.get("status") or "") == "error":
                    # A failed/denied write or edit reached nothing on disk:
                    # match the live path (and the Claude replay path) by
                    # rendering a plain activity row instead of a file card.
                    touches = []
                if touches:
                    flush_tools()
                    for touch in touches:
                        result.append({
                            "role": "system",
                            "tool_name": "_filecard",
                            "content": touch["file_path"],
                            "file_path": touch["file_path"],
                            "action": touch.get("action") or "touched",
                            "tool": tool,
                        })
                    continue
                summary = _summarize_opencode_tool_input(tool, raw_input)
                line = f"{_tool_icon(tool)} {tool}"
                if summary:
                    line += f" {summary}"
                pending_tools.append(line)
        flush_tools()
    if metadata:
        _overlay_assistant_timings(result, chat.user_turn_timings)
    return result


def _opencode_child_status(messages: list) -> str:
    """A child session's lifecycle state, read from its own messages.

    opencode's session objects carry no status field, but the last assistant
    message does: an ``error`` payload marks a failure, and a ``time`` record
    without ``completed`` marks a turn still in flight.
    """
    last: dict | None = None
    for message in messages:
        if not isinstance(message, dict):
            continue
        info = message.get("info")
        if isinstance(info, dict) and info.get("role") == "assistant":
            last = info
    if last is None:
        return "completed"
    if last.get("error"):
        return "failed"
    time_info = last.get("time")
    if (
        isinstance(time_info, dict)
        and time_info.get("created")
        and not time_info.get("completed")
    ):
        return "running"
    return "completed"


def _opencode_child_turn_index(info: dict, chat) -> int:
    """The parent turn a child belongs to: the last one sent before it began."""
    time_info = info.get("time")
    created_ms = time_info.get("created") if isinstance(time_info, dict) else None
    if not isinstance(created_ms, (int, float)) or isinstance(created_ms, bool):
        return 0
    best = 0
    for key, timing in (chat.user_turn_timings or {}).items():
        sent_at = (timing or {}).get("sent_at")
        if not sent_at:
            continue
        try:
            idx = int(key)
            sent_ms = datetime.fromisoformat(
                str(sent_at).replace("Z", "+00:00")
            ).timestamp() * 1000
        except (TypeError, ValueError):
            continue
        if sent_ms <= created_ms and idx > best:
            best = idx
    return best


def _overlay_transcript_metadata(
    entries: list[dict], transcript_rows: list[dict]
) -> None:
    metadata = [
        row for row in transcript_rows
        if row.get("role") == "assistant"
    ]
    targets: list[int] = []
    last: int | None = None
    for index, row in enumerate(entries):
        if row.get("role") == "user":
            if last is not None:
                targets.append(last)
            last = None
        elif row.get("role") == "assistant":
            last = index
    if last is not None:
        targets.append(last)
    # The two lists cover different spans, and which end they disagree at
    # decides how to pair them:
    #
    #   fewer session rows than transcript turns — a resumed or handed-over
    #     chat, whose JSONL starts at the resume point while the transcript
    #     store holds every turn. The rows they share are the NEWEST ones, so
    #     match the tails; the unmatched head gets no metadata.
    #
    #   more session rows than transcript turns — a turn is in flight. The
    #     session already carries the live assistant text while the transcript
    #     still ends at the last completed turn, because `record_turn` only
    #     runs once a turn finishes. The surplus is at the NEWEST end, so match
    #     the heads and leave the live reply without metadata. Matching tails
    #     here would shift every usage record forward by one and hang the
    #     previous turn's token count on the reply still being written.
    #
    # Zipping from the front unconditionally (the original) got the first case
    # wrong from its very first row; right-aligning unconditionally gets the
    # second wrong the same way.
    pairs = min(len(targets), len(metadata))
    if not pairs:
        return
    if len(targets) <= len(metadata):
        targets, metadata = targets[-pairs:], metadata[-pairs:]
    else:
        targets, metadata = targets[:pairs], metadata[:pairs]
    for index, source in zip(targets, metadata):
        for key in ("usage", "quota", "effective_model"):
            if source.get(key):
                entries[index][key] = source[key]


def _messages_from_archived_transcript(
    pcm,
    config,
    chat,
) -> list[dict] | None:
    """Parse vault markdown for an archived chat, or None when unavailable."""
    if not getattr(chat, "archived", False) or not getattr(chat, "archive_path", ""):
        return None
    archive_path = Path(chat.archive_path)
    if not archive_path.is_absolute():
        archive_path = config.workspace_root / archive_path
    try:
        text = archive_path.read_text(encoding="utf-8")
    except OSError:
        logger.warning(
            "Failed to read archived transcript for %s at %s",
            getattr(chat, "chat_id", ""),
            archive_path,
        )
        return None
    parsed = pcm._parse_transcript_messages(text)
    parsed = chat_service._normalize_handover_messages(parsed)
    # Map transcript timestamp field to the frontend's sent_at key.
    for parsed_entry in parsed:
        if "timestamp" in parsed_entry and "sent_at" not in parsed_entry:
            parsed_entry["sent_at"] = parsed_entry["timestamp"]
    _overlay_assistant_timings(parsed, chat.user_turn_timings)
    return parsed


def _read_session_segment(session_id: str, directories: list[str]) -> list:
    """One session's messages, from whichever root recorded it.

    The projects directory is slugged from the cwd the session ran in, so a chat's
    own agent root is where to look first and the install root second — the latter
    holds every session from before the re-rooting.

    An EMPTY result counts as "not in this root", not as success. That is not a
    detail: asked for a session it does not have, `get_session_messages_full`
    returns `[]` rather than raising — which is exactly how the original bug hid.
    Stopping at the first empty answer would have fixed today's chats by blanking
    every chat from before the migration instead.

    Raises when no root has it, so the caller's "skip this segment" path still
    works and the archived-transcript fallback still gets its turn.
    """
    from ciao.transcripts import get_session_messages_full

    for directory in directories:
        try:
            segment = get_session_messages_full(session_id, directory=directory)
        except (FileNotFoundError, ValueError):
            continue
        if segment:
            return segment
    raise FileNotFoundError(
        f"no session {session_id!r} under any of: {', '.join(directories)}"
    )


_THINKING_KEEP_CHARS = 512


def _prune_rows_for_wire(rows: list[dict]) -> list[dict]:
    """Annotate every row with its absolute index and prune oversized rows.

    Only ``_thinking`` rows are truncated today (head+tail with a lazy marker);
    the collapsed ``_activity`` / ``_filecard`` summaries are already small.
    The unpruned row stays fetchable via the part endpoint using index ``i``.
    """
    out: list[dict] = []
    for idx, row in enumerate(rows):
        item = dict(row)
        item["i"] = idx
        if item.get("tool_name") == "_thinking":
            text = item.get("content") or ""
            gap = len(text) - 2 * _THINKING_KEEP_CHARS
            if gap > 64:
                item["content"] = (
                    text[:_THINKING_KEEP_CHARS]
                    + f"\n… ({gap} chars hidden, expand to load)\n"
                    + text[-_THINKING_KEEP_CHARS:]
                )
                item["lazy"] = True
                item["full_length"] = len(text)
        out.append(item)
    return out


async def _assemble_chat_messages(
    pcm: Any, config: Any, chat: Any
) -> list[dict]:
    """Build the full chronological history row list for one chat.

    This is the expensive part of ``GET /api/chats/{id}/messages`` — provider
    session reads plus rendering — split out so the pagination envelope and
    the per-part endpoint can share one assembly path.
    """
    chat_id = chat.chat_id
    handover_messages = list(getattr(chat, "handover_messages", []) or [])
    if not chat.session_id:
        # A provider may fail before creating its session (for example while
        # opencode is starting). The durable transcript still contains the
        # user turn and the persisted error, so do not hide it behind the
        # session-less handover fast path.
        current = pcm._transcripts.current_messages(
            ChatContext.for_web(chat_id), getattr(chat, "provider", "claude")
        )
        if current:
            return [*handover_messages, *current]
        archived = _messages_from_archived_transcript(pcm, config, chat)
        if archived is not None:
            return [*handover_messages, *archived]
        return [*handover_messages, *current]

    provider = getattr(chat, "provider", "claude")
    # Every provider stores its sessions per-cwd, and a chat's cwd is its agent
    # root. Reading with the install root found nothing for any chat created since
    # the re-rooting — for Claude and opencode alike.
    _resolver = getattr(pcm, "_agent_root_for_chat", None)
    session_root = Path(
        _resolver(chat_id) if _resolver is not None else config.workspace_root
    )
    if provider not in {"claude", "opencode"}:
        current = pcm._transcripts.current_messages(
            ChatContext.for_web(chat_id), provider
        )
        if current:
            _overlay_assistant_timings(current, chat.user_turn_timings)
            return [*handover_messages, *current]
        archived = _messages_from_archived_transcript(pcm, config, chat)
        if archived is not None:
            return [*handover_messages, *archived]
        return list(handover_messages)
    if provider == "opencode":
        if getattr(chat, "archived", False):
            # An archived chat is read-only and its provider-side session may
            # be gone; serve the vault markdown without paying a provider
            # session read (for opencode, a throwaway server spawn) first.
            archived = _messages_from_archived_transcript(pcm, config, chat)
            if archived is not None:
                return [*handover_messages, *archived]
        # Stitch the same lineage Claude uses: each provider keeps its turns
        # only in the session that wrote them, and ciaobot rotates via
        # ``_rotate_session_id`` (autocompact / resume-fallback / continuation).
        # Reading only ``chat.session_id`` blanked every chat that had rotated
        # — exactly the bug that hid the first turn of chat-a495fc8f.
        session_ids: list[str] = []
        seen: set[str] = set()
        for sid in (*getattr(chat, "previous_session_ids", []), chat.session_id):
            sid_str = str(sid or "").strip()
            if not sid_str or sid_str in seen:
                continue
            seen.add(sid_str)
            session_ids.append(sid_str)
        rendered: list[dict] = []
        start_user_idx = 0
        for sid in session_ids:
            try:
                opencode_thread = await OpencodeProvider.read_thread(
                    session_root, sid
                )
                if not opencode_thread:
                    continue
                segment = _render_opencode_thread(
                    opencode_thread, chat, start_user_idx=start_user_idx
                )
            except Exception:  # noqa: BLE001 — one missing segment must not blank siblings
                continue
            if segment:
                # Advance the global turn offset by the user-bubble count in
                # this segment so the next segment's ``turn_index`` + timings
                # stay aligned with ``chat.user_turn_timings``.
                user_count = sum(1 for row in segment if row.get("role") == "user")
                rendered.extend(segment)
                start_user_idx += user_count
        current = pcm._transcripts.current_messages(
            ChatContext.for_web(chat_id), provider
        )
        if rendered:
            if current and current[-1].get("role") == "assistant" and current[-1].get("is_error"):
                has_error_in_rendered = False
                for row in reversed(rendered):
                    if row.get("role") == "assistant":
                        if row.get("is_error") or row.get("content") == current[-1].get("content"):
                            has_error_in_rendered = True
                        break
                if not has_error_in_rendered:
                    err_msg = dict(current[-1])
                    rendered.append(err_msg)
            _overlay_transcript_metadata(
                rendered,
                current,
            )
            return [*handover_messages, *rendered]
        if current:
            _overlay_assistant_timings(current, chat.user_turn_timings)
            return [*handover_messages, *current]
        archived = _messages_from_archived_transcript(pcm, config, chat)
        if archived is not None:
            return [*handover_messages, *archived]
        return list(handover_messages)


    result: list[dict] = []
    # A chat can rotate through more than one SDK session file within the
    # same conversation (autocompact, or a resume-failure fallback) — each
    # file only holds the turns written after it started. Walk the full
    # lineage (oldest first) so history renders continuously across the
    # rotation instead of only showing the newest segment.
    session_ids = [*chat.previous_session_ids, chat.session_id]
    # A session's JSONL lives in a directory slugged from the CWD it was started
    # in, and that is the chat's AGENT ROOT — `~/repos/ciao/work`, not the install
    # root. Passing the install root looked up
    # `~/.claude/projects/-Users-me-repos-ciao/<session>.jsonl`, which does not
    # exist for any chat created since the re-rooting; the FileNotFoundError was
    # swallowed as "this segment is missing" and every such chat rendered EMPTY.
    #
    # The install root is still tried, second: chats from before the migration
    # have their transcripts under exactly that slug, and they must keep
    # rendering.
    session_dirs: list[str] = []
    for candidate in (session_root, Path(config.workspace_root)):
        text = str(candidate)
        if text not in session_dirs:
            session_dirs.append(text)
    msgs: list | None = None
    for sid in session_ids:
        if not sid:
            continue
        try:
            # Reading and stitching a session's JSONL is unbounded synchronous
            # work (it grows with the conversation), and this route is re-hit
            # by every client's 15s poll. Left on the event loop it stalled
            # every other request on the node, including the 5s chat-socket
            # keepalives whose absence trips the PWA's half-open watchdog.
            segment = await asyncio.to_thread(
                _read_session_segment, sid, session_dirs
            )
        except (FileNotFoundError, ValueError):
            # This segment's file doesn't exist on this machine (remote chat,
            # or pruned after rotating away). Skip it rather than blanking
            # the whole history — the other segments may still be intact.
            continue
        if msgs is None:
            msgs = []
        msgs.extend(segment)

    # An SDK session can still exist while containing no renderable messages
    # (for example after an archived session was compacted or partially
    # cleaned up). Archived chats have a durable Markdown copy; use it in that
    # case as well as when the provider-side session is missing entirely.
    if msgs is None or (not msgs and chat.archived):
        archived = _messages_from_archived_transcript(pcm, config, chat)
        if archived is not None:
            return [*handover_messages, *archived]
        return list(handover_messages)

    user_idx = 0
    failed_tool_ids = _failed_tool_use_ids(msgs)
    for m in msgs:
        if m.type == "assistant":
            blocks = _extract_assistant_blocks(
                m.message,
                workspace_root=config.workspace_root,
            )
            # Drop the CLI's "No response requested." sentinel that marks
            # interrupted turns. If the message contained ONLY that sentinel
            # (no tools, no other text), skip the whole entry.
            blocks = [
                b for b in blocks
                if not (b["kind"] == "text" and _is_no_response_sentinel(b["text"]))
            ]
            if not blocks:
                continue
            # Collapse a pure control ack ("Set model to ..." / "Set mode to
            # ...") into a single system bubble. These follow the SDK-injected
            # /model or /mode user turn that we skip below.
            text_blocks = [b for b in blocks if b["kind"] == "text"]
            tool_blocks = [b for b in blocks if b["kind"] == "tool_use"]
            thinking_blocks = [b for b in blocks if b["kind"] == "thinking"]
            if not tool_blocks and not thinking_blocks and len(text_blocks) == 1:
                label = _classify_control_ack(text_blocks[0]["text"])
                if label:
                    result.append({"role": "system", "content": label})
                    continue
            # Merge contiguous non-file tool_use blocks into a single _activity
            # entry so the frontend renders one collapsible group per cluster.
            # File-mutating tool calls (Write/Edit/MultiEdit/NotebookEdit) break
            # that group and emit a standalone _filecard so the PWA can render
            # a clickable preview card inline with the message.
            pending_tools: list[str] = []

            def flush_tools():
                if pending_tools:
                    result.append({
                        "role": "system",
                        "content": "\n".join(pending_tools),
                        "tool_name": "_activity",
                    })
                    pending_tools.clear()

            for blk in blocks:
                if blk["kind"] == "tool_use":
                    name = blk["name"] or "tool"
                    summary = blk.get("summary") or ""
                    touches = blk.get("file_touches")
                    if not isinstance(touches, list) or not touches:
                        touch = blk.get("file_touch")
                        touches = [touch] if touch else []
                    if (
                        touches
                        and blk.get("id") in failed_tool_ids
                        and not _touches_survive_failure(name)
                    ):
                        # Denied or errored write: nothing reached disk, so
                        # render a plain activity row instead of a file card
                        # that implies the write happened.
                        touches = []
                    if touches:
                        flush_tools()
                        for touch in touches:
                            if not isinstance(touch, dict) or not touch.get("file_path"):
                                continue
                            result.append({
                                "role": "system",
                                "tool_name": "_filecard",
                                "content": touch["file_path"],
                                "file_path": touch["file_path"],
                                "action": touch.get("action") or "touched",
                                "tool": name,
                            })
                        continue
                    line = f"{_tool_icon(name)} {name}"
                    if summary:
                        line += f" {summary}"
                    pending_tools.append(line)
                elif blk["kind"] == "thinking":
                    # Reasoning: tag as `_thinking` so the PWA folds it into the
                    # collapsed Activity trace (never the final answer bubble),
                    # matching the live stream. Emit in order relative to tools
                    # and text by flushing any pending tool group first.
                    flush_tools()
                    text = blk["text"].strip()
                    if text:
                        result.append({
                            "role": "system",
                            "content": text,
                            "tool_name": "_thinking",
                        })
                else:
                    flush_tools()
                    text = blk["text"].strip()
                    if text:
                        result.append({"role": "assistant", "content": text})
            flush_tools()
            continue

        content = _extract_text_content(m.message)
        if m.type == "user":
            content = _strip_injected_context(content)
        content = content.strip()
        if not content:
            continue
        # Drop rate limit telemetry status events (allowed, rejected, warnings)
        # so transient usage telemetry does not pollute the chat history. A hard
        # "Rate limit exceeded" carries no "Rate limit:" prefix and still surfaces.
        if m.type == "system" and is_rate_limit_telemetry(content):
            continue
        # Drop SDK-injected control slash commands (/model, /mode). Skipping
        # without incrementing user_idx keeps chat.user_turn_images aligned
        # with real user sends, which would otherwise shift by one per model
        # change.
        if m.type == "user" and _is_control_slash_command(content):
            continue
        # Claude Code writes interrupt markers as synthetic user turns. Hide
        # them and, critically, do not increment user_idx: the next real queued
        # user turn owns the next image bucket.
        if m.type == "user" and _is_interrupted_request_sentinel(content):
            continue
        # Drop the CLI's interrupted-turn sentinel on the user side too: when
        # a turn is steered, the CLI splices a synthetic user message with
        # this exact content to keep the parent-uuid chain valid.
        if m.type == "user" and _is_no_response_sentinel(content):
            continue
        # CLI-synthesized user envelopes (subagent completions, bash output,
        # slash-command echoes). Promote <task-notification> to a clean system
        # bubble so subagent completions stay visible; hide the rest. Skip
        # without incrementing user_idx — these aren't real user turns and the
        # image-ref index must only advance on human sends.
        if m.type == "user":
            if _is_cli_internal_envelope(content):
                # A task-notification envelope earns a status line; every
                # other envelope is hidden outright. Asking the summariser
                # first would have let a record that merely *opens* with a
                # notification but carries anything after the closing tag
                # fall through to the blanket hide.
                task_summary = _summarize_task_notification(content)
                if task_summary is not None:
                    result.append({"role": "system", "content": task_summary})
                continue
            # Our own subagent-synthesis nudge (ciao/subagent_tracking.py).
            # It's a server-injected prompt, not something the user typed, so
            # showing the paragraph verbatim reads as words they never wrote.
            # Collapse it to a status line, and skip without incrementing
            # user_idx — subagent_tracking._is_countable_user_turn applies the
            # same rule, so the two turn counters stay aligned.
            if subagent_tracking.is_synthesis_nudge(content):
                result.append({"role": "system", "content": _SYNTHESIS_NUDGE_LABEL})
                continue
            is_compact = (
                isinstance(m.message, dict) and bool(m.message.get("isCompactSummary"))
            ) or content.startswith("This session is being continued from a previous conversation")
            if is_compact:
                result.append({"role": "system", "content": content})
                continue
        entry: dict = {
            "role": m.type,
            "content": content,
        }
        if m.type == "user":
            # Image refs are recorded per user-turn index at send time. JSON
            # keys are strings, but tolerate int lookups too in case the map
            # has been mutated in-memory since the last save.
            refs = chat.user_turn_images.get(str(user_idx))
            if refs is None:
                refs = chat.user_turn_images.get(user_idx)
            if refs:
                entry["images"] = list(refs)
            else:
                # Fall back to inline base64 images from the SDK session.
                # This handles sessions that were context-compacted: the
                # user_turn_images index map becomes stale after compaction
                # shifts the turn numbering, but inline images survive.
                inline = _extract_inline_images(m.message)
                if inline:
                    entry["images"] = inline
            # Surface the user-turn index so the client can dedup replayed
            # user_echo events against history it already loaded.
            entry["turn_index"] = user_idx
            # Attach the persisted send time so the UI footer can show it on
            # reload. Missing for pre-feature chats: the frontend treats an
            # empty string as "no timestamp".
            timing = chat.user_turn_timings.get(str(user_idx)) or chat.user_turn_timings.get(user_idx)
            if timing and timing.get("sent_at"):
                entry["sent_at"] = timing["sent_at"]
            # Loop/schedule ticks are user turns in the session file too, so the
            # flag has to come from our own per-turn record.
            if chat.user_turn_unattended.get(str(user_idx)) or chat.user_turn_unattended.get(user_idx):
                entry["unattended"] = True
            user_idx += 1
        result.append(entry)
    # Stitch the durable transcript's per-turn metadata (token usage with the
    # context %, quota, effective model) onto the rendered rows — the session
    # JSONL carries none of it, so without this the turn footer showed only
    # the completion time and duration. The opencode branch has made the same
    # overlay since the transcript store gained these fields.
    current = pcm._transcripts.current_messages(
        ChatContext.for_web(chat_id), provider
    )
    if current and result:
        _overlay_transcript_metadata(result, current)
    _overlay_assistant_timings(result, chat.user_turn_timings)
    return [*handover_messages, *result]
