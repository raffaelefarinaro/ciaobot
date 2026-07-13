"""Tests for the project files surface (list + upload).

Covers the manager-level methods (``list_project_files``,
``save_project_file_upload``) and the route handlers
(``GET/POST /api/projects/{id}/files``). The endpoints are scoped to a
project's vault folder; they reject manual projects without a vault entry,
single-file personal projects (the file IS the project), traversal in
filenames, and oversized or disallowed extensions.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from ciao.web.routes_api import (
    _read_upload_limited,
    project_files_list,
    project_files_upload,
)


# ── fixtures ───────────────────────────────────────────────────────────────


def _make_manager(
    tmp_path: Path, *, vault_root: Path | None = None
) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        vault_root=vault_root or Path("memory-vault"),
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )


def _make_work_project(root: Path, folder_name: str) -> Path:
    folder = root / "memory-vault" / "work" / "projects" / "active" / folder_name
    folder.mkdir(parents=True)
    (folder / "README.md").write_text(
        f"---\nname: {folder_name}\nstatus: active\n---\n# {folder_name}\n",
        encoding="utf-8",
    )
    return folder


def _make_personal_file(root: Path, stem: str) -> Path:
    parent = root / "memory-vault" / "personal" / "projects" / "active"
    parent.mkdir(parents=True, exist_ok=True)
    md = parent / f"{stem}.md"
    md.write_text(f"---\nname: {stem}\nstatus: active\n---\n# {stem}\n", encoding="utf-8")
    return md


def _make_client(pcm: ProjectChatManager, config: CiaoConfig) -> TestClient:
    app = Starlette(
        routes=[
            Route("/api/projects/{project_id}/files", project_files_list, methods=["GET"]),
            Route("/api/projects/{project_id}/files", project_files_upload, methods=["POST"]),
        ]
    )
    app.state.project_chat_manager = pcm
    app.state.config = config
    return TestClient(app)


# ── list ───────────────────────────────────────────────────────────────────


def test_list_returns_files_with_classification(tmp_path: Path) -> None:
    folder = _make_work_project(tmp_path, "2026-q2-foo")
    (folder / "notes.md").write_text("# notes", encoding="utf-8")
    (folder / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (folder / "data.json").write_text("{}", encoding="utf-8")
    (folder / "spec.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")

    pcm = _make_manager(tmp_path)
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "2026-q2-foo")
    files = pcm.list_project_files(proj.project_id)
    by_path = {f["path"]: f for f in files}

    # README.md is created by the fixture; should appear too.
    assert by_path["README.md"]["kind"] == "markdown"
    assert by_path["notes.md"]["kind"] == "markdown"
    assert by_path["shot.png"]["kind"] == "image"
    assert by_path["data.json"]["kind"] == "text"
    assert by_path["spec.pdf"]["kind"] == "binary"
    # vault_path is workspace-relative and round-trippable.
    assert by_path["notes.md"]["vault_path"].endswith("/notes.md")
    assert by_path["notes.md"]["vault_path"].startswith("memory-vault/")


def test_external_vault_files_list_and_upload_with_absolute_viewer_path(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    vault = tmp_path / "external-vault"
    folder = vault / "work" / "projects" / "active" / "external-project"
    folder.mkdir(parents=True)
    (folder / "README.md").write_text(
        "---\nname: External\nstatus: active\n---\n# External\n",
        encoding="utf-8",
    )
    (folder / "notes.md").write_text("hello", encoding="utf-8")

    pcm = _make_manager(workspace, vault_root=vault)
    project = next(
        p for p in pcm.list_projects() if p.vault_folder == "external-project"
    )

    files = {entry["path"]: entry for entry in pcm.list_project_files(project.project_id)}
    assert files["notes.md"]["vault_path"] == str((folder / "notes.md").resolve())

    uploaded = pcm.save_project_file_upload(project.project_id, b"new", "new.md")
    assert uploaded["vault_path"] == str((folder / "new.md").resolve())
    assert (folder / "new.md").read_bytes() == b"new"


def test_list_recurses_subdirectories(tmp_path: Path) -> None:
    folder = _make_work_project(tmp_path, "2026-q2-foo")
    (folder / "screenshots").mkdir()
    (folder / "screenshots" / "login.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (folder / "screenshots" / "checkout.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    pcm = _make_manager(tmp_path)
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "2026-q2-foo")
    files = pcm.list_project_files(proj.project_id)
    paths = {f["path"] for f in files}
    assert "screenshots/login.png" in paths
    assert "screenshots/checkout.png" in paths


def test_list_skips_hidden_and_gitkeep(tmp_path: Path) -> None:
    folder = _make_work_project(tmp_path, "2026-q2-foo")
    (folder / ".gitkeep").write_text("", encoding="utf-8")
    (folder / ".secret").write_text("nope", encoding="utf-8")
    (folder / ".hidden").mkdir()
    (folder / ".hidden" / "x.md").write_text("hidden", encoding="utf-8")
    (folder / "visible.md").write_text("visible", encoding="utf-8")

    pcm = _make_manager(tmp_path)
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "2026-q2-foo")
    files = pcm.list_project_files(proj.project_id)
    paths = {f["path"] for f in files}
    assert "visible.md" in paths
    assert all(not p.startswith(".") for p in paths)
    assert ".hidden/x.md" not in paths


def test_list_returns_promoted_personal_project_contents(tmp_path: Path) -> None:
    """A stray single-file personal project gets auto-promoted at init, so
    its main markdown shows up in the Files listing under the new folder."""
    _make_personal_file(tmp_path, "Wedding")
    pcm = _make_manager(tmp_path)
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "Wedding")
    files = pcm.list_project_files(proj.project_id)
    paths = {f["path"] for f in files}
    # The original single-file content is now at Wedding/Wedding.md.
    assert "Wedding.md" in paths


def test_list_empty_for_manual_project(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    proj = pcm.create_project(name="Manual", workspace="personal")
    assert proj.vault_folder == ""
    files = pcm.list_project_files(proj.project_id)
    assert files == []


def test_list_route_404_for_unknown_project(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "s.json",
        media_root=tmp_path / ".runtime" / "media",
    )
    client = _make_client(pcm, config)
    resp = client.get("/api/projects/proj-unknown/files")
    assert resp.status_code == 404


def test_list_route_returns_200_with_entries(tmp_path: Path) -> None:
    folder = _make_work_project(tmp_path, "2026-q2-foo")
    (folder / "notes.md").write_text("hi", encoding="utf-8")
    pcm = _make_manager(tmp_path)
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "2026-q2-foo")
    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "s.json",
        media_root=tmp_path / ".runtime" / "media",
    )
    client = _make_client(pcm, config)
    resp = client.get(f"/api/projects/{proj.project_id}/files")
    assert resp.status_code == 200
    data = resp.json()
    assert any(f["path"] == "notes.md" for f in data)


# ── upload ─────────────────────────────────────────────────────────────────


def test_upload_saves_file_to_vault_folder(tmp_path: Path) -> None:
    folder = _make_work_project(tmp_path, "2026-q2-foo")
    pcm = _make_manager(tmp_path)
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "2026-q2-foo")

    entry = pcm.save_project_file_upload(proj.project_id, b"hello world", "notes.md")
    assert entry["path"] == "notes.md"
    saved = folder / "notes.md"
    assert saved.exists()
    assert saved.read_bytes() == b"hello world"


def test_upload_collision_appends_suffix(tmp_path: Path) -> None:
    folder = _make_work_project(tmp_path, "2026-q2-foo")
    (folder / "notes.md").write_text("first", encoding="utf-8")
    pcm = _make_manager(tmp_path)
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "2026-q2-foo")

    entry = pcm.save_project_file_upload(proj.project_id, b"second", "notes.md")
    assert entry["path"] == "notes-2.md"
    entry2 = pcm.save_project_file_upload(proj.project_id, b"third", "notes.md")
    assert entry2["path"] == "notes-3.md"


def test_upload_rejects_traversal(tmp_path: Path) -> None:
    _make_work_project(tmp_path, "2026-q2-foo")
    pcm = _make_manager(tmp_path)
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "2026-q2-foo")
    with pytest.raises(ValueError):
        pcm.save_project_file_upload(proj.project_id, b"x", "../escape.md")
    with pytest.raises(ValueError):
        pcm.save_project_file_upload(proj.project_id, b"x", "sub/dir.md")


def test_upload_rejects_hidden_filename(tmp_path: Path) -> None:
    _make_work_project(tmp_path, "2026-q2-foo")
    pcm = _make_manager(tmp_path)
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "2026-q2-foo")
    with pytest.raises(ValueError):
        pcm.save_project_file_upload(proj.project_id, b"x", ".env")


def test_upload_rejects_unsupported_extension(tmp_path: Path) -> None:
    _make_work_project(tmp_path, "2026-q2-foo")
    pcm = _make_manager(tmp_path)
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "2026-q2-foo")
    with pytest.raises(ValueError):
        pcm.save_project_file_upload(proj.project_id, b"x", "trojan.exe")


def test_upload_rejects_oversized(tmp_path: Path) -> None:
    _make_work_project(tmp_path, "2026-q2-foo")
    pcm = _make_manager(tmp_path)
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "2026-q2-foo")
    with pytest.raises(ValueError):
        pcm.save_project_file_upload(proj.project_id, b"x" * (50 * 1024 * 1024 + 1), "big.zip")


class _RecordingUpload:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.offset = 0
        self.read_sizes: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if size < 0:
            size = len(self.payload) - self.offset
        chunk = self.payload[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk


async def test_bounded_upload_reader_accepts_exact_limit() -> None:
    upload = _RecordingUpload(b"1234")
    assert await _read_upload_limited(upload, 4) == b"1234"
    assert -1 not in upload.read_sizes
    assert max(upload.read_sizes) <= 5


async def test_bounded_upload_reader_stops_one_byte_over_limit() -> None:
    upload = _RecordingUpload(b"1234567890")
    with pytest.raises(ValueError, match="file too large"):
        await _read_upload_limited(upload, 4)
    assert upload.offset == 5
    assert -1 not in upload.read_sizes


def test_upload_to_promoted_personal_project_succeeds(tmp_path: Path) -> None:
    """Personal projects that start as a single .md auto-promote to folder
    form on init, then accept uploads like any other project."""
    _make_personal_file(tmp_path, "Wedding")
    pcm = _make_manager(tmp_path)
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "Wedding")
    entry = pcm.save_project_file_upload(proj.project_id, b"hello", "guests.md")
    assert entry["path"] == "guests.md"
    saved = tmp_path / "memory-vault" / "personal" / "projects" / "active" / "Wedding" / "guests.md"
    assert saved.read_bytes() == b"hello"


def test_upload_manual_project_returns_409(tmp_path: Path) -> None:
    """Manual PWA projects (no vault_folder) genuinely have nowhere to put
    uploads; the route still returns 409 in that case."""
    pcm = _make_manager(tmp_path)
    proj = pcm.create_project(name="Manual", workspace="personal")
    assert proj.vault_folder == ""
    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "s.json",
        media_root=tmp_path / ".runtime" / "media",
    )
    client = _make_client(pcm, config)
    resp = client.post(
        f"/api/projects/{proj.project_id}/files",
        files={"file1": ("x.md", b"hi", "text/markdown")},
    )
    assert resp.status_code == 409


def test_upload_route_round_trip(tmp_path: Path) -> None:
    folder = _make_work_project(tmp_path, "2026-q2-foo")
    pcm = _make_manager(tmp_path)
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "2026-q2-foo")
    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "s.json",
        media_root=tmp_path / ".runtime" / "media",
    )
    client = _make_client(pcm, config)
    resp = client.post(
        f"/api/projects/{proj.project_id}/files",
        files={
            "file1": ("a.md", b"alpha", "text/markdown"),
            "file2": ("b.exe", b"bad", "application/octet-stream"),
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    saved_paths = {e["path"] for e in body["saved"]}
    assert "a.md" in saved_paths
    assert any(e["filename"] == "b.exe" for e in body["errors"])
    assert (folder / "a.md").read_bytes() == b"alpha"
