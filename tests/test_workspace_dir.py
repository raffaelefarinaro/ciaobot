"""Tests for GET /api/workspace-dir, the confined read-only folder listing (#2782).

Browsing is confined to named roots (the workspace, the vault and, with a
``chat_id``, the chat's agent root and linked repo). The same route answers
400 ``not_a_directory`` for a file, so the link router can classify a path
in one request.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web import routes_api
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import workspace_dir


@dataclass
class _FakeConfig:
    workspace_root: Path
    vault_root: Path | None = None


def _make_fake_client(config: _FakeConfig) -> TestClient:
    app = Starlette(routes=[Route("/api/workspace-dir", workspace_dir, methods=["GET"])])
    app.state.config = config
    return TestClient(app, raise_server_exceptions=False)


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )


def _make_manager_client(manager: ProjectChatManager) -> TestClient:
    app = Starlette(routes=[Route("/api/workspace-dir", workspace_dir, methods=["GET"])])
    app.state.project_chat_manager = manager
    app.state.config = manager._config
    return TestClient(app, raise_server_exceptions=False)


def _one_chat(manager: ProjectChatManager):
    project = manager.create_project("Browse", workspace="work")
    return manager.create_chat(project.project_id, title="Browse")


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "Beta").mkdir()
    (root / "alpha").mkdir()
    (root / "b.MD").write_bytes(b"abc")
    (root / "a.txt").write_text("hi", encoding="utf-8")
    (root / ".hidden").write_text("secret", encoding="utf-8")
    (root / ".git").mkdir()
    return root


def test_empty_path_lists_workspace_root(ws: Path) -> None:
    client = _make_fake_client(_FakeConfig(workspace_root=ws))
    resp = client.get("/api/workspace-dir")
    assert resp.status_code == 200
    body = resp.json()
    assert [e["name"] for e in body["entries"]] == ["alpha", "Beta", "a.txt", "b.MD"]
    alpha = body["entries"][0]
    assert alpha["type"] == "dir"
    assert alpha["size"] is None
    assert alpha["ext"] == ""
    b_md = body["entries"][3]
    assert b_md["size"] == 3
    assert b_md["ext"] == "md"
    assert isinstance(b_md["mtime"], int)
    assert b_md["path"] == (ws / "b.MD").resolve().as_posix()
    assert body["root"] == {"name": "workspace", "path": ws.resolve().as_posix()}
    assert body["parent"] is None
    assert body["truncated"] is False


def test_relative_path_lists_subfolder(ws: Path) -> None:
    client = _make_fake_client(_FakeConfig(workspace_root=ws))
    body = client.get("/api/workspace-dir", params={"path": "alpha"}).json()
    assert body["path"] == (ws / "alpha").resolve().as_posix()
    assert body["parent"] == ws.resolve().as_posix()


def test_trailing_slash_and_line_suffix_are_ignored(ws: Path) -> None:
    client = _make_fake_client(_FakeConfig(workspace_root=ws))
    slash = client.get("/api/workspace-dir", params={"path": "alpha/"})
    suffix = client.get("/api/workspace-dir", params={"path": "alpha:12"})
    assert slash.status_code == 200
    assert suffix.status_code == 200
    assert slash.json()["path"] == suffix.json()["path"] == (ws / "alpha").resolve().as_posix()


def test_absolute_path_inside_root_lists(ws: Path) -> None:
    client = _make_fake_client(_FakeConfig(workspace_root=ws))
    resp = client.get("/api/workspace-dir", params={"path": str(ws / "alpha")})
    assert resp.status_code == 200
    assert resp.json()["path"] == (ws / "alpha").resolve().as_posix()


def test_file_answers_not_a_directory(ws: Path) -> None:
    client = _make_fake_client(_FakeConfig(workspace_root=ws))
    resp = client.get("/api/workspace-dir", params={"path": "a.txt"})
    assert resp.status_code == 400
    assert resp.json() == {
        "error": "not_a_directory",
        "kind": "file",
        "path": (ws / "a.txt").resolve().as_posix(),
    }


def test_file_outside_roots_is_still_classified(ws: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("x", encoding="utf-8")
    client = _make_fake_client(_FakeConfig(workspace_root=ws))
    resp = client.get("/api/workspace-dir", params={"path": str(outside)})
    assert resp.status_code == 400
    assert resp.json()["kind"] == "file"


def test_missing_path_is_404(ws: Path) -> None:
    client = _make_fake_client(_FakeConfig(workspace_root=ws))
    resp = client.get("/api/workspace-dir", params={"path": "nope"})
    assert resp.status_code == 404


def test_directory_outside_roots_is_403(ws: Path, tmp_path: Path) -> None:
    (tmp_path / "elsewhere").mkdir()
    client = _make_fake_client(_FakeConfig(workspace_root=ws))
    absolute = client.get("/api/workspace-dir", params={"path": str(tmp_path / "elsewhere")})
    assert absolute.status_code == 403
    assert absolute.json() == {"error": "outside_roots"}
    relative = client.get("/api/workspace-dir", params={"path": "../elsewhere"})
    assert relative.status_code == 403


def test_symlink_to_outside_dir_is_refused(ws: Path, tmp_path: Path) -> None:
    (tmp_path / "elsewhere").mkdir()
    (ws / "link").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    client = _make_fake_client(_FakeConfig(workspace_root=ws))
    listing = client.get("/api/workspace-dir").json()
    link = next(e for e in listing["entries"] if e["name"] == "link")
    assert link["type"] == "dir"
    assert client.get("/api/workspace-dir", params={"path": "link"}).status_code == 403


def test_vault_outside_workspace_is_a_second_root(ws: Path, tmp_path: Path) -> None:
    (tmp_path / "vault").mkdir()
    client = _make_fake_client(_FakeConfig(workspace_root=ws, vault_root=tmp_path / "vault"))
    resp = client.get("/api/workspace-dir", params={"path": str(tmp_path / "vault")})
    assert resp.status_code == 200
    body = resp.json()
    assert body["root"]["name"] == "vault"
    assert [r["name"] for r in body["roots"]] == ["workspace", "vault"]


def test_vault_inside_workspace_is_not_a_second_root(ws: Path) -> None:
    client = _make_fake_client(_FakeConfig(workspace_root=ws, vault_root=ws / "alpha"))
    listing = client.get("/api/workspace-dir").json()
    assert len(listing["roots"]) == 1
    body = client.get("/api/workspace-dir", params={"path": "alpha"}).json()
    assert body["root"]["name"] == "workspace"


def test_cap_applies_after_hidden_filter_and_sort(ws: Path, monkeypatch) -> None:
    monkeypatch.setattr(routes_api, "_WORKSPACE_DIR_MAX_ENTRIES", 3)
    client = _make_fake_client(_FakeConfig(workspace_root=ws))
    body = client.get("/api/workspace-dir").json()
    assert [e["name"] for e in body["entries"]] == ["alpha", "Beta", "a.txt"]
    assert body["truncated"] is True


def test_control_characters_are_bad_path(ws: Path) -> None:
    client = _make_fake_client(_FakeConfig(workspace_root=ws))
    resp = client.get("/api/workspace-dir", params={"path": "a\x01b"})
    assert resp.status_code == 400
    assert resp.json() == {"error": "bad path"}


def test_chat_roots_include_agent_root_and_linked_repo(tmp_path, tmp_path_factory, monkeypatch) -> None:
    manager = _make_manager(tmp_path)
    chat = _one_chat(manager)
    root = tmp_path / "work"
    (root / "drafts").mkdir(parents=True)
    # Outside tmp_path, or the no-chat case would find it inside the workspace root.
    repo = tmp_path_factory.mktemp("linked-repo")
    manager.get_project(chat.project_id).repo_path = str(repo)
    monkeypatch.setattr(CiaoConfig, "agent_root", lambda self, workspace: root)
    client = _make_manager_client(manager)

    drafts = client.get(
        "/api/workspace-dir", params={"path": "drafts", "chat_id": chat.chat_id}
    )
    assert drafts.status_code == 200
    assert drafts.json()["path"] == (root / "drafts").resolve().as_posix()

    linked = client.get(
        "/api/workspace-dir", params={"path": str(repo), "chat_id": chat.chat_id}
    )
    assert linked.status_code == 200
    assert linked.json()["root"]["name"] == "repo"

    unlinked = client.get("/api/workspace-dir", params={"path": str(repo)})
    assert unlinked.status_code == 403


def test_unicode_space_name_lists_and_opens(ws: Path) -> None:
    (ws / "Shot A").mkdir()
    client = _make_fake_client(_FakeConfig(workspace_root=ws))
    listing = client.get("/api/workspace-dir").json()
    entry = next(e for e in listing["entries"] if e["name"] == "Shot A")
    assert client.get("/api/workspace-dir", params={"path": entry["path"]}).status_code == 200


def test_unreadable_dir_is_403(ws: Path, monkeypatch) -> None:
    def _deny(*args, **kwargs):
        raise OSError("denied")

    monkeypatch.setattr(routes_api.os, "scandir", _deny)
    client = _make_fake_client(_FakeConfig(workspace_root=ws))
    resp = client.get("/api/workspace-dir")
    assert resp.status_code == 403
    assert resp.json() == {"error": "unreadable"}


def test_broken_symlink_is_omitted(ws: Path, tmp_path: Path) -> None:
    (ws / "dangling").symlink_to(tmp_path / "gone")
    client = _make_fake_client(_FakeConfig(workspace_root=ws))
    names = [e["name"] for e in client.get("/api/workspace-dir").json()["entries"]]
    assert "dangling" not in names


def test_no_usable_roots_is_404(tmp_path: Path) -> None:
    client = _make_fake_client(_FakeConfig(workspace_root=tmp_path / "missing"))
    assert client.get("/api/workspace-dir").status_code == 404


def test_relative_path_falls_through_to_vault(ws: Path, tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / "notes").mkdir(parents=True)
    client = _make_fake_client(_FakeConfig(workspace_root=ws, vault_root=vault))
    body = client.get("/api/workspace-dir", params={"path": "notes"}).json()
    assert body["root"]["name"] == "vault"
    assert body["parent"] == vault.resolve().as_posix()


def test_unknown_chat_is_404(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    client = _make_manager_client(manager)
    resp = client.get("/api/workspace-dir", params={"chat_id": "missing"})
    assert resp.status_code == 404
    assert resp.json() == {"error": "chat not found"}
