"""Read a workspace's `Workspace/Care-Report.md` into a fixed shape.

The daily Workspace care run (the `system-memory-curation` schedule) writes
one Markdown checklist per workspace and ticks it as each step finishes. This
module reads that file for the PWA and never writes it: the agent owns the
file, and the app only shows what the file says.

Reading is deterministic, bounded and never raises. A missing file is the
"never run" state; an oversized or unreadable file is reported as such; a file
whose lines do not match the checklist format yields what could be read. The
path is fixed under the workspace's vault and opened without following a
symlink at any step, so nothing planted at `Workspace/Care-Report.md` can point
the reader at a file elsewhere on disk.
"""

from __future__ import annotations

import os
import re
import stat as stat_module
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPORT_RELATIVE_PATH = Path("Workspace") / "Care-Report.md"
UNSURE_RELATIVE_PATH = Path("Workspace") / "Unsure.md"

# A real report is a few kilobytes: a header, under ten step lines and a short
# list. Anything past this is not a checklist the run wrote.
MAX_REPORT_BYTES = 64 * 1024
MAX_STEPS = 40
MAX_NEEDS = 20
MAX_FIELD_CHARS = 600

# The step files the care prompt dispatches (ciao/stock/schedules.json, step 2),
# under the durable-memory procedures' `references/` folder. A test keeps this
# map equal to the prompt's own list.
STEP_FILES: dict[str, str] = {
    "unsure": "curation-pass-1-unsure.md",
    "drain": "drain-legacy-queues.md",
    "second-look": "curation-pass-8-second-look.md",
    "closed-tasks": "curation-pass-9-closed-tasks.md",
    "regions": "curation-pass-2-regions.md",
    "learnings": "curation-pass-4-learnings.md",
    "stale": "curation-pass-3-stale-notes.md",
    "guide-review": "curation-pass-7-guide-body.md",
    "rules": "curation-pass-7b-decision-rules.md",
    "hygiene": "curation-pass-6-hygiene.md",
}

_STEP_LINE = re.compile(r"^\s*[-*]\s+\[([ xX!])\]\s+([A-Za-z0-9][A-Za-z0-9_-]{0,63})\s*:\s*(.*)$")
_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_DATE_LINE = re.compile(r"^\s*(?:date:\s*)?\d{4}-\d{2}-\d{2}\b", re.IGNORECASE)
_WEEKLY = re.compile(r"^\s*Weekly done:\s*(.*)$", re.IGNORECASE)
_INTERRUPTED = re.compile(r"^\s*Interrupted last run:\s*(.*)$", re.IGNORECASE)
_RETRY = re.compile(r"^\s*Weekly retry:\s*(.*)$", re.IGNORECASE)
_RETRY_PAIR = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_-]{0,63})\s*=\s*(\d{1,3})$")
# "## What needs you", a bare "What needs you" line or a bold one, with an
# optional trailing colon: the prompt asks for the heading, the reply style
# writes the bare line, and a model may bold it.
_NEEDS_HEADING = re.compile(r"^\s*(?:#{1,6}\s+)?(\*\*)?What needs you:?(?(1)\*\*):?\s*$", re.IGNORECASE)
_HEADING = re.compile(r"^\s*#{1,6}\s+")
_SECTION = re.compile(r"^\s*#{2,6}\s+")
_LIST_ITEM = re.compile(r"^\s*(?:\d{1,3}[.)]|[-*+])\s+(.*)$")
# What a model writes for "no ids" instead of leaving the line out.
_PLACEHOLDERS = {"none", "n/a", "na", "-", "\u2014", "empty", "nothing"}
_BOLD_LEAD = re.compile(r"^\*\*(.+?)\*\*\s*[:\-\u2014]?\s*(.*)$")
_STATES = {" ": "open", "x": "done", "X": "done", "!": "blocked"}
# The em dash the prompt writes, and the ASCII stand-ins a model may use instead.
_EVIDENCE_SEPARATORS = (" \u2014 ", " -- ", " \u2013 ")


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_FIELD_CHARS else text[: MAX_FIELD_CHARS - 1] + "\u2026"


def _split_step(state: str, rest: str) -> dict[str, str]:
    what, tail = rest, ""
    for separator in _EVIDENCE_SEPARATORS:
        if separator in rest:
            what, tail = rest.split(separator, 1)
            break
    evidence, verify = tail, ""
    if state == "blocked":
        evidence = re.sub(r"^blocked:\s*", "", tail, flags=re.IGNORECASE)
    else:
        marker = tail.lower().rfind("; verify:")
        if marker >= 0:
            evidence, verify = tail[:marker], tail[marker + len("; verify:"):]
        elif tail.lower().startswith("verify:"):
            evidence, verify = "", tail[len("verify:"):]
    return {"what": _clip(what), "evidence": _clip(evidence), "verify": _clip(verify)}


def _ids(raw: str) -> list[str]:
    parts = (piece.strip().strip("`").strip(".") for piece in re.split(r"[,\s]+", raw))
    return [part for part in parts if part and part.lower() not in _PLACEHOLDERS][:MAX_STEPS]


def _retry(raw: str) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    for piece in raw.split(","):
        found = _RETRY_PAIR.match(piece.replace("`", "").strip())
        if found and len(pairs) < MAX_STEPS:
            pairs.append({"id": found.group(1).lower(), "runs": int(found.group(2))})
    return pairs


def _needs_item(text: str) -> dict[str, str]:
    text = " ".join(text.split())
    title, detail = text, ""
    bold = _BOLD_LEAD.match(text)
    if bold:
        title, detail = bold.group(1), bold.group(2)
    else:
        colon = text.find(": ")
        if 0 < colon <= 80:
            title, detail = text[:colon], text[colon + 2:]
    lowered = text.lower()
    if "unsure" in lowered:
        action = "unsure"
    elif "agents.md" in lowered or "guide" in lowered or "region" in lowered or "standing memory" in lowered:
        action = "guide"
    else:
        action = "report"
    return {"title": _clip(title.strip()), "detail": _clip(detail.strip()), "action": action}


def parse_care_report(text: str) -> dict[str, Any]:
    """Parse the text of a care report. Pure: no I/O, never raises.

    The checklist is the `- [ ]` / `- [x]` / `- [!]` lines before the first
    `##` heading; a checkbox in a later section (a hygiene finding, say) is not a
    step. `## What needs you` (or a bare or bold "What needs you" line) opens a
    list of numbered or bulleted items, ended by the next heading.
    """
    updated = ""
    weekly_done = ""
    weekly_retry: list[dict[str, Any]] = []
    interrupted: list[str] = []
    steps: list[dict[str, str]] = []
    needs: list[dict[str, str]] = []
    nothing_due = False
    recognised = False
    in_checklist = True
    in_needs = False
    current: list[str] | None = None

    def flush() -> None:
        if current is not None and len(needs) < MAX_NEEDS:
            needs.append(_needs_item(" ".join(current)))

    for line in text.splitlines():
        if _NEEDS_HEADING.match(line):
            flush()
            current = None
            in_needs = True
            in_checklist = False
            recognised = True
            continue
        if in_needs:
            if _HEADING.match(line):
                flush()
                current = None
                in_needs = False
            else:
                item = _LIST_ITEM.match(line)
                if item:
                    flush()
                    current = [item.group(1)]
                elif current is not None and line.strip():
                    current.append(line.strip())
                continue
        # A section heading ends the checklist only once it has begun: a report
        # titled with `##`, or one that heads its list "## Checklist", still has
        # its steps below that heading.
        if _SECTION.match(line) and steps:
            in_checklist = False
            continue
        if not in_checklist:
            continue
        step = _STEP_LINE.match(line)
        if step:
            recognised = True
            if len(steps) < MAX_STEPS:
                state = _STATES[step.group(1)]
                steps.append({"id": step.group(2).lower(), "state": state, **_split_step(state, step.group(3))})
            continue
        weekly = _WEEKLY.match(line)
        if weekly:
            recognised = True
            found = _DATE.search(weekly.group(1))
            weekly_done = found.group(1) if found else ""
            continue
        retry = _RETRY.match(line)
        if retry:
            recognised = True
            weekly_retry = _retry(retry.group(1))
            continue
        interrupted_line = _INTERRUPTED.match(line)
        if interrupted_line:
            recognised = True
            interrupted = _ids(interrupted_line.group(1))
            continue
        if not updated and not steps and (_HEADING.match(line) or _DATE_LINE.match(line)):
            found = _DATE.search(line)
            if found:
                updated = found.group(1)
                recognised = True
            continue
        if line.strip().lower().rstrip(".") == "nothing due":
            nothing_due = True
            recognised = True
    if in_needs:
        flush()

    counts = {
        "total": len(steps),
        "done": sum(1 for s in steps if s["state"] == "done"),
        "blocked": sum(1 for s in steps if s["state"] == "blocked"),
        "open": sum(1 for s in steps if s["state"] == "open"),
    }
    return {
        "updated": updated,
        "weekly_done": weekly_done,
        "weekly_retry": weekly_retry,
        "interrupted_ids": interrupted,
        "nothing_due": nothing_due and not steps,
        "steps": steps,
        "needs_you": needs,
        "counts": counts,
        "malformed": not recognised,
    }


def _empty(status: str) -> dict[str, Any]:
    body = parse_care_report("")
    body["malformed"] = False
    return {"status": status, "modified_at": "", **body}


# Read once at import: whether `os.open` takes `dir_fd` (POSIX yes, Windows no).
_OPEN_TAKES_DIR_FD = os.open in os.supports_dir_fd


def _fd_relative_supported() -> bool:
    return sys.platform != "win32" and _OPEN_TAKES_DIR_FD


def _read_fd(fd: int, expect: os.stat_result | None = None) -> tuple[str, bytes, float]:
    """Judge and read an opened descriptor: a regular file with one link, under the cap.

    `expect` is the `lstat` taken before a path-based open; the opened file must
    be that same file, so a swap between the check and the open is refused.
    """
    info = os.fstat(fd)
    if not stat_module.S_ISREG(info.st_mode):
        return "unreadable", b"", 0.0
    # A hard link makes the report another file's contents under the vault's name.
    if info.st_nlink > 1:
        return "unreadable", b"", 0.0
    if expect is not None and (expect.st_ino or expect.st_dev) and (
        (info.st_dev, info.st_ino) != (expect.st_dev, expect.st_ino)
    ):
        return "unreadable", b"", 0.0
    if info.st_size > MAX_REPORT_BYTES:
        return "too_large", b"", 0.0
    chunks: list[bytes] = []
    remaining = MAX_REPORT_BYTES + 1
    while remaining > 0:
        chunk = os.read(fd, min(remaining, 16384))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    if len(raw) > MAX_REPORT_BYTES:
        return "too_large", b"", 0.0
    return "ok", raw, info.st_mtime


def _read_fd_relative(vault_root: Path) -> tuple[str, bytes, float]:
    """POSIX: open the report without following a symlink at any step.

    The directory and the file are opened relative to the vault's own
    descriptor with `O_NOFOLLOW`, so a symlink planted at `Workspace` or at the
    report (or swapped in between a check and the open) is refused, not
    followed. `O_NONBLOCK` keeps a FIFO from hanging the open; the descriptor is
    then `fstat`ed, so the type and size checked are those of what was opened.
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    root_fd = os.open(vault_root, os.O_RDONLY | directory)
    try:
        dir_fd = os.open(REPORT_RELATIVE_PATH.parent.name, os.O_RDONLY | directory | nofollow, dir_fd=root_fd)
        try:
            fd = os.open(REPORT_RELATIVE_PATH.name, os.O_RDONLY | nofollow | nonblock, dir_fd=dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        os.close(root_fd)
    try:
        return _read_fd(fd)
    finally:
        os.close(fd)


# The reparse tags that redirect a path: a symlink and a junction (mount
# point). Other reparse points are not links: OneDrive Files On-Demand marks
# every synced file and folder with a cloud tag even when fully downloaded.
_LINK_REPARSE_TAGS = {
    getattr(stat_module, "IO_REPARSE_TAG_SYMLINK", 0xA000000C),
    getattr(stat_module, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003),
}


def _is_link(info: os.stat_result) -> bool:
    """A symlink, or on Windows a symlink or junction reparse point.

    The resolved-path-inside-the-vault check and the same-file check after the
    open stay the backstop for anything else.
    """
    if stat_module.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    if not attributes & getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        return False
    return getattr(info, "st_reparse_tag", 0) in _LINK_REPARSE_TAGS


def _read_by_path(vault_root: Path) -> tuple[str, bytes, float]:
    """Windows (no `dir_fd`): check each step with `lstat`, then open and re-check.

    Neither `Workspace` nor the report may be a symlink or reparse point, the
    folder must be a folder and the report a regular file, and the resolved
    path must sit under the vault. The opened file is then `fstat`ed and must be
    the very file `lstat` saw (device and file index), so a swap between the
    checks and the open is refused rather than followed.
    """
    root = vault_root.resolve()
    folder = root / REPORT_RELATIVE_PATH.parent.name
    path = folder / REPORT_RELATIVE_PATH.name
    folder_info = os.lstat(folder)
    if _is_link(folder_info) or not stat_module.S_ISDIR(folder_info.st_mode):
        return "unreadable", b"", 0.0
    info = os.lstat(path)
    if _is_link(info) or not stat_module.S_ISREG(info.st_mode):
        return "unreadable", b"", 0.0
    if not path.resolve().is_relative_to(root):
        return "unreadable", b"", 0.0
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        return _read_fd(fd, expect=info)
    finally:
        os.close(fd)


def _read_bounded(vault_root: Path) -> tuple[str, bytes, float]:
    if _fd_relative_supported():
        return _read_fd_relative(vault_root)
    return _read_by_path(vault_root)


def read_care_report(vault_root: Path) -> dict[str, Any]:
    """Read `<vault_root>/Workspace/Care-Report.md`. Never raises.

    `status` is `missing` (no report yet: care never ran here), `too_large`,
    `unreadable` (a symlink, a directory, a FIFO or any other non-regular file,
    or an I/O error) or `ok`.
    """
    try:
        status, raw, mtime = _read_bounded(vault_root)
    except FileNotFoundError:
        return _empty("missing")
    except OSError:
        # ELOOP (a symlink), ENOTDIR (`Workspace` is not a folder), EACCES, EIO.
        return _empty("unreadable")
    except (ValueError, RuntimeError):
        return _empty("unreadable")
    if status != "ok":
        return _empty(status)
    text = raw.decode("utf-8", errors="replace")
    modified = datetime.fromtimestamp(mtime, UTC).isoformat().replace("+00:00", "Z")
    return {"status": "ok", "modified_at": modified, **parse_care_report(text)}
