from __future__ import annotations

from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web.routes_api import open_chat_endpoint


def _client(pcm=None) -> TestClient:
    app = Starlette(routes=[Route("/api/open-chat/{chat_id}", open_chat_endpoint, methods=["GET"])])
    app.state.project_chat_manager = pcm
    return TestClient(app)


def test_open_chat_endpoint_without_manager_returns_not_found() -> None:
    resp = _client(pcm=None).get("/api/open-chat/c1")
    assert resp.status_code == 404
    assert resp.json() == {"ok": False, "error": "chat not found"}


def test_open_chat_endpoint_publishes_event_for_known_chat() -> None:
    published: list[dict] = []
    pcm = SimpleNamespace(
        get_chat=lambda chat_id: {"chat_id": chat_id} if chat_id == "c1" else None,
        events=SimpleNamespace(publish=published.append),
    )

    resp = _client(pcm=pcm).get("/api/open-chat/c1")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "chat_id": "c1"}
    assert published == [{"type": "open_chat", "chat_id": "c1"}]
