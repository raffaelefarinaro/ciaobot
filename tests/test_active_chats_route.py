from __future__ import annotations

from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web.routes_api import active_chats_endpoint


def _client(pcm=None) -> TestClient:
    app = Starlette(routes=[Route("/api/active-chats", active_chats_endpoint, methods=["GET"])])
    app.state.project_chat_manager = pcm
    return TestClient(app)


def test_active_chats_endpoint_without_manager_returns_empty() -> None:
    resp = _client(pcm=None).get("/api/active-chats")
    assert resp.status_code == 200
    assert resp.json() == {"active_chat_ids": []}


def test_active_chats_endpoint_unions_streams_and_background_agents() -> None:
    pcm = SimpleNamespace(
        active_chat_ids=lambda: ["c-agents", "c-both", "c-stream"],
    )

    resp = _client(pcm=pcm).get("/api/active-chats")

    assert resp.status_code == 200
    assert resp.json() == {"active_chat_ids": ["c-agents", "c-both", "c-stream"]}
