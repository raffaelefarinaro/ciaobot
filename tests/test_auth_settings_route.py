from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web.auth import make_serializer
from ciao.web.routes_auth import auth_settings_get, auth_settings_update


def _app(tmp_path: Path, *, auth_required: bool = False, token: str = "old-secret") -> Starlette:
    app = Starlette(
        routes=[
            Route("/api/auth/settings", auth_settings_get, methods=["GET"]),
            Route("/api/auth/settings", auth_settings_update, methods=["POST"]),
        ]
    )
    config = SimpleNamespace(
        pwa_auth_required=auth_required,
        pwa_auth_token=token,
        workspace_root=tmp_path,
    )
    app.state.config = config
    app.state.serializer = make_serializer(token)
    return app


def test_auth_settings_get(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path, auth_required=False, token=""))
    res = client.get("/api/auth/settings")
    assert res.status_code == 200
    assert res.json() == {"auth_required": False, "password_configured": False}


def test_auth_settings_enable_password(tmp_path: Path) -> None:
    app = _app(tmp_path, auth_required=False, token="")
    client = TestClient(app, client=("127.0.0.1", 5555))

    res = client.post("/api/auth/settings", json={"password": "hunter2"})
    assert res.status_code == 200
    body = res.json()
    assert body["auth_required"] is True
    assert body["password_configured"] is True
    assert app.state.config.pwa_auth_required is True
    assert app.state.config.pwa_auth_token == "hunter2"
    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "PWA_AUTH_REQUIRED=true" in env
    assert "PWA_AUTH_TOKEN=hunter2" in env
    assert "ciao_session=" in res.headers.get("set-cookie", "")


def test_auth_settings_change_requires_current_password(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path, auth_required=True, token="old-secret"))

    bad = client.post(
        "/api/auth/settings",
        json={"password": "new-secret", "current_password": "wrong"},
    )
    assert bad.status_code == 401

    ok = client.post(
        "/api/auth/settings",
        json={"password": "new-secret", "current_password": "old-secret"},
    )
    assert ok.status_code == 200
    assert ok.json()["auth_required"] is True


def test_auth_settings_cannot_disable_protection(tmp_path: Path) -> None:
    """Protection is the default: Settings changes the password, and only an
    explicit PWA_AUTH_REQUIRED=false in the workspace .env turns it off."""
    app = _app(tmp_path, auth_required=True, token="old-secret")
    client = TestClient(app, client=("127.0.0.1", 5555))

    res = client.post(
        "/api/auth/settings",
        json={"auth_required": False, "current_password": "old-secret"},
    )

    assert res.status_code == 400
    assert "PWA_AUTH_REQUIRED=false" in res.json()["error"]
    assert app.state.config.pwa_auth_required is True
    assert not (tmp_path / ".env").exists()


def test_auth_settings_rejects_a_too_short_password(tmp_path: Path) -> None:
    app = _app(tmp_path, auth_required=True, token="old-secret")
    client = TestClient(app, client=("127.0.0.1", 5555))

    res = client.post(
        "/api/auth/settings",
        json={"password": "ab", "current_password": "old-secret"},
    )

    assert res.status_code == 400
    assert app.state.config.pwa_auth_token == "old-secret"


def test_auth_settings_enable_allowed_from_remote_peer(tmp_path: Path) -> None:
    """A headless host is only reachable remotely, so it must be protectable that way.

    Requiring a local caller here would leave a Mac mini reached over a tailnet
    from a phone permanently unprotectable; the call is logged instead.
    """
    app = _app(tmp_path, auth_required=False, token="")
    client = TestClient(app, client=("10.0.0.9", 5555))

    res = client.post(
        "/api/auth/settings",
        json={"password": "hunter2"},
    )
    assert res.status_code == 200
    assert app.state.config.pwa_auth_required is True
    assert app.state.config.pwa_auth_token == "hunter2"


def test_auth_settings_change_from_remote_peer_still_works(tmp_path: Path) -> None:
    """Once protection is on, the current password is the proof — location is not."""
    app = _app(tmp_path, auth_required=True, token="old-secret")
    client = TestClient(app, client=("10.0.0.9", 5555))

    res = client.post(
        "/api/auth/settings",
        json={"password": "new-secret", "current_password": "old-secret"},
    )
    assert res.status_code == 200
    assert app.state.config.pwa_auth_token == "new-secret"
