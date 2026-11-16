"""Opening files by descriptor, and replacing them atomically.

``open_fd`` is ``os.open`` for the call sites that need a descriptor (a file
created with an explicit mode, ``O_EXCL``, or refusing to write through a
link). ``follow_symlinks=False`` adds ``O_NOFOLLOW``.
"""

from __future__ import annotations

import os

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
