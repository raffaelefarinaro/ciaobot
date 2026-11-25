"""Opening files by descriptor, and replacing them atomically.

``open_fd`` is ``os.open`` for the call sites that need a descriptor (a file
created with an explicit mode, ``O_EXCL``, or refusing to write through a
link). ``follow_symlinks=False`` adds ``O_NOFOLLOW``.

The descriptor helpers below are the only place the curation log writer
touches platform calls (effective ids, ``fchown``/``fchmod``, ``fsync``, the
Linux POSIX ACL xattr). Unsupported calls answer "none" where that is the
correct reading and raise otherwise; nothing here substitutes a weaker path.
"""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import struct
import sys
from pathlib import Path

# Linux POSIX ACL tags that name a user or group other than the file's owner
# and group, which the mode bits cannot express.
_ACL_USER = 0x02
_ACL_GROUP = 0x08
_ACL_XATTR = "system.posix_acl_access"
_ACL_NO_XATTR_ERRNOS = frozenset(
    code
    for code in (
        getattr(errno, "ENODATA", None),
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
        getattr(errno, "ENOSYS", None),
    )
    if code is not None
)


def open_fd(
    path: str | os.PathLike[str],
    flags: int,
    mode: int = 0o777,
    *,
    follow_symlinks: bool = True,
) -> int:
    """``os.open`` with POSIX semantics; see the module docstring."""
    if not follow_symlinks:
        flags |= os.O_NOFOLLOW
    return os.open(path, flags, mode)


# POSIX rename(2) is atomic against a concurrent rename onto the same target.


def replace_file(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
    """``os.replace``: atomic on POSIX even against a concurrent replace."""
    os.replace(src, dst)





def read_connected_note(root: Path, relative: Path) -> str:
    """Read a regular note without following a swapped directory or file symlink."""
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("expected a note path relative to the brain")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    directory = os.open("/", flags)
    try:
        for part in (*root.absolute().parts[1:], *relative.parts[:-1]):
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("connected note is not a regular file")
            with open(fd, encoding="utf-8", closefd=False) as stream:
                return stream.read()
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def publish_new_from_fd(source_fd: int, directory_fd: int, name: str) -> None:
    """Publish the open inode at an absent name without resolving its staging name."""
    if sys.platform == "darwin":
        clone = ctypes.CDLL(None, use_errno=True).fclonefileat
        clone.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        clone.restype = ctypes.c_int
        if clone(source_fd, directory_fd, os.fsencode(name), 0):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), name)
    elif sys.platform.startswith("linux"):
        # Supplying dst_dir_fd selects linkat(AT_SYMLINK_FOLLOW). procfs resolves
        # this magic link to the open file, even if its old name was rebound.
        os.link(f"/proc/self/fd/{source_fd}", name, dst_dir_fd=directory_fd,
                follow_symlinks=True)
    else:
        raise OSError(errno.ENOTSUP, "descriptor-based publication is unavailable")


def effective_uid() -> int:
    """The effective user id that owns files this process may publish."""
    return os.geteuid()


def effective_gid() -> int:
    """The effective group id a published file defaults to."""
    return os.getegid()


def write_all(fd: int, data: bytes) -> None:
    """Write every byte of *data* to *fd*, raising ``OSError`` on a short failure."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def flush_descriptor(fd: int) -> None:
    """Flush *fd*'s data to stable storage."""
    os.fsync(fd)


def chown_group(fd: int, gid: int) -> None:
    """Give the open file *gid* as its group, leaving the owner unchanged."""
    os.fchown(fd, -1, gid)


def chmod_descriptor(fd: int, mode: int) -> None:
    """Set the open file's permission bits exactly."""
    os.fchmod(fd, mode)


def descriptor_posix_acl_blob(fd: int) -> bytes | None:
    """The Linux POSIX access ACL of *fd*, or ``None`` when it has none.

    ``None`` covers a platform without the xattr interface and a filesystem
    that declares no ACL support. Any other error propagates.
    """
    getxattr = getattr(os, "getxattr", None)
    if getxattr is None:
        return None
    try:
        blob: bytes = getxattr(fd, _ACL_XATTR)
        return blob
    except OSError as exc:
        if exc.errno in _ACL_NO_XATTR_ERRNOS:
            return None
        raise


def acl_blob_names_other_principals(blob: bytes) -> bool:
    """Whether a POSIX ACL blob carries a named user or group entry.

    The base owner/group/other entries are what the mode bits already say, so
    they are harmless. A blob that does not parse as version 2 entries is
    treated as naming someone: the caller refuses rather than guess.
    """
    if len(blob) < 4 or (len(blob) - 4) % 8:
        return True
    for offset in range(4, len(blob), 8):
        (tag,) = struct.unpack_from("<H", blob, offset)
        if tag in (_ACL_USER, _ACL_GROUP):
            return True
    return False
