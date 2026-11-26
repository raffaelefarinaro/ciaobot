"""Read back the facts an import put on the Unsure list, for the import tests."""

from __future__ import annotations

import re
from pathlib import Path

QUEUE = "Workspace/Unsure.md"

_LINE = re.compile(
    r"^- \[[ xX]\] (?P<text>.*?) \(suggested destination: (?P<kind>\S+)(?: (?P<target>.*?))?\)"
    r" — unsure because [^;]*; source: (?P<source>[^;]+); \S+$"
)


def list_proposals(path: Path) -> list[dict[str, str]]:
    """The imported facts on the list at *path*, as rows of text, kind, target and source."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    rows: list[dict[str, str]] = []
    for line in lines:
        match = _LINE.match(line)
        if match is not None:
            rows.append(
                {
                    "text": match["text"],
                    "kind": match["kind"],
                    "target": match["target"] or "",
                    "source": match["source"],
                    "request": "",
                }
            )
    return rows
