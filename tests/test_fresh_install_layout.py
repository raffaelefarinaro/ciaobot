"""A fresh install is CREATED in the per-workspace layout, not migrated into it.

Setup used to scaffold `memory-vault/personal` plus agent assets at the install
root — so every new user was manufactured into exactly the state the re-rooting
exists to fix, and met a blocking "migrate now" tile on first boot. The migration
engine had an audience that regenerated itself.

The receipt matters as much as the folders: `agent_root()` answers per-root only
when a receipt says so, and files nested with a gate that still says "shared" is
the one combination that breaks every layout-dependent path.
"""

from __future__ import annotations

import json
from pathlib import Path

from ciao.cli import setup_workspace
from ciao.config import CiaoConfig, reset_reroot_cache
from ciao.operator_actions import DetectionContext, detect_actions
from ciao.web.agent_assets import workspace_health


def _fresh(tmp_path: Path) -> CiaoConfig:
    setup_workspace(tmp_path, auth_token="t", auth_required=False)
    reset_reroot_cache()
    return CiaoConfig.from_env({
        "PWA_AUTH_TOKEN": "t",
        "CIAO_WORKSPACE": str(tmp_path),
        "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
        "CIAO_VAULT_ROOT": str(tmp_path / "memory-vault"),
    })


def test_a_fresh_setup_builds_the_nested_layout(tmp_path: Path) -> None:
    config = _fresh(tmp_path)

    assert (tmp_path / "personal" / "memory-vault").is_dir()
    assert not (tmp_path / "memory-vault").exists()
    assert config.agent_root("personal") == tmp_path / "personal"
    assert config.workspace_vault_root("personal") == tmp_path / "personal" / "memory-vault"


def test_a_fresh_setup_puts_the_agent_assets_in_the_workspace(tmp_path: Path) -> None:
    """`agent_roots_for` reads the receipt for the gate and the REGISTRY for the
    names, so both must exist before the asset loop — with no registry it falls
    back to the install root, which is how the first attempt still left
    `.claude/`, `commands/` and a stock CLAUDE.md beside the nested vault."""
    _fresh(tmp_path)

    for asset in ("CLAUDE.md", "commands", "subagents", ".claude"):
        assert (tmp_path / "personal" / asset).exists(), asset
        assert not (tmp_path / asset).exists(), f"litter at the install root: {asset}"


def test_a_fresh_setup_needs_no_migration_and_shows_no_tile(tmp_path: Path) -> None:
    """The point of the whole change: a new user never sees migration UX."""
    from ciao.workspace_reroot import migrate_if_needed

    config = _fresh(tmp_path)

    assert migrate_if_needed(config)["status"] == "already_migrated"
    # A fresh install is a configured install, so the GitHub-star nudge would
    # legitimately surface; it is not migration UX, so silence it here.
    (tmp_path / ".runtime" / "star-receipt.json").write_text(
        json.dumps({"status": "starred", "at": "2026-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    kinds = {a.kind for a in detect_actions(
        DetectionContext(config=config, runtime_dir=tmp_path / ".runtime")
    )}
    assert "workspace-unmigrated" not in kinds
    assert kinds == set(), kinds


def test_a_fresh_setup_is_healthy_before_its_first_boot(tmp_path: Path) -> None:
    """Setup builds the generated catalogs too. Without that a brand-new install
    showed nine Workspace Health warnings and an assets tile on an install where
    nothing was wrong — it just had not synced yet."""
    config = _fresh(tmp_path)

    health = workspace_health(config)

    assert health["status"] == "ok", [
        c for c in health["checks"] if c["status"] != "ok"
    ]


def test_the_receipt_records_that_it_was_born_this_way(tmp_path: Path) -> None:
    """A reader can tell "created per-root" from "migrated" without inferring it
    from an empty move list."""
    _fresh(tmp_path)

    receipt = json.loads(
        (tmp_path / ".runtime" / "migration" / "workspace-rooting.json").read_text()
    )

    assert receipt["status"] == "migrated"
    assert receipt["born_per_root"] is True
    assert receipt["origin"] == "born"
    assert receipt["moves"] == []
