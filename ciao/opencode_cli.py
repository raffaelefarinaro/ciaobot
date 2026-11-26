"""OpenCode CLI helpers: binary resolution, install and sign-in status, and the version floor.

The OpenCode runtime is ``opencode acp`` (``ciao/providers/acp/agents.py``). What stays here is
the CLI-side work Ciaobot still does outside a chat: finding the binary, the Settings status row,
the credential and skill inventory, and the version floor shared by Settings, and the releaser.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import socket
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ciao.os_support.processes import ProcessTree, bounded_output, tree_spawn_options
from ciao.providers.acp.connection import agent_base_env
from ciao.tool_path import prepend_engine_path, resolve_tool

if TYPE_CHECKING:
    import subprocess


# The lowest OpenCode CLI version Ciaobot will run. Every message below is
# derived from it, so the floor is stated once.
OPENCODE_MIN_VERSION = (2, 3, 2)
OPENCODE_MIN_TEXT = ".".join(str(part) for part in OPENCODE_MIN_VERSION)

OPENCODE_V2_REQUIRED = (
    f"Ciaobot requires OpenCode {OPENCODE_MIN_TEXT} or newer. OpenCode 1 cannot be "
    f"upgraded in place: install OpenCode {OPENCODE_MIN_TEXT} or newer (npm install -g @opencode/cli, "
    "brew install anomalyco/tap/opencode-v2, or "
    "curl -fsSL https://opencode.ai/v2/install | bash), then retry this chat."
)


# Budget for the Settings inventory read: spawn a private server, wait for
# it to finish loading, read three routes. Measured ~0.4s on 2.0.27.
_INVENTORY_TIMEOUT = 15.0


_INVENTORY_POLL = 0.1


_SYSTEM_SKILLS_TTL_SECONDS = 300.0


_REQUEST_TIMEOUT = 30.0


_SHUTDOWN_TIMEOUT = 5.0


def _stop_server_sync(process: subprocess.Popen[bytes], tree: ProcessTree) -> None:
    """Blocking twin of `_stop_server`, same order: ask, wait, then kill."""
    import subprocess

    try:
        if process.poll() is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                tree.kill_descendants()
            return
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            tree.terminate()
        try:
            process.wait(timeout=_SHUTDOWN_TIMEOUT)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, FileNotFoundError, PermissionError, OSError):
                tree.kill()
                process.wait()
    finally:
        tree.close()


@dataclass(frozen=True, slots=True)
class OpencodeSettings:
    """Operator override for the opencode default model.

    Empty string means "no override": the default falls through to whatever
    model the session's configured provider resolves. Mirrors
    so ``AppSettings.provider_default_models`` can drive it the same way.
    """

    default_model: str = ""


def opencode_default_model(config: object) -> str:
    """The operator's opencode default model, or ``""`` when there is none."""
    settings = getattr(config, "opencode", None)
    if settings is None:
        return ""
    return str(getattr(settings, "default_model", "") or "")


def resolve_opencode_binary(env: Mapping[str, str] | None = None) -> str | None:
    """Absolute path to the opencode CLI, or None when it is not installed."""
    source: Mapping[str, str] = {**os.environ, **env} if env else os.environ
    explicit = str(source.get("CIAO_OPENCODE_BIN", "")).strip()
    if explicit:
        path = Path(explicit).expanduser()
        return str(path.resolve()) if path.is_file() else None
    return resolve_tool("opencode")


#: Where someone with no OpenCode at all is sent. The download page lists every
#: installer for every platform, so a provider row links there rather than at a
#: single method; the row's command carries the one-line form.
OPENCODE_INSTALL_DOCS_URL = "https://opencode.ai/download"


#: The sign-in command shown on the provider row and run by the host terminal.
OPENCODE_LOGIN_COMMAND = "opencode auth login"


#: OpenCode's MCP view is the ``mcps`` slash command (not ``/mcp``).
OPENCODE_MCP_SLASH = "/mcps"


def opencode_launch_command(binary: str | None) -> str:
    """Terminal line that starts OpenCode; the PWA then types ``mcp_slash``."""
    from ciao.tool_path import terminal_launch_command

    return terminal_launch_command("opencode", binary)


def opencode_install_command() -> str:
    """The documented one-line installer for this platform.

    ``resolve_opencode_binary`` finds the curl installer's ``~/.opencode/bin``
    (via ``common_tool_dirs``, so no engine restart is needed) and npm's or
    Homebrew's bins too, so any of the three documented installers is picked up
    on the next status refresh.
    """
    return "curl -fsSL https://opencode.ai/v2/install | bash"


def opencode_path_hint() -> str:
    """PATH line for the curl installer's directory, or "" when none is needed.

    Only that installer drops the binary somewhere no default PATH carries; the
    npm shim and Homebrew already install into directories Ciaobot searches.
    """
    from ciao.os_support.shell_hints import path_hint

    return path_hint(str(Path.home() / ".opencode" / "bin"), persist=True)


def auth_command(*, device_auth: bool = False) -> list[str]:
    """Interactive login command, for ``ciao auth opencode`` and the PWA.

    ``device_auth`` has no opencode equivalent and is ignored.
    """
    binary = resolve_opencode_binary()
    if not binary:
        raise FileNotFoundError("opencode CLI not found")
    return [binary, "auth", "login"]


def _free_port() -> int:
    """Reserve an ephemeral loopback port and hand back the number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def version_number(value: object) -> tuple[int, ...] | None:
    """Parse the dotted version number out of CLI output such as ``opencode v2.0.16``.

    The published parser behind Ciaobot's OpenCode version policy. It reads
    the permissive dotted-integer prefix it has always read — an optional
    ``v``, digits joined by dots, prose around them, and any suffix
    (``2.0.16-beta.3``) after the numbers ignored — and returns that prefix
    as a tuple: ``"2.0.16"`` → ``(2, 0, 16)``, ``"2.0"`` → ``(2, 0)``,
    ``"2.0.16.1"`` → ``(2, 0, 16, 1)``. ``None``, ``""`` and ``"unknown"``
    parse to ``None``. This is not a validated SemVer, and Python's ordinary
    string conversion of non-string input is relied on as it always was.
    """
    match = re.search(r"(?<!\d)v?(\d+(?:\.\d+)+)", str(value or ""))
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def cli_version_error(payload: object) -> str | None:
    """Return the user-facing error when the installed OpenCode CLI is too old or not V2.

    Version-only judgment on the ``version`` reported by ``opencode --version``: a
    Mapping whose version parses to major 2 at or above ``OPENCODE_MIN_VERSION``
    is supported. Anything unparseable, below the floor, outside major 2, blank,
    or a payload that is not a Mapping at all is refused with
    ``OPENCODE_V2_REQUIRED``, plus ``Installed CLI version: <version>.`` when a
    non-blank version was readable. ``None`` means the version is in the
    supported range and nothing more: it does not promise that the CLI can
    start, is authenticated, or serves a compatible API.
    """
    if not isinstance(payload, Mapping):
        return OPENCODE_V2_REQUIRED
    version = str(payload.get("version") or "").strip()
    parsed = version_number(version)
    if parsed is None or parsed < OPENCODE_MIN_VERSION or parsed[0] != 2:
        detail = f" Installed CLI version: {version}." if version else ""
        return f"{OPENCODE_V2_REQUIRED}{detail}"
    return None


def opencode_version_line(binary: str, *, timeout: float = 5.0) -> str:
    """The first line of ``<binary> --version``, or ``""`` when it cannot be read.

    The one version probe: the Settings status row and the ACP start gate both use it. It runs
    with the agent's environment policy (no ``CIAO_*`` or PWA token, the engine's bin dir first on
    PATH), so a ``#!/usr/bin/env node`` shim finds node under a minimal launchd PATH.
    """
    import subprocess

    env = agent_base_env(())
    env["PATH"] = prepend_engine_path(env.get("PATH"))
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""


# A failed probe of a present binary is remembered this long, so a broken install does not cost
# a timeout on every chat start. A readable version is kept until the binary's mtime changes.
_VERSION_FAILURE_TTL = 20.0


# (monotonic time the probe finished, version or "" for a failed probe)
_VERSION_CACHE: dict[tuple[str, int], tuple[float, str]] = {}


_VERSION_CACHE_GUARD = threading.Lock()


def cached_opencode_version(binary: str) -> str:
    """``opencode_version_line`` cached per binary path and mtime, so an upgrade is seen.

    A readable version is kept for that mtime. A failed probe is kept for
    ``_VERSION_FAILURE_TTL`` seconds, then retried. A missing binary is not cached at all.
    """
    try:
        key = (binary, os.stat(binary).st_mtime_ns)
    except OSError:
        return opencode_version_line(binary)
    with _VERSION_CACHE_GUARD:
        cached = _VERSION_CACHE.get(key)
    if cached is not None:
        probed_at, version = cached
        if version or time.monotonic() - probed_at < _VERSION_FAILURE_TTL:
            return version
    version = opencode_version_line(binary)
    with _VERSION_CACHE_GUARD:
        _VERSION_CACHE[key] = (time.monotonic(), version)
    return version


def opencode_start_supported(version: str) -> bool:
    """True when ``version`` is in the OpenCode floor: the ACP start gate's ``supported``."""
    return cli_version_error({"version": version}) is None


def _data(payload: object) -> object:
    """Unwrap V2's common ``{"data": ...}`` response envelope."""
    if isinstance(payload, Mapping) and "data" in payload:
        return payload["data"]
    return payload


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


_CREDENTIAL_WORD_RE = re.compile(r"credentials?\b", re.IGNORECASE)


_AUTH_LIST_MAX_STDOUT = 1024 * 1024


def opencode_credential_inventory() -> dict[str, Any]:
    """Project only bounded, user-visible metadata from the CLI's auth listing."""
    import unicodedata

    unknown: dict[str, Any] = {"status": "unknown", "providers": []}
    try:
        binary = resolve_opencode_binary()
        if not binary:
            return unknown
        result = bounded_output(
            [binary, "auth", "list", "--format", "json", "--standalone"],
            timeout=5.0, max_stdout=_AUTH_LIST_MAX_STDOUT,
        )
        if result is None or result[0] != 0:
            return unknown
        rows = json.loads(result[1].decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return unknown
    if not isinstance(rows, list):
        return unknown

    def clean(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        value = value.strip()
        if not value or len(value) > 128 or any(
            unicodedata.category(ch) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for ch in value
        ):
            return None
        return value

    partial = len(rows) > 100
    providers: list[dict[str, Any]] = []
    seen: set[str] = set()
    order = ("oauth", "api_key", "environment", "unknown")
    for row in rows[:100]:
        if not isinstance(row, dict):
            partial = True
            continue
        provider_id, name = clean(row.get("id")), clean(row.get("name"))
        connections = row.get("connections")
        if provider_id is None or name is None or not isinstance(connections, list):
            partial = True
            continue
        if provider_id in seen:
            partial = True
            continue
        if len(connections) > 32:
            partial = True
        methods: set[str] = set()
        for connection in connections[:32]:
            if not isinstance(connection, dict):
                partial = True
                continue
            if connection.get("type") == "env":
                methods.add("environment")
            elif connection.get("type") == "credential" and connection.get("method") in ("oauth", "key"):
                methods.add("oauth" if connection["method"] == "oauth" else "api_key")
            else:
                methods.add("unknown")
                partial = True
        if methods:
            seen.add(provider_id)
            providers.append({
                "id": provider_id, "name": name,
                "auth_types": [method for method in order if method in methods],
                "configured": True, "verified": None,
            })
    providers.sort(key=lambda item: (item["id"], item["name"]))
    return {"status": "partial" if partial else "complete", "providers": providers}


def _credential_count(binary: str, *, timeout: float) -> int | None:
    """How many providers opencode holds credentials for, or None if unknown.

    2.0.16 prints a plain table (and "No authenticated integrations" when
    empty), so ask for `--format json` and count providers with at least one
    connection. Earlier 2.x builds printed a decorated TUI box ending in
    `N credentials`; that count is the fallback. Counting non-empty lines
    counts the decoration, which is how this once reported "10 provider(s)
    authenticated" against an empty store.

    `~/.local/share/opencode/auth.json` is deliberately not read: parsing a
    provider's cached credential file to determine identity is out of bounds.

    `--standalone` runs a private server for this one call; without it the CLI
    reads (or starts) the shared background service, which can be an older
    opencode than the installed one.
    """
    listed = bounded_output(
        [binary, "auth", "list", "--format", "json", "--standalone"],
        timeout=timeout, max_stdout=_AUTH_LIST_MAX_STDOUT,
    )
    if listed is None:
        return None
    try:
        text = listed[1].decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        providers = json.loads(text)
    except (ValueError, RecursionError):
        providers = None
    if isinstance(providers, list):
        return sum(
            1 for item in providers
            if isinstance(item, dict) and item.get("connections")
        )
    listed = bounded_output(
        [binary, "auth", "list", "--standalone"],
        timeout=timeout, max_stdout=_AUTH_LIST_MAX_STDOUT,
    )
    if listed is None:
        return None
    try:
        text = _ANSI_RE.sub("", listed[1].decode("utf-8"))
    except UnicodeDecodeError:
        return None
    i = 0
    while i < len(text):
        if not text[i].isdecimal():
            i += 1
            continue
        start = i
        while i < len(text) and text[i].isdecimal():
            i += 1
        end = i
        while i < len(text) and text[i].isspace():
            i += 1
        if i > end and _CREDENTIAL_WORD_RE.match(text, i):
            try:
                return int(text[start:end])
            except ValueError:
                return None
    return None


_InventoryRows = tuple[list[dict[str, Any]] | None, list[dict[str, Any]] | None]


_NO_INVENTORY: _InventoryRows = (None, None)


def _read_loaded_inventory(
    client: httpx.Client, process: Any, *, deadline: float, location: str
) -> _InventoryRows:
    """Skill and plugin rows of a private server once it has loaded.

    A fresh server answers before it has loaded: first nothing, then only the
    two built-in skills, then everything at once with `plugin.updated` last
    (2.0.27). Built-in plugins always exist, so a non-empty plugin list is the
    load-complete signal; an empty list or partial skills must not be shown as
    the inventory. All-None means unknown (never loaded, exited, or unreadable).
    """
    params = {"location[directory]": location}

    def rows(path: str) -> list[dict[str, Any]] | None:
        try:
            response = client.get(path, params=params, timeout=2.0)
            data = _data(response.json())
        except (httpx.HTTPError, ValueError):
            return None
        if response.status_code != 200 or not isinstance(data, list):
            return None
        return [row for row in data if isinstance(row, dict)]

    while time.monotonic() < deadline:
        if process.poll() is not None:
            return _NO_INVENTORY
        try:
            response = client.get("/api/plugin", params=params, timeout=2.0)
        except httpx.HTTPError:
            time.sleep(_INVENTORY_POLL)
            continue
        if response.status_code in (401, 404):
            return _NO_INVENTORY
        if response.status_code == 200:
            try:
                data = _data(response.json())
            except ValueError:
                return _NO_INVENTORY
            if isinstance(data, list) and data:
                plugin_rows = [row for row in data if isinstance(row, dict)]
                return rows("/api/skill"), plugin_rows
        time.sleep(_INVENTORY_POLL)
    return _NO_INVENTORY


def _fetch_inventory_rows(binary: str, *, timeout: float = _INVENTORY_TIMEOUT) -> _InventoryRows:
    """Spawn a private `opencode serve` in home, read the inventory, stop it."""
    import subprocess

    port = _free_port()
    password = secrets.token_urlsafe(24)
    home = Path.home()
    try:
        process = subprocess.Popen(
            [binary, "serve", "--port", str(port), "--hostname", "127.0.0.1"],
            cwd=str(home),
            env={**agent_base_env(()), "OPENCODE_SERVER_PASSWORD": password},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **tree_spawn_options(dies_with_engine=True),
        )
    except OSError:
        return _NO_INVENTORY
    try:
        tree = ProcessTree(process.pid, dies_with_engine=True)
    except OSError:
        # A server that cannot be stopped as a tree would leak; do not run it.
        process.kill()
        process.wait()
        return _NO_INVENTORY
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            auth=("opencode", password),
            timeout=httpx.Timeout(_REQUEST_TIMEOUT),
        ) as client:
            return _read_loaded_inventory(
                client, process, deadline=time.monotonic() + timeout, location=str(home)
            )
    finally:
        _stop_server_sync(process, tree)


def _row_names(rows: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for row in rows:
        name = str(row.get("id") or row.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


@dataclass(frozen=True)
class _Inventory:
    """What the opencode CLI loads globally; ``None`` fields could not be read."""

    skills: list[str] | None
    bundled_skills: list[str] | None


def _opencode_inventory(binary: str) -> _Inventory:
    """Skills and plugins, and the skills opencode ships.

    A skill whose path is under ``skill://`` is embedded in the opencode binary (its
    ``embedded`` source); user and plugin skills have absolute file paths. Built-in
    plugins are opencode internals, and a plugin that failed to load (a
    duplicate ID, say) brings nothing, so both are left out.

    Read from a private `opencode serve` in the home directory that Ciaobot
    starts and stops, never through `opencode api`, which goes to the shared
    background service.
    """
    skill_rows, plugin_rows = _fetch_inventory_rows(binary)
    skills = bundled = None
    if skill_rows is not None and plugin_rows is not None:
        bundled = _row_names([
            row for row in skill_rows if str(row.get("path") or "").startswith("skill://")
        ])
        own = [name for name in _row_names(skill_rows) if name not in bundled]
        plugins = _row_names([
            row for row in plugin_rows
            if (row.get("source") or {}).get("type") != "builtin"
            and (row.get("state") or {}).get("status", "active") == "active"
        ])
        skills = own + [name for name in plugins if name not in own]
    return _Inventory(skills=skills, bundled_skills=bundled)


def opencode_login_status(*, timeout: float = 5.0, inventory: bool = True) -> dict[str, Any]:
    """Bounded, credential-free opencode install/auth status for Settings."""
    from ciao.provider_versions import release_check
    from ciao.setup_status import PROVIDER_STATE_NOT_INSTALLED, _provider

    binary: str | None
    try:
        binary = resolve_opencode_binary()
    except OSError as exc:
        # Present but not resolvable to something runnable — an interrupted
        # install, or a path that fails to resolve. Saying "not installed" would
        # send the operator to install something they already have.
        return _provider(
            name="opencode",
            ok=False,
            auth="broken",
            command="opencode",
            detail=f"installed but broken: {exc}",
            version="unknown",
        )
    if not binary:
        # The binary is absent, so this is an install step, not a login step:
        # `not_installed` is the state the PWA and the setup wizard both render
        # with the install command and the docs link. opencode's credentials are
        # beside the binary, so a missing binary cannot also be a missing login.
        return _provider(
            name="opencode",
            ok=False,
            auth="not_installed",
            state=PROVIDER_STATE_NOT_INSTALLED,
            command=opencode_install_command(),
            detail="opencode is not installed on this machine.",
            version="not installed",
            install_url=OPENCODE_INSTALL_DOCS_URL,
            terminal_action="opencode-install",
            # Mirrors Claude's row so the wizard offers one PATH line whatever
            # the provider. Empty when the installer used needs none.
            path_command=opencode_path_hint(),
        )
    # Unlike auth-list, this version probe still captures unbounded output.
    version = opencode_version_line(binary, timeout=timeout)
    if cli_version_error({"version": version}) is not None:
        return _provider(
            name="opencode",
            ok=False,
            auth="unsupported_version",
            command="opencode --version",
            detail=OPENCODE_V2_REQUIRED,
            version=version or "unknown",
            terminal_action="opencode-install",
            release_check=release_check("opencode", version, binary=binary),
        )
    credentials = _credential_count(binary, timeout=timeout)
    # opencode's own free tier serves models with no credentials at all, so an
    # empty credential store still means "usable" — it does not mean "not set
    # up". Say which it is rather than implying the user has connected
    # something they have not.
    if credentials is None:
        detail = "installed; credential state unknown"
        auth = "unknown"
    elif credentials > 0:
        detail = f"{credentials} provider credential(s)"
        auth = "oauth"
    else:
        detail = "no credentials — free models only"
        auth = "free"
    found = _opencode_inventory(binary) if inventory else _Inventory(None, None)
    return _provider(
        name="opencode",
        ok=True,
        auth=auth,
        command=OPENCODE_LOGIN_COMMAND,
        terminal_action="opencode-login",
        mcp_command=opencode_launch_command(binary),
        mcp_slash=OPENCODE_MCP_SLASH,
        detail=detail,
        version=version or "unknown",
        skills=found.skills,
        bundled_skills=found.bundled_skills,
        release_check=release_check("opencode", version, binary=binary),
    )


_system_skills_cache: tuple[float, tuple[str, ...]] | None = None


_system_skills_lock = threading.Lock()


def opencode_system_skills(env: Mapping[str, str] | None = None) -> list[str]:
    """Skills the opencode CLI loads (user, global and built-in), for the slash picker.

    Read from a private server and cached for ``_SYSTEM_SKILLS_TTL_SECONDS``,
    failures included, so a broken opencode does not stall every picker load.
    """
    global _system_skills_cache
    with _system_skills_lock:
        now = time.monotonic()
        cached = _system_skills_cache
        if cached is not None and now - cached[0] < _SYSTEM_SKILLS_TTL_SECONDS:
            return list(cached[1])
        names: list[str] = []
        try:
            binary = resolve_opencode_binary(env)
        except OSError:
            binary = None
        if binary:
            skill_rows, _plugins = _fetch_inventory_rows(binary)
            if skill_rows is not None:
                names = _row_names(skill_rows)
        _system_skills_cache = (now, tuple(names))
        return names


