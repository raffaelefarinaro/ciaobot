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


# Probing the terminal PATH costs a full interactive login shell — measured
# ~0.74s for `zsh -lic` on a stock machine, more with nvm/conda/oh-my-zsh. The
# setup wizard polls `setup_status` every 2s while telling the user "this check
# refreshes on its own" after they edit their rc file, so the probe cannot
# simply be cached for the process lifetime: the wizard would keep showing the
# PATH hint after the terminal was fixed.
#
# A TTL is the wrong instrument for that — short enough to feel live means
# re-spawning a login shell every couple of seconds forever (setup_status used
# to additionally clear this cache on every call, which made every single poll
# pay the 0.74s). Instead, invalidate on the thing that actually changes: the
# files a login shell reads. A handful of stat() calls per poll, and the answer
# still refreshes the moment the user saves their rc file.
_RC_CANDIDATES = (
    "~/.zshenv", "~/.zprofile", "~/.zshrc", "~/.zlogin",
    "~/.bash_profile", "~/.bash_login", "~/.bashrc", "~/.profile",
    "~/.config/fish/config.fish",
    "/etc/profile", "/etc/zshenv", "/etc/zprofile", "/etc/zshrc",
    "/etc/paths",
)


def _rc_fingerprint() -> tuple[object, ...]:
    """Identity of everything a login shell's PATH is built from.

    Existence is part of it (``None`` for a missing file), so *creating* a
    ``~/.zshrc`` invalidates too. ``$SHELL`` is included because changing it
    changes which of these files are read at all.
    """
    marks: list[object] = [os.environ.get("SHELL", "")]
    paths = [Path(p).expanduser() for p in _RC_CANDIDATES]
    # /etc/paths.d/* is a directory of fragments; each one contributes.
    paths.extend(sorted(Path("/etc/paths.d").glob("*")) if Path("/etc/paths.d").is_dir() else [])
    for path in paths:
        try:
            marks.append((str(path), path.stat().st_mtime_ns))
        except OSError:
            marks.append((str(path), None))
    return tuple(marks)


_terminal_path_cache: tuple[tuple[object, ...], str] | None = None
_terminal_path_lock = threading.Lock()


def clear_terminal_path_cache() -> None:
    """Drop the cached login-shell PATH probe (tests and forced refresh)."""
    global _terminal_path_cache
    with _terminal_path_lock:
        _terminal_path_cache = None


def _probe_terminal_path() -> str:
    """Spawn the user's login shell and read back its PATH. Expensive."""
    shell = os.environ.get("SHELL", "/bin/zsh")
    # Without PATH the shell builds its own from /etc/profile (path_helper) and
    # the user's rc files — which is the question being asked. Inheriting ours
    # would answer it wrongly: the engine prepends common_tool_dirs() to its
    # own PATH at startup (ciao/main.py), so ~/.local/bin would come back out
    # of the probe as if the user's terminal had it.
    env = {k: v for k, v in os.environ.items() if k != "PATH"}
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
            return out.split(_START, 1)[1].split(_END, 1)[0]
    except Exception:
        pass
    return ""


def terminal_path() -> str:
    """PATH the user's own interactive login shell reports, or "".

    Exactly what a command typed in Terminal would be resolved against — no
    fallback directories, no inherited process PATH. Telling someone to run a
    bare ``claude`` is only correct when *this* PATH resolves it, so the
    augmented :func:`login_shell_path` (which appends ``~/.local/bin`` and
    Homebrew whether or not the user's shell has them) cannot answer that
    question.

    Cached against :func:`_rc_fingerprint`, so an unchanged machine never pays
    for a second login shell and a saved rc file is picked up on the very next
    call. The probe runs under the lock (single-flight): concurrent wizard
    polls share one spawn instead of each starting their own, which is what
    happened when an rc file took longer to source than the poll interval.
    """
    global _terminal_path_cache
    fingerprint = _rc_fingerprint()
    with _terminal_path_lock:
        cached = _terminal_path_cache
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        # Deliberately inside the lock. It serialises wizard polls, but the
        # alternative — probing outside it — lets N concurrent callers spawn N
        # login shells, which is the cost this whole function exists to avoid.
        value = _probe_terminal_path()
        _terminal_path_cache = (fingerprint, value)
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
