"""Turns one MCP server on or off in the provider's own config.

Claude Code: ``disabledMcpServers`` in the global config's project entry, under the CLI's own lock, with a one-time ``.ciao-bak`` copy. OpenCode: ``"disabled"`` (V2) or ``"enabled"`` (legacy) on the entry in the most specific file that defines it. It never adds or removes servers, never touches secrets, never rewrites JSONC with comments, and never removes a lock it did not create.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import tempfile
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ciao.agent_paths import claude_config_project_key, claude_global_config_path
from ciao.web.mcp_inventory import (
    _candidate_paths,
    _jsonc_to_json,
    _provider_inventory,
    _read_config,
    claude_disabled_servers,
)

SERVER_NAME_MAX = 128
CLAUDE_LOCK_WAIT_S = 5.0
CLAUDE_LOCK_POLL_S = 0.05
BACKUP_SUFFIX = ".ciao-bak"
UNREADABLE_MESSAGE = "The command line tool's settings file could not be read or changed, so nothing was changed."
BUSY_MESSAGE = "Claude Code is saving its settings. Try again in a moment."


class McpToggleError(Exception):
    def __init__(self, reason: str, message: str, status: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = message
        self.status = status


@dataclass(frozen=True)
class McpToggleResult:
    provider: str
    server: str
    enabled: bool
    changed: bool
    applies_to: str  # Claude: project key; OpenCode: config file path
    warning: str | None = None  # set when the effective state differs after the write

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _unreadable() -> McpToggleError:
    return McpToggleError("unreadable_config", UNREADABLE_MESSAGE, 409)


def _validate_server(server: object) -> None:
    if (
        not isinstance(server, str)
        or not server
        or server != server.strip()
        or len(server) > SERVER_NAME_MAX
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in server)
    ):
        raise McpToggleError("invalid_server", "Name an MCP server.", 400)


@contextlib.contextmanager
def _claude_config_lock(config_path: Path) -> Iterator[None]:
    lock = config_path.with_name(config_path.name + ".lock")
    deadline = time.monotonic() + CLAUDE_LOCK_WAIT_S
    while True:
        try:
            os.mkdir(lock)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise McpToggleError("config_busy", BUSY_MESSAGE, 409) from None
            time.sleep(CLAUDE_LOCK_POLL_S)
        except OSError:
            raise _unreadable() from None
    try:
        created = os.stat(lock)
    except OSError:
        with contextlib.suppress(OSError):
            os.rmdir(lock)
        raise _unreadable() from None
    identity = (created.st_ino, created.st_mtime_ns)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            current = os.stat(lock)
            if (current.st_ino, current.st_mtime_ns) == identity:
                os.rmdir(lock)


def _backup_once(path: Path) -> None:
    """Copy the original bytes once, owner-only, without following a link at the backup path."""
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    try:
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        return
    try:
        with os.fdopen(fd, "wb") as handle, path.open("rb") as source:
            shutil.copyfileobj(source, handle)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(backup)
        raise


def _json_text(data: Any) -> str:
    """The exact text to write, refused unless it reads back as the same data."""
    try:
        text = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    except ValueError:  # NaN or Infinity: the decoder's shared NaN object hides it from the comparison
        raise _unreadable() from None
    if json.loads(text) != data:
        raise _unreadable()
    return text


def _write_text_atomic(path: Path, text: str) -> None:
    target = path.resolve()
    mode = stat.S_IMODE(target.stat().st_mode)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _claude_managed_names(agent_root: Path) -> set[str]:
    for path, scope, _source, jsonc in _candidate_paths(agent_root, "claude"):
        if scope == "managed":
            config, _ = _read_config(path, jsonc=jsonc)
            servers = config.get("mcpServers") if config is not None else None
            return {str(name) for name in servers} if isinstance(servers, dict) else set()
    return set()


def _claude_edit(config_path: Path, key: str, server: str, enabled: bool) -> bool:
    try:
        data: Any = json.loads(config_path.read_text(encoding="utf-8"))
    except ValueError:
        raise _unreadable() from None
    if not isinstance(data, dict):
        raise _unreadable()
    projects: Any = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        raise _unreadable()
    entry: Any = projects.setdefault(key, {})
    if not isinstance(entry, dict):
        raise _unreadable()
    current = entry.get("disabledMcpServers")
    names = [n for n in current if isinstance(n, str)] if isinstance(current, list) else []
    if enabled:
        new = [n for n in names if n != server]
    else:
        new = names if server in names else [*names, server]
    changed = new != names
    if changed:
        entry["disabledMcpServers"] = new
        text = _json_text(data)
        _backup_once(config_path)
        _write_text_atomic(config_path, text)
    return changed


def _set_claude(server: str, agent_root: Path, enabled: bool, config_path: Path | None) -> McpToggleResult:
    if server in _claude_managed_names(agent_root):
        raise McpToggleError(
            "managed", f"{server} is managed by your organization and can't be turned off here.", 403
        )
    config_path = config_path or claude_global_config_path()
    if not config_path.is_file():
        raise _unreadable()
    # Lock, backup and write the real file, as Claude Code does (realpath), so a symlink keeps its link.
    config_path = config_path.resolve()
    key = claude_config_project_key(agent_root)
    try:
        with _claude_config_lock(config_path):
            changed = _claude_edit(config_path, key, server, enabled)
    except (OSError, ValueError):
        raise _unreadable() from None
    effective_on = server not in set(claude_disabled_servers(agent_root, config_path))
    warning = (
        None
        if effective_on == enabled
        else "Claude Code's global disabled list still turns it off; remove it there with /mcp in Claude Code."
    )
    return McpToggleResult("claude", server, enabled, changed, key, warning)


def _env_defines(content: str, server: str) -> bool:
    try:
        data: Any = json.loads(content)
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    mcp = data.get("mcp")
    if not isinstance(mcp, dict):
        return False
    servers = mcp["servers"] if "servers" in mcp else mcp
    return isinstance(servers, dict) and server in servers


def _opencode_warning(server: str, agent_root: Path, enabled: bool) -> str | None:
    rows = _provider_inventory(
        provider="opencode", agent_root=agent_root, explicitly_denied=set(), runtime={}, available=True
    )["servers"]
    row = next((r for r in rows if r["name"] == server), None)
    if row is None or row["enabled"] == enabled:
        return None
    return f"Another OpenCode config file still turns {server} {'off' if enabled else 'on'}."


def _set_opencode(server: str, agent_root: Path, enabled: bool) -> McpToggleResult:
    if _env_defines(os.environ.get("OPENCODE_CONFIG_CONTENT", ""), server):
        raise McpToggleError(
            "env_config", f"OPENCODE_CONFIG_CONTENT in Ciaobot's environment defines {server}; change it there.", 409
        )
    for path, _scope, _source, _jsonc in _candidate_paths(agent_root, "opencode"):
        if not path.is_file():
            continue
        try:
            raw = path.read_text(encoding="utf-8")
            cleaned = _jsonc_to_json(raw)
            data: Any = json.loads(cleaned)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        mcp = data.get("mcp")
        if not isinstance(mcp, dict):
            continue
        nested = mcp.get("servers")
        v2 = nested is not None
        servers = nested if v2 else mcp
        if not isinstance(servers, dict):
            continue
        entry = servers.get(server)
        if not isinstance(entry, dict):
            continue
        on = entry.get("disabled") is not True and (v2 or entry.get("enabled") is not False)
        if on == enabled:
            return McpToggleResult(
                "opencode", server, enabled, False, str(path), _opencode_warning(server, agent_root, enabled)
            )
        if cleaned != raw:
            raise McpToggleError(
                "config_has_comments",
                f"{path.name} has comments or trailing commas, so Ciaobot leaves it alone. Change {server} in that file yourself.",
                409,
            )
        if v2:
            entry.pop("enabled", None)
            if enabled:
                entry.pop("disabled", None)
            else:
                entry["disabled"] = True
        else:
            entry.pop("disabled", None)
            entry["enabled"] = enabled
        text = _json_text(data)
        try:
            _write_text_atomic(path, text)
        except (OSError, ValueError):
            raise _unreadable() from None
        return McpToggleResult(
            "opencode", server, enabled, True, str(path), _opencode_warning(server, agent_root, enabled)
        )
    raise McpToggleError(
        "not_configured",
        f"No OpenCode config file for this workspace defines {server}. Turn it off where it is defined.",
        404,
    )


def set_mcp_server_enabled(
    *,
    provider: str,
    server: str,
    agent_root: Path,
    enabled: bool,
    claude_config_path: Path | None = None,
) -> McpToggleResult:
    """Turn ``server`` on or off for ``provider`` in ``agent_root``. Blocking; call via ``asyncio.to_thread``."""
    _validate_server(server)
    if provider == "claude":
        return _set_claude(server, agent_root, enabled, claude_config_path)
    if provider == "opencode":
        return _set_opencode(server, agent_root, enabled)
    raise McpToggleError("invalid_provider", "Choose claude or opencode.", 400)
