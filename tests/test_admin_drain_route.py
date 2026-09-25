from __future__ import annotations

from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web import auth
from ciao.web.routes_api import admin_drain, admin_drain_cancel


def _manager() -> SimpleNamespace:
    calls: list[str] = []
    manager = SimpleNamespace(
        calls=calls,
        active_chat_ids=lambda: ["c-1", "c-2"],
        begin_restart_drain=lambda: calls.append("begin"),
        cancel_restart_drain=lambda: calls.append("cancel"),
    )
    return manager


def _client(pcm: object) -> TestClient:
    app = Starlette(
        routes=[
            Route("/api/admin/drain", admin_drain, methods=["POST"]),
            Route("/api/admin/drain/cancel", admin_drain_cancel, methods=["POST"]),
        ]
    )
    app.state.project_chat_manager = pcm
    return TestClient(app)


def test_drain_closes_admission_and_reports_active_chats() -> None:
    pcm = _manager()

    resp = _client(pcm).post("/api/admin/drain")

    assert resp.status_code == 200
    assert resp.json() == {"draining": True, "active_chat_ids": ["c-1", "c-2"]}
    # The caller polls /api/active-chats afterwards, so the drain has to have
    # actually happened rather than merely been acknowledged.
    assert pcm.calls == ["begin"]


def test_drain_cancel_reopens_admission() -> None:
    pcm = _manager()

    resp = _client(pcm).post("/api/admin/drain/cancel")

    assert resp.status_code == 200
    assert resp.json() == {"draining": False}
    assert pcm.calls == ["cancel"]


def test_drain_without_manager_is_a_clear_failure() -> None:
    resp = _client(pcm=None).post("/api/admin/drain")

    # 503 rather than a silent success: the updater's foreground half must not
    # think admission is closed when there is no chat manager to close it on.
    assert resp.status_code == 503


# The updater runs `ciao update apply` from a terminal, before any PWA session
# exists, and it has to be refused from anywhere but this machine. Both facts
# live in `_LOOPBACK_ONLY_API`: in that set, a request is let through only when
# the peer is loopback, and the session check below is never reached.
def test_drain_routes_are_loopback_only_and_session_free() -> None:
    for path in ("/api/admin/drain", "/api/admin/drain/cancel"):
        assert path in auth._LOOPBACK_ONLY_API
        # Public would mean "from anywhere, no session" — the opposite promise.
        assert path not in auth._PUBLIC_API
