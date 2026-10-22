"""Read-only MCP inventory from provider-native configuration and sessions."""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any


def _jsonc_to_json(text: str) -> str:
    """Remove JSONC comments and trailing commas without touching strings."""
    out: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < len(text):
            next_char = text[index + 1]
            if next_char == "/":
                index += 2
                while index < len(text) and text[index] not in "\r\n":
                    index += 1
                continue
            if next_char == "*":
                end = text.find("*/", index + 2)
                if end < 0:
                    raise ValueError("unterminated JSONC comment")
                index = end + 2
                continue
        out.append(char)
        index += 1

    cleaned = "".join(out)
    out.clear()
    index = 0
    in_string = False
    escaped = False
    while index < len(cleaned):
        char = cleaned[index]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < len(cleaned) and cleaned[lookahead].isspace():
                lookahead += 1
            if lookahead < len(cleaned) and cleaned[lookahead] in "]}":
                index += 1
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _read_config(path: Path, *, jsonc: bool = False) -> tuple[dict[str, Any] | None, bool]:
    """Read an object config; second result says an existing source was unreadable."""
    try:
        if not path.is_file():
            return None, False
        raw = path.read_text(encoding="utf-8")
        data = json.loads(_jsonc_to_json(raw) if jsonc else raw)
    except FileNotFoundError:
        return None, False
    except (OSError, ValueError, TypeError):
        return None, True
    if not isinstance(data, dict):
        return None, True
    return data, False


def _candidate_paths(agent_root: Path, provider: str) -> list[tuple[Path, str, str, bool]]:
    """Return native config paths most-specific first, with safe scope labels."""
    candidates: list[tuple[Path, str, str, bool]] = []
    home = Path.home()
    if provider == "claude":
        candidates.extend(
            [
                (agent_root / ".mcp.json", "project", "project", False),
                (agent_root / ".claude" / "settings.local.json", "local", "local", False),
                (agent_root / ".claude" / "settings.json", "project", "project", False),
                (home / ".claude.json", "user", "user", False),
                (home / ".claude" / "settings.json", "user", "user", False),
            ]
        )
        managed = (
            Path("/Library/Application Support/ClaudeCode/managed-settings.json")
            if os.name == "posix" and __import__("sys").platform == "darwin"
            else Path("/etc/claude-code/managed-settings.json")
        )
        candidates.append((managed, "managed", "managed", False))
        return candidates

    explicit = os.environ.get("OPENCODE_CONFIG", "").strip()
    if explicit:
        explicit_path = Path(explicit).expanduser()
        candidates.append((explicit_path, "custom", "custom", explicit_path.suffix == ".jsonc"))
    config_dir = os.environ.get("OPENCODE_CONFIG_DIR", "").strip()
    if config_dir:
        root = Path(config_dir).expanduser()
        for filename in ("opencode.jsonc", "opencode.json"):
            candidates.append((root / filename, "custom", "custom", filename.endswith("jsonc")))

    current = agent_root
    while True:
        for base in (current, current / ".opencode"):
            for filename in ("opencode.jsonc", "opencode.json"):
                candidates.append(
                    (base / filename, "project", "project", filename.endswith("jsonc"))
                )
        if current.parent == current:
            break
        current = current.parent

    xdg_config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    for root, scope in ((xdg_config / "opencode", "user"), (home / ".opencode", "user")):
        for filename in ("opencode.jsonc", "opencode.json", "config.json"):
            candidates.append((root / filename, scope, scope, filename.endswith("jsonc")))
    seen: set[Path] = set()
    unique: list[tuple[Path, str, str, bool]] = []
    for candidate in candidates:
        resolved = candidate[0].expanduser()
        if resolved not in seen:
            seen.add(resolved)
            unique.append((resolved, *candidate[1:]))
    return unique


def _claude_servers(config: dict[str, Any], scope: str, agent_root: Path) -> dict[str, Any]:
    """Extract MCP records from Claude config shapes without returning values."""
    direct = config.get("mcpServers")
    servers: dict[str, Any] = dict(direct) if isinstance(direct, dict) else {}
    return servers


def _claude_project_servers(config: dict[str, Any], agent_root: Path) -> dict[str, Any]:
    projects = config.get("projects")
    entry = projects.get(str(agent_root)) if isinstance(projects, dict) else None
    local = entry.get("mcpServers") if isinstance(entry, dict) else None
    return dict(local) if isinstance(local, dict) else {}


def _server_kind(provider: str, config: dict[str, Any]) -> str:
    raw = config.get("type")
    if provider == "claude":
        if isinstance(raw, str) and raw in {"stdio", "http", "sse"}:
            return str(raw)
        return "http" if config.get("url") else "stdio"
    if isinstance(raw, str) and raw in {"local", "remote"}:
        return str(raw)
    return "remote" if config.get("url") else "local"


def _provider_inventory(
    *,
    provider: str,
    agent_root: Path,
    explicitly_denied: set[str],
    runtime: dict[str, list[dict[str, str]]],
    available: bool,
) -> dict[str, Any]:
    rows: dict[str, dict[str, Any]] = {}
    priorities: dict[str, int] = {}
    inventory_state = "loaded"
    for path, scope, source, jsonc in _candidate_paths(agent_root, provider):
        config, unreadable = _read_config(path, jsonc=jsonc)
        if unreadable:
            inventory_state = "unknown"
            continue
        if config is None:
            continue
        if provider == "claude":
            servers = _claude_servers(config, scope, agent_root)
            project_servers = _claude_project_servers(config, agent_root)
        else:
            raw_servers = config.get("mcp")
            if raw_servers is None:
                continue
            if not isinstance(raw_servers, dict):
                inventory_state = "unknown"
                continue
            nested = raw_servers.get("servers")
            if nested is not None:
                if not isinstance(nested, dict):
                    inventory_state = "unknown"
                    continue
                servers = nested
            else:
                servers = raw_servers
            project_servers = {}
        for raw_name, raw_config in servers.items():
            name = str(raw_name).strip()
            if not name or not isinstance(raw_config, dict):
                continue
            priority = _scope_priority(provider, scope)
            if name in rows and priorities[name] <= priority:
                continue
            if provider == "claude":
                kind = _server_kind(provider, raw_config)
                allowed: bool | None = False if name in explicitly_denied else None
                enabled = True
            else:
                kind = str(raw_config.get("type") or ("remote" if raw_config.get("url") else "local"))
                enabled = raw_config.get("disabled") is not True and raw_config.get("enabled") is not False
                allowed = None
            rows[name] = {
                "name": name,
                "scope": scope,
                "source": source,
                "kind": kind,
                "enabled": enabled,
                "allowed": allowed,
                "connections": [],
            }
            priorities[name] = priority
        for raw_name, raw_config in project_servers.items():
            name = str(raw_name).strip()
            if not name or not isinstance(raw_config, dict):
                continue
            priority = 0
            if name in rows and priorities[name] <= priority:
                continue
            explicitly_blocked = name in explicitly_denied
            rows[name] = {
                "name": name,
                "scope": "local",
                "source": "user",
                "kind": _server_kind(provider, raw_config),
                "enabled": True,
                "allowed": False if explicitly_blocked else None,
                "connections": [],
            }
            priorities[name] = priority

    for name, connections in runtime.items():
        row = rows.get(name)
        if row is None:
            row = {
                "name": name,
                "scope": "account" if provider == "claude" else "runtime",
                "source": "provider",
                "kind": "remote",
                "enabled": True,
                "allowed": None,
                "connections": [],
            }
            rows[name] = row
        row["connections"] = connections

    return {
        "provider": provider,
        "available": available,
        "inventory_state": inventory_state,
        "servers": sorted(rows.values(), key=lambda row: row["name"].casefold()),
    }


def _shell_prefix(agent_root: Path) -> dict[str, str]:
    root = str(agent_root)
    if os.name == "nt":
        return {
            "kind": "powershell",
            "cd_prefix": f"Set-Location -LiteralPath '{root.replace(chr(39), chr(39) * 2)}'; ",
        }
    return {"kind": "posix", "cd_prefix": f"cd -- {shlex.quote(root)} && "}


async def provider_inventory(
    *,
    workspace: str,
    agent_root: Path,
    explicitly_denied: set[str],
    manager: Any,
    claude_available: bool,
    opencode_available: bool,
) -> dict[str, Any]:
    """Build the settings payload from native config and live cached clients."""
    connections = (
        await manager.provider_mcp_runtime_connections(
            workspace,
            {
                "claude": _configured_names(agent_root, "claude"),
                "opencode": _configured_names(agent_root, "opencode"),
            },
        )
        if manager is not None
        else {"claude": {}, "opencode": {}}
    )
    claude_runtime = connections.get("claude", {})
    opencode_runtime = connections.get("opencode", {})
    claude = _provider_inventory(
        provider="claude",
        agent_root=agent_root,
        explicitly_denied=explicitly_denied,
        runtime=claude_runtime,
        available=claude_available,
    )
    opencode = _provider_inventory(
        provider="opencode",
        agent_root=agent_root,
        explicitly_denied=explicitly_denied,
        runtime=opencode_runtime,
        available=opencode_available,
    )
    return {
        "workspace": workspace,
        "agent_root": str(agent_root),
        "providers": [claude, opencode],
        "shell": _shell_prefix(agent_root),
    }


def _configured_names(agent_root: Path, provider: str) -> list[str]:
    """Names known from files, used to surface unknown live-session status."""
    names: list[str] = []
    for path, _scope, _source, jsonc in _candidate_paths(agent_root, provider):
        config, _unreadable = _read_config(path, jsonc=jsonc)
        if config is None:
            continue
        servers = (
            _claude_servers(config, _scope, agent_root)
            if provider == "claude"
            else config.get("mcp", {}).get("servers", config.get("mcp", {}))
            if isinstance(config.get("mcp"), dict)
            else {}
        )
        if isinstance(servers, dict):
            for name, server in servers.items():
                if isinstance(server, dict) and str(name) not in names:
                    names.append(str(name))
        if provider == "claude":
            for name, server in _claude_project_servers(config, agent_root).items():
                if isinstance(server, dict) and str(name) not in names:
                    names.append(str(name))
    return names


def _scope_priority(provider: str, scope: str) -> int:
    if provider == "claude":
        return {"local": 0, "project": 1, "user": 2, "managed": 3}.get(scope, 4)
    return {"custom": 0, "project": 1, "user": 2}.get(scope, 3)
