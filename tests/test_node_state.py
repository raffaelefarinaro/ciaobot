"""Tests for multi-device node state management and handover routes."""

import json
from pathlib import Path
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.node_state import NodeStateManager
from ciao.schedules import ScheduleEntry, ScheduleManager, ScheduleStore
from ciao.loops import LoopEntry, LoopManager, LoopStore
from ciao.web.routes_api import (
    node_demote_endpoint,
    node_handover_endpoint,
    node_peers_endpoint,
    node_status_endpoint,
)


def test_node_state_manager_defaults(tmp_path: Path):
    mgr = NodeStateManager(tmp_path)
    assert mgr.is_active() is True
    assert mgr.get_role() == "active"

    status = mgr.get_status()
    assert status["node_id"]
    assert status["role"] == "active"
    assert status["active_since"] is not None
    assert isinstance(status["peers"], list)


def test_node_state_role_transitions(tmp_path: Path):
    mgr = NodeStateManager(tmp_path)

    # Demote
    mgr.demote()
    assert mgr.is_active() is False
    assert mgr.get_role() == "standby"
    assert mgr.get_status()["active_since"] is None

    # Promote
    mgr.promote()
    assert mgr.is_active() is True
    assert mgr.get_role() == "active"
    assert mgr.get_status()["active_since"] is not None


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
async def test_loop_manager_pauses_in_standby(tmp_path: Path):
    node_mgr = NodeStateManager(tmp_path)
    store = LoopStore(tmp_path)

    loop_entry = store.create(
        prompt="loop prompt",
        web_chat_id="chat-1",
        interval_minutes=1,
        autostart=True,
    )

    dispatches = []
    async def mock_dispatch(entry):
        dispatches.append(entry)

    loop_mgr = LoopManager(
        store=store,
        dispatch=mock_dispatch,
        is_node_active=node_mgr.is_active,
    )
    loop_mgr.start_loop(loop_entry.loop_id)

    # Set standby
    node_mgr.demote()
    await loop_mgr.tick()
    assert len(dispatches) == 0


def test_node_api_routes(tmp_path: Path):
    app = Starlette(
        routes=[
            Route("/api/node/status", node_status_endpoint, methods=["GET"]),
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
    assert data["role"] == "active"

    # 2. Add peer endpoint
    res_peer = client.post("/api/node/peers", json={"action": "add", "url": "http://10.0.0.5:8543", "node_id": "server-2"})
    assert res_peer.status_code == 200
    assert len(res_peer.json()["status"]["peers"]) == 1

    # 3. Demote endpoint
    res_demote = client.post("/api/node/demote")
    assert res_demote.status_code == 200
    assert res_demote.json()["status"]["role"] == "standby"

    # 4. Handover (Force) endpoint
    res_handover = client.post("/api/node/handover", json={"force": True})
    assert res_handover.status_code == 200
    assert res_handover.json()["status"]["role"] == "active"
