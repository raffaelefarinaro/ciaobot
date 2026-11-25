"""Descriptor-relative, no-clobber writes into an ordinary linked folder.

A folder shared brain is a user's own directory, including one a cloud client
syncs. A write into it must never follow a symlink out of that directory, must
never replace or delete a file another writer put there, and must be provable
after a crash. Every operation here therefore works from an open directory
descriptor and a recorded ``(st_dev, st_ino)`` chain rather than from a
pathname that another process can swap between validation and use.

A note is staged in a private ``0o700`` directory in trusted storage outside
every linked/synced tree: engine state when it is already on the destination
volume, otherwise a per-volume staging directory at the mount root. The
directory is created with ``mkdir`` and opened with
``O_NOFOLLOW|O_DIRECTORY``. The staged file is written through that descriptor,
tagged with a random operation identity in an extended attribute, and
published with one ``link(2)`` from the descriptor, which fails rather than
replacing an existing destination. Cleanup removes a source only when its
``(st_dev, st_ino)`` still matches the one this call wrote, and removes the
staging directory only with ``rmdir`` (which cannot remove a file). The shared
tree is never used for staging or cleanup, and the destination is never
unlinked, renamed or replaced.

An extended attribute is required because a device/inode pair can be reused
after a delete/recreate, and matching bytes alone are not proof. A filesystem
that cannot carry the tag raises :class:`UnsupportedFilesystem`; the caller
refuses the publication rather than making a weaker claim.

``open_chain``/``revalidate_chain`` compare the opened directory chain with the
reviewed chain. ``verified_rename_observability`` probes the same filesystem
for ancestor metadata changes on a private rename; publishing refuses with
manual export when the probe fails. It then snapshots ``ancestor_stamps`` for
every directory above the note parent up to the filesystem root (refusing a
chain that cannot reach it), revalidates the visible chain, links, and compares
the stamps again, so a parent, the linked root or any higher ancestor moved
away and back inside the link is reported as indeterminate instead of being
treated as a successful write.

The enforced assumption is a non-hostile local caller. The private engine
directory and random token are not a separate security principal; a same-UID
process that deliberately enters the staging directory is outside this
module's guarantee. Staging lives outside every linked/synced tree, so an
ordinary cloud client or external writer does not see it. Ancestor move
detection relies on POSIX rename updating directory ``ctime``/``mtime`` and
link count; a backend that does not is unsupported for that guarantee.
"""

from __future__ import annotations

import ctypes
import errno
import os
import secrets
import stat
import sys
from dataclasses import dataclass

from ciao.os_support.files import acl_blob_names_other_principals, descriptor_posix_acl_blob
from ciao.os_support.private import is_private_descriptor_strict

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_OPERATION_ATTR = "com.ciaobot.share" if sys.platform == "darwin" else "user.ciaobot.share"
_UNSUPPORTED_ERRNOS = frozenset(
    code for code in (
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
        getattr(errno, "ENOSYS", None),
    ) if code is not None
)
_MISSING_XATTR_ERRNOS = frozenset(
    code for code in (
        getattr(errno, "ENODATA", None),
        getattr(errno, "ENOATTR", None),
    ) if code is not None
)


@dataclass(frozen=True, slots=True)
class DirectoryIdentity:
    """One directory in the reviewed path chain, as the kernel reports it."""

    dev: int
    ino: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> DirectoryIdentity:
        return cls(dev=value.st_dev, ino=value.st_ino)

    def matches(self, value: os.stat_result) -> bool:
        return value.st_dev == self.dev and value.st_ino == self.ino


class FolderChanged(OSError):
    """The reviewed folder, or a directory on the way to the note, changed."""


class UnsupportedFilesystem(OSError):
    """The volume cannot carry the operation tag that proves this write."""


def _directory_flags() -> int:
    return os.O_RDONLY | _DIRECTORY | _NOFOLLOW


def chain_matches(
    expected: list[DirectoryIdentity], actual: list[DirectoryIdentity],
) -> bool:
    return len(expected) == len(actual) and all(
        want.dev == have.dev and want.ino == have.ino
        for want, have in zip(expected, actual)
    )


def open_root(path: str) -> int:
    """Open ``path`` as a directory, refusing a link in the final component."""
    try:
        fd = os.open(path, _directory_flags())
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise FolderChanged(errno.ELOOP, "the linked folder is a link or not a directory") from exc
        raise
    try:
        opened = os.fstat(fd)
        if not stat.S_ISDIR(opened.st_mode):
            raise FolderChanged(errno.ENOTDIR, "the linked folder is not a directory")
        try:
            named = os.stat(path, follow_symlinks=False)
        except OSError as exc:
            raise FolderChanged(errno.ELOOP, "the linked folder changed while opening") from exc
        if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
            raise FolderChanged(errno.ELOOP, "the linked folder changed while opening")
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_chain(root_path: str, parts: tuple[str, ...]) -> tuple[int, list[DirectoryIdentity]]:
    """Open every directory before the final note name and return the parent.

    ``parts`` is the already-validated relative path; the last element is the
    note file name, so only its ancestors are opened as directories. The
    returned chain starts with the linked root and ends with the note's parent.
    The caller owns the returned descriptor.
    """
    if not parts:
        raise ValueError("a note path needs at least one component")
    root_fd = open_root(root_path)
    parent = root_fd
    try:
        chain = [DirectoryIdentity.from_stat(os.fstat(parent))]
        for part in parts[:-1]:
            try:
                child = os.open(part, _directory_flags(), dir_fd=parent)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise FolderChanged(
                        errno.ELOOP, "a path component is a link or not a directory"
                    ) from exc
                raise
            try:
                identity = DirectoryIdentity.from_stat(os.fstat(child))
                chain.append(identity)
            except BaseException:
                os.close(child)
                raise
            os.close(parent)
            parent = child
        return parent, chain
    except BaseException:
        os.close(parent)
        raise


def revalidate_chain(
    root_path: str, parts: tuple[str, ...], expected: list[DirectoryIdentity],
) -> None:
    """Re-open the visible path and require the same directory chain.

    A caller that already holds a descriptor uses this immediately before and
    after a mutation. A rename of the root or a parent makes the visible path
    resolve to a different inode, so the held descriptor no longer describes
    the approved destination.
    """
    try:
        parent_fd, chain = open_chain(root_path, parts)
    except FileNotFoundError as exc:
        raise FolderChanged(errno.ENOENT, "the approved folder path disappeared") from exc
    try:
        if not chain_matches(expected, chain):
            raise FolderChanged(errno.ELOOP, "the approved folder path changed")
    finally:
        os.close(parent_fd)


def ancestor_stamps(parent_fd: int, max_levels: int = 512) -> tuple[tuple[int, int, int], ...]:
    """Metadata of every directory above the note parent, up to the root.

    A rename of the note parent, the linked root, or any higher ancestor changes
    the ``ctime``/``mtime``/link count of the directory that held it.
    Snapshotting the whole ancestor chain immediately before and after the link
    therefore detects a move-away/back anywhere above the destination. The link
    itself changes only the note parent, which is deliberately not part of these
    stamps. A chain that cannot reach the filesystem root within ``max_levels``
    is refused rather than silently truncated.
    """
    stamps: list[tuple[int, int, int]] = []
    seen: set[tuple[int, int]] = set()
    current = os.dup(parent_fd)
    reached_root = False
    try:
        for _ in range(max_levels):
            upper = os.open("..", _directory_flags(), dir_fd=current)
            opened = os.fstat(upper)
            key = (opened.st_dev, opened.st_ino)
            if key in seen:
                os.close(upper)
                reached_root = True
                break
            seen.add(key)
            stamps.append((opened.st_ctime_ns, opened.st_mtime_ns, opened.st_nlink))
            os.close(current)
            current = upper
        if not reached_root:
            raise OSError(errno.ELOOP, "the ancestor chain did not reach the filesystem root")
        return tuple(stamps)
    finally:
        os.close(current)


def open_directory_nofollow(path: str) -> int:
    """Open a directory without following the final component, checking identity."""
    try:
        fd = os.open(path, _directory_flags())
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise FolderChanged(errno.ELOOP, "the directory is a link or not a directory") from exc
        raise
    try:
        opened = os.fstat(fd)
        if not stat.S_ISDIR(opened.st_mode):
            raise FolderChanged(errno.ENOTDIR, "the path is not a directory")
        named = os.stat(path, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
            raise FolderChanged(errno.ELOOP, "the directory changed while opening")
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_or_create_private_dir(
    parent_fd: int, name: str,
) -> tuple[int, DirectoryIdentity]:
    """Create a private directory only if absent; never chmod an existing one.

    The final component is opened with ``O_NOFOLLOW`` and checked against the
    named inode. A just-created directory is owned by this process, so its mode
    may be tightened through the descriptor. An existing directory must already
    be owned by this user, mode 0700 and free of an external ACL grant; it is
    refused without modification otherwise.
    """
    if not name or "/" in name or name in {".", ".."}:
        raise ValueError("a staging directory name must be one component")
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    # No chmod follows: a directory that replaced the one created above must
    # never be modified. 0700 passed to mkdir is only narrowed by umask, so a
    # genuinely new directory is already private; anything else is refused.
    fd = os.open(name, _directory_flags(), dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise FolderChanged(errno.ELOOP, "the staging directory changed while opening")
        if not is_private_descriptor_strict(fd):
            raise UnsupportedFilesystem(
                errno.EACCES,
                "the staging directory is not owner-only or grants an external ACL",
            )
        if sys.platform != "darwin":
            acl = descriptor_posix_acl_blob(fd)
            if acl is not None and acl_blob_names_other_principals(acl):
                raise UnsupportedFilesystem(
                    errno.EACCES,
                    "the staging directory's POSIX ACL grants another principal",
                )
        return fd, DirectoryIdentity.from_stat(opened)
    except BaseException:
        os.close(fd)
        raise


def verified_hardlink_support(staging_root: str) -> bool:
    """Probe whether this filesystem supports the hard link publication step."""
    root_fd = os.open(staging_root, _directory_flags())
    first = f".ciao-link-probe-{secrets.token_hex(12)}"
    second = first + "-b"
    try:
        fd = os.open(
            first, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600,
            dir_fd=root_fd,
        )
        os.close(fd)
        try:
            os.link(first, second, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            return True
        except OSError:
            return False
    except OSError:
        return False
    finally:
        for name in (second, first):
            try:
                os.unlink(name, dir_fd=root_fd)
            except OSError:
                pass
        os.close(root_fd)


def verified_rename_observability(staging_root: str) -> bool:
    """Probe whether this volume updates ancestor metadata on a rename.

    The move-away/back guard compares the ctime/mtime/link-count stamps of the
    directories above the note parent. That is only a proof when a rename
    actually updates those stamps. The probe runs entirely in engine staging
    on the same filesystem: it creates a private child directory, records the
    parent stamps, renames the child in place, and requires the stamps to
    change. A volume that fails the probe is unsupported for the guard and the
    caller must fail closed or offer manual export.
    """
    root_fd = os.open(staging_root, _directory_flags())
    first = f".ciao-rename-probe-{secrets.token_hex(12)}"
    second = first + "-b"
    try:
        os.mkdir(first, 0o700, dir_fd=root_fd)
        before_stat = os.fstat(root_fd)
        before = (before_stat.st_ctime_ns, before_stat.st_mtime_ns, before_stat.st_nlink)
        try:
            os.rename(first, second, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        except OSError:
            return False
        after_stat = os.fstat(root_fd)
        after = (after_stat.st_ctime_ns, after_stat.st_mtime_ns, after_stat.st_nlink)
        return after != before
    except OSError:
        return False
    finally:
        for name in (second, first):
            try:
                os.rmdir(name, dir_fd=root_fd)
            except OSError:
                pass
        os.close(root_fd)


def stat_entry(parent_fd: int, name: str) -> os.stat_result | None:
    """``lstat`` one name relative to ``parent_fd``; ``None`` when absent."""
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def create_staging_dir(
    staging_root: str, name: str,
) -> tuple[int, DirectoryIdentity]:
    """Create and open a private staging directory inside engine state."""
    root_fd = os.open(staging_root, _directory_flags())
    try:
        os.mkdir(name, 0o700, dir_fd=root_fd)
        try:
            fd = os.open(name, _directory_flags(), dir_fd=root_fd)
        except BaseException:
            try:
                os.rmdir(name, dir_fd=root_fd)
            except OSError:
                pass
            raise
    finally:
        os.close(root_fd)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISDIR(opened.st_mode):
            raise FolderChanged(errno.ENOTDIR, "the staging directory is not a directory")
        return fd, DirectoryIdentity.from_stat(opened)
    except BaseException:
        os.close(fd)
        raise


def open_staging_dir(
    staging_root: str, name: str, *, expected_dev: int, expected_ino: int,
) -> int:
    """Open a recorded staging directory only when it is still the same inode."""
    root_fd = os.open(staging_root, _directory_flags())
    try:
        fd = os.open(name, _directory_flags(), dir_fd=root_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise FolderChanged(errno.ELOOP, "the staging directory changed") from exc
        raise
    finally:
        os.close(root_fd)
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_dev != expected_dev
            or opened.st_ino != expected_ino
        ):
            raise FolderChanged(errno.ELOOP, "the staging directory changed")
        return fd
    except BaseException:
        os.close(fd)
        raise

def create_staged_file(
    staging_fd: int, name: str, data: bytes, token: str,
) -> tuple[int, int]:
    """Create, tag, write and fsync one file inside an already-open staging dir."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW
    fd = os.open(name, flags, 0o644, dir_fd=staging_fd)
    failure: BaseException | None = None
    identity: tuple[int, int] | None = None
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(errno.EINVAL, "the staged note is not a regular file")
        identity = (opened.st_dev, opened.st_ino)
        set_operation_tag(fd, token)
        os.fsync(fd)
        return identity
    except BaseException as exc:
        failure = exc
        raise
    finally:
        os.close(fd)
        if failure is not None:
            # Remove only the inode this call created, and only from inside
            # the private staging directory. Never touch the destination.
            _discard_named_file(staging_fd, name, identity, token)


def _discard_named_file(
    staging_fd: int, name: str, identity: tuple[int, int] | None,
    token: str | None = None,
) -> None:
    if identity is None:
        return
    try:
        current = os.stat(name, dir_fd=staging_fd, follow_symlinks=False)
    except OSError:
        return
    if (
        not stat.S_ISREG(current.st_mode)
        or (current.st_dev, current.st_ino) != identity
    ):
        return
    if token is not None:
        # Inode numbers are reusable (notably after unlink on Linux). The
        # operation tag is the durable identity of the file this call wrote.
        try:
            fd = open_regular(staging_fd, name)
        except OSError:
            return
        try:
            if read_operation_tag(fd) != token:
                return
        except (OSError, UnsupportedFilesystem):
            return
        finally:
            os.close(fd)
    try:
        os.unlink(name, dir_fd=staging_fd)
    except OSError:
        pass


def same_mount(staging_fd: int, parent_fd: int) -> bool:
    """Whether staging and the destination are on one filesystem (hardlinkable)."""
    return os.fstat(staging_fd).st_dev == os.fstat(parent_fd).st_dev


def link_no_clobber(
    staging_fd: int, source_name: str, parent_fd: int, final_name: str,
    *, expected_dev: int, expected_ino: int, token: str,
) -> None:
    """Atomically publish a staged inode without replacing anything.

    Staging must be on the same filesystem as the destination, so this is one
    hard link rather than a copy. The source name is re-checked immediately
    before ``link(2)`` and must name the exact inode the caller wrote.
    ``link(2)`` itself fails with ``EEXIST`` when the destination exists,
    including a symlink or a hard link to another file, so a concurrent
    external writer always wins and its bytes survive.
    """
    if not same_mount(staging_fd, parent_fd):
        raise UnsupportedFilesystem(
            errno.EXDEV, "staging and destination are on different filesystems"
        )
    try:
        source_fd = open_regular(staging_fd, source_name)
    except OSError as exc:
        raise FolderChanged(errno.ELOOP, "the staged note changed before linking") from exc
    try:
        current = os.fstat(source_fd)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_dev != expected_dev
            or current.st_ino != expected_ino
            or read_operation_tag(source_fd) != token
        ):
            raise FolderChanged(errno.ELOOP, "the staged note changed before linking")
    finally:
        os.close(source_fd)
    os.link(
        source_name, final_name,
        src_dir_fd=staging_fd, dst_dir_fd=parent_fd, follow_symlinks=False,
    )


def discard_staging(
    staging_fd: int, staging_name: str, staging_root: str,
    *, expected_dev: int | None = None, expected_ino: int | None = None,
    token: str | None = None,
) -> None:
    """Best-effort non-destructive cleanup of engine-owned staging.

    A source file is removed only when its inode still matches the one this
    operation wrote. The directory is then removed with ``rmdir``, which cannot
    delete a file and fails on a non-empty or replaced directory. Nothing in
    the shared folder is ever unlinked, renamed or removed by this helper.
    """
    if isinstance(expected_dev, int) and isinstance(expected_ino, int):
        _discard_named_file(staging_fd, "note", (expected_dev, expected_ino), token)
    try:
        root_fd = os.open(staging_root, _directory_flags())
    except OSError:
        return
    try:
        os.rmdir(staging_name, dir_fd=root_fd)
    except OSError:
        pass
    finally:
        os.close(root_fd)

def fsync_directory(fd: int) -> None:
    """Flush a directory entry change to stable storage."""
    os.fsync(fd)


def open_regular(parent_fd: int, name: str) -> int:
    """Open one existing regular file with no-follow, or raise."""
    fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError(errno.EINVAL, "the destination is not a regular file")
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_all(fd: int, limit: int) -> bytes:
    """Read at most ``limit`` bytes from an already-open file descriptor."""
    chunks: list[bytes] = []
    remaining = limit
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


if sys.platform == "darwin":
    _libc = ctypes.CDLL(None, use_errno=True)
    _fsetxattr = _libc.fsetxattr
    _fsetxattr.argtypes = [
        ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int,
    ]
    _fsetxattr.restype = ctypes.c_int
    _fgetxattr = _libc.fgetxattr
    _fgetxattr.argtypes = [
        ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int,
    ]
    _fgetxattr.restype = ctypes.c_ssize_t

    def _xattr_error(exc: OSError) -> OSError:
        if exc.errno in _UNSUPPORTED_ERRNOS:
            return UnsupportedFilesystem(exc.errno, "this volume cannot carry an operation tag")
        return exc

    def set_operation_tag(fd: int, token: str) -> None:
        value = token.encode("utf-8")
        if _fsetxattr(
            fd, _OPERATION_ATTR.encode("utf-8"), ctypes.c_char_p(value),
            len(value), 0, 0,
        ) != 0:
            error = ctypes.get_errno()
            raise _xattr_error(OSError(error, os.strerror(error)))

    def read_operation_tag(fd: int, limit: int = 256) -> str | None:
        buffer = ctypes.create_string_buffer(limit)
        size = _fgetxattr(fd, _OPERATION_ATTR.encode("utf-8"), buffer, limit, 0, 0)
        if size < 0:
            error = ctypes.get_errno()
            if error in _MISSING_XATTR_ERRNOS:
                return None
            raise _xattr_error(OSError(error, os.strerror(error)))
        if size > limit:
            raise UnsupportedFilesystem(errno.EOVERFLOW, "the operation tag is too large")
        try:
            return buffer.raw[:size].decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise UnsupportedFilesystem(errno.EINVAL, "the operation tag is not UTF-8") from exc

else:
    def set_operation_tag(fd: int, token: str) -> None:
        setxattr = getattr(os, "setxattr", None)
        if setxattr is None:
            raise UnsupportedFilesystem(errno.ENOTSUP, "this build cannot write an operation tag")
        try:
            setxattr(fd, _OPERATION_ATTR, token.encode("utf-8"))
        except OSError as exc:
            if exc.errno in _UNSUPPORTED_ERRNOS:
                raise UnsupportedFilesystem(exc.errno, "this volume cannot carry an operation tag") from exc
            raise

    def read_operation_tag(fd: int, limit: int = 256) -> str | None:
        getxattr = getattr(os, "getxattr", None)
        if getxattr is None:
            raise UnsupportedFilesystem(errno.ENOTSUP, "this build cannot read an operation tag")
        try:
            value: bytes = getxattr(fd, _OPERATION_ATTR)
        except OSError as exc:
            if exc.errno in _MISSING_XATTR_ERRNOS:
                return None
            if exc.errno in _UNSUPPORTED_ERRNOS:
                raise UnsupportedFilesystem(exc.errno, "this volume cannot carry an operation tag") from exc
            raise
        if len(value) > limit:
            raise UnsupportedFilesystem(errno.EOVERFLOW, "the operation tag is too large")
        try:
            return value.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise UnsupportedFilesystem(errno.EINVAL, "the operation tag is not UTF-8") from exc
