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


def _chat_client(tmp_path: Path, monkeypatch):
    """A client over a real chat registry with chats in two workspaces.

    Each workspace has its own agent root (``tmp_path/<name>``); the patched
    ``agent_root`` refuses any workspace it was not told about, so a lookup
    under the wrong workspace fails loudly instead of quietly resolving.
    """
    from ciao.config import CiaoConfig, WorkspaceConfig
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
    roots = {name: tmp_path / name for name in ("work", "home")}
    for name, root in roots.items():
        (root / "vault").mkdir(parents=True)
        config.workspaces[name] = WorkspaceConfig(name=name, vault_root=f"{name}/vault")

    def agent_root(self, workspace):
        assert workspace in roots, f"unexpected workspace {workspace!r}"
        return roots[workspace]

    def vault_root(self, workspace):
        assert workspace in roots, f"unexpected workspace {workspace!r}"
        return roots[workspace] / "vault"

    monkeypatch.setattr(CiaoConfig, "agent_root", agent_root)
    monkeypatch.setattr(CiaoConfig, "workspace_vault_root", vault_root)
    chats = {}
    for name in roots:
        project = manager.create_project(f"Shots {name}", workspace=name)
        chats[name] = manager.create_chat(project.project_id, title=name).chat_id
    app = Starlette(routes=[Route("/api/workspace-image", workspace_image, methods=["GET"])])
    app.state.config = config
    app.state.project_chat_manager = manager
    return TestClient(app, raise_server_exceptions=False), chats, roots


def test_chat_id_anchors_to_the_chat_workspace(tmp_path: Path, monkeypatch) -> None:
    client, chats, roots = _chat_client(tmp_path, monkeypatch)
    (roots["work"] / "memory-vault").mkdir()
    (roots["work"] / "memory-vault" / "shot.png").write_bytes(_PNG_BYTES)
    # The same relative path under the primary root is the wrong workspace.
    (tmp_path / "memory-vault").mkdir(exist_ok=True)
    (tmp_path / "memory-vault" / "shot.png").write_bytes(b"wrong workspace")

    resp = client.get(
        "/api/workspace-image", params={"path": "memory-vault/shot.png", "chat_id": chats["work"]}
    )

    assert resp.status_code == 200
    assert resp.content == _PNG_BYTES
    assert resp.headers["content-type"] == "image/png"


def test_another_workspace_chat_does_not_serve_the_file(tmp_path: Path, monkeypatch) -> None:
    client, chats, roots = _chat_client(tmp_path, monkeypatch)
    (roots["work"] / "shot.png").write_bytes(_PNG_BYTES)

    resp = client.get("/api/workspace-image", params={"path": "shot.png", "chat_id": chats["home"]})

    assert resp.status_code == 404


def test_chat_id_never_falls_back_to_the_primary_root(tmp_path: Path, monkeypatch) -> None:
    client, chats, _roots = _chat_client(tmp_path, monkeypatch)
    (tmp_path / "elsewhere.png").write_bytes(_PNG_BYTES)

    resp = client.get("/api/workspace-image", params={"path": "elsewhere.png", "chat_id": chats["work"]})

    assert resp.status_code == 404


def test_chat_id_lookup_is_exact_and_never_walks_the_tree(tmp_path: Path, monkeypatch) -> None:
    # A message can name any number of made-up image paths; each must cost an
    # exact lookup, not a directory walk. The file exists deeper in the root,
    # where only a fuzzy pass would find it.
    import ciao.web.routes_api as routes_api

    client, chats, roots = _chat_client(tmp_path, monkeypatch)
    (roots["work"] / "deep" / "er").mkdir(parents=True)
    (roots["work"] / "deep" / "er" / "shot.png").write_bytes(_PNG_BYTES)
    real = routes_api._resolve_workspace_path
    fuzzy_calls: list[str] = []

    def spy(roots_arg, raw, *, allow_fuzzy=True):
        if allow_fuzzy:
            fuzzy_calls.append(raw)
        return real(roots_arg, raw, allow_fuzzy=allow_fuzzy)

    monkeypatch.setattr(routes_api, "_resolve_workspace_path", spy)

    resp = client.get("/api/workspace-image", params={"path": "shot.png", "chat_id": chats["work"]})

    assert resp.status_code == 404
    assert fuzzy_calls == []


def test_chat_id_with_a_non_image_file_is_415(tmp_path: Path, monkeypatch) -> None:
    client, chats, roots = _chat_client(tmp_path, monkeypatch)
    (roots["work"] / "notes.md").write_text("# notes\n", encoding="utf-8")

    resp = client.get("/api/workspace-image", params={"path": "notes.md", "chat_id": chats["work"]})

    assert resp.status_code == 415


def test_unknown_chat_id_is_404(tmp_path: Path, monkeypatch) -> None:
    client, _chats, _roots = _chat_client(tmp_path, monkeypatch)
    (tmp_path / "pic.png").write_bytes(_PNG_BYTES)

    resp = client.get("/api/workspace-image", params={"path": "pic.png", "chat_id": "nope"})

    assert resp.status_code == 404
