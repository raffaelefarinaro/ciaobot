"""Tests for multi-device node state management and handover routes."""

import json
from pathlib import Path
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.node_state import NodeStateManager
from ciao.schedules import ScheduleEntry, ScheduleManager, ScheduleStore
from ciao.web.routes_node import (
    node_connect_endpoint,
    node_demote_endpoint,
    node_handover_endpoint,
    node_peers_endpoint,
    node_status_endpoint,
)


def test_node_state_manager_defaults(tmp_path: Path):
    mgr = NodeStateManager(tmp_path)
    assert mgr.is_active() is True
    assert mgr.get_role() == "host"

    status = mgr.get_status()
    assert status["node_id"]
    assert status["role"] == "host"
    assert status["mode"] == "host"
    assert status["active_since"] is not None
    assert isinstance(status["peers"], list)


def test_node_state_role_transitions(tmp_path: Path):
    mgr = NodeStateManager(tmp_path)

    # Demote → client
    mgr.demote()
    assert mgr.is_active() is False
    assert mgr.get_role() == "client"
    assert mgr.get_status()["active_since"] is None

    # Promote → host
    mgr.promote()
    assert mgr.is_active() is True
    assert mgr.get_role() == "host"
    assert mgr.get_status()["active_since"] is not None


def test_node_state_connect_as_client(tmp_path: Path):
    mgr = NodeStateManager(tmp_path)
    status = mgr.connect_as_client("100.101.252.27", host_session="sess-abc")
    assert status["role"] == "client"
    assert status["host_url"] == "http://100.101.252.27:8443"
    assert status["has_host_session"] is True
    assert mgr.get_active_peer_url() == "http://100.101.252.27:8443"
    assert mgr.get_host_session() == "sess-abc"

    mgr.promote()
    assert mgr.get_role() == "host"
    assert mgr.get_host_session() is None
    assert mgr.get_active_peer_url() is None


def test_node_state_migrates_legacy_roles(tmp_path: Path):
    state = tmp_path / "node_state.json"
    state.write_text(
        json.dumps(
            {
                "node_id": "legacy",
                "role": "standby",
                "peers": [{"node_id": "home", "url": "http://10.0.0.1:8443", "is_active": True}],
            }
        ),
        encoding="utf-8",
    )
    mgr = NodeStateManager(tmp_path)
    assert mgr.get_role() == "client"
    assert mgr.get_host_url() == "http://10.0.0.1:8443"


def test_node_state_peer_management(tmp_path: Path):
    mgr = NodeStateManager(tmp_path)

    mgr.add_peer("http://192.168.1.50:8543", peer_id="home-server")
    status = mgr.get_status()
    assert len(status["peers"]) == 1
    assert status["peers"][0]["node_id"] == "home-server"
    assert status["peers"][0]["url"] == "http://192.168.1.50:8543"

    mgr.remove_peer("http://192.168.1.50:8543")
    status_after = mgr.get_status()
    assert len(status_after["peers"]) == 0


@pytest.mark.asyncio
async def test_schedule_manager_pauses_in_standby(tmp_path: Path):
    node_mgr = NodeStateManager(tmp_path)
    store = ScheduleStore(tmp_path)
    
    # Create entry due right now
    entry = store.create(
        daily_time_utc="12:00",
        prompt="test prompt",
        model="opus",
        mode="auto",
        chat_id=123,
    )

    dispatched = []
    async def mock_dispatch(*args, **kwargs):
        dispatched.append(args)

    sched_mgr = ScheduleManager(
        store=store,
        dispatch_to_web=mock_dispatch,
        is_node_active=node_mgr.is_active,
    )

    # Set standby
    node_mgr.demote()

    # Tick should do nothing while in standby
    await sched_mgr.tick()
    assert len(dispatched) == 0

    # Promote to active
    node_mgr.promote()
    # (tick with matching time will dispatch when active)


@pytest.mark.asyncio
async def test_interval_schedules_pause_in_standby(tmp_path: Path):
    """Interval entries are due on every tick once their gap elapses, so a
    standby node that kept ticking them would double-run the host's cadence."""
    node_mgr = NodeStateManager(tmp_path)
    store = ScheduleStore(tmp_path)

    store.create(
        daily_time_utc="",
        prompt="interval prompt",
        model="",
        mode="auto",
        chat_id=0,
        frequency="interval",
        interval_minutes=1,
        web_chat_id="chat-1",
    )

    dispatches = []
    async def mock_dispatch(entry, model, mode, provider, *, target_chat_id=None):
        dispatches.append(entry)

    sched_mgr = ScheduleManager(
        store=store,
        dispatch_to_web=mock_dispatch,
        prepare_chat=lambda entry, *args: entry.web_chat_id,
        is_node_active=node_mgr.is_active,
    )

    node_mgr.demote()
    await sched_mgr.tick()
    assert len(dispatches) == 0


def test_node_api_routes(tmp_path: Path):
    app = Starlette(
        routes=[
            Route("/api/node/status", node_status_endpoint, methods=["GET"]),
            Route("/api/node/connect", node_connect_endpoint, methods=["POST"]),
            Route("/api/node/handover", node_handover_endpoint, methods=["POST"]),
            Route("/api/node/demote", node_demote_endpoint, methods=["POST"]),
            Route("/api/node/peers", node_peers_endpoint, methods=["POST"]),
        ]
    )

    node_mgr = NodeStateManager(tmp_path)
    app.state.node_state_manager = node_mgr

    client = TestClient(app)

    # 1. Status endpoint
    res = client.get("/api/node/status")
    assert res.status_code == 200
    data = res.json()
    assert data["role"] == "host"

    # 2. Add peer endpoint
    res_peer = client.post("/api/node/peers", json={"action": "add", "url": "http://10.0.0.5:8543", "node_id": "server-2"})
    assert res_peer.status_code == 200
    assert len(res_peer.json()["status"]["peers"]) == 1

    # 3. Demote endpoint
    res_demote = client.post("/api/node/demote")
    assert res_demote.status_code == 200
    assert res_demote.json()["status"]["role"] == "client"

    # 4. Handover (Force) endpoint
    res_handover = client.post("/api/node/handover", json={"force": True})
    assert res_handover.status_code == 200
    assert res_handover.json()["status"]["role"] == "host"


def test_node_connect_requires_password(tmp_path: Path):
    app = Starlette(
        routes=[Route("/api/node/connect", node_connect_endpoint, methods=["POST"])]
    )
    app.state.node_state_manager = NodeStateManager(tmp_path)
    client = TestClient(app)

    res = client.post("/api/node/connect", json={"host_url": "http://10.0.0.1:8443"})
    assert res.status_code == 400
    assert res.json()["auth_required"] is True


def test_node_connect_rejects_host_without_password(tmp_path: Path, monkeypatch):
    import httpx

    app = Starlette(
        routes=[Route("/api/node/connect", node_connect_endpoint, methods=["POST"])]
    )
    app.state.node_state_manager = NodeStateManager(tmp_path)
    client = TestClient(app)

    class _Resp:
        def __init__(self, status_code: int, payload: dict):
            self.status_code = status_code
            self._payload = payload
            self.headers = {"content-type": "application/json"}

        def json(self):
            return self._payload

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            assert url.endswith("/api/startup-status")
            return _Resp(200, {"overall_ready": True, "auth_required": False})

        async def post(self, url, json=None):
            raise AssertionError("login should not be attempted")

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    res = client.post(
        "/api/node/connect",
        json={"host_url": "http://10.0.0.1:8443", "password": "secret"},
    )
    assert res.status_code == 400
    body = res.json()
    assert body["password_required_on_host"] is True
    assert "no password" in body["error"].lower()


def test_node_connect_logs_in_via_api_auth(tmp_path: Path, monkeypatch):
    """Host login path is /api/auth (not the non-existent /api/auth/login)."""
    import httpx

    from ciao.web.auth import SESSION_COOKIE

    app = Starlette(
        routes=[Route("/api/node/connect", node_connect_endpoint, methods=["POST"])]
    )
    app.state.node_state_manager = NodeStateManager(tmp_path)
    client = TestClient(app)
    posted: list[str] = []

    class _Headers(dict):
        def get_list(self, key: str) -> list[str]:
            raw = self.get(key)
            return [raw] if raw else []

    class _Resp:
        def __init__(self, status_code: int, payload: dict | None = None, *, set_cookie: str | None = None):
            self.status_code = status_code
            self._payload = payload or {}
            self.headers = _Headers({"content-type": "application/json"})
            if set_cookie:
                self.headers["set-cookie"] = set_cookie
            self.cookies = {SESSION_COOKIE: "host-session-token"} if set_cookie else {}
            self.text = ""

        def json(self):
            return self._payload

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            assert url.endswith("/api/startup-status")
            return _Resp(200, {"overall_ready": True, "auth_required": True})

        async def post(self, url, json=None):
            posted.append(url)
            assert url.endswith("/api/auth")
            assert not url.endswith("/api/auth/login")
            assert json == {"token": "correct-password"}
            return _Resp(
                200,
                {"ok": True},
                set_cookie=f"{SESSION_COOKIE}=host-session-token; Path=/; HttpOnly",
            )

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    res = client.post(
        "/api/node/connect",
        json={"host_url": "http://10.0.0.1:8443", "password": "correct-password"},
    )
    assert res.status_code == 200
    assert posted == ["http://10.0.0.1:8443/api/auth"]
    status = res.json()["status"]
    assert status["role"] == "client"
    assert status["has_host_session"] is True
