"""GET /api/admin/restart-blockers."""

from __future__ import annotations

from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web import auth
from ciao.web.routes_api import admin_restart_blockers

_PATH = "/api/admin/restart-blockers"


def _client(pcm=None) -> TestClient:
    app = Starlette(routes=[Route(_PATH, admin_restart_blockers, methods=["GET"])])
    app.state.project_chat_manager = pcm
    return TestClient(app)


def test_returns_empty_without_manager() -> None:
    resp = _client().get(_PATH)
    assert resp.status_code == 200
    assert resp.json() == {"restart_blockers": []}


def test_returns_manager_blockers() -> None:
    blockers = [{"chat_id": "c1", "reasons": ["open_question"]}]
    pcm = SimpleNamespace(restart_blockers=lambda: blockers)
    assert _client(pcm).get(_PATH).json() == {"restart_blockers": blockers}


def test_route_is_loopback_only_and_not_public() -> None:
    assert _PATH in auth._LOOPBACK_ONLY_API
    assert _PATH not in auth._PUBLIC_API
