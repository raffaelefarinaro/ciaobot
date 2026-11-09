"""The browser routes for a chat's needs-you cards: start, dismiss and continue.

These are the real routes behind the real session gate, over a real manager, so a
test sees what the browser sees. Each POST carries the Origin header the PWA sends.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web.auth import SESSION_COOKIE, AuthMiddleware
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_chat_actions import (
    chat_action_continue,
    chat_action_dismiss,
    chat_action_start,
    chat_mcp_auth_request,
)
from tests.test_ws_auth import _real_events_app, _signed_client
from tests.test_chat_actions_runtime import (
    FakeClient,
    live_opencode,
    make_manager,
    make_runtime,
    opencode_chat,
    sign_in_routes,
)

_ORIGIN = "https://ciao.example"
_HEADERS = {"Origin": _ORIGIN}


def _client(manager: ProjectChatManager, *, signed_in: bool = True) -> TestClient:
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[
            Route("/api/chats/{chat_id}/actions/{action_id}/start", chat_action_start, methods=["POST"]),
            Route("/api/chats/{chat_id}/actions/{action_id}/dismiss", chat_action_dismiss, methods=["POST"]),
            Route("/api/chats/{chat_id}/actions/{action_id}/continue", chat_action_continue, methods=["POST"]),
            Route("/api/chats/{chat_id}/actions/mcp-auth", chat_mcp_auth_request, methods=["POST"]),
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


def _raise(manager: ProjectChatManager, chat_id: str, provider: str = "opencode") -> dict[str, Any]:
    payload = manager.raise_action(
        chat_id,
        "mcp_auth",
        {"server": "linear", "provider": provider},
        origin="engine",
    )
    assert payload is not None
    return cast(dict[str, Any], payload)


@contextmanager
def _live_chat(tmp_path: Path, client: FakeClient | None = None) -> Iterator[tuple[ProjectChatManager, str]]:
    manager = make_manager(tmp_path)
    chat = opencode_chat(manager, live_opencode(tmp_path, client or FakeClient(sign_in_routes())))
    make_runtime(manager, poll_deadline_s=600.0)
    yield manager, chat.chat_id
    for task in list(manager.actions_runtime._polls.values()):
        task.cancel()


def test_start_returns_the_waiting_action(tmp_path: Path) -> None:
    with _live_chat(tmp_path) as (manager, cid):
        action = _raise(manager, cid)

        response = _client(manager).post(
            f"/api/chats/{cid}/actions/{action['id']}/start", headers=_HEADERS, json={}
        )

    assert response.status_code == 200
    assert response.json()["action"]["state"] == "waiting"


def test_start_of_an_unknown_chat_is_404(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)

    response = _client(manager).post(
        "/api/chats/no-such-chat/actions/x/start", headers=_HEADERS, json={}
    )

    assert response.status_code == 404
    assert response.json() == {"error": "not found"}


def test_start_of_an_unknown_action_is_a_conflict_with_the_current_list(tmp_path: Path) -> None:
    with _live_chat(tmp_path) as (manager, cid):
        current = _raise(manager, cid)

        response = _client(manager).post(
            f"/api/chats/{cid}/actions/stale-id/start", headers=_HEADERS, json={}
        )

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "chat_action_conflict"
    assert body["actions"] == [current]


def test_a_start_body_that_is_not_an_object_is_400(tmp_path: Path) -> None:
    with _live_chat(tmp_path) as (manager, cid):
        action = _raise(manager, cid)

        response = _client(manager).post(
            f"/api/chats/{cid}/actions/{action['id']}/start",
            headers={**_HEADERS, "Content-Type": "application/json"},
            content=b"[]",
        )

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_body", "action": None}


def test_dismiss_returns_the_remaining_actions(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    project = manager.create_project("Dismiss", workspace="work")
    chat = manager.create_chat(project.project_id, title="Dismiss", provider="opencode")
    action = _raise(manager, chat.chat_id)

    response = _client(manager).post(
        f"/api/chats/{chat.chat_id}/actions/{action['id']}/dismiss", headers=_HEADERS
    )

    assert response.status_code == 200
    assert response.json() == {"actions": []}


def test_a_stale_dismissal_is_409(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    project = manager.create_project("Stale", workspace="work")
    chat = manager.create_chat(project.project_id, title="Stale", provider="opencode")
    current = _raise(manager, chat.chat_id)

    response = _client(manager).post(
        f"/api/chats/{chat.chat_id}/actions/stale-id/dismiss", headers=_HEADERS
    )

    assert response.status_code == 409
    assert response.json()["error"] == "chat_action_conflict"
    assert response.json()["actions"] == [current]


def test_continue_before_done_is_400_invalid_state(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    project = manager.create_project("Early", workspace="work")
    chat = manager.create_chat(project.project_id, title="Early", provider="opencode")
    action = _raise(manager, chat.chat_id)

    response = _client(manager).post(
        f"/api/chats/{chat.chat_id}/actions/{action['id']}/continue", headers=_HEADERS
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_state"
    assert body["action"]["state"] == "open"


def test_continue_delivers_the_wake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = make_manager(tmp_path)
    project = manager.create_project("Wake", workspace="work")
    chat = manager.create_chat(project.project_id, title="Wake", provider="opencode")
    action = _raise(manager, chat.chat_id)
    manager.update_action(chat.chat_id, action["id"], state="done")
    monkeypatch.setattr(manager, "_deliver_wake", lambda *a, **k: "started")

    response = _client(manager).post(
        f"/api/chats/{chat.chat_id}/actions/{action['id']}/continue", headers=_HEADERS
    )

    assert response.status_code == 200
    assert response.json() == {"actions": [], "delivery": "started"}


def test_the_routes_need_a_session(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)

    response = _client(manager, signed_in=False).post(
        "/api/chats/c/actions/a/start", headers=_HEADERS, json={}
    )

    assert response.status_code == 401


def test_the_events_snapshot_carries_chat_actions(tmp_path: Path) -> None:
    app = _real_events_app(tmp_path)
    manager = app.state.project_chat_manager
    project = manager.create_project("Snapshot", workspace="work")
    chat = manager.create_chat(project.project_id, title="Snapshot", provider="opencode")
    payload = _raise(manager, chat.chat_id)

    with _signed_client(app).websocket_connect(
        "/ws/events", headers={"Origin": "http://testserver"}
    ) as ws:
        snapshot = ws.receive_json()
        assert snapshot["chat_actions"] == {chat.chat_id: [payload]}
        manager.dismiss_action(chat.chat_id, payload["id"])
        assert ws.receive_json() == {
            "type": "chat_actions_changed",
            "chat_id": chat.chat_id,
            "actions": [],
        }


def test_continue_of_an_unknown_chat_is_404(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)

    response = _client(manager).post(
        "/api/chats/no-such-chat/actions/x/continue", headers=_HEADERS
    )

    assert response.status_code == 404
    assert response.json() == {"error": "not found"}


def test_dismiss_of_an_unknown_chat_is_404(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)

    response = _client(manager).post(
        "/api/chats/no-such-chat/actions/x/dismiss", headers=_HEADERS
    )

    assert response.status_code == 404
    assert response.json() == {"error": "not found"}


def test_a_stale_continue_is_409_with_the_current_list(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    project = manager.create_project("Stale continue", workspace="work")
    chat = manager.create_chat(project.project_id, title="Stale continue", provider="opencode")
    current = _raise(manager, chat.chat_id)

    response = _client(manager).post(
        f"/api/chats/{chat.chat_id}/actions/stale-id/continue", headers=_HEADERS
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "chat_action_conflict"
    assert body["actions"] == [current]


def test_mcp_auth_request_raises_the_chat_card(tmp_path: Path) -> None:
    with _live_chat(tmp_path) as (manager, cid):
        response = _client(manager).post(
            f"/api/chats/{cid}/actions/mcp-auth", headers=_HEADERS, json={"server": "linear"}
        )

        body = response.json()
        assert response.status_code == 200
        assert body["action"]["state"] == "open"
        assert body["action"]["params"] == {"server": "linear", "provider": "opencode"}
        assert manager.chat_action_payloads(cid) == [body["action"]]


def test_mcp_auth_request_replaces_a_done_card(tmp_path: Path) -> None:
    with _live_chat(tmp_path) as (manager, cid):
        old = _raise(manager, cid)
        manager.update_action(cid, old["id"], state="done")

        response = _client(manager).post(
            f"/api/chats/{cid}/actions/mcp-auth", headers=_HEADERS, json={"server": "linear"}
        )

        body = response.json()
        assert response.status_code == 200
        assert body["action"]["id"] != old["id"]
        assert body["action"]["state"] == "open"
        assert manager.chat_action_payloads(cid) == [body["action"]]


def test_mcp_auth_request_of_an_unknown_chat_is_404(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)

    response = _client(manager).post(
        "/api/chats/no-such-chat/actions/mcp-auth", headers=_HEADERS, json={"server": "linear"}
    )

    assert response.status_code == 404
    assert response.json() == {"error": "not found"}


def test_mcp_auth_request_needs_exactly_a_server(tmp_path: Path) -> None:
    with _live_chat(tmp_path) as (manager, cid):
        client = _client(manager)
        url = f"/api/chats/{cid}/actions/mcp-auth"
        bodies = [
            {"server": 1},
            {"server": "linear", "url": "https://x.example"},
            {},
        ]
        responses = [client.post(url, headers=_HEADERS, json=body) for body in bodies]
        responses.append(
            client.post(url, headers={**_HEADERS, "Content-Type": "application/json"}, content=b"[]")
        )

        assert [r.status_code for r in responses] == [400] * len(responses)
        assert all(r.json() == {"error": "invalid_body", "action": None} for r in responses)
        assert manager.chat_action_payloads(cid) == []


def test_mcp_auth_request_in_an_archived_chat_is_400(tmp_path: Path) -> None:
    with _live_chat(tmp_path) as (manager, cid):
        chat = manager.get_chat(cid)
        assert chat is not None
        chat.archived = True

        response = _client(manager).post(
            f"/api/chats/{cid}/actions/mcp-auth", headers=_HEADERS, json={"server": "linear"}
        )

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_state", "action": None}


def test_mcp_auth_request_needs_a_session(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)

    response = _client(manager, signed_in=False).post(
        "/api/chats/c/actions/mcp-auth", headers=_HEADERS, json={"server": "linear"}
    )

    assert response.status_code == 401
