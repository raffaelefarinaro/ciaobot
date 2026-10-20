"""`/api/welcome*`: the host-only password reveal, folder open and dismissal.

Real ``AuthMiddleware`` and real handlers; the folder opener is a recorder, so
no Finder or Explorer window is ever opened.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.setup_marker import welcome_pending, write_welcome_pending
from ciao.web import auth, routes_welcome
from ciao.web.auth import SESSION_COOKIE, AuthMiddleware
from ciao.web.routes_welcome import (
    welcome_dismiss,
    welcome_open_folder,
    welcome_password,
    welcome_status,
)

SECRET = "test-secret"
BASE = "http://localhost:8443"
PASSWORD = "s3cret-pass"
PATHS = (
    "/api/welcome",
    "/api/welcome/password",
    "/api/welcome/open-folder",
    "/api/welcome/dismiss",
)


def _client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    peer: str = "127.0.0.1",
    base_url: str = BASE,
) -> tuple[TestClient, list[Path]]:
    opened: list[Path] = []
    monkeypatch.setattr(routes_welcome, "_open_path_with_default_app", opened.append)
    serializer = URLSafeTimedSerializer(SECRET)
    app = Starlette(
        routes=[
            Route("/api/welcome", welcome_status, methods=["GET"]),
            Route("/api/welcome/password", welcome_password, methods=["GET"]),
            Route("/api/welcome/open-folder", welcome_open_folder, methods=["POST"]),
            Route("/api/welcome/dismiss", welcome_dismiss, methods=["POST"]),
        ],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = SimpleNamespace(
        workspace_root=tmp_path,
        pwa_auth_token=PASSWORD,
        primary_workspace=lambda: "personal",
        default_provider_for_workspace=lambda name: "claude",
    )
    return TestClient(app, base_url=base_url, client=(peer, 50000)), opened


def _signed(client: TestClient, origin: str = BASE) -> dict[str, str]:
    cookie = f"{SESSION_COOKIE}={client.app.state.serializer.dumps({'user': 'owner'})}"
    return {"Cookie": cookie, "Origin": origin}


def _mark(tmp_path: Path) -> None:
    write_welcome_pending(tmp_path / ".runtime")


def test_routes_are_in_neither_middleware_allowlist() -> None:
    for path in PATHS:
        assert path not in auth._PUBLIC_API
        assert path not in auth._LOOPBACK_ONLY_API


def test_unsigned_loopback_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _mark(tmp_path)
    client, _ = _client(tmp_path, monkeypatch)
    res = client.get("/api/welcome/password")
    assert res.status_code == 401
    assert PASSWORD not in res.text


def test_status_not_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _client(tmp_path, monkeypatch)
    res = client.get("/api/welcome", headers=_signed(client))
    assert res.status_code == 200
    assert res.json() == {"pending": False}


def test_status_pending_on_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _mark(tmp_path)
    client, _ = _client(tmp_path, monkeypatch)
    body = client.get("/api/welcome", headers=_signed(client)).json()
    assert body["pending"] is True
    assert body["workspace_root"] == str(tmp_path.resolve())
    assert body["provider"] == "claude"
    assert body["provider_label"] == "Claude Code"
    assert body["on_host"] is True
    assert "password" not in body


def test_status_remote_reports_not_on_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _mark(tmp_path)
    client, _ = _client(tmp_path, monkeypatch, peer="100.64.0.7")
    body = client.get("/api/welcome", headers=_signed(client)).json()
    assert body["pending"] is True
    assert body["on_host"] is False


def test_password_on_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _mark(tmp_path)
    client, _ = _client(tmp_path, monkeypatch)
    res = client.get("/api/welcome/password", headers=_signed(client))
    assert res.status_code == 200
    assert res.json() == {"password": PASSWORD}
    assert res.headers["cache-control"] == "no-store"


def test_password_remote_peer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _mark(tmp_path)
    client, _ = _client(tmp_path, monkeypatch, peer="100.64.0.7")
    res = client.get("/api/welcome/password", headers=_signed(client))
    assert res.status_code == 403
    assert res.json()["reason"] == "not_on_host"
    assert PASSWORD not in res.text


def test_password_behind_tailscale_serve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _mark(tmp_path)
    tail = "https://mini.tail1234.ts.net"
    client, _ = _client(tmp_path, monkeypatch, base_url=tail)
    headers = {
        **_signed(client, origin=tail),
        "Tailscale-Headers-Info": "x",
        "Tailscale-User-Login": "a@example.com",
        "X-Forwarded-For": "100.64.0.7",
    }
    res = client.get("/api/welcome/password", headers=headers)
    assert res.status_code == 403
    assert res.json()["reason"] == "not_on_host"
    assert PASSWORD not in res.text

    local, _ = _client(tmp_path, monkeypatch)
    headers = {**_signed(local), "Tailscale-Headers-Info": "x"}
    res = local.get("/api/welcome/password", headers=headers)
    assert res.status_code == 403
    assert PASSWORD not in res.text


def test_password_rebound_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _mark(tmp_path)
    evil = "http://evil.example:8443"
    client, _ = _client(tmp_path, monkeypatch, base_url=evil)
    res = client.get("/api/welcome/password", headers=_signed(client, origin=evil))
    assert res.status_code == 403
    assert PASSWORD not in res.text


def test_password_not_pending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _client(tmp_path, monkeypatch)
    res = client.get("/api/welcome/password", headers=_signed(client))
    assert res.status_code == 404
    assert PASSWORD not in res.text


def test_open_folder_on_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, opened = _client(tmp_path, monkeypatch)
    res = client.post("/api/welcome/open-folder", headers=_signed(client))
    assert res.status_code == 200
    assert opened == [tmp_path.resolve()]


def test_open_folder_remote_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, opened = _client(tmp_path, monkeypatch, peer="100.64.0.7")
    res = client.post("/api/welcome/open-folder", headers=_signed(client))
    assert res.status_code == 403
    assert opened == []


def test_dismiss_clears_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _mark(tmp_path)
    client, _ = _client(tmp_path, monkeypatch, peer="100.64.0.7")
    assert client.post("/api/welcome/dismiss", headers=_signed(client)).status_code == 200
    assert welcome_pending(tmp_path / ".runtime") is False
    assert client.get("/api/welcome", headers=_signed(client)).json() == {"pending": False}
    assert client.post("/api/welcome/dismiss", headers=_signed(client)).status_code == 200


def test_password_refused_for_a_different_port_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A different local port is a different origin, so it is not the host."""
    _mark(tmp_path)
    client, _ = _client(tmp_path, monkeypatch)
    res = client.get(
        "/api/welcome/password", headers=_signed(client, origin="http://localhost:3000")
    )
    assert res.status_code == 403
    assert PASSWORD not in res.text


def test_open_folder_refused_for_a_different_port_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A different local port is a different origin, so it is not the host."""
    client, opened = _client(tmp_path, monkeypatch)
    res = client.post(
        "/api/welcome/open-folder", headers=_signed(client, origin="http://localhost:3000")
    )
    assert res.status_code == 403
    assert opened == []


def test_open_folder_timeout_is_a_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = _client(tmp_path, monkeypatch)

    def _hang(path: Path) -> None:
        raise subprocess.TimeoutExpired(["open"], 30)

    monkeypatch.setattr(routes_welcome, "_open_path_with_default_app", _hang)
    res = client.post("/api/welcome/open-folder", headers=_signed(client))
    assert res.status_code == 500
    assert "error" in res.json()
