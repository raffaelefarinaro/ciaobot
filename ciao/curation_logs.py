"""Bounded byte primitives for the packaged curation logs.

The two decision logs, ``Workspace/Curation-Log.md`` and
``Workspace/Weekly-Review-Log.md``, are written by two paths: the managed
``curation-log-append`` verb and the weekly ``last_full_pass`` marker. This
module owns the file-level half of both: bounded no-follow reads, the exact
bytes an append or a marker produces, and an atomic private publication.

Nothing here acquires the curation state lock or reads the lease. The caller
(:mod:`ciao.curation_run`) holds the canonical state lock around every call
that reads a log and then publishes it, so the check and the write are one
critical section.
"""

from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path

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


class CurationLogError(RuntimeError):
    """A log cannot be read or written safely. The message is bounded and carries no payload."""


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


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise CurationLogError("the log is not valid UTF-8") from None


def with_full_pass_marker(existing: bytes | None, stamp: str) -> bytes:
    """The log with ``last_full_pass: <stamp>`` in its frontmatter, every other byte kept.

    An existing owned field is replaced in place; a block with no such field
    gets one before its closing fence; a document with no frontmatter gets a
    new block in front. Ambiguous control state (an unclosed block or two owned
    fields) refuses, so nothing is guessed at.
    """
    line_value = f"{MARKER_KEY}: {stamp}"
    if existing is None:
        result = f"---\n{line_value}\n---\n\n# Curation log\n".encode()
    else:
        text = _decode(existing)
        if text.startswith("---\n") or text.startswith("---\r\n"):
            result = _marker_in_block(text, line_value).encode("utf-8")
        else:
            newline = "\n"
            result = f"---{newline}{line_value}{newline}---{newline}{newline}{text}".encode()
    if len(result) > LOG_MAX_BYTES:
        raise CurationLogError("the log would exceed its size limit; archive it first")
    return result


def _marker_in_block(text: str, line_value: str) -> str:
    lines = text.splitlines(keepends=True)
    close = next(
        (index for index in range(1, len(lines)) if lines[index].rstrip("\r\n") == "---"),
        None,
    )
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
        return "".join(lines)
    lines.insert(close, line_value + newline)
    return "".join(lines)


def _private_dir_is_real(path: Path) -> os.stat_result:
    try:
        entry = os.lstat(path)
    except OSError:
        raise CurationLogError("the vault or workspace directory cannot be inspected") from None
    if not stat.S_ISDIR(entry.st_mode):
        raise CurationLogError("the vault or workspace path is not a real directory")
    return entry


def canonical_vault(vault_root: Path) -> Path:
    """The vault root with every alias resolved. An unresolvable root refuses.

    Root aliases converge here, so a log path built from an alias is the same
    file the canonical state lock protects.
    """
    try:
        return Path(vault_root).resolve(strict=True)
    except (OSError, RuntimeError):
        raise CurationLogError("the vault root cannot be resolved") from None


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
        os.mkdir(workspace, 0o700)
    _private_dir_is_real(workspace)
    return workspace


def read_log(path: Path) -> bytes | None:
    """The log's bytes, ``None`` when its leaf is absent. Never follows a link or blocks on a FIFO."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise CurationLogError("the log cannot be opened safely") from None
    try:
        entry = os.fstat(fd)
        if not stat.S_ISREG(entry.st_mode):
            raise CurationLogError("the log is not a regular file")
        if entry.st_uid != os.geteuid():
            raise CurationLogError("the log is not owned by this user")
        if descriptor_acl_grants_another_account(fd):
            raise CurationLogError("the log carries an ACL that grants another account")
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
        return b"".join(chunks)
    finally:
        os.close(fd)


def _identity(entry: os.stat_result) -> tuple[int, int, int, int, int]:
    return (entry.st_dev, entry.st_ino, stat.S_IMODE(entry.st_mode), entry.st_uid, entry.st_gid)


def publish_log(path: Path, data: bytes, *, previous: bytes | None) -> None:
    """Atomically replace the log with *data*, keeping an existing file's owner, group and mode.

    ``previous`` says what the caller read: ``None`` requires the leaf to be
    absent right before publication, and bytes require a regular file whose
    identity has not changed since. The temporary sibling is private and is
    removed on any failure.
    """
    path = Path(path)
    parent = path.parent
    _private_dir_is_real(parent)
    before = _leaf_identity(path, previous is not None)
    tmp = parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        try:
            if before is not None:
                if before[3] != os.geteuid():
                    raise CurationLogError("the log is not owned by this user")
                if before[4] != os.getegid():
                    os.fchown(fd, -1, before[4])
                os.fchmod(fd, before[2])
            if descriptor_acl_grants_another_account(fd):
                raise CurationLogError("the staged log carries an ACL that grants another account")
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        if _leaf_identity(path, previous is not None) != before:
            raise CurationLogError("the log changed while it was being written")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _leaf_identity(path: Path, required: bool) -> tuple[int, int, int, int, int] | None:
    try:
        entry = os.lstat(path)
    except FileNotFoundError:
        if required:
            raise CurationLogError("the log disappeared while it was being written") from None
        return None
    if not required:
        raise CurationLogError("the log appeared while it was being written")
    if not stat.S_ISREG(entry.st_mode):
        raise CurationLogError("the log is not a regular file")
    return _identity(entry)
