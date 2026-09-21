"""SQLite FTS5 full-text indexing and search for vault and transcripts."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import yaml

from ciao import vault_index
from ciao.async_reads import keyed_lock

logger = logging.getLogger(__name__)

# Directory-based type inference (similar to vault_index.py). `.vault-trash`
# holds notes `ciao.vault_review` has trashed: still on disk so a restore is
# possible, but no longer part of the vault, so never searchable.
EXCLUDED_VAULT_DIRS = {"Logs", "Templates", ".obsidian", ".vault-trash"}

# The reserved-bookkeeping definitions live in `vault_index` so the scan, the
# lint, and this index cannot disagree about what counts as a note.
RESERVED_UNINDEXED_FILES = vault_index.RESERVED_UNINDEXED_FILES
_is_reserved_bookkeeping = vault_index.is_reserved_bookkeeping


def _signature(st: os.stat_result) -> tuple[float, int, float]:
    """The stored change signature for one file.

    Deliberately not mtime alone. mtime is the only field a writer fully
    controls, so the cases where it lies are exactly the cases a note vanishes
    from recall: `cp -p` from a backup, `rsync --times`, `tar -x`, and a git
    checkout all restore a file with a mtime the index has already recorded, and
    an mtime-only comparison then reports "unchanged" and never re-reads the new
    bytes. ``st_size`` catches any change of length; ``st_ctime`` catches the
    same-length rewrite, because the kernel stamps it on every inode change and
    userspace cannot set it backwards.

    The cost is nil — all three come from the stat the walk already performs —
    and a false positive only re-reads one file.
    """
    return (st.st_mtime, st.st_size, st.st_ctime)


# The size stamped over rows that predate the signature columns. No real file
# can have it, so it identifies a legacy row exactly.
#
# A positive mark, not "size IS NULL". NULL is also what a code path that wrote
# a meta row WITHOUT a signature would leave behind, and adopting that on mtime
# alone would let a note the index never actually read pass as indexed — the
# mutation matrix caught this: with a NULL test, stubbing out index_file's
# signature write became undetectable. An unmarked NULL now means "unknown",
# which re-reads, and only the migration can say "legacy".
_LEGACY_SIZE = -1


def _is_legacy_row(prior: tuple[float | None, int | None, float | None]) -> bool:
    """True for a meta row the migration marked as predating the columns."""
    return prior[1] == _LEGACY_SIZE


def _write_meta_row(
    conn: sqlite3.Connection,
    meta_table: str,
    rel_str: str,
    st: os.stat_result,
) -> None:
    """Record the signature the next pass compares against."""
    mtime, size, ctime = _signature(st)
    conn.execute(
        f"INSERT OR REPLACE INTO {meta_table} "
        "(path, mtime, size, ctime, indexed_at) VALUES (?, ?, ?, ?, ?)",
        (rel_str, mtime, size, ctime, datetime.now(timezone.utc).isoformat()),
    )


def _settle_excluded_row(
    conn: sqlite3.Connection,
    fts_table: str,
    meta_table: str,
    rel_str: str,
    st: os.stat_result,
) -> None:
    """Remove an excluded note's searchable row but keep its meta row fresh.

    The meta row's signature is what lets every later pass skip re-reading and
    re-parsing the file — opted-out notes are often the vault's largest
    (rolled log archives), and index_vault runs on every vault_search call.
    """
    conn.execute(f"DELETE FROM {fts_table} WHERE path = ?", (rel_str,))
    _write_meta_row(conn, meta_table, rel_str, st)


def _like_prefix_pattern(prefix: str) -> str:
    """A LIKE pattern matching every stored key under ``prefix``.

    Escapes explicitly, because a workspace name is user-chosen and may contain
    `_` or `%`, which LIKE reads as wildcards: a prefix of `my_work/` would
    otherwise also match `my-work/`.
    """
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


def _walk_notes(
    root: str, suffix: str, exclude_dirs: set[str]
) -> Iterator[tuple[str, os.DirEntry[str]]]:
    """Yield ``(path relative to root, entry)`` for every note file beneath it.

    Replaces ``Path.rglob``, which is where most of the 26ms unchanged-index
    pass of issue #450 went. rglob descends into the excluded subtrees
    (``Logs/``, ``.obsidian/``, ``.vault-trash/``) and discards their files only
    after yielding them, so a vault with a large rolled-log archive paid a stat
    per archived file on every single search; and it forces a second stat
    syscall per note, because the caller has to re-stat a path rglob has
    already stat'ed. Pruning happens before the descent here, and the caller
    reuses ``DirEntry.stat()``.

    Directory symlinks are not followed, matching ``Path.rglob``'s default: a
    vault holding a symlink back into itself must not loop.
    """
    stack: list[tuple[str, str]] = [("", root)]
    while stack:
        rel, path = stack.pop()
        try:
            scan = os.scandir(path)
        except OSError:
            # An unreadable directory is skipped, not fatal — the same outcome
            # rglob produces, and the caller's prune is what decides whether
            # its rows survive.
            continue
        with scan:
            for entry in scan:
                name = entry.name
                child_rel = name if not rel else rel + os.sep + name
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir:
                    if name not in exclude_dirs:
                        stack.append((child_rel, entry.path))
                elif name.endswith(suffix):
                    yield child_rel, entry

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)

# Every stored key is a path relative to the key base, so no key can begin with
# a separator: this prefix matches no row. It is the fail-closed answer for a
# vault whose rows have no identifying prefix, where the alternative ("" — match
# everything) leaked one workspace's notes into another's search. Public so
# callers of vault_key_prefix can recognise the fail-closed answer by name
# instead of re-deriving the sentinel's value.
NO_MATCH_KEY_PREFIX = os.sep


SEARCH_DB_NAME = "vault-fts.db"


def get_db_path(runtime_dir: Path | None = None) -> Path:
    """Resolve the path to the SQLite search database for one install.

    The default is INSTALL-OWNED: ``<runtime_dir>/vault-fts.db``. Derived state
    must not be shared between installs, or a production install and a dev
    checkout using ``~/.ciao/vault-fts.db`` would clear each other's index on
    every alternating search (``_ensure_path_base`` drops the tables when the
    key base changes). Callers pass their own runtime directory, which is
    ``config.state_path.parent`` for the app and CLI.

    ``CIAO_MEMORY_DIR`` stays an explicit override and wins over
    ``runtime_dir``: tests and migration tooling point it at a temporary
    directory, and an operator may deliberately share one database. When it is
    set the caller is responsible for that sharing — the ownership check in
    ``_ensure_path_base`` diagnoses a mismatch instead of mixing key formats.

    ``runtime_dir=None`` with no override returns the legacy global path
    ``~/.ciao/vault-fts.db``. That is a fallback for the rare caller with no
    install context; every real entry point (CLI, MCP, startup indexing) passes
    its runtime directory so they all resolve the same install-owned database.
    The legacy database is never moved or deleted here: it is left in place and
    a fresh install-owned index is rebuilt on first use.
    """
    override = os.environ.get("CIAO_MEMORY_DIR", "").strip()
    if override:
        db_dir = Path(override).expanduser()
    elif runtime_dir is not None:
        db_dir = Path(runtime_dir).expanduser()
    else:
        db_dir = Path.home() / ".ciao"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / SEARCH_DB_NAME


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
            size INTEGER,
            ctime REAL,
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
            size INTEGER,
            ctime REAL,
            indexed_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS search_config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    _migrate_meta_signature(conn)
    conn.commit()


def _migrate_meta_signature(conn: sqlite3.Connection) -> None:
    """Add the size/ctime signature columns to a database created before them.

    ``CREATE TABLE IF NOT EXISTS`` is a no-op on an existing install, so the new
    columns have to be added explicitly or every pass would read NULL for a
    signature it just wrote. The rows that were already there are then marked as
    legacy, once, so a later pass can tell "recorded before the columns existed"
    from "written without a signature", which must not be adopted.
    """
    for table in ("vault_meta", "transcript_meta"):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        added = [
            (column, decl)
            for column, decl in (("size", "INTEGER"), ("ctime", "REAL"))
            if column not in columns
        ]
        for column, decl in added:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        if added:
            # Only on the pass that adds the columns. Running it every time
            # would also rescue NULLs written after the migration, which is
            # precisely the state that has to stay unadoptable.
            conn.execute(
                f"UPDATE {table} SET size = ? WHERE size IS NULL", (_LEGACY_SIZE,)
            )


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
        # Diagnosed, not silent: with install-owned databases this only happens
        # when two installs deliberately share one database (an explicit
        # CIAO_MEMORY_DIR or a manually shared runtime). Saying so is the whole
        # difference between "your search index is empty" and "another install
        # owns this database"; the rows are derived, so dropping them is safe.
        logger.warning(
            "FTS search: database path base changed from %r to %r; dropping the "
            "derived index. Two installs are sharing one search database (check "
            "CIAO_MEMORY_DIR / CIAO_RUNTIME_ROOT).",
            row[0],
            resolved,
        )
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


def _is_reserved_key(root_rel: str) -> bool:
    """``vault_index.is_reserved_bookkeeping`` for a root-relative key string.

    Gated on the filename first. The real check builds a ``Path`` to read its
    parts, and doing that for every note in the vault on every search was pure
    overhead: the predicate can only be true for the handful of reserved names,
    so the set lookup decides it for everything else.
    """
    name = root_rel.rpartition(os.sep)[2]
    if name.casefold() not in RESERVED_UNINDEXED_FILES:
        return False
    return _is_reserved_bookkeeping(Path(root_rel))


def _index_directory(
    conn: sqlite3.Connection,
    root_dir: Path,
    meta_table: str,
    fts_table: str,
    file_suffix: str = ".md",
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

    # The prune below must only consider rows under the directory being indexed.
    # Unscoped, indexing one agent root DELETED every row belonging to the
    # others, so a two-workspace install kept exactly one workspace's notes
    # searchable at a time and every switch paid a full re-index.
    scope_prefix = _scope_prefix(root_dir, base)

    # Only this pass's rows, not the whole table. One database holds every
    # workspace's notes and every transcript archive, so the unscoped SELECT
    # made each workspace's search pay for every other workspace's index — work
    # that can never match a key this pass produces, because every key it writes
    # starts with `scope_prefix`. A NULL prefix (a vault outside the key base)
    # has no such guarantee, so it still loads everything.
    if scope_prefix:
        cursor = conn.execute(
            f"SELECT path, mtime, size, ctime FROM {meta_table} "
            "WHERE path LIKE ? ESCAPE '\\'",
            (_like_prefix_pattern(scope_prefix),),
        )
    else:
        cursor = conn.execute(f"SELECT path, mtime, size, ctime FROM {meta_table}")
    existing = {row[0]: (row[1], row[2], row[3]) for row in cursor.fetchall()}

    # The key prefix the walk's root-relative paths are joined onto. The old
    # loop re-derived it per file, and tested the exclusion set against every
    # part of every key; both are constant for the whole pass.
    key_prefix = scope_prefix if scope_prefix is not None else ""
    # A vault reached through a directory named `Logs`/`Templates` indexed
    # nothing before, because the exclusion was tested against the whole
    # base-relative key, prefix segments included. Kept rather than quietly
    # narrowed: narrowing an exclusion is a change to search results.
    prefix_excluded = any(part in exclude_dirs for part in Path(key_prefix).parts)
    walk = (
        iter(())
        if prefix_excluded
        else _walk_notes(str(root_dir), file_suffix, exclude_dirs)
    )

    found_paths: set[str] = set()
    indexed_count = 0

    for root_rel, entry in walk:
        # Skip specific excluded files (casefolded: the reserved names are
        # spelled lowercase by OKF and titlecase by this vault's history)
        if entry.name.casefold() in exclude_files:
            continue
        # The memory pipeline's own bookkeeping, but only where the pipeline
        # writes it — a user note elsewhere sharing the name stays indexed.
        # Checked against the indexed root, not the key base: keys may carry
        # an install-root prefix, but the pipeline's write location is always
        # `<root>/Workspace/`.
        if _is_reserved_key(root_rel):
            continue

        rel_str = key_prefix + root_rel
        found_paths.add(rel_str)

        try:
            # The walk's own stat, not a second syscall against the same path.
            st = entry.stat()
        except OSError:
            continue

        # Check if file changed
        signature = _signature(st)
        prior = existing.get(rel_str)
        if prior == signature:
            continue
        if prior is not None and _is_legacy_row(prior) and prior[0] == signature[0]:
            # An upgraded install: every row has a mtime and no size/ctime, and
            # a NULL can never equal a real signature. Re-reading them all costs
            # 82s on a 10,000-note vault — DELETE+INSERT against a POPULATED
            # fts5 index, far more than the 37s cold build — and it would be
            # paid inside one vault_search request, on the first search after an
            # auto-update. So the row is adopted on exactly the rule that wrote
            # it (mtime) and stamped with the full signature from the stat in
            # hand: no read, no parse, no fts5 write.
            #
            # This never makes a LATER miss possible — from here the row is
            # fully signed. What it gives up is a one-time repair: a note that a
            # timestamp-preserving restore had already desynced BEFORE the
            # upgrade stays stale, exactly as it is stale today, until it is
            # next touched or `vault_index_refresh` rebuilds. Trading a
            # guaranteed minute-long stall for every upgrading user against a
            # rare pre-existing staleness with a working recovery path.
            _write_meta_row(conn, meta_table, rel_str, st)
            continue

        try:
            text = Path(entry.path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            logger.warning("FTS search: failed to read %s", entry.path)
            continue

        if _search_opted_out(text):
            # Remove any FTS rows indexed before the note opted out, but keep
            # (and refresh) the meta row so the signature short-circuit above
            # stops every later pass from re-reading the file.
            _settle_excluded_row(conn, fts_table, meta_table, rel_str, st)
            continue

        title = _parse_title(text, entry.name.removesuffix(file_suffix))

        # Delete old index entry if it exists
        conn.execute(f"DELETE FROM {fts_table} WHERE path = ?", (rel_str,))
        # Insert new entry
        conn.execute(
            f"INSERT INTO {fts_table} (path, title, body) VALUES (?, ?, ?)",
            (rel_str, title, text),
        )
        _write_meta_row(conn, meta_table, rel_str, st)
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
        # `existing` is already this pass's rows: the SELECT above applies
        # `scope_prefix`, and an empty prefix means the pass owns the table.
        deleted_paths = set(existing) - found_paths
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
    return _index_directory(
        conn=conn,
        root_dir=vault_root,
        meta_table="vault_meta",
        fts_table="vault_fts",
        exclude_dirs=EXCLUDED_VAULT_DIRS,
        exclude_files=set(vault_index.GENERATED_VAULT_FILES),
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
        st = file_path.stat()
    except (OSError, UnicodeError):
        # Same decode guard as the bulk pass: a file that is not valid UTF-8
        # is skipped, not fatal. `index_file` runs inline on lifecycle writes
        # (archiving a chat, saving a note), so an escaping UnicodeDecodeError
        # would fail the whole operation over one unreadable file.
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
            _settle_excluded_row(conn, fts_table, meta_table, rel_str, st)
            conn.commit()
            return False

    title = _parse_title(text, file_path.stem)

    conn.execute(f"DELETE FROM {fts_table} WHERE path = ?", (rel_str,))
    conn.execute(
        f"INSERT INTO {fts_table} (path, title, body) VALUES (?, ?, ?)",
        (rel_str, title, text),
    )
    _write_meta_row(conn, meta_table, rel_str, st)
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
    scope_sql = ""
    scope_args: tuple[str, ...] = ()
    if path_prefix:
        scope_sql = " AND path LIKE ? ESCAPE '\\'"
        scope_args = (_like_prefix_pattern(path_prefix),)

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


# ── Scoped evidence drill-down ─────────────────────────────────────────────
#
# `search` answers with `_public_snippet`: the FTS-highlighted lines only,
# inside SQLite's 32-token budget. That narrowness is a privacy property, not
# an accident — it is what keeps an unrelated private line out of a recall
# answer. It is also narrow enough to cut away the half of a sentence that
# decides the answer: a qualification ("…only for work logged before the
# amendment"), a negation, or the paragraph naming the current value. Recall is
# then told to answer from snippets alone, so it answers confidently from a
# fragment whose meaning is the opposite of the note's.
#
# `expand_note` is the bounded repair. It is deliberately NOT a note reader:
#
# * Scoped — the only admissible argument is a stored key the index currently
#   holds *in `vault_fts`* and under the caller's own `path_prefix`. Another
#   workspace's note, a transcript, a `search: false` note, reserved
#   bookkeeping, a traversal, an absolute path, and a file that is not an
#   indexed note all have no row, and are refused before anything is read from
#   disk. The filesystem read is then re-checked for containment under the
#   vault root, so a symlink inside the vault cannot point out of it.
# * Bounded — the answer is the markdown section around each matched line,
#   capped at `EXPAND_MAX_WINDOWS` windows, `EXPAND_MAX_LINES` lines per window
#   and `EXPAND_MAX_CHARS` characters overall. A secret sitting in a *different*
#   section of the same note is still not returned, which is exactly the
#   property `_public_snippet` was protecting; credential-shaped assignments
#   that do land inside a window have their value redacted as well.
# * Explicit — nothing calls this automatically. Recall asks for it, by the
#   path a search already returned, when the snippet cannot support the answer.
#
# There is no opaque, expiring result token: the stored key IS the reference,
# and it is re-validated against the live index and re-read from disk on every
# call. A note that has since been edited, renamed, opted out, or trashed is
# therefore refreshed or refused rather than answered from a stale handle.

EXPAND_MAX_WINDOWS = 3
EXPAND_MAX_LINES = 40
EXPAND_MAX_CHARS = 1800

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

# Credential-shaped assignments whose VALUE is dropped from an expanded window.
# Defence in depth behind the section bound, not the primary guarantee: the
# primary guarantee is that a window never leaves the matched section.
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b((?:api[_-]?key|secret[_-]?key|client[_-]?secret|access[_-]?token"
    r"|auth[_-]?token|refresh[_-]?token|password|passwd|passphrase"
    r"|private[_-]?key|bearer)\s*[:=]\s*)(\S.*)"
)

# A bare, long, space-free base64 run: a PEM body or a raw token carries no
# keyword of its own, so the assignment rule above would not see it.
_OPAQUE_BLOB_RE = re.compile(r"^[A-Za-z0-9+/=_-]{40,}$")


def _redact_secrets(line: str) -> str:
    """Drop the value of a credential-shaped line, keeping its label."""
    if _OPAQUE_BLOB_RE.match(line.strip()):
        return "[redacted]"
    return _SECRET_ASSIGNMENT_RE.sub(lambda m: m.group(1) + "[redacted]", line)


# Function words carry no lexical signal, and inside this function they are
# actively harmful. The terms below do not decide WHICH note is expanded — the
# index already did that — they decide which LINES of it come back, and a line
# is admitted if it contains any one term. A query phrased as a sentence
# therefore anchored a window on whatever block happened to contain "for" or
# "the": "penalty percentage for late payment" pulled back an Access block
# holding a door code, which is neither purpose-driven nor bounded by anything
# the caller asked for. Only words >= 3 characters are candidates at all, so the
# list only needs the longer function words.
_EXPAND_STOPWORDS = frozenset(
    {
        "about", "after", "again", "all", "and", "any", "are", "before", "being",
        "but", "can", "did", "does", "doing", "done", "for", "from", "had", "has",
        "have", "her", "here", "hers", "him", "his", "how", "its", "into", "just",
        "many", "may", "might", "more", "most", "much", "must", "not", "now",
        "off", "one", "only", "our", "ours", "out", "over", "own", "same",
        "should", "since", "some", "still", "such", "than", "that", "the",
        "their", "them", "then", "there", "these", "they", "this", "those",
        "through", "too", "under", "until", "very", "was", "were", "what",
        "when", "where", "which", "while", "who", "whom", "whose", "why",
        "will", "with", "would", "you", "your", "yours",
    }
)


def _expand_terms(query: str) -> list[str]:
    """Lexical stems used to locate the lines worth expanding around.

    A light singular stem, not the Porter stemmer FTS5 indexes with: this only
    has to find the line the snippet came from inside one already-matched note,
    and a lenient match costs at most one extra bounded window.

    Function words are dropped (see ``_EXPAND_STOPWORDS``). A query with nothing
    else left returns no terms at all, which is the honest answer: the caller
    gets the ``no_line_match`` fallback — one block, flagged as context rather
    than evidence — instead of a window chosen by the word "the".
    """
    terms: list[str] = []
    for word in re.findall(r"\w+", query.casefold()):
        if len(word) < 3 and not word.isdigit():
            continue
        if word in _EXPAND_STOPWORDS:
            continue
        stem = word[:-1] if len(word) >= 5 and word.endswith("s") else word
        if stem not in terms:
            terms.append(stem)
    return terms


def _strip_frontmatter(text: str) -> tuple[str, int]:
    """Body text and the number of leading lines the frontmatter occupied.

    Frontmatter is never expanded. It is note bookkeeping — ids, tags, aliases,
    source paths — and none of it is the qualification an answer was missing,
    so returning it would only widen the blast radius for no recall benefit.
    """
    match = FRONTMATTER_RE.match(text)
    if not match:
        return text, 0
    head = text[: match.end()]
    return text[match.end():], head.count("\n")


def _section_bounds(lines: list[str], index: int) -> tuple[int, int]:
    """The markdown block containing ``index`` as ``(start, end_exclusive)``.

    Every heading is a boundary, whatever its level. A section that swallowed
    its own subsections would make the note's title heading enclose the entire
    file — and a note's title is exactly what a recall query matches — so the
    first drill-down would have returned the whole note, which is the rule this
    is meant to refine, not repeal. Ending at the next heading of any level
    keeps a window to one block, so a sibling block holding an unrelated
    secret or an unrelated person's details is never part of the answer. A
    deeper subsection is reachable only by matching a line inside it, which
    costs one of the (bounded) windows.
    """
    start = 0
    for i in range(index, -1, -1):
        if _HEADING_RE.match(lines[i]):
            start = i
            break
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if _HEADING_RE.match(lines[i]):
            end = i
            break
    return start, end


def _clip_window(
    start: int, end: int, focus: int, max_lines: int
) -> tuple[int, int]:
    """Narrow a section to ``max_lines`` centred on the matched line."""
    if end - start <= max_lines:
        return start, end
    low = max(start, focus - max_lines // 2)
    high = min(end, low + max_lines)
    low = max(start, high - max_lines)
    return low, high


def _heading_for(lines: list[str], start: int) -> str:
    match = _HEADING_RE.match(lines[start]) if start < len(lines) else None
    return match.group(2).strip() if match else ""


def expand_note(
    conn: sqlite3.Connection,
    key_base: Path,
    vault_root: Path,
    stored_key: str,
    query: str,
    *,
    path_prefix: str = "",
    max_windows: int = EXPAND_MAX_WINDOWS,
    max_lines: int = EXPAND_MAX_LINES,
    max_chars: int = EXPAND_MAX_CHARS,
) -> dict[str, Any] | None:
    """Bounded extra context from ONE note a search already matched.

    ``stored_key`` is the ``path`` a :func:`search_vault` row carried, relative
    to ``key_base``. Returns ``None`` — never a partial or an approximate
    answer — when the key is not an indexed, in-scope note of ``vault_root``,
    which is what makes this a refinement of the snippet rule rather than a way
    around it.
    """
    key = str(stored_key).strip().replace("/", os.sep)
    if not key or "\x00" in key or Path(key).is_absolute():
        return None
    # Scope first, so an out-of-scope key is never even looked up. A caller
    # holding the fail-closed NO_MATCH_KEY_PREFIX sentinel matches nothing,
    # because no stored key starts with a separator.
    if path_prefix and not key.startswith(path_prefix):
        return None
    row = conn.execute(
        "SELECT title FROM vault_fts WHERE path = ?", (key,)
    ).fetchone()
    if row is None:
        return None

    root = Path(vault_root).resolve()
    try:
        resolved = (Path(key_base) / key).resolve()
    except OSError:
        return None
    # Re-checked on the filesystem even though the index vouched for the key:
    # the row proves a note by that name was indexed, not that the path still
    # resolves inside the vault. A symlink planted under the vault resolves
    # out of it here and is refused.
    if not resolved.is_relative_to(root) or not resolved.is_file():
        return None
    try:
        text = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        # `UnicodeError` (the base of `UnicodeDecodeError`) is not an
        # `OSError`: a note rewritten as non-UTF-8 after it was indexed keeps
        # its FTS row — the incremental pass logs the decode failure and
        # leaves the previous row in place — so the lookup above still reaches
        # the file and the read is what fails. Refusing here is the documented
        # `note_not_matched` answer (ciao/control_plane.py::vault_expand);
        # letting it escape would turn a read-only, fail-closed tool into an
        # internal error.
        return None

    revision = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    body, offset = _strip_frontmatter(text)
    lines = body.splitlines()
    terms = _expand_terms(query)
    # Headings are not evidence, and anchoring on one wastes a window on a
    # block that only names the topic. The qualification the snippet cut is on
    # a body line; that line's block is what has to come back.
    matched = [
        i
        for i, line in enumerate(lines)
        if not _HEADING_RE.match(line)
        and any(term in line.casefold() for term in terms)
    ]
    reason = "matched"
    if not matched:
        # The note matched on its title, or on a term the Porter stemmer
        # conflated. Fall back to the first block with body text: still one
        # bounded window of the same note, never the whole file.
        reason = "no_line_match"
        first = next(
            (
                i
                for i, line in enumerate(lines)
                if line.strip() and not _HEADING_RE.match(line)
            ),
            -1,
        )
        matched = [first] if first >= 0 else []

    windows: list[tuple[int, int, int]] = []
    truncated = False
    for index in matched:
        start, end = _section_bounds(lines, index)
        low, high = _clip_window(start, end, index, max_lines)
        if any(low >= w_low and high <= w_high for w_low, w_high, _ in windows):
            # Another matched line in the same section already covers this one:
            # nothing is omitted, so this is not truncation.
            continue
        if len(windows) >= max_windows:
            truncated = True
            break
        if (low, high) != (start, end):
            truncated = True
        windows.append((low, high, start))

    sections: list[dict[str, Any]] = []
    used = 0
    for low, high, start in windows:
        rendered = "\n".join(_redact_secrets(line) for line in lines[low:high]).strip()
        if not rendered:
            continue
        if used + len(rendered) > max_chars:
            rendered = rendered[: max(0, max_chars - used)].rstrip()
            truncated = True
        if not rendered:
            truncated = True
            break
        used += len(rendered)
        sections.append(
            {
                "heading": _heading_for(lines, start),
                "start_line": offset + low + 1,
                "end_line": offset + high,
                "text": rendered,
            }
        )
        if used >= max_chars:
            truncated = True
            break

    return {
        "path": key,
        "title": str(row[0] or ""),
        "revision": revision,
        "reason": reason,
        "sections": sections,
        "truncated": truncated,
        "frontmatter_omitted": offset > 0,
    }


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
    """Append one search's returned note paths to the hits log. Never raises.

    The append and the size-cap rotation that follows are one critical section
    under the log's lock: control-plane searches run in a bounded worker pool,
    so distinct searches finish concurrently. An unlocked read/truncate-rewrite
    could otherwise drop a record another worker had just appended, or leave a
    half-written line, and `read_search_hit_paths` would then miss genuine
    retrievals — marking stale notes as never retrieved.
    """
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        path = runtime_dir / SEARCH_HITS_NAME
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "query": query[:200],
            "paths": paths[:50],
        }
        with keyed_lock(f"search-hits:{path}"):
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            if path.stat().st_size > _HITS_MAX_BYTES:
                lines = path.read_text(encoding="utf-8").splitlines()[-_HITS_KEEP_LINES:]
                # Atomic replace, not an in-place truncating write: a reader
                # never observes a half-rewritten file, and a failed write
                # leaves the previous log intact.
                tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
                tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
                os.replace(tmp, path)
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
    except (OSError, UnicodeError):
        # A log truncated mid-character by the size-cap rewrite is not valid
        # UTF-8, and `UnicodeDecodeError` is not an `OSError`. No evidence is
        # the same answer as no log.
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
