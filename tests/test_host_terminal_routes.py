"""`/api/host/terminal`: the auth boundary and the wire contract.

Real ``AuthMiddleware`` and real handlers; ``host_terminal.capabilities`` and
``launch`` are recorders, so nothing is ever opened.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import host_terminal
from ciao.web import auth
from ciao.web.auth import SESSION_COOKIE, AuthMiddleware
from ciao.web.routes_host_terminal import host_terminal_launch, host_terminal_status

SECRET = "test-secret"
BASE = "http://localhost:8443"
PATH = "/api/host/terminal"


class _Recorder:
    def __init__(self) -> None:
        self.launches: list[tuple[Path, str | None, Path]] = []
        self.caps = host_terminal.Capabilities(True, "macos", terminal="Terminal")
        self.launch_error: Exception | None = None

    def capabilities(self) -> host_terminal.Capabilities:
        return self.caps

    def launch(self, folder: Path, *, action: str | None, runtime_dir: Path) -> str:
        if self.launch_error is not None:
            raise self.launch_error
        self.launches.append((folder, action, runtime_dir))
        return "Terminal"


class _Pcm:
    def __init__(self, folder: Path) -> None:
        chat = SimpleNamespace(id="c1", project_id="p1", code_mode=False, repo_location=None)
        self.project = SimpleNamespace(id="p1", repo_path=str(folder), workspace="")
        self.chat = chat

    def get_chat(self, chat_id: str) -> Any:
        return self.chat if chat_id == "c1" else None

    def get_project(self, project_id: str) -> Any:
        return self.project if project_id == "p1" else None


def _client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    peer: str = "127.0.0.1",
    base_url: str = BASE,
) -> tuple[TestClient, _Recorder, Path]:
    recorder = _Recorder()
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    serializer = URLSafeTimedSerializer(SECRET)
    app = Starlette(
        routes=[
            Route(PATH, host_terminal_status, methods=["GET"]),
            Route(PATH, host_terminal_launch, methods=["POST"]),
        ],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = SimpleNamespace(
        workspace_root=tmp_path, state_path=tmp_path / ".runtime" / "state.json"
    )
    app.state.project_chat_manager = _Pcm(repo)
    monkeypatch.setattr(host_terminal, "capabilities", recorder.capabilities)
    monkeypatch.setattr(host_terminal, "launch", recorder.launch)
    return TestClient(app, base_url=base_url, client=(peer, 50000)), recorder, repo


def _signed(client: TestClient, origin: str = BASE) -> dict[str, str]:
    cookie = f"{SESSION_COOKIE}={client.app.state.serializer.dumps({'user': 'owner'})}"
    return {"Cookie": cookie, "Origin": origin}


def test_route_is_in_neither_middleware_allowlist() -> None:
    assert PATH not in auth._PUBLIC_API
    assert PATH not in auth._LOOPBACK_ONLY_API


def test_loopback_without_a_session_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, rec, _ = _client(tmp_path, monkeypatch)
    assert client.get(PATH).status_code == 401
    assert client.post(PATH, json={"action": "gws-install"}, headers={"Origin": BASE}).status_code == 401
    assert rec.launches == []


def test_remote_signed_in_browser_sees_not_on_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = _client(tmp_path, monkeypatch, peer="100.64.0.7")
    response = client.get(PATH, headers=_signed(client))
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False and body["reason"] == "not_on_host" and body["actions"] == []


def test_remote_signed_in_browser_cannot_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, rec, _ = _client(tmp_path, monkeypatch, peer="100.64.0.7")
    response = client.post(PATH, json={"action": "gws-install"}, headers=_signed(client))
    assert response.status_code == 403
    assert response.json()["reason"] == "not_on_host"
    assert rec.launches == []


def test_rebound_host_header_is_not_on_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    origin = "http://evil.example:8443"
    client, rec, _ = _client(tmp_path, monkeypatch, base_url=origin)
    response = client.post(PATH, json={"action": "gws-install"}, headers=_signed(client, origin))
    assert response.status_code == 403
    assert rec.launches == []


def test_loopback_peer_behind_a_proxy_is_not_on_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, rec, _ = _client(tmp_path, monkeypatch)
    headers = {**_signed(client), "X-Forwarded-For": "203.0.113.9"}
    response = client.post(PATH, json={"action": "gws-install"}, headers=headers)
    assert response.status_code == 403
    assert rec.launches == []


def test_cross_origin_post_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, rec, _ = _client(tmp_path, monkeypatch)
    response = client.post(
        PATH, json={"action": "gws-install"}, headers=_signed(client, "https://evil.example")
    )
    assert response.status_code == 403
    assert rec.launches == []


def test_status_on_the_host_reports_capabilities(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = _client(tmp_path, monkeypatch)
    body = client.get(PATH, headers=_signed(client)).json()
    assert body["available"] is True and body["terminal"] == "Terminal"
    assert body["actions"] == list(host_terminal.ACTION_IDS)


def test_action_launches_in_the_workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, rec, _ = _client(tmp_path, monkeypatch)
    response = client.post(PATH, json={"action": "gws-install"}, headers=_signed(client))
    assert response.status_code == 200
    assert response.json()["ok"] is True and response.json()["launched"] == "Terminal"
    assert rec.launches == [(tmp_path, "gws-install", tmp_path / ".runtime")]


@pytest.mark.parametrize(
    "body",
    [
        {"action": "rm -rf"},
        {"action": 5},
        {"command": "ls"},
        {"action": "gws-install", "command": "ls"},
        {"action": "gws-install", "path": "/etc"},
        {"open": "folder"},
        {"open": "/etc", "chat_id": "c1"},
        {"open": "folder", "chat_id": "c1", "path": "/etc"},
        ["gws-install"],
    ],
)
def test_unknown_action_and_free_text_command_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: Any
) -> None:
    client, rec, _ = _client(tmp_path, monkeypatch)
    response = client.post(PATH, json=body, headers=_signed(client))
    assert response.status_code == 400
    assert rec.launches == []


def test_invalid_json_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, rec, _ = _client(tmp_path, monkeypatch)
    response = client.post(PATH, content=b"{nope", headers=_signed(client))
    assert response.status_code == 400
    assert rec.launches == []


def test_folder_open_resolves_from_the_chat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, rec, repo = _client(tmp_path, monkeypatch)
    response = client.post(PATH, json={"open": "folder", "chat_id": "c1"}, headers=_signed(client))
    assert response.status_code == 200
    assert rec.launches == [(repo.resolve(), None, tmp_path / ".runtime")]


def test_unknown_chat_is_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, rec, _ = _client(tmp_path, monkeypatch)
    response = client.post(PATH, json={"open": "folder", "chat_id": "nope"}, headers=_signed(client))
    assert response.status_code == 404
    assert rec.launches == []


def test_headless_host_is_409_with_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, rec, _ = _client(tmp_path, monkeypatch)
    rec.launch_error = host_terminal.TerminalUnavailable("unavailable_headless")
    response = client.post(PATH, json={"action": "gws-install"}, headers=_signed(client))
    assert response.status_code == 409
    assert response.json()["reason"] == "unavailable_headless"


def test_launch_oserror_is_500(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, rec, _ = _client(tmp_path, monkeypatch)
    rec.launch_error = OSError("boom")
    response = client.post(PATH, json={"action": "gws-install"}, headers=_signed(client))
    assert response.status_code == 500
    assert response.json()["reason"] == "launch_failed"
