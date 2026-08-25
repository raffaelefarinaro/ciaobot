"""Exporting the vault as a portable OKF bundle.

The link swap made the vault conformant; this is what cashes that in. The module
converts nothing — it adds the three things a bare folder cannot carry: a
bundle-root `index.md` with `okf_version`, a single-artifact tarball, and
workspace scoping so the work workspace can be shared without the personal one.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

from ciao.okf import OKF_VERSION, bundle_members, export_bundle, format_bundle_index
from ciao.vault_index import scan_vault


def _note(vault: Path, relative: str, body: str) -> None:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "memory-vault"
    _note(vault, "work/People/Aymen.md",
          "---\ntype: person\ntitle: Aymen\nrelated:\n  - work/People/Mo\n---\n"
          "# Aymen\n\nSee [Mo](./Mo.md).\n")
    _note(vault, "work/People/Mo.md", "---\ntype: person\ntitle: Mo\n---\n# Mo\n")
    _note(vault, "work/products/slc.md", "---\ntype: product\ntitle: SLC\n---\n# SLC\n")
    _note(vault, "personal/People/Alba.md",
          "---\ntype: person\ntitle: Alba\n---\n# Alba\n")
    # Excluded from the index, so never in a bundle.
    _note(vault, "Logs/2026-01-01.md", "# Log\n")
    return vault


def test_workspace_scoping_leaves_the_other_workspace_out(tmp_path: Path) -> None:
    """The case this exists for: hand the work bundle to colleagues without
    handing over the personal vault."""
    vault = _vault(tmp_path)

    summary = export_bundle(vault, tmp_path / "work.tar.gz", workspace="work")

    assert summary["written"] is True
    members = bundle_members(tmp_path / "work.tar.gz")
    assert "People/Aymen.md" in members
    assert not any("Alba" in name for name in members)
    # The workspace subtree becomes the bundle root, so no `work/` prefix remains.
    assert not any(name.startswith("work/") for name in members)


def test_the_bundle_root_index_carries_okf_version(tmp_path: Path) -> None:
    """`okf_version` is a bundle-level key per the spec — on the root index only,
    never stamped onto every note."""
    vault = _vault(tmp_path)
    export_bundle(vault, tmp_path / "work.tar.gz", workspace="work")

    with tarfile.open(tmp_path / "work.tar.gz", "r:gz") as archive:
        index = archive.extractfile("index.md").read().decode("utf-8")
        note = archive.extractfile("People/Mo.md").read().decode("utf-8")

    assert f"okf_version: {OKF_VERSION}" in index
    assert "okf_version" not in note


def test_logs_and_generated_files_never_reach_the_bundle(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    export_bundle(vault, tmp_path / "all.tar.gz")

    members = bundle_members(tmp_path / "all.tar.gz")
    assert not any(name.startswith("Logs/") for name in members)
    # Exactly one index.md: the generated bundle entry point.
    assert members.count("index.md") == 1


def test_an_unmigrated_vault_is_refused_and_names_an_example(tmp_path: Path) -> None:
    """A bundle whose edges are wikilinks is precisely what OKF exists to avoid:
    every consumer sees pages and no graph. Refusing is the point of the check."""
    vault = _vault(tmp_path)
    _note(vault, "work/People/Old.md",
          "---\ntype: person\n---\n# Old\n\nSee [[work/People/Mo]].\n")

    summary = export_bundle(vault, tmp_path / "work.tar.gz", workspace="work")

    assert summary["skipped"] == "vault still uses wikilinks"
    assert "Old.md" in summary["example"]
    assert not (tmp_path / "work.tar.gz").exists()


def test_force_exports_an_unmigrated_vault(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _note(vault, "work/People/Old.md",
          "---\ntype: person\n---\n# Old\n\nSee [[work/People/Mo]].\n")

    summary = export_bundle(
        vault, tmp_path / "work.tar.gz", workspace="work", force=True
    )

    assert summary["written"] is True


def test_links_leaving_the_workspace_are_counted_not_rewritten(tmp_path: Path) -> None:
    """Editing a note on the way out would make the bundle disagree with the vault
    it came from, so a dangling edge is reported instead."""
    vault = _vault(tmp_path)
    _note(vault, "work/People/Bridge.md",
          "---\ntype: person\ntitle: Bridge\nrelated:\n  - personal/People/Alba\n---\n"
          "# Bridge\n")

    summary = export_bundle(vault, tmp_path / "work.tar.gz", workspace="work")

    assert summary["cross_workspace_links"] == 1
    with tarfile.open(tmp_path / "work.tar.gz", "r:gz") as archive:
        text = archive.extractfile("People/Bridge.md").read().decode("utf-8")
    assert "personal/People/Alba" in text  # untouched


def test_an_unknown_workspace_writes_nothing(tmp_path: Path) -> None:
    vault = _vault(tmp_path)

    summary = export_bundle(vault, tmp_path / "nope.tar.gz", workspace="nope")

    assert "no notes found" in summary["skipped"]
    assert not (tmp_path / "nope.tar.gz").exists()


def test_index_groups_by_type_with_relative_links(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    entries = [
        e for e in scan_vault(vault)
        if str(e.path).startswith("memory-vault/work/")
    ]

    text = format_bundle_index(entries, "work")

    assert "## person (2)" in text
    assert "## product (1)" in text
    assert "- [Mo](./People/Mo.md)" in text


def test_a_re_rooted_workspace_vault_exports_its_whole_tree(tmp_path: Path) -> None:
    """Per-workspace re-rooting hands the exporter `<install>/work/memory-vault`,
    so entries read `memory-vault/People/A.md` with no `work/` segment. Filtering
    on one dropped every note and `--workspace-name work` reported "no notes
    found" instead of writing the bundle it advertises."""
    vault = tmp_path / "install" / "work" / "memory-vault"
    _note(vault, "People/Aymen.md",
          "---\ntype: person\ntitle: Aymen\n---\n# Aymen\n\nSee [Mo](./Mo.md).\n")
    _note(vault, "People/Mo.md", "---\ntype: person\ntitle: Mo\n---\n# Mo\n")

    summary = export_bundle(vault, tmp_path / "work.tar.gz", workspace="work")

    assert "skipped" not in summary
    assert summary["written"] is True
    assert summary["concepts"] == 2
    # Nothing leaves the bundle root: the whole vault IS the workspace.
    assert summary["cross_workspace_links"] == 0
    members = bundle_members(tmp_path / "work.tar.gz")
    assert members == ["People/Aymen.md", "People/Mo.md", "index.md"]

    with tarfile.open(tmp_path / "work.tar.gz", "r:gz") as archive:
        index = archive.extractfile("index.md").read().decode("utf-8")
    # The bundle is still labelled with the workspace even though no path
    # segment named it.
    assert "title: work" in index
    assert "](./People/Aymen.md)" in index
