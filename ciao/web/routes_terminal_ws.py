"""The browser terminal's byte stream: `/ws/terminal/{session_id}` (#1800).

This is the socket that carries a terminal session to the browser, and
nothing else: no shell is started here, no session is created, no grant is
minted. The shells belong to :mod:`ciao.terminal_sessions`, the one manager
that owns every process, and the access policy belongs to
:mod:`ciao.web.routes_terminal`, which this module asks and re-decides
neither.

The door is the same one the HTTP half uses, in the same order, and every
refusal is a close code rather than a body:

* **the terminal policy** (``terminal_policy``) — the handshake must come
  from this engine's own page, over a secure transport when it is remote.
  A WebSocket handshake is not a read, so it may only present an
  ``Origin``: there is no ``Referer`` fallback here.
* **the signed session** — through the app's own ``authorize_websocket``,
  which is also what every other ``/ws/*`` route uses. It answers 4001
  for a missing session, and 4003 for a foreign origin — though the
  terminal's own policy has already refused a foreign origin by then.
* **a live grant** (``terminal_grant``) — the browser must have unlocked
  the terminal with the password. A missing or revoked grant is 4401, and
  a grant is re-checked while the socket is open: a revoked one closes
  the socket within a second.

A socket that clears the door names a session, and a session that is gone
is 4404. All of it happens *before* the socket is accepted and before the
manager is asked for anything, so a caller that cannot use the terminal
never reaches a shell.

**The first frame** is the attach frame, and it is due within five
seconds: exactly ``{"type": "attach", "offset": <int>, "takeover":
<bool>}``. The ``offset`` is the absolute byte count the client has
already consumed — not the last byte's index — and it must be a plain
integer between zero and the session's current sequence. Anything else,
or no frame at all, is 4403.

One session has one controller, and the controller is a random
``connection_id`` this route mints per attachment: a second connection
needs ``takeover: true`` and is otherwise 4409, and a takeover displaces
the old attachment, which finds out with a 4409 of its own.

**The stream** is bytes, not text, and it is addressed by absolute byte
offset. The server sends ``ready`` (the snapshot, taken with the
controller assignment), then the replay frames, then ``replay_end``; from
there it sends ``output`` frames — ``{start, end, data}`` over the half
open range ``[start, end)``, with ``data`` the strict base64 of the raw
bytes. The client's cursor advances only over the bytes the server
actually sent, and a client that fell behind the replay ring is 4413
rather than a stream with a hole in it.

**The client** sends ``input`` (strict base64, at most 64 KiB decoded),
``resize`` (the manager's own bounds) and ``ping``. A frame the protocol
does not describe is answered with ``{"type": "error", "reason":
"bad_frame"}`` and a 1002 close: one bounded refusal, no state kept, no
per-frame allocation.

A disconnect **detaches** and never ends: the shell keeps running, and
the session stays recoverable for its detached lifetime. Only the
explicit end route kills a shell.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import secrets
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from ciao.terminal_replay import TERMINAL_REPLAY_CHUNK_BYTES
from ciao.terminal_sessions import (
    TERMINAL_MAX_PENDING_WRITE_BYTES,
    TerminalSessionError,
    TerminalSessionManager,
)
from ciao.web.auth import authorize_websocket
from ciao.web.routes_terminal import terminal_grant, terminal_policy

#: How long the first frame is due. The contract's window is five seconds;
#: a client that says nothing is closed with 4403.
ATTACH_TIMEOUT_S = 5.0

#: How often the grant is re-checked while the socket is open. The
#: contract's watchdog is a second: a revoked grant closes the socket
#: within one second of the revocation.
GRANT_WATCHDOG_INTERVAL_S = 1.0

#: The largest client frame the server reads, in bytes. A frame over it
#: is a bad frame: the envelope is bounded, so a hostile client cannot
#: make the server hold an unbounded message.
MAX_FRAME_BYTES = 1024 * 1024

#: How long the pump waits for output before it looks at the session
#: again. The manager's own wait is what bounds the loop.
_PUMP_TIMEOUT_S = 1.0

#: The close codes this route answers with. They are the ones the wire
#: contract fixes, and they are a closed vocabulary: a client maps a
#: number, never a sentence.
CLOSE_POLICY = 4403
CLOSE_UNAUTHORIZED = 4401
CLOSE_NOT_FOUND = 4404
CLOSE_TAKEN_OVER = 4409
CLOSE_SLOW_CONSUMER = 4413
CLOSE_BAD_FRAME = 1002
CLOSE_NORMAL = 1000
CLOSE_INTERNAL = 1011

#: How many random bytes a connection id carries. It names the controller
#: of one attachment: not a grant, not a cookie and not a browser.
_CONNECTION_ID_BYTES = 16

#: The manager's reasons on the stream, and the close code each one
#: earns. A reason that is not here is an internal failure: the socket is
#: closed with 1011 rather than left half open.
_STREAM_CLOSE: dict[str, int] = {
    "taken_over": CLOSE_TAKEN_OVER,
    "not_found": CLOSE_NOT_FOUND,
    "slow_consumer": CLOSE_SLOW_CONSUMER,
}

#: The reasons a client frame can earn. ``not_controller`` is a takeover
#: in everything but its name: this connection does not hold the session.
_CLIENT_CLOSE: dict[str, int] = {
    "taken_over": CLOSE_TAKEN_OVER,
    "not_controller": CLOSE_TAKEN_OVER,
    "not_found": CLOSE_NOT_FOUND,
}


# ── the small helpers ──────────────────────────────────────────────────────


def _manager(websocket: WebSocket) -> TerminalSessionManager:
    """The app's one terminal session manager.

    There is no fallback and no second registry: the app builds the
    manager, publishes it, and this route reads that one.
    """
    manager: TerminalSessionManager = websocket.app.state.terminal_sessions
    return manager


async def _send(websocket: WebSocket, payload: dict[str, Any]) -> bool:
    """Send one frame; ``False`` when the socket is already gone."""
    try:
        await websocket.send_json(payload)
    except (WebSocketDisconnect, RuntimeError):
        return False
    return True


async def _close(websocket: WebSocket, code: int) -> None:
    """Close the socket, whatever state it is in."""
    with contextlib.suppress(WebSocketDisconnect, RuntimeError):
        await websocket.close(code=code)


async def _bad_frame(websocket: WebSocket) -> int:
    """One bounded refusal for a frame the protocol does not describe."""
    await _send(websocket, {"type": "error", "reason": "bad_frame"})
    return CLOSE_BAD_FRAME


def _b64(data: bytes) -> str:
    """Strict base64 of raw bytes, as the wire carries it."""
    return base64.b64encode(data).decode("ascii")


# ── the first frame ────────────────────────────────────────────────────────


async def _first_frame(websocket: WebSocket) -> dict[str, Any] | None:
    """The attach frame, or ``None`` when there is not one.

    The frame is due within :data:`ATTACH_TIMEOUT_S` and it is exactly
    ``{"type": "attach", "offset": <int>, "takeover": <bool>}``: a missing
    key, an extra key, a type that is not a plain integer or a takeover
    that is not a plain boolean are all the same refusal. The ``offset``
    is checked against the session's own sequence by the caller, which
    knows it.
    """
    try:
        raw = await asyncio.wait_for(
            websocket.receive_text(), timeout=ATTACH_TIMEOUT_S
        )
    except (TimeoutError, WebSocketDisconnect, RuntimeError):
        return None
    if len(raw) > MAX_FRAME_BYTES:
        return None
    try:
        frame = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(frame, dict) or set(frame) != {"type", "offset", "takeover"}:
        return None
    if frame["type"] != "attach":
        return None
    offset = frame["offset"]
    # `type(x) is int`, not `isinstance`: a bool is an int in Python.
    if type(offset) is not int:  # noqa: E721 - exact int, not isinstance
        return None
    if type(frame["takeover"]) is not bool:  # noqa: E721
        return None
    if offset < 0:
        return None
    return {"offset": offset, "takeover": frame["takeover"]}


# ── the client frames ──────────────────────────────────────────────────────


def _client_payload(raw: str) -> dict[str, Any] | None:
    """The frame a client sent, or ``None`` when it is not one.

    The envelope is bounded: a frame over :data:`MAX_FRAME_BYTES` is
    refused without being parsed, so a hostile client cannot make the
    server hold — or read — an unbounded message.
    """
    if len(raw) > MAX_FRAME_BYTES:
        return None
    try:
        frame = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(frame, dict):
        return None
    return frame


async def _handle_client_frame(
    websocket: WebSocket,
    manager: TerminalSessionManager,
    session_id: str,
    connection_id: str,
    frame: dict[str, Any],
) -> int | None:
    """Handle one client frame.

    ``None`` means the socket carries on; a close code means it is over.
    A frame the protocol does not describe is a bad frame; a frame the
    protocol describes but the manager refuses is an error frame, and the
    socket stays: the client learns why and may try again.
    """
    kind = frame.get("type")
    if kind == "ping":
        if set(frame) != {"type"}:
            return await _bad_frame(websocket)
        if not await _send(websocket, {"type": "pong"}):
            return CLOSE_NORMAL
        return None

    if kind == "input":
        if set(frame) != {"type", "data"}:
            return await _bad_frame(websocket)
        data = frame["data"]
        if not isinstance(data, str):
            return await _bad_frame(websocket)
        # The encoded length is checked before the payload is decoded, so
        # a frame that cannot be admitted is never decoded at all.
        if len(data) > 4 * ((TERMINAL_MAX_PENDING_WRITE_BYTES + 2) // 3):
            return await _bad_frame(websocket)
        try:
            payload = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            return await _bad_frame(websocket)
        if len(payload) > TERMINAL_MAX_PENDING_WRITE_BYTES:
            return await _bad_frame(websocket)
        try:
            await manager.write_input(session_id, connection_id, payload)
        except TerminalSessionError as exc:
            return await _client_error(websocket, exc)
        return None

    if kind == "resize":
        if set(frame) != {"type", "cols", "rows"}:
            return await _bad_frame(websocket)
        try:
            await manager.resize(
                session_id, connection_id, frame["cols"], frame["rows"]
            )
        except TerminalSessionError as exc:
            return await _client_error(websocket, exc)
        return None

    return await _bad_frame(websocket)


async def _client_error(websocket: WebSocket, exc: TerminalSessionError) -> int | None:
    """A refusal the manager made, in the client's own words.

    A refusal that is about *control* ends the socket: this connection
    does not hold the session. A refusal that is about the frame — a size
    out of range, a full input backlog, a terminal that would not take
    the bytes, a shell that has already ended — is an error frame, and
    the socket carries on. The shell's own end is announced by the pump,
    with the exit code.
    """
    reason = exc.reason
    close = _CLIENT_CLOSE.get(reason)
    if close is not None:
        return close
    if not await _send(websocket, {"type": "error", "reason": reason}):
        return CLOSE_NORMAL
    return None


# ── the route ──────────────────────────────────────────────────────────────


async def terminal_stream(websocket: WebSocket) -> None:
    """Carry one terminal session to the browser.

    The door, the attach, the stream and the end. Nothing else: no shell
    is started here, and a disconnect never ends the session.
    """
    session_id = str(websocket.path_params.get("session_id", "") or "")

    # ── the door ──
    if terminal_policy(websocket) is not None:
        await _close(websocket, CLOSE_POLICY)
        return
    if not await authorize_websocket(websocket):
        return
    if terminal_grant(websocket) is None:
        await _close(websocket, CLOSE_UNAUTHORIZED)
        return

    manager = _manager(websocket)
    try:
        manager.get(session_id)
    except TerminalSessionError:
        await _close(websocket, CLOSE_NOT_FOUND)
        return

    await websocket.accept()

    connection_id = secrets.token_urlsafe(_CONNECTION_ID_BYTES)

    # ── the attach ──
    frame = await _first_frame(websocket)
    if frame is None:
        await _close(websocket, CLOSE_POLICY)
        return
    try:
        info = manager.get(session_id)
    except TerminalSessionError:
        await _close(websocket, CLOSE_NOT_FOUND)
        return
    if frame["offset"] > info.seq:
        await _close(websocket, CLOSE_POLICY)
        return
    try:
        attachment = manager.attach(
            session_id,
            connection_id,
            frame["offset"],
            takeover=frame["takeover"],
        )
    except TerminalSessionError as exc:
        if exc.reason == "taken_over":
            await _close(websocket, CLOSE_TAKEN_OVER)
            return
        if exc.reason == "not_found":
            await _close(websocket, CLOSE_NOT_FOUND)
            return
        await _close(websocket, CLOSE_POLICY)
        return

    # ── the snapshot ──
    snapshot = attachment.snapshot
    ready = {
        "type": "ready",
        "session_id": attachment.info.session_id,
        "folder": str(attachment.info.folder),
        "cols": attachment.info.cols,
        "rows": attachment.info.rows,
        "seq": attachment.info.seq,
        "replay_start": attachment.info.replay_start,
        "truncated": snapshot.truncated,
    }
    if not await _send(websocket, ready):
        await _close(websocket, CLOSE_NORMAL)
        return
    position = 0
    while position < len(snapshot.data):
        chunk = snapshot.data[position : position + TERMINAL_REPLAY_CHUNK_BYTES]
        end = snapshot.start + position + len(chunk)
        if not await _send(
            websocket,
            {
                "type": "output",
                "start": snapshot.start + position,
                "end": end,
                "data": _b64(chunk),
            },
        ):
            await _close(websocket, CLOSE_NORMAL)
            return
        position += len(chunk)
    if not await _send(
        websocket,
        {"type": "replay_end", "seq": snapshot.end, "truncated": snapshot.truncated},
    ):
        await _close(websocket, CLOSE_NORMAL)
        return

    # ── the two loops ──
    async def _pump() -> None:
        """Carry the session's output to the client, byte by byte."""
        cursor = attachment.next_offset
        while True:
            try:
                current = manager.get(session_id)
            except TerminalSessionError:
                await _close(websocket, CLOSE_NOT_FOUND)
                return
            if current.exited and cursor >= current.seq:
                # The shell is gone: say so, with its own exit code.
                await _send(websocket, {"type": "exit", "code": current.exit_code})
                await _close(websocket, CLOSE_NORMAL)
                return
            try:
                arrived = await manager.wait_for_output(
                    session_id, connection_id, cursor, timeout=_PUMP_TIMEOUT_S
                )
            except TerminalSessionError as exc:
                await _close(
                    websocket, _STREAM_CLOSE.get(exc.reason, CLOSE_INTERNAL)
                )
                return
            if not arrived:
                continue
            try:
                chunk = manager.read(session_id, connection_id, cursor)
            except TerminalSessionError as exc:
                await _close(
                    websocket, _STREAM_CLOSE.get(exc.reason, CLOSE_INTERNAL)
                )
                return
            if chunk.end <= chunk.start:
                continue
            if not await _send(
                websocket,
                {
                    "type": "output",
                    "start": chunk.start,
                    "end": chunk.end,
                    "data": _b64(chunk.data),
                },
            ):
                await _close(websocket, CLOSE_NORMAL)
                return
            # The cursor advances only over the bytes that were sent.
            cursor = chunk.end

    async def _watch_grant() -> None:
        """Close the socket when the grant is gone."""
        while True:
            await asyncio.sleep(GRANT_WATCHDOG_INTERVAL_S)
            if terminal_grant(websocket) is None:
                await _close(websocket, CLOSE_UNAUTHORIZED)
                return

    pump = asyncio.create_task(_pump(), name="terminal-stream-pump")
    watchdog = asyncio.create_task(_watch_grant(), name="terminal-stream-watchdog")
    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except (WebSocketDisconnect, RuntimeError):
                return
            # The grant is re-checked for every message, so a revoked one
            # ends the socket even between the watchdog's ticks.
            if terminal_grant(websocket) is None:
                await _close(websocket, CLOSE_UNAUTHORIZED)
                return
            if len(raw) > MAX_FRAME_BYTES:
                close = await _bad_frame(websocket)
                await _close(websocket, close)
                return
            payload = _client_payload(raw)
            if payload is None:
                close = await _bad_frame(websocket)
                await _close(websocket, close)
                return
            outcome = await _handle_client_frame(
                websocket, manager, session_id, connection_id, payload
            )
            if outcome is not None:
                await _close(websocket, outcome)
                return
    finally:
        # The socket detaches; it never ends the session. The two loops are
        # cancelled and joined, so neither outlives the socket.
        pump.cancel()
        watchdog.cancel()
        with contextlib.suppress(TerminalSessionError):
            manager.detach(session_id, connection_id)
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(pump, watchdog, return_exceptions=True)
