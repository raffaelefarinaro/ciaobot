"""Direct writes to the Unsure list, `<vault>/Workspace/Unsure.md`.

The list is one plain Markdown file: a `# Unsure` heading and one task line per
item, `- [ ] [<kind>: ]<text> — unsure because <reason>; source: <where>; <date>`.
A person ticks a line to approve it or deletes it to reject it. The format and
what a ticked line means are in the stock memory guide
(`references/unsure-and-proposals.md`).

This module is the only engine code that writes it. Appends hold the file lock,
write atomically, skip a line the list already carries (compared on kind and
text, so a retry or a second run adds nothing) and never write credential-shaped
text. The Memory · Unsure page reads the list parsed (:func:`read_list`) and
ticks, unticks or removes one line (:func:`change_line`) under the same lock,
naming the line by a hash of its exact text so a page showing an older copy of
the list cannot act on a line that has changed since.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat as stat_module
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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


# ── The page's reads and single-line edits ────────────────────────────────────

# A real list is a few kilobytes; past this the page shows nothing and refuses
# to write, so one huge file cannot be read into memory or rewritten.
MAX_FILE_BYTES = 256 * 1024

_ITEM = re.compile(r"^(\s*[-*]\s*\[)([ xX])(\]\s+)(.*?)\s*$")
# A plain bullet under the heading: not something a care run acts on, but it is
# still on the list, so the page shows it rather than hiding it.
_BULLET = re.compile(r"^\s*[-*]\s+(?!\[[ xX]\]\s)(\S.*?)\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# The kinds the stock guide allows (unsure-and-proposals.md).
_KINDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("observation", re.compile(r"^observation:\s*", re.IGNORECASE)),
    ("drop", re.compile(r"^drop\s+(\S+?):\s*", re.IGNORECASE)),
    ("restore", re.compile(r"^restore to\s+(\S+?):\s*", re.IGNORECASE)),
    ("category", re.compile(r"^category:\s*", re.IGNORECASE)),
    ("rule", re.compile(r"^rule flip\s+(\S+?):\s*", re.IGNORECASE)),
    ("repo", re.compile(r"^repo\s+(.+?):\s+", re.IGNORECASE)),
)
# The tail every writer adds after the item: " — unsure because …" (" — removed
# because …" on a restore line).
_TAIL = re.compile(r"\s+—\s+(?=(?:unsure|removed) because\b)", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.?!](?=\s+[\"'“(A-Z0-9]|\s*$)")


class UnsureError(RuntimeError):
    """The list could not be read or changed; `status` is the HTTP answer."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


def line_id(line: str) -> str:
    """The id the page names a line by: a hash of its exact text."""
    return hashlib.sha256(line.encode("utf-8")).hexdigest()[:16]


def _title(text: str) -> str:
    """The first sentence of an item's text, or all of it when it has no end."""
    head = text.split(" — ", 1)[0].strip()
    match = _SENTENCE_END.search(head)
    return head[: match.end()].strip() if match else head


def parse_item(line: str) -> dict[str, Any]:
    """One line of the list as the page shows it. Never raises.

    A plain bullet, or a tick line with no text, is returned with
    `malformed: true` and its raw text, so it is shown, never dropped.
    """
    raw = line.rstrip("\r\n")
    item: dict[str, Any] = {
        "id": line_id(raw),
        "raw": raw.strip(),
        "checked": False,
        "kind": "",
        "qualifier": "",
        "title": "",
        "text": "",
        "reason": "",
        "source": "",
        "date": "",
        "malformed": False,
    }
    match = _ITEM.match(raw)
    if match is None:
        bullet = _BULLET.match(raw)
        body = bullet.group(1) if bullet else raw.strip()
        item.update({"malformed": True, "title": body, "text": body})
        return item
    item["checked"] = match.group(2) != " "
    body = match.group(4)
    parts = _TAIL.split(body, maxsplit=1)
    head = parts[0].strip()
    if len(parts) == 2:
        fields = [f.strip() for f in parts[1].split(";")]
        item["reason"] = re.sub(r"^(?:unsure|removed) because\s+", "", fields[0], flags=re.IGNORECASE)
        for field in fields[1:]:
            if field.lower().startswith("source:"):
                item["source"] = field.split(":", 1)[1].strip()
            elif _DATE.match(field):
                item["date"] = field
    for kind, pattern in _KINDS:
        kind_match = pattern.match(head)
        if kind_match is not None:
            item["kind"] = kind
            item["qualifier"] = kind_match.group(1) if kind_match.groups() else ""
            head = head[kind_match.end():]
            break
    item["text"] = head
    item["title"] = _title(head)
    if not item["title"]:
        item["malformed"] = True
        item["title"] = body
    return item


def parse_list(text: str) -> list[dict[str, Any]]:
    """Every item on the list in file order; headings, prose and fenced code are skipped."""
    items: list[dict[str, Any]] = []
    fenced = False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced or not line.strip():
            continue
        if _ITEM.match(line) or _BULLET.match(line):
            items.append(parse_item(line))
    return items


def _is_link(info: os.stat_result) -> bool:
    if stat_module.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


_NOT_PLAIN = "the list is not a plain file in the vault"


def _checked_read(vault_root: Path) -> str | None:
    """The list's text, or None when there is no list. Raises :class:`UnsureError`.

    Neither `Workspace` nor the file may be a link, the file must be one regular
    file (one hard link) under the size cap and inside the vault, and the file
    opened must be the one checked, so a swap in between is refused.
    """
    root = Path(vault_root).resolve()
    folder = root / Path(UNSURE_RELATIVE).parent
    path = unsure_path(root)
    try:
        folder_info = os.lstat(folder)
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if _is_link(folder_info) or not stat_module.S_ISDIR(folder_info.st_mode):
        raise UnsureError(_NOT_PLAIN, 409)
    if _is_link(info) or not stat_module.S_ISREG(info.st_mode) or info.st_nlink > 1:
        raise UnsureError(_NOT_PLAIN, 409)
    if not path.resolve().is_relative_to(root):
        raise UnsureError(_NOT_PLAIN, 409)
    if info.st_size > MAX_FILE_BYTES:
        raise UnsureError("the list is too large to show here", 413)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or not stat_module.S_ISREG(opened.st_mode):
            raise UnsureError(_NOT_PLAIN, 409)
        raw = os.read(fd, MAX_FILE_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_FILE_BYTES:
        raise UnsureError("the list is too large to show here", 413)
    return raw.decode("utf-8", errors="replace")


def _payload(text: str | None) -> dict[str, Any]:
    items = parse_list(text or "")
    ticked = sum(1 for i in items if i["checked"])
    return {
        "found": text is not None,
        "items": items,
        "counts": {"waiting": len(items) - ticked, "ticked": ticked},
    }


def read_list(vault_root: Path) -> dict[str, Any]:
    """The parsed list: `{found, items, counts: {waiting, ticked}}`. Raises :class:`UnsureError`."""
    return _payload(_checked_read(vault_root))


def change_line(vault_root: Path, op: str, target: str) -> dict[str, Any]:
    """Tick, untick or remove the line whose :func:`line_id` is `target`.

    Done under the list's file lock (the one :func:`append_unsure` takes) and
    written atomically, so a concurrent append is never lost. Raises
    :class:`UnsureError`: 409 when no line has that id any more (the list
    changed since the page read it), 400 for an unknown action or a plain bullet
    asked to be ticked. Returns the list as it is after the write.
    """
    if op not in ("tick", "untick", "remove"):
        raise UnsureError("unknown action", 400)
    path = unsure_path(Path(vault_root).resolve())
    with queue_lock(path):
        text = _checked_read(vault_root)
        if text is None:
            raise UnsureError("the list changed; reload it", 409)
        lines = text.splitlines(keepends=True)
        fenced = False
        index = -1
        for n, line in enumerate(lines):
            if _FENCE.match(line):
                fenced = not fenced
                continue
            if not fenced and line_id(line.rstrip("\r\n")) == target:
                index = n
                break
        if index < 0:
            raise UnsureError("the list changed; reload it", 409)
        line = lines[index]
        if op == "remove":
            del lines[index]
        else:
            match = _ITEM.match(line.rstrip("\r\n"))
            if match is None:
                raise UnsureError("this line has no tick box", 400)
            mark = "x" if op == "tick" else " "
            lines[index] = f"{match.group(1)}{mark}{line[match.end(2):]}"
        updated = "".join(lines)
        write_queue_atomically(path, updated)
    return _payload(updated)
