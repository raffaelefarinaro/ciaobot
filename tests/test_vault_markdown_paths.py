"""Tests for the /api/vault-markdown-paths endpoint."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web.routes_api import vault_markdown_paths


@dataclass
class _FakeConfig:
    workspace_root: Path
    vault_root: Path | None = None


def _make_client(workspace_root: Path, vault_root: Path | None = None) -> TestClient:
    app = Starlette(routes=[Route("/api/vault-markdown-paths", vault_markdown_paths, methods=["GET"])])
    app.state.config = _FakeConfig(workspace_root=workspace_root, vault_root=vault_root)
    return TestClient(app)


def test_lists_workspace_markdown_paths(tmp_path: Path):
    docs = tmp_path / "memory-vault" / "work" / "projects" / "active" / "rossmann"
    docs.mkdir(parents=True)
    (docs / "README.md").write_text("# Rossmann MVP\n", encoding="utf-8")
    (docs / "Shelf Recognition Spec.md").write_text("See [[README|Rossmann MVP]]\n", encoding="utf-8")
    hidden = tmp_path / "memory-vault" / ".obsidian" / "cache.md"
    hidden.parent.mkdir(parents=True)
    hidden.write_text("hidden", encoding="utf-8")

    client = _make_client(tmp_path)
    resp = client.get("/api/vault-markdown-paths")
    assert resp.status_code == 200
    paths = resp.json()["paths"]
    assert "memory-vault/work/projects/active/rossmann/README.md" in paths
    assert "memory-vault/work/projects/active/rossmann/Shelf Recognition Spec.md" in paths
    assert not any(".obsidian" in p for p in paths)
