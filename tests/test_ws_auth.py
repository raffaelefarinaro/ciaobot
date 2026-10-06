from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from itsdangerous import URLSafeTimedSerializer
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ciao.web.auth import SESSION_COOKIE
from ciao.web.routes_chat import ws_events


class _IdleSubscription:
    def close(self) -> None:
        pass

    async def __aiter__(self):
        await asyncio.Event().wait()
        yield  # pragma: no cover


def _events_app() -> Starlette:
    app = Starlette(routes=[WebSocketRoute("/ws/events", ws_events)])
    app.state.serializer = URLSafeTimedSerializer("test-secret")
    app.state.config = SimpleNamespace()
    app.state.project_chat_manager = SimpleNamespace(
        active_stream_chat_ids=lambda: [],
        get_chat=lambda _cid: None,
        background_agent_counts={},
        background_runs={},
        chat_pin_states={},
        events=SimpleNamespace(attach=_IdleSubscription),
    )
    return app


def _signed_client(app: Starlette) -> TestClient:
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, app.state.serializer.dumps({"user": "owner"}))
    return client


def test_ws_requires_session() -> None:
    client = TestClient(_events_app())
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/events"):
            pass


def test_ws_accepts_session_cookie() -> None:
    app = _events_app()
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, app.state.serializer.dumps({"user": "owner"}))
    with client.websocket_connect("/ws/events") as ws:
        assert ws.receive_json()["type"] == "snapshot"


def test_ws_rejects_cross_origin() -> None:
    client = TestClient(_events_app())
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/ws/events", headers={"Origin": "http://evil.example"}
        ):
            pass


def test_event_published_while_the_snapshot_is_built_reaches_the_client() -> None:
    # A `chat_streaming_done` in the gap between the snapshot and the
    # subscription was lost, leaving the client's spinner on forever.
    from ciao.web.chat_broker import EventsHub

    hub = EventsHub()

    def _ids() -> list[str]:
        hub.publish({"type": "chat_streaming_done", "chat_id": "c1"})
        return []

    app = _events_app()
    app.state.project_chat_manager = SimpleNamespace(
        active_stream_chat_ids=_ids,
        get_chat=lambda _cid: None,
        background_agent_counts={},
        background_runs={},
        chat_pin_states={},
        events=hub,
    )
    with _signed_client(app).websocket_connect(
        "/ws/events", headers={"Origin": "http://testserver"}
    ) as ws:
        assert ws.receive_json()["type"] == "snapshot"
        assert ws.receive_json() == {"type": "chat_streaming_done", "chat_id": "c1"}


def test_snapshot_failure_detaches_the_events_subscription() -> None:
    from ciao.web.chat_broker import EventsHub

    hub = EventsHub()

    def _boom() -> list[str]:
        raise KeyError("boom")

    app = _events_app()
    app.state.project_chat_manager = SimpleNamespace(
        active_stream_chat_ids=_boom,
        get_chat=lambda _cid: None,
        background_agent_counts={},
        background_runs={},
        chat_pin_states={},
        events=hub,
    )
    with pytest.raises(KeyError):
        with _signed_client(app).websocket_connect(
            "/ws/events", headers={"Origin": "http://testserver"}
        ):
            pass  # pragma: no cover
    assert hub.subscriber_count == 0


def _real_events_app(tmp_path: Path) -> Starlette:
    """A `/ws/events` app over a real manager, so the snapshot is real state.

    The auth/origin tests above use a `SimpleNamespace` stub with empty maps.
    That cannot answer what the snapshot actually carries, and a stub that
    quietly grows a field is how a required snapshot key goes missing without
    any test noticing.
    """
    from ciao.config import CiaoConfig
    from ciao.sessions import StateStore
    from ciao.transcripts import TranscriptStore
    from ciao.web.project_chats import ProjectChatManager

    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    manager = ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )
    app = Starlette(routes=[WebSocketRoute("/ws/events", ws_events)])
    app.state.serializer = URLSafeTimedSerializer("test-secret")
    app.state.config = config
    app.state.project_chat_manager = manager
    return app


def test_events_snapshot_contains_closed_and_open_chat_pins(tmp_path: Path) -> None:
    """The snapshot lists every chat, unpinned ones included.

    A reconnecting client has to be able to close a pin the engine no longer
    holds. It can only do that for a chat the map mentions, so a chat with an
    empty pin still has to appear — otherwise "absent" silently means "keep
    whatever you had", and a stale panel survives every reconnect.
    """
    app = _real_events_app(tmp_path)
    manager = app.state.project_chat_manager
    project = manager.create_project("Pins", workspace="work")
    pinned = manager.create_chat(project.project_id, title="Pinned")
    closed = manager.create_chat(project.project_id, title="Closed")
    manager.set_chat_pin(pinned.chat_id, "/tmp/some/file.md", expected_revision=0)

    with _signed_client(app).websocket_connect(
        "/ws/events", headers={"Origin": "http://testserver"}
    ) as ws:
        snapshot = ws.receive_json()

    assert snapshot["type"] == "snapshot"
    states = snapshot["chat_pins"]
    assert states[pinned.chat_id] == {
        "path": "/tmp/some/file.md",
        "dismissed_paths": [],
        "revision": 1,
    }
    assert states[closed.chat_id] == {"path": "", "dismissed_paths": [], "revision": 0}


def test_pin_event_is_delivered_to_two_subscribers(tmp_path: Path) -> None:
    """Two connected devices see the same persisted mutation.

    The event is what makes a pin cross-device at all, and it has to carry the
    complete payload (path, dismissals, revision) rather than a delta: a
    subscriber that missed the previous event cannot apply a delta, and the
    revision is what its next manual write has to quote.
    """
    app = _real_events_app(tmp_path)
    manager = app.state.project_chat_manager
    project = manager.create_project("Pins", workspace="work")
    chat = manager.create_chat(project.project_id, title="Pinned")

    client = _signed_client(app)
    with client.websocket_connect(
        "/ws/events", headers={"Origin": "http://testserver"}
    ) as first:
        assert first.receive_json()["type"] == "snapshot"
        with client.websocket_connect(
            "/ws/events", headers={"Origin": "http://testserver"}
        ) as second:
            assert second.receive_json()["type"] == "snapshot"
            manager.set_chat_pin(
                chat.chat_id, "/tmp/shared/file.md", expected_revision=0
            )
            expected = {
                "type": "chat_pin_changed",
                "chat_id": chat.chat_id,
                "path": "/tmp/shared/file.md",
                "dismissed_paths": [],
                "revision": 1,
            }
            assert first.receive_json() == expected
            assert second.receive_json() == expected
