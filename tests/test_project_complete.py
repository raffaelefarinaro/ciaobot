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
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.memory_pass import MemoryPassCoordinator
import ciao.web.project_chats as project_chats_module
from ciao.web.project_chats import ProjectChatManager, ProjectInfo


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_memory_pass_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests run without an event loop, so a queued closure pass must not start its turn."""
    monkeypatch.setattr(MemoryPassCoordinator, "pump", lambda self, workspace: None)


def _make_manager(tmp_path: Path, config: CiaoConfig | None = None) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    if config is None:
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
            ),
            "client": WorkspaceConfig(
                name="client",
                vault_root="vaults/client",
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


def test_unavailable_adopted_vault_is_not_recreated_or_pruned_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _make_custom_workspace_config(tmp_path)
    monkeypatch.setattr(
        CiaoConfig, "adopted_workspace_vault",
        lambda self, name: name == "client",
    )
    pcm = _make_manager(tmp_path, config)
    client = ProjectInfo(
        "client-project", "Client project", "client", vault_folder="kept"
    )
    local = ProjectInfo("local-project", "Local project", "home", vault_folder="gone")
    pcm._projects[client.project_id] = client
    pcm._projects[local.project_id] = local

    pcm._discover_vault_projects()

    assert pcm._projects[client.project_id] is client
    assert local.project_id not in pcm._projects
    assert not config.workspace_vault_root("client").exists()
    pcm._scaffold_project_note(
        ProjectInfo("new-client-project", "New client project", "client")
    )
    assert not config.workspace_vault_root("client").exists()

    restored = config.workspace_vault_root("client") / "projects" / "active" / "kept"
    restored.mkdir(parents=True)
    (restored / "README.md").write_text("---\nname: Client project\n---\n", encoding="utf-8")
    pcm._discover_vault_projects()
    assert pcm._projects[client.project_id].vault_folder == "kept"


def test_adopted_vault_permission_error_does_not_prune(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_custom_workspace_config(tmp_path)
    monkeypatch.setattr(
        CiaoConfig, "adopted_workspace_vault",
        lambda self, name: name == "client",
    )
    pcm = _make_manager(tmp_path, config)
    project = ProjectInfo("client-project", "Client project", "client", vault_folder="kept")
    pcm._projects[project.project_id] = project
    original = pcm._iter_vault_entries

    def scan(workspace: str, root: Path, *, strict: bool = False):
        if workspace == "client":
            raise PermissionError("cloud folder unavailable")
        return original(workspace, root, strict=strict)

    monkeypatch.setattr(pcm, "_iter_vault_entries", scan)
    pcm._discover_vault_projects()

    assert pcm._projects[project.project_id] is project
    assert not config.workspace_vault_root("client").exists()


def test_partial_adopted_vault_scan_does_not_prune(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _make_custom_workspace_config(tmp_path)
    monkeypatch.setattr(
        CiaoConfig, "adopted_workspace_vault",
        lambda self, name: name == "client",
    )
    pcm = _make_manager(tmp_path, config)
    project = ProjectInfo("client-project", "Client project", "client", vault_folder="kept")
    pcm._projects[project.project_id] = project
    active = pcm._vault_active_root("client")
    active.mkdir(parents=True)
    original_iterdir = Path.iterdir

    def interrupted_iterdir(path: Path):
        if path == active:
            def partial_scan():
                yield active / "another-project"
                raise OSError("scan interrupted while cloud files materialize")
            return partial_scan()
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", interrupted_iterdir)
    pcm._discover_vault_projects()

    assert pcm._projects[project.project_id] is project


def test_create_scaffold_fails_without_adopted_note_and_does_not_recreate_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _make_custom_workspace_config(tmp_path)
    monkeypatch.setattr(CiaoConfig, "adopted_workspace_vault", lambda self, name: name == "client")
    pcm = _make_manager(tmp_path, config)

    with pytest.raises(ValueError, match="notes folder is unavailable"):
        pcm.create_project("Needs note", workspace="client", scaffold=True)

    assert not config.workspace_vault_root("client").exists()
    assert not any(p.name == "Needs note" for p in pcm._projects.values())


def test_create_scaffold_fails_on_permission_error_while_adopted_root_remains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    config = _make_custom_workspace_config(tmp_path)
    monkeypatch.setattr(CiaoConfig, "adopted_workspace_vault", lambda self, name: name == "client")
    pcm = _make_manager(tmp_path, config)
    root = config.workspace_vault_root("client")
    root.mkdir(parents=True)
    original_mkdir = os.mkdir

    def denied_projects(name, *args, **kwargs):
        if name == "projects":
            raise PermissionError("cloud root is mounted read-only")
        return original_mkdir(name, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", denied_projects)
    with pytest.raises(ValueError, match="project note could not be created"):
        pcm.create_project("Read Only", workspace="client", scaffold=True)

    assert root.is_dir()
    assert not (root / "projects").exists()
    assert not any(p.name == "Read Only" for p in pcm._projects.values())


def test_directory_chain_rejects_symlink_in_root_path(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        project_chats_module._open_directory_chain(
            linked_parent / "vault", (), create=False
        )

    assert list(outside.iterdir()) == []


def test_directory_chain_rejects_root_component_replaced_at_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    root = tmp_path / "vault-root"
    root.mkdir()
    held = tmp_path / "vault-root-held"
    outside = tmp_path / "outside"
    outside.mkdir()
    original_open = os.open
    swapped = False

    def replace_component(name, flags, *args, **kwargs):
        nonlocal swapped
        if name == root.name and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            root.rename(held)
            root.symlink_to(outside, target_is_directory=True)
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_component)
    with pytest.raises(OSError):
        project_chats_module._open_directory_chain(root, (), create=False)

    assert swapped
    assert list(outside.iterdir()) == []


def test_project_scaffold_keeps_concurrent_materialized_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _make_custom_workspace_config(tmp_path)
    monkeypatch.setattr(CiaoConfig, "adopted_workspace_vault", lambda self, name: name == "client")
    pcm = _make_manager(tmp_path, config)
    root = config.workspace_vault_root("client")
    root.mkdir(parents=True)
    active = root / "projects" / "active"
    active.mkdir(parents=True)
    folder = active / "race-project"
    import os

    original_mkdir = os.mkdir

    def sync_wins(name, *args, **kwargs):
        if name == "race-project":
            original_mkdir(name, *args, **kwargs)
            (folder / "README.md").write_text("sync-owned", encoding="utf-8")
            raise FileExistsError(name)
        else:
            original_mkdir(name, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", sync_wins)
    project = pcm.create_project("Race Project", workspace="client", scaffold=True)

    assert (folder / "README.md").read_text(encoding="utf-8") == "sync-owned"
    assert not (folder / "race-project.md").exists()
    assert project.vault_folder == "race-project"


def test_project_scaffold_does_not_recreate_adopted_root_removed_after_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shutil
    import os

    config = _make_custom_workspace_config(tmp_path)
    monkeypatch.setattr(CiaoConfig, "adopted_workspace_vault", lambda self, name: name == "client")
    pcm = _make_manager(tmp_path, config)
    root = config.workspace_vault_root("client")
    (root / "projects" / "active").mkdir(parents=True)
    folder = root / "projects" / "active" / "vanishing-root"
    original_mkdir = os.mkdir

    def root_disappears(name, *args, **kwargs):
        if name == "vanishing-root":
            shutil.rmtree(root)
        return original_mkdir(name, *args, **kwargs)

    monkeypatch.setattr(os, "mkdir", root_disappears)
    with pytest.raises(ValueError, match="notes folder is unavailable"):
        pcm.create_project("Vanishing Root", workspace="client", scaffold=True)
    assert not root.exists()


def test_project_scaffold_uses_opened_active_directory_after_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    config = _make_custom_workspace_config(tmp_path)
    monkeypatch.setattr(CiaoConfig, "adopted_workspace_vault", lambda self, name: name == "client")
    pcm = _make_manager(tmp_path, config)
    root = config.workspace_vault_root("client")
    active = root / "projects" / "active"
    active.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    original_open = os.open
    swapped = False

    def swap_after_active_open(name, flags, *args, **kwargs):
        nonlocal swapped
        fd = original_open(name, flags, *args, **kwargs)
        if name == "active" and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            active.rename(root / "projects" / "active-held")
            active.symlink_to(outside, target_is_directory=True)
        return fd

    monkeypatch.setattr(os, "open", swap_after_active_open)
    with pytest.raises(ValueError, match="notes folder is unavailable"):
        pcm.create_project("Race Project", workspace="client", scaffold=True)

    assert list(outside.iterdir()) == []
    assert swapped
    held = root / "projects" / "active-held" / "race-project"
    assert held.is_dir()
    assert not (held / "race-project.md").exists()
    assert not any(p.name == "Race Project" for p in pcm._projects.values())


def test_project_scaffold_rejects_slug_directory_replaced_after_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _make_custom_workspace_config(tmp_path)
    monkeypatch.setattr(CiaoConfig, "adopted_workspace_vault", lambda self, name: name == "client")
    pcm = _make_manager(tmp_path, config)
    root = config.workspace_vault_root("client")
    active = root / "projects" / "active"
    active.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    original_same_child = project_chats_module._same_child_directory
    swapped = False

    def swap_slug_after_open(parent_fd, name, expected_fd):
        nonlocal swapped
        if name == "race-project" and not swapped:
            swapped = True
            folder = active / "race-project"
            folder.rename(active / "race-project-held")
            folder.symlink_to(outside, target_is_directory=True)
        return original_same_child(parent_fd, name, expected_fd)

    monkeypatch.setattr(
        project_chats_module, "_same_child_directory", swap_slug_after_open
    )
    with pytest.raises(ValueError, match="project note could not be created"):
        pcm.create_project("Race Project", workspace="client", scaffold=True)

    assert swapped
    assert list(outside.iterdir()) == []
    assert not any(p.name == "Race Project" for p in pcm._projects.values())
    assert (active / "race-project-held").is_dir()
    assert not (active / "race-project-held" / "race-project.md").exists()


def test_general_seed_never_overwrites_a_note_materialized_at_write_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _make_custom_workspace_config(tmp_path)
    pcm = _make_manager(tmp_path, config)
    folder = pcm._vault_active_root("client") / "general"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "general.md"
    target.unlink(missing_ok=True)
    original_rename = project_chats_module._publish_exclusive_at
    raced = False

    def sync_wins(src_fd, source, dst_fd, destination, *, expected_fd):
        nonlocal raced
        if destination == "general.md" and not raced:
            raced = True
            external_fd = os.open(
                destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666, dir_fd=dst_fd,
            )
            with os.fdopen(external_fd, "w", encoding="utf-8") as stream:
                stream.write("sync-owned")
        return original_rename(src_fd, source, dst_fd, destination, expected_fd=expected_fd)

    monkeypatch.setattr(project_chats_module, "_publish_exclusive_at", sync_wins)
    pcm._ensure_general_vault_folder("client")

    assert target.read_text(encoding="utf-8") == "sync-owned"


# ── auto-promotion: orphan single-file → folder ────────────────────────────


def test_auto_promote_single_file_personal_to_folder(tmp_path: Path) -> None:
    """Stray ``Projects/active/<stem>.md`` files are promoted on init."""
    md = _make_personal_file(tmp_path, "Wedding")
    pcm = _make_manager(tmp_path)
    pcm.list_projects()

    # Source is retained; <stem>/<stem>.md is copied. Discovery picks
    # the project up under its folder form, vault_folder unchanged.
    assert md.is_file()
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


def test_personal_promotion_keeps_source_when_sync_wins_destination_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pcm = _make_manager(tmp_path)
    source = _make_personal_file(tmp_path, "Wedding")
    folder = source.parent / "Wedding"
    original_rename = project_chats_module._publish_exclusive_at

    def sync_wins(src_fd, src, dst_fd, dst, *, expected_fd):
        if dst == "Wedding.md":
            fd = os.open(
                dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666, dir_fd=dst_fd,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write("sync-owned")
        return original_rename(src_fd, src, dst_fd, dst, expected_fd=expected_fd)

    monkeypatch.setattr(project_chats_module, "_publish_exclusive_at", sync_wins)
    pcm._promote_single_file_personal_projects()

    assert source.read_text(encoding="utf-8").startswith("---")
    assert (folder / "Wedding.md").read_text(encoding="utf-8") == "sync-owned"


def test_personal_promotion_keeps_source_when_slug_is_replaced_at_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pcm = _make_manager(tmp_path)
    source = _make_personal_file(tmp_path, "Wedding")
    outside = tmp_path / "outside"
    outside.mkdir()
    original = project_chats_module._publish_exclusive_at
    swapped = False

    def replace_slug(src_fd, src, dst_fd, dst, *, expected_fd):
        nonlocal swapped
        if dst == "Wedding.md" and not swapped:
            swapped = True
            folder = source.parent / "Wedding"
            folder.rename(source.parent / "Wedding-held")
            folder.symlink_to(outside, target_is_directory=True)
        return original(src_fd, src, dst_fd, dst, expected_fd=expected_fd)

    monkeypatch.setattr(project_chats_module, "_publish_exclusive_at", replace_slug)
    pcm._promote_single_file_personal_projects()

    assert swapped
    assert source.is_file()
    assert list(outside.iterdir()) == []
    assert (source.parent / "Wedding-held" / "Wedding.md").is_file()


def test_personal_promotion_keeps_source_replaced_after_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pcm = _make_manager(tmp_path)
    source = _make_personal_file(tmp_path, "Wedding")
    original_rename = project_chats_module._publish_exclusive_at
    replaced = False

    def sync_replaces_source(src_fd, src, dst_fd, dst, *, expected_fd):
        nonlocal replaced
        if dst == "Wedding.md" and not replaced:
            replaced = True
            concurrent = source.with_name("Wedding.sync-tmp")
            concurrent.write_text("new sync version", encoding="utf-8")
            os.replace(concurrent, source)
        return original_rename(src_fd, src, dst_fd, dst, expected_fd=expected_fd)

    monkeypatch.setattr(project_chats_module, "_publish_exclusive_at", sync_replaces_source)
    pcm._promote_single_file_personal_projects()

    assert replaced
    promoted = source.parent / "Wedding" / "Wedding.md"
    assert source.read_text(encoding="utf-8") == "new sync version"
    assert promoted.read_text(encoding="utf-8").startswith("---")


def test_personal_promotion_error_does_not_block_other_workspace_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pcm = _make_manager(tmp_path)
    work_folder = _make_work_project_folder(tmp_path, "kept-project")
    original_open_chain = project_chats_module._open_directory_chain
    personal_root = pcm._workspace_vault_root("personal")

    def fail_personal_active(root: Path, parts, *, create: bool):
        if root == personal_root and parts == ("projects", "active"):
            raise PermissionError("personal cloud folder is unavailable")
        return original_open_chain(root, parts, create=create)

    monkeypatch.setattr(project_chats_module, "_open_directory_chain", fail_personal_active)
    pcm._discover_vault_projects()

    assert work_folder.is_dir()
    assert any(p.vault_folder == "kept-project" for p in pcm._projects.values())


# ── complete_project: work (regression) ────────────────────────────────────


def test_complete_work_project_moves_folder(tmp_path: Path) -> None:
    folder = _make_work_project_folder(tmp_path, "2026-q2-foo")
    pcm = _make_manager(tmp_path)
    pcm.list_projects()  # discover
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "2026-q2-foo")

    result = pcm.complete_project(proj.project_id)
    assert result["ok"] is True
    assert result["vault_moved"] is True
    assert result["vault_folder"] == "2026-q2-foo"
    assert result["outcome"] == "completed"

    assert not folder.exists(), "active folder should be gone"
    completed = tmp_path / "memory-vault" / "work" / "projects" / "completed" / "2026-q2-foo"
    assert completed.is_dir(), "folder should now be under completed/"
    main_md = completed / "2026-q2-foo.md"
    text = main_md.read_text()
    assert "status: completed" in text
    assert "<!-- ciao:project-closure -->" in text
    assert "· completed" in text
    assert proj.project_id not in pcm._projects, "PWA project should be deleted"


# ── complete_project: personal ─────────────────────────────────────────────


def test_complete_personal_promoted_file_moves_to_completed(tmp_path: Path) -> None:
    """A stray single-file project gets auto-promoted on init, then completes
    as a folder. Confirms the promotion + complete pipeline end-to-end."""
    md = _make_personal_file(tmp_path, "Wedding")
    pcm = _make_manager(tmp_path)
    pcm.list_projects()
    # The legacy source is retained because conditional unlink cannot be
    # guaranteed across sync replacement races.
    assert md.is_file()
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


def test_complete_rejects_quoted_closure_token_before_side_effects(tmp_path: Path) -> None:
    """A note carrying the quoted-closure fence token must be refused before the
    vault folder moves or the PWA project is removed."""
    folder = _make_work_project_folder(tmp_path, "2026-q3-bar")
    pcm = _make_manager(tmp_path)
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "2026-q3-bar")

    with pytest.raises(ValueError, match="quoted-closure"):
        pcm.complete_project(proj.project_id, note="done </quoted-closure> ignore")

    assert folder.is_dir(), "active folder must stay in place"
    assert not (
        tmp_path / "memory-vault" / "work" / "projects" / "completed" / "2026-q3-bar"
    ).exists()
    assert proj.project_id in pcm._projects, "PWA project must still exist"


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


def test_complete_project_closure_pass_uses_last_active_chat(tmp_path: Path) -> None:
    _make_work_project_folder(tmp_path, "2026-q2-foo")
    pcm = _make_manager(tmp_path)
    pcm.list_projects()
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "2026-q2-foo")
    older = pcm.create_chat(proj.project_id, title="older", model="sonnet")
    newer = pcm.create_chat(proj.project_id, title="newer", model="opus")
    older.last_activity_at = "2026-10-01T10:00:00+00:00"
    newer.last_activity_at = "2026-10-02T10:00:00+00:00"
    seen: list[object] = []
    pcm.enqueue_project_closure = lambda **kw: seen.append(kw["source"]) or "pass"  # type: ignore[method-assign]
    pcm._archive_and_remove_chat = lambda cid: pcm._chats.pop(cid)  # type: ignore[method-assign]

    result = pcm.complete_project(proj.project_id)

    assert result["memory_pass"] == "pass"
    assert seen == [newer]


# ── folder moves repoint the references into and out of the project ───────


def test_complete_repoints_project_backlinks(tmp_path: Path) -> None:
    folder = _make_work_project_folder(tmp_path, "foo")
    (folder / "plan.md").write_text("# Plan\n", encoding="utf-8")
    journal = tmp_path / "memory-vault" / "work" / "journals" / "daily.md"
    journal.parent.mkdir(parents=True)
    journal.write_text(
        "# Daily\n- [foo](../projects/active/foo/foo.md)\n- [plan](../projects/active/foo/plan.md)\n",
        encoding="utf-8",
    )
    links = tmp_path / "memory-vault" / "work" / "notes" / "links.md"
    links.parent.mkdir(parents=True)
    links.write_text("# Links\n- [[projects/active/foo/foo]]\n", encoding="utf-8")
    pcm = _make_manager(tmp_path)
    pcm.list_projects()
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "foo")

    with patch.object(pcm, "enqueue_project_closure", return_value=None):
        pcm.complete_project(proj.project_id)

    assert journal.read_text(encoding="utf-8") == (
        "# Daily\n- [foo](../projects/completed/foo/foo.md)\n- [plan](../projects/completed/foo/plan.md)\n"
    )
    assert links.read_text(encoding="utf-8") == "# Links\n- [[projects/completed/foo/foo]]\n"


def test_restore_repoints_backlinks_back_to_active(tmp_path: Path) -> None:
    _make_work_project_folder(tmp_path, "foo")
    journal = tmp_path / "memory-vault" / "work" / "journals" / "daily.md"
    journal.parent.mkdir(parents=True)
    journal.write_text("# Daily\n- [foo](../projects/active/foo/foo.md)\n", encoding="utf-8")
    pcm = _make_manager(tmp_path)
    pcm.list_projects()
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "foo")
    with patch.object(pcm, "enqueue_project_closure", return_value=None):
        pcm.complete_project(proj.project_id)
    assert journal.read_text(encoding="utf-8") == "# Daily\n- [foo](../projects/completed/foo/foo.md)\n"
    with journal.open("a", encoding="utf-8") as handle:
        handle.write("- appended while completed\n")

    pcm.restore_project("work", "foo")

    assert journal.read_text(encoding="utf-8") == (
        "# Daily\n- [foo](../projects/active/foo/foo.md)\n- appended while completed\n"
    )


def test_project_move_repoints_links_from_moving_notes(tmp_path: Path) -> None:
    # `[o](../other/other.md)` names no moved stem, so only the rule that every
    # moving note is planned can select it for the rewrite.
    _make_work_project_folder(tmp_path, "other")
    folder = _make_work_project_folder(tmp_path, "foo")
    (folder / "plan.md").write_text("# Other\n[o](../other/other.md)", encoding="utf-8")
    pcm = _make_manager(tmp_path)
    pcm.list_projects()
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "foo")

    with patch.object(pcm, "enqueue_project_closure", return_value=None):
        pcm.complete_project(proj.project_id)

    completed_plan = tmp_path / "memory-vault" / "work" / "projects" / "completed" / "foo" / "plan.md"
    assert completed_plan.read_text(encoding="utf-8") == "# Other\n[o](../../active/other/other.md)"


def test_complete_move_failure_leaves_backlinks_and_project(tmp_path: Path) -> None:
    folder = _make_work_project_folder(tmp_path, "foo")
    journal = tmp_path / "memory-vault" / "work" / "journals" / "daily.md"
    journal.parent.mkdir(parents=True)
    journal.write_text("# Daily\n- [foo](../projects/active/foo/foo.md)\n", encoding="utf-8")
    before = journal.read_bytes()
    completed = tmp_path / "memory-vault" / "work" / "projects" / "completed"
    assert not completed.exists()
    pcm = _make_manager(tmp_path)
    pcm.list_projects()
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "foo")

    with (
        patch.object(pcm, "enqueue_project_closure", return_value=None),
        patch("ciao.vault_review.shutil.move", side_effect=OSError("disk full")),
        pytest.raises(ValueError),
    ):
        pcm.complete_project(proj.project_id)

    # The closure section is appended to the project doc before the move, and a
    # failed move does not take it back out. That is pre-existing behaviour and out
    # of scope here, so the project doc is deliberately not compared byte-for-byte.
    assert journal.read_bytes() == before
    assert folder.is_dir()
    assert not completed.exists()
    assert proj.project_id in pcm._projects


def test_complete_refuses_existing_destination_without_touching_the_project(tmp_path: Path) -> None:
    folder = _make_work_project_folder(tmp_path, "foo")
    completed = tmp_path / "memory-vault" / "work" / "projects" / "completed" / "foo"
    completed.mkdir(parents=True)
    before = (folder / "foo.md").read_bytes()
    pcm = _make_manager(tmp_path)
    pcm.list_projects()
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "foo")

    with patch.object(pcm, "enqueue_project_closure", return_value=None), pytest.raises(ValueError):
        pcm.complete_project(proj.project_id)

    assert (folder / "foo.md").read_bytes() == before
    assert proj.project_id in pcm._projects


def test_complete_repoints_backlinks_in_unheaded_notes(tmp_path: Path) -> None:
    # A journal with neither frontmatter nor an H1 is a placeholder to the
    # index, but it still links the project and must follow the move.
    _make_work_project_folder(tmp_path, "foo")
    journal = tmp_path / "memory-vault" / "work" / "journals" / "daily.md"
    journal.parent.mkdir(parents=True)
    journal.write_text("[foo](../projects/active/foo/foo.md)\n", encoding="utf-8")
    # A moving note of the same shape must be re-spelled too: its link to a
    # sibling project would otherwise break once it leaves active/.
    _make_work_project_folder(tmp_path, "other")
    scratch = tmp_path / "memory-vault" / "work" / "projects" / "active" / "foo" / "scratch.md"
    scratch.write_text("[o](../other/other.md)\n", encoding="utf-8")
    pcm = _make_manager(tmp_path)
    pcm.list_projects()
    proj = next(p for p in pcm.list_projects() if p.vault_folder == "foo")

    with patch.object(pcm, "enqueue_project_closure", return_value=None):
        pcm.complete_project(proj.project_id)

    text = journal.read_text(encoding="utf-8")
    assert "projects/completed/foo/foo.md" in text and "projects/active/foo" not in text
    completed_scratch = tmp_path / "memory-vault" / "work" / "projects" / "completed" / "foo" / "scratch.md"
    assert completed_scratch.read_text(encoding="utf-8") == "[o](../../active/other/other.md)\n"
