"""Regression tests for issue #421: project update must recompute vault_doc_path.

`update_project(vault_folder=...)` used to leave `vault_doc_path` pointing at
the old location after a vault-folder rename, while `project_get` serves the
stored value directly (only `list_projects` runs the discovery pass that
heals it).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=f"memory-vault/{name}")
            for name in ("personal", "work")
        },
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )


def _make_stem_doc(root: Path, stem: str) -> Path:
    """Create memory-vault/work/projects/active/<stem>/<stem>.md (issue shape)."""
    folder = root / "memory-vault" / "work" / "projects" / "active" / stem
    folder.mkdir(parents=True)
    doc = folder / f"{stem}.md"
    doc.write_text(
        f"---\nname: {stem}\nstatus: active\n---\n# {stem}\n",
        encoding="utf-8",
    )
    return folder


def test_update_recomputes_vault_doc_path_after_rename(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    _make_stem_doc(tmp_path, "maf-onsite")
    proj = pcm.create_project("maf-onsite", "work", "")
    bound = next(p for p in pcm.list_projects("work") if p.project_id == proj.project_id)
    assert bound.vault_folder == "maf-onsite"
    assert bound.vault_doc_path == (
        "memory-vault/work/projects/active/maf-onsite/maf-onsite.md"
    )

    # Rename the backing vault entry on disk, then rebind via update.
    shutil.move(
        str(tmp_path / "memory-vault/work/projects/active/maf-onsite"),
        str(tmp_path / "memory-vault/work/projects/active/maf-pilot"),
    )
    (tmp_path / "memory-vault/work/projects/active/maf-pilot/maf-onsite.md").rename(
        tmp_path / "memory-vault/work/projects/active/maf-pilot/maf-pilot.md"
    )

    updated = pcm.update_project(proj.project_id, vault_folder="maf-pilot")
    assert updated is not None
    assert updated.vault_folder == "maf-pilot"
    assert updated.vault_doc_path == (
        "memory-vault/work/projects/active/maf-pilot/maf-pilot.md"
    )

    # project_get serves the stored value: it must be fresh without waiting
    # for a list_projects() discovery pass.
    fetched = pcm.get_project(proj.project_id)
    assert fetched is not None
    assert fetched.vault_doc_path == (
        "memory-vault/work/projects/active/maf-pilot/maf-pilot.md"
    )

    # Re-sending the same vault_folder (repro step 5) stays consistent.
    again = pcm.update_project(proj.project_id, vault_folder="maf-pilot")
    assert again is not None
    assert again.vault_doc_path == (
        "memory-vault/work/projects/active/maf-pilot/maf-pilot.md"
    )


def test_update_clears_vault_doc_path_when_binding_cleared(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    _make_stem_doc(tmp_path, "maf-onsite")
    proj = pcm.create_project("maf-onsite", "work", "")
    pcm.list_projects("work")

    updated = pcm.update_project(proj.project_id, vault_folder="")
    assert updated is not None
    assert updated.vault_folder == ""
    assert updated.vault_doc_path == ""


def test_update_leaves_no_stale_path_when_doc_missing(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    _make_stem_doc(tmp_path, "maf-onsite")
    proj = pcm.create_project("maf-onsite", "work", "")
    pcm.list_projects("work")

    # Bind to a folder with no canonical doc: no stale pointer may survive.
    (tmp_path / "memory-vault" / "work" / "projects" / "active" / "empty").mkdir(
        parents=True
    )
    updated = pcm.update_project(proj.project_id, vault_folder="empty")
    assert updated is not None
    assert updated.vault_doc_path == ""
