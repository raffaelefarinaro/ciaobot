"""WebSocket chat routes for the PWA.

Two sockets:

- `/ws/chat/{chat_id}` — per-chat: streams text deltas / tool activity for one
  conversation. Carries client → server messages: send, stop, focus.
- `/ws/events` — global: cross-chat awareness (streaming start/done, result
  ready, title updates). Drives sidebar spinners, unread badges, in-app toasts.

The actual SDK call runs in a `ChatStreamBroker`-managed background task, so
WebSocket disconnects do not abort an in-flight response. Push delivery and
auto-title generation also live in the broker (see
`ProjectChatManager.start_stream`), not here — they must work even when zero
clients are connected.
"""

from __future__ import annotations

import asyncio
import json
import logging

from starlette.websockets import WebSocket, WebSocketDisconnect

from ciao.web.auth import verify_session
from ciao.web.chat_broker import ChatStream
from ciao.models import ImageAttachment

logger = logging.getLogger(__name__)


async def _forward_stream(websocket: WebSocket, stream: ChatStream) -> bool:
    """Pump events from the stream to the WS client.

    Returns True if the client is still connected, False on disconnect.
    """
    try:
        async for payload in stream.subscribe():
            try:
                await websocket.send_json(payload)
            except (WebSocketDisconnect, RuntimeError):
                return False
    except Exception:
        logger.exception("Stream subscription error")
        return False
    return True


# How often the per-connection attach loop re-checks the broker for a stream
# it isn't forwarding yet. Streams can start without any client send —
# schedules, queued follow-ups, and between-turns background-subagent turns —
# so attachment can't be driven by the send path alone. Replay of buffered
# events makes the poll gap lossless.
_ATTACH_POLL_SECONDS = 0.5


async def _attach_streams(websocket: WebSocket, pcm, chat_id: str) -> None:
    """Forward every broker stream for this chat until the socket dies."""
    last: ChatStream | None = None
    while True:
        stream = pcm.get_active_stream(chat_id)
        if stream is not None and stream is not last:
            last = stream
            if not await _forward_stream(websocket, stream):
                return
            # Immediately re-check: a queued follow-up or background stream
            # may already have replaced the one that just finished.
            continue
        await asyncio.sleep(_ATTACH_POLL_SECONDS)


async def ws_chat(websocket: WebSocket) -> None:
    """Per-chat streaming WebSocket."""
    serializer = websocket.app.state.serializer
    if not verify_session(websocket, serializer):
        await websocket.close(code=4001, reason="unauthorized")
        return

    await websocket.accept()
    chat_id = websocket.path_params["chat_id"]
    pcm = websocket.app.state.project_chat_manager
    app = websocket.app

    if pcm.get_chat(chat_id) is None:
        await websocket.send_json({"type": "error", "message": "chat not found"})
        await websocket.close(code=4004)
        return

    is_focused = False

    def _set_focused(value: bool) -> None:
        nonlocal is_focused
        if value == is_focused:
            return
        is_focused = value
        delta = 1 if value else -1
        app.state.focused_chats[chat_id] = max(
            0, app.state.focused_chats.get(chat_id, 0) + delta
        )

    # One persistent attach loop per connection: it re-attaches to any
    # in-flight stream (buffered-event replay covers the gap) and picks up
    # streams that start without a client send — schedules, queued
    # follow-ups, and between-turns background-subagent turns.
    forward_task: asyncio.Task = asyncio.create_task(
        _attach_streams(websocket, pcm, chat_id)
    )

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                try:
                    await websocket.send_json({"type": "error", "message": "invalid json"})
                except (WebSocketDisconnect, RuntimeError):
                    break
                continue

            msg_type = msg.get("type", "message")

            if msg_type == "focus":
                _set_focused(bool(msg.get("focused")))
                continue

            if msg_type == "stop":
                await pcm.stop_chat(chat_id)
                try:
                    await websocket.send_json({"type": "status", "message": "stopped"})
                except (WebSocketDisconnect, RuntimeError):
                    break
                continue

            if msg_type == "permission_response":
                # Approve/deny reply to a prior ``permission_request``. The
                # server silently drops stale request ids (chat has no
                # provider yet, or the turn already ended); the UI is
                # expected to clear the prompt optimistically on click.
                request_id = str(msg.get("request_id", ""))
                approved = bool(msg.get("approved", False))
                reason = str(msg.get("reason", ""))
                if request_id:
                    pcm.respond_permission(
                        chat_id,
                        request_id=request_id,
                        approved=approved,
                        reason=reason,
                    )
                continue

            if msg_type == "message":
                text = msg.get("text", "")
                if not text:
                    continue

                images: list[ImageAttachment] = []
                for ref in msg.get("images", []):
                    attachment = pcm.resolve_image_ref(ref)
                    if attachment:
                        images.append(attachment)

                # Concurrent-send handling. `mode` drives behavior when a
                # stream is already in flight:
                #   "queue" (default): buffer for flush when the turn finishes.
                #   "steer":           inject into the current SDK turn; if
                #                      that fails (no active client), fall
                #                      back to queue.
                send_mode = msg.get("mode") or "queue"
                active_stream = pcm.get_active_stream(chat_id)
                if active_stream is not None:
                    handled = False
                    if send_mode == "steer":
                        try:
                            handled = await pcm.steer_stream(chat_id, text, images=images or None)
                        except Exception:
                            logger.exception("steer_stream failed for %s", chat_id)
                    if not handled:
                        handled = pcm.queue_message(chat_id, text, images=images or None)
                    if handled:
                        continue
                    # Else: the stream raced-finish between check and queue;
                    # fall through to start a new stream below.

                try:
                    await websocket.send_json({"type": "status", "message": "thinking"})
                except (WebSocketDisconnect, RuntimeError):
                    break

                try:
                    pcm.start_stream(chat_id, text, images=images or None)
                except Exception as exc:
                    logger.exception("Failed to start stream for %s", chat_id)
                    try:
                        await websocket.send_json({"type": "error", "message": str(exc)})
                    except (WebSocketDisconnect, RuntimeError):
                        break
                    continue
                # The attach loop picks the new stream up on its next tick;
                # the user_echo is buffered so nothing is lost to the gap.

    except WebSocketDisconnect:
        pass
    finally:
        if is_focused:
            app.state.focused_chats[chat_id] = max(
                0, app.state.focused_chats.get(chat_id, 0) - 1
            )
        # Detach forwarders; the underlying stream keeps running server-side
        # so a later reconnect can resume it.
        if not forward_task.done():
            forward_task.cancel()


async def ws_events(websocket: WebSocket) -> None:
    """Global awareness WebSocket. Streams cross-chat lifecycle events.

    Event payloads (JSON):
    - `chat_streaming_started`  {chat_id, project_id}
    - `chat_streaming_done`     {chat_id, project_id, is_error}
    - `chat_result_ready`       {chat_id, project_id, title, snippet}
    - `chat_subagents_ready`    {chat_id, project_id, remaining}
    - `chat_title`              {chat_id, title}
    - `open_chat`               {chat_id}  (menu-bar deep link into running PWA)

    On connect, sends a snapshot of currently-active streams so a fresh client
    can paint sidebar indicators without waiting for the next event.
    """
    serializer = websocket.app.state.serializer
    if not verify_session(websocket, serializer):
        await websocket.close(code=4001, reason="unauthorized")
        return

    await websocket.accept()
    pcm = websocket.app.state.project_chat_manager

    # Snapshot: tell the client which chats are currently streaming so the
    # sidebar dots render immediately on reload.
    try:
        snapshot = []
        for cid in pcm.active_stream_chat_ids():
            chat = pcm.get_chat(cid)
            snapshot.append({
                "chat_id": cid,
                "project_id": chat.project_id if chat else "",
            })
        await websocket.send_json({
            "type": "snapshot",
            "active_streams": snapshot,
            # Chats with background subagents still running, so a fresh
            # client can paint the "N agents running" indicator immediately
            # (and clear a count left stale by a missed event during a gap).
            "background_agents": pcm.background_agent_counts,
        })
    except (WebSocketDisconnect, RuntimeError):
        return

    sub_task: asyncio.Task | None = None

    async def _pump_events() -> None:
        async for payload in pcm.events.subscribe():
            try:
                await websocket.send_json(payload)
            except (WebSocketDisconnect, RuntimeError):
                return

    sub_task = asyncio.create_task(_pump_events())
    try:
        while True:
            # Drain client → server messages so disconnects are noticed quickly.
            # The events socket is one-way; ignore any payloads.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if sub_task is not None and not sub_task.done():
            sub_task.cancel()
