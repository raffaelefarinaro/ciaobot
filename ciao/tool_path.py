"""Resolve external CLI tools against the user's real login-shell PATH.

macOS launches GUI apps (Finder, the menu-bar companion, LaunchServices) and
launchd jobs with a stripped-down PATH — typically ``/usr/bin:/bin:/usr/sbin:
/sbin`` — that omits Homebrew (``/opt/homebrew/bin``), nvm's node bin, and
``~/.local/bin``. A tool installed with ``npm install -g`` or ``brew install``
then works fine in the user's terminal but is invisible to ``shutil.which`` in
the server process, so features like the Google Workspace ``gws`` CLI report as
"missing" even though they are installed.

This module recovers the interactive login shell's PATH once per process and
resolves tools against it, so lookups match what the user sees in a terminal.
"""

from __future__ import annotations

import functools
import glob
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

# Markers wrap the printed PATH so noisy shell rc files (which may echo banners
# to stdout) don't corrupt the value we extract.
_START = "__CIAO_PATH_START__"
_END = "__CIAO_PATH_END__"


def common_tool_dirs() -> list[str]:
    """Best-effort fallback dirs when the shell probe fails or is incomplete."""
    home = Path.home()
    dirs = [
        "/opt/homebrew/bin",
        "/opt/homebrew/sbin",
        "/usr/local/bin",
        "/usr/local/sbin",
        str(home / ".local" / "bin"),
        str(home / "bin"),
    ]
    # nvm installs globals under the active node version; we can't know which is
    # active without nvm loaded, so include every installed version's bin dir.
    dirs.extend(sorted(glob.glob(str(home / ".nvm" / "versions" / "node" / "*" / "bin"))))
    return dirs


# The setup wizard polls ``setup_status`` every 2s while telling the user
# "this check refreshes on its own" after they edit their rc file to add
# ``~/.local/bin``. A process-lifetime cache would keep serving the pre-edit
# miss, so the wizard would keep showing the PATH hint after the terminal is
# fixed until the engine restarts. Keep only a short TTL so repeated
# ``resolve_on_terminal_path`` calls within one status probe share one shell
# invocation, while the next poll re-probes. ``setup_status`` additionally
# clears this cache at the start of each call (see
# ``clear_terminal_path_cache``) so every wizard poll is fresh even when polls
# land inside the TTL window.
_TERMINAL_PATH_TTL_SECONDS = 5.0
_terminal_path_cache: tuple[float, str] | None = None
_terminal_path_lock = threading.Lock()


def clear_terminal_path_cache() -> None:
    """Drop the cached login-shell PATH probe (tests and status refresh)."""
    global _terminal_path_cache
    with _terminal_path_lock:
        _terminal_path_cache = None


def terminal_path() -> str:
    """PATH the user's own interactive login shell reports, or "".

    Exactly what a command typed in Terminal would be resolved against — no
    fallback directories, no inherited process PATH. Telling someone to run a
    bare ``claude`` is only correct when *this* PATH resolves it, so the
    augmented :func:`login_shell_path` (which appends ``~/.local/bin`` and
    Homebrew whether or not the user's shell has them) cannot answer that
    question. Cached briefly (see ``_TERMINAL_PATH_TTL_SECONDS``), unlike the
    process-lifetime :func:`login_shell_path`.
    """
    global _terminal_path_cache
    now = time.monotonic()
    with _terminal_path_lock:
        if (
            _terminal_path_cache is not None
            and now - _terminal_path_cache[0] < _TERMINAL_PATH_TTL_SECONDS
        ):
            return _terminal_path_cache[1]
    shell = os.environ.get("SHELL", "/bin/zsh")
    # Without PATH the shell builds its own from /etc/profile (path_helper) and
    # the user's rc files — which is the question being asked. Inheriting ours
    # would answer it wrongly: the engine prepends common_tool_dirs() to its
    # own PATH at startup (ciao/main.py), so ~/.local/bin would come back out
    # of the probe as if the user's terminal had it.
    env = {k: v for k, v in os.environ.items() if k != "PATH"}
    value = ""
    try:
        # -l login, -i interactive so nvm / rbenv / rc-file PATH edits apply.
        result = subprocess.run(
            [shell, "-lic", f'printf "{_START}%s{_END}" "$PATH"'],
            capture_output=True,
            text=True,
            timeout=5.0,
            env=env,
        )
        out = result.stdout
        if _START in out and _END in out:
            value = out.split(_START, 1)[1].split(_END, 1)[0]
    except Exception:
        pass
    with _terminal_path_lock:
        _terminal_path_cache = (time.monotonic(), value)
    return value


def resolve_on_terminal_path(cmd: str) -> str | None:
    """Absolute path to ``cmd`` as the user's terminal would resolve it, or None."""
    path = terminal_path()
    return shutil.which(cmd, path=path) if path else None


@functools.lru_cache(maxsize=1)
def login_shell_path() -> str:
    """PATH as seen by the user's interactive login shell.

    Returns the current process PATH augmented with the login shell's PATH and a
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
    """Absolute path to ``cmd`` on the login-shell PATH, or None if not found.

    Drop-in replacement for ``shutil.which(cmd)`` that also searches the dirs a
    GUI/launchd-launched server would otherwise miss.
    """
    return shutil.which(cmd, path=login_shell_path())
