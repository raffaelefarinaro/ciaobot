"""SQLite FTS5 full-text indexing and search for vault and transcripts."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import stat
import weakref
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
import yaml

from ciao import vault_index
from ciao.async_reads import keyed_lock
from ciao.os_support.files import read_connected_note
from ciao.os_support.private import descriptor_acl_grants_another_account, is_private_descriptor_strict, path_acl_grants_another_account
from ciao.redact import redact_secret_line

if TYPE_CHECKING:
    from ciao.config import SharedBrainLink, WorkspaceConfig

logger = logging.getLogger(__name__)

# Directory-based type inference (similar to vault_index.py). `.vault-trash`
# holds notes older versions of Vault Review trashed: still on disk so a restore is
# possible, but no longer part of the vault, so never searchable.
# `.archived-workspaces` is the same kind of exclusion for whole workspaces
# (see `ciao.workspace_archive`).
EXCLUDED_VAULT_DIRS = {
    "Logs",
    "Templates",
    ".obsidian",
    ".vault-trash",
    vault_index.ARCHIVED_WORKSPACES_DIR,
}

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
        except OSError as exc:
            raise OSError(f"incomplete FTS scan at {path}") from exc
        with scan:
            for entry in scan:
                name = entry.name
                child_rel = name if not rel else rel + KEY_SEPARATOR + name
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError as exc:
                    raise OSError(f"incomplete FTS scan at {entry.path}") from exc
                if is_dir:
                    if name not in exclude_dirs:
                        stack.append((child_rel, entry.path))
                elif name.endswith(suffix):
                    yield child_rel, entry

# `\r?\n`, as in vault_links: a CRLF note's frontmatter is still frontmatter.
FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)

# Stored keys are relative paths spelled with `/` on every OS, the way the
# search rows, `expand_note` and the MCP telemetry hand them to clients; a key
# built from the native separator came out as `a\b.md` on Windows.
KEY_SEPARATOR = "/"

# The one spelling `_is_reserved_key` has to recognise in a key's parent
# directory, derived from the shared rule so this walk cannot drift from it. A
# substring test rather than a suffix one: it has to also cover a top-level
# `Workspace/Tasks/` (no leading separator) and may over-match, which only
# costs a `Path` construction the predicate then settles. The gate is therefore
# a strict superset of `is_reserved_bookkeeping` by construction and decides
# nothing on its own — keep it that way, or the walk could start rejecting rows
# the predicate would accept.
_TASK_RECORDS_PARENT_KEY = KEY_SEPARATOR.join(vault_index.TASK_RECORDS_PARENT_PARTS)

# Every stored key is a path relative to the key base, so no key can begin with
# a separator: this prefix matches no row. It is the fail-closed answer for a
# vault whose rows have no identifying prefix, where the alternative ("" — match
# everything) leaked one workspace's notes into another's search. Public so
# callers of vault_key_prefix can recognise the fail-closed answer by name
# instead of re-deriving the sentinel's value.
NO_MATCH_KEY_PREFIX = KEY_SEPARATOR


SEARCH_DB_NAME = "vault-fts.db"


def get_db_path(runtime_dir: Path | None = None, *, create: bool = True) -> Path:
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
    if create:
        db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / SEARCH_DB_NAME


def connected_db_path(runtime: Path, root: Path, source_roots: frozenset[Path] = frozenset(),
                      *, create: bool = True) -> Path:
    """Keep the local index out of a connected folder, including a root adopted as '.'."""
    base = Path(os.environ.get("CIAO_MEMORY_DIR") or runtime).expanduser().absolute()
    roots = source_roots | {root.resolve()}
    if any(base.resolve().is_relative_to(tree) for tree in roots):
        install = hashlib.sha256(os.fsencode(runtime.resolve())).hexdigest()
        base = Path.home() / ".ciao" / "connected-indexes" / install
    directory = base / "connected"
    if any(directory.resolve().is_relative_to(tree) for tree in roots):
        raise OSError("no local index location outside the connected folder")

    # SQLite reopens the path and creates its journals by name. Walk trusted
    # ancestors before creating anything. A root-owned sticky temp ancestor
    # protects the next (owner-checked) entry against another user's rename.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent = os.open("/", flags)
    try:
        for position, component in enumerate(directory.parts[1:], start=1):
            try:
                child = os.open(component, flags, dir_fd=parent)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=parent)
            os.close(parent)
            parent = child
            info = os.fstat(child)
            if position == len(base.parts) - 1 and info.st_uid != os.geteuid():
                raise PermissionError("connected index storage must belong to the current user")
            sticky_system = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if (info.st_uid not in (0, os.geteuid())
                or (info.st_mode & 0o022 and not sticky_system)
                or descriptor_acl_grants_another_account(child)):
                raise PermissionError("untrusted connected index directory")
        if not is_private_descriptor_strict(parent):
            raise PermissionError("connected index directory must be owner-private")
        # Closing an independent fd on an open SQLite database can release
        # this process's SQLite locks. Only open a newly created file.
        with keyed_lock(f"connected-file:{directory}"):
            for suffix in ("", "-journal", "-wal", "-shm"):
                name = SEARCH_DB_NAME + suffix
                try:
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    if suffix:
                        continue
                    if not create:
                        raise
                    try:
                        fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                                     0o600, dir_fd=parent)
                    except FileExistsError:
                        pass
                    else:
                        os.close(fd)
                    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600
                    or path_acl_grants_another_account(directory / name)):
                    raise PermissionError("connected index database must be owner-private")
    finally:
        os.close(parent)
    return directory / SEARCH_DB_NAME


def connected_entries(conn: sqlite3.Connection, namespace: str) -> list[vault_index.Entry] | None:
    row = conn.execute("SELECT entries FROM connected_indexes WHERE namespace=?", (namespace,)).fetchone()
    if row is None:
        return None
    entries = json.loads(row[0])
    for entry in entries:
        entry["path"] = Path(entry["path"])
    return [vault_index.Entry(**entry) for entry in entries]


# Scanning a large connected vault may hold the SQLite writer for many seconds.
# This bounds SQLite's retries per contended operation across processes.
CONNECTED_INDEX_BUSY_SECONDS = 30.0


class ConnectedIndexBusy(RuntimeError):
    """SQLite could not acquire the connected index lock within its wait budget."""


@contextmanager
def connected_index_connection(database: Path) -> Iterator[sqlite3.Connection]:
    try:
        with closing(sqlite3.connect(database, timeout=CONNECTED_INDEX_BUSY_SECONDS)) as conn:
            yield conn
    except sqlite3.OperationalError as exc:
        if getattr(exc, "sqlite_errorcode", 0) & 0xff != sqlite3.SQLITE_BUSY:
            raise
        raise ConnectedIndexBusy(
            f"Connected index busy (wait limit {CONNECTED_INDEX_BUSY_SECONDS:g}s). Retry the operation."
        ) from exc


class ConnectedWorkspaceChanged(RuntimeError):
    """A request lost its workspace registration while waiting for the index."""


@dataclass(frozen=True)
class ConnectedRegistration:
    registry: Path
    generation: tuple[int, int, int, int] | None
    workspaces: dict[str, WorkspaceConfig] | None = field(repr=False, compare=False)

    @staticmethod
    def _generation(info: os.stat_result) -> tuple[int, int, int, int]:
        return info.st_dev, info.st_ino, info.st_ctime_ns, info.st_size

    @classmethod
    def capture(cls, runtime: Path) -> ConnectedRegistration:
        # Both registry writers publish with atomic replace. Capture the file
        # that authorized this request, including legacy bootstrap's absent file.
        from ciao.config import _parse_workspaces_json

        registry = runtime / "workspaces.json"
        try:
            stream = registry.open(encoding="utf-8")
        except FileNotFoundError:
            return cls(registry, None, None)
        with stream:
            generation = cls._generation(os.fstat(stream.fileno()))
            registration = cls(registry, generation, _parse_workspaces_json(stream.read(), generation=generation))
            # Keep the old inode allocated even if archive and restore both
            # finish while this request is queued. A cancelled asyncio caller
            # may leave a worker running, so the pin follows the shared token.
            weakref.finalize(registration, os.close, os.dup(stream.fileno()))
            return registration

    def authorize(self, root: Path, workspace: str, base: Path, vault_base: Path | None, *,
                  allow_subfolders: bool = False, link: SharedBrainLink | None = None) -> None:
        self.verify()
        if not root.is_absolute() or root.resolve() != root:
            raise ConnectedWorkspaceChanged("Connected index source is no longer canonical")
        if self.workspaces is None:
            if link is not None:
                raise ConnectedWorkspaceChanged("Shared brain has no persisted registration")
            return  # Bootstrap has no disk registry; the caller resolved its configured source.
        from ciao.config import CiaoConfig

        row = self.workspaces.get(workspace)
        if row is None:
            raise ConnectedWorkspaceChanged("Workspace is no longer registered for connected indexing")
        if allow_subfolders or link is not None:
            selected = max((item for item in row.shared_brains if root.is_relative_to(Path(item.path))),
                           key=lambda item: len(item.path), default=None)
            if selected is not None or link is not None:
                # SharedBrainLink equality deliberately omits lifetime_id.
                if (link is None or selected != link or selected.lifetime_id != link.lifetime_id
                        or Path(link.path).resolve() != Path(link.path)):
                    raise ConnectedWorkspaceChanged("Shared brain registration changed; select the link again")
                return
        # Reuse the normal layout and legacy-root resolver with only the pinned
        # registry data. Never authorize an old source from its workspace name.
        config = CiaoConfig(pwa_auth_token="", workspace_root=base,
                            state_path=self.registry.parent / "state.json", media_root=self.registry.parent,
                            vault_root=vault_base or base / "memory-vault", workspaces={workspace: replace(row)})
        sources = [config.workspace_vault_root(workspace), config.agent_vault_root(workspace)]
        if not any((root == source or (allow_subfolders and root.is_relative_to(source)))
                   and source.resolve() == source for source in sources):
            raise ConnectedWorkspaceChanged("Source is no longer registered for this workspace")

    def verify(self) -> None:
        try:
            current = self._generation(self.registry.stat())
        except FileNotFoundError:
            current = None
        if current != self.generation:
            raise ConnectedWorkspaceChanged("Workspace registration changed; start a new index request")


def refresh_connected_index(conn: sqlite3.Connection, root: Path, workspace: str,
                            base: Path, namespace: str, registration: ConnectedRegistration) -> tuple[list[vault_index.Entry], int, int]:
    # Reserve the writer before reading any source data. CLI processes and
    # server threads then publish in capture order on independent connections.
    conn.execute("BEGIN IMMEDIATE")
    with conn:
        registration.verify()
        captured: dict[Path, str] = {}
        indexed, removed = _index_directory_pass(
            conn, root, "vault_meta", "vault_fts", exclude_dirs=EXCLUDED_VAULT_DIRS,
            exclude_files=set(vault_index.GENERATED_VAULT_FILES), path_base=base,
            key_namespace=namespace, captured=captured,
        )
        entries = vault_index.scan_vault(
            root, workspace=workspace, strict=True, path_prefix=Path("."), captured=captured,
        )
        payload = [dict(asdict(entry), path=entry.path_key) for entry in entries]
        conn.execute("INSERT OR REPLACE INTO connected_indexes VALUES (?, ?)",
                     (namespace, json.dumps(payload)))
        conn.execute("INSERT OR REPLACE INTO connected_index_owners VALUES (?, ?, ?)",
                     (namespace, str(registration.registry.parent.resolve()), workspace))
        registration.verify()
    return entries, indexed, removed


def lookup_connected_entries(root: Path, workspace: str, runtime: Path, base: Path, *,
                             refresh: bool = False, source_roots: frozenset[Path] = frozenset(),
                             registration: ConnectedRegistration | None = None, vault_base: Path | None = None,
                             link: SharedBrainLink | None = None) -> list[vault_index.Entry]:
    """Read the local snapshot, or build one transactionally from its original root."""
    registration = registration or ConnectedRegistration.capture(runtime)
    registration.authorize(root, workspace, base, vault_base, allow_subfolders=True, link=link)
    database = connected_db_path(runtime, root, source_roots)
    namespace = adopted_vault_key_prefix(root, workspace, link=link)
    with connected_index_connection(database) as conn:
        init_db(conn)
        if not refresh:
            cached = connected_entries(conn, namespace)
            if cached is not None:
                registration.verify()
                return cached
        entries, _, _ = refresh_connected_index(conn, root, workspace, base, namespace, registration)
        return entries


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
    conn.execute("CREATE TABLE IF NOT EXISTS connected_indexes (namespace TEXT PRIMARY KEY, entries TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS connected_index_owners "
                 "(namespace TEXT PRIMARY KEY, runtime TEXT NOT NULL, workspace TEXT NOT NULL)")
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
        for table in ("vault_fts", "vault_meta", "transcript_fts", "transcript_meta", "connected_indexes", "connected_index_owners"):
            conn.execute(f"DELETE FROM {table}")
    conn.execute(
        "INSERT OR REPLACE INTO search_config (key, value) VALUES ('path_base', ?)",
        (resolved,),
    )


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

    That case is a supported layout, not a corrupt one: existing-folder setup
    with an absolute vault root points a workspace at a vault outside the
    install, while ``path_base`` stays the install root. Compared against
    ``base`` exactly as the key-writing loop does — unresolved — so the scope can
    never claim a prefix the stored keys do not have.
    """
    try:
        relative = Path(root_dir).relative_to(Path(base)).as_posix()
    except ValueError:
        return None
    return "" if relative in {"", "."} else relative + KEY_SEPARATOR


def _is_reserved_key(root_rel: str) -> bool:
    """``vault_index.is_reserved_bookkeeping`` for a root-relative key string.

    Gated on cheap string tests first. The real check builds a ``Path`` to read
    its parts, and doing that for every note in the vault on every search was
    pure overhead: the predicate can only be true for the handful of reserved
    names and for a file directly under ``Workspace/Tasks/`` (the task board's
    own records, #973-B2, named by id rather than by a reserved basename). Both
    gates are deliberately wider than the rule — the shared predicate still
    decides every path that reaches it, so neither can fork the second rule.
    """
    parent, _, name = root_rel.rpartition(KEY_SEPARATOR)
    if (
        name.casefold() not in RESERVED_UNINDEXED_FILES
        and _TASK_RECORDS_PARENT_KEY not in parent.casefold()
    ):
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
    key_namespace: str | None = None,
) -> tuple[int, int]:
    """Index a complete traversal atomically, preserving prior rows on error."""
    conn.execute("SAVEPOINT fts_index_pass")
    try:
        result = _index_directory_pass(
            conn, root_dir, meta_table, fts_table, file_suffix, exclude_dirs,
            exclude_files, path_base, key_namespace,
        )
        conn.execute("RELEASE SAVEPOINT fts_index_pass")
        conn.commit()
        return result
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT fts_index_pass")
        conn.execute("RELEASE SAVEPOINT fts_index_pass")
        conn.rollback()
        raise


def _index_directory_pass(
    conn: sqlite3.Connection,
    root_dir: Path,
    meta_table: str,
    fts_table: str,
    file_suffix: str = ".md",
    exclude_dirs: set[str] | None = None,
    exclude_files: set[str] | None = None,
    path_base: Path | None = None,
    key_namespace: str | None = None,
    captured: dict[Path, str] | None = None,
) -> tuple[int, int]:
    """Incrementally index markdown files. Returns (indexed_count, removed_count)."""
    exclude_dirs = exclude_dirs or set()
    exclude_files = exclude_files or set()
    base = _key_base(root_dir, path_base)
    if path_base is not None and captured is None:
        _ensure_path_base(conn, base)

    if key_namespace is None:
        # A standard vault may still be awaiting initial setup. Preserve its
        # existing rows and the install's base registration in that case.
        try:
            root_dir.stat()
        except FileNotFoundError:
            return 0, 0

    if key_namespace is not None and (
        not key_namespace.endswith("/")
        or key_namespace.startswith("/")
        or ".." in Path(key_namespace).parts
        or "\x00" in key_namespace
    ):
        raise ValueError("FTS key namespace must be a safe relative directory prefix")

    # The prune below must only consider rows under the directory being indexed.
    # Unscoped, indexing one agent root DELETED every row belonging to the
    # others, so a two-workspace install kept exactly one workspace's notes
    # searchable at a time and every switch paid a full re-index.
    scope_prefix = key_namespace if key_namespace is not None else _scope_prefix(root_dir, base)

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

    # FTS has no path index. Resolve row IDs once so content comparisons
    # against a captured source read do not scan the table for each note.
    prior_rows = dict(conn.execute(
        f"SELECT path, rowid FROM {fts_table} WHERE path LIKE ? ESCAPE '\\'",
        (_like_prefix_pattern(key_prefix),),
    )) if captured is not None else {}
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

        if key_namespace and entry.is_symlink():
            continue
        rel_str = key_prefix + root_rel
        found_paths.add(rel_str)

        try:
            # The walk's own stat, not a second syscall against the same path.
            st = entry.stat()
        except OSError as exc:
            raise OSError(f"incomplete FTS scan at {entry.path}") from exc

        # Check if file changed
        signature = _signature(st)
        prior = existing.get(rel_str)
        if captured is not None:
            relative = Path(root_rel)
            text = read_connected_note(root_dir, relative)
            captured[relative] = text
            if prior == signature and conn.execute(
                f"SELECT 1 FROM {fts_table} WHERE rowid = ? AND body = ?", (prior_rows.get(rel_str), text)
            ).fetchone():
                continue
        elif prior == signature:
            continue
        if captured is None and prior is not None and _is_legacy_row(prior) and prior[0] == signature[0]:
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
            if captured is None:
                text = (read_connected_note(root_dir, Path(root_rel)) if key_namespace
                    else Path(entry.path).read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            # A note that is not UTF-8 is a known content error, not an
            # unavailable root: keep its previous row and keep indexing others.
            logger.warning("FTS search: failed to decode %s; keeping the prior row", entry.path)
            continue
        except OSError as exc:
            raise OSError(f"incomplete FTS scan at {entry.path}") from exc

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
        # over a vault outside the install — existing-folder setup with an
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
    return indexed_count, removed_count


def index_vault(
    conn: sqlite3.Connection,
    vault_root: Path,
    *,
    path_base: Path | None = None,
    key_namespace: str | None = None,
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
        key_namespace=key_namespace,
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


def adopted_vault_key_prefix(vault_root: Path, workspace: str, *, link: SharedBrainLink | None = None) -> str:
    """Opaque local FTS namespace for an adopted root outside the install tree."""
    identity = f"{workspace}\0{Path(vault_root).resolve()}".encode("utf-8")
    if link is not None:
        identity += b"\0" + json.dumps([link.name, link.path, link.identity, link.lifetime_id]).encode()
    return f"adopted/{hashlib.sha256(identity).hexdigest()[:20]}/"


def vault_key_prefix(
    vault_root: Path, path_base: Path | None, *, key_namespace: str | None = None,
) -> str:
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
    if key_namespace is not None:
        if not key_namespace.endswith("/") or key_namespace.startswith("/") or ".." in Path(key_namespace).parts:
            return NO_MATCH_KEY_PREFIX
        return key_namespace
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


def forget_subtree(conn: sqlite3.Connection, prefix: str) -> int:
    """Drop every vault row stored under ``prefix``; return how many went.

    The prune in :func:`_index_directory` is scoped to the directory being
    indexed, so a vault that stops being indexed at all — an archived
    workspace's root — keeps its rows forever unless someone removes them. An
    unscoped search would then keep returning notes the operator archived.

    Refuses the two prefixes that do not name one subtree: ``""`` (every row)
    and :data:`NO_MATCH_KEY_PREFIX` (a vault outside the key base, whose rows
    this prefix cannot identify).
    """
    with conn:
        return _forget_subtree(conn, prefix)


def forget_connected_workspace(conn: sqlite3.Connection, runtime: Path, workspace: str,
                               known_namespaces: set[str]) -> int:
    # Acquire the same writer as refresh BEFORE enumerating ownership. A
    # finishing capture can otherwise commit a new subfolder after enumeration.
    conn.execute("BEGIN IMMEDIATE")
    with conn:
        namespaces = known_namespaces | {row[0] for row in conn.execute(
            "SELECT namespace FROM connected_index_owners WHERE runtime = ? AND workspace = ?",
            (str(runtime.resolve()), workspace),
        )}
        return sum(_forget_subtree(conn, namespace) for namespace in namespaces)


def _forget_subtree(conn: sqlite3.Connection, prefix: str) -> int:
    if not prefix or prefix == NO_MATCH_KEY_PREFIX:
        return 0
    pattern = _like_prefix_pattern(prefix)
    conn.execute("DELETE FROM connected_index_owners WHERE namespace = ?", (prefix,))
    conn.execute("DELETE FROM connected_indexes WHERE namespace = ?", (prefix,))
    removed = conn.execute(
        "DELETE FROM vault_fts WHERE path LIKE ? ESCAPE '\\'", (pattern,)
    ).rowcount
    conn.execute("DELETE FROM vault_meta WHERE path LIKE ? ESCAPE '\\'", (pattern,))
    return int(removed or 0)


def forget_transcript_subtree(conn: sqlite3.Connection, prefix: str) -> int:
    """Drop every transcript row stored under ``prefix``; return how many went.

    The transcript twin of :func:`forget_subtree`, on ``transcript_fts`` and
    ``transcript_meta``, with the same two refused prefixes. Used when a
    workspace's own ``Logs`` leaves the install's live tree.
    """
    if not prefix or prefix == NO_MATCH_KEY_PREFIX:
        return 0
    pattern = _like_prefix_pattern(prefix)
    removed = conn.execute(
        "DELETE FROM transcript_fts WHERE path LIKE ? ESCAPE '\\'", (pattern,)
    ).rowcount
    conn.execute("DELETE FROM transcript_meta WHERE path LIKE ? ESCAPE '\\'", (pattern,))
    conn.commit()
    return int(removed or 0)


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
    rel_str = rel.as_posix()

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
    path_prefixes: Sequence[str] = (),
) -> list[dict[str, str]]:
    """Search FTS5 table with Porter stemmer query. Returns ranked results with snippets.

    A multi-word query returns the notes holding every word first, in BM25
    order; when those are fewer than ``limit``, notes holding any of the words
    fill the rest, also in BM25 order. Results therefore need not contain
    every word, and ``rank`` is comparable only within each of the two runs.

    ``path_prefix`` restricts results to one subtree of the stored keys. It is
    how a workspace-scoped search stays inside its own agent root now that the
    prune no longer deletes every other root's rows on each pass.
    ``path_prefixes`` restricts results to any of several subtrees; pass one or
    the other, not both.
    """
    if path_prefix and path_prefixes:
        raise ValueError("pass path_prefix or path_prefixes, not both")
    # Sanitize search term. If query is a simple string, escape double quotes
    # and wrap words. SQLite FTS5 MATCH syntax is powerful.
    # Multi-word queries try AND first, then OR fills any remaining slots.
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
    elif path_prefixes:
        scope_sql = " AND (" + " OR ".join(["path LIKE ? ESCAPE '\\'"] * len(path_prefixes)) + ")"
        scope_args = tuple(_like_prefix_pattern(p) for p in path_prefixes)

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
        if len(rows) < limit and len(words) > 1:
            # AND-of-all-words is precise but brittle: a paraphrase ("how much
            # do I charge per hour") matches no note at all, and a knowledge
            # update usually matches only one side of it, because the note
            # holding the old value and the note holding the new one rarely
            # share every query word. So whenever AND leaves the page short,
            # top it up with the OR query's BM25 order, skipping notes AND
            # already returned. AND hits keep the lead; zero AND hits is just
            # the case where the whole page comes from OR.
            seen = {row[0] for row in rows}
            try:
                or_rows = conn.execute(sql, (" OR ".join(words), *scope_args, limit + len(rows))).fetchall()
            except sqlite3.OperationalError:
                # A top-up must never cost the AND hits already found: keep
                # them and skip the backfill (only zero AND hits falls through
                # to the literal match below).
                if not rows:
                    raise
                or_rows = []
            rows += [row for row in or_rows if row[0] not in seen][: limit - len(rows)]
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
    path_prefixes: Sequence[str] = (),
) -> list[dict[str, str]]:
    """Search transcripts, optionally scoped to one or more archives' stored keys.

    ``path_prefix`` has the same meaning it has in :func:`search_vault`, and
    exists for the same reason: one database holds every re-rooted workspace's
    rows, and the prune now preserves sibling roots. ``search_vault`` gained the
    filter and this did not, so a transcript search still answered with another
    workspace's chat titles and snippets — the same disclosure, reached through
    ``--logs`` instead of the notes path.

    Transcripts live under each workspace's ``<root>/Logs`` and the shared
    ``Logs/``. Scope a workspace's search with ``path_prefixes`` naming both.
    Left unscoped, a search answers from every archive in the install.
    """
    return search(
        conn,
        "transcript_fts",
        query,
        limit,
        path_prefix=path_prefix,
        path_prefixes=path_prefixes,
    )


# ── Scoped evidence drill-down ─────────────────────────────────────────────
#
# `search` answers with `_public_snippet`: the FTS-highlighted lines only,
# inside SQLite's 32-token budget, narrow enough to cut away the half of a
# sentence that decides the answer: a qualification ("…only for work logged
# before the amendment"), a negation, or the paragraph naming the current value.
# Recall that answered from snippets alone answered confidently from a fragment
# whose meaning is the opposite of the note's, so the core prompt now has it
# read the best-matching full note before answering; its privacy guard is the
# answer-level "private working evidence" rule, not the snippet's narrowness.
#
# `expand_note` is now only the library function the behavioural-eval recall
# contracts measure; no agent path calls it. It is deliberately NOT a note
# reader:
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
# * Explicit — nothing calls this automatically; it takes the path a search
#   already returned.
#
# There is no opaque, expiring result token: the stored key IS the reference,
# and it is re-validated against the live index and re-read from disk on every
# call. A note that has since been edited, renamed, opted out, or trashed is
# therefore refreshed or refused rather than answered from a stale handle.

EXPAND_MAX_WINDOWS = 3
EXPAND_MAX_LINES = 40
EXPAND_MAX_CHARS = 1800

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

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
    key = str(stored_key).strip()
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
        # the file and the read is what fails. Refusing here keeps the
        # fail-closed contract: letting it escape would turn a read-only
        # lookup into an internal error.
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
        rendered = "\n".join(redact_secret_line(line) for line in lines[low:high]).strip()
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
            with path.open("a", encoding="utf-8", newline="") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            if path.stat().st_size > _HITS_MAX_BYTES:
                lines = path.read_text(encoding="utf-8").splitlines()[-_HITS_KEEP_LINES:]
                # Atomic replace, not an in-place truncating write: a reader
                # never observes a half-rewritten file, and a failed write
                # leaves the previous log intact.
                tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
                tmp.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")
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
