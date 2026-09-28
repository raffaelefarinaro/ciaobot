"""Track live WebSocket client connections for the host.

The engine is intentionally stateless about browser sessions, but a few
callbacks need to know who is attached: we record every accepted
`/ws/chat/{id}` and `/ws/events` socket, together with the peer address and a
connection kind, and `ConnectionTracker.chat_client_count` answers how many
browsers currently have a given chat open.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from starlette.websockets import WebSocket


def _peer_host(websocket: WebSocket) -> str:
    """The TCP peer address, which no caller can forge."""
    client = websocket.client
    if client is not None:
        host = getattr(client, "host", None)
        if isinstance(host, str) and host:
            return host
    return "unknown"


def _client_host(websocket: WebSocket) -> str:
    """Best-guess client IP for display, preferring the last proxy hop.

    `X-Forwarded-For` is caller-supplied, so this is a label only. Never derive
    a trust decision from it.
    """
    forwarded = websocket.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return _peer_host(websocket)


def _connection_record(
    websocket: WebSocket, kind: str, **extra: Any
) -> dict[str, Any]:
    """Build a serialisable client connection record."""
    client = websocket.client
    host = _client_host(websocket)
    port = int(client.port) if client is not None and client.port is not None else 0
    return {
        "id": f"conn-{uuid.uuid4().hex[:12]}",
        "kind": kind,
        "client_host": host,
        "client_port": port,
        "user_agent": websocket.headers.get("user-agent", ""),
        "connected_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        **extra,
    }


class ConnectionTracker:
    """In-memory registry of accepted WebSocket connections."""

    def __init__(self) -> None:
        self._connections: dict[str, dict[str, Any]] = {}

    def register(self, websocket: WebSocket, kind: str, **extra: Any) -> str:
        """Record a new connection and return a handle for unregister."""
        record = _connection_record(websocket, kind, **extra)
        connection_id = str(record["id"])
        self._connections[connection_id] = record
        return connection_id

    def unregister(self, connection_id: str) -> None:
        self._connections.pop(connection_id, None)

    def chat_client_count(self, chat_id: str) -> int:
        """Count open `/ws/chat/{chat_id}` sockets for one chat.

        Loopback sockets count: a client on the same machine as the engine is
        still a client.
        """
        if not chat_id:
            return 0
        return sum(
            1
            for record in self._connections.values()
            if record.get("kind") == "chat" and record.get("chat_id") == chat_id
        )
