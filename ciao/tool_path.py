"""Resolve external CLI tools against the user's real terminal PATH.

macOS launches GUI apps (Finder, the menu-bar companion, LaunchServices) and
launchd jobs with a stripped-down PATH — typically ``/usr/bin:/bin:/usr/sbin:
/sbin`` — that omits Homebrew (``/opt/homebrew/bin``), nvm's node bin, and
``~/.local/bin``. A tool installed with ``npm install -g`` or ``brew install``
then works fine in the user's terminal but is invisible to ``shutil.which`` in
the server process, so features like the Google Workspace ``gws`` CLI report as
"missing" even though they are installed.

This module recovers the PATH the user's own terminal would use, merges it with
the process PATH and a curated directory list, and resolves tools against the
result. What "the user's terminal" means is an operating-system difference, so
the probing and the tool resolution live in :mod:`ciao.os_support.tool_path`:
POSIX spawns an interactive login shell, Windows reads the PATH a new logon
gets out of the registry. Nothing here branches on the platform.
"""

from __future__ import annotations

import functools
import os

from ciao.os_support.tool_path import (
    clear_terminal_path_cache,
    common_tool_dirs,
    resolve_executable as _resolve_executable,
    terminal_path,
)

__all__ = [
    "clear_terminal_path_cache",
    "common_tool_dirs",
    "login_shell_path",
    "resolve_on_terminal_path",
    "resolve_tool",
    "terminal_path",
]


def resolve_on_terminal_path(cmd: str) -> str | None:
    """Absolute path to ``cmd`` as the user's terminal would resolve it, or None."""
    path = terminal_path()
    return _resolve_executable(cmd, path=path) if path else None


@functools.lru_cache(maxsize=1)
def login_shell_path() -> str:
    """PATH as seen by the user's interactive login shell.

    Returns the current process PATH augmented with the terminal's PATH and a
    set of well-known tool directories. Deduplicated, order-preserving. Cached
    for the process lifetime — PATH directories are stable even after a tool is
    installed into one of them.
    """
    current = os.environ.get("PATH", "")
    shell_path = terminal_path()

    ordered: list[str] = []
    seen: set[str] = set()
    for chunk in (shell_path, current):
        for d in chunk.split(os.pathsep):
            if d and d not in seen:
                seen.add(d)
                ordered.append(d)
    for d in common_tool_dirs():
        if d and os.path.isdir(d) and d not in seen:
            seen.add(d)
            ordered.append(d)
    return os.pathsep.join(ordered)


def resolve_tool(cmd: str) -> str | None:
    """Absolute path to ``cmd`` on the terminal PATH, or None if not found.

    Drop-in replacement for ``shutil.which(cmd)`` that also searches the dirs a
    GUI/launchd-launched server would otherwise miss, and on Windows resolves
    the npm shim a global install leaves behind to the executable behind it.

    Raises :class:`~ciao.os_support.tool_path.ToolResolutionError` (an
    ``OSError``) when the match is an npm shim whose executable is missing, so
    a broken install is not reported as a tool that is not installed.
    """
    return _resolve_executable(cmd, path=login_shell_path())