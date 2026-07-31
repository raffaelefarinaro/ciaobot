"""Track live WebSocket client connections for the host/client page.

The engine is intentionally stateless about browser sessions, but for the
Settings page we want to answer: "is another device currently connected to
this host?". We record every accepted `/ws/chat/{id}` and `/ws/events` socket,
together with the peer address and a connection kind. Only the local node
serves this data; the proxy middleware forwards remote sockets to the host, so
the host sees the real client address.
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
    a trust decision from it: see ``_connection_record``, which classifies
    ``is_local`` from the TCP peer instead.
    """
    forwarded = websocket.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return _peer_host(websocket)


def _is_loopback_host(host: str) -> bool:
    """True when ``host`` is a local loopback address or hostname."""
    if not host:
        return False
    lowered = host.lower().strip()
    if lowered in {"localhost", "127.0.0.1", "::1", "::ffff:127.0.0.1"}:
        return True
    if lowered.startswith("127."):
        return True
    # IPv6 ::1 compressed forms.
    if lowered in {"0:0:0:0:0:0:0:1", "::1"}:
        return True
    return False


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
        # From the TCP peer, never the displayed host: `is_local` hides a
        # connection from the "connected clients" panel, and a remote caller
        # sending `X-Forwarded-For: 127.0.0.1` must not be able to hide itself.
        "is_local": _is_loopback_host(_peer_host(websocket)),
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

    def list_clients(self, *, remote_only: bool = False) -> list[dict[str, Any]]:
        """Return connection records, optionally filtering out local sockets."""
        records = list(self._connections.values())
        if remote_only:
            records = [r for r in records if not r.get("is_local")]
        # Stable sort: oldest first.
        records.sort(key=lambda r: r.get("connected_at", ""))
        return records

    def chat_client_count(self, chat_id: str) -> int:
        """Count open `/ws/chat/{chat_id}` sockets for one chat.

        Includes loopback sockets (unlike `list_clients`'s default UI use),
        since a client on the same machine as the server is still a client.
        """
        if not chat_id:
            return 0
        return sum(
            1
            for record in self._connections.values()
            if record.get("kind") == "chat" and record.get("chat_id") == chat_id
        )

    def snapshot(self) -> dict[str, Any]:
        """Full summary for diagnostics."""
        return {
            "total": len(self._connections),
            "remote": len([r for r in self._connections.values() if not r.get("is_local")]),
            "clients": self.list_clients(),
        }
