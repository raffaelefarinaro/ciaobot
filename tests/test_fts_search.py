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


# -- a vault outside the key base (re-rooting defect) ------------------------
#
# `CIAO_VAULT_MODE=existing` with an absolute vault root points a workspace at a
# vault OUTSIDE the install, while `path_base` stays the install root. The scope
# then had no prefix and degraded to `""` — which the prune reads as "every row"
# and the search filter reads as "every root". So one index pass over such a
# vault deleted every other workspace's rows, and its search answered with
# whatever notes were left, from any workspace.


def _outside_vault(tmp_path: Path) -> Path:
    """A vault that is not under the install root, with one note in it."""
    outside = tmp_path / "elsewhere" / "notes"
    outside.mkdir(parents=True)
    (outside / "Outside.md").write_text("# Outside\nfindme outside\n", encoding="utf-8")
    return outside


def test_indexing_a_vault_outside_the_key_base_prunes_nothing(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The data-loss half: an unresolvable scope must not mean "every row".

    Before this, the pass below removed both agent roots' notes from the index —
    every workspace on the install lost its search results to one chat whose
    vault happens to live elsewhere.
    """
    install, personal, work = _roots(tmp_path)
    fts_search.index_vault(db_conn, personal, path_base=install)
    fts_search.index_vault(db_conn, work, path_base=install)
    outside = _outside_vault(tmp_path)

    _indexed, removed = fts_search.index_vault(db_conn, outside, path_base=install)

    assert removed == 0
    paths = {row[0] for row in db_conn.execute("SELECT path FROM vault_meta")}
    assert "personal/memory-vault/People/User.md" in paths
    assert "work/memory-vault/People/User.md" in paths


def test_a_vault_outside_the_key_base_cannot_search_another_roots_notes(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The disclosure half: an unresolvable scope must not mean "every root".

    No prefix describes an outside vault's rows, so the filter fails closed —
    an empty result set — rather than handing that chat every other workspace's
    notes. Scoping the prune is what makes this load-bearing: those rows now
    survive the pass, so `""` would return them.
    """
    install, personal, work = _roots(tmp_path)
    fts_search.index_vault(db_conn, personal, path_base=install)
    fts_search.index_vault(db_conn, work, path_base=install)
    outside = _outside_vault(tmp_path)
    fts_search.index_vault(db_conn, outside, path_base=install)

    prefix = fts_search.vault_key_prefix(outside, install)
    rows = fts_search.search_vault(db_conn, "findme", limit=10, path_prefix=prefix)

    assert rows == []
    # The other roots keep their own scoped results.
    personal_prefix = fts_search.vault_key_prefix(personal, install)
    personal_rows = fts_search.search_vault(
        db_conn, "findme", limit=10, path_prefix=personal_prefix
    )
    assert [row["path"] for row in personal_rows] == [
        "personal/memory-vault/People/User.md"
    ]


def test_a_deleted_note_is_still_pruned_when_the_scope_resolves(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Refusing to prune is confined to the unresolvable case.

    The legacy single-vault call (no `path_base`, so the base is the vault's own
    parent) and the per-root call must both keep pruning what they should.
    """
    install, personal, _work = _roots(tmp_path)
    note = personal / "People" / "User.md"
    fts_search.index_vault(db_conn, personal, path_base=None)
    note.unlink()

    _indexed, removed = fts_search.index_vault(db_conn, personal, path_base=None)

    assert removed == 1
    assert db_conn.execute("SELECT count(*) FROM vault_meta").fetchone()[0] == 0
    assert install.exists()


# -- transcript scoping (the --logs half of the same leak) --------------------
#
# `search_vault` grew a `path_prefix` when the prune stopped deleting sibling
# roots' rows; `search_logs` did not. So one database holding two roots'
# transcripts answered every `--logs` query with both, handing over the other
# workspace's chat titles and snippets.


def _log_roots(tmp_path: Path) -> Path:
    """Two agent roots, each keeping its own transcript archive in its vault.

    The pre-promotion layout: `logs_root_for` derives `<vault>/Logs` until the
    receipt says the archive was promoted, and `index_file` keys an archived chat
    the same way, so per-root transcript rows are reachable on a real install.
    """
    install = tmp_path / "install"
    for name in ("personal", "work"):
        chats = install / name / "memory-vault" / "Logs" / "Chats"
        chats.mkdir(parents=True)
        (chats / "2026-06-08-chat.md").write_text(
            f"# {name} session\nfindme {name}\n", encoding="utf-8"
        )
    return install


def test_a_scoped_transcript_search_cannot_return_another_roots_chat(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    install = _log_roots(tmp_path)
    for name in ("personal", "work"):
        vault = install / name / "memory-vault"
        fts_search.index_logs(
            db_conn, vault, logs_root=vault / "Logs", path_base=install
        )

    personal_logs = install / "personal" / "memory-vault" / "Logs"
    prefix = fts_search.logs_key_prefix(personal_logs, install)
    rows = fts_search.search_logs(db_conn, "findme", limit=10, path_prefix=prefix)

    assert [row["path"] for row in rows] == [
        os.path.join("personal", "memory-vault", "Logs", "Chats", "2026-06-08-chat.md")
    ]
    # Both roots' rows are in the one database, which is what makes the filter
    # load-bearing rather than decorative.
    assert len(fts_search.search_logs(db_conn, "findme", limit=10)) == 2


def test_logs_key_prefix_names_the_archive_in_both_layouts(
    db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    install = _log_roots(tmp_path)
    promoted = install / "Logs"
    promoted.mkdir(parents=True)
    per_root = install / "personal" / "memory-vault" / "Logs"

    # Promoted (D5): one archive shared by every workspace, so one prefix.
    assert fts_search.logs_key_prefix(promoted, install) == os.path.join("Logs", "")
    assert fts_search.logs_key_prefix(per_root, install) == os.path.join(
        "personal", "memory-vault", "Logs", ""
    )

    # An archive outside the key base has no prefix at all, so it fails closed:
    # no results, rather than every root's transcripts.
    fts_search.index_logs(
        db_conn,
        per_root.parent,
        logs_root=per_root,
        path_base=install,
    )
    outside = tmp_path / "elsewhere" / "Logs"
    outside.mkdir(parents=True)
    outside_prefix = fts_search.logs_key_prefix(outside, install)
    assert fts_search.search_logs(db_conn, "findme", path_prefix=outside_prefix) == []
    assert len(fts_search.search_logs(db_conn, "findme")) == 1


def test_bookkeeping_files_are_never_indexed(
    db_conn: sqlite3.Connection, temp_vault: Path
) -> None:
    """The memory pipeline's own queue and logs stay out of recall.

    Indexed, they ranked above real notes for ordinary queries — the proposals
    queue was a top-3 result for "fiancee" on a live vault.
    """
    workspace = temp_vault / "Workspace"
    workspace.mkdir()
    (workspace / "Memory-Proposals.md").write_text(
        "# Memory Proposals\n\n- [review] the wedding venue is Tortoreto\n",
        encoding="utf-8",
    )
    (workspace / "Curation-Log.md").write_text(
        "# Curation Log\n\nProcessed wedding proposals.\n", encoding="utf-8"
    )
    fts_search.index_vault(db_conn, temp_vault)

    paths = [r["path"] for r in fts_search.search_vault(db_conn, "wedding", limit=10)]
    assert not any("Memory-Proposals" in p or "Curation-Log" in p for p in paths)
    assert any("Ciaobot-Search" in p for p in paths)  # real notes still hit


def test_search_false_frontmatter_opts_a_note_out(
    db_conn: sqlite3.Connection, temp_vault: Path
) -> None:
    """`search: false` removes a note from the index, including old rows."""
    note = temp_vault / "Scratch.md"
    note.write_text("# Scratch\nA wedding scratchpad.\n", encoding="utf-8")
    fts_search.index_vault(db_conn, temp_vault)
    paths = [r["path"] for r in fts_search.search_vault(db_conn, "scratchpad")]
    assert any("Scratch" in p for p in paths)

    time.sleep(0.01)
    note.write_text(
        "---\nsearch: false\n---\n# Scratch\nA wedding scratchpad.\n",
        encoding="utf-8",
    )
    os.utime(note, (time.time() + 5, time.time() + 5))
    fts_search.index_vault(db_conn, temp_vault)
    assert fts_search.search_vault(db_conn, "scratchpad") == []


def test_index_file_honours_exclusions(
    db_conn: sqlite3.Connection, temp_vault: Path
) -> None:
    """Force-indexing a reserved or opted-out file removes its rows instead."""
    workspace = temp_vault / "Workspace"
    workspace.mkdir()
    queue = workspace / "Memory-Proposals.md"
    queue.write_text("# Memory Proposals\n\n- [review] secret pending fact\n", encoding="utf-8")

    assert fts_search.index_file(db_conn, temp_vault, queue) is False
    assert fts_search.search_vault(db_conn, "secret pending") == []
