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


# -- the surfaces that read a vault, in both layouts -------------------------


def test_the_startup_index_refresh_writes_one_index_per_root(tmp_path: Path) -> None:
    """It wrote a single index at `config.vault_root`, which the migration
    empties — so every root's index went stale and the log said only
    "does not exist yet; skipping"."""
    from ciao.main import _refresh_vault_index

    config = _install(tmp_path, migrated=True)

    assert _refresh_vault_index(tmp_path, config.vault_root, config.vault_scan_targets())

    for name in ("personal", "work"):
        index = tmp_path / name / "memory-vault" / "INDEX.md"
        assert index.is_file(), name
        # Per root there is nothing to disambiguate, so no workspace prefix.
        assert "memory-vault/" not in index.read_text(encoding="utf-8")


def test_the_startup_refresh_still_writes_one_shared_index_before_migrating(
    tmp_path: Path,
) -> None:
    from ciao.main import _refresh_vault_index

    config = _install(tmp_path, migrated=False)

    assert _refresh_vault_index(tmp_path, config.vault_root, config.vault_scan_targets())

    assert (tmp_path / "memory-vault" / "INDEX.md").is_file()
    assert not (tmp_path / "personal" / "memory-vault" / "INDEX.md").exists()


def test_workspace_health_checks_the_vaults_that_exist(tmp_path: Path) -> None:
    """It reported "Vault root is missing" and "Vault is not writable" on a
    CORRECTLY migrated install, because it checked the path the migration
    empties."""
    from ciao.web.agent_assets import workspace_health

    config = _install(tmp_path, migrated=True)

    checks = workspace_health(config)["checks"]
    vault_checks = [c for c in checks if c["id"].startswith("vault-")]

    assert vault_checks, checks
    assert [c for c in vault_checks if c["status"] == "error"] == []
    assert {c["id"] for c in vault_checks} == {
        "vault-root-personal",
        "vault-writable-personal",
        "vault-root-work",
        "vault-writable-work",
    }


def test_workspace_health_is_unchanged_before_migrating(tmp_path: Path) -> None:
    from ciao.web.agent_assets import workspace_health

    config = _install(tmp_path, migrated=False)

    ids = {c["id"] for c in workspace_health(config)["checks"]}

    assert "vault-root" in ids and "vault-writable" in ids


def test_a_migrated_node_id_is_still_deletable(tmp_path: Path) -> None:
    """`raw.startswith("memory-vault/")` rejected every id on a migrated
    install, so the Memory Map could delete nothing at all."""
    config = _install(tmp_path, migrated=True)
    entries, _ = scan_targets(config.vault_scan_targets())
    node_id = str(entries[0].path)
    assert node_id.startswith("personal/memory-vault/")

    # The resolution the handler performs, exercised directly.
    resolved = None
    for target, _name, prefix in config.vault_scan_targets():
        marker = f"{prefix.as_posix()}/"
        if node_id.startswith(marker):
            root = Path(target).resolve()
            resolved = (root / Path(node_id[len(marker):])).resolve()
            resolved.relative_to(root)
            break
    assert resolved is not None and resolved.is_file(), node_id


def test_a_node_id_outside_every_vault_is_refused(tmp_path: Path) -> None:
    """The containment check has to stay meaningful: this is a permanent delete."""
    config = _install(tmp_path, migrated=True)

    for hostile in ("../../etc/passwd.md", "work/memory-vault/../../escape.md", "nope/x.md"):
        matched = []
        for target, _name, prefix in config.vault_scan_targets():
            marker = f"{prefix.as_posix()}/"
            if not hostile.startswith(marker):
                continue
            root = Path(target).resolve()
            try:
                candidate = (root / Path(hostile[len(marker):])).resolve()
                candidate.relative_to(root)
            except (OSError, ValueError):
                continue
            matched.append(candidate)
        assert matched == [], (hostile, matched)


# -- re-home detection, in both layouts --------------------------------------


def _person(root: Path, rel: str, tags: list[str]) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    tag_line = "tags: [" + ", ".join(tags) + "]\n" if tags else ""
    path.write_text(
        f"---\ntype: person\n{tag_line}---\n# {path.stem}\n", encoding="utf-8"
    )


def test_rehome_finds_the_same_candidates_in_both_layouts(tmp_path: Path) -> None:
    """It returned ZERO candidates from every root after the migration.

    Its predicate was `<workspace>/People/...` relative to `memory-vault`, and
    inside a root's own vault there is no workspace segment — so the proposals UI
    silently lost every re-home hint.
    """
    from ciao.vault_rehome import detect_misfiled_people

    shared = _install(tmp_path / "a", migrated=False)
    _person(tmp_path / "a" / "memory-vault" / "personal", "People/Mo.md", ["scandit"])
    rooted = _install(tmp_path / "b", migrated=True)
    _person(tmp_path / "b" / "personal" / "memory-vault", "People/Mo.md", ["scandit"])

    before = detect_misfiled_people(
        shared.vault_root, workspaces=["personal", "work"],
        targets=shared.vault_scan_targets(),
    )
    reset_reroot_cache()
    after = detect_misfiled_people(
        rooted.vault_root, workspaces=["personal", "work"],
        targets=rooted.vault_scan_targets(),
    )

    assert [c.path for c in before] == [c.path for c in after]
    assert [c.destination for c in before] == [c.destination for c in after]
    assert any(c.path == "personal/People/Mo.md" for c in after), [c.path for c in after]


def test_the_candidate_identity_survives_the_migration(tmp_path: Path) -> None:
    """The identity string is what queue bullets contain and `_rehome_signal`
    joins on. Deriving it from the on-disk path would make every existing bullet
    stop matching its own note the moment an install migrated — silently, because
    a failed join renders as "no signal" rather than as an error."""
    from ciao.vault_rehome import detect_misfiled_people

    config = _install(tmp_path, migrated=True)
    _person(tmp_path / "personal" / "memory-vault", "People/Mo.md", ["scandit"])

    candidates = detect_misfiled_people(
        config.vault_root, workspaces=["personal", "work"],
        targets=config.vault_scan_targets(),
    )

    paths = [c.path for c in candidates]
    # Workspace plus vault-relative, NOT the on-disk `personal/memory-vault/...`.
    assert "personal/People/Mo.md" in paths
    assert not any("memory-vault" in p for p in paths), paths
    moved = next(c for c in candidates if c.path == "personal/People/Mo.md")
    assert moved.destination == "work/People/Mo.md"


def test_user_md_is_still_never_a_candidate_per_root(tmp_path: Path) -> None:
    """P5.9's rule has to survive the new scan path."""
    from ciao.vault_rehome import detect_misfiled_people

    config = _install(tmp_path, migrated=True)
    _person(tmp_path / "personal" / "memory-vault", "People/User.md", ["scandit"])

    candidates = detect_misfiled_people(
        config.vault_root, workspaces=["personal", "work"],
        targets=config.vault_scan_targets(),
    )

    assert not any(c.path.endswith("User.md") for c in candidates), [c.path for c in candidates]


def test_a_note_outside_a_people_directory_is_not_a_candidate(tmp_path: Path) -> None:
    from ciao.vault_rehome import detect_misfiled_people

    config = _install(tmp_path, migrated=True)
    _person(tmp_path / "personal" / "memory-vault", "Ideas/Mo.md", ["scandit"])

    candidates = detect_misfiled_people(
        config.vault_root, workspaces=["personal", "work"],
        targets=config.vault_scan_targets(),
    )

    assert not any("Ideas" in c.path for c in candidates), [c.path for c in candidates]
