from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ciao.web.auth import SESSION_COOKIE
from ciao.web.routes_chat import ws_events


async def _never_yield():
    await asyncio.Event().wait()
    yield  # pragma: no cover


def _events_app(*, auth_required: bool) -> Starlette:
    app = Starlette(routes=[WebSocketRoute("/ws/events", ws_events)])
    app.state.serializer = URLSafeTimedSerializer("test-secret")
    app.state.config = SimpleNamespace(pwa_auth_required=auth_required)
    app.state.project_chat_manager = SimpleNamespace(
        active_stream_chat_ids=lambda: [],
        get_chat=lambda _cid: None,
        background_agent_counts={},
        events=SimpleNamespace(subscribe=_never_yield),
    )
    return app


def test_ws_connects_without_session_when_auth_off() -> None:
    client = TestClient(_events_app(auth_required=False))
    with client.websocket_connect(
        "/ws/events", headers={"Origin": "http://testserver"}
    ) as ws:
        assert ws.receive_json()["type"] == "snapshot"


def test_ws_requires_session_when_auth_on() -> None:
    client = TestClient(_events_app(auth_required=True))
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/events"):
            pass


def test_ws_accepts_session_cookie_when_auth_on() -> None:
    app = _events_app(auth_required=True)
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, app.state.serializer.dumps({"user": "owner"}))
    with client.websocket_connect("/ws/events") as ws:
        assert ws.receive_json()["type"] == "snapshot"


def test_ws_rejects_cross_origin() -> None:
    client = TestClient(_events_app(auth_required=False))
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/ws/events", headers={"Origin": "http://evil.example"}
        ):
            pass
