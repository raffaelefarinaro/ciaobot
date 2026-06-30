"""Tests for the /api/workspace-image endpoint.

Mirror of ``test_workspace_file`` for the image variant: same sandbox
contract, but the allowed extensions are image types and the media type
is derived from the filename. These tests spin up a minimal Starlette
app around the handler with a tmp workspace to exercise each guard.
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
    # they sit under the workspace and would pass the sandbox check.
    client = _make_client(workspace)
    resp = client.get("/api/workspace-image", params={"path": "docs/readme.md"})
    assert resp.status_code == 415


def test_missing_path_returns_400(workspace: Path) -> None:
    client = _make_client(workspace)
    resp = client.get("/api/workspace-image")
    assert resp.status_code == 400


def test_traversal_returns_403(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret.png"
    outside.write_bytes(_PNG_BYTES)
    client = _make_client(workspace)
    resp = client.get("/api/workspace-image", params={"path": "../secret.png"})
    assert resp.status_code == 403


def test_absolute_path_outside_workspace_returns_403(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "other.png"
    outside.write_bytes(_PNG_BYTES)
    client = _make_client(workspace)
    resp = client.get("/api/workspace-image", params={"path": str(outside)})
    assert resp.status_code == 403


def test_symlink_escape_returns_403(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.png"
    outside.write_bytes(_PNG_BYTES)
    link = workspace / "escape.png"
    link.symlink_to(outside)
    client = _make_client(workspace)
    resp = client.get("/api/workspace-image", params={"path": "escape.png"})
    assert resp.status_code == 403


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
