"""Tests for vault auto-discovery and `complete_project` across both workspaces.

Personal projects are dual-form: a project entry can live under
``memory-vault/personal/projects/active/`` as either a folder ``<name>/`` (with optional
``README.md``) or a single file ``<name>.md``. ``complete_project`` resolves
which form exists at move time and moves it to ``completed/``.

Work projects are folder-only and rooted under
``memory-vault/work/projects/active/``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager


# ── fixtures ───────────────────────────────────────────────────────────────


def _make_manager(tmp_path: Path, config: CiaoConfig | None = None) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    if config is None:
        config = CiaoConfig(
            pwa_auth_token="test-token",
            workspace_root=tmp_path,
            state_path=runtime / "state.json",
            media_root=runtime / "media",
        )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )


def _make_custom_workspace_config(tmp_path: Path) -> CiaoConfig:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={
            "home": WorkspaceConfig(
                name="home",
                vault_root="memory-vault/home",
                gws_profile="personal",
                model_bucket="personal",
            ),
            "client": WorkspaceConfig(
                name="client",
                vault_root="vaults/client",
                gws_profile="work",
                model_bucket="work",
            ),
        },
    )


def _make_workspace_project_folder(
    vault_root: Path,
    folder_name: str,
    *,
    name: str = "",
    description: str = "",
    status: str = "active",
) -> Path:
    """Create <vault_root>/projects/<status>/<folder>/README.md."""
    folder = vault_root / "projects" / status / folder_name
    folder.mkdir(parents=True)
    fm_name = name or folder_name
    readme = folder / "README.md"
    readme.write_text(
        f"---\nname: {fm_name}\ndescription: {description}\nstatus: {status}\n---\n"
        f"# {fm_name}\n",
        encoding="utf-8",
    )
    project_md = folder / f"{folder_name}.md"
    project_md.write_text(
        f"---\nname: {fm_name}\nstatus: {status}\n---\n# {fm_name}\n",
        encoding="utf-8",
    )
    return folder


def _make_work_project_folder(root: Path, folder_name: str, *, name: str = "", description: str = "") -> Path:
    """Create memory-vault/work/projects/active/<folder>/README.md"""
    folder = root / "memory-vault" / "work" / "projects" / "active" / folder_name
    folder.mkdir(parents=True)
    fm_name = name or folder_name
    readme = folder / "README.md"
    readme.write_text(
        f"---\nname: {fm_name}\ndescription: {description}\nstatus: active\n---\n"
        f"# {fm_name}\n",
        encoding="utf-8",
    )
    # The main project markdown that complete_project rewrites is
    # <folder>/<folder>.md (matches the work convention).
    project_md = folder / f"{folder_name}.md"
    project_md.write_text(
        f"---\nname: {fm_name}\nstatus: active\n---\n# {fm_name}\n",
        encoding="utf-8",
    )
    return folder


def _make_personal_file(root: Path, stem: str, *, name: str = "", description: str = "") -> Path:
    """Create memory-vault/personal/projects/active/<stem>.md"""
    parent = root / "memory-vault" / "personal" / "projects" / "active"
    parent.mkdir(parents=True, exist_ok=True)
    fm_name = name or stem
    md = parent / f"{stem}.md"
    md.write_text(
        f"---\nname: {fm_name}\ndescription: {description}\nstatus: active\n---\n"
        f"# {fm_name}\n",
        encoding="utf-8",
    )
    return md


def _make_personal_folder(root: Path, folder_name: str, *, name: str = "", description: str = "") -> Path:
    """Create memory-vault/personal/projects/active/<folder>/ with a README and main md."""
    folder = root / "memory-vault" / "personal" / "projects" / "active" / folder_name
    folder.mkdir(parents=True)
    fm_name = name or folder_name
    readme = folder / "README.md"
    readme.write_text(
        f"---\nname: {fm_name}\ndescription: {description}\nstatus: active\n---\n"
        f"# {fm_name}\n",
        encoding="utf-8",
    )
    main_md = folder / f"{folder_name}.md"
    main_md.write_text(
        f"---\nname: {fm_name}\nstatus: active\n---\n# {fm_name}\n",
        encoding="utf-8",
    )
    return folder


# ── auto-discovery: work (regression) ──────────────────────────────────────


def test_discover_work_folder(tmp_path: Path) -> None:
    _make_work_project_folder(tmp_path, "2026-q2-foo", name="Q2 Foo", description="A work project")
    pcm = _make_manager(tmp_path)
    projects = pcm.list_projects(workspace="work")
    foo = next((p for p in projects if p.vault_folder == "2026-q2-foo"), None)
    assert foo is not None, "work folder was not auto-discovered"
    assert foo.workspace == "work"
    assert foo.name == "Q2 Foo"
    assert foo.context == "A work project"


def test_configured_workspaces_drive_general_projects_and_discovery(tmp_path: Path) -> None:
    config = _make_custom_workspace_config(tmp_path)
    _make_workspace_project_folder(
        tmp_path / "vaults" / "client",
        "acme",
        name="Acme",
        description="Client vault project",
    )

    pcm = _make_manager(tmp_path, config)
    projects = pcm.list_projects()
    workspaces = {p.workspace for p in projects}
    assert "home" in workspaces
    assert "client" in workspaces
    assert "personal" not in workspaces
    assert "work" not in workspaces

    general_by_workspace = {
        p.workspace: p for p in projects if p.name == "General"
    }
    assert set(general_by_workspace) == {"home", "client"}
    assert (
        tmp_path
        / "memory-vault"
        / "home"
        / "projects"
        / "active"
        / "general"
        / "general.md"
    ).exists()
    assert (
        tmp_path
        / "vaults"
        / "client"
        / "projects"
        / "active"
        / "general"
        / "general.md"
    ).exists()

    client_project = next(
        p for p in pcm.list_projects(workspace="client") if p.vault_folder == "acme"
    )
    assert client_project.name == "Acme"
    assert client_project.context == "Client vault project"
    assert client_project.vault_doc_path == "vaults/client/projects/active/acme/README.md"


def test_qn_prefix_kept_and_stable_when_folder_still_on_disk(tmp_path: Path) -> None:
    """A prefixed project whose vault folder still exists on disk must keep its
    prefix. Stripping it would orphan the row, dedup would merge it into the
    prefix-free project, and discovery would recreate it from the on-disk
    folder on the next boot — the strip -> merge -> rediscover churn. Building
    the manager twice must be stable (no churn).
    """
    # Both the prefixed folder and a prefix-free sibling exist on disk.
    _make_work_project_folder(tmp_path, "2026-q2-foo", name="2026-q2-foo")
    _make_work_project_folder(tmp_path, "foo", name="foo")
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "web_projects.json").write_text(
        json.dumps({
            "version": 1,
            "projects": {
                "proj-prefixed": {
                    "name": "2026-q2-foo", "workspace": "work", "context": "",
                    "created_at": "2026-04-01T00:00:00Z", "order": 1,
                    "vault_folder": "2026-q2-foo",
                },
            },
            "chats": {},
        }),
        encoding="utf-8",
    )

    pcm = _make_manager(tmp_path)
    proj = pcm._projects.get("proj-prefixed")
    assert proj is not None, "prefixed project was dropped (churn)"
    assert proj.vault_folder == "2026-q2-foo", "prefix should be kept while folder exists"

    # Second construction reloads saved state and re-runs migration + discovery.
    # proj-prefixed must survive unchanged (proves the churn is gone).
    pcm2 = _make_manager(tmp_path)
    proj2 = pcm2._projects.get("proj-prefixed")
    assert proj2 is not None and proj2.vault_folder == "2026-q2-foo"


def test_qn_prefix_stripped_when_folder_renamed_away(tmp_path: Path) -> None:
    """When the prefixed folder was genuinely renamed away (only the
    prefix-free folder exists), the row is still repointed to the stripped slug.
    """
    _make_work_project_folder(tmp_path, "bar", name="bar")  # only prefix-free exists
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "web_projects.json").write_text(
        json.dumps({
            "version": 1,
            "projects": {
                "proj-bar": {
                    "name": "2026-q2-bar", "workspace": "work", "context": "",
                    "created_at": "2026-04-01T00:00:00Z", "order": 1,
                    "vault_folder": "2026-q2-bar",
                },
            },
            "chats": {},
        }),
        encoding="utf-8",
    )

    pcm = _make_manager(tmp_path)
    work = pcm.list_projects(workspace="work")
    assert any(p.vault_folder == "bar" for p in work), "row should be repointed to 'bar'"
    assert not any(p.vault_folder == "2026-q2-bar" for p in work), "stale prefix should be gone"


def test_retired_claude_code_cli_project_is_migrated_out(tmp_path: Path) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    state_path = runtime / "web_projects.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "projects": {
                    "proj-cc-cli": {
                        "name": "Claude Code CLI",
                        "workspace": "personal",
                        "context": "Auto-imported sessions",
                        "created_at": "2026-04-19T10:52:15Z",
                        "order": 2,
                        "vault_folder": "",
                    },
                    "proj-manual": {
                        "name": "Manual",
                        "workspace": "personal",
                        "context": "",
                        "created_at": "2026-04-19T10:52:15Z",
                        "order": 3,
                        "vault_folder": "",
                    },
                },
                "chats": {
                    "chat-cc-deadbeef": {
                        "project_id": "proj-cc-cli",
                        "title": "Imported CLI chat",
                        "model": "opus",
                        "provider": "claude",
                        "mode": "auto",
                        "session_id": "deadbeef-dead-beef-dead-beefdeadbeef",
                        "created_at": "2026-04-19T10:52:15Z",
                    },
                    "chat-normal": {
                        "project_id": "proj-manual",
                        "title": "Normal chat",
                        "model": "opus",
                        "provider": "claude",
                        "mode": "auto",
                        "session_id": "",
                        "created_at": "2026-04-19T10:52:15Z",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    pcm = _make_manager(tmp_path)

    projects = pcm.list_projects(workspace="personal")
    assert all(p.name != "Claude Code CLI" for p in projects)
    assert pcm.get_chat("chat-cc-deadbeef") is None
    assert pcm.get_chat("chat-normal") is not None

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert "proj-cc-cli" not in persisted["projects"]
    assert "chat-cc-deadbeef" not in persisted["chats"]


# ── auto-discovery: personal ───────────────────────────────────────────────


def test_discover_personal_single_file(tmp_path: Path) -> None:
    _make_personal_file(tmp_path, "Wedding", name="Wedding", description="Marriage prep")
    pcm = _make_manager(tmp_path)
    projects = pcm.list_projects(workspace="personal")
    wedding = next((p for p in projects if p.vault_folder == "Wedding"), None)
    assert wedding is not None, "single-file personal project was not auto-discovered"
    assert wedding.workspace == "personal"
    assert wedding.name == "Wedding"
    assert wedding.context == "Marriage prep"


def test_discover_personal_folder(tmp_path: Path) -> None:
    _make_personal_folder(tmp_path, "Upwordo", name="Upwordo", description="German learning app")
    pcm = _make_manager(tmp_path)
    projects = pcm.list_projects(workspace="personal")
    upw = next((p for p in projects if p.vault_folder == "Upwordo"), None)
    assert upw is not None
    assert upw.workspace == "personal"
    assert upw.context == "German learning app"


def test_discover_skips_gitkeep_and_dotfiles(tmp_path: Path) -> None:
    parent = tmp_path / "memory-vault" / "personal" / "projects" / "active"
    parent.mkdir(parents=True)
    (parent / ".gitkeep").write_text("", encoding="utf-8")
    (parent / ".hidden.md").write_text("---\nname: hidden\n---\n", encoding="utf-8")
    pcm = _make_manager(tmp_path)
    projects = pcm.list_projects(workspace="personal")
    # General is auto-managed and bound to its own vault folder; exclude it
    # when asserting that nothing else got picked up.
    discovered = [p for p in projects if p.vault_folder and p.name != "General"]
    assert discovered == [], f"expected no discovered projects, got {[p.name for p in discovered]}"


def test_personal_and_work_can_coexist(tmp_path: Path) -> None:
    _make_personal_file(tmp_path, "Wedding")
    _make_work_project_folder(tmp_path, "2026-q2-foo")
    pcm = _make_manager(tmp_path)
    # Exclude the auto-managed General projects (vault_folder="general").
    work = [p for p in pcm.list_projects(workspace="work")
            if p.vault_folder and p.name != "General"]
    personal = [p for p in pcm.list_projects(workspace="personal")
                if p.vault_folder and p.name != "General"]
    assert {p.vault_folder for p in work} == {"2026-q2-foo"}
    assert {p.vault_folder for p in personal} == {"Wedding"}


# ── adoption: a manually created PWA project picks up its vault entry ──────


def test_personal_unbound_project_adopts_matching_folder(tmp_path: Path) -> None:
    """A user-created PWA project with no vault_folder adopts the folder that
    appears later — does not create a duplicate entry."""
    pcm = _make_manager(tmp_path)
    manual = pcm.create_project("Wedding", workspace="personal")
    assert manual.vault_folder == ""

    _make_personal_folder(tmp_path, "Wedding")
    projects = pcm.list_projects(workspace="personal")
    wedding_entries = [p for p in projects if p.name == "Wedding"]
    assert len(wedding_entries) == 1, f"expected 1, got {wedding_entries}"
    assert wedding_entries[0].project_id == manual.project_id
    assert wedding_entries[0].vault_folder == "Wedding"


# ── pruning ────────────────────────────────────────────────────────────────


def test_prune_removes_personal_project_when_folder_deleted(tmp_path: Path) -> None:
    folder = _make_personal_folder(tmp_path, "Wedding")
    pcm = _make_manager(tmp_path)
    pcm.list_projects()  # triggers initial discovery
    assert any(p.vault_folder == "Wedding" for p in pcm.list_projects())

    import shutil as _shutil
    _shutil.rmtree(folder)
    pruned = pcm.list_projects()
    assert not any(p.vault_folder == "Wedding" for p in pruned), "expected prune"


def test_prune_keeps_personal_project_with_chats(tmp_path: Path) -> None:
    folder = _make_personal_folder(tmp_path, "Wedding")
    pcm = _make_manager(tmp_path)
    pcm.list_projects()
    wedding = next(p for p in pcm.list_projects() if p.vault_folder == "Wedding")
    pcm.create_chat(wedding.project_id, title="planning")

    import shutil as _shutil
    _shutil.rmtree(folder)
    survivors = pcm.list_projects()
    assert any(p.project_id == wedding.project_id for p in survivors), \
        "project with chats should survive pruning even if vault entry is gone"


# ── auto-promotion: orphan single-file → folder ────────────────────────────


def test_auto_promote_single_file_personal_to_folder(tmp_path: Path) -> None:
    """Stray ``Projects/active/<stem>.md`` files are promoted on init."""
    md = _make_personal_file(tmp_path, "Wedding")
    pcm = _make_manager(tmp_path)
    pcm.list_projects()

    # Source file is gone; <stem>/<stem>.md takes its place. Discovery picks
    # the project up under its folder form, vault_folder unchanged.
    assert not md.exists()
    promoted = tmp_path / "memory-vault" / "personal" / "projects" / "active" / "Wedding" / "Wedding.md"
    assert promoted.is_file()
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "Wedding")
    assert proj.name == "Wedding"


def test_auto_promote_skips_when_target_exists(tmp_path: Path) -> None:
    """If a folder already exists, the orphan file is left alone for manual
    intervention rather than guessing which one wins."""
    _make_personal_folder(tmp_path, "Wedding")  # folder takes precedence
    md = _make_personal_file(tmp_path, "Wedding")  # stray legacy file
    pcm = _make_manager(tmp_path)
    pcm.list_projects()

    # Both still on disk: promotion refused because Wedding/ exists.
    assert md.exists()
    assert (tmp_path / "memory-vault" / "personal" / "projects" / "active" / "Wedding").is_dir()


# ── complete_project: work (regression) ────────────────────────────────────


def test_complete_work_project_moves_folder(tmp_path: Path) -> None:
    folder = _make_work_project_folder(tmp_path, "2026-q2-foo")
    pcm = _make_manager(tmp_path)
    pcm.list_projects()  # discover
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "2026-q2-foo")

    result = pcm.complete_project(proj.project_id)
    assert result == {"ok": True, "vault_moved": True, "vault_folder": "2026-q2-foo"}

    assert not folder.exists(), "active folder should be gone"
    completed = tmp_path / "memory-vault" / "work" / "projects" / "completed" / "2026-q2-foo"
    assert completed.is_dir(), "folder should now be under completed/"
    main_md = completed / "2026-q2-foo.md"
    assert "status: completed" in main_md.read_text()
    assert proj.project_id not in pcm._projects, "PWA project should be deleted"


# ── complete_project: personal ─────────────────────────────────────────────


def test_complete_personal_promoted_file_moves_to_completed(tmp_path: Path) -> None:
    """A stray single-file project gets auto-promoted on init, then completes
    as a folder. Confirms the promotion + complete pipeline end-to-end."""
    md = _make_personal_file(tmp_path, "Wedding")
    pcm = _make_manager(tmp_path)
    pcm.list_projects()
    # Auto-promoted at startup.
    assert not md.exists()
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "Wedding")

    result = pcm.complete_project(proj.project_id)
    assert result["ok"] is True
    assert result["vault_moved"] is True
    assert result["vault_folder"] == "Wedding"

    moved_folder = tmp_path / "memory-vault" / "personal" / "projects" / "completed" / "Wedding"
    assert moved_folder.is_dir(), "folder should now be under completed/"
    main_md = moved_folder / "Wedding.md"
    assert "status: completed" in main_md.read_text()
    assert proj.project_id not in pcm._projects


def test_complete_personal_folder_moves_to_completed(tmp_path: Path) -> None:
    folder = _make_personal_folder(tmp_path, "Upwordo")
    pcm = _make_manager(tmp_path)
    pcm.list_projects()
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "Upwordo")

    result = pcm.complete_project(proj.project_id)
    assert result["vault_moved"] is True

    assert not folder.exists(), "active folder should be gone"
    moved_folder = tmp_path / "memory-vault" / "personal" / "projects" / "completed" / "Upwordo"
    assert moved_folder.is_dir()
    main_md = moved_folder / "Upwordo.md"
    assert "status: completed" in main_md.read_text()


def test_complete_no_vault_entry_still_deletes_pwa_project(tmp_path: Path) -> None:
    """If the vault entry was already moved/deleted, completing the PWA
    project should still succeed (vault_moved=False) and clean up state."""
    pcm = _make_manager(tmp_path)
    proj = pcm.create_project("Stale", workspace="personal")
    pcm.update_project(proj.project_id, vault_folder="Stale")

    result = pcm.complete_project(proj.project_id)
    assert result["vault_moved"] is False
    assert result["vault_folder"] == "Stale"
    assert proj.project_id not in pcm._projects


def test_complete_project_moves_configured_workspace_vault_folder(tmp_path: Path) -> None:
    config = _make_custom_workspace_config(tmp_path)
    _make_workspace_project_folder(tmp_path / "vaults" / "client", "acme", name="Acme")
    pcm = _make_manager(tmp_path, config)
    proj = next(p for p in pcm.list_projects("client") if p.vault_folder == "acme")

    result = pcm.complete_project(proj.project_id)

    assert result["vault_moved"] is True
    active = tmp_path / "vaults" / "client" / "projects" / "active" / "acme"
    completed = tmp_path / "vaults" / "client" / "projects" / "completed" / "acme"
    assert not active.exists()
    assert completed.is_dir()
    assert "status: completed" in (completed / "acme.md").read_text()
    assert proj.project_id not in pcm._projects


# ── list_completed_projects ─────────────────────────────────────────────────


def test_list_completed_projects_scans_both_workspaces(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    # Complete one project in each workspace so the completed/ trees populate.
    _make_work_project_folder(tmp_path, "2026-q2-foo", name="Q2 Foo")
    _make_personal_folder(tmp_path, "Upwordo", name="Upwordo", description="App")
    pcm.list_projects()
    work_proj = next(p for p in pcm.list_projects() if p.vault_folder == "2026-q2-foo")
    pers_proj = next(p for p in pcm.list_projects() if p.vault_folder == "Upwordo")
    pcm.complete_project(work_proj.project_id)
    pcm.complete_project(pers_proj.project_id)

    all_completed = pcm.list_completed_projects()
    by_stem = {c["stem"]: c for c in all_completed}
    assert by_stem["2026-q2-foo"]["workspace"] == "work"
    assert by_stem["2026-q2-foo"]["name"] == "Q2 Foo"
    assert by_stem["2026-q2-foo"]["vault_doc_path"] == (
        "memory-vault/work/projects/completed/2026-q2-foo/README.md"
    )
    assert by_stem["Upwordo"]["workspace"] == "personal"
    assert by_stem["Upwordo"]["context"] == "App"
    assert by_stem["Upwordo"]["vault_doc_path"] == (
        "memory-vault/personal/projects/completed/Upwordo/README.md"
    )

    # Workspace filter scopes the scan.
    work_only = pcm.list_completed_projects("work")
    assert {c["stem"] for c in work_only} == {"2026-q2-foo"}


def test_list_completed_projects_empty(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    assert pcm.list_completed_projects() == []


def test_list_completed_projects_scans_configured_workspace(tmp_path: Path) -> None:
    config = _make_custom_workspace_config(tmp_path)
    _make_workspace_project_folder(
        tmp_path / "vaults" / "client",
        "done",
        name="Done",
        description="Finished client work",
        status="completed",
    )
    pcm = _make_manager(tmp_path, config)

    completed = pcm.list_completed_projects("client")

    assert completed == [
        {
            "stem": "done",
            "name": "Done",
            "context": "Finished client work",
            "workspace": "client",
            "vault_doc_path": "vaults/client/projects/completed/done/README.md",
        }
    ]


# ── restore_project ─────────────────────────────────────────────────────────


def test_restore_work_project_round_trip(tmp_path: Path) -> None:
    _make_work_project_folder(tmp_path, "2026-q2-foo", name="Q2 Foo")
    pcm = _make_manager(tmp_path)
    pcm.list_projects()
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "2026-q2-foo")
    pcm.complete_project(proj.project_id)
    assert proj.project_id not in pcm._projects

    result = pcm.restore_project("work", "2026-q2-foo")
    assert result["ok"] is True
    assert result["project"] is not None
    assert result["project"]["vault_folder"] == "2026-q2-foo"

    # Folder is back under active/ with status flipped, and the PWA project
    # has been recreated via discovery.
    active = tmp_path / "memory-vault" / "work" / "projects" / "active" / "2026-q2-foo"
    completed = tmp_path / "memory-vault" / "work" / "projects" / "completed" / "2026-q2-foo"
    assert active.is_dir()
    assert not completed.exists()
    assert "status: active" in (active / "2026-q2-foo.md").read_text()
    assert any(p.vault_folder == "2026-q2-foo" for p in pcm.list_projects(workspace="work"))


def test_restore_configured_workspace_project(tmp_path: Path) -> None:
    config = _make_custom_workspace_config(tmp_path)
    _make_workspace_project_folder(
        tmp_path / "vaults" / "client",
        "done",
        name="Done",
        status="completed",
    )
    pcm = _make_manager(tmp_path, config)

    result = pcm.restore_project("client", "done")

    assert result["ok"] is True
    assert result["project"] is not None
    assert result["project"]["workspace"] == "client"
    assert result["project"]["vault_folder"] == "done"
    active = tmp_path / "vaults" / "client" / "projects" / "active" / "done"
    completed = tmp_path / "vaults" / "client" / "projects" / "completed" / "done"
    assert active.is_dir()
    assert not completed.exists()
    assert "status: active" in (active / "done.md").read_text()


def test_restore_missing_folder_raises(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    with pytest.raises(ValueError, match="not found"):
        pcm.restore_project("work", "does-not-exist")


def test_restore_rejects_bad_workspace_and_stem(tmp_path: Path) -> None:
    pcm = _make_manager(tmp_path)
    with pytest.raises(ValueError, match="Invalid workspace"):
        pcm.restore_project("nonsense", "foo")
    with pytest.raises(ValueError, match="Invalid project folder"):
        pcm.restore_project("work", "../escape")


def test_restore_refuses_when_active_folder_exists(tmp_path: Path) -> None:
    """A completed stem that collides with an existing active folder must not
    clobber the live project."""
    _make_work_project_folder(tmp_path, "dup", name="Dup")
    pcm = _make_manager(tmp_path)
    pcm.list_projects()
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "dup")
    pcm.complete_project(proj.project_id)
    # Recreate an active folder with the same stem after completion.
    _make_work_project_folder(tmp_path, "dup", name="Dup Again")

    with pytest.raises(ValueError, match="already exists"):
        pcm.restore_project("work", "dup")
