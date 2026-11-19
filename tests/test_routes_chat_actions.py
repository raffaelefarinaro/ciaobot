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
)
from tests.test_chat_actions_runtime import make_manager, make_runtime
from tests.test_pwa_api_docs import _parse_routes
from tests.test_ws_auth import _real_events_app, _signed_client

_ORIGIN = "https://ciao.example"
_HEADERS = {"Origin": _ORIGIN}


def _client(manager: ProjectChatManager, *, signed_in: bool = True) -> TestClient:
    serializer = URLSafeTimedSerializer("test-secret")
    app = Starlette(
        routes=[
            Route("/api/chats/{chat_id}/actions/{action_id}/start", chat_action_start, methods=["POST"]),
            Route("/api/chats/{chat_id}/actions/{action_id}/dismiss", chat_action_dismiss, methods=["POST"]),
            Route("/api/chats/{chat_id}/actions/{action_id}/continue", chat_action_continue, methods=["POST"]),
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


def _raise(manager: ProjectChatManager, chat_id: str, kind: str = "restart") -> dict[str, Any]:
    params = {"mode": "auto"} if kind == "permission_mode" else {"reason": "config"}
    payload = manager.raise_action(chat_id, kind, params, origin="agent")
    assert payload is not None
    return cast(dict[str, Any], payload)


@contextmanager
def _live_chat(tmp_path: Path) -> Iterator[tuple[ProjectChatManager, str]]:
    manager = make_manager(tmp_path)
    project = manager.create_project("Actions", workspace="work")
    chat = manager.create_chat(project.project_id, title="Actions")
    make_runtime(manager, poll_deadline_s=600.0)
    yield manager, chat.chat_id
    for task in list(manager.actions_runtime._polls.values()):
        task.cancel()


def test_start_returns_the_started_action(tmp_path: Path) -> None:
    with _live_chat(tmp_path) as (manager, cid):
        action = _raise(manager, cid, "permission_mode")

        response = _client(manager).post(
            f"/api/chats/{cid}/actions/{action['id']}/start", headers=_HEADERS, json={}
        )

    assert response.status_code == 200
    assert response.json()["action"]["state"] == "done"


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
    chat = manager.create_chat(project.project_id, title="Dismiss")
    action = _raise(manager, chat.chat_id)

    response = _client(manager).post(
        f"/api/chats/{chat.chat_id}/actions/{action['id']}/dismiss", headers=_HEADERS
    )

    assert response.status_code == 200
    assert response.json() == {"actions": []}


def test_a_stale_dismissal_is_409(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    project = manager.create_project("Stale", workspace="work")
    chat = manager.create_chat(project.project_id, title="Stale")
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
    chat = manager.create_chat(project.project_id, title="Early")
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
    chat = manager.create_chat(project.project_id, title="Wake")
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
    chat = manager.create_chat(project.project_id, title="Snapshot")
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
    chat = manager.create_chat(project.project_id, title="Stale continue")
    current = _raise(manager, chat.chat_id)

    response = _client(manager).post(
        f"/api/chats/{chat.chat_id}/actions/stale-id/continue", headers=_HEADERS
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "chat_action_conflict"
    assert body["actions"] == [current]


def test_removed_mcp_routes_are_gone() -> None:
    """The MCP sign-in and turn-off routes are not in the app's route table, and have no handler."""
    from ciao.web import routes_chat_actions

    app_source = (Path(__file__).resolve().parents[1] / "ciao" / "web" / "app.py").read_text(encoding="utf-8")
    paths = {path for path, _methods in _parse_routes(app_source)}

    assert "/api/chats/{chat_id}/actions/mcp-auth" not in paths
    assert "/api/chats/{chat_id}/actions/{action_id}/turn-off" not in paths
    assert not hasattr(routes_chat_actions, "chat_mcp_auth_request")
    assert not hasattr(routes_chat_actions, "chat_action_turn_off")
