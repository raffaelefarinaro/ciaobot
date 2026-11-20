"""The tail of a finished background shell's output file, read defensively.

The path is the one the provider reported in the shell's launch acknowledgement, and Ciaobot
reads it only to show the chat how the shell ended. It must be a regular file (never followed
through a symlink), owned by the engine's user, and it is read from its end, a bounded number of
bytes. Any failure gives ``None``: the notice then says there is no output to show.
"""

from __future__ import annotations

import logging
import os
import re
import stat

logger = logging.getLogger(__name__)

TAIL_LINES = 20
TAIL_BYTES = 2000
_READ_CAP = 64 * 1024
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
# Control characters other than tab; newlines are kept for splitting and dropped afterwards.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def read_tail(path: str) -> list[str] | None:
    """The last lines of *path* as plain text, or ``None`` when it cannot be read safely."""
    if not path or not os.path.isabs(path):
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        before = os.lstat(path)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            return None
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (info.st_ino, info.st_dev) != (before.st_ino, before.st_dev):
            return None
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            return None
        size = info.st_size
        start = max(0, size - _READ_CAP)
        os.lseek(fd, start, os.SEEK_SET)
        raw = b""
        while len(raw) < size - start:
            chunk = os.read(fd, size - start - len(raw))
            if not chunk:
                break
            raw += chunk
    except OSError:
        return None
    finally:
        os.close(fd)
    text = raw.decode("utf-8", "replace")
    if start and "\n" in text:
        # The first line of a mid-file read may be cut; it is not the start of a line.
        text = text.split("\n", 1)[1]
    text = _CONTROL_RE.sub("", _ANSI_RE.sub("", text.replace("\r", "")))
    lines = [line for line in text.split("\n") if line.strip()][-TAIL_LINES:]
    kept: list[str] = []
    budget = TAIL_BYTES
    for line in reversed(lines):
        cost = len(line.encode("utf-8", "replace")) + 1
        if cost > budget:
            break
        kept.append(line)
        budget -= cost
    return list(reversed(kept))
