"""Direct writes to the Unsure list, `<vault>/Workspace/Unsure.md`.

The list is one plain Markdown file: a `# Unsure` heading and one task line per
item, `- [ ] [<kind>: ]<text> — unsure because <reason>; [to: <destination>; ]source: <where>; <date>`.
A person ticks a line to approve it or deletes it to reject it. The format and
what a ticked line means are in the stock memory guide
(`references/unsure-and-proposals.md`).

This module is the only engine code that writes it. Appends hold the file lock,
write atomically, skip a line the list already carries (compared on kind and
text, so a retry or a second run adds nothing) and never write credential-shaped
text. The Memory · Unsure page reads the list parsed (:func:`read_list`) and
ticks, unticks or removes one line (:func:`change_line`) under the same lock,
naming the line by a hash of its exact text so a page showing an older copy of
the list cannot act on a line that has changed since. It can also put back one
unticked line back, the one the person just removed (:func:`restore_line`, the page's Undo).
"""

from __future__ import annotations

import hashlib
import os
import re
import stat as stat_module
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ciao.care_report import vault_relative_path
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
    destination: str = ""

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
    if item.destination:
        tail.append(f"to: {_flat(item.destination)}")
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
# The page's writes wait this long for the lock, not the 30 s a care run's
# append may wait: a person clicking gets "busy, try again" quickly.
PAGE_LOCK_TIMEOUT_S = 2.0

_ITEM = re.compile(r"^(\s*[-*]\s*\[)([ xX])(\]\s+)(.*?)\s*$")
# A plain bullet under the heading: not something a care run acts on, but it is
# still on the list, so the page shows it rather than hiding it.
_BULLET = re.compile(r"^\s*[-*]\s+(?!\[[ xX]\]\s)(\S.*?)\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# The kinds the stock guide allows (unsure-and-proposals.md), written in lower case.
_KINDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("observation", re.compile(r"^observation:\s*")),
    ("drop", re.compile(r"^drop\s+(\S+?):\s*")),
    ("restore", re.compile(r"^restore to\s+(\S+?):\s*")),
    ("category", re.compile(r"^category:\s*")),
    ("rule", re.compile(r"^rule flip\s+(\S+?):\s*")),
    ("repo", re.compile(r"^repo\s+(.+?):\s+")),
)
# The tail every writer adds after the item: " — unsure because …" (" — removed
# because …" on a restore line).
_TAIL = re.compile(r"\s+—\s+(?=(?:unsure|removed) because\b)", re.IGNORECASE)
_SENTENCE_END = re.compile(r"[.?!](?=\s+[\"'“(A-Z0-9]|\s*$)")
_ID = re.compile(r"^[0-9a-f]{16}-\d{1,5}$")
# The exact line an approval writes, or the entry it removes or puts back, quoted
# after the item's first sentence (straight or curly quotes; the last one wins).
_QUOTED = re.compile(r"\"([^\"]+)\"|“([^”]+)”")
# A Markdown heading outside fenced code: the section an item sits under.
_HEADING_LINE = re.compile(r"^ {0,3}#{1,6}\s+(.*?)\s*#*\s*$")
# What a `to:` field may name: one of these regions, or a vault note outside the
# folders the engine and the person keep for themselves (`Workspace/` holds this
# list, the care report and proposals; `automations/` holds schedule prompts) and
# not the workspace guide. Anything else is shown as not allowed and never
# approved from the page; the care step leaves such a line ticked.
DESTINATION_REGIONS = ("profile", "memory")
_DESTINATION_DENIED_DIRS = ("workspace", "automations")
# Characters a put-back line may not hold: controls (tab included), format
# characters (bidi overrides, zero-width), and line and paragraph separators.
_FORBIDDEN_CATEGORIES = ("Cc", "Cf", "Zl", "Zp")
# The longest line the page's Undo may put back. The page sends the line it
# removed; one past this is refused rather than written.
MAX_RESTORE_CHARS = 4000


class UnsureError(RuntimeError):
    """The list could not be read or changed; `status` is the HTTP answer, `code` its kind."""

    def __init__(self, message: str, status: int, code: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def line_id(line: str, occurrence: int = 0) -> str:
    """The id the page names a line by: a hash of its exact text, and which copy it is.

    Two identical lines get `<hash>-0` and `<hash>-1`, so an action never
    touches both, and an id whose copy is gone no longer matches.
    """
    return f"{hashlib.sha256(line.encode('utf-8')).hexdigest()[:16]}-{occurrence}"


def is_line_id(value: str) -> bool:
    return bool(_ID.fullmatch(value))


def _split(text: str) -> list[str]:
    """The file's lines, split on `\\n` only (one trailing `\\r` stays on its line).

    `str.splitlines` also breaks on U+2028, NEL, VT, FF and the file
    separators; the reader and the writer must cut the file at the same places,
    or an id read from one line would name another.
    """
    return text.split("\n")


def _bare(piece: str) -> str:
    return piece[:-1] if piece.endswith("\r") else piece


def _listed(pieces: list[str]) -> list[tuple[int, str, str]]:
    """`(index, line, id)` for every item line: tick lines and plain bullets outside fenced code."""
    found: list[tuple[int, str, str]] = []
    seen: dict[str, int] = {}
    fenced = False
    for index, piece in enumerate(pieces):
        line = _bare(piece)
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced or not line.strip():
            continue
        if _ITEM.match(line) or _BULLET.match(line):
            occurrence = seen.get(line, 0)
            seen[line] = occurrence + 1
            found.append((index, line, line_id(line, occurrence)))
    return found


def _title(text: str) -> str:
    """The first sentence of an item's text, or all of it when it has no end."""
    head = text.split(" — ", 1)[0].strip()
    match = _SENTENCE_END.search(head)
    return head[: match.end()].strip() if match else head


def destination_allowed(destination: str) -> bool:
    """Whether a line's `to:` names a place an approval may write.

    A region in :data:`DESTINATION_REGIONS`, or a vault-relative `.md` note
    (no leading `/` or `~`, no `..` or dot segment, no control character: the
    rules of :func:`ciao.care_report.vault_relative_path`) outside `Workspace/`
    and `automations/`, and not `AGENTS.md`. Empty is not allowed: there is
    nothing to show.
    """
    if destination in DESTINATION_REGIONS:
        return True
    if vault_relative_path(destination) != destination:
        return False
    # Compare as a case-insensitive disk would: NFKC (fullwidth letters, the
    # Kelvin sign) then casefold (`ſ` is `s`), each segment without trailing dots
    # or spaces, which some file systems drop.
    folded = [
        unicodedata.normalize("NFKC", segment).casefold().rstrip(". ")
        for segment in destination.split("/")
    ]
    if not folded[-1].endswith(".md") or not destination.casefold().endswith(".md"):
        return False
    return folded[0] not in _DESTINATION_DENIED_DIRS and folded != ["agents.md"]


def parse_item(line: str, item_id: str | None = None) -> dict[str, Any]:
    """One line of the list as the page shows it. Never raises.

    A plain bullet, or a tick line with no text, is returned with
    `malformed: true` and its raw text, so it is shown, never dropped.
    """
    raw = _bare(line)
    item: dict[str, Any] = {
        "id": item_id or line_id(raw),
        "raw": raw.strip(),
        "checked": False,
        "kind": "",
        "qualifier": "",
        "title": "",
        "text": "",
        "reason": "",
        "source": "",
        "date": "",
        "destination": "",
        "destination_ok": False,
        "proposal": "",
        "section": "",
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
        # Read from the end: the date, then the source; a reason may hold "; " itself.
        fields = [f.strip() for f in parts[1].split(";")]
        if len(fields) > 1 and _DATE.match(fields[-1]):
            item["date"] = fields.pop()
        # The source and the destination (`to:`) close the tail, in either order.
        while len(fields) > 1:
            named = re.match(r"^(sources?|to):\s*(.*)$", fields[-1], re.IGNORECASE | re.DOTALL)
            if named is None:
                break
            key = "destination" if named.group(1).lower() == "to" else "source"
            if item[key]:
                break
            item[key] = named.group(2).strip()
            fields.pop()
        reason = "; ".join(fields)
        item["reason"] = re.sub(r"^(?:unsure|removed) because\s+", "", reason, flags=re.IGNORECASE)
    for kind, pattern in _KINDS:
        kind_match = pattern.match(head)
        if kind_match is not None:
            item["kind"] = kind
            item["qualifier"] = kind_match.group(1) if kind_match.groups() else ""
            head = head[kind_match.end():]
            break
    item["destination_ok"] = destination_allowed(item["destination"])
    item["text"] = head
    item["title"] = _title(head)
    # An old line may be nothing but the quoted entry: then that is the proposal.
    quoted = _QUOTED.findall(head[len(item["title"]):]) or _QUOTED.findall(head if _QUOTED.fullmatch(head) else "")
    if quoted:
        item["proposal"] = next(part for part in quoted[-1] if part).strip()
    if not item["title"]:
        item["malformed"] = True
        item["title"] = body
    return item


def _sections(pieces: list[str]) -> list[str]:
    """For each line, the text of the nearest heading above it outside fenced code ("" for none)."""
    sections: list[str] = []
    current = ""
    fenced = False
    for piece in pieces:
        line = _bare(piece)
        if _FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            heading = _HEADING_LINE.match(line)
            if heading is not None:
                current = heading.group(1)
        sections.append(current)
    return sections


def parse_list(text: str) -> list[dict[str, Any]]:
    """Every item on the list in file order, with the heading it sits under; headings, prose and fenced code are skipped."""
    pieces = _split(text)
    sections = _sections(pieces)
    items = []
    for index, line, item_id in _listed(pieces):
        item = parse_item(line, item_id)
        item["section"] = sections[index]
        items.append(item)
    return items


def _is_link(info: os.stat_result) -> bool:
    if stat_module.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _not_plain() -> UnsureError:
    return UnsureError("Workspace/Unsure.md is not a plain file in the vault, so the page will not read or change it", 422, "not_plain_file")


def _too_large() -> UnsureError:
    return UnsureError("Workspace/Unsure.md is too large to show here; open the file instead", 413, "too_large")


def _changed() -> UnsureError:
    return UnsureError("the list changed; reload it", 409, "line_changed")


def _checked_read(vault_root: Path) -> str | None:
    """The list's text, or None when there is no list. Raises :class:`UnsureError`.

    Neither `Workspace` nor the file may be a link, the file must be one regular
    file (one hard link) under the size cap and inside the vault, and the file
    opened must be the one checked, so a swap in between is refused. (The write
    side re-checks the folder itself: :func:`ciao.file_kernel.write_queue_atomically`.)
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
        raise _not_plain()
    if _is_link(info) or not stat_module.S_ISREG(info.st_mode) or info.st_nlink > 1:
        raise _not_plain()
    if not path.resolve().is_relative_to(root):
        raise _not_plain()
    if info.st_size > MAX_FILE_BYTES:
        raise _too_large()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or not stat_module.S_ISREG(opened.st_mode):
            raise _not_plain()
        raw = os.read(fd, MAX_FILE_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_FILE_BYTES:
        raise _too_large()
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


def _allowed_line(line: str) -> bool:
    """One line with no control, format or separator character, starting at its bullet."""
    if not line or line[0] not in "-*":
        return False
    # Plain spaces around the box: `\s` in the item pattern also matches NBSP and friends.
    if len(line) > 1 and not line.startswith(("- ", "* ")):
        return False
    return not any(unicodedata.category(ch) in _FORBIDDEN_CATEGORIES for ch in line)


def restore_line(
    vault_root: Path,
    line: str,
    before: str = "",
    after: str = "",
    section: str = "",
    *,
    lock_timeout_s: float = PAGE_LOCK_TIMEOUT_S,
) -> dict[str, Any]:
    """Add one unticked item line: the page's Undo of a dismissal.

    The line must be one unticked `- [ ]` item with text, starting at its
    bullet (no leading whitespace), at most :data:`MAX_RESTORE_CHARS` characters,
    with no character in Unicode categories Cc (tab included), Cf, Zl or Zp and
    nothing credential-shaped, so the page can never add a ticked line, a second
    line or one a care run would act on. It goes back where it was: after the item
    whose id is `after`, or before the one whose id is `before`, whichever is
    still under the heading `section` the line sat under; else at the end of that
    heading's part of the file; else at the end. A list that already holds the
    exact line is left as it is. Same lock and atomic write as
    :func:`change_line`. Raises :class:`UnsureError` (`bad_request`) or
    :class:`ciao.file_kernel.QueueLockError`. Returns the list after the write.
    """
    match = _ITEM.match(line) if _allowed_line(line) and line[1:6] == " [ ] " else None
    if match is None or match.group(2) != " " or not match.group(4).strip():
        raise UnsureError("only one unticked item line can be added back", 400, "bad_request")
    if len(line) > MAX_RESTORE_CHARS or redact_text(line) != line:
        raise UnsureError("this line cannot be added back from the page", 400, "bad_request")
    if (before and not is_line_id(before)) or (after and not is_line_id(after)) or not _allowed_line("- " + section):
        raise UnsureError("op and id are required", 400, "bad_request")
    path = unsure_path(Path(vault_root).resolve())
    with queue_lock(path, timeout_s=lock_timeout_s):
        text = _checked_read(vault_root)
        if text is None or not text.strip():
            fresh = HEADING + "\n\n" + line + "\n"
            write_queue_atomically(path, fresh)
            return _payload(fresh)
        pieces = _split(text)
        listed = _listed(pieces)
        if any(existing == line for _, existing, _ in listed):
            return _payload(text)
        sections = _sections(pieces)
        at = _restore_place(pieces, sections, listed, before, after, section)
        pieces.insert(at, line)
        if at == len(pieces) - 1:
            pieces.append("")
        updated = "\n".join(pieces)
        write_queue_atomically(path, updated)
    return _payload(updated)


def _restore_place(
    pieces: list[str], sections: list[str], listed: list[tuple[int, str, str]], before: str, after: str, section: str
) -> int:
    """The line index a put-back line goes in at (see :func:`restore_line`)."""
    where = {item_id: index for index, _, item_id in listed}

    def same(index: int) -> bool:
        # No section given (an older page): any neighbour that is still there will do.
        return not section or sections[index] == section

    if after in where and same(where[after]):
        return where[after] + 1
    if before in where and same(where[before]):
        return where[before]
    # The end of the heading's part (or of the file): before the next heading,
    # after the last non-blank line.
    if section:
        starts = [i for i, piece in enumerate(pieces) if sections[i] == section and _HEADING_LINE.match(_bare(piece))]
        if not starts:
            return _end_of(pieces, len(pieces))
        stop = next((i for i in range(starts[0] + 1, len(pieces)) if sections[i] != section), len(pieces))
        return _end_of(pieces, stop)
    return _end_of(pieces, len(pieces))


def _end_of(pieces: list[str], stop: int) -> int:
    """Just after the last non-blank line before `stop`."""
    end = stop
    while end > 0 and not pieces[end - 1].strip():
        end -= 1
    return end


def change_line(
    vault_root: Path, op: str, target: str, *, lock_timeout_s: float = PAGE_LOCK_TIMEOUT_S
) -> dict[str, Any]:
    """Tick, untick or remove the item line whose id is `target`.

    Only a line :func:`parse_list` lists can be named, so the heading, prose and
    fenced code are never changed. Done under the list's file lock (the one
    :func:`append_unsure` takes, waiting at most `lock_timeout_s`) and written
    atomically, so a concurrent append is never lost. Raises :class:`UnsureError`
    (`line_changed` when no line has that id any more, `bad_request` for an
    unknown action or a tick on a plain bullet) or
    :class:`ciao.file_kernel.QueueLockError`. Returns the list after the write.
    """
    if op not in ("tick", "untick", "remove") or not is_line_id(target):
        raise UnsureError("op and id are required", 400, "bad_request")
    path = unsure_path(Path(vault_root).resolve())
    with queue_lock(path, timeout_s=lock_timeout_s):
        text = _checked_read(vault_root)
        if text is None:
            raise _changed()
        pieces = _split(text)
        found = [(index, line) for index, line, item_id in _listed(pieces) if item_id == target]
        if not found:
            raise _changed()
        index, line = found[0]
        if op == "remove":
            del pieces[index]
        else:
            match = _ITEM.match(line)
            if match is None:
                raise UnsureError("this line has no tick box", 400, "bad_request")
            mark = "x" if op == "tick" else " "
            pieces[index] = f"{match.group(1)}{mark}{pieces[index][match.end(2):]}"
        updated = "\n".join(pieces)
        write_queue_atomically(path, updated)
    return _payload(updated)
