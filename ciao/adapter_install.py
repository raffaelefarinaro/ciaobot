"""The ACP adapters Ciaobot runs, from a machine-wide copy at the pin or installed on demand (#3425).

Nothing agent-related ships in the engine. An adapter is a pinned npm package. When the machine
already has the pinned version on its PATH, Ciaobot uses that copy and installs nothing. Otherwise
the adapter lives in a private prefix outside the app bundle, so an app update does not remove it.
The adapter drives the user's own ``claude`` or ``codex`` CLI, which Ciaobot never installs or
bundles. The pins below are the only copy of the adapter versions: the Settings install lines, the
startup install and the status probes all read them here.

Each managed adapter has its own folder in the prefix (``<prefix>/<name>/``) holding the lockfile
shipped in ``ciao/stock/adapters/<name>/``. The install is ``npm ci`` against that lockfile, so
every package is pinned by integrity hash, with ``--ignore-scripts``, ``--omit=optional``, an argv
list and no shell. npm inherits the adapter's lock, so the lock lasts as long as npm does, even
when the engine that started it has exited. A successful install writes a marker that names the
pin and the lockfile hash; an adapter without a matching marker is not ready, so a half-finished
tree never runs.

An adapter starts as ``[node, <entry>]`` with Node resolved by absolute path, so the
``#!/usr/bin/env node`` shebang does not depend on PATH. A launch takes the adapter's lock shared,
so launches overlap and only an install excludes them.

Run as ``python -I -m ciao.adapter_install <name>``, this module is also the Settings terminal line:
it takes the same lock and writes the same marker as the engine does.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import logging
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from ciao.os_support.processes import isolated_probe_flags
from ciao.redact import redact_text
from ciao.tool_path import resolve_tool, terminal_path

logger = logging.getLogger(__name__)

CLAUDE_ADAPTER = "claude-agent-acp"
CODEX_ADAPTER = "codex-acp"

if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    import msvcrt
else:
    import fcntl


@dataclass(frozen=True)
class AdapterPin:
    package: str
    version: str
    cli: str  # the user's CLI this adapter drives: "claude" or "codex"


ADAPTERS: dict[str, AdapterPin] = {
    CLAUDE_ADAPTER: AdapterPin("@agentclientprotocol/claude-agent-acp", "0.108.1", "claude"),
    # codex-acp uses the codex CLI named by CODEX_PATH; it does not install @openai/codex itself.
    CODEX_ADAPTER: AdapterPin("@agentclientprotocol/codex-acp", "2.7.0", "codex"),
}
# The codex CLI version codex-acp 2.7.0 is built against. The Settings terminal installs it only
# when no codex resolves; Ciaobot never installs a CLI itself.
CODEX_CLI_INSTALL = "npm install -g @openai/codex@0.166.0"
# Provider id to the adapter its chats run through.
PROVIDER_ADAPTERS: dict[str, str] = {"claude": CLAUDE_ADAPTER, "codex": CODEX_ADAPTER}

NODE_MIN_MAJOR = 22
INSTALL_TIMEOUT_S = 300
# A launch on Windows has no shared lock, so it waits this long for a brief exclusive holder.
_WINDOWS_LAUNCH_WAIT_S = 1.0
NPM_CI_FLAGS = ("ci", "--ignore-scripts", "--omit=optional", "--no-audit", "--no-fund")
# npm's own environment, from the engine's environment. Everything else is dropped, so provider
# keys, tokens and NODE_OPTIONS never reach npm.
_NPM_ENV_KEYS = (
    "PATH", "HOME", "USERPROFILE", "TMPDIR", "LANG", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    # Windows needs these for npm to start at all.
    "SYSTEMROOT", "APPDATA", "LOCALAPPDATA", "PATHEXT", "TEMP", "TMP", "COMSPEC",
    "ProgramFiles", "ProgramData", "SystemDrive", "windir",
)
_NPM_CONFIG_PREFIXES = ("npm_config_",)
# errno values that mean "another holder has the lock" (anything else is a real failure).
if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    _LOCK_HELD = frozenset({errno.EACCES, errno.EDEADLOCK})
else:
    _LOCK_HELD = frozenset({errno.EAGAIN, errno.EWOULDBLOCK})
# How far up from a machine-wide adapter's bin to look for its package.json.
_PACKAGE_SEARCH_DEPTH = 6

# A state is the first failing check, in this order, or "ready".
STATES = ("cli_missing", "node_missing", "node_old", "adapter_missing", "adapter_outdated", "ready")

# Guards the in-process "attempted" and "failure" records. Installs themselves are serialized by
# the per-adapter file lock, so Claude and Codex installs do not wait on each other.
_STATE_LOCK = threading.Lock()
# Windows has no shared file lock, so launches in this process take this lock around their read.
_LAUNCH_LOCK = threading.Lock()
_ATTEMPTED: set[str] = set()
_FAILURES: dict[str, str] = {}
_NODE_MAJOR: dict[tuple[str, int], int] = {}
_NPM_AUTH = re.compile(r"(?i)(_auth(?:token)?\s*=\s*)\S+")
_NPM_TOKEN = re.compile(r"npm_[A-Za-z0-9]{8,}")
# npm's pointer to its debug log, and the log's own file name: neither says why the install failed.
_NPM_NOISE = ("A complete log of this run can be found in", "_logs")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class AdapterNotReady(RuntimeError):
    """The adapter cannot start yet. The message is safe to show."""


class AdapterInstallError(RuntimeError):
    """An install did not finish. The message is redacted and safe to show."""


class _UnsafePrefix(RuntimeError):
    """The private prefix is not a folder this user alone may put code in."""


def _windows() -> bool:
    return sys.platform == "win32"


def adapter_prefix() -> Path:
    """The private prefix the adapters live in. Not an env var: a fixed location per platform."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Ciaobot" / "adapters"
    from ciao.install_receipt import default_receipt_path

    return default_receipt_path().parent / "adapters"


def absolute_executable(path: str | None) -> str | None:
    """``path`` when it is an absolute, executable file in a folder others cannot write; else ``None``.

    A relative path would resolve against the working directory of the process that runs it, so
    it is refused here, for every executable the adapters are handed. So is a file whose folder
    is writable by its group or by others, since anyone there could replace it.
    """
    if not path or not os.path.isabs(path):
        return None
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        return None
    if not _windows() and os.stat(os.path.dirname(path)).st_mode & 0o022:
        return None
    return path


def _cli_path(name: str) -> str | None:
    if ADAPTERS[name].cli == "claude":
        from ciao.setup_status import claude_cli_path

        return absolute_executable(claude_cli_path() or None)
    from ciao.providers.acp.agents import codex_binary

    return absolute_executable(codex_binary())


def _node_path() -> str | None:
    return absolute_executable(resolve_tool("node"))


def _node_major(node: str) -> int | None:
    """The major version of ``node``, cached on its path and mtime, so an upgrade is re-probed."""
    try:
        key = (node, os.stat(node).st_mtime_ns)
    except OSError:
        return None
    if key not in _NODE_MAJOR:
        major = _probe_node_major(node)
        if major is None:
            return None
        _NODE_MAJOR[key] = major
    return _NODE_MAJOR[key]


def _probe_node_major(node: str) -> int | None:
    try:
        out = subprocess.run(
            [node, "--version"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.match(r"\s*v?(\d+)\.", out)
    return int(match.group(1)) if match else None


def _stock_bytes(name: str, filename: str) -> bytes:
    return resources.files("ciao.stock").joinpath("adapters", name, filename).read_bytes()


def _lock_sha256(name: str) -> str:
    return hashlib.sha256(_stock_bytes(name, "package-lock.json")).hexdigest()


def _adapter_dir(name: str) -> Path:
    return adapter_prefix() / name


def _lock_path(name: str) -> Path:
    return adapter_prefix() / f".ciao-{name}.lock"


def _package_dir(name: str) -> Path | None:
    """``<prefix>/<name>/node_modules/<pkg>``, or ``None`` if any link in its path is a symlink.

    The prefix is resolved first; the package directory must then resolve to exactly that path.
    """
    expected = Path(os.path.realpath(adapter_prefix())) / name / "node_modules"
    expected = expected.joinpath(*ADAPTERS[name].package.split("/"))
    if os.path.realpath(expected) != str(expected):
        return None
    return expected


def _marker_path(name: str) -> Path:
    return adapter_prefix() / f".ciao-{name}.ok"


def _marker_ok(name: str) -> bool:
    try:
        data = json.loads(_marker_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        isinstance(data, dict)
        and data.get("version") == ADAPTERS[name].version
        and data.get("lock_sha256") == _lock_sha256(name)
    )


def _private_problem(path: Path) -> str | None:
    """Why ``path`` may not hold code we run, or ``None``. POSIX only: Windows has no owner bits."""
    if _windows():
        return None
    if path.is_symlink():
        return f"{path} is a symbolic link."
    if not path.exists():
        return None
    try:
        st = path.stat()
    except OSError:
        return f"{path} cannot be inspected."
    if st.st_uid != os.geteuid():
        return f"{path} is not owned by this user."
    if st.st_mode & 0o077:
        return f"{path} is open to other users."
    return None


def _prefix_problem(name: str) -> str | None:
    return _private_problem(adapter_prefix()) or _private_problem(_adapter_dir(name))


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)


def _package_json(pkg_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((pkg_dir / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _entry(name: str, pkg_dir: Path) -> Path | None:
    """The adapter's JS entry from its package.json ``bin``: inside the package, with no symlinks."""
    data = _package_json(pkg_dir)
    if data is None:
        return None
    bin_field = data.get("bin")
    rel = bin_field.get(name) if isinstance(bin_field, dict) else bin_field
    if not isinstance(rel, str) or not rel:
        return None
    entry = os.path.normpath(pkg_dir / rel)
    try:
        Path(entry).relative_to(pkg_dir)
    except ValueError:
        return None
    if os.path.realpath(entry) != entry or not os.path.isfile(entry):
        return None
    return Path(entry)


def _others_can_write(path: Path) -> bool:
    try:
        return bool(os.stat(path).st_mode & stat.S_IWOTH)
    except OSError:
        return True  # a folder we cannot inspect is not one we trust


def _owning_package_dir(real: str, package: str) -> Path | None:
    """The folder of the package whose package.json names ``package``, searching up from ``real``."""
    current = Path(real).parent
    for _ in range(_PACKAGE_SEARCH_DEPTH):
        data = _package_json(current)
        if data is not None and data.get("name") == package:
            return current
        if current.parent == current:
            break
        current = current.parent
    return None


def _global_adapter(name: str) -> tuple[str, Path] | None:
    """A machine-wide adapter at the pin that this engine may run, as ``(bin path, JS entry)``.

    The bin is found on the terminal PATH like any tool, and is used only when it is absolute and
    its real path lies in no world-writable folder, and its package.json names the pinned package
    at the pinned version. It is never modified, reinstalled or copied.
    """
    pin = ADAPTERS[name]
    bin_path = absolute_executable(resolve_tool(name))
    if bin_path is None:
        return None
    real = os.path.realpath(bin_path)
    if not _windows() and any(_others_can_write(folder) for folder in Path(real).parents):
        return None
    pkg_dir = _owning_package_dir(real, pin.package)
    if pkg_dir is None:
        return None
    data = _package_json(pkg_dir)
    if data is None or data.get("version") != pin.version:
        return None
    entry = _entry(name, pkg_dir)
    return None if entry is None else (bin_path, entry)


def in_use_path(name: str) -> str | None:
    """The machine-wide adapter this engine runs for ``name``, by its bin path, or ``None``."""
    found = _global_adapter(name)
    return found[0] if found else None


def _base_problem(name: str) -> str | None:
    """``cli_missing``, ``node_missing`` or ``node_old`` for the first failing check, else ``None``."""
    if _cli_path(name) is None:
        return "cli_missing"
    node = _node_path()
    if node is None:
        return "node_missing"
    major = _node_major(node)
    if major is None or major < NODE_MIN_MAJOR:
        return "node_old"
    return None


def _check(name: str, *, marker: bool) -> str:
    """The first failing check for ``name``, or ``"ready"``. ``marker=False`` skips the marker."""
    if (problem := _base_problem(name)) is not None:
        return problem
    if _global_adapter(name) is not None:
        return "ready"
    if _prefix_problem(name) is not None:
        return "adapter_missing"
    pkg_dir = _package_dir(name)
    if pkg_dir is None:
        return "adapter_missing"
    data = _package_json(pkg_dir)
    installed = data.get("version") if data else None
    if not isinstance(installed, str):
        return "adapter_missing"
    if installed != ADAPTERS[name].version:
        return "adapter_outdated"
    if _entry(name, pkg_dir) is None:
        return "adapter_missing"
    if marker and not _marker_ok(name):
        return "adapter_missing"
    return "ready"


def detect(name: str) -> str:
    """The first failing check for ``name``, or ``"ready"``. Reads files; spawns only node.

    A machine-wide adapter at the pin counts as ready with no private install. Otherwise the
    private tree must be complete, match the pin and carry a marker for the same lockfile.
    """
    return _check(name, marker=True)


def _try_lock(fd: int, *, shared: bool) -> bool:
    """True when the lock was taken. False only when another holder has it; other errors raise."""
    try:
        if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in _LOCK_HELD:
            return False
        raise
    return True


def _unlock(fd: int) -> None:
    if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _lock_held_elsewhere(name: str) -> bool:
    """True when an install holds the adapter's lock right now. Never creates the lock file.

    The probe is shared, so a launch's shared hold does not count: only an exclusive holder does.
    """
    try:
        fd = os.open(_lock_path(name), os.O_RDWR | _NOFOLLOW)
    except OSError:
        return False
    try:
        try:
            taken = _try_lock(fd, shared=True)
        except OSError:
            return False
        if taken:
            _unlock(fd)
        return not taken
    finally:
        os.close(fd)


@contextmanager
def _adapter_lock(name: str, *, shared: bool, wait_s: float) -> Iterator[int | None]:
    """Hold the adapter's file lock, waiting up to ``wait_s``. Yields the locked fd, or ``None``.

    The prefix is validated before anything is created in it, and the lock file is opened without
    following a symlink.
    """
    if (problem := _prefix_problem(name)) is not None:
        raise _UnsafePrefix(problem)
    _ensure_private_dir(adapter_prefix())
    fd = os.open(_lock_path(name), os.O_RDWR | os.O_CREAT | _NOFOLLOW, 0o600)
    taken = False
    try:
        deadline = time.monotonic() + wait_s
        while True:
            taken = _try_lock(fd, shared=shared)
            if taken or time.monotonic() >= deadline:
                break
            time.sleep(0.2)
        yield fd if taken else None
    finally:
        if taken:
            _unlock(fd)
        os.close(fd)


@contextmanager
def _launch_hold(name: str) -> Iterator[bool]:
    """A launch's hold on the adapter tree. Yields whether it was taken.

    On POSIX the hold is shared, so launches overlap and only an exclusive install excludes them.
    Windows has no shared file lock, so launches in this process serialise on a thread lock and
    take the exclusive lock briefly for the read.
    """
    if _windows():
        with _LAUNCH_LOCK, _adapter_lock(name, shared=False, wait_s=_WINDOWS_LAUNCH_WAIT_S) as fd:
            yield fd is not None
    else:
        with _adapter_lock(name, shared=True, wait_s=0) as fd:
            yield fd is not None


def describe(name: str, state: str) -> str:
    """One sentence for a Settings card or a startup phase, for a state from :func:`detect`."""
    pin = ADAPTERS[name]
    if state == "ready":
        if (used := in_use_path(name)) is not None:
            return f"The ACP adapter {name} {pin.version} is using the adapter found at {used}."
        return f"The ACP adapter {name} {pin.version} is the Ciaobot-managed adapter."
    if state == "adapter_missing" and (problem := _prefix_problem(name)):
        return f"The private adapters folder cannot be used: {problem}"
    if state in ("adapter_missing", "adapter_outdated") and _lock_held_elsewhere(name):
        return f"The ACP adapter {name} {pin.version} is being installed."
    text = {
        "cli_missing": f"The {pin.cli} CLI is not installed on this machine, so {name} cannot run.",
        "node_missing": f"Node {NODE_MIN_MAJOR} or newer is needed to run {name}, and none was found.",
        "node_old": f"Node {NODE_MIN_MAJOR} or newer is needed to run {name}.",
        "adapter_missing": f"The ACP adapter {name} {pin.version} is not installed for Ciaobot yet.",
        "adapter_outdated": f"The ACP adapter {name} is not the pinned version {pin.version}.",
    }.get(state, "")
    failure = _FAILURES.get(name)
    return f"{text} {failure}".strip() if failure and text else text


def launch_argv(name: str) -> list[str]:
    """``[node, <entry>]`` for a ready adapter. Raises :class:`AdapterNotReady` otherwise.

    A machine-wide adapter at the pin is used as it is, with no lock. A managed one is read under
    the adapter's shared lock, so a launch never reads a tree an install is writing.
    """
    if _base_problem(name) is None and (found := _global_adapter(name)) is not None:
        if (node := _node_path()) is not None:
            return [node, str(found[1])]
    try:
        with _launch_hold(name) as taken:
            if not taken:
                raise AdapterNotReady(f"The ACP adapter {name} is being installed. Try again in a moment.")
            state = _check(name, marker=True)
            node = _node_path()
            pkg_dir = _package_dir(name)
            entry = _entry(name, pkg_dir) if pkg_dir is not None else None
    except _UnsafePrefix as exc:
        raise AdapterNotReady(f"The private adapters folder cannot be used: {exc}") from None
    except OSError:
        raise AdapterNotReady(describe(name, "adapter_missing")) from None
    # Described after the lock is released, or describe() would report this launch's own lock.
    if state != "ready" or node is None or entry is None:
        raise AdapterNotReady(describe(name, state if state != "ready" else "adapter_missing"))
    return [node, str(entry)]


def install_command(name: str) -> str:
    """The terminal line that installs the pinned adapter.

    It runs this module with the engine's Python and the same isolation flags the engine uses, so
    a terminal started in a writable workspace cannot make ``python -m`` import a planted module
    from the current directory.
    """
    return shlex.join([sys.executable, *isolated_probe_flags(), "-m", "ciao.adapter_install", name])


def failure(name: str) -> str | None:
    """Why the last install in this process failed, or ``None``. Cleared by a successful install."""
    return _FAILURES.get(name)


def _npm_env(node: str) -> dict[str, str]:
    """npm's environment: an allowlist, with the node that passed the version check first on PATH."""
    env = {key: os.environ[key] for key in _NPM_ENV_KEYS if key in os.environ}
    env.update(
        {
            key: value
            for key, value in os.environ.items()
            if key.lower().startswith(_NPM_CONFIG_PREFIXES)
        }
    )
    # npm runs ``env node`` from its own shebang, so the node that passed the check comes first.
    base = terminal_path() or env.get("PATH", "")
    env["PATH"] = os.pathsep.join(part for part in (os.path.dirname(node), base) if part)
    return env


def _failure_line(raw: bytes | str) -> str:
    """The one npm line that says why an install failed, redacted and with the home folder as ``~``."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    lines = [line for line in lines if not any(noise in line for noise in _NPM_NOISE)]
    code = next((line for line in lines if line.startswith("npm error code")), None)
    error = next((line for line in lines if line.startswith("npm error")), None)
    line = code or error or (lines[-1] if lines else "no output")
    line = redact_text(line).strip()
    line = _NPM_AUTH.sub(r"\1[redacted]", line)
    line = _NPM_TOKEN.sub("npm_<redacted>", line)
    home = str(Path.home())
    if home and home != "/":
        line = line.replace(home, "~")
    return line[:200]


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """Kill npm and everything it started. On POSIX the install runs in its own session."""
    if _windows():
        # taskkill /T takes the child tree; proc.kill is the fallback if taskkill cannot run.
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
                timeout=30,
            )
        with contextlib.suppress(OSError):
            proc.kill()
    else:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.SubprocessError, OSError):
        proc.wait(timeout=10)


def install(name: str) -> None:
    """Install the pinned adapter into its own folder with the machine's own npm (``npm ci``).

    Does nothing when a machine-wide adapter at the pin or a complete private tree is ready.
    Raises :class:`AdapterInstallError` with a redacted reason for every other failure, including
    an OS error while preparing the folder. npm's output is never logged, only the exit code.
    """
    state = detect(name)
    if state == "ready":
        return
    if state == "cli_missing" or state.startswith("node_"):
        raise AdapterInstallError(describe(name, state))
    try:
        with _adapter_lock(name, shared=False, wait_s=INSTALL_TIMEOUT_S) as fd:
            if fd is None:
                raise AdapterInstallError(f"another install of {name} is still running; try again later.")
            _install_locked(name, fd)
    except _UnsafePrefix as exc:
        raise AdapterInstallError(f"The private adapters folder cannot be used: {exc}") from None
    except OSError as exc:
        raise AdapterInstallError(f"the adapter folder could not be prepared ({type(exc).__name__}).") from None


def _install_locked(name: str, lock_fd: int) -> None:
    state = detect(name)
    if state == "ready":
        return
    if state == "cli_missing" or state.startswith("node_"):
        raise AdapterInstallError(describe(name, state))
    npm = absolute_executable(resolve_tool("npm"))
    if npm is None:
        raise AdapterInstallError("npm was not found on this machine.")
    node = _node_path() or ""
    pin = ADAPTERS[name]
    adapter_dir = _adapter_dir(name)
    try:
        # Removed before npm starts, so a kill or a timeout never leaves a marker for a partial tree.
        _marker_path(name).unlink(missing_ok=True)
        _ensure_private_dir(adapter_dir)
        (adapter_dir / "package.json").write_bytes(_stock_bytes(name, "package.json"))
        (adapter_dir / "package-lock.json").write_bytes(_stock_bytes(name, "package-lock.json"))
    except OSError as exc:
        raise AdapterInstallError(f"the adapter files could not be written ({type(exc).__name__}).") from None
    # The lock fd goes to npm, so the lock outlives this process if the engine dies mid-install.
    options: dict[str, Any] = {"start_new_session": not _windows()}
    if not _windows():
        options["pass_fds"] = (lock_fd,)
    try:
        proc = subprocess.Popen(
            [npm, *NPM_CI_FLAGS],
            cwd=adapter_dir,
            env=_npm_env(node),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **options,
        )
    except OSError as exc:
        raise AdapterInstallError(f"npm could not run ({type(exc).__name__}).") from None
    try:
        out, err = proc.communicate(timeout=INSTALL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        with contextlib.suppress(subprocess.SubprocessError, OSError):
            proc.communicate(timeout=10)  # drain the pipes once more; the group is dead
        raise AdapterInstallError(f"npm install timed out after {INSTALL_TIMEOUT_S} s.") from None
    if proc.returncode != 0:
        logger.warning("npm ci of the %s adapter exited %s", name, proc.returncode)
        raise AdapterInstallError(
            f"npm install failed (exit {proc.returncode}): {_failure_line(err or out)}"
        )
    after = _check(name, marker=False)
    if after != "ready":
        raise AdapterInstallError(f"npm finished, but {name} is still {after}.")
    marker = {"version": pin.version, "lock_sha256": _lock_sha256(name)}
    try:
        _marker_path(name).write_text(json.dumps(marker, sort_keys=True), encoding="utf-8", newline="")
    except OSError as exc:
        raise AdapterInstallError(f"the install marker could not be written ({type(exc).__name__}).") from None


def _startup_claim(name: str) -> str | None:
    """``None`` when this process should start the install now; else the reason it should not.

    The reason is ``""`` when the adapter is ready, so a caller never reads a missing reason as ready
    once an attempt has failed: a failed attempt answers with its recorded failure.
    """
    state = detect(name)
    if state == "ready":
        return ""
    if state not in ("adapter_missing", "adapter_outdated"):
        return describe(name, state)
    with _STATE_LOCK:
        if name in _ATTEMPTED:
            return _FAILURES.get(name) or describe(name, state)
        _ATTEMPTED.add(name)
    return None


def _run_startup_install(name: str) -> str:
    """Run the claimed install. Returns ``""`` when it finished ready, else the recorded reason."""
    try:
        install(name)
    except (AdapterInstallError, OSError) as exc:
        message = str(exc) if isinstance(exc, AdapterInstallError) else f"the install failed ({type(exc).__name__})."
        with _STATE_LOCK:
            _FAILURES[name] = message
        logger.warning("Could not install the %s adapter: %s", name, message)
        return message
    with _STATE_LOCK:
        _FAILURES.pop(name, None)
    return ""


def install_at_startup(name: str) -> str:
    """Install the adapter once, in this thread, when its CLI is present and the adapter is missing.

    Returns ``""`` when the adapter is ready, else the reason it is not. A failed attempt is not
    retried in this process; the Settings install line is the retry.
    """
    if (reason := _startup_claim(name)) is not None:
        return reason
    return _run_startup_install(name)


def start_install_in_background(name: str) -> str | None:
    """Start the startup install on a daemon thread and return at once.

    Returns ``None`` when an install was started, ``""`` when the adapter is ready, else the reason
    it cannot be installed now. The install is tracked by its lock and shown on the Settings card,
    so the caller does not wait for npm.
    """
    if (reason := _startup_claim(name)) is not None:
        return reason
    threading.Thread(
        target=_run_startup_install,
        args=(name,),
        name=f"ciao-adapter-install-{name}",
        daemon=True,
    ).start()
    return None


def _main(argv: list[str]) -> int:
    """``python -I -m ciao.adapter_install <name>``: the Settings terminal install line."""
    if len(argv) != 2 or argv[1] not in ADAPTERS:
        print(f"usage: python -m ciao.adapter_install {{{','.join(ADAPTERS)}}}", file=sys.stderr)
        return 2
    name = argv[1]
    try:
        install(name)
    except AdapterInstallError as exc:
        print(f"Ciaobot could not install {name}: {exc}", file=sys.stderr)
        return 1
    print(f"{name} {ADAPTERS[name].version} is installed in {adapter_prefix()}.")
    return 0


if __name__ == "__main__":  # pragma: no cover - the terminal line; the function is tested
    raise SystemExit(_main(sys.argv))
