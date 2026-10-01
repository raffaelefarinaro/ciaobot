"""Resolving a path to the same spelling on every OS.

POSIX is ``Path.resolve()`` exactly as the call sites used it before this
module existed.

On Windows, ``Path.resolve()`` (``ntpath.realpath``) asks the OS for the final
path, which comes back with the ``\\\\?\\`` extended-length prefix, and only
removes the prefix when the plain spelling still names an existing file at
the moment it checks. A directory another thread is creating at that instant
fails the check, so the same folder resolves to ``\\\\?\\C:\\...`` once and to
``C:\\...`` the next time. Comparing the two (the vault-root symlink guard in
``ciao.config``) then reports a symlink that is not there; two chats starting
at once was enough to hit it. :func:`resolve_path` removes a drive-letter
``\\\\?\\`` prefix, which names exactly the same path. A UNC
(``\\\\?\\UNC\\...``) result is left alone.
"""

from __future__ import annotations

import sys
from pathlib import Path

if sys.platform == "win32":
    _PREFIX = "\\\\?\\"

    def resolve_path(path: Path) -> Path:
        """``path.resolve()``, without the extended-length prefix on a drive path."""
        resolved = path.resolve()
        text = str(resolved)
        if text.startswith(_PREFIX) and not text[len(_PREFIX):].upper().startswith("UNC\\"):
            return Path(text[len(_PREFIX):])
        return resolved

else:

    def resolve_path(path: Path) -> Path:
        """``path.resolve()``."""
        return path.resolve()
