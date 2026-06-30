"""Route-level coverage for the file snapshot endpoints.

Exercises the full PWA contract: list snapshots, read one snapshot, restore
one snapshot, write a user edit back, and verify the restore captures a new
linear entry rather than rewriting history.
"""

from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import (
    file_content,
    file_history,
    file_restore,
    workspace_file,
    workspace_file_write,
)


def _make_manager(tmp_path: Path) -> tuple[ProjectChatManager, CiaoConfig]:
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    # SnapshotStore is rooted under config.state_path.parent (i.e. the tmp
    # runtime dir set above), so no extra wiring needed here.
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    pcm = ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )
    return pcm, config


def _make_client(pcm: ProjectChatManager, config: CiaoConfig) -> TestClient:
    app = Starlette(routes=[
        Route("/api/file-history", file_history, methods=["GET"]),
        Route("/api/file-content", file_content, methods=["GET"]),
        Route("/api/file-restore", file_restore, methods=["POST"]),
        Route("/api/workspace-file", workspace_file, methods=["GET"]),
        Route("/api/workspace-file", workspace_file_write, methods=["POST"]),
    ])
    app.state.project_chat_manager = pcm
    app.state.config = config
    return TestClient(app)


def _make_chat(pcm: ProjectChatManager) -> str:
    proj = pcm.create_project(name="P", workspace="personal")
    chat = pcm.create_chat(project_id=proj.project_id)
    return chat.chat_id


def test_history_lists_snapshots(tmp_path: Path) -> None:
    pcm, config = _make_manager(tmp_path)
    client = _make_client(pcm, config)
    chat_id = _make_chat(pcm)

    target = tmp_path / "note.md"
    target.write_text("v1")
    import asyncio
    asyncio.run(pcm.snapshots.capture(chat_id=chat_id, file_path=str(target), action="written", tool="Write"))
    target.write_text("v2")
    asyncio.run(pcm.snapshots.capture(chat_id=chat_id, file_path=str(target), action="edited", tool="Edit"))

    r = client.get("/api/file-history", params={"chat_id": chat_id, "file_path": str(target)})
    assert r.status_code == 200, r.text
    snapshots = r.json()["snapshots"]
    assert [s["seq"] for s in snapshots] == [1, 2]
    assert snapshots[0]["action"] == "written"
    assert snapshots[1]["action"] == "edited"


def test_content_returns_snapshot_text(tmp_path: Path) -> None:
    pcm, config = _make_manager(tmp_path)
    client = _make_client(pcm, config)
    chat_id = _make_chat(pcm)

    target = tmp_path / "n.md"
    target.write_text("hello")
    import asyncio
    asyncio.run(pcm.snapshots.capture(chat_id=chat_id, file_path=str(target), action="written", tool="Write"))

    r = client.get(
        "/api/file-content",
        params={"chat_id": chat_id, "file_path": str(target), "seq": 1},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["content"] == "hello"
    assert body["meta"]["seq"] == 1


def test_missing_snapshot_404s(tmp_path: Path) -> None:
    pcm, config = _make_manager(tmp_path)
    client = _make_client(pcm, config)
    chat_id = _make_chat(pcm)
    r = client.get(
        "/api/file-content",
        params={"chat_id": chat_id, "file_path": "/nope.md", "seq": 99},
    )
    assert r.status_code == 404


def test_history_requires_known_chat(tmp_path: Path) -> None:
    pcm, config = _make_manager(tmp_path)
    client = _make_client(pcm, config)
    r = client.get(
        "/api/file-history",
        params={"chat_id": "chat-bogus", "file_path": "/x.md"},
    )
    assert r.status_code == 404


def test_restore_writes_back_and_appends_new_snapshot(tmp_path: Path) -> None:
    pcm, config = _make_manager(tmp_path)
    client = _make_client(pcm, config)
    chat_id = _make_chat(pcm)

    target = tmp_path / "n.md"
    target.write_text("first")
    import asyncio
    asyncio.run(pcm.snapshots.capture(chat_id=chat_id, file_path=str(target), action="written", tool="Write"))
    target.write_text("second")
    asyncio.run(pcm.snapshots.capture(chat_id=chat_id, file_path=str(target), action="edited", tool="Edit"))

    r = client.post(
        "/api/file-restore",
        json={"chat_id": chat_id, "file_path": str(target), "seq": 1},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"]
    assert body["restored_seq"] == 1
    assert body["new_seq"] == 3  # linear history: capture-after-restore appends seq 3

    # Disk now matches the restored content.
    assert target.read_text() == "first"

    # History grew by one entry tagged action="restored".
    r = client.get("/api/file-history", params={"chat_id": chat_id, "file_path": str(target)})
    snapshots = r.json()["snapshots"]
    assert [s["seq"] for s in snapshots] == [1, 2, 3]
    assert snapshots[-1]["action"] == "restored"


def test_restore_rejects_path_outside_sandbox(tmp_path: Path) -> None:
    pcm, config = _make_manager(tmp_path)
    client = _make_client(pcm, config)
    chat_id = _make_chat(pcm)

    # Create a snapshot for a path the agent claimed but that's outside
    # the workspace root: the store records it, the route refuses to write.
    outside = Path("/tmp/should-not-be-touched.md").resolve()
    pcm.snapshots._base.joinpath(chat_id).mkdir(parents=True, exist_ok=True)
    # Inject a fake snapshot using the store API on a content we never wrote
    # to disk. Use a path that survives sandbox check (use tmp_path) but
    # then forge a request targeting `outside`.
    inside = tmp_path / "inside.md"
    inside.write_text("safe")
    import asyncio
    asyncio.run(pcm.snapshots.capture(chat_id=chat_id, file_path=str(inside), action="written", tool="Write"))

    # Route refuses an absolute path outside the workspace root.
    r = client.post(
        "/api/file-restore",
        json={"chat_id": chat_id, "file_path": str(outside), "seq": 1},
    )
    # Either 403 (sandbox) or 404 (no snapshot under that path) is acceptable
    # — both indicate the write did not happen. Asserting the disk path
    # remains absent is the real safety check.
    assert r.status_code in (403, 404), r.text
    assert not outside.exists()


def test_pwa_edit_writes_back_and_snapshots(tmp_path: Path) -> None:
    pcm, config = _make_manager(tmp_path)
    client = _make_client(pcm, config)
    chat_id = _make_chat(pcm)

    target = tmp_path / "new.md"
    body = {"chat_id": chat_id, "path": str(target), "content": "from PWA editor"}
    r = client.post("/api/workspace-file", json=body)
    assert r.status_code == 200, r.text
    assert target.read_text() == "from PWA editor"
    snap = r.json().get("snapshot")
    assert snap is not None
    assert snap["action"] == "edited"
    assert snap["tool"] == "PWAEdit"


def test_pwa_edit_rejects_outside_sandbox(tmp_path: Path) -> None:
    pcm, config = _make_manager(tmp_path)
    client = _make_client(pcm, config)
    chat_id = _make_chat(pcm)
    r = client.post(
        "/api/workspace-file",
        json={"chat_id": chat_id, "path": "/etc/should-not-be-written.txt", "content": "x"},
    )
    assert r.status_code in (403, 415), r.text


def test_pwa_edit_rejects_fuzzy_match_on_write(tmp_path: Path) -> None:
    pcm, config = _make_manager(tmp_path)
    client = _make_client(pcm, config)
    chat_id = _make_chat(pcm)
    
    # Create an existing file
    existing = tmp_path / "existing.md"
    existing.write_text("original content")
    
    # Try to write to a close filename (fuzzy match candidate)
    fuzzy_path = tmp_path / "existin.md"
    
    body = {"chat_id": chat_id, "path": str(fuzzy_path), "content": "mutated content"}
    r = client.post("/api/workspace-file", json=body)
    
    assert r.status_code == 200, r.text
    assert fuzzy_path.read_text() == "mutated content"
    assert existing.read_text() == "original content"
