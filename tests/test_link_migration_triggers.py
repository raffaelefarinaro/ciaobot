"""The three OKF migration triggers: detect and surface, never rewrite."""
from __future__ import annotations
from pathlib import Path
from ciao.operator_actions import DetectionContext, detect_actions
from ciao.os_audit import audit_upgrade_notices
from ciao.vault_migrate_links import has_unmigrated_links, write_receipt
from types import SimpleNamespace


def _vault(tmp_path: Path) -> Path:
    v = tmp_path / "memory-vault"
    (v / "personal").mkdir(parents=True)
    (v / "personal" / "a.md").write_text("---\ntype: note\n---\n# A\n\nSee [[People/Mo]].\n")
    (v / "Logs").mkdir()
    (v / "Logs" / "x.md").write_text("# L\n\n[[People/Mo]]\n")
    return v


def test_detector_ignores_excluded_trees(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    assert has_unmigrated_links(v) == "personal/a.md"
    (v / "personal" / "a.md").write_text("---\ntype: note\n---\n# A\n\nSee [Mo](../People/Mo.md).\n")
    # Only Logs/ is left, which the migration never touches — so nothing to offer.
    assert has_unmigrated_links(v) == ""


def test_detector_skips_code_and_escapes(tmp_path: Path) -> None:
    v = tmp_path / "memory-vault"
    (v / "personal").mkdir(parents=True)
    (v / "personal" / "a.md").write_text(
        "---\ntype: note\n---\n# A\n\n```\n[[People/Mo]]\n```\n\n`[[People/Mo]]` and \\[[People/Mo]]\n"
    )
    assert has_unmigrated_links(v) == ""


def _cfg(v: Path):
    # `vault_mode="existing"` is the notice's own applicability, shared with the
    # Home card since #816: the retired wikilink dialect is in scope for a vault
    # this install adopted, not for one Ciaobot created conformant. A stub that
    # does not declare its mode is a vault Ciaobot created, and is silent.
    return SimpleNamespace(
        vault_root=v, workspace_root=v.parent, vault_mode="existing",
        workspace_names=lambda: ["personal"],
        workspace_vault_root=lambda n: v / n,
        canonical_workspace_vault_root=lambda n: v / n,
    )


def test_notice_is_raised_when_the_vault_is_unmigrated(tmp_path: Path) -> None:
    v = _vault(tmp_path)
    rt = tmp_path / ".runtime"
    result = audit_upgrade_notices(_cfg(v), runtime_dir=rt)
    kinds = {n["type"] for n in result["notices"]}
    assert "unmigrated_vault_links" in kinds
    notice = next(n for n in result["notices"] if n["type"] == "unmigrated_vault_links")
    assert "personal/a.md" in notice["detail"]
    assert "vault-unmigrate-links" in notice["remedy"]   # the way back is stated


def test_notice_stops_once_a_receipt_exists(tmp_path: Path) -> None:
    """A migrated vault must not keep nagging, even if a stray wikilink survives
    somewhere the migration declined to touch."""
    v = _vault(tmp_path)
    rt = tmp_path / ".runtime"
    write_receipt(rt, {"vault_root": str(v), "files_rewritten": 1})
    result = audit_upgrade_notices(_cfg(v), runtime_dir=rt)
    assert "unmigrated_vault_links" not in {n["type"] for n in result["notices"]}


def test_notice_keeps_asking_after_a_partial_migration(tmp_path: Path) -> None:
    """A run that could not write every note left wikilinks behind, so the notice
    is still the truth. Reading its receipt as "converted" is what let a
    half-finished migration go quiet on every surface at once."""
    v = _vault(tmp_path)
    rt = tmp_path / ".runtime"
    write_receipt(rt, {
        "vault_root": str(v),
        "rewrites": [{"path": "other.md", "offset": 0, "from": "x", "to": "y"}],
        "failed": [{"path": "personal/a.md", "error": "Permission denied"}],
    })
    result = audit_upgrade_notices(_cfg(v), runtime_dir=rt)
    assert "unmigrated_vault_links" in {n["type"] for n in result["notices"]}


def test_no_runtime_dir_means_no_notice(tmp_path: Path) -> None:
    """Programmatic callers that pass no runtime root cannot check the receipt,
    so they must not guess the vault is unmigrated."""
    v = _vault(tmp_path)
    result = audit_upgrade_notices(_cfg(v))
    assert "unmigrated_vault_links" not in {n["type"] for n in result["notices"]}


def test_a_scratch_vault_is_out_of_scope_on_both_surfaces(tmp_path: Path) -> None:
    """A vault Ciaobot created conformant never had the dialect to migrate.

    The Home card has always gated on `vault_mode`; the audit did not, so the two
    surfaces answered different questions about the same vault — the cheap way to
    "stop them disagreeing" would have been to let the surface that cannot afford
    the walk declare the condition out of scope, which deletes a finding from a
    diagnostic. The shared applicability is the adopted-vault one both had half of:
    here a `scratch` vault is silent, on the audit as well as on Home.
    """
    v = _vault(tmp_path)
    cfg = _cfg(v)
    cfg.vault_mode = "scratch"

    result = audit_upgrade_notices(cfg, runtime_dir=tmp_path / ".runtime")

    assert "unmigrated_vault_links" not in {n["type"] for n in result["notices"]}


def test_the_audit_reports_what_home_only_points_at(tmp_path: Path) -> None:
    """Home hedges because it may not walk; the audit walks, so it tells the truth.

    One applicability, two costs. The card cannot claim a wikilink exists on the
    strength of a receipt's absence, so the notice is the surface that names a
    first offending note — and it keeps doing that however the card is worded.
    """
    v = _vault(tmp_path)
    rt = tmp_path / ".runtime"
    cfg = _cfg(v)

    tile = next(
        a for a in detect_actions(DetectionContext(config=cfg, runtime_dir=rt))
        if a.kind == "unmigrated-links"
    )
    result = audit_upgrade_notices(cfg, runtime_dir=rt)
    notice = next(
        n for n in result["notices"] if n["type"] == "unmigrated_vault_links"
    )

    assert "may still" in tile.title
    assert "personal/a.md" not in tile.detail
    assert "personal/a.md" in notice["detail"]
