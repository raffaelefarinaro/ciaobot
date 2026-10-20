"""POST /api/auth/host-sign-in: loopback-only, opens a one-time sign-in page on the host."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.os_support.private import is_private
from ciao.web import routes_api, routes_auth
from ciao.web.auth import SESSION_COOKIE, AuthMiddleware
from ciao.web.routes_auth import auth_host_sign_in

_TAILSCALE_SERVE_HEADERS = {
    "Tailscale-Headers-Info": "https://tailscale.com/s/serve-headers",
    "Tailscale-User-Login": "someone@example.com",
    "X-Forwarded-For": "100.101.252.27",
    "X-Forwarded-Host": "mini.tail1.ts.net",
    "X-Forwarded-Proto": "https",
}


async def _ok(request):
    return JSONResponse({"ok": True})


@pytest.fixture
def serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer("test-secret")


@pytest.fixture
def opened(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr(routes_auth, "_last_host_sign_in", 0.0)
    monkeypatch.setattr(routes_auth, "_desktop_available", lambda: True)
    monkeypatch.setattr(routes_api, "_open_path_with_default_app", calls.append)
    return calls


def _make_app(tmp_path, serializer, *, bootstrap_mode=False) -> Starlette:
    app = Starlette(
        routes=[
            Route("/api/auth/host-sign-in", auth_host_sign_in, methods=["POST"]),
            Route("/", _ok),
        ],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = SimpleNamespace(
        pwa_auth_token="pw", workspace_root=tmp_path, bootstrap_mode=bootstrap_mode
    )
    return app


def _client(app, base_url="http://localhost:8443", peer="127.0.0.1") -> TestClient:
    return TestClient(app, base_url=base_url, client=(peer, 5555))


_HOST_ORIGIN = {"Origin": "http://localhost:8443"}


def _token_file(tmp_path):
    return tmp_path / ".runtime" / "setup-token"


def _pages(tmp_path):
    return list((tmp_path / ".runtime").glob("sign-in-*.html"))


def test_host_sign_in_opens_a_private_page_with_a_fresh_token(tmp_path, serializer, opened):
    client = _client(_make_app(tmp_path, serializer))

    response = client.post("/api/auth/host-sign-in", headers=_HOST_ORIGIN)

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert len(opened) == 1
    page = opened[0]
    assert page.parent == tmp_path / ".runtime"
    assert page.match("sign-in-*.html")
    token = _token_file(tmp_path).read_text(encoding="utf-8").strip()
    assert token
    assert f"http://localhost:8443/?setup={token}" in page.read_text(encoding="utf-8")
    assert is_private(page)
    assert is_private(_token_file(tmp_path))
    assert token not in response.text
    assert token not in str(dict(response.headers))


def test_the_opened_token_signs_the_browser_in(tmp_path, serializer, opened):
    client = _client(_make_app(tmp_path, serializer))
    client.post("/api/auth/host-sign-in", headers=_HOST_ORIGIN)
    token = _token_file(tmp_path).read_text(encoding="utf-8").strip()

    response = client.get(f"/?setup={token}", follow_redirects=False)

    assert response.status_code == 302
    assert SESSION_COOKIE in response.headers["set-cookie"]
    assert not _token_file(tmp_path).exists()


def test_tailscale_serve_cannot_trigger_host_sign_in(tmp_path, serializer, opened):
    app = _make_app(tmp_path, serializer)
    tailscale = _client(app, base_url="https://mini.tail1.ts.net")
    forwarded = _client(app)

    first = tailscale.post("/api/auth/host-sign-in", headers=_TAILSCALE_SERVE_HEADERS)
    second = forwarded.post(
        "/api/auth/host-sign-in", headers={**_HOST_ORIGIN, "X-Forwarded-For": "100.1.2.3"}
    )

    assert first.status_code == 401
    assert second.status_code == 401
    assert opened == []
    assert not _token_file(tmp_path).exists()


def test_signed_in_remote_browser_is_refused(tmp_path, serializer, opened):
    client = _client(
        _make_app(tmp_path, serializer), base_url="http://ciao.example", peer="10.0.0.9"
    )
    client.cookies.set(SESSION_COOKIE, serializer.dumps({"user": "owner"}))

    response = client.post(
        "/api/auth/host-sign-in", headers={"Origin": "http://ciao.example"}
    )

    assert response.status_code == 403
    assert response.json()["reason"] == "not_on_host"
    assert opened == []
    assert not _token_file(tmp_path).exists()


def test_cross_origin_page_is_refused(tmp_path, serializer, opened):
    client = _client(_make_app(tmp_path, serializer))

    response = client.post(
        "/api/auth/host-sign-in", headers={"Origin": "http://localhost:3000"}
    )

    assert response.status_code == 403
    assert opened == []
    assert not _token_file(tmp_path).exists()


def test_no_desktop_reports_no_desktop(tmp_path, serializer, opened, monkeypatch):
    monkeypatch.setattr(routes_auth, "_desktop_available", lambda: False)
    client = _client(_make_app(tmp_path, serializer))

    response = client.post("/api/auth/host-sign-in", headers=_HOST_ORIGIN)

    assert response.status_code == 409
    assert response.json()["reason"] == "no_desktop"
    assert opened == []
    assert not _token_file(tmp_path).exists()


def test_cooldown(tmp_path, serializer, opened):
    client = _client(_make_app(tmp_path, serializer))

    first = client.post("/api/auth/host-sign-in", headers=_HOST_ORIGIN)
    second = client.post("/api/auth/host-sign-in", headers=_HOST_ORIGIN)

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["reason"] == "cooldown"
    assert len(opened) == 1


def test_open_failure_cleans_up(tmp_path, serializer, monkeypatch):
    monkeypatch.setattr(routes_auth, "_last_host_sign_in", 0.0)
    monkeypatch.setattr(routes_auth, "_desktop_available", lambda: True)

    def boom(path):
        raise OSError("no opener")

    monkeypatch.setattr(routes_api, "_open_path_with_default_app", boom)
    client = _client(_make_app(tmp_path, serializer))

    response = client.post("/api/auth/host-sign-in", headers=_HOST_ORIGIN)

    assert response.status_code == 409
    assert response.json()["reason"] == "open_failed"
    assert _pages(tmp_path) == []
    assert not _token_file(tmp_path).exists()


def test_bootstrap_mode_is_refused(tmp_path, serializer, opened):
    client = _client(_make_app(tmp_path, serializer, bootstrap_mode=True))

    response = client.post("/api/auth/host-sign-in", headers=_HOST_ORIGIN)

    assert response.status_code == 409
    assert response.json()["reason"] == "setup_required"
    assert opened == []


def test_ipv6_loopback_host_builds_a_bracketed_url(tmp_path, serializer, opened):
    client = _client(
        _make_app(tmp_path, serializer), base_url="http://[::1]:8443", peer="::1"
    )

    response = client.post(
        "/api/auth/host-sign-in", headers={"Origin": "http://[::1]:8443"}
    )

    assert response.status_code == 200
    assert "http://[::1]:8443/?setup=" in opened[0].read_text(encoding="utf-8")


def test_host_header_port_is_ignored(tmp_path, serializer, opened):
    client = _client(_make_app(tmp_path, serializer))

    response = client.post("/api/auth/host-sign-in", headers={"Host": "localhost:9999"})

    assert response.status_code == 403
    assert response.json()["reason"] == "not_on_host"
    assert opened == []
    assert not _token_file(tmp_path).exists()


def test_portless_host_uses_the_socket_port(tmp_path, serializer, opened):
    client = _client(_make_app(tmp_path, serializer))

    response = client.post("/api/auth/host-sign-in", headers={"Host": "localhost"})

    assert response.status_code == 200
    assert "http://localhost:8443/?setup=" in opened[0].read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_existing_world_readable_token_file_is_recreated_private(tmp_path, serializer, opened):
    token_file = _token_file(tmp_path)
    token_file.parent.mkdir()
    token_file.write_text("old\n", encoding="utf-8")
    os.chmod(token_file, 0o644)
    client = _client(_make_app(tmp_path, serializer))

    response = client.post("/api/auth/host-sign-in", headers=_HOST_ORIGIN)

    assert response.status_code == 200
    assert is_private(token_file)
