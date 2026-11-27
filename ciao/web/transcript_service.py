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
* the per-session renderers - the Claude session JSONL and an ACP session
  journal (:func:`_render_acp_journal`);
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
import base64
import json
import logging
import os
import re
import stat
from collections import Counter
from pathlib import Path
from typing import Any

from ciao import cli_envelopes, provider_registry
from ciao.tool_summary import summarize_claude_tool_input
from ciao.background_notice import format_finished_runs
from ciao.models import ChatContext
from ciao.rate_limits import is_rate_limit_telemetry
from ciao.web.chat_broker import extract_file_touches, normalize_file_touch_paths

logger = logging.getLogger(__name__)

CHAT_SEARCH_MAX_TRANSCRIPT_BYTES = 512 * 1024
CHAT_SEARCH_MAX_READ_BYTES = 8 * 1024 * 1024
CHAT_SEARCH_MAX_CANDIDATES = 100
CHAT_SEARCH_PAGE_SIZE = 30


def _read_search_file(path: Path, limit: int) -> bytes | None:
    """Read one regular file without following any path component symlink."""
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        return None
    parts = path.absolute().parts
    descriptors: list[int] = []
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(parts[0], flags)
        descriptors.append(fd)
        for part in parts[1:-1]:
            fd = os.open(part, flags, dir_fd=fd)
            descriptors.append(fd)
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        descriptors.append(file_fd)
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            return None
        with os.fdopen(os.dup(file_fd), "rb") as stream:
            return stream.read(limit + 1)
    except OSError:
        return None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _casefold_match(text: str, query: str) -> tuple[int, int] | None:
    """Return source character offsets for a casefolded literal match."""
    folded_parts: list[str] = []
    offsets: list[int] = []
    for index, character in enumerate(text):
        folded = character.casefold()
        folded_parts.append(folded)
        offsets.extend([index] * len(folded))
    folded_text = "".join(folded_parts)
    start = folded_text.find(query.casefold())
    if start < 0:
        return None
    end = start + len(query.casefold())
    return offsets[start], offsets[end - 1] + 1


def _search_archive_path(pcm, workspace: str, chat) -> Path | None:
    """Return an archive only from this workspace/shared root's exact chat folder."""
    if not chat.archive_path:
        return None
    path: Path | None = pcm._locate_archive(chat.chat_id, chat.archive_path)
    if path is None:
        return None
    config = pcm._config
    for root in (config.chats_root(workspace), config.logs_root / "Chats"):
        expected_chat_root = (root / chat.chat_id).absolute()
        try:
            relative = path.absolute().relative_to(expected_chat_root)
        except ValueError:
            continue
        if relative.parts and all(part not in {".", ".."} for part in relative.parts):
            return path
    return None


def _workspace_search_root_identity(pcm, workspace: str) -> tuple[object, ...] | None:
    """Snapshot workspace roots and directory identities for post-read checks."""
    config = pcm._config
    try:
        workspace_config = config.workspace(workspace)
        roots = (
            config.workspace_root,
            config.vault_root,
            config.workspace_vault_root(workspace),
            config.agent_root(workspace),
            config.chats_root(workspace),
            config.logs_root / "Chats",
            Path(pcm._transcripts._runtime_root) / "transcripts",
        )
    except (OSError, ValueError):
        return None
    root_identity: list[tuple[object, ...]] = []
    for root in dict.fromkeys(Path(path).absolute() for path in roots):
        try:
            info = os.stat(root, follow_symlinks=False)
            identity: tuple[object, ...] = (
                str(root), info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)
            )
        except OSError:
            identity = (str(root), None, None, None)
        root_identity.append(identity)
    return (
        config.workspace_root.absolute(),
        workspace,
        getattr(workspace_config, "vault_root", None),
        tuple(root_identity),
    )


def _search_visible_rows(rows: list[dict]) -> list[str]:
    """Extract the text rows the normal transcript renderer makes visible."""
    visible: list[str] = []
    for row in rows:
        role = row.get("role")
        content = str(row.get("content") or "")
        if role == "user":
            content = _strip_injected_context(content)
            if cli_envelopes.is_hidden_continuation(content):
                continue
        elif role != "assistant" or row.get("is_error"):
            continue
        if content:
            visible.append(content)
    return visible


def _search_handover_text(messages: object, limit: int) -> tuple[str, int, bool]:
    """Bound handover scanning and apply the same visibility rules as transcripts."""
    if not isinstance(messages, list):
        return "", 0, False
    rows: list[dict] = []
    consumed = 0
    for row in messages:
        if not isinstance(row, dict) or row.get("role") not in {"user", "assistant"} or row.get("is_error"):
            continue
        content = row.get("content")
        if not isinstance(content, str) or not content:
            continue
        # Truncate before encoding so even one maliciously large in-memory row
        # allocates at most a few times the configured byte budget.
        remaining = limit - consumed
        encoded = content[:remaining + 1].encode("utf-8")
        if len(encoded) > remaining:
            return "", limit, True
        consumed += len(encoded)
        rows.append({"role": row["role"], "content": content, "is_error": row.get("is_error")})
    return "\n".join(_search_visible_rows(rows)), consumed, False


def search_workspace_chats(pcm, workspace: str, query: str, cursor: str | None) -> dict:
    """Search a bounded projection of saved chat text in one registered workspace."""
    normalized = query.casefold()
    anchor: tuple[str, str] | None = None
    if cursor:
        try:
            raw_cursor = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            payload = json.loads(raw_cursor)
            if (not isinstance(payload, dict) or payload.get("w") != workspace
                    or payload.get("q") != normalized or not isinstance(payload.get("t"), str)
                    or not isinstance(payload.get("id"), str)):
                raise ValueError
            anchor = (payload["t"], payload["id"])
        except (ValueError, TypeError, json.JSONDecodeError):
            raise ValueError("invalid cursor") from None

    _project_ids, chat_ids = pcm.workspace_scope(workspace)
    root_identity = _workspace_search_root_identity(pcm, workspace)
    candidates = []
    for chat_id in chat_ids:
        chat = pcm._chats.get(chat_id)
        project = pcm._projects.get(chat.project_id) if chat else None
        if (chat is None or project is None or project.workspace != workspace
                or project.is_system or chat.helper):
            continue
        key = (chat.last_activity_at or chat.created_at or "", chat.chat_id)
        if anchor is not None and key >= anchor:
            continue
        candidates.append((key, chat, project))
    candidates.sort(key=lambda item: item[0], reverse=True)
    items: list[dict] = []
    examined = consumed = 0
    skipped_large = False
    next_key = anchor
    for key, chat, project in candidates:
        if examined >= CHAT_SEARCH_MAX_CANDIDATES or len(items) >= CHAT_SEARCH_PAGE_SIZE:
            break
        if consumed >= CHAT_SEARCH_MAX_READ_BYTES:
            break
        examined += 1
        original_identity = (chat.project_id, chat.archived, chat.archive_path, chat.provider)
        original_project_identity = (project.workspace, project.kind, project.name)
        title_match = normalized in chat.title.casefold()
        if chat.archived:
            path = _search_archive_path(pcm, workspace, chat)
        else:
            path = pcm._transcripts.current_path(ChatContext.for_web(chat.chat_id), chat.provider or "claude")
        allowance = min(CHAT_SEARCH_MAX_TRANSCRIPT_BYTES, CHAT_SEARCH_MAX_READ_BYTES - consumed)
        text, handover_bytes, handover_too_large = _search_handover_text(
            getattr(chat, "handover_messages", []), allowance
        )
        if handover_too_large:
            if allowance < CHAT_SEARCH_MAX_TRANSCRIPT_BYTES:
                examined -= 1
                break
            consumed += allowance
            skipped_large = True
            text = ""
        else:
            consumed += handover_bytes
        if path is not None:
            allowance = min(CHAT_SEARCH_MAX_TRANSCRIPT_BYTES, CHAT_SEARCH_MAX_READ_BYTES - consumed)
            if allowance <= 0:
                examined -= 1
                break
            raw = _read_search_file(path, allowance)
            if raw is not None:
                consumed += min(len(raw), allowance)
                if len(raw) > allowance:
                    if allowance == CHAT_SEARCH_MAX_TRANSCRIPT_BYTES:
                        skipped_large = True
                    else:
                        examined -= 1
                        break
                else:
                    try:
                        source = raw.decode("utf-8")
                        rows = (pcm._parse_transcript_messages(source) if chat.archived else
                                pcm._transcripts.current_messages_from_data(json.loads(source)))
                        visible = _search_visible_rows(rows)
                        text = "\n".join([text, *visible]).strip()
                    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                        pass
        if (pcm._chats.get(chat.chat_id) is not chat
                or pcm._projects.get(chat.project_id) is not project
                or project.workspace != workspace
                or project.is_system
                or chat.helper
                or (project.workspace, project.kind, project.name) != original_project_identity
                or (chat.project_id, chat.archived, chat.archive_path, chat.provider) != original_identity
                or chat.chat_id not in pcm.workspace_scope(workspace)[1]
                or root_identity is None
                or _workspace_search_root_identity(pcm, workspace) != root_identity):
            next_key = key
            continue
        match = _casefold_match(text, normalized)
        if title_match or match is not None:
            snippet = ""
            if match is not None:
                match_start, match_end = match
                start = max(0, match_start - 65)
                snippet = " ".join(text[start:match_end + 80].split())[:160]
            items.append({"chat_id": chat.chat_id, "title": chat.title,
                          "project_title": project.name, "archived": bool(chat.archived),
                          "last_activity_at": chat.last_activity_at or chat.created_at,
                          "snippet": snippet})
        next_key = key
    more = next_key is not None and any(item[0] < next_key for item in candidates)
    next_cursor = None
    if more and next_key is not None:
        payload = json.dumps({"w": workspace, "q": normalized, "t": next_key[0],
                              "id": next_key[1]}, separators=(",", ":")).encode()
        next_cursor = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return {"items": items, "next_cursor": next_cursor,
            "skipped_large_transcripts": skipped_large}


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
    agent_root: Path | None = None,
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
            summary = summarize_claude_tool_input(name, tinput)
            touches = normalize_file_touch_paths(
                extract_file_touches(name, tinput),
                workspace_root,
                agent_root=agent_root,
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
    stripped = cli_envelopes.strip_injected_context(content)
    if stripped != content:
        return _strip_image_manifest(stripped).strip() or content
    legacy = _strip_legacy_context_prefix(content)
    legacy = _strip_image_manifest(legacy)
    return legacy.strip() or content


# The user-record skip rules (/model and /mode echoes, the interrupted-turn
# sentinel, the interrupt marker, the CLI envelope tags) and the
# task-notification grammar live in ciao/cli_envelopes.py. They are shared
# with any reader that counts user turns, which must skip exactly the
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



_is_cli_internal_envelope = cli_envelopes.is_cli_envelope


# Stands in for the injected subagent-synthesis nudge in the transcript. Same
# icon as the subagent-completion lines above so the pair reads as one story.
_SYNTHESIS_NUDGE_LABEL = "\U0001F916 Background agents finished — asked for a consolidated report"


# Prompt Ciaobot used to inject when background agents finished. It is no
# longer sent (#1394: the CLI now runs a follow-up turn itself). It is kept
# because session files recorded before then contain it, and the renderer
# collapses it into the status line above.
_SYNTHESIS_NUDGE = (
    "The background agent(s) you dispatched have now finished. Review their "
    "results (read their transcripts or output as needed) and post your "
    "consolidated final report for this task now. Do not dispatch new "
    "background agents to answer this. If you already posted the final "
    "report, reply with a brief confirmation instead of repeating it."
)

_WHITESPACE_RE = re.compile(r"\s+")


def _collapse_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


_SYNTHESIS_PROMPT_COLLAPSED = _collapse_whitespace(_SYNTHESIS_NUDGE)


def _is_synthesis_nudge(text: str) -> bool:
    """True when ``text`` is the legacy subagent-synthesis prompt (no longer sent).

    Matches on the tail: the nudge is sent with the chat's context prefix
    attached, and the /messages prefix stripper only removes the shapes it
    recognizes.
    """
    return _collapse_whitespace(text).endswith(_SYNTHESIS_PROMPT_COLLAPSED)


def _summarize_task_notification(content: str) -> str | None:
    """Render a <task-notification> envelope as a one-line system bubble.

    Returns None if `content` carries no task-notification. The CLI emits this
    XML as a user-role message after a Task subagent finishes. We surface it
    as a system status bubble so the user retains visibility into subagent
    completions without seeing the raw envelope.

    Only the first notification is summarised when the CLI concatenates
    several into one record; the rest stay hidden with the envelope. A record
    that merely *carries* the grammar — the human's own prose quoting it, or
    shell output from a command that printed a session JSONL — is not a
    completion and returns None, the same rule the shared
    envelope vocabulary applies.
    """
    fields = cli_envelopes.envelope_notification_fields(content)
    if fields is None:
        return None
    status = fields.get("status", "completed")
    summary = fields.get("summary", "")
    first_line = summary.splitlines()[0].strip() if summary else ""
    icon = "\U0001F916"  # 🤖
    if first_line:
        # Trim aggressively so the bubble stays one line.
        snippet = first_line if len(first_line) <= 120 else first_line[:117] + "..."
        return f"{icon} Subagent {status}: {snippet}"
    return f"{icon} Subagent {status}"


def _native_task_notice(content: str) -> str | None:
    """Wake-format text for provider-native shell completions, else None.

    Background shells and Task subagents both draw as the background-run
    card, the same card an OpenCode subagent wake gets. The first
    notification decides: a ``Monitor event`` or an unrecognised summary falls
    through to the one-line status bubble. A record can concatenate several
    completions; the consecutive native ones share one notice. No log path:
    the provider's output file is not copied into the notice. A subagent's tail
    is its inline report.
    """
    runs: list[cli_envelopes.NativeTaskRun] = []
    for fields, _task_ids in cli_envelopes.envelope_notifications(content):
        run = cli_envelopes.native_task_run(fields) or cli_envelopes.subagent_task_run(fields)
        if run is None:
            break
        runs.append(run)
    if not runs:
        return None
    entries: list[dict[str, Any]] = [
        {
            "status": run.status,
            "label": run.label,
            "run_id": run.run_id,
            "exit_code": run.exit_code,
            "error": run.error,
            "log_path": None,
            "last_lines": list(run.tail) or None,
        }
        for run in runs
    ]
    return format_finished_runs(entries, tail_lines=50)[0]


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
    for turn, idx in _last_assistant_index_per_turn(entries).items():
        rec = timings.get(str(turn)) or timings.get(turn)
        if not isinstance(rec, dict):
            continue
        completed = rec.get("completed_at")
        if completed:
            entries[idx]["sent_at"] = completed
        duration = rec.get("duration_ms")
        if isinstance(duration, (int, float)):
            entries[idx]["duration_ms"] = int(duration)


def _last_assistant_index_per_turn(entries: list[dict]) -> dict[int, int]:
    """Index of the last assistant row of each user turn, in list order."""
    current_turn: int | None = None
    last_assistant_idx_in_turn: dict[int, int] = {}
    for i, entry in enumerate(entries):
        role = entry.get("role")
        if role == "user":
            ti = entry.get("turn_index")
            current_turn = ti if isinstance(ti, int) else None
        elif role == "assistant" and current_turn is not None:
            last_assistant_idx_in_turn[current_turn] = i
    return last_assistant_idx_in_turn


def _mark_preceding_answer(rows: list[dict]) -> None:
    """Mark the existing answer before a hidden continuation; append nothing.

    A hidden continuation prompt is skipped before it can render, so the row
    that answered the user just before it would otherwise fold into Activity
    like any mid-turn note. Scanning backwards keeps the absolute row list (and
    the wire indices built from it) untouched: stop at a real user row, skip
    system/tool rows and empty assistant text, and at the nearest nonempty
    tool-free assistant text either leave an explicit ``commentary`` note alone
    or tag the answer ``final_answer``.
    """
    for row in reversed(rows):
        role = row.get("role")
        if role == "user":
            return
        if role != "assistant" or row.get("tool_name"):
            continue
        if not str(row.get("content") or "").strip():
            continue
        if row.get("phase") == "commentary":
            return
        row["phase"] = "final_answer"
        return


def _stamp_turn_metadata(entry: dict, chat: Any, user_idx: int) -> None:
    """Overlay one user turn's send-time facts (images, send time, unattended, origin)."""
    refs = chat.user_turn_images.get(str(user_idx))
    if refs:
        entry["images"] = list(refs)
    timing = chat.user_turn_timings.get(str(user_idx)) or {}
    if timing.get("sent_at"):
        entry["sent_at"] = timing["sent_at"]
    if chat.user_turn_unattended.get(str(user_idx)):
        entry["unattended"] = True
    origin = chat.user_turn_origin.get(str(user_idx))
    if origin:
        entry["origin"] = origin


# Subagent calls (OpenCode `subagent`, Claude `Agent`/`Task`) render as one card row. Their
# nested rows are capped per card; the rest are a count. Mirrors web/src/lib/subagentCard.ts.
_SUBAGENT_TOOLS = frozenset({"agent", "subagent", "task"})
SUBAGENT_STEP_LIMIT = 50
_SUBAGENT_INPUT_RE = re.compile(r"^\[([^\]]*)\]\s*(.*)$", re.DOTALL)


def _is_subagent_tool(tool: str) -> bool:
    return tool.strip().lower() in _SUBAGENT_TOOLS


def _subagent_fields(summary: str) -> tuple[str, str]:
    """(agent_type, description) from a journalled ``[type] description`` input.

    Journals written before the input carried the description kept the call's title
    (``subagent``) as its input; that names no description.
    """
    text = summary.strip()
    if not text or _is_subagent_tool(text):
        return "", ""
    match = _SUBAGENT_INPUT_RE.match(text)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return "", text


def _subagent_step_key(step: dict) -> str | None:
    step_id = step.get("tool_use_id")
    if not step_id:
        return None
    return f"{step_id}\n{step['file_path']}" if step.get("kind") == "file" else str(step_id)


def _subagent_title(description: str) -> str:
    return f"Subagent: {description}" if description else "Subagent"


class _ActivityRun:
    """The open run of activity lines; each line keeps the tool_use_id that wrote it."""

    __slots__ = ("lines", "ids")

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.ids: list[str] = []


class _SubagentCard:
    """One subagent call while the journal renders: its wire fields and nested steps."""

    __slots__ = ("tool_use_id", "agent_type", "description", "state", "steps", "_step_at")

    def __init__(self, tool_use_id: str, agent_type: str, description: str) -> None:
        self.tool_use_id = tool_use_id
        self.agent_type = agent_type
        self.description = description
        # No terminal record yet: running. A launch acknowledgement or a later result settles it.
        self.state = "running"
        self.steps: list[dict] = []
        self._step_at: dict[str, int] = {}

    def remember(self, agent_type: str, description: str) -> None:
        if description:
            self.agent_type = agent_type
            self.description = description

    def add_step(self, step: dict) -> None:
        # A child call announced again (same call, and file) replaces its own step.
        key = _subagent_step_key(step)
        at = self._step_at.get(key) if key is not None else None
        if at is None:
            if key is not None:
                self._step_at[key] = len(self.steps)
            self.steps.append(step)
        else:
            self.steps[at] = step

    def settle(self, record: dict) -> None:
        status = record.get("subagent_status")
        if status:
            self.state = "done" if status == "completed" else "failed"
        elif record.get("is_error"):
            self.state = "failed"
        elif record.get("background"):
            # The launch acknowledgement: the subagent keeps running.
            return
        else:
            self.state = "done"

    def row(self) -> dict:
        return {
            "role": "system",
            "content": _subagent_title(self.description),
            "tool_name": "_subagent",
            "tool_use_id": self.tool_use_id,
            "subagent": {
                "tool_use_id": self.tool_use_id,
                "agent_type": self.agent_type,
                "description": self.description,
                "state": self.state,
                "step_count": len(self.steps),
                "steps": self.steps[:SUBAGENT_STEP_LIMIT],
            },
        }


class _ShellCard:
    """One shell call while the journal renders: its command, background flag and settled result.

    Mirrors ``web/src/lib/shellCard.ts``. A background shell's launch acknowledgement leaves it
    running: its end is not on the wire, so it stays running until a later result for the call.
    """

    __slots__ = ("tool_use_id", "command", "background", "state", "exit_code", "output_tail")

    def __init__(self, tool_use_id: str) -> None:
        self.tool_use_id = tool_use_id
        self.command = ""
        self.background = False
        self.state = "running"
        self.exit_code: int | None = None
        self.output_tail = ""

    def remember(self, command: str, background: bool) -> None:
        if command:
            self.command = command
        self.background = self.background or background

    def settle(self, record: dict) -> None:
        if record.get("background"):
            # The launch acknowledgement: the shell keeps running.
            self.background = True
            return
        code = record.get("exit_code")
        self.exit_code = code if isinstance(code, int) and not isinstance(code, bool) else None
        failed = bool(record.get("is_error")) or (self.exit_code is not None and self.exit_code != 0)
        self.state = "failed" if failed else "done"
        self.output_tail = str(record.get("output_tail") or "")

    def row(self) -> dict:
        return {
            "role": "system",
            "content": self.command or "Shell",
            "tool_name": "_shell",
            "tool_use_id": self.tool_use_id,
            "shell": {
                "tool_use_id": self.tool_use_id,
                "command": self.command,
                "background": self.background,
                "state": self.state,
                "exit_code": self.exit_code,
                "output_tail": self.output_tail,
            },
        }


def _render_acp_journal(
    records: list[dict],
    chat: Any,
    *,
    start_user_idx: int,
    workspace_root: Path | None,
    agent_root: Path | None,
) -> list[dict]:
    """Render one ACP session's journal rows into the provider-neutral PWA row shape.

    ``records`` is the ``acp_journal`` a provider's ``read_thread`` returns: user prompts and
    the ``event_to_json`` payloads of each turn, in the order they happened. Consecutive text
    deltas join into one assistant row; a tool call, a thinking block or a prompt ends that
    row. Subagent text and thinking (a payload with ``parent_tool_use_id``) stay out of the
    parent's reply. A subagent call is one ``_subagent`` row: its children's activity and file
    cards nest under it. A call written more than once (a re-announce) is one row or line, not
    two. A ``result`` row carries no text of its own (the text was already streamed), so it only
    ends a run.
    """
    result: list[Any] = []
    user_idx = int(start_user_idx)
    text_run: list[str] = []
    run: _ActivityRun | None = None
    line_at: dict[str, tuple[_ActivityRun, int]] = {}
    cards: dict[str, _SubagentCard] = {}
    shells: dict[str, _ShellCard] = {}
    filed: set[str] = set()

    def flush_text() -> None:
        if text_run:
            text = "".join(text_run).strip()
            text_run.clear()
            if text:
                result.append({"role": "assistant", "content": text})

    def flush_tools() -> None:
        nonlocal run
        if run is not None and run.lines:
            result.append(run)
        run = None

    def add_line(call_id: str, line: str) -> None:
        nonlocal run
        if call_id and call_id in line_at:
            held, at = line_at[call_id]
            held.lines[at] = line
            return
        if run is None:
            run = _ActivityRun()
        run.lines.append(line)
        run.ids.append(call_id)
        if call_id:
            line_at[call_id] = (run, len(run.lines) - 1)

    def file_steps(record: dict, tool: str, call_id: str) -> list[dict] | None:
        raw_touches = record.get("file_touches") or (
            [record["file_touch"]] if record.get("file_touch") else []
        )
        touches = normalize_file_touch_paths(
            [t for t in raw_touches if isinstance(t, dict) and t.get("file_path")],
            workspace_root,
            agent_root=agent_root,
        )
        if not touches:
            return None
        return [
            {
                "kind": "file",
                "file_path": touch["file_path"],
                "action": touch.get("action") or "touched",
                "tool": tool,
                "tool_use_id": call_id,
            }
            for touch in touches
        ]

    for record in records:
        kind = record.get("type")
        parent = record.get("parent_tool_use_id")
        if kind == "user" and record.get("turn_origin"):
            # A turn the engine recorded on its own (a claimed shell notice, before the
            # provider's continuation reply): its notice card, not a user turn. It takes no
            # user index, and it ends the reply before it, so that reply is its own answer.
            flush_text()
            flush_tools()
            notice = str(record.get("text") or "").strip()
            if notice:
                result.append({
                    "role": "system",
                    "content": notice,
                    "origin": str(record["turn_origin"]),
                })
            continue
        if kind == "user":
            flush_text()
            flush_tools()
            content = _strip_injected_context(str(record.get("text") or "")).strip()
            if not content:
                continue
            # A hidden continuation is a turn the host sent itself (after `ciao chat
            # reload`): skipped before user_idx advances, and it marks the answer
            # before it as final, as the other renderers do.
            if cli_envelopes.is_hidden_continuation(content):
                # Activity runs and subagent cards are not dict rows yet: the scan skips them.
                _mark_preceding_answer([item if isinstance(item, dict) else {"role": "system"} for item in result])
                continue
            entry: dict = {"role": "user", "content": content, "turn_index": user_idx}
            _stamp_turn_metadata(entry, chat, user_idx)
            result.append(entry)
            user_idx += 1
        elif kind == "text_delta":
            if parent:
                continue
            flush_tools()
            text_run.append(str(record.get("text") or ""))
        elif kind == "thinking":
            if parent:
                continue
            flush_text()
            flush_tools()
            text = str(record.get("text") or "").strip()
            if text:
                result.append({"role": "system", "content": text, "tool_name": "_thinking"})
        elif kind == "tool_use":
            tool = str(record.get("tool_name") or "tool")
            call_id = str(record.get("tool_use_id") or "")
            summary = str(record.get("tool_input") or "")
            if parent and parent in cards:
                # A child of a subagent call nests under its card, never in the parent's trace.
                steps = file_steps(record, tool, call_id)
                if steps is None:
                    steps = [{
                        "kind": "activity",
                        "line": f"{_tool_icon(tool)} {tool}" + (f" {summary}" if summary else ""),
                        "tool_use_id": call_id,
                    }]
                for step in steps:
                    cards[parent].add_step(step)
                continue
            if _is_subagent_tool(tool) and not parent:
                agent_type, description = _subagent_fields(summary)
                card = cards.get(call_id)
                if card is not None:
                    card.remember(agent_type, description)
                    continue
                flush_text()
                flush_tools()
                card = _SubagentCard(call_id, agent_type, description)
                cards[call_id] = card
                result.append(card)
                continue
            if call_id and call_id in filed:
                continue
            steps = file_steps(record, tool, call_id)
            if steps is not None:
                flush_text()
                flush_tools()
                filed.add(call_id)
                for step in steps:
                    result.append({
                        "role": "system",
                        "tool_name": "_filecard",
                        "content": step["file_path"],
                        "file_path": step["file_path"],
                        "action": step["action"],
                        "tool": tool,
                    })
                continue
            if record.get("shell") and call_id and not parent:
                # A shell call is one card row, keyed by its call id; a re-announce updates it.
                shell = shells.get(call_id)
                if shell is None:
                    flush_text()
                    flush_tools()
                    shell = _ShellCard(call_id)
                    shells[call_id] = shell
                    result.append(shell)
                shell.remember(summary, bool(record.get("shell_background")))
                continue
            flush_text()
            line = f"{_tool_icon(tool)} {tool}"
            if summary:
                line += f" {summary}"
            # A child whose parent card is not in this journal keeps the subagent marker.
            add_line(call_id, f"↳ {line}" if parent else line)
        elif kind == "tool_result":
            call_id = str(record.get("tool_use_id") or "")
            card = cards.get(call_id)
            if card is not None and not parent:
                card.settle(record)
            shell = shells.get(call_id)
            if shell is not None and not parent:
                shell.settle(record)
        elif kind == "result":
            # A turn ends here. Its reply text must not run on into the next turn's
            # (a provider's own continuation has no user row between them).
            flush_text()
        # Any other kind (status, permission_request, ...) adds no row: a call
        # is one activity line, and the terminal result's text was streamed.
    flush_text()
    flush_tools()
    rows: list[dict] = []
    for item in result:
        if isinstance(item, _ActivityRun):
            rows.append({"role": "system", "content": "\n".join(item.lines), "tool_name": "_activity"})
        elif isinstance(item, (_SubagentCard, _ShellCard)):
            rows.append(item.row())
        else:
            rows.append(item)
    return rows


def _session_lineage(chat: Any) -> list[str]:
    """Every session id a chat has used, oldest first, without blanks or repeats.

    Each provider keeps its turns only in the session that wrote them, and ciaobot rotates
    through ``_rotate_session_id`` (autocompact, resume fallback, continuation), so history
    is stitched across the whole lineage.
    """
    session_ids: list[str] = []
    seen: set[str] = set()
    for sid in (*getattr(chat, "previous_session_ids", []), chat.session_id):
        sid_str = str(sid or "").strip()
        if not sid_str or sid_str in seen:
            continue
        seen.add(sid_str)
        session_ids.append(sid_str)
    return session_ids


def _append_unrendered_error(rendered: list[dict], current: list[dict]) -> None:
    """Keep the chat's persisted error turn when the provider's history does not show it."""
    if not current or current[-1].get("role") != "assistant" or not current[-1].get("is_error"):
        return
    for row in reversed(rendered):
        if row.get("role") == "assistant":
            if row.get("is_error") or row.get("content") == current[-1].get("content"):
                return
            break
    rendered.append(dict(current[-1]))


async def _acp_history_rows(
    pcm: Any, config: Any, provider: str, chat: Any, session_root: Path
) -> list[dict]:
    """Rendered history for every session in a chat's lineage, from each session's journal."""
    provider_cls = provider_registry.require(provider).factory()
    rendered: list[dict] = []
    start_user_idx = chat.turn_index_base
    for sid in _session_lineage(chat):
        try:
            thread = await provider_cls.read_thread(
                session_root, sid, runtime_root=config.state_path.parent
            )
            records = thread.get("acp_journal") if isinstance(thread, dict) else None
            if not isinstance(records, list) or not records:
                continue
            segment = _render_acp_journal(
                records,
                chat,
                start_user_idx=start_user_idx,
                workspace_root=Path(config.workspace_root),
                agent_root=session_root,
            )
        except Exception:  # noqa: BLE001 — one unreadable journal must not blank its siblings
            logger.warning("Could not render an ACP session journal; its turns are left out.")
            continue
        if segment:
            # Advance the turn offset by this segment's user rows.
            rendered.extend(segment)
            start_user_idx += sum(1 for row in segment if row.get("role") == "user")
    return rendered


def _stamp_user_turn(
    entry: dict,
    chat: Any,
    turn_index: int,
    raw_message: Any,
    fallback_sent_at: str = "",
) -> None:
    """Stamp the index-derived fields for one user row.

    ``raw_message`` is the SDK message object, needed only for the inline-image
    fallback on a session row; ``fallback_sent_at`` is the durable turn's own
    timestamp, used only when ``user_turn_timings`` has no send time.

    The fields this helper owns are cleared before stamping, so re-stamping a
    row with the turn index it really belongs to cannot leave the previous
    turn's flags on it. Images are always re-derived from this turn's refs (or
    the session message's inline blocks), never kept from the row's old index.
    """
    for key in ("images", "sent_at", "unattended", "origin"):
        entry.pop(key, None)
    # Image refs are recorded per user-turn index at send time. JSON keys are
    # strings, but tolerate int lookups too in case the map has been mutated
    # in-memory since the last save.
    refs = chat.user_turn_images.get(str(turn_index))
    if refs is None:
        refs = chat.user_turn_images.get(turn_index)
    if refs:
        entry["images"] = list(refs)
    elif raw_message is not None:
        # Fall back to inline base64 images from the SDK session. This handles
        # sessions that were context-compacted: the user_turn_images index map
        # becomes stale after compaction shifts the turn numbering, but inline
        # images survive.
        inline = _extract_inline_images(raw_message)
        if inline:
            entry["images"] = inline
    # Surface the user-turn index so the client can dedup replayed user_echo
    # events against history it already loaded.
    entry["turn_index"] = turn_index
    # Attach the persisted send time so the UI footer can show it on reload.
    # Missing for pre-feature chats: the frontend treats an empty string as
    # "no timestamp".
    timing = chat.user_turn_timings.get(str(turn_index)) or chat.user_turn_timings.get(turn_index)
    sent_at = timing.get("sent_at") if isinstance(timing, dict) else ""
    if not sent_at:
        sent_at = fallback_sent_at
    if sent_at:
        entry["sent_at"] = sent_at
    # Loop/schedule ticks are user turns in the session file too, so the flag
    # has to come from our own per-turn record.
    if chat.user_turn_unattended.get(str(turn_index)) or chat.user_turn_unattended.get(turn_index):
        entry["unattended"] = True
    origin = chat.user_turn_origin.get(str(turn_index)) or chat.user_turn_origin.get(turn_index)
    if origin:
        entry["origin"] = origin


def _align_user_turns(
    result: list[dict],
    session_user_rows: list[tuple[int, str, Any]],
    current_rows: list[dict],
    chat: Any,
    *,
    rebuilt_head: bool = False,
) -> tuple[list[dict], list[tuple[dict, int]]]:
    """Align rendered session user rows with the durable transcript's turns.

    This module numbers user rows by their position in the provider session,
    while the engine numbers turns per send. A send that died before the
    provider recorded anything is absent from the session, so every later
    session row inherited its ``turn_index``, its ``sent_at`` and its per-turn
    flags. The durable transcript does hold that turn (``is_error`` on its
    assistant row), so walk both lists in order, count the durable turns the
    session cannot show, and number the rows positionally from ``base``.

    Numbering positionally is what keeps the common shapes untouched: with no
    lost turn the session's own ``base + position`` is already the engine's
    numbering for steered and post-handover rows alike. The counts decide what
    a mismatch means: of the sends the session is short of, ``lost`` turns
    explain some, and whatever is left can only be a *different* text for a
    send the session does hold — an engine-rewritten prompt (an attachment's
    ``ciao-drop:`` refs expanded into absolute paths), which consumes its
    session row instead of taking a turn number of its own.

    A durable file also holds turns that are not counted sends at all: our own
    hidden continuation prompts are written by ``record_turn`` without bumping
    ``user_turn_count`` and are hidden by the renderer, so they are dropped
    before the walk.

    Only an *error* durable turn with no session counterpart is rendered as a
    lost turn.

    It also returns the text pairing, as ``(rendered user row, durable user
    row index)``, so the metadata overlay can follow it.
    """
    durable = [
        (index, row)
        for index, row in enumerate(current_rows)
        if row.get("role") == "user"
    ]
    # Continuation prompts are recorded as turns but never counted as sends and
    # never rendered, so they own no turn index.
    durable = [
        (index, row)
        for index, row in durable
        if not cli_envelopes.is_hidden_continuation(str(row.get("content") or ""))
    ]
    if not session_user_rows:
        return result, []
    base = chat.turn_index_base
    # The durable file belongs to a provider stint: a handover leaves the
    # previous provider's file on disk, so it can hold turns this chat no
    # longer renders. A steer bumps `user_turn_count` without writing a durable
    # turn, so it is subtracted from how many durable turns the current stint
    # can have. `count == 0` is the older-state/test-double case: nothing is
    # sliced.
    count = int(getattr(chat, "user_turn_count", 0) or 0)
    expected = 0
    if count:
        steered = sum(
            1
            for key in getattr(chat, "user_turn_steered", {})
            if int(key) >= base and int(key) < count
        )
        expected = (count - base) - steered
        if expected <= 0:
            # Nothing this stint can be aligned against.
            return result, []
        if expected < len(durable):
            durable = durable[-expected:]
    if rebuilt_head:
        # The session starts with turns whose prompts it does not hold (an ACP journal rebuilt
        # from a load replay), so its first prompt is not the stint's first turn. Anchor at the
        # END: of the turns the stint counted, the session holds the last ones, except failed
        # sends it never recorded. Matching from the start instead paired a repeated text with
        # its first, older occurrence.
        stint_turns = expected if count else len(durable)
        session_seen = {" ".join(text.split()) for _pos, text, _message in session_user_rows}
        unrecorded_failures = sum(
            1
            for index, row in durable
            if index + 1 < len(current_rows)
            and current_rows[index + 1].get("role") == "assistant"
            and current_rows[index + 1].get("is_error")
            and " ".join(str(row.get("content") or "").split()) not in session_seen
        )
        head = max(0, stint_turns - len(session_user_rows) - unrecorded_failures)
        durable = durable[head:]
        base += head
        if count:
            expected -= head
        if not durable:
            # Nothing durable to pair with (no transcript, or only the running turn): the
            # session's prompts are the stint's last turns, in order.
            for position, (rendered_at, _text, message) in enumerate(session_user_rows):
                _stamp_user_turn(result[rendered_at], chat, base + position, raw_message=message)
            return result, []
    if not durable:
        return result, []
    durable_texts = [
        " ".join(str(row.get("content") or "").split()) for _index, row in durable
    ]
    session_texts = [
        " ".join(text.split()) for _pos, text, _message in session_user_rows
    ]

    def failed_turn(index: int) -> bool:
        """Whether the durable reply to the turn at ``index`` is an error row."""
        if index + 1 >= len(current_rows):
            return False
        reply = current_rows[index + 1]
        return reply.get("role") == "assistant" and bool(reply.get("is_error"))

    def lost_turn_rows(abs_turn: int, index: int, row: dict) -> list[dict]:
        """The two durable rows for a send the provider never recorded."""
        user_row: dict = {
            "role": "user",
            "content": row.get("content") or "",
            "turn_index": abs_turn,
            "sent_at": str(row.get("sent_at") or ""),
        }
        refs = chat.user_turn_images.get(str(abs_turn))
        if refs is None:
            refs = chat.user_turn_images.get(abs_turn)
        if refs:
            user_row["images"] = list(refs)
        return [user_row, {**current_rows[index + 1], "is_error": True}]

    def has_session_counterpart(text: str, start: int) -> bool:
        """Whether any session user row from ``start`` on carries this text.

        A durable transcript can hold fewer turns than the session — a chat
        whose recording starts partway through, or a store that was reset. Its
        turn indices are then relative to that shorter list, so re-stamping a
        session row from them would misnumber it. Only a turn with no
        counterpart at all may be counted, which keeps this path from ever
        duplicating the session rows it holds.
        """
        return text in session_texts[start:]

    def run_length(texts: list[str], start: int) -> int:
        """How many consecutive rows from ``start`` carry the same text."""
        length = 0
        while start + length < len(texts) and texts[start + length] == texts[start]:
            length += 1
        return length

    durable_counts = Counter(durable_texts)
    session_counts = Counter(session_texts)

    def is_lost_like(position: int) -> bool:
        """An error turn whose identical-text run outnumbers the session's."""
        index, _row = durable[position]
        text = durable_texts[position]
        return failed_turn(index) and durable_counts[text] > session_counts[text]

    # How many sends the session is short of, and how many of those the error
    # turns explain. What is left can only be a non-error turn the session
    # cannot show.
    surplus = max(0, expected - len(session_user_rows)) if count else 0
    missing = max(0, surplus - sum(1 for p in range(len(durable)) if is_lost_like(p)))

    pairs: list[tuple[int, int, dict]] = []
    # Durable turns the session cannot render, in durable order: the session
    # position they sit before, "lost" (a failed send to render) or "skipped"
    # (a turn that only consumes an index), their index in ``current_rows`` and
    # the durable row itself.
    shifts: list[tuple[int, str, int, dict]] = []
    session_pos = 0
    for position, (index, row) in enumerate(durable):
        text = durable_texts[position]
        if session_pos >= len(session_user_rows):
            # The session ended first: a failed turn still earns a row, and a
            # non-error turn takes an index only when one is genuinely missing.
            if failed_turn(index):
                shifts.append((len(session_user_rows), "lost", index, row))
            elif missing > 0:
                missing -= 1
                shifts.append((len(session_user_rows), "skipped", index, row))
            continue
        if (
            text != session_texts[session_pos]
            and not failed_turn(index)
            and has_session_counterpart(text, session_pos)
        ):
            # The session holds sends the durable store never recorded (a send
            # that never reached record_turn). They own no durable turn, so
            # move past them to this turn's own row before pairing it. A failed
            # turn keeps the fall-through so the run-length and lost-turn logic
            # decide.
            session_pos = session_texts.index(text, session_pos)
        if text == session_texts[session_pos]:
            # `retryFromError` re-sends the same text after a failure, so a run
            # of identical texts has to align from the RIGHT: the failed send
            # keeps its row, and the retry keeps the index the engine counted
            # for it. Equal-length runs (a genuine repeat, or several sends
            # that all reached the provider) pair one-to-one as before.
            if failed_turn(index) and run_length(durable_texts, position) > run_length(
                session_texts, session_pos
            ):
                shifts.append((session_pos, "lost", index, row))
                continue
            pairs.append((session_pos, index, row))
            session_pos += 1
        elif not has_session_counterpart(text, session_pos):
            # A failed send is the case the whole path exists for: the session
            # cannot hold it, so it owns its row and its index.
            if failed_turn(index):
                shifts.append((session_pos, "lost", index, row))
                continue
            if count == 0:
                # Older state with no counter: the file's own length is the
                # only evidence, so a missing turn takes an index.
                shifts.append((session_pos, "skipped", index, row))
                continue
            if missing > 0:
                missing -= 1
                shifts.append((session_pos, "skipped", index, row))
                continue
            # No send is missing here: this durable turn is an engine-rewritten
            # prompt (attachment refs expanded into paths), so it is the SAME
            # send as the session row at the cursor. Consume that row so a
            # later lost turn anchors after it.
            if session_pos < len(session_user_rows):
                pairs.append((session_pos, index, row))
                session_pos += 1
    shift_anchors = [anchor for anchor, _kind, _index, _row in shifts]

    def shifts_up_to(position: int) -> int:
        return sum(1 for anchor in shift_anchors if anchor <= position)

    paired_by_position = {position: row for position, _index, row in pairs}
    for position, (rendered_at, _text, message) in enumerate(session_user_rows):
        paired = paired_by_position.get(position)
        _stamp_user_turn(
            result[rendered_at],
            chat,
            base + position + shifts_up_to(position),
            raw_message=message,
            fallback_sent_at=str((paired or {}).get("sent_at") or ""),
        )
    paired_rows = [
        (result[session_user_rows[position][0]], index) for position, index, _row in pairs
    ]
    insertions: list[tuple[int, list[dict]]] = []
    for ordinal, (anchor, kind, index, row) in enumerate(shifts):
        if kind != "lost":
            continue
        target = (
            session_user_rows[anchor][0]
            if anchor < len(session_user_rows)
            else len(result)
        )
        # The shift entries ahead of this one (all at the same or an earlier
        # anchor) already own the turn numbers before it.
        insertions.append(
            (target, lost_turn_rows(base + anchor + ordinal, index, row))
        )
    # Anchors are non-decreasing along the walk, so inserting from the highest
    # one down keeps the earlier positions valid; within one anchor the last
    # lost entry goes in first to keep the durable order.
    for target, rows in reversed(insertions):
        result[target:target] = rows
    return result, paired_rows


def _overlay_transcript_metadata(
    entries: list[dict], transcript_rows: list[dict]
) -> None:
    # A wake reply (origin turn) belongs to no user segment: it follows the
    # notice card, in the same segment as the reply before it. Skipping those
    # rows keeps one metadata entry per user segment, so every real reply keeps
    # its own footer (the wake reply's own usage is not shown).
    metadata = [
        row for row in transcript_rows
        if row.get("role") == "assistant" and not row.get("origin")
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


def _overlay_reply_metadata(
    entries: list[dict], transcript_rows: list[dict], paired: list[tuple[dict, int]]
) -> None:
    """Overlay durable reply metadata by the alignment's pairing, else positionally."""
    if not transcript_rows:
        return
    if paired:
        _overlay_paired_metadata(entries, transcript_rows, paired)
    else:
        _overlay_transcript_metadata(entries, transcript_rows)


def _overlay_paired_metadata(
    entries: list[dict],
    transcript_rows: list[dict],
    paired: list[tuple[dict, int]],
) -> None:
    """Copy each paired durable turn's reply metadata onto its session or journal turn.

    Used by both history paths: the Claude session JSONL and an ACP session journal.

    ``paired`` is ``_align_user_turns``' text pairing: a rendered user row and
    the index of the durable user row it matched. The durable reply is the row
    after it; the target is the last assistant row before the next user row,
    which is where the turn footer reads its usage.
    """
    durable_for = {id(row): index for row, index in paired}
    current: int | None = None
    last_assistant: dict | None = None

    def flush() -> None:
        if current is None or last_assistant is None:
            return
        if current + 1 >= len(transcript_rows):
            return
        source = transcript_rows[current + 1]
        if source.get("role") != "assistant":
            return
        for key in ("usage", "quota", "effective_model"):
            if source.get(key):
                last_assistant[key] = source[key]

    for row in entries:
        if row.get("role") == "user":
            flush()
            current = durable_for.get(id(row))
            last_assistant = None
        elif row.get("role") == "assistant":
            last_assistant = row
    flush()


def _stamp_archive_turn_indices(rows: list[dict], chat) -> None:
    """Give archived user rows the ``turn_index`` the live transcript uses.

    Hidden continuation turns are already dropped from ``rows`` and carry no
    index. Steered indices bump ``user_turn_count`` without a user row, so they
    are skipped. Counting back from ``user_turn_count`` keeps the newest rows
    right when earlier rows belong to a previous provider stint (below
    ``turn_index_base``); those get no index.
    """
    base = chat.turn_index_base
    count = chat.user_turn_count
    steered = {int(key) for key in chat.user_turn_steered}
    user_rows = [row for row in rows if row.get("role") == "user"]
    if count > 0:
        idx = count - 1
        for row in reversed(user_rows):
            while idx in steered:
                idx -= 1
            if idx < base:
                break
            row["turn_index"] = idx
            idx -= 1
    else:
        idx = base
        for row in user_rows:
            while idx in steered:
                idx += 1
            row["turn_index"] = idx
            idx += 1


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
    parsed: list[dict] = pcm._parse_transcript_messages(text)
    # Map transcript timestamp field to the frontend's sent_at key.
    for parsed_entry in parsed:
        if "timestamp" in parsed_entry and "sent_at" not in parsed_entry:
            parsed_entry["sent_at"] = parsed_entry["timestamp"]
    _stamp_archive_turn_indices(parsed, chat)
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
    descriptor = provider_registry.get(provider)
    if descriptor is not None and descriptor.acp_spec_path:
        # An ACP agent's history is its session journal (the provider read_thread). With no
        # journal, the neutral branch below serves the local transcript instead.
        acp_rows = await _acp_history_rows(pcm, config, provider, chat, session_root)
        if acp_rows:
            current = pcm._transcripts.current_messages(ChatContext.for_web(chat_id), provider)
            # A journal need not hold every prompt since `turn_index_base`: one rebuilt from a
            # loadSession replay has the replayed turns' events but not their prompts. Number
            # the user rows from the durable turns they pair with, as the session path does,
            # so a row carries the engine's turn index (the live `user_echo` is deduped by it).
            # A rebuilt start is matched against the END of the durable turns, and with no
            # durable turn to pair, the prompts take the stint's last turn indices.
            journal_user_rows = [
                (position, str(row.get("content") or ""), None)
                for position, row in enumerate(acp_rows)
                if row.get("role") == "user"
            ]
            # Rows before the first prompt are replies whose prompts the journal never got.
            rebuilt_head = bool(journal_user_rows) and any(
                row.get("role") != "user" and not row.get("origin")
                for row in acp_rows[: journal_user_rows[0][0]]
            )
            acp_rows, paired = _align_user_turns(
                acp_rows, journal_user_rows, current, chat, rebuilt_head=rebuilt_head
            )
            _append_unrendered_error(acp_rows, current)
            _overlay_reply_metadata(acp_rows, current, paired)
            _overlay_assistant_timings(acp_rows, chat.user_turn_timings)
            return [*handover_messages, *acp_rows]
    if not provider_registry.flag(provider, "session_jsonl_history"):
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

    user_idx = chat.turn_index_base
    # Every user row this session rendered, with the text the durable
    # transcript is compared against and the SDK message its images can be
    # re-derived from: a send that died before the provider wrote it shifts
    # this numbering off the durable one.
    session_user_rows: list[tuple[int, str, Any]] = []
    failed_tool_ids = _failed_tool_use_ids(msgs)
    for m in msgs:
        if m.type == "assistant":
            blocks = _extract_assistant_blocks(
                m.message,
                workspace_root=config.workspace_root,
                agent_root=session_root,
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
        # The SDK marks compaction recaps explicitly. Handle them before
        # context stripping and synthetic-user filters: recap text is opaque
        # summary content, not a user turn, even if it resembles a CLI marker.
        if m.type == "user" and cli_envelopes.is_compact_summary(m):
            result.append({"role": "system", "content": content, "origin": "compaction"})
            continue
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
        # Our hidden continuations (cli_envelopes.RELOAD_CONTINUE_PROMPT and the
        # legacy code-mode one): hidden outright. Skip without incrementing
        # user_idx so markers keep their turn_index.
        if m.type == "user" and cli_envelopes.is_hidden_continuation(content):
            _mark_preceding_answer(result)
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
                # fall through to the blanket hide. The summariser itself
                # answers None for an envelope that is not a notification, so
                # <bash-stdout> from a command that printed a session JSONL
                # stays hidden instead of rendering a status line assembled
                # out of shell output.
                native_notice = _native_task_notice(content)
                if native_notice is not None:
                    result.append({
                        "role": "system",
                        "content": native_notice,
                        "origin": "background_run",
                    })
                    continue
                task_summary = _summarize_task_notification(content)
                if task_summary is not None:
                    result.append({"role": "system", "content": task_summary})
                continue
            # Our own subagent-synthesis nudge (legacy, no longer sent).
            # It's a server-injected prompt, not something the user typed, so
            # showing the paragraph verbatim reads as words they never wrote.
            # Collapse it to a status line, and skip without incrementing
            # user_idx.
            if _is_synthesis_nudge(content):
                result.append({"role": "system", "content": _SYNTHESIS_NUDGE_LABEL})
                continue
        entry: dict = {
            "role": m.type,
            "content": content,
        }
        if m.type == "user":
            _stamp_user_turn(entry, chat, user_idx, m.message)
            session_user_rows.append((len(result), content, m.message))
            user_idx += 1
        result.append(entry)
    # Stitch the durable transcript's per-turn metadata (token usage with the
    # context %, quota, effective model) onto the rendered rows — the session
    # JSONL carries none of it, so without this the turn footer showed only
    # the completion time and duration. The ACP journal path applies the same
    # overlay.
    current = pcm._transcripts.current_messages(
        ChatContext.for_web(chat_id), provider
    )
    # Number the session rows from the durable turns they pair with, and
    # render the failed turns the session cannot contain. Metadata follows the
    # same text pairing; the positional overlay is only for chats the alignment
    # could not pair.
    result, paired = _align_user_turns(result, session_user_rows, current, chat)
    if result:
        _overlay_reply_metadata(result, current, paired)
    _overlay_assistant_timings(result, chat.user_turn_timings)
    return [*handover_messages, *result]
