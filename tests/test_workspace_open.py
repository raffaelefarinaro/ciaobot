"""Tests for the /api/workspace-open endpoint."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.web.routes_api import workspace_open


@dataclass
class _FakeConfig:
    workspace_root: Path


def _make_client(workspace_root: Path) -> TestClient:
    app = Starlette(routes=[Route("/api/workspace-open", workspace_open, methods=["POST"])])
    app.state.config = _FakeConfig(workspace_root=workspace_root)
    return TestClient(app)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "deck.pptx").write_bytes(b"fake-pptx")
    return ws


def test_workspace_open_resolves_relative_path(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []

    def _fake_open(path: Path) -> None:
        opened.append(str(path))

    monkeypatch.setattr("ciao.web.routes_api._open_path_with_default_app", _fake_open)
    client = _make_client(workspace)
    resp = client.post("/api/workspace-open", json={"path": "deck.pptx"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert opened == [str((workspace / "deck.pptx").resolve())]


def test_workspace_open_missing_path(workspace: Path) -> None:
    client = _make_client(workspace)
    resp = client.post("/api/workspace-open", json={"path": "missing.pptx"})
    assert resp.status_code == 404


def test_workspace_open_requires_path(workspace: Path) -> None:
    client = _make_client(workspace)
    resp = client.post("/api/workspace-open", json={})
    assert resp.status_code == 400
