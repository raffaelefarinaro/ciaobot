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


# Untrusted request bodies (the API, agent and webhook endpoints) nest no
# deeper than a settings or command object, a handful of levels at most; 64
# leaves an order of magnitude of headroom. The limit is explicit so it holds on
# every Python version instead of depending on when the interpreter's JSON
# parser runs out of stack (3.12 raises RecursionError, 3.14 does not).
MAX_JSON_DEPTH = 64

# One match per JSON string or per structural bracket, so a quoted bracket or an
# escaped quote never counts toward depth. An unterminated string runs to the
# end of the text: requiring the closing quote would make every later quote a
# fresh, failing match attempt, which is quadratic on `"\"\"\"...`.
_JSON_DEPTH_TOKEN = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*(?:"|\\?\Z)|[\[\]{}]', re.DOTALL)


def json_nesting_exceeds(data: bytes | str, limit: int = MAX_JSON_DEPTH) -> bool:
    """Whether ``data`` nests ``[``/``{`` deeper than ``limit`` outside strings.

    Runs in time linear in the length of ``data`` and needs it to be neither
    valid JSON nor valid text: bytes are decoded the way ``json.loads`` decodes
    them, with undecodable bytes replaced, so any body can be scanned.
    """
    text = data if isinstance(data, str) else data.decode(json.detect_encoding(data), "replace")
    depth = 0
    for match in _JSON_DEPTH_TOKEN.finditer(text):
        token = match.group()
        if token in "[{":
            depth += 1
            if depth > limit:
                return True
        elif token in "]}":
            depth -= 1
    return False


def loads_untrusted(data: bytes | str) -> Any:
    """``json.loads`` for a body from the network, refusing deep nesting first.

    Raises ``ValueError`` (``UnicodeDecodeError`` and ``json.JSONDecodeError``
    included) for anything that is not JSON or nests past ``MAX_JSON_DEPTH``.
    """
    if json_nesting_exceeds(data):
        raise ValueError(f"JSON nests deeper than {MAX_JSON_DEPTH} levels")
    return json.loads(data)


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
