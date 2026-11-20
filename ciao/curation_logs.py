"""Bounded byte primitives for the packaged curation logs.

The two decision logs, ``Workspace/Curation-Log.md`` and
``Workspace/Weekly-Review-Log.md``, are written by two paths: the managed
``curation-log-append`` verb and the weekly ``last_full_pass`` marker. This
module owns the file-level half of both:

* a bounded, no-follow read that returns the bytes *and* the identity it saw
  (:class:`LogSnapshot`), so a publish can prove nothing changed since;
* the exact bytes an append or a marker produces, including one shared
  frontmatter splitter that the reader and the writer both use;
* a private, atomic publish that compares the live file with the captured
  identity, never with a fresh one taken at publish time.

Every OS failure is mapped to :class:`CurationLogError` with an errno name
only: no absolute path, no temporary name, no payload.

Nothing here acquires the curation state lock or reads the lease. The caller
(:mod:`ciao.curation_run`) holds the canonical state lock around every read
and publish, and re-checks lease authority immediately before publishing.
"""

from __future__ import annotations

import errno
import os
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ciao.os_support import files as osf
from ciao.os_support.private import descriptor_acl_grants_another_account

PAYLOAD_MAX_BYTES = 256 * 1024
LOG_MAX_BYTES = 8 * 1024 * 1024
APPEND_SEPARATOR = b"\n\n"
LOG_HEADINGS: dict[str, bytes] = {
    "curation": b"# Curation log\n",
    "weekly-review": b"# Weekly review log\n",
}
MARKER_KEY = "last_full_pass"
_WORKSPACE_DIR = "Workspace"
_FENCE = "---"

# (st_dev, st_ino, permission bits, st_uid, st_gid, st_nlink, st_size, st_mtime_ns)
Identity = tuple[int, int, int, int, int, int, int, int]


class CurationLogError(RuntimeError):
    """A log cannot be read or written safely. The message is bounded and carries no payload."""


def _mapped(action: str, exc: OSError) -> CurationLogError:
    """The bounded form of an OS failure: the action and the errno name, nothing else."""
    name = errno.errorcode.get(exc.errno or -1, "EIO")
    return CurationLogError(f"{action} failed ({name})")


def _identity(entry: os.stat_result) -> Identity:
    return (
        entry.st_dev,
        entry.st_ino,
        stat.S_IMODE(entry.st_mode),
        entry.st_uid,
        entry.st_gid,
        entry.st_nlink,
        entry.st_size,
        entry.st_mtime_ns,
    )


@dataclass(frozen=True)
class LogSnapshot:
    """What a read saw: the bytes, and the identity of the file they came from.

    ``data`` and ``identity`` are both ``None`` for a reliably absent leaf.
    """

    data: bytes | None
    identity: Identity | None


def check_payload(payload: bytes) -> None:
    """Refuse a payload the append may not publish. A ``ValueError`` is a usage error."""
    if not payload:
        raise ValueError("the report text is empty")
    if len(payload) > PAYLOAD_MAX_BYTES:
        raise ValueError(f"the report text is over {PAYLOAD_MAX_BYTES // 1024} KiB")
    if payload.startswith(b"\xef\xbb\xbf"):
        raise ValueError("the report text must be UTF-8 without a byte-order mark")
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("the report text must be valid UTF-8") from None


def read_payload_file(path: Path) -> bytes:
    """The report file's bytes, at most the payload cap plus one, never blocking.

    A symlink, a FIFO, a directory or any other non-regular file is a usage
    error (``ValueError``). The open is non-blocking, so a FIFO cannot hang.
    """
    try:
        fd = osf.open_fd(path, os.O_RDONLY | os.O_NONBLOCK, follow_symlinks=False)
    except OSError:
        raise ValueError("the report file cannot be opened as a regular file") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("the report file must be a regular file")
        chunks: list[bytes] = []
        total = 0
        while total <= PAYLOAD_MAX_BYTES:
            chunk = os.read(fd, min(65536, PAYLOAD_MAX_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks)
    except OSError:
        raise ValueError("the report file cannot be read") from None
    finally:
        os.close(fd)


def bytes_appended(payload: bytes) -> int:
    """What an append adds to an existing log: the separator and the payload."""
    return len(APPEND_SEPARATOR) + len(payload)


def appended_bytes(
    existing: bytes | None,
    payload: bytes,
    *,
    heading: bytes = LOG_HEADINGS["curation"],
) -> bytes:
    """The whole log after one append, refusing a result over the size cap."""
    base = existing if existing is not None else heading
    result = base + APPEND_SEPARATOR + payload
    if len(result) > LOG_MAX_BYTES:
        raise CurationLogError("the log would exceed its size limit; archive it first")
    return result


# ── Frontmatter ───────────────────────────────────────────────────────────


def _is_fence(line: str) -> bool:
    """A frontmatter fence: ``---`` with optional trailing spaces or a line ending."""
    return line.rstrip("\r\n").rstrip(" \t") == _FENCE


def frontmatter_block(text: str) -> tuple[list[str], int | None] | None:
    """The document's lines and its closing-fence index, judged the same for read and write.

    ``None`` when the document has no frontmatter opener. ``(lines, None)``
    when the opener has no closer. Otherwise ``(lines, close)``, where
    ``lines[close]`` is the closing fence and ``lines[1:close]`` the block.
    """
    lines = text.splitlines(keepends=True)
    if not lines or not _is_fence(lines[0]):
        return None
    close = next((index for index in range(1, len(lines)) if _is_fence(lines[index])), None)
    return lines, close


def marker_value(text: str) -> str:
    """The ``last_full_pass`` value in the frontmatter, or "" when it is absent or ambiguous."""
    parsed = frontmatter_block(text)
    if parsed is None or parsed[1] is None:
        return ""
    lines, close = parsed
    values = [
        line.partition(":")[2].strip().strip("\"'")
        for line in lines[1:close]
        if line.partition(":")[0].strip() == MARKER_KEY
    ]
    return values[0] if len(values) == 1 else ""


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise CurationLogError("the log is not valid UTF-8") from None


def with_full_pass_marker(existing: bytes | None, stamp: str) -> bytes:
    """The log with ``last_full_pass: <stamp>`` in its frontmatter, every other byte kept.

    An existing owned field is replaced in place; a block with no such field
    gets one before its closing fence; a document with no frontmatter gets a
    new block in front. An unclosed block or two owned fields refuse.
    """
    line_value = f"{MARKER_KEY}: {stamp}"
    if existing is None:
        result = f"---\n{line_value}\n---\n\n# Curation log\n".encode()
    else:
        text = _decode(existing)
        parsed = frontmatter_block(text)
        if parsed is None:
            result = f"---\n{line_value}\n---\n\n{text}".encode()
        else:
            result = _marker_in_block(parsed, line_value).encode("utf-8")
    if len(result) > LOG_MAX_BYTES:
        raise CurationLogError("the log would exceed its size limit; archive it first")
    return result


def _marker_in_block(parsed: tuple[list[str], int | None], line_value: str) -> str:
    lines, close = parsed
    if close is None:
        raise CurationLogError("the log frontmatter is not closed")
    newline = "\r\n" if lines[0].endswith("\r\n") else "\n"
    owned = [
        index
        for index in range(1, close)
        if lines[index].partition(":")[0].strip() == MARKER_KEY
    ]
    if len(owned) > 1:
        raise CurationLogError("the log frontmatter repeats the marker field")
    if owned:
        lines[owned[0]] = line_value + newline
    else:
        lines.insert(close, line_value + newline)
    return "".join(lines)


# ── Vault, workspace and identity ─────────────────────────────────────────


def canonical_vault(vault_root: Path) -> Path:
    """The vault root with every alias resolved. An unresolvable root refuses.

    Root aliases converge here, so a log path built from an alias is the same
    file the canonical state lock protects.
    """
    try:
        return Path(vault_root).resolve(strict=True)
    except (OSError, RuntimeError):
        raise CurationLogError("the vault root cannot be resolved") from None


def _private_dir_is_real(path: Path) -> None:
    try:
        entry = os.lstat(path)
    except OSError as exc:
        raise _mapped("inspect directory", exc) from None
    if not stat.S_ISDIR(entry.st_mode):
        raise CurationLogError("the vault or workspace path is not a real directory")


def ensure_workspace_dir(vault_root: Path) -> Path:
    """The vault's ``Workspace`` directory, created privately when it is absent.

    Only that one directory is ever created. A missing vault root, or a
    workspace path that is a link or not a directory, refuses.
    """
    vault = canonical_vault(vault_root)
    _private_dir_is_real(vault)
    workspace = vault / _WORKSPACE_DIR
    try:
        os.lstat(workspace)
    except FileNotFoundError:
        try:
            os.mkdir(workspace, 0o700)
        except OSError as exc:
            raise _mapped("create workspace directory", exc) from None
    except OSError as exc:
        raise _mapped("inspect workspace directory", exc) from None
    _private_dir_is_real(workspace)
    return workspace


def refuse_identity_overlap(path: Path, others: list[Path]) -> None:
    """Refuse when *path* resolves to the same file as any of *others*.

    Follows links, so an alias of the state file or of the other log counts.
    """
    try:
        mine = os.stat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise _mapped("inspect log", exc) from None
    for other in others:
        try:
            theirs = os.stat(other)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _mapped("inspect curation file", exc) from None
        if (theirs.st_dev, theirs.st_ino) == (mine.st_dev, mine.st_ino):
            raise CurationLogError("the log is the same file as another curation file")


# ── Read and publish ──────────────────────────────────────────────────────


def _acl_refusal(fd: int) -> None:
    blob = osf.descriptor_posix_acl_blob(fd)
    if blob is not None and osf.acl_blob_names_other_principals(blob):
        raise CurationLogError("the file carries an ACL that names another account")
    if descriptor_acl_grants_another_account(fd):
        raise CurationLogError("the file carries an ACL that grants another account")


def read_log(path: Path) -> LogSnapshot:
    """The log's bytes and the identity they came from.

    A reliably absent leaf returns an empty snapshot. A symlink, FIFO,
    directory, another owner's file, a hard-linked file, an ACL naming another
    account, or a file over the cap refuses. Identity is taken from the open
    descriptor and re-checked after the read, so a concurrent change refuses.
    """
    try:
        fd = osf.open_fd(path, os.O_RDONLY | os.O_NONBLOCK, follow_symlinks=False)
    except FileNotFoundError:
        return LogSnapshot(None, None)
    except OSError as exc:
        raise _mapped("open log", exc) from None
    try:
        entry = os.fstat(fd)
        if not stat.S_ISREG(entry.st_mode):
            raise CurationLogError("the log is not a regular file")
        if entry.st_uid != osf.effective_uid():
            raise CurationLogError("the log is not owned by this user")
        if entry.st_nlink != 1:
            raise CurationLogError("the log has another hard link")
        if entry.st_size > LOG_MAX_BYTES:
            raise CurationLogError("the log is over its size limit; archive it first")
        _acl_refusal(fd)
        identity = _identity(entry)
        chunks: list[bytes] = []
        total = 0
        while total <= LOG_MAX_BYTES:
            chunk = os.read(fd, min(65536, LOG_MAX_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > LOG_MAX_BYTES:
            raise CurationLogError("the log is over its size limit; archive it first")
        if _identity(os.fstat(fd)) != identity:
            raise CurationLogError("the log changed while it was being read")
        return LogSnapshot(b"".join(chunks), identity)
    except OSError as exc:
        raise _mapped("read log", exc) from None
    finally:
        os.close(fd)


def _leaf_matches(path: Path, snapshot: LogSnapshot) -> None:
    """Refuse unless the live leaf is still the file *snapshot* describes."""
    try:
        entry = os.lstat(path)
    except FileNotFoundError:
        entry = None
    except OSError as exc:
        raise _mapped("recheck log", exc) from None
    if snapshot.identity is None:
        if entry is not None:
            raise CurationLogError("the log appeared while it was being written")
        return
    if entry is None:
        raise CurationLogError("the log disappeared while it was being written")
    if not stat.S_ISREG(entry.st_mode) or _identity(entry) != snapshot.identity:
        raise CurationLogError("the log changed after it was read")


def publish_log(path: Path, data: bytes, *, snapshot: LogSnapshot) -> None:
    """Atomically replace the log with *data*, but only if it is still the file *snapshot* read.

    An existing file keeps its owner, group and permission bits. A file that
    is read-only, owned by someone else, hard-linked, or carries a foreign ACL
    refuses before anything is written. The temporary sibling is private and
    is removed on any failure.
    """
    path = Path(path)
    _private_dir_is_real(path.parent)
    if snapshot.identity is not None and not snapshot.identity[2] & stat.S_IWUSR:
        raise CurationLogError("the log is read-only")
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    fd: int | None = None
    try:
        try:
            fd = osf.open_fd(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, follow_symlinks=False)
            if snapshot.identity is not None:
                gid = snapshot.identity[4]
                if gid != osf.effective_gid():
                    osf.chown_group(fd, gid)
                osf.chmod_descriptor(fd, snapshot.identity[2])
            _acl_refusal(fd)
            osf.write_all(fd, data)
            osf.flush_descriptor(fd)
        except OSError as exc:
            raise _mapped("write staged log", exc) from None
        _leaf_matches(path, snapshot)
        try:
            osf.replace_file(tmp, path)
        except OSError as exc:
            raise _mapped("replace log", exc) from None
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    finally:
        if fd is not None:
            os.close(fd)
