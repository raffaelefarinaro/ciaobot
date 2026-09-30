"""Opening files by descriptor with POSIX semantics on every OS.

``open_fd`` is ``os.open`` for the call sites that need a descriptor (a file
created with an explicit mode, ``O_EXCL``, or refusing to write through a
link). POSIX calls ``os.open`` exactly as those call sites did before this
module existed. Windows differs in two ways:

- ``os.open`` opens in text mode unless ``O_BINARY`` is given. The C runtime
  then rewrites every ``\\n`` byte to ``\\r\\n`` on write, even through
  ``os.fdopen(fd, "wb")``, so an uploaded image comes out corrupted. Every
  descriptor is opened ``O_BINARY``: bytes written are bytes stored, as on
  POSIX.
- There is no ``O_NOFOLLOW``. ``follow_symlinks=False`` checks the final
  component first and refuses a symbolic link or a junction with ``ELOOP``,
  the errno POSIX gives. Other reparse points (OneDrive placeholders, dedup)
  are ordinary files to the user and are opened. The check and the open are
  two steps, where POSIX has one; planting a link in between needs write
  access to the directory and, for a symlink, the privilege to create one.
"""

from __future__ import annotations

import os
import sys

if sys.platform == "win32":
    import errno
    import stat

    _LINK_TAGS = frozenset({stat.IO_REPARSE_TAG_SYMLINK, stat.IO_REPARSE_TAG_MOUNT_POINT})

    def _refuse_link(path: str | os.PathLike[str]) -> None:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return
        if st.st_reparse_tag in _LINK_TAGS:
            raise OSError(errno.ELOOP, "refusing to open a link", os.fspath(path))

    def open_fd(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        follow_symlinks: bool = True,
    ) -> int:
        """``os.open`` with POSIX semantics; see the module docstring."""
        if not follow_symlinks:
            _refuse_link(path)
        return os.open(path, flags | os.O_BINARY, mode)

else:

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
