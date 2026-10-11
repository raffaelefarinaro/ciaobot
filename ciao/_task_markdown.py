"""Shared mechanics for splitting managed task-section list items."""

from __future__ import annotations

import re


def split_marked_items(
    section_inner: str, marker: re.Pattern[str]
) -> list[tuple[str, str]]:
    """Return ``(marker id or "", text)`` entries, preserving stray prose.

    Managed task sections use the same Markdown list boundary: a line starting
    with ``- `` begins an item, and following lines continue it until another
    such line. Each section supplies its own trailing marker expression, so
    its ID syntax and rendering remain local to that format.
    """
    items: list[tuple[str, str]] = []
    current: list[str] = []

    def finish(lines: list[str]) -> tuple[str, str]:
        text = "\n".join(lines).rstrip()
        if not lines or not lines[0].startswith("- "):
            return ("", text)
        found = marker.search(lines[0])
        return (found.group(1) if found is not None else "", text)

    for line in section_inner.splitlines():
        if line.startswith("- ") and current:
            items.append(finish(current))
            current = []
        current.append(line)
    if current:
        items.append(finish(current))
    return items
