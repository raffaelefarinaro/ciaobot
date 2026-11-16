"""The media type a file name maps to.

POSIX is ``mimetypes.guess_type`` exactly as the call sites used it before this
module existed: Python's own table plus the system's ``mime.types`` files.
"""

from __future__ import annotations

import mimetypes

def guess_type(name: str) -> str | None:
    """The media type for ``name`` (``mimetypes.guess_type``), or None."""
    return mimetypes.guess_type(name)[0]
