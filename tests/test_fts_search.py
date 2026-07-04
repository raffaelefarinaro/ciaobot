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
