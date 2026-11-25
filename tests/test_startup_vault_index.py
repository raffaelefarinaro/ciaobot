from __future__ import annotations

import os
from pathlib import Path
import pytest

from ciao.main import _refresh_vault_index
from ciao import vault_index


def test_refresh_vault_index_uses_packaged_module_without_workspace_script(tmp_path: Path) -> None:
    vault = tmp_path / "memory-vault"
    note = vault / "People" / "Ada.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\ntype: person\ntitle: Ada\ntags: [test]\n---\n# Ada\n",
        encoding="utf-8",
    )

    assert not (tmp_path / "scripts" / "vault_index.py").exists()

    assert _refresh_vault_index(tmp_path, vault, ciao_owned_roots={vault}) is True

    index = vault / "INDEX.md"
    assert index.is_file()
    assert "Ada" in index.read_text(encoding="utf-8")


def test_ciao_owned_local_index_refresh_replaces_previous_snapshot(tmp_path: Path) -> None:
    vault = tmp_path / "memory-vault"
    note = vault / "People" / "Ada.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\ntype: person\n---\n# Ada\n", encoding="utf-8")
    owned_roots = {vault}

    assert _refresh_vault_index(tmp_path, vault, ciao_owned_roots=owned_roots) is True
    index = vault / "INDEX.md"
    first = index.read_text(encoding="utf-8")
    note.unlink()
    (vault / "People" / "Bea.md").write_text(
        "---\ntype: person\n---\n# Bea\n", encoding="utf-8"
    )

    assert _refresh_vault_index(tmp_path, vault, ciao_owned_roots=owned_roots) is True
    second = index.read_text(encoding="utf-8")
    assert second != first
    assert "Bea" in second
    assert "Ada" not in second


def test_unowned_adopted_index_is_preserved_on_refresh(tmp_path: Path) -> None:
    root = tmp_path / "adopted-vault"
    root.mkdir()
    prior = root / "INDEX.md"
    prior.write_text("sync-owned index\n", encoding="utf-8")

    assert _refresh_vault_index(
        tmp_path,
        targets=[(root, "adopted", Path("adopted"))],
        ciao_owned_roots=set(),
    ) is True
    assert prior.read_text(encoding="utf-8") == "sync-owned index\n"


def test_refresh_vault_index_skips_missing_vault_root(tmp_path: Path, caplog) -> None:
    # Bootstrap mode starts before the wizard created the vault: skip with an
    # INFO log, never raise, and never scaffold the vault preemptively.
    vault = tmp_path / "memory-vault"

    with caplog.at_level("INFO", logger="ciao.main"):
        assert _refresh_vault_index(tmp_path, vault) is False

    assert not vault.exists()
    assert any("skipping index refresh" in r.message for r in caplog.records)
    assert all(r.levelname != "WARNING" for r in caplog.records)


def test_refresh_vault_index_reports_incomplete_per_root_scan(tmp_path: Path) -> None:
    available = tmp_path / "available"
    available.mkdir()
    missing = tmp_path / "missing-cloud-root"

    assert _refresh_vault_index(
        tmp_path, targets=[(available, "home", Path("home")), (missing, "drive", Path("drive"))],
        ciao_owned_roots={available},
    ) is False
    assert (available / "INDEX.md").is_file()
    assert not missing.exists()


def test_adopted_startup_scan_keeps_cloud_markdown_index_unmanaged(
    tmp_path: Path,
) -> None:
    import sqlite3

    from ciao.fts_search import (
        adopted_vault_key_prefix,
        connected_db_path,
        search_vault,
    )

    root = tmp_path / "adopted"
    note = root / "People" / "Mira.md"
    note.parent.mkdir(parents=True)
    note.write_text("# orchid garden\n", encoding="utf-8")
    (root / "INDEX.md").write_bytes(b"cloud-owned sentinel\r\n")

    assert _refresh_vault_index(
        tmp_path,
        targets=[(root, "personal", Path("personal"))],
        ciao_owned_roots=set(),
        preserve_index_roots={root},
    )

    assert (root / "INDEX.md").read_bytes() == b"cloud-owned sentinel\r\n"
    conn = sqlite3.connect(connected_db_path(tmp_path / ".runtime", root))
    try:
        assert search_vault(
            conn,
            "orchid",
            path_prefix=adopted_vault_key_prefix(root, "personal"),
        )
    finally:
        conn.close()


def test_partial_scan_preserves_previous_index_and_continues_other_roots(
    tmp_path: Path, monkeypatch,
) -> None:
    partial = tmp_path / "partial"
    healthy = tmp_path / "healthy"
    partial.mkdir()
    healthy.mkdir()
    prior = partial / "INDEX.md"
    prior.write_text("previous complete index\n", encoding="utf-8")
    original_scan = vault_index.scan_vault

    def scan(root: Path, **kwargs):
        if root == partial:
            raise OSError("cloud scan interrupted after partial enumeration")
        return original_scan(root, **kwargs)

    monkeypatch.setattr(vault_index, "scan_vault", scan)
    assert _refresh_vault_index(
        tmp_path,
        targets=[(partial, "drive", Path("drive")), (healthy, "home", Path("home"))],
        ciao_owned_roots={healthy},
    ) is False

    assert prior.read_text(encoding="utf-8") == "previous complete index\n"
    assert (healthy / "INDEX.md").is_file()


def test_unreadable_note_does_not_replace_existing_index(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "cloud-vault"
    note = root / "Projects" / "Partial.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Note\n", encoding="utf-8")
    prior = root / "INDEX.md"
    prior.write_text("last complete snapshot\n", encoding="utf-8")
    original_read_text = Path.read_text

    def unreadable(path: Path, *args, **kwargs):
        if path == note:
            raise PermissionError("cloud placeholder not hydrated")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    assert _refresh_vault_index(
        tmp_path, targets=[(root, "drive", Path("drive"))], ciao_owned_roots={root}
    ) is False
    assert prior.read_text(encoding="utf-8") == "last complete snapshot\n"


def test_single_root_scan_is_strict_before_replacing_existing_index(
    tmp_path: Path, monkeypatch,
) -> None:
    import pytest

    root = tmp_path / "memory-vault"
    note = root / "Projects" / "Partial.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Note\n", encoding="utf-8")
    prior = root / "INDEX.md"
    prior.write_text("last complete snapshot\n", encoding="utf-8")
    from ciao import fts_search

    original_read = fts_search.read_connected_note

    def unreadable(base: Path, relative: Path):
        if base / relative == note:
            raise PermissionError("cloud placeholder is unavailable")
        return original_read(base, relative)

    monkeypatch.setattr(fts_search, "read_connected_note", unreadable)
    assert _refresh_vault_index(tmp_path, root) is False
    assert prior.read_text(encoding="utf-8") == "last complete snapshot\n"


def test_index_replace_failure_preserves_previous_file(tmp_path: Path, monkeypatch) -> None:
    import pytest

    from ciao.vault_index import write_index_file

    dest = tmp_path / "vault" / "INDEX.md"
    dest.parent.mkdir()
    dest.write_text("previous complete index\n", encoding="utf-8")

    def failed_replace(*args, **kwargs):
        raise OSError("simulated interrupted replacement")

    monkeypatch.setattr(os, "replace", failed_replace)
    with pytest.raises(OSError, match="interrupted replacement"):
        write_index_file([], dest, replace_existing=True)

    assert dest.read_text(encoding="utf-8") == "previous complete index\n"
    assert list(dest.parent.glob(".ciao-index-*.tmp")) == []


def test_repeated_refresh_cleans_each_temporary_file(tmp_path: Path) -> None:
    from ciao.vault_index import write_index_file

    dest = tmp_path / "vault" / "INDEX.md"
    dest.parent.mkdir()
    dest.write_text("previous complete index\n", encoding="utf-8")

    for _ in range(3):
        write_index_file([], dest, replace_existing=True)

    assert dest.read_bytes().startswith(b"<!-- generated by ciao vault-index")
    assert list(dest.parent.glob(".ciao-index-*.tmp")) == []


def test_index_flush_failure_preserves_previous_file(tmp_path: Path, monkeypatch) -> None:
    import pytest

    from ciao.vault_index import write_index_file

    dest = tmp_path / "vault" / "INDEX.md"
    dest.parent.mkdir()
    dest.write_text("previous complete index\n", encoding="utf-8")

    def fail_fsync(_fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="disk full"):
        write_index_file([], dest, replace_existing=True)

    assert dest.read_text(encoding="utf-8") == "previous complete index\n"


def test_index_publication_never_replaces_an_externally_owned_index(
    tmp_path: Path,
) -> None:
    import pytest

    from ciao.vault_index import write_index_file

    dest = tmp_path / "vault" / "INDEX.md"
    dest.parent.mkdir()
    dest.write_text("sync-owned index\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        write_index_file([], dest)

    assert dest.read_text(encoding="utf-8") == "sync-owned index\n"


def test_index_publication_creates_an_absent_index(tmp_path: Path) -> None:
    from ciao.vault_index import write_index_file

    dest = tmp_path / "vault" / "INDEX.md"
    dest.parent.mkdir()

    write_index_file([], dest)

    assert dest.read_bytes().startswith(b"<!-- generated by ciao vault-index")


def test_connected_lookup_keeps_original_paths_and_local_snapshot_on_scan_failure(tmp_path, monkeypatch):
    import sqlite3
    from ciao import fts_search

    runtime = tmp_path / "runtime"
    roots = [tmp_path / "alpha", tmp_path / "beta"]
    for root in roots:
        root.mkdir()
        (root / "A.md").write_text(f"# {root.name}\n[Link](./B.md)\n")
        (root / "B.md").write_text("# Second\n")
        entries = fts_search.lookup_connected_entries(root, "personal", runtime, tmp_path)
        assert entries[0].related == ["B.md"]
        assert (root / entries[0].path).is_file()
        assert not (root / "INDEX.md").exists()
    (roots[0] / "A.md").write_text("# Changed\n")
    old_walk = fts_search._walk_notes

    def partial_walk(*args):
        yield from old_walk(*args)
        raise PermissionError("interrupted traversal")

    monkeypatch.setattr(fts_search, "_walk_notes", partial_walk)
    with pytest.raises(PermissionError, match="interrupted traversal"):
        fts_search.lookup_connected_entries(roots[0], "personal", runtime, tmp_path, refresh=True)
    for root in roots:
        entries = fts_search.lookup_connected_entries(root, "personal", runtime, tmp_path)
        assert entries[0].title == root.name
        conn = sqlite3.connect(fts_search.connected_db_path(runtime, root))
        try:
            namespace = fts_search.adopted_vault_key_prefix(root, "personal")
            assert fts_search.search_vault(conn, root.name, path_prefix=namespace)
            assert not fts_search.search_vault(conn, "Changed", path_prefix=namespace)
        finally:
            conn.close()
