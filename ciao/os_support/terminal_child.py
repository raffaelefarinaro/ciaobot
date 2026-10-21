"""The helper that turns a fresh process into the terminal's own shell.

The engine cannot give a child a controlling terminal the way a terminal
emulator does: on POSIX the controlling terminal is a property of the
*session*, and the process that owns the session is the one that was made a
session leader. ``Popen(start_new_session=True)`` already does that, so this
helper only has to claim the pty slave as the controlling terminal and then
*become* the shell — no fork, no second child, no extra session.

The rules it follows, and why:

- ``fcntl.ioctl(0, termios.TIOCSCTTY, 0)`` claims the already-open slave as
  the controlling terminal of the current session. Without it the shell would
  own a session but no tty, and every interactive program would fail.
- ``os.execvpe`` replaces this process, so the pid, the session and the
  process group stay exactly the ones ``Popen`` created. A helper that forked
  would leave the shell as a child of a helper that then has to be waited for,
  and the terminal's session leader would no longer be the shell.
- It never calls ``setsid``: the session is already the engine's child, and a
  second one would detach the terminal from the group the cleanup signals.
- It imports nothing from ``ciao`` and runs under ``-I -S``, so no user or
  site initialisation runs and no credential is read on the way in.
- It writes nothing to stdout or stderr, and it never prints a traceback: the
  pty carries the shell's own output, and a traceback here would land in the
  user's terminal and could carry the path of the program. A failure is a
  status, and only a status.

The POSIX-only imports are inside the function, so the engine's import-all
gate can load this module on Windows too; that is also what keeps the
``win32`` type check clean. Importing the module does nothing: the work
happens in :func:`main`, under a ``__main__`` guard.
"""

from __future__ import annotations

import os
import sys

#: The terminal could not be claimed: the process is not the session leader
#: it needed to be, or the descriptor is not a terminal.
CLAIM_FAILED = 125
#: The program could not be run at all.
EXEC_FAILED = 126


def _claim_controlling_terminal() -> None:
    """Make the pty slave this process inherited its session's terminal.

    The imports are inside the function on purpose: this module is loaded by
    the engine's import-all gate on Windows, where neither module exists.
    """
    if sys.platform == "win32":  # pragma: no cover - the adapter is POSIX-only
        raise OSError("a controlling terminal is a POSIX thing")
    import fcntl
    import termios

    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def main(argv: list[str]) -> int:
    """Claim the terminal and become ``argv[0]``; never returns on success."""
    if not argv:
        return 2
    try:
        _claim_controlling_terminal()
    except OSError:
        return CLAIM_FAILED
    try:
        os.execvpe(argv[0], argv, os.environ)
    except OSError:
        return EXEC_FAILED
    return 127  # only reached when exec itself failed


if __name__ == "__main__":
    if sys.platform == "win32":  # pragma: no cover - the adapter is POSIX-only
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1:]))
