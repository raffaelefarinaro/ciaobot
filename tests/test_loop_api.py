"""API tests for the loops endpoints (create / update / run-now / delete)."""

from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.loops import LoopManager, LoopStore
from ciao.web.routes_api import create_loop, list_loops, loop_detail, run_loop_now


class _Chat:
    def __init__(self, title: str, *, archived: bool = False) -> None:
        self.title = title
        self.archived = archived


class _NewChat:
    def __init__(self, chat_id: str) -> None:
        self.chat_id = chat_id


class _ProjectChats:
    """Stub PCM: knows an idle, a busy, and two archived chats."""

    def __init__(self) -> None:
        self.chats = {
            "chat-idle": _Chat("Idle chat"),
            "chat-busy": _Chat("Busy chat"),
            "chat-archived": _Chat("Archived chat", archived=True),
            "chat-archived-broken": _Chat("Broken archive", archived=True),
        }
        self._continued = 0

    def get_chat(self, chat_id: str):
        return self.chats.get(chat_id)

    def chat_stream_active(self, chat_id: str) -> bool:
        return chat_id == "chat-busy"

    def continue_archived_chat(self, chat_id: str):
        if chat_id == "chat-archived-broken":
            raise ValueError("No message history found in transcript")
        self._continued += 1
        new_id = f"chat-continued-{self._continued}"
        self.chats[new_id] = _Chat("Continued chat")
        return _NewChat(new_id)


def _make_client(tmp_path: Path) -> tuple[TestClient, LoopManager]:
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    pcm = _ProjectChats()

    async def dispatch(entry):
        return {"status": "ok", "chat_id": entry.web_chat_id}

    manager = LoopManager(
        store=LoopStore(runtime),
        dispatch=dispatch,
        chat_busy=pcm.chat_stream_active,
        chat_exists=lambda chat_id: pcm.get_chat(chat_id) is not None,
    )
    app = Starlette(
        routes=[
            Route("/api/loops", list_loops, methods=["GET"]),
            Route("/api/loops", create_loop, methods=["POST"]),
            Route("/api/loop-run/{loop_id}", run_loop_now, methods=["POST"]),
            Route("/api/loops/{loop_id}", loop_detail, methods=["PATCH", "DELETE"]),
        ]
    )
    app.state.loop_manager = manager
    app.state.project_chat_manager = pcm
    return TestClient(app), manager


def test_create_requires_prompt_and_valid_chat(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    assert client.post("/api/loops", json={"web_chat_id": "chat-idle"}).status_code == 400
    assert client.post("/api/loops", json={"prompt": "p", "web_chat_id": "chat-nope"}).status_code == 400
    assert client.post(
        "/api/loops", json={"prompt": "p", "web_chat_id": "chat-idle", "interval_minutes": 0}
    ).status_code == 400
    assert client.post(
        "/api/loops", json={"prompt": "p", "web_chat_id": "chat-idle", "interval_minutes": "x"}
    ).status_code == 400


def test_create_and_list(tmp_path: Path) -> None:
    client, manager = _make_client(tmp_path)
    resp = client.post(
        "/api/loops",
        json={
            "prompt": "check PRs",
            "web_chat_id": "chat-idle",
            "interval_minutes": 5,
            "title": "PR watcher",
            "autostart": True,
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["loop_id"].startswith("loop-")
    assert body["interval_minutes"] == 5
    assert body["autostart"] is True
    assert body["running"] is False  # created without "start"
    assert body["context_label"] == "Idle chat"

    listed = client.get("/api/loops").json()
    assert [item["loop_id"] for item in listed] == [body["loop_id"]]


def test_create_with_start_marks_running(tmp_path: Path) -> None:
    client, manager = _make_client(tmp_path)
    resp = client.post(
        "/api/loops",
        json={"prompt": "p", "web_chat_id": "chat-idle", "start": True},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["running"] is True
    assert manager.is_running(body["loop_id"])
    # Running but never fired: next_run is "now-ish", not null.
    assert body["next_run"]


def test_patch_updates_and_toggles_running(tmp_path: Path) -> None:
    client, manager = _make_client(tmp_path)
    loop_id = client.post(
        "/api/loops", json={"prompt": "p", "web_chat_id": "chat-idle"}
    ).json()["loop_id"]

    resp = client.patch(
        f"/api/loops/{loop_id}",
        json={"prompt": "new prompt", "interval_minutes": 3, "autostart": True, "running": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["prompt"] == "new prompt"
    assert body["interval_minutes"] == 3
    assert body["autostart"] is True
    assert body["running"] is True

    resp = client.patch(f"/api/loops/{loop_id}", json={"running": False})
    assert resp.json()["running"] is False
    assert not manager.is_running(loop_id)

    assert client.patch(f"/api/loops/{loop_id}", json={"interval_minutes": 0}).status_code == 400
    assert client.patch(f"/api/loops/{loop_id}", json={"web_chat_id": "chat-nope"}).status_code == 400
    assert client.patch("/api/loops/loop-nope", json={"prompt": "x"}).status_code == 404


def test_start_on_archived_chat_forks_new_chat(tmp_path: Path) -> None:
    client, manager = _make_client(tmp_path)
    loop_id = client.post(
        "/api/loops", json={"prompt": "p", "web_chat_id": "chat-archived"}
    ).json()["loop_id"]

    resp = client.patch(f"/api/loops/{loop_id}", json={"running": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["running"] is True
    assert body["web_chat_id"] == "chat-continued-1"
    assert manager.is_running(loop_id)
    assert manager.get(loop_id).web_chat_id == "chat-continued-1"


def test_start_on_archived_chat_with_unreadable_transcript_errors(tmp_path: Path) -> None:
    client, manager = _make_client(tmp_path)
    loop_id = client.post(
        "/api/loops", json={"prompt": "p", "web_chat_id": "chat-archived-broken"}
    ).json()["loop_id"]

    resp = client.patch(f"/api/loops/{loop_id}", json={"running": True})
    assert resp.status_code == 409
    assert "error" in resp.json()
    assert not manager.is_running(loop_id)
    assert manager.get(loop_id).web_chat_id == "chat-archived-broken"


def test_run_now_and_busy_conflict(tmp_path: Path) -> None:
    client, _ = _make_client(tmp_path)
    idle = client.post(
        "/api/loops", json={"prompt": "p", "web_chat_id": "chat-idle"}
    ).json()["loop_id"]
    busy = client.post(
        "/api/loops", json={"prompt": "p", "web_chat_id": "chat-busy"}
    ).json()["loop_id"]

    resp = client.post(f"/api/loop-run/{idle}")
    assert resp.status_code == 201
    assert resp.json()["status"] == "started"

    resp = client.post(f"/api/loop-run/{busy}")
    assert resp.status_code == 409

    assert client.post("/api/loop-run/loop-nope").status_code == 404


def test_delete_stops_and_removes(tmp_path: Path) -> None:
    client, manager = _make_client(tmp_path)
    loop_id = client.post(
        "/api/loops", json={"prompt": "p", "web_chat_id": "chat-idle", "start": True}
    ).json()["loop_id"]
    assert manager.is_running(loop_id)
    assert client.delete(f"/api/loops/{loop_id}").json() == {"ok": True}
    assert not manager.is_running(loop_id)
    assert client.get("/api/loops").json() == []
    assert client.delete(f"/api/loops/{loop_id}").json() == {"ok": False}
