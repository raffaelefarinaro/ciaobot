"""In-memory broker for per-chat streaming responses.

Purpose: decouple the SDK streaming task from the WebSocket lifecycle so that
closing the app (or a flaky network) does not abort an in-flight response. Any
number of WS clients can subscribe to the same chat's stream; new subscribers
receive a replay of buffered events (so the UI can re-attach seamlessly).
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from ciao.models import (
    AssistantTextDelta,
    PermissionRequestEvent,
    ResultEvent,
    StreamEvent,
    SystemStatusEvent,
    ThinkingEvent,
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
STREAM_KEEPALIVE_SECONDS = 15.0


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


def extract_file_touch(tool_name: str, tool_input: object) -> dict | None:
    """If this tool mutates a file, return `{file_path, action}`, else None.

    Accepts both shapes of ``tool_input``:

    - ``dict``: the raw SDK input as persisted in the session JSONL. We pick
      the path out of ``file_path`` (Claude), ``path`` (Pi), or
      ``notebook_path`` (NotebookEdit).
    - ``str``: the live stream summary produced by
      ``_summarize_tool_input`` in ``ciao/providers/claude.py``. For the
      file-touch tools it already collapses the dict into the path string
      itself, so the value passes through unchanged.

    The classification is advisory: it tells the PWA "render an inline card
    for this write" but is NOT a security boundary. The actual read goes
    through ``/api/workspace-file`` which sandboxes against
    ``config.workspace_root`` + ``extra_workspace_roots``. If the agent
    claims to have written to ``/etc/passwd`` the card appears and the
    viewer refuses with a 403.
    """
    if tool_name not in _FILE_TOUCH_TOOLS:
        return None
    action = _FILE_TOUCH_ACTIONS.get(tool_name, "touched")
    if isinstance(tool_input, dict):
        fields = _FILE_TOUCH_TOOLS[tool_name]
        for field in fields:
            path = tool_input.get(field)
            if isinstance(path, str) and path.strip():
                return {"file_path": path.strip(), "action": action}
        return None
    if isinstance(tool_input, str):
        path = tool_input.strip()
        if not path:
            return None
        return {"file_path": path, "action": action}
    return None


def event_to_json(event: StreamEvent) -> dict | None:
    """Convert a StreamEvent into the JSON payload sent to WS clients."""
    if isinstance(event, AssistantTextDelta):
        payload: dict = {"type": "text_delta", "text": event.text}
        if event.parent_tool_use_id:
            payload["parent_tool_use_id"] = event.parent_tool_use_id
        return payload
    if isinstance(event, ToolUseEvent):
        payload = {"type": "tool_use", "tool_name": event.tool_name}
        if event.tool_input:
            payload["tool_input"] = event.tool_input
        if event.tool_use_id:
            payload["tool_use_id"] = event.tool_use_id
        if event.parent_tool_use_id:
            payload["parent_tool_use_id"] = event.parent_tool_use_id
        touch = extract_file_touch(event.tool_name, event.tool_input)
        if touch:
            payload["file_touch"] = touch
        return payload
    if isinstance(event, ThinkingEvent):
        payload = {"type": "thinking", "text": event.text}
        if event.parent_tool_use_id:
            payload["parent_tool_use_id"] = event.parent_tool_use_id
        return payload
    if isinstance(event, SystemStatusEvent):
        return {"type": "status", "message": event.status or ""}
    if isinstance(event, ResultEvent):
        return {
            "type": "result",
            "text": event.result,
            "is_error": event.is_error,
            "effective_model": event.effective_model,
            "usage": event.usage,
            "session_id": event.session_id or "",
        }
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
        "user_stopped",
    )

    def __init__(self, prompt_text: str = "") -> None:
        self._events: list[dict] = []
        self._subs: set[asyncio.Queue[dict | None]] = set()
        self._done: bool = False
        # Original user prompt (kept so auto-title can run after clients reconnect).
        self.prompt_text: str = prompt_text
        # Messages queued by the user while this stream was running. Flushed
        # into a follow-up stream after this one finishes. Each entry is
        # {"text": str, "images": list[str]} where images are ref strings
        # (resolved back to ImageAttachments at flush time).
        self._pending: list[dict] = []
        # Set by `stop_chat()` when the user hits the Stop button. The drive
        # loop reads this to decide whether to still flush queued messages
        # after an interrupted turn (a user stop is intentional, not an
        # error, so queued follow-ups should still go out).
        self.user_stopped: bool = False

    def enqueue(self, text: str, images: list[str] | None = None) -> None:
        """Queue a user message to flush after this stream finishes."""
        self._pending.append({"text": text, "images": list(images or [])})

    def drain_pending(self) -> list[dict]:
        """Return and clear any queued messages."""
        out = list(self._pending)
        self._pending.clear()
        return out

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
