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

from ciao import vault_relocate
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


def test_undo_preserves_registry_changes_made_after_the_relocation(tmp_path: Path) -> None:
    """Undo's scope is one workspace, not a snapshot restore of the whole file.

    A workspace added, removed, or edited after the relocation ran must
    survive `--undo` — only the relocated workspace's vault_root reverts.
    """
    install, config = _shared_root_install(tmp_path)
    runtime = install / ".runtime"

    applied = apply(config, "scandit", runtime)
    assert applied["status"] == "relocated"

    # Something unrelated changes the registry after the relocation.
    registry_path = runtime / "workspaces.json"
    entries = json.loads(registry_path.read_text(encoding="utf-8"))
    entries.append({"name": "newcomer", "vault_root": "memory-vault/newcomer"})
    registry_path.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")

    result = undo(config, "scandit", runtime)

    assert result["status"] == "undone", result
    after = json.loads(registry_path.read_text(encoding="utf-8"))
    scandit_entry = next(e for e in after if e["name"] == "scandit")
    assert scandit_entry["vault_root"] == str(install / "memory-vault")
    # The unrelated addition survived the undo.
    assert any(e["name"] == "newcomer" for e in after)


def test_apply_refuses_when_workspace_is_missing_from_the_registry(tmp_path: Path) -> None:
    """A registry that cannot record the new location must not lose the vault.

    Covers an install whose workspaces come from CIAO_WORKSPACES (an env var)
    rather than workspaces.json, or a registry missing this workspace's entry:
    apply must refuse before moving anything, not move it and report success.
    """
    install, config = _shared_root_install(tmp_path)
    runtime = install / ".runtime"
    (runtime / "workspaces.json").unlink()
    _commit_all(install, "drop registry")

    result = apply(config, "scandit", runtime)

    assert result["status"] == "refused"
    assert any("registry" in r.lower() for r in result["refusals"])
    assert (install / "memory-vault" / "People").is_dir(), "moved despite an unpersistable result"


def test_retrying_apply_after_success_does_not_clobber_the_relocated_receipt(
    tmp_path: Path,
) -> None:
    """A harmless re-run of --apply must not make undo a no-op.

    After a successful relocation, `plan()` correctly refuses a second
    `--apply` ("already at its standard location"). That refusal must not
    overwrite the completed receipt, or the documented `--undo` stops working
    for a relocation that is still fully in effect.
    """
    install, config = _shared_root_install(tmp_path)
    runtime = install / ".runtime"

    first = apply(config, "scandit", runtime)
    assert first["status"] == "relocated"

    second = apply(config, "scandit", runtime)
    assert second["status"] == "refused"
    assert "already at its standard location" in second["refusals"][0]

    receipt = json.loads(receipt_path(runtime, "scandit").read_text(encoding="utf-8"))
    assert receipt["status"] == "relocated", "the successful receipt was overwritten"

    result = undo(config, "scandit", runtime)
    assert result["status"] == "undone", result


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


def test_plan_refuses_a_destination_nested_inside_the_source(tmp_path: Path) -> None:
    """The canonical destination must never be at or beneath the source.

    With the global vault root nested under the pinned vault (source
    install/legacy, canonical install/legacy/nested/personal), no
    other-workspace overlap is found and the whole-directory move is
    approved — but `git mv` then fails moving a directory into itself, after
    apply() has already created the destination parents inside the source.
    """
    install = _git_install(tmp_path)
    legacy = install / "legacy"
    (legacy / "People").mkdir(parents=True)
    (legacy / "People" / "Peter.md").write_text("# Peter\n", encoding="utf-8")
    _write_registry(install, [{"name": "personal", "vault_root": str(legacy)}])
    _commit_all(install)
    # The global vault root sits INSIDE the pinned vault, so the canonical
    # location (vault_root/personal) nests beneath the source.
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path / "install",
        state_path=tmp_path / "install" / ".runtime" / "state.json",
        media_root=tmp_path / "install" / ".runtime" / "media",
        vault_root=legacy / "nested",
        workspaces={
            "personal": WorkspaceConfig(name="personal", vault_root=str(legacy)),
        },
    )

    result = plan(config, "personal")

    assert result.refused
    assert any("at or beneath" in r for r in result.refusals), result.refusals
    assert not result.whole_directory


def test_apply_moves_the_whole_directory_and_repoints_the_registry(tmp_path: Path) -> None:
    install, config = _pinned_install(tmp_path)
    runtime = install / ".runtime"

    result = apply(config, "personal", runtime)

    assert result["status"] == "relocated", result.get("refusals")
    assert not (install / "legacy-personal-notes").exists()
    assert (install / "memory-vault" / "personal" / "People" / "Peter.md").is_file()
    entries = json.loads((runtime / "workspaces.json").read_text(encoding="utf-8"))
    assert entries[0]["vault_root"] == "memory-vault/personal"


def test_apply_whole_directory_into_a_preexisting_empty_destination_renames_exactly(
    tmp_path: Path,
) -> None:
    """`git mv` nests into an existing directory instead of renaming into it.

    plan() only guarantees the destination is EMPTY, not absent — an empty
    memory-vault/personal/ can already exist. Content must land directly at
    the destination, not at memory-vault/personal/legacy-personal-notes/...
    """
    install, config = _pinned_install(tmp_path)
    (install / "memory-vault" / "personal").mkdir(parents=True)
    _commit_all(install, "pre-create empty destination")
    runtime = install / ".runtime"

    result = apply(config, "personal", runtime)

    assert result["status"] == "relocated", result.get("refusals")
    assert (install / "memory-vault" / "personal" / "People" / "Peter.md").is_file()
    assert not (install / "memory-vault" / "personal" / "legacy-personal-notes").exists()


# -- ambiguous shared-root ownership -----------------------------------------


def test_plan_refuses_when_the_shared_vault_root_has_multiple_owners(tmp_path: Path) -> None:
    """Two legacy workspaces both pinned to the vault root itself.

    Classifying loose top-level entries as one workspace's own content would
    hand every one of them to whichever workspace is relocated first,
    stranding the other — CiaoConfig.legacy_entity_workspace treats this same
    shape as ambiguous, and plan() must refuse it too rather than guess.
    """
    install = _git_install(tmp_path)
    vault_root = install / "memory-vault"
    (vault_root / "People").mkdir(parents=True)
    (vault_root / "People" / "Peter.md").write_text("# Peter\n", encoding="utf-8")
    _write_registry(
        install,
        [
            {"name": "scandit", "vault_root": str(vault_root)},
            {"name": "other", "vault_root": str(vault_root)},
        ],
    )
    _commit_all(install)
    config = _config(
        tmp_path / "install", {"scandit": str(vault_root), "other": str(vault_root)}
    )

    result = plan(config, "scandit")

    assert result.refused
    assert "more than one workspace" in result.refusals[0]
    assert result.entries == []


# -- registry persistence failure --------------------------------------------


def test_apply_rolls_back_moves_when_the_registry_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write failure discovered only after the moves must unwind them.

    The workspace-is-registered guard only proves the registry was READABLE
    before anything moved; a write failure (permissions, disk full) can still
    surface only once every git mv has already succeeded. Leaving the vault
    moved with the registry unrepointed would orphan it under a path nothing
    resolves to.
    """
    install, config = _shared_root_install(tmp_path)
    runtime = install / ".runtime"

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(vault_relocate, "_write_registry", _boom)

    result = apply(config, "scandit", runtime)

    assert result["status"] == "refused"
    assert any("registry" in r.lower() for r in result["refusals"])
    assert (install / "memory-vault" / "People" / "Peter.md").is_file(), "move was not rolled back"
    assert not (install / "memory-vault" / "scandit").exists()
    entries = json.loads((runtime / "workspaces.json").read_text(encoding="utf-8"))
    scandit_entry = next(e for e in entries if e["name"] == "scandit")
    assert scandit_entry["vault_root"] == str(install / "memory-vault")


def test_apply_reports_a_failed_partial_move_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing rollback must not read as a clean refusal.

    When a later forward `git mv` fails and unwinding an earlier completed
    move fails too (the same disk/permission problem persists), the operator
    must not get a refusal that implies the source layout is intact while
    some notes sit at the destination with no applied receipt to recover
    from.
    """
    install, config = _shared_root_install(tmp_path)
    runtime = install / ".runtime"

    real_run_git = vault_relocate._run_git
    forward_calls = {"n": 0}

    def flaky_git(root: Path, *args: str) -> tuple[int, str]:
        if args[:1] == ("mv",) and len(args) == 3:
            src = args[1]
            forward = src.startswith("memory-vault/") and not src.startswith(
                "memory-vault/scandit"
            )
            if forward:
                forward_calls[0] += 1
                if forward_calls[0] == 2:
                    return (128, "fatal: simulated forward failure")
            # Rollback: moving FROM the scandit destination BACK to source.
            if src.startswith("memory-vault/scandit"):
                return (128, "fatal: simulated rollback failure")
        return real_run_git(root, *args)

    forward_calls = [0]
    monkeypatch.setattr(vault_relocate, "_run_git", flaky_git)

    result = apply(config, "scandit", runtime)

    assert result["status"] == "refused"
    assert any("simulated forward failure" in r for r in result["refusals"])
    # The rollback failure is surfaced, not swallowed: the refusal names the
    # divergence between disk and registry instead of claiming a clean undo.
    assert any("rollback also failed" in r for r in result["refusals"]), result["refusals"]


# -- undo collision with a recreated source ----------------------------------


def test_undo_refuses_when_the_original_path_was_recreated(tmp_path: Path) -> None:
    """Something (e.g. a still-running server) recreated the old location.

    Reversing with `git mv` would treat the recreated directory as a
    container and nest the restored content inside it rather than merge or
    overwrite — undo must refuse instead of guessing.
    """
    install, config = _shared_root_install(tmp_path)
    runtime = install / ".runtime"

    applied = apply(config, "scandit", runtime)
    assert applied["status"] == "relocated"

    # Something recreates the original path before the operator restarts.
    (install / "memory-vault" / "People").mkdir()
    (install / "memory-vault" / "People" / "new-note.md").write_text(
        "fresh\n", encoding="utf-8"
    )

    result = undo(config, "scandit", runtime)

    assert result["status"] == "refused"
    assert "already exists" in result["reason"]
    # Nothing was nested into it, and the relocated content is untouched.
    assert (install / "memory-vault" / "People" / "new-note.md").is_file()
    assert not (install / "memory-vault" / "People" / "People").exists()
    assert (install / "memory-vault" / "scandit" / "People" / "Peter.md").is_file()


def test_undo_preflights_all_recreated_paths_before_reversing_anything(tmp_path: Path) -> None:
    install, config = _shared_root_install(tmp_path)
    runtime = install / ".runtime"

    applied = apply(config, "scandit", runtime)
    assert applied["status"] == "relocated"

    # Recreate the path for the second reverse move. A collision there must
    # not leave the first move already reversed.
    (install / "memory-vault" / "People").mkdir()
    (install / "memory-vault" / "Projects").mkdir()

    result = undo(config, "scandit", runtime)

    assert result["status"] == "refused"
    assert result["reversed"] == []
    assert (install / "memory-vault" / "scandit" / "People" / "Peter.md").is_file()
    assert (install / "memory-vault" / "scandit" / "Projects" / "roadmap.md").is_file()


def test_undo_rolls_earlier_reversals_forward_when_a_later_one_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed reverse `git mv` must not strand already-reversed files.

    The receipt and registry still describe the relocated layout, so after
    reversing two of three moves a failure in the third has to re-relocate
    the two it already reversed — otherwise those files sit outside the
    configured vault until the next successful undo.
    """
    install, config = _shared_root_install(tmp_path)
    runtime = install / ".runtime"

    applied = apply(config, "scandit", runtime)
    assert applied["status"] == "relocated"

    real_run_git = vault_relocate._run_git
    # The apply path also runs git mv; only fail the SECOND reverse move
    # (destination -> source), once one reversal has already landed.
    state = {"reverse_calls": 0}

    def flaky_git(root: Path, *args: str) -> tuple[int, str]:
        if args[:1] == ("mv",) and len(args) == 3 and state["reverse_calls"] >= 0:
            # Distinguish reverse moves (dest -> src) from forward ones.
            dest_arg, src_arg = args[1], args[2]
            if dest_arg.startswith("memory-vault/scandit") and src_arg.startswith(
                "memory-vault/"
            ) and not src_arg.startswith("memory-vault/scandit"):
                state["reverse_calls"] += 1
                if state["reverse_calls"] == 2:
                    return (128, "fatal: simulated git mv failure")
        return real_run_git(root, *args)

    monkeypatch.setattr(vault_relocate, "_run_git", flaky_git)

    result = undo(config, "scandit", runtime)

    assert result["status"] == "failed"
    assert "simulated git mv failure" in result["reason"]
    # The earlier reversal was rolled forward: the relocated layout is intact
    # and the receipt still describes it.
    assert result["reversed"] == []
    assert (install / "memory-vault" / "scandit" / "People" / "Peter.md").is_file()
    assert (install / "memory-vault" / "scandit" / "Projects" / "roadmap.md").is_file()
    assert receipt_path(runtime, "scandit").is_file()


# -- CLI: CIAO_RUNTIME_ROOT is honored ---------------------------------------


def test_cli_honors_ciao_runtime_root_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import cli

    install, _ = _shared_root_install(tmp_path)
    custom_runtime = tmp_path / "elsewhere" / "runtime"
    custom_runtime.mkdir(parents=True)
    # Move the registry the fixture wrote under install/.runtime to the
    # custom runtime root, so CiaoConfig and the CLI agree on where it lives.
    registry = json.loads((install / ".runtime" / "workspaces.json").read_text(encoding="utf-8"))
    (custom_runtime / "workspaces.json").write_text(
        json.dumps(registry, indent=2) + "\n", encoding="utf-8"
    )

    monkeypatch.setenv("CIAO_WORKSPACE", str(install))
    monkeypatch.setenv("CIAO_RUNTIME_ROOT", str(custom_runtime))
    monkeypatch.delenv("CIAO_VAULT_ROOT", raising=False)
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test")

    rc = cli.main(["vault-relocate", "scandit", "--apply"])

    assert rc == 0
    assert (custom_runtime / "migration" / "vault-relocate-scandit.json").is_file()
    assert not (install / ".runtime" / "migration").exists()


# -- install-root vault (existing-folder setup) ------------------------------


def test_plan_refuses_when_the_vault_root_is_the_install_root(tmp_path: Path) -> None:
    """CIAO_VAULT_ROOT and the workspace's own vault_root both "." (a
    documented existing-folder setup): the vault root IS the whole install,
    so its top level mixes CLAUDE.md/.git/.runtime with vault content. There
    is no safe way to tell those apart, so this must refuse rather than
    classify CLAUDE.md as movable workspace content.
    """
    install = _git_install(tmp_path)
    (install / "People").mkdir()
    (install / "People" / "Peter.md").write_text("# Peter\n", encoding="utf-8")
    (install / "CLAUDE.md").write_text("# guide\n", encoding="utf-8")
    _commit_all(install)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=install,
        state_path=install / ".runtime" / "state.json",
        media_root=install / ".runtime" / "media",
        vault_root=Path("."),
        workspaces={"scandit": WorkspaceConfig(name="scandit", vault_root=".")},
    )

    result = plan(config, "scandit")

    assert result.refused
    assert "install root itself" in result.refusals[0]
    assert result.entries == []


# -- nested workspace vaults inside a whole-directory move -------------------


def test_plan_refuses_a_whole_directory_move_containing_another_workspace(
    tmp_path: Path,
) -> None:
    """A pinned/legacy root can itself contain another workspace's nested
    vault (personal -> legacy/, client -> legacy/client/). Moving `legacy`
    whole would carry `client`'s notes along while only `personal`'s registry
    entry gets repointed, stranding `client` at a path that just vanished.
    """
    install = _git_install(tmp_path)
    legacy = install / "legacy"
    (legacy / "client" / "Notes").mkdir(parents=True)
    (legacy / "client" / "Notes" / "note.md").write_text("# Note\n", encoding="utf-8")
    (legacy / "People").mkdir()
    (legacy / "People" / "Peter.md").write_text("# Peter\n", encoding="utf-8")
    _commit_all(install)
    config = _config(
        tmp_path / "install",
        {"personal": str(legacy), "client": str(legacy / "client")},
    )

    result = plan(config, "personal")

    assert result.refused
    assert "client" in result.refusals[0]
    assert not result.whole_directory


# -- registry authority is passed explicitly, not read from os.environ ------


def test_apply_refuses_when_registry_is_not_authoritative(tmp_path: Path) -> None:
    """registry_authoritative reflects what the CALLER's merged environment
    actually sourced workspaces from — vault_relocate has no way to know
    this on its own, so it trusts the flag over a coincidentally-valid
    workspaces.json.
    """
    install, config = _shared_root_install(tmp_path)
    runtime = install / ".runtime"

    result = apply(config, "scandit", runtime, registry_authoritative=False)

    assert result["status"] == "refused"
    assert (install / "memory-vault" / "People").is_dir(), "moved despite non-authoritative registry"


def test_undo_refuses_when_registry_is_not_authoritative(tmp_path: Path) -> None:
    install, config = _shared_root_install(tmp_path)
    runtime = install / ".runtime"
    applied = apply(config, "scandit", runtime)
    assert applied["status"] == "relocated"

    result = undo(config, "scandit", runtime, registry_authoritative=False)

    assert result["status"] == "refused"
    assert (install / "memory-vault" / "scandit" / "People").is_dir(), "reversed despite non-authoritative registry"


# -- CLI: an explicit --workspace ignores ambient env, uses the target's .env


def test_cli_explicit_workspace_loads_runtime_root_from_target_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--workspace <install> must resolve CIAO_RUNTIME_ROOT from THAT
    install's own .env, not the calling shell's ambient env — an ambient
    value belongs to whatever install the caller's own session is for, and
    letting it leak in means apply can move the target's vault while writing
    the registry entry to the wrong (caller's own) runtime directory.
    """
    from ciao import cli

    install, _ = _shared_root_install(tmp_path)
    custom_runtime = tmp_path / "custom-runtime"
    custom_runtime.mkdir()
    registry = json.loads((install / ".runtime" / "workspaces.json").read_text(encoding="utf-8"))
    (custom_runtime / "workspaces.json").write_text(
        json.dumps(registry, indent=2) + "\n", encoding="utf-8"
    )
    (install / ".env").write_text(f"CIAO_RUNTIME_ROOT={custom_runtime}\n", encoding="utf-8")

    decoy_runtime = tmp_path / "decoy-runtime"
    decoy_runtime.mkdir()
    monkeypatch.setenv("CIAO_WORKSPACE", str(tmp_path / "unrelated"))
    monkeypatch.setenv("CIAO_RUNTIME_ROOT", str(decoy_runtime))
    monkeypatch.delenv("CIAO_VAULT_ROOT", raising=False)
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test")

    rc = cli.main(["vault-relocate", "scandit", "--workspace", str(install), "--apply"])

    assert rc == 0
    assert (custom_runtime / "migration" / "vault-relocate-scandit.json").is_file()
    assert not (decoy_runtime / "migration").exists()
