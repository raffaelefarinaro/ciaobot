"""Reading every note in an install, in both layouts.

Measured on a clone of the reference install before this landed:

    BEFORE migration: scan_vault(config.vault_root) -> 583 notes
    AFTER  migration: scan_vault(config.vault_root) ->   0 notes

The migration moves the vault correctly; nothing that read it knew. Two separate
failures hid behind that one number: the vault root stops existing, and
`Entry.workspace` — inferred from the first path segment — starts reporting
FOLDER names, so `?workspace=personal` filters nearly everything out and the
graph invents a workspace called `projects`.
"""

from __future__ import annotations

import json
from pathlib import Path

from ciao.config import CiaoConfig, WorkspaceConfig, reset_reroot_cache
from ciao.vault_index import _build_graph, filter_entries, scan_targets, scan_vault


def _note(path: Path, title: str, related: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rel = f"related: [{related}]\n" if related else ""
    path.write_text(f"---\ntype: note\ntitle: {title}\n{rel}---\n# {title}\n", encoding="utf-8")


def _install(tmp_path: Path, *, migrated: bool) -> CiaoConfig:
    """Two workspaces holding a note of the SAME name, in one layout or the other."""
    root = tmp_path
    (root / ".runtime").mkdir(parents=True, exist_ok=True)
    if migrated:
        for name in ("personal", "work"):
            _note(root / name / "memory-vault" / "People" / "User.md", f"{name} user")
        receipt = root / ".runtime" / "migration" / "workspace-rooting.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps({"status": "migrated"}), encoding="utf-8")
    else:
        for name in ("personal", "work"):
            _note(root / "memory-vault" / name / "People" / "User.md", f"{name} user")
    reset_reroot_cache()
    return CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=root,
        vault_root=root / "memory-vault",
        state_path=root / ".runtime" / "state.json",
        media_root=root / ".runtime" / "media",
        workspaces={
            "personal": WorkspaceConfig(name="personal", vault_root="memory-vault/personal"),
            "work": WorkspaceConfig(name="work", vault_root="memory-vault/work"),
        },
    )


def test_targets_are_one_shared_vault_before_the_re_rooting(tmp_path: Path) -> None:
    config = _install(tmp_path, migrated=False)

    targets = config.vault_scan_targets()

    assert targets == [(tmp_path / "memory-vault", "", Path("memory-vault"))]


def test_targets_are_one_per_root_after_the_re_rooting(tmp_path: Path) -> None:
    config = _install(tmp_path, migrated=True)

    targets = config.vault_scan_targets()

    assert targets == [
        (tmp_path / "personal" / "memory-vault", "personal", Path("personal/memory-vault")),
        (tmp_path / "work" / "memory-vault", "work", Path("work/memory-vault")),
    ]


def test_every_note_is_found_in_both_layouts(tmp_path: Path) -> None:
    shared = _install(tmp_path / "a", migrated=False)
    rooted = _install(tmp_path / "b", migrated=True)

    before, _ = scan_targets(shared.vault_scan_targets())
    reset_reroot_cache()
    after, _ = scan_targets(rooted.vault_scan_targets())

    assert len(before) == 2
    assert len(after) == 2, "a migrated install used to come back with zero notes"


def test_the_workspace_is_stamped_not_inferred_after_the_re_rooting(tmp_path: Path) -> None:
    """Inference reads the first path segment, which inside a root is a folder."""
    config = _install(tmp_path, migrated=True)

    entries, _ = scan_targets(config.vault_scan_targets())

    assert sorted({e.workspace for e in entries}) == ["personal", "work"]
    assert "People" not in {e.workspace for e in entries}
    assert len(filter_entries(entries, workspace="personal")) == 1
    assert len(filter_entries(entries, workspace="work")) == 1


def test_two_roots_holding_the_same_note_name_do_not_collide(tmp_path: Path) -> None:
    """Rendered paths are the graph's node ids. Keyed per root they were equal."""
    config = _install(tmp_path, migrated=True)

    entries, absolute = scan_targets(config.vault_scan_targets())

    ids = [str(e.path) for e in entries]
    assert len(set(ids)) == len(ids) == 2, ids
    assert set(ids) == {
        "personal/memory-vault/People/User.md",
        "work/memory-vault/People/User.md",
    }
    # And each id resolves to the real file, which is what mtime needs.
    for node_id in ids:
        assert absolute[node_id].is_file(), node_id


def test_a_related_link_does_not_resolve_across_roots(tmp_path: Path) -> None:
    """One scan per target, so a link cannot reach another root's note.

    The graph already wanted that — it drops cross-workspace edges — and scanning
    per root gives it for free rather than needing a filter.
    """
    config = _install(tmp_path, migrated=True)
    _note(
        tmp_path / "personal" / "memory-vault" / "Ideas" / "Thing.md",
        "Thing",
        related="People/User",
    )

    entries, _ = scan_targets(config.vault_scan_targets())
    graph = _build_graph(entries)

    crossing = [
        (src, dst)
        for src, dsts in graph.items()
        for dst in dsts
        if src.split("/")[0] != dst.split("/")[0]
    ]
    assert crossing == [], crossing
    # The link still resolves WITHIN its own root.
    assert graph["personal/memory-vault/Ideas/Thing.md"] == {
        "personal/memory-vault/People/User.md"
    }


def test_a_single_vault_scan_is_unchanged(tmp_path: Path) -> None:
    """Defaults must render exactly as before, or every INDEX.md changes."""
    vault = tmp_path / "memory-vault"
    _note(vault / "People" / "Mo.md", "Mo")

    entries = scan_vault(vault)

    assert [str(e.path) for e in entries] == ["memory-vault/People/Mo.md"]
    assert entries[0].workspace == "personal"


def test_the_memory_map_no_longer_reads_the_shared_vault_root(tmp_path: Path) -> None:
    """Conservation over the sweep: the handler must not resolve the vault itself."""
    import inspect

    from ciao.web import routes_api

    source = inspect.getsource(routes_api.vault_graph)
    assert "vault_scan_targets" in source
    # Comments may still name it (one explains the bug); no CODE line may read it.
    code = [
        line for line in source.splitlines()
        if "config.vault_root" in line and not line.strip().startswith("#")
    ]
    assert code == [], code
