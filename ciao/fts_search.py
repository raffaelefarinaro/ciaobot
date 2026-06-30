"""SQLite FTS5 full-text indexing and search for vault and transcripts."""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import yaml

logger = logging.getLogger(__name__)

# Directory-based type inference (similar to vault_index.py)
EXCLUDED_VAULT_DIRS = {"Logs", "Templates", ".obsidian"}

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def get_db_path() -> Path:
    """Resolve the path to the SQLite search database.

    Defaults to ``~/.ciao/vault-fts.db``. Overridable via ``CIAO_MEMORY_DIR``
    so that tests can point to a temporary directory.
    """
    override = os.environ.get("CIAO_MEMORY_DIR", "").strip()
    if override:
        db_dir = Path(override).expanduser()
    else:
        db_dir = Path.home() / ".ciao"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "vault-fts.db"


def init_db(conn: sqlite3.Connection) -> None:
    """Create the virtual search tables and tracking metadata tables."""
    # SQLite FTS5 table for core memory-vault files
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS vault_fts USING fts5(
            path, title, body,
            tokenize='porter'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vault_meta (
            path TEXT PRIMARY KEY,
            mtime REAL,
            indexed_at TEXT
        )
    """)

    # SQLite FTS5 table for transcripts and meeting logs
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
            path, title, body,
            tokenize='porter'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transcript_meta (
            path TEXT PRIMARY KEY,
            mtime REAL,
            indexed_at TEXT
        )
    """)
    conn.commit()


def _parse_title(text: str, filename_stem: str) -> str:
    """Extract a title from frontmatter or the first H1, falling back to the filename stem."""
    m = FRONTMATTER_RE.match(text)
    if m:
        try:
            fm = yaml.safe_load(m.group(1))
            if isinstance(fm, dict):
                title = fm.get("title") or fm.get("name")
                if title:
                    return str(title).strip()
        except yaml.YAMLError:
            pass

    body = text[m.end():] if m else text
    h = H1_RE.search(body)
    if h:
        return h.group(1).strip()
    return filename_stem


def _index_directory(
    conn: sqlite3.Connection,
    root_dir: Path,
    meta_table: str,
    fts_table: str,
    file_pattern: str = "*.md",
    exclude_dirs: set[str] | None = None,
    exclude_files: set[str] | None = None,
) -> tuple[int, int]:
    """Incrementally index markdown files. Returns (indexed_count, removed_count)."""
    exclude_dirs = exclude_dirs or set()
    exclude_files = exclude_files or set()

    # Get existing indexed files and their mtimes
    cursor = conn.execute(f"SELECT path, mtime FROM {meta_table}")
    existing = {row[0]: row[1] for row in cursor.fetchall()}

    found_paths: set[str] = set()
    indexed_count = 0

    # Walk directory
    for md_path in root_dir.rglob(file_pattern):
        # Resolve path relative to memory-vault's parent (so it starts with memory-vault/)
        try:
            rel = md_path.relative_to(root_dir.parent)
        except ValueError:
            rel = md_path.relative_to(root_dir)
        rel_str = str(rel)

        # Skip excluded directories
        if any(p in exclude_dirs for p in rel.parts):
            continue
        # Skip specific excluded files
        if rel.name in exclude_files:
            continue

        found_paths.add(rel_str)

        try:
            stat = md_path.stat()
            mtime = stat.st_mtime
        except OSError:
            continue

        # Check if file changed
        if rel_str in existing and existing[rel_str] == mtime:
            continue

        try:
            text = md_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            logger.warning("FTS search: failed to read %s", md_path)
            continue

        title = _parse_title(text, md_path.stem)

        # Delete old index entry if it exists
        conn.execute(f"DELETE FROM {fts_table} WHERE path = ?", (rel_str,))
        # Insert new entry
        conn.execute(
            f"INSERT INTO {fts_table} (path, title, body) VALUES (?, ?, ?)",
            (rel_str, title, text),
        )
        # Update metadata
        conn.execute(
            f"INSERT OR REPLACE INTO {meta_table} (path, mtime, indexed_at) VALUES (?, ?, ?)",
            (rel_str, mtime, datetime.now(timezone.utc).isoformat()),
        )
        indexed_count += 1

    # Remove deleted files from the index
    removed_count = 0
    deleted_paths = set(existing.keys()) - found_paths
    for rel_str in deleted_paths:
        conn.execute(f"DELETE FROM {fts_table} WHERE path = ?", (rel_str,))
        conn.execute(f"DELETE FROM {meta_table} WHERE path = ?", (rel_str,))
        removed_count += 1

    if indexed_count > 0 or removed_count > 0:
        conn.commit()

    return indexed_count, removed_count


def index_vault(conn: sqlite3.Connection, vault_root: Path) -> tuple[int, int]:
    """Incremental indexer for core vault files (excludes Logs, Templates)."""
    return _index_directory(
        conn=conn,
        root_dir=vault_root,
        meta_table="vault_meta",
        fts_table="vault_fts",
        exclude_dirs=EXCLUDED_VAULT_DIRS,
        exclude_files={"INDEX.md", "MEMORY.md"},
    )


def index_logs(conn: sqlite3.Connection, vault_root: Path) -> tuple[int, int]:
    """Incremental indexer for conversation transcripts and meeting logs."""
    logs_root = vault_root / "Logs"
    if not logs_root.exists():
        return 0, 0
    return _index_directory(
        conn=conn,
        root_dir=logs_root,
        meta_table="transcript_meta",
        fts_table="transcript_fts",
    )


def index_file(conn: sqlite3.Connection, vault_root: Path, file_path: Path) -> bool:
    """Force re-index a single file (e.g. immediately after archiving a chat)."""
    if not file_path.exists():
        return False
    try:
        rel = file_path.relative_to(vault_root.parent)
    except ValueError:
        return False
    rel_str = str(rel)

    # Determine which table it belongs to
    is_log = "Logs" in rel.parts
    fts_table = "transcript_fts" if is_log else "vault_fts"
    meta_table = "transcript_meta" if is_log else "vault_meta"

    try:
        text = file_path.read_text(encoding="utf-8")
        stat = file_path.stat()
        mtime = stat.st_mtime
    except OSError:
        return False

    title = _parse_title(text, file_path.stem)

    conn.execute(f"DELETE FROM {fts_table} WHERE path = ?", (rel_str,))
    conn.execute(
        f"INSERT INTO {fts_table} (path, title, body) VALUES (?, ?, ?)",
        (rel_str, title, text),
    )
    conn.execute(
        f"INSERT OR REPLACE INTO {meta_table} (path, mtime, indexed_at) VALUES (?, ?, ?)",
        (rel_str, mtime, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return True


def search(
    conn: sqlite3.Connection,
    fts_table: str,
    query: str,
    limit: int = 10,
) -> list[dict[str, str]]:
    """Search FTS5 table with Porter stemmer query. Returns ranked results with snippets."""
    # Sanitize search term. If query is a simple string, escape double quotes
    # and wrap words. SQLite FTS5 MATCH syntax is powerful.
    # To support basic multi-word queries gracefully, we join words with AND.
    words = re.findall(r"\w+", query)
    if not words:
        return []

    # Join words with AND for proximity/co-occurrence
    match_query = " AND ".join(words)

    sql = f"""
        SELECT path, title, snippet({fts_table}, 2, '<<<', '>>>', '...', 32) AS snippet, rank
        FROM {fts_table}
        WHERE {fts_table} MATCH ?
        ORDER BY rank
        LIMIT ?
    """
    try:
        cursor = conn.execute(sql, (match_query, limit))
        rows = cursor.fetchall()
    except sqlite3.OperationalError:
        # Fall back to literal match if complex match expression syntax is invalid
        sql = f"""
            SELECT path, title, snippet({fts_table}, 2, '<<<', '>>>', '...', 32) AS snippet, rank
            FROM {fts_table}
            WHERE {fts_table} MATCH ?
            ORDER BY rank
            LIMIT ?
        """
        clean_query = query.replace('"', " ")
        escaped_query = f'"{clean_query}"'
        cursor = conn.execute(sql, (escaped_query, limit))
        rows = cursor.fetchall()

    return [
        {
            "path": row[0],
            "title": row[1],
            "snippet": row[2].replace("\n", " ").strip() if row[2] else "",
            "rank": f"{row[3]:.4f}",
        }
        for row in rows
    ]


def search_vault(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[dict[str, str]]:
    return search(conn, "vault_fts", query, limit)


def search_logs(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[dict[str, str]]:
    return search(conn, "transcript_fts", query, limit)
