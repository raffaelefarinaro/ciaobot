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
import contextlib
import hashlib
import json
import logging
import re

from starlette.websockets import WebSocket, WebSocketDisconnect

from ciao.web.auth import authorize_websocket
from ciao.web.chat_broker import QUEUE_DELIVERIES, STREAM_KEEPALIVE_SECONDS, ChatStream
from ciao.web.connection_tracker import ConnectionTracker
from ciao.models import ImageAttachment
from ciao.web.project_chats import ChatInfo, RestartDrainingError

logger = logging.getLogger(__name__)


def _recorded_turn_origin(chat: ChatInfo | None, turn_index: object) -> str:
    """The origin the engine recorded for a user turn, if any.

    A duplicate echo repeats what the engine stored, so a client that
    reconnects keeps the turn's tag. Nothing here reads the incoming frame:
    a repeat may not tag a turn the engine never tagged.
    """
    if chat is None or turn_index is None:
        return ""
    return str(chat.user_turn_origin.get(str(turn_index)) or "")


async def _resettle_after_question_answer(websocket: WebSocket, chat_id: str) -> None:
    """Re-settle a delegated attempt whose native question card was just answered.

    Best effort by design: the user's answer already succeeded, and a settlement
    that cannot be made is logged, never raised back at the socket. The control
    plane owns the decision (``resettle_after_question_answer``); this only finds
    it and runs the write off the event loop, the way ``routes_tasks`` runs every
    other control-plane call.
    """
    service = getattr(websocket.app.state, "mcp_service", None)
    plane = getattr(service, "control_plane", None)
    if plane is None:
        return
    try:
        await asyncio.to_thread(plane.resettle_after_question_answer, chat_id)
    except Exception:  # noqa: BLE001 — a settlement must never fail the answer
        logger.exception(
            "delegation: could not re-settle the attempt for chat %s after its card "
            "was answered",
            chat_id,
        )


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

# Bounds on a `queue_set_delivery` frame: a queue is a handful of chips, and
# entry ids are client-chosen short strings.
_MAX_QUEUE_FRAME_IDS = 100
_MAX_QUEUE_ENTRY_ID_LEN = 128

# A client send id is compared byte-for-byte, logged, and persisted in chat
# metadata, so it is restricted to a token alphabet: no whitespace, control
# characters or quotes can reach a log line or the metadata file.
_MAX_SEND_ID_LEN = 64
_SEND_ID_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


async def _attach_streams(websocket: WebSocket, pcm, chat_id: str) -> None:
    """Forward every broker stream for this chat until the socket dies.

    While idle it sends a keepalive every STREAM_KEEPALIVE_SECONDS so the PWA
    watchdog does not read silence as a dead socket.
    """
    last: ChatStream | None = None
    loop = asyncio.get_running_loop()
    last_sent = loop.time()
    while True:
        stream = pcm.get_active_stream(chat_id)
        if stream is not None and stream is not last:
            last = stream
            if not await _forward_stream(websocket, stream):
                return
            # The stream's own subscribe() was writing keepalives until it
            # ended.
            last_sent = loop.time()
            # Immediately re-check: a queued follow-up or background stream
            # may already have replaced the one that just finished.
            continue
        if loop.time() - last_sent >= STREAM_KEEPALIVE_SECONDS:
            try:
                await websocket.send_json({"type": "keepalive"})
            except (WebSocketDisconnect, RuntimeError):
                return
            last_sent = loop.time()
        await asyncio.sleep(_ATTACH_POLL_SECONDS)


async def ws_chat(websocket: WebSocket) -> None:
    """Per-chat streaming WebSocket."""
    if not await authorize_websocket(websocket):
        return

    await websocket.accept()
    chat_id = websocket.path_params["chat_id"]
    pcm = websocket.app.state.project_chat_manager
    app = websocket.app

    if pcm.get_chat(chat_id) is None:
        await websocket.send_json({"type": "error", "message": "chat not found"})
        await websocket.close(code=4004)
        return

    tracker: ConnectionTracker | None = getattr(
        websocket.app.state, "connection_tracker", None
    )
    conn_id = tracker.register(websocket, "chat", chat_id=chat_id) if tracker else ""

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
                # provider acknowledgement is sent back as a result event; the
                # client keeps the card retryable until that acknowledgement.
                request_id = str(msg.get("request_id", ""))
                raw_approved = msg.get("approved")
                session_id = str(msg.get("session_id") or "").strip()
                reason = str(msg.get("reason", ""))
                if request_id and not isinstance(raw_approved, bool):
                    try:
                        await websocket.send_json({
                            "type": "permission_response_result",
                            "request_id": request_id,
                            **({"session_id": session_id} if session_id else {}),
                            "ok": False,
                            "error": "Permission approval must be a JSON boolean.",
                            "retryable": False,
                        })
                    except (WebSocketDisconnect, RuntimeError):
                        break
                    continue
                if request_id:
                    permission_payload = {
                        "request_id": request_id,
                        "approved": bool(raw_approved),
                        "reason": reason,
                    }
                    if session_id:
                        permission_payload["session_id"] = session_id
                    result = await pcm.respond_permission(
                        chat_id,
                        **permission_payload,
                    )
                    try:
                        await websocket.send_json({
                            "type": "permission_response_result",
                            "request_id": request_id,
                            "session_id": session_id,
                            "ok": result.ok,
                            "error": result.error,
                            "retryable": result.retryable,
                            **({"expired": True} if result.expired else {}),
                            **({"already_resolved": True} if result.already_resolved else {}),
                        })
                    except (WebSocketDisconnect, RuntimeError):
                        break
                continue

            if msg_type == "question_response":
                request_id = str(msg.get("request_id", "")).strip()
                session_id = str(msg.get("session_id") or "").strip()
                raw_answers = msg.get("answers")
                answers: dict[str, list[str]] = {}
                if isinstance(raw_answers, dict):
                    for question_id, values in raw_answers.items():
                        if isinstance(values, list):
                            answers[str(question_id)] = [
                                str(value) for value in values
                            ]
                action = str(msg.get("action") or "").strip().lower()
                if request_id and action not in {"reply", "cancel"}:
                    try:
                        await websocket.send_json({
                            "type": "question_response_result",
                            "request_id": request_id,
                            **({"session_id": session_id} if session_id else {}),
                            "ok": False,
                            "state": "rejected",
                            "error": "Question action must be reply or cancel.",
                            "retryable": False,
                        })
                    except (WebSocketDisconnect, RuntimeError):
                        break
                    continue
                if request_id:
                    question_payload = {
                        "request_id": request_id,
                        "answers": answers,
                        "action": action,
                    }
                    if session_id:
                        question_payload["session_id"] = session_id
                    result = await pcm.respond_question(
                        chat_id,
                        **question_payload,
                    )
                    try:
                        await websocket.send_json({
                            "type": "question_response_result",
                            "request_id": request_id,
                            **({"session_id": session_id} if session_id else {}),
                            "ok": result.ok,
                            "state": "cancelled" if action == "cancel" else "answered",
                            "error": result.error,
                            "retryable": result.retryable,
                            **({"delivered_as_message": True} if result.delivered_as_message else {}),
                            **({"already_resolved": True} if result.already_resolved else {}),
                        })
                    except (WebSocketDisconnect, RuntimeError):
                        break
                    if result.ok and not result.delivered_as_message:
                        # Answering a native card starts no turn, so nothing else
                        # re-settles a delegated attempt the card was holding at
                        # ``needs_you`` with a `done` report already on it (#1110).
                        # Best effort: a settlement that cannot be made must not
                        # fail the answer the user just gave. A dead form's reply
                        # starts a turn, whose own result settles the attempt.
                        await _resettle_after_question_answer(websocket, chat_id)
                continue

            if msg_type == "capability_response":
                # Answer to a prior ``model_capability_question`` (image
                # input against a non-vision model). ``action`` is switch,
                # picker, or cancel; switch carries the chosen ``model_id``.
                # Stale request ids are dropped silently like the other
                # response verbs.
                request_id = str(msg.get("request_id", "")).strip()
                action = str(msg.get("action", "")).strip()
                model_id = str(msg.get("model_id", "")).strip()
                if request_id and action:
                    pcm.respond_capability(
                        chat_id,
                        request_id=request_id,
                        action=action,
                        model_id=model_id,
                    )
                continue

            if msg_type == "queue_reorder":
                pcm.reorder_queue(
                    chat_id,
                    entry_id=str(msg.get("entry_id", "")),
                    before_id=str(msg.get("before_id")) if msg.get("before_id") else None,
                )
                continue

            if msg_type == "queue_edit":
                images: list[ImageAttachment] = []
                for ref in msg.get("images", []):
                    attachment = pcm.resolve_image_ref(ref)
                    if attachment:
                        images.append(attachment)
                pcm.edit_queue(
                    chat_id,
                    entry_id=str(msg.get("entry_id", "")),
                    text=str(msg.get("text", "")),
                    images=images or None,
                )
                continue

            if msg_type == "queue_remove":
                pcm.remove_queue(
                    chat_id,
                    entry_id=str(msg.get("entry_id", "")),
                )
                continue

            if msg_type == "queue_set_delivery":
                # Same trust rules as the other queue frames: the socket is
                # bound to this chat, so ids only ever match its own queue.
                # Shape and size are checked here; the manager checks values.
                raw_ids = msg.get("entry_ids")
                delivery_value = msg.get("delivery")
                if (
                    isinstance(raw_ids, list)
                    and 0 < len(raw_ids) <= _MAX_QUEUE_FRAME_IDS
                    and all(
                        isinstance(x, str) and 0 < len(x) <= _MAX_QUEUE_ENTRY_ID_LEN
                        for x in raw_ids
                    )
                    and delivery_value in QUEUE_DELIVERIES
                ):
                    try:
                        await pcm.set_queue_delivery(chat_id, raw_ids, delivery_value)
                    except RestartDrainingError as exc:
                        # "Send now" can start a turn; same answer as a message.
                        try:
                            await websocket.send_json({
                                "type": "server_restarting",
                                "message": str(exc),
                            })
                        except (WebSocketDisconnect, RuntimeError):
                            break
                    except Exception as exc:
                        logger.exception("queue_set_delivery failed for %s", chat_id)
                        try:
                            await websocket.send_json({"type": "error", "message": str(exc)})
                        except (WebSocketDisconnect, RuntimeError):
                            break
                continue

            if msg_type == "message":
                text = msg.get("text", "")
                images = []
                for ref in msg.get("images", []):
                    attachment = pcm.resolve_image_ref(ref)
                    if attachment:
                        images.append(attachment)

                # An image-only follow-up is a real message: the images are
                # the content, so they go out with an empty prompt instead of
                # being dropped without a word (#1112). Only an entry with
                # neither text nor images has nothing to send.
                if not str(text or "").strip() and not images:
                    continue

                # A client with an open socket to a chat that was archived
                # from another tab/device (or a stale UI) can still send.
                # Reject before start_stream fires a background task that
                # raises "Cannot send messages to an archived chat" —
                # which logs a traceback and surfaces a raw error bubble.
                target_chat = pcm.get_chat(chat_id)
                if target_chat is not None and target_chat.archived:
                    try:
                        await websocket.send_json({
                            "type": "error",
                            "message": "This chat has been archived.",
                            "archived": True,
                        })
                    except (WebSocketDisconnect, RuntimeError):
                        break
                    continue

                # Idempotency. The client mints a send id per logical send and
                # puts it on the frame, so a recovery resend of a message the
                # engine already owns is answered with a duplicate echo
                # instead of starting a second turn. A deliberate repeat of
                # the same text carries a fresh id and is never suppressed.
                send_id = _SEND_ID_UNSAFE.sub("", str(msg.get("send_id") or ""))[:_MAX_SEND_ID_LEN]
                owned_turn = (
                    pcm.find_turn_by_send_id(chat_id, send_id)
                    if send_id
                    else None
                )
                duplicate = bool(send_id) and (
                    owned_turn is not None
                    or bool(pcm.has_pending_send(chat_id, send_id))
                )
                logger.debug(
                    "ws send chat=%s send_id=%s len=%d images=%d duplicate=%s",
                    chat_id,
                    send_id,
                    len(str(text or "")),
                    len(images or []),
                    duplicate,
                )
                if duplicate:
                    logger.info(
                        "Suppressing duplicate send %s for chat %s", send_id, chat_id
                    )
                    duplicate_payload: dict = {
                        "type": "user_echo",
                        "duplicate": True,
                        "text": text,
                        "images": [img.original_filename for img in images or []],
                        "send_id": send_id,
                    }
                    if owned_turn is not None:
                        duplicate_payload["turn_index"] = owned_turn.get("turn_index")
                        sent_at = str(owned_turn.get("sent_at") or "")
                        if sent_at:
                            duplicate_payload["sent_at"] = sent_at
                        # The echo repeats the tag the engine recorded, so a
                        # client that reconnects after a recovery resend does
                        # not lose the turn's notice. The frame's own tag is
                        # never trusted: a repeat cannot rewrite the turn.
                        recorded_origin = _recorded_turn_origin(
                            target_chat, owned_turn.get("turn_index")
                        )
                        if recorded_origin:
                            duplicate_payload["origin"] = recorded_origin
                    try:
                        await websocket.send_json(duplicate_payload)
                    except (WebSocketDisconnect, RuntimeError):
                        break
                    continue

                # Concurrent-send handling. A message sent while a stream is in
                # flight is buffered. It flushes as the next turn, or, when the
                # client marks it `delivery: "steer"`, is handed to the running
                # turn now (an older PWA sends no delivery and gets the queue).
                delivery = "steer" if msg.get("delivery") == "steer" else "queue"
                # A client may tag a deliberate retry of its own turn, so the
                # PWA draws a notice instead of a user bubble. The tag is
                # presentation metadata: only the exact "retry" value is
                # honoured, so no engine origin (schedule, background_run, …)
                # can be forged from the wire. An untagged send is unchanged.
                origin = "retry" if msg.get("origin") == "retry" else ""
                origin_kwargs = {"origin": origin} if origin else {}
                active_stream = pcm.get_active_stream(chat_id)
                if active_stream is not None:
                    handled = pcm.queue_message(
                        chat_id, text, images=images or None,
                        entry_id=str(msg.get("entry_id")) if msg.get("entry_id") else None,
                        delivery=delivery,
                        send_id=send_id,
                        **origin_kwargs,
                    )
                    if handled:
                        if delivery == "steer":
                            await pcm.steer_queue(chat_id)
                        continue
                    # Else: the stream raced-finish between check and queue;
                    # fall through to start a new stream below.

                # Start the turn BEFORE touching the socket. The stream runs
                # in a broker-owned background task and buffers every event
                # (user_echo included) for replay, so it does not need this
                # connection to survive. The previous order wrote a
                # "thinking" status first and dropped the whole message when
                # that write hit a socket that had just died (WebKit
                # suspension closes it right after the send frame arrives) —
                # the turn never started and the client's optimistic bubble
                # was wiped by the reconnecting history reload.
                try:
                    pcm.start_stream(
                        chat_id, text, images=images or None, send_id=send_id,
                        **origin_kwargs,
                    )
                except RestartDrainingError as exc:
                    # Not a chat failure — the server is draining for
                    # restart. Tell the client so it can show the restart
                    # overlay instead of an inline "Fix this error" bubble.
                    try:
                        await websocket.send_json({
                            "type": "server_restarting",
                            "message": str(exc),
                        })
                    except (WebSocketDisconnect, RuntimeError):
                        break
                    continue
                except Exception as exc:
                    logger.exception("Failed to start stream for %s", chat_id)
                    try:
                        await websocket.send_json({"type": "error", "message": str(exc)})
                    except (WebSocketDisconnect, RuntimeError):
                        break
                    continue
                # Best-effort ack. A dead socket here must not drop the turn:
                # breaking only ends this connection, and the reconnecting
                # client replays the buffered user_echo from the live stream.
                try:
                    await websocket.send_json({"type": "status", "message": "thinking"})
                except (WebSocketDisconnect, RuntimeError):
                    break
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
        if tracker and conn_id:
            tracker.unregister(conn_id)


async def ws_events(websocket: WebSocket) -> None:
    """Global awareness WebSocket. Streams cross-chat lifecycle events.

    Event payloads (JSON):
    - `chat_streaming_started`  {chat_id, project_id, background?}  (background: a wake)
    - `chat_streaming_done`     {chat_id, project_id, is_error, last_error}
    - `chat_result_ready`       {chat_id, project_id, title, snippet}
    - `chat_subagents_ready`    {chat_id, project_id, remaining}
    - `chat_read`               {chat_id, last_read_at}
    - `chat_unread`             {chat_id, last_read_at}  (marked unread on purpose)
    - `chat_pin_changed`        {chat_id, path, dismissed_paths, revision}
    - `chat_terminal_offer_changed` {chat_id, terminal_offer}  (null = cleared)
    - `chat_title`              {chat_id, title}
    - `server_restarting`       {message}  (restart drain began; show overlay)

    On connect, sends a snapshot of currently-active streams so a fresh client
    can paint sidebar indicators without waiting for the next event.
    """
    if not await authorize_websocket(websocket):
        return

    await websocket.accept()
    pcm = websocket.app.state.project_chat_manager

    # Attach before the snapshot is built: an event published between the
    # snapshot and the subscription would otherwise be lost, and a lost
    # `chat_streaming_done` leaves the client believing the turn still runs.
    subscription = pcm.events.attach()

    # Snapshot: tell the client which chats are currently streaming so the
    # sidebar dots render immediately on reload.
    try:
        snapshot = []
        for cid in pcm.active_stream_chat_ids():
            chat = pcm.get_chat(cid)
            entry = {
                "chat_id": cid,
                "project_id": chat.project_id if chat else "",
            }
            if pcm.background_stream_active(cid):
                entry["background"] = True
            snapshot.append(entry)
        await websocket.send_json({
            "type": "snapshot",
            "active_streams": snapshot,
            # Chats with background subagents still running, so a fresh
            # client can paint the "N agents running" indicator immediately
            # (and clear a count left stale by a missed event during a gap).
            "background_agents": pcm.background_agent_counts,
            # Late connectors that missed `server_restarting` still get the
            # overlay instead of a chat-level turn rejection.
            "restarting": bool(getattr(pcm, "_restart_draining", False)),
            # Durable pinned-file state per chat id (#1118), each value the
            # same `{path, dismissed_paths, revision}` payload the
            # `chat_pin_changed` event and a pin PATCH carry. Every persisted
            # chat is listed, INCLUDING unpinned and archived ones: a client
            # reconnecting after a gap has to be able to close a pin the engine
            # no longer holds, and it cannot do that for a chat that is absent.
            "chat_pins": pcm.chat_pin_states,
            # The active terminal offer of every chat that has one, each
            # value the same `{id, command}` payload the
            # `chat_terminal_offer_changed` event carries. Sparse and
            # COMPLETE: the map REPLACES the client's cache, so a chat that
            # is missing from it has no offer and a stale offer is cleared
            # by its absence.
            "chat_terminal_offers": pcm.chat_terminal_offer_states,
            "keyboard_shortcuts": (
                websocket.app.state.app_settings.settings.keyboard_shortcuts or {}
                if getattr(websocket.app.state, "app_settings", None) is not None else {}
            ),
            "keyboard_send_mode": (
                websocket.app.state.app_settings.settings.keyboard_send_mode or "modifier"
                if getattr(websocket.app.state, "app_settings", None) is not None else "modifier"
            ),
            "mid_turn_delivery": (
                websocket.app.state.app_settings.settings.mid_turn_delivery or "queue"
                if getattr(websocket.app.state, "app_settings", None) is not None else "queue"
            ),
            "appearance": (
                websocket.app.state.app_settings.appearance_payload()
                if getattr(websocket.app.state, "app_settings", None) is not None else None
            ),
            "keyboard_revision": (
                hashlib.sha256(
                    json.dumps([
                        websocket.app.state.app_settings.settings.keyboard_shortcuts or {},
                        websocket.app.state.app_settings.settings.keyboard_send_mode or "modifier",
                        websocket.app.state.app_settings.settings.mid_turn_delivery or "queue",
                    ], sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                if getattr(websocket.app.state, "app_settings", None) is not None else ""
            ),
        })
    except (WebSocketDisconnect, RuntimeError):
        subscription.close()
        return
    except BaseException:
        # Anything else must not leave the subscriber queue registered.
        subscription.close()
        raise

    tracker: ConnectionTracker | None = getattr(
        websocket.app.state, "connection_tracker", None
    )
    conn_id = tracker.register(websocket, "events") if tracker else ""

    sub_task: asyncio.Task | None = None

    async def _pump_events() -> None:
        try:
            async for payload in subscription:
                await websocket.send_json(payload)
        except (WebSocketDisconnect, RuntimeError):
            return
        # The hub ended the stream (queue overflow). Close the socket so the
        # client reconnects and takes a fresh snapshot.
        with contextlib.suppress(RuntimeError):
            await websocket.close()

    sub_task = asyncio.create_task(_pump_events())
    try:
        while True:
            # Drain client → server messages so disconnects are noticed quickly.
            # The events socket is one-way; ignore any payloads.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        subscription.close()
        if sub_task is not None and not sub_task.done():
            sub_task.cancel()
        if tracker and conn_id:
            tracker.unregister(conn_id)
