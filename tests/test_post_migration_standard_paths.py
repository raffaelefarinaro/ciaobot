"""The standard vault location, and the health surfaces built on it, after the
re-rooting.

Each test here covers a defect found on the operator's own migrated install, by
inspection rather than by the suite: the checks were all green while telling the
operator their correctly-migrated vault was in the wrong place.
"""

from __future__ import annotations

import json
from pathlib import Path

from ciao.config import CiaoConfig, WorkspaceConfig, reset_reroot_cache
from ciao.operator_actions import DetectionContext, detect_actions
from ciao.web.agent_assets import repair_workspace_health, workspace_health


def _install(tmp_path: Path, *, migrated: bool) -> CiaoConfig:
    root = tmp_path
    (root / ".runtime").mkdir(parents=True, exist_ok=True)
    if migrated:
        for name in ("personal", "work"):
            (root / name / "memory-vault").mkdir(parents=True, exist_ok=True)
            (root / name / "memory-vault" / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
        receipt = root / ".runtime" / "migration" / "workspace-rooting.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps({"status": "migrated"}), encoding="utf-8")
        stored = {n: f"{n}/memory-vault" for n in ("personal", "work")}
    else:
        for name in ("personal", "work"):
            (root / "memory-vault" / name).mkdir(parents=True, exist_ok=True)
            (root / "memory-vault" / name / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
        stored = {n: f"memory-vault/{n}" for n in ("personal", "work")}
    reset_reroot_cache()
    return CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=root,
        vault_root=root / "memory-vault",
        state_path=root / ".runtime" / "state.json",
        media_root=root / ".runtime" / "media",
        workspaces={
            n: WorkspaceConfig(name=n, vault_root=v) for n, v in stored.items()
        },
    )


# -- the standard location follows the layout ---------------------------------


def test_standard_vault_location_is_under_the_agent_root_once_migrated(tmp_path: Path) -> None:
    config = _install(tmp_path, migrated=True)

    assert config.canonical_workspace_vault_root("work") == tmp_path / "work" / "memory-vault"
    assert config.canonical_workspace_vault_root("work") == config.workspace_vault_root("work")


def test_standard_vault_location_stays_shared_before_migrating(tmp_path: Path) -> None:
    config = _install(tmp_path, migrated=False)

    assert config.canonical_workspace_vault_root("work") == tmp_path / "memory-vault" / "work"


def test_no_vault_location_tile_on_a_correctly_migrated_install(tmp_path: Path) -> None:
    """The tile that fired for every workspace, forever, on a healthy install."""
    config = _install(tmp_path, migrated=True)

    actions = detect_actions(DetectionContext(config=config, runtime_dir=tmp_path / ".runtime"))

    assert [a.id for a in actions if a.kind == "vault-location"] == []


def test_a_vault_moved_out_of_its_root_still_raises_the_tile(tmp_path: Path) -> None:
    """The fix must not silence the check it was built for."""
    config = _install(tmp_path, migrated=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    config.workspaces["work"].vault_root = str(elsewhere)

    actions = detect_actions(DetectionContext(config=config, runtime_dir=tmp_path / ".runtime"))

    assert [a.id for a in actions if a.kind == "vault-location"] == ["vault-location:work"]


# -- health rows belong to the root being checked -----------------------------


def test_each_agent_root_reports_only_its_own_memory_file(tmp_path: Path) -> None:
    config = _install(tmp_path, migrated=True)

    rows = [c for c in workspace_health(config)["checks"] if "MEMORY.md" in c["title"]]

    assert len(rows) == 2, [r["title"] for r in rows]
    assert sorted(r["path"] for r in rows) == ["memory-vault/MEMORY.md"] * 2
    assert sorted(r["title"] for r in rows) == [
        "Workspace MEMORY.md (personal)",
        "Workspace MEMORY.md (work)",
    ]
    assert not any(Path(r["path"]).is_absolute() for r in rows)


def test_the_shared_layout_still_reports_every_workspace_memory_file(tmp_path: Path) -> None:
    config = _install(tmp_path, migrated=False)

    rows = [c for c in workspace_health(config)["checks"] if "MEMORY.md" in c["title"]]

    assert sorted(r["path"] for r in rows) == [
        "memory-vault/personal/MEMORY.md",
        "memory-vault/work/MEMORY.md",
    ]


# -- the fix button repairs the roots, not the install root -------------------


def test_fix_issues_scaffolds_the_agent_roots_and_leaves_the_install_root_bare(
    tmp_path: Path,
) -> None:
    config = _install(tmp_path, migrated=True)

    repair_workspace_health(config)

    for name in ("personal", "work"):
        assert (tmp_path / name / "CLAUDE.md").exists()
        assert (tmp_path / name / "subagents").is_dir()
        assert (tmp_path / name / "commands").is_dir()
    # The debris the migration exists to remove must not come back.
    assert not (tmp_path / "CLAUDE.md").exists()
    assert not (tmp_path / "subagents").exists()
    assert not (tmp_path / "commands").exists()


def test_fix_issues_still_scaffolds_the_install_root_before_migrating(tmp_path: Path) -> None:
    config = _install(tmp_path, migrated=False)

    repair_workspace_health(config)

    assert (tmp_path / "CLAUDE.md").exists()
    assert (tmp_path / "subagents").is_dir()
