from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao import repo_link
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.control_plane import CiaoControlPlane
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import create_project, project_detail, repo_candidates


def _make_config(tmp_path: Path) -> CiaoConfig:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=f"memory-vault/{name}")
            for name in ("personal", "work")
        },
    )


def _make_manager(tmp_path: Path) -> tuple[ProjectChatManager, CiaoConfig]:
    config = _make_config(tmp_path)
    runtime = tmp_path / ".runtime"
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    pcm = ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )
    return pcm, config


def _init(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    return path


def test_create_project_persists_repo_path(tmp_path: Path) -> None:
    pcm, _config = _make_manager(tmp_path)
    repo = _init(tmp_path / "repo")
    project = pcm.create_project("Code", "personal", repo_path=str(repo))
    second, _ = _make_manager(tmp_path)
    reloaded = second.get_project(project.project_id)
    assert reloaded is not None
    assert os.path.samefile(reloaded.repo_path, repo)
    assert reloaded.to_dict()["repo"]["branch"] == "main"


def test_unlinked_project_reports_repo_none(tmp_path: Path) -> None:
    pcm, _config = _make_manager(tmp_path)
    data = pcm.create_project("Plain", "personal").to_dict()
    assert data["repo"] is None
    assert data["repo_path"] == ""


def test_create_project_rejects_invalid_repo_and_creates_nothing(tmp_path: Path) -> None:
    pcm, _config = _make_manager(tmp_path)
    before = len(pcm.list_projects())
    with pytest.raises(ValueError):
        pcm.create_project("Bad", "personal", repo_path=str(tmp_path / "nope"))
    assert len(pcm.list_projects()) == before


def test_update_project_links_then_unlinks(tmp_path: Path) -> None:
    pcm, _config = _make_manager(tmp_path)
    repo = _init(tmp_path / "repo")
    pid = pcm.create_project("Code", "personal").project_id
    linked = pcm.update_project(pid, repo_path=str(repo))
    assert linked is not None
    assert linked.to_dict()["repo"]["branch"] == "main"
    unlinked = pcm.update_project(pid, repo_path="")
    assert unlinked is not None
    assert unlinked.repo_path == ""
    assert unlinked.to_dict()["repo"] is None


def test_update_project_invalid_repo_changes_nothing(tmp_path: Path) -> None:
    pcm, _config = _make_manager(tmp_path)
    pid = pcm.create_project("Old", "personal").project_id
    with pytest.raises(ValueError):
        pcm.update_project(pid, name="New", repo_path=str(tmp_path / "nope"))
    project = pcm.get_project(pid)
    assert project is not None
    assert project.name == "Old"


def test_routes_create_and_patch_repo_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pcm, config = _make_manager(tmp_path)
    repo = _init(tmp_path / "root" / "repo")
    plain = tmp_path / "plain"
    plain.mkdir()
    app = Starlette(
        routes=[
            Route("/api/projects", create_project, methods=["POST"]),
            Route("/api/projects/{project_id}", project_detail, methods=["PATCH", "DELETE"]),
            Route("/api/repos/candidates", repo_candidates, methods=["GET"]),
        ]
    )
    app.state.project_chat_manager = pcm
    app.state.config = config
    client = TestClient(app)

    res = client.post(
        "/api/projects",
        json={"name": "X", "workspace": "personal", "repo_path": str(repo)},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["repo"]["branch"] == "main"

    res = client.patch(f"/api/projects/{body['project_id']}", json={"repo_path": str(plain)})
    assert res.status_code == 400
    assert "Not a git repository" in res.json()["error"]

    res = client.patch(f"/api/projects/{body['project_id']}", json={"repo_path": 3})
    assert res.status_code == 400

    monkeypatch.setattr(repo_link, "REPO_ROOTS", (str(tmp_path / "root"),))
    res = client.get("/api/repos/candidates")
    assert [c["name"] for c in res.json()["candidates"]] == ["repo"]


def test_control_plane_forwards_repo_path(tmp_path: Path) -> None:
    pcm, config = _make_manager(tmp_path)
    repo = _init(tmp_path / "repo")
    plane = CiaoControlPlane(config, project_chat_manager=pcm, schedule_manager=SimpleNamespace())
    principal = SimpleNamespace(workspace="personal", project_id="", chat_id="")
    created = plane.project_create(principal, "Q4", "", str(repo))
    assert created["data"]["repo"]["branch"] == "main"
    pid = created["data"]["project_id"]
    updated = plane.project_update(principal, pid, repo_path="")
    assert updated["data"]["repo"] is None
