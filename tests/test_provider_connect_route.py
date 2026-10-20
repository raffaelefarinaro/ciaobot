"""`POST /api/settings/providers/{provider}/connect`: host-only, via host_terminal.

Real ``AuthMiddleware`` and real handler; ``host_terminal.launch`` is a
recorder, so no terminal is ever opened.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import host_terminal, provider_registry
from ciao.web import routes_api
from ciao.web.auth import SESSION_COOKIE, AuthMiddleware
from ciao.web.routes_api import provider_connection_action

SECRET = "test-secret"
BASE = "http://localhost:8443"
PATH = "/api/settings/providers/claude/connect"


class _Recorder:
    def __init__(self) -> None:
        self.launches: list[tuple[Path, str | None, Path]] = []
        self.error: Exception | None = None

    def launch(self, folder: Path, *, action: str | None, runtime_dir: Path) -> str:
        if self.error is not None:
            raise self.error
        self.launches.append((folder, action, runtime_dir))
        return "Terminal"


def _client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    peer: str = "127.0.0.1",
    base_url: str = BASE,
) -> tuple[TestClient, _Recorder]:
    recorder = _Recorder()
    serializer = URLSafeTimedSerializer(SECRET)
    app = Starlette(
        routes=[
            Route("/api/settings/providers/{provider}/{action}", provider_connection_action, methods=["POST"]),
        ],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = SimpleNamespace(
        workspace_root=tmp_path, state_path=tmp_path / ".runtime" / "state.json"
    )
    monkeypatch.setattr(host_terminal, "launch", recorder.launch)
    monkeypatch.setattr(host_terminal, "action_command", lambda a: f"cmd-for-{a}")
    return TestClient(app, base_url=base_url, client=(peer, 50000)), recorder


def _signed(client: TestClient, origin: str = BASE) -> dict[str, str]:
    cookie = f"{SESSION_COOKIE}={client.app.state.serializer.dumps({'user': 'owner'})}"
    return {"Cookie": cookie, "Origin": origin}


def test_unauthenticated_loopback_is_401(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, rec = _client(tmp_path, monkeypatch)
    assert client.post(PATH, headers={"Origin": BASE}).status_code == 401
    assert rec.launches == []


def test_remote_signed_in_browser_gets_403_with_the_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, rec = _client(tmp_path, monkeypatch, peer="100.64.0.7")
    response = client.post(PATH, headers=_signed(client))
    assert response.status_code == 403
    body = response.json()
    assert body["reason"] == "not_on_host"
    assert body["command"] == "cmd-for-claude-login"
    assert "cmd-for-claude-login" in body["error"]
    assert rec.launches == []


def test_rebound_host_is_not_on_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    origin = "http://evil.example:8443"
    client, rec = _client(tmp_path, monkeypatch, base_url=origin)
    response = client.post(PATH, headers=_signed(client, origin))
    assert response.status_code == 403
    assert response.json()["reason"] == "not_on_host"
    assert rec.launches == []


def test_loopback_peer_behind_a_proxy_is_not_on_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, rec = _client(tmp_path, monkeypatch)
    headers = {**_signed(client), "X-Forwarded-For": "203.0.113.9"}
    assert client.post(PATH, headers=headers).status_code == 403
    assert rec.launches == []


def test_cross_origin_post_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, rec = _client(tmp_path, monkeypatch)
    response = client.post(PATH, headers=_signed(client, "https://evil.example"))
    assert response.status_code == 403
    assert rec.launches == []


@pytest.mark.parametrize("provider", provider_registry.provider_ids())
def test_host_browser_opens_the_login_in_a_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    client, rec = _client(tmp_path, monkeypatch)
    response = client.post(f"/api/settings/providers/{provider}/connect", headers=_signed(client))
    assert response.status_code == 202
    assert response.json() == {"ok": True, "launched": "Terminal", "command": f"cmd-for-{provider}-login"}
    assert rec.launches == [(tmp_path, f"{provider}-login", tmp_path / ".runtime")]


def test_headless_host_is_409_with_the_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, rec = _client(tmp_path, monkeypatch)
    rec.error = host_terminal.TerminalUnavailable("unavailable_headless")
    response = client.post(PATH, headers=_signed(client))
    assert response.status_code == 409
    body = response.json()
    assert body["reason"] == "unavailable_headless"
    assert "cmd-for-claude-login" in body["error"]


def test_launch_oserror_is_500_with_the_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, rec = _client(tmp_path, monkeypatch)
    rec.error = OSError("boom")
    response = client.post(PATH, headers=_signed(client))
    assert response.status_code == 500
    body = response.json()
    assert body["reason"] == "launch_failed"
    assert "cmd-for-claude-login" in body["error"]


def test_unknown_provider_is_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, rec = _client(tmp_path, monkeypatch)
    assert client.post("/api/settings/providers/nope/connect", headers=_signed(client)).status_code == 404
    assert rec.launches == []


def test_every_provider_has_a_login_action() -> None:
    for provider_id in provider_registry.provider_ids():
        assert f"{provider_id}-login" in host_terminal.ACTION_IDS


def test_the_macos_only_launcher_is_gone() -> None:
    assert not hasattr(routes_api, "_launch_provider_login")
