from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web.connection_tracker import ConnectionTracker, _is_loopback_host
from ciao.web.routes_api import node_connected_clients_endpoint


def _make_ws(*, host: str = "192.168.0.10", port: int = 54321, user_agent: str = "", forwarded: str = "") -> SimpleNamespace:
    headers = {"user-agent": user_agent}
    if forwarded:
        headers["x-forwarded-for"] = forwarded
    return SimpleNamespace(
        client=SimpleNamespace(host=host, port=port),
        headers=headers,
    )


def test_is_loopback_host() -> None:
    assert _is_loopback_host("127.0.0.1") is True
    assert _is_loopback_host("localhost") is True
    assert _is_loopback_host("192.168.0.10") is False
    assert _is_loopback_host("") is False


def test_tracker_registers_and_unregisters() -> None:
    tracker = ConnectionTracker()
    ws = _make_ws(host="10.0.0.5")
    conn_id = tracker.register(ws, "events")
    assert conn_id.startswith("conn-")
    assert len(tracker.list_clients()) == 1
    tracker.unregister(conn_id)
    assert tracker.list_clients() == []


def test_tracker_filters_local_and_remote() -> None:
    tracker = ConnectionTracker()
    remote = tracker.register(_make_ws(host="10.0.0.5"), "events")
    local = tracker.register(_make_ws(host="127.0.0.1"), "events")

    all_clients = tracker.list_clients()
    remote_clients = tracker.list_clients(remote_only=True)

    assert {c["client_host"] for c in all_clients} == {"10.0.0.5", "127.0.0.1"}
    assert {c["client_host"] for c in remote_clients} == {"10.0.0.5"}

    # Cleanup is symmetric regardless of filter.
    tracker.unregister(remote)
    tracker.unregister(local)
    assert tracker.list_clients() == []


def test_tracker_prefers_x_forwarded_for() -> None:
    tracker = ConnectionTracker()
    ws = _make_ws(host="10.0.0.1", forwarded="203.0.113.4, 198.51.100.2")
    conn_id = tracker.register(ws, "chat", chat_id="chat-1")
    record = tracker.list_clients()[0]
    assert record["client_host"] == "203.0.113.4"
    assert record["is_local"] is False
    assert record["kind"] == "chat"
    assert record["chat_id"] == "chat-1"
    tracker.unregister(conn_id)


def test_connected_clients_endpoint_returns_remote_only() -> None:
    app = Starlette(routes=[Route("/api/node/connected-clients", node_connected_clients_endpoint)])
    tracker = ConnectionTracker()
    app.state.connection_tracker = tracker

    tracker.register(_make_ws(host="10.0.0.5"), "events")
    tracker.register(_make_ws(host="127.0.0.1"), "chat", chat_id="chat-1")

    client = TestClient(app)
    res = client.get("/api/node/connected-clients")
    assert res.status_code == 200
    payload = res.json()
    assert payload["ok"] is True
    assert len(payload["clients"]) == 1
    assert payload["clients"][0]["client_host"] == "10.0.0.5"


def test_connected_clients_endpoint_no_tracker() -> None:
    app = Starlette(routes=[Route("/api/node/connected-clients", node_connected_clients_endpoint)])
    client = TestClient(app)
    res = client.get("/api/node/connected-clients")
    assert res.status_code == 200
    assert res.json() == {"ok": True, "clients": []}
