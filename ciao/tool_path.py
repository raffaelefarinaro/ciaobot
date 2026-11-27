"""Resolve external CLI tools against the user's real terminal PATH.

macOS launches GUI apps (Finder, the menu-bar companion, LaunchServices) and
launchd jobs with a stripped-down PATH — typically ``/usr/bin:/bin:/usr/sbin:
/sbin`` — that omits Homebrew (``/opt/homebrew/bin``), nvm's node bin, and
``~/.local/bin``. A tool installed with ``npm install -g`` or ``brew install``
then works fine in the user's terminal but is invisible to ``shutil.which`` in
the server process, so a provider CLI such as ``opencode`` reports as "missing"
even though it is installed.

This module recovers the PATH the user's own terminal would use, merges it with
the process PATH and a curated directory list, and resolves tools against the
result. The login-shell probe and the tool lookup live in :mod:`ciao.os_support.tool_path`.
"""

from __future__ import annotations

import functools
import os

from ciao.os_support.tool_path import (
    clear_terminal_path_cache,
    common_tool_dirs,
    dedupe_path,
    engine_bin_dir,
    engine_path_export,
    prepend_engine_path,
    resolve_command as _resolve_command,
    resolve_executable as _resolve_executable,
    terminal_path,
)

__all__ = [
    "clear_terminal_path_cache",
    "common_tool_dirs",
    "engine_bin_dir",
    "engine_path_export",
    "login_shell_path",
    "prepend_engine_path",
    "resolve_command",
    "resolve_on_terminal_path",
    "resolve_tool",
    "terminal_launch_command",
    "terminal_path",
]


def resolve_on_terminal_path(cmd: str) -> str | None:
    """Absolute path to ``cmd`` as the user's terminal would resolve it, or None."""
    path = terminal_path()
    return _resolve_executable(cmd, path=path) if path else None


def terminal_launch_command(name: str, binary: str | None) -> str:
    """The line that starts ``name`` in the user's terminal.

    The bare word when that terminal resolves it, else ``binary`` by absolute
    path, shell-quoted. With neither, the bare word.
    """
    import shlex

    try:
        in_terminal = resolve_on_terminal_path(name)
    except Exception:
        in_terminal = None
    if in_terminal:
        return name
    return shlex.quote(binary) if binary else name


@functools.lru_cache(maxsize=1)
def login_shell_path() -> str:
    """PATH as seen by the user's interactive login shell.

    Returns the current process PATH augmented with the terminal's PATH and a
    set of well-known tool directories. Deduplicated and order-preserving.
    Cached for the process lifetime — PATH directories are stable even after a
    tool is installed into one of them.
    """
    current = os.environ.get("PATH", "")
    shell_path = terminal_path()
    extra = [d for d in common_tool_dirs() if d and os.path.isdir(d)]
    return dedupe_path(os.pathsep.join([shell_path, current, *extra]))


def resolve_tool(cmd: str) -> str | None:
    """Absolute path to ``cmd`` on the terminal PATH, or None if not found.

    Drop-in replacement for ``shutil.which(cmd)`` that also searches the dirs a
    GUI/launchd-launched server would otherwise miss.
    """
    return _resolve_executable(cmd, path=login_shell_path())


def resolve_command(cmd: str) -> list[str]:
    """The argv prefix that spawns ``cmd`` on the terminal PATH, or ``[]``.

    For callers that spawn a tool: ``[exe]``. Pass the tool's arguments after it
    in the same list, so they never go through a shell.
    """
    return _resolve_command(cmd, path=login_shell_path())
