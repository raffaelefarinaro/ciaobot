"""In-memory broker for per-chat streaming responses.

Purpose: decouple the SDK streaming task from the WebSocket lifecycle so that
closing the app (or a flaky network) does not abort an in-flight response. Any
number of WS clients can subscribe to the same chat's stream; new subscribers
receive a replay of buffered events (so the UI can re-attach seamlessly).
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
import uuid
from pathlib import Path
from typing import AsyncIterator, Callable

from ciao.models import (
    AssistantTextDelta,
    ModelCapabilityQuestionEvent,
    ModelChangedEvent,
    PermissionRequestEvent,
    PromptCancelledEvent,
    ResultEvent,
    SteerDeliveredEvent,
    StreamEvent,
    SystemStatusEvent,
    ThinkingEvent,
    TokenUsageEvent,
    ToolUseEvent,
)

logger = logging.getLogger(__name__)


# Shared by ChatStream's live `_pending` list and ProjectChatManager's
# persisted `chat.pending_queue` (the parked queue used once a stream tears
# down mid-error/question-pause/retry) so reorder/edit/remove behave
# identically whether or not a live stream exists for the chat.
def reorder_pending_list(
    items: list[dict], entry_id: str, before_id: str | None = None
) -> bool:
    """Move the entry with ``entry_id`` just before ``before_id`` in ``items``.

    If ``before_id`` is None, move to the end. Returns True on success.
    """
    src_idx = next((i for i, p in enumerate(items) if p.get("id") == entry_id), -1)
    if src_idx == -1:
        return False
    item = items.pop(src_idx)
    if before_id is None:
        items.append(item)
        return True
    dst_idx = next((i for i, p in enumerate(items) if p.get("id") == before_id), -1)
    if dst_idx == -1:
        # Target vanished; keep the item at the end rather than dropping it.
        items.append(item)
        return False
    items.insert(dst_idx, item)
    return True


def edit_pending_list(
    items: list[dict], entry_id: str, text: str, images: list[str] | None = None
) -> bool:
    """Update text and images of the entry with ``entry_id`` in ``items``."""
    for item in items:
        if item.get("id") == entry_id:
            item["text"] = text
            item["images"] = list(images or [])
            item.pop("origin", None)
            return True
    return False


def remove_pending_list(items: list[dict], entry_id: str) -> bool:
    """Remove the entry with ``entry_id`` from ``items``."""
    for i, item in enumerate(items):
        if item.get("id") == entry_id:
            items.pop(i)
            return True
    return False


QUEUE_DELIVERIES = frozenset({"queue", "steer"})
#: An engine-injected turn's source, or ``retry`` for a user-requested retry.
#: Recorded per turn in ``ChatInfo.user_turn_origin`` and sent on
#: ``user_echo`` / ``/messages`` so the PWA draws a notice card instead of a
#: user bubble. Empty string = typed by the person.
TURN_ORIGINS = frozenset({
    "background_run", "cli_task_wake", "retry", "schedule", "task_delegation",
    "task_resume", "webhook", "update_task",
})
# ``steer_id`` of an entry handed to ``steer_turn`` whose delivery id is not
# known yet. It locks the entry (no edit, remove or re-dispatch) meanwhile.
STEER_DISPATCHING = "dispatching"


def _flushed_entry(entry: dict) -> dict:
    """A drained entry as it flushes or parks: delivery state is queue-only.

    Anything leaving the live queue is a plain follow-up. In-flight steer state
    never outlives the live queue (``_fallback_steers`` reverts it first).
    """
    flushed = {
        "id": entry.get("id", ""),
        "text": entry.get("text", ""),
        "images": list(entry.get("images") or []),
    }
    if entry.get("origin"):
        flushed["origin"] = str(entry["origin"])
    if entry.get("send_id"):
        flushed["send_id"] = str(entry["send_id"])
    if entry.get("send_ids"):
        flushed["send_ids"] = list(entry["send_ids"])
    return flushed


def shared_origin(entries: list[dict]) -> str:
    """The origin every entry carries, or "" if any entry lacks it or they differ.

    A steered or combined message retains an engine origin or user-requested
    retry tag only when every entry shares it. Mixed-in ordinary typed words
    must stay a visible bubble.
    """
    origins = {str(e.get("origin") or "") for e in entries}
    if len(origins) != 1:
        return ""
    return origins.pop()


def public_queue(entries: list[dict], waiting: list[dict] | None = None) -> list[dict]:
    """The ``queue_state`` shape of queue entries: no internal steer id.

    ``steering`` is true while an entry has been handed to the provider and is
    waiting for its delivery confirmation. ``waiting`` entries are parked on the
    chat (Stop, an error, a question pause): they come first and carry
    ``waiting: true``, so a client never has to infer which chips are held.
    Parked entries never carry a steer id and may predate the ``delivery`` field.
    """
    return [
        {
            "id": entry.get("id", ""),
            "text": entry.get("text", ""),
            "images": list(entry.get("images") or []),
            "delivery": entry.get("delivery") or "queue",
            "steering": bool(entry.get("steer_id")),
            "waiting": is_waiting,
        }
        for is_waiting, group in ((True, waiting or []), (False, entries))
        for entry in group
    ]


# Heartbeat cadence for an idle stream subscription. A background tool (e.g. a
# dynamic workflow or a long Bash) can leave the parent turn with no events to
# publish for tens of seconds. Without traffic the per-chat WebSocket goes
# silent and can die half-open (readyState OPEN, nothing flowing), so events
# published when the gap ends never reach the client. Yielding a lightweight
# keepalive frame every few seconds keeps the socket warm and makes a dead
# socket surface promptly (the send raises, the forwarder stops). Clients
# ignore the `keepalive` type but may use it as a liveness signal.
#
# Keep this short: the PWA's half-open watchdog treats ~2 missed keepalives as
# stale, so 5s here → recovery in ~12s instead of ~45s with a 15s cadence.
# `_attach_streams` sends it too while a chat is idle.
STREAM_KEEPALIVE_SECONDS = 5.0


# Tool names that mutate a file on disk and carry the target path in their input.
# `Write` and `Edit` both use `file_path`; `MultiEdit` does too. `NotebookEdit`
# stores the path under `notebook_path`. Lowercase variants seen in some
# providers (`write`, `edit`) are mirrored for safety. Some tool calls use
# `path` instead of `file_path`, so path extraction accepts aliases.
_FILE_TOUCH_TOOLS: dict[str, tuple[str, ...]] = {
    "Write": ("file_path", "path"),
    "write": ("file_path", "path"),
    "Edit": ("file_path", "path"),
    "edit": ("file_path", "path"),
    "MultiEdit": ("file_path", "path"),
    "NotebookEdit": ("notebook_path", "path"),
    "generate_image": ("ImageName", "image_path", "path", "file_path"),
    "generateImage": ("ImageName", "image_path", "path", "file_path"),
    "write_file": ("path", "file_path"),
    "writeFile": ("path", "file_path"),
    # The model's deliberate "show this to the user" signal (ciao/mcp_server.py
    # file_surface), not a mutation — reuses the file-touch pipeline so it gets
    # the same inline card, with a distinct action the frontend auto-pins on.
    "mcp__ciaobot__file_surface": ("path",),
}

# Shell tools whose command text may create or overwrite files. Parsed
# conservatively — false negatives are preferable to inventing paths from
# descriptions like "Create the guest CSV".
_SHELL_FILE_TOOLS = frozenset({"Bash", "bash", "run_command", "exec_command"})

# Action labels surfaced on the inline file card. Keep these short — the
# frontend renders them next to the basename.
_FILE_TOUCH_ACTIONS: dict[str, str] = {
    "Write": "written",
    "write": "written",
    "Edit": "edited",
    "edit": "edited",
    "MultiEdit": "edited",
    "NotebookEdit": "edited",
    "generate_image": "generated",
    "generateImage": "generated",
    "write_file": "written",
    "writeFile": "written",
    "mcp__ciaobot__file_surface": "surfaced",
}

_SHELL_REDIRECT_RE = re.compile(
    r"(?:^|[\s;|&])(?:\d*)(>>?)\s*['\"]?([^\s'\"<>&|;]+)['\"]?"
)
_SHELL_HEREDOC_RE = re.compile(
    r"(?:^|[\s;|&])(?:\w+)?\s*(>>?)\s*['\"]?([^\s'\"<>&|;]+)['\"]?\s*<<"
)
_SHELL_TOUCH_RE = re.compile(r"(?:^|[\s;|&])touch\s+([^\n;&|]+)")
# The CLI surface's `ciao file surface <path>` is the same deliberate "show
# this to the user" signal the MCP `mcp__ciaobot__file_surface` tool carried,
# so it reuses the file-touch pipeline and auto-pins the panel (S5/S6). `ciao`
# must sit at a real command position — start of the shell input or after a
# command separator (; | & or a newline) — never mid-argument, so `echo ciao
# file surface out/report.md` (a string that merely contains the words) is not
# mistaken for an explicit surfacing request.
_CIAO_FILE_SURFACE_RE = re.compile(r"(?:^|[;&|\n]\s*)ciao\s+file\s+surface\s+([^\n;&|]+)")
_SHELL_TEE_RE = re.compile(
    r"(?:^|[\s;|&])tee(\s+-a)?\s+['\"]?([^\s'\"<>&|;]+)['\"]?"
)
_SHELL_COPY_RE = re.compile(r"(?:^|[\s;|&])(?:cp|mv|install)\s+([^\n;&|]+)")

# Bare words after `>` are often English ("echo hi > There"), not files.
# Require a path separator, a dotfile, an allow-listed extension, or a
# conventional extensionless filename before promoting a shell target to an
# Outputs chip. Dedicated Write/Edit tools still accept any explicit path.
_SHELL_PATH_EXT_RE = re.compile(
    r"\.(?:md|markdown|txt|py|ts|tsx|js|jsx|vue|css|html|json|yaml|yml|toml|"
    r"sh|bash|zsh|rs|go|java|xml|sql|cfg|ini|log|csv|tsv|env|example|"
    r"pdf|pptx|png|jpe?g|gif|webp|svg|avif|bmp|ico|lock|sum)$",
    re.IGNORECASE,
)
_EXTENSIONLESS_SHELL_NAMES = frozenset({
    "makefile",
    "dockerfile",
    "containerfile",
    "license",
    "licence",
    "readme",
    "changelog",
    "gemfile",
    "rakefile",
    "procfile",
    "vagrantfile",
    "gitignore",
    "dockerignore",
    "editorconfig",
    "npmrc",
    "browserslist",
})


def _looks_like_shell_path(path: str) -> bool:
    if not path or path in {".", "..", "-", "/dev/null", "/dev/stdout", "/dev/stderr"}:
        return False
    if path.startswith("/dev/") or path.startswith("-"):
        return False
    # Expansions / globs are too ambiguous to surface as Outputs chips.
    if any(ch in path for ch in "*?$`\n"):
        return False
    base = path.rstrip("/").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if not base or base in {".", ".."}:
        return False
    if "/" in path or "\\" in path or path.startswith("."):
        return True
    if _SHELL_PATH_EXT_RE.search(base):
        return True
    if base.lower() in _EXTENSIONLESS_SHELL_NAMES:
        return True
    return False


def _bash_command_text(tool_input: object) -> str:
    if isinstance(tool_input, dict):
        for key in ("command", "cmd"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return ""
    if isinstance(tool_input, str):
        return tool_input
    return ""


# `2>/dev/null`, `>log`, `<in`: a redirection, not an argument. shlex keeps it
# in the token list, so a `cp a b 2>/dev/null` would otherwise take the
# redirect as the destination.
_SHELL_REDIRECT_TOKEN_RE = re.compile(r"^\d*(?:[<>]|&>)")


def _argv_before_redirect(tokens: list[str]) -> list[str]:
    for i, token in enumerate(tokens):
        if _SHELL_REDIRECT_TOKEN_RE.match(token):
            return tokens[:i]
    return tokens


def _paths_from_shell_command(command: str) -> list[dict]:
    """Best-effort paths a shell command creates or overwrites."""
    results: list[dict] = []
    seen: set[str] = set()

    def add(raw: str, action: str) -> None:
        path = raw.strip().strip("'\"")
        if not _looks_like_shell_path(path) or path in seen:
            return
        seen.add(path)
        results.append({"file_path": path, "action": action})

    for match in _SHELL_HEREDOC_RE.finditer(command):
        add(match.group(2), "created" if match.group(1) == ">" else "written")
    for match in _SHELL_REDIRECT_RE.finditer(command):
        add(match.group(2), "created" if match.group(1) == ">" else "written")
    for match in _SHELL_TOUCH_RE.finditer(command):
        try:
            tokens = _argv_before_redirect(shlex.split(match.group(1)))
        except ValueError:
            continue
        for token in tokens:
            if token.startswith("-"):
                continue
            add(token, "created")
    for match in _SHELL_TEE_RE.finditer(command):
        add(match.group(2), "written" if match.group(1) else "created")
    for match in _SHELL_COPY_RE.finditer(command):
        try:
            argv = _argv_before_redirect(shlex.split(match.group(1)))
            tokens = [t for t in argv if not t.startswith("-")]
        except ValueError:
            continue
        if len(tokens) >= 2:
            add(tokens[-1], "created")
    for match in _CIAO_FILE_SURFACE_RE.finditer(command):
        try:
            tokens = shlex.split(match.group(1))
        except ValueError:
            continue
        for token in tokens:
            if token.startswith("-") or token in {"--json", "--"}:
                continue
            # An explicit `ciao file surface <path>` is a deliberate surfacing
            # request, not a shell redirect: accept the argument as-is, without
            # the `_looks_like_shell_path` heuristic that suppresses redirect
            # false positives (and would drop valid viewable files like .htm
            # the PWA HTML viewer recognizes).
            path = token.strip().strip("'\"")
            if not path or path in seen:
                continue
            seen.add(path)
            results.append({"file_path": path, "action": "surfaced"})
    return results


def extract_file_touches(tool_name: str, tool_input: object) -> list[dict]:
    """If this tool mutates files, return ``[{file_path, action}, …]``.

    Accepts both shapes of ``tool_input``:

    - ``dict``: the raw SDK input as persisted in the session JSONL. We pick
      the path out of ``file_path``, ``path``, or ``notebook_path``
      (NotebookEdit). For shell tools we parse ``command``.
    - ``str``: the live stream summary produced by
      ``_summarize_tool_input`` in ``ciao/providers/claude.py``. For the
      dedicated file-touch tools it already collapses the dict into the path
      string itself. For Bash the summary is often a description, so callers
      that have the raw input should prefer
      ``ToolUseEvent.file_touches`` computed before summarisation.

    The classification is advisory: it tells the PWA "render an inline card
    for this write". The actual read goes through ``/api/workspace-file``.
    That endpoint has no workspace sandbox — it serves any allowlisted file
    on disk — so a card pointing at ``/etc/passwd`` would render but the
    extension allowlist still refuses to return its contents.
    """
    if tool_name in _SHELL_FILE_TOOLS:
        return _paths_from_shell_command(_bash_command_text(tool_input))
    if tool_name not in _FILE_TOUCH_TOOLS:
        return []
    action = _FILE_TOUCH_ACTIONS.get(tool_name, "touched")
    if isinstance(tool_input, dict):
        fields = _FILE_TOUCH_TOOLS[tool_name]
        for field in fields:
            path = tool_input.get(field)
            if isinstance(path, str) and path.strip():
                return [{"file_path": path.strip(), "action": action}]
        return []
    if isinstance(tool_input, str):
        path = tool_input.strip()
        if not path:
            return []
        return [{"file_path": path, "action": action}]
    return []


def normalize_file_touch_paths(
    touches: list[dict],
    workspace_root: Path | None = None,
    *,
    agent_root: Path | None = None,
) -> list[dict]:
    """Canonicalise file-card paths and discard invalid surface requests.

    Providers can refer to the same workspace file using an absolute path or
    a workspace-relative path. The PWA deduplicates file cards by path string,
    so normalise both forms to one workspace-relative spelling before they
    reach the client.

    ``file_surface`` is different from Write/Edit: it promises that an
    existing artifact was deliberately selected for preview. Its tool-use
    event arrives before the structured MCP result, so reject a missing target
    here instead of briefly painting a card for a request that will fail.

    A relative tool path is the agent's cwd-relative path, so it resolves
    against ``agent_root`` when given (else ``workspace_root``). The canonical
    spelling stays relative to ``workspace_root``, which is what the viewer
    endpoints anchor relative paths to.
    """
    if not touches:
        return touches
    root = workspace_root.resolve() if workspace_root is not None else None
    base = agent_root.resolve() if agent_root is not None else root
    normalized: list[dict] = []
    seen: set[str] = set()
    for touch in touches:
        item = dict(touch)
        path_text = item.get("file_path")
        if not isinstance(path_text, str) or not path_text.strip():
            continue
        raw_path = Path(path_text.strip())
        target = raw_path
        if not raw_path.is_absolute() and base is not None:
            target = base / raw_path
        try:
            resolved = target.resolve()
        except (OSError, RuntimeError):
            resolved = target

        if item.get("action") == "surfaced" and base is not None:
            # Match CiaoControlPlane._safe_relative: file_surface accepts only
            # existing files whose requested path is relative to, and resolves
            # inside, the active workspace.
            if raw_path.is_absolute():
                continue
            try:
                if not resolved.is_relative_to(base) or not resolved.is_file():
                    continue
            except OSError:
                continue

        canonical = path_text.strip()
        if root is not None:
            try:
                canonical = resolved.relative_to(root).as_posix()
            except ValueError:
                canonical = str(resolved)
        item["file_path"] = canonical
        if canonical in seen:
            continue
        seen.add(canonical)
        normalized.append(item)
    return normalized


def refine_file_touch_actions(
    touches: list[dict],
    workspace_root: Path | None = None,
    *,
    agent_root: Path | None = None,
) -> list[dict]:
    """Upgrade ``written`` → ``created`` when the path does not yet exist.

    Tool-use events fire before the CLI executes the write, so a missing
    file at this moment means the tool is creating it.
    """
    if not touches:
        return touches
    root = workspace_root.resolve() if workspace_root is not None else None
    refined: list[dict] = []
    for touch in normalize_file_touch_paths(
        touches, workspace_root, agent_root=agent_root
    ):
        item = dict(touch)
        action = item.get("action")
        path_text = item.get("file_path")
        if (
            action in {"written", "touched"}
            and isinstance(path_text, str)
            and path_text.strip()
        ):
            path = Path(path_text.strip())
            if not path.is_absolute() and root is not None:
                path = root / path
            try:
                if not path.exists():
                    item["action"] = "created"
            except OSError:
                pass
        refined.append(item)
    return refined


def apply_file_touches_to_payload(
    payload: dict,
    *,
    workspace_root: Path | None = None,
    agent_root: Path | None = None,
) -> None:
    """Normalise ``file_touch`` / ``file_touches`` on a tool_use WS payload."""
    if payload.get("type") != "tool_use":
        return
    raw = payload.get("file_touches")
    touches: list[dict]
    if isinstance(raw, list) and raw:
        touches = [t for t in raw if isinstance(t, dict) and t.get("file_path")]
    elif isinstance(payload.get("file_touch"), dict):
        touches = [payload["file_touch"]]
    else:
        return
    touches = refine_file_touch_actions(
        touches, workspace_root, agent_root=agent_root
    )
    if not touches:
        payload.pop("file_touch", None)
        payload.pop("file_touches", None)
        return
    payload["file_touch"] = touches[0]
    if len(touches) > 1:
        payload["file_touches"] = touches
    else:
        payload.pop("file_touches", None)


def event_to_json(event: StreamEvent) -> dict | None:
    """Convert a StreamEvent into the JSON payload sent to WS clients."""
    if isinstance(event, PromptCancelledEvent):
        # Host bookkeeping only: the drive loop parks the prompt in the
        # chat's waiting queue.
        return None
    if isinstance(event, SteerDeliveredEvent):
        # Host bookkeeping only: the drive loop turns it into a ``steered``
        # event once the queue entries are matched to it.
        return None
    if isinstance(event, AssistantTextDelta):
        payload: dict = {"type": "text_delta", "text": event.text}
        if event.parent_tool_use_id:
            payload["parent_tool_use_id"] = event.parent_tool_use_id
        if event.phase:
            payload["phase"] = event.phase
        return payload
    if isinstance(event, ToolUseEvent):
        if event.type == "tool_result":
            # A call's completion is its own frame (#1416). OpenCode sends
            # one after every call; Claude never does. It marks the call's
            # existing trace line instead of painting a second one, so it
            # must not go through the call path below (no file_touch, no
            # file-touch extraction).
            payload = {"type": "tool_result", "tool_name": event.tool_name}
            if event.tool_use_id:
                payload["tool_use_id"] = event.tool_use_id
            if event.is_error:
                payload["is_error"] = True
            if event.tool_input:
                payload["detail"] = event.tool_input
            if event.parent_tool_use_id:
                payload["parent_tool_use_id"] = event.parent_tool_use_id
            if event.session_id:
                payload["session_id"] = event.session_id
            return payload
        payload = {"type": "tool_use", "tool_name": event.tool_name}
        if event.tool_input:
            payload["tool_input"] = event.tool_input
        if event.tool_use_id:
            payload["tool_use_id"] = event.tool_use_id
        if event.parent_tool_use_id:
            payload["parent_tool_use_id"] = event.parent_tool_use_id
        if event.request_id:
            payload["request_id"] = event.request_id
        if event.session_id:
            payload["session_id"] = event.session_id
        if event.backgroundable:
            payload["backgroundable"] = True
        touches: list[dict] = []
        if event.file_touches:
            touches = [
                t for t in event.file_touches
                if isinstance(t, dict) and t.get("file_path")
            ]
        if not touches:
            touches = extract_file_touches(event.tool_name, event.tool_input)
        if touches:
            payload["file_touch"] = touches[0]
            if len(touches) > 1:
                payload["file_touches"] = touches
        return payload
    if isinstance(event, ThinkingEvent):
        payload = {"type": "thinking", "text": event.text}
        if event.parent_tool_use_id:
            payload["parent_tool_use_id"] = event.parent_tool_use_id
        return payload
    if isinstance(event, SystemStatusEvent):
        return {"type": "status", "message": event.status or ""}
    if isinstance(event, ModelChangedEvent):
        return {"type": "model_changed", "model": event.model}
    if isinstance(event, TokenUsageEvent):
        return {
            "type": "token_usage",
            "input_tokens": event.input_tokens,
            "output_tokens": event.output_tokens,
        }
    if isinstance(event, ResultEvent):
        payload = {
            "type": "result",
            "text": event.result,
            "is_error": event.is_error,
            "effective_model": event.effective_model,
            "usage": event.usage,
            "session_id": event.session_id or "",
        }
        if event.stopped:
            payload["stopped"] = True
        if event.fallback_final:
            payload["fallback_final"] = True
        if event.recovered_with_pending:
            payload["recovered_with_pending"] = True
        if event.quota:
            payload["quota"] = event.quota
        return payload
    if isinstance(event, PermissionRequestEvent):
        payload = {
            "type": "permission_request",
            "tool_name": event.tool_name,
            "tool_input": event.tool_input,
            "message": event.message,
            "request_id": event.request_id,
        }
        if event.session_id:
            payload["session_id"] = event.session_id
        return payload
    if isinstance(event, ModelCapabilityQuestionEvent):
        return {
            "type": "model_capability_question",
            "request_id": event.request_id,
            "missing": event.missing,
            "current_model": event.current_model,
            "candidates": event.candidates,
            "timeout_s": event.timeout_s,
        }
    return None


class ChatStream:
    """A single in-flight chat response, buffered for replay."""

    __slots__ = (
        "_events",
        "_subs",
        "_done",
        "prompt_text",
        "_pending",
        "_draining",
        "_pending_id_seq",
        # Turn indexes of steers delivered into this turn; the drive loop
        # stamps them with the turn's completion time at the ResultEvent.
        "steered_turn_indexes",
        # Delivery ids confirmed before ``steer_queue`` recorded them on the
        # entries (the echo can beat ``steer_turn``'s return).
        "early_steer_deliveries",
        # True only while ``steer_turn`` is being awaited: a delivery id that
        # arrives then may belong to the dispatch in flight.
        "steer_inflight",
        # The user-turn index of the turn now running, so a steer delivered
        # into it can inherit its unattended flag.
        "running_turn_index",
        # Returns the chat's waiting (parked) entries, which stay visible in
        # this stream's ``queue_state`` but are never flushed by it.
        "waiting",
        "user_stopped",
        "moved_to_background",
        "move_epoch",
        "move_in_flight",
        "move_succeeded",
        "background",
        # The asyncio.Task currently iterating this stream's turn events.
        # Set by the drive loop before awaiting the turn; cleared when the
        # turn ends. `stop_chat` cancels it after the provider-level stop
        # fails to end the turn within its grace window, so Stop always
        # reacts immediately instead of waiting on a hung provider.
        "turn_task",
        # Set by `stop_chat` immediately before it cancels `turn_task`, so the
        # drive loop can tell "the turn we were awaiting was force-closed" from
        # "this drive task is itself being cancelled" (shutdown). `user_stopped`
        # cannot: it stays true until the end of the turn, so a shutdown
        # cancellation landing anywhere in that stretch was swallowed and the
        # drive loop carried on past its own cancellation.
        "force_closing",
        "accepting_queue",
        # Open capability questions keyed by request_id, each
        # {"event": asyncio.Event(), "answer": None}. Populated by the
        # image-capability pre-flight, resolved via `resolve_capability`.
        "pending_capability",
        "code_mode_continue",
        "reload_continue",
        "network_retry_turn",
        "network_retry_armed",
    )

    def __init__(self, prompt_text: str = "", *, background: bool = False) -> None:
        self._events: list[dict] = []
        self._subs: set[asyncio.Queue[dict | None]] = set()
        self._done: bool = False
        # Original user prompt (kept so auto-title can run after clients reconnect).
        self.prompt_text: str = prompt_text
        # Messages queued by the user while this stream was running. Flushed
        # one at a time into follow-up turns after this one finishes. Each
        # entry is {"id": str, "text": str, "images": list[str]} where images
        # are ref strings (resolved back to ImageAttachments at flush time).
        self._pending: list[dict] = []
        # Entry currently removed by drain_one but not yet echoed, parked, or
        # intentionally skipped. It remains an owner for WS idempotency while
        # the drive loop awaits work such as a provider reload.
        self._draining: list[dict] = []
        self._pending_id_seq = 0
        self.steered_turn_indexes: list[int] = []
        self.early_steer_deliveries: set[str] = set()
        self.steer_inflight: bool = False
        self.running_turn_index: int | None = None
        self.waiting: Callable[[], list[dict]] | None = None
        # Set by `stop_chat()` when the user hits the Stop button. Stop holds
        # the whole queue: the drive loop parks every queued message on the
        # chat (chips stay) instead of flushing it, so nothing is sent after
        # the user said stop. A queued message goes out only when the user
        # sends it (Steer / Send now) or sends a new message.
        self.user_stopped: bool = False
        # Marked at request time by move_chat_to_background, so this turn's
        # reply is interim and its announce waits for the wake. Marking before
        # the provider is awaited means the turn-end read can never be too
        # early, and a slow response can never land on a later turn (#1413).
        self.moved_to_background: bool = False
        # Bumped when a queued follow-up turn begins. A move request that
        # resolves afterwards must not touch the turn that replaced its own
        # (#1413 review round 2).
        self.move_epoch: int = 0
        # Move requests of the current epoch still awaiting an answer, and
        # whether one of them moved something. Together they let an
        # unsuccessful request undo its own mark without erasing a successful
        # one (#1413 review round 3).
        self.move_in_flight: int = 0
        self.move_succeeded: bool = False
        # True for streams carrying between-turns background-subagent events
        # (no user prompt drove them). A background stream must never absorb
        # queued user messages — a user send while one is active
        # starts a real turn instead (the drain is cancelled first).
        self.background: bool = background
        self.turn_task: asyncio.Task | None = None
        self.force_closing: bool = False
        # Cleared by the drive loop the moment it decides this stream is over.
        # `drain_one()` and the stream teardown are not one atomic step, so a
        # message enqueued between them used to land in `_pending` that nothing
        # would ever read: the loop had already looked, and `finally` tore the
        # stream down. `queue_message` refuses once this is False, so the
        # caller starts a real turn instead of parking a message in a queue
        # with no reader.
        self.accepting_queue: bool = True
        self.pending_capability: dict[str, dict] | None = None
        # Set by `arm_code_mode_continuation` when the agent switched this chat
        # into code mode mid-turn; the drive loop consumes it once at turn end.
        self.code_mode_continue: bool = False
        self.reload_continue: bool = False
        # True when this turn began with a network retry pending on the chat: the
        # retried turn itself, or a user turn that just cleared it. Its end settles
        # the schedule row (ProjectChatManager._settle_schedule_retry).
        self.network_retry_turn: bool = False
        # Set when the agent armed a network retry during this turn, so the turn end
        # does not clear the retry it just asked for (chat_streaming turn end).
        self.network_retry_armed: bool = False

    def enqueue(
        self,
        text: str,
        images: list[str] | None = None,
        entry_id: str | None = None,
        delivery: str = "queue",
        origin: str = "",
        send_id: str = "",
    ) -> str:
        """Queue a user message to flush after this stream finishes.

        Returns the queue entry id. If ``entry_id`` is supplied by the client,
        use it so the frontend and backend can reference the same item for
        reorder/edit/remove. Otherwise generate one.

        ``delivery`` is ``"queue"`` (a follow-up turn) or ``"steer"`` (handed
        to the running turn by ``ProjectChatManager.steer_queue``).
        ``steer_id`` stays empty until that dispatch.
        ``send_id`` is the client send id, so a repeated frame for the same
        logical send is recognised instead of queued twice.
        """
        self._pending_id_seq += 1
        resolved_id = entry_id or f"q-{self._pending_id_seq}-{uuid.uuid4().hex[:8]}"
        entry: dict = {
            "id": resolved_id,
            "text": text,
            "images": list(images or []),
            "delivery": delivery,
            "steer_id": "",
        }
        if origin:
            entry["origin"] = origin
        if send_id:
            entry["send_id"] = send_id
        self._pending.append(entry)
        return resolved_id

    def has_pending_send(self, send_id: str) -> bool:
        """True when a queued entry already carries this client send id."""
        if not send_id:
            return False
        return any(
            entry.get("send_id") == send_id
            or send_id in (entry.get("send_ids") or [])
            for entry in [*self._pending, *self._draining]
        )

    def drain_one(self) -> dict | None:
        """Return and remove the next queued message, or None if empty."""
        if not self._pending:
            return None
        entry = _flushed_entry(self._pending.pop(0))
        self._draining.append(entry)
        return entry

    def finish_drain_one(self, entry: dict) -> None:
        """Release the idempotency owner once a drained entry changes state."""
        self._draining = [current for current in self._draining if current is not entry]

    def take_draining(self) -> list[dict]:
        """Take entries left in the drain window for teardown parking."""
        entries = self._draining
        self._draining = []
        return entries

    def drain_pending(self) -> list[dict]:
        """Return and clear any queued messages. Kept for compatibility."""
        out = [_flushed_entry(entry) for entry in self._pending]
        self._pending.clear()
        return out

    def reorder_pending(self, entry_id: str, before_id: str | None = None) -> bool:
        """Move the entry with ``entry_id`` just before ``before_id``.

        If ``before_id`` is None, move to the end. Returns True on success.
        """
        return reorder_pending_list(self._pending, entry_id, before_id)

    def edit_pending(self, entry_id: str, text: str, images: list[str] | None = None) -> bool:
        """Update text and images of the queued entry with ``entry_id``.

        Refused for an entry already handed to the provider as a steer.
        """
        if self._is_steering(entry_id):
            return False
        return edit_pending_list(self._pending, entry_id, text, images)

    def remove_pending(self, entry_id: str) -> bool:
        """Remove the queued entry with ``entry_id`` (not one in flight)."""
        if self._is_steering(entry_id):
            return False
        return remove_pending_list(self._pending, entry_id)

    def _is_steering(self, entry_id: str) -> bool:
        return any(p.get("id") == entry_id and p.get("steer_id") for p in self._pending)

    def adopt_waiting(self, entries: list[dict], delivery: str) -> None:
        """Take waiting (parked) entries into this live queue, ahead of its own.

        They were queued earlier, so list order puts them first.
        """
        adopted: list[dict] = []
        for entry in entries:
            d: dict = {
                "id": entry.get("id", ""),
                "text": entry.get("text", ""),
                "images": list(entry.get("images") or []),
                "delivery": delivery,
                "steer_id": "",
            }
            if entry.get("origin"):
                d["origin"] = entry["origin"]
            if entry.get("send_id"):
                d["send_id"] = str(entry["send_id"])
            if entry.get("send_ids"):
                d["send_ids"] = list(entry["send_ids"])
            adopted.append(d)
        self._pending = [*adopted, *self._pending]

    def set_delivery(self, entry_ids: list[str], delivery: str) -> bool:
        """Switch entries between ``"queue"`` and ``"steer"``.

        An entry already handed to the provider is locked: it is skipped (and
        never moved back to ``"queue"``). Returns True if any named entry
        exists and is not locked.
        """
        wanted = set(entry_ids)
        changed = False
        for entry in self._pending:
            if entry.get("id") in wanted and not entry.get("steer_id"):
                entry["delivery"] = delivery
                changed = True
        return changed

    def steer_candidates(self) -> list[dict]:
        """Steer-mode entries not yet handed to the provider, in list order.

        An entry with neither text nor images has nothing to send.
        """
        return [
            dict(entry)
            for entry in self._pending
            if entry.get("delivery") == "steer"
            and not entry.get("steer_id")
            and (str(entry.get("text", "")).strip() or entry.get("images"))
        ]

    def revert_blank_steers(self) -> list[str]:
        """Revert steer-mode entries with nothing to send; return their ids."""
        reverted: list[str] = []
        for entry in self._pending:
            if (
                entry.get("delivery") == "steer"
                and not entry.get("steer_id")
                and not str(entry.get("text", "")).strip()
                and not entry.get("images")
            ):
                entry["delivery"] = "queue"
                reverted.append(str(entry.get("id", "")))
        return reverted

    def mark_steering(self, entry_ids: list[str], steer_id: str) -> None:
        """Record that ``entry_ids`` were handed to the provider as ``steer_id``."""
        wanted = set(entry_ids)
        for entry in self._pending:
            if entry.get("id") in wanted:
                entry["steer_id"] = steer_id

    def confirm_steering(self, steer_id: str) -> bool:
        """Swap the dispatch placeholder for the provider's delivery id.

        False when no entry is still waiting on it (a Stop or turn end already
        reverted them), so the caller does not resurrect them.
        """
        confirmed = False
        for entry in self._pending:
            if entry.get("steer_id") == STEER_DISPATCHING:
                entry["steer_id"] = steer_id
                confirmed = True
        return confirmed

    def take_steered(self, steer_id: str) -> list[dict]:
        """Remove and return the entries dispatched as ``steer_id``."""
        taken = [p for p in self._pending if p.get("steer_id") == steer_id]
        self._pending = [p for p in self._pending if p.get("steer_id") != steer_id]
        return taken

    def revert_steers(self) -> list[str]:
        """Put every steer-mode entry back in queue mode; return their ids."""
        reverted: list[str] = []
        for entry in self._pending:
            if entry.get("delivery") == "steer":
                entry["delivery"] = "queue"
                entry["steer_id"] = ""
                reverted.append(str(entry.get("id", "")))
        return reverted

    @property
    def pending(self) -> list[dict]:
        """Read-only snapshot of queued messages in order."""
        return list(self._pending)

    def pending_public(self) -> list[dict]:
        """The whole visible queue in the ``queue_state`` wire shape.

        Waiting entries first (they were queued earlier), then this stream's own
        queue. See ``public_queue``.
        """
        waiting = self.waiting() if self.waiting is not None else []
        return public_queue(self._pending, waiting)

    def publish_pending(self) -> None:
        """Publish the live queue, empty included, so a client clears its chips."""
        self.publish({"type": "queue_state", "queue": self.pending_public()})

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    @property
    def subscriber_count(self) -> int:
        """Clients currently attached to this stream.

        Zero means nothing this turn emits reaches a browser right now, which is
        how ``file_surface`` can tell that its pin request went nowhere.
        """
        return len(self._subs)

    @property
    def done(self) -> bool:
        return self._done

    def buffered_events(self) -> list[dict]:
        """Return a shallow copy of currently buffered events."""
        return list(self._events)

    def publish(self, payload: dict) -> None:
        """Record an event and fan-out to subscribers."""
        self._events.append(payload)
        for queue in list(self._subs):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                logger.warning("Chat stream subscriber queue full, dropping event")

    def publish_queue_state(self, queue: list[dict]) -> None:
        """Publish a parked queue while this stream can still reach subscribers.

        An empty list is not published: the client treats ``queue_state`` as
        the whole queue and would clear the chips. A finished stream has
        already delivered its end sentinel, so a late event never arrives.
        """
        if self._done or not queue:
            return
        self.publish({"type": "queue_state", "queue": public_queue([], queue)})

    def publish_live(self, payload: dict) -> None:
        """Fan out an ephemeral control event without adding it to replay."""
        for queue in list(self._subs):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                logger.warning("Chat stream subscriber queue full, dropping event")

    def deny_tool_use(self, tool_use_id: str) -> None:
        """Retract the file card for a tool call that was refused.

        ``file_touch`` is attached when a call is *requested*, so a refused
        Write still painted an Outputs chip for a file that was never created.
        The permission gate keys requests by ``tool_use_id`` — the same id the
        ``tool_use`` event carries — so the two can be matched here: strip the
        touch from the replay buffer (reload, second tab) and tell live clients
        to drop the chip they already rendered.
        """
        if not tool_use_id:
            return
        for ev in self._events:
            if ev.get("type") == "tool_use" and ev.get("tool_use_id") == tool_use_id:
                ev.pop("file_touch", None)
                ev.pop("file_touches", None)
        self.publish({"type": "tool_denied", "tool_use_id": tool_use_id})

    def resolve_permission(self, request_id: str, session_id: str = "") -> bool:
        """Drop a previously-published ``permission_request`` from replay.

        Subscribers that connect after a permission has been answered
        (chat reopened, WS reconnect, second tab) would otherwise replay
        the buffered prompt and re-render an Approve/Deny card for a
        request that's already been resolved server-side. Stripping the
        event here keeps the buffer in sync with reality so a fresh
        client paints the chat without phantom prompts.

        Returns True if a matching event was found and removed.
        """
        if not request_id:
            return False
        before = len(self._events)
        self._events = [
            ev
            for ev in self._events
            if not (
                ev.get("type") == "permission_request"
                and ev.get("request_id") == request_id
                and (not session_id or ev.get("session_id") == session_id)
            )
        ]
        removed = len(self._events) < before
        self.publish_live({
            "type": "permission_resolved",
            "request_id": request_id,
            "session_id": session_id,
        })
        return removed

    def resolve_question(self, request_id: str, session_id: str = "") -> bool:
        """Remove a resolved native question from replay and notify clients."""
        if not request_id:
            return False
        before = len(self._events)
        self._events = [
            ev
            for ev in self._events
            if not (
                ev.get("type") == "tool_use"
                and ev.get("tool_name") == "AskUserQuestion"
                and ev.get("request_id") == request_id
                and (not session_id or ev.get("session_id") == session_id)
            )
        ]
        removed = len(self._events) < before
        self.publish_live({
            "type": "question_resolved",
            "request_id": request_id,
            "session_id": session_id,
        })
        return removed

    def open_capability(self, request_id: str) -> bool:
        """Register an open capability question for ``request_id``.

        Called by the image-capability pre-flight after it publishes the
        question event, so the waiting coroutine and the client's eventual
        ``capability_response`` share one asyncio.Event. Returns False when a
        question for this request_id is already open.
        """
        if not request_id:
            return False
        if self.pending_capability is None:
            self.pending_capability = {}
        if request_id in self.pending_capability:
            return False
        self.pending_capability[request_id] = {"event": asyncio.Event(), "answer": None}
        return True

    def resolve_capability(
        self, request_id: str, action: str, model_id: str = ""
    ) -> bool:
        """Resolve an open capability question with the client's answer.

        Strips the question from the replay buffer (so a reconnect does not
        re-render a card for an already-resolved request) and wakes the
        pre-flight waiter with ``{"action": action, "model_id": model_id}``.
        Returns False when no question is open for ``request_id``.
        """
        if not request_id:
            return False
        entry = None
        if self.pending_capability is not None:
            entry = self.pending_capability.pop(request_id, None)
        self._events = [
            ev
            for ev in self._events
            if not (
                ev.get("type") == "model_capability_question"
                and ev.get("request_id") == request_id
            )
        ]
        if entry is None:
            return False
        entry["answer"] = {"action": action, "model_id": model_id}
        entry["event"].set()
        return True

    def finish(self) -> None:
        """Mark the stream complete and notify subscribers."""
        if self._done:
            return
        self._done = True
        for queue in list(self._subs):
            try:
                queue.put_nowait(None)  # sentinel
            except asyncio.QueueFull:
                pass

    async def subscribe(self) -> AsyncIterator[dict]:
        """Iterate buffered + future events until the stream finishes."""
        queue: asyncio.Queue[dict | None] = asyncio.Queue()
        # Replay the buffer before accepting live events.
        for ev in self._events:
            queue.put_nowait(ev)
        if self._done:
            queue.put_nowait(None)
        self._subs.add(queue)
        try:
            while True:
                try:
                    item = await asyncio.wait_for(
                        queue.get(), timeout=STREAM_KEEPALIVE_SECONDS
                    )
                except asyncio.TimeoutError:
                    # Idle gap (e.g. a background tool with no parent-side
                    # events). Emit a keepalive so the socket has traffic and
                    # a dead one is detected on the next send.
                    yield {"type": "keepalive"}
                    continue
                if item is None:
                    return
                yield item
        finally:
            self._subs.discard(queue)


class ChatStreamBroker:
    """Tracks one ChatStream per chat_id."""

    def __init__(self) -> None:
        self._streams: dict[str, ChatStream] = {}

    def get(self, chat_id: str) -> ChatStream | None:
        """Return the active (not-yet-done) stream for this chat, if any."""
        stream = self._streams.get(chat_id)
        if stream is None or stream.done:
            return None
        return stream

    def register(self, chat_id: str, stream: ChatStream) -> None:
        """Replace this chat's stream without finishing whatever was there.

        KNOWN HAZARD (not fixed here): if the previous entry was live
        (`not previous.done`) and had a subscriber attached, this leaves
        that subscriber stuck. `_attach_streams` (ciao/web/routes_chat.py)
        only re-polls the broker after its current `_forward_stream` call
        returns, and that call only returns once `subscribe()` yields the
        `None` sentinel from `finish()`. A stream nobody ever finishes keeps
        yielding keepalives (see `subscribe()` above) forever, so the client
        looks alive but never re-attaches to the stream that replaced this
        one. Both current call sites (`ProjectChatManager.start_stream` and
        `_drain_between_turns`) route the old stream's `finish()` through an
        `asyncio.Task.cancel()` that is fire-and-forget relative to this
        call, so cleanup normally lands a tick later — a race window, not a
        guarantee.
        """
        self._streams[chat_id] = stream

    def clear(self, chat_id: str, stream: ChatStream | None = None) -> None:
        """Drop the stream for this chat (only if it matches, to avoid racing).

        Does not call `stream.finish()` — see the hazard note on `register`.
        """
        current = self._streams.get(chat_id)
        if current is None:
            return
        if stream is not None and current is not stream:
            return
        self._streams.pop(chat_id, None)


class EventsSubscription:
    """One consumer of `EventsHub`.

    Registered with the hub on construction, so events published after
    `EventsHub.attach()` returns are queued even before iteration starts. The
    caller must `close()` it.
    """

    _RESYNC: dict = {"type": "resync"}

    def __init__(self, hub: "EventsHub") -> None:
        self._hub = hub
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=256)

    def offer(self, payload: dict) -> None:
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            # A dropped event can be `chat_streaming_done`, which a client
            # cannot infer. Discard the backlog and end the stream so the
            # socket reconnects and takes a fresh snapshot instead.
            logger.warning("EventsHub subscriber queue full, forcing resync")
            while not self._queue.empty():
                self._queue.get_nowait()
            self._queue.put_nowait(self._RESYNC)
            self.close()

    def close(self) -> None:
        self._hub._subs.discard(self)

    async def __aiter__(self) -> AsyncIterator[dict]:
        """Yield live events until closed; ends after a forced resync."""
        while True:
            try:
                payload = await asyncio.wait_for(
                    self._queue.get(), timeout=STREAM_KEEPALIVE_SECONDS
                )
            except asyncio.TimeoutError:
                # Keep the idle /ws/events socket warm (see
                # STREAM_KEEPALIVE_SECONDS). Without traffic it can die
                # half-open and miss the `chat_streaming_done` recovery
                # signal a client uses to refetch after a quiet turn.
                yield {"type": "keepalive"}
                continue
            if payload is self._RESYNC:
                return
            yield payload


class EventsHub:
    """App-wide pub/sub for cross-chat awareness events.

    Used by the global `/ws/events` socket so a client stays informed about
    activity in chats it isn't viewing (per-project spinner, unread badges,
    in-app toasts). Distinct from `ChatStream`, which carries per-turn deltas.

    Events are fire-and-forget; no replay buffer. Subscribers only see events
    that fire after they attach; past activity comes from REST.
    """

    def __init__(self) -> None:
        self._subs: set[EventsSubscription] = set()

    @property
    def subscriber_count(self) -> int:
        """Number of live global-event consumers."""
        return len(self._subs)

    def publish(self, payload: dict) -> None:
        """Fan-out to live subscribers."""
        for sub in list(self._subs):
            sub.offer(payload)

    def attach(self) -> EventsSubscription:
        """Register a consumer now; iterate it and `close()` it when done."""
        sub = EventsSubscription(self)
        self._subs.add(sub)
        return sub
