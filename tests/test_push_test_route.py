"""Tests for POST /api/push/test (Settings → Notifications test button)."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web import routes_push
from ciao.web.push import PushManager
from ciao.web.routes_push import push_test

ENDPOINT = "https://push.example/1"
SUBSCRIPTION = {"endpoint": ENDPOINT, "keys": {"p256dh": "x", "auth": "y"}}


def _make_client(tmp_path: Path, *, subject: str = "mailto:t@localhost") -> TestClient:
    app = Starlette(routes=[Route("/api/push/test", push_test, methods=["POST"])])
    manager = PushManager(tmp_path, subject=subject)
    manager.add(dict(SUBSCRIPTION))
    app.state.push_manager = manager
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_cooldowns():
    routes_push._last_test_at.clear()
    yield
    routes_push._last_test_at.clear()


def test_push_test_requires_endpoint(tmp_path: Path) -> None:
    client = _make_client(tmp_path)

    resp = client.post("/api/push/test", json={})

    assert resp.status_code == 400
    assert "endpoint" in resp.json()["error"]


def test_push_test_unknown_endpoint_is_404(tmp_path: Path) -> None:
    """Only an endpoint the server already holds can be targeted, so this
    cannot become a relay for arbitrary push targets."""
    client = _make_client(tmp_path)

    resp = client.post("/api/push/test", json={"endpoint": "https://push.example/other"})

    assert resp.status_code == 404


def test_push_test_accepted(tmp_path: Path, monkeypatch) -> None:
    import pywebpush

    sent: list[str] = []
    monkeypatch.setattr(
        pywebpush,
        "webpush",
        lambda **kwargs: sent.append(kwargs["subscription_info"]["endpoint"]),
    )
    client = _make_client(tmp_path)

    resp = client.post("/api/push/test", json={"endpoint": ENDPOINT})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "accepted": True}
    assert sent == [ENDPOINT]


def test_push_test_rejected_by_push_service_is_502(tmp_path: Path, monkeypatch) -> None:
    import pywebpush

    def boom(**kwargs):
        raise RuntimeError("push service said no")

    monkeypatch.setattr(pywebpush, "webpush", boom)
    client = _make_client(tmp_path)

    resp = client.post("/api/push/test", json={"endpoint": ENDPOINT})

    assert resp.status_code == 502


def test_push_test_cooldown_is_429(tmp_path: Path, monkeypatch) -> None:
    import pywebpush

    monkeypatch.setattr(pywebpush, "webpush", lambda **kwargs: None)
    client = _make_client(tmp_path)

    first = client.post("/api/push/test", json={"endpoint": ENDPOINT})
    second = client.post("/api/push/test", json={"endpoint": ENDPOINT})

    assert first.status_code == 200
    assert second.status_code == 429


def test_push_test_unconfigured_is_502(tmp_path: Path) -> None:
    client = _make_client(tmp_path, subject="")

    resp = client.post("/api/push/test", json={"endpoint": ENDPOINT})

    assert resp.status_code == 502


def test_push_test_is_session_protected() -> None:
    """Not a public route and not loopback-only: it must carry the normal
    session + origin checks so a phone cannot probe a server it has no
    session for."""
    from ciao.web import auth

    assert "/api/push/test" not in auth._PUBLIC_API
    assert "/api/push/test" not in auth._LOOPBACK_ONLY_API
