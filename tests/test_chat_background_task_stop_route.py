"""Stop for a running native background task or shell (#2203)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.providers.claude import ClaudeProvider
from ciao.providers.opencode import OpencodeProvider
from ciao.web.auth import SESSION_COOKIE, AuthMiddleware
from ciao.web.routes_api import chat_background_task_stop

_ORIGIN = "https://ciao.example"
_URL = "/api/chats/chat-1/background-tasks/{}/stop"


class _ClaudeClient:
    def __init__(self) -> None:
        self.stopped: list[str] = []

    async def stop_task(self, task_id: str) -> None:
        self.stopped.append(task_id)


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _OpencodeClient:
    def __init__(self, status_code: int = 204, error: Exception | None = None) -> None:
        self.deleted: list[str] = []
        self._status = status_code
        self._error = error

    async def delete(self, path: str, **_kw: object) -> _Response:
        self.deleted.append(path)
        if self._error is not None:
            raise self._error
        return _Response(self._status)


def _claude_provider(tmp_path: Path, client: _ClaudeClient) -> ClaudeProvider:
    provider = ClaudeProvider(tmp_path)
    provider._client = client  # type: ignore[assignment]
    provider._native_tasks["t1"] = {
        "command": "sleep 600",
        "task_type": "local_bash",
        "started_at": 1.0,
        "started_mono": 1e18,
    }
    return provider


def _opencode_provider(tmp_path: Path, client: _OpencodeClient) -> OpencodeProvider:
    provider = OpencodeProvider(tmp_path)
    provider._client = client  # type: ignore[assignment]
    provider._process = SimpleNamespace(returncode=None)
    provider._session_id = "ses_1"
    provider._background_shells = {"sh_1"}
    return provider


def _client(provider: object | None, *, chat_exists: bool = True):
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[
            Route(
                "/api/chats/{chat_id}/background-tasks/{task_id}/stop",
                chat_background_task_stop,
                methods=["POST"],
            )
        ],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    chat = SimpleNamespace(chat_id="chat-1", archived=False)
    app.state.project_chat_manager = SimpleNamespace(
        get_chat=lambda cid: chat if chat_exists and cid == "chat-1" else None,
        _providers=(
            {"chat-1": SimpleNamespace(provider=provider)} if provider is not None else {}
        ),
    )
    client = TestClient(app, base_url=_ORIGIN)
    return client, {SESSION_COOKIE: serializer.dumps({"user": "owner"})}


def _post(client: TestClient, cookies: dict, task_id: str):
    return client.post(_URL.format(task_id), cookies=cookies, headers={"Origin": _ORIGIN})


def test_stop_a_tracked_claude_task(tmp_path: Path) -> None:
    sdk = _ClaudeClient()
    client, cookies = _client(_claude_provider(tmp_path, sdk))

    resp = _post(client, cookies, "t1")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert sdk.stopped == ["t1"]


def test_stop_an_unknown_task_is_404(tmp_path: Path) -> None:
    sdk = _ClaudeClient()
    client, cookies = _client(_claude_provider(tmp_path, sdk))

    assert _post(client, cookies, "other").status_code == 404
    assert sdk.stopped == []


def test_stop_without_a_live_provider_is_404() -> None:
    client, cookies = _client(None)
    assert _post(client, cookies, "t1").status_code == 404


def test_stop_in_an_unknown_chat_is_404(tmp_path: Path) -> None:
    client, cookies = _client(
        _claude_provider(tmp_path, _ClaudeClient()), chat_exists=False
    )
    assert _post(client, cookies, "t1").status_code == 404


def test_stop_a_tracked_opencode_shell(tmp_path: Path) -> None:
    sdk = _OpencodeClient()
    client, cookies = _client(_opencode_provider(tmp_path, sdk))

    resp = _post(client, cookies, "sh_1")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert sdk.deleted == ["/api/shell/sh_1"]


def test_stop_an_unknown_opencode_shell_is_404(tmp_path: Path) -> None:
    sdk = _OpencodeClient()
    client, cookies = _client(_opencode_provider(tmp_path, sdk))

    assert _post(client, cookies, "sh_other").status_code == 404
    assert sdk.deleted == []


def test_opencode_refusing_the_stop_is_404_and_a_transport_error_is_502(
    tmp_path: Path,
) -> None:
    refused, cookies = _client(_opencode_provider(tmp_path, _OpencodeClient(404)))
    assert _post(refused, cookies, "sh_1").status_code == 404

    broken, cookies = _client(
        _opencode_provider(tmp_path, _OpencodeClient(error=httpx.ConnectError("down")))
    )
    assert _post(broken, cookies, "sh_1").status_code == 502


def test_stop_requires_the_session_cookie(tmp_path: Path) -> None:
    sdk = _ClaudeClient()
    client, _cookies = _client(_claude_provider(tmp_path, sdk))

    resp = client.post(_URL.format("t1"), headers={"Origin": _ORIGIN})

    assert resp.status_code == 401
    assert sdk.stopped == []


def test_opencode_stop_background_shell_deletes_the_shell(tmp_path: Path) -> None:
    sdk = _OpencodeClient()
    provider = _opencode_provider(tmp_path, sdk)

    assert asyncio.run(provider.stop_background_shell("sh_1")) is True
    assert asyncio.run(provider.stop_background_shell("sh_x")) is False
    assert sdk.deleted == ["/api/shell/sh_1"]


def test_opencode_shell_rows_carry_a_first_seen_started_at(tmp_path: Path) -> None:
    provider = OpencodeProvider(tmp_path)
    provider._track_background_event(
        {
            "type": "shell.created",
            "data": {"info": {"id": "sh_1", "command": "sleep 9", "metadata": {"sessionID": "ses_1"}}},
        },
        "ses_1",
    )
    # A shell found only by the server list is first seen at reconcile time.
    provider._replace_background_shells({"sh_1", "sh_2"})

    rows = {row["id"]: row for row in provider.background_shell_rows()}
    assert set(rows) == {"sh_1", "sh_2"}
    assert all(isinstance(row["started_at"], float) for row in rows.values())

    first = rows["sh_1"]["started_at"]
    provider._replace_background_shells({"sh_1"})
    assert provider.background_shell_rows()[0]["started_at"] == first
    provider._track_background_event(
        {"type": "shell.exited", "data": {"id": "sh_1"}}, "ses_1"
    )
    assert "sh_1" not in provider._shell_started
