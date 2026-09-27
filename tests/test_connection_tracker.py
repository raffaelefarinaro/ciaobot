from __future__ import annotations

from types import SimpleNamespace

from ciao.web.connection_tracker import ConnectionTracker


def _make_ws(*, host: str = "192.168.0.10", port: int = 54321, user_agent: str = "", forwarded: str = "") -> SimpleNamespace:
    headers = {"user-agent": user_agent}
    if forwarded:
        headers["x-forwarded-for"] = forwarded
    return SimpleNamespace(
        client=SimpleNamespace(host=host, port=port),
        headers=headers,
    )


def test_tracker_registers_and_unregisters() -> None:
    tracker = ConnectionTracker()
    conn_id = tracker.register(_make_ws(host="10.0.0.5"), "chat", chat_id="chat-1")
    assert conn_id.startswith("conn-")
    assert tracker.chat_client_count("chat-1") == 1
    tracker.unregister(conn_id)
    assert tracker.chat_client_count("chat-1") == 0


def test_record_labels_the_last_proxy_hop() -> None:
    """The record's `client_host` prefers the last proxy hop, and the TCP peer's
    port is kept. `X-Forwarded-For` is caller-written, so it only ever titles a
    row — no trust decision is derived from it."""
    tracker = ConnectionTracker()
    conn_id = tracker.register(
        _make_ws(host="10.0.0.1", port=51000, forwarded="203.0.113.4, 198.51.100.2"),
        "chat",
        chat_id="chat-1",
    )
    record = tracker._connections[conn_id]
    assert record["client_host"] == "203.0.113.4"
    assert record["client_port"] == 51000
    assert record["kind"] == "chat"
    assert record["chat_id"] == "chat-1"

    tracker.unregister(conn_id)


def test_record_falls_back_to_the_tcp_peer() -> None:
    """Without a forwarding header the record is named by the peer address,
    which is the one value a caller cannot forge."""
    tracker = ConnectionTracker()
    conn_id = tracker.register(_make_ws(host="203.0.113.9"), "events")
    assert tracker._connections[conn_id]["client_host"] == "203.0.113.9"
    tracker.unregister(conn_id)


def test_chat_client_count_filters_by_chat_and_kind() -> None:
    """`file_surface` relies on this being scoped to exactly one chat_id and
    to `kind == "chat"` sockets, so a different chat's clients, or the
    global `/ws/events` socket, must never inflate the count."""
    tracker = ConnectionTracker()
    a = tracker.register(_make_ws(host="10.0.0.1"), "chat", chat_id="chat-1")
    b = tracker.register(_make_ws(host="10.0.0.2"), "chat", chat_id="chat-1")
    c = tracker.register(_make_ws(host="10.0.0.3"), "chat", chat_id="chat-2")
    events = tracker.register(_make_ws(host="10.0.0.4"), "events")

    assert tracker.chat_client_count("chat-1") == 2
    assert tracker.chat_client_count("chat-2") == 1
    assert tracker.chat_client_count("chat-3") == 0
    assert tracker.chat_client_count("") == 0

    tracker.unregister(a)
    assert tracker.chat_client_count("chat-1") == 1

    tracker.unregister(b)
    tracker.unregister(c)
    tracker.unregister(events)
    assert tracker.chat_client_count("chat-1") == 0
    assert tracker.chat_client_count("chat-2") == 0
