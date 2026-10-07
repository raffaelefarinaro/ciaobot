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
and the bare command name would then match nothing — and then unwraps a command
wrapper. ``npm install -g`` writes ``opencode.cmd`` and ``opencode.ps1``
wrappers beside the real binary; spawning one runs ``cmd.exe``, which starts the
real program and only exits when it does, so the engine stops a wrapper rather
than the tool behind it. Any ``.cmd``/``.bat``/``.ps1`` is treated that way, in
any directory, because nvm-windows, Volta and a custom ``npm prefix`` all put
them somewhere other than ``%APPDATA%\\npm``. The wrapper names the program it
launches on its last argument line, so that is what is read — a package whose
entry point is a script or an extensionless file launches an interpreter, and
naming ``node.exe`` in place of the tool would spawn node with no arguments, so
that raises instead. Nothing is guessed from the package layout, and a wrapper
whose executable is missing (or which launches no single executable) raises
:class:`ToolResolutionError` rather than passing off an unusable tool as one that
is not installed.

``resolve_command()`` is for the callers that spawn an npm-installed tool: the
argv prefix to run it, as a list, so its arguments never meet a shell. A real
executable is ``[exe]``, exactly ``resolve_executable()``. A wrapper whose launch
line hands a script to node (``@googleworkspace/cli``'s ``run.js``) is
``[node.exe, script]``: node found on the same PATH, the script read off the
launch line the same way, both required to exist. ``cmd.exe`` is never the
answer, because it would split an unquoted path at a space and rewrite ``&``,
``%`` and quotes in the tool's arguments. On POSIX it is ``[shutil.which]``.

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
import sysconfig
import threading
from pathlib import Path
from typing import Iterable

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

    Each value is split on ``;``, every entry trimmed, unquoted and expanded,
    empties dropped and duplicates kept at their first position — the order and
    the substitutions Windows applies to a new logon's PATH. Duplicates are
    compared the way the OS compares them, so ``C:\\Tools`` twice with different
    case is one directory; ``normcase`` is the identity on POSIX, where the two
    really would be different.
    """
    entries: list[str] = []
    seen: set[str] = set()
    for value in values:
        for raw in value.split(";"):
            entry = _expand_vars(raw.strip().strip('"'))
            if not entry:
                continue
            key = os.path.normcase(entry)
            if key not in seen:
                seen.add(key)
                entries.append(entry)
    return ";".join(entries)


def engine_bin_dir() -> str:
    """The directory holding the ``ciao`` entry point of the running engine.

    ``sysconfig.get_path("scripts")``, deliberately not the resolved interpreter
    path and not the interpreter's own directory. On a venv the interpreter's
    directory *is* the scripts directory, but a **global** install keeps them
    apart: ``pip`` writes the console script to a sibling ``Scripts`` on Windows
    (``...\\Python\\3.12\\x64\\python.exe`` vs ``...\\Python\\3.12\\Scripts\\ciao.exe``),
    so ``Path(sys.executable).parent`` holds no ``ciao`` there and the
    engine-first PATH this feeds (:func:`prepend_engine_path`) would be inert.
    ``get_path`` is venv-aware and does not resolve symlinks, so on POSIX both a
    venv and a global install still yield the interpreter's own ``bin/``, never
    the venv's resolved base interpreter — where no ``ciao`` entry point lives.
    """
    return str(Path(sysconfig.get_path("scripts")))


def prepend_engine_path(path: str | None = None) -> str:
    """``path`` (or the process PATH) with :func:`engine_bin_dir` moved to the front.

    The user's other directories stay in their original order behind the engine's
    bin dir, and the engine dir is not duplicated if it is already first. The
    agent harness inherits this PATH, so a ``ciao <command>`` the agent runs
    resolves to the engine that launched the turn rather than to a stale install
    (for example an older uv-tool ``ciao``) earlier on the user's PATH.
    """
    current = os.environ.get("PATH", "") if path is None else path
    entries = [entry for entry in current.split(os.pathsep) if entry]
    bin_dir = engine_bin_dir()
    remaining = [
        entry
        for entry in entries
        if os.path.normcase(entry) != os.path.normcase(bin_dir)
    ]
    # Always exactly one engine entry, first: an already-first dir keeps its
    # place and a repeated one elsewhere is not duplicated.
    return os.pathsep.join([bin_dir, *remaining])


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
    doubled, a token written with none gets one, and the rest is spelled with
    THIS OS's separator — the text came out of a Windows file, so it is full of
    backslashes that are not separators on a POSIX test run.
    """
    match = _SHIM_DIR.match(path)
    if match is None:
        return path
    tail = path[match.end() :].lstrip("\\/").replace("\\", _SEP)
    return base + _SEP + tail


# Everything the shim quotes on one line, judged by extension where needed.
_QUOTED_PATH = re.compile(r'"([^"\r\n]+)"')
_EXTENSION = re.compile(r"\.[A-Za-z0-9]+$")

# A shim launches its tool on ONE line, and that line is the last one carrying
# the argument list: `%*` in a .cmd/.bat, `$args` in a .ps1. Everything else in
# the file is setup — `SET dp0=%~dp0`, `SET "_prog=%dp0%\node.exe"`, an existence
# check — and a path quoted in there says nothing about what is run. Scanning
# the whole file for quoted `.exe` paths read npm's own script-package shim as
# `node.exe`, which is the interpreter rather than the tool.
_LAUNCH_ARGUMENTS = {".cmd": "%*", ".bat": "%*", ".ps1": "$args"}

# The one executable a shim may legitimately hand back is never node itself: it
# is the tool's own binary. `node.exe` beside an entry point is an interpreter
# being handed a script, and resolving to it would spawn node with no arguments.
_NODE = "node.exe"


def _suffix_of(path: str) -> str:
    match = _EXTENSION.search(path)
    return match.group(0).lower() if match else ""


def _shim_launch_program(shim: Path) -> str:
    """The quoted program token on the shim's launch line, or "".

    The first quoted token on that line, which is the program npm runs; a later
    one is the entry point or an argument it is given.
    """
    marker = _LAUNCH_ARGUMENTS.get(shim.suffix.lower())
    if marker is None:
        return ""
    try:
        text = shim.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in reversed(text.splitlines()):
        if marker in line:
            tokens = _QUOTED_PATH.findall(line)
            return tokens[0].strip() if tokens else ""
    return ""


def _shim_executable(shim: Path) -> str:
    """The executable an npm shim launches, read out of the shim, or "".

    "" means the shim does not launch a single executable Ciaobot can run: its
    program is an interpreter (a script package, an extensionless entry point),
    it names nothing at all, or the file cannot be read. Nothing is inferred
    from where the shim sits or from the package layout.
    """
    program = _shim_launch_program(shim)
    if _suffix_of(program) != ".exe":
        return ""
    target = _expand_shim_dir(program, str(shim.parent))
    if _suffix_of(target) != ".exe" or os.path.basename(target).lower() == _NODE:
        return ""
    return target


# How a wrapper names node as its program: npm's .cmd templates use `_prog`
# (its own node.exe, or plain `node`) or a bare `node` before the script, its
# .ps1 template `node$exe` (`$basedir/node$exe` when node sits beside it).
_NODE_PROGRAMS = {"%_prog%", "node", _NODE, "node$exe"}


def _shim_node_script(shim: Path) -> str:
    """The script a wrapper hands to node on its launch line, or "".

    "" unless that line's program is node and a quoted script follows it; the
    script is resolved against the wrapper's own directory, as
    :func:`_shim_executable` resolves an executable.
    """
    marker = _LAUNCH_ARGUMENTS.get(shim.suffix.lower())
    if marker is None:
        return ""
    try:
        text = shim.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in reversed(text.splitlines()):
        if marker not in line:
            continue
        quoted = [token.strip() for token in _QUOTED_PATH.findall(line)]
        head = line.split('"', 1)[0].split()
        if head and head[-1].lower() in _NODE_PROGRAMS:
            program, rest = head[-1], quoted
        elif quoted:
            program, rest = quoted[0], quoted[1:]
        else:
            return ""
        name = program.replace("\\", "/").rsplit("/", 1)[-1].lower()
        if program.lower() not in _NODE_PROGRAMS and name not in _NODE_PROGRAMS:
            return ""
        # A .ps1 spells its paths with "/"; normpath gives this OS's form.
        return os.path.normpath(_expand_shim_dir(rest[0], str(shim.parent))) if rest else ""
    return ""


def _npm_bin_dir() -> Path | None:
    """The global bin dir ``npm install -g`` writes its shims into, or None.

    ``%APPDATA%\\npm`` is where a default npm install puts them. None means the
    variable is not set, so there is no such directory to look in.
    """
    appdata = os.environ.get("APPDATA", "")
    return Path(appdata) / "npm" if appdata else None


def _unwrap_shim(cmd: str, found: str) -> str:
    """The executable behind a command wrapper, read out of the wrapper itself.

    Raises :class:`ToolResolutionError` naming what was found and what it did not
    lead to. The alternative — handing a caller the ``.cmd``, which Windows runs
    through ``cmd.exe`` — is the bug this exists to fix: the engine would then
    stop a wrapper instead of the program behind it.
    """
    shim = Path(found)
    if shim.suffix.lower() not in _LAUNCH_ARGUMENTS:
        return found
    target = _shim_executable(shim)
    if not target:
        program = _shim_launch_program(shim)
        launched = f"{program!r} through an interpreter" if program else "no program at all"
        raise ToolResolutionError(
            f"{found} is the wrapper for {cmd!r} and launches {launched}, so there "
            "is no single executable to run. Install the tool again to repair it."
        )
    if not os.path.isfile(target):
        raise ToolResolutionError(
            f"{found} is the wrapper for {cmd!r} and launches {target}, which is "
            "not there. Install the tool again to repair it."
        )
    return target


# The PATHEXT Windows itself substitutes for a process whose environment has
# none — CreateProcess' documented default, not a compatibility shim, and the
# same list `shutil.which` falls back to.
_DEFAULT_PATHEXT = (".COM", ".EXE", ".BAT", ".CMD")


def _pathext_suffixes() -> list[str]:
    """The suffixes a command name may carry on this machine's PATH."""
    suffixes = [
        "." + part.strip().lstrip(".")
        for part in os.environ.get("PATHEXT", "").split(";")
        if part.strip()
    ]
    return suffixes or list(_DEFAULT_PATHEXT)


def _search_pathext(cmd: str, path: str) -> str | None:
    """The first file named ``cmd`` on ``path``, honouring ``PATHEXT``.

    ``shutil.which`` does this too, but it decides from ``sys.platform`` inside
    the stdlib, so on POSIX CI it searches for the bare name only and the suffix
    order is never exercised. The order here is the documented one: within a
    directory, the name as written when it already carries a PATHEXT suffix,
    then each suffix in PATHEXT order. Only ``path`` is searched — the current
    directory never is, so a file dropped into a workspace cannot be spawned as
    a tool.
    """
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


# A literal `sys.platform` check, not a module constant: mypy only narrows the
# platform-specific stubs (here `winreg`) on the literal form.
if sys.platform == "win32":
    import logging

    import winreg

    logger = logging.getLogger(__name__)

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
        except OSError as exc:
            # A key nobody can read is a PATH we cannot see, not a PATH that is
            # empty; the wizard shows the difference between "claude is missing"
            # and "claude is there and we cannot look".
            logger.debug("os_support.tool_path: cannot read %s: %s", subkey, exc)
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

    def resolve_executable(cmd: str, *, path: str) -> str | None:
        """Absolute path to a runnable executable for ``cmd``, or None.

        Raises :class:`ToolResolutionError` when ``cmd`` is on PATH as a command
        wrapper that does not lead to a single executable, or whose executable is
        not there; see :func:`_unwrap_shim`.
        """
        found = _search_pathext(cmd, path)
        return None if found is None else _unwrap_shim(cmd, found)

    def resolve_command(cmd: str, *, path: str) -> list[str]:
        """The argv prefix that runs ``cmd``, or ``[]`` when it is not on PATH.

        ``[exe]`` for an executable or a wrapper around one; ``[node, script]``
        for a wrapper that hands a script to node. Raises
        :class:`ToolResolutionError` when a wrapper leads to neither, or its node
        or script is not there; never answers with the wrapper itself.
        """
        found = _search_pathext(cmd, path)
        if found is None:
            return []
        shim = Path(found)
        if shim.suffix.lower() not in _LAUNCH_ARGUMENTS or _shim_executable(shim):
            return [_unwrap_shim(cmd, found)]
        script = _shim_node_script(shim)
        if not script:
            _unwrap_shim(cmd, found)  # raises, naming what the wrapper launches
        if not os.path.isfile(script):
            raise ToolResolutionError(
                f"{found} is the wrapper for {cmd!r} and runs {script} with node, "
                "which is not there. Install the tool again to repair it."
            )
        node = resolve_executable("node", path=path)
        if not node:
            raise ToolResolutionError(
                f"{found} is the wrapper for {cmd!r} and runs {script} with node, "
                "but node is not on PATH."
            )
        return [node, script]

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

    def resolve_executable(cmd: str, *, path: str) -> str | None:
        """Absolute path to ``cmd`` on ``path``, or None. See the module docstring."""
        return shutil.which(cmd, path=path)

    def resolve_command(cmd: str, *, path: str) -> list[str]:
        """The argv prefix that runs ``cmd``: ``[shutil.which]``, or ``[]``."""
        found = resolve_executable(cmd, path=path)
        return [found] if found else []
