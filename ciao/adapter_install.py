"""The ACP adapters Ciaobot runs, installed from this machine's own Node and npm (#3425).

Nothing agent-related ships in the engine. Each adapter is a pinned npm package in a private
prefix outside the app bundle, so an app update does not remove it. The adapter drives the user's
own ``claude`` or ``codex`` CLI, which Ciaobot never installs or bundles. The pins below are the
only copy of the adapter versions: the Settings install lines, the startup install and the
status probes all read them here.

npm runs with ``--omit=optional`` (the per-platform native CLI packages are hundreds of MB and
are not needed, since the user's own CLI is used), ``--ignore-scripts``, an argv list and no
shell. An adapter starts as ``[node, <entry>]`` with Node resolved by absolute path, so the
``#!/usr/bin/env node`` shebang does not depend on PATH.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ciao.tool_path import resolve_tool

logger = logging.getLogger(__name__)

CLAUDE_ADAPTER = "claude-agent-acp"
CODEX_ADAPTER = "codex-acp"


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
NPM_INSTALL_FLAGS = ("install", "--no-audit", "--no-fund", "--omit=optional", "--ignore-scripts")

# A state is the first failing check, in this order, or "ready".
STATES = ("cli_missing", "node_missing", "node_old", "adapter_missing", "adapter_outdated", "ready")

_LOCK = threading.Lock()
_ATTEMPTED: set[str] = set()
_FAILURES: dict[str, str] = {}
_USERINFO = re.compile(r"(\w+://)[^/\s@]+@")
_NPM_TOKEN = re.compile(r"npm_[A-Za-z0-9]{8,}")


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


def _package_dir(name: str) -> Path:
    return adapter_prefix() / "node_modules" / Path(*ADAPTERS[name].package.split("/"))


def _cli_path(name: str) -> str | None:
    if ADAPTERS[name].cli == "claude":
        from ciao.setup_status import claude_cli_path

        return claude_cli_path() or None
    from ciao.providers.acp.agents import codex_binary

    return codex_binary()


def _node_path() -> str | None:
    return resolve_tool("node")


_NODE_MAJOR: dict[str, int] = {}


def _node_major(node: str) -> int | None:
    """The major version of ``node``. Only a successful probe is cached, so a timeout is retried."""
    if node in _NODE_MAJOR:
        return _NODE_MAJOR[node]
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
    if match is None:
        return None
    _NODE_MAJOR[node] = int(match.group(1))
    return _NODE_MAJOR[node]


def _package_json(name: str) -> dict[str, Any] | None:
    try:
        data = json.loads((_package_dir(name) / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _entry(name: str) -> Path | None:
    """The adapter's JS entry, from its package.json ``bin``. Must sit inside its package."""
    data = _package_json(name)
    if data is None:
        return None
    bin_field = data.get("bin")
    rel = bin_field.get(name) if isinstance(bin_field, dict) else bin_field
    if not isinstance(rel, str) or not rel:
        return None
    package_dir = _package_dir(name).resolve()
    entry = (package_dir / rel).resolve()
    try:
        entry.relative_to(package_dir)
    except ValueError:
        return None
    return entry if entry.is_file() else None


def detect(name: str) -> str:
    """The first failing check for ``name``, or ``"ready"``. Reads package.json; spawns only node."""
    if _cli_path(name) is None:
        return "cli_missing"
    node = _node_path()
    if node is None:
        return "node_missing"
    major = _node_major(node)
    if major is None or major < NODE_MIN_MAJOR:
        return "node_old"
    data = _package_json(name)
    installed = data.get("version") if data else None
    if not isinstance(installed, str):
        return "adapter_missing"
    if installed != ADAPTERS[name].version:
        return "adapter_outdated"
    if _entry(name) is None:
        return "adapter_missing"
    return "ready"


def describe(name: str, state: str) -> str:
    """One sentence for a Settings card or a startup phase, for a state from :func:`detect`."""
    pin = ADAPTERS[name]
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
    """``[node, <entry>]`` for a ready adapter. Raises :class:`AdapterNotReady` otherwise."""
    state = detect(name)
    node = _node_path()
    entry = _entry(name)
    if state != "ready" or node is None or entry is None:
        raise AdapterNotReady(describe(name, state if state != "ready" else "adapter_missing"))
    return [node, str(entry)]


def install_command(name: str) -> str:
    """The terminal line that installs the pinned adapter into the private prefix."""
    pin = ADAPTERS[name]
    return shlex.join(
        ["npm", *NPM_INSTALL_FLAGS, "--prefix", str(adapter_prefix()), f"{pin.package}@{pin.version}"]
    )


def failure(name: str) -> str | None:
    """Why the last install in this process failed, or ``None``. Cleared by a successful install."""
    return _FAILURES.get(name)


def _npm_env() -> dict[str, str]:
    # Lazy: the ACP connection module imports this one.
    from ciao.providers.acp.connection import agent_base_env

    return agent_base_env(())


def _last_line(raw: bytes | str) -> str:
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    line = lines[-1] if lines else "no output"
    line = _USERINFO.sub(r"\1", line)
    line = _NPM_TOKEN.sub("npm_<redacted>", line)
    return line[:200]


def install(name: str) -> None:
    """Install the pinned adapter into the private prefix with the machine's own npm.

    Raises :class:`AdapterInstallError` with a redacted reason. Output is never logged, only the
    exit code.
    """
    state = detect(name)
    if state == "ready":
        return
    if state == "cli_missing" or state.startswith("node_"):
        raise AdapterInstallError(describe(name, state))
    npm = resolve_tool("npm")
    if npm is None:
        raise AdapterInstallError("npm was not found on this machine.")
    pin = ADAPTERS[name]
    prefix = adapter_prefix()
    argv = [npm, *NPM_INSTALL_FLAGS, "--prefix", str(prefix), f"{pin.package}@{pin.version}"]
    try:
        prefix.mkdir(parents=True, exist_ok=True)
        done = subprocess.run(
            argv,
            cwd=prefix,
            env=_npm_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=INSTALL_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AdapterInstallError(f"npm could not run ({type(exc).__name__}).") from None
    if done.returncode != 0:
        logger.warning("npm install of the %s adapter exited %s", name, done.returncode)
        raise AdapterInstallError(
            f"npm install failed (exit {done.returncode}): {_last_line(done.stderr or done.stdout)}"
        )
    after = detect(name)
    if after != "ready":
        raise AdapterInstallError(f"npm finished, but {name} is still {after}.")


def install_at_startup(name: str) -> str:
    """Install the adapter once, when its CLI is present and the adapter is missing or outdated.

    Returns ``""`` when the adapter is ready, else the reason it is not. A failed attempt is not
    retried in this process; the Settings install line is the retry.
    """
    with _LOCK:
        if name in _ATTEMPTED:
            return _FAILURES.get(name, "")
        state = detect(name)
        if state == "ready":
            return ""
        if state not in ("adapter_missing", "adapter_outdated"):
            return describe(name, state)
        _ATTEMPTED.add(name)
        try:
            install(name)
        except AdapterInstallError as exc:
            _FAILURES[name] = str(exc)
            logger.warning("Could not install the %s adapter: %s", name, exc)
            return str(exc)
        _FAILURES.pop(name, None)
        return ""
