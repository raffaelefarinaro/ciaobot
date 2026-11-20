"""The tail of a finished background shell's output file, read defensively.

The path is the one the provider reported in the shell's launch metadata, and Ciaobot reads it
only to show the chat how the shell ended. It is read only when all of these hold:

- it has no ``.`` or ``..`` segment and its realpath is itself (no symlink in the path, parent
  directories included);
- it sits under a provider output root, or it has a provider's file-name shape:
  OpenCode writes ``<data>/opencode/shell/<hex>/sh_<id>.out`` (the root is determined: the
  XDG data directory), and Claude writes ``.../tasks/<id>.output``. Claude's root is not
  determined in this code, so for it only the file-name shape and realpath equality apply;
- it is a regular file (checked before and after opening, opened non-blocking so a FIFO cannot
  stall the read), owned by the engine's user, and read from its end, at most 64 KiB.

The text is sanitised (ANSI and every non-printing character removed) and cut to 20 lines and
2000 bytes. Any failure gives ``None``, which means "the output could not be read".
"""

from __future__ import annotations

import logging
import os
import re
import stat

from ciao.providers.acp.normalize import sanitize_output

logger = logging.getLogger(__name__)

TAIL_LINES = 20
TAIL_BYTES = 2000
_READ_CAP = 64 * 1024
_OPENCODE_NAME = re.compile(r"/shell/[0-9a-f]{1,64}/sh_[A-Za-z0-9_-]{1,64}\.out$")
_CLAUDE_NAME = re.compile(r"/tasks/[A-Za-z0-9_-]{1,64}\.output$")


def _opencode_shell_root() -> str:
    """Where OpenCode writes a background shell's output: ``<XDG data>/opencode/shell``, resolved."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.realpath(os.path.join(base, "opencode", "shell"))


def is_shell_output_path(path: str) -> bool:
    """Whether *path* may be read as a shell's output file (see the module docstring)."""
    if not path or not os.path.isabs(path) or ".." in path.split("/") or "." in path.split("/")[1:]:
        return False
    if os.path.realpath(path) != path:
        return False
    if path.startswith(_opencode_shell_root() + os.sep) and _OPENCODE_NAME.search(path):
        return True
    return _CLAUDE_NAME.search(path) is not None


def read_tail(path: str) -> list[str] | None:
    """The last lines of *path* as plain text, ``[]`` when it is empty, ``None`` when unreadable."""
    if not is_shell_output_path(path):
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode):
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
    lines = [line for line in sanitize_output(text).split("\n") if line.strip()][-TAIL_LINES:]
    kept: list[str] = []
    budget = TAIL_BYTES
    for line in reversed(lines):
        cost = len(line.encode("utf-8", "replace")) + 1
        if cost > budget:
            break
        kept.append(line)
        budget -= cost
    if kept or not lines:
        return list(reversed(kept))
    # The last line alone is over the byte limit: its final bytes are the part that matters.
    tail_bytes = lines[-1].encode("utf-8", "replace")[-TAIL_BYTES:].decode("utf-8", "ignore").strip()
    return [tail_bytes] if tail_bytes else None
