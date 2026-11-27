"""The small file kernel every durable vault write stands on.

Three things only, with no knowledge of proposals, receipts or any vault
format: a cross-process lock for one file (kept outside the vault), an atomic
whole-file replace, and the content revision a caller compares before it
overwrites. Writers take :func:`queue_lock` for the path, read, check the
revision, then :func:`write_queue_atomically`; a lock that cannot be taken raises
:class:`QueueLockError` and the write is not made.

The portable-name checks live here too: a vault is synced between machines, so
a file name has to be valid on every platform before anything creates it.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat as stat_module
import tempfile
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager, contextmanager, nullcontext
from pathlib import Path

from ciao.os_support.locks import lock_exclusive, unlock
from ciao.os_support.users import user_key


def content_revision(text: str) -> str:
    """The revision of one destination's text at a point in time."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


QUEUE_LOCK_TIMEOUT_S = 30.0
"""How long a managed queue write waits for the queue lock before failing.

Bounded like the guide lock so a wedged holder cannot pin a request forever,
and long enough that ordinary concurrency never trips it.
"""

# Queue locks live outside the vault. The lock sits beside the queue today only
# because it was simplest, but the vault is user-owned content: a stray
# ``*.lock`` there pollutes the tree and broke byte-identical round trips. Keep the lock file in a stable per-install
# directory keyed by the resolved queue path, so the vault stays untouched.
_QUEUE_LOCK_DIR_ENV = "CIAO_QUEUE_LOCK_DIR"


def lock_path_for(resolved_key: str) -> Path:
    """A lock file for a vault path, outside the vault it guards.

    Uses ``CIAO_QUEUE_LOCK_DIR`` when set (tests pin it), else a per-user
    directory under the system temp root. Deterministic in the resolved
    guarded path so every process and thread guarding the same file picks the
    same lock. The user component (the uid; the SID on Windows) keeps two local
    accounts from colliding on a shared ``/tmp``.

    Public so other lock users do not reinvent — or diverge from — where this
    install puts its lock files.
    """
    base = os.environ.get(_QUEUE_LOCK_DIR_ENV, "").strip()
    if base:
        root = Path(base)
    else:
        root = Path(tempfile.gettempdir()) / f"ciao-queue-locks-{user_key()}"
    digest = hashlib.sha256(resolved_key.encode("utf-8")).hexdigest()[:32]
    return root / f"{digest}.lock"


def _process_umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


# Read once, at import: reading the umask means setting it, which is not safe
# to do while other threads create files.
_DEFAULT_FILE_MODE = 0o666 & ~_process_umask()
_DIR_FD_WRITES = all(fn in os.supports_dir_fd for fn in (os.open, os.stat, os.rename, os.unlink))


class FolderChangedError(OSError):
    """The folder a write was staged in is no longer the folder at its path."""


def write_queue_atomically(
    path: Path,
    text: str,
    *,
    publish_guard: Callable[[], AbstractContextManager[object]] | None = None,
) -> None:
    """Replace a vault file via a fresh temp file and a rename in the same folder.

    In-place ``write_text`` truncates before it writes, so a partial write or an
    error mid-write can lose unrelated lines permanently. A temp file plus an
    atomic rename means a reader (or a crash) sees either the complete old file
    or the complete new one. Callers must hold :func:`queue_lock` for the path.

    Nothing on the way follows a link planted in the vault: the folder is opened
    with ``O_NOFOLLOW`` and everything after happens relative to that open
    folder; the temp file gets a fresh random name created with
    ``O_CREAT | O_EXCL | O_NOFOLLOW`` (mode 0600 while it is written); and just
    before the rename the folder at the path must still be the folder that was
    opened, so a folder swapped for a link meanwhile refuses the write
    (:class:`FolderChangedError`). The replaced file keeps its permission bits;
    a new file gets the process default. The data is fsynced before the rename.

    ``publish_guard``, when given, is entered after the new bytes are staged and
    wraps only the replacement itself. Raising from it refuses the write: the
    staged file is removed and the old file stays exactly as it was.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    temp_name = f".{path.name}.{secrets.token_hex(8)}.tmp"
    guard = publish_guard if publish_guard is not None else nullcontext
    if not _DIR_FD_WRITES:
        _write_by_path(path, temp_name, data, guard)
        return
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow)
    try:
        folder = os.fstat(dir_fd)
        mode = _existing_mode(lambda: os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False))
        fd = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=dir_fd,
        )
        staged = True
        try:
            try:
                _write_all(fd, data)
                os.fchmod(fd, mode)
                os.fsync(fd)
            finally:
                os.close(fd)
            with guard():
                now = os.stat(path.parent, follow_symlinks=False)
                if (now.st_dev, now.st_ino) != (folder.st_dev, folder.st_ino):
                    raise FolderChangedError(f"{path.parent} changed while {path.name} was written")
                os.rename(temp_name, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                staged = False
            try:
                os.fsync(dir_fd)
            except OSError:
                pass
        except BaseException:
            if staged:
                try:
                    os.unlink(temp_name, dir_fd=dir_fd)
                except OSError:
                    pass
            raise
    finally:
        os.close(dir_fd)


def _existing_mode(stat_call: Callable[[], os.stat_result]) -> int:
    try:
        info = stat_call()
    except FileNotFoundError:
        return _DEFAULT_FILE_MODE
    return stat_module.S_IMODE(info.st_mode) if stat_module.S_ISREG(info.st_mode) else _DEFAULT_FILE_MODE


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _write_by_path(
    path: Path,
    temp_name: str,
    data: bytes,
    guard: Callable[[], AbstractContextManager[object]],
) -> None:
    """Windows (no ``dir_fd``): the same steps by path, checking the folder with ``lstat``."""
    folder_path = path.parent
    folder = os.lstat(folder_path)
    if stat_module.S_ISLNK(folder.st_mode) or getattr(folder, "st_file_attributes", 0) & 0x400:
        raise FolderChangedError(f"{folder_path} is a link")
    mode = _existing_mode(lambda: os.lstat(path))
    temp = folder_path / temp_name
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    staged = True
    try:
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(temp, mode)
        with guard():
            now = os.lstat(folder_path)
            if (now.st_dev, now.st_ino) != (folder.st_dev, folder.st_ino):
                raise FolderChangedError(f"{folder_path} changed while {path.name} was written")
            os.replace(temp, path)
            staged = False
    except BaseException:
        if staged:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
        raise


# Re-entrancy depth per resolved queue path for the current thread. A wrapper
# (`queue_resolution`) holds the lock across a body that calls a writer
# (`remove_proposal_by_substring`) which also takes it; the inner acquire must
# not try to flock the same file again (that would deadlock against ourselves).
_QUEUE_LOCK_DEPTH = threading.local()


class QueueLockError(RuntimeError):
    """The proposal queue lock could not be acquired.

    Deliberately fatal to the write it guards, exactly like
    :class:`ciao.memory_tool.MemoryLockError`: a caller that swallowed this and
    wrote anyway would reintroduce the lost-update race the lock exists to
    prevent.
    """

    retryable = True


@contextmanager
def queue_lock(
    proposals_path: Path, *, timeout_s: float = QUEUE_LOCK_TIMEOUT_S
):
    """Serialize a read-check-replace on one proposal queue file.

    The undo path re-reads the queue, compares its revision against the
    receipt's after image, and only then replaces the file. Without a lock the
    check and the replace are not atomic: another writer can land in between,
    and the stale replacement silently discards that update. Every managed
    queue writer (``append_proposals``, ``remove_proposal_by_substring``, the
    PWA batch/sweep/single routes, and :func:`_undo_queue`) takes this same
    lock, so the revision check is meaningful across processes and threads.

    Re-entrant within one thread so a wrapper can hold it across a body that
    calls a writer which takes it again; the cross-process guard is the
    ``flock`` held by the outermost acquire.

    A lock that cannot be taken raises :class:`QueueLockError`; callers must let
    it propagate rather than fall through to an unlocked write.
    """
    import time

    try:
        key = str(proposals_path.resolve())
    except OSError:
        key = str(proposals_path)
    depths = getattr(_QUEUE_LOCK_DEPTH, "depths", None)
    if depths is None:
        depths = {}
        _QUEUE_LOCK_DEPTH.depths = depths
    if depths.get(key, 0) > 0:
        # Already held by this thread; the outermost acquire owns the flock.
        depths[key] += 1
        try:
            yield
        finally:
            depths[key] -= 1
        return

    lock_path = lock_path_for(key)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+", encoding="utf-8", newline="")
    except OSError as exc:
        raise QueueLockError(f"could not open queue lock {lock_path}: {exc}") from exc
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        try:
            lock_exclusive(handle.fileno(), blocking=False)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                raise QueueLockError(
                    f"queue lock {lock_path} is held; the write was not applied"
                )
            time.sleep(0.05)
        except OSError as exc:
            handle.close()
            raise QueueLockError(f"could not lock {lock_path}: {exc}") from exc
    depths[key] = 1
    try:
        yield
    finally:
        depths.pop(key, None)
        try:
            unlock(handle.fileno())
        except OSError:
            pass
        handle.close()


def safe_note_stem(name: str) -> str:
    """A name as a filename stem, without path separators."""
    cleaned = re.sub(r"[\\/:*?\"<>|]+", " ", name).strip().rstrip(".")
    return cleaned[:80]


WINDOWS_RESERVED_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)


def portable_name_problem(part: str) -> str | None:
    """Why *part* cannot be a file or folder name on every platform, or None.

    A vault is synced between machines, and ``NUL.md`` or ``a:b`` would be
    silently discarded or rejected where the note lands next.
    """
    if (
        any(ch in '<>:"|?*' for ch in part)
        or part.endswith((".", " "))
        or part.split(".")[0].strip().upper() in WINDOWS_RESERVED_STEMS
    ):
        return "is not a valid file name on every platform"
    return None
