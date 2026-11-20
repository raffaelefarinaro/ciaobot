"""The one terminal surface, whichever platform the engine runs on.

A real terminal is a pty with a controlling terminal
(:mod:`ciao.os_support.terminal_posix`). This module is the one entry point
callers use.

One thing is deliberately not here:

- **No policy.** The facade validates nothing, retries nothing and wraps
  nothing. The adapter's own refusals — a folder that is not there
  (:class:`FileNotFoundError`), a size out of range (:class:`ValueError`), a
  shell that could not start (:class:`OSError`) — reach the caller unchanged,
  because the facade has nothing to add to them.

The adapter is imported only when a terminal is asked for.

The protocol's read size is :data:`TERMINAL_READ_BYTES`, the value the
adapter already uses, so the two cannot drift apart silently: the adapter
keeps the behaviour, and this module only names it.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

#: How many bytes one read asks for. The value the adapter already uses.
TERMINAL_READ_BYTES = 65536


@runtime_checkable
class TerminalProcess(Protocol):
    """A running terminal: bytes in, bytes out, and an end.

    The contract the adapter honours, so a caller can hold it without
    knowing which platform it got. The methods are the adapter's own; the
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


def spawn_terminal(
    *, cwd: Path, cols: int = 80, rows: int = 24, argv: Sequence[str] | None = None
) -> TerminalProcess:
    """Start the platform's real terminal in ``cwd``, and return it running.

    The size is in characters, and the defaults are the ones the adapter
    uses. The terminal is a pty. ``argv`` runs that program instead of the
    shell; only engine code passes it.

    Nothing is validated here and nothing is caught: the adapter's refusals
    (``FileNotFoundError`` for a folder that is not there, ``ValueError`` for
    a size out of range, ``OSError`` for a terminal that could not start)
    reach the caller as they are.
    """
    from ciao.os_support.terminal_posix import spawn_terminal as _spawn
    return _spawn(cwd=cwd, cols=cols, rows=rows, argv=argv)
