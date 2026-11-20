"""Exclusive advisory file locks with ``flock`` semantics.

``lock_exclusive``/``unlock`` call ``fcntl.flock`` exactly as the call sites
did before this module existed; a non-blocking attempt on a held lock raises
:class:`BlockingIOError`.
"""

from __future__ import annotations

import fcntl

def lock_exclusive(fd: int, *, blocking: bool = True) -> None:
    """Take the exclusive lock on ``fd``; see the module docstring."""
    fcntl.flock(fd, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)

def unlock(fd: int) -> None:
    """Release the lock on ``fd``; a no-op when it is not held, like ``flock``."""
    fcntl.flock(fd, fcntl.LOCK_UN)
