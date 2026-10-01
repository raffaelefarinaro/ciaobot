"""The media type a file name maps to.

POSIX is ``mimetypes.guess_type`` exactly as the call sites used it before this
module existed: Python's own table plus the system's ``mime.types`` files.

On Windows the module-level ``mimetypes`` reads extra types out of the registry
on first use, and whatever an installed program registered wins: a ``.zip`` came
back ``application/x-zip-compressed`` on one machine and something else on the
next, so what the engine served depended on what else was installed. A private
``mimetypes.MimeTypes()`` holds Python's built-in table and never reads the
registry, so every Windows machine answers the same, as POSIX does from its
standard files.
"""

from __future__ import annotations

import mimetypes
import sys

if sys.platform == "win32":
    _TABLE = mimetypes.MimeTypes()

    def guess_type(name: str) -> str | None:
        """The media type for ``name`` from Python's own table, or None."""
        return _TABLE.guess_type(name)[0]

else:

    def guess_type(name: str) -> str | None:
        """The media type for ``name`` (``mimetypes.guess_type``), or None."""
        return mimetypes.guess_type(name)[0]
