"""Small JSON I/O helpers."""

from __future__ import annotations

import json
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
