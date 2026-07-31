"""Authenticated MCP adapter for Ciaobot's application control plane."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, cast

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

from ciao.control_plane import CiaoControlPlane, ControlPlaneError, McpPrincipal


logger = logging.getLogger(__name__)

_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_MCP_HTTP_PROBE_TIMEOUT_S = 3.0


def _workspace_env_path(workspace_root: Path) -> Path:
    return workspace_root.resolve() / ".env"


def _read_dotenv_value(env_path: Path, key: str) -> str:
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        env_key, value = line.split("=", 1)
        if env_key.strip() == key:
            return value.strip().strip("'\"")
    return ""


def _env_key_configured(key: str, workspace_root: Path) -> bool:
    if os.environ.get(key, "").strip():
        return True
    return bool(_read_dotenv_value(_workspace_env_path(workspace_root), key))


def _collect_env_refs(value: Any, *, default_source: str = "config") -> list[tuple[str, str]]:
    """Collect ``${VAR}`` references and explicit ``env`` map keys from MCP config."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(key: str, source: str) -> None:
        key = str(key).strip()
        if not key or key in seen:
            return
        seen.add(key)
        found.append((key, source))

    def walk(node: Any, source: str) -> None:
        if isinstance(node, str):
            for match in _ENV_REF_RE.finditer(node):
                add(match.group(1), source)
            return
        if isinstance(node, dict):
            for key, child in node.items():
                if source == "env":
                    # Keys under mcpServers.<name>.env are themselves env vars.
                    add(str(key), "env")
                    walk(child, "env")
                    continue
                child_source = source
                if source == "config" and key in {"headers", "env", "args", "url", "command"}:
                    child_source = str(key)
                walk(child, child_source)
            return
        if isinstance(node, (list, tuple)):
            for child in node:
                walk(child, source)

    walk(value, default_source)
    return found


def _resolve_env_template(value: str, workspace_root: Path) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        return os.environ.get(key, "").strip() or _read_dotenv_value(
            _workspace_env_path(workspace_root), key
        )

    return _ENV_REF_RE.sub(repl, value)


def _observed_project_mcp_tools(runtime_root: Path, server_name: str) -> list[str]:
    """Return tool names seen in agent telemetry for a project MCP server."""
    path = runtime_root / "agent_tool_calls.jsonl"
    if not path.is_file():
        return []
    prefix = f"mcp__{server_name}__"
    bare = f"mcp__{server_name}"
    found: set[str] = set()
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (TypeError, ValueError):
                    continue
                tool = str(record.get("tool") or "")
                if tool.startswith(prefix) or tool == bare:
                    found.add(tool)
    except OSError:
        return []
    return sorted(found)


def _probe_http_mcp_tools(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout_s: float = _MCP_HTTP_PROBE_TIMEOUT_S,
) -> tuple[list[str], str]:
    """Best-effort ``tools/list`` against an HTTP/SSE MCP endpoint.

    Returns ``(tool_names, error)``. Empty tools with an empty error means the
    server responded but advertised no tools.
    """
    endpoint = url.strip()
    if not endpoint:
        return [], "missing url"
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    ).encode("utf-8")
    req_headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        **(headers or {}),
    }
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers=req_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read().decode("utf-8", errors="replace")
            content_type = (response.headers.get("content-type") or "").lower()
    except urllib.error.HTTPError as exc:
        return [], f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 — probe is best-effort
        return [], str(exc) or exc.__class__.__name__

    body = raw
    if "text/event-stream" in content_type or raw.lstrip().startswith("event:"):
        data_lines: list[str] = []
        for line in raw.splitlines():
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
        body = "\n".join(data_lines)
    try:
        message = json.loads(body)
    except (TypeError, ValueError):
        return [], "non-JSON tools/list response"
    if not isinstance(message, dict):
        return [], "unexpected tools/list payload"
    if message.get("error"):
        err = message["error"]
        if isinstance(err, dict):
            return [], str(err.get("message") or err.get("code") or err)
        return [], str(err)
    result = message.get("result") or {}
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        return [], "tools/list missing tools array"
    names = sorted(
        {
            str(tool.get("name") or "").strip()
            for tool in tools
            if isinstance(tool, dict) and str(tool.get("name") or "").strip()
        }
    )
    return names, ""


_READ = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
_DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)


@dataclass(slots=True)
class _Session:
    principal: McpPrincipal
    token: str
    expires_at: int


class McpSessionRegistry:
    """In-memory verifier for short-lived managed-process bearer tokens."""

    def __init__(self, ttl_seconds: int = 12 * 60 * 60) -> None:
        self._ttl_seconds = max(60, int(ttl_seconds))
        self._by_token: dict[str, _Session] = {}
        self._by_key: dict[tuple[str, str, str], str] = {}
        self._lock = threading.RLock()

    def issue(
        self,
        *,
        chat_id: str,
        project_id: str,
        workspace: str,
        provider: str,
        role: str = "chat",
        handoff_depth: int = 0,
    ) -> tuple[str, McpPrincipal]:
        key = (chat_id, provider, role)
        now = int(time.time())
        with self._lock:
            existing_token = self._by_key.get(key)
            existing = self._by_token.get(existing_token or "")
            if existing is not None and existing.expires_at > now:
                prior = existing.principal
                # Reuse only when scope still matches. A moved project or
                # corrected workspace must not keep minting stale-scoped tools.
                if (
                    prior.project_id == project_id
                    and prior.workspace == workspace
                    and prior.handoff_depth == handoff_depth
                ):
                    return existing.token, existing.principal
                # revoke() owns the _by_token/_by_key pairing; the lock is an
                # RLock so re-entering it here is safe.
                self.revoke(existing.token)
            token = secrets.token_urlsafe(36)
            principal = McpPrincipal(
                token_id=secrets.token_hex(8),
                chat_id=chat_id,
                project_id=project_id,
                workspace=workspace,
                provider=provider,
                role=role,  # type: ignore[arg-type]
                handoff_depth=handoff_depth,
            )
            session = _Session(
                principal=principal,
                token=token,
                expires_at=now + self._ttl_seconds,
            )
            self._by_token[token] = session
            self._by_key[key] = token
            return token, principal

    def revoke_chat(self, chat_id: str) -> int:
        with self._lock:
            doomed = [token for token, item in self._by_token.items() if item.principal.chat_id == chat_id]
            for token in doomed:
                item = self._by_token.pop(token)
                self._by_key.pop((item.principal.chat_id, item.principal.provider, item.principal.role), None)
            return len(doomed)

    def revoke(self, token: str) -> bool:
        with self._lock:
            item = self._by_token.pop(token, None)
            if item is None:
                return False
            self._by_key.pop((item.principal.chat_id, item.principal.provider, item.principal.role), None)
            return True

    async def verify_token(self, token: str) -> AccessToken | None:
        now = int(time.time())
        with self._lock:
            item = self._by_token.get(token)
            if item is None:
                return None
            if item.expires_at <= now:
                self.revoke(token)
                return None
            return AccessToken(
                token=token,
                client_id="ciaobot-managed-provider",
                scopes=["ciaobot"],
                expires_at=item.expires_at,
                subject=item.principal.chat_id,
                claims=item.principal.to_claims(),
            )

    def status(self) -> dict[str, Any]:
        now = int(time.time())
        with self._lock:
            active = [item for item in self._by_token.values() if item.expires_at > now]
            return {
                "active_sessions": len(active),
                "providers": sorted({item.principal.provider for item in active}),
                "chats": sorted({item.principal.chat_id for item in active}),
            }


class CiaoMcpService:
    """Own the FastMCP server, authentication, tool catalog, and telemetry."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self.registry = McpSessionRegistry()
        self.control_plane: CiaoControlPlane | None = None
        self._tool_names: set[str] = set()
        self._last_error = ""
        self._telemetry_path = Path(config.state_path).parent / "mcp_tool_calls.jsonl"
        issuer = f"http://127.0.0.1:{int(config.pwa_port)}"
        self.server = FastMCP(
            "ciaobot",
            instructions=(
                "Use these tools for Ciaobot memory, vault, projects, chats, "
                "schedules, loops, files, and application state. Prefer them "
                "over curl, the ciao CLI, or direct .runtime edits. All paths "
                "are relative to the active workspace or vault."
            ),
            host="127.0.0.1",
            streamable_http_path="/",
            json_response=True,
            stateless_http=True,
            token_verifier=self.registry,
            auth=AuthSettings(
                # pydantic coerces the str to AnyHttpUrl during validation.
                issuer_url=cast(AnyHttpUrl, issuer),
                required_scopes=["ciaobot"],
                resource_server_url=None,
            ),
        )
        self._register_tools()
        self.http_app = self.server.streamable_http_app()

    def bind(self, control_plane: CiaoControlPlane) -> None:
        self.control_plane = control_plane

    @property
    def url(self) -> str:
        # Starlette's Mount canonicalizes the inner root to a trailing slash.
        return f"http://127.0.0.1:{int(self.config.pwa_port)}/mcp/"

    def credentials_for_chat(self, chat: Any, project: Any, *, role: str = "chat") -> tuple[str, str]:
        token, _principal = self.registry.issue(
            chat_id=chat.chat_id,
            project_id=chat.project_id,
            workspace=project.workspace,
            provider=chat.provider,
            role=role,
            handoff_depth=1 if role == "handoff" else 0,
        )
        return self.url, token

    @asynccontextmanager
    async def lifespan(self):
        async with self.server.session_manager.run():
            yield

    def status(self) -> dict[str, Any]:
        workspace_root = Path(getattr(self.config, "workspace_root", Path.cwd())).resolve()
        return {
            "enabled": bool(getattr(self.config, "mcp_enabled", True)),
            "url": self.url,
            "bound": self.control_plane is not None,
            "tool_count": len(self._tool_names),
            "tools": sorted(self._tool_names),
            "last_error": self._last_error,
            "env_path": str(_workspace_env_path(workspace_root)),
            "project_servers": self._discover_project_mcp_servers(),
            **self.registry.status(),
        }

    def project_server_env_keys(self) -> set[str]:
        """Env var names referenced by discovered project MCP server configs."""
        keys: set[str] = set()
        for server in self._discover_project_mcp_servers():
            for entry in server.get("env_keys") or []:
                if isinstance(entry, dict) and entry.get("key"):
                    keys.add(str(entry["key"]))
        return keys

    def probe_project_server_tools(self, name: str) -> dict[str, Any]:
        """Lazy tools discovery for one project MCP server.

        HTTP/SSE servers are probed with ``tools/list``. Stdio servers return
        observed telemetry tools only (spawning the command from Settings is
        intentionally avoided).
        """
        servers = {str(s.get("name")): s for s in self._discover_project_mcp_servers()}
        server = servers.get(name)
        if server is None:
            return {"ok": False, "error": f"unknown MCP server '{name}'", "tools": []}
        observed = list(server.get("tools") or [])
        if server.get("transport") != "http":
            return {
                "ok": True,
                "name": name,
                "tools": observed,
                "tools_source": server.get("tools_source") or ("observed" if observed else "none"),
                "tools_note": (
                    "Stdio MCP tools are discovered when a chat loads the server. "
                    f"They surface as {server.get('tool_prefix')}*."
                ),
                "tool_prefix": server.get("tool_prefix"),
            }
        if not server.get("ready", True):
            return {
                "ok": False,
                "name": name,
                "error": "Configure the required .env keys before probing tools.",
                "tools": observed,
                "tools_source": server.get("tools_source") or ("observed" if observed else "none"),
                "tool_prefix": server.get("tool_prefix"),
            }
        workspace_root = Path(getattr(self.config, "workspace_root", Path.cwd())).resolve()
        raw_meta = server.get("_meta")
        meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        raw_headers = meta.get("headers")
        headers_raw: dict[str, Any] = raw_headers if isinstance(raw_headers, dict) else {}
        headers = {
            str(k): _resolve_env_template(str(v), workspace_root)
            for k, v in headers_raw.items()
        }
        tools, error = _probe_http_mcp_tools(str(server.get("url") or ""), headers=headers)
        prefixed = [f"mcp__{name}__{tool}" for tool in tools]
        merged = sorted(set(observed) | set(prefixed) | set(tools))
        if error and not merged:
            return {
                "ok": False,
                "name": name,
                "error": error,
                "tools": observed,
                "tools_source": "observed" if observed else "none",
                "tool_prefix": server.get("tool_prefix"),
            }
        return {
            "ok": True,
            "name": name,
            "tools": merged,
            "tools_source": "probed" if tools else ("observed" if observed else "none"),
            "tools_note": "" if tools else (error or ""),
            "tool_prefix": server.get("tool_prefix"),
        }

    def _discover_project_mcp_servers(self) -> list[dict[str, Any]]:
        servers: list[dict[str, Any]] = []
        workspace_root = Path(getattr(self.config, "workspace_root", Path.cwd())).resolve()
        env_path = _workspace_env_path(workspace_root)
        runtime_root = Path(getattr(self.config, "state_path", workspace_root / ".runtime" / "state.json")).parent
        candidates: list[tuple[str, Path]] = [
            ("project", workspace_root / ".mcp.json"),
            ("project", workspace_root.parent / ".mcp.json"),
            ("project", workspace_root.parent / "ciao" / ".mcp.json"),
        ]

        seen: set[str] = set()
        for source, path in candidates:
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            mcp_dict = data.get("mcpServers") or data.get("mcp_servers") or {}
            if not isinstance(mcp_dict, dict):
                continue
            for name, meta in mcp_dict.items():
                server_name = str(name)
                if server_name in seen:
                    continue
                seen.add(server_name)
                meta_dict = meta if isinstance(meta, dict) else {}
                url = str(meta_dict.get("url", "") or "")
                command = str(meta_dict.get("command", "") or "")
                args_raw = meta_dict.get("args") or []
                args = [str(item) for item in args_raw] if isinstance(args_raw, list) else []
                transport = "http" if url else "stdio"
                env_refs = _collect_env_refs(meta_dict)
                env_keys = [
                    {
                        "key": key,
                        "configured": _env_key_configured(key, workspace_root),
                        "source": ref_source,
                    }
                    for key, ref_source in env_refs
                ]
                ready = all(entry["configured"] for entry in env_keys)
                observed = _observed_project_mcp_tools(runtime_root, server_name)
                tool_prefix = f"mcp__{server_name}__"
                payload: dict[str, Any] = {
                    "name": server_name,
                    "url": url,
                    "command": command,
                    "args": args,
                    "transport": transport,
                    "source": f"{source} ({path.parent.name})",
                    "config_path": str(path.resolve()),
                    "env_path": str(env_path),
                    "env_keys": env_keys,
                    "ready": ready,
                    "tool_prefix": tool_prefix,
                    "tools": observed,
                    "tools_source": "observed" if observed else "none",
                    "tools_note": (
                        ""
                        if observed
                        else (
                            f"Tools load when a chat starts this server "
                            f"(prefix {tool_prefix}*)."
                            if ready
                            else "Add the required .env keys below, then start a chat that uses this server."
                        )
                    ),
                    # Internal probe helper; stripped from status responses.
                    "_meta": meta_dict,
                }
                servers.append(payload)
        return servers

    def status_for_api(self) -> dict[str, Any]:
        """Public status payload without internal probe helpers."""
        payload = self.status()
        servers = []
        for server in payload.get("project_servers") or []:
            if not isinstance(server, dict):
                continue
            public = {k: v for k, v in server.items() if not str(k).startswith("_")}
            # Source is always project-scoped for this list; keep it out of the UI payload.
            public.pop("source", None)
            servers.append(public)
        payload["project_servers"] = servers
        return payload

    def _workspace_root(self) -> Path:
        return Path(getattr(self.config, "workspace_root", Path.cwd())).resolve()

    def _project_mcp_json_candidates(self) -> list[Path]:
        workspace_root = self._workspace_root()
        return [
            workspace_root / ".mcp.json",
            workspace_root.parent / ".mcp.json",
            # Sibling checkout used by some local monorepo layouts.
            workspace_root.parent / "ciao" / ".mcp.json",
        ]

    def _preferred_mcp_json_path(self, *, create: bool = False) -> Path | None:
        """Prefer an existing project ``.mcp.json`` that already has servers."""
        for path in self._project_mcp_json_candidates():
            if not path.is_file():
                continue
            try:
                data = self._read_mcp_json(path)
                servers = data.get("mcpServers") or data.get("mcp_servers") or {}
            except ValueError:
                continue
            if isinstance(servers, dict) and servers:
                return path
        for path in self._project_mcp_json_candidates():
            if path.is_file():
                return path
        if create:
            return self._workspace_root() / ".mcp.json"
        return None

    def _read_mcp_json(self, path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError(f"invalid .mcp.json at {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"invalid .mcp.json at {path}: expected object")
        return data

    def _write_mcp_json(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def _mcp_servers_dict(self, data: dict[str, Any]) -> dict[str, Any]:
        raw = data.get("mcpServers")
        if raw is None:
            raw = data.get("mcp_servers")
        if raw is None:
            servers: dict[str, Any] = {}
            data["mcpServers"] = servers
            return servers
        if not isinstance(raw, dict):
            raise ValueError(".mcp.json mcpServers must be an object")
        if "mcpServers" not in data and "mcp_servers" in data:
            # Normalize legacy key on write.
            data["mcpServers"] = raw
            data.pop("mcp_servers", None)
        return raw

    def _find_server_file(self, name: str) -> tuple[Path, dict[str, Any], dict[str, Any]] | None:
        for path in self._project_mcp_json_candidates():
            if not path.is_file():
                continue
            try:
                data = self._read_mcp_json(path)
                servers = self._mcp_servers_dict(data)
            except ValueError:
                continue
            if name in servers:
                return path, data, servers
        return None

    def upsert_project_server(
        self,
        name: str,
        *,
        url: str = "",
        command: str = "",
        args: list[str] | None = None,
        env_keys: dict[str, str] | None = None,
        bind_env_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create or update a project MCP server in ``.mcp.json``."""
        server_name = str(name or "").strip()
        if not server_name or "/" in server_name or "\\" in server_name:
            raise ValueError("invalid MCP server name")
        url = str(url or "").strip()
        command = str(command or "").strip()
        args_list = [str(item).strip() for item in (args or []) if str(item).strip()]
        if url and command:
            raise ValueError("provide either url or command, not both")
        if not url and not command:
            raise ValueError("url or command is required")

        found = self._find_server_file(server_name)
        if found:
            path, data, servers = found
            meta = servers.get(server_name)
            meta = dict(meta) if isinstance(meta, dict) else {}
        else:
            preferred = self._preferred_mcp_json_path(create=True)
            if preferred is None:
                raise ValueError("no writable .mcp.json location is available")
            path = preferred
            data = self._read_mcp_json(path) if path.is_file() else {}
            servers = self._mcp_servers_dict(data)
            if server_name in servers:
                meta = dict(servers[server_name]) if isinstance(servers[server_name], dict) else {}
            else:
                meta = {}

        if url:
            meta["type"] = "http"
            meta["url"] = url
            meta.pop("command", None)
            meta.pop("args", None)
        else:
            meta.pop("type", None)
            meta.pop("url", None)
            meta["command"] = command
            if args_list:
                meta["args"] = args_list
            else:
                meta.pop("args", None)

        keys_to_bind = [str(k).strip() for k in (bind_env_keys or []) if str(k).strip()]
        if env_keys:
            keys_to_bind.extend(str(k).strip() for k in env_keys if str(k).strip())
        if keys_to_bind:
            raw_env = meta.get("env")
            env_map: dict[str, Any] = dict(raw_env) if isinstance(raw_env, dict) else {}
            for key in keys_to_bind:
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                    raise ValueError(f"invalid env key '{key}'")
                # Keep existing header/env templates; only add missing env bindings.
                existing_refs = {ref for ref, _src in _collect_env_refs(meta)}
                if key not in existing_refs:
                    env_map[key] = f"${{{key}}}"
            if env_map:
                meta["env"] = env_map

        servers[server_name] = meta
        data["mcpServers"] = servers
        self._write_mcp_json(path, data)

        if env_keys:
            updates = {
                str(k).strip(): str(v)
                for k, v in env_keys.items()
                if str(k).strip() and str(v).strip()
            }
            if updates:
                env_path = _workspace_env_path(self._workspace_root())
                _write_mcp_env_values(env_path, updates)
                for key, value in updates.items():
                    os.environ[key] = value.strip()

        return self.status_for_api()

    def delete_project_server(self, name: str) -> dict[str, Any]:
        found = self._find_server_file(name)
        if found is None:
            raise ValueError(f"unknown MCP server '{name}'")
        path, data, servers = found
        servers.pop(name, None)
        data["mcpServers"] = servers
        self._write_mcp_json(path, data)
        return self.status_for_api()

    def save_project_server_env_keys(
        self,
        updates: dict[str, str],
        *,
        server: str | None = None,
        bind_missing: bool = True,
    ) -> dict[str, Any]:
        """Write MCP secrets to ``.env`` and optionally bind new keys into ``.mcp.json``."""
        cleaned = {
            str(k).strip(): str(v)
            for k, v in updates.items()
            if str(k).strip()
        }
        if not cleaned:
            raise ValueError("no keys to save")
        for key in cleaned:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError(f"invalid env key '{key}'")

        allowed = self.project_server_env_keys()
        unknown = sorted(set(cleaned) - allowed)
        if unknown:
            if not server:
                raise ValueError(
                    f"unsupported MCP env key(s): {', '.join(unknown)}. "
                    "Pass server=<name> to bind new keys into that MCP config."
                )
            if not bind_missing:
                raise ValueError(f"unsupported MCP env key(s): {', '.join(unknown)}")
            found = self._find_server_file(server)
            if found is None:
                raise ValueError(f"unknown MCP server '{server}'")
            path, data, servers = found
            meta = dict(servers.get(server) or {})
            env_map = dict(meta.get("env") or {}) if isinstance(meta.get("env"), dict) else {}
            for key in unknown:
                env_map[key] = f"${{{key}}}"
            meta["env"] = env_map
            servers[server] = meta
            data["mcpServers"] = servers
            self._write_mcp_json(path, data)

        env_path = _workspace_env_path(self._workspace_root())
        _write_mcp_env_values(env_path, cleaned)
        for key, value in cleaned.items():
            value = value.strip()
            if value:
                os.environ[key] = value
            else:
                os.environ.pop(key, None)
        return self.status_for_api()

    def usage(self, *, limit: int | None = None) -> dict[str, Any]:
        """Aggregate per-tool call counts from the telemetry log.

        Reads ``mcp_tool_calls.jsonl`` (written by :meth:`_record_tool_call`) and
        groups the records by tool name so the PWA can render a usage table.
        """
        tools: dict[str, dict[str, Any]] = {}
        total = 0
        total_errors = 0
        if self._telemetry_path.exists():
            try:
                with self._telemetry_path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except (ValueError, TypeError):
                            continue
                        name = str(record.get("tool") or "")
                        if not name:
                            continue
                        entry = tools.setdefault(
                            name,
                            {"calls": 0, "errors": 0, "total_ms": 0, "providers": set(), "last_used": ""},
                        )
                        entry["calls"] += 1
                        total += 1
                        if record.get("status") != "ok":
                            entry["errors"] += 1
                            total_errors += 1
                        try:
                            entry["total_ms"] += int(record.get("duration_ms") or 0)
                        except (ValueError, TypeError):
                            pass
                        provider = str(record.get("provider") or "")
                        if provider:
                            entry["providers"].add(provider)
                        timestamp = str(record.get("timestamp") or "")
                        if timestamp > entry["last_used"]:
                            entry["last_used"] = timestamp
            except OSError:
                pass
        rows: list[dict[str, Any]] = []
        for name, entry in tools.items():
            calls = entry["calls"]
            rows.append(
                {
                    "tool": name,
                    "calls": calls,
                    "errors": entry["errors"],
                    "avg_ms": int(entry["total_ms"] / calls) if calls else 0,
                    "providers": sorted(entry["providers"]),
                    "last_used": entry["last_used"],
                }
            )
        # Include registered tools that have never been called so the table
        # reflects the full catalog rather than only what has run so far.
        for name in self._tool_names:
            if name not in tools:
                rows.append(
                    {"tool": name, "calls": 0, "errors": 0, "avg_ms": 0, "providers": [], "last_used": ""}
                )
        rows.sort(key=lambda item: (item["calls"], item["tool"]), reverse=True)
        if limit is not None:
            rows = rows[:limit]
        return {
            "total_calls": total,
            "total_errors": total_errors,
            "tool_count": len(self._tool_names),
            "tools": rows,
        }

    def _principal(self) -> McpPrincipal:
        access = get_access_token()
        if access is None or not isinstance(access.claims, dict):
            raise ControlPlaneError("unauthorized", "A managed Ciaobot MCP session is required.")
        principal = McpPrincipal.from_claims(access.claims)
        if not principal.token_id:
            raise ControlPlaneError("unauthorized", "The MCP session has no principal.")
        return principal

    async def _invoke(
        self,
        name: str,
        operation: Callable[[CiaoControlPlane, McpPrincipal], Any],
        *,
        mutating: bool = False,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        principal: McpPrincipal | None = None
        status = "ok"
        error_code = ""
        try:
            if self.control_plane is None:
                raise ControlPlaneError("unavailable", "Ciaobot control plane is not ready.", retryable=True)
            principal = self._principal()
            if mutating and principal.role == "handoff":
                raise ControlPlaneError(
                    "handoff_read_only",
                    "Agent handoff participants have read-only Ciaobot access.",
                )
            if mutating and self.control_plane.chat_mode(principal) == "plan":
                raise ControlPlaneError("plan_mode_read_only", "Mutating Ciaobot tools are disabled in plan mode.")
            value = operation(self.control_plane, principal)
            if inspect.isawaitable(value):
                value = await value
            if isinstance(value, dict):
                return value
            return {"ok": True, "data": value}
        except ControlPlaneError as exc:
            status = "error"
            error_code = exc.code
            self._last_error = str(exc)
            return {"ok": False, "error": exc.payload()}
        except (ValueError, KeyError, LookupError) as exc:
            status = "error"
            error_code = "invalid_request"
            self._last_error = str(exc)
            return {
                "ok": False,
                "error": {"code": error_code, "message": str(exc), "retryable": False},
            }
        except Exception as exc:  # noqa: BLE001 - tool boundary must be fail-safe
            status = "error"
            error_code = "internal_error"
            self._last_error = str(exc)
            logger.exception("Ciaobot MCP tool %s failed internally", name)
            return {
                "ok": False,
                "error": {
                    "code": error_code,
                    "message": "Ciaobot could not complete the operation.",
                    "retryable": True,
                },
            }
        finally:
            self._record_tool_call(
                name=name,
                principal=principal,
                status=status,
                error_code=error_code,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

    def _record_tool_call(
        self,
        *,
        name: str,
        principal: McpPrincipal | None,
        status: str,
        error_code: str,
        duration_ms: int,
    ) -> None:
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "surface": "mcp",
            "tool": name,
            "token_id": principal.token_id if principal else "",
            "chat_id": principal.chat_id if principal else "",
            "provider": principal.provider if principal else "",
            "status": status,
            "error_code": error_code,
            "duration_ms": duration_ms,
        }
        try:
            self._telemetry_path.parent.mkdir(parents=True, exist_ok=True)
            with self._telemetry_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError:
            pass

    def _tool(self, *args: Any, **kwargs: Any):
        name = str(kwargs.get("name") or (args[0] if args else ""))
        if name:
            self._tool_names.add(name)
        return self.server.tool(*args, **kwargs)

    def _register_tools(self) -> None:  # noqa: C901 - catalog is intentionally explicit
        tool = self._tool

        @tool(name="context_get", annotations=_READ, structured_output=True)
        async def context_get() -> dict[str, Any]:
            """Return the active Ciaobot workspace, project, chat, provider, and
            control surface, plus local server/startup/active-chat status folded
            in under the ``system`` key (the former system_status_get)."""

            def _op(cp: CiaoControlPlane, p: McpPrincipal) -> dict[str, Any]:
                result = cp.context_get(p)
                status = cp.system_status_get(p)
                data = result.get("data") if isinstance(result, dict) else None
                if isinstance(data, dict):
                    data["system"] = (
                        status.get("data") if isinstance(status, dict) else status
                    )
                return result

            return await self._invoke("context_get", _op)

        # Bounded-memory read/add/replace/remove are the `ciao memory` CLI
        # subcommands; keep only the proposal-review flow on MCP.
        @tool(name="memory_proposals_list", annotations=_READ, structured_output=True)
        async def memory_proposals_list() -> dict[str, Any]:
            """List reviewable memory proposals produced from archived chats."""
            return await self._invoke(
                "memory_proposals_list", lambda cp, p: cp.memory_proposals_list(p)
            )

        @tool(name="memory_proposal_resolve", annotations=_WRITE, structured_output=True)
        async def memory_proposal_resolve(
            text: str,
            action: str,
            target: str = "",
        ) -> dict[str, Any]:
            """Accept or reject one proposal selected by a unique text substring."""
            return await self._invoke(
                "memory_proposal_resolve",
                lambda cp, p: cp.memory_proposal_resolve(
                    p, text, action=action, target=target  # type: ignore[arg-type]
                ),
                mutating=True,
            )

        @tool(name="vault_search", annotations=_READ, structured_output=True)
        async def vault_search(query: str, limit: int = 10) -> dict[str, Any]:
            """Full-text search the active workspace vault."""
            return await self._invoke("vault_search", lambda cp, p: cp.vault_search(p, query, limit))

        # vault_index_refresh -> `ciao index`; vault_lint -> `ciao lint`.

        @tool(name="projects_list", annotations=_READ, structured_output=True)
        async def projects_list(include_completed: bool = False) -> dict[str, Any]:
            """List projects in the active workspace."""
            return await self._invoke("projects_list", lambda cp, p: cp.projects_list(p, include_completed))

        @tool(name="project_get", annotations=_READ, structured_output=True)
        async def project_get(project_id: str = "") -> dict[str, Any]:
            """Get one project by ID or name within the active workspace. Omit to get the active project."""
            return await self._invoke("project_get", lambda cp, p: cp.project_get(p, project_id))

        @tool(name="project_create", annotations=_WRITE, structured_output=True)
        async def project_create(name: str, context: str = "") -> dict[str, Any]:
            """Create a project in the active workspace."""
            return await self._invoke("project_create", lambda cp, p: cp.project_create(p, name, context), mutating=True)

        @tool(name="project_update", annotations=_WRITE, structured_output=True)
        async def project_update(
            project_id: str = "",
            name: str | None = None,
            context: str | None = None,
            vault_folder: str | None = None,
        ) -> dict[str, Any]:
            """Update project metadata or its safe vault-folder binding. Omit project_id for active project."""
            return await self._invoke(
                "project_update",
                lambda cp, p: cp.project_update(
                    p, project_id, name=name, context=context, vault_folder=vault_folder
                ),
                mutating=True,
            )

        @tool(name="project_complete", annotations=_DESTRUCTIVE, structured_output=True)
        async def project_complete(project_id: str = "") -> dict[str, Any]:
            """Move a vault-backed project to completed and archive its active project record."""
            return await self._invoke("project_complete", lambda cp, p: cp.project_complete(p, project_id), mutating=True)

        @tool(name="project_restore", annotations=_WRITE, structured_output=True)
        async def project_restore(stem: str) -> dict[str, Any]:
            """Restore a completed vault project into the active workspace."""
            return await self._invoke("project_restore", lambda cp, p: cp.project_restore(p, stem), mutating=True)

        @tool(name="project_delete", annotations=_DESTRUCTIVE, structured_output=True)
        async def project_delete(project_id: str = "") -> dict[str, Any]:
            """Delete a non-vault-backed project and its chats."""
            return await self._invoke("project_delete", lambda cp, p: cp.project_delete(p, project_id), mutating=True)

        @tool(name="project_files_list", annotations=_READ, structured_output=True)
        async def project_files_list(project_id: str = "") -> dict[str, Any]:
            """List files inside a project's vault folder. Omit project_id for active project."""
            return await self._invoke("project_files_list", lambda cp, p: cp.project_files_list(p, project_id))

        @tool(name="chats_list", annotations=_READ, structured_output=True)
        async def chats_list(project_id: str = "") -> dict[str, Any]:
            """List active and archived chats in the active workspace or one project."""
            return await self._invoke("chats_list", lambda cp, p: cp.chats_list(p, project_id))

        @tool(name="chat_get", annotations=_READ, structured_output=True)
        async def chat_get(chat_id: str = "") -> dict[str, Any]:
            """Get one chat by ID within the active workspace. Omit to get the calling chat."""
            return await self._invoke("chat_get", lambda cp, p: cp.chat_get(p, chat_id))

        @tool(name="chat_create", annotations=_WRITE, structured_output=True)
        async def chat_create(
            project_id: str | None = None,
            title: str = "New Chat",
            provider: str | None = None,
            model: str | None = None,
            mode: str | None = None,
            control_surface: str | None = None,
            prompt: str | None = None,
        ) -> dict[str, Any]:
            """Create a fresh chat, optionally sending its first prompt in the same call.

            Args:
                project_id: Project id or case-insensitive name. Omit to use the
                    calling chat's own project — you don't need to call
                    projects_list first for the common case of a sub-topic in
                    the current project.
                provider: Provider override. Omit to inherit the target
                    project's workspace default.
                model: Model override. Omit to inherit the target project's
                    workspace default.
                prompt: If given, immediately starts the new chat's first turn
                    with this text — skips a separate chat_send call.
            """
            return await self._invoke(
                "chat_create",
                lambda cp, p: cp.chat_create(
                    p,
                    project_id,
                    title=title,
                    provider=provider,
                    model=model,
                    mode=mode,
                    control_surface=control_surface,  # type: ignore[arg-type]
                    prompt=prompt,
                ),
                mutating=True,
            )

        @tool(name="chat_update", annotations=_WRITE, structured_output=True)
        async def chat_update(
            chat_id: str = "",
            title: str | None = None,
            provider: str | None = None,
            model: str | None = None,
            mode: str | None = None,
            thinking_level: str | None = None,
            project_id: str | None = None,
            model_bucket: str | None = None,
            control_surface: str | None = None,
        ) -> dict[str, Any]:
            """Update chat metadata and same-backend model settings. Omit chat_id for calling chat."""
            return await self._invoke(
                "chat_update",
                lambda cp, p: cp.chat_update(
                    p,
                    chat_id,
                    title=title,
                    provider=provider,
                    model=model,
                    mode=mode,
                    thinking_level=thinking_level,
                    project_id=project_id,
                    model_bucket=model_bucket,
                    control_surface=control_surface,
                ),
                mutating=True,
            )

        @tool(name="chat_send", annotations=_WRITE, structured_output=True)
        async def chat_send(chat_id: str, prompt: str) -> dict[str, Any]:
            """Start or queue a user turn in another Ciaobot chat."""
            return await self._invoke("chat_send", lambda cp, p: cp.chat_send(p, chat_id, prompt), mutating=True)

        @tool(name="chat_continue", annotations=_WRITE, structured_output=True)
        async def chat_continue(chat_id: str) -> dict[str, Any]:
            """Continue an archived chat as a new active chat."""
            return await self._invoke("chat_continue", lambda cp, p: cp.chat_continue(p, chat_id), mutating=True)

        @tool(name="chat_retry", annotations=_WRITE, structured_output=True)
        async def chat_retry(
            chat_id: str = "",
            action: str = "try_now",
            prompt: str = "",
        ) -> dict[str, Any]:
            """Manage a deferred provider-limit retry: set, stop, or (default)
            immediately try it now."""
            return await self._invoke(
                "chat_retry",
                lambda cp, p: cp.chat_retry_update(
                    p, chat_id, action=action, prompt=prompt  # type: ignore[arg-type]
                ),
                mutating=True,
            )

        @tool(name="chat_handover", annotations=_WRITE, structured_output=True)
        async def chat_handover(
            chat_id: str = "",
            provider: str = "",
            model: str = "",
            messages: list[dict[str, Any]] | None = None,
            model_bucket: str = "",
        ) -> dict[str, Any]:
            """Continue a chat on a fresh provider session with optional visible
            history. With provider and model both empty, this just clears the
            current provider session in place (the former chat_new_session)."""

            def _op(cp: CiaoControlPlane, p: McpPrincipal) -> Any:
                if not provider and not model:
                    return cp.chat_new_session(p, chat_id)
                return cp.chat_handover(
                    p,
                    chat_id,
                    provider=provider,
                    model=model,
                    messages=messages,
                    model_bucket=model_bucket,
                )

            return await self._invoke("chat_handover", _op, mutating=True)

        @tool(name="chat_fork", annotations=_WRITE, structured_output=True)
        async def chat_fork(
            chat_id: str = "",
            messages: list[dict[str, Any]] | None = None,
            turn_index: int = 0,
        ) -> dict[str, Any]:
            """Create an independent chat from visible history through one turn."""
            return await self._invoke(
                "chat_fork",
                lambda cp, p: cp.chat_fork(
                    p, chat_id, messages=messages or [], turn_index=turn_index
                ),
                mutating=True,
            )

        @tool(name="chat_archive", annotations=_WRITE, structured_output=True)
        async def chat_archive(chat_id: str = "") -> dict[str, Any]:
            """Archive a chat to the vault and trigger normal post-archive processing.

            Args:
                chat_id: The ID of the chat to archive. Omit or pass empty to
                    archive the calling chat.
            """
            return await self._invoke(
                "chat_archive", lambda cp, p: cp.chat_archive(p, chat_id), mutating=True
            )

        @tool(name="chat_delete", annotations=_DESTRUCTIVE, structured_output=True)
        async def chat_delete(chat_id: str = "") -> dict[str, Any]:
            """Delete a chat; deleting the current caller is deferred until the turn finishes."""
            return await self._invoke(
                "chat_delete", lambda cp, p: cp.chat_delete(p, chat_id), mutating=True
            )

        @tool(name="chat_stop", annotations=_DESTRUCTIVE, structured_output=True)
        async def chat_stop(chat_id: str) -> dict[str, Any]:
            """Stop another chat's active provider turn; the current caller cannot stop itself."""
            return await self._invoke(
                "chat_stop", lambda cp, p: cp.chat_stop(p, chat_id), mutating=True
            )

        @tool(name="handoffs_list", annotations=_READ, structured_output=True)
        async def handoffs_list(chat_id: str = "") -> dict[str, Any]:
            """List agent handoffs (cross-provider sub-chats) attached to a chat."""
            return await self._invoke(
                "handoffs_list", lambda cp, p: cp.handoffs_list(p, chat_id)
            )

        @tool(name="handoff_start", annotations=_WRITE, structured_output=True)
        async def handoff_start(
            provider: str,
            model: str,
            message: str,
            chat_id: str = "",
            model_bucket: str = "",
            user_authorized: bool = False,
        ) -> dict[str, Any]:
            """Start a bounded handoff to another provider/model and return its first reply.

            Spawns a read-only sub-chat (the participant) attached to this turn.
            Start one only after the user explicitly asks to consult, hand off to,
            delegate to, or route work to another model or provider — never
            unsolicited. You are the sole conduit: the user cannot write directly
            into the participant, and a participant cannot itself start a nested
            handoff. Never search for or invoke a provider binary (like `codex` or
            `ollama`) directly — this tool is the only supported path for
            cross-provider delegation. If the participant asks a clarifying
            question that needs the user's input, relay it through this chat,
            then send the answer back via handoff_send.
            """
            return await self._invoke(
                "handoff_start",
                lambda cp, p: cp.handoff_start(
                    p,
                    provider=provider,
                    model=model,
                    message=message,
                    chat_id=chat_id,
                    model_bucket=model_bucket,
                    user_authorized=user_authorized,
                ),
                mutating=True,
            )

        @tool(name="handoff_send", annotations=_WRITE, structured_output=True)
        async def handoff_send(
            subchat_id: str,
            message: str,
            user_authorized: bool = False,
        ) -> dict[str, Any]:
            """Send a follow-up message to an active handoff."""
            return await self._invoke(
                "handoff_send",
                lambda cp, p: cp.handoff_send(
                    p, subchat_id, message, user_authorized=user_authorized
                ),
                mutating=True,
            )

        @tool(name="handoff_events", annotations=_READ, structured_output=True)
        async def handoff_events(subchat_id: str) -> dict[str, Any]:
            """Read the event transcript for a handoff."""
            return await self._invoke(
                "handoff_events", lambda cp, p: cp.handoff_events(p, subchat_id)
            )

        @tool(name="handoff_close", annotations=_WRITE, structured_output=True)
        async def handoff_close(subchat_id: str) -> dict[str, Any]:
            """Close a handoff once it has successfully finished and you have
            enough information — don't leave it open once you're done with it."""
            return await self._invoke(
                "handoff_close", lambda cp, p: cp.handoff_close(p, subchat_id), mutating=True
            )

        @tool(name="handoff_cancel", annotations=_DESTRUCTIVE, structured_output=True)
        async def handoff_cancel(subchat_id: str) -> dict[str, Any]:
            """Abort active work in a handoff."""
            return await self._invoke(
                "handoff_cancel", lambda cp, p: cp.handoff_cancel(p, subchat_id), mutating=True
            )

        @tool(name="handoff_extend", annotations=_WRITE, structured_output=True)
        async def handoff_extend(
            subchat_id: str,
            user_authorized: bool = False,
        ) -> dict[str, Any]:
            """Extend a handoff past its message/time limit (12 messages / 30
            minutes) — call this only after explicitly asking the user for
            authorization; never pass user_authorized=True on your own judgment."""
            return await self._invoke(
                "handoff_extend",
                lambda cp, p: cp.handoff_extend(
                    p, subchat_id, user_authorized=user_authorized
                ),
                mutating=True,
            )

        @tool(name="schedules_list", annotations=_READ, structured_output=True)
        async def schedules_list() -> dict[str, Any]:
            """List schedules in the active workspace with their next run."""
            return await self._invoke("schedules_list", lambda cp, p: cp.schedules_list(p))

        @tool(name="schedule_preview", annotations=_READ, structured_output=True)
        async def schedule_preview(
            prompt: str,
            daily_time: str = "09:00",
            timezone: str = "UTC",
            frequency: str = "weekly",
            days_of_week: list[str] | None = None,
            day_of_month: int | None = None,
            run_at_date: str | None = None,
            project_id: str | None = None,
            chat_id: str | None = None,
            title: str = "",
            provider: str = "",
            model: str = "",
            archive_policy: str = "manual",
        ) -> dict[str, Any]:
            """Validate a schedule and compute its next run without saving it.

            Call this before schedule_create for a new recurring schedule and
            show the user the resulting next_run, workspace, and project as
            part of the draft. A missing or invalid next_run means the fields
            don't validate as given — don't create it yet. See schedule_create's
            docstring for field semantics (they're identical here)."""
            values = {key: value for key, value in locals().items() if key != "self"}
            return await self._invoke("schedule_preview", lambda cp, p: cp.schedule_preview(p, **values))

        @tool(name="schedule_create", annotations=_WRITE, structured_output=True)
        async def schedule_create(
            prompt: str,
            daily_time: str = "09:00",
            timezone: str = "UTC",
            frequency: str = "weekly",
            days_of_week: list[str] | None = None,
            day_of_month: int | None = None,
            run_at_date: str | None = None,
            project_id: str | None = None,
            chat_id: str | None = None,
            title: str = "",
            description: str = "",
            provider: str = "",
            model: str = "",
            archive_policy: str = "manual",
        ) -> dict[str, Any]:
            """Create a validated Ciaobot schedule (recurring, one-off, or manual-only).

            Show the user a concise draft and get confirmation before creating
            it, unless they already explicitly asked you to apply it — call
            schedule_preview first. The draft must include next_run, the
            target workspace, and the target project (or chat). Ask if they
            want a different workspace/project when that isn't obvious from
            the request. Schedules always belong to one logical workspace
            (Personal, Work, …) and show under Automations for that workspace.

            Args:
                prompt: The prompt dispatched each run. Start with the goal in
                    3-7 words (becomes the chat-title hint); keep only
                    schedule-specific logic — a fresh project run already
                    inherits canonical docs and skills. Aim for <=1000 chars
                    for a simple check, <=4000 for an aggregation/review. For
                    routine checks, have it exit early with a one-line no-op
                    when there's nothing to report. Supports two placeholders:
                    {{ERROR_LOG}} (sanitized server error tail) and
                    {{ISSUE_REPORT}} (server errors + failed background jobs);
                    Ciaobot clears the consumed error log after a clean run
                    that uses one.
                daily_time: Local HH:MM in `timezone` (persisted as the legacy
                    field daily_time_utc).
                timezone: IANA name, e.g. "Europe/Rome". Use the user's local
                    timezone unless they ask for UTC.
                frequency: "daily" | "weekly" | "monthly" | "manual" | "once".
                days_of_week: weekly only — lowercase "mon".."sun".
                day_of_month: 1-31, monthly only.
                run_at_date: "YYYY-MM-DD", once only, must be in the future.
                project_id: Project id or case-insensitive name — creates a
                    fresh chat in that project per run. When omitted (and
                    chat_id is also omitted), defaults to this chat's project
                    and stamps that project's workspace. Preferred for
                    vault-aware automation.
                chat_id: Posts into one existing chat instead. Use only when
                    conversation continuity across runs matters; resolve it
                    via chats_list first (chat titles aren't unique, unlike
                    project names, so there's no name lookup for this one).
                    When set, project_id is not auto-inherited.
                model: Empty inherits the target workspace's default model at
                    dispatch time; override only when necessary.
                provider: Empty inherits the target workspace's default
                    provider at dispatch time; override only when necessary.
                archive_policy: "manual" | "auto".

            An enabled schedule with a missed latest occurrence (e.g. the
            server was off) runs once on startup; older missed intervals are
            not replayed.
            """
            values = {key: value for key, value in locals().items() if key != "self"}
            return await self._invoke("schedule_create", lambda cp, p: cp.schedule_create(p, **values), mutating=True)

        @tool(name="schedule_update", annotations=_WRITE, structured_output=True)
        async def schedule_update(
            schedule_id: str,
            prompt: str | None = None,
            daily_time: str | None = None,
            timezone: str | None = None,
            frequency: str | None = None,
            days_of_week: list[str] | None = None,
            day_of_month: int | None = None,
            run_at_date: str | None = None,
            project_id: str | None = None,
            chat_id: str | None = None,
            title: str | None = None,
            description: str | None = None,
            provider: str | None = None,
            model: str | None = None,
            archive_policy: str | None = None,
        ) -> dict[str, Any]:
            """Update a Ciaobot schedule through validated fields. Field
            semantics match schedule_create. System schedules (scope=system)
            only accept enabled/workspace changes — everything else raises
            system_schedule_read_only."""
            values = {
                key: value
                for key, value in locals().items()
                if key not in {"self", "schedule_id"}
            }
            return await self._invoke(
                "schedule_update",
                lambda cp, p: cp.schedule_update(p, schedule_id, **values),
                mutating=True,
            )

        @tool(name="schedule_action", annotations=_DESTRUCTIVE, structured_output=True)
        async def schedule_action(schedule_id: str, action: str) -> dict[str, Any]:
            """Run one lifecycle action on a schedule.

            action:
                "pause"  — pause without deleting.
                "resume" — resume a paused schedule.
                "run"    — dispatch immediately through the normal chat pipeline.
                "delete" — delete a removable user schedule (destructive). System
                    schedules (scope=system) cannot be deleted — this raises
                    schedule_not_removable instead.
            """
            dispatch = {
                "pause": lambda cp, p: cp.schedule_pause(p, schedule_id),
                "resume": lambda cp, p: cp.schedule_resume(p, schedule_id),
                "run": lambda cp, p: cp.schedule_run(p, schedule_id),
                "delete": lambda cp, p: cp.schedule_delete(p, schedule_id),
            }
            op = dispatch.get(action)
            if op is None:
                raise ControlPlaneError(
                    "invalid_action", "action must be pause, resume, run, or delete."
                )
            return await self._invoke("schedule_action", op, mutating=True)

        @tool(name="loops_list", annotations=_READ, structured_output=True)
        async def loops_list() -> dict[str, Any]:
            """List in-chat loops in the active workspace."""
            return await self._invoke("loops_list", lambda cp, p: cp.loops_list(p))

        @tool(name="loop_create", annotations=_WRITE, structured_output=True)
        async def loop_create(
            prompt: str,
            chat_id: str = "",
            interval_minutes: int = 10,
            title: str = "",
            autostart: bool = False,
            start: bool = True,
        ) -> dict[str, Any]:
            """Create an interval loop: re-sends one prompt into a fixed chat
            every N minutes, retaining that chat's context. Use a loop rather
            than a schedule for sub-day recurrence that needs one
            conversation's continuity; use a schedule instead when each run
            should get a fresh project chat.

            Args:
                chat_id: An existing chat id, or omit / pass empty / "this" for
                    the calling chat. If you must target another chat, resolve
                    its id via chats_list first — chat titles aren't unique.
                prompt: Give a short, fixed no-change response for a no-op
                    tick, so repeated iterations stay cheap and scannable.
                interval_minutes: There is no model field — each iteration
                    uses the target chat's current model and mode.
                autostart: Only controls whether the loop starts again on
                    server boot. It does NOT start the loop now — `start`
                    does that.
                start: True (default) begins the cadence immediately, so the
                    first tick fires within a minute. Pass False only when the
                    user asked for a loop they will start by hand later; say
                    which you did instead of claiming a stopped loop is
                    running. The returned payload carries the real `running`
                    flag — report that, not your intent.

            If the target chat is busy when a tick fires, that iteration is
            skipped and retried on the next tick (not queued). If the target
            chat is missing or archived, the loop stops. Loops do not catch
            up missed ticks after downtime (unlike schedules, which fire once
            for a missed occurrence on startup).
            """
            return await self._invoke(
                "loop_create",
                lambda cp, p: cp.loop_create(
                    p, chat_id, prompt, interval_minutes, title, autostart, start
                ),
                mutating=True,
            )

        @tool(name="loop_update", annotations=_WRITE, structured_output=True)
        async def loop_update(
            loop_id: str,
            prompt: str | None = None,
            chat_id: str | None = None,
            interval_minutes: int | None = None,
            title: str | None = None,
            autostart: bool | None = None,
        ) -> dict[str, Any]:
            """Update an in-chat loop."""
            values = {
                key: value
                for key, value in locals().items()
                if key not in {"self", "loop_id"}
            }
            return await self._invoke("loop_update", lambda cp, p: cp.loop_update(p, loop_id, **values), mutating=True)

        @tool(name="loop_action", annotations=_DESTRUCTIVE, structured_output=True)
        async def loop_action(loop_id: str, action: str) -> dict[str, Any]:
            """Run one lifecycle action on an in-chat loop.

            action:
                "start"  — start the loop's runtime cadence.
                "stop"   — stop the cadence without deleting it.
                "run"    — run one iteration immediately.
                "delete" — delete the loop (destructive).
            """
            dispatch = {
                "start": lambda cp, p: cp.loop_start(p, loop_id),
                "stop": lambda cp, p: cp.loop_stop(p, loop_id),
                "run": lambda cp, p: cp.loop_run(p, loop_id),
                "delete": lambda cp, p: cp.loop_delete(p, loop_id),
            }
            op = dispatch.get(action)
            if op is None:
                raise ControlPlaneError(
                    "invalid_action", "action must be start, stop, run, or delete."
                )
            return await self._invoke("loop_action", op, mutating=True)

        # Workspace file read/write use the provider's native filesystem tools.
        @tool(name="file_surface", annotations=_READ, structured_output=True)
        async def file_surface(path: str) -> dict[str, Any]:
            """Deliberately open a workspace file in the user's pinned preview panel.

            Use this to show the user a file you produced or want to highlight,
            even one you only read, or one a subagent wrote, instead of relying on
            them to notice it. Ordinary Write/Edit calls no longer auto-open the
            panel; call this when a file is worth surfacing.

            The pin happens in the browser, so this validates the path and reports
            how many live clients were listening. ``viewers: 0`` means nobody had
            the chat open and no panel opened: do not tell the user the file is in
            their panel in that case."""
            return await self._invoke("file_surface", lambda cp, p: cp.file_surface(p, path))

        # File history/snapshot/restore are covered by the workspace git repo.
        @tool(name="adversarial_review", annotations=_WRITE, structured_output=True)
        async def adversarial_review(
            artifact: str,
            doc_type: str = "document",
            focus: str = "",
            context: str = "",
            models: str = "",
            format: str = "markdown",
        ) -> dict[str, Any]:
            """Send an artifact to several models for a multi-model adversarial review.

            Each configured model reviews the artifact independently (no shared
            context between them) and the results are synthesized into per-model
            verdicts plus a combined issue list. Use when the user explicitly asks
            for a review, critique, red-team, or second opinion, when they're about
            to ship something high-stakes (a PRD, brief, plan, customer email, exec
            deck, public post), or when you just produced a substantive artifact
            yourself and want it pressure-tested before declaring done. Skip for
            trivial outputs (one-line answers, simple lookups) — the panel costs
            real tokens and time.

            Args:
                artifact: The full text to review, inlined directly (not a file path).
                doc_type: Artifact type, e.g. "prd", "plan", "brief", "email", "code".
                focus: Optional area to focus the critique on.
                context: Optional extra context for the reviewers (audience, constraints).
                models: Comma-separated model ids to override the configured panel
                    (Settings → Models → Adversarial review panel) for this one call.
                format: "markdown" (a rendered report) or "json" (raw per-model results).
            """
            return await self._invoke(
                "adversarial_review",
                lambda cp, p: cp.adversarial_review(
                    p, artifact, doc_type=doc_type, focus=focus, context=context,
                    models=models, format=format,
                ),
                mutating=True,
            )

        # agent_context_get / workspace_health_* -> `ciao health get|fix`;
        # skills_list -> `ciao skills list`; skills_sync -> `ciao skills-sync`.
        # local_session_* (status/preflight/handback/resync) are dropped: shell
        # agents commit/push with git directly, and the PWA "Sync to Remote"
        # feature drives the control plane through its own REST route.
        # package_status_get / lifecycle_* are host/PWA concerns, not agent tools.


async def mcp_status_endpoint(request: Request) -> JSONResponse:
    service = getattr(request.app.state, "mcp_service", None)
    if service is None:
        return JSONResponse({"enabled": False, "bound": False, "tool_count": 0, "project_servers": []})
    return JSONResponse(service.status_for_api())


async def mcp_usage_endpoint(request: Request) -> JSONResponse:
    service = getattr(request.app.state, "mcp_service", None)
    if service is None:
        return JSONResponse({"total_calls": 0, "total_errors": 0, "tool_count": 0, "tools": []})
    return JSONResponse(service.usage())


def _write_mcp_env_values(path: Path, updates: dict[str, str]) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            out.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key not in remaining:
            out.append(line)
            continue
        value = remaining.pop(key).strip()
        if value:
            out.append(f"{key}={value}")
    for key, value in remaining.items():
        value = value.strip()
        if value:
            out.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


async def mcp_env_keys_endpoint(request: Request) -> JSONResponse:
    """Save MCP-related secrets into the workspace ``.env``.

    Known keys from discovered servers are accepted. Unknown keys require
    ``server`` so they can be bound into that server's ``.mcp.json`` env map.
    Values are never returned.
    """
    service = getattr(request.app.state, "mcp_service", None)
    if service is None:
        return JSONResponse({"error": "MCP service unavailable"}, status_code=503)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected object"}, status_code=400)
    raw_keys = body.get("keys")
    if not isinstance(raw_keys, dict):
        return JSONResponse({"error": "keys must be an object"}, status_code=400)
    updates = {str(k): str(v) for k, v in raw_keys.items()}
    server = str(body.get("server") or "").strip() or None
    try:
        payload = await asyncio.to_thread(
            service.save_project_server_env_keys,
            updates,
            server=server,
            bind_missing=True,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(payload)


async def mcp_servers_collection_endpoint(request: Request) -> JSONResponse:
    """Create a project MCP server in ``.mcp.json``."""
    service = getattr(request.app.state, "mcp_service", None)
    if service is None:
        return JSONResponse({"error": "MCP service unavailable"}, status_code=503)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected object"}, status_code=400)
    args = body.get("args")
    args_list = [str(item) for item in args] if isinstance(args, list) else None
    env_keys = body.get("env_keys") if isinstance(body.get("env_keys"), dict) else None
    try:
        payload = await asyncio.to_thread(
            service.upsert_project_server,
            str(body.get("name") or ""),
            url=str(body.get("url") or ""),
            command=str(body.get("command") or ""),
            args=args_list,
            env_keys={str(k): str(v) for k, v in env_keys.items()} if env_keys else None,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(payload, status_code=201)


async def mcp_server_item_endpoint(request: Request) -> JSONResponse:
    """Update or delete one project MCP server."""
    service = getattr(request.app.state, "mcp_service", None)
    if service is None:
        return JSONResponse({"error": "MCP service unavailable"}, status_code=503)
    name = str(request.path_params.get("name") or "").strip()
    if not name:
        return JSONResponse({"error": "missing server name"}, status_code=400)
    if request.method == "DELETE":
        try:
            payload = await asyncio.to_thread(service.delete_project_server, name)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        return JSONResponse(payload)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected object"}, status_code=400)
    args = body.get("args")
    args_list = [str(item) for item in args] if isinstance(args, list) else None
    env_keys = body.get("env_keys") if isinstance(body.get("env_keys"), dict) else None
    # Preserve existing transport fields when omitted.
    current = None
    for server in service.status_for_api().get("project_servers") or []:
        if isinstance(server, dict) and server.get("name") == name:
            current = server
            break
    if current is None:
        return JSONResponse({"error": f"unknown MCP server '{name}'"}, status_code=404)
    url = body["url"] if "url" in body else (current.get("url") or "")
    command = body["command"] if "command" in body else (current.get("command") or "")
    if args_list is None and "args" not in body:
        args_list = list(current.get("args") or [])
    try:
        payload = await asyncio.to_thread(
            service.upsert_project_server,
            name,
            url=str(url or ""),
            command=str(command or ""),
            args=args_list,
            env_keys={str(k): str(v) for k, v in env_keys.items()} if env_keys else None,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(payload)


async def mcp_server_tools_endpoint(request: Request) -> JSONResponse:
    """Lazy tool discovery for one project MCP server (HTTP probe or observed)."""
    service = getattr(request.app.state, "mcp_service", None)
    if service is None:
        return JSONResponse({"ok": False, "error": "MCP service unavailable", "tools": []}, status_code=503)
    name = str(request.path_params.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "missing server name", "tools": []}, status_code=400)
    result = await asyncio.to_thread(service.probe_project_server_tools, name)
    if result.get("error") == f"unknown MCP server '{name}'":
        return JSONResponse(result, status_code=404)
    return JSONResponse(result)
