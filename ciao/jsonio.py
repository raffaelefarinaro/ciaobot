"""Small JSON and secret-file I/O helpers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def read_json_dict(path: Path) -> dict[str, Any]:
    """Load a UTF-8 JSON file expected to hold an object, as a typed ``dict``.

    Centralizes the ``json.loads(path.read_text(encoding="utf-8"))`` pattern and
    its ``Any`` return so callers get a ``dict`` without repeating the
    annotation. Callers remain responsible for handling missing/invalid files
    (this propagates ``OSError`` / ``json.JSONDecodeError``).
    """
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def write_private_text(path: Path, text: str) -> None:
    """Write ``text`` so the file is owner-only for its whole lifetime.

    For files that hold secrets (OAuth credentials, provider keys, tokens),
    ``write_text`` would first create them with umask-default permissions and
    only tighten afterwards; this creates with 0600 directly, then chmods to
    repair a pre-existing file that was made looser by an older version.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o600)
