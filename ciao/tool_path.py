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
from pathlib import Path

# Markers wrap the printed PATH so noisy shell rc files (which may echo banners
# to stdout) don't corrupt the value we extract.
_START = "__CIAO_PATH_START__"
_END = "__CIAO_PATH_END__"


def _common_tool_dirs() -> list[str]:
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


@functools.lru_cache(maxsize=1)
def login_shell_path() -> str:
    """PATH as seen by the user's interactive login shell.

    Returns the current process PATH augmented with the login shell's PATH and a
    set of well-known tool directories. Deduplicated, order-preserving. Cached
    for the process lifetime — PATH directories are stable even after a tool is
    installed into one of them.
    """
    current = os.environ.get("PATH", "")
    shell = os.environ.get("SHELL", "/bin/zsh")
    shell_path = ""
    try:
        # -l login, -i interactive so nvm / rbenv / rc-file PATH edits apply.
        result = subprocess.run(
            [shell, "-lic", f'printf "{_START}%s{_END}" "$PATH"'],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        out = result.stdout
        if _START in out and _END in out:
            shell_path = out.split(_START, 1)[1].split(_END, 1)[0]
    except Exception:
        shell_path = ""

    ordered: list[str] = []
    seen: set[str] = set()
    for chunk in (shell_path, current):
        for d in chunk.split(os.pathsep):
            if d and d not in seen:
                seen.add(d)
                ordered.append(d)
    for d in _common_tool_dirs():
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
