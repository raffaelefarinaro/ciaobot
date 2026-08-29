"""SQLite FTS5 full-text indexing and search for vault and transcripts."""

from __future__ import annotations

import json
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

# Vault bookkeeping the memory pipeline itself writes (casefolded names).
# Indexing them made the proposals queue and the curation logs rank above real
# notes for ordinary recall queries — the memory system's paperwork must never
# compete with the memories it manages. Matched only directly under a
# `Workspace/` directory — where the pipeline writes them — so a user's own
# note that happens to share a name (`projects/team/Weekly-Review-Log.md`)
# stays searchable.
RESERVED_UNINDEXED_FILES = frozenset(
    {
        "memory-proposals.md",
        "memory-consolidations.md",
        "curation-log.md",
        "weekly-review-log.md",
    }
)


def _is_reserved_bookkeeping(rel_to_root: Path) -> bool:
    """True for the memory pipeline's own files, exactly where it writes them.

    ``rel_to_root`` is the path relative to the indexed root (the vault). The
    pipeline only ever writes these files at ``<vault>/Workspace/<name>``, so
    the match is exact: a user's note under any other directory that happens
    to be named ``workspace`` (``projects/acme/workspace/Curation-Log.md``)
    stays searchable.
    """
    return (
        len(rel_to_root.parts) == 2
        and rel_to_root.parts[0].casefold() == "workspace"
        and rel_to_root.name.casefold() in RESERVED_UNINDEXED_FILES
    )


def _settle_excluded_row(
    conn: sqlite3.Connection, fts_table: str, meta_table: str, rel_str: str, mtime: float
) -> None:
    """Remove an excluded note's searchable row but keep its meta row fresh.

    The meta row's mtime is what lets every later pass skip re-reading and
    re-parsing the file — opted-out notes are often the vault's largest
    (rolled log archives), and index_vault runs on every vault_search call.
    """
    conn.execute(f"DELETE FROM {fts_table} WHERE path = ?", (rel_str,))
    conn.execute(
        f"INSERT OR REPLACE INTO {meta_table} (path, mtime, indexed_at) VALUES (?, ?, ?)",
        (rel_str, mtime, datetime.now(timezone.utc).isoformat()),
    )

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)

# Every stored key is a path relative to the key base, so no key can begin with
# a separator: this prefix matches no row. It is the fail-closed answer for a
# vault whose rows have no identifying prefix, where the alternative ("" — match
# everything) leaked one workspace's notes into another's search. Public so
# callers of vault_key_prefix can recognise the fail-closed answer by name
# instead of re-deriving the sentinel's value.
NO_MATCH_KEY_PREFIX = os.sep


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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS search_config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()


def _ensure_path_base(conn: sqlite3.Connection, base: Path) -> None:
    """Record which directory the stored paths are relative to, and wipe on change.

    Every row's key is a path relative to one base. Two callers using different
    bases would write two key formats into one table, so the same note would
    appear twice under different names and neither prune would remove the other.
    Rather than migrate keys, the index is dropped: it is derived state,
    rebuilding it costs one pass, and a half-converted search index is worse
    than an empty one because it answers queries with paths that resolve wrong.
    """
    resolved = str(Path(base).resolve())
    row = conn.execute(
        "SELECT value FROM search_config WHERE key = 'path_base'"
    ).fetchone()
    if row is not None and row[0] == resolved:
        return
    if row is not None:
        for table in ("vault_fts", "vault_meta", "transcript_fts", "transcript_meta"):
            conn.execute(f"DELETE FROM {table}")
    conn.execute(
        "INSERT OR REPLACE INTO search_config (key, value) VALUES ('path_base', ?)",
        (resolved,),
    )
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


def _search_opted_out(text: str) -> bool:
    """True when a note's frontmatter carries ``search: false``.

    The general escape hatch behind ``RESERVED_UNINDEXED_FILES``: any note can
    take itself out of recall (rolled log archives, scratch files) without the
    engine having to learn its name.
    """
    match = FRONTMATTER_RE.match(text)
    if not match:
        return False
    try:
        fm = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return False
    return isinstance(fm, dict) and fm.get("search") is False


def _public_snippet(raw: str) -> str:
    """Keep only FTS-highlighted lines in a returned search snippet.

    FTS5 can include unrelated adjacent lines when a short note fits inside
    the snippet token budget. Vault search is model-facing, so returning only
    lines containing a matched term avoids leaking nearby private metadata.
    """
    lines = raw.splitlines()
    highlighted = [line.strip() for line in lines if "<<<" in line or ">>>" in line]
    selected = highlighted or lines
    return " ".join(
        line.replace("<<<", "").replace(">>>", "").strip()
        for line in selected
        if line.strip()
    )


def _key_base(root_dir: Path, path_base: Path | None) -> Path:
    """The directory stored keys are relative to.

    Defaults to the indexed directory's parent, which is what every caller
    relied on when one install held one vault. Callers pass the install root
    instead, so a key stays unique when several agent roots each hold a vault of
    the same name — otherwise ``personal/memory-vault/People/User.md`` and
    ``work/memory-vault/People/User.md`` both key as
    ``memory-vault/People/User.md`` and the second pass overwrites the first.
    """
    return Path(path_base) if path_base is not None else root_dir.parent


def _scope_prefix(root_dir: Path, base: Path) -> str | None:
    """Stored-key prefix identifying the rows one indexing pass owns.

    ``""`` means the pass owns every row, because the indexed directory IS the
    key base. ``None`` means the answer is unknown: the indexed directory is not
    under the base, so no prefix describes its rows and callers must fail closed
    rather than read the empty prefix as "everything".

    That case is a supported layout, not a corrupt one: ``CIAO_VAULT_MODE=existing``
    with an absolute vault root points a workspace at a vault outside the
    install, while ``path_base`` stays the install root. Compared against
    ``base`` exactly as the key-writing loop does — unresolved — so the scope can
    never claim a prefix the stored keys do not have.
    """
    try:
        relative = str(Path(root_dir).relative_to(Path(base)))
    except ValueError:
        return None
    return "" if relative in {"", "."} else relative + os.sep


def _index_directory(
    conn: sqlite3.Connection,
    root_dir: Path,
    meta_table: str,
    fts_table: str,
    file_pattern: str = "*.md",
    exclude_dirs: set[str] | None = None,
    exclude_files: set[str] | None = None,
    path_base: Path | None = None,
) -> tuple[int, int]:
    """Incrementally index markdown files. Returns (indexed_count, removed_count)."""
    exclude_dirs = exclude_dirs or set()
    exclude_files = exclude_files or set()
    base = _key_base(root_dir, path_base)
    if path_base is not None:
        _ensure_path_base(conn, base)

    # Get existing indexed files and their mtimes
    cursor = conn.execute(f"SELECT path, mtime FROM {meta_table}")
    existing = {row[0]: row[1] for row in cursor.fetchall()}

    # The prune below must only consider rows under the directory being indexed.
    # Unscoped, indexing one agent root DELETED every row belonging to the
    # others, so a two-workspace install kept exactly one workspace's notes
    # searchable at a time and every switch paid a full re-index.
    scope_prefix = _scope_prefix(root_dir, base)

    found_paths: set[str] = set()
    indexed_count = 0

    # Walk directory
    for md_path in root_dir.rglob(file_pattern):
        try:
            rel = md_path.relative_to(base)
        except ValueError:
            rel = md_path.relative_to(root_dir)
        rel_str = str(rel)

        # Skip excluded directories
        if any(p in exclude_dirs for p in rel.parts):
            continue
        # Skip specific excluded files (casefolded: the reserved names are
        # spelled lowercase by OKF and titlecase by this vault's history)
        if rel.name.casefold() in exclude_files:
            continue
        # The memory pipeline's own bookkeeping, but only where the pipeline
        # writes it — a user note elsewhere sharing the name stays indexed.
        # Checked against the indexed root, not the key base: keys may carry
        # an install-root prefix, but the pipeline's write location is always
        # `<root>/Workspace/`.
        if _is_reserved_bookkeeping(md_path.relative_to(root_dir)):
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

        if _search_opted_out(text):
            # Remove any FTS rows indexed before the note opted out, but keep
            # (and refresh) the meta row so the mtime short-circuit above
            # stops every later pass from re-reading the file.
            _settle_excluded_row(conn, fts_table, meta_table, rel_str, mtime)
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

    # Remove deleted files from the index, within this subtree only.
    removed_count = 0
    if scope_prefix is None:
        # Nothing identifies this pass's rows (the indexed directory is outside
        # the key base), so pruning would have to guess. The old code guessed
        # "everything": the empty prefix put every row in scope, and one pass
        # over a vault outside the install — CIAO_VAULT_MODE=existing with an
        # absolute root — deleted every OTHER agent root's rows. Keeping rows
        # for notes deleted from this vault is the strictly smaller error: they
        # are stale search hits until a pass that can be scoped runs, whereas
        # the guess destroyed every other workspace's index.
        deleted_paths: set[str] = set()
    else:
        in_scope = {
            key for key in existing if not scope_prefix or key.startswith(scope_prefix)
        }
        deleted_paths = in_scope - found_paths
    for rel_str in deleted_paths:
        conn.execute(f"DELETE FROM {fts_table} WHERE path = ?", (rel_str,))
        conn.execute(f"DELETE FROM {meta_table} WHERE path = ?", (rel_str,))
        removed_count += 1

    # Unconditional: committing with no pending writes is a no-op under the
    # default transaction control every caller uses, and a counter-based gate
    # silently rolls back any write path that forgets to reach it — the
    # opt-out settle had exactly that bug.
    conn.commit()

    return indexed_count, removed_count


def index_vault(
    conn: sqlite3.Connection,
    vault_root: Path,
    *,
    path_base: Path | None = None,
) -> tuple[int, int]:
    """Incremental indexer for core vault files (excludes Logs, Templates)."""
    from ciao.vault_index import GENERATED_VAULT_FILES

    return _index_directory(
        conn=conn,
        root_dir=vault_root,
        meta_table="vault_meta",
        fts_table="vault_fts",
        exclude_dirs=EXCLUDED_VAULT_DIRS,
        exclude_files=set(GENERATED_VAULT_FILES),
        path_base=path_base,
    )


def index_logs(
    conn: sqlite3.Connection,
    vault_root: Path,
    *,
    logs_root: Path | None = None,
    path_base: Path | None = None,
) -> tuple[int, int]:
    """Incremental indexer for conversation transcripts and meeting logs.

    ``logs_root`` names the archive explicitly. The re-rooting promotes it out of
    the vault, so deriving it as ``vault_root / "Logs"`` indexes nothing on a
    migrated install; callers holding a config pass ``config.logs_root``.
    """
    logs_root = Path(logs_root) if logs_root is not None else vault_root / "Logs"
    if not logs_root.exists():
        return 0, 0
    return _index_directory(
        conn=conn,
        root_dir=logs_root,
        meta_table="transcript_meta",
        fts_table="transcript_fts",
        path_base=path_base,
    )


def vault_key_prefix(vault_root: Path, path_base: Path | None) -> str:
    """The stored-key prefix that identifies one vault's rows.

    Callers pass this to :func:`search_vault` so a search cannot return a note
    from another agent root. Until now that isolation was an accident of the
    prune deleting every other root's rows on each index pass; with the prune
    scoped, the filter has to be explicit or the rows of every root become
    visible to every search.

    A vault outside the key base has no prefix, and the two possible answers are
    not symmetric: ``""`` means "match every row", so it handed that chat every
    other workspace's notes — and now that the prune no longer wipes those rows,
    they are all there to hand over. It fails closed instead: a prefix no stored
    key can carry, so the search returns nothing until that vault is keyed under
    the same base as the rest.
    """
    base = _key_base(vault_root, path_base)
    # Compare unresolved first, exactly as the key-writing loop does
    # (_scope_prefix's own contract): a vault reached through a symlink under
    # the base writes keys spelled with the symlink, and resolving both sides
    # here returned NO_MATCH for those rows — every search of that workspace
    # failed closed forever despite a healthy index. The resolved comparison
    # stays as a fallback for callers that spell the same real paths
    # differently (e.g. /var vs /private/var on macOS).
    prefix = _scope_prefix(Path(vault_root), Path(base))
    if prefix is None:
        prefix = _scope_prefix(Path(vault_root).resolve(), Path(base).resolve())
    return NO_MATCH_KEY_PREFIX if prefix is None else prefix


def index_file(
    conn: sqlite3.Connection,
    vault_root: Path,
    file_path: Path,
    *,
    path_base: Path | None = None,
) -> bool:
    """Force re-index a single file (e.g. immediately after archiving a chat)."""
    if not file_path.exists():
        return False
    base = _key_base(vault_root, path_base)
    if path_base is not None:
        _ensure_path_base(conn, base)
    try:
        rel = file_path.relative_to(base)
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

    if not is_log:
        try:
            rel_to_root = file_path.relative_to(vault_root)
        except ValueError:
            # A non-log file that cannot be placed under the vault root (a
            # symlinked or differently-normalized vault_root spelling): the
            # reserved-bookkeeping check would run against a base-relative
            # key it can never match (fail open), and any row written here
            # would sit outside the bulk pass's scoped prune. Fail closed:
            # index nothing.
            return False
        if _is_reserved_bookkeeping(rel_to_root):
            # Force-indexing must honour the same exclusions as the bulk pass,
            # and clean up rows written before the file became excluded. Both
            # rows go: the bulk pass never records these files in found_paths,
            # so a kept meta row would be pruned on the next pass anyway.
            conn.execute(f"DELETE FROM {fts_table} WHERE path = ?", (rel_str,))
            conn.execute(f"DELETE FROM {meta_table} WHERE path = ?", (rel_str,))
            conn.commit()
            return False
        if _search_opted_out(text):
            # Same settle as the bulk pass: drop the searchable row but keep
            # the meta row fresh, so the next index_vault does not re-read the
            # whole file (the bulk pass keeps opted-out notes in found_paths,
            # so the prune spares this row).
            _settle_excluded_row(conn, fts_table, meta_table, rel_str, mtime)
            conn.commit()
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
    *,
    path_prefix: str = "",
) -> list[dict[str, str]]:
    """Search FTS5 table with Porter stemmer query. Returns ranked results with snippets.

    ``path_prefix`` restricts results to one subtree of the stored keys. It is
    how a workspace-scoped search stays inside its own agent root now that the
    prune no longer deletes every other root's rows on each pass.
    """
    # Sanitize search term. If query is a simple string, escape double quotes
    # and wrap words. SQLite FTS5 MATCH syntax is powerful.
    # To support basic multi-word queries gracefully, we join words with AND.
    words = re.findall(r"\w+", query)
    if not words:
        return []

    # Join words with AND for proximity/co-occurrence
    match_query = " AND ".join(words)
    # LIKE with an explicit ESCAPE, because a workspace name is user-chosen and
    # may contain `_`, which LIKE treats as a single-character wildcard: a
    # prefix of `my_work/` would otherwise also match `my-work/`.
    scope_sql = ""
    scope_args: tuple[str, ...] = ()
    if path_prefix:
        scope_sql = " AND path LIKE ? ESCAPE '\\'"
        escaped = (
            path_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        scope_args = (escaped + "%",)

    sql = f"""
        SELECT path, title, snippet({fts_table}, 2, '<<<', '>>>', '...', 32) AS snippet, rank
        FROM {fts_table}
        WHERE {fts_table} MATCH ?{scope_sql}
        ORDER BY rank
        LIMIT ?
    """
    try:
        cursor = conn.execute(sql, (match_query, *scope_args, limit))
        rows = cursor.fetchall()
        if not rows and len(words) > 1:
            # AND-of-all-words returns nothing for paraphrase queries ("how
            # much do I charge per hour" — no note holds every word). Degrade
            # to OR and let BM25 rank the notes matching the distinctive
            # terms; a weaker match beats an empty answer for recall.
            cursor = conn.execute(sql, (" OR ".join(words), *scope_args, limit))
            rows = cursor.fetchall()
    except sqlite3.OperationalError:
        # Fall back to literal match if complex match expression syntax is invalid
        sql = f"""
            SELECT path, title, snippet({fts_table}, 2, '<<<', '>>>', '...', 32) AS snippet, rank
            FROM {fts_table}
            WHERE {fts_table} MATCH ?{scope_sql}
            ORDER BY rank
            LIMIT ?
        """
        clean_query = query.replace('"', " ")
        escaped_query = f'"{clean_query}"'
        cursor = conn.execute(sql, (escaped_query, *scope_args, limit))
        rows = cursor.fetchall()

    return [
        {
            "path": row[0],
            "title": row[1],
            "snippet": _public_snippet(row[2]) if row[2] else "",
            "rank": f"{row[3]:.4f}",
        }
        for row in rows
    ]


def search_vault(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 10,
    *,
    path_prefix: str = "",
) -> list[dict[str, str]]:
    return search(conn, "vault_fts", query, limit, path_prefix=path_prefix)


def search_logs(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 10,
    *,
    path_prefix: str = "",
) -> list[dict[str, str]]:
    """Search transcripts, optionally scoped to one archive's stored keys.

    ``path_prefix`` has the same meaning it has in :func:`search_vault`, and
    exists for the same reason: one database holds every re-rooted workspace's
    rows, and the prune now preserves sibling roots. ``search_vault`` gained the
    filter and this did not, so a transcript search still answered with another
    workspace's chat titles and snippets — the same disclosure, reached through
    ``--logs`` instead of the notes path.

    Left ``""`` for a single-archive install, and correct there: after the
    re-rooting ``Logs/`` is PROMOTED to the install root UNSPLIT (D5), so its
    rows have one prefix that every workspace shares. The filter matters for the
    layouts where each root keeps its own archive under its own vault, which is
    what a not-yet-migrated root and every per-root ``index_file`` write produce.
    """
    return search(conn, "transcript_fts", query, limit, path_prefix=path_prefix)


# ── Retrieval telemetry (decay-by-disuse signal) ───────────────────────────
#
# Every vault_search result set is appended here so the memory audit can tell
# which notes recall actually uses. A note that is both stale and never
# retrieved is the strongest demotion candidate the nightly curator sees.
# Strictly best-effort and signal-only: nothing reads this to delete anything.

SEARCH_HITS_NAME = "vault_search_hits.jsonl"
_HITS_MAX_BYTES = 1 * 1024 * 1024
_HITS_KEEP_LINES = 2000


def record_search_hits(runtime_dir: Path, query: str, paths: list[str]) -> None:
    """Append one search's returned note paths to the hits log. Never raises."""
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        path = runtime_dir / SEARCH_HITS_NAME
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "query": query[:200],
            "paths": paths[:50],
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        if path.stat().st_size > _HITS_MAX_BYTES:
            lines = path.read_text(encoding="utf-8").splitlines()[-_HITS_KEEP_LINES:]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001 — telemetry must never break search
        logger.debug("Could not record search hits", exc_info=True)


def read_search_hit_paths(
    runtime_dir: Path, *, since_days: int = 90
) -> set[str] | None:
    """Note paths returned by any search in the window; None when no log exists.

    None matters: an install that never wrote the log has no retrieval
    evidence, and the audit must not read that absence as "never retrieved".
    """
    path = runtime_dir / SEARCH_HITS_NAME
    if not path.exists():
        return None
    cutoff = datetime.now(timezone.utc).timestamp() - since_days * 86400
    hits: set[str] = set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                # One junk line must not void the audit's whole stale-notes
                # section (the caller wraps this in a broad advisory except).
                continue
            try:
                ts = datetime.fromisoformat(str(record.get("ts", ""))).timestamp()
            except ValueError:
                continue
            if ts < cutoff:
                continue
            paths = record.get("paths", [])
            if not isinstance(paths, list):
                continue
            for item in paths:
                if isinstance(item, str):
                    hits.add(item)
    except OSError:
        return None
    return hits


def logs_key_prefix(logs_root: Path, path_base: Path | None) -> str:
    """The stored-key prefix that identifies one transcript archive's rows.

    The companion to :func:`vault_key_prefix`, for callers holding a ``logs_root``
    rather than a vault root. Same computation and the same fail-closed answer
    for an archive outside the key base — named separately so a call site cannot
    read as if a vault path were being passed.
    """
    return vault_key_prefix(logs_root, path_base)
