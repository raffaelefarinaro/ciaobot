"""MCP Settings web API routes: status, usage, env keys, and project servers.

The MCP adapter itself lives in ``ciao/mcp_server.py``; the HTTP endpoints
that surface it in Settings (status/usage reads, ``.env`` key writes, project
server CRUD, and lazy tool discovery) live here. Endpoints reach the service
through ``request.app.state.mcp_service``, so this module has no import from
the adapter — that keeps ``mcp_server.py`` free to import the probe helpers
back from here without a cycle.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse

_MCP_HTTP_PROBE_TIMEOUT_S = 3.0


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
