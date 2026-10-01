"""Where the user's terminal finds their CLIs, and what a tool actually is.

``terminal_path()`` answers the question the setup wizard asks — "would a
command typed in this user's own terminal find ``claude``?" — and
``resolve_executable()`` answers the one every provider asks: an absolute path
to a file it can spawn.

**POSIX.** A GUI app, LaunchServices or launchd starts the engine with a
stripped-down PATH (``/usr/bin:/bin:/usr/sbin:/sbin``) that omits Homebrew,
nvm's node bin and ``~/.local/bin``, so a tool installed with
``npm install -g`` or ``brew install`` works fine in the user's terminal and is
invisible to ``shutil.which`` in the server process. ``terminal_path()``
therefore spawns the user's interactive login shell and reads back the PATH it
built, and ``resolve_executable()`` is ``shutil.which``.

**Windows.** There is no login shell to spawn, and the PATH a new logon gets is
not inherited from whatever started the process either: it is built from the
registry — ``HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\
Environment`` and then ``HKCU\\Environment``, machine before user, with
``%VARS%`` expanded. A process started from Explorer, a shortcut or a scheduled
task keeps the PATH its own parent had, which is how a tool the user can run by
name ends up invisible to the engine. ``terminal_path()`` reads those two values
with ``winreg`` and spawns nothing; two registry opens cost less than the login
shell they replace by four orders of magnitude, so the answer is not cached and
the wizard's PATH hint goes live the moment the user edits their environment.

``resolve_executable()`` honours ``PATHEXT`` — a service does not inherit one,
and the bare command name would then match nothing — and then unwraps an npm
shim. ``npm install -g`` writes ``opencode.cmd`` and ``opencode.ps1`` wrappers
beside the real binary; spawning one runs ``cmd.exe``, which starts the real
program and only exits when it does, so the engine stops a wrapper rather than
the tool behind it. The shim names its own target, so that is what is read:
nothing is guessed from the package layout, and a shim whose target is missing
(or which is not a shim at all) raises :class:`ToolResolutionError` rather than
passing off an unusable tool as one that is not installed.

The helpers below the split are the same code both branches run, defined once
rather than inside either of them: the PATHEXT search, the shim reader and the
registry join are the interesting logic here, and hiding them in a branch would
leave them unexercised on the POSIX CI that gates every change.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Iterable

_WINDOWS = sys.platform == "win32"

# The separator this OS joins path parts with, captured once: the shim reader
# below splices it into a path it assembles from text a Windows file wrote.
# Read from the running OS rather than hardcoded, so the reader is the same
# code whichever OS runs the tests.
_SEP = os.sep


class ToolResolutionError(OSError):
    """A tool was found on PATH but is not an executable Ciaobot can run.

    An ``OSError`` because that is what it is — a path on disk that cannot be
    executed. Callers that only want to know whether a tool is usable already
    treat an ``OSError`` as "not available" (``providers.opencode`` answering a
    model list, the wizard's PATH and auth hints), so this reports a broken npm
    install through the same door as a missing binary instead of needing a new
    one at every call site.
    """


# ── shared by both branches ────────────────────────────────────────────────

# Windows expands a PATH entry's %VARS% when it assembles a logon's PATH. Only
# %NAME% is expanded, as RegExpandString does: $NAME is ordinary text in a
# Windows PATH. A name that is not set is left exactly as it is, which is what
# the OS does too — the entry stays broken and stays visible.
_VAR = re.compile(r"%([^%]+)%")


def _expand_vars(entry: str) -> str:
    """``%VARS%`` in one PATH entry, expanded against this process's environment.

    ``os.environ`` on Windows is case-insensitive, so ``%SystemRoot%`` and
    ``%SYSTEMROOT%`` both resolve against the one variable the machine set.
    """
    return _VAR.sub(lambda m: os.environ.get(m.group(1), m.group(0)), entry)


def _join_path_sources(values: Iterable[str]) -> str:
    """One PATH string from the raw per-scope values, in the order given.

    Each value is split on ``;``, every entry trimmed and expanded, empties
    dropped and duplicates kept at their first position — the order and the
    substitutions Windows applies to a new logon's PATH.
    """
    entries: list[str] = []
    for value in values:
        for raw in value.split(";"):
            entry = _expand_vars(raw.strip())
            if entry and entry not in entries:
                entries.append(entry)
    return ";".join(entries)


# npm writes the shim's own directory into the wrapper it generates, spelled
# three ways across its templates, all of them still in use: the modern shim
# `SET`s a variable and references it (`SET dp0=%~dp0` then
# `"%dp0%\node_modules\…"`), its `cmd-shim` template interpolates the argument
# with no separator (`"%~dp0node.exe"`), and a .ps1 shim uses `$basedir`.
# Ordered longest-first, so `%dp0%` is never read as a bare `%dp0`.
_SHIM_DIR = re.compile(r"^(?:%~dp0%|%dp0%|%~dp0|%dp0|\$basedir)", re.IGNORECASE)


def _expand_shim_dir(path: str, base: str) -> str:
    """One quoted path out of an npm shim, with the shim's own directory in it
    resolved to ``base``.

    Whatever separator npm wrote after the token is absorbed rather than
    doubled, and a token written with none gets one, so
    ``%dp0%\\node_modules\\pkg\\bin.exe``, ``%~dp0\\node.exe`` and
    ``%~dp0node.exe`` all come out as one path. A path that names no token is
    already absolute and is left alone.
    """
    match = _SHIM_DIR.match(path)
    if match is None:
        return path
    return base + _SEP + path[match.end() :].lstrip("\\/")


# Everything the shim quotes is a path it uses; the reader below only decides
# what to do with them, so they are read once and judged by extension.
_QUOTED_PATH = re.compile(r'"([^"\r\n]+)"')
_EXTENSION = re.compile(r"\.[A-Za-z0-9]+$")

# A shim that hands a script to an interpreter names no executable for the tool:
# `node.exe` without the entry point beside it is not the CLI, and resolving to
# it would spawn node with no arguments at all.
_SCRIPT_SUFFIXES = (".js", ".mjs", ".cjs")


def _suffix_of(path: str) -> str:
    match = _EXTENSION.search(path)
    return match.group(0).lower() if match else ""


def _shim_executable(shim: Path) -> str:
    """The executable an npm shim launches, read out of the shim, or "".

    "" means the file does not name one this can run: it is not an npm shim, it
    passes a ``.js`` entry point to an interpreter, or it quotes no executable at
    all. Nothing is inferred from where the shim sits or from the package layout.
    """
    try:
        text = shim.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    quoted = _QUOTED_PATH.findall(text)
    if any(_suffix_of(q) in _SCRIPT_SUFFIXES for q in quoted):
        return ""
    for candidate in reversed(quoted):
        if _suffix_of(candidate) == ".exe":
            return _expand_shim_dir(candidate, str(shim.parent)).strip()
    return ""


def _comparable(path: Path) -> str:
    """A path spelled for comparison: on Windows neither separators nor case
    distinguish two directories, and a registry value is spelled its own way."""
    return str(path).replace("/", "\\").lower()


# What npm writes beside a global install, and the wrappers it writes it as.
_NPM_SHIM_SUFFIXES = (".cmd", ".bat", ".ps1")


def _npm_bin_dir() -> Path | None:
    """The global bin dir ``npm install -g`` writes its shims into, or None.

    ``%APPDATA%\\npm`` is where a default npm install puts them. None means the
    variable is not set, so there is no such directory to look in.
    """
    appdata = os.environ.get("APPDATA", "")
    return Path(appdata) / "npm" if appdata else None


def _is_npm_shim(path: Path) -> bool:
    """``path`` is a wrapper ``npm install -g`` left in the npm bin dir.

    Restricted to that directory on purpose: an extension alone says nothing
    about what a file runs, and only npm's own wrappers can be read as one.
    """
    if path.suffix.lower() not in _NPM_SHIM_SUFFIXES:
        return False
    npm_dir = _npm_bin_dir()
    return npm_dir is not None and _comparable(path.parent) == _comparable(npm_dir)


def _unwrap_npm_shim(cmd: str, found: str) -> str:
    """The executable behind an npm shim, read out of the shim itself.

    Raises :class:`ToolResolutionError` naming what was found and what it did
    not lead to, because the alternative — handing a caller a wrapper that
    cannot be stopped on its own, or reporting nothing at all — is what the
    engine would then spawn.
    """
    shim = Path(found)
    if not _is_npm_shim(shim):
        return found
    target = _shim_executable(shim)
    if not target:
        raise ToolResolutionError(
            f"{found} is an npm shim for {cmd!r} that names no executable: it "
            "launches the program through an interpreter, so there is no binary "
            "to run. Install the tool again to repair it."
        )
    if not os.path.isfile(target):
        raise ToolResolutionError(
            f"{found} is the npm shim for {cmd!r} and launches {target}, which "
            "is not there. Install the tool again to repair it."
        )
    return target


# The PATHEXT a Windows install ships with. Used only when the process has none
# of its own, as `shutil.which` does for the same reason.
_DEFAULT_PATHEXT = (".COM", ".EXE", ".BAT", ".CMD")


def _pathext_suffixes() -> list[str]:
    """The suffixes a command name may carry on this machine's PATH."""
    suffixes = [
        "." + part.strip().lstrip(".")
        for part in os.environ.get("PATHEXT", "").split(";")
        if part.strip()
    ]
    return suffixes or list(_DEFAULT_PATHEXT)


def _search_pathext(cmd: str, path: str | None) -> str | None:
    """The first file named ``cmd`` on ``path``, honouring ``PATHEXT``.

    ``shutil.which`` does this too, but it decides from ``sys.platform`` inside
    the stdlib, so on POSIX CI it searches for the bare name only and the suffix
    order is never exercised. The order here is the documented one: within a
    directory, the name as written when it already carries a PATHEXT suffix,
    then each suffix in PATHEXT order. Only ``path`` is searched — the current
    directory never is, so a file dropped into a workspace cannot be spawned as
    a tool.
    """
    if path is None:
        return shutil.which(cmd)
    if not path:
        return None
    suffixes = _pathext_suffixes()
    names = [cmd] if cmd.lower().endswith(tuple(s.lower() for s in suffixes)) else []
    names += [cmd + suffix for suffix in suffixes]
    for directory in path.split(os.pathsep):
        if not directory:
            continue
        for name in names:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                return candidate
    return None


if _WINDOWS:
    import winreg

    # The two places a new logon's PATH is written, in the order Windows
    # combines them: the machine value first, then the user's, so a directory
    # both name is taken from the machine half.
    _MACHINE_ENVIRONMENT = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
    _USER_ENVIRONMENT = "Environment"
    _PATH_SOURCES = (
        (winreg.HKEY_LOCAL_MACHINE, _MACHINE_ENVIRONMENT),
        (winreg.HKEY_CURRENT_USER, _USER_ENVIRONMENT),
    )

    def _registry_path(hive: int, subkey: str) -> str:
        """The raw ``Path`` value from one Environment key, or "" when there is none.

        Unexpanded on purpose: the machine value is REG_EXPAND_SZ and
        ``QueryValueEx`` hands back the ``%VARS%`` as written, which is what
        :func:`_expand_vars` is for. A value of any other type is not a PATH.
        """
        try:
            with winreg.OpenKey(hive, subkey) as key:
                value, _kind = winreg.QueryValueEx(key, "Path")
        except OSError:
            return ""
        return value if isinstance(value, str) else ""

    def terminal_path() -> str:
        """The PATH a new logon on this machine gets, or "" when it cannot be read.

        Machine entries first, then the user's. Nothing from this process's own
        PATH is mixed in, for the reason the POSIX branch gives: the engine
        prepends :func:`common_tool_dirs` to it at startup, so an inherited
        answer would claim a directory the user's own terminal does not have.
        """
        return _join_path_sources(
            _registry_path(hive, subkey) for hive, subkey in _PATH_SOURCES
        )

    def clear_terminal_path_cache() -> None:
        """Nothing to clear: the registry is read on every call.

        The POSIX branch caches a login-shell spawn and needs a way to drop it.
        Reading two registry values costs less than a millisecond, so caching
        them would only risk answering with a PATH the user has just edited.
        """

    def common_tool_dirs() -> list[str]:
        """Directories a Windows tool install lands in that a PATH may not carry.

        A new logon's PATH is assembled from the registry, so unlike launchd's
        it is normally complete and this list repairs far less than the POSIX
        one. It names where the CLIs Ciaobot resolves actually go when an
        installer's PATH edit has not reached the running process: npm's global
        shims (``opencode``, and anything else installed with
        ``npm install -g``), WinGet's links (``uv``), and the documented Claude
        installer's ``%USERPROFILE%\\.local\\bin``.
        """
        local = os.environ.get("LOCALAPPDATA", "")
        npm_dir = _npm_bin_dir()
        dirs = [str(Path.home() / ".local" / "bin")]
        if npm_dir is not None:
            dirs.append(str(npm_dir))
        if local:
            dirs.append(str(Path(local) / "Microsoft" / "WindowsApps"))
        return dirs

    def resolve_executable(cmd: str, *, path: str | None = None) -> str | None:
        """Absolute path to a runnable executable for ``cmd``, or None.

        Raises :class:`ToolResolutionError` when ``cmd`` is on PATH as an npm
        shim whose real executable is not there; see :func:`_unwrap_npm_shim`.
        """
        found = _search_pathext(cmd, path)
        return None if found is None else _unwrap_npm_shim(cmd, found)

else:
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

    def resolve_executable(cmd: str, *, path: str | None = None) -> str | None:
        """Absolute path to ``cmd`` on ``path``, or None. See the module docstring."""
        return shutil.which(cmd, path=path)