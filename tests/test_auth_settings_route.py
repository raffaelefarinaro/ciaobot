from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web.auth import SESSION_COOKIE, AuthMiddleware, make_serializer
from ciao.web.routes_auth import auth_settings_get, auth_settings_update
from ciao.os_support.private import is_private


def _app(tmp_path: Path, *, token: str = "old-secret") -> Starlette:
    app = Starlette(
        routes=[
            Route("/api/auth/settings", auth_settings_get, methods=["GET"]),
            Route("/api/auth/settings", auth_settings_update, methods=["POST"]),
        ]
    )
    config = SimpleNamespace(
        pwa_auth_token=token,
        workspace_root=tmp_path,
    )
    app.state.config = config
    app.state.serializer = make_serializer(token)
    return app


def test_auth_settings_get(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path, token="old-secret"))
    res = client.get("/api/auth/settings")
    assert res.status_code == 200
    assert res.json() == {"password_configured": True}


def test_auth_settings_change_writes_only_the_token(tmp_path: Path) -> None:
    app = _app(tmp_path, token="old-secret")
    client = TestClient(app, client=("127.0.0.1", 5555))

    res = client.post(
        "/api/auth/settings",
        json={"password": "hunter2", "current_password": "old-secret"},
    )
    assert res.status_code == 200
    assert res.json()["password_configured"] is True
    assert app.state.config.pwa_auth_token == "hunter2"
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "PWA_AUTH_TOKEN=hunter2" in env
    assert "PWA_AUTH_REQUIRED" not in env
    # .env holds the password in clear text by design, so it must at least be
    # owner-only
    assert is_private(tmp_path / ".env")
    assert "ciao_session=" in res.headers.get("set-cookie", "")


def test_auth_settings_change_requires_current_password(tmp_path: Path) -> None:
    app = _app(tmp_path, token="old-secret")
    client = TestClient(app)

    bad = client.post(
        "/api/auth/settings",
        json={"password": "new-secret", "current_password": "wrong"},
    )
    assert bad.status_code == 403
    assert bad.json()["code"] == "wrong_current_password"
    assert app.state.config.pwa_auth_token == "old-secret"
    assert "set-cookie" not in bad.headers

    ok = client.post(
        "/api/auth/settings",
        json={"password": "new-secret", "current_password": "old-secret"},
    )
    assert ok.status_code == 200
    assert ok.json()["password_configured"] is True


def test_auth_settings_change_needs_the_current_password_from_loopback_too(tmp_path: Path) -> None:
    """Protection is always on: there is no first-password path without proof."""
    app = _app(tmp_path, token="old-secret")
    client = TestClient(app, client=("127.0.0.1", 5555))

    res = client.post("/api/auth/settings", json={"password": "hunter2"})

    assert res.status_code == 400
    assert res.json()["code"] == "current_password_required"
    assert app.state.config.pwa_auth_token == "old-secret"
    assert not (tmp_path / ".env").exists()


def test_auth_settings_wrong_current_password_is_never_401(tmp_path: Path) -> None:
    """401 means the session is gone and sends the PWA to /login."""
    serializer = make_serializer("old-secret")
    app = Starlette(
        routes=[Route("/api/auth/settings", auth_settings_update, methods=["POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.config = SimpleNamespace(pwa_auth_token="old-secret", workspace_root=tmp_path)
    app.state.serializer = serializer
    client = TestClient(app, base_url="https://ciao.example")
    client.cookies.set(SESSION_COOKIE, serializer.dumps({"user": "owner"}))

    res = client.post(
        "/api/auth/settings",
        json={"password": "new-secret", "current_password": "wrong"},
        headers={"Origin": "https://ciao.example"},
    )

    assert res.status_code == 403
    assert res.json()["code"] == "wrong_current_password"


def test_auth_settings_rejects_a_too_short_password(tmp_path: Path) -> None:
    app = _app(tmp_path, token="old-secret")
    client = TestClient(app, client=("127.0.0.1", 5555))

    res = client.post(
        "/api/auth/settings",
        json={"password": "ab", "current_password": "old-secret"},
    )

    assert res.status_code == 400
    assert app.state.config.pwa_auth_token == "old-secret"


def test_auth_settings_change_from_remote_peer_still_works(tmp_path: Path) -> None:
    """The current password is the proof — location is not."""
    app = _app(tmp_path, token="old-secret")
    client = TestClient(app, client=("10.0.0.9", 5555))

    res = client.post(
        "/api/auth/settings",
        json={"password": "new-secret", "current_password": "old-secret"},
    )
    assert res.status_code == 200
    assert app.state.config.pwa_auth_token == "new-secret"
