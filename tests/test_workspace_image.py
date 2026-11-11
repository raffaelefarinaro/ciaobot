"""Tests for the /api/workspace-image endpoint.

Mirror of ``test_workspace_file`` for the image variant: no workspace
sandbox (any file on disk is served), the allowed extensions are image
types, and the media type is derived from the filename. These tests spin up
a minimal Starlette app around the handler with a tmp workspace.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web.routes_api import workspace_image

_PNG_BYTES = bytes.fromhex(
    # 1x1 transparent PNG
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6300010000000500010d0a2db40000000049454e44"
    "ae426082"
)


@dataclass
class _FakeConfig:
    workspace_root: Path


def _make_client(workspace_root: Path) -> TestClient:
    app = Starlette(routes=[Route("/api/workspace-image", workspace_image, methods=["GET"])])
    app.state.config = _FakeConfig(workspace_root=workspace_root)
    return TestClient(app)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "docs").mkdir()
    (ws / "docs" / "images").mkdir()
    (ws / "docs" / "images" / "pic.png").write_bytes(_PNG_BYTES)
    (ws / "docs" / "images" / "diagram.svg").write_text(
        "<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8"
    )
    (ws / "docs" / "readme.md").write_text("# hello\n", encoding="utf-8")
    return ws


def test_valid_relative_png_returns_bytes(workspace: Path) -> None:
    client = _make_client(workspace)
    resp = client.get("/api/workspace-image", params={"path": "docs/images/pic.png"})
    assert resp.status_code == 200
    assert resp.content == _PNG_BYTES
    assert resp.headers["content-type"].startswith("image/png")


def test_svg_returns_svg_mime(workspace: Path) -> None:
    client = _make_client(workspace)
    resp = client.get("/api/workspace-image", params={"path": "docs/images/diagram.svg"})
    assert resp.status_code == 200
    assert "svg" in resp.headers["content-type"]


def test_markdown_is_rejected(workspace: Path) -> None:
    # The image endpoint must not accidentally serve text files, even though
    # they sit under the workspace.
    client = _make_client(workspace)
    resp = client.get("/api/workspace-image", params={"path": "docs/readme.md"})
    assert resp.status_code == 415


def test_missing_path_returns_400(workspace: Path) -> None:
    client = _make_client(workspace)
    resp = client.get("/api/workspace-image")
    assert resp.status_code == 400


def test_traversal_relative_escape_is_served(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret.png"
    outside.write_bytes(_PNG_BYTES)
    client = _make_client(workspace)
    resp = client.get("/api/workspace-image", params={"path": "../secret.png"})
    assert resp.status_code == 200
    assert resp.content == _PNG_BYTES


def test_absolute_path_outside_workspace_is_served(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "other.png"
    outside.write_bytes(_PNG_BYTES)
    client = _make_client(workspace)
    resp = client.get("/api/workspace-image", params={"path": str(outside)})
    assert resp.status_code == 200
    assert resp.content == _PNG_BYTES


def test_symlink_escape_is_served(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.png"
    outside.write_bytes(_PNG_BYTES)
    link = workspace / "escape.png"
    link.symlink_to(outside)
    client = _make_client(workspace)
    resp = client.get("/api/workspace-image", params={"path": "escape.png"})
    assert resp.status_code == 200
    assert resp.content == _PNG_BYTES


def test_missing_file_returns_404(workspace: Path) -> None:
    client = _make_client(workspace)
    resp = client.get("/api/workspace-image", params={"path": "docs/images/nope.png"})
    assert resp.status_code == 404


def test_file_too_large_returns_413(workspace: Path) -> None:
    big = workspace / "big.png"
    big.write_bytes(b"x" * (15 * 1024 * 1024 + 1))
    client = _make_client(workspace)
    resp = client.get("/api/workspace-image", params={"path": "big.png"})
    assert resp.status_code == 413


def test_directory_path_returns_404(workspace: Path) -> None:
    client = _make_client(workspace)
    resp = client.get("/api/workspace-image", params={"path": "docs/images"})
    assert resp.status_code == 404


# ── Chat-scoped resolution (`chat_id`) ──────────────────────────────────
#
# A chat message embeds `![shot](memory-vault/x.png)` relative to its own
# workspace. Unscoped, the relative path anchors to `workspace_root` (and the
# fuzzy fallback can land on a same-named file in another workspace), so the
# chat transcript names its chat and the image resolves the way that chat's
# file links do (`/api/chats/{chat_id}/file-path`).


def _chat_client(tmp_path: Path):
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
    project = manager.create_project("Shots", workspace="work")
    chat = manager.create_chat(project.project_id, title="Shots")
    app = Starlette(routes=[Route("/api/workspace-image", workspace_image, methods=["GET"])])
    app.state.config = config
    app.state.project_chat_manager = manager
    return TestClient(app, raise_server_exceptions=False), chat.chat_id, CiaoConfig


def test_chat_id_resolves_against_the_chat_workspace(tmp_path: Path, monkeypatch) -> None:
    client, chat_id, config_cls = _chat_client(tmp_path)
    root = tmp_path / "work"
    (root / "memory-vault").mkdir(parents=True)
    (root / "memory-vault" / "shot.png").write_bytes(_PNG_BYTES)
    # The same relative path under the primary root is the wrong workspace.
    (tmp_path / "memory-vault").mkdir(exist_ok=True)
    (tmp_path / "memory-vault" / "shot.png").write_bytes(b"wrong workspace")
    monkeypatch.setattr(config_cls, "agent_root", lambda self, workspace: root)

    resp = client.get(
        "/api/workspace-image", params={"path": "memory-vault/shot.png", "chat_id": chat_id}
    )

    assert resp.status_code == 200
    assert resp.content == _PNG_BYTES
    assert resp.headers["content-type"] == "image/png"


def test_chat_id_never_falls_back_to_another_workspace(tmp_path: Path, monkeypatch) -> None:
    client, chat_id, config_cls = _chat_client(tmp_path)
    root = tmp_path / "work"
    root.mkdir()
    (tmp_path / "elsewhere.png").write_bytes(_PNG_BYTES)
    monkeypatch.setattr(config_cls, "agent_root", lambda self, workspace: root)

    resp = client.get("/api/workspace-image", params={"path": "elsewhere.png", "chat_id": chat_id})

    assert resp.status_code == 404


def test_unknown_chat_id_is_404(tmp_path: Path) -> None:
    client, _chat_id, _config_cls = _chat_client(tmp_path)
    (tmp_path / "pic.png").write_bytes(_PNG_BYTES)

    resp = client.get("/api/workspace-image", params={"path": "pic.png", "chat_id": "nope"})

    assert resp.status_code == 404
