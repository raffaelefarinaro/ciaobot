"""The ACP adapters Ciaobot runs, installed from this machine's own Node and npm (#3425).

Nothing agent-related ships in the engine. Each adapter is a pinned npm package in a private
prefix outside the app bundle, so an app update does not remove it. The adapter drives the user's
own ``claude`` or ``codex`` CLI, which Ciaobot never installs or bundles. The pins below are the
only copy of the adapter versions: the Settings install lines, the startup install and the
status probes all read them here.

Each adapter has its own folder in the prefix (``<prefix>/<name>/``) holding the lockfile shipped
in ``ciao/stock/adapters/<name>/``. The install is ``npm ci`` against that lockfile, so every
package is pinned by integrity hash, with ``--ignore-scripts``, ``--omit=optional``, an argv list
and no shell. A successful install writes a marker that names the pin and the lockfile hash; an
adapter without a matching marker is not ready, so a half-finished tree never runs.

An adapter starts as ``[node, <entry>]`` with Node resolved by absolute path, so the
``#!/usr/bin/env node`` shebang does not depend on PATH. Each install and each launch holds the
adapter's own file lock in the prefix, so two engines, or a Settings terminal install, cannot
corrupt a tree.

Run as ``python -m ciao.adapter_install <name>``, this module is also the Settings terminal line:
it takes the same lock and writes the same marker as the engine does.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

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
NPM_CI_FLAGS = ("ci", "--ignore-scripts", "--omit=optional", "--no-audit", "--no-fund")
# npm's own environment, from the engine's environment. Everything else is dropped, so provider
# keys, tokens and NODE_OPTIONS never reach npm.
_NPM_ENV_KEYS = (
    "PATH", "HOME", "USERPROFILE", "TMPDIR", "LANG", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    # Windows needs these for npm to start at all.
    "SYSTEMROOT", "APPDATA", "LOCALAPPDATA", "PATHEXT",
)
_NPM_CONFIG_PREFIXES = ("npm_config_",)

# A state is the first failing check, in this order, or "ready".
STATES = ("cli_missing", "node_missing", "node_old", "adapter_missing", "adapter_outdated", "ready")

# Guards the in-process "attempted" and "failure" records. Installs themselves are serialized by
# the per-adapter file lock, so Claude and Codex installs do not wait on each other.
_STATE_LOCK = threading.Lock()
_ATTEMPTED: set[str] = set()
_FAILURES: dict[str, str] = {}
_NODE_MAJOR: dict[tuple[str, int], int] = {}
_NPM_AUTH = re.compile(r"(?i)(_auth(?:token)?\s*=\s*)\S+")
_NPM_TOKEN = re.compile(r"npm_[A-Za-z0-9]{8,}")
# npm's pointer to its debug log, and the log's own file name: neither says why the install failed.
_NPM_NOISE = ("A complete log of this run can be found in", "_logs")


class AdapterNotReady(RuntimeError):
    """The adapter cannot start yet. The message is safe to show."""


class AdapterInstallError(RuntimeError):
    """An install did not finish. The message is redacted and safe to show."""


def adapter_prefix() -> Path:
    """The private prefix the adapters live in. Not an env var: a fixed location per platform."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Ciaobot" / "adapters"
    from ciao.install_receipt import default_receipt_path

    return default_receipt_path().parent / "adapters"


def absolute_executable(path: str | None) -> str | None:
    """``path`` when it is an absolute, executable file; otherwise ``None``.

    A relative path would resolve against the working directory of the process that runs it, so
    it is refused here, for every executable the adapters are handed.
    """
    if not path or not os.path.isabs(path):
        return None
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
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
    if sys.platform == "win32" or not path.exists():
        return None
    try:
        st = path.stat()
    except OSError:
        return f"{path} cannot be inspected."
    if st.st_uid != os.geteuid():
        return f"{path} is not owned by this user."
    if st.st_mode & 0o022:
        return f"{path} is writable by other users."
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


def _check(name: str, *, marker: bool) -> str:
    """The first failing check for ``name``, or ``"ready"``. ``marker=False`` skips the marker."""
    if _cli_path(name) is None:
        return "cli_missing"
    node = _node_path()
    if node is None:
        return "node_missing"
    major = _node_major(node)
    if major is None or major < NODE_MIN_MAJOR:
        return "node_old"
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
    """The first failing check for ``name``, or ``"ready"``. Reads files; spawns only node."""
    return _check(name, marker=True)


def _lock_held_elsewhere(name: str) -> bool:
    """True when another holder has the adapter's lock right now. Never creates the lock file."""
    try:
        fd = os.open(adapter_prefix() / f".ciao-{name}.lock", os.O_RDWR)
    except OSError:
        return False
    try:
        if _try_lock(fd):
            _unlock(fd)
            return False
        return True
    finally:
        os.close(fd)


def _try_lock(fd: int) -> bool:
    try:
        if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def _adapter_lock(name: str, *, wait_s: float) -> Iterator[bool]:
    """Hold the adapter's file lock, waiting up to ``wait_s``. Yields whether it was taken."""
    _ensure_private_dir(adapter_prefix())
    fd = os.open(adapter_prefix() / f".ciao-{name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    taken = False
    deadline = time.monotonic() + wait_s
    try:
        while True:
            taken = _try_lock(fd)
            if taken or time.monotonic() >= deadline:
                break
            time.sleep(0.2)
        yield taken
    finally:
        if taken:
            _unlock(fd)
        os.close(fd)


def describe(name: str, state: str) -> str:
    """One sentence for a Settings card or a startup phase, for a state from :func:`detect`."""
    pin = ADAPTERS[name]
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

    Holds the adapter's lock while it reads the tree, so a launch never reads a half-written one.
    """
    with _adapter_lock(name, wait_s=0) as taken:
        if not taken:
            raise AdapterNotReady(f"The ACP adapter {name} is being installed. Try again in a moment.")
        state = detect(name)
        node = _node_path()
        pkg_dir = _package_dir(name)
        entry = _entry(name, pkg_dir) if pkg_dir is not None else None
    # Described after the lock is released, or describe() would report this launch's own lock.
    if state != "ready" or node is None or entry is None:
        raise AdapterNotReady(describe(name, state if state != "ready" else "adapter_missing"))
    return [node, str(entry)]


def install_command(name: str) -> str:
    """The terminal line that installs the pinned adapter. It runs this module with the engine's Python."""
    return shlex.join([sys.executable, "-m", "ciao.adapter_install", name])


def failure(name: str) -> str | None:
    """Why the last install in this process failed, or ``None``. Cleared by a successful install."""
    return _FAILURES.get(name)


def _npm_env() -> dict[str, str]:
    env = {key: os.environ[key] for key in _NPM_ENV_KEYS if key in os.environ}
    env.update(
        {
            key: value
            for key, value in os.environ.items()
            if key.lower().startswith(_NPM_CONFIG_PREFIXES)
        }
    )
    path = terminal_path()
    if path:
        env["PATH"] = path
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
    """Kill npm and everything it started. The install runs in its own session on POSIX."""
    if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
        proc.kill()
    else:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.SubprocessError, OSError):
        proc.wait(timeout=10)


def install(name: str) -> None:
    """Install the pinned adapter into its own folder with the machine's own npm (``npm ci``).

    Raises :class:`AdapterInstallError` with a redacted reason. npm's output is never logged, only
    the exit code. The marker is removed before npm starts and written only after a ready check.
    """
    state = detect(name)
    if state == "ready":
        return
    if state == "cli_missing" or state.startswith("node_"):
        raise AdapterInstallError(describe(name, state))
    with _adapter_lock(name, wait_s=INSTALL_TIMEOUT_S) as taken:
        if not taken:
            raise AdapterInstallError(f"another install of {name} is still running; try again later.")
        _install_locked(name)


def _install_locked(name: str) -> None:
    state = detect(name)
    if state == "ready":
        return
    if state == "cli_missing" or state.startswith("node_"):
        raise AdapterInstallError(describe(name, state))
    if (problem := _prefix_problem(name)) is not None:
        raise AdapterInstallError(f"The private adapters folder cannot be used: {problem}")
    npm = absolute_executable(resolve_tool("npm"))
    if npm is None:
        raise AdapterInstallError("npm was not found on this machine.")
    pin = ADAPTERS[name]
    adapter_dir = _adapter_dir(name)
    _marker_path(name).unlink(missing_ok=True)
    try:
        _ensure_private_dir(adapter_prefix())
        _ensure_private_dir(adapter_dir)
        (adapter_dir / "package.json").write_bytes(_stock_bytes(name, "package.json"))
        (adapter_dir / "package-lock.json").write_bytes(_stock_bytes(name, "package-lock.json"))
        proc = subprocess.Popen(
            [npm, *NPM_CI_FLAGS],
            cwd=adapter_dir,
            env=_npm_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=sys.platform != "win32",
        )
    except OSError as exc:
        raise AdapterInstallError(f"npm could not run ({type(exc).__name__}).") from None
    try:
        out, err = proc.communicate(timeout=INSTALL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        _marker_path(name).unlink(missing_ok=True)
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
    _marker_path(name).write_text(json.dumps(marker, sort_keys=True), encoding="utf-8")


def install_at_startup(name: str, on_install: Callable[[], None] | None = None) -> str:
    """Install the adapter once, when its CLI is present and the adapter is missing or outdated.

    ``on_install`` runs only when an install is about to start. Returns ``""`` when the adapter is
    ready, else the reason it is not. A failed attempt is not retried in this process; the Settings
    install line is the retry.
    """
    with _STATE_LOCK:
        if name in _ATTEMPTED:
            return _FAILURES.get(name, "")
        state = detect(name)
        if state == "ready":
            return ""
        if state not in ("adapter_missing", "adapter_outdated"):
            return describe(name, state)
        _ATTEMPTED.add(name)
    if on_install is not None:
        on_install()
    try:
        install(name)
    except AdapterInstallError as exc:
        with _STATE_LOCK:
            _FAILURES[name] = str(exc)
        logger.warning("Could not install the %s adapter: %s", name, exc)
        return str(exc)
    with _STATE_LOCK:
        _FAILURES.pop(name, None)
    return ""


def _main(argv: list[str]) -> int:
    """``python -m ciao.adapter_install <name>``: the Settings terminal install line."""
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
