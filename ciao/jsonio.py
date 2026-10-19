"""Small JSON and secret-file I/O helpers."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from ciao.os_support.private import make_private, open_private


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
    fd = open_private(path, os.O_WRONLY | os.O_TRUNC)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    make_private(path)


def extract_json(text: str) -> dict | None:
    """Return the first JSON object in model text, ignoring fences and prose around it."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
    try:
        parsed: dict = json.loads(text)
        return parsed
    except Exception:
        pass
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                blob = text[start : i + 1]
                try:
                    parsed_blob: dict = json.loads(blob)
                    return parsed_blob
                except Exception:
                    return None
    return None
