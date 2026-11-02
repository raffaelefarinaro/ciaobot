"""Split and write YAML frontmatter without hand-rolled string handling."""

from __future__ import annotations

import json
import re

import yaml

_FENCE_RE = re.compile(
    r"\A﻿?---[ \t]*\r?\n(.*?)(?:\r?\n)?^---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL | re.MULTILINE,
)

_NEEDS_ESCAPE_RE = re.compile("[\x00-\x1f\x7f-\x9f]")


def split_frontmatter(text: str) -> tuple[str, str] | None:
    """Return (yaml_block, body) when *text* opens with a fenced block, else None.

    The closing fence is a line that is exactly `---` (trailing spaces allowed),
    so `---` inside a value never ends the block.
    """
    m = _FENCE_RE.match(text)
    if m is None:
        return None
    return m.group(1), text[m.end():]


def yaml_scalar(value: object) -> str:
    """One-line YAML for a scalar, quoted only when YAML needs it."""
    if isinstance(value, str) and _NEEDS_ESCAPE_RE.search(value):
        # C0 controls fold into a multi-line quoted scalar, and C1 controls such
        # as U+0085 are YAML line breaks that safe_dump emits raw. JSON's
        # double-quoted form is valid YAML, always one line, and escapes both.
        return json.dumps(value, ensure_ascii=True)
    dumped: str = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True, width=1_000_000)
    return dumped.removesuffix("\n...\n").rstrip("\n")


def unquote_scalar(raw: str) -> str:
    """Decode a one-line YAML scalar a hand-rolled reader pulled off a `key:` line."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        try:
            value = yaml.safe_load(raw)
        except yaml.YAMLError:
            return raw[1:-1]
        if isinstance(value, str):
            return value
    return raw
