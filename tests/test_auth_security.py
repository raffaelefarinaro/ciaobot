from __future__ import annotations

from types import SimpleNamespace

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web.auth import AuthMiddleware, SESSION_COOKIE
from ciao.web.routes_api import auth_login, auth_logout


async def _ok(_request):
    return JSONResponse({"ok": True})


def _auth_cookie(serializer: URLSafeTimedSerializer) -> dict[str, str]:
    return {SESSION_COOKIE: serializer.dumps({"user": "owner"})}


def _protected_client() -> tuple[TestClient, URLSafeTimedSerializer]:
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/api/demo", _ok, methods=["GET", "POST"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    return TestClient(app, base_url="https://ciao.example"), serializer


def test_state_changing_request_rejects_cross_origin() -> None:
    client, serializer = _protected_client()
    resp = client.post(
        "/api/demo",
        cookies=_auth_cookie(serializer),
        headers={"Origin": "https://evil.example"},
    )

    assert resp.status_code == 403


def test_state_changing_request_allows_matching_origin() -> None:
    client, serializer = _protected_client()
    resp = client.post(
        "/api/demo",
        cookies=_auth_cookie(serializer),
        headers={"Origin": "https://ciao.example"},
    )

    assert resp.status_code == 200


def test_safe_request_does_not_require_origin() -> None:
    client, serializer = _protected_client()
    resp = client.get("/api/demo", cookies=_auth_cookie(serializer))

    assert resp.status_code == 200


def _auth_client() -> TestClient:
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[
            Route("/api/auth", auth_login, methods=["POST"]),
            Route("/api/auth/logout", auth_logout, methods=["POST"]),
        ],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = SimpleNamespace(pwa_auth_token="test-token")
    return TestClient(app, base_url="https://ciao.example")


def _setup_token_client(tmp_path, *, base_url: str = "http://localhost:8443") -> TestClient:
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[Route("/", _ok, methods=["GET"])],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = SimpleNamespace(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
    )
    return TestClient(app, base_url=base_url)


def test_login_cookie_is_secure_and_host_only() -> None:
    resp = _auth_client().post("/api/auth", json={"token": "test-token"})

    assert resp.status_code == 200
    set_cookie = resp.headers["set-cookie"]
    assert "ciao_session=" in set_cookie
    # Host-only cookie: no Domain attribute, scoped to the exact host.
    assert "Domain=" not in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Secure" in set_cookie


def test_logout_clears_host_only_cookie() -> None:
    client = _auth_client()
    login = client.post("/api/auth", json={"token": "test-token"})
    assert login.status_code == 200

    resp = client.post(
        "/api/auth/logout",
        headers={"Origin": "https://ciao.example"},
    )

    assert resp.status_code == 200
    set_cookie = resp.headers["set-cookie"]
    assert "Domain=" not in set_cookie
    assert "Max-Age=0" in set_cookie


def test_setup_token_redeems_localhost_session_and_deletes_token(tmp_path) -> None:
    token_path = tmp_path / ".runtime" / "setup-token"
    token_path.parent.mkdir()
    token_path.write_text("setup-secret\n", encoding="utf-8")

    resp = _setup_token_client(tmp_path).get("/?setup=setup-secret", follow_redirects=False)

    assert resp.status_code == 302
    assert resp.headers["location"] == "/"
    set_cookie = resp.headers["set-cookie"]
    assert "ciao_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Secure" not in set_cookie
    assert not token_path.exists()


def test_setup_token_rejects_non_localhost_host(tmp_path) -> None:
    token_path = tmp_path / ".runtime" / "setup-token"
    token_path.parent.mkdir()
    token_path.write_text("setup-secret\n", encoding="utf-8")

    resp = _setup_token_client(tmp_path, base_url="https://ciao.example").get(
        "/?setup=setup-secret",
        follow_redirects=False,
    )

    assert resp.status_code == 403
    assert "set-cookie" not in resp.headers
    assert token_path.exists()


def test_setup_token_rejects_invalid_token(tmp_path) -> None:
    token_path = tmp_path / ".runtime" / "setup-token"
    token_path.parent.mkdir()
    token_path.write_text("setup-secret\n", encoding="utf-8")

    resp = _setup_token_client(tmp_path).get("/?setup=wrong", follow_redirects=False)

    assert resp.status_code == 401
    assert "set-cookie" not in resp.headers
    assert token_path.exists()
