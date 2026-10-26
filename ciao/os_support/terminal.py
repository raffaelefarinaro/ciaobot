"""The one terminal surface, whichever platform the engine runs on.

A real terminal is a platform difference: a pty with a controlling terminal on
POSIX (:mod:`ciao.os_support.terminal_posix`), a pseudoconsole on Windows
(:mod:`ciao.os_support.terminal_windows`). The two adapters expose the same
shape, but nothing chose between them, so every caller would have had to
branch on ``sys.platform`` itself — which this package exists to prevent
(``ciao/os_support/__init__.py``).

This module is that choice, made once. :class:`TerminalProcess` is the
contract both adapters already honour, and :func:`spawn_terminal` starts the
platform's real terminal behind it. A caller that wants a terminal imports
this module and never learns which platform it got.

Two things are deliberately not here:

- **No policy.** The facade validates nothing, retries nothing and wraps
  nothing. The adapter's own refusals — a folder that is not there
  (:class:`FileNotFoundError`), a size out of range (:class:`ValueError`), a
  shell that could not start (:class:`OSError`) — reach the caller unchanged,
  because the facade has nothing to add to them.
- **No third platform.** macOS and Linux run the POSIX adapter and Windows
  runs the Windows one; those are the platforms the engine runs on. A stub
  for anything else would be a code path nothing can reach, and a caller that
  reached it would be told a terminal exists where none does.

The one ``sys.platform`` check lives here, in this module, and nowhere else
outside ``ciao/os_support/``. Only the chosen adapter is imported, and only
when a terminal is actually asked for: importing the Windows adapter on
POSIX would drag a native library load into an import path that never needs
it.

The protocol's read size is :data:`TERMINAL_READ_BYTES`, the value both
adapters already use, so the two cannot drift apart silently: the adapters
keep the behaviour, and this module only names it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Protocol, runtime_checkable

#: How many bytes one read asks for. The value both adapters already use.
TERMINAL_READ_BYTES = 65536


@runtime_checkable
class TerminalProcess(Protocol):
    """A running terminal: bytes in, bytes out, and an end.

    The contract both platform adapters honour, so a caller can hold either
    one without knowing which it got. The methods are the adapter's own; the
    meanings are the same on both platforms:

    - ``read`` returns the bytes the terminal has produced, ``None`` when it
      is quiet for now and ``b""`` when it is over;
    - ``write`` sends bytes in and reports how many it took;
    - ``resize`` changes the window size, in characters;
    - ``poll`` reports the shell's exit code, or ``None`` while it runs;
    - ``close`` ends the terminal and everything it owns.
    """

    pid: int

    def read(self, size: int = TERMINAL_READ_BYTES) -> bytes | None:
        """The next output bytes: some, none yet (``None``), or the end (``b""``)."""
        ...

    def write(self, data: bytes) -> int:
        """Send input bytes; returns how many the terminal took."""
        ...

    def resize(self, cols: int, rows: int) -> None:
        """Tell the terminal a new window size, in characters."""
        ...

    def poll(self) -> int | None:
        """The shell's exit code, or ``None`` while it still runs."""
        ...

    def close(self) -> None:
        """End the terminal and everything it owns."""
        ...


def spawn_terminal(*, cwd: Path, cols: int = 80, rows: int = 24) -> TerminalProcess:
    """Start the platform's real terminal in ``cwd``, and return it running.

    The size is in characters, and the defaults are the ones both adapters
    use. The terminal is the platform's own: a pty on POSIX, a pseudoconsole
    on Windows.

    Nothing is validated here and nothing is caught: the adapter's refusals
    (``FileNotFoundError`` for a folder that is not there, ``ValueError`` for
    a size out of range, ``OSError`` for a terminal that could not start)
    reach the caller as they are.
    """
    if sys.platform == "win32":
        from ciao.os_support.terminal_windows import spawn_terminal as _spawn
    else:
        from ciao.os_support.terminal_posix import spawn_terminal as _spawn
    return _spawn(cwd=cwd, cols=cols, rows=rows)
