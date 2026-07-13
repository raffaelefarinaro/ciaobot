"""REST API routes for the PWA."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import hmac
import shlex
import shutil
import subprocess
import sys
from dataclasses import asdict, replace
from datetime import datetime, timedelta, UTC
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from ciao import subagent_tracking
from ciao.config import WorkspaceConfig, CLAUDE_AI_CONNECTORS, coerce_claude_ai_mcps
from ciao.models import THINKING_LEVELS, ChatContext
from ciao.model_tiers import codex_tier_models
from ciao.package_version import package_changelog, package_status, update_package
from ciao.tool_path import login_shell_path, resolve_tool
from ciao.providers.claude import _summarize_tool_input
from ciao.providers.codex import CodexProvider, codex_login_status
from ciao.provider_service import supported_providers
from ciao.schedules import (
    ScheduleEntry,
    compute_last_expected_run,
    compute_next_run,
    normalize_archive_policy,
    was_dispatched_since,
)
from ciao.setup_status import setup_status
from ciao.cli import _auth_command_for_provider
from ciao.skills_inventory import build_skill_inventory
from ciao.web.auth import SESSION_COOKIE, session_cookie_kwargs
from ciao.web.chat_broker import extract_file_touch
from ciao.web.project_chats import (
    _PROJECT_UPLOAD_MAX_BYTES,
    _normalize_handover_messages,
)
from ciao.web.routes_helpers import (
    _allowed_roots,
    _commit_and_push,
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

_WORKSPACE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_WORKSPACE_PROVIDER_LABELS = {
    "claude": "Claude",
    "codex": "Codex",
    "ollama": "Ollama",
    "openrouter": "OpenRouter",
}
_PROVIDER_KEY_META = {
    "CIAO_OLLAMA_API_KEY": {
        "label": "Ollama Cloud API key",
        "description": "Routes configured Ollama cloud models directly through ollama.com.",
    },
    "OPENROUTER_API_KEY": {
        "label": "OpenRouter API key",
        "description": "Optional key for critique/review model routing.",
    },
}
_SERVICE_KEY_META = {
    "OPENAI_API_KEY": {
        "label": "OpenAI voice API key",
        "description": "Used directly by Ciaobot for cloud transcription and speech, not for Codex login.",
    },
}
_GWS_BUILTIN_PROFILES = ("personal", "work")
_GWS_PROFILE_META = {
    "personal": {
        "label": "Personal Google account",
        "purpose": "Private Gmail, Calendar, and Tasks. Keep this separate from company systems.",
        "examples": ["Gmail", "Calendar", "Tasks"],
    },
    "work": {
        "label": "Work Google account",
        "purpose": "Company Drive, Docs, Sheets, Slides, Gmail, Calendar, and Tasks.",
        "examples": ["Drive", "Docs", "Sheets", "Slides", "Gmail", "Calendar"],
    },
}
_GWS_AUTH_FILES = ("credentials.json", "credentials.enc")


def _known_workspace_names(pcm: object) -> set[str]:
    config = getattr(pcm, "_config", None)
    workspace_names = getattr(config, "workspace_names", None)
    if callable(workspace_names):
        names = {str(name) for name in workspace_names() if str(name)}
        if names:
            return names
    return {"personal", "work"}


def _ollama_cloud_available(config) -> bool:
    ollama = getattr(config, "ollama", None)
    if ollama is None:
        return False
    return bool(getattr(ollama, "api_key", "")) and ollama.api_key != "ollama"


def _ollama_backend_available(config) -> bool:
    ollama = getattr(config, "ollama", None)
    if ollama is None:
        return False
    return bool(ollama.local_models) or _ollama_cloud_available(config)


def _openrouter_model_options(config) -> list[str]:
    openrouter = config.openrouter
    if not openrouter.available:
        return []
    return list(
        dict.fromkeys(
            [
                openrouter.haiku_model,
                openrouter.sonnet_model,
                openrouter.opus_model,
                *openrouter.models,
            ]
        )
    )


def _ollama_cloud_model_options(config) -> list[str]:
    ollama = config.ollama
    tier_models = (
        [ollama.haiku_model, ollama.sonnet_model, ollama.opus_model, ollama.title_model]
        if _ollama_cloud_available(config)
        else []
    )
    return list(dict.fromkeys([*ollama.models, *tier_models]))


def _workspace_provider_options(config) -> list[dict[str, str]]:
    values = ["claude", "codex"]
    if _ollama_backend_available(config):
        values.append("ollama")
    openrouter = getattr(config, "openrouter", None)
    if openrouter is not None and openrouter.available:
        values.append("openrouter")
    return [
        {"value": value, "label": _WORKSPACE_PROVIDER_LABELS[value]}
        for value in values
    ]


def _workspace_provider_values(config) -> set[str]:
    return {option["value"] for option in _workspace_provider_options(config)}


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


def _extract_assistant_blocks(raw: object) -> list[dict]:
    """Return ordered text/tool_use blocks for an assistant message.

    Items: {"kind": "text", "text": str} or
           {"kind": "tool_use", "name": str, "summary": str,
            "file_touch": {file_path, action} | None}.
    ``file_touch`` is populated when the tool mutates a file on disk so the
    PWA can render an inline file card on reload instead of the generic
    activity row.
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
        elif btype == "tool_use":
            name = block.get("name", "")
            tinput = block.get("input") or {}
            if not isinstance(tinput, dict):
                tinput = {}
            summary = _summarize_tool_input(name, tinput)
            touch = extract_file_touch(name, tinput)
            entry = {"kind": "tool_use", "name": name, "summary": summary}
            if touch:
                entry["file_touch"] = touch
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
_INTERRUPTED_REQUEST_SENTINEL = "[Request interrupted by user]"


def _is_no_response_sentinel(text: str) -> bool:
    return text.strip() == _NO_RESPONSE_SENTINEL


def _is_interrupted_request_sentinel(text: str) -> bool:
    return text.strip() == _INTERRUPTED_REQUEST_SENTINEL


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
                    touch = blk.get("file_touch")
                    if touch:
                        flush_tools()
                        rendered.append({
                            "role": "system",
                            "tool_name": "_filecard",
                            "content": touch["file_path"],
                            "file_path": touch["file_path"],
                            "action": touch["action"],
                            "tool": name,
                        })
                        continue
                    line = f"{_tool_icon(name)} {name}"
                    if summary:
                        line += f" {summary}"
                    pending_tools.append(line)
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


def _local_session_jsonl_paths(session_id: str, workspace_root: Path) -> list[Path]:
    """Find local Claude Code JSONL files for ``session_id``."""
    try:
        from ciao.transcripts import _claude_projects_dir
    except ImportError:
        return []
    paths: list[Path] = []
    preferred = _claude_projects_dir(workspace_root) / f"{session_id}.jsonl"
    if preferred.exists():
        paths.append(preferred)
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


def _local_subagent_transcripts(session_id: str, workspace_root: Path) -> list[dict]:
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

    for path in _local_session_jsonl_paths(session_id, workspace_root):
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

# Simple in-memory rate limiter for auth_login: max 10 attempts per IP per minute.
_login_attempts: dict[str, list[tuple[float, int]]] = {}
_MAX_LOGIN_ATTEMPTS = 10
_LOGIN_WINDOW_SECONDS = 60


def _check_login_rate_limit(client_ip: str) -> bool:
    """Return True if the IP is within the rate limit, False if blocked."""
    now = datetime.now(UTC).timestamp()
    window_start = now - _LOGIN_WINDOW_SECONDS
    entries = _login_attempts.get(client_ip, [])
    # Drop stale entries
    entries = [(t, c) for (t, c) in entries if t > window_start]
    total = sum(c for (_t, c) in entries)
    if total >= _MAX_LOGIN_ATTEMPTS:
        _login_attempts[client_ip] = entries
        return False
    entries.append((now, 1))
    _login_attempts[client_ip] = entries
    return True


async def auth_login(request: Request) -> JSONResponse:
    app = request.app
    client_ip = request.client.host if request.client else "unknown"
    if not _check_login_rate_limit(client_ip):
        return JSONResponse({"error": "rate limited"}, status_code=429)
    body = await request.json()
    token = body.get("token", "")
    if not hmac.compare_digest(token, app.state.config.pwa_auth_token):
        return JSONResponse({"error": "invalid token"}, status_code=401)
    signed = app.state.serializer.dumps({"user": "owner"})
    response = JSONResponse({"ok": True})
    response.set_cookie(SESSION_COOKIE, signed, **_session_cookie_kwargs(request))
    return response


def _session_cookie_kwargs(request: Request) -> dict:
    return session_cookie_kwargs(request)


async def auth_logout(request: Request) -> JSONResponse:
    response = JSONResponse({"ok": True})
    cookie_kwargs = _session_cookie_kwargs(request)
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        domain=cookie_kwargs.get("domain"),
        secure=bool(cookie_kwargs.get("secure")),
        httponly=True,
        samesite="lax",
    )
    return response


async def auth_check(request: Request) -> JSONResponse:
    # Bootstrap mode must land the browser on the setup wizard. The wizard
    # lives in the login view, and with auth off by default nothing would
    # ever route there — the SPA would open straight into the app on the
    # throwaway bootstrap workspace. Report unauthenticated until setup
    # finishes so the router redirects to /login → first-run wizard.
    config = getattr(request.app.state, "config", None)
    if getattr(config, "bootstrap_mode", False):
        return JSONResponse({"error": "setup required"}, status_code=401)
    return JSONResponse({"ok": True})


# ── Projects ─────────────────────────────────────────────────────────────


async def list_workspaces(request: Request) -> JSONResponse:
    """Return configured logical workspaces for the PWA sidebar."""
    config = request.app.state.config
    return JSONResponse(_workspaces_payload(config))


def _workspace_to_dict(workspace: WorkspaceConfig) -> dict:
    return {
        "name": getattr(workspace, "name", ""),
        "vault_root": getattr(workspace, "vault_root", ""),
        "default_provider": getattr(workspace, "default_provider", "claude"),
        "default_model": getattr(workspace, "default_model", ""),
        "disallowed_tools": (
            list(getattr(workspace, "disallowed_tools", None))
            if getattr(workspace, "disallowed_tools", None) is not None
            else None
        ),
        # claude.ai connector MCP toggle. null = per-workspace default
        # (personal off, else on). The effective denylist is computed by
        # ``CiaoConfig.disallowed_tools_for_workspace``.
        "claude_ai_mcps": getattr(workspace, "claude_ai_mcps", None),
        "gws_profile": getattr(workspace, "gws_profile", ""),
        "model_bucket": getattr(workspace, "model_bucket", ""),
    }


def _workspaces_payload(config) -> dict:
    workspaces = [_workspace_to_dict(workspace) for workspace in config.workspaces.values()]
    return {
        "workspaces": workspaces,
        "active": workspaces[0]["name"] if workspaces else None,
        # App-wide fallback when a workspace's default_model is empty, so the
        # PWA can label "Inherit default (<model>)" instead of a vague hint.
        "app_default_model": getattr(config, "claude_default_model", "") or "",
        "provider_options": _workspace_provider_options(config),
        # The claude.ai connector set the toggle controls, so the PWA can label
        # the switch without hardcoding tool names.
        "claude_ai_connectors": list(CLAUDE_AI_CONNECTORS),
    }


def _parse_disallowed_tools_value(raw: object) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        if raw.strip().lower() == "default":
            return None
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    raise ValueError("disallowed_tools must be a list, comma-separated string, or null")


def _workspace_from_request(
    data: dict,
    *,
    config,
    existing: WorkspaceConfig | None = None,
) -> WorkspaceConfig:
    name = str(data.get("name", existing.name if existing else "")).strip()
    if not _WORKSPACE_NAME_RE.match(name):
        raise ValueError("workspace name must use letters, numbers, dashes, or underscores")
    provider = str(
        data.get(
            "default_provider",
            existing.default_provider if existing else "claude",
        )
    ).strip() or "claude"
    available_providers = _workspace_provider_values(config)
    if provider not in available_providers:
        allowed = ", ".join(sorted(available_providers))
        raise ValueError(f"default_provider must be one of: {allowed}")
    if "disallowed_tools" in data:
        disallowed_tools = _parse_disallowed_tools_value(data.get("disallowed_tools"))
    elif existing is not None:
        disallowed_tools = existing.disallowed_tools
    else:
        disallowed_tools = None
    if "claude_ai_mcps" in data:
        claude_ai_mcps = coerce_claude_ai_mcps(data.get("claude_ai_mcps"))
    elif existing is not None:
        claude_ai_mcps = existing.claude_ai_mcps
    else:
        claude_ai_mcps = None
    return WorkspaceConfig(
        name=name,
        vault_root=str(data.get("vault_root", existing.vault_root if existing else name)).strip()
        or name,
        default_provider=provider,
        default_model=str(
            data.get("default_model", existing.default_model if existing else "")
        ).strip(),
        disallowed_tools=disallowed_tools,
        claude_ai_mcps=claude_ai_mcps,
        gws_profile=str(data.get("gws_profile", existing.gws_profile if existing else "")).strip(),
        model_bucket=str(
            data.get("model_bucket", existing.model_bucket if existing else "")
        ).strip(),
    )


def _workspaces_path(config) -> Path:
    return Path(config.state_path).resolve().parent / "workspaces.json"


def _persist_workspaces(config) -> None:
    path = _workspaces_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [_workspace_to_dict(workspace) for workspace in config.workspaces.values()]
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


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
    if key == "OPENAI_API_KEY" and bool(getattr(config, "openai_api_key", None)):
        return "api_key"
    if key == "CIAO_OLLAMA_API_KEY" and getattr(config.ollama, "api_key", "ollama") != "ollama":
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
        "connections": {
            key: providers[key]
            for key in ("claude", "codex")
            if key in providers
        },
    }


def _launch_provider_login(config, provider: str) -> tuple[bool, str]:
    """Open the provider-owned interactive login in macOS Terminal."""
    if provider not in {"claude", "codex"}:
        raise ValueError(f"unsupported provider '{provider}'")
    command = _auth_command_for_provider(provider)
    rendered = shlex.join(command)
    if sys.platform != "darwin":
        return False, rendered
    runtime_root = Path(config.runtime_root)
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
    if provider not in {"claude", "codex"}:
        return JSONResponse({"error": "unsupported provider"}, status_code=404)
    if action == "connect":
        try:
            opened, command = await asyncio.to_thread(_launch_provider_login, config, provider)
        except (FileNotFoundError, OSError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "opened": opened, "command": command}, status_code=202)
    if action == "verify":
        payload = await asyncio.to_thread(_provider_config_payload, config)
        return JSONResponse(payload["connections"].get(provider, {}))
    if action == "logout":
        try:
            command = _auth_command_for_provider(provider)[:1] + ["logout"]
            run = await asyncio.to_thread(
                subprocess.run,
                command,
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
    for key, value in updates.items():
        value = value.strip()
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
        if key == "OPENAI_API_KEY":
            config.openai_api_key = value or None
        elif key == "CIAO_OLLAMA_API_KEY":
            if value:
                base_url = os.environ.get("CIAO_OLLAMA_URL", "").strip() or "https://ollama.com"
                config.ollama = replace(config.ollama, api_key=value, base_url=base_url)
            else:
                base_url = os.environ.get("CIAO_OLLAMA_URL", "").strip() or "http://localhost:11434"
                config.ollama = replace(config.ollama, api_key="ollama", base_url=base_url)


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
    root = Path(config.workspace_root).resolve()
    if profile == "personal":
        return root / "secrets" / "gws-personal"
    if profile == "work":
        return root / "secrets" / "gws"
    # Wizard-named workspaces carry their own profile names; each gets its
    # own credentials directory alongside the legacy two.
    safe = re.sub(r"[^a-z0-9_-]+", "-", profile.strip().lower()).strip("-")
    if not safe:
        return None
    return root / "secrets" / f"gws-{safe}"


def _gws_file_present(config_dir: Path | None, names: tuple[str, ...]) -> bool:
    if config_dir is None:
        return False
    return any((config_dir / name).is_file() for name in names)


def _gws_profile_usage(config) -> dict[str, list[str]]:
    default_profile = getattr(config, "gws_default_profile", "personal") or "personal"
    usage: dict[str, list[str]] = {}
    for workspace in config.workspaces.values():
        profile = (getattr(workspace, "gws_profile", "") or default_profile).strip()
        if not profile:
            profile = default_profile
        usage.setdefault(profile, []).append(getattr(workspace, "name", ""))
    return usage


def _gws_profile_names(config) -> list[str]:
    names = list(_GWS_BUILTIN_PROFILES)
    default_profile = getattr(config, "gws_default_profile", "personal") or "personal"
    for profile in [default_profile, *_gws_profile_usage(config).keys()]:
        profile = str(profile).strip()
        if profile and profile not in names:
            names.append(profile)
    return names


def _extract_email_from_id_token(id_token: str | None) -> str:
    if not id_token:
        return ""
    try:
        import base64
        import json
        parts = id_token.split(".")
        if len(parts) >= 2:
            payload_b64 = parts[1]
            payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
            payload_json = base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8")
            payload = json.loads(payload_json)
            return payload.get("email") or ""
    except Exception:
        pass
    return ""


def _gws_profile_payload(config, profile: str, usage: dict[str, list[str]]) -> dict:
    meta = _GWS_PROFILE_META.get(
        profile,
        {
            "label": f"{profile} Google profile",
            "purpose": "Custom Google Workspace profile configured outside the built-in personal/work wrapper.",
            "examples": [],
        },
    )
    config_dir = _gws_profile_config_dir(config, profile)
    credentials_present = _gws_file_present(config_dir, _GWS_AUTH_FILES)
    client_secret_present = _gws_file_present(config_dir, ("client_secret.json",))
    wrapper_path = Path(config.workspace_root).resolve() / "scripts" / "gws-profile.sh"
    helper_path = Path(config.workspace_root).resolve() / "scripts" / "gws-auth-helper.py"
    setup_command = ""
    headless_auth_command = ""
    if profile in _GWS_BUILTIN_PROFILES:
        setup_command = f"scripts/gws-profile.sh {profile} auth login --full"
        headless_auth_command = f"python3 scripts/gws-auth-helper.py {profile}"

    email = ""
    if config_dir:
        creds_path = config_dir / "credentials.json"
        if creds_path.is_file():
            try:
                with open(creds_path, "r", encoding="utf-8") as f:
                    creds_data = json.load(f)
                email = creds_data.get("email") or ""
            except Exception:
                pass

    return {
        "name": profile,
        "label": meta["label"],
        "purpose": meta["purpose"],
        "examples": meta["examples"],
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
    }


def _gws_integration_payload(config) -> dict:
    usage = _gws_profile_usage(config)
    binary_path = resolve_tool("gws") or ""
    wrapper_path = Path(config.workspace_root).resolve() / "scripts" / "gws-profile.sh"
    helper_path = Path(config.workspace_root).resolve() / "scripts" / "gws-auth-helper.py"
    return {
        "installed": bool(binary_path),
        "binary_path": binary_path,
        "default_profile": getattr(config, "gws_default_profile", "personal") or "personal",
        "wrapper_path": str(wrapper_path) if wrapper_path.is_file() else "",
        "headless_helper_path": str(helper_path) if helper_path.is_file() else "",
        "profiles": [
            _gws_profile_payload(config, profile, usage)
            for profile in _gws_profile_names(config)
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

    if profile not in _GWS_BUILTIN_PROFILES:
        return JSONResponse({"error": f"Invalid profile: {profile}"}, status_code=400)

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

    if profile not in _GWS_BUILTIN_PROFILES:
        return JSONResponse({"error": f"Invalid profile: {profile}"}, status_code=400)

    config_dir = _gws_profile_config_dir(config, profile)
    if config_dir is None:
        return JSONResponse({"error": "Could not determine config directory"}, status_code=500)

    secret_path = config_dir / "client_secret.json"
    if not secret_path.is_file():
        return JSONResponse({"error": "client_secret.json not found for this profile"}, status_code=400)

    try:
        with open(secret_path, "r", encoding="utf-8") as f:
            secret = json.load(f)
        
        installed = secret.get("installed") or secret.get("web")
        if not installed:
            return JSONResponse({"error": "client_secret.json missing 'installed' or 'web' section"}, status_code=400)

        client_id = installed.get("client_id")
        redirect_uris = installed.get("redirect_uris", ["http://localhost"])
        redirect_uri = redirect_uris[0]
        
        if not client_id:
            return JSONResponse({"error": "client_secret.json missing client_id"}, status_code=400)

        scopes = (
            "https://www.googleapis.com/auth/gmail.modify "
            "https://www.googleapis.com/auth/calendar "
            "https://www.googleapis.com/auth/tasks "
            "openid "
            "https://www.googleapis.com/auth/userinfo.email "
            "https://www.googleapis.com/auth/userinfo.profile"
        )
        if profile == "work":
            scopes = (
                "https://www.googleapis.com/auth/drive "
                "https://www.googleapis.com/auth/spreadsheets "
                "https://www.googleapis.com/auth/gmail.modify "
                "https://www.googleapis.com/auth/calendar "
                "https://www.googleapis.com/auth/documents "
                "https://www.googleapis.com/auth/presentations "
                "https://www.googleapis.com/auth/tasks "
                "openid "
                "https://www.googleapis.com/auth/userinfo.email "
                "https://www.googleapis.com/auth/userinfo.profile"
            )

        import urllib.parse
        params = {
            "scope": scopes,
            "access_type": "offline",
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "client_id": client_id,
            "prompt": "select_account consent",
        }
        auth_url = "https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode(params)
        return JSONResponse({"auth_url": auth_url})
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

    if profile not in _GWS_BUILTIN_PROFILES:
        return JSONResponse({"error": f"Invalid profile: {profile}"}, status_code=400)

    if not code_or_url:
        return JSONResponse({"error": "Missing authorization code or redirect URL"}, status_code=400)

    config_dir = _gws_profile_config_dir(config, profile)
    if config_dir is None:
        return JSONResponse({"error": "Could not determine config directory"}, status_code=500)

    secret_path = config_dir / "client_secret.json"
    if not secret_path.is_file():
        return JSONResponse({"error": "client_secret.json not found for this profile"}, status_code=400)

    try:
        with open(secret_path, "r", encoding="utf-8") as f:
            secret = json.load(f)
        
        installed = secret.get("installed") or secret.get("web")
        if not installed:
            return JSONResponse({"error": "client_secret.json missing 'installed' or 'web' section"}, status_code=400)

        client_id = installed.get("client_id")
        client_secret = installed.get("client_secret")
        redirect_uris = installed.get("redirect_uris", ["http://localhost"])
        redirect_uri = redirect_uris[0]

        if not client_id or not client_secret:
            return JSONResponse({"error": "client_secret.json missing client_id or client_secret"}, status_code=400)

        code = code_or_url.strip()
        if "code=" in code or code.startswith("http"):
            import urllib.parse
            parsed = urllib.parse.urlparse(code)
            query = urllib.parse.parse_qs(parsed.query)
            if "error" in query:
                return JSONResponse({"error": f"Google returned error: {query['error'][0]}"}, status_code=400)
            if "code" not in query:
                return JSONResponse({"error": "No authorization 'code' found in the redirect URL"}, status_code=400)
            code = query["code"][0]

        import urllib.request
        import urllib.error
        import urllib.parse
        
        data = urllib.parse.urlencode(
            {
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        def _do_exchange():
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            tokens = await asyncio.to_thread(_do_exchange)
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8")
                err_json = json.loads(err_body)
                err_desc = err_json.get("error_description") or err_json.get("error") or "Unknown OAuth error"
            except Exception:
                err_desc = f"HTTP {e.code}"
            return JSONResponse({"error": f"Token exchange failed: {err_desc}"}, status_code=400)
        except Exception as e:
            return JSONResponse({"error": f"Token exchange failed: {str(e)}"}, status_code=400)

        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            return JSONResponse({
                "error": "No refresh token returned. The account might already be authorized. "
                         "Please revoke the old grant at https://myaccount.google.com/permissions and try again."
            }, status_code=400)

        email = _extract_email_from_id_token(tokens.get("id_token"))

        creds = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "type": "authorized_user",
        }
        if email:
            creds["email"] = email

        creds_path = config_dir / "credentials.json"
        
        for name in ("credentials.enc", "token_cache.json"):
            stale = config_dir / name
            if stale.exists():
                backup = config_dir / (name + ".old")
                try:
                    if backup.exists():
                        backup.unlink()
                    stale.rename(backup)
                except Exception as e:
                    logger.warning(f"Failed to move stale {name}: {e}")

        creds_path.write_text(json.dumps(creds, indent=2), encoding="utf-8")
        creds_path.chmod(0o600)

        key_file = config_dir / ".encryption_key"
        if key_file.exists():
            try:
                key_file.chmod(0o600)
            except Exception as e:
                logger.warning(f"Failed to fix .encryption_key permissions: {e}")

        return JSONResponse(_gws_integration_payload(config))
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

    if profile not in _GWS_BUILTIN_PROFILES:
        return JSONResponse({"error": f"Invalid profile: {profile}"}, status_code=400)

    config_dir = _gws_profile_config_dir(config, profile)
    if config_dir is None:
        return JSONResponse({"error": "Could not determine config directory"}, status_code=500)

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



async def list_projects(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    workspace = request.query_params.get("workspace")
    projects = pcm.list_projects(workspace)
    return JSONResponse([p.to_dict() for p in projects])


async def create_project(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    body = await request.json()
    project = pcm.create_project(
        name=body["name"],
        workspace=body.get("workspace", "personal"),
        context=body.get("context", ""),
    )
    return JSONResponse(project.to_dict(), status_code=201)


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
            model_bucket=body.get("model_bucket"),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse(chat.to_dict(local=True), status_code=201)


# ── Chats ────────────────────────────────────────────────────────────────

def _codex_reasoning_levels(catalog: list[dict]) -> dict[str, list[str]]:
    """Per-model reasoning levels from the codex catalog, tier aliases included."""
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
    for tier, model_id in codex_tier_models(catalog).items():
        levels[tier] = list(levels.get(model_id, []))
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
        ok = pcm.delete_chat(chat_id)
        return JSONResponse({"ok": ok})
    # PATCH
    body = await request.json()
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
            model_bucket=body.get("model_bucket"),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if chat is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(chat.to_dict(local=pcm.is_session_local(chat)))


async def chat_new_session(request: Request) -> JSONResponse:
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    chat = pcm.new_session(chat_id)
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
            model_bucket=str(body.get("model_bucket", "")).strip(),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if chat is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(chat.to_dict(local=pcm.is_session_local(chat)))


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
    outcome = pcm.archive_chat(chat_id)
    if outcome is not None:
        pcm.run_archive_postprocess(chat_id, outcome, chat_meta, project_meta)
    return JSONResponse({
        "ok": True,
        "archived_to": str(outcome.path) if outcome is not None else None,
    })


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
                result.append(entry)
                user_idx += 1
                continue
            if kind == "agentMessage":
                flush_tools()
                text = str(item.get("text") or "").strip()
                if text:
                    entry = {"role": "assistant", "content": text}
                    phase = str(item.get("phase") or "")
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
                        result.append({
                            "role": "system",
                            "tool_name": "_filecard",
                            "content": file_path,
                            "file_path": file_path,
                            "action": str(change.get("kind") or "edited"),
                            "tool": "Edit",
                        })
                continue
            if kind == "commandExecution":
                command = item.get("command")
                if isinstance(command, list):
                    label = " ".join(str(part) for part in command)
                else:
                    label = str(command or "")
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


def _overlay_codex_transcript_metadata(
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


async def chat_messages(request: Request) -> JSONResponse:
    """Return conversation history for a chat.

    Claude chats read the SDK session file via ``get_session_messages``.

    When a Claude chat is archived, its SDK session blob is deleted to reclaim
    disk space. In that case we fall back to the durable markdown transcript in
    the vault so the PWA can still render the conversation read-only.
    """
    pcm = request.app.state.project_chat_manager
    chat_id = request.path_params["chat_id"]
    chat = pcm.get_chat(chat_id)
    if chat is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    handover_messages = list(getattr(chat, "handover_messages", []) or [])
    if not chat.session_id:
        return JSONResponse(handover_messages)

    config = request.app.state.config
    if getattr(chat, "provider", "claude") == "codex":
        thread = await CodexProvider.read_thread(
            config.workspace_root, chat.session_id
        )
        if thread is not None:
            rendered = _render_codex_thread(thread, chat)
            if rendered:
                _overlay_codex_transcript_metadata(
                    rendered,
                    pcm._transcripts.current_messages(
                        ChatContext.for_web(chat_id), "codex"
                    ),
                )
                return JSONResponse(handover_messages + rendered)
        current = pcm._transcripts.current_messages(
            ChatContext.for_web(chat_id), "codex"
        )
        if current:
            _overlay_assistant_timings(current, chat.user_turn_timings)
            return JSONResponse(handover_messages + current)

    try:
        from claude_agent_sdk import get_session_messages
    except ImportError:
        return JSONResponse({"error": "SDK not available"}, status_code=500)

    result: list[dict] = []
    try:
        msgs = get_session_messages(
            chat.session_id,
            directory=str(config.workspace_root),
        )
    except (FileNotFoundError, ValueError):
        # Session file doesn't exist on this machine (remote chat) or was
        # deleted after archiving. If the chat is archived and has a transcript
        # path, render the durable markdown record instead.
        msgs = None

    if msgs is None:
        if chat.archived and chat.archive_path:
            archive_path = Path(chat.archive_path)
            if not archive_path.is_absolute():
                archive_path = config.workspace_root / archive_path
            try:
                text = archive_path.read_text(encoding="utf-8")
                parsed = pcm._parse_transcript_messages(text)
                parsed = _normalize_handover_messages(parsed)
                # Map transcript timestamp field to the frontend's sent_at key.
                for entry in parsed:
                    if "timestamp" in entry and "sent_at" not in entry:
                        entry["sent_at"] = entry["timestamp"]
                _overlay_assistant_timings(parsed, chat.user_turn_timings)
                return JSONResponse(handover_messages + parsed)
            except OSError:
                logger.warning(
                    "Failed to read archived transcript for %s at %s",
                    chat_id,
                    archive_path,
                )
        return JSONResponse(handover_messages)

    user_idx = 0
    for m in msgs:
        if m.type == "assistant":
            blocks = _extract_assistant_blocks(m.message)
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
            if not tool_blocks and len(text_blocks) == 1:
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
                    touch = blk.get("file_touch")
                    if touch:
                        flush_tools()
                        result.append({
                            "role": "system",
                            "tool_name": "_filecard",
                            "content": touch["file_path"],
                            "file_path": touch["file_path"],
                            "action": touch["action"],
                            "tool": name,
                        })
                        continue
                    line = f"{_tool_icon(name)} {name}"
                    if summary:
                        line += f" {summary}"
                    pending_tools.append(line)
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

    workspace = str(config.workspace_root)

    def _finalize(entries: list[dict]) -> JSONResponse:
        _merge_subagent_dispatch_meta(
            entries, chat.session_id, Path(config.workspace_root)
        )
        return JSONResponse(entries)

    try:
        from claude_agent_sdk import get_subagent_messages, list_subagents
    except ImportError:
        return _finalize(_local_subagent_transcripts(chat.session_id, Path(config.workspace_root)))

    try:
        agent_ids = list_subagents(chat.session_id, directory=workspace)
    except (FileNotFoundError, ValueError):
        return _finalize(_local_subagent_transcripts(chat.session_id, Path(config.workspace_root)))
    except Exception:  # noqa: BLE001 — defensive against SDK surprises
        return _finalize(_local_subagent_transcripts(chat.session_id, Path(config.workspace_root)))

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
        result = _local_subagent_transcripts(chat.session_id, Path(config.workspace_root))

    return _finalize(result)


def _merge_subagent_dispatch_meta(
    entries: list[dict], session_id: str, workspace_root: Path
) -> None:
    """Attach dispatch metadata from the parent session JSONL in place."""
    if not entries:
        return
    path = subagent_tracking.find_parent_session_file(session_id, workspace_root)
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


# Binary downloads (PDFs, ZIPs, office docs) live under their own endpoint so
# the text and image viewers stay strictly typed. Same (unrestricted) path
# contract as ``workspace_file``/``workspace_image``: any allowlisted-extension
# file on disk is served, relative paths anchoring to the workspace. The browser
# decides whether to render inline (PDF) or save (everything else) based on
# the inferred MIME type; we set ``Content-Disposition: inline`` with the
# original filename so downloads keep a sensible name either way.
_WORKSPACE_BINARY_EXTS = frozenset({
    ".pdf", ".zip", ".docx", ".xlsx", ".pptx",
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
    """Serve a read-only binary file (PDF, ZIP, office doc) from the workspace."""
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

    is_raw = request.query_params.get("raw") == "1"
    filename = resolved.name

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
                result = await asyncio.to_thread(
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
                if result.returncode != 0:
                    return JSONResponse(
                        {"error": f"LibreOffice conversion failed: {result.stderr or result.stdout}"},
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

    # `inline` lets PDFs preview in a tab; non-renderable types still
    # download but with a sensible filename. We set custom frame headers
    # to allow embedding inside the PWA's same-origin file viewer iframe,
    # bypassing the global middleware's default frame-blocking headers.
    headers = {
        "Content-Disposition": f'inline; filename="{filename}"',
        "X-Frame-Options": "SAMEORIGIN",
        "Content-Security-Policy": "; ".join(
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
        ),
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
    web_project_id = entry_dict.get("web_project_id")
    web_chat_id = entry_dict.get("web_chat_id")
    if web_project_id and pcm:
        project = pcm.get_project(web_project_id)
        entry_dict["context_label"] = f"{project.name} (new chat per run)" if project else web_project_id
    elif web_chat_id and pcm:
        chat = pcm.get_chat(web_chat_id)
        entry_dict["context_label"] = chat.title if chat else web_chat_id
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
    schedules = sm.list()
    return JSONResponse([_enrich_schedule(s, pcm) for s in schedules])


async def list_automation(request: Request) -> JSONResponse:
    """Status of background automations for the Settings → Automation page.

    Reads the job-run log and returns one entry per known job (jobs that
    never ran still appear), each with its last run, recent history, and
    aggregate stats. Read-only.
    """
    from ciao import job_runs

    return JSONResponse(job_runs.automation_summary())


async def create_schedule(request: Request) -> JSONResponse:
    sm = request.app.state.schedule_manager
    state = request.app.state.state_store
    pcm = request.app.state.project_chat_manager
    body = await request.json()

    web_chat_id = body.get("web_chat_id")
    web_project_id = body.get("web_project_id")

    # Resolve model/mode: explicit override wins; otherwise pick the
    # per-workspace default for the schedule's project (so personal
    # schedules can default to Ollama, work to Anthropic). Falls back
    # to the globally-selected model when no project is attached.
    ctx = ChatContext(chat_id=0)
    explicit_model = (body.get("model") or "").strip()
    if explicit_model:
        model = explicit_model
    elif web_project_id:
        model = pcm.schedule_default_model(web_project_id)
    else:
        model = state.get_selected_model(ctx)
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
    if workspace not in known_workspaces and web_project_id:
        project = pcm.get_project(web_project_id)
        workspace = project.workspace if project else ""
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
        archive_policy=archive_policy,
        workspace=workspace if workspace in known_workspaces else "",
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
        # Re-stamp the workspace to match the new target project so the
        # stale-id fallback keeps routing to the right General project.
        pcm = request.app.state.project_chat_manager
        project = pcm.get_project(entry.web_project_id) if entry.web_project_id else None
        entry.workspace = project.workspace if project else ""
    if "workspace" in body:
        ws = (body["workspace"] or "").strip().lower()
        pcm = request.app.state.project_chat_manager
        entry.workspace = ws if ws in _known_workspace_names(pcm) else ""
    if "model" in body:
        # Empty string means "reset to current default"; otherwise use the
        # provided value. Matches create_schedule semantics so a UI PATCH
        # can't silently produce a schedule with model="" that then falls to
        # whatever the dispatcher interprets as the default.
        new_model = (body["model"] or "").strip()
        if not new_model:
            state = request.app.state.state_store
            new_model = state.get_selected_model(ChatContext(chat_id=0))
        entry.model = new_model
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
    if not web_chat_id or pcm.get_chat(web_chat_id) is None:
        return JSONResponse({"error": "web_chat_id must point to an existing chat"}, status_code=400)
    try:
        interval_minutes = int(body.get("interval_minutes", 10))
    except (TypeError, ValueError):
        return JSONResponse({"error": "interval_minutes must be an integer"}, status_code=400)
    if interval_minutes < 1:
        return JSONResponse({"error": "interval_minutes must be >= 1"}, status_code=400)

    entry = lm.create(
        prompt=prompt,
        web_chat_id=web_chat_id,
        interval_minutes=interval_minutes,
        title=(body.get("title") or "").strip(),
        autostart=bool(body.get("autostart")),
    )
    if body.get("start"):
        lm.start_loop(entry.loop_id)
    return JSONResponse(_enrich_loop(entry, lm, pcm), status_code=201)


async def loop_detail(request: Request) -> JSONResponse:
    """Handle PATCH (update / start / stop) and DELETE for a single loop."""
    loop_id = request.path_params["loop_id"]
    lm = request.app.state.loop_manager
    pcm = request.app.state.project_chat_manager
    if request.method == "DELETE":
        return JSONResponse({"ok": lm.delete(loop_id)})
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
            lm.start_loop(loop_id)
        else:
            lm.stop_loop(loop_id)
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
    codex_catalog = await CodexProvider.model_catalog(config.workspace_root)
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
    codex_tiers = codex_tier_models(codex_catalog)
    model_reasoning_levels = _codex_reasoning_levels(codex_catalog)
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
    # Cloud allowlist + locally-discovered daemon models both count as
    # "Ollama" for bucketing: they show in the personal Claude bucket,
    # never in the work (Anthropic subscription) bucket.
    ollama_cloud = _ollama_cloud_model_options(config)
    ollama = list(dict.fromkeys([*ollama_cloud, *config.ollama.local_models]))
    claude_work = [m for m in config.claude_models if m not in ollama]
    claude_personal = [m for m in config.claude_models if m in ollama]

    work_default = config.claude_default_model if config.claude_default_model in claude_work else (claude_work[0] if claude_work else "")
    personal_default = claude_personal[0] if claude_personal else ""

    # OpenRouter backend: available when an API key is set. The picker
    # offers the per-tier alias defaults plus any discovered/disabled
    # anthropic-family models.
    or_settings = config.openrouter
    openrouter_models = _openrouter_model_options(config)
    openrouter_tiers = {
        "haiku": or_settings.haiku_model,
        "sonnet": or_settings.sonnet_model,
        "opus": or_settings.opus_model,
    } if or_settings.available else {}
    openrouter_default = or_settings.sonnet_model if or_settings.available else ""

    return JSONResponse({
        "models": config.claude_models,
        "default": config.claude_default_model,
        "provider_models": {
            "claude_work": claude_work,
            "claude_personal": claude_personal,
            "openrouter": openrouter_models,
            "codex": codex_models,
        },
        "provider_defaults": {
            "claude_work": work_default,
            "claude_personal": personal_default,
            "openrouter": openrouter_default,
            "codex": codex_default,
        },
        # Per-backend tier models, so the picker can show
        # "sonnet -> kimi (ollama) / gpt-5.6-terra (codex)". Tier names are
        # resolved to provider-native ids only at the dispatch boundary.
        "alias_tiers": {
            "ollama": {
                "haiku": config.ollama.haiku_model,
                "sonnet": config.ollama.sonnet_model,
                "opus": config.ollama.opus_model,
            },
            "openrouter": openrouter_tiers,
            "codex": codex_tiers,
        },
        "backends": {
            "ollama": _ollama_backend_available(config),
            "openrouter": or_settings.available,
            "anthropic": True,
            "codex": bool(codex_models),
        },
        "ollama_models": ollama,
        "ollama_local_models": list(config.ollama.local_models),
        "openrouter_models": openrouter_models,
        "codex_models": codex_models,
        "codex_model_metadata": codex_model_metadata,
        "model_reasoning_levels": model_reasoning_levels,
        "thinking_levels": {k: list(v) for k, v in THINKING_LEVELS.items()},
    })


# ── Routine settings (Settings → Models tab) ────────────────────────────

def _routines_payload(config, app_settings) -> dict:
    """Shared GET/PATCH response: overrides, effective values, options."""
    import shutil
    from ciao.voice import kokoro_available, mlx_whisper_available

    s = app_settings.settings
    ollama = config.ollama
    if config.title_model_override:
        title_effective = config.title_model_override
    elif shutil.which("apfel") is not None:
        title_effective = "apfel"
    else:
        title_effective = config.haiku_model_for_workspace("personal")
    from ciao.critique import critique_models_effective

    critique_effective = critique_models_effective(config)
    if config.insights_model_override:
        insights_effective = config.insights_model_override
    else:
        insights_effective = config.sonnet_model_for_workspace("personal")

    return {
        # Overrides as stored ("" = automatic default).
        "title_model": s.title_model,
        "insights_model": s.insights_model,

        "critique_models": s.critique_models,
        "ollama_haiku_model": s.ollama_haiku_model,
        "ollama_sonnet_model": s.ollama_sonnet_model,
        "ollama_opus_model": s.ollama_opus_model,
        "openrouter_haiku_model": s.openrouter_haiku_model,
        "openrouter_sonnet_model": s.openrouter_sonnet_model,
        "openrouter_opus_model": s.openrouter_opus_model,
        # What actually runs right now, after defaults.
        "title_model_effective": title_effective,
        "insights_model_effective": insights_effective,

        "critique_models_effective": critique_effective,
        "alias_tiers": {
            "ollama": {
                "haiku": config.ollama.haiku_model,
                "sonnet": config.ollama.sonnet_model,
                "opus": config.ollama.opus_model,
            },
            "openrouter": {
                "haiku": config.openrouter.haiku_model,
                "sonnet": config.openrouter.sonnet_model,
                "opus": config.openrouter.opus_model,
            } if config.openrouter.available else {},
            "codex": {"haiku": "luna", "sonnet": "terra", "opus": "sol"},
        },
        "transcription": {
            "engine": config.transcription_engine,
            "local_model": config.transcription_local_model,
            "local_available": mlx_whisper_available(),
            "cloud_available": bool(config.openai_api_key),
        },
        "speech": {
            "engine": config.tts_engine,
            "cloud_voice": config.tts_cloud_voice,
            "local_voice": config.tts_local_voice,
            "local_available": kokoro_available(),
            "cloud_available": bool(config.openai_api_key),
        },
        # Grouped options for the routine model selectors.
        "model_options": {
            "anthropic": ["haiku", "sonnet", "opus"],
            "ollama_cloud": _ollama_cloud_model_options(config),
            "ollama_local": list(ollama.local_models),
            "openrouter": _openrouter_model_options(config),
        },
        "backends": {
            "ollama": _ollama_backend_available(config),
            "openrouter": config.openrouter.available,
            "anthropic": True,
        },
        "workspace_context": {
            "workspace_root": str(config.workspace_root),
            "vault_root": str(config.vault_root),
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
    if request.method == "GET":
        # Re-discover local daemon models so a freshly `ollama pull`-ed
        # model appears in the selectors without a restart. Bounded by the
        # discovery timeout (2s) and run off the event loop.
        from ciao.config import (
            refresh_local_ollama_models,
            refresh_cloud_ollama_models,
            refresh_openrouter_models,
        )

        await asyncio.to_thread(refresh_local_ollama_models, config)
        await asyncio.to_thread(refresh_cloud_ollama_models, config)
        await asyncio.to_thread(refresh_openrouter_models, config)
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
    return JSONResponse(_routines_payload(config, app_settings))


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
    """Return startup phase progress."""
    from ciao import __version__

    tracker = getattr(request.app.state, "startup_tracker", None)
    if tracker is None:
        return JSONResponse({"phases": [], "overall_ready": True, "version": __version__})
    return JSONResponse({**tracker.to_dict(), "version": __version__})


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
    ids = set(pcm.active_stream_chat_ids())
    ids.update(pcm.background_agent_counts)
    return JSONResponse({"active_chat_ids": sorted(ids)})


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
    pcm.events.publish({"type": "open_chat", "chat_id": chat_id})
    return JSONResponse({"ok": True, "chat_id": chat_id})


async def setup_status_endpoint(request: Request) -> JSONResponse:
    """Return first-run setup readiness for the onboarding wizard."""
    return JSONResponse(setup_status(request.app.state.config))


async def package_status_endpoint(request: Request) -> JSONResponse:
    """Return installed package version and best-effort update status."""
    fetcher = getattr(request.app.state, "package_status_fetcher", None)
    if callable(fetcher):
        return JSONResponse(await asyncio.to_thread(fetcher))
    return JSONResponse(await asyncio.to_thread(package_status))


async def package_changelog_endpoint(request: Request) -> JSONResponse:
    """Return the commits between the installed and latest release for the update modal."""
    fetcher = getattr(request.app.state, "package_status_fetcher", None)
    status = await asyncio.to_thread(fetcher if callable(fetcher) else package_status)
    current = str(status.get("current_version") or "")
    latest = str(status.get("latest_version") or "")
    changelog = await asyncio.to_thread(
        package_changelog,
        current_version=current,
        latest_version=latest,
    )
    return JSONResponse(
        {
            "current_version": current,
            "latest_version": latest,
            "update_available": bool(status.get("update_available")),
            **changelog,
        }
    )


async def package_update_endpoint(request: Request) -> JSONResponse:
    """Perform package update and restart the server on success."""
    res = await asyncio.to_thread(update_package)
    if res.get("ok"):
        config = request.app.state.config
        async def _do_restart():
            await asyncio.sleep(2)
            fn = getattr(request.app.state, "request_restart", None)
            if callable(fn):
                fn(config.restart_exit_code)
            else:
                from ciao.signals import RestartRequested
                raise RestartRequested(config.restart_exit_code)

        asyncio.create_task(_do_restart())
        return JSONResponse(res)
    else:
        status_code = 400 if res.get("mode") in {"editable", "unknown"} else 500
        return JSONResponse(res, status_code=status_code)


async def voice_install_local_endpoint(request: Request) -> JSONResponse:
    """Install local voice transcription dependencies (mlx-whisper)."""
    return await _pip_install_and_restart(request, "mlx-whisper>=0.4.0")


async def tts_install_local_endpoint(request: Request) -> JSONResponse:
    """Install local speech synthesis dependencies (kokoro-onnx)."""
    return await _pip_install_and_restart(request, "kokoro-onnx>=0.5.0")


async def _pip_install_and_restart(request: Request, requirement: str) -> JSONResponse:
    """pip-install one requirement into the running env, then restart."""
    import sys
    import subprocess

    cmd = [sys.executable, "-m", "pip", "install", requirement]
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
        output = (result.stdout.strip() + "\n" + result.stderr.strip()).strip()
        if result.returncode == 0:
            config = request.app.state.config
            async def _do_restart():
                await asyncio.sleep(2)
                fn = getattr(request.app.state, "request_restart", None)
                if callable(fn):
                    fn(config.restart_exit_code)
                else:
                    from ciao.signals import RestartRequested
                    raise RestartRequested(config.restart_exit_code)

            asyncio.create_task(_do_restart())
            return JSONResponse({"ok": True, "output": output})
        else:
            return JSONResponse(
                {"ok": False, "error": f"Command exited with code {result.returncode}", "output": output},
                status_code=500,
            )
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


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
    """True when this server was started from an interactive terminal."""
    try:
        return sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


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
    if default_provider not in _WORKSPACE_PROVIDER_LABELS:
        return JSONResponse(
            {"error": f"unknown provider '{default_provider}'"}, status_code=400
        )

    from ciao.cli import detect_vault_mode, setup_workspace

    # The wizard no longer asks scratch-vs-existing: when the request does
    # not pin a mode, inspect the folder — empty starts from scratch, one
    # with visible content is an existing notes folder the onboarding agent
    # adapts in place.
    vault_mode = str(body.get("vault_mode", "")).strip().lower() or detect_vault_mode(workspace)

    written = setup_workspace(
        workspace,
        auth_token=str(body.get("auth_token", "")).strip() or config.pwa_auth_token,
        auth_required=bool(body.get("auth_required", False)),
        push_contact=push_contact,
        vault_root=str(body.get("vault_root", "")).strip() or None,
        vault_mode=vault_mode,
        workspace_name=str(body.get("workspace_name", "")).strip() or "personal",
        default_provider=default_provider,
        python_path=str(body.get("python", "")).strip() or None,
        port=port,
        launch_agents_dir=str(body.get("launch_agents_dir", "")).strip() or None,
        app_dir=str(body.get("app_dir", "")).strip() or None,
    )
    # Hand the chosen workspace to the relaunched process. A foreground
    # `ciao run` restarts by re-execing itself with the current environment,
    # and nothing else tells the fresh process where setup landed — without
    # this it boots straight back into the bootstrap wizard.
    os.environ["CIAO_WORKSPACE"] = str(Path(workspace).expanduser().resolve())
    os.environ["PWA_PORT"] = str(port)
    # Best-effort: bring the menu bar companion up right away so setup ends
    # with the icon visible instead of waiting for the next login. Only for
    # the real per-user LaunchAgents dir — scripted/test setups pass a custom
    # dir and must not register anything with launchd.
    real_launch_agents = (
        sys.platform == "darwin"
        and not str(body.get("launch_agents_dir", "")).strip()
    )
    if real_launch_agents:
        menubar_plist = Path.home() / "Library" / "LaunchAgents" / "com.ciao.menubar.plist"
        if menubar_plist.exists():
            try:
                loaded = subprocess.run(
                    ["launchctl", "kickstart", f"gui/{os.getuid()}/com.ciao.menubar"],
                    capture_output=True, timeout=10,
                )
                if loaded.returncode != 0:
                    subprocess.run(
                        ["launchctl", "load", "-w", str(menubar_plist)],
                        capture_output=True, timeout=10,
                    )
            except (OSError, subprocess.SubprocessError):
                logger.info("Could not start the menu bar agent; it will load at next login.")

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





def _run_step(args: list[str], *, cwd: str, timeout: int) -> subprocess.CompletedProcess:
    """Run a deploy step, turning missing-binary and timeout errors into a
    failed CompletedProcess so the handler reports a structured error instead
    of crashing with a 500. Under launchd the server PATH may omit Homebrew,
    so a bare ``npm`` can raise FileNotFoundError before ``run`` returns."""
    try:
        return subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(
            args=args, returncode=127, stdout="",
            stderr=f"{args[0]} not found on PATH: {exc}",
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=args, returncode=124, stdout="",
            stderr=f"{args[0]} timed out after {timeout}s",
        )


def _run_root_npm_install(codebase_root: Path) -> subprocess.CompletedProcess:
    args = ["npm", "install", "--no-audit", "--no-fund"]
    if not (codebase_root / "package.json").exists():
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="skipped: no root package.json",
            stderr="",
        )
    return _run_step(args, cwd=str(codebase_root), timeout=180)


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
    codebase_root = Path(__file__).resolve().parents[2]
    steps = []

    def _record(step: str, result: subprocess.CompletedProcess) -> dict:
        out = (result.stdout.strip() or result.stderr.strip())[:500]
        return {"step": step, "ok": result.returncode == 0, "output": out}

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

    # 1. Git pull (idempotent after snapshot, but catches any race push)
    result = await asyncio.to_thread(
        _run_step, ["git", "pull"], cwd=str(codebase_root), timeout=60,
    )
    steps.append(_record("git pull", result))
    if result.returncode != 0:
        return JSONResponse(
            {"steps": steps, "ok": False, "error": f"git pull failed: {steps[-1]['output']}"},
            status_code=500,
        )

    # 2. pip install
    import sys
    result = await asyncio.to_thread(
        _run_step, [sys.executable, "-m", "pip", "install", "-e", "."],
        cwd=str(codebase_root), timeout=120,
    )
    steps.append(_record("pip install", result))
    if result.returncode != 0:
        return JSONResponse(
            {"steps": steps, "ok": False, "error": f"pip install failed: {steps[-1]['output']}"},
            status_code=500,
        )

    # 2b. npm install at repo root, only when a root package exists. The PWA's
    # package.json lives under web/, so running npm at the repo root on this
    # project would otherwise emit ENOENT on every deploy.
    result = await asyncio.to_thread(
        _run_root_npm_install, codebase_root,
    )
    steps.append(_record("npm install (root)", result))

    # 3. npm build
    web_dir = codebase_root / "web"
    result = await asyncio.to_thread(
        _run_step, ["npm", "run", "build"],
        cwd=str(web_dir), timeout=120,
    )
    steps.append(_record("npm build", result))
    if result.returncode != 0:
        return JSONResponse(
            {"steps": steps, "ok": False, "error": f"npm build failed: {steps[-1]['output']}"},
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
        fn = getattr(request.app.state, "request_restart", None)
        if callable(fn):
            fn(config.restart_exit_code)
        else:
            raise RestartRequested(config.restart_exit_code)

    asyncio.create_task(_do_restart())
    steps.append({"step": "restart", "ok": True})

    return JSONResponse({"steps": steps, "ok": True})


async def admin_skills(request: Request) -> JSONResponse:
    """List skills known to Ciaobot, labelled as custom or GitHub/package."""
    config = request.app.state.config
    return JSONResponse(build_skill_inventory(config.workspace_root))


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
    projects = pcm.list_projects("personal")
    project = next((p for p in projects if p.name == "General"), None)
    if project is None:
        return {"error": "no personal project to host the merge chat"}

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
