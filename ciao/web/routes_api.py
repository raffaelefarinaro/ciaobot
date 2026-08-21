"""REST API routes for the PWA."""

from __future__ import annotations

import asyncio
import errno
import functools
import hashlib
import json
import logging
import mimetypes
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import asdict, replace
from datetime import datetime, timedelta, UTC
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence, cast
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from ciao import proposal_kinds
from ciao import subagent_tracking
from ciao import desktop_build
from ciao import provider_registry
from ciao import vault_rehome
from ciao.memory_tool import resolve_region
from ciao.config import WorkspaceConfig
from ciao.loops import publish_loops_changed
from ciao.models import THINKING_LEVELS, ChatContext
from ciao.workspaces import (
    WORKSPACE_NAME_RE,
    parse_disallowed_tools_value,
    persist_workspaces,
    workspace_from_request,
    workspace_provider_options,
    workspace_provider_values,
    workspace_to_dict,
)
# Kept as an alias: several call sites predate the shared module.
_WORKSPACE_NAME_RE = WORKSPACE_NAME_RE
from ciao.tool_path import login_shell_path, resolve_tool
from ciao.providers.claude import _summarize_tool_input
from ciao.providers.codex import CodexProvider, codex_login_status
from ciao.providers.opencode import (
    OpencodeProvider,
    _file_touches as _opencode_file_touches,
    _summarize_tool_input as _summarize_opencode_tool_input,
)
from ciao.provider_service import capabilities_for, supported_providers
from ciao.schedules import (
    ScheduleEntry,
    compute_last_expected_run,
    compute_next_run,
    normalize_archive_policy,
    was_dispatched_since,
)
from ciao.setup_status import setup_status
from ciao.cli import _auth_command_for_provider
from ciao.rate_limits import is_rate_limit_telemetry
from ciao.skills_inventory import build_skill_inventory
from ciao.vault_index import (
    _build_graph,
    filter_entries,
    scan_targets,
    strip_references,
)
from ciao.vault_lint import EXCLUDE_DIRS, _links_in
from ciao.web.chat_broker import extract_file_touches, normalize_file_touch_paths
from ciao.web.project_chats import (
    RestartDrainingError,
    _ALLOWED_IMAGE_EXTENSIONS,
    _PROJECT_UPLOAD_MAX_BYTES,
    _normalize_handover_messages,
)
from ciao.web.routes_helpers import (
    _allowed_roots,
    _commit_and_push,
    _git_pull_with_retry,
    _resolve_workspace_path,
)

logger = logging.getLogger(__name__)

_UPLOAD_READ_CHUNK_BYTES = 1024 * 1024


async def _read_upload_limited(upload, max_bytes: int) -> bytes:
    """Read an UploadFile while buffering at most its size cap plus one byte.

    Starlette spools multipart files, but ``UploadFile.read()`` without a size
    copies the complete file into memory. Read at most one byte beyond the cap
    so oversized uploads are rejected before that unbounded allocation.
    """
    if max_bytes < 0:
        raise ValueError("invalid upload size limit")
    data = bytearray()
    while True:
        read_size = min(_UPLOAD_READ_CHUNK_BYTES, max_bytes + 1 - len(data))
        chunk = await upload.read(read_size)
        if not chunk:
            return bytes(data)
        data.extend(chunk)
        if len(data) > max_bytes:
            raise ValueError("file too large")


_STATS_CACHE_PATH = Path.home() / ".claude" / "stats-cache.json"

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

# Provider API keys editable from Settings. Empty: every provider authenticates
# through its own CLI (`ciao auth <provider>`), so there is no key to type here.
_PROVIDER_KEY_META: dict[str, dict[str, str]] = {}
# Keys Ciaobot itself consumes, as opposed to provider logins. Empty since
# voice moved on-device: OPENAI_API_KEY lived here for cloud transcription and
# speech, and nothing else in the app ever read it.
_SERVICE_KEY_META: dict[str, dict[str, str]] = {}
# Labels and example chips for the two account names that predate the account
# registry. Nothing creates them any more — a fresh install starts with no
# Google account — but an install that already has one keeps its wording.
# Annotated because the values are heterogeneous (str labels alongside a
# list[str] of examples): without it mypy widens every lookup to Sequence[str],
# and `meta["purpose"]` stops being usable where a str is expected.
_GWS_PROFILE_META: dict[str, dict[str, Any]] = {
    "personal": {
        "label": "Personal Google account",
        "purpose": "Private Google account. Keep this separate from company systems.",
        # Shown for accounts connected before scopes were recorded. Their
        # credentials.json has no `scopes` key and re-consent is the only way
        # to get one, so without this an upgrading user's connected account
        # silently loses every chip it used to show.
        "examples": ["Gmail", "Calendar", "Tasks"],
    },
    "work": {
        "label": "Work Google account",
        "purpose": "Company Google account used for work Drive, Docs, Sheets, and Slides.",
        "examples": ["Drive", "Docs", "Sheets", "Slides", "Gmail", "Calendar"],
    },
}
_GWS_AUTH_FILES = ("credentials.json", "credentials.enc")


def _gws_purpose_with_chips(purpose: str, chips: list[str]) -> str:
    """Append the granted services to the profile's standing description.

    The description is not replaced: for the personal profile it carries the
    "keep this separate from company systems" guidance, which matters most
    once an account is actually connected.
    """
    if len(chips) == 1:
        joined = chips[0]
    elif len(chips) == 2:
        joined = f"{chips[0]} and {chips[1]}"
    else:
        joined = f"{', '.join(chips[:-1])}, and {chips[-1]}"
    return f"{purpose} Connected to {joined}."


def _known_workspace_names(pcm: object) -> set[str]:
    config = getattr(pcm, "_config", None)
    workspace_names = getattr(config, "workspace_names", None)
    if callable(workspace_names):
        names = {str(name) for name in workspace_names() if str(name)}
        if names:
            return names
    return {"personal", "work"}


def _workspace_provider_options(config) -> list[dict[str, str]]:
    return workspace_provider_options(config)


def _workspace_provider_values(config) -> set[str]:
    return workspace_provider_values(config)


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
    stripped = _CONTEXT_BLOCK_RE.sub("", content, count=1)
    if stripped != content:
        return _strip_image_manifest(stripped).strip() or content
    legacy = _strip_legacy_context_prefix(content)
    legacy = _strip_image_manifest(legacy)
    return legacy.strip() or content


# Slash commands the Claude Agent SDK injects as user turns when the PWA
# changes model or mode mid-session (via ClaudeSDKClient.set_model /
# set_permission_mode). They end up in the session JSONL and would otherwise
# render as user bubbles the user didn't type. The assistant acknowledgement
# ("Set model to ..." / "Set mode to ...") gets collapsed into a single
# system bubble in _classify_control_ack below.
_CONTROL_SLASH_PREFIXES = ("/model", "/mode")


def _is_control_slash_command(content: str) -> bool:
    head = content.strip().split(None, 1)[0] if content.strip() else ""
    return head in _CONTROL_SLASH_PREFIXES


# Sentinel that the Claude Code CLI writes into the session JSONL when a turn
# is interrupted (steer/queue mid-stream) or hits an empty rate-limit error.
# It's the `UXH` constant in claude_agent_sdk/_bundled/claude. Claude Code's
# own UI hides these (`case UXH: return null`); we mirror that here so reloads
# don't render a literal "No response requested." bubble after every interrupt.
_NO_RESPONSE_SENTINEL = "No response requested."

# Matches the Claude Agent SDK's own _SKIP_FIRST_PROMPT_PATTERN
# ([Request interrupted by user[^\]]*]) so we cover every CLI variant, not
# just the bare form. Steer/queue interrupts an in-flight tool call produce
# "[Request interrupted by user for tool use]" — without this wildcard that
# variant survives as a synthetic user record and renders as a quoted bubble
# that looks like an error reply to a question.
_INTERRUPTED_REQUEST_RE = re.compile(
    r"\[Request interrupted by user[^\]]*\]"
)


def _is_no_response_sentinel(text: str) -> bool:
    return text.strip() == _NO_RESPONSE_SENTINEL


def _is_interrupted_request_sentinel(text: str) -> bool:
    return bool(_INTERRUPTED_REQUEST_RE.fullmatch(text.strip()))


def _classify_control_ack(text: str) -> str | None:
    """Return a user-facing label if `text` is an SDK control ack, else None."""
    t = text.strip()
    if t.startswith("Set model to "):
        return f"\U0001F504 {t}"  # 🔄
    if t.startswith("Set mode to "):
        return f"\U0001F504 {t}"
    return None


# CLI-internal user-message envelopes. The Claude Code CLI synthesizes
# user-role messages wrapped in these XML tags to feed the parent agent
# subagent completion, bash output, slash-command invocations, etc. They
# are NOT from the human; they're the CLI talking to its own model. The
# tag names come from the constant table in
# claude_agent_sdk/_bundled/claude (IO="task-notification",
# EtH="bash-input", WV="command-name", and so on).
#
# Without this filter the envelopes leak into chat history as user bubbles:
# the browser strips the unknown tags and lays out only the inner text,
# producing the "task_id  toolu_id  /tmp/.../output completed\nAgent ..."
# blocks visible in chats with parallel subagents.
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

_TASK_NOTIFICATION_RE = re.compile(
    r"^\s*<task-notification>(.*)</task-notification>\s*$",
    re.DOTALL,
)

# Pulls <tag>content</tag> pairs out of a task-notification body. Names match
# the schema fields the CLI emits (task-id, tool-use-id, output-file, status,
# summary, plus an optional task-type).
_INNER_TAG_RE = re.compile(r"<([a-z-]+)>(.*?)</\1>", re.DOTALL)

# The subagent's own final message often self-reports its sign-off ("Agent
# "X" completed", "...finished", "...done", ...) rather than a fixed CLI
# string, so the "already shaped, pass through as-is" check has to tolerate
# whatever terminal-status verb the model picked instead of matching only
# "completed" — otherwise it doubles up with the generic wrapper below (e.g.
# "Subagent completed: Agent "X" finished").
_AGENT_SELF_STATUS_RE = re.compile(
    r'^Agent "[^"]+" (?:completed|finished|done|succeeded|failed)\b', re.IGNORECASE
)


def _is_cli_internal_envelope(content: str) -> bool:
    """True if `content` starts with a CLI-synthesized user-message wrapper."""
    return bool(_CLI_ENVELOPE_RE.match(content))


# Stands in for the injected subagent-synthesis nudge in the transcript. Same
# icon as the subagent-completion lines above so the pair reads as one story.
_SYNTHESIS_NUDGE_LABEL = "\U0001F916 Background agents finished — asked for a consolidated report"


def _summarize_task_notification(content: str) -> str | None:
    """Render a <task-notification> envelope as a one-line system bubble.

    Returns None if `content` isn't a task-notification. The CLI emits this
    XML as a user-role message after a Task subagent finishes. We surface it
    as a system status bubble so the user retains visibility into subagent
    completions without seeing the raw envelope.
    """
    m = _TASK_NOTIFICATION_RE.match(content)
    if not m:
        return None
    fields = {tag: text.strip() for tag, text in _INNER_TAG_RE.findall(m.group(1))}
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
        from ciao.transcripts import _claude_projects_dir
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
    projects_root = Path.home() / ".claude" / "projects"
    try:
        for path in projects_root.glob(f"*/{session_id}.jsonl"):
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
        nested_paths = sorted(projects_root.glob(f"*/{session_id}/subagents/*.jsonl"))
    except OSError:
        nested_paths = []
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


# ── Auth ────────────────────────────────────────────────────────────────

from ciao.web.routes_auth import (
    auth_check,
    auth_login,
    auth_logout,
    auth_settings_get,
    auth_settings_update,
)


# ── Projects ─────────────────────────────────────────────────────────────


async def list_workspaces(request: Request) -> JSONResponse:
    """Return configured logical workspaces for the PWA sidebar."""
    config = request.app.state.config
    return JSONResponse(_workspaces_payload(config))


def _workspace_to_dict(workspace: WorkspaceConfig, config) -> dict:
    return workspace_to_dict(workspace, config)


def _workspaces_payload(config) -> dict:
    workspaces = [_workspace_to_dict(workspace, config) for workspace in config.workspaces.values()]
    return {
        "workspaces": workspaces,
        "active": workspaces[0]["name"] if workspaces else None,
        # App-wide fallback when a workspace's default_model is empty, so the
        # PWA can label "Inherit default (<model>)" instead of a vague hint.
        "app_default_model": getattr(config, "claude_default_model", "") or "",
        "provider_options": _workspace_provider_options(config),
    }


def _parse_disallowed_tools_value(raw: object) -> list[str] | None:
    return parse_disallowed_tools_value(raw)


def _workspace_from_request(
    data: dict,
    *,
    config,
    existing: WorkspaceConfig | None = None,
) -> WorkspaceConfig:
    return workspace_from_request(data, config=config, existing=existing)


def _persist_workspaces(config) -> None:
    persist_workspaces(config)


def _refresh_project_manager_workspaces(request: Request) -> None:
    pcm = getattr(request.app.state, "project_chat_manager", None)
    refresh = getattr(pcm, "refresh_workspaces", None)
    if callable(refresh):
        refresh()


async def upsert_workspace_setting(request: Request) -> JSONResponse:
    config = request.app.state.config
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected an object"}, status_code=400)
    route_name = request.path_params.get("name")
    if route_name:
        body = {**body, "name": route_name}
    existing = config.workspace(str(body.get("name", "")).strip())
    try:
        workspace = _workspace_from_request(body, config=config, existing=existing)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    created = workspace.name not in config.workspaces
    config.workspaces[workspace.name] = workspace
    _persist_workspaces(config)
    _refresh_project_manager_workspaces(request)
    return JSONResponse(_workspaces_payload(config), status_code=201 if created else 200)


async def delete_workspace_setting(request: Request) -> JSONResponse:
    config = request.app.state.config
    name = str(request.path_params.get("name", "")).strip()
    if name not in config.workspaces:
        return JSONResponse({"error": "workspace not found"}, status_code=404)
    if len(config.workspaces) <= 1:
        return JSONResponse({"error": "cannot delete the last workspace"}, status_code=400)
    config.workspaces.pop(name, None)
    _persist_workspaces(config)
    _refresh_project_manager_workspaces(request)
    return JSONResponse(_workspaces_payload(config))


def _env_path(config) -> Path:
    return Path(config.workspace_root).resolve() / ".env"


def _read_env_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def _write_env_values(path: Path, updates: dict[str, str]) -> None:
    lines = _read_env_lines(path)
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            out.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key not in remaining:
            out.append(line)
            continue
        value = remaining.pop(key).strip()
        if value:
            out.append(f"{key}={value}")
    for key, value in remaining.items():
        value = value.strip()
        if value:
            out.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def _read_env_value(path: Path, key: str) -> str:
    for line in _read_env_lines(path):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        env_key, value = line.split("=", 1)
        if env_key.strip() == key:
            return value.strip().strip("'\"")
    return ""


def _claude_oauth_ready() -> bool:
    """True when Claude Code OAuth credentials are present on disk."""
    from ciao.setup_status import _claude_oauth_account

    raw = os.environ.get("CLAUDE_CREDENTIALS_PATH", "").strip()
    credentials_path = (
        Path(raw).expanduser() if raw else Path.home() / ".claude" / ".credentials.json"
    )
    if credentials_path.is_file():
        return True
    raw_cfg = os.environ.get("CLAUDE_CONFIG_PATH", "").strip()
    config_path = Path(raw_cfg).expanduser() if raw_cfg else Path.home() / ".claude.json"
    return bool(_claude_oauth_account(config_path))


def _provider_key_auth_method(config, key: str) -> str:
    """Return how a provider key is authenticated: 'api_key', 'oauth', or 'missing'."""
    env_value = os.environ.get(key, "").strip()
    if env_value:
        return "api_key"
    file_value = _read_env_value(_env_path(config), key)
    if file_value:
        return "api_key"
    if key == "ANTHROPIC_API_KEY" and _claude_oauth_ready():
        return "oauth"
    return "missing"


def _provider_key_configured(config, key: str) -> bool:
    return _provider_key_auth_method(config, key) != "missing"


def _provider_config_payload(config) -> dict:
    def key_payload(meta_by_key: dict) -> dict:
        keys = {}
        for key, meta in meta_by_key.items():
            auth_method = _provider_key_auth_method(config, key)
            keys[key] = {
                **meta,
                "configured": auth_method != "missing",
                "auth_method": auth_method,
            }
        return keys

    providers = setup_status(config, env=os.environ).get("providers", {})
    return {
        "keys": key_payload(_PROVIDER_KEY_META),
        "service_keys": key_payload(_SERVICE_KEY_META),
        "auto_update_github_skills": getattr(config, "auto_update_github_skills", False),
        "requires_restart": True,
        "env_path": str(_env_path(config)),
        # Each row carries its own labels so the Settings card does not have to
        # map provider ids to names; a new provider gets a correct card for
        # free instead of falling through to another provider's label.
        "connections": {
            descriptor.id: {
                **providers[descriptor.id],
                "label": descriptor.cli_label,
                "short_label": descriptor.short_label,
            }
            for descriptor in provider_registry.descriptors()
            if descriptor.id in providers
        },
    }


def _launch_provider_login(config, provider: str) -> tuple[bool, str]:
    """Open the provider-owned interactive login in macOS Terminal."""
    if not provider_registry.is_provider(provider):
        raise ValueError(f"unsupported provider '{provider}'")
    command = _auth_command_for_provider(provider)
    rendered = shlex.join(command)
    if sys.platform != "darwin":
        return False, rendered
    runtime_root = Path(config.state_path).parent
    runtime_root.mkdir(parents=True, exist_ok=True)
    script = runtime_root / f"provider-login-{provider}.command"
    script.write_text(
        "#!/bin/zsh\n"
        "script_path=$0\n"
        "rm -f -- \"$script_path\"\n"
        f"{rendered}\n"
        "status=$?\n"
        "echo\n"
        "echo 'Authentication finished. You can close this window.'\n"
        "exit $status\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    subprocess.Popen(
        ["/usr/bin/open", "-a", "Terminal", str(script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return True, rendered


async def provider_connection_action(request: Request) -> JSONResponse:
    provider = request.path_params["provider"]
    action = request.path_params["action"]
    config = request.app.state.config
    if not provider_registry.is_provider(provider):
        return JSONResponse({"error": "unsupported provider"}, status_code=404)
    if action == "connect":
        try:
            opened, command = await asyncio.to_thread(_launch_provider_login, config, provider)
        except (FileNotFoundError, OSError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "opened": opened, "command": command}, status_code=202)
    if action == "verify":
        if provider == "claude":
            from ciao.setup_status import clear_claude_discovery_cache

            await asyncio.to_thread(clear_claude_discovery_cache)
        payload = await asyncio.to_thread(_provider_config_payload, config)
        return JSONResponse(payload["connections"].get(provider, {}))
    if action == "logout":
        try:
            logout_command = _auth_command_for_provider(provider)[:1] + ["logout"]
            run = await asyncio.to_thread(
                subprocess.run,
                logout_command,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if run.returncode != 0:
            return JSONResponse(
                {"error": (run.stderr or run.stdout or "logout failed").strip()},
                status_code=400,
            )
        return JSONResponse({"ok": True})
    return JSONResponse({"error": "unsupported action"}, status_code=404)


def _apply_provider_key_updates(config, updates: dict[str, str]) -> None:
    """Push edited service keys into the process env.

    No provider key reaches the live config: every provider authenticates
    through its own CLI, so there is nothing here to re-point.
    """
    for key, value in updates.items():
        value = value.strip()
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)


async def provider_config_settings(request: Request) -> JSONResponse:
    config = request.app.state.config
    if request.method == "GET":
        return JSONResponse(await asyncio.to_thread(_provider_config_payload, config))
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected object"}, status_code=400)
    updates = {}
    if "keys" in body:
        if not isinstance(body["keys"], dict):
            return JSONResponse({"error": "keys must be an object"}, status_code=400)
        key_updates = {str(key): str(value) for key, value in body["keys"].items()}
        supported_keys = set(_PROVIDER_KEY_META) | set(_SERVICE_KEY_META)
        unsupported = sorted(set(key_updates) - supported_keys)
        if unsupported:
            return JSONResponse(
                {"error": f"unsupported provider key(s): {', '.join(unsupported)}"},
                status_code=400,
            )
        updates.update(key_updates)
    if "auto_update_github_skills" in body:
        val = bool(body["auto_update_github_skills"])
        updates["CIAO_AUTO_UPDATE_GITHUB_SKILLS"] = "true" if val else "false"
        config.auto_update_github_skills = val

    _write_env_values(_env_path(config), updates)
    provider_key_changes = {
        k: v for k, v in updates.items()
        if k in _PROVIDER_KEY_META or k in _SERVICE_KEY_META
    }
    _apply_provider_key_updates(config, provider_key_changes)
    if provider_key_changes:
        async def _do_restart():
            await asyncio.sleep(0.5)
            fn = getattr(request.app.state, "request_restart", None)
            if callable(fn):
                fn(config.restart_exit_code)
            else:
                from ciao.signals import RestartRequested
                raise RestartRequested(config.restart_exit_code)
        asyncio.create_task(_do_restart())
    return JSONResponse(await asyncio.to_thread(_provider_config_payload, config))


def _gws_profile_config_dir(config, profile: str) -> Path | None:
    # Single source of truth lives in ciao.gws_auth so the health monitor and
    # re-login manager map profiles to credential dirs the same way.
    from ciao import gws_auth

    return gws_auth.profile_config_dir(config, profile)


def _gws_file_present(config_dir: Path | None, names: tuple[str, ...]) -> bool:
    if config_dir is None:
        return False
    return any((config_dir / name).is_file() for name in names)


def _gws_profile_usage(config) -> dict[str, list[str]]:
    """Workspaces per profile.

    Only explicit links count: a workspace with no account selected is listed
    under none, rather than being attributed to an operator-level default it
    never chose.
    """
    usage: dict[str, list[str]] = {}
    for workspace in config.workspaces.values():
        profile = str(getattr(workspace, "gws_profile", "") or "").strip()
        if not profile:
            continue
        usage.setdefault(profile, []).append(getattr(workspace, "name", ""))
    return usage


def _gws_profile_names(config) -> list[str]:
    """The Google accounts to show: the ones the user added or connected.

    No built-in list: a fresh install shows none until an account is added.
    """
    from ciao import gws_auth

    return gws_auth.known_profiles(config)


def _ensure_gws_profile_registered(config, profile: str) -> None:
    """Record ``profile`` in the account registry if it is not there yet.

    Disconnecting deletes the credential files, which is also all that makes an
    unregistered (pre-registry or terminal-created) account discoverable. The
    account itself must survive that, or "Disconnect" would silently delete the
    card and leave no way back to it.
    """
    from ciao import gws_auth

    entries = gws_auth.load_profile_registry(config)
    if any(entry["name"] == profile for entry in entries):
        return
    label = str(_GWS_PROFILE_META.get(profile, {}).get("label", "")) or f"{profile} Google account"
    entries.append({"name": profile, "label": label})
    try:
        gws_auth.save_profile_registry(config, entries)
    except OSError:
        logger.exception("Failed to persist the Google account registry")


def _valid_gws_profile(profile: object) -> str:
    """Return the profile slug, or "" when the name cannot address a directory."""
    from ciao import gws_auth

    if not isinstance(profile, str):
        return ""
    return gws_auth.slugify_profile(profile)


def _gws_profile_payload(
    config,
    profile: str,
    usage: dict[str, list[str]],
    health: dict | None = None,
    labels: dict[str, str] | None = None,
) -> dict:
    custom_label = (labels or {}).get(profile, "")
    meta = _GWS_PROFILE_META.get(
        profile,
        {
            "label": custom_label or f"{profile} Google account",
            "purpose": "Google account you added. Link it to the workspaces that should use it.",
        },
    )
    if custom_label:
        meta = {**meta, "label": custom_label}
    config_dir = _gws_profile_config_dir(config, profile)
    credentials_present = _gws_file_present(config_dir, _GWS_AUTH_FILES)
    client_secret_present = _gws_file_present(config_dir, ("client_secret.json",))
    wrapper_path = Path(config.workspace_root).resolve() / "scripts" / "gws-profile.sh"
    helper_path = Path(config.workspace_root).resolve() / "scripts" / "gws-auth-helper.py"
    # The wrapper and helper take the profile name, so every account — not just
    # the two legacy ones — gets a working terminal alternative.
    setup_command = f"scripts/gws-profile.sh {profile} auth login --full"
    headless_auth_command = f"python3 scripts/gws-auth-helper.py {profile}"

    from ciao import gws_auth

    email = ""
    chips: list[str] = []
    if config_dir:
        creds_path = config_dir / "credentials.json"
        if creds_path.is_file():
            try:
                with open(creds_path, "r", encoding="utf-8") as f:
                    creds_data = json.load(f)
                email = creds_data.get("email") or ""
                # gws_auth owns both the shape tolerance and the label
                # catalogue, so a scope added to its scope sets cannot show up
                # here as a raw URL without someone naming it there first.
                chips = gws_auth.scope_labels(creds_data.get("scopes"))
            except Exception:
                pass

    # Connections made before scopes were recorded have none, and keep the
    # profile's standing description and curated chip list.
    examples = chips or list(meta.get("examples") or [])
    purpose = _gws_purpose_with_chips(meta["purpose"], chips) if chips else meta["purpose"]

    # Cached token-health snapshot from the periodic monitor (issue #145).
    # Read-only and cheap — never runs the `auth status` subprocess here.
    token_valid: bool | None = None
    token_error = ""
    needs_relogin = False
    if credentials_present and isinstance(health, dict) and "token_valid" in health:
        token_valid = bool(health.get("token_valid"))
        token_error = str(health.get("token_error") or "")
        needs_relogin = not token_valid

    return {
        "name": profile,
        "label": meta["label"],
        "purpose": purpose,
        "examples": examples,
        "configured": credentials_present,
        "credentials_present": credentials_present,
        "client_secret_present": client_secret_present,
        "config_dir": str(config_dir) if config_dir is not None else "",
        "workspaces": usage.get(profile, []),
        "setup_command": setup_command,
        "headless_auth_command": headless_auth_command,
        "wrapper_available": wrapper_path.is_file(),
        "helper_available": helper_path.is_file(),
        "email": email,
        "token_valid": token_valid,
        "token_error": token_error,
        "needs_relogin": needs_relogin,
    }


def _gws_integration_payload(config) -> dict:
    from ciao import gws_auth

    usage = _gws_profile_usage(config)
    binary_path = resolve_tool("gws") or ""
    wrapper_path = Path(config.workspace_root).resolve() / "scripts" / "gws-profile.sh"
    helper_path = Path(config.workspace_root).resolve() / "scripts" / "gws-auth-helper.py"
    try:
        health = gws_auth.read_health_cache(Path(config.state_path).parent)
    except Exception:
        health = {}
    labels = {
        entry["name"]: entry["label"]
        for entry in gws_auth.load_profile_registry(config)
        if entry.get("label")
    }
    names = _gws_profile_names(config)
    # An operator default that names no existing account is not a default the
    # UI should advertise; workspaces then show "No Google account" instead.
    default_profile = str(getattr(config, "gws_default_profile", "") or "").strip()
    if default_profile not in names:
        default_profile = ""
    return {
        "installed": bool(binary_path),
        "binary_path": binary_path,
        "default_profile": default_profile,
        "wrapper_path": str(wrapper_path) if wrapper_path.is_file() else "",
        "headless_helper_path": str(helper_path) if helper_path.is_file() else "",
        "profiles": [
            _gws_profile_payload(config, profile, usage, health.get(profile), labels)
            for profile in names
        ],
    }


async def gws_integration_settings(request: Request) -> JSONResponse:
    return JSONResponse(_gws_integration_payload(request.app.state.config))


GWS_CLI_PACKAGE = "@googleworkspace/cli"


async def gws_install(request: Request) -> JSONResponse:
    """Install the Google Workspace CLI globally via npm.

    Runs ``npm install -g @googleworkspace/cli`` so the ``gws`` binary becomes
    available on PATH. Returns the refreshed integration payload so the UI can
    reflect the new status without a restart (unlike the local voice engine, no
    Python import changes, so no server restart is needed).
    """
    config = request.app.state.config

    if resolve_tool("gws"):
        return JSONResponse(
            {
                "ok": True,
                "output": "gws is already installed.",
                "integration": _gws_integration_payload(config),
            }
        )

    npm = resolve_tool("npm")
    if not npm:
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    "npm was not found on PATH. Install Node.js/npm, then run "
                    f"'npm install -g {GWS_CLI_PACKAGE}' manually."
                ),
            },
            status_code=500,
        )

    cmd = [npm, "install", "-g", GWS_CLI_PACKAGE]
    env = dict(os.environ)
    env["PATH"] = login_shell_path()
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    output = (result.stdout.strip() + "\n" + result.stderr.strip()).strip()
    if result.returncode != 0:
        return JSONResponse(
            {
                "ok": False,
                "error": f"npm exited with code {result.returncode}",
                "output": output,
            },
            status_code=500,
        )

    return JSONResponse(
        {
            "ok": True,
            "output": output,
            "integration": _gws_integration_payload(config),
        }
    )


async def gws_save_client_secret(request: Request) -> JSONResponse:
    config = request.app.state.config
    try:
        body = await request.json()
        profile = body.get("profile")
        client_secret_str = body.get("client_secret")
    except Exception:
        return JSONResponse({"error": "Invalid request payload"}, status_code=400)

    profile = _valid_gws_profile(profile)
    if not profile:
        return JSONResponse({"error": "Invalid profile"}, status_code=400)

    if not client_secret_str:
        return JSONResponse({"error": "Missing client_secret content"}, status_code=400)

    try:
        secret_json = json.loads(client_secret_str)
        if "installed" not in secret_json and "web" not in secret_json:
            return JSONResponse({"error": "client_secret.json missing 'installed' or 'web' section"}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"Invalid JSON format: {str(e)}"}, status_code=400)

    config_dir = _gws_profile_config_dir(config, profile)
    if config_dir is None:
        return JSONResponse({"error": "Could not determine config directory"}, status_code=500)

    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        path = config_dir / "client_secret.json"
        path.write_text(json.dumps(secret_json, indent=2), encoding="utf-8")
        path.chmod(0o600)
    except Exception as e:
        return JSONResponse({"error": f"Failed to write client_secret.json: {str(e)}"}, status_code=500)

    return JSONResponse(_gws_integration_payload(config))


async def gws_auth_url(request: Request) -> JSONResponse:
    config = request.app.state.config
    try:
        body = await request.json()
        profile = body.get("profile")
    except Exception:
        return JSONResponse({"error": "Invalid request payload"}, status_code=400)

    profile = _valid_gws_profile(profile)
    if not profile:
        return JSONResponse({"error": "Invalid profile"}, status_code=400)

    config_dir = _gws_profile_config_dir(config, profile)
    if config_dir is None:
        return JSONResponse({"error": "Could not determine config directory"}, status_code=500)

    from ciao import gws_auth

    try:
        installed = gws_auth.load_client_secret(config_dir)
        client_id = installed.get("client_id")
        if not client_id:
            return JSONResponse({"error": "client_secret.json missing client_id"}, status_code=400)
        redirect_uris = installed.get("redirect_uris", ["http://localhost"])
        redirect_uri = redirect_uris[0]
        auth_url = gws_auth.build_auth_url(
            client_id=client_id,
            redirect_uri=redirect_uri,
            scopes=gws_auth.scopes_for_profile(profile),
        )
        return JSONResponse({"auth_url": auth_url})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"Failed to generate authorization URL: {str(e)}"}, status_code=500)


async def gws_exchange_code(request: Request) -> JSONResponse:
    config = request.app.state.config
    try:
        body = await request.json()
        profile = body.get("profile")
        code_or_url = body.get("code")
    except Exception:
        return JSONResponse({"error": "Invalid request payload"}, status_code=400)

    profile = _valid_gws_profile(profile)
    if not profile:
        return JSONResponse({"error": "Invalid profile"}, status_code=400)

    if not code_or_url:
        return JSONResponse({"error": "Missing authorization code or redirect URL"}, status_code=400)

    config_dir = _gws_profile_config_dir(config, profile)
    if config_dir is None:
        return JSONResponse({"error": "Could not determine config directory"}, status_code=500)

    from ciao import gws_auth

    try:
        installed = gws_auth.load_client_secret(config_dir)
        redirect_uris = installed.get("redirect_uris", ["http://localhost"])
        redirect_uri = redirect_uris[0]
        code = gws_auth.extract_code_from_input(code_or_url)
        # Token exchange + credential write happen off the event loop; the
        # helper never logs the code, tokens, or secret.
        await asyncio.to_thread(
            gws_auth.exchange_and_store,
            config,
            profile,
            code=code,
            redirect_uri=redirect_uri,
        )
        # Refresh the cached token-validity state so the Settings UI clears
        # the "Login expired" banner immediately instead of waiting up to
        # ``CIAO_GWS_HEALTH_INTERVAL`` seconds. Mirrors gws_relogin_status.
        monitor = getattr(request.app.state, "gws_health_monitor", None)
        if monitor is not None:
            try:
                await asyncio.to_thread(monitor.check_once)
            except Exception:
                logger.exception("Post-exchange health refresh failed")
        return JSONResponse(_gws_integration_payload(config))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"Authentication exchange failed: {str(e)}"}, status_code=500)


async def gws_disconnect(request: Request) -> JSONResponse:
    config = request.app.state.config
    try:
        body = await request.json()
        profile = body.get("profile")
        delete_client_secret = bool(body.get("delete_client_secret", False))
    except Exception:
        return JSONResponse({"error": "Invalid request payload"}, status_code=400)

    profile = _valid_gws_profile(profile)
    if not profile:
        return JSONResponse({"error": "Invalid profile"}, status_code=400)

    config_dir = _gws_profile_config_dir(config, profile)
    if config_dir is None:
        return JSONResponse({"error": "Could not determine config directory"}, status_code=500)

    # Disconnecting keeps the account; only "remove" deletes it.
    _ensure_gws_profile_registered(config, profile)

    try:
        for name in ("credentials.json", "credentials.enc", "token_cache.json",
                     "credentials.json.old", "credentials.enc.old", "token_cache.json.old"):
            path = config_dir / name
            if path.exists():
                path.unlink()
        
        if delete_client_secret:
            secret_path = config_dir / "client_secret.json"
            if secret_path.exists():
                secret_path.unlink()
    except Exception as e:
        return JSONResponse({"error": f"Failed to disconnect profile: {str(e)}"}, status_code=500)

    return JSONResponse(_gws_integration_payload(config))


async def gws_add_profile(request: Request) -> JSONResponse:
    """Register a Google account so workspaces can be linked to it.

    Adding is bookkeeping only: it records the name and label. Credentials
    arrive later through the OAuth flow, which creates the credential dir.
    """
    config = request.app.state.config
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request payload"}, status_code=400)

    from ciao import gws_auth

    raw_name = str(body.get("name", "") or "").strip()
    profile = _valid_gws_profile(raw_name)
    if not profile:
        return JSONResponse(
            {"error": "Give the account a name using letters, numbers, dashes, or underscores."},
            status_code=400,
        )
    if profile in gws_auth.GWS_SERVICE_NAMES:
        return JSONResponse(
            {
                "error": (
                    f"'{profile}' is reserved for a Google Workspace service; "
                    "choose another account name."
                )
            },
            status_code=400,
        )
    if profile in _gws_profile_names(config):
        return JSONResponse(
            {"error": f"A Google account named '{profile}' already exists."},
            status_code=400,
        )
    label = str(body.get("label", "") or "").strip() or f"{raw_name} Google account"
    entries = gws_auth.load_profile_registry(config)
    entries.append({"name": profile, "label": label})
    try:
        gws_auth.save_profile_registry(config, entries)
    except OSError as exc:
        return JSONResponse({"error": f"Failed to save the account list: {exc}"}, status_code=500)
    return JSONResponse(_gws_integration_payload(config))


async def gws_remove_profile(request: Request) -> JSONResponse:
    """Forget a Google account and delete its stored credentials.

    Workspaces pointing at it are unlinked in the same pass so the registry
    cannot keep a dangling reference to an account that no longer exists.
    """
    config = request.app.state.config
    try:
        body = await request.json()
        profile = body.get("profile")
    except Exception:
        return JSONResponse({"error": "Invalid request payload"}, status_code=400)

    from ciao import gws_auth

    profile = _valid_gws_profile(profile)
    if not profile:
        return JSONResponse({"error": "Invalid profile"}, status_code=400)

    config_dir = _gws_profile_config_dir(config, profile)
    if config_dir is not None and config_dir.is_dir():
        try:
            shutil.rmtree(config_dir)
        except OSError as exc:
            return JSONResponse(
                {"error": f"Failed to delete stored credentials: {exc}"}, status_code=500
            )

    entries = [
        entry for entry in gws_auth.load_profile_registry(config) if entry["name"] != profile
    ]
    try:
        gws_auth.save_profile_registry(config, entries)
    except OSError as exc:
        return JSONResponse({"error": f"Failed to save the account list: {exc}"}, status_code=500)

    unlinked = False
    for workspace in config.workspaces.values():
        if getattr(workspace, "gws_profile", "") == profile:
            workspace.gws_profile = ""
            unlinked = True
    if unlinked:
        config.persist_workspace_registry()

    return JSONResponse(_gws_integration_payload(config))


def _gws_relogin_manager(request: Request):
    """Return the app's re-login manager, creating one on first use.

    Lazily attached so route modules (and tests) that build a bare app with a
    ``config`` on ``app.state`` still get a working manager without extra
    wiring. ``main.py`` also attaches one at startup.
    """
    manager = getattr(request.app.state, "gws_relogin_manager", None)
    if manager is None:
        from ciao.gws_auth import GwsReloginManager

        manager = GwsReloginManager(request.app.state.config)
        request.app.state.gws_relogin_manager = manager
    return manager


async def gws_relogin_start(request: Request) -> JSONResponse:
    """Start a server-managed OAuth re-login for a profile (issue #145).

    Binds a loopback callback listener inside this long-lived process and
    returns the Google consent URL. The listener survives across chat turns
    (unlike ``gws auth login`` in a background bash task), captures the
    redirect, and exchanges the code server-side. Never returns tokens.
    """
    try:
        body = await request.json()
        profile = body.get("profile")
    except Exception:
        return JSONResponse({"error": "Invalid request payload"}, status_code=400)

    profile = _valid_gws_profile(profile)
    if not profile:
        return JSONResponse({"error": "Invalid profile"}, status_code=400)

    manager = _gws_relogin_manager(request)
    try:
        result = await asyncio.to_thread(manager.start, profile)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": f"Failed to start re-login: {str(e)}"}, status_code=500)
    return JSONResponse(result)


async def gws_relogin_status(request: Request) -> JSONResponse:
    """Poll a pending re-login. Returns pending/completed/error/none."""
    profile = _valid_gws_profile(request.query_params.get("profile", ""))
    if not profile:
        return JSONResponse({"error": "Invalid profile"}, status_code=400)
    manager = _gws_relogin_manager(request)
    result = manager.status(profile)
    # When a re-login just completed, refresh the health cache so Settings and
    # the next status check see the profile as valid without waiting a cycle.
    if result.get("status") == "completed":
        monitor = getattr(request.app.state, "gws_health_monitor", None)
        if monitor is not None:
            try:
                await asyncio.to_thread(monitor.check_once)
            except Exception:
                logger.exception("Post-relogin health refresh failed")
    return JSONResponse(result)


async def gws_relogin_cancel(request: Request) -> JSONResponse:
    """Cancel a pending re-login and tear down its loopback listener."""
    try:
        body = await request.json()
        profile = body.get("profile")
    except Exception:
        return JSONResponse({"error": "Invalid request payload"}, status_code=400)
    profile = _valid_gws_profile(profile)
    if not profile:
        return JSONResponse({"error": "Invalid profile"}, status_code=400)
    manager = _gws_relogin_manager(request)
    return JSONResponse(manager.cancel(profile))


async def list_projects(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    workspace = request.query_params.get("workspace")
    projects = pcm.list_projects(workspace)
    return JSONResponse([p.to_dict() for p in projects])


async def create_project(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    config = request.app.state.config
    body = await request.json()
    project = pcm.create_project(
        name=body["name"],
        # An omitted workspace has to resolve to one that exists: create_project
        # does not validate the name, so an unknown one yields a project that is
        # filtered out of every workspace's sidebar.
        workspace=body.get("workspace") or config.primary_workspace(),
        context=body.get("context", ""),
    )
    return JSONResponse(project.to_dict(), status_code=201)


async def reorder_projects(request: Request) -> JSONResponse:
    """Persist a drag-reordered project sequence for one workspace."""
    pcm = request.app.state.project_chat_manager
    body = await request.json()
    workspace = body.get("workspace") or ""
    ordered_ids = body.get("order")
    if not workspace or not isinstance(ordered_ids, list):
        return JSONResponse(
            {"error": "workspace and order[] are required"}, status_code=400
        )
    projects = pcm.reorder_projects(workspace, [str(pid) for pid in ordered_ids])
    return JSONResponse([p.to_dict() for p in projects])


async def project_detail(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    project_id = request.path_params["project_id"]
    if request.method == "DELETE":
        try:
            ok = pcm.delete_project(project_id)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": ok})
    # PATCH
    body = await request.json()
    try:
        project = pcm.update_project(
            project_id,
            name=body.get("name"),
            context=body.get("context"),
            vault_folder=body.get("vault_folder"),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if project is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(project.to_dict())


async def project_complete(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    project_id = request.path_params["project_id"]
    try:
        result = pcm.complete_project(project_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(result)


async def list_completed_projects(request: Request) -> JSONResponse:
    """List completed (archived) projects by scanning the vault completed/ tree.

    Read-only. Optional ``workspace`` query param scopes to one workspace.
    """
    pcm = request.app.state.project_chat_manager
    workspace = request.query_params.get("workspace")
    return JSONResponse(pcm.list_completed_projects(workspace))


async def project_restore(request: Request) -> JSONResponse:
    """Restore a completed project back to active/. Body: ``{workspace, stem}``."""
    pcm = request.app.state.project_chat_manager
    body = await request.json()
    try:
        result = pcm.restore_project(
            workspace=body.get("workspace", ""),
            stem=body.get("stem", ""),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(result)


async def project_chats(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    project_id = request.path_params["project_id"]
    chats = pcm.list_chats(project_id)
    return JSONResponse([c.to_dict() for c in chats])


async def project_files_list(request: Request) -> JSONResponse:
    """List files under a project's vault folder.

    Returns 200 with ``[]`` for projects without a folder-backed vault entry
    (manual projects, single-file personal projects, missing folders), so the
    UI can hide the section without distinguishing the cases.
    """
    pcm = request.app.state.project_chat_manager
    project_id = request.path_params["project_id"]
    project = pcm.get_project(project_id)
    if project is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    files = pcm.list_project_files(project_id)
    return JSONResponse(files)


async def project_files_upload(request: Request) -> JSONResponse:
    """Upload one or more files into a project's vault folder."""
    pcm = request.app.state.project_chat_manager
    project_id = request.path_params["project_id"]
    project = pcm.get_project(project_id)
    if project is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    form = await request.form()
    saved: list[dict] = []
    errors: list[dict] = []
    for key in form:
        upload = form[key]
        if not hasattr(upload, "read"):
            continue
        filename = getattr(upload, "filename", "") or ""
        try:
            data = await _read_upload_limited(upload, _PROJECT_UPLOAD_MAX_BYTES)
            entry = pcm.save_project_file_upload(project_id, data, filename)
            saved.append(entry)
        except LookupError as exc:
            # Project has no vault folder to upload into. Same status across
            # all uploads in this request — return 409 immediately.
            return JSONResponse({"error": str(exc)}, status_code=409)
        except ValueError as exc:
            errors.append({"filename": filename, "error": str(exc)})
    return JSONResponse({"saved": saved, "errors": errors})


_DESKTOP_DROP_GRANT_TTL_SECONDS = 5 * 60
_DESKTOP_DROP_MAX_FILES = 100


def _looks_like_nsird_screenshot(path: Path) -> bool:
    """True if ``path`` points into a macOS ``screencaptureui`` staging directory.

    Files freshly captured to the clipboard by ``screencaptureui`` land under
    ``.../TemporaryItems/NSIRD_screencaptureui_<id>/Screenshot *.png`` and the
    kernel only lets the capturing process read them, so another process
    reading them raises ``OSError`` (EPERM). Detect the case by path: match a
    ``NSIRD_`` path component rather than the errno (EPERM and EACCES both map
    to ``PermissionError``, so the subclass tells us nothing) or the literal
    ``screencaptureui`` name, which the id suffix changes per capture.
    """
    return any(part.startswith("NSIRD_") for part in path.parts)


def _desktop_drop_read_error(path: Path, exc: OSError) -> str:
    """User-facing text for a dropped file this process cannot read.

    A drag straight from the macOS screenshot thumbnail hands over a path only
    the app that received the drop may read, so the desktop shell stages a copy
    first (`stage_dropped_file` in desktop/src-tauri/src/lib.rs). When there is
    no staged copy, because the shell is older than that fix or the drop was not
    an image, the raw errno tells the user nothing they can act on.

    Four tiers, narrowest first. An NSIRD path gets screenshot-specific advice.
    ``EDEADLK`` means a cloud placeholder (see below). Any other permission
    denial still gets actionable text, just without naming a screenshot, so a
    plain unreadable drop is not mislabelled and does not regress to a raw
    errno. Anything else falls through to the errno, which is all we know
    about it.
    """
    if _looks_like_nsird_screenshot(path):
        return (
            "macOS won't let us read this screenshot directly. "
            "Save it to disk first, then drag it in."
        )
    if exc.errno == errno.EDEADLK:
        # A file dragged out of iCloud Drive (or any other File Provider) whose
        # bytes are not on disk: `stat` reports the real size, so the grant's
        # existence check passes, and the read is then refused with EDEADLK
        # ("Resource deadlock avoided") because this process may not ask the
        # provider to materialise it. Unlike EPERM the errno is unambiguous
        # here, so it needs no corroborating path check. The desktop shell
        # stages unreadable drops past this (`needs_drop_staging` in
        # desktop/src-tauri/src/lib.rs); a file over the staging limit, an
        # older shell, or a client node transferring a non-image still lands
        # here. A non-image dropped on a host does not: the path is handed to
        # the agent unread, so the agent hits the same errno on its own.
        return (
            f"{path.name} is not downloaded to this Mac yet. Right-click it in "
            "Finder, choose Download Now, then drag it in again."
        )
    if isinstance(exc, PermissionError):
        return (
            f"macOS would not let Ciaobot read {path.name}. Save the file to a "
            "folder first, then drag it in."
        )
    return str(exc)


def _clear_desktop_drop_staging(request: Request, grant_id: str) -> None:
    """Delete the desktop shell's staged copies for a consumed grant.

    Only the image copies are dead weight by this point: their bytes are in
    media_root or on the host. A staged non-image copy is the agent's only
    readable handle on a cloud placeholder, so it is deliberately left in
    place for the agent to keep reading; the shell's stale sweep reclaims it
    later. Best-effort: the shell's own stale sweep covers a grant that errored
    out before reaching here.
    """
    try:
        # The id reaches us from the request body, and this builds an rmtree
        # target. Re-check the UUID form here rather than trusting that every
        # caller validated it first.
        if str(UUID(grant_id)) != grant_id:
            return
    except (ValueError, AttributeError):
        return
    grant_dir = request.app.state.config.state_path.parent / "desktop-drop-grants"
    staged_dir = grant_dir / "staged" / grant_id
    try:
        for index_dir in staged_dir.iterdir():
            if not index_dir.is_dir():
                continue
            for staged in index_dir.iterdir():
                if (
                    staged.is_file()
                    and staged.suffix.lower() in _ALLOWED_IMAGE_EXTENSIONS
                ):
                    staged.unlink(missing_ok=True)
            # Drop the index dir once every copy in it went away.
            try:
                index_dir.rmdir()
            except OSError:
                pass
        staged_dir.rmdir()
    except OSError:
        pass


def _consume_desktop_drop_grant(request: Request, grant_id: str) -> list[Path]:
    """Consume a native-app grant and return only its explicitly dropped paths."""
    try:
        canonical_id = str(UUID(grant_id))
    except (ValueError, AttributeError) as exc:
        raise ValueError("invalid desktop drop grant") from exc
    if canonical_id != grant_id:
        raise ValueError("invalid desktop drop grant")

    config = request.app.state.config
    grant_dir = config.state_path.parent / "desktop-drop-grants"
    source = grant_dir / f"{grant_id}.json"
    consuming = grant_dir / f".{grant_id}.consuming"
    try:
        source.replace(consuming)
    except FileNotFoundError as exc:
        raise LookupError("desktop drop grant not found or already used") from exc

    try:
        payload = json.loads(consuming.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("invalid desktop drop grant") from exc
    finally:
        consuming.unlink(missing_ok=True)

    if not isinstance(payload, dict):
        raise ValueError("invalid desktop drop grant")
    created_at = payload.get("created_at")
    raw_paths = payload.get("paths")
    if not isinstance(created_at, (int, float)):
        raise ValueError("invalid desktop drop grant")
    age = datetime.now(UTC).timestamp() - float(created_at)
    if age < -30 or age > _DESKTOP_DROP_GRANT_TTL_SECONDS:
        raise ValueError("desktop drop grant expired")
    if (
        not isinstance(raw_paths, list)
        or not raw_paths
        or len(raw_paths) > _DESKTOP_DROP_MAX_FILES
        or not all(isinstance(path, str) for path in raw_paths)
    ):
        raise ValueError("invalid desktop drop grant")

    paths = [Path(path) for path in raw_paths]
    if any(not path.is_absolute() or not path.exists() for path in paths):
        raise ValueError("a dropped file is no longer available")
    return paths


async def desktop_drop_import(request: Request) -> JSONResponse:
    """Resolve a single-use native Finder drop for the active host or client."""
    body = await request.json()
    grant_id = str(body.get("grant_id") or "")
    project_id = str(body.get("project_id") or "")
    chat_id = str(body.get("chat_id") or "")
    try:
        paths = _consume_desktop_drop_grant(request, grant_id)
    except LookupError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    node_mgr = getattr(request.app.state, "node_state_manager", None)
    role = node_mgr.get_role() if node_mgr else "host"
    is_client = role in {"client", "standby"}
    image_paths = [
        path
        for path in paths
        if path.is_file() and path.suffix.lower() in _ALLOWED_IMAGE_EXTENSIONS
    ]
    regular_paths = [path for path in paths if path not in image_paths]
    errors: list[dict[str, str]] = []

    if not is_client:
        pcm = request.app.state.project_chat_manager
        host_image_refs: list[str] = []
        if image_paths and pcm.get_chat(chat_id) is None:
            errors.extend(
                {"filename": path.name, "error": "chat not found"}
                for path in image_paths
            )
        else:
            for path in image_paths:
                try:
                    if path.stat().st_size > request.app.state.config.max_image_size_bytes:
                        raise ValueError("image too large")
                    host_image_refs.append(
                        pcm.save_image_upload(path.read_bytes(), path.name).path.name
                    )
                except OSError as exc:
                    errors.append(
                        {
                            "filename": path.name,
                            "error": _desktop_drop_read_error(path, exc),
                        }
                    )
                except ValueError as exc:
                    errors.append({"filename": path.name, "error": str(exc)})
        _clear_desktop_drop_staging(request, grant_id)
        return JSONResponse(
            {
                "paths": [str(path) for path in regular_paths],
                "image_refs": host_image_refs,
                "errors": errors,
            }
        )

    if node_mgr is None:
        return JSONResponse({"error": "client node state unavailable"}, status_code=503)
    host_url = node_mgr.get_active_peer_url()
    if not host_url:
        return JSONResponse({"error": "client has no reachable host"}, status_code=503)

    import httpx

    from ciao.web.auth import SESSION_COOKIE

    headers = {"origin": host_url.rstrip("/")}
    host_session = node_mgr.get_host_session()
    if host_session:
        headers["cookie"] = f"{SESSION_COOKIE}={host_session}"
    timeout = httpx.Timeout(60.0, connect=5.0)
    imported_paths: list[str] = []
    image_refs: list[str] = []

    try:
        # These are fixed API endpoints, so a redirect is never expected.
        # Refusing it also prevents a configured/compromised peer from
        # forwarding the stored host-session cookie to another origin.
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            if image_paths:
                image_files = []
                for index, path in enumerate(image_paths):
                    # Per-file, like the host branch above: one unreadable
                    # screenshot must not turn the whole drop into a 502.
                    try:
                        if path.stat().st_size > request.app.state.config.max_image_size_bytes:
                            errors.append({"filename": path.name, "error": "image too large"})
                            continue
                        data = path.read_bytes()
                    except OSError as exc:
                        errors.append(
                            {
                                "filename": path.name,
                                "error": _desktop_drop_read_error(path, exc),
                            }
                        )
                        continue
                    image_files.append(
                        (
                            f"file{index}",
                            (
                                path.name,
                                data,
                                mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                            ),
                        )
                    )
                if image_files:
                    response = await client.post(
                        f"{host_url.rstrip('/')}/api/chats/{chat_id}/images",
                        headers=headers,
                        files=image_files,
                    )
                    payload = response.json()
                    if response.is_success and isinstance(payload, list):
                        for entry in payload:
                            if entry.get("ref"):
                                image_refs.append(str(entry["ref"]))
                            elif entry.get("error"):
                                errors.append(
                                    {
                                        "filename": str(entry.get("filename") or ""),
                                        "error": str(entry["error"]),
                                    }
                                )
                    else:
                        raise ValueError(
                            payload.get("error", "host image upload failed")
                            if isinstance(payload, dict)
                            else "host image upload failed"
                        )

            files: list[tuple[str, tuple[str, bytes, str]]] = []
            for path in regular_paths:
                if path.is_dir():
                    errors.append(
                        {
                            "filename": path.name,
                            "error": "folders cannot be transferred to the host",
                        }
                    )
                    continue
                try:
                    if path.stat().st_size > _PROJECT_UPLOAD_MAX_BYTES:
                        errors.append({"filename": path.name, "error": "file too large"})
                        continue
                    data = path.read_bytes()
                except OSError as exc:
                    errors.append(
                        {
                            "filename": path.name,
                            "error": _desktop_drop_read_error(path, exc),
                        }
                    )
                    continue
                files.append(
                    (
                        f"file{len(files)}",
                        (
                            path.name,
                            data,
                            mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                        ),
                    )
                )
            if files:
                response = await client.post(
                    f"{host_url.rstrip('/')}/api/projects/{project_id}/files",
                    headers=headers,
                    files=files,
                )
                payload = response.json()
                if not response.is_success or not isinstance(payload, dict):
                    raise ValueError(
                        payload.get("error", "host file upload failed")
                        if isinstance(payload, dict)
                        else "host file upload failed"
                    )
                imported_paths.extend(
                    str(entry["absolute_path"])
                    for entry in payload.get("saved", [])
                    if entry.get("absolute_path")
                )
                errors.extend(
                    {
                        "filename": str(entry.get("filename") or ""),
                        "error": str(entry.get("error") or "upload failed"),
                    }
                    for entry in payload.get("errors", [])
                )
    except (OSError, httpx.HTTPError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    finally:
        _clear_desktop_drop_staging(request, grant_id)

    return JSONResponse(
        {"paths": imported_paths, "image_refs": image_refs, "errors": errors}
    )


async def create_project_chat(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    project_id = request.path_params["project_id"]
    body = await request.json()
    try:
        chat = pcm.create_chat(
            project_id,
            title=body.get("title", "New Chat"),
            model=body.get("model"),
            mode=body.get("mode"),
            provider=body.get("provider"),
            control_surface=body.get("control_surface"),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(chat.to_dict(local=True), status_code=201)


# ── Chats ────────────────────────────────────────────────────────────────

def _codex_reasoning_levels(catalog: list[dict]) -> dict[str, list[str]]:
    """Per-model reasoning levels from the codex catalog."""
    levels: dict[str, list[str]] = {}
    for item in catalog:
        if item.get("hidden"):
            continue
        model_id = str(item.get("model") or item.get("id") or "")
        if not model_id:
            continue
        efforts = item.get("supportedReasoningEfforts")
        levels[model_id] = [
            str(option.get("reasoningEffort"))
            for option in efforts or []
            if isinstance(option, dict) and option.get("reasoningEffort")
        ]
    return levels


async def _unsupported_codex_level_error(
    config, pcm, chat_id: str, body: dict
) -> JSONResponse | None:
    """Reject a codex thinking level the target model doesn't support.

    ``update_chat`` validates against the static ``THINKING_LEVELS`` union;
    the model catalog is authoritative when discovery works, so narrow the
    check to the target model here. Fails open when the catalog is
    unavailable or has no levels for the model, leaving the union check as
    the backstop.
    """
    level = body.get("thinking_level")
    if not level:
        return None
    chat = pcm.get_chat(chat_id)
    if chat is None:
        return None
    provider = body.get("provider") or chat.provider
    if provider != "codex":
        return None
    model = body.get("model") or chat.model
    try:
        catalog = await CodexProvider.model_catalog(config.workspace_root)
    except Exception:
        return None
    allowed = _codex_reasoning_levels(catalog).get(model)
    if allowed and level not in allowed:
        return JSONResponse(
            {
                "error": (
                    f"Unknown thinking level '{level}' for codex model "
                    f"'{model}' (allowed: {', '.join(allowed)})"
                )
            },
            status_code=400,
        )
    return None


async def list_all_chats(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    return JSONResponse(pcm.list_chats_dicts())


async def chat_detail(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    if request.method == "DELETE":
        # `only_if_empty` is how closing a chat discards a never-used draft.
        # The check has to happen here: "empty" means default title, no user
        # turns, no session and no live stream, and `user_turn_count` is not
        # in any payload the PWA receives — a client-side approximation of the
        # rule deletes chats the server would have kept.
        if request.query_params.get("only_if_empty") in {"1", "true"}:
            if not pcm.is_empty_chat(chat_id):
                return JSONResponse({"ok": False, "deleted": False, "reason": "not empty"})
        ok = pcm.delete_chat(chat_id)
        return JSONResponse({"ok": ok, "deleted": ok})
    # PATCH
    body = await request.json()
    if "control_surface" in body:
        surface = str(body.get("control_surface") or "").strip()
        if surface not in {"", "legacy", "mcp", "auto"}:
            return JSONResponse(
                {"error": "control_surface must be legacy, mcp, auto, or empty"},
                status_code=400,
            )
    level_error = await _unsupported_codex_level_error(
        request.app.state.config, pcm, chat_id, body
    )
    if level_error is not None:
        return level_error
    try:
        chat = pcm.update_chat(
            chat_id,
            title=body.get("title"),
            model=body.get("model"),
            provider=body.get("provider"),
            mode=body.get("mode"),
            project_id=body.get("project_id"),
            thinking_level=body.get("thinking_level"),
        )
        if chat is not None and "control_surface" in body:
            changed = chat.control_surface != surface
            chat.control_surface = surface
            if changed:
                pcm._revoke_mcp_chat(chat_id)
                provider_service = pcm._providers.pop(chat_id, None)
                if provider_service is not None:
                    asyncio.create_task(provider_service.disconnect())
                pcm._save(reason="chat_control_surface")
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if chat is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(chat.to_dict(local=pcm.is_session_local(chat)))


async def chat_new_session(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    try:
        chat = pcm.new_session(chat_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if chat is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(chat.to_dict(local=True))


async def chat_handover(request: Request) -> JSONResponse:
    """Explicitly continue a chat on a fresh provider session."""
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    body = await request.json()
    provider = str(body.get("provider", "")).strip()
    model = str(body.get("model", "")).strip()
    raw_messages = body.get("messages", [])
    messages = raw_messages if isinstance(raw_messages, list) else []
    try:
        chat = pcm.handover_chat(
            chat_id,
            provider=provider,
            model=model,
            messages=[m for m in messages if isinstance(m, dict)],
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if chat is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(chat.to_dict(local=pcm.is_session_local(chat)))


async def chat_fork(request: Request) -> JSONResponse:
    """Create an independent chat from history through one final answer."""
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "JSON body must be an object"}, status_code=400)
    messages = body.get("messages")
    if not isinstance(messages, list):
        return JSONResponse({"error": "messages must be a list"}, status_code=400)
    turn_index = body.get("turn_index")
    if (
        not isinstance(turn_index, int)
        or isinstance(turn_index, bool)
        or turn_index < 0
    ):
        return JSONResponse(
            {"error": "turn_index must be a non-negative integer"},
            status_code=400,
        )
    try:
        fork = pcm.fork_chat(
            chat_id,
            messages=[row for row in messages if isinstance(row, dict)],
            turn_index=turn_index,
        )
    except KeyError:
        return JSONResponse({"error": "not found"}, status_code=404)
    except ValueError as exc:
        status = 404 if str(exc) == "Source project not found" else 400
        return JSONResponse({"error": str(exc)}, status_code=status)
    except Exception as exc:
        logger.exception("Failed to fork chat %s", chat_id)
        return JSONResponse({"error": f"Failed to fork chat: {exc}"}, status_code=500)
    return JSONResponse(fork.to_dict(local=True))


async def chat_continue(request: Request) -> JSONResponse:
    """Create a new active chat that continues from an archived chat."""
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    try:
        chat = pcm.continue_archived_chat(chat_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": f"Failed to continue chat: {exc}"}, status_code=500)
    return JSONResponse(chat.to_dict(local=pcm.is_session_local(chat)))


async def chat_retry(request: Request) -> JSONResponse:
    """Manage deferred retry state for a chat."""
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    body = await request.json()
    action = str(body.get("action", "try_now"))
    if action == "stop":
        chat = pcm.stop_chat_retry(chat_id)
    elif action == "set":
        prompt = str(body.get("prompt", ""))
        images = [str(x) for x in body.get("images", []) if str(x)]
        chat = pcm.set_chat_retry(chat_id, prompt, image_refs=images, reason="manual")
    elif action == "try_now":
        stream = pcm.try_chat_retry_now(chat_id)
        chat = pcm.get_chat(chat_id)
        if chat is not None and stream is None and chat.retry_status == "pending":
            return JSONResponse({"error": "retry not started"}, status_code=409)
    else:
        return JSONResponse({"error": "unknown retry action"}, status_code=400)
    if chat is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(chat.to_dict(local=pcm.is_session_local(chat)))


async def chat_stop(request: Request) -> JSONResponse:
    """Stop an in-flight turn over plain HTTP.

    The websocket ``stop`` message (see routes_chat.py) is the normal path,
    but it depends on that chat's socket being connected at the moment the
    user clicks Stop. A socket cycling through reconnects (e.g. the per-chat
    liveness watchdog force-reconnecting under load) can swallow the message
    indefinitely with no visible error, leaving a turn nobody can interrupt.
    This route reaches ``stop_chat`` independently of any socket state.
    """
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    if pcm.get_chat(chat_id) is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    stopped = await pcm.stop_chat(chat_id)
    return JSONResponse({"stopped": stopped})


async def chat_prompt(request: Request) -> JSONResponse:
    """Send a prompt to start a model turn in the chat (background task)."""
    from ciao.models import ImageAttachment

    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    body = await request.json()
    prompt = body.get("prompt", "").strip()
    if not prompt:
        return JSONResponse({"error": "prompt is required"}, status_code=400)

    chat = pcm.get_chat(chat_id)
    if chat is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if chat.archived:
        # Reject before starting a doomed stream: stream_chat would raise
        # "Cannot send messages to an archived chat" from the background
        # task, producing a server traceback + a raw error bubble. Same
        # guard the loop dispatcher uses (issue #126).
        return JSONResponse(
            {"error": "chat is archived", "archived": True}, status_code=409
        )

    images: list[ImageAttachment] = []
    for ref in body.get("images", []):
        attachment = pcm.resolve_image_ref(ref)
        if attachment:
            images.append(attachment)

    try:
        pcm.start_stream(chat_id, prompt, images=images or None)
    except Exception as exc:
        logger.exception("Failed to start stream for %s", chat_id)
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse({"ok": True, "chat_id": chat_id})


async def chat_mark_read(request: Request) -> JSONResponse:
    """Mark a chat as read on the server. Emits a chat_read event so other
    tabs/devices clear their unread state, and cancels any pending delayed
    push for this chat.
    """
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    chat = pcm.mark_read(chat_id)
    if chat is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"ok": True, "last_read_at": chat.last_read_at})


async def chats_mark_all_read(request: Request) -> JSONResponse:
    """Mark every unread, non-archived chat as read. Returns the affected ids."""
    pcm = request.app.state.project_chat_manager
    touched = pcm.mark_all_read()
    return JSONResponse({"ok": True, "chat_ids": touched})


async def chat_archive(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    # Capture chat/project metadata BEFORE archive_chat() mutates the chat
    # (it flips ``archived=True`` but leaves project_id intact; pull project
    # info too so the trajectory record carries workspace + context).
    chat_meta = pcm.get_chat(chat_id)
    project_meta = (
        pcm.get_project(chat_meta.project_id) if chat_meta is not None else None
    )
    result = await pcm.archive_chat(chat_id)
    outcome = result.outcome if result is not None else None
    if outcome is not None:
        pcm.run_archive_postprocess(chat_id, outcome, chat_meta, project_meta)
    # Report the cascade per subchat rather than a bare ok. The client marks
    # only what `archived_chat_ids` confirms — a delegate the server skipped is
    # still live, and hiding it from the sidebar while it streams and spends
    # tokens is worse than leaving the row visible. `stopped_chat_ids` is what
    # the user is warned about; `failed_chat_ids` are the subchats they may
    # still need to deal with by hand.
    delegates = result.delegates if result is not None else []
    return JSONResponse({
        "ok": True,
        "archived_to": str(outcome.path) if outcome is not None else None,
        # A chat with an empty transcript yields no ArchiveOutcome but is still
        # archived, so this is keyed off the cascade running at all.
        "archived_chat_ids": (
            ([chat_id] + result.archived_ids()) if result is not None else []
        ),
        # The initiating client clears the active pane as soon as this response
        # arrives. Return the lifecycle record as well as publishing it over
        # /ws/events, so that client cannot miss the first "running" state in
        # the archive/event race.
        "postprocess": (
            dict(chat_meta.postprocess)
            if chat_meta and chat_meta.postprocess
            else None
        ),
        "stopped_chat_ids": result.stopped_ids() if result is not None else [],
        "failed_chat_ids": result.failed_ids() if result is not None else [],
        "subchats": [row.to_dict() for row in delegates],
    })


async def chat_retry_insights(request: Request) -> JSONResponse:
    """Re-run session-insights extraction for a single archived chat.

    The retry works in text mode against the rendered archive (the raw session
    JSONL is reclaimed at archive time). Returns a job status; a pipeline that
    is already running for the chat is left alone.
    """
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    status = pcm.retry_insights(chat_id)
    if status == "not_found":
        return JSONResponse({"error": "not found"}, status_code=404)
    if status == "not_archived":
        return JSONResponse(
            {"error": "chat is not archived", "chat_id": chat_id}, status_code=409
        )
    if status == "no_archive":
        return JSONResponse(
            {"error": "no archive file for this chat", "chat_id": chat_id}, status_code=409
        )
    if status == "already_has":
        return JSONResponse({"status": "already_has", "chat_id": chat_id}, status_code=200)
    if status == "running":
        return JSONResponse({"status": "running", "chat_id": chat_id}, status_code=202)
    return JSONResponse({"status": "started", "chat_id": chat_id}, status_code=202)


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


def _codex_content_text(raw: object) -> str:
    """Extract text from a Codex app-server user-message content array."""
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, list):
        return ""
    parts: list[str] = []
    for block in raw:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and str(block.get("type") or "") in {
            "text",
            "inputText",
        }:
            parts.append(str(block.get("text") or ""))
    return "\n".join(part for part in parts if part)


def _strip_codex_command_expansion(content: str) -> str:
    if not content.startswith("[CIAO_COMMAND_BEGIN]\n"):
        return content
    for line in content.splitlines()[1:4]:
        if not line.startswith("user_input_json="):
            continue
        try:
            original = json.loads(line.split("=", 1)[1])
        except (json.JSONDecodeError, ValueError):
            return content
        return str(original) if isinstance(original, str) else content
    return content


def _render_codex_thread(thread: dict, chat) -> list[dict]:
    """Render Codex thread items into the provider-neutral PWA row shape."""
    result: list[dict] = []
    turns = thread.get("turns")
    if not isinstance(turns, list):
        turns = []
    user_idx = 0
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        items = turn.get("items")
        if not isinstance(items, list):
            continue
        agent_message_items = [
            item
            for item in items
            if isinstance(item, dict)
            and item.get("type") == "agentMessage"
            and str(item.get("text") or "").strip()
        ]
        has_final_answer = any(
            isinstance(item, dict)
            and item.get("type") == "agentMessage"
            and str(item.get("phase") or "") == "final_answer"
            and str(item.get("text") or "").strip()
            for item in items
        )
        fallback_agent_message_id = ""
        commentary_only = bool(agent_message_items) and all(
            str(item.get("phase") or "") == "commentary"
            for item in agent_message_items
        )
        if (
            str(turn.get("status") or "") == "completed"
            and not has_final_answer
            and commentary_only
        ):
            # A completed Codex turn can contain only a substantive commentary
            # item. Match the live provider fallback so reopening the chat does
            # not fold the completed response back into Activity.
            for item in reversed(items):
                if (
                    isinstance(item, dict)
                    and item.get("type") == "agentMessage"
                    and str(item.get("text") or "").strip()
                ):
                    fallback_agent_message_id = str(item.get("id") or "")
                    break
        pending_tools: list[str] = []

        def flush_tools() -> None:
            if pending_tools:
                result.append({
                    "role": "system",
                    "content": "\n".join(pending_tools),
                    "tool_name": "_activity",
                })
                pending_tools.clear()

        for item in items:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "")
            if kind == "userMessage":
                flush_tools()
                content = _strip_injected_context(
                    _codex_content_text(item.get("content"))
                ).strip()
                content = _strip_codex_command_expansion(content).strip()
                if not content:
                    continue
                entry: dict = {
                    "role": "user",
                    "content": content,
                    "turn_index": user_idx,
                }
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
            if kind == "agentMessage":
                flush_tools()
                text = str(item.get("text") or "").strip()
                if text:
                    entry = {"role": "assistant", "content": text}
                    phase = str(item.get("phase") or "")
                    if (
                        fallback_agent_message_id
                        and str(item.get("id") or "") == fallback_agent_message_id
                    ):
                        phase = "final_answer"
                    if phase in {"commentary", "final_answer"}:
                        entry["phase"] = phase
                    result.append(entry)
                continue
            if kind == "fileChange":
                flush_tools()
                changes = item.get("changes")
                for change in changes if isinstance(changes, list) else []:
                    if not isinstance(change, dict):
                        continue
                    file_path = str(change.get("path") or "")
                    if file_path:
                        kind_name = str(change.get("kind") or "update").lower()
                        action = (
                            "created"
                            if kind_name in {"add", "create"}
                            else "edited"
                        )
                        result.append({
                            "role": "system",
                            "tool_name": "_filecard",
                            "content": file_path,
                            "file_path": file_path,
                            "action": action,
                            "tool": "Write" if action == "created" else "Edit",
                        })
                continue
            if kind == "commandExecution":
                command = item.get("command")
                if isinstance(command, list):
                    label = " ".join(str(part) for part in command)
                else:
                    label = str(command or "")
                touches = extract_file_touches("Bash", {"command": label})
                if touches:
                    flush_tools()
                    for touch in touches:
                        result.append({
                            "role": "system",
                            "tool_name": "_filecard",
                            "content": touch["file_path"],
                            "file_path": touch["file_path"],
                            "action": touch.get("action") or "touched",
                            "tool": "Bash",
                        })
                else:
                    pending_tools.append(
                        f"{_tool_icon('Bash')} Bash {label}".strip()
                    )
                continue
            if kind in {"mcpToolCall", "dynamicToolCall"}:
                name = str(item.get("tool") or item.get("name") or kind)
                server = str(item.get("server") or "")
                label = f"{server}/{name}" if server else name
                pending_tools.append(f"{_tool_icon(name)} {label}")
                continue
            if kind == "collabAgentToolCall":
                status = str(item.get("status") or "")
                prompt = str(item.get("prompt") or "").strip()
                detail = f" {prompt[:180]}" if prompt else ""
                pending_tools.append(
                    f"{_tool_icon('Task')} Agent {status}{detail}".strip()
                )
        flush_tools()
    _overlay_assistant_timings(result, chat.user_turn_timings)
    return result


def _render_opencode_thread(thread: dict, chat, *, metadata: bool = True) -> list[dict]:
    """Render opencode session messages into the provider-neutral PWA row shape.

    ``thread`` is :meth:`OpencodeProvider.read_thread`'s ``{"info", "messages"}``
    payload; each message is ``{"info": {role, ...}, "parts": [...]}``.

    ``metadata`` overlays ``chat``'s per-turn images/timings/unattended flags
    onto the rows. Pass ``False`` when ``thread`` is a *child* session: its
    turn numbering restarts at 0, so the parent chat's turn metadata does not
    apply to it.
    """
    messages = thread.get("messages")
    if not isinstance(messages, list):
        messages = []
    result: list[dict] = []
    user_idx = 0
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
    parsed = _normalize_handover_messages(parsed)
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


async def chat_messages(request: Request) -> JSONResponse:
    """Return conversation history for a chat.

    Claude chats read the SDK session file via ``get_session_messages``.
    Codex chats read the app-server thread via ``thread/read``; opencode chats
    read the session history from a short-lived ``opencode serve``. Both fall
    back to the durable ``.runtime`` transcript when the provider-side session
    is unreadable.

    When a chat is archived, provider-side session storage is deleted to reclaim
    disk space (Claude SDK blob, Codex thread). In that case we fall back to the
    durable markdown transcript in the vault so the PWA can still render the
    conversation read-only.
    """
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    chat = pcm.get_chat(chat_id)
    if chat is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    handover_messages = list(getattr(chat, "handover_messages", []) or [])
    if not chat.session_id:
        # A provider may fail before creating its session (for example while
        # opencode is starting). The durable transcript still contains the
        # user turn and the persisted error, so do not hide it behind the
        # session-less handover fast path.
        current = pcm._transcripts.current_messages(
            ChatContext.for_web(chat_id), getattr(chat, "provider", "claude")
        )
        return JSONResponse(handover_messages + current)

    config = request.app.state.config
    provider = getattr(chat, "provider", "claude")
    # Every provider stores its sessions per-cwd, and a chat's cwd is its agent
    # root. Reading with the install root found nothing for any chat created since
    # the re-rooting — for Claude that rendered an empty conversation, and codex
    # and opencode were reading from the wrong root for the same reason.
    _resolver = getattr(pcm, "_agent_root_for_chat", None)
    session_root = Path(
        _resolver(chat_id) if _resolver is not None else config.workspace_root
    )
    if provider in ("codex", "opencode"):
        if getattr(chat, "archived", False):
            # An archived chat is read-only and its provider-side session may
            # be gone; serve the vault markdown without paying a provider
            # session read (for opencode, a throwaway server spawn) first.
            archived = _messages_from_archived_transcript(pcm, config, chat)
            if archived is not None:
                return JSONResponse(handover_messages + archived)
        rendered: list[dict] = []
        if provider == "codex":
            thread = await CodexProvider.read_thread(session_root, chat.session_id)
            if thread is not None:
                rendered = _render_codex_thread(thread, chat)
        else:
            opencode_thread = await OpencodeProvider.read_thread(
                session_root, chat.session_id
            )
            if opencode_thread:
                rendered = _render_opencode_thread(opencode_thread, chat)
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
            return JSONResponse(handover_messages + rendered)
        if current:
            _overlay_assistant_timings(current, chat.user_turn_timings)
            return JSONResponse(handover_messages + current)
        archived = _messages_from_archived_transcript(pcm, config, chat)
        if archived is not None:
            return JSONResponse(handover_messages + archived)
        return JSONResponse(handover_messages)

    from ciao.transcripts import get_session_messages_full

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
            return JSONResponse(handover_messages + archived)
        return JSONResponse(handover_messages)

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
            task_summary = _summarize_task_notification(content)
            if task_summary is not None:
                result.append({"role": "system", "content": task_summary})
                continue
            if _is_cli_internal_envelope(content):
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
    _overlay_assistant_timings(result, chat.user_turn_timings)
    return JSONResponse(handover_messages + result)


async def chat_subagents(request: Request) -> JSONResponse:
    """Return subagent activity for this chat's session, if any.

    Uses the SDK helpers added in ``claude-agent-sdk`` v0.1.60:
    ``list_subagents`` to discover subagent ids, and ``get_subagent_messages``
    to fetch each one's transcript. Returns an array shaped like:

    ``[{"agent_id": str, "messages": [...same shape as /messages...]}]``

    Each entry additionally carries dispatch metadata parsed from the parent
    session JSONL when available (see ciao/subagent_tracking.py):
    ``tool_use_id``, ``description``, ``subagent_type``, ``is_async``,
    ``status`` ("running"/"completed"/"failed"), and ``turn_index`` — the
    user turn that dispatched the agent, aligned with the ``turn_index``
    stamped on user bubbles by /messages so the PWA can anchor the subagent
    panel to the right turn.

    Empty array when the chat has no session, no subagents were spawned, or
    the SDK can't find the session on this machine (e.g. a remote chat that
    hasn't been pulled locally).
    """
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    chat = pcm.get_chat(chat_id)
    if chat is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not chat.session_id:
        return JSONResponse([])

    config = request.app.state.config
    if getattr(chat, "provider", "claude") == "codex":
        parent = await CodexProvider.read_thread(
            config.workspace_root, chat.session_id
        )
        if parent is None:
            return JSONResponse([])
        entries: list[dict] = []
        for item in await CodexProvider.read_collab_tree(
            config.workspace_root, parent
        ):
            thread = item.get("thread")
            if not isinstance(thread, dict):
                continue
            agent_id = str(item["agent_id"])
            raw_status = str(item.get("status") or "")
            if raw_status in {"pendingInit", "running"}:
                status = "running"
            elif raw_status in {"errored", "interrupted", "notFound"}:
                status = "failed"
            else:
                status = "completed"
            entries.append({
                "agent_id": agent_id,
                "parent_agent_id": str(item.get("parent_agent_id") or ""),
                "messages": _render_codex_thread(thread, chat),
                "tool_use_id": str(item.get("tool_use_id") or ""),
                "description": str(item.get("description") or ""),
                "subagent_type": "codex",
                "is_async": True,
                "status": status,
                "turn_index": int(item.get("root_turn_index") or 0),
            })
        return JSONResponse(entries)

    if getattr(chat, "provider", "claude") == "opencode":
        opencode_entries: list[dict] = []
        provider_service = pcm._providers.get(chat_id)
        live_provider = provider_service.provider if provider_service is not None else None
        if (
            isinstance(live_provider, OpencodeProvider)
            and live_provider.has_live_server
            and live_provider.current_session_id == chat.session_id
        ):
            collab_tree = await live_provider.read_live_collab_tree()
        else:
            collab_tree = await OpencodeProvider.read_collab_tree(
                config.workspace_root, chat.session_id
            )
        for item in collab_tree:
            info = item.get("info")
            info = info if isinstance(info, dict) else {}
            agent_id = str(info.get("id") or "")
            if not agent_id:
                continue
            messages = item.get("messages")
            messages = messages if isinstance(messages, list) else []
            opencode_entries.append({
                "agent_id": agent_id,
                "parent_agent_id": str(info.get("parentID") or ""),
                "messages": _render_opencode_thread(item, chat, metadata=False),
                "tool_use_id": "",
                "description": str(info.get("title") or ""),
                "subagent_type": "opencode",
                "is_async": True,
                # opencode's session objects carry no status field, but this
                # endpoint is polled every few seconds while a turn streams —
                # derive the lifecycle from the child's own messages and anchor
                # it to the parent turn sent before the child was created.
                "status": _opencode_child_status(messages),
                "turn_index": _opencode_child_turn_index(info, chat),
            })
        return JSONResponse(opencode_entries)

    workspace = str(config.workspace_root)
    resolver = getattr(pcm, "_agent_root_for_chat", None)
    agent_root = resolver(chat_id) if resolver is not None else None

    def _finalize(entries: list[dict]) -> JSONResponse:
        _merge_subagent_dispatch_meta(
            entries, chat.session_id, Path(config.workspace_root), agent_root=agent_root
        )
        return JSONResponse(entries)

    try:
        from claude_agent_sdk import get_subagent_messages, list_subagents
    except ImportError:
        return _finalize(
            _local_subagent_transcripts(
                chat.session_id, Path(config.workspace_root), agent_root=agent_root
            )
        )

    try:
        agent_ids = list_subagents(chat.session_id, directory=workspace)
    except (FileNotFoundError, ValueError):
        return _finalize(
            _local_subagent_transcripts(
                chat.session_id, Path(config.workspace_root), agent_root=agent_root
            )
        )
    except Exception:  # noqa: BLE001 — defensive against SDK surprises
        return _finalize(
            _local_subagent_transcripts(
                chat.session_id, Path(config.workspace_root), agent_root=agent_root
            )
        )

    result: list[dict] = []
    for agent_id in agent_ids:
        try:
            msgs = get_subagent_messages(
                chat.session_id,
                agent_id,
                directory=workspace,
            )
        except (FileNotFoundError, ValueError):
            continue
        except Exception:  # noqa: BLE001 — defensive
            continue

        rendered = _render_subagent_messages(msgs)
        result.append({"agent_id": agent_id, "messages": rendered})

    if not result:
        result = _local_subagent_transcripts(
            chat.session_id, Path(config.workspace_root), agent_root=agent_root
        )

    return _finalize(result)


def _merge_subagent_dispatch_meta(
    entries: list[dict], session_id: str, workspace_root: Path, *, agent_root: Path | None = None
) -> None:
    """Attach dispatch metadata from the parent session JSONL in place."""
    if not entries:
        return
    path = subagent_tracking.find_parent_session_file(
        session_id, workspace_root, agent_root=agent_root
    )
    if path is None:
        return
    try:
        state = subagent_tracking.parse_session_subagents(path)
    except Exception:  # noqa: BLE001 — metadata is best-effort decoration
        logger.exception("subagent dispatch-meta parse failed for %s", session_id)
        return
    for entry in entries:
        # SDK ids are bare ("a319..."); the local-JSONL fallback uses the
        # file stem ("agent-a319...").
        agent_id = str(entry.get("agent_id", "")).removeprefix("agent-")
        info = state.subagents.get(agent_id)
        if info is None:
            continue
        entry["tool_use_id"] = info.tool_use_id
        entry["description"] = info.description
        entry["subagent_type"] = info.subagent_type
        entry["is_async"] = info.is_async
        entry["status"] = info.status
        if info.turn_index is not None:
            entry["turn_index"] = info.turn_index


# ── Voice ────────────────────────────────────────────────────────────────

async def chat_voice(request: Request) -> JSONResponse:
    """Upload and transcribe a voice file."""
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    chat = pcm.get_chat(chat_id)
    if chat is None:
        return JSONResponse({"error": "chat not found"}, status_code=404)

    form = await request.form()
    upload = form.get("audio")
    if upload is None:
        return JSONResponse({"error": "no audio file"}, status_code=400)

    filename = getattr(upload, "filename", "audio.webm") or "audio.webm"

    try:
        data = await _read_upload_limited(
            upload, request.app.state.config.max_voice_size_bytes
        )
        path = pcm.save_voice_upload(data, filename)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    try:
        text, cost = await pcm.transcribe_voice(path)
    except ValueError as exc:
        path.unlink(missing_ok=True)
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        path.unlink(missing_ok=True)
        return JSONResponse({"error": f"Transcription failed: {exc}"}, status_code=500)

    path.unlink(missing_ok=True)

    return JSONResponse({
        "text": text,
        "cost": round(cost, 6),
    })


async def chat_speak(request: Request) -> Response:
    """Synthesize speech for a message; returns the audio bytes directly."""
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    chat = pcm.get_chat(chat_id)
    if chat is None:
        return JSONResponse({"error": "chat not found"}, status_code=404)

    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    text = (body.get("text") or "").strip() if isinstance(body, dict) else ""
    if not text:
        return JSONResponse({"error": "no text to speak"}, status_code=400)

    try:
        audio, mime, cost = await pcm.synthesize_speech(text)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": f"Speech synthesis failed: {exc}"}, status_code=500)

    return Response(
        audio,
        media_type=mime,
        headers={"X-TTS-Cost": f"{cost:.6f}", "Cache-Control": "no-store"},
    )


async def chat_reentry_summary(request: Request) -> JSONResponse:
    """Return an ephemeral Apple Intelligence summary for a reopened chat."""
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    chat = pcm.get_chat(chat_id)
    if chat is None:
        return JSONResponse({"error": "chat not found"}, status_code=404)
    if chat.archived:
        return JSONResponse({"available": True, "summary": ""})

    try:
        summary = await pcm.generate_reentry_summary(chat_id)
    except Exception as exc:  # noqa: BLE001 — orientation aid must never block chat use
        logger.info("Re-entry summary failed for %s: %s", chat_id, exc)
        return JSONResponse({"available": False, "summary": ""})
    return JSONResponse({"available": bool(summary), "summary": summary})


# ── Images ───────────────────────────────────────────────────────────────

async def chat_images(request: Request) -> JSONResponse:
    """Upload images and return references."""
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    chat = pcm.get_chat(chat_id)
    if chat is None:
        return JSONResponse({"error": "chat not found"}, status_code=404)

    form = await request.form()
    results = []
    for key in form:
        upload = form[key]
        if not hasattr(upload, "read"):
            continue
        filename = getattr(upload, "filename", "image.jpg") or "image.jpg"
        try:
            data = await _read_upload_limited(
                upload, request.app.state.config.max_image_size_bytes
            )
            attachment = pcm.save_image_upload(data, filename)
            results.append({
                "ref": attachment.path.name,
                "mime_type": attachment.mime_type,
                "filename": attachment.original_filename,
            })
        except ValueError as exc:
            results.append({"error": str(exc), "filename": filename})

    return JSONResponse(results)


async def image_blob(request: Request) -> Response:
    """Serve an uploaded image file by its ref (filename under media_root)."""
    pcm = request.app.state.project_chat_manager
    ref = request.path_params["ref"]
    attachment = pcm.resolve_image_ref(ref)
    if attachment is None:
        return Response(status_code=404)
    return FileResponse(attachment.path, media_type=attachment.mime_type)


# Extensions the workspace-file viewer is allowed to serve. Keep this
# conservative: the PWA viewer is a read-only inspector, not a generic file
# server, and binary/media types are served by other dedicated endpoints.
_WORKSPACE_FILE_EXTS = frozenset({
    ".md", ".markdown", ".txt",
    ".py", ".ts", ".tsx", ".js", ".jsx", ".vue",
    ".css", ".html", ".json",
    ".yaml", ".yml", ".toml",
    ".sh", ".rs", ".go", ".java", ".xml", ".sql",
    ".cfg", ".ini", ".log", ".csv", ".excalidraw",
})
# Intentionally excluded: .env, .example — these commonly hold secrets or
# sample secrets. The viewer is a read-only inspector and should not serve
# them even though they are under workspace_root.
_WORKSPACE_FILE_MAX_BYTES = 2 * 1024 * 1024  # 2 MB
_LINE_SUFFIX_RE = re.compile(r":\d+$")

# Images embedded in vault markdown docs (e.g. `![](images/foo.png)`) are
# served by a dedicated endpoint so the text viewer stays strictly text.
# MIME types are derived from the extension whitelist below.
_WORKSPACE_IMAGE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif", ".bmp", ".ico",
})
# Larger cap than the text viewer: screenshots and dashboard captures are
# commonly a few MB. Still bounded so a pathological request can't stream a
# gigabyte.
_WORKSPACE_IMAGE_MAX_BYTES = 15 * 1024 * 1024  # 15 MB




async def workspace_file(request: Request) -> Response:
    """Serve a read-only allowlisted text file from the host filesystem.

    Path is provided as a query string (`?path=...`). The path may be
    workspace-relative or absolute, with an optional `:line` suffix that is
    stripped. All results canonicalise via ``Path.resolve()``. There is no
    workspace sandbox: any allowlisted-extension file on disk is served.
    Relative paths anchor to ``config.workspace_root``.
    """
    config = request.app.state.config
    raw = request.query_params.get("path", "").strip()
    roots = _allowed_roots(config)
    result = _resolve_workspace_path(roots, raw, allow_fuzzy=True)
    if isinstance(result, Response):
        return result
    resolved = result

    if resolved.suffix.lower() not in _WORKSPACE_FILE_EXTS:
        return JSONResponse({"error": "unsupported type"}, status_code=415)
    if resolved.stat().st_size > _WORKSPACE_FILE_MAX_BYTES:
        return JSONResponse({"error": "file too large"}, status_code=413)

    # Force revalidation on every load. Without this, browsers fall back to
    # heuristic freshness (~10% of file age since Last-Modified). Two consequences
    # that bit us in practice:
    #   1. Different callers can encode the same file under different paths
    #      (workspace-relative vs absolute), giving each its own cache entry.
    #      A stale entry then sticks around even after the file has been edited.
    #   2. Markdown previews kept showing pre-edit content for minutes/hours.
    # ETag + Last-Modified are still emitted by FileResponse, so a 304 path
    # remains available; we only change *whether* the browser asks.
    return FileResponse(
        resolved,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


# ── HTML artifacts ───────────────────────────────────────────────────────
# An artifact is a self-contained page the model authored (a dashboard, an
# annotated diff, a comparison) that the PWA embeds in the pinned panel.
# It is served from this host, so it needs a policy of its own.

_WORKSPACE_HTML_EXTS = frozenset({".html", ".htm"})

# Read this before "hardening" it: `script-src 'unsafe-inline'` is load-bearing.
# An artifact inlines its own <script> and <style> — that is the entire point of
# a single self-contained file — so removing 'unsafe-inline' does not tighten
# this policy, it breaks every artifact and leaves a blank frame with a console
# error the user never sees.
#
# Containment comes from the other directives, not from script-src:
#   - `sandbox allow-scripts` (no allow-same-origin) puts the document in an
#     opaque origin. It cannot read the session cookie, localStorage, or the
#     embedding page, even though it is served from the same host.
#   - `connect-src 'none'` kills fetch, XHR, WebSocket and EventSource.
#   - `img-src data:` (no http/https) closes the beacon-through-an-image-URL
#     exfiltration path that a permissive img-src leaves open.
#   - `form-action 'none'` and `base-uri 'none'` stop navigation-based leaks.
# The net effect: an artifact can render and respond to clicks, and has no way
# to reach /api/*, phone home, or read anything of the user's.
#
# `allow-popups` and `blob:` sources remain absent. Self-contained audio/video
# artifacts use data URLs, so media access is allowed only for embedded data.
_ARTIFACT_CSP = "; ".join(
    [
        "default-src 'none'",
        "script-src 'unsafe-inline'",
        "style-src 'unsafe-inline'",
        "img-src data:",
        "media-src data:",
        "font-src data:",
        "connect-src 'none'",
        "form-action 'none'",
        "base-uri 'none'",
        "frame-ancestors 'self'",
        "sandbox allow-scripts",
    ]
)

# CSP for host-rendered binary previews (PDF, and PPTX after conversion).
# Looser than _ARTIFACT_CSP because the renderer here is the browser's own
# viewer loading our assets, not model-authored markup.
_EMBEDDED_PREVIEW_CSP = "; ".join(
    [
        "default-src 'self'",
        "base-uri 'self'",
        "object-src 'none'",
        "frame-ancestors 'self'",
        "form-action 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        "img-src 'self' data: blob:",
        "media-src 'self' blob:",
        "font-src 'self' data: https://fonts.gstatic.com",
        "connect-src 'self' ws: wss: https://fonts.googleapis.com https://fonts.gstatic.com",
    ]
)


async def workspace_html(request: Request) -> Response:
    """Serve a workspace ``.html`` file as a renderable page, not as source.

    ``/api/workspace-file`` already serves ``.html`` but as ``text/plain``,
    which is what the panel's Code view wants. This endpoint is the Preview
    side: same file, ``text/html``, under ``_ARTIFACT_CSP``. See that constant
    for why the policy is shaped the way it is.

    Why a real endpoint instead of an ``srcdoc`` iframe: ``srcdoc`` and
    ``blob:`` documents inherit the *embedder's* CSP, which is ``script-src
    'self'`` (``ciao/web/security.py``). Inline artifact script would be
    silently blocked. A frame loaded from a URL gets its own CSP from these
    response headers instead.

    The size cap is deliberately the same 2 MB as the text viewer and the
    snapshot store, so there is no state where a file renders but has no
    history, or has history but refuses to render. Over the cap the panel
    shows a 413 and the user opens the file in a real browser instead
    (``/api/workspace-open``).

    Fuzzy resolution is kept for parity with the Code view, so the same path
    string that shows the source also renders the page. Note that this means
    fuzzy matching decides which document gets to execute script; the sandbox
    above is what keeps that from mattering.
    """
    config = request.app.state.config
    raw = request.query_params.get("path", "").strip()
    roots = _allowed_roots(config)
    result = _resolve_workspace_path(roots, raw, allow_fuzzy=True)
    if isinstance(result, Response):
        return result
    resolved = result

    if resolved.suffix.lower() not in _WORKSPACE_HTML_EXTS:
        return JSONResponse({"error": "unsupported type"}, status_code=415)
    if resolved.stat().st_size > _WORKSPACE_FILE_MAX_BYTES:
        return JSONResponse({"error": "file too large"}, status_code=413)

    return FileResponse(
        resolved,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Security-Policy": _ARTIFACT_CSP,
            # SecurityHeadersMiddleware sets X-Frame-Options: DENY through
            # setdefault, so an explicit value here wins and lets the PWA
            # embed the frame. frame-ancestors above is the modern equivalent;
            # both are sent because the desktop shell's webview is older.
            "X-Frame-Options": "SAMEORIGIN",
            # Same revalidation reasoning as workspace_file: the panel reloads
            # the frame after the model revises an artifact, and a heuristically
            # fresh cache entry would show the pre-edit page.
            "Cache-Control": "no-cache",
        },
    )


_VAULT_MD_EXCLUDE_DIRS = frozenset({"Logs", "Templates", ".obsidian"})


async def vault_markdown_paths(request: Request) -> JSONResponse:
    """Return workspace-relative paths to markdown files for link resolution."""
    config = request.app.state.config
    workspace = config.workspace_root.resolve()
    paths: list[str] = []
    seen: set[str] = set()
    for root in _allowed_roots(config):
        if not root.is_dir():
            continue
        for md_path in root.rglob("*.md"):
            try:
                rel = md_path.relative_to(root)
            except ValueError:
                continue
            if any(part.startswith(".") for part in rel.parts):
                continue
            if any(part in _VAULT_MD_EXCLUDE_DIRS for part in rel.parts):
                continue
            try:
                resolved = md_path.resolve()
            except OSError:
                continue
            try:
                display = str(resolved.relative_to(workspace))
            except ValueError:
                display = str(resolved)
            if display in seen:
                continue
            seen.add(display)
            paths.append(display)
        for md_path in root.rglob("*.markdown"):
            try:
                rel = md_path.relative_to(root)
            except ValueError:
                continue
            if any(part.startswith(".") for part in rel.parts):
                continue
            if any(part in _VAULT_MD_EXCLUDE_DIRS for part in rel.parts):
                continue
            try:
                resolved = md_path.resolve()
            except OSError:
                continue
            try:
                display = str(resolved.relative_to(workspace))
            except ValueError:
                display = str(resolved)
            if display in seen:
                continue
            seen.add(display)
            paths.append(display)
    paths.sort()
    return JSONResponse({"paths": paths})


_BACKLINKS_LIMIT = 30


def _without_markdown_extension(path: str) -> str:
    return re.sub(r"\.(?:md|markdown)$", "", path, flags=re.IGNORECASE)


def _normalize_vault_link_ref(ref: str) -> str:
    normalized = ref.strip().replace("\\", "/")
    if normalized.startswith("memory-vault/"):
        normalized = normalized[len("memory-vault/"):]
    return _without_markdown_extension(normalized)


def _add_backlink_index_entry(
    index: dict[str, list[str]],
    key: str,
    path: str,
) -> None:
    if not key:
        return
    matches = index.setdefault(key, [])
    if path not in matches:
        matches.append(path)


def _build_backlink_index(paths: Iterable[str]) -> dict[str, list[str]]:
    """Build the same path/stem lookup keys as the frontend vault-link index."""
    index: dict[str, list[str]] = {}
    for path in paths:
        no_ext = _without_markdown_extension(path)
        _add_backlink_index_entry(index, no_ext, path)
        _add_backlink_index_entry(index, posixpath.basename(no_ext), path)
        marker = "memory-vault/"
        marker_index = no_ext.find(marker)
        if marker_index >= 0:
            _add_backlink_index_entry(
                index,
                no_ext[marker_index + len(marker):],
                path,
            )
    return index


def _resolve_backlink_target(
    ref: str,
    current_path: str,
    index: dict[str, list[str]],
    path_set: set[str],
) -> str | None:
    """Resolve one link ref using the frontend's relative/path/stem rules.

    ``ref`` comes from ``vault_lint._links_in`` and is already note-relative and
    extension-less (`./People/Mo`), so the relative candidates below are the
    exact target in the common case. The path/stem fallbacks still matter: a
    note under ``Logs/`` cites vault-root-relative paths (see
    ``ciao/insights.py``), and those only resolve through the index.
    """
    normalized = _normalize_vault_link_ref(ref)
    if not normalized:
        return None

    current_dir = posixpath.dirname(current_path)
    relative_candidates = [
        posixpath.normpath(posixpath.join(current_dir, f"{normalized}.md")),
        posixpath.normpath(posixpath.join(current_dir, f"{normalized}.markdown")),
    ]
    for candidate in relative_candidates:
        if candidate in path_set:
            return candidate

    direct = index.get(normalized, [])
    if len(direct) == 1:
        return direct[0]
    if len(direct) > 1:
        relative_pick = next(
            (path for path in direct if path in relative_candidates),
            None,
        )
        if relative_pick is not None:
            return relative_pick
        if "/" in normalized:
            return direct[0]
        return None

    tail = posixpath.basename(normalized)
    stem_matches = index.get(tail, [])
    if len(stem_matches) == 1:
        return stem_matches[0]
    return None


def _references_note(
    content: str,
    current_path: str,
    target_path: str,
    index: dict[str, list[str]],
    path_set: set[str],
) -> bool:
    """True if ``content`` has a markdown link resolving to ``target_path``.

    Reuses ``vault_lint._links_in`` so links documented inside code fences/spans
    or escaped (``\\[label](x.md)``) don't count, and so a backlink and a
    broken-link finding can never disagree about what a link is. Resolution
    mirrors the frontend so two notes with the same filename stem do not share
    false backlinks.
    """
    for ref in _links_in(content):
        if _resolve_backlink_target(ref, current_path, index, path_set) == target_path:
            return True
    return False


async def vault_backlinks(request: Request) -> JSONResponse:
    """Return notes that link to the given markdown path (incoming links)."""
    target_path = request.query_params.get("path", "").strip()
    if not target_path:
        return JSONResponse({"backlinks": []})
    config = request.app.state.config
    workspace_root = config.workspace_root.resolve()
    candidates: list[tuple[Path, str]] = []
    seen_paths: set[str] = set()

    for root in _allowed_roots(config):
        if not root.is_dir():
            continue
        for pattern in ("*.md", "*.markdown"):
            for md_path in root.rglob(pattern):
                try:
                    relative_parts = md_path.relative_to(root).parts
                    resolved = md_path.resolve()
                except (OSError, ValueError):
                    continue
                if any(
                    part.startswith(".") or part in EXCLUDE_DIRS
                    for part in relative_parts
                ):
                    continue
                try:
                    display_path = str(resolved.relative_to(workspace_root))
                except ValueError:
                    display_path = str(resolved)
                display_path = display_path.replace("\\", "/")
                if display_path in seen_paths:
                    continue
                seen_paths.add(display_path)
                candidates.append((resolved, display_path))

    candidates.sort(key=lambda item: item[1])
    path_set = {display_path for _path, display_path in candidates}
    clean_target = _LINE_SUFFIX_RE.sub("", target_path).replace("\\", "/")
    resolved_target = next(
        (display for _path, display in candidates if display == clean_target),
        None,
    )
    if resolved_target is None:
        try:
            raw_target = Path(clean_target)
            target_on_disk = (
                raw_target.resolve()
                if raw_target.is_absolute()
                else (workspace_root / raw_target).resolve()
            )
        except (OSError, ValueError):
            return JSONResponse({"backlinks": []})
        resolved_target = next(
            (display for path, display in candidates if path == target_on_disk),
            None,
        )
    if resolved_target is None:
        return JSONResponse({"backlinks": []})

    index = _build_backlink_index(path_set)
    target_stem = Path(resolved_target).stem.casefold()
    backlinks: list[dict[str, str]] = []
    for md_path, display_path in candidates:
        if display_path == resolved_target:
            continue
        try:
            content = md_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # Cheap gate before the code-fence stripping + link parse in _links_in.
        if target_stem not in content.casefold():
            continue

        if _references_note(
            content,
            display_path,
            resolved_target,
            index,
            path_set,
        ):
            backlinks.append({"path": display_path, "title": md_path.stem})
            if len(backlinks) >= _BACKLINKS_LIMIT:
                return JSONResponse({"backlinks": backlinks})
    return JSONResponse({"backlinks": backlinks})


async def vault_graph(request: Request) -> JSONResponse:
    """Return the vault as a note graph for the Memory Map page.

    Nodes are notes with frontmatter (or an inferred type); edges come from
    both frontmatter ``related:``/``relatedTo:`` and body markdown links,
    already merged and resolved to real paths by ``vault_index.scan_vault``.
    Optional ``?workspace=`` scopes to one logical workspace; cross-workspace
    edges are dropped rather than left dangling.
    """
    config = request.app.state.config
    workspace = request.query_params.get("workspace", "").strip() or None
    # Every vault in the install, which is ONE shared vault before the
    # re-rooting and one per agent root after it. Scanning `config.vault_root`
    # returned zero notes on a migrated install, so the whole map went blank.
    # Reads and parses every markdown file, so run it off the event loop or a
    # large vault stalls other requests, including the 5s chat-socket keepalives
    # (see chat_messages above for the same fix).
    entries, absolute = await asyncio.to_thread(
        scan_targets, config.vault_scan_targets()
    )
    workspaces = sorted({e.workspace for e in entries if e.workspace})
    scoped = filter_entries(entries, workspace=workspace) if workspace else entries
    graph = _build_graph(scoped)
    by_path = {str(e.path) for e in scoped}

    # `mtime` lets the Memory Map seed its local view from the note you touched
    # most recently, which is a far more useful entry point than "whatever the
    # biggest hub is". Entry carries no timestamp, so stat the files here; it is
    # one stat per note against files scan_vault has just read anyway.
    def _mtime(rel: str) -> float:
        # Resolved through the scan's own map. Rendered paths are no longer a
        # fixed offset from one vault root, so stripping a `memory-vault/`
        # prefix and joining resolved to nothing on a migrated install and every
        # note reported mtime 0 — which silently broke the map's "most recently
        # touched note" entry point rather than failing loudly.
        target = absolute.get(rel)
        if target is None:
            return 0.0
        try:
            return target.stat().st_mtime
        except OSError:
            # A note indexed but unreadable (race with a delete, broken
            # symlink) must not fail the whole graph request.
            return 0.0

    nodes = [
        {
            "id": str(e.path),
            "title": e.title,
            "type": e.type,
            "tags": e.tags,
            "aliases": e.aliases,
            "description": e.description,
            "workspace": e.workspace,
            "degree": len(graph.get(str(e.path), ())),
            "mtime": _mtime(str(e.path)),
        }
        for e in scoped
    ]
    seen: set[tuple[str, str]] = set()
    edges = []
    for src, targets in graph.items():
        if src not in by_path:
            continue
        for tgt in targets:
            if tgt not in by_path:
                continue
            first, second = sorted((src, tgt))
            key = (first, second)
            if key in seen:
                continue
            seen.add(key)
            edges.append({"source": key[0], "target": key[1]})
    return JSONResponse({
        "workspace": workspace or "all",
        "workspaces": workspaces,
        "nodes": nodes,
        "edges": edges,
    })


async def vault_delete_note(request: Request) -> JSONResponse:
    """Permanently delete one vault note from the Memory Map.

    ``path`` is the same id the graph, backlinks, and file viewer already use
    (the ``Entry.path`` string form, e.g. "memory-vault/work/People/Mo.md").
    Deliberately scoped to ``config.vault_root`` — unlike the workspace-file
    endpoints, this is a permanent, unrecoverable delete, so it does not
    inherit their "any file on disk" reach. Every other note that links to it
    (frontmatter ``related:``/``relatedTo:`` or a body markdown link) is
    rewritten first, so deleting a note never leaves a dangling link in the
    graph or in another note's text.
    """
    config = request.app.state.config
    raw = request.query_params.get("path", "").strip()
    if not raw:
        return JSONResponse({"error": "missing path"}, status_code=400)
    if Path(raw).suffix.lower() not in {".md", ".markdown"}:
        return JSONResponse({"error": "unsupported type"}, status_code=415)

    # The id is a path rendered by the scan, which is `memory-vault/...` on a
    # shared vault and `<root>/memory-vault/...` per agent root. Matching a fixed
    # `memory-vault/` prefix rejected every id on a migrated install, so the
    # Memory Map could not delete anything, and a matching prefix joined to
    # `config.vault_root` would have resolved outside any real vault. Resolving
    # against the scan's own targets keeps the containment check meaningful:
    # exactly one vault can own the note, and it must be under that one.
    vault_root = None
    resolved = None
    for target, _name, prefix in config.vault_scan_targets():
        marker = f"{prefix.as_posix()}/"
        if not raw.startswith(marker):
            continue
        try:
            candidate_root = Path(target).expanduser().resolve()
            candidate = (candidate_root / Path(raw[len(marker):])).resolve()
            candidate.relative_to(candidate_root)
        except (OSError, ValueError):
            continue
        vault_root, resolved = candidate_root, candidate
        break
    if resolved is None or vault_root is None:
        return JSONResponse({"error": "not a vault note"}, status_code=400)
    if not resolved.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)

    edited = await asyncio.to_thread(strip_references, vault_root, raw)
    try:
        await asyncio.to_thread(resolved.unlink)
    except OSError as exc:
        return JSONResponse({"error": f"delete failed: {exc}"}, status_code=500)
    return JSONResponse({"ok": True, "edited_backlinks": edited})


# Binary downloads (PDFs, ZIPs, office docs) live under their own endpoint so
# the text and image viewers stay strictly typed. Same (unrestricted) path
# contract as ``workspace_file``/``workspace_image``: any allowlisted-extension
# file on disk is served, relative paths anchoring to the workspace. The browser
# decides whether to render inline (PDF) or save (everything else) based on
# the inferred MIME type. Saved-page archives are forced to download so their
# packaged HTML cannot execute under the PWA origin.
_WORKSPACE_BINARY_EXTS = frozenset({
    ".pdf", ".zip", ".docx", ".xlsx", ".pptx", ".mht", ".mhtml",
})
_WORKSPACE_BINARY_MAX_BYTES = 50 * 1024 * 1024  # 50 MB


def _find_soffice() -> str | None:
    import shutil
    for cmd in ("soffice", "libreoffice", "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        if shutil.which(cmd) or Path(cmd).exists():
            return cmd
    return None


async def libreoffice_status_endpoint(request: Request) -> JSONResponse:
    """Whether LibreOffice (soffice) is available to render .pptx previews."""
    return JSONResponse({"available": _find_soffice() is not None})


async def libreoffice_install_endpoint(request: Request) -> JSONResponse:
    """Install LibreOffice via Homebrew Cask. No server restart needed —
    workspace_binary probes for soffice fresh on every request."""
    from ciao.upgrade import upgrade_libreoffice

    result = await upgrade_libreoffice()
    if not result.success:
        error = result.stderr.strip() or "Install failed."
        return JSONResponse({"ok": False, "error": error}, status_code=500)
    return JSONResponse({"ok": True, "output": result.stdout})


async def workspace_binary(request: Request) -> Response:
    """Serve an allowlisted binary file from the workspace."""
    config = request.app.state.config
    raw = request.query_params.get("path", "").strip()
    roots = _allowed_roots(config)
    result = _resolve_workspace_path(roots, raw, allow_fuzzy=True)
    if isinstance(result, Response):
        return result
    resolved = result

    if resolved.suffix.lower() not in _WORKSPACE_BINARY_EXTS:
        return JSONResponse({"error": "unsupported type"}, status_code=415)
    if resolved.stat().st_size > _WORKSPACE_BINARY_MAX_BYTES:
        return JSONResponse({"error": "file too large"}, status_code=413)

    source_ext = resolved.suffix.lower()
    is_raw = request.query_params.get("raw") == "1"
    filename = resolved.name
    media_type: str | None = None

    if resolved.suffix.lower() == ".pptx" and not is_raw:
        soffice = _find_soffice()
        if not soffice:
            return JSONResponse(
                {
                    "error": (
                        "LibreOffice is required to preview PowerPoint files in the PWA. "
                        "Please install it (e.g. `brew install --cask libreoffice` on macOS "
                        "or `apt install libreoffice` on Linux) and try again."
                    )
                },
                status_code=500,
            )

        import hashlib
        import shutil
        import tempfile

        cache_dir = Path(config.state_path).parent / "pptx_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        path_hash = hashlib.sha256(str(resolved.resolve()).encode("utf-8")).hexdigest()
        pdf_path = cache_dir / f"{path_hash}.pdf"

        if not pdf_path.exists() or resolved.stat().st_mtime > pdf_path.stat().st_mtime:
            with tempfile.TemporaryDirectory() as tmp_dir:
                conversion = await asyncio.to_thread(
                    subprocess.run,
                    [
                        soffice,
                        "--headless",
                        "--convert-to",
                        "pdf",
                        "--outdir",
                        tmp_dir,
                        str(resolved),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if conversion.returncode != 0:
                    return JSONResponse(
                        {"error": f"LibreOffice conversion failed: {conversion.stderr or conversion.stdout}"},
                        status_code=500,
                    )
                generated = Path(tmp_dir) / (resolved.stem + ".pdf")
                if not generated.exists():
                    return JSONResponse(
                        {"error": "LibreOffice did not produce a PDF output."},
                        status_code=500,
                    )
                shutil.move(str(generated), str(pdf_path))

        orig_stem = resolved.stem
        resolved = pdf_path
        media_type = "application/pdf"
        filename = f"{orig_stem}.pdf"
    else:
        media_type, _ = mimetypes.guess_type(resolved.name)
        if media_type is None:
            _FALLBACK_MIMES = {
                ".pdf": "application/pdf",
                ".zip": "application/zip",
                ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            }
            media_type = _FALLBACK_MIMES.get(resolved.suffix.lower(), "application/octet-stream")

    # `inline` lets PDFs preview in a tab; saved-page archives are explicit
    # downloads. Custom frame headers let supported previews embed inside the
    # PWA's same-origin file viewer iframe.
    disposition = "attachment" if source_ext in {".mht", ".mhtml"} else "inline"
    headers = {
        "Content-Disposition": f'{disposition}; filename="{filename}"',
        "X-Frame-Options": "SAMEORIGIN",
        "Content-Security-Policy": _EMBEDDED_PREVIEW_CSP,
    }
    return FileResponse(
        resolved,
        media_type=media_type,
        headers=headers,
    )


async def workspace_image(request: Request) -> Response:
    """Serve a read-only image from disk.

    Same (unrestricted) path contract as ``workspace_file``: any file on disk
    is served, relative paths anchoring to ``config.workspace_root``. Extension
    must be in ``_WORKSPACE_IMAGE_EXTS``; the correct media type is inferred
    from the extension so browsers render it in ``<img>`` tags.

    Used by the markdown viewer to resolve relative image references (e.g.
    ``![alt](images/foo.png)`` inside a vault doc) against the doc's folder.
    """
    config = request.app.state.config
    raw = request.query_params.get("path", "").strip()
    roots = _allowed_roots(config)
    result = _resolve_workspace_path(roots, raw, allow_fuzzy=True)
    if isinstance(result, Response):
        return result
    resolved = result

    if resolved.suffix.lower() not in _WORKSPACE_IMAGE_EXTS:
        return JSONResponse({"error": "unsupported type"}, status_code=415)
    if resolved.stat().st_size > _WORKSPACE_IMAGE_MAX_BYTES:
        return JSONResponse({"error": "file too large"}, status_code=413)

    media_type, _ = mimetypes.guess_type(resolved.name)
    if media_type is None:
        # Fallback: SVGs and a few uncommon types occasionally miss the
        # mimetypes DB depending on platform. Map from the extension.
        _FALLBACK_MIMES = {
            ".svg": "image/svg+xml",
            ".avif": "image/avif",
            ".webp": "image/webp",
        }
        media_type = _FALLBACK_MIMES.get(resolved.suffix.lower(), "application/octet-stream")
    return FileResponse(resolved, media_type=media_type)


def _open_path_with_default_app(path: Path) -> None:
    """Open *path* with the OS default application on the machine running Ciao."""
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=True, timeout=30)
        return
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
        return
    opener = shutil.which("xdg-open")
    if not opener:
        raise OSError("xdg-open is not available on this platform")
    subprocess.run([opener, str(path)], check=True, timeout=30)


async def workspace_open(request: Request) -> Response:
    """Open a file with the OS default application on the machine running Ciao.

    Body: ``{"path": str}``. Uses the same path resolver as the workspace
    viewers (relative paths anchor to workspace_root; fuzzy basename lookup
    is allowed). The open happens server-side, so this only works when the
    PWA is talking to a local Ciao instance.
    """
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "bad json"}, status_code=400)
    raw = str(body.get("path", "")).strip()
    if not raw:
        return JSONResponse({"error": "missing path"}, status_code=400)

    config = request.app.state.config
    roots = _allowed_roots(config)
    result = _resolve_workspace_path(roots, raw, allow_fuzzy=True)
    if isinstance(result, Response):
        return result
    resolved = result

    try:
        await asyncio.to_thread(_open_path_with_default_app, resolved)
    except FileNotFoundError:
        return JSONResponse({"error": "not found"}, status_code=404)
    except subprocess.CalledProcessError as exc:
        return JSONResponse(
            {"error": f"failed to open file (exit {exc.returncode})"},
            status_code=500,
        )
    except OSError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse({"ok": True, "path": str(resolved)})


# ── File snapshots / history ─────────────────────────────────────────────
#
# The PWA renders a History and Diff tab on every file card. These routes
# back those tabs. The capture path lives in ``project_chats.py`` (broker
# event loop hooks ``SnapshotStore.schedule_capture`` on file-touch tool
# calls); these routes are read-only views over the same store.
#
# Path sandboxing: snapshots are keyed by ``chat_id`` and ``file_path`` as
# supplied by the agent — we don't re-validate the path here because the
# store URL-encodes it into a single directory component and lookups are
# purely string-keyed. There's no filesystem traversal possible from the
# store side. Reading a snapshot's blob also stays inside the store, never
# the original path.

def _resolve_chat_for_snapshots(request: Request):
    """Shared lookup: return (pcm, chat, chat_id, file_path) or a Response."""
    pcm = request.app.state.project_chat_manager
    chat_id = request.query_params.get("chat_id", "").strip()
    file_path = request.query_params.get("file_path", "").strip()
    if not chat_id or not file_path:
        return JSONResponse({"error": "missing chat_id or file_path"}, status_code=400)
    chat = pcm.get_chat(chat_id)
    if chat is None:
        return JSONResponse({"error": "chat not found"}, status_code=404)
    return pcm, chat, chat_id, file_path


async def file_history(request: Request) -> Response:
    """List snapshots for ``(chat_id, file_path)``. Newest last."""
    resolved = _resolve_chat_for_snapshots(request)
    if isinstance(resolved, Response):
        return resolved
    pcm, _chat, chat_id, file_path = resolved
    snapshots = pcm.snapshots.list_snapshots(chat_id=chat_id, file_path=file_path)
    return JSONResponse({"snapshots": snapshots})


async def file_content(request: Request) -> Response:
    """Return the content of one snapshot.

    Query: ``chat_id``, ``file_path``, ``seq`` (int). 404 if the snapshot
    doesn't exist. 413 if the snapshot was recorded as truncated (file was
    bigger than ``MAX_SNAPSHOT_BYTES`` at capture time).
    """
    resolved = _resolve_chat_for_snapshots(request)
    if isinstance(resolved, Response):
        return resolved
    pcm, _chat, chat_id, file_path = resolved
    try:
        seq = int(request.query_params.get("seq", "0"))
    except ValueError:
        return JSONResponse({"error": "bad seq"}, status_code=400)
    if seq <= 0:
        return JSONResponse({"error": "bad seq"}, status_code=400)

    result = pcm.snapshots.read_snapshot(
        chat_id=chat_id, file_path=file_path, seq=seq,
    )
    if result is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    content, meta = result
    if meta.get("truncated"):
        return JSONResponse(
            {"error": "snapshot was too large to capture", "meta": meta},
            status_code=413,
        )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return JSONResponse({"error": "binary snapshot, use workspace-binary"}, status_code=415)
    return JSONResponse({"content": text, "meta": meta})


async def file_restore(request: Request) -> Response:
    """Restore a snapshot's content to disk. Writes a new snapshot to mark
    the restore so the history stays append-only and the user can undo by
    restoring the previous version again.

    Body: ``{"chat_id": str, "file_path": str, "seq": int}``.
    Returns: ``{"ok": true, "restored_seq": int, "new_seq": int}``.
    """
    pcm = request.app.state.project_chat_manager
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "bad json"}, status_code=400)
    chat_id = str(body.get("chat_id", "")).strip()
    file_path = str(body.get("file_path", "")).strip()
    try:
        seq = int(body.get("seq", 0))
    except (TypeError, ValueError):
        return JSONResponse({"error": "bad seq"}, status_code=400)
    if not chat_id or not file_path or seq <= 0:
        return JSONResponse({"error": "missing chat_id, file_path, or seq"}, status_code=400)
    if pcm.get_chat(chat_id) is None:
        return JSONResponse({"error": "chat not found"}, status_code=404)

    # Resolve the write target. There is no workspace sandbox: restoration
    # writes wherever the snapshot's recorded path points. Relative paths
    # anchor to the primary workspace root.
    config = request.app.state.config
    roots = _allowed_roots(config)
    try:
        candidate = Path(file_path).expanduser()
        resolved = candidate.resolve() if candidate.is_absolute() else (roots[0] / candidate).resolve()
    except (OSError, ValueError):
        return JSONResponse({"error": "bad path"}, status_code=400)

    snap = pcm.snapshots.read_snapshot(chat_id=chat_id, file_path=file_path, seq=seq)
    if snap is None:
        return JSONResponse({"error": "snapshot not found"}, status_code=404)
    content, meta = snap
    if meta.get("truncated"):
        return JSONResponse({"error": "snapshot was truncated, cannot restore"}, status_code=409)

    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_bytes(content)
    except OSError as exc:
        return JSONResponse({"error": f"write failed: {exc}"}, status_code=500)

    # Capture the restored state as a new snapshot so history stays linear.
    new_meta = await pcm.snapshots.capture(
        chat_id=chat_id, file_path=file_path, action="restored", tool="Restore",
    )
    new_seq = new_meta.seq if new_meta else 0
    return JSONResponse({"ok": True, "restored_seq": seq, "new_seq": new_seq})


async def workspace_file_write(request: Request) -> Response:
    """Write user-edited content back to a workspace file from the in-PWA
    editor (FileViewerModal edit mode). Snapshots the result so the edit is
    auditable alongside agent edits.

    Body: ``{"chat_id": str, "path": str, "content": str}``. ``chat_id``
    determines which chat's history the snapshot lands in; if omitted the
    write still goes through but no snapshot is recorded.
    """
    pcm = request.app.state.project_chat_manager
    config = request.app.state.config
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "bad json"}, status_code=400)
    raw_path = str(body.get("path", "")).strip()
    content = body.get("content", "")
    chat_id = str(body.get("chat_id", "")).strip()
    if not raw_path or not isinstance(content, str):
        return JSONResponse({"error": "missing path or content"}, status_code=400)
    if len(content.encode("utf-8")) > _WORKSPACE_FILE_MAX_BYTES:
        return JSONResponse({"error": "content too large"}, status_code=413)

    roots = _allowed_roots(config)
    result = _resolve_workspace_path(roots, raw_path, allow_fuzzy=False)
    if isinstance(result, Response):
        # Resolver returns 404 for missing files. For an edit-and-save flow
        # we allow creating new files anywhere; relative paths still anchor
        # to the primary workspace root.
        try:
            candidate = Path(raw_path).expanduser()
            resolved = candidate.resolve() if candidate.is_absolute() else (roots[0] / candidate).resolve()
        except (OSError, ValueError):
            return JSONResponse({"error": "bad path"}, status_code=400)
    else:
        resolved = result
    if resolved.suffix.lower() not in _WORKSPACE_FILE_EXTS:
        return JSONResponse({"error": "unsupported type"}, status_code=415)

    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content)
    except OSError as exc:
        return JSONResponse({"error": f"write failed: {exc}"}, status_code=500)

    snap_meta = None
    if chat_id and pcm.get_chat(chat_id) is not None:
        snap_meta = await pcm.snapshots.capture(
            chat_id=chat_id,
            file_path=str(resolved),
            action="edited",
            tool="PWAEdit",
        )
    return JSONResponse({
        "ok": True,
        "snapshot": snap_meta.to_dict() if snap_meta else None,
    })


# ── Schedules ───────────────────────────────────────────────────────────

def _enrich_schedule(
    entry: ScheduleEntry, pcm=None, *, now: datetime | None = None
) -> dict:
    """Serialize a ScheduleEntry and attach computed fields (context_label, next_run)."""
    entry_dict = asdict(entry)
    if pcm is not None and hasattr(pcm, "schedule_effective_routing"):
        provider, model, workspace = pcm.schedule_effective_routing(entry)
        entry_dict["effective_provider"] = provider
        entry_dict["effective_model"] = model
        entry_dict["workspace"] = workspace
    else:
        entry_dict["effective_provider"] = entry.provider
        entry_dict["effective_model"] = entry.model
    web_project_id = entry_dict.get("web_project_id")
    web_chat_id = entry_dict.get("web_chat_id")
    # Whether the target still resolves. Stated explicitly because the PWA
    # cannot infer it from context_label: that field is always set, so a
    # truthy label suppressed the "unavailable" indicator and a stale target
    # rendered as an ordinary one (previously as a bare `proj-...` id).
    entry_dict["context_available"] = True
    if web_project_id and pcm:
        project = pcm.get_project(web_project_id)
        if project:
            entry_dict["context_label"] = f"{project.name} (new chat per run)"
        else:
            # A stale id is not a label. Show the remembered name, which is
            # also what the dispatcher re-homes by.
            remembered = (entry_dict.get("web_project_name") or "").strip()
            entry_dict["context_label"] = (
                f"{remembered} (new chat per run)" if remembered else "Project not found"
            )
            entry_dict["context_available"] = bool(
                remembered and pcm.find_project(remembered, entry_dict.get("workspace") or "")
            )
    elif web_chat_id and pcm:
        chat = pcm.get_chat(web_chat_id)
        entry_dict["context_label"] = chat.title if chat else web_chat_id
        entry_dict["context_available"] = chat is not None
    else:
        entry_dict["context_label"] = ""
    next_run = compute_next_run(entry)
    entry_dict["next_run"] = next_run.isoformat() if next_run is not None else None
    # "Missed" detection: a schedule whose last expected fire has passed but
    # which never recorded a trigger for that day. The 5-minute grace avoids
    # flagging a schedule during the brief window between its fire time and the
    # next poll tick (or the startup catch-up pass).
    last_expected = compute_last_expected_run(entry, now=now)
    entry_dict["last_expected_run"] = (
        last_expected.isoformat() if last_expected is not None else None
    )
    missed = False
    if last_expected is not None:
        expected_day = last_expected.date().isoformat()
        # A schedule is "missed" only if the cron path skipped this slot. A
        # manual "Run now" stamps ``last_dispatched_at`` but not
        # ``last_triggered_on``, so we also check the dispatch stamp: any
        # dispatch at or after the expected fire means the schedule was
        # attended to (even a late manual run the next morning), regardless of
        # whether the auto tick stamped the daily-idempotency key.
        dispatched_since_expected = was_dispatched_since(entry, last_expected)
        not_triggered = (
            not entry.last_triggered_on or expected_day > entry.last_triggered_on
        ) and not dispatched_since_expected
        overdue = ((now or datetime.now(UTC)) - last_expected) > timedelta(minutes=5)
        missed = not_triggered and overdue
    entry_dict["missed"] = missed
    entry_dict["last_dispatched_at"] = entry.last_dispatched_at or None
    return entry_dict


async def list_schedules(request: Request) -> JSONResponse:
    sm = request.app.state.schedule_manager
    pcm = request.app.state.project_chat_manager
    schedules = sm.list_entries()
    return JSONResponse([_enrich_schedule(s, pcm) for s in schedules])


async def list_automation(request: Request) -> JSONResponse:
    """Status of background automations for the Settings → Automation page.

    Reads the job-run log and returns one entry per automation this machine
    can actually run (jobs that never ran still appear), each with its last
    run, recent history, and aggregate stats. Scheduled jobs whose schedule is
    not installed here are omitted — nothing would ever trigger them.
    Read-only.
    """
    from ciao import job_runs

    installed: set[str] | None = None
    try:
        sm = request.app.state.schedule_manager
        installed = {entry.schedule_id for entry in sm.list_entries()}
    except Exception:  # noqa: BLE001 — no schedule manager: filter nothing
        installed = None

    return JSONResponse(job_runs.automation_summary(installed_schedules=installed))


async def trigger_backfill_insights(request: Request) -> JSONResponse:
    """Run session insights over every archive that is missing them.

    Accepts an optional ``model`` for a one-off run with a different model —
    the recovery path when the configured insights model keeps failing (it
    times out on slow local backends). The stored Settings → Models choice is
    left alone.
    """
    import asyncio
    from ciao.job_runs import track
    from ciao.insights import backfill_insights_task, format_backfill_summary

    config = request.app.state.config
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — empty body means "use the configured model"
        body = {}
    model = (body or {}).get("model")
    model = model.strip() if isinstance(model, str) else ""

    async def _run_backfill():
        async with track(
            "backfill_insights", "Insights backfill", category="system",
            model=model,
        ) as handle:
            result = await backfill_insights_task(
                config, mode="both", model_override=model,
            )
            handle.extra.update(result)
            summary = format_backfill_summary(result)
            handle.extra["summary"] = summary
            if model:
                handle.extra["model_override"] = model
            if result["errors"]:
                handle.status = "error"
                handle.error = summary

    asyncio.create_task(_run_backfill())
    return JSONResponse({"status": "started", "model": model}, status_code=202)


async def create_schedule(request: Request) -> JSONResponse:
    sm = request.app.state.schedule_manager
    state = request.app.state.state_store
    pcm = request.app.state.project_chat_manager
    body = await request.json()

    web_chat_id = body.get("web_chat_id")
    web_project_id = body.get("web_project_id")

    # Persist only explicit overrides. Empty model/provider values mean
    # "inherit the selected workspace" and are resolved afresh at dispatch
    # time, so changing a workspace default also changes future runs.
    ctx = ChatContext(chat_id=0)
    model = (body.get("model") or "").strip()
    mode = state.get_mode(ctx)

    frequency = body.get("frequency", "weekly")
    run_at_date = body.get("run_at_date")
    # Reject one-off schedules pointed at a past datetime — they would
    # never auto-fire, and silently keeping them around is worse than 400.
    if frequency == "once":
        if not run_at_date or not body.get("time"):
            return JSONResponse(
                {"error": "once schedules require run_at_date and time"},
                status_code=400,
            )
        try:
            target_date = datetime.fromisoformat(run_at_date).date()
            hh, mm = body["time"].split(":")
            tz = ZoneInfo(body.get("timezone", "Europe/Zurich"))
            target_dt = datetime(
                target_date.year, target_date.month, target_date.day,
                int(hh), int(mm), tzinfo=tz,
            )
        except (ValueError, KeyError):
            return JSONResponse({"error": "invalid run_at_date or time"}, status_code=400)
        if target_dt <= datetime.now(tz):
            return JSONResponse(
                {"error": "run_at_date must be in the future"},
                status_code=400,
            )

    # Manual schedules don't auto-fire, so `time` is optional. For everything
    # else we still require it (create will happily take "" but then the entry
    # would never tick).
    provider = (body.get("provider") or "").strip()
    if provider and provider not in supported_providers():
        return JSONResponse({"error": f"unknown provider '{provider}'"}, status_code=400)
    try:
        archive_policy = normalize_archive_policy(body.get("archive_policy"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    # Stamp the workspace from the target project so the schedule still routes
    # correctly after a fresh init regenerates project IDs (web_project_id goes
    # stale; workspace survives). Explicit body override wins.
    workspace = (body.get("workspace") or "").strip().lower()
    known_workspaces = _known_workspace_names(pcm)
    target_project = pcm.get_project(web_project_id) if web_project_id else None
    if workspace not in known_workspaces and web_project_id:
        workspace = target_project.workspace if target_project else ""
    entry = sm.create(
        daily_time_utc=body.get("time") or "",
        prompt=body["prompt"],
        model=model,
        provider=provider,
        mode=mode,
        chat_id=body.get("chat_id", 0),
        timezone_name=body.get("timezone", "Europe/Zurich"),
        days_of_week=body.get("days_of_week"),
        thread_id=body.get("thread_id"),
        frequency=frequency,
        day_of_month=body.get("day_of_month"),
        run_at_date=run_at_date,
        web_chat_id=web_chat_id,
        web_project_id=web_project_id,
        web_project_name=target_project.name if target_project else "",
        archive_policy=archive_policy,
        workspace=workspace if workspace in known_workspaces else "",
        title=str(body.get("title", "")).strip(),
        description=str(body.get("description", "")).strip(),
    )
    return JSONResponse(_enrich_schedule(entry, pcm), status_code=201)


async def run_schedule_now(request: Request) -> JSONResponse:
    """Trigger a schedule immediately."""
    schedule_id = request.path_params["schedule_id"]
    sm = request.app.state.schedule_manager
    try:
        result = await sm.dispatch_now(schedule_id)
    except ValueError:
        return JSONResponse({"error": "not found"}, status_code=404)
    except RuntimeError as exc:
        if "paused" in str(exc).lower():
            return JSONResponse({"error": str(exc)}, status_code=409)
        raise
    return JSONResponse(result, status_code=201)


async def schedule_detail(request: Request) -> JSONResponse:
    """Handle PATCH (update) and DELETE for a single schedule."""
    schedule_id = request.path_params["schedule_id"]
    if request.method == "DELETE":
        sm = request.app.state.schedule_manager
        ok = sm.delete(schedule_id)
        return JSONResponse({"ok": ok})
    # PATCH
    store = request.app.state.schedule_manager._store
    entry = store.get(schedule_id)
    if entry is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    body = await request.json()
    if "title" in body:
        entry.title = str(body["title"]).strip()
    if "description" in body:
        entry.description = str(body["description"]).strip()
    if "time" in body:
        entry.daily_time_utc = body["time"]
    if "prompt" in body:
        entry.prompt = body["prompt"]
    if "timezone" in body:
        entry.timezone_name = body["timezone"]
    if "days_of_week" in body:
        entry.days_of_week = body["days_of_week"] or None
    if "thread_id" in body:
        entry.thread_id = body["thread_id"] or None
    if "chat_id" in body:
        entry.chat_id = body["chat_id"]
    if "frequency" in body:
        entry.frequency = body["frequency"]
    if "day_of_month" in body:
        entry.day_of_month = body["day_of_month"]
    if "run_at_date" in body:
        entry.run_at_date = body["run_at_date"] or None
    if "web_chat_id" in body:
        entry.web_chat_id = body["web_chat_id"] or None
    if "web_project_id" in body:
        entry.web_project_id = body["web_project_id"] or None
        # Re-stamp the workspace and the target's name. Project ids regenerate
        # per instance, so the name is what lets a later run find the same
        # project again instead of silently falling back to General.
        pcm = request.app.state.project_chat_manager
        project = pcm.get_project(entry.web_project_id) if entry.web_project_id else None
        entry.workspace = project.workspace if project else ""
        entry.web_project_name = project.name if project else ""
    if "workspace" in body:
        ws = (body["workspace"] or "").strip().lower()
        pcm = request.app.state.project_chat_manager
        entry.workspace = ws if ws in _known_workspace_names(pcm) else ""
    if "model" in body:
        # Empty means "inherit workspace default" and must stay empty so the
        # next dispatch observes any workspace configuration change.
        entry.model = (body["model"] or "").strip()
    if "provider" in body:
        new_provider = (body["provider"] or "").strip()
        if new_provider and new_provider not in supported_providers():
            return JSONResponse(
                {"error": f"unknown provider '{new_provider}'"}, status_code=400
            )
        entry.provider = new_provider
    try:
        if "archive_policy" in body:
            entry.archive_policy = normalize_archive_policy(body.get("archive_policy"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if "enabled" in body:
        entry.enabled = bool(body["enabled"])
    store.replace(entry)
    pcm = request.app.state.project_chat_manager
    return JSONResponse(_enrich_schedule(entry, pcm))


# ── Loops ────────────────────────────────────────────────────────────────
# In-chat loops: re-dispatch a prompt into one fixed chat every N minutes.
# Runtime start/stop state lives in the LoopManager (autostart decides what
# runs at boot), so PATCH {"running": bool} toggles the manager, everything
# else edits the persisted entry.

def _enrich_loop(entry, manager, pcm=None) -> dict:
    """Serialize a LoopEntry and attach computed fields (running, context_label, next_run)."""
    entry_dict = asdict(entry)
    running = manager.is_running(entry.loop_id)
    entry_dict["running"] = running
    chat = pcm.get_chat(entry.web_chat_id) if pcm else None
    entry_dict["context_label"] = chat.title if chat else entry.web_chat_id
    next_run = None
    if running:
        if entry.last_run_at:
            try:
                last = datetime.fromisoformat(entry.last_run_at)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=UTC)
                next_run = (last + entry.interval()).isoformat()
            except ValueError:
                pass
        else:
            next_run = datetime.now(UTC).isoformat(timespec="seconds")
    entry_dict["next_run"] = next_run
    return entry_dict


async def list_loops(request: Request) -> JSONResponse:
    lm = request.app.state.loop_manager
    pcm = request.app.state.project_chat_manager
    return JSONResponse([_enrich_loop(entry, lm, pcm) for entry in lm.list()])


async def create_loop(request: Request) -> JSONResponse:
    lm = request.app.state.loop_manager
    pcm = request.app.state.project_chat_manager
    body = await request.json()

    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return JSONResponse({"error": "prompt is required"}, status_code=400)
    web_chat_id = (body.get("web_chat_id") or "").strip()
    if not web_chat_id:
        return JSONResponse({"error": "web_chat_id must point to an existing chat"}, status_code=400)
    chat = pcm.get_chat(web_chat_id)
    if chat is None:
        return JSONResponse({"error": "web_chat_id must point to an existing chat"}, status_code=400)
    try:
        interval_minutes = int(body.get("interval_minutes", 10))
    except (TypeError, ValueError):
        return JSONResponse({"error": "interval_minutes must be an integer"}, status_code=400)
    if interval_minutes < 1:
        return JSONResponse({"error": "interval_minutes must be >= 1"}, status_code=400)

    # A loop inherits the workspace of its chat's project, same as a schedule.
    loop_project_id = getattr(chat, "project_id", "") or ""
    loop_project = pcm.get_project(loop_project_id) if loop_project_id else None
    entry = lm.create(
        prompt=prompt,
        web_chat_id=web_chat_id,
        interval_minutes=interval_minutes,
        title=(body.get("title") or "").strip(),
        # Starting implies autostart, so a running loop survives a restart
        # instead of going quietly dead (see CiaoControlPlane.loop_create).
        autostart=bool(body.get("autostart")) or bool(body.get("start")),
        web_project_id=loop_project_id,
        workspace=getattr(loop_project, "workspace", "") or "",
    )
    if body.get("start"):
        lm.start_loop(entry.loop_id)
    publish_loops_changed(pcm)
    return JSONResponse(_enrich_loop(entry, lm, pcm), status_code=201)


async def loop_detail(request: Request) -> JSONResponse:
    """Handle PATCH (update / start / stop) and DELETE for a single loop."""
    loop_id = request.path_params["loop_id"]
    lm = request.app.state.loop_manager
    pcm = request.app.state.project_chat_manager
    if request.method == "DELETE":
        deleted = lm.delete(loop_id)
        publish_loops_changed(pcm)
        return JSONResponse({"ok": deleted})
    # PATCH
    entry = lm.get(loop_id)
    if entry is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    body = await request.json()
    if "prompt" in body:
        prompt = (body["prompt"] or "").strip()
        if not prompt:
            return JSONResponse({"error": "prompt is required"}, status_code=400)
        entry.prompt = prompt
    if "title" in body:
        entry.title = (body["title"] or "").strip()
    if "interval_minutes" in body:
        try:
            interval_minutes = int(body["interval_minutes"])
        except (TypeError, ValueError):
            return JSONResponse({"error": "interval_minutes must be an integer"}, status_code=400)
        if interval_minutes < 1:
            return JSONResponse({"error": "interval_minutes must be >= 1"}, status_code=400)
        entry.interval_minutes = interval_minutes
    if "web_chat_id" in body:
        web_chat_id = (body["web_chat_id"] or "").strip()
        if not web_chat_id or pcm.get_chat(web_chat_id) is None:
            return JSONResponse({"error": "web_chat_id must point to an existing chat"}, status_code=400)
        entry.web_chat_id = web_chat_id
    if "autostart" in body:
        entry.autostart = bool(body["autostart"])
    lm.replace(entry)
    if "running" in body:
        if body["running"]:
            chat = pcm.get_chat(entry.web_chat_id)
            if chat is None:
                project = pcm._resolve_loop_project(entry)
                if project is None:
                    # Nothing left to dispatch into: the target chat is gone and
                    # the loop's project/workspace no longer resolves. Starting
                    # it would mark it running while every tick no-ops.
                    return JSONResponse(
                        {
                            "error": (
                                "This loop's chat and project are both gone. "
                                "Point it at an existing chat before starting it."
                            )
                        },
                        status_code=409,
                    )
                new_chat = pcm.create_chat(
                    project.project_id,
                    title=entry.title or f"Loop: {entry.prompt[:30]}",
                )
                entry.web_chat_id = new_chat.chat_id
                lm.replace(entry)
            elif chat.archived:
                # The target chat was archived (e.g. by an auto-archive
                # policy) while the loop was stopped. Resuming into a dead
                # chat would just auto-stop again on the next tick, so fork
                # a fresh chat from the archived transcript and re-point the
                # loop at it instead.
                try:
                    new_chat = pcm.continue_archived_chat(entry.web_chat_id)
                except ValueError as exc:
                    return JSONResponse({"error": str(exc)}, status_code=409)
                entry.web_chat_id = new_chat.chat_id
                lm.replace(entry)
            lm.start_loop(loop_id)
        else:
            lm.stop_loop(loop_id)
    publish_loops_changed(pcm)
    return JSONResponse(_enrich_loop(entry, lm, pcm))


async def run_loop_now(request: Request) -> JSONResponse:
    """Fire one loop iteration immediately (works even when stopped)."""
    loop_id = request.path_params["loop_id"]
    lm = request.app.state.loop_manager
    try:
        result = await lm.run_now(loop_id)
    except ValueError:
        return JSONResponse({"error": "not found"}, status_code=404)
    if result.get("status") == "busy":
        return JSONResponse(
            {"error": "chat has a turn in flight; retry when it finishes", **result},
            status_code=409,
        )
    if result.get("status") == "missing-chat":
        return JSONResponse({"error": "target chat no longer exists", **result}, status_code=409)
    return JSONResponse(result, status_code=201)


# ── Models ───────────────────────────────────────────────────────────────

async def list_models(request: Request) -> JSONResponse:
    config = request.app.state.config
    # `?refresh=1` bypasses the provider catalog caches. Each provider serves its
    # own catalog on demand, so there is nothing to warm at startup; this is the
    # on-demand equivalent, used by the Settings tab so a provider connected in
    # another window shows up without waiting out the TTL.
    refresh = str(request.query_params.get("refresh", "")).strip().lower() in {
        "1", "true", "yes", "on",
    }
    # Independent per-provider discovery calls (each may spin up an app-server
    # and round-trip an RPC) — sequential awaits summed their latencies, so a
    # cold cache (the 5-minute TTL lapses between normal chat-creation gaps)
    # stalled every "New Chat" for as long as both providers took combined.
    codex_catalog, opencode_catalog = await asyncio.gather(
        CodexProvider.model_catalog(config.workspace_root, force=refresh),
        OpencodeProvider.model_catalog(config.workspace_root, force=refresh),
    )
    visible_codex = [item for item in codex_catalog if not item.get("hidden")]
    codex_models = [
        str(item.get("model") or item.get("id") or "")
        for item in visible_codex
        if str(item.get("model") or item.get("id") or "")
    ]
    codex_default = next(
        (
            str(item.get("model") or item.get("id") or "")
            for item in visible_codex
            if item.get("isDefault")
        ),
        codex_models[0] if codex_models else "",
    )
    # The operator's per-provider default model wins over the catalog default.
    codex_operator_default = config.default_model_for_provider("codex")
    if codex_operator_default in codex_models:
        codex_default = codex_operator_default
    # opencode is bring-your-own-provider: its catalog is whatever backends the
    # user has connected, so an empty list simply means "not signed in yet".
    opencode_models = [
        str(item.get("model") or "") for item in opencode_catalog if item.get("model")
    ]
    opencode_operator_default = config.default_model_for_provider("opencode")
    if opencode_operator_default in opencode_models:
        opencode_default = opencode_operator_default
    else:
        opencode_default = opencode_models[0] if opencode_models else ""
    # Per-model reasoning-effort variants, merged into the same map the PWA
    # already reads for Codex so the picker needs no provider-specific branch.
    opencode_reasoning_levels = {
        str(item.get("model")): list(item.get("variants") or [])
        for item in opencode_catalog
        if item.get("model")
    }
    model_reasoning_levels = {
        **opencode_reasoning_levels,
        **_codex_reasoning_levels(codex_catalog),
    }
    codex_model_metadata: dict[str, dict] = {}
    for item in visible_codex:
        model_id = str(item.get("model") or item.get("id") or "")
        if not model_id:
            continue
        codex_model_metadata[model_id] = {
            "display_name": str(item.get("displayName") or model_id),
            "description": str(item.get("description") or ""),
            "default_reasoning_effort": str(
                item.get("defaultReasoningEffort") or ""
            ),
            "input_modalities": list(item.get("inputModalities") or []),
        }
    # Claude Code serves one upstream, so its models are a single list rather
    # than the work/personal split the routing-backend era needed.
    claude_models = list(config.claude_models)
    claude_default = (
        config.claude_default_model
        if config.claude_default_model in claude_models
        else (claude_models[0] if claude_models else "")
    )

    return JSONResponse({
        "models": config.claude_models,
        "default": config.claude_default_model,
        "provider_models": {
            "claude": claude_models,
            "codex": codex_models,
            "opencode": opencode_models,
        },
        "provider_defaults": {
            "claude": claude_default,
            "codex": codex_default,
            "opencode": opencode_default,
        },
        "backends": {
            "anthropic": True,
            "codex": bool(codex_models),
            "opencode": bool(opencode_models),
        },
        "codex_models": codex_models,
        "codex_model_metadata": codex_model_metadata,
        "opencode_models": opencode_models,
        # Registry-driven descriptors so the PWA can build its provider list
        # (labels, buckets, capabilities) without a hard-coded union.
        "providers": [
            {
                "id": item.id,
                "label": item.label,
                "short_label": item.short_label,
                "capabilities": asdict(capabilities_for(item.id)),
            }
            for item in provider_registry.descriptors()
        ],
        "model_reasoning_levels": model_reasoning_levels,
        "thinking_levels": {k: list(v) for k, v in THINKING_LEVELS.items()},
    })


# ── Routine settings (Settings → Models tab) ────────────────────────────

def _routines_payload(config, app_settings) -> dict:
    """Shared GET/PATCH response: overrides, effective values, options."""
    from ciao import native_sidecar
    from ciao.voice import (
        apple_dictation_available,
        apple_speech_available,
        dictation_unavailable_reason,
        system_voices,
    )

    s = app_settings.settings
    from ciao.critique import critique_models_effective

    critique_effective = critique_models_effective(config)
    if config.insights_model_override:
        insights_effective = config.insights_model_override
    else:
        insights_effective = config.default_model_for_workspace(
            config.primary_workspace()
        )

    # On Automatic the insights routine resolves per workspace
    # (resolve_insights_model takes the chat's workspace), so the single
    # *_effective value above is only the primary-workspace answer. Reporting it
    # alone reads as a global choice and is wrong for every other workspace, so
    # ship the whole map and let the UI say what actually varies. Empty when an
    # override is set, because then one model really does apply everywhere.
    insights_by_workspace: dict[str, str] = {}
    for name in config.workspace_names():
        if not config.insights_model_override:
            insights_by_workspace[name] = config.default_model_for_workspace(name)

    return {
        # Overrides as stored ("" = automatic default).
        "insights_model": s.insights_model,

        "critique_models": s.critique_models,
        # Per-provider default model for new chats, as stored (missing =
        # provider's own catalog default).
        "provider_default_models": s.provider_default_models or {},
        # Per-provider default thinking level for new chats, as stored.
        "provider_default_thinking": s.provider_default_thinking or {},
        # Per-provider routine models, as stored (missing = provider default).
        "provider_insights_models": s.provider_insights_models or {},
        # Per-provider default execution mode for new chats, as stored
        # (missing = built-in default). Effective defaults below.
        "provider_default_modes": s.provider_default_modes or {},
        "provider_default_modes_effective": {
            item.id: config.default_mode_for_provider(item.id)
            for item in provider_registry.descriptors()
        },
        # What actually runs right now, after defaults.
        "insights_model_effective": insights_effective,
        # Per-workspace resolution for the Automatic case; empty when overridden.
        "insights_model_by_workspace": insights_by_workspace,

        "critique_models_effective": critique_effective,
        # The "apple" title/insights options are hardware-gated: they need
        # macOS 26+, the desktop app, and Apple Intelligence switched on in
        # System Settings. No app-side opt-in: the routine rows show the
        # missing prerequisite instead of hiding the option.
        "apple_model_available": native_sidecar.apple_model_available(),
        "apple_model_unavailable_reason": native_sidecar.apple_model_unavailable_reason(),
        "transcription": {
            "locale": config.transcription_locale,
            # On-device dictation needs macOS 26+, the installed app, and a
            # dictation language. Settings hides the local option entirely when
            # it cannot run, and shows the reason when the user asks.
            "available": apple_dictation_available(),
            "unavailable_reason": dictation_unavailable_reason(),
        },
        "speech": {
            "local_voice": config.tts_local_voice,
            "available": apple_speech_available(),
            # Voices differ per machine, so the picker is populated from the
            # system rather than a hardcoded list, best quality first.
            "local_voices": system_voices(),
        },
        # Grouped options for the routine model selectors.
        "model_options": {
            "anthropic": list(config.claude_models),
        },
        "backends": {
            "anthropic": True,
        },
        "workspace_context": {
            "workspace_root": str(config.workspace_root),
            # `vault_root` is the configured path, which after the re-rooting is
            # the emptied shared one — true but useless on its own, so the vaults
            # that actually hold notes are reported beside it.
            "vault_root": str(config.vault_root),
            "vault_roots": [
                {"workspace": name, "path": str(root)}
                for root, name, _prefix in (
                    config.vault_scan_targets()
                    if hasattr(config, "vault_scan_targets")
                    else []
                )
            ],
        },
    }


async def settings_routines(request: Request) -> JSONResponse:
    """GET returns routine settings; PATCH updates the runtime overrides.

    Persisted in ``.runtime/app_settings.json`` and applied to the live
    config immediately — no restart needed. Empty string clears an
    override back to the env-backed default.
    """
    config = request.app.state.config
    app_settings = request.app.state.app_settings
    if app_settings is None:
        return JSONResponse({"error": "settings store unavailable"}, status_code=503)
    if request.method == "PATCH":
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "expected an object"}, status_code=400)
        try:
            app_settings.update(body)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        app_settings.apply_to_config(config)
    # _routines_payload probes the native sidecar, which spawns a subprocess on
    # first call. Off the event loop: the availability checks it replaced were
    # in-process find_spec/which calls, so this used to be free.
    return JSONResponse(await asyncio.to_thread(_routines_payload, config, app_settings))


# ── Status ───────────────────────────────────────────────────────────────

async def status_endpoint(request: Request) -> JSONResponse:
    """GET returns status, PATCH updates model/mode."""
    state = request.app.state.state_store
    ctx = ChatContext(chat_id=0)
    ctx_state = state.get_context(ctx)
    if request.method == "PATCH":
        body = await request.json()
        if "model" in body:
            state.set_active_model(body["model"], ctx)
        if "mode" in body:
            state.set_mode(body["mode"], ctx)
        ctx_state = state.get_context(ctx)

    return JSONResponse({
        "active_model": ctx_state.active_model,
        "mode": ctx_state.mode,
        "cost": state.bot_state.cost,
    })


async def startup_status_endpoint(request: Request) -> JSONResponse:
    """Return startup phase progress and node role state."""
    from ciao import __version__

    node_mgr = getattr(request.app.state, "node_state_manager", None)
    role = node_mgr.get_role() if node_mgr else "host"
    active_peer_url = node_mgr.get_active_peer_url() if node_mgr else None
    config = getattr(request.app.state, "config", None)

    tracker = getattr(request.app.state, "startup_tracker", None)
    payload = tracker.to_dict() if tracker is not None else {"phases": [], "overall_ready": True}
    latest_version, update_available = await _cached_update_hint(request)
    payload.update({
        "version": __version__,
        "desktop_api_version": 1,
        # Identifies the machine that answered. A client asks its host for this
        # so the mirrored UI can name whose data it is showing.
        "node_id": node_mgr.node_id if node_mgr else "",
        "node_role": role,
        "active_peer_url": active_peer_url,
        "host_url": node_mgr.get_host_url() if node_mgr else None,
        "has_host_session": bool(node_mgr.get_host_session()) if node_mgr else False,
        "auth_required": bool(getattr(config, "pwa_auth_required", False)) if config else False,
        "latest_version": latest_version,
        "update_available": update_available,
    })
    return JSONResponse(payload)


async def _refresh_update_hint(app: Any, fetcher: Callable[[], dict[str, object]]) -> None:
    """Populate the cached update hint off the request path."""

    try:
        status = await asyncio.to_thread(fetcher)
    except Exception:
        # Deliberately broad: this runs detached, and a failed release lookup
        # must never surface as an unhandled task exception.
        return
    app.state.update_hint = (
        str(status.get("latest_version") or ""),
        bool(status.get("update_available")),
    )


async def _cached_update_hint(request: Request) -> tuple[str, bool]:
    """Return ``(latest_version, update_available)`` without ever blocking.

    The menu bar polls this endpoint on a short client timeout to decide whether
    the engine is alive, so the release lookup must stay off the request path
    entirely. ``asyncio.to_thread`` cannot be cancelled, so even a ``wait_for``
    around it would block until the thread finished — instead the lookup runs
    detached and this only reads the last value it stored. The first poll after
    a cold start reports no hint; the next one picks it up.
    """

    app = request.app
    fetcher = getattr(app.state, "package_status_fetcher", None)
    if not callable(fetcher):
        return "", False
    task = getattr(app.state, "update_hint_task", None)
    if task is None or task.done():
        app.state.update_hint_task = asyncio.create_task(_refresh_update_hint(app, fetcher))
    return getattr(app.state, "update_hint", ("", False))


async def active_chats_endpoint(request: Request) -> JSONResponse:
    """Return chat IDs with in-flight work (streaming or background subagents).

    Drives the macOS menu bar: it spins the icon while anything is working and
    marks those chats in the open-chats list. Unauthenticated like the
    startup-status endpoint, since the local menu bar process has no session;
    it only leaks opaque chat IDs, not their contents.
    """
    pcm = getattr(request.app.state, "project_chat_manager", None)
    if pcm is None:
        return JSONResponse({"active_chat_ids": []})
    return JSONResponse({"active_chat_ids": pcm.active_chat_ids()})


def _menubar_chat_needs_input(pending_question: str, pending_permission: str = "") -> bool:
    """True when AskUserQuestion JSON or an Approve/Deny prompt is waiting."""
    if (pending_permission or "").strip():
        return True
    raw = (pending_question or "").strip()
    if not raw:
        return False
    try:
        parsed = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(parsed, dict):
        return False
    questions = parsed.get("questions")
    return isinstance(questions, list) and len(questions) > 0


async def menubar_chats_endpoint(request: Request) -> JSONResponse:
    """Open-chat summaries for the macOS menu bar.

    Usable without a session, but only from a loopback peer (the tray holds no
    session cookie) — see ``_LOOPBACK_ONLY_API`` in ``ciao.web.auth``. Unlike
    ``/api/active-chats`` this returns titles and workspace names, so it must
    not be reachable from the network.

    In client mode the proxy forwards this to the active peer so the tray list
    matches the chats that ``/api/active-chats`` reports as working — local
    ``web_projects.json`` can lag the leader after handover.
    """
    limit_raw = request.query_params.get("limit", "10")
    try:
        limit = max(1, min(50, int(limit_raw)))
    except ValueError:
        limit = 10

    pcm = getattr(request.app.state, "project_chat_manager", None)
    if pcm is None:
        return JSONResponse({"chats": [], "attention_count": 0})

    chats = pcm.list_chats()
    # Delegate completion is internal model-to-model traffic: it wakes the
    # supervisor, so the PWA deliberately does not report a nested delegate as
    # a second unread chat. Keep the tray feed on the same rule. An archived or
    # missing supervisor makes the delegate an orphan, which remains a normal
    # visible chat and may be unread.
    active_chat_ids = {
        candidate.chat_id
        for candidate in chats
        if not candidate.archived
    }

    rows: list[dict[str, object]] = []
    attention_count = 0
    for chat in chats:
        if chat.archived:
            continue
        project = pcm.get_project(chat.project_id)
        if project is None:
            continue
        activity = chat.last_activity_at or ""
        read = chat.last_read_at or ""
        nested_delegate = bool(
            getattr(chat, "spawned_from_chat_id", "")
            and getattr(chat, "spawned_from_chat_id", "") in active_chat_ids
        )
        unread = not nested_delegate and bool(activity) and activity > read
        needs_input = _menubar_chat_needs_input(
            chat.pending_question, getattr(chat, "pending_permission", "")
        )
        if unread or needs_input:
            attention_count += 1
        rows.append(
            {
                "chat_id": chat.chat_id,
                "title": chat.title or "Untitled chat",
                "workspace": project.workspace,
                "last_activity_at": activity,
                "unread": unread,
                "needs_input": needs_input,
            }
        )
    rows.sort(key=lambda row: str(row.get("last_activity_at") or ""), reverse=True)
    # attention_count is counted over every non-archived chat but the list is
    # truncated to `limit`, so a chat needing attention that is not among the
    # most recent would be counted in the menu bar badge with no row to explain
    # it. Float those chats to the front (stable, so recency order survives
    # within each group) — the badge then always has something to point at.
    rows.sort(key=lambda row: not (row["unread"] or row["needs_input"]))
    return JSONResponse({"chats": rows[:limit], "attention_count": attention_count})


async def open_chat_endpoint(request: Request) -> JSONResponse:
    """Ask an already-open PWA to navigate to a chat.

    macOS ``open -a PWA /chat/...`` often focuses the installed app without
    changing the window URL when it is already running. The menu bar calls
    this unauthenticated local endpoint first; connected clients receive an
    ``open_chat`` event over ``/ws/events`` and switch chats in place.
    """
    chat_id = str(request.path_params.get("chat_id") or "").strip()
    if not chat_id:
        return JSONResponse({"ok": False, "error": "missing chat_id"}, status_code=400)
    pcm = getattr(request.app.state, "project_chat_manager", None)
    if pcm is None or pcm.get_chat(chat_id) is None:
        return JSONResponse({"ok": False, "error": "chat not found"}, status_code=404)
    delivered = bool(getattr(pcm.events, "subscriber_count", 0))
    pcm.events.publish({"type": "open_chat", "chat_id": chat_id})
    return JSONResponse({"ok": True, "chat_id": chat_id, "delivered": delivered})


async def setup_status_endpoint(request: Request) -> JSONResponse:
    """Return first-run setup readiness for the onboarding wizard."""
    return JSONResponse(setup_status(request.app.state.config))



def _host_name(value: str) -> str:
    host = value.strip()
    if host.startswith("["):
        end = host.find("]")
        host = host[1:end] if end != -1 else host
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    return host.rstrip(".").lower()


def _localhost_request(request: Request) -> bool:
    name = _host_name(request.headers.get("host", ""))
    if not name:
        name = (request.url.hostname or "").rstrip(".").lower()
    # 0.0.0.0 counts as loopback: a browser pointed at it can only reach the
    # viewer's own machine (users copy it from the uvicorn bind-address log).
    return name in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _same_host_header(request: Request, value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if not parsed.hostname:
        return False
    request_host = _host_name(request.headers.get("host", ""))
    if not request_host:
        request_host = (request.url.hostname or "").rstrip(".").lower()
    return parsed.hostname.rstrip(".").lower() == request_host


def _setup_finish_origin_allowed(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin:
        return _same_host_header(request, origin)
    referer = request.headers.get("referer")
    if referer:
        return _same_host_header(request, referer)
    return True


def _interactive_foreground_run() -> bool:
    """True when setup can hand the bootstrap server to launchd.

    The bundled desktop app deliberately starts bootstrap with no terminal
    attached, but it still owns the one-time onboarding process and must hand
    the configured server to the LaunchAgent when setup completes.
    """
    try:
        return sys.stderr.isatty() or os.environ.get("CIAO_BOOTSTRAP_LAUNCHD_HANDOFF") == "1"
    except (AttributeError, ValueError):
        return os.environ.get("CIAO_BOOTSTRAP_LAUNCHD_HANDOFF") == "1"


def _schedule_launchd_server_handoff() -> bool:
    """Spawn a detached helper that starts the launchd server agent.

    The helper runs after this process exits (a foreground `ciao run` still
    holds the port), so the wizard's finish can hand the server to launchd
    and the user can close the terminal. The agent's RunAtLoad + KeepAlive
    cover the race: if the port is still held on first launch, launchd
    retries. Returns False when the plist is missing or the spawn fails, in
    which case the caller falls back to the in-place re-exec restart.
    """
    plist = Path.home() / "Library" / "LaunchAgents" / "com.ciao.server.plist"
    if not plist.exists():
        return False
    script = (
        "sleep 3; "
        f"/bin/launchctl load -w '{plist}' 2>/dev/null; "
        f"/bin/launchctl kickstart gui/{os.getuid()}/com.ciao.server 2>/dev/null; "
        "exit 0"
    )
    try:
        subprocess.Popen(
            ["/bin/sh", "-c", script],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    print(
        "\nSetup complete — Ciaobot is moving to the background service.\n"
        "You can close this terminal; the server now starts automatically at login.\n",
        file=sys.stderr,
        flush=True,
    )
    return True


async def setup_finish_endpoint(request: Request) -> JSONResponse:
    """Write real setup config from bootstrap mode and request supervisor restart."""
    config = request.app.state.config
    if not getattr(config, "bootstrap_mode", False):
        return JSONResponse({"error": "setup finish is only available in bootstrap mode"}, status_code=409)
    if not _localhost_request(request) or not _setup_finish_origin_allowed(request):
        return JSONResponse(
            {
                "error": "setup finish is localhost-only — open the wizard at "
                f"http://localhost:{config.pwa_port}"
            },
            status_code=403,
        )
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "json object is required"}, status_code=400)

    # The wizard's primary question is the workspace: one root folder holding
    # the vault (memory-vault/ by default) plus app data, all one git repo.
    # vault_root is optional and only set when the second brain lives
    # elsewhere (existing notes folder).
    workspace = str(body.get("workspace", "")).strip()
    if not workspace:
        return JSONResponse({"error": "workspace is required"}, status_code=400)
    from ciao.setup_status import tcc_protected_location

    protected = tcc_protected_location(workspace)
    if protected:
        return JSONResponse(
            {
                "error": (
                    f"'{workspace}' is inside ~/{protected}, which macOS privacy "
                    "protection blocks background services from reading — the "
                    "Ciaobot server and menu bar would fail to start. Pick a "
                    "folder outside ~/Desktop, ~/Documents, and ~/Downloads "
                    "(for example ~/ciaobot)."
                )
            },
            status_code=400,
        )
    # Optional: an empty push contact leaves Web Push disabled until the
    # operator configures one in Settings.
    push_contact = str(body.get("push_contact", "")).strip()
    try:
        port = int(body.get("port") or config.pwa_port)
    except (TypeError, ValueError):
        return JSONResponse({"error": "port must be an integer"}, status_code=400)
    if port < 1 or port > 65535:
        return JSONResponse({"error": "port must be between 1 and 65535"}, status_code=400)
    default_provider = str(body.get("provider") or "claude").strip().lower()
    if default_provider not in _workspace_provider_values(config):
        return JSONResponse(
            {"error": f"unknown provider '{default_provider}'"}, status_code=400
        )

    from ciao.cli import detect_vault_mode, setup_workspace

    # The wizard no longer asks scratch-vs-existing: when the request does
    # not pin a mode, inspect the folder — empty starts from scratch, one
    # with visible content is an existing notes folder the onboarding agent
    # adapts in place.
    vault_mode = str(body.get("vault_mode", "")).strip().lower() or detect_vault_mode(workspace)
    workspace_name = str(body.get("workspace_name", "")).strip() or "personal"
    if not _WORKSPACE_NAME_RE.fullmatch(workspace_name):
        return JSONResponse(
            {
                "error": (
                    "workspace name must use letters, numbers, dashes, "
                    "or underscores"
                )
            },
            status_code=400,
        )

    # Password protection is not optional: the wizard collects the password, and
    # the bootstrap token it would otherwise inherit is a machine-generated
    # value nobody could type on a second device.
    from ciao.web.auth import MIN_PWA_PASSWORD_LENGTH

    password = str(body.get("password") or body.get("auth_token") or "").strip()
    if len(password) < MIN_PWA_PASSWORD_LENGTH:
        return JSONResponse(
            {
                "error": (
                    "password is required and must be at least "
                    f"{MIN_PWA_PASSWORD_LENGTH} characters"
                )
            },
            status_code=400,
        )

    # setup_workspace only writes workspace files and the bundled-engine
    # LaunchAgent. The release installer owns app downloads, so setup remains
    # responsive and never reaches out to GitHub.
    written = await asyncio.to_thread(
        functools.partial(
            setup_workspace,
            workspace,
            auth_token=password,
            auth_required=True,
            push_contact=push_contact,
            vault_root=str(body.get("vault_root", "")).strip() or None,
            vault_mode=vault_mode,
            workspace_name=workspace_name,
            default_provider=default_provider,
            python_path=str(body.get("python", "")).strip() or None,
            port=port,
            launch_agents_dir=str(body.get("launch_agents_dir", "")).strip() or None,
            app_dir=str(body.get("app_dir", "")).strip() or None,
        )
    )
    # Hand the chosen workspace to the relaunched process. A foreground
    # `ciao run` restarts by re-execing itself with the current environment,
    # and nothing else tells the fresh process where setup landed — without
    # this it boots straight back into the bootstrap wizard.
    os.environ["CIAO_WORKSPACE"] = str(Path(workspace).expanduser().resolve())
    os.environ["PWA_PORT"] = str(port)
    # Same reason for the credentials: `load_dotenv` does not override values
    # already in the environment, so a stale PWA_AUTH_TOKEN inherited from the
    # bootstrap process would outrank the password just written to .env.
    os.environ["PWA_AUTH_TOKEN"] = password
    os.environ["PWA_AUTH_REQUIRED"] = "true"
    # Only the real per-user LaunchAgents dir may be registered with launchd —
    # scripted/test setups pass a custom dir and must not touch it. Nothing
    # menu-bar related happens here any more: Ciaobot.app is the menu bar, and
    # setup_workspace above has just retired the old `com.ciao.menubar` agent.
    real_launch_agents = (
        sys.platform == "darwin"
        and not str(body.get("launch_agents_dir", "")).strip()
    )

    restart = bool(body.get("restart", True))
    # An interactive foreground `ciao run` (the documented install flow) hands
    # the server over to launchd instead of re-execing: a detached helper
    # loads the server agent once this process has exited and released the
    # port, and the wizard requests a clean exit (code 0, no relaunch). The
    # user can then close the terminal. Under launchd stderr is a log file,
    # not a TTY, so a supervised server keeps the plain re-exec restart.
    handoff = (
        restart
        and real_launch_agents
        and _interactive_foreground_run()
        and _schedule_launchd_server_handoff()
    )
    if restart:
        restart_fn = getattr(request.app.state, "request_restart", None)
        if callable(restart_fn):
            restart_fn(0 if handoff else config.restart_exit_code)

    return JSONResponse({
        "ok": True,
        "restart_requested": restart,
        "workspace": str(Path(workspace).expanduser().resolve()),
        "written": [str(path) for path in written],
    })


def _setup_fs_guard(request: Request) -> JSONResponse | None:
    """Bootstrap-mode + localhost guard shared by the setup folder-picker routes."""
    config = request.app.state.config
    if not getattr(config, "bootstrap_mode", False):
        return JSONResponse({"error": "not found"}, status_code=404)
    if not _localhost_request(request) or not _setup_finish_origin_allowed(request):
        return JSONResponse(
            {
                "error": "setup filesystem access is localhost-only — open the "
                f"wizard at http://localhost:{config.pwa_port}"
            },
            status_code=403,
        )
    return None


def _setup_dir_listing(target: Path) -> dict:
    """Return the folder-picker listing payload for a resolved directory."""
    home = Path.home().resolve()
    dirs: list[dict[str, str]] = []
    for entry in target.iterdir():
        if entry.name.startswith("."):
            continue
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        dirs.append({"name": entry.name, "path": str(entry)})
    dirs.sort(key=lambda row: row["name"].lower())
    display = str(target)
    if target == home:
        display = "~"
    elif str(target).startswith(str(home) + os.sep):
        display = "~" + str(target)[len(str(home)):]
    parent = target.parent
    return {
        "path": str(target),
        "display_path": display,
        "parent": str(parent) if parent != target else None,
        "dirs": dirs,
        "home": str(home),
    }


def _resolve_setup_dir(raw: str) -> Path | None:
    """Expand and resolve a picker path; None when it is not an existing directory."""
    try:
        target = Path(raw).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if not target.is_dir():
        return None
    return target


async def setup_list_dirs_endpoint(request: Request) -> JSONResponse:
    """List local subdirectories for the first-run setup folder picker."""
    guard = _setup_fs_guard(request)
    if guard is not None:
        return guard
    raw = str(request.query_params.get("path") or "~").strip() or "~"
    target = _resolve_setup_dir(raw)
    if target is None:
        return JSONResponse({"error": f"not a directory: {raw}"}, status_code=400)
    try:
        return JSONResponse(_setup_dir_listing(target))
    except PermissionError:
        return JSONResponse({"error": f"permission denied: {target}"}, status_code=400)
    except OSError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def setup_inspect_folder_endpoint(request: Request) -> JSONResponse:
    """Probe a candidate workspace folder for the first-run setup wizard.

    Returns the inferred vault mode ("scratch" vs "existing"), the resolved
    vault root, and any nested workspace directories the folder already
    contains (e.g. legacy ``memory-vault/personal/`` and
    ``memory-vault/work/``). The wizard uses this to show existing workspace
    chips when they are present; otherwise it asks for the logical workspace
    name that will be assigned to the selected folder.
    """
    from ciao.cli import detect_vault_mode
    from ciao.setup_status import detect_nested_workspaces

    guard = _setup_fs_guard(request)
    if guard is not None:
        return guard
    raw = str(request.query_params.get("path") or "").strip()
    if not raw:
        return JSONResponse({"error": "path is required"}, status_code=400)
    target = _resolve_setup_dir(raw)
    if target is None:
        return JSONResponse({"error": f"not a directory: {raw}"}, status_code=400)
    # Reuse the same "scratch vs existing" rule the setup/finish endpoint
    # applies, so the wizard and the server agree before the user clicks
    # Finish. The vault root mirrors setup_workspace's logic: an existing
    # notes folder (no prior scaffold) is the vault itself; otherwise the
    # vault lives under memory-vault/.
    mode = detect_vault_mode(target)
    existing_env_path = target / ".env"
    vault_root = target / "memory-vault"
    if mode == "existing" and not vault_root.is_dir():
        vault_root = target
    nested = detect_nested_workspaces(vault_root) if mode == "existing" else []
    return JSONResponse({
        "path": str(target),
        "mode": mode,
        "vault_root": str(vault_root),
        "existing_workspaces": nested,
        "has_env": existing_env_path.is_file(),
    })


async def setup_mkdir_endpoint(request: Request) -> JSONResponse:
    """Create a folder from the first-run setup folder picker and return the refreshed listing."""
    guard = _setup_fs_guard(request)
    if guard is not None:
        return guard
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "json object is required"}, status_code=400)
    name = str(body.get("name", "")).strip()
    if not name:
        return JSONResponse({"error": "name is required"}, status_code=400)
    if "/" in name or "\\" in name or os.sep in name or name.startswith("."):
        return JSONResponse({"error": "folder name must not contain path separators or start with a dot"}, status_code=400)
    parent = _resolve_setup_dir(str(body.get("path", "")).strip())
    if parent is None:
        return JSONResponse({"error": "path must be an existing directory"}, status_code=400)
    try:
        (parent / name).mkdir()
    except FileExistsError:
        return JSONResponse({"error": f"already exists: {name}"}, status_code=400)
    except OSError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    try:
        return JSONResponse(_setup_dir_listing(parent))
    except OSError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


# ── Admin ────────────────────────────────────────────────────────────────

async def admin_snapshot(request: Request) -> JSONResponse:
    """Trigger a git snapshot (add, commit, push)."""
    mgr = getattr(request.app.state, "local_session_manager", None)
    confirm_warnings = False
    try:
        body = await request.json()
        confirm_warnings = bool(body.get("confirm_warnings", False))
    except ValueError:
        pass

    if mgr is not None:
        preflight = await mgr.preflight()
        if preflight["blockers"]:
            return JSONResponse(
                {"ok": False, "error": "Blocked by secrets check", "blockers": preflight["blockers"]},
                status_code=400
            )
        if preflight["warnings"] and not confirm_warnings:
            return JSONResponse(
                {"ok": False, "error": "Warnings exist, require confirmation", "warnings": preflight["warnings"]},
                status_code=400
            )

    config = request.app.state.config
    ws = config.workspace_root

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["git", "add", "-A"],
            cwd=str(ws), capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return JSONResponse({"error": f"git add failed: {result.stderr}"}, status_code=500)

        status = await asyncio.to_thread(
            subprocess.run,
            ["git", "status", "--porcelain"],
            cwd=str(ws), capture_output=True, text=True, timeout=10,
        )
        if not status.stdout.strip():
            return JSONResponse({"ok": True, "message": "Nothing to commit"})

        from datetime import UTC, datetime
        ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
        await asyncio.to_thread(
            subprocess.run,
            ["git", "commit", "-m", f"pwa snapshot {ts}"],
            cwd=str(ws), capture_output=True, text=True, timeout=30,
        )

        push = await asyncio.to_thread(
            subprocess.run,
            ["git", "push"],
            cwd=str(ws), capture_output=True, text=True, timeout=60,
        )

        return JSONResponse({
            "ok": True,
            "message": f"Snapshot committed and {'pushed' if push.returncode == 0 else 'push failed'}",
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)





# Enough of a failing step's tail to carry a traceback or a pip resolver
# error. The old 500 was not enough for either.
_DEPLOY_STEP_OUTPUT_CHARS = 4000


def _record_step(step: str, result: subprocess.CompletedProcess) -> dict:
    """Capture a step's output, keeping the part that says what went wrong.

    Two things this must not do, both of which hid real failures:

    * Truncate from the *head*. Build tools put progress chatter first and
      the diagnosis last, so `[:500]` on a failed `pip install` showed
      "Preparing editable metadata..." and cut off before the error.
    * Take ``stdout or stderr``. pip writes progress to stdout and errors
      to stderr, so stdout is never empty and stderr was always discarded.

    The full output is logged regardless: the response is the only other
    copy, and a truncated card was previously the sole record of a failure.
    """
    parts = [p for p in (result.stdout.strip(), result.stderr.strip()) if p]
    combined = "\n".join(parts)
    out = combined[-_DEPLOY_STEP_OUTPUT_CHARS:]
    if len(combined) > _DEPLOY_STEP_OUTPUT_CHARS:
        out = f"[earlier output trimmed]\n{out}"
    ok = result.returncode == 0
    if not ok:
        logger.error(
            "deploy step %r failed (exit %s):\n%s", step, result.returncode, combined
        )
    return {"step": step, "ok": ok, "output": out}


def _pip_install_hint(output: str) -> str:
    """Turn a development deploy's "cannot uninstall" wall into guidance.

    A packaged app cannot be replaced by the developer-only deploy action. The
    raw installer output otherwise sends users to debug the wrong thing.
    """
    lowered = output.lower()
    if "cannot uninstall" in lowered or "no record file" in lowered:
        return (
            "The running engine belongs to a packaged Ciaobot.app and cannot be "
            "replaced by the development deploy action. Use the signed in-app "
            "updater or re-run the one-line installer for production updates."
        )
    return ""


def _resolve_codebase_root(config) -> Path:
    """Where the deploy steps run git, pip, and npm.

    ``CIAO_APP_REPO`` wins over the module path for developer-mode deploys. A
    packaged app does not resolve a checkout for production updates.
    """
    configured = getattr(config, "app_repo", None)
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2]


def _checkout_problem(codebase_root: Path) -> str:
    """Why ``codebase_root`` cannot be deployed from, or an empty string.

    Checked up front because the underlying failures are misleading: git says
    "not a git repository" and npm says ENOENT, neither of which points at an
    engine that was installed rather than checked out.
    """
    if not (codebase_root / ".git").exists():
        return f"{codebase_root} is not a git checkout"
    if not (codebase_root / "web" / "package.json").exists():
        return f"{codebase_root} has no web/package.json"
    return ""


def _run_root_npm_install(codebase_root: Path) -> subprocess.CompletedProcess:
    args = ["npm", "install", "--no-audit", "--no-fund"]
    if not (codebase_root / "package.json").exists():
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="skipped: no root package.json",
            stderr="",
        )
    return desktop_build.run_step(args, cwd=str(codebase_root), timeout=180)


async def admin_deploy(request: Request) -> JSONResponse:
    """Snapshot local work, pull latest, rebuild frontend, restart service."""
    mgr = getattr(request.app.state, "local_session_manager", None)
    confirm_warnings = False
    try:
        body = await request.json()
        confirm_warnings = bool(body.get("confirm_warnings", False))
    except ValueError:
        pass

    if mgr is not None:
        preflight = await mgr.preflight()
        if preflight["blockers"]:
            return JSONResponse(
                {"steps": [], "ok": False, "error": "Blocked by secrets check", "blockers": preflight["blockers"]},
                status_code=400
            )
        if preflight["warnings"] and not confirm_warnings:
            return JSONResponse(
                {"steps": [], "ok": False, "error": "Warnings exist, require confirmation", "warnings": preflight["warnings"]},
                status_code=400
            )

    config = request.app.state.config
    ws = config.workspace_root
    codebase_root = _resolve_codebase_root(config)
    steps = []

    problem = _checkout_problem(codebase_root)
    if problem:
        hint = (
            f"{problem}. Set CIAO_APP_REPO to the ciaobot checkout so Restart can "
            "pull, reinstall, and rebuild from source."
        )
        steps.append({"step": "locate checkout", "ok": False, "output": hint})
        return JSONResponse({"steps": steps, "ok": False, "error": hint}, status_code=400)
    steps.append({"step": "locate checkout", "ok": True, "output": str(codebase_root)})

    # 0. Snapshot: stage, commit (if dirty), rebase, push.
    #    Captures in-flight writes so the pull that follows can't clobber them
    #    and so the peer instance can see this side's work.
    from datetime import UTC, datetime
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    ok, detail = await _commit_and_push(ws, f"pwa snapshot before deploy {ts}")
    steps.append({"step": "snapshot", "ok": ok, "output": detail[:500]})
    if not ok:
        return JSONResponse(
            {"steps": steps, "ok": False, "error": f"snapshot failed: {detail}"},
            status_code=500,
        )

    # 1. Git pull (idempotent after snapshot, but catches any race push).
    #    Uses the same retry helper as the snapshot step so a short DNS
    #    resolver flap doesn't fail the whole deploy with a confusing
    #    "Could not resolve host" error.
    rc, pull_out = await _git_pull_with_retry(codebase_root)
    if rc != 0:
        out = (pull_out or "").strip()[:500]
        steps.append({"step": "git pull", "ok": False, "output": out})
        return JSONResponse(
            {"steps": steps, "ok": False, "error": f"git pull failed: {out}"},
            status_code=500,
        )
    steps.append({"step": "git pull", "ok": True, "output": (pull_out or "").strip()[:500]})

    # 2. pip install
    import sys
    result = await asyncio.to_thread(
        desktop_build.run_step, [sys.executable, "-m", "pip", "install", "-e", "."],
        cwd=str(codebase_root), timeout=120,
    )
    steps.append(_record_step("pip install", result))
    if result.returncode != 0:
        # str() because `steps` is inferred as list[dict[str, object]] from its
        # first append; _record_step always puts a string here.
        hint = _pip_install_hint(str(steps[-1]["output"]))
        if hint:
            steps[-1]["output"] = f"{hint}\n\n{steps[-1]['output']}"
        # The step card renders the full output; the top-level error is the
        # one-line headline above it, not a second copy of the same text.
        return JSONResponse(
            {"steps": steps, "ok": False, "error": hint or "pip install failed."},
            status_code=500,
        )

    # 2b. npm install at repo root, only when a root package exists. The PWA's
    # package.json lives under web/, so running npm at the repo root on this
    # project would otherwise emit ENOENT on every deploy.
    result = await asyncio.to_thread(
        _run_root_npm_install, codebase_root,
    )
    steps.append(_record_step("npm install (root)", result))

    # 3. npm build
    web_dir = codebase_root / "web"
    result = await asyncio.to_thread(
        desktop_build.run_step, ["npm", "run", "build"],
        cwd=str(web_dir), timeout=120,
    )
    steps.append(_record_step("npm build", result))
    if result.returncode != 0:
        return JSONResponse(
            {"steps": steps, "ok": False, "error": f"npm build failed: {steps[-1]['output']}"},
            status_code=500,
        )

    # 3b. Desktop shell. Changes under desktop/ only reach the window through a
    # rebuilt bundle, so dev instances rebuild it here and swap it in during the
    # restart below. Released installs skip this: no checkout, no cargo. The
    # rebuild is minutes long, hence the staleness check rather than doing it on
    # every restart.

    relaunch_desktop = False
    if getattr(config, "dev_mode", False):
        needed, reason = await asyncio.to_thread(desktop_build.needs_rebuild, codebase_root)
        if not needed:
            steps.append({"step": "desktop app", "ok": True, "output": f"skipped: {reason}"})
        else:
            steps.append({"step": "desktop app", "ok": True, "output": f"rebuilding: {reason}"})
            desktop_steps, relaunch_desktop = await asyncio.to_thread(
                desktop_build.build_and_stage, codebase_root, runner=desktop_build.run_step,
            )
            steps.extend(desktop_steps)
            failed = next((s for s in desktop_steps if not s["ok"]), None)
            if failed is not None:
                return JSONResponse(
                    {"steps": steps, "ok": False, "error": f"{failed['step']} failed: {failed['output']}"},
                    status_code=500,
                )

    # 4. Signal restart. Must go through app.state.request_restart (which sets
    # the restart flag and calls server.shutdown()). Raising RestartRequested
    # inside this detached task does NOT work:
    # the exception never reaches the `except RestartRequested` wrapping
    # server.serve() in ciao.main, so it gets swallowed as an unhandled task
    # exception and the process keeps running with stale code. Deploy then looks
    # successful (frontend rebuilt) but backend changes never load.
    from ciao.signals import RestartRequested

    async def _do_restart():
        await asyncio.sleep(2)
        # The desktop swap runs before the engine restart, not after: the
        # relaunched app comes up against a live engine and then rides the
        # normal restart-drain path, instead of racing launchd for the runtime
        # directory while the engine is down.
        if relaunch_desktop:
            try:
                installed = await asyncio.to_thread(
                    desktop_build.install_staged_and_relaunch, runner=desktop_build.run_step,
                )
                for step in installed:
                    if step["ok"]:
                        logger.info("deploy: %s: %s", step["step"], step["output"])
                    else:
                        logger.error("deploy: %s: %s", step["step"], step["output"])
            except Exception:
                # A failed relaunch must not strand the engine on stale code;
                # the operator can reopen the app by hand.
                logger.exception("deploy: desktop install and relaunch failed")
        fn = getattr(request.app.state, "request_restart", None)
        if callable(fn):
            fn(config.restart_exit_code)
        else:
            raise RestartRequested(config.restart_exit_code)

    asyncio.create_task(_do_restart())
    steps.append({
        "step": "restart",
        "ok": True,
        "output": "swapping in the rebuilt desktop app first" if relaunch_desktop else "",
    })

    return JSONResponse({"steps": steps, "ok": True})


async def admin_skills(request: Request) -> JSONResponse:
    """List skills known to Ciaobot, labelled as custom or GitHub/package.

    Merged across every agent root. Reading `workspace_root` alone showed
    `{custom: 0, github: 0, stock: 29}` on a migrated install — measured — while
    19 custom and 7 upstream skills sat in the primary root's catalog. The page
    looked empty.

    A skill of the same name in two roots is reported once, with the workspaces
    that hold it, because the page is a catalog rather than a per-root listing
    and two rows for one name reads as a duplicate rather than as sharing.
    """
    config = request.app.state.config
    targets = getattr(config, "agent_root_targets", None)
    roots = list(targets()) if callable(targets) else [(config.workspace_root, "")]

    merged: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for root, name in roots:
        inventory = build_skill_inventory(root)
        for skill in inventory.get("skills", []):
            key = str(skill.get("name") or "")
            existing = merged.get(key)
            if existing is None:
                skill["workspaces"] = [name] if name else []
                merged[key] = skill
                counts[str(skill.get("label") or "")] = (
                    counts.get(str(skill.get("label") or ""), 0) + 1
                )
            elif name and name not in existing.get("workspaces", []):
                existing.setdefault("workspaces", []).append(name)
    return JSONResponse(
        {"counts": counts, "skills": [merged[k] for k in sorted(merged)]}
    )


async def admin_add_skill(request: Request) -> JSONResponse:
    """Add an upstream skill from GitHub."""
    config = request.app.state.config
    try:
        body = await request.json()
        source = body.get("source", "").strip()
        skill = body.get("skill", "").strip() or None
        agent = body.get("agent", "claude-code").strip() or "claude-code"
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Invalid request body: {e}"}, status_code=400)

    if not source:
        return JSONResponse({"ok": False, "error": "GitHub URL or owner/repo is required"}, status_code=400)

    try:
        import sys
        
        script_path = Path(config.workspace_root) / "scripts" / "skills_add.py"
        if not script_path.exists():
            return JSONResponse({"ok": False, "error": f"Script {script_path} does not exist"}, status_code=500)

        cmd = [sys.executable, str(script_path), source]
        if skill:
            cmd.extend(["--skill", skill])
        cmd.extend(["--agent", agent])

        # Run script to add the skill to skills-lock.json
        result = await asyncio.to_thread(
            subprocess.run,
            cmd,
            cwd=str(config.workspace_root), capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
            return JSONResponse({"ok": False, "error": err}, status_code=500)
        
        # Run sync-skills immediately so it mirrors custom/locked skills to the local Claude catalog
        sync_result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "ciao.cli", "sync-skills", "--workspace", str(config.workspace_root)],
            cwd=str(config.workspace_root), capture_output=True, text=True, timeout=60,
        )
        if sync_result.returncode != 0:
            err = sync_result.stderr.strip() or sync_result.stdout.strip() or f"sync exit code {sync_result.returncode}"
            return JSONResponse({"ok": False, "error": f"Skill added but sync failed: {err}"}, status_code=500)

    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    return JSONResponse({"ok": True, "message": "Skill added and synchronized successfully."})



async def admin_status(request: Request) -> JSONResponse:
    """Extended status for the settings page."""
    config = request.app.state.config
    state = request.app.state.state_store

    branch = ""
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(config.workspace_root),
            capture_output=True, text=True, timeout=5,
        )
        branch = result.stdout.strip()
    except Exception:
        pass

    return JSONResponse({
        "cost": state.bot_state.cost,
        "branch": branch,
        "models": config.claude_models,
        "default_model": config.claude_default_model,
        "default_mode": config.claude_mode,
    })


# ── Local session flow (current-branch sync + conflict-resolution chat) ──


def _local_manager(request: Request):
    return getattr(request.app.state, "local_session_manager", None)


def _open_merge_chat(request: Request, branch: str) -> dict:
    """Open an interactive chat that resolves sync conflicts on ``branch``
    with the user. Returns {ok, chat_id, project_id} or {error}."""
    config = request.app.state.config
    pcm = request.app.state.project_chat_manager
    # Any workspace can host this; prefer the primary one, then settle for the
    # first workspace that has a General project. Keying on a workspace named
    # "personal" meant the whole sync-conflict flow failed on installs whose
    # workspaces are named anything else.
    workspace = config.primary_workspace()
    project = next(
        (p for p in pcm.list_projects(workspace) if p.name == "General"), None
    )
    if project is None:
        for candidate in config.workspace_names():
            project = next(
                (p for p in pcm.list_projects(candidate) if p.name == "General"), None
            )
            if project is not None:
                break
    if project is None:
        return {"error": "no General project in any workspace to host the merge chat"}

    from datetime import UTC, datetime
    from ciao.local_session import MERGE_PROMPT

    title = f"Resolve sync conflicts: {branch} {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')}"
    prompt = MERGE_PROMPT.replace("{branch}", str(branch))

    chat = pcm.create_chat(
        project.project_id, title=title, model=config.claude_default_model
    )
    pcm.start_stream(chat.chat_id, prompt)
    return {"ok": True, "chat_id": chat.chat_id, "project_id": project.project_id}


async def local_preflight(request: Request) -> JSONResponse:
    """Git preflight check for dirty changes, file categories, and secrets."""
    mgr = _local_manager(request)
    if mgr is None:
        return JSONResponse(
            {"error": "local session manager not initialised"}, status_code=500
        )
    return JSONResponse(await mgr.preflight())


async def local_status(request: Request) -> JSONResponse:
    """Current workspace git state: git_repo, branch (may be null), dirty."""
    mgr = _local_manager(request)
    if mgr is None:
        return JSONResponse(
            {"error": "local session manager not initialised"}, status_code=500
        )
    return JSONResponse(mgr.status())


async def local_handback(request: Request) -> JSONResponse:
    """Commit the session and sync the current branch with origin.

    Clean pull -> pushed directly. Conflict -> an interactive resolution chat
    is opened in Ciaobot. Never creates or switches branches.
    """
    mgr = _local_manager(request)
    if mgr is None:
        return JSONResponse(
            {"error": "local session manager not initialised"}, status_code=500
        )
    branch = mgr.branch
    if branch is None:
        return JSONResponse(
            {"ok": False, "error": "workspace is not a git repository (or is on a detached HEAD)"},
            status_code=400,
        )

    confirm_warnings = False
    try:
        body = await request.json()
        confirm_warnings = bool(body.get("confirm_warnings", False))
    except ValueError:
        pass

    preflight = await mgr.preflight()
    if preflight["blockers"]:
        return JSONResponse(
            {"ok": False, "error": "Blocked by secrets check", "blockers": preflight["blockers"]},
            status_code=400
        )
    if preflight["warnings"] and not confirm_warnings:
        return JSONResponse(
            {"ok": False, "error": "Warnings exist, require confirmation", "warnings": preflight["warnings"]},
            status_code=400
        )

    result = await mgr.commit_and_sync()
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    if result.get("merged"):
        return JSONResponse(result)
    # Conflict: hand off to an interactive resolution chat.
    merge = _open_merge_chat(request, result.get("branch") or branch)
    return JSONResponse({**result, "merge": merge})


async def local_resync(request: Request) -> JSONResponse:
    """After the conflict chat pushed the branch, merge origin/<branch> in."""
    mgr = _local_manager(request)
    if mgr is None:
        return JSONResponse(
            {"error": "local session manager not initialised"}, status_code=500
        )
    result = await mgr.resync()
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


async def handover_merge(request: Request) -> JSONResponse:
    """Open an interactive chat that resolves sync conflicts on a branch. Also
    used by ``local_handback`` when the automatic pull conflicts."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = {}
    branch = (body.get("branch") if isinstance(body, dict) else None) or ""
    if not branch:
        mgr = _local_manager(request)
        branch = (mgr.branch if mgr else None) or ""
    if not branch:
        return JSONResponse(
            {"error": "workspace is not a git repository (or is on a detached HEAD)"},
            status_code=400,
        )
    merge = _open_merge_chat(request, branch)
    return JSONResponse(merge, status_code=200 if merge.get("ok") else 500)



async def debug_issues(request: Request) -> JSONResponse:
    """Runtime issue report (server errors + failed job runs) for self-fix.

    Only available when ``CIAO_DEV_MODE`` is set; hidden (404) otherwise so
    the endpoint does not advertise itself on production instances.
    """
    config = request.app.state.config
    if not getattr(config, "dev_mode", False):
        return JSONResponse(
            {"error": "debug endpoints require CIAO_DEV_MODE"}, status_code=404
        )
    from ciao.debug_report import DEFAULT_LOG_LINES, build_issue_report

    try:
        lines = int(request.query_params.get("lines", DEFAULT_LOG_LINES))
    except ValueError:
        lines = DEFAULT_LOG_LINES
    lines = max(1, min(lines, 2000))
    report = await asyncio.to_thread(
        build_issue_report, config.workspace_root, log_lines=lines
    )
    return JSONResponse(report)


async def cli_stats(request: Request) -> JSONResponse:
    """Return Claude Code CLI stats from ~/.claude/stats-cache.json."""
    if not _STATS_CACHE_PATH.exists():
        return JSONResponse({"error": "stats-cache.json not found"}, status_code=404)
    try:
        data = json.loads(_STATS_CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return JSONResponse({"error": "failed to read stats"}, status_code=500)
    return JSONResponse(data)


# ── Proposal queue ──────────────────────────────────────────────────────


# A section header opens with a date (either a plain ``YYYY-MM-DD`` or the
# timestamped ``YYYY-MM-DDThh:mm:ss+00:00`` form the curators append). The date
# is what ``dismiss-older-than`` buckets rows against.
_SECTION_DATE_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})")
# The queue file lives at this relative path inside each workspace's vault.
_PROPOSALS_REL = ("Workspace", "Memory-Proposals.md")
# Skill-proposal files live under this folder, one dated file per candidate.
_SKILL_PROPOSALS_REL = ("Workspace", "Skill-Proposals")


def _proposals_file(config, workspace: str) -> Path:
    """The proposal queue for one workspace, rooted at its vault folder."""
    return config.workspace_vault_root(workspace).joinpath(*_PROPOSALS_REL)


def _skill_proposals_dir(config, workspace: str) -> Path:
    """The skill-proposal queue folder for one workspace."""
    return config.workspace_vault_root(workspace).joinpath(*_SKILL_PROPOSALS_REL)


def _stable_proposal_id(workspace: str, path: str, kind: str, text: str, source: str, dup: int) -> str:
    """A content-derived, stable id for one queued proposal.

    The id hashes the bullet's content plus the workspace and file it lives in,
    so dismissing a neighbouring row never renumbers or renames a survivor.
    ``dup`` is the occurrence index among identical bullets inside one file,
    used only to keep two textually identical rows addressable; it is stable
    because it counts only same-file duplicates, which are unaffected by rows
    in other files (or non-duplicate rows in this one) being removed.
    """
    digest = hashlib.sha256(f"{workspace}\x00{path}\x00{kind}\x00{text}\x00{source}".encode("utf-8")).hexdigest()[:16]
    return f"{digest}{f':{dup}' if dup else ''}"


def _rehome_signal(config) -> dict[str, dict[str, Any]]:
    """Live rehome evidence for every person note, keyed by its queue path.

    Re-computed from the vault rather than trusted from the bullet text: the
    bullet records the destination and reason at queue time, but the UI needs
    to know whether that destination is backed by a tag signal *now*. Keys are
    the vault-relative path forms the bullet names (``personal/People/Mo.md``).
    """
    try:
        candidates = vault_rehome.detect_misfiled_people(
            config.vault_root,
            workspaces=config.workspace_names(),
            # Every vault in the install. Scanning `config.vault_root` returned
            # zero candidates on a migrated install, so the proposals UI silently
            # lost every re-home hint.
            targets=(
                config.vault_scan_targets()
                if hasattr(config, "vault_scan_targets")
                else None
            ),
        )
    except Exception:  # noqa: BLE001 — a broken scan must not fail the list route
        logger.exception("proposal list: rehome signal scan failed")
        return {}
    out: dict[str, dict[str, Any]] = {}
    roles = vault_rehome.resolve_role_workspaces(list(config.workspace_names()))
    for candidate in candidates:
        # Only a single clean signal makes a destination justified; every
        # judgement case the queue holds is explicitly not that.
        justified = candidate.bucket == "mechanical" and bool(candidate.destination)
        # The candidate set is tag-derived, not the guess: a no-tag note names
        # no workspace even though a default counterpart was computed, and a
        # dual-tag note names both. A UI renders these as a picker.
        signalled_roles = {
            vault_rehome.TAG_WORKSPACE_ROLES[t]
            for t in candidate.tags
            if t in vault_rehome.TAG_WORKSPACE_ROLES
        }
        candidate_ws = sorted({roles[role] for role in signalled_roles if role in roles} - {""})
        out[candidate.path] = {
            "destination": candidate.destination,
            "target_workspace": candidate.target_workspace,
            "reason": candidate.reason,
            "justified": justified,
            # Every workspace the tags name is a candidate destination. A
            # dual-tag row yields two, so a UI can render a picker instead of a
            # single pre-filled accept.
            "candidates": candidate_ws,
        }
    return out


def _leak_warning(config, kind: str, workspace: str) -> bool:
    """True when accepting this row would leak a region into the wrong session.

    A ``[memory]`` / ``[profile]`` accept edits one CLAUDE.md region. While one
    guide is shared by every workspace, a proposal queued from another workspace
    and accepted here writes a fact into sessions that did not originate it.
    Region-edit kinds only: a rehome is a file move, not a region write, so it
    never leaks.

    Per-workspace guides have LANDED, which retires this for a migrated install:
    ``_promote_region_row`` resolves the guide through ``agent_root``, so a work
    row is written into work's own ``CLAUDE.md`` and nothing else loads it. The
    condition used to be "not the primary workspace" with the comment "until
    per-workspace guides land", so after the re-rooting it told the operator that
    accepting their own work row would be "visible in every workspace" — of a
    guide only that workspace reads. A warning that is false is worse than none:
    it teaches the operator to click through warnings.
    """
    try:
        accept = proposal_kinds.accept_for(kind)
    except proposal_kinds.UnknownKindError:
        return False
    if accept.action != "edit_region":
        return False
    try:
        shared_guide = Path(config.agent_root(workspace)) == Path(config.workspace_root)
    except (AttributeError, ValueError):
        # No agent_root seam to ask: assume the shared layout, which is the
        # answer that warns rather than the one that stays quiet.
        shared_guide = True
    if not shared_guide:
        return False
    return workspace != config.primary_workspace()


def _perform_rehome_move(config, row: dict[str, Any], target: str) -> dict[str, Any]:
    """Move a queued person note into ``target``, links and all.

    Until now a rehome accept dropped the bullet and moved nothing — the panel
    said so in prose ("Re-home rows are not moved here") and `move_file` was a
    declared accept descriptor that nothing handled. So the queue could ask the
    question and never carry out the answer.

    The row names the note in RENDERED identity form (``personal/People/Mo.md``);
    the mover works install-relative (``personal/memory-vault/People/Mo.md``),
    because that is the space in which a relative link's arithmetic is real. The
    leaf comes from the workspace's own vault directory rather than a constant,
    for the same reason the rebuilds take it.
    """
    from ciao.vault_rehome import move_note_between_roots

    note = str((row.get("rehome") or {}).get("note") or "")
    parts = Path(note).parts
    if len(parts) < 2:
        return {"ok": False, "error": f"the bullet does not name a note ({note!r})"}
    workspace = parts[0]
    try:
        install_root = Path(config.workspace_root)
        vault = Path(config.workspace_vault_root(workspace))
        relative_vault = vault.relative_to(install_root)
        targets = config.vault_scan_targets()
        names = list(config.workspace_names())
    except (AttributeError, ValueError) as exc:
        return {"ok": False, "error": f"could not resolve the vault layout: {exc}"}
    # Derived from the registry, never assumed: the vault sits at
    # `<workspace>/<leaf>` per root and at `<leaf>/<workspace>` while shared. The
    # mover moves a note BETWEEN roots, which only exist in the first shape, so
    # the second is refused with the reason rather than silently building
    # `personal/personal/People/Mo.md` and reporting the note missing.
    vault_parts = relative_vault.parts
    if len(vault_parts) != 2 or vault_parts[0] != workspace:
        return {
            "ok": False,
            "error": (
                f"'{workspace}' does not have its own workspace folder yet "
                f"(its vault is {relative_vault.as_posix()}), so there is no other "
                "root to move a note into"
            ),
        }
    source = (relative_vault / Path(*parts[1:])).as_posix()
    result = move_note_between_roots(
        install_root, source, target, targets=targets, workspaces=names, apply=True
    )
    if result["refusals"]:
        return {"ok": False, "error": result["refusals"][0], "move": result}
    return {
        "ok": True,
        "destination": result["destination"],
        "files_rewritten": result["files_rewritten"],
        "already_moved": bool(result.get("already_moved")),
        "move": result,
    }


def _rehome_target(row: dict[str, Any], requested: str) -> tuple[str, str]:
    """The workspace a rehome accept should move into, or an error.

    An explicit request wins, because a row whose tags name two workspaces is a
    question only the operator can answer. Otherwise the destination has to be
    backed by a single clean tag signal — accepting an unjustified guess would
    move somebody's note on the strength of nothing.
    """
    signal = row.get("rehome") or {}
    if requested:
        # Any registered workspace, not only the ones the tags name. The tags are
        # a hint; the operator asking is the authority, and most queued rows have
        # no tag naming anywhere — restricting the choice to tag-named candidates
        # left every one of the reference install's fourteen rows unmovable, which
        # is the complaint that started this. `move_note_between_roots` still
        # refuses an unregistered name.
        return requested, ""
    if not signal.get("justified"):
        return "", "no tag backs a destination for this note, so pick one explicitly"
    destination = str(signal.get("destination") or "")
    target = Path(destination).parts[0] if destination else ""
    if not target:
        return "", "the signal names no destination workspace"
    return target, ""


def _scan_proposal_rows(config) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Scan every workspace's proposal queue and skill-proposal folder.

    Returns (rows, by_id) where ``by_id`` maps a stable id to the file context
    needed to remove that row later (workspace, absolute path, line index). Each
    row carries the queue fields plus kind-specific signal: a rehome exposes
    candidate destinations and whether any is justified, and a region accept
    from a foreign workspace carries the leak warning.
    """
    primary = config.primary_workspace()
    rows: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    rehome = _rehome_signal(config)
    ws_names = list(config.workspace_names())

    for workspace in config.workspace_names():
        queue = _proposals_file(config, workspace)
        rel_path = Path(workspace).joinpath(*_PROPOSALS_REL).as_posix()
        seen_dup: dict[tuple[str, str, str, str], int] = {}
        if queue.is_file():
            for line_index, raw in enumerate(queue.read_text(encoding="utf-8").splitlines()):
                bullet = proposal_kinds.parse_bullet(raw)
                if bullet is None:
                    continue
                key = (bullet.kind, bullet.text, bullet.source)
                dup = seen_dup.get(key, 0)
                seen_dup[key] = dup + 1
                pid = _stable_proposal_id(workspace, rel_path, bullet.kind, bullet.text, bullet.source, dup)
                row: dict[str, Any] = {
                    "id": pid,
                    "kind": bullet.kind,
                    "text": bullet.text,
                    "source": bullet.source,
                    "workspace": workspace,
                    "path": rel_path,
                    "line": line_index,
                }
                accept = proposal_kinds.accept_for(bullet.kind)
                if accept.action == "edit_region":
                    row["region"] = resolve_region(bullet.kind)
                    row["leak_warning"] = _leak_warning(config, bullet.kind, workspace)
                else:
                    # Rehome rows: expose the live signal. The destination named
                    # in the bullet is a guess unless the tags justify it, and a
                    # dual-tag note names more than one candidate.
                    signal = _rehome_lookup(rehome, bullet.text, ws_names)
                    row["rehome"] = {
                        # The note this row is about, so a UI can show a name and
                        # a direction instead of reprinting the whole bullet.
                        "note": signal["note"],
                        "destination": signal["destination"],
                        "candidates": signal["candidates"],
                        "justified": signal["justified"],
                        "reason": signal["reason"],
                    }
                rows.append(row)
                by_id[pid] = {
                    "workspace": workspace,
                    "path": str(queue),
                    "line": line_index,
                    "row": row,
                }
        # Skill proposals are files, not bullets: no parse_bullet, no accept
        # descriptor, and a whole file is the atomic unit.
        #
        # They are registered in `by_id` all the same, with `file: True` so the
        # handlers can tell a file from a bullet. Listing them without
        # registering them left the read surface working and the write surface
        # missing: the UI renders a dismiss button per row, and every one of the
        # 49 skill rows on a real vault answered 404 "unknown proposal id" —
        # from both the single-row and the batch endpoint. A row you cannot act
        # on is a notification wearing a button.
        skill_dir = _skill_proposals_dir(config, workspace)
        if skill_dir.is_dir():
            for f in sorted(skill_dir.glob("*.md")):
                row_id = _stable_proposal_id(workspace, rel_path, "skill", f.name, "", 0)
                row = {
                    "id": row_id,
                    "kind": "skill",
                    "text": f.stem,
                    "source": "",
                    "workspace": workspace,
                    "path": Path(workspace).joinpath(*_SKILL_PROPOSALS_REL, f.name).as_posix(),
                    "line": -1,
                }
                rows.append(row)
                by_id[row_id] = {
                    "workspace": workspace,
                    "path": str(f),
                    "line": -1,
                    "row": row,
                    "file": True,
                }
    return rows, by_id


def _dismiss_skill_proposal(ctx: dict[str, Any]) -> dict[str, Any]:
    """Take one skill-proposal FILE out of the queue.

    A reviewed proposal is a resolved decision: whether it was implemented or
    disregarded, keeping the file in the queue re-asks the same question. So
    dismiss deletes it rather than moving it aside — the queue is globbed one
    level deep, so either clears it, and the decision is the operator's to keep
    in the Curation-Log. A missing file is already gone, not an error.
    """
    source = Path(ctx["path"])
    if not source.is_file():
        return {"ok": True, "deleted": True}
    try:
        source.unlink()
    except OSError as exc:
        return {"ok": False, "error": f"could not delete {source.name}: {exc}"}
    return {"ok": True, "deleted": True}


def _rehome_lookup(
    rehome: dict[str, dict[str, Any]], text: str, workspaces: Sequence[str] = ()
) -> dict[str, Any]:
    """Resolve a rehome bullet's live signal from its named path.

    The bullet names the source path in backticks (``personal/People/Mo.md``);
    pull that out and match it against the scan keyed by path.

    The alternation is built from the REGISTERED workspace names rather than
    hardcoding ``personal|work``: a workspace named anything else never matched,
    so its rows silently showed "no live rehome signal" forever. Escaped, because
    a workspace name is the user's and may contain regex metacharacters.
    """
    names = [re.escape(n) for n in workspaces if n] or [r"[^/`]+"]
    m = re.search(rf"`((?:{'|'.join(names)})/[^`]+\.md)`", text)
    path = m.group(1) if m else ""
    signal = rehome.get(path)
    if signal is None:
        # The bullet outlived its cause: the note was tagged, moved, or a later
        # rule settled it, and nothing re-detects it now. Marked `stale` rather
        # than left looking undecided — the queue rendered it identically to a
        # genuine "needs a decision" row, so the operator could not tell which
        # rows were asking them something and which were just litter. Two of the
        # reference install's fourteen are in this state.
        return {
            "note": path,
            "destination": "",
            "candidates": [],
            "justified": False,
            "stale": True,
            "reason": "no live rehome signal for this note",
        }
    return {"note": path, "stale": False, **signal}


async def list_proposals(request: Request) -> JSONResponse:
    """Return every queued proposal across workspaces, plus skill proposals.

    Rows are keyed by a stable content-derived id so a UI can act on one without
    a later dismiss renumbering it (see ``_stable_proposal_id``). Rehome rows
    carry candidate destinations and a ``justified`` flag, so the UI never
    pre-fills an accept for a destination no tag backs. Skill-proposal files are
    surfaced under the same ``rows`` list with ``kind: "skill"``.
    """
    config = request.app.state.config
    rows, _by_id = _scan_proposal_rows(config)
    return JSONResponse({"rows": rows})


def _resolve_batch(config, ids: list[str]) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Map ids to removable file contexts, or return an error.

    Returns (None, error) on the first unknown id: the whole batch must resolve
    before anything is written, so an unknown id aborts the batch without
    touching any file.
    """
    _, by_id = _scan_proposal_rows(config)
    resolved: list[dict[str, Any]] = []
    for pid in ids:
        ctx = by_id.get(pid)
        if ctx is None:
            return None, f"unknown proposal id: {pid}"
        resolved.append(ctx)
    return resolved, None


async def dismiss_older_than(request: Request) -> JSONResponse:
    """Atomically drop every queued row dated before a cutoff.

    A July proposal about a forgotten chat is not worth promoting; this clears
    whole dated sections at once. Atomic: all matching rows are removed in one
    rewrite of each affected file, so a crash mid-batch leaves no file half
    written.
    """
    config = request.app.state.config
    raw = request.query_params.get("date", "").strip()
    try:
        cutoff = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return JSONResponse({"error": "date must be YYYY-MM-DD"}, status_code=400)
    removed = 0
    for workspace in config.workspace_names():
        queue = _proposals_file(config, workspace)
        if not queue.is_file():
            continue
        lines = queue.read_text(encoding="utf-8").splitlines()
        keep = []
        section_date = None
        changed = False
        for raw_line in lines:
            m = _SECTION_DATE_RE.match(raw_line)
            if m:
                section_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
                keep.append(raw_line)
                continue
            if proposal_kinds.parse_bullet(raw_line) is not None and section_date is not None and section_date < cutoff:
                removed += 1
                changed = True
                continue
            keep.append(raw_line)
        if changed:
            queue.write_text("\n".join(keep).rstrip() + "\n", encoding="utf-8")
    return JSONResponse({"ok": True, "removed": removed})


async def proposals_batch(request: Request) -> JSONResponse:
    """Accept or dismiss a set of proposals atomically.

    Body: ``{"action": "accept"|"dismiss", "ids": [...]}``. Every id must
    resolve or the batch is rejected with 404 and no file changes. ``accept``
    routes through each row's own descriptor (region edit for memory/profile,
    a file move for rehome) and returns per-row results; it never performs the
    edit itself, matching the MCP resolve path where promotion is a separate
    explicit step.
    """
    config = request.app.state.config
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    action = str(body.get("action", "")).strip()
    raw_ids = body.get("ids")
    if action not in {"accept", "dismiss"} or not isinstance(raw_ids, list) or not raw_ids:
        return JSONResponse({"error": "action must be accept|dismiss and ids[] is required"}, status_code=400)
    ids = [str(pid).strip() for pid in raw_ids]
    requested_workspace = str(body.get("workspace", "") or "").strip()
    resolved, error = _resolve_batch(config, ids)
    if error:
        return JSONResponse({"error": error}, status_code=404)

    # Re-home rows are MOVES, so they are handled before the queue-file grouping
    # too, and one at a time: each move rewrites references across both vaults, so
    # the second move has to see what the first one wrote. Off the event loop for
    # the same reason as the single-row path — a sweep per row is real work, and a
    # cancelled handler leaves notes moved with their rows still queued.
    move_rows = [
        ctx for ctx in resolved
        if action == "accept" and ctx["row"].get("kind") == "rehome"
    ]
    results_moves: list[dict[str, Any]] = []
    moved_ids: set[str] = set()
    for ctx in move_rows:
        row = ctx["row"]
        target, target_error = _rehome_target(row, requested_workspace)
        if target_error:
            results_moves.append({
                "id": row["id"], "action": "move_file", "dismissed": False,
                "error": target_error,
            })
            continue
        outcome = await asyncio.to_thread(_perform_rehome_move, config, row, target)
        if not outcome.get("ok"):
            results_moves.append({
                "id": row["id"], "action": "move_file", "dismissed": False,
                "error": outcome["error"],
            })
            continue
        moved_ids.add(row["id"])
        results_moves.append({
            "id": row["id"], "action": "move_file", "dismissed": True,
            "destination": outcome.get("destination", ""),
            "already_moved": outcome.get("already_moved", False),
        })
    # Only the rows whose move landed may have their bullet dropped; a failed move
    # keeps its row so the note is not left somewhere nobody asked for with
    # nothing recording it.
    resolved = [
        ctx for ctx in resolved
        if ctx not in move_rows or ctx["row"]["id"] in moved_ids
    ]

    # Skill proposals are whole files, so they are handled before the grouping:
    # the grouping below rewrites a queue file by dropping bullet lines, and a
    # skill row has no line in any queue.
    results = list(results_moves)
    file_rows = [ctx for ctx in resolved if ctx.get("file")]
    resolved = [ctx for ctx in resolved if not ctx.get("file")]
    for ctx in file_rows:
        row = ctx["row"]
        # Same result shape a bullet dismiss returns, so the client needs no
        # second contract for a row it renders identically.
        if action != "dismiss":
            results.append({
                "id": row["id"],
                "action": action,
                "dismissed": False,
                "error": "a skill proposal is a file; there is nothing to promote",
            })
            continue
        outcome = _dismiss_skill_proposal(ctx)
        entry = {"id": row["id"], "action": "dismiss", "dismissed": bool(outcome.get("ok"))}
        if not outcome.get("ok"):
            entry["error"] = outcome["error"]
        results.append(entry)

    # Group by file so each affected file is rewritten exactly once.
    by_file: dict[str, dict[str, Any]] = {}
    for ctx in resolved:
        entry = by_file.setdefault(ctx["path"], {"workspace": ctx["workspace"], "lines": set(), "rows": []})
        entry["lines"].add(ctx["line"])
        entry["rows"].append(ctx["row"])

    for path, entry in by_file.items():
        queue = Path(path)
        # Write every promotion BEFORE dropping any bullet, and only drop the
        # ones that landed. A batch that removed the lines first would lose every
        # fact whose region was over cap, silently and in bulk.
        promoted: dict[str, dict[str, Any]] = {}
        keep_lines: set[int] = set()
        if action == "accept":
            for row in entry["rows"]:
                accept = proposal_kinds.accept_for(row["kind"])
                if accept.action != "edit_region":
                    continue
                outcome = _promote_region_row(config, row)
                promoted[row["id"]] = outcome
                if not outcome.get("ok"):
                    keep_lines.add(int(row["line"]))

        lines = queue.read_text(encoding="utf-8").splitlines()
        # Remove highest index first so lower indices stay valid.
        for line_index in sorted(entry["lines"], reverse=True):
            if line_index in keep_lines:
                continue
            if 0 <= line_index < len(lines):
                del lines[line_index]
        queue.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        for row in entry["rows"]:
            if action == "accept":
                accept = proposal_kinds.accept_for(row["kind"])
                outcome = promoted.get(row["id"], {})
                failed = accept.action == "edit_region" and not outcome.get("ok")
                result = {
                    "id": row["id"],
                    "action": accept.action,
                    "dismissed": not failed,
                }
                if accept.action == "edit_region":
                    result["region"] = outcome.get("region", accept.region)
                    result["promoted"] = bool(outcome.get("ok"))
                    result["leak_warning"] = row.get("leak_warning", False)
                    if failed:
                        result["error"] = outcome.get("error", "could not write the region")
                else:
                    result["promoted"] = False
                    result["destination"] = row.get("rehome", {}).get("destination", "")
                    result["justified"] = row.get("rehome", {}).get("justified", False)
                results.append(result)
            else:
                results.append({"id": row["id"], "action": "dismiss", "dismissed": True})
    return JSONResponse({"ok": True, "action": action, "results": results})


def _promote_region_row(config, row: dict[str, Any]) -> dict[str, Any]:
    """Write an accepted memory/profile fact into its workspace's region.

    Accept used to remove the bullet and return a descriptor saying what SHOULD
    happen, matching the MCP flow where the agent edits and then dismisses. In a
    UI where a person clicks Accept that meant the fact left the queue and landed
    nowhere — one click from losing it.

    Order is write-then-dismiss, never the reverse, which is the same rule the
    curation prompt states: the reverse loses the fact if anything fails between
    the two steps. So this returns a failure and the caller keeps the bullet.

    The guide is resolved through ``agent_root``, so before the re-rooting this
    writes the shared guide (and the row's ``leak_warning`` is why the UI asks
    for confirmation first) and afterwards that workspace's own.
    """
    from ciao.memory_tool import ensure_regions, resolve_region as _resolve, update_region

    region = _resolve(row.get("region") or row["kind"])
    limit = int(
        getattr(config, "memory_char_limit", 2200)
        if region == "memory"
        else getattr(config, "user_char_limit", 1375)
    )
    guide = Path(config.agent_root(row["workspace"])) / "CLAUDE.md"
    try:
        # A guide with no region markers yet is not a reason to refuse a
        # promotion — a workspace can be newer than its last skill sync. This is
        # the same call sync makes, and it is a no-op once the markers are there.
        ensure_regions(guide)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"could not prepare {guide}: {exc}", "region": region}
    try:
        result = update_region(
            guide, region, action="add", entry=row["text"], char_limit=limit
        )
    except (ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc), "region": region}
    return {"ok": True, "region": region, "usage": result.get("usage", {})}


async def proposal_action(request: Request) -> JSONResponse:
    """Accept or dismiss exactly one proposal by its stable id.

    ``accept`` PERFORMS the promotion: a memory/profile row is written into that
    workspace's bounded region, then the bullet is dropped. Write-then-dismiss,
    never the reverse — if the write fails (over cap, unreadable guide) the bullet
    stays and the error comes back, because the reverse order loses the fact.

    ``dismiss`` drops the bullet without writing anything. Unknown id is 404.
    """
    config = request.app.state.config
    pid = request.path_params["id"]
    _rows, by_id = _scan_proposal_rows(config)
    ctx = by_id.get(pid)
    if ctx is None:
        return JSONResponse({"error": f"unknown proposal id: {pid}"}, status_code=404)
    action = request.path_params.get("action", "").strip()
    row = ctx["row"]

    if ctx.get("file"):
        # A whole file, not a bullet in a queue: the line-removal path below
        # would read it and delete line -1 of it.
        if action != "dismiss":
            return JSONResponse(
                {
                    "error": "a skill proposal is a file, so there is nothing to "
                             "promote; open it and turn it into a skill, or dismiss it",
                    "id": pid,
                },
                status_code=400,
            )
        outcome = _dismiss_skill_proposal(ctx)
        if not outcome.get("ok"):
            return JSONResponse({"error": outcome["error"], "id": pid}, status_code=409)
        return JSONResponse({"id": pid, "action": "dismiss", "dismissed": True})

    promoted: dict[str, Any] = {}
    if action == "accept":
        accept = proposal_kinds.accept_for(row["kind"])
        if accept.action == "move_file":
            target, error = _rehome_target(row, request.query_params.get("workspace", "").strip())
            if error:
                return JSONResponse({"error": error, "id": pid}, status_code=400)
            # Off the event loop: the sweep reads and rewrites notes across both
            # vaults, and doing that inline blocked the loop long enough for the
            # request to time out — after the git mv and before the queue row was
            # dropped, so the note moved and its row stayed.
            outcome = await asyncio.to_thread(_perform_rehome_move, config, row, target)
            if not outcome.get("ok"):
                # Move-then-dismiss, the same order as a region write: the bullet
                # survives a failed move so the note is not silently left where it
                # was with nothing recording that it should not be.
                return JSONResponse(
                    {"error": outcome["error"], "id": pid}, status_code=409
                )
            promoted = outcome
        elif accept.action == "edit_region":
            promoted = _promote_region_row(config, row)
            if not promoted.get("ok"):
                # The bullet is untouched, so the fact is still queued and the
                # operator can fix the cause (usually an over-cap region) and
                # retry. Losing it silently is the one outcome to avoid.
                return JSONResponse(
                    {
                        "error": promoted.get("error", "could not write the region"),
                        "id": pid,
                        "region": promoted.get("region", ""),
                    },
                    status_code=409,
                )

    queue = Path(ctx["path"])
    lines = queue.read_text(encoding="utf-8").splitlines()
    line_index = ctx["line"]
    if 0 <= line_index < len(lines):
        del lines[line_index]
    queue.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    if action == "accept":
        accept = proposal_kinds.accept_for(row["kind"])
        result = {"id": pid, "action": accept.action, "dismissed": True}
        if accept.action == "edit_region":
            result["region"] = promoted.get("region", accept.region)
            result["promoted"] = True
            result["usage"] = promoted.get("usage", {})
            result["leak_warning"] = row.get("leak_warning", False)
        else:
            # Rehome: the note itself is not moved here. Moving a file and
            # rewriting every reference to it is `vault_rehome`'s job and it is
            # reversible through its own receipt; doing half of it from a queue
            # row would leave the links pointing at a path that moved.
            result["promoted"] = False
            result["destination"] = row.get("rehome", {}).get("destination", "")
            result["justified"] = row.get("rehome", {}).get("justified", False)
        return JSONResponse({"ok": True, "result": result})
    return JSONResponse({"ok": True, "result": {"id": pid, "action": "dismiss", "dismissed": True}})


# ── Operator-action housekeeping strip ───────────────────────────────────


def _housekeeping_context(request: Request) -> "operator_actions.DetectionContext":
    """Build the cheap detection context from the request's app state.

    The package-status fetcher is the cached one the app already owns (see
    ``make_cached_package_status`` in ``app.py``), so detection never blocks
    on GitHub. The schedule manager, when present, exposes the missed one-time
    reminders.
    """
    from ciao import operator_actions

    config = request.app.state.config
    fetcher = getattr(request.app.state, "package_status_fetcher", None)
    return operator_actions.DetectionContext(
        config=config,
        schedule_store=getattr(request.app.state, "schedule_manager", None),
        package_status=fetcher if callable(fetcher) else None,
    )


async def list_housekeeping(request: Request) -> JSONResponse:
    """Return every detectable operator action for the home strip.

    This is the detector pass. Each action carries ``run_label``, ``chat_label``
    and ``chat_prompt`` so the client can render the buttons it needs and seed
    a chat without a second round-trip.
    """
    from ciao import operator_actions

    actions = operator_actions.detect_actions(_housekeeping_context(request))
    return JSONResponse({"actions": [action.as_dict() for action in actions]})


async def run_housekeeping_action(request: Request) -> JSONResponse:
    """Perform one action's mechanical work, then re-detect and return the list.

    Re-running detection in the same response is what keeps the client from
    rendering a stale strip: a condition that cleared is gone, and one that
    persists returns with its detail replaced by the failure. Unknown id is
    404, never 500.
    """
    from ciao import operator_actions

    action_id = request.path_params["action_id"]
    context = _housekeeping_context(request)
    try:
        result, summary = operator_actions.run_action(action_id, context)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except Exception as exc:  # noqa: BLE001 — a failed run is a tile, not a crash
        logger.exception("operator action %s failed", action_id)
        actions = operator_actions.detect_actions(context)
        # The condition persisted, so the same id is still detected. Replace its
        # detail with the failure text so the client shows a failed tile rather
        # than silently re-offering the button as though nothing happened.
        failure = str(exc)
        return JSONResponse(
            {
                "ok": False,
                "action_id": action_id,
                "error": failure,
                "summary": f"Run failed: {failure}",
                "actions": [
                    action.as_dict()
                    if action.id != action_id
                    else {
                        **action.as_dict(),
                        "detail": f"Run failed: {failure}",
                    }
                    for action in actions
                ],
            }
        )
    actions = operator_actions.detect_actions(context)
    return JSONResponse(
        {
            "ok": True,
            "action_id": action_id,
            "result": result,
            "summary": summary,
            "actions": [action.as_dict() for action in actions],
        }
    )
