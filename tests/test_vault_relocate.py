"""Fixture coverage for relocating one workspace's vault to its standard folder.

Two shapes: the vault sits at some other path entirely (whole_directory) and
the vault IS the shared vault root itself, with the workspace's own content
loose at the top level alongside other workspaces' folders and vault-wide
shared state (Logs, Templates, generated indexes).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.vault_relocate import apply, plan, receipt_path, undo


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )
    return (proc.stdout + proc.stderr).strip()


def _config(tmp_path: Path, workspaces: dict[str, str]) -> CiaoConfig:
    return CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=root)
            for name, root in workspaces.items()
        },
    )


def _git_install(tmp_path: Path) -> Path:
    install = tmp_path / "install"
    install.mkdir()
    _git(install, "init", "-b", "main")
    _git(install, "config", "user.email", "test@example.com")
    _git(install, "config", "user.name", "Test")
    (install / ".runtime").mkdir()
    (install / ".gitignore").write_text(".runtime/\n", encoding="utf-8")
    return install


def _commit_all(install: Path, message: str = "seed") -> None:
    _git(install, "add", "-A")
    _git(install, "commit", "-m", message)


def _write_registry(install: Path, entries: list[dict]) -> None:
    (install / ".runtime" / "workspaces.json").write_text(
        json.dumps(entries, indent=2) + "\n", encoding="utf-8"
    )


# -- shared-root shape: vault_root doubles as one workspace's own folder ----


def _shared_root_install(tmp_path: Path, *, symlink: bool = False) -> tuple[Path, CiaoConfig]:
    """scandit's vault sits directly at vault_root, alongside a sibling
    workspace's already-nested folder and vault-wide shared state — the exact
    shape the housekeeping card's screenshot showed."""
    install = _git_install(tmp_path)
    vault_root = install / "memory-vault"
    (vault_root / "People").mkdir(parents=True)
    (vault_root / "People" / "Peter.md").write_text("# Peter\n", encoding="utf-8")
    (vault_root / "Projects").mkdir()
    (vault_root / "Projects" / "roadmap.md").write_text("# Roadmap\n", encoding="utf-8")
    (vault_root / "other").mkdir()  # a sibling workspace, already nested correctly
    (vault_root / "other" / "note.md").write_text("# Other\n", encoding="utf-8")
    (vault_root / "Logs").mkdir()
    (vault_root / "Logs" / "chat.md").write_text("log\n", encoding="utf-8")
    (vault_root / "INDEX.md").write_text("generated\n", encoding="utf-8")
    if symlink:
        (vault_root / "External").symlink_to(tmp_path)
    _write_registry(
        install,
        [
            {"name": "scandit", "vault_root": str(vault_root)},
            {"name": "other", "vault_root": "memory-vault/other"},
        ],
    )
    _commit_all(install)
    config = _config(
        tmp_path / "install",
        {"scandit": str(vault_root), "other": "memory-vault/other"},
    )
    return install, config


def test_plan_classifies_shared_root_entries(tmp_path: Path) -> None:
    install, config = _shared_root_install(tmp_path)

    result = plan(config, "scandit")

    assert not result.whole_directory
    by_name = {entry.name: entry.action for entry in result.entries}
    assert by_name["People"] == "move"
    assert by_name["Projects"] == "move"
    assert by_name["other"] == "skip"
    assert by_name["Logs"] == "skip"
    assert by_name["INDEX.md"] == "skip"
    assert not result.refused


def test_plan_refuses_on_an_unclassifiable_symlink(tmp_path: Path) -> None:
    install, config = _shared_root_install(tmp_path, symlink=True)

    result = plan(config, "scandit")

    assert result.refused
    unclassified = {entry.name for entry in result.entries if entry.action == "unclassified"}
    assert unclassified == {"External"}


def test_plan_refuses_when_already_at_standard_location(tmp_path: Path) -> None:
    install, _ = _shared_root_install(tmp_path)
    config = _config(tmp_path / "install", {"scandit": "memory-vault/scandit"})

    result = plan(config, "scandit")

    assert result.refused
    assert "already at its standard location" in result.refusals[0]


def test_apply_moves_workspace_content_and_leaves_shared_state(tmp_path: Path) -> None:
    install, config = _shared_root_install(tmp_path)
    runtime = install / ".runtime"

    result = apply(config, "scandit", runtime)

    assert result["status"] == "relocated", result.get("refusals")
    destination = install / "memory-vault" / "scandit"
    assert (destination / "People" / "Peter.md").is_file()
    assert (destination / "Projects" / "roadmap.md").is_file()
    # Shared state and the sibling workspace were left exactly where they were.
    assert (install / "memory-vault" / "Logs" / "chat.md").is_file()
    assert (install / "memory-vault" / "INDEX.md").is_file()
    assert (install / "memory-vault" / "other" / "note.md").is_file()
    # The registry now points scandit at the standard folder.
    entries = json.loads((runtime / "workspaces.json").read_text(encoding="utf-8"))
    scandit_entry = next(e for e in entries if e["name"] == "scandit")
    assert scandit_entry["vault_root"] == "memory-vault/scandit"
    # The in-memory config sees it immediately too.
    assert config.workspace("scandit").vault_root == "memory-vault/scandit"
    assert receipt_path(runtime, "scandit").is_file()


def test_apply_preserves_history_so_git_log_follow_works(tmp_path: Path) -> None:
    install, config = _shared_root_install(tmp_path)
    runtime = install / ".runtime"

    apply(config, "scandit", runtime)
    _git(install, "add", "-A")
    _git(install, "commit", "-m", "relocate")

    log = _git(
        install, "log", "--follow", "--oneline", "--",
        "memory-vault/scandit/People/Peter.md",
    )
    assert "seed" in log, "history did not follow the move"


def test_apply_refuses_on_modified_tracked_file(tmp_path: Path) -> None:
    install, config = _shared_root_install(tmp_path)
    (install / "memory-vault" / "People" / "Peter.md").write_text("edited\n", encoding="utf-8")
    runtime = install / ".runtime"

    result = apply(config, "scandit", runtime)

    assert result["status"] == "refused"
    assert any("uncommitted" in r for r in result["refusals"])
    assert (install / "memory-vault" / "People").is_dir(), "it moved something despite refusing"


def test_apply_refuses_when_plan_has_unclassified_entries(tmp_path: Path) -> None:
    install, config = _shared_root_install(tmp_path, symlink=True)
    runtime = install / ".runtime"

    result = apply(config, "scandit", runtime)

    assert result["status"] == "refused"
    assert (install / "memory-vault" / "People").is_dir()


def test_apply_then_undo_restores_a_byte_identical_tree_and_registry(tmp_path: Path) -> None:
    install, config = _shared_root_install(tmp_path)
    runtime = install / ".runtime"
    before_registry = json.loads((runtime / "workspaces.json").read_text(encoding="utf-8"))

    applied = apply(config, "scandit", runtime)
    assert applied["status"] == "relocated"

    result = undo(config, "scandit", runtime)

    assert result["status"] == "undone", result
    assert (install / "memory-vault" / "People" / "Peter.md").is_file()
    assert not (install / "memory-vault" / "scandit").exists()
    after_registry = json.loads((runtime / "workspaces.json").read_text(encoding="utf-8"))
    assert after_registry == before_registry
    assert config.workspace("scandit").vault_root == str(install / "memory-vault")


def test_undo_with_no_receipt_is_a_no_op(tmp_path: Path) -> None:
    install, config = _shared_root_install(tmp_path)
    runtime = install / ".runtime"

    result = undo(config, "scandit", runtime)

    assert result["status"] == "nothing_to_undo"


# -- whole-directory shape: the vault lives at an unrelated path ------------


def _pinned_install(tmp_path: Path) -> tuple[Path, CiaoConfig]:
    install = _git_install(tmp_path)
    legacy = install / "legacy-personal-notes"
    (legacy / "People").mkdir(parents=True)
    (legacy / "People" / "Peter.md").write_text("# Peter\n", encoding="utf-8")
    _write_registry(install, [{"name": "personal", "vault_root": str(legacy)}])
    _commit_all(install)
    config = _config(tmp_path / "install", {"personal": str(legacy)})
    return install, config


def test_plan_treats_an_unrelated_path_as_the_whole_directory(tmp_path: Path) -> None:
    install, config = _pinned_install(tmp_path)

    result = plan(config, "personal")

    assert result.whole_directory
    assert result.entries == []
    assert not result.refused


def test_apply_moves_the_whole_directory_and_repoints_the_registry(tmp_path: Path) -> None:
    install, config = _pinned_install(tmp_path)
    runtime = install / ".runtime"

    result = apply(config, "personal", runtime)

    assert result["status"] == "relocated", result.get("refusals")
    assert not (install / "legacy-personal-notes").exists()
    assert (install / "memory-vault" / "personal" / "People" / "Peter.md").is_file()
    entries = json.loads((runtime / "workspaces.json").read_text(encoding="utf-8"))
    assert entries[0]["vault_root"] == "memory-vault/personal"
