"""The paste-back route for a needs-you sign-in: POST /api/chats/{id}/actions/{aid}/callback.

The route is the real one behind the real session gate, over a real manager. The
replay goes to a real loopback listener, so a test sees exactly what the provider's
listener would see.
"""

from __future__ import annotations

import logging
import socket
import threading
from collections.abc import Callable, Iterator, Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.chat_actions import ChatAction, ChatActionError
from ciao.oauth_callback import CallbackTarget
from ciao.web import chat_actions_runtime
from ciao.web.chat_actions_base import ActionContext, ActionHandler
from ciao.web.auth import SESSION_COOKIE, AuthMiddleware
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_chat_actions import (
    chat_action_callback,
    chat_action_continue,
    chat_action_dismiss,
    chat_action_start,
)
from tests.test_chat_actions_runtime import make_manager, make_runtime

_ORIGIN = "https://ciao.example"
_HEADERS = {"Origin": _ORIGIN}
_JSON_HEADERS = {"Origin": _ORIGIN, "Content-Type": "application/json"}
_CALLBACK = "/api/chats/{cid}/actions/{aid}/callback"

Listener = tuple[int, list[str], Callable[..., None]]


def _client(manager: ProjectChatManager, *, signed_in: bool = True) -> TestClient:
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[
            Route("/api/chats/{chat_id}/actions/{action_id}/start", chat_action_start, methods=["POST"]),
            Route("/api/chats/{chat_id}/actions/{action_id}/dismiss", chat_action_dismiss, methods=["POST"]),
            Route("/api/chats/{chat_id}/actions/{action_id}/continue", chat_action_continue, methods=["POST"]),
            Route("/api/chats/{chat_id}/actions/{action_id}/callback", chat_action_callback, methods=["POST"]),
        ],
        middleware=[Middleware(AuthMiddleware, serializer=serializer)],
    )
    app.state.serializer = serializer
    app.state.config = manager._config
    app.state.project_chat_manager = manager
    client = TestClient(app, base_url=_ORIGIN, raise_server_exceptions=False)
    if signed_in:
        client.cookies.set(SESSION_COOKIE, serializer.dumps({"user": "owner"}))
    return client


def _waiting_card(
    manager: ProjectChatManager,
    port: int,
    *,
    provider: str = "claude",
    host: str = "localhost",
    waiting: bool = True,
) -> tuple[str, str]:
    """A chat with an ``mcp_auth`` card whose authorize URL names ``host:port``.

    The card is ``waiting`` (with that authorize URL) unless ``waiting`` is false, then ``open``.
    """
    project = manager.create_project("Paste", workspace="work")
    chat = manager.create_chat(project.project_id, title="Paste", provider=provider)
    aid = str(
        manager.raise_action(
            chat.chat_id, "mcp_auth", {"server": "notion", "provider": provider}, origin="engine"
        )["id"]
    )
    if waiting:
        authorize_url = (
            "https://mcp.notion.com/authorize?response_type=code"
            f"&redirect_uri=http%3A%2F%2F{host}%3A{port}%2Fcallback&state=S1"
        )
        manager.update_action(chat.chat_id, aid, state="waiting", params_update={"url": authorize_url})
    return chat.chat_id, aid


@pytest.fixture
def manager(tmp_path: Path) -> ProjectChatManager:
    return make_manager(tmp_path)


@pytest.fixture
def listener() -> Iterator[Listener]:
    """A real loopback HTTP listener. Yields (port, paths seen, set_status(status, location=""))."""
    paths: list[str] = []
    answer: dict[str, Any] = {"status": 200, "location": ""}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            paths.append(self.path)
            self.send_response(answer["status"])
            if answer["location"]:
                self.send_header("Location", answer["location"])
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def set_status(status: int, location: str = "") -> None:
        answer["status"] = status
        answer["location"] = location

    try:
        yield server.server_address[1], paths, set_status
    finally:
        server.shutdown()
        server.server_close()


def _recorder(monkeypatch: pytest.MonkeyPatch) -> list[CallbackTarget]:
    calls: list[CallbackTarget] = []

    async def record(target: CallbackTarget) -> int:
        calls.append(target)
        return 200

    monkeypatch.setattr(chat_actions_runtime, "replay_callback", record)
    return calls


def _post(client: TestClient, cid: str, aid: str, body: dict[str, object]) -> Any:
    return client.post(_CALLBACK.format(cid=cid, aid=aid), json=body, headers=_HEADERS)


def test_callback_replays_exactly_to_the_flow_listener(manager: ProjectChatManager, listener: Listener) -> None:
    port, paths, _ = listener
    cid, aid = _waiting_card(manager, port)
    response = _post(
        _client(manager), cid, aid, {"url": f"http://localhost:{port}/callback?code=abc&state=S1&iss=x"}
    )
    assert response.status_code == 200
    assert paths == ["/callback?code=abc&state=S1&iss=x"]
    action = response.json()["action"]
    assert action["state"] == "waiting"
    assert action["params"]["callback_used"] == "1"


def test_an_opencode_card_replays_the_same_way(manager: ProjectChatManager, listener: Listener) -> None:
    port, paths, _ = listener
    cid, aid = _waiting_card(manager, port, provider="opencode", host="127.0.0.1")
    response = _post(_client(manager), cid, aid, {"url": f"http://127.0.0.1:{port}/callback?code=x&state=S1"})
    assert response.status_code == 200
    assert paths == ["/callback?code=x&state=S1"]


def test_callback_is_single_use(manager: ProjectChatManager, listener: Listener) -> None:
    port, paths, _ = listener
    cid, aid = _waiting_card(manager, port)
    client = _client(manager)
    body = {"url": f"http://localhost:{port}/callback?code=abc&state=S1"}
    assert _post(client, cid, aid, body).status_code == 200
    second = _post(client, cid, aid, body)
    assert second.status_code == 400
    assert second.json()["error"] == "already_used"
    assert len(paths) == 1


def test_refused_url_sends_nothing(manager: ProjectChatManager, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _recorder(monkeypatch)
    cid, aid = _waiting_card(manager, 62471)
    response = _post(_client(manager), cid, aid, {"url": "http://localhost:62471/callback?code=a&state=S9"})
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "state_mismatch"
    assert body["action"]["state"] == "waiting"
    assert calls == []
    action = manager.get_action(cid, aid)
    assert action is not None
    assert "callback_used" not in action.params


def test_replay_target_comes_from_the_stored_authorize_url(
    manager: ProjectChatManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _recorder(monkeypatch)
    cid, aid = _waiting_card(manager, 62471)
    response = _post(_client(manager), cid, aid, {"url": "http://localhost:62471/callback?code=abc&state=S1"})
    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0].port == 62471
    assert calls[0].path == "/callback"
    assert calls[0].replay_url().startswith("http://127.0.0.1:62471/callback?")


def test_redirect_is_not_followed(manager: ProjectChatManager, listener: Listener) -> None:
    port, paths, set_status = listener
    set_status(302, f"http://127.0.0.1:{port}/second")
    cid, aid = _waiting_card(manager, port)
    response = _post(_client(manager), cid, aid, {"url": f"http://localhost:{port}/callback?code=abc&state=S1"})
    assert response.status_code == 200
    assert response.json()["action"]["state"] == "waiting"
    assert len(paths) == 1
    assert paths[0].startswith("/callback?")
    assert "/second" not in paths


def test_proxy_env_is_ignored(
    manager: ProjectChatManager, listener: Listener, monkeypatch: pytest.MonkeyPatch
) -> None:
    port, paths, _ = listener
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    cid, aid = _waiting_card(manager, port)
    response = _post(_client(manager), cid, aid, {"url": f"http://localhost:{port}/callback?code=abc&state=S1"})
    assert response.status_code == 200
    assert len(paths) == 1
    assert response.json()["action"]["state"] == "waiting"


def test_unreachable_listener_fails_the_action(manager: ProjectChatManager) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    cid, aid = _waiting_card(manager, port)
    response = _post(_client(manager), cid, aid, {"url": f"http://localhost:{port}/callback?code=abc&state=S1"})
    assert response.status_code == 200
    action = response.json()["action"]
    assert action["state"] == "failed"
    assert action["error"] == chat_actions_runtime.UNREACHABLE_ERROR


def test_listener_400_fails_the_action(manager: ProjectChatManager, listener: Listener) -> None:
    port, _, set_status = listener
    set_status(400)
    cid, aid = _waiting_card(manager, port)
    response = _post(_client(manager), cid, aid, {"url": f"http://localhost:{port}/callback?code=abc&state=S1"})
    assert response.status_code == 200
    action = response.json()["action"]
    assert action["state"] == "failed"
    assert action["error"] == chat_actions_runtime.REFUSED_ERROR


def test_not_waiting_is_400_invalid_state(manager: ProjectChatManager) -> None:
    cid, aid = _waiting_card(manager, 62471, waiting=False)
    response = _post(_client(manager), cid, aid, {"url": "http://localhost:62471/callback?code=a&state=S1"})
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_state"
    assert body["action"]["state"] == "open"


def test_a_kind_without_a_callback_is_400_unsupported_kind(
    manager: ProjectChatManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _recorder(monkeypatch)
    project = manager.create_project("Command", workspace="work")
    chat = manager.create_chat(project.project_id, title="Command", provider="claude")
    aid = str(manager.raise_action(chat.chat_id, "command", {"command": "ls"}, origin="agent")["id"])
    manager.update_action(chat.chat_id, aid, state="waiting")
    response = _post(_client(manager), chat.chat_id, aid, {"url": "http://localhost:62471/callback?code=a&state=S1"})
    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_kind"
    assert calls == []


def test_a_stale_id_is_409(manager: ProjectChatManager) -> None:
    cid, _ = _waiting_card(manager, 62471)
    body = {"url": "http://localhost:62471/callback?code=a&state=S1"}
    response = _post(_client(manager), cid, "no-such-action", body)
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "chat_action_conflict"
    assert body["actions"] == manager.chat_action_payloads(cid)


def test_unknown_chat_is_404(manager: ProjectChatManager) -> None:
    response = _post(_client(manager), "no-such-chat", "x", {"url": "http://localhost:62471/callback?code=a&state=S1"})
    assert response.status_code == 404
    assert response.json() == {"error": "not found"}


def test_body_shape(manager: ProjectChatManager) -> None:
    cid, aid = _waiting_card(manager, 62471)
    client = _client(manager)
    url = _CALLBACK.format(cid=cid, aid=aid)
    responses = [
        client.post(url, headers=_JSON_HEADERS, json={"url": 1}),
        client.post(url, headers=_JSON_HEADERS, json={"url": "x", "extra": 1}),
        client.post(url, headers=_JSON_HEADERS, content=b"[]"),
        client.post(url, headers=_JSON_HEADERS, content=b"not json"),
    ]
    for response in responses:
        assert response.status_code == 400
        assert response.json() == {"error": "invalid_body", "action": None}
    too_large = client.post(url, headers=_JSON_HEADERS, content=b"x" * 9000)
    assert too_large.status_code == 413
    assert too_large.json() == {"error": "too_large", "action": None}


def test_callback_route_requires_session(manager: ProjectChatManager) -> None:
    cid, aid = _waiting_card(manager, 62471)
    body = {"url": "http://localhost:62471/callback?code=a&state=S1"}
    response = _post(_client(manager, signed_in=False), cid, aid, body)
    assert response.status_code == 401


def test_code_is_not_logged(
    manager: ProjectChatManager,
    listener: Listener,
    caplog: pytest.LogCaptureFixture,
) -> None:
    port, paths, _ = listener
    cid, aid = _waiting_card(manager, port)
    caplog.set_level(logging.DEBUG)
    response = _post(_client(manager), cid, aid, {"url": f"http://localhost:{port}/callback?code=SECRETCODE&state=S1"})
    assert response.status_code == 200
    assert paths == ["/callback?code=SECRETCODE&state=S1"]
    assert "SECRETCODE" not in caplog.text
    # The test client itself logs under "httpx" (its POST to ciao.example). The replay
    # must not log under it either: an AsyncClient would log the 127.0.0.1 replay URL.
    assert not [
        record for record in caplog.records if record.name == "httpx" and "127.0.0.1" in record.getMessage()
    ]
    assert "callback replayed (status 200)" in caplog.text


def test_an_agent_cannot_raise_callback_used(manager: ProjectChatManager) -> None:
    project = manager.create_project("Agent", workspace="work")
    chat = manager.create_chat(project.project_id, title="Agent", provider="claude")
    with pytest.raises(ChatActionError) as refused:
        manager.raise_action(
            chat.chat_id,
            "mcp_auth",
            {"server": "notion", "provider": "claude", "callback_used": "1"},
            origin="agent",
        )
    assert refused.value.reason == "unknown_param"
    aid = str(
        manager.raise_action(
            chat.chat_id, "mcp_auth", {"server": "notion", "provider": "claude"}, origin="engine"
        )["id"]
    )
    with pytest.raises(ChatActionError) as invalid:
        manager.update_action(chat.chat_id, aid, state="waiting", params_update={"callback_used": "2"})
    assert invalid.value.reason == "invalid_param"


def test_callback_used_accepts_zero_and_one(manager: ProjectChatManager) -> None:
    project = manager.create_project("Mark", workspace="work")
    chat = manager.create_chat(project.project_id, title="Mark", provider="claude")
    aid = str(
        manager.raise_action(
            chat.chat_id, "mcp_auth", {"server": "notion", "provider": "claude"}, origin="engine"
        )["id"]
    )
    for mark in ("0", "1"):
        payload = manager.update_action(
            chat.chat_id, aid, state="waiting", params_update={"callback_used": mark}
        )
        assert payload is not None
        assert payload["params"]["callback_used"] == mark  # type: ignore[index]
    with pytest.raises(ChatActionError) as invalid:
        manager.update_action(chat.chat_id, aid, state="waiting", params_update={"callback_used": "2"})
    assert invalid.value.reason == "invalid_param"


@pytest.mark.asyncio
async def test_a_new_sign_in_start_clears_the_single_use_mark(
    manager: ProjectChatManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed card keeps its mark; starting again must re-arm paste-back."""
    calls = _recorder(monkeypatch)
    authorize_url = (
        "https://mcp.notion.com/authorize?response_type=code"
        "&redirect_uri=http%3A%2F%2Flocalhost%3A62471%2Fcallback&state=S2"
    )

    class RestartFlow(ActionHandler):
        async def start(self, ctx: ActionContext, action: ChatAction, body: Mapping[str, object]) -> None:
            del body
            ctx.pcm.update_action(
                ctx.chat.chat_id, action.id, state="waiting", params_update={"url": authorize_url}
            )

    monkeypatch.setitem(chat_actions_runtime.MCP_AUTH_FLOWS, "claude", RestartFlow())
    project = manager.create_project("Restart", workspace="work")
    chat = manager.create_chat(project.project_id, title="Restart", provider="claude")
    aid = str(
        manager.raise_action(
            chat.chat_id, "mcp_auth", {"server": "notion", "provider": "claude"}, origin="engine"
        )["id"]
    )
    manager.update_action(
        chat.chat_id,
        aid,
        state="failed",
        params_update={"url": authorize_url, "callback_used": "1"},
        error=chat_actions_runtime.UNREACHABLE_ERROR,
    )
    runtime = make_runtime(manager, poll_deadline_s=600.0)

    await runtime.start(chat.chat_id, aid, {}, is_loopback=True)
    action = manager.get_action(chat.chat_id, aid)
    assert action is not None
    assert action.state == "waiting"
    assert action.params["callback_used"] == "0"

    await runtime.callback(chat.chat_id, aid, "http://localhost:62471/callback?code=abc&state=S2", is_loopback=True)
    assert len(calls) == 1
    action = manager.get_action(chat.chat_id, aid)
    assert action is not None
    assert action.params["callback_used"] == "1"
    for task in runtime._polls.values():
        task.cancel()


@pytest.mark.parametrize("status", [0, 400])
def test_a_late_replay_error_does_not_overwrite_a_done_card(
    manager: ProjectChatManager, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    cid, aid = _waiting_card(manager, 62471)

    async def finish_then_refuse(target: CallbackTarget) -> int:
        del target
        # The flow's own poll sees the exchange finish while the replay is in flight.
        manager.update_action(cid, aid, state="done")
        return status

    monkeypatch.setattr(chat_actions_runtime, "replay_callback", finish_then_refuse)
    response = _post(_client(manager), cid, aid, {"url": "http://localhost:62471/callback?code=a&state=S1"})
    assert response.status_code == 200
    assert response.json()["action"]["state"] == "done"
    action = manager.get_action(cid, aid)
    assert action is not None
    assert action.state == "done"


def test_a_waiting_card_without_an_authorize_url_is_invalid_state(
    manager: ProjectChatManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _recorder(monkeypatch)
    project = manager.create_project("No url", workspace="work")
    chat = manager.create_chat(project.project_id, title="No url", provider="claude")
    aid = str(
        manager.raise_action(
            chat.chat_id, "mcp_auth", {"server": "notion", "provider": "claude"}, origin="engine"
        )["id"]
    )
    manager.update_action(chat.chat_id, aid, state="waiting")
    response = _post(_client(manager), chat.chat_id, aid, {"url": "http://localhost:62471/callback?code=a&state=S1"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_state"
    assert calls == []


def test_a_second_paste_with_a_bad_state_is_already_used(
    manager: ProjectChatManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _recorder(monkeypatch)
    cid, aid = _waiting_card(manager, 62471)
    client = _client(manager)
    good = {"url": "http://localhost:62471/callback?code=abc&state=S1"}
    assert _post(client, cid, aid, good).status_code == 200
    second = _post(client, cid, aid, {"url": "http://localhost:62471/callback?code=abc&state=S9"})
    assert second.status_code == 400
    assert second.json()["error"] == "already_used"
    assert len(calls) == 1
