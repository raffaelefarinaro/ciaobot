"""Unit tests for SQLite FTS5 search module ciao.fts_search."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from ciao import fts_search


@pytest.fixture
def temp_vault(tmp_path: Path) -> Path:
    """Create a temporary memory-vault structure."""
    vault = tmp_path / "memory-vault"
    vault.mkdir()

    # Core vault files
    people = vault / "People"
    people.mkdir()
    (people / "User.md").write_text(
        "---\ntags: [personal, core]\nname: Alex Example\n---\n# User Profile\nUser resides in Zurich.",
        encoding="utf-8",
    )

    projects = vault / "Projects"
    projects.mkdir()
    (projects / "Ciaobot-Search.md").write_text(
        "---\ntype: project\nworkspace: personal\n---\n# Search Improvements\nWe should discuss the wedding venue next week.",
        encoding="utf-8",
    )

    # Excluded files
    (vault / "INDEX.md").write_text("# Auto Index", encoding="utf-8")
    (vault / "MEMORY.md").write_text("# Curator memory", encoding="utf-8")

    # Excluded directories
    templates = vault / "Templates"
    templates.mkdir()
    (templates / "Project-Template.md").write_text("# Template doc", encoding="utf-8")

    # Log files
    logs = vault / "Logs"
    logs.mkdir()
    chats = logs / "Chats"
    chats.mkdir()
    (chats / "2026-06-08-chat.md").write_text(
        "# Session Curation\nDiscussed learning model and marrying options.",
        encoding="utf-8",
    )

    return vault


@pytest.fixture
def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    fts_search.init_db(conn)
    return conn


def test_init_db(db_conn: sqlite3.Connection) -> None:
    # Verify tables are created
    tables = [
        row[0]
        for row in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()
    ]
    assert "vault_fts" in tables
    assert "vault_meta" in tables
    assert "transcript_fts" in tables
    assert "transcript_meta" in tables


def test_index_vault_incremental(db_conn: sqlite3.Connection, temp_vault: Path) -> None:
    # First indexing pass
    indexed, removed = fts_search.index_vault(db_conn, temp_vault)
    assert indexed == 2  # User.md and Ciaobot-Search.md
    assert removed == 0

    # Second pass with no changes should skip
    indexed, removed = fts_search.index_vault(db_conn, temp_vault)
    assert indexed == 0
    assert removed == 0


def test_index_vault_modified_reindex(db_conn: sqlite3.Connection, temp_vault: Path) -> None:
    fts_search.index_vault(db_conn, temp_vault)

    # Modify one file and change its mtime artificially
    user_md = temp_vault / "People" / "User.md"
    user_md.write_text(
        "---\nname: Alex Example\n---\n# User Profile\nModified resides in Zurich.",
        encoding="utf-8",
    )
    # Force modification time change
    stat = user_md.stat()
    new_mtime = stat.st_mtime + 5.0
    os.utime(user_md, (new_mtime, new_mtime))

    indexed, removed = fts_search.index_vault(db_conn, temp_vault)
    assert indexed == 1
    assert removed == 0

    # Verify search matches new content
    results = fts_search.search_vault(db_conn, "Modified")
    assert len(results) == 1
    assert "Modified" in results[0]["snippet"]


def test_index_vault_deletion(db_conn: sqlite3.Connection, temp_vault: Path) -> None:
    fts_search.index_vault(db_conn, temp_vault)

    # Delete User.md
    user_md = temp_vault / "People" / "User.md"
    user_md.unlink()

    indexed, removed = fts_search.index_vault(db_conn, temp_vault)
    assert indexed == 0
    assert removed == 1

    # Verify no search results for User
    results = fts_search.search_vault(db_conn, "Alex")
    assert len(results) == 0


def test_index_logs(db_conn: sqlite3.Connection, temp_vault: Path) -> None:
    # Logs are excluded from index_vault
    fts_search.index_vault(db_conn, temp_vault)
    results = fts_search.search_vault(db_conn, "marrying")
    assert len(results) == 0

    # Index logs separately
    indexed, removed = fts_search.index_logs(db_conn, temp_vault)
    assert indexed == 1
    assert removed == 0

    # Search logs for "marry" (stemming should match "marrying")
    results = fts_search.search_logs(db_conn, "marry")
    assert len(results) == 1
    assert "2026-06-08-chat" in results[0]["path"]
    assert "marrying" in results[0]["snippet"].lower()


def test_index_file(db_conn: sqlite3.Connection, temp_vault: Path) -> None:
    user_md = temp_vault / "People" / "User.md"
    # Index single file directly
    success = fts_search.index_file(db_conn, temp_vault, user_md)
    assert success is True

    # Verify indexed
    results = fts_search.search_vault(db_conn, "Zurich")
    assert len(results) == 1
    assert "Alex" in results[0]["title"]


def test_search_stemming_and_ranking(db_conn: sqlite3.Connection, temp_vault: Path) -> None:
    fts_search.index_vault(db_conn, temp_vault)

    # Check stemming: "weddings" should match "wedding" in Ciaobot-Search.md
    results = fts_search.search_vault(db_conn, "weddings")
    assert len(results) == 1
    assert "Ciaobot-Search.md" in results[0]["path"]
    assert "wedding" in results[0]["snippet"]

    # Check proximity/ranking
    results = fts_search.search_vault(db_conn, "wedding venue")
    assert len(results) == 1
    assert "venue" in results[0]["snippet"]


def test_search_snippet_omits_unmatched_adjacent_private_lines(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    vault = tmp_path / "memory-vault"
    people = vault / "People"
    people.mkdir(parents=True)
    note = people / "Ada.md"
    note.write_text(
        "# Ada Lovelace\n\n"
        "Ada prefers written project updates on Fridays.\n"
        "Internal sentinel: VAULT_PRIVATE_SENTINEL_2026.\n",
        encoding="utf-8",
    )

    fts_search.index_vault(db_conn, vault)
    results = fts_search.search_vault(db_conn, "Ada preference")

    assert len(results) == 1
    assert "written" in results[0]["snippet"]
    assert "Fridays" in results[0]["snippet"]
    assert "VAULT_PRIVATE_SENTINEL_2026" not in results[0]["snippet"]


# -- multi-root indexing (P10.6 defect, found on real data) ------------------
#
# Measured on an APFS clone of the reference install after the migration: a
# rebuild reported "personal 158 indexed, work 426 indexed" and left 426 rows in
# the database. Personal's notes were gone. Two causes, both in one line each:
# the stored key was relative to each root's own parent, so both roots keyed as
# `memory-vault/...` and the second pass overwrote the first; and the prune was
# unscoped, so it deleted every row the first pass had written.


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Two agent roots, each with a vault holding a note of the SAME name."""
    install = tmp_path / "install"
    for name in ("personal", "work"):
        notes = install / name / "memory-vault" / "People"
        notes.mkdir(parents=True)
        (notes / "User.md").write_text(
            f"---\ntitle: {name} user\n---\n# {name}\nfindme {name}\n", encoding="utf-8"
        )
    return install, install / "personal" / "memory-vault", install / "work" / "memory-vault"


def test_two_roots_with_the_same_note_name_both_survive_one_rebuild(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    install, personal, work = _roots(tmp_path)

    fts_search.index_vault(db_conn, personal, path_base=install)
    fts_search.index_vault(db_conn, work, path_base=install)

    paths = {row[0] for row in db_conn.execute("SELECT path FROM vault_meta")}
    assert paths == {
        "personal/memory-vault/People/User.md",
        "work/memory-vault/People/User.md",
    }


def test_indexing_one_root_does_not_prune_another(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The prune is scoped to the subtree being indexed.

    Unscoped it deleted every row outside that subtree, so a two-workspace
    install kept exactly one workspace's notes searchable at a time and every
    switch paid a full re-index of the incoming one.
    """
    install, personal, work = _roots(tmp_path)
    fts_search.index_vault(db_conn, personal, path_base=install)

    indexed, removed = fts_search.index_vault(db_conn, work, path_base=install)

    assert (indexed, removed) == (1, 0)
    assert db_conn.execute("SELECT count(*) FROM vault_meta").fetchone()[0] == 2


def test_a_deleted_note_is_still_pruned_within_its_own_root(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Scoping the prune must not stop it pruning what it should."""
    install, personal, work = _roots(tmp_path)
    fts_search.index_vault(db_conn, personal, path_base=install)
    fts_search.index_vault(db_conn, work, path_base=install)
    (personal / "People" / "User.md").unlink()

    _indexed, removed = fts_search.index_vault(db_conn, personal, path_base=install)

    assert removed == 1
    paths = {row[0] for row in db_conn.execute("SELECT path FROM vault_meta")}
    assert paths == {"work/memory-vault/People/User.md"}


def test_a_scoped_search_cannot_return_another_roots_note(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Isolation used to be a side effect of the prune deleting the other root.

    With the prune scoped, both roots' rows coexist, so the filter has to be
    explicit or every search sees every workspace.
    """
    install, personal, work = _roots(tmp_path)
    fts_search.index_vault(db_conn, personal, path_base=install)
    fts_search.index_vault(db_conn, work, path_base=install)

    prefix = fts_search.vault_key_prefix(personal, install)
    rows = fts_search.search_vault(db_conn, "findme", limit=10, path_prefix=prefix)

    assert [row["path"] for row in rows] == ["personal/memory-vault/People/User.md"]
    unscoped = fts_search.search_vault(db_conn, "findme", limit=10)
    assert len(unscoped) == 2


def test_a_workspace_name_holding_a_like_wildcard_does_not_widen_the_scope(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """`_` is a single-character wildcard in LIKE, and workspace names are the
    user's, so `my_work/` would otherwise also match `my-work/`."""
    install = tmp_path / "install"
    for name in ("my_work", "my-work"):
        notes = install / name / "memory-vault"
        notes.mkdir(parents=True)
        (notes / "note.md").write_text(f"# {name}\nfindme\n", encoding="utf-8")
        fts_search.index_vault(db_conn, notes, path_base=install)

    prefix = fts_search.vault_key_prefix(install / "my_work" / "memory-vault", install)
    rows = fts_search.search_vault(db_conn, "findme", limit=10, path_prefix=prefix)

    assert [row["path"] for row in rows] == ["my_work/memory-vault/note.md"]


def test_vault_key_prefix_names_the_root_in_both_layouts(tmp_path: Path) -> None:
    install = tmp_path / "install"
    shared = install / "memory-vault" / "personal"
    rooted = install / "personal" / "memory-vault"
    shared.mkdir(parents=True)
    rooted.mkdir(parents=True)

    assert fts_search.vault_key_prefix(shared, install) == os.path.join(
        "memory-vault", "personal", ""
    )
    assert fts_search.vault_key_prefix(rooted, install) == os.path.join(
        "personal", "memory-vault", ""
    )
    # No base means the legacy default: relative to the indexed directory's own
    # parent, which is exactly why two roots collided.
    assert fts_search.vault_key_prefix(rooted, None) == os.path.join("memory-vault", "")
    assert fts_search.vault_key_prefix(shared, None) == os.path.join("personal", "")


def test_changing_the_key_base_wipes_the_index_rather_than_mixing_formats(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Two bases in one table means the same note under two keys, and neither
    prune removes the other. Dropping is honest: the index is derived."""
    install, personal, _work = _roots(tmp_path)
    fts_search.index_vault(db_conn, personal, path_base=install)
    assert db_conn.execute("SELECT count(*) FROM vault_meta").fetchone()[0] == 1

    fts_search.index_vault(db_conn, personal, path_base=personal.parent)

    paths = {row[0] for row in db_conn.execute("SELECT path FROM vault_meta")}
    assert paths == {os.path.join("memory-vault", "People", "User.md")}


def test_no_test_can_reach_the_real_search_database(tmp_path: Path) -> None:
    """The guard that stops a test wiping the developer's own vault index.

    `get_db_path()` falls back to `~/.ciao/vault-fts.db`, and a test that
    migrated a fixture install rebuilt the index without an explicit `db_path` —
    wiping the real database and refilling it with four fixture notes. The
    conftest fixture is autouse and unconditional because remembering per test is
    what failed.
    """
    resolved = fts_search.get_db_path()

    assert Path.home() not in resolved.parents or "pytest" in str(resolved), resolved
    assert resolved != Path.home() / ".ciao" / "vault-fts.db"
