"""The run-bound native-install lock (#2340).

This module owns the run-bound native-install lock
(``NativeRunLock``): one ``0700`` directory ``native-install.lock`` with one
``0600`` ``run.json`` provenance record under the caller's private
install-state directory. ``prepare_native_state_dir`` is the explicit,
mutating preparation step: call it after the read-only freshness preflight.
The target must lie below a trusted root (the home directory by default, or an
explicit caller-supplied root), and every component from that root down is
opened descriptor-relative and no-follow before anything is created; a symlink
anywhere in the controlled chain is refused rather than followed. It creates
only missing components with ``0700`` while never chmod'ing, repairing or
replacing an existing one; a umask or permission failure leaves what was
created as evidence and refuses. The trusted root keeps macOS system prefixes
such as ``/var`` outside the validated chain. ``NativeRunLock.acquire`` itself
never creates the state directory. Creation of a lock is one atomic ``mkdir``
plus one exclusive record create, both descriptor-relative. An existing lock
is classified honestly and refused: a valid record is ``lock_busy`` (a run may
be live or stale, and no same-run shortcut adopts it); an empty directory or a
missing, empty, truncated or undecodable private record is ``lock_unknown``
(concurrent or interrupted, never corruption — it may be a live creator
between ``mkdir`` and record write, an interrupted one, or a lock/record that
disappeared between classification steps during a concurrent release); a symlink,
non-directory, non-private directory, foreign entry, unsafe record shape or a complete
record this version cannot accept is ``lock_unsafe`` (not a corruption
diagnosis). Neither is repaired, deleted or
adopted; a partial retry stays refused and there is no stale-lock recovery, no
flock unlock as authorization, and no silent reinstall. Manual
recovery is an operator decision outside this primitive: if no native install is
running, re-run the read-only freshness preflight and inspect the fixed private
state-directory evidence; the tooling does not delete, adopt or roll it back
automatically, and its errors never print the inspected path. A released lock
is removed only when its directory and record still match the device/inode and
exact bytes this run created, it is the only entry, the record has no extra hard
link, and both are still private. A refusal before the record unlink leaves the
state untouched; a failure after the unlink (for example, a final ``rmdir``) has
already removed the record and leaves the directory, and still raises
``lock_cleanup_refused`` instead of reporting success. The error carries a
path-free ``LockCleanupReport`` (record/directory removed) and a
``original_reason`` where one exists, so the coordinator can record what
happened; ``cleanup`` can be ``None`` after an asynchronous interruption before
a report was recorded. There is no recursive deletion or chmod. Release is serialized by an
internal lock and a re-entrant call is refused with ``lock_reentrant_release``;
a failed release is not silently retried and keeps its cleanup report. A
``SIGKILL`` or unprovable cleanup leaves evidence for the next refusal. Signal
handlers must not call ``release`` either; cancellation belongs to the
coordinator's normal loop. A same-UID writer can still race between a check and
the mutation it guards, so these checks are evidence, not proof.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Final, NoReturn

from ciao.os_support.private import (
    descriptor_acl_grants_another_account,
    is_owned_descriptor,
    is_private_descriptor_strict,
)


#: The machine-level native-install lock directory, under ciaobot's state dir.
LOCK_DIR_NAME: Final[str] = "native-install.lock"
#: The one provenance record inside that directory.
LOCK_RECORD_NAME: Final[str] = "run.json"
#: The lock record schema. Independent of the public event schema.
LOCK_SCHEMA_VERSION: Final[int] = 1
#: The provenance record is one small JSON object.
MAX_LOCK_RECORD_BYTES: Final[int] = 1024

#: Test seam: all lock inode checks go through this alias so a focused test
#: can inject an fstat failure without changing the private-descriptor helper.
_fstat = os.fstat


def _canonical_lock_run_id(value: object) -> str:
    """Return ``value`` only when it is already the canonical UUID spelling."""
    if type(value) is not str:
        raise NativeLockError("invalid_run_id")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise NativeLockError("invalid_run_id") from None
    if str(parsed) != value:
        raise NativeLockError("invalid_run_id")
    return value


@dataclass(frozen=True, slots=True)
class LockCleanupReport:
    """What a refused cleanup actually removed, with no paths or inodes."""

    record_removed: bool
    directory_removed: bool


class NativeLockError(RuntimeError):
    """A fixed native-install lock refusal with no interpolated path detail.

    ``cleanup`` is a path-free :class:`LockCleanupReport` when a cleanup
    refusal recorded one. It is ``None`` for a non-cleanup error, and can be
    ``None`` after an asynchronous interruption (for example a signal or
    ``KeyboardInterrupt``) before any report could be recorded; callers must
    not fabricate a success or a removal state from a missing report.
    """

    def __init__(
        self,
        reason: str,
        *,
        original_reason: str | None = None,
        cleanup: LockCleanupReport | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.original_reason = original_reason
        self.cleanup = cleanup


def _is_missing(exc: OSError) -> bool:
    """Whether an OS error means the name disappeared during a concurrent release."""
    return isinstance(exc, FileNotFoundError) or exc.errno == errno.ENOENT


def _open_existing_dir(path: Path, *, exact_private: bool) -> int:
    """Open an existing real directory, optionally requiring exact ``0700``."""
    try:
        status = os.lstat(path)
    except OSError:
        raise NativeLockError("state_dir_unsafe") from None
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise NativeLockError("state_dir_unsafe")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise NativeLockError("state_dir_unsafe") from None
    try:
        try:
            opened = _fstat(descriptor)
        except OSError:
            raise NativeLockError("state_dir_unsafe") from None
        if (opened.st_dev, opened.st_ino) != (status.st_dev, status.st_ino):
            raise NativeLockError("state_dir_unsafe")
        if not stat.S_ISDIR(opened.st_mode):
            raise NativeLockError("state_dir_unsafe")
        if exact_private:
            try:
                private = is_private_descriptor_strict(descriptor)
            except OSError:
                raise NativeLockError("state_dir_unsafe") from None
            if not private:
                raise NativeLockError("state_dir_unsafe")
        else:
            try:
                owned = is_owned_descriptor(descriptor)
                acl_grant = descriptor_acl_grants_another_account(descriptor)
            except OSError:
                raise NativeLockError("state_dir_unsafe") from None
            if (
                not owned
                or acl_grant
                or stat.S_IMODE(opened.st_mode) & 0o022
            ):
                raise NativeLockError("state_dir_unsafe")
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    return descriptor


def _close_quietly(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


class NativeStateDir:
    """The validated private install-state directory, still held open. Single use.

    :func:`prepare_native_state_dir` returns this handle with the final
    directory descriptor kept open. :meth:`NativeRunLock.acquire` consumes it
    through :meth:`take`, which re-proves the held descriptor, so acquisition
    never reopens the directory by path. Close it with :meth:`close`, or use it
    as a context manager, when it is not handed to acquisition.
    """

    __slots__ = ("_path", "_fd", "_identity")

    def __init__(self, path: Path, fd: int, identity: tuple[int, int]) -> None:
        self._path = path
        self._fd = fd
        self._identity = identity

    @property
    def path(self) -> Path:
        return self._path

    def take(self) -> int:
        """Transfer the held descriptor to the caller, after re-proving it.

        Refuses ``state_dir_unsafe`` when the handle was already taken or
        closed, when the held descriptor is no longer the directory named by
        :attr:`path`, when it is not a directory, or when it is no longer
        strictly private. Every refusal closes the descriptor.
        """
        descriptor, self._fd = self._fd, -1
        if descriptor < 0:
            raise NativeLockError("state_dir_unsafe")
        try:
            held = _fstat(descriptor)
            named = os.lstat(self._path)
            private = is_private_descriptor_strict(descriptor)
        except OSError:
            _close_quietly(descriptor)
            raise NativeLockError("state_dir_unsafe") from None
        if (
            not stat.S_ISDIR(held.st_mode)
            or stat.S_ISLNK(named.st_mode)
            or (held.st_dev, held.st_ino) != self._identity
            or (named.st_dev, named.st_ino) != self._identity
            or not private
        ):
            _close_quietly(descriptor)
            raise NativeLockError("state_dir_unsafe")
        return descriptor

    def close(self) -> None:
        descriptor, self._fd = self._fd, -1
        if descriptor >= 0:
            _close_quietly(descriptor)

    def __enter__(self) -> NativeStateDir:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def prepare_native_state_dir(
    state_dir: Path, *, trusted_root: Path | None = None
) -> NativeStateDir:
    """Explicitly create the missing private install-state chain, or refuse.

    This is the only mutating preparation step. It must run after the native
    installer's read-only freshness preflight and before lock acquisition. The
    target must lie below ``trusted_root`` (the caller's home directory by
    default; tests and embedding callers may pass another explicit root). Every
    component from that root down to the target is opened descriptor-relative
    and no-follow: a symlink anywhere in the HOME-relative chain is a refusal,
    not something to follow. Existing components are judged and never
    chmod'ed, repaired, replaced or removed. Missing components are created
    ``0700`` and immediately checked; a restrictive umask or permission failure
    is ``state_dir_unsafe`` and leaves what was created as evidence. The final
    state directory is never created by :meth:`NativeRunLock.acquire` itself.

    The return value is a single-use :class:`NativeStateDir` handle that keeps
    the final directory descriptor open. Pass it to :meth:`NativeRunLock.acquire`,
    or close it.

    The trusted root is the portability boundary: on macOS the system prefix
    above the user's home (including ``/var``) is not inspected, while every
    component the user controls below the root is. A same-UID writer can still
    race between validating a component and creating its child; each created
    component is checked, but that is evidence, not proof of exclusive
    ownership.
    """
    if sys.platform == "win32":
        raise NativeLockError("unsupported_platform")
    target = Path(state_dir)
    try:
        root = Path.home() if trusted_root is None else Path(trusted_root)
    except (OSError, RuntimeError):
        raise NativeLockError("state_dir_unsafe") from None
    for path in (target, root):
        if not path.is_absolute():
            raise NativeLockError("state_dir_unsafe")
        if any(part in {".", ".."} for part in path.parts):
            raise NativeLockError("state_dir_unsafe")
    if target != root and root not in target.parents:
        raise NativeLockError("state_dir_unsafe")

    parent_fd = _open_existing_dir(root, exact_private=target == root)
    current = root
    try:
        for part in target.relative_to(root).parts:
            component = current / part
            created = False
            try:
                os.lstat(component)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=parent_fd)
                    created = True
                except FileExistsError:
                    pass
                except OSError:
                    raise NativeLockError("state_dir_unsafe") from None
            except OSError:
                raise NativeLockError("state_dir_unsafe") from None

            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                child_fd = os.open(part, flags, dir_fd=parent_fd)
            except OSError:
                raise NativeLockError("state_dir_unsafe") from None
            try:
                try:
                    opened = _fstat(child_fd)
                except OSError:
                    raise NativeLockError("state_dir_unsafe") from None
                if not stat.S_ISDIR(opened.st_mode):
                    raise NativeLockError("state_dir_unsafe")
                try:
                    named = os.stat(
                        part, dir_fd=parent_fd, follow_symlinks=False
                    )
                except OSError:
                    raise NativeLockError("state_dir_unsafe") from None
                if (named.st_dev, named.st_ino) != (
                    opened.st_dev,
                    opened.st_ino,
                ):
                    raise NativeLockError("state_dir_unsafe")
                if created or component == target:
                    try:
                        private = is_private_descriptor_strict(child_fd)
                    except OSError:
                        raise NativeLockError("state_dir_unsafe") from None
                    if not private:
                        raise NativeLockError("state_dir_unsafe")
                else:
                    try:
                        owned = is_owned_descriptor(child_fd)
                        acl_grant = descriptor_acl_grants_another_account(
                            child_fd
                        )
                    except OSError:
                        raise NativeLockError("state_dir_unsafe") from None
                    if (
                        not owned
                        or acl_grant
                        or stat.S_IMODE(opened.st_mode) & 0o022
                    ):
                        raise NativeLockError("state_dir_unsafe")
            except BaseException:
                try:
                    os.close(child_fd)
                except OSError:
                    pass
                raise
            try:
                os.close(parent_fd)
            except OSError:
                pass
            parent_fd = child_fd
            current = component
        try:
            held = _fstat(parent_fd)
        except OSError:
            raise NativeLockError("state_dir_unsafe") from None
        handle = NativeStateDir(target, parent_fd, (held.st_dev, held.st_ino))
        parent_fd = -1
        return handle
    finally:
        if parent_fd >= 0:
            _close_quietly(parent_fd)


def _lock_record_bytes(run_id: str) -> bytes:
    payload = {
        "schema": LOCK_SCHEMA_VERSION,
        "run_id": run_id,
        "pid": os.getpid(),
    }
    encoded = (
        json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        )
        + b"\n"
    )
    if len(encoded) > MAX_LOCK_RECORD_BYTES:
        raise NativeLockError("lock_record_failed")
    return encoded


def _write_record(fd: int, data: bytes) -> None:
    """Write the small provenance record, completing short writes."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("the lock record write made no progress")
        view = view[written:]


def _read_bounded_fd(fd: int, limit: int) -> bytes | None:
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            return None
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)


def _record_state(data: bytes) -> str:
    """Classify record bytes as ``busy``, ``unknown`` or ``unsafe``.

    Empty, truncated or undecodable bytes are ``unknown`` (an interrupted
    write). A complete JSON value this schema cannot accept is ``unsafe``
    (foreign or incompatible), never described as corruption.
    """
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "unknown"
    if not isinstance(payload, dict) or set(payload) != {"schema", "run_id", "pid"}:
        return "unsafe"
    if type(payload["schema"]) is not int or payload["schema"] != LOCK_SCHEMA_VERSION:
        return "unsafe"
    run_id = payload["run_id"]
    if type(run_id) is not str:
        return "unsafe"
    try:
        _canonical_lock_run_id(run_id)
    except NativeLockError:
        return "unsafe"
    pid = payload["pid"]
    if type(pid) is not int or pid <= 0:
        return "unsafe"
    return "busy"


def _lock_record_state(dir_fd: int) -> str:
    """Classify an existing record as ``busy``, ``unknown`` or ``unsafe``.

    A missing, empty, truncated or undecodable record in an otherwise private
    single-record directory is ``unknown``: a live creator may not have written
    it yet, or an earlier run was interrupted. A record with a foreign shape or
    privacy (symlink, hard link, non-regular file, oversized, wrong owner, mode
    or ACL) or a complete record this schema cannot accept is ``unsafe``.
    """
    try:
        entry = os.stat(LOCK_RECORD_NAME, dir_fd=dir_fd, follow_symlinks=False)
    except OSError as exc:
        return "unknown" if _is_missing(exc) else "unsafe"
    if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
        return "unsafe"
    if entry.st_size > MAX_LOCK_RECORD_BYTES:
        return "unsafe"
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(LOCK_RECORD_NAME, flags, dir_fd=dir_fd)
    except OSError as exc:
        return "unknown" if _is_missing(exc) else "unsafe"
    try:
        try:
            opened = _fstat(descriptor)
        except OSError as exc:
            return "unknown" if _is_missing(exc) else "unsafe"
        if (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino):
            return "unsafe"
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            return "unsafe"
        try:
            if not is_private_descriptor_strict(descriptor):
                return "unsafe"
        except OSError:
            return "unsafe"
        if opened.st_size > MAX_LOCK_RECORD_BYTES:
            return "unsafe"
        data = _read_bounded_fd(descriptor, MAX_LOCK_RECORD_BYTES)
        if data is None:
            return "unknown"
        return _record_state(data)
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _existing_lock_reason(parent_fd: int) -> str:
    """Classify a pre-existing lock directory without changing it.

    A valid record is ``lock_busy``. An empty directory or a missing, empty,
    truncated or undecodable record is ``lock_unknown``: concurrent (a live
    creator between ``mkdir`` and its record write) or interrupted, never
    corruption. A symlink, non-directory, non-private directory, foreign entry
    or unsafe record shape is ``lock_unsafe``. Every answer leaves the state
    untouched.
    """
    try:
        entry = os.stat(LOCK_DIR_NAME, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        return "lock_unknown" if _is_missing(exc) else "lock_unsafe"
    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
        return "lock_unsafe"
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        dir_fd = os.open(LOCK_DIR_NAME, flags, dir_fd=parent_fd)
    except OSError as exc:
        return "lock_unknown" if _is_missing(exc) else "lock_unsafe"
    try:
        try:
            opened = _fstat(dir_fd)
        except OSError as exc:
            return "lock_unknown" if _is_missing(exc) else "lock_unsafe"
        if (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino):
            return "lock_unsafe"
        try:
            if not is_private_descriptor_strict(dir_fd):
                return "lock_unsafe"
        except OSError as exc:
            return "lock_unknown" if _is_missing(exc) else "lock_unsafe"
        try:
            names = sorted(os.listdir(dir_fd))
        except OSError as exc:
            return "lock_unknown" if _is_missing(exc) else "lock_unsafe"
        if not names:
            return "lock_unknown"
        if names != [LOCK_RECORD_NAME]:
            return "lock_unsafe"
        state = _lock_record_state(dir_fd)
        if state == "busy":
            return "lock_busy"
        return "lock_unknown" if state == "unknown" else "lock_unsafe"
    finally:
        try:
            os.close(dir_fd)
        except OSError:
            pass


def _remove_new_lock_best_effort(
    parent_fd: int,
    dir_fd: int,
    dir_identity: tuple[int, int],
    record_identity: tuple[int, int] | None,
) -> LockCleanupReport:
    """Remove only a lock this call just created, or leave it all in place.

    Used after a failed acquisition. Every step is descriptor-relative and
    inode-checked; a substituted directory, a foreign entry, a hard-linked
    record or any error leaves the remaining state for the next run to refuse.
    The returned report says exactly what was removed, without paths or inodes.
    """
    record_removed = False
    directory_removed = False
    try:
        named = os.stat(LOCK_DIR_NAME, dir_fd=parent_fd, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != dir_identity:
            return LockCleanupReport(record_removed, directory_removed)
        if not stat.S_ISDIR(named.st_mode):
            return LockCleanupReport(record_removed, directory_removed)
        names = sorted(os.listdir(dir_fd))
        if record_identity is not None and names == [LOCK_RECORD_NAME]:
            entry = os.stat(
                LOCK_RECORD_NAME, dir_fd=dir_fd, follow_symlinks=False
            )
            if (entry.st_dev, entry.st_ino) != record_identity:
                return LockCleanupReport(record_removed, directory_removed)
            if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
                return LockCleanupReport(record_removed, directory_removed)
            os.unlink(LOCK_RECORD_NAME, dir_fd=dir_fd)
            record_removed = True
        elif names:
            return LockCleanupReport(record_removed, directory_removed)
        if os.listdir(dir_fd):
            return LockCleanupReport(record_removed, directory_removed)
        named = os.stat(LOCK_DIR_NAME, dir_fd=parent_fd, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != dir_identity:
            return LockCleanupReport(record_removed, directory_removed)
        os.rmdir(LOCK_DIR_NAME, dir_fd=parent_fd)
        directory_removed = True
        return LockCleanupReport(record_removed, directory_removed)
    except OSError:
        return LockCleanupReport(record_removed, directory_removed)


class NativeRunLock:
    """A run-bound, exclusive native-install lock with inode-bound cleanup.

    Acquisition atomically creates ``<state_dir>/native-install.lock`` with
    ``0700`` and one ``0600`` ``run.json`` provenance record (schema, canonical
    run id, pid). An existing directory is classified and refused, never
    repaired: a valid record is ``lock_busy``; an empty directory or a partial,
    truncated or undecodable private record is ``lock_unknown`` (concurrent or
    interrupted, never corruption); a symlink, foreign entry, non-private
    directory, unsafe record shape or a complete record this version cannot
    accept is ``lock_unsafe`` (not a corruption diagnosis). There is deliberately no
    same-run shortcut and no stale-lock recovery — a dead process's valid record
    is still evidence that a native install may have run.

    Release removes only the directory and record this object created. Both are
    re-opened relative to held directory descriptors, required to match the
    recorded device/inode, required to have no extra link or foreign entry, and
    the record's exact bytes must still be present. A refusal before the record
    unlink leaves the state untouched; a refusal after it (for example, the
    final ``rmdir`` fails because a foreign entry appeared) has already removed
    the record and leaves the directory, and still raises
    ``lock_cleanup_refused`` — it is never reported as success and is not
    retried. There is no recursive deletion and no path is chmod'ed, replaced or
    recovered. Call :meth:`release` or use the context manager; dropping the
    object does not remove the lock. Release is serialized by an internal
    re-entrant lock; a same-thread re-entrant call raises
    ``lock_reentrant_release`` instead of reusing descriptors.

    Limits: a same-UID writer can race between an identity check and a
    ``mkdir``/``unlink``/``rmdir``; the checks are evidence, not proof, and this
    module does not claim otherwise. A ``SIGKILL`` leaves the lock in place, and
    the next acquisition refuses it as ``lock_busy`` or ``lock_unknown``. A
    failed acquisition removes the directory it just created only when that
    removal can be proven. When an attempted removal cannot be proven (a
    substituted name, a foreign entry, an extra hard link), it leaves the state
    and raises ``lock_cleanup_refused`` from the original refusal; a permission
    or umask failure preserves the new evidence and reports the original lock
    refusal without attempting an unsafe cleanup. Acquisition consumes the
    handle :func:`prepare_native_state_dir` returned and re-binds its held
    descriptor; it never reopens the state directory by path and never creates
    or repairs it.
    """

    def __init__(
        self,
        *,
        state_dir: Path,
        run_id: str,
        parent_fd: int,
        dir_fd: int,
        dir_identity: tuple[int, int],
        record_identity: tuple[int, int],
        record_bytes: bytes,
    ) -> None:
        self._state_dir = state_dir
        self._run_id = run_id
        self._parent_fd = parent_fd
        self._dir_fd = dir_fd
        self._dir_identity = dir_identity
        self._record_identity = record_identity
        self._record_bytes = record_bytes
        self._released = False
        self._cleanup_failed = False
        self._cleanup_report: LockCleanupReport | None = None
        self._releasing = False
        self._release_guard = threading.RLock()

    @classmethod
    def acquire(cls, state: NativeStateDir, *, run_id: str) -> NativeRunLock:
        """Atomically create this run's lock, or refuse an existing one.

        ``state`` is the handle :func:`prepare_native_state_dir` returned. It is
        consumed here: its held descriptor is re-bound, never reopened by path.
        """
        if sys.platform == "win32":
            raise NativeLockError("unsupported_platform")
        if not isinstance(state, NativeStateDir):
            raise NativeLockError("state_dir_unsafe")
        canonical = _canonical_lock_run_id(run_id)
        parent_fd = state.take()
        state_dir = state.path
        dir_fd: int | None = None
        record_fd: int | None = None
        dir_identity: tuple[int, int] | None = None
        record_identity: tuple[int, int] | None = None
        record_bytes = b""
        cleanup_ok = False
        keep_open = False
        try:
            try:
                os.mkdir(LOCK_DIR_NAME, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                raise NativeLockError(_existing_lock_reason(parent_fd)) from None
            except OSError:
                raise NativeLockError("lock_create_failed") from None

            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                dir_fd = os.open(LOCK_DIR_NAME, flags, dir_fd=parent_fd)
            except OSError:
                # The new name cannot be proven safe to remove without a
                # descriptor, so it is left for the next run to classify.
                raise NativeLockError("lock_create_failed") from None
            try:
                opened = _fstat(dir_fd)
            except OSError:
                raise NativeLockError("lock_create_failed") from None
            dir_identity = (opened.st_dev, opened.st_ino)
            try:
                private_dir = is_private_descriptor_strict(dir_fd)
            except OSError:
                raise NativeLockError("lock_create_failed") from None
            if not stat.S_ISDIR(opened.st_mode) or not private_dir:
                raise NativeLockError("lock_create_failed")
            try:
                named = os.stat(
                    LOCK_DIR_NAME, dir_fd=parent_fd, follow_symlinks=False
                )
            except OSError:
                raise NativeLockError("lock_unsafe") from None
            if (named.st_dev, named.st_ino) != dir_identity:
                raise NativeLockError("lock_unsafe")
            try:
                if os.listdir(dir_fd):
                    raise NativeLockError("lock_unsafe")
            except OSError:
                raise NativeLockError("lock_unsafe") from None
            cleanup_ok = True

            record_bytes = _lock_record_bytes(canonical)
            record_flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                record_fd = os.open(
                    LOCK_RECORD_NAME, record_flags, 0o600, dir_fd=dir_fd
                )
            except OSError:
                raise NativeLockError("lock_record_failed") from None
            try:
                try:
                    opened_record = _fstat(record_fd)
                except OSError:
                    raise NativeLockError("lock_record_failed") from None
                record_identity = (opened_record.st_dev, opened_record.st_ino)
                try:
                    private_record = is_private_descriptor_strict(record_fd)
                except OSError:
                    cleanup_ok = False
                    raise NativeLockError("lock_record_failed") from None
                if not private_record:
                    cleanup_ok = False
                    raise NativeLockError("lock_record_failed")
                try:
                    _write_record(record_fd, record_bytes)
                except OSError:
                    raise NativeLockError("lock_record_failed") from None
                try:
                    opened_record = _fstat(record_fd)
                except OSError:
                    raise NativeLockError("lock_record_failed") from None
                if (
                    not stat.S_ISREG(opened_record.st_mode)
                    or opened_record.st_nlink != 1
                    or opened_record.st_size != len(record_bytes)
                ):
                    cleanup_ok = False
                    raise NativeLockError("lock_record_failed")
            finally:
                if record_fd is not None:
                    try:
                        os.close(record_fd)
                    except OSError:
                        pass
                    record_fd = None

            if dir_fd is None or record_identity is None:
                raise NativeLockError("lock_record_failed")
            lock = cls(
                state_dir=state_dir,
                run_id=canonical,
                parent_fd=parent_fd,
                dir_fd=dir_fd,
                dir_identity=dir_identity,
                record_identity=record_identity,
                record_bytes=record_bytes,
            )
            keep_open = True
            return lock
        except BaseException as original:
            if cleanup_ok:
                report = LockCleanupReport(False, False)
                if dir_fd is not None and dir_identity is not None:
                    try:
                        report = _remove_new_lock_best_effort(
                            parent_fd, dir_fd, dir_identity, record_identity
                        )
                    except BaseException:
                        report = LockCleanupReport(False, False)
                if not report.directory_removed:
                    # The original failure and the failed cleanup are both
                    # surfaced; neither is hidden behind the other.
                    raise NativeLockError(
                        "lock_cleanup_refused",
                        original_reason=(
                            original.reason
                            if isinstance(original, NativeLockError)
                            else None
                        ),
                        cleanup=report,
                    ) from original
            raise
        finally:
            if record_fd is not None:
                try:
                    os.close(record_fd)
                except OSError:
                    pass
            if not keep_open:
                if dir_fd is not None:
                    try:
                        os.close(dir_fd)
                    except OSError:
                        pass
                try:
                    os.close(parent_fd)
                except OSError:
                    pass

    @property
    def path(self) -> Path:
        return self._state_dir / LOCK_DIR_NAME

    @property
    def record_path(self) -> Path:
        return self.path / LOCK_RECORD_NAME

    @property
    def run_id(self) -> str:
        return self._run_id

    def assert_held(self, *, run_id: str | None = None) -> None:
        """Prove this object still holds the exact lock it created; never mutates.

        The check is descriptor-bound: the held directory descriptor must still
        name the created device/inode, the public name under the parent must still
        resolve to that same directory, the directory must still be private and
        contain exactly the one record, and the record must still be the same
        inode, private, single-linked, the expected size and the exact bytes this
        run wrote. Those bytes bind the schema, canonical run id and pid, so an
        optional ``run_id`` must also match both this object and the record.

        A released object, a descriptor closed by a failed release, a substituted
        directory or record, an extra entry or link, changed bytes and a changed
        run id are all refusals with fixed reasons: ``lock_not_held``,
        ``lock_changed``, ``lock_unsafe``, ``lock_run_mismatch`` or
        ``invalid_run_id``. Nothing here writes, unlinks, renames, chmods or
        repairs; a refusal leaves the state for release or the next acquisition
        to classify. As everywhere else in this lock, a same-UID writer can still
        race between a check and the next operation; this narrows and reports the
        window, it does not claim to close it.
        """
        with self._release_guard:
            if (
                self._released
                or self._cleanup_failed
                or self._dir_fd < 0
                or self._parent_fd < 0
            ):
                raise NativeLockError("lock_not_held")
            if run_id is not None:
                canonical = _canonical_lock_run_id(run_id)
                if canonical != self._run_id:
                    raise NativeLockError("lock_run_mismatch")
            try:
                state_named = os.lstat(self._state_dir)
                state_held = _fstat(self._parent_fd)
            except OSError:
                raise NativeLockError("lock_changed") from None
            if (
                not stat.S_ISDIR(state_named.st_mode)
                or (state_named.st_dev, state_named.st_ino)
                != (state_held.st_dev, state_held.st_ino)
            ):
                raise NativeLockError("lock_changed")
            try:
                if not is_private_descriptor_strict(self._parent_fd):
                    raise NativeLockError("lock_unsafe")
            except OSError:
                raise NativeLockError("lock_unsafe") from None
            try:
                held = _fstat(self._dir_fd)
            except OSError:
                raise NativeLockError("lock_not_held") from None
            if (
                not stat.S_ISDIR(held.st_mode)
                or (held.st_dev, held.st_ino) != self._dir_identity
            ):
                raise NativeLockError("lock_changed")
            try:
                named = os.stat(
                    LOCK_DIR_NAME,
                    dir_fd=self._parent_fd,
                    follow_symlinks=False,
                )
            except OSError:
                raise NativeLockError("lock_changed") from None
            if (
                not stat.S_ISDIR(named.st_mode)
                or (named.st_dev, named.st_ino) != self._dir_identity
            ):
                raise NativeLockError("lock_changed")
            try:
                if not is_private_descriptor_strict(self._dir_fd):
                    raise NativeLockError("lock_unsafe")
            except OSError:
                raise NativeLockError("lock_unsafe") from None
            try:
                names = os.listdir(self._dir_fd)
            except OSError:
                raise NativeLockError("lock_changed") from None
            if names != [LOCK_RECORD_NAME]:
                raise NativeLockError("lock_changed")
            flags = (
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_BINARY", 0)
            )
            try:
                descriptor = os.open(
                    LOCK_RECORD_NAME, flags, dir_fd=self._dir_fd
                )
            except OSError:
                raise NativeLockError("lock_changed") from None
            try:
                try:
                    opened = _fstat(descriptor)
                except OSError:
                    raise NativeLockError("lock_not_held") from None
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino) != self._record_identity
                ):
                    raise NativeLockError("lock_changed")
                try:
                    if not is_private_descriptor_strict(descriptor):
                        raise NativeLockError("lock_unsafe")
                except OSError:
                    raise NativeLockError("lock_unsafe") from None
                if opened.st_size != len(self._record_bytes):
                    raise NativeLockError("lock_changed")
                data = _read_bounded_fd(descriptor, MAX_LOCK_RECORD_BYTES)
                if data != self._record_bytes:
                    raise NativeLockError("lock_changed")
            finally:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def release(self) -> None:
        """Remove this run's own lock, or refuse and leave it untouched."""
        with self._release_guard:
            if self._released:
                return
            if self._cleanup_failed:
                # A previous attempt already refused. Do not turn that into a
                # silent success, and do not reuse descriptors that were closed.
                raise NativeLockError(
                    "lock_cleanup_refused", cleanup=self._cleanup_report
                )
            if self._releasing:
                # A signal handler or callback re-entered release on the same
                # thread while the first attempt owned the descriptors.
                raise NativeLockError("lock_reentrant_release")
            self._releasing = True
            parent_fd = self._parent_fd
            dir_fd = self._dir_fd
            self._parent_fd = -1
            self._dir_fd = -1
            try:
                self._release_checked(parent_fd, dir_fd)
            except BaseException as failure:
                self._cleanup_failed = True
                if isinstance(failure, NativeLockError):
                    self._cleanup_report = failure.cleanup
                raise
            else:
                self._released = True
            finally:
                self._releasing = False
                for descriptor in (dir_fd, parent_fd):
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass

    def _release_checked(self, parent_fd: int, dir_fd: int) -> None:
        record_removed = False
        directory_removed = False

        def refuse() -> NoReturn:
            raise NativeLockError(
                "lock_cleanup_refused",
                cleanup=LockCleanupReport(
                    record_removed, directory_removed
                ),
            ) from None

        try:
            named = os.stat(LOCK_DIR_NAME, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            refuse()
        if (named.st_dev, named.st_ino) != self._dir_identity:
            refuse()
        if not stat.S_ISDIR(named.st_mode):
            refuse()
        try:
            if not is_private_descriptor_strict(dir_fd):
                refuse()
        except OSError:
            refuse()
        try:
            names = sorted(os.listdir(dir_fd))
        except OSError:
            refuse()
        if names != [LOCK_RECORD_NAME]:
            refuse()
        try:
            entry = os.stat(
                LOCK_RECORD_NAME, dir_fd=dir_fd, follow_symlinks=False
            )
        except OSError:
            refuse()
        if (entry.st_dev, entry.st_ino) != self._record_identity:
            refuse()
        if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
            refuse()
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(LOCK_RECORD_NAME, flags, dir_fd=dir_fd)
        except OSError:
            refuse()
        try:
            try:
                opened = _fstat(descriptor)
            except OSError:
                refuse()
            if (opened.st_dev, opened.st_ino) != self._record_identity:
                refuse()
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                refuse()
            try:
                if not is_private_descriptor_strict(descriptor):
                    refuse()
            except OSError:
                refuse()
            if opened.st_size != len(self._record_bytes):
                refuse()
            data = _read_bounded_fd(descriptor, MAX_LOCK_RECORD_BYTES)
            if data != self._record_bytes:
                refuse()
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.unlink(LOCK_RECORD_NAME, dir_fd=dir_fd)
        except OSError:
            refuse()
        record_removed = True
        try:
            named = os.stat(LOCK_DIR_NAME, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            refuse()
        if (named.st_dev, named.st_ino) != self._dir_identity:
            refuse()
        if not stat.S_ISDIR(named.st_mode):
            refuse()
        try:
            if os.listdir(dir_fd):
                refuse()
        except OSError:
            refuse()
        try:
            os.rmdir(LOCK_DIR_NAME, dir_fd=parent_fd)
        except OSError:
            refuse()
        directory_removed = True

    def __enter__(self) -> NativeRunLock:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            self.release()
        except NativeLockError as cleanup_error:
            if exc is not None:
                # Surface the cleanup refusal and keep the body's exception
                # visible as its cause instead of suppressing either one.
                raise cleanup_error from exc
            raise


@dataclass(frozen=True, slots=True)
class NativeLockStatus:
    """A read-only classification of the native-install lock. Never a refusal.

    ``state`` is ``absent`` (no lock, or no state directory), ``busy`` (a valid
    record names a run and pid), ``unknown`` (concurrent or interrupted, as
    acquisition classifies it) or ``unsafe`` (a foreign or unprivate shape). The
    run id, pid and liveness are reported only for ``busy``. ``pid_alive`` is
    ``None`` when liveness cannot be answered.
    """

    state: str
    run_id: str | None
    pid: int | None
    pid_alive: bool | None


_STATUS_BY_REASON: Final[dict[str, str]] = {
    "lock_busy": "busy",
    "lock_unknown": "unknown",
    "lock_unsafe": "unsafe",
}


def _read_record_bytes(dir_fd: int) -> bytes | None:
    try:
        descriptor = os.open(
            LOCK_RECORD_NAME,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            dir_fd=dir_fd,
        )
    except OSError:
        return None
    try:
        return _read_bounded_fd(descriptor, MAX_LOCK_RECORD_BYTES)
    finally:
        _close_quietly(descriptor)


def _read_lock_identity(parent_fd: int) -> tuple[str, int] | None:
    """The run id and pid a lock already proved ``busy`` records, or ``None``."""
    try:
        dir_fd = os.open(
            LOCK_DIR_NAME,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
    except OSError:
        return None
    try:
        data = _read_record_bytes(dir_fd)
    finally:
        _close_quietly(dir_fd)
    if data is None:
        return None
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    run_id = payload.get("run_id")
    pid = payload.get("pid")
    if type(run_id) is not str or type(pid) is not int:
        return None
    return run_id, pid


def _pid_alive(pid: int) -> bool | None:
    """Whether ``pid`` exists, by signal 0; ``None`` when that cannot be answered."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return None
    return True


def inspect_native_lock(state_dir: Path) -> NativeLockStatus:
    """Report the run lock under ``state_dir``; never create, repair, adopt or delete.

    The state directory is opened with the same private, descriptor-relative
    checks acquisition uses, and the lock is classified by the same reasons.
    A busy lock also reports its recorded run id and pid, and whether that pid
    is alive. Nothing is raised for any state of the filesystem: a directory
    that cannot be opened safely is ``unsafe``.
    """
    try:
        os.lstat(state_dir)
    except FileNotFoundError:
        return NativeLockStatus("absent", None, None, None)
    except (OSError, ValueError):
        return NativeLockStatus("unsafe", None, None, None)
    try:
        parent_fd = _open_existing_dir(state_dir, exact_private=True)
    except NativeLockError:
        return NativeLockStatus("unsafe", None, None, None)
    try:
        try:
            os.stat(LOCK_DIR_NAME, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return NativeLockStatus("absent", None, None, None)
        except OSError:
            return NativeLockStatus("unsafe", None, None, None)
        state = _STATUS_BY_REASON[_existing_lock_reason(parent_fd)]
        if state != "busy":
            return NativeLockStatus(state, None, None, None)
        identity = _read_lock_identity(parent_fd)
    finally:
        _close_quietly(parent_fd)
    if identity is None:
        return NativeLockStatus("unknown", None, None, None)
    run_id, pid = identity
    return NativeLockStatus("busy", run_id, pid, _pid_alive(pid))
