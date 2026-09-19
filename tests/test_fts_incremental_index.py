"""The index pass must get cheaper without ever missing a change (issue #450).

Every `vault_search` runs `index_vault` first, so the pass is on the request
path and its cost is paid per search. Making it cheaper is only worth anything
if it still sees every edit, delete and rename — a stale index that answers a
search with the previous version of a note is far worse than a slow one, so the
freshness tests here are the ones that matter and the cost tests exist to stop
the walk quietly growing back.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from ciao import fts_search


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    fts_search.init_db(connection)
    return connection


def _note(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntype: note\n---\n# {path.stem}\n\n{body}\n", encoding="utf-8")


def _install(tmp_path: Path) -> tuple[Path, Path]:
    """An install root holding one workspace's vault. Returns (base, vault)."""
    vault = tmp_path / "personal" / "memory-vault"
    _note(vault / "Notes" / "Alpha.md", "the original body mentions kingfisher")
    return tmp_path, vault


def _paths(conn: sqlite3.Connection, base: Path, vault: Path, term: str) -> set[str]:
    return {
        row["path"]
        for row in fts_search.search_vault(
            conn, term, limit=20, path_prefix=fts_search.vault_key_prefix(vault, base)
        )
    }


# ── freshness ──────────────────────────────────────────────────────────────


def test_an_edited_note_is_found_under_its_new_text(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    base, vault = _install(tmp_path)
    fts_search.index_vault(conn, vault, path_base=base)

    _note(vault / "Notes" / "Alpha.md", "rewritten to mention pangolin instead")
    fts_search.index_vault(conn, vault, path_base=base)

    assert _paths(conn, base, vault, "pangolin") == {
        "personal/memory-vault/Notes/Alpha.md"
    }
    assert _paths(conn, base, vault, "kingfisher") == set()


def test_an_edit_that_restores_the_indexed_mtime_is_still_picked_up(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The `cp -p` / `tar -x` / `rsync --times` shape.

    Restoring a note from a backup, or unpacking a vault archive, writes new
    bytes under a timestamp the index has already recorded. An mtime-only
    comparison calls that "unchanged" and the restored text never becomes
    searchable — the note silently keeps answering with the version it had
    before the restore.
    """
    base, vault = _install(tmp_path)
    note = vault / "Notes" / "Alpha.md"
    fts_search.index_vault(conn, vault, path_base=base)
    indexed_at = note.stat()
    _note(note, "restored from a backup and mentions pangolin now")
    os.utime(note, (indexed_at.st_atime, indexed_at.st_mtime))
    assert note.stat().st_mtime == indexed_at.st_mtime

    fts_search.index_vault(conn, vault, path_base=base)

    assert _paths(conn, base, vault, "pangolin") == {
        "personal/memory-vault/Notes/Alpha.md"
    }


def test_a_same_length_rewrite_under_the_indexed_mtime_is_still_picked_up(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The case a (mtime, size) pair also misses.

    Same byte count, same restored mtime: only ctime separates the two
    versions, and the kernel stamps that on every inode change regardless of
    what the writer asks for.
    """
    base, vault = _install(tmp_path)
    note = vault / "Notes" / "Alpha.md"
    fts_search.index_vault(conn, vault, path_base=base)
    indexed_at = note.stat()
    original = note.read_text(encoding="utf-8")
    note.write_text(original.replace("kingfisher", "pangolinxx"), encoding="utf-8")
    os.utime(note, (indexed_at.st_atime, indexed_at.st_mtime))
    rewritten = note.stat()
    assert rewritten.st_size == indexed_at.st_size
    assert rewritten.st_mtime == indexed_at.st_mtime
    assert rewritten.st_ctime != indexed_at.st_ctime, (
        "this filesystem cannot distinguish the two versions at all"
    )

    fts_search.index_vault(conn, vault, path_base=base)

    assert _paths(conn, base, vault, "pangolinxx") == {
        "personal/memory-vault/Notes/Alpha.md"
    }


def test_a_deleted_note_disappears_from_search(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    base, vault = _install(tmp_path)
    fts_search.index_vault(conn, vault, path_base=base)
    assert _paths(conn, base, vault, "kingfisher")

    (vault / "Notes" / "Alpha.md").unlink()
    _indexed, removed = fts_search.index_vault(conn, vault, path_base=base)

    assert removed == 1
    assert _paths(conn, base, vault, "kingfisher") == set()
    assert conn.execute("SELECT count(*) FROM vault_meta").fetchone()[0] == 0


def test_a_renamed_note_is_found_under_its_new_path_only(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A rename keeps the bytes and the mtime, and only the key changes.

    Both halves have to happen in one pass: the new key indexed, the old key
    pruned. Missing the prune leaves a search hit pointing at a path that no
    longer opens.
    """
    base, vault = _install(tmp_path)
    fts_search.index_vault(conn, vault, path_base=base)

    (vault / "Notes" / "Alpha.md").rename(vault / "Notes" / "Renamed.md")
    fts_search.index_vault(conn, vault, path_base=base)

    assert _paths(conn, base, vault, "kingfisher") == {
        "personal/memory-vault/Notes/Renamed.md"
    }
    keys = {row[0] for row in conn.execute("SELECT path FROM vault_meta")}
    assert keys == {"personal/memory-vault/Notes/Renamed.md"}


def test_a_note_moved_between_directories_is_found_at_its_new_key(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    base, vault = _install(tmp_path)
    fts_search.index_vault(conn, vault, path_base=base)

    (vault / "People").mkdir()
    (vault / "Notes" / "Alpha.md").rename(vault / "People" / "Alpha.md")
    fts_search.index_vault(conn, vault, path_base=base)

    assert _paths(conn, base, vault, "kingfisher") == {
        "personal/memory-vault/People/Alpha.md"
    }


def test_a_note_added_after_the_first_pass_becomes_searchable(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    base, vault = _install(tmp_path)
    fts_search.index_vault(conn, vault, path_base=base)

    _note(vault / "Notes" / "Beta.md", "a brand new note about pangolin")
    fts_search.index_vault(conn, vault, path_base=base)

    assert _paths(conn, base, vault, "pangolin") == {
        "personal/memory-vault/Notes/Beta.md"
    }


def test_index_file_writes_a_signature_the_bulk_pass_accepts(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Force-indexing and the bulk pass must agree on what "unchanged" means.

    They write the same meta row from different code paths; if one of them
    recorded a partial signature, the other would re-read that file on every
    later search forever.
    """
    base, vault = _install(tmp_path)
    fts_search.index_file(conn, vault, vault / "Notes" / "Alpha.md", path_base=base)

    indexed, _removed = fts_search.index_vault(conn, vault, path_base=base)

    assert indexed == 0


def _upgraded_install(tmp_path: Path) -> tuple[sqlite3.Connection, Path, Path]:
    """An install indexed by the old code, then opened by the new code.

    The meta tables are rewound to the pre-signature schema after a normal
    pass, which is exactly the state an auto-update lands a user in: every row
    has the mtime the old code recorded and nothing else.
    """
    conn = sqlite3.connect(tmp_path / "search.db")
    fts_search.init_db(conn)
    base, vault = _install(tmp_path)
    _note(vault / "Notes" / "Beta.md", "a second note mentioning heron")
    fts_search.index_vault(conn, vault, path_base=base)
    for table in ("vault_meta", "transcript_meta"):
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}_signed")
        conn.execute(
            f"CREATE TABLE {table} (path TEXT PRIMARY KEY, mtime REAL, indexed_at TEXT)"
        )
        conn.execute(f"INSERT INTO {table} SELECT path, mtime, indexed_at FROM {table}_signed")
        conn.execute(f"DROP TABLE {table}_signed")
    conn.commit()
    fts_search.init_db(conn)  # the upgrade's column migration
    return conn, base, vault


def test_a_database_written_before_the_signature_columns_is_migrated(
    tmp_path: Path,
) -> None:
    """An existing install must not read NULL for a signature it just wrote.

    `CREATE TABLE IF NOT EXISTS` never adds a column, so without the explicit
    migration every pass on an upgraded install would re-read the whole vault,
    forever.
    """
    conn, base, vault = _upgraded_install(tmp_path)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(vault_meta)")}
    assert {"size", "ctime"} <= columns
    fts_search.index_vault(conn, vault, path_base=base)
    second, _removed = fts_search.index_vault(conn, vault, path_base=base)

    assert second == 0


def test_the_first_pass_after_an_upgrade_re_reads_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stall this avoids is minutes long, inside one search request.

    A pre-signature row has a NULL signature, and a NULL never equals a real
    one, so the obvious reading of "unknown means changed" re-reads and
    re-writes every note in the vault — a DELETE+INSERT per note against a
    populated fts5 index — on the first search after an auto-update. The row is
    adopted on the mtime that wrote it instead, and stamped from the stat
    already in hand.
    """
    conn, base, vault = _upgraded_install(tmp_path)

    reads: list[str] = []
    real_read = Path.read_text

    def spy(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.suffix == ".md":
            reads.append(str(self))
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)
    indexed, removed = fts_search.index_vault(conn, vault, path_base=base)

    assert (indexed, removed) == (0, 0)
    assert reads == [], "the upgrade pass must not re-read the vault"
    # And the rows are fully signed afterwards, so the strong check applies
    # from here on.
    rows = conn.execute("SELECT size, ctime FROM vault_meta").fetchall()
    assert rows and all(size is not None and ctime is not None for size, ctime in rows)


def test_a_meta_row_with_no_signature_is_re_read_not_adopted(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """"Predates the columns" and "was written without a signature" differ.

    Only the migration can say a row is legacy, and it says so by stamping a
    size no file can have. A bare NULL is a row some code path wrote without
    recording what it indexed — adopting that on its mtime would let a note the
    index never read pass as indexed, forever. It has to be re-read.
    """
    base, vault = _install(tmp_path)
    fts_search.index_vault(conn, vault, path_base=base)
    conn.execute("UPDATE vault_meta SET size = NULL, ctime = NULL")
    conn.commit()

    indexed, _removed = fts_search.index_vault(conn, vault, path_base=base)

    assert indexed == 1
    assert _paths(conn, base, vault, "kingfisher") == {
        "personal/memory-vault/Notes/Alpha.md"
    }


def test_a_note_edited_before_the_upgrade_pass_is_still_re_indexed(
    tmp_path: Path,
) -> None:
    """Adopting a legacy row must not become "trust every legacy row".

    The mtime still has to match. A note changed between the last pre-upgrade
    pass and the first post-upgrade one is the ordinary case, and missing it
    would be the stale index the whole change exists to avoid.
    """
    conn, base, vault = _upgraded_install(tmp_path)
    _note(vault / "Notes" / "Beta.md", "rewritten before the upgrade pass: pangolin")

    indexed, _removed = fts_search.index_vault(conn, vault, path_base=base)

    assert indexed == 1
    assert _paths(conn, base, vault, "pangolin") == {
        "personal/memory-vault/Notes/Beta.md"
    }
    assert _paths(conn, base, vault, "heron") == set()


# ── cost ───────────────────────────────────────────────────────────────────


def test_the_walk_never_descends_into_an_excluded_subtree(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Logs/` and `.obsidian/` were enumerated and stat'ed, then discarded.

    A vault's rolled-log archive is routinely larger than the vault itself, and
    every search paid for it. Excluding after the yield is what made the
    unchanged pass cost what it did.
    """
    base, vault = _install(tmp_path)
    for excluded in ("Logs", "Templates", ".obsidian", ".vault-trash"):
        _note(vault / excluded / "Inside.md", "should never be walked")

    visited: list[str] = []
    real_scandir = os.scandir

    def spy(path):  # type: ignore[no-untyped-def]
        visited.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", spy)
    fts_search.index_vault(conn, vault, path_base=base)

    assert not [
        seen
        for seen in visited
        if os.path.basename(seen)
        in {"Logs", "Templates", ".obsidian", ".vault-trash"}
    ]


def test_the_metadata_load_is_scoped_to_the_root_being_indexed(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """One database holds every workspace, so the unscoped SELECT read them all.

    Those rows can never match a key this pass produces — each key starts with
    the pass's own prefix — so the work was pure waste that grew with the size
    of the OTHER workspaces.
    """
    base, vault = _install(tmp_path)
    other = tmp_path / "work" / "memory-vault"
    _note(other / "Notes" / "Theirs.md", "a note in another workspace")
    fts_search.index_vault(conn, vault, path_base=base)
    fts_search.index_vault(conn, other, path_base=base)

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    fts_search.index_vault(conn, vault, path_base=base)
    conn.set_trace_callback(None)

    loads = [s for s in statements if "FROM vault_meta" in s]
    assert loads, "the pass must still read the rows it owns"
    assert all("path LIKE" in s for s in loads), (
        "the signature load must be restricted to this root's key prefix"
    )


def test_an_unchanged_pass_writes_nothing(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    base, vault = _install(tmp_path)
    fts_search.index_vault(conn, vault, path_base=base)

    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    indexed, removed = fts_search.index_vault(conn, vault, path_base=base)
    conn.set_trace_callback(None)

    assert (indexed, removed) == (0, 0)
    assert not [s for s in statements if s.lstrip().upper().startswith(("INSERT", "DELETE", "UPDATE"))]
