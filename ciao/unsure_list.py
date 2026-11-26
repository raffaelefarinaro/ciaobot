"""Direct writes to the Unsure list, `<vault>/Workspace/Unsure.md`.

The list is one plain Markdown file: a `# Unsure` heading and one task line per
item, `- [ ] [<kind>: ]<text> — unsure because <reason>; source: <where>; <date>`.
A person ticks a line to approve it or deletes it to reject it. The format and
what a ticked line means are in the stock memory guide
(`references/unsure-and-proposals.md`).

This module is the only code that appends to it. It holds the file lock, writes
atomically, skips a line the list already carries (compared on kind and text, so
a retry or a second run adds nothing) and never writes credential-shaped text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ciao.file_kernel import queue_lock, write_queue_atomically
from ciao.redact import redact_text

UNSURE_RELATIVE = "Workspace/Unsure.md"
HEADING = "# Unsure"
MAX_TEXT_CHARS = 500

_LINE = re.compile(r"^\s*-\s*\[[ xX]\]\s+(.*?)\s*$")


@dataclass(frozen=True)
class UnsureItem:
    """One thing a writer could not settle."""

    text: str
    reason: str = ""
    source: str = ""
    kind: str = ""

    def as_bullet(self) -> str:
        """The line this item renders as today."""
        return render_line(self, today=datetime.now(UTC).date().isoformat())


def unsure_path(vault_root: Path) -> Path:
    return Path(vault_root) / UNSURE_RELATIVE


def _flat(value: str) -> str:
    return " ".join(value.split())


def _key_text(head: str) -> str:
    """The part of a line before its ` — ` tail, flattened and case-folded."""
    return _flat(head.split(" — ", 1)[0]).casefold()


def item_key(kind: str, text: str) -> str:
    """What two lines must share to count as the same item."""
    prefix = f"{_flat(kind)}: " if kind else ""
    return _key_text(prefix + _flat(text))


def render_line(item: UnsureItem, *, today: str) -> str:
    text = _flat(item.text)[:MAX_TEXT_CHARS]
    kind = _flat(item.kind)
    head = f"{kind}: {text}" if kind else text
    tail: list[str] = []
    if item.reason:
        tail.append(f"unsure because {_flat(item.reason)}")
    if item.source:
        tail.append(f"source: {_flat(item.source)}")
    tail.append(today)
    return f"- [ ] {head} — {'; '.join(tail)}"


def existing_keys(text: str) -> set[str]:
    keys: set[str] = set()
    for line in text.splitlines():
        match = _LINE.match(line)
        if match is not None:
            keys.add(_key_text(match.group(1)))
    return keys


def read_lines(vault_root: Path) -> dict[str, str]:
    """Every item line on the list as `{key: line}`; empty when there is no list."""
    try:
        text = unsure_path(vault_root).read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError):
        return {}
    rows: dict[str, str] = {}
    for line in text.splitlines():
        match = _LINE.match(line)
        if match is not None:
            rows.setdefault(_key_text(match.group(1)), line.strip())
    return rows


def read_keys(vault_root: Path) -> set[str]:
    """The keys of every line currently on the list; empty when there is no list."""
    try:
        return existing_keys(unsure_path(vault_root).read_text(encoding="utf-8"))
    except (FileNotFoundError, UnicodeDecodeError):
        return set()


def append_unsure(
    vault_root: Path, items: Iterable[UnsureItem], *, today: str | None = None
) -> list[str]:
    """Append the items the list does not already carry; return the lines written.

    Raises :class:`ciao.file_kernel.QueueLockError` or :class:`OSError` without
    writing when the file cannot be locked or replaced.
    """
    stamp = today or datetime.now(UTC).date().isoformat()
    path = unsure_path(vault_root)
    wanted: list[tuple[str, str]] = []
    for item in items:
        if not _flat(item.text):
            continue
        line = render_line(item, today=stamp)
        if redact_text(line) != line:
            continue
        wanted.append((item_key(item.kind, item.text), line))
    if not wanted:
        return []
    with queue_lock(path):
        try:
            current = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            current = ""
        seen = existing_keys(current)
        added: list[str] = []
        for key, line in wanted:
            if key in seen:
                continue
            seen.add(key)
            added.append(line)
        if not added:
            return []
        base = current if current.strip() else HEADING + "\n\n"
        if not base.endswith("\n"):
            base += "\n"
        write_queue_atomically(path, base + "\n".join(added) + "\n")
    return added
