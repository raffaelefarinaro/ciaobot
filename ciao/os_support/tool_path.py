"""Where the user's terminal finds their CLIs, and what a tool actually is.

``terminal_path()`` answers the question the setup wizard asks — "would a
command typed in this user's own terminal find ``claude``?" — and
``resolve_executable()`` answers the one every provider asks: an absolute path
to a file it can spawn.

A GUI app, LaunchServices or launchd starts the engine with a
stripped-down PATH (``/usr/bin:/bin:/usr/sbin:/sbin``) that omits Homebrew,
nvm's node bin and ``~/.local/bin``, so a tool installed with
``npm install -g`` or ``brew install`` works fine in the user's terminal and is
invisible to ``shutil.which`` in the server process. ``terminal_path()``
therefore spawns the user's interactive login shell and reads back the PATH it
built, and ``resolve_executable()`` is ``shutil.which``.

``resolve_command()`` is for the callers that spawn a tool: the argv prefix to
run it, as a list, so its arguments never meet a shell — ``[shutil.which]``, or
``[]``.
"""

from __future__ import annotations

import glob
import os
import shlex
import shutil
import subprocess
import sys
import sysconfig
import threading
from pathlib import Path, PureWindowsPath


def engine_bin_dir() -> str:
    """The directory holding the ``ciao`` entry point of the running engine.

    ``sysconfig.get_path("scripts")``, deliberately not the resolved interpreter
    path: ``get_path`` is venv-aware and does not resolve symlinks, so both a
    venv and a global install yield the interpreter's own ``bin/``, never the
    venv's resolved base interpreter — where no ``ciao`` entry point lives.
    """
    return str(Path(sysconfig.get_path("scripts")))


def dedupe_path(path: str) -> str:
    """``path`` with each directory once, at its first position, empties dropped.

    Directories are compared the way the OS compares them (``normcase``, which is
    the identity on POSIX). Every place that composes a PATH to hand on, to a child
    process or to a service definition, goes through here, so a directory the
    inherited value already repeats is never multiplied by the next composition.
    """
    entries: list[str] = []
    seen: set[str] = set()
    for entry in path.split(os.pathsep):
        if not entry:
            continue
        key = os.path.normcase(entry)
        if key not in seen:
            seen.add(key)
            entries.append(entry)
    return os.pathsep.join(entries)


def prepend_engine_path(path: str | None = None) -> str:
    """``path`` (or the process PATH) with :func:`engine_bin_dir` moved to the front.

    The user's other directories stay in their original order behind the engine's
    bin dir, and the engine dir is not duplicated if it is already first. The
    agent harness inherits this PATH, so a ``ciao <command>`` the agent runs
    resolves to the engine that launched the turn rather than to a stale install
    (for example an older uv-tool ``ciao``) earlier on the user's PATH.
    """
    current = os.environ.get("PATH", "") if path is None else path
    # The engine dir is first, so the dedupe keeps it there and drops any later
    # copy of it; the rest keeps its order, one occurrence each.
    return dedupe_path(os.pathsep.join([engine_bin_dir(), current]))


def shell_path_export(directory: str, *, windows: bool) -> str:
    """The POSIX shell line that puts ``directory`` first on PATH, quoted.

    For bash and zsh, and for Git Bash on Windows (Claude Code's shell there), a
    Windows directory is written the way Git Bash spells it (``C:\\x\\Scripts``
    -> ``/c/x/Scripts``): a drive colon would otherwise split the PATH entry in
    two. A Windows agent whose shell is PowerShell (Codex) or cmd uses
    :func:`powershell_path_export` or its own syntax instead.
    """
    if windows:
        win = PureWindowsPath(directory)
        drive = win.drive
        if len(drive) == 2 and drive.endswith(":"):
            directory = "/" + "/".join([drive[0].lower(), *win.parts[1:]])
        else:
            directory = win.as_posix()
    return f'export PATH={shlex.quote(directory)}:"$PATH"'


def powershell_path_export(directory: str) -> str:
    """The PowerShell line that puts ``directory`` first on PATH, single-quoted."""
    quoted = "'" + directory.replace("'", "''") + ";'"
    return f"$env:PATH = {quoted} + $env:PATH"


def engine_powershell_export() -> str:
    """:func:`powershell_path_export` for the engine's bin dir on Windows; ``""`` elsewhere."""
    return powershell_path_export(engine_bin_dir()) if sys.platform == "win32" else ""


def engine_path_export() -> str:
    """The shell line that makes ``ciao`` in an agent's shell the running engine's.

    The engine already starts the agent with :func:`prepend_engine_path`, but
    the agent's shell sources the user's rc files, which can put a stale
    ``ciao`` (an older uv-tool install in ``~/.local/bin``) back in front. The
    context capsule and the care prompt hand the agent this line to run first.
    """
    return shell_path_export(engine_bin_dir(), windows=sys.platform == "win32")


# Markers wrap the printed PATH so noisy shell rc files (which may echo
# banners to stdout) don't corrupt the value we extract.
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
        # The documented OpenCode curl installer drops the binary here,
        # beside the credentials, and it is on no default macOS PATH.
        str(home / ".opencode" / "bin"),
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
    zdotdir = os.environ.get("ZDOTDIR", "")
    marks: list[object] = [os.environ.get("SHELL", ""), zdotdir]
    paths = [Path(p).expanduser() for p in _RC_CANDIDATES]
    # With ZDOTDIR set, zsh reads $ZDOTDIR/.zshrc and friends INSTEAD of the
    # ~/.z* files above, so without these the fingerprint is blind to the only
    # file such a user would ever edit: the wizard's PATH hint would then never
    # clear until the engine restarts.
    if zdotdir:
        paths.extend(
            Path(zdotdir).expanduser() / name
            for name in (".zshenv", ".zprofile", ".zshrc", ".zlogin")
        )
    # /etc/paths.d/* is a directory of fragments; each one contributes.
    try:
        paths.extend(sorted(Path("/etc/paths.d").glob("*")) if Path("/etc/paths.d").is_dir() else [])
    except OSError:
        marks.append(("/etc/paths.d", None))
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
            text=True, encoding="utf-8",
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
    augmented :func:`ciao.tool_path.login_shell_path` (which appends
    ``~/.local/bin`` and Homebrew whether or not the user's shell has them)
    cannot answer that question.

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

def resolve_executable(cmd: str, *, path: str) -> str | None:
    """Absolute path to ``cmd`` on ``path``, or None. See the module docstring."""
    return shutil.which(cmd, path=path)

def resolve_command(cmd: str, *, path: str) -> list[str]:
    """The argv prefix that runs ``cmd``: ``[shutil.which]``, or ``[]``."""
    found = resolve_executable(cmd, path=path)
    return [found] if found else []
