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
from typing import AsyncIterator

from ciao.models import (
    AssistantTextDelta,
    ModelChangedEvent,
    PermissionRequestEvent,
    ResultEvent,
    StreamEvent,
    SystemStatusEvent,
    ThinkingEvent,
    TokenUsageEvent,
    ToolUseEvent,
)

logger = logging.getLogger(__name__)


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
STREAM_KEEPALIVE_SECONDS = 5.0


# Tool names that mutate a file on disk and carry the target path in their input.
# `Write` and `Edit` both use `file_path`; `MultiEdit` does too. `NotebookEdit`
# stores the path under `notebook_path`. Lowercase variants seen in some
# providers (`write`, `edit`) are mirrored for safety. Pi tool calls use
# `path` instead of Claude's `file_path`, so path extraction accepts aliases.
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
}

_SHELL_REDIRECT_RE = re.compile(
    r"(?:^|[\s;|&])(?:\d*)(>>?)\s*['\"]?([^\s'\"<>&|;]+)['\"]?"
)
_SHELL_HEREDOC_RE = re.compile(
    r"(?:^|[\s;|&])(?:\w+)?\s*(>>?)\s*['\"]?([^\s'\"<>&|;]+)['\"]?\s*<<"
)
_SHELL_TOUCH_RE = re.compile(r"(?:^|[\s;|&])touch\s+([^\n;&|]+)")
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
    r"excalidraw|pdf|pptx|png|jpe?g|gif|webp|svg|avif|bmp|ico|lock|sum)$",
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
            tokens = shlex.split(match.group(1))
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
            tokens = [t for t in shlex.split(match.group(1)) if not t.startswith("-")]
        except ValueError:
            continue
        if len(tokens) >= 2:
            add(tokens[-1], "created")
    return results


def extract_file_touches(tool_name: str, tool_input: object) -> list[dict]:
    """If this tool mutates files, return ``[{file_path, action}, …]``.

    Accepts both shapes of ``tool_input``:

    - ``dict``: the raw SDK input as persisted in the session JSONL. We pick
      the path out of ``file_path`` (Claude), ``path`` (Pi), or
      ``notebook_path`` (NotebookEdit). For shell tools we parse ``command``.
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


def extract_file_touch(tool_name: str, tool_input: object) -> dict | None:
    """If this tool mutates a file, return `{file_path, action}`, else None."""
    touches = extract_file_touches(tool_name, tool_input)
    return touches[0] if touches else None


def refine_file_touch_actions(
    touches: list[dict],
    workspace_root: Path | None = None,
) -> list[dict]:
    """Upgrade ``written`` → ``created`` when the path does not yet exist.

    Tool-use events fire before the CLI executes the write, so a missing
    file at this moment means the tool is creating it.
    """
    if not touches:
        return touches
    root = workspace_root.resolve() if workspace_root is not None else None
    refined: list[dict] = []
    for touch in touches:
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
    touches = refine_file_touch_actions(touches, workspace_root)
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
    if isinstance(event, AssistantTextDelta):
        payload: dict = {"type": "text_delta", "text": event.text}
        if event.parent_tool_use_id:
            payload["parent_tool_use_id"] = event.parent_tool_use_id
        if event.phase:
            payload["phase"] = event.phase
        return payload
    if isinstance(event, ToolUseEvent):
        payload = {"type": "tool_use", "tool_name": event.tool_name}
        if event.tool_input:
            payload["tool_input"] = event.tool_input
        if event.tool_use_id:
            payload["tool_use_id"] = event.tool_use_id
        if event.parent_tool_use_id:
            payload["parent_tool_use_id"] = event.parent_tool_use_id
        if event.request_id:
            payload["request_id"] = event.request_id
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
        if event.quota:
            payload["quota"] = event.quota
        return payload
    if isinstance(event, PermissionRequestEvent):
        return {
            "type": "permission_request",
            "tool_name": event.tool_name,
            "tool_input": event.tool_input,
            "message": event.message,
            "request_id": event.request_id,
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
        "_pending_id_seq",
        "user_stopped",
        "background",
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
        self._pending_id_seq = 0
        # Set by `stop_chat()` when the user hits the Stop button. The drive
        # loop reads this to decide whether to still flush queued messages
        # after an interrupted turn (a user stop is intentional, not an
        # error, so queued follow-ups should still go out).
        self.user_stopped: bool = False
        # True for streams carrying between-turns background-subagent events
        # (no user prompt drove them). A background stream must never absorb
        # queued/steered user messages — a user send while one is active
        # starts a real turn instead (the drain is cancelled first).
        self.background: bool = background

    def enqueue(
        self,
        text: str,
        images: list[str] | None = None,
        entry_id: str | None = None,
    ) -> str:
        """Queue a user message to flush after this stream finishes.

        Returns the queue entry id. If ``entry_id`` is supplied by the client,
        use it so the frontend and backend can reference the same item for
        reorder/edit/remove. Otherwise generate one.
        """
        self._pending_id_seq += 1
        resolved_id = entry_id or f"q-{self._pending_id_seq}-{uuid.uuid4().hex[:8]}"
        self._pending.append({"id": resolved_id, "text": text, "images": list(images or [])})
        return resolved_id

    def drain_one(self) -> dict | None:
        """Return and remove the next queued message, or None if empty."""
        if not self._pending:
            return None
        return self._pending.pop(0)

    def drain_pending(self) -> list[dict]:
        """Return and clear any queued messages. Kept for compatibility."""
        out = list(self._pending)
        self._pending.clear()
        return out

    def reorder_pending(self, entry_id: str, before_id: str | None = None) -> bool:
        """Move the entry with ``entry_id`` just before ``before_id``.

        If ``before_id`` is None, move to the end. Returns True on success.
        """
        items = self._pending
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

    def edit_pending(self, entry_id: str, text: str, images: list[str] | None = None) -> bool:
        """Update text and images of the queued entry with ``entry_id``."""
        for item in self._pending:
            if item.get("id") == entry_id:
                item["text"] = text
                item["images"] = list(images or [])
                return True
        return False

    def remove_pending(self, entry_id: str) -> bool:
        """Remove the queued entry with ``entry_id``."""
        for i, item in enumerate(self._pending):
            if item.get("id") == entry_id:
                self._pending.pop(i)
                return True
        return False

    @property
    def pending(self) -> list[dict]:
        """Read-only snapshot of queued messages in order."""
        return list(self._pending)

    @property
    def has_pending(self) -> bool:
        return bool(self._pending)

    @property
    def done(self) -> bool:
        return self._done

    @property
    def event_count(self) -> int:
        return len(self._events)

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

    def resolve_permission(self, request_id: str) -> bool:
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
            )
        ]
        return len(self._events) < before

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
        self._streams[chat_id] = stream

    def clear(self, chat_id: str, stream: ChatStream | None = None) -> None:
        """Drop the stream for this chat (only if it matches, to avoid racing)."""
        current = self._streams.get(chat_id)
        if current is None:
            return
        if stream is not None and current is not stream:
            return
        self._streams.pop(chat_id, None)


class EventsHub:
    """App-wide pub/sub for cross-chat awareness events.

    Used by the global `/ws/events` socket so a client stays informed about
    activity in chats it isn't viewing (per-project spinner, unread badges,
    in-app toasts). Distinct from `ChatStream`, which carries per-turn deltas.

    Events are fire-and-forget; no replay buffer. Subscribers only see events
    that fire after they connect; past activity comes from REST.
    """

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue[dict]] = set()

    def publish(self, payload: dict) -> None:
        """Fan-out to live subscribers. Drop events for full subscriber queues."""
        for queue in list(self._subs):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                logger.warning("EventsHub subscriber queue full, dropping event")

    async def subscribe(self) -> AsyncIterator[dict]:
        """Iterate live events. Caller is responsible for cancelling the
        iterator (e.g. on WebSocket disconnect)."""
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=256)
        self._subs.add(queue)
        try:
            while True:
                try:
                    yield await asyncio.wait_for(
                        queue.get(), timeout=STREAM_KEEPALIVE_SECONDS
                    )
                except asyncio.TimeoutError:
                    # Keep the idle /ws/events socket warm (see
                    # STREAM_KEEPALIVE_SECONDS). Without traffic it can die
                    # half-open and miss the `chat_streaming_done` recovery
                    # signal a client uses to refetch after a quiet turn.
                    yield {"type": "keepalive"}
        finally:
            self._subs.discard(queue)
