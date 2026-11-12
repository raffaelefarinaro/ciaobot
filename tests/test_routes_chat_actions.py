"""The browser routes for a chat's needs-you cards: start, dismiss and continue.

These are the real routes behind the real session gate, over a real manager, so a
test sees what the browser sees. Each POST carries the Origin header the PWA sends.
"""

from __future__ import annotations

import json
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

from ciao.chat_actions import action_key
from ciao.web.auth import SESSION_COOKIE, AuthMiddleware
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_chat_actions import (
    chat_action_continue,
    chat_action_dismiss,
    chat_action_start,
    chat_action_turn_off,
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
            Route("/api/chats/{chat_id}/actions/{action_id}/turn-off", chat_action_turn_off, methods=["POST"]),
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


def _isolate_opencode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG_CONTENT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


def _record_workspace_reloads(
    manager: ProjectChatManager, monkeypatch: pytest.MonkeyPatch, result: dict[str, list[str]]
) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    async def fake(workspace: str, provider: str) -> dict[str, list[str]]:
        calls.append((workspace, provider))
        return result

    monkeypatch.setattr(manager, "reload_workspace_providers", fake)
    return calls


def test_turn_off_writes_the_config_dismisses_and_reloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_opencode(tmp_path, monkeypatch)
    with _live_chat(tmp_path) as (manager, cid):
        root = Path(manager._agent_root_for_chat(cid))
        root.mkdir(parents=True, exist_ok=True)
        config = root / "opencode.json"
        config.write_text(json.dumps({"mcp": {"servers": {"linear": {"type": "remote", "url": "https://x.invalid"}}}}))
        calls = _record_workspace_reloads(manager, monkeypatch, {"reloaded": [cid], "pending": []})
        action = _raise(manager, cid)

        response = _client(manager).post(f"/api/chats/{cid}/actions/{action['id']}/turn-off", headers=_HEADERS)

        chat = manager.get_chat(cid)
        assert chat is not None
        assert action_key("mcp_auth", {"server": "linear"}) in chat.dismissed_action_keys
        assert calls == [(manager._workspace_for_chat(cid), "opencode")]

    assert response.status_code == 200
    assert response.json() == {"actions": [], "reload": {"reloaded": [cid], "pending": []}}
    assert json.loads(config.read_text())["mcp"]["servers"]["linear"]["disabled"] is True


def test_turn_off_reloads_the_chat_itself_when_its_workspace_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_opencode(tmp_path, monkeypatch)
    with _live_chat(tmp_path) as (manager, cid):
        root = Path(manager._agent_root_for_chat(cid))
        root.mkdir(parents=True, exist_ok=True)
        (root / "opencode.json").write_text(
            json.dumps({"mcp": {"servers": {"linear": {"type": "remote", "url": "https://x.invalid"}}}})
        )
        _record_workspace_reloads(manager, monkeypatch, {"reloaded": [], "pending": []})
        reloaded: list[str] = []

        async def fake_reload(chat_id: str) -> bool:
            reloaded.append(chat_id)
            return True

        monkeypatch.setattr(manager, "reload_provider", fake_reload)
        monkeypatch.setattr(manager, "active_chat_ids", lambda: [])
        action = _raise(manager, cid)

        response = _client(manager).post(f"/api/chats/{cid}/actions/{action['id']}/turn-off", headers=_HEADERS)

    assert response.status_code == 200
    assert response.json()["reload"] == {"reloaded": [cid], "pending": []}
    assert reloaded == [cid]


def test_turn_off_refusal_keeps_the_card(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_opencode(tmp_path, monkeypatch)
    with _live_chat(tmp_path) as (manager, cid):
        root = Path(manager._agent_root_for_chat(cid))
        root.mkdir(parents=True, exist_ok=True)
        (root / "opencode.jsonc").write_text(
            '// comment\n{"mcp": {"servers": {"linear": {"type": "remote", "url": "https://x.invalid"}}}}'
        )
        calls = _record_workspace_reloads(manager, monkeypatch, {"reloaded": [], "pending": []})
        action = _raise(manager, cid)

        response = _client(manager).post(f"/api/chats/{cid}/actions/{action['id']}/turn-off", headers=_HEADERS)

        assert manager.chat_action_payloads(cid) == [action]
        assert calls == []

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "config_has_comments"
    assert body["action"]["id"] == action["id"]


def test_turn_off_write_failure_is_400_unreadable_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_opencode(tmp_path, monkeypatch)
    with _live_chat(tmp_path) as (manager, cid):
        root = Path(manager._agent_root_for_chat(cid))
        root.mkdir(parents=True, exist_ok=True)
        config = root / "opencode.json"
        original = json.dumps({"mcp": {"servers": {"linear": {"type": "remote", "url": "https://x.invalid"}}}})
        config.write_text(original)
        calls = _record_workspace_reloads(manager, monkeypatch, {"reloaded": [], "pending": []})
        action = _raise(manager, cid)

        def refuse(*_args: object, **_kwargs: object) -> None:
            raise PermissionError("read-only")

        monkeypatch.setattr("ciao.web.mcp_toggle.os.replace", refuse)
        response = _client(manager).post(f"/api/chats/{cid}/actions/{action['id']}/turn-off", headers=_HEADERS)

        assert manager.chat_action_payloads(cid) == [action]
        assert calls == []
        assert config.read_text() == original

    assert response.status_code == 400
    assert response.json()["error"] == "unreadable_config"


def test_a_stale_turn_off_is_409(tmp_path: Path) -> None:
    with _live_chat(tmp_path) as (manager, cid):
        current = _raise(manager, cid)

        response = _client(manager).post(f"/api/chats/{cid}/actions/nope/turn-off", headers=_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"] == "chat_action_conflict"
    assert response.json()["actions"] == [current]


def test_turn_off_of_an_unknown_chat_is_404(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)

    response = _client(manager).post("/api/chats/ghost/actions/a/turn-off", headers=_HEADERS)

    assert response.status_code == 404
    assert response.json() == {"error": "not found"}


def test_turn_off_needs_a_session(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)

    response = _client(manager, signed_in=False).post(
        "/api/chats/c/actions/a/turn-off", headers=_HEADERS
    )

    assert response.status_code == 401


def test_turn_off_refuses_a_card_for_the_other_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_opencode(tmp_path, monkeypatch)
    with _live_chat(tmp_path) as (manager, cid):
        root = Path(manager._agent_root_for_chat(cid))
        root.mkdir(parents=True, exist_ok=True)
        config = root / "opencode.json"
        original = json.dumps({"mcp": {"servers": {"linear": {"type": "remote", "url": "https://x.invalid"}}}})
        config.write_text(original)
        calls = _record_workspace_reloads(manager, monkeypatch, {"reloaded": [cid], "pending": []})
        action = _raise(manager, cid, provider="claude")

        response = _client(manager).post(f"/api/chats/{cid}/actions/{action['id']}/turn-off", headers=_HEADERS)

        assert manager.chat_action_payloads(cid) == [action]
        assert calls == []
        assert config.read_text() == original

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "invalid_state"
    assert body["action"]["id"] == action["id"]
