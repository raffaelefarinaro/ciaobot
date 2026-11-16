"""Resolving a path.

POSIX is ``Path.resolve()`` exactly as the call sites used it before this
module existed.
"""

from __future__ import annotations

from pathlib import Path

def resolve_path(path: Path) -> Path:
    """``path.resolve()``."""
    return path.resolve()
