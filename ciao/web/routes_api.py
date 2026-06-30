"""REST API routes for the PWA."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import hmac
import subprocess
from dataclasses import asdict, replace
from datetime import datetime, timedelta, UTC
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from ciao.config import WorkspaceConfig
from ciao.models import THINKING_LEVELS, ChatContext
from ciao.package_version import package_status, update_package
from ciao.providers.claude import _summarize_tool_input
from ciao.schedules import (
    ScheduleEntry,
    compute_last_expected_run,
    compute_next_run,
    normalize_archive_policy,
    was_dispatched_since,
)
from ciao.setup_status import setup_status
from ciao.skills_inventory import build_skill_inventory
from ciao.web.auth import SESSION_COOKIE, session_cookie_kwargs
from ciao.web.chat_broker import extract_file_touch
from ciao.web.project_chats import _normalize_handover_messages

logger = logging.getLogger(__name__)

from ciao.web.routes_helpers import (
    _allowed_roots,
    _resolve_workspace_path,
    _commit_and_push,
)

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
_ALLOWED_CHAT_PROVIDERS = {"claude", "pi"}
_PROVIDER_KEY_META = {
    "ANTHROPIC_API_KEY": {
        "label": "Anthropic API key",
        "description": "Fallback key for Claude provider auth when OAuth is not used.",
    },
    "OPENAI_API_KEY": {
        "label": "OpenAI API key",
        "description": "Used by cloud voice transcription and other OpenAI-backed features.",
    },
    "CIAO_OLLAMA_API_KEY": {
        "label": "Ollama Cloud API key",
        "description": "Routes configured Ollama cloud models directly through ollama.com.",
    },
    "OPENROUTER_API_KEY": {
        "label": "OpenRouter API key",
        "description": "Optional key for critique/review model routing.",
    },
}


def _known_workspace_names(pcm: object) -> set[str]:
    config = getattr(pcm, "_config", None)
    workspace_names = getattr(config, "workspace_names", None)
    if callable(workspace_names):
        names = {str(name) for name in workspace_names() if str(name)}
        if names:
            return names
    return {"personal", "work"}


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
    if first_line.startswith("Agent ") and " completed" in first_line:
        # Already shaped like 'Agent "X" completed'; pass it through.
        return f"{icon} {first_line}"
    if first_line:
        # Trim aggressively so the bubble stays one line; full output lives in
        # the subagent transcript fetchable via /api/chats/{id}/subagents.
        snippet = first_line if len(first_line) <= 120 else first_line[:117] + "..."
        return f"{icon} Subagent {status}: {snippet}"
    return f"{icon} Subagent {status}"


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
    return JSONResponse({"ok": True})


# ── Projects ─────────────────────────────────────────────────────────────


async def list_workspaces(request: Request) -> JSONResponse:
    """Return configured logical workspaces for the PWA sidebar."""
    config = request.app.state.config
    workspaces = [
        {
            "name": getattr(workspace, "name", ""),
            "vault_root": getattr(workspace, "vault_root", ""),
            "default_provider": getattr(workspace, "default_provider", "claude"),
            "default_model": getattr(workspace, "default_model", ""),
            "gws_profile": getattr(workspace, "gws_profile", ""),
            "model_bucket": getattr(workspace, "model_bucket", ""),
            "disallowed_tools": getattr(workspace, "disallowed_tools", None),
        }
        for workspace in config.workspaces.values()
    ]
    active = workspaces[0]["name"] if workspaces else None
    return JSONResponse({"workspaces": workspaces, "active": active})


def _workspace_to_dict(workspace: WorkspaceConfig) -> dict:
    return {
        "name": workspace.name,
        "vault_root": workspace.vault_root,
        "default_provider": workspace.default_provider,
        "default_model": workspace.default_model,
        "disallowed_tools": (
            list(workspace.disallowed_tools)
            if workspace.disallowed_tools is not None
            else None
        ),
        "gws_profile": workspace.gws_profile,
        "model_bucket": workspace.model_bucket,
    }


def _workspaces_payload(config) -> dict:
    workspaces = [_workspace_to_dict(workspace) for workspace in config.workspaces.values()]
    return {
        "workspaces": workspaces,
        "active": workspaces[0]["name"] if workspaces else None,
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


def _workspace_from_request(data: dict, *, existing: WorkspaceConfig | None = None) -> WorkspaceConfig:
    name = str(data.get("name", existing.name if existing else "")).strip()
    if not _WORKSPACE_NAME_RE.match(name):
        raise ValueError("workspace name must use letters, numbers, dashes, or underscores")
    provider = str(
        data.get(
            "default_provider",
            existing.default_provider if existing else "claude",
        )
    ).strip() or "claude"
    if provider not in _ALLOWED_CHAT_PROVIDERS:
        raise ValueError("default_provider must be either 'claude' or 'pi'")
    if "disallowed_tools" in data:
        disallowed_tools = _parse_disallowed_tools_value(data.get("disallowed_tools"))
    elif existing is not None:
        disallowed_tools = existing.disallowed_tools
    else:
        disallowed_tools = None
    return WorkspaceConfig(
        name=name,
        vault_root=str(data.get("vault_root", existing.vault_root if existing else name)).strip()
        or name,
        default_provider=provider,
        default_model=str(
            data.get("default_model", existing.default_model if existing else "")
        ).strip(),
        disallowed_tools=disallowed_tools,
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
        workspace = _workspace_from_request(body, existing=existing)
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


def _provider_key_configured(config, key: str) -> bool:
    env_value = os.environ.get(key, "").strip()
    if env_value:
        return True
    file_value = _read_env_value(_env_path(config), key)
    if file_value:
        return True
    if key == "OPENAI_API_KEY":
        return bool(getattr(config, "openai_api_key", None))
    if key == "CIAO_OLLAMA_API_KEY":
        return getattr(config.ollama, "api_key", "ollama") != "ollama"
    return False


def _provider_config_payload(config) -> dict:
    return {
        "keys": {
            key: {
                **meta,
                "configured": _provider_key_configured(config, key),
            }
            for key, meta in _PROVIDER_KEY_META.items()
        },
        "requires_restart": True,
        "env_path": str(_env_path(config)),
    }


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
        return JSONResponse(_provider_config_payload(config))
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict) or not isinstance(body.get("keys"), dict):
        return JSONResponse({"error": "expected object with keys"}, status_code=400)
    updates = {str(key): str(value) for key, value in body["keys"].items()}
    unsupported = sorted(set(updates) - set(_PROVIDER_KEY_META))
    if unsupported:
        return JSONResponse(
            {"error": f"unsupported provider key(s): {', '.join(unsupported)}"},
            status_code=400,
        )
    _write_env_values(_env_path(config), updates)
    _apply_provider_key_updates(config, updates)
    return JSONResponse(_provider_config_payload(config))


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
        data = await upload.read()
        filename = getattr(upload, "filename", "") or ""
        try:
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


def _pi_session_dir() -> Path:
    override = os.environ.get("CIAO_PI_SESSION_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".pi" / "agent" / "sessions"


def _pi_user_text(content: object) -> str:
    """Flatten a Pi user/toolResult content field (string or block array) to text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)


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


def _clean_user_content(content: str) -> str:
    if "<USER_REQUEST>" in content:
        parts = content.split("<USER_REQUEST>", 1)
        if "</USER_REQUEST>" in parts[1]:
            content = parts[1].split("</USER_REQUEST>", 1)[0]
    return _strip_injected_context(content)



def _read_pi_session_messages(
    chat_id: str,
    timings: dict | None = None,
    user_turn_images: dict | None = None,
) -> list[dict]:
    """Read the Pi RPC transcript for ``chat_id`` and map to the FE shape.

    Pi writes one JSONL per session under ``~/.pi/agent/sessions/<chat_id>/``;
    when a chat has multiple files (resumes/branches), the most-recently
    modified one is the live transcript. Returns an empty list when there is
    no session yet (chat created but Pi never spawned successfully, or the
    binary is missing).

    Output shape mirrors what ``chat_messages`` returns from Claude sessions:
    user/assistant bubbles plus collapsed ``_activity`` tool groups. Pi-only
    entries the FE has no place for (``branchSummary``, ``compactionSummary``,
    ``custom``, ``bashExecution``, ``thinking``) are dropped, matching how the
    Claude reader drops thinking/control-ack noise.
    """
    chat_dir = _pi_session_dir() / chat_id
    if not chat_dir.exists():
        return []
    files = sorted(chat_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return []

    timings = timings or {}
    user_turn_images = user_turn_images or {}
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

    try:
        with files[0].open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "message":
                    continue
                msg = entry.get("message") or {}
                role = msg.get("role")
                if role == "user":
                    flush_tools()
                    text = _pi_user_text(msg.get("content")).strip()
                    if not text:
                        continue
                    user_entry: dict = {
                        "role": "user",
                        "content": text,
                        "turn_index": user_idx,
                    }
                    refs = user_turn_images.get(str(user_idx))
                    if refs is None:
                        refs = user_turn_images.get(user_idx)
                    if refs:
                        user_entry["images"] = list(refs)
                    rec = timings.get(str(user_idx)) or timings.get(user_idx)
                    if isinstance(rec, dict) and rec.get("sent_at"):
                        user_entry["sent_at"] = rec["sent_at"]
                    result.append(user_entry)
                    user_idx += 1
                elif role == "assistant":
                    for block in (msg.get("content") or []):
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            text = (block.get("text") or "").strip()
                            if text:
                                flush_tools()
                                result.append({"role": "assistant", "content": text})
                        elif btype == "toolCall":
                            name = block.get("name") or "tool"
                            args = block.get("arguments") or {}
                            touch = extract_file_touch(name, args) if isinstance(args, dict) else None
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
                            summary = _summarize_tool_input(name, args) if isinstance(args, dict) else ""
                            line_text = f"{_tool_icon(name)} {name}"
                            if summary:
                                line_text += f" {summary}"
                            pending_tools.append(line_text)
                        # "thinking" blocks: hidden, like the Claude reader.
                # Other roles (toolResult, branchSummary, compactionSummary,
                # custom, bashExecution) don't get their own bubble — the
                # toolCall line already represents what the user needs to see.
    except OSError:
        return []
    flush_tools()
    _overlay_assistant_timings(result, timings)
    return result


async def chat_messages(request: Request) -> JSONResponse:
    """Return conversation history for a chat.

    Routes by provider: Claude chats read the SDK session file via
    ``get_session_messages``; Pi chats read Pi's own JSONL transcript under
    ``~/.pi/agent/sessions/<chat_id>/`` since the SDK can't parse them.

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
    if chat.provider == "pi":
        return JSONResponse(
            handover_messages + _read_pi_session_messages(
                chat_id,
                chat.user_turn_timings,
                chat.user_turn_images,
            )
        )
    if not chat.session_id:
        return JSONResponse(handover_messages)

    try:
        from claude_agent_sdk import get_session_messages
    except ImportError:
        return JSONResponse({"error": "SDK not available"}, status_code=500)

    config = request.app.state.config
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

    try:
        from claude_agent_sdk import get_subagent_messages, list_subagents
    except ImportError:
        return JSONResponse({"error": "SDK not available"}, status_code=500)

    config = request.app.state.config
    workspace = str(config.workspace_root)

    try:
        agent_ids = list_subagents(chat.session_id, directory=workspace)
    except (FileNotFoundError, ValueError):
        return JSONResponse([])
    except Exception:  # noqa: BLE001 — defensive against SDK surprises
        return JSONResponse([])

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

        rendered: list[dict] = []
        for m in msgs:
            if m.type == "assistant":
                blocks = _extract_assistant_blocks(m.message)
                # Drop the CLI's interrupted-turn sentinel (mirrors
                # chat_messages above).
                blocks = [
                    b for b in blocks
                    if not (b["kind"] == "text" and _is_no_response_sentinel(b["text"]))
                ]
                if not blocks:
                    continue
                pending_tools: list[str] = []

                def flush_tools():
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

            content = _extract_text_content(m.message).strip()
            if not content:
                continue
            if _is_no_response_sentinel(content):
                continue
            rendered.append({"role": m.type, "content": content})

        result.append({"agent_id": agent_id, "messages": rendered})

    return JSONResponse(result)


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

    data = await upload.read()
    filename = getattr(upload, "filename", "audio.webm") or "audio.webm"

    try:
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
        data = await upload.read()
        filename = getattr(upload, "filename", "image.jpg") or "image.jpg"
        try:
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
    """Serve a read-only text file from inside the workspace.

    Path is provided as a query string (`?path=...`). The path may be
    workspace-relative or absolute, with an optional `:line` suffix that is
    stripped. All results canonicalise via ``Path.resolve()`` and must land
    under ``config.workspace_root`` or one of ``config.extra_workspace_roots``
    (e.g. ``~/repos`` for project-linked repos) — anything else returns 403.
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
# the text and image viewers stay strictly typed. Same sandbox contract as
# ``workspace_file``/``workspace_image``: path lands under
# ``config.workspace_root`` after canonicalisation or we refuse. The browser
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
                "connect-src 'self' ws: wss:",
            ]
        ),
    }
    return FileResponse(
        resolved,
        media_type=media_type,
        headers=headers,
    )


async def workspace_image(request: Request) -> Response:
    """Serve a read-only image from inside the workspace.

    Same sandbox contract as ``workspace_file``: path lands under
    ``config.workspace_root`` after canonicalisation or we refuse. Extension
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

    # Sandbox the write: the path the agent gave us at edit time may have
    # been absolute or relative, but restoration MUST land inside an allowed
    # root. Anything else (e.g. /etc/passwd if the agent claimed to write
    # there during a malicious turn) refuses.
    config = request.app.state.config
    roots = _allowed_roots(config)
    try:
        candidate = Path(file_path).expanduser()
        resolved = candidate.resolve() if candidate.is_absolute() else (roots[0] / candidate).resolve()
    except (OSError, ValueError):
        return JSONResponse({"error": "bad path"}, status_code=400)
    if not any(resolved.is_relative_to(root) for root in roots):
        return JSONResponse({"error": "forbidden"}, status_code=403)

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
        # we want to allow creating new files inside the sandbox too, but
        # only if the path canonicalises under a root. Recheck explicitly.
        try:
            candidate = Path(raw_path).expanduser()
            resolved = candidate.resolve() if candidate.is_absolute() else (roots[0] / candidate).resolve()
        except (OSError, ValueError):
            return JSONResponse({"error": "bad path"}, status_code=400)
        if not any(resolved.is_relative_to(root) for root in roots):
            return JSONResponse({"error": "forbidden"}, status_code=403)
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
    if provider and provider not in ("claude", "pi"):
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
        if new_provider and new_provider not in ("claude", "pi"):
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


# ── Models ───────────────────────────────────────────────────────────────

async def list_models(request: Request) -> JSONResponse:
    config = request.app.state.config
    # Cloud allowlist + locally-discovered daemon models both count as
    # "Ollama" for bucketing: they show in the personal Claude bucket and
    # in the Pi bucket, never in the work (Anthropic subscription) bucket.
    ollama = list(
        dict.fromkeys([*config.ollama.models, *config.ollama.local_models])
    )
    pi_native = list(config.pi.models)
    pi_only = [m for m in pi_native if m not in ollama]
    claude_work = [m for m in config.claude_models if m not in ollama and m not in pi_only]
    claude_personal = [m for m in config.claude_models if m in ollama]
    pi_personal = list(dict.fromkeys([*pi_native, *ollama]))

    work_default = config.claude_default_model if config.claude_default_model in claude_work else (claude_work[0] if claude_work else "")
    personal_default = claude_personal[0] if claude_personal else ""
    pi_default = config.pi.default_model or (pi_personal[0] if pi_personal else "")

    return JSONResponse({
        "models": config.claude_models,
        "default": config.claude_default_model,
        "provider_models": {
            "claude_work": claude_work,
            "claude_personal": claude_personal,
            "pi_personal": pi_personal,
        },
        "provider_defaults": {
            "claude_work": work_default,
            "claude_personal": personal_default,
            "pi_personal": pi_default,
        },
        "ollama_models": ollama,
        # Subset of ollama_models served by the local daemon (free,
        # on-device). The picker can badge these as "local".
        "ollama_local_models": list(config.ollama.local_models),
        # Keyed by provider (not bucket): both Claude buckets share the SDK's
        # effort levels. Empty selection = provider default, no flag sent.
        "thinking_levels": {k: list(v) for k, v in THINKING_LEVELS.items()},
    })


# ── Routine settings (Settings → Models tab) ────────────────────────────

def _routines_payload(config, app_settings) -> dict:
    """Shared GET/PATCH response: overrides, effective values, options."""
    import shutil
    from ciao.voice import mlx_whisper_available

    s = app_settings.settings
    ollama = config.ollama
    if config.title_model_override:
        title_effective = config.title_model_override
    elif shutil.which("apfel") is not None:
        title_effective = "apfel"
    elif ollama.models:
        title_effective = ollama.title_model
    else:
        title_effective = config.title_model
    if config.critique_models:
        critique_effective = config.critique_models
    elif os.environ.get("OPENROUTER_API_KEY"):
        critique_effective = "openrouter/anthropic/claude-3.7-sonnet,openrouter/google/gemini-2.5-pro,openrouter/minimax/minimax-m3,openrouter/zai/glm-5.2"
    else:
        critique_effective = "openai-codex/gpt-5.5,kimi-k2.7-code:cloud,deepseek-v4-pro:cloud"

    return {
        # Overrides as stored ("" = automatic default).
        "title_model": s.title_model,
        "insights_model": s.insights_model,
        "critique_models": s.critique_models,
        # What actually runs right now, after defaults.
        "title_model_effective": title_effective,
        "insights_model_effective": config.insights_model,
        "critique_models_effective": critique_effective,
        "transcription": {
            "engine": config.transcription_engine,
            "local_model": config.transcription_local_model,
            "local_available": mlx_whisper_available(),
            "cloud_available": bool(config.openai_api_key),
        },
        # Grouped options for the routine model selectors.
        "model_options": {
            "anthropic": ["haiku", "sonnet", "opus"],
            "ollama_cloud": list(ollama.models),
            "ollama_local": list(ollama.local_models),
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
        from ciao.config import refresh_local_ollama_models

        await asyncio.to_thread(refresh_local_ollama_models, config)
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
    tracker = getattr(request.app.state, "startup_tracker", None)
    if tracker is None:
        return JSONResponse({"phases": [], "overall_ready": True})
    return JSONResponse(tracker.to_dict())


async def setup_status_endpoint(request: Request) -> JSONResponse:
    """Return first-run setup readiness for the onboarding wizard."""
    return JSONResponse(setup_status(request.app.state.config))


async def package_status_endpoint(request: Request) -> JSONResponse:
    """Return installed package version and best-effort update status."""
    fetcher = getattr(request.app.state, "package_status_fetcher", None)
    if callable(fetcher):
        return JSONResponse(await asyncio.to_thread(fetcher))
    return JSONResponse(await asyncio.to_thread(package_status))


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
    import sys
    import subprocess

    cmd = [sys.executable, "-m", "pip", "install", "mlx-whisper>=0.4.0"]
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
    return name in {"localhost", "127.0.0.1", "::1"}


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


async def setup_finish_endpoint(request: Request) -> JSONResponse:
    """Write real setup config from bootstrap mode and request supervisor restart."""
    config = request.app.state.config
    if not getattr(config, "bootstrap_mode", False):
        return JSONResponse({"error": "setup finish is only available in bootstrap mode"}, status_code=409)
    if not _localhost_request(request) or not _setup_finish_origin_allowed(request):
        return JSONResponse({"error": "setup finish is localhost-only"}, status_code=403)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "json object is required"}, status_code=400)

    workspace = str(body.get("workspace", "")).strip()
    if not workspace:
        return JSONResponse({"error": "workspace is required"}, status_code=400)
    push_contact = str(body.get("push_contact", "")).strip()
    if not push_contact:
        return JSONResponse({"error": "push_contact is required"}, status_code=400)
    try:
        port = int(body.get("port") or config.pwa_port)
    except (TypeError, ValueError):
        return JSONResponse({"error": "port must be an integer"}, status_code=400)
    if port < 1 or port > 65535:
        return JSONResponse({"error": "port must be between 1 and 65535"}, status_code=400)

    from ciao.cli import setup_workspace

    written = setup_workspace(
        workspace,
        auth_token=str(body.get("auth_token", "")).strip() or config.pwa_auth_token,
        auth_required=bool(body.get("auth_required", True)),
        push_contact=push_contact,
        vault_root=str(body.get("vault_root", "")).strip() or None,
        vault_mode=str(body.get("vault_mode", "scratch")).strip().lower(),
        python_path=str(body.get("python", "")).strip() or None,
        port=port,
        launch_agents_dir=str(body.get("launch_agents_dir", "")).strip() or None,
        app_dir=str(body.get("app_dir", "")).strip() or None,
    )
    restart = bool(body.get("restart", True))
    if restart:
        restart_fn = getattr(request.app.state, "request_restart", None)
        if callable(restart_fn):
            restart_fn(config.restart_exit_code)

    return JSONResponse({
        "ok": True,
        "restart_requested": restart,
        "workspace": str(Path(workspace).expanduser().resolve()),
        "written": [str(path) for path in written],
    })


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
        subprocess.run, ["git", "pull"],
        cwd=str(ws), capture_output=True, text=True, timeout=60,
    )
    steps.append(_record("git pull", result))
    if result.returncode != 0:
        return JSONResponse(
            {"steps": steps, "ok": False, "error": f"git pull failed: {steps[-1]['output']}"},
            status_code=500,
        )

    # 2. pip install
    result = await asyncio.to_thread(
        subprocess.run, ["pip", "install", "-e", "."],
        cwd=str(ws), capture_output=True, text=True, timeout=120,
    )
    steps.append(_record("pip install", result))
    if result.returncode != 0:
        return JSONResponse(
            {"steps": steps, "ok": False, "error": f"pip install failed: {steps[-1]['output']}"},
            status_code=500,
        )

    # 2b. npm install at repo root. Picks up server-side node tools
    # (codeburn, slidev theme, anything we add later to the root
    # package.json). Non-fatal: a transient npm hiccup shouldn't block
    # a deploy, since the Python server runs regardless.
    result = await asyncio.to_thread(
        subprocess.run, ["npm", "install", "--no-audit", "--no-fund"],
        cwd=str(ws), capture_output=True, text=True, timeout=180,
    )
    steps.append(_record("npm install (root)", result))

    # 3. npm build
    web_dir = ws / "web"
    result = await asyncio.to_thread(
        subprocess.run, ["npm", "run", "build"],
        cwd=str(web_dir), capture_output=True, text=True, timeout=120,
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
    """List skills known to Ciao, labelled as custom or GitHub/package."""
    config = request.app.state.config
    return JSONResponse(build_skill_inventory(config.workspace_root))


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
        # Device identity for the Settings "commit to main" panel (always
        # shown now: every instance works on its own dev/<device> branch).
        "device_name": config.device_name,
        "dispatch_schedules": config.dispatch_schedules,
    })


# ── Local session flow (per-device branch + direct/agent-merged handover) ──


def _local_manager(request: Request):
    return getattr(request.app.state, "local_session_manager", None)


def _open_merge_chat(request: Request, branch: str) -> dict:
    """Open an interactive chat that merges ``branch`` into ``main``, resolving
    conflicts with the user. Returns {ok, chat_id, project_id} or {error}."""
    config = request.app.state.config
    pcm = request.app.state.project_chat_manager
    projects = pcm.list_projects("personal")
    project = next((p for p in projects if p.name == "General"), None)
    if project is None:
        return {"error": "no personal project to host the merge chat"}

    from datetime import UTC, datetime
    from ciao.local_session import MERGE_PROMPT, MERGE_PROMPT_MAIN

    if branch == "main":
        title = f"Resolve sync conflicts: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')}"
        prompt = MERGE_PROMPT_MAIN
    else:
        title = f"Merge to main: {branch} {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')}"
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
    """Current device-session state: device name, branch, dirty."""
    mgr = _local_manager(request)
    if mgr is None:
        return JSONResponse(
            {"error": "local session manager not initialised"}, status_code=500
        )
    return JSONResponse(mgr.status())


async def local_handback(request: Request) -> JSONResponse:
    """Commit the device session and land it on ``main``.

    Clean merge -> pushed to main directly. Conflict -> an interactive merge
    chat is opened in CiaoBot to resolve it.
    """
    mgr = _local_manager(request)
    if mgr is None:
        return JSONResponse(
            {"error": "local session manager not initialised"}, status_code=500
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

    result = await mgr.commit_to_main()
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    if result.get("merged"):
        return JSONResponse(result)
    # Conflict: hand off to an interactive merge chat.
    merge = _open_merge_chat(request, result.get("branch") or mgr.branch)
    return JSONResponse({**result, "merge": merge})


async def local_resync(request: Request) -> JSONResponse:
    """After the merge chat pushed main, re-point the device branch at it."""
    mgr = _local_manager(request)
    if mgr is None:
        return JSONResponse(
            {"error": "local session manager not initialised"}, status_code=500
        )
    result = await mgr.resync()
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


async def handover_merge(request: Request) -> JSONResponse:
    """Open an interactive chat that merges a branch into ``main``. Also used
    by ``local_handback`` when the automatic merge conflicts."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = {}
    branch = (body.get("branch") if isinstance(body, dict) else None) or ""
    if not branch:
        mgr = _local_manager(request)
        branch = mgr.branch if mgr else "main"
    merge = _open_merge_chat(request, branch)
    return JSONResponse(merge, status_code=200 if merge.get("ok") else 500)



async def cli_stats(request: Request) -> JSONResponse:
    """Return Claude Code CLI stats from ~/.claude/stats-cache.json."""
    if not _STATS_CACHE_PATH.exists():
        return JSONResponse({"error": "stats-cache.json not found"}, status_code=404)
    try:
        data = json.loads(_STATS_CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return JSONResponse({"error": "failed to read stats"}, status_code=500)
    return JSONResponse(data)
