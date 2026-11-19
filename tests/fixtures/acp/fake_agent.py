"""A scripted ACP agent for tests: ``python fake_agent.py <scenario.json>``.

Scenario JSON (every key optional):

- ``initialize``: the ``agentCapabilities`` dict returned from ``initialize``.
- ``config_options``: SessionConfigOption dicts returned from ``session/new``
  and ``session/load``.
- ``sessions``: session ids handed out by ``session/new`` in order (default
  ``fake-1``, ``fake-2``, ...).
- ``load_replay``: ``session/update`` payloads (the ``update`` dict) sent
  during ``session/load``.
- ``prompts``: one entry per ``session/prompt`` call, in order. Each entry is
  ``{"steps": [...]}``; a step is one of
  ``{"update": <sessionUpdate dict>}``,
  ``{"update_detached": <sessionUpdate dict>}`` (sent without waiting),
  ``{"permission": {"toolCall": {...}, "options": [...]}}``,
  ``{"sleep_ms": N}``,
  ``{"stop": "<stopReason>", "usage": {...}?}``,
  ``{"crash": true}`` (exit 1), or
  ``{"await_cancel": true}`` (block until ``session/cancel``, stop ``cancelled``).
  A prompt with no ``stop`` step stops with ``end_turn``.
- ``after_prompt``: steps sent after the prompt response (late updates).
  ``after_prompt_delay_ms`` waits that long before sending them (default 50).
- ``load_error`` / ``resume_error``: fail ``session/load`` or ``session/resume``
  with that message.
- ``new_error``: fail ``session/new`` with that message.
- ``new_hang``: never answer ``session/new`` (the request blocks until the client goes away).
- ``config_error``: fail ``session/set_config_option`` with that message.
- ``stderr``: lines written to stderr at startup.
- ``initialize_delay_ms``: sleep before answering ``initialize``.
- ``exit_during_initialize``: exit 1 when ``initialize`` arrives.
- ``initialize_error``: answer ``initialize`` with a JSON-RPC error carrying that message.
- ``spawn_tree``: at startup start a process tree and write ``{"pid", "child",
  "grandchild", "great"}`` to ``<scenario>.pids.json``: ``child`` is a plain
  ``sleep 60``; ``grandchild`` is a separate Python process that ignores SIGTERM
  and has its own SIGTERM-ignoring child ``great``. The fake never signals them.
- ``ignore_sigterm``: ignore SIGTERM (only SIGKILL ends the process).
- ``record_env``: environment variable names whose values are logged.

Every inbound call is appended as one JSON line to ``<scenario>.calls.jsonl``
as ``{"method": ..., "params": {...}}`` (wire-style camelCase params); a
permission reply adds ``{"method": "permission_outcome", "params": {...}}``.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import acp
from acp.connection import StreamDirection
from acp.schema import (
    AgentCapabilities,
    AuthenticateResponse,
    LoadSessionResponse,
    NewSessionResponse,
    PromptResponse,
    RequestPermissionRequest,
    SessionNotification,
)


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(by_alias=True, exclude_none=True, mode="json")
    return value


class FakeAgent:
    def __init__(self, scenario_path: Path) -> None:
        self.scenario: dict[str, Any] = json.loads(scenario_path.read_text(encoding="utf-8"))
        self.calls_path = Path(str(scenario_path) + ".calls.jsonl")
        self.conn: Any = None
        self.session_count = 0
        self.prompt_count = 0
        self.cancel_events: dict[str, asyncio.Event] = {}
        self.pending_permissions: dict[str, set[asyncio.Future[Any]]] = {}

    def on_connect(self, conn: Any) -> None:
        self.conn = conn

    def log(self, method: str, params: dict[str, Any]) -> None:
        with self.calls_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"method": method, "params": params}) + "\n")

    async def initialize(self, protocol_version: int, client_capabilities: Any = None,
                         client_info: Any = None, **kwargs: Any) -> Any:
        params: dict[str, Any] = {
            "protocolVersion": protocol_version,
            "clientCapabilities": _dump(client_capabilities),
            "clientInfo": _dump(client_info),
        }
        env = {name: os.environ.get(name) for name in self.scenario.get("record_env", [])}
        if env:
            params["env"] = env
        params["cwd"] = os.getcwd()
        params["pid"] = os.getpid()
        params["pgid"] = os.getpgrp()
        params["sid"] = os.getsid(0)
        self.log("initialize", params)
        await asyncio.sleep(self.scenario.get("initialize_delay_ms", 0) / 1000)
        if self.scenario.get("exit_during_initialize"):
            os._exit(1)
        if self.scenario.get("initialize_error"):
            raise acp.RequestError(-32000, str(self.scenario["initialize_error"]))
        from acp.schema import InitializeResponse
        return InitializeResponse(
            protocol_version=1,
            agent_capabilities=AgentCapabilities.model_validate(self.scenario.get("initialize", {})),
        )

    async def authenticate(self, method_id: str, **kwargs: Any) -> Any:
        self.log("authenticate", {"methodId": method_id})
        return AuthenticateResponse()

    def _next_session_id(self) -> str:
        ids = self.scenario.get("sessions", [])
        index = self.session_count
        self.session_count += 1
        return ids[index] if index < len(ids) else f"fake-{index + 1}"

    def _logged_meta(self, kwargs: dict[str, Any]) -> Any:
        if kwargs.get("field_meta"):
            return kwargs["field_meta"]
        meta = {key: value for key, value in kwargs.items() if key != "field_meta"}
        return meta or None

    async def new_session(self, cwd: str, additional_directories: Any = None,
                          mcp_servers: Any = None, **kwargs: Any) -> Any:
        self.log("session/new", {
            "cwd": cwd,
            "mcpServers": [_dump(server) for server in mcp_servers or []],
            "additionalDirectories": additional_directories,
            "_meta": self._logged_meta(kwargs),
        })
        if self.scenario.get("new_error"):
            raise acp.RequestError(-32002, str(self.scenario["new_error"]))
        if self.scenario.get("new_hang"):
            await asyncio.Event().wait()
        return NewSessionResponse.model_validate({
            "sessionId": self._next_session_id(),
            "configOptions": self.scenario.get("config_options", []),
        })

    async def load_session(self, cwd: str, session_id: str, mcp_servers: Any = None,
                           additional_directories: Any = None, **kwargs: Any) -> Any:
        self.log("session/load", {
            "cwd": cwd,
            "sessionId": session_id,
            "additionalDirectories": additional_directories,
            "_meta": self._logged_meta(kwargs),
        })
        if self.scenario.get("load_error"):
            raise acp.RequestError(-32002, str(self.scenario["load_error"]))
        for update in self.scenario.get("load_replay", []):
            await self._send_update(session_id, update)
        return LoadSessionResponse.model_validate({"configOptions": self.scenario.get("config_options", [])})

    async def resume_session(self, session_id: str, cwd: str, additional_directories: Any = None,
                             mcp_servers: Any = None, **kwargs: Any) -> Any:
        self.log("session/resume", {
            "cwd": cwd,
            "sessionId": session_id,
            "additionalDirectories": additional_directories,
            "_meta": self._logged_meta(kwargs),
        })
        if self.scenario.get("resume_error"):
            raise acp.RequestError(-32002, str(self.scenario["resume_error"]))
        from acp.schema import ResumeSessionResponse
        return ResumeSessionResponse.model_validate(
            {"configOptions": self.scenario.get("config_options", [])})

    async def set_session_mode(self, session_id: str, mode_id: str, **kwargs: Any) -> Any:
        self.log("session/set_mode", {"sessionId": session_id, "modeId": mode_id})
        return None

    async def set_config_option(self, config_id: str, session_id: str, value: Any, **kwargs: Any) -> Any:
        self.log("session/set_config_option",
                 {"sessionId": session_id, "configId": config_id, "value": value})
        if self.scenario.get("config_error"):
            raise acp.RequestError(-32602, str(self.scenario["config_error"]))
        from acp.schema import SetSessionConfigOptionResponse
        return SetSessionConfigOptionResponse.model_validate(
            {"configOptions": self.scenario.get("config_options", [])})

    async def close_session(self, session_id: str, **kwargs: Any) -> Any:
        self.log("session/close", {"sessionId": session_id})
        return None

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        self.log("session/cancel", {"sessionId": session_id})
        self.cancel_events.setdefault(session_id, asyncio.Event()).set()
        for future in list(self.pending_permissions.get(session_id, ())):
            if not future.done():
                future.cancel()

    def observe_stream(self, event: Any) -> None:
        """Log a permission reply when it is read off the wire.

        The reply resolves a future that wakes several tasks later, while a
        following ``session/cancel`` is logged by its own task. Logging from the
        awaiting coroutine therefore did not reflect arrival order.
        """
        message = event.message
        if event.direction != StreamDirection.INCOMING or "method" in message:
            return
        result = message.get("result")
        if isinstance(result, dict) and "outcome" in result:
            self.log("permission_outcome", result)

    async def _send_update(self, session_id: str, update: dict[str, Any]) -> None:
        note = SessionNotification.model_validate({"sessionId": session_id, "update": update})
        await self.conn.session_update(session_id=session_id, update=note.update)

    async def _run_steps(self, session_id: str, steps: list[dict[str, Any]]) -> PromptResponse | None:
        for step in steps:
            if "update" in step:
                await self._send_update(session_id, step["update"])
            elif "update_detached" in step:
                # Not awaited: the prompt response can be sent first, so the
                # client still has to drain updates that land after it.
                asyncio.create_task(self._send_update(session_id, step["update_detached"]))
            elif "permission" in step:
                request = RequestPermissionRequest.model_validate({
                    "sessionId": session_id,
                    "toolCall": step["permission"]["toolCall"],
                    "options": step["permission"]["options"],
                })
                future = asyncio.ensure_future(self.conn.request_permission(
                    session_id=session_id, tool_call=request.tool_call, options=request.options))
                self.pending_permissions.setdefault(session_id, set()).add(future)
                try:
                    await future
                except asyncio.CancelledError:
                    self.log("permission_outcome", {"outcome": {"outcome": "cancelled"}})
                    return PromptResponse(stop_reason="cancelled")
                finally:
                    self.pending_permissions[session_id].discard(future)
            elif "sleep_ms" in step:
                await asyncio.sleep(step["sleep_ms"] / 1000)
            elif step.get("crash"):
                os._exit(1)
            elif step.get("await_cancel"):
                await self.cancel_events.setdefault(session_id, asyncio.Event()).wait()
                return PromptResponse(stop_reason="cancelled")
            elif "stop" in step:
                return PromptResponse.model_validate(
                    {"stopReason": step["stop"], **({"usage": step["usage"]} if "usage" in step else {})})
        return None

    async def prompt(self, session_id: str, prompt: Any, **kwargs: Any) -> Any:
        self.log("session/prompt", {"sessionId": session_id, "prompt": [_dump(b) for b in prompt]})
        prompts = self.scenario.get("prompts", [])
        entry = prompts[self.prompt_count] if self.prompt_count < len(prompts) else {"steps": []}
        self.prompt_count += 1
        self.cancel_events[session_id] = asyncio.Event()
        response = await self._run_steps(session_id, entry.get("steps", []))
        after = self.scenario.get("after_prompt")
        if after:
            delay = self.scenario.get("after_prompt_delay_ms", 50) / 1000
            asyncio.get_running_loop().call_later(
                delay, lambda: asyncio.ensure_future(self._run_steps(session_id, after)))
        return response or PromptResponse(stop_reason="end_turn")

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.log(f"_{method}", params)
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        self.log(f"_{method}", params)


_GRANDCHILD = """
import json, os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
great = subprocess.Popen([sys.executable, "-c",
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])
with open(sys.argv[1], "w") as handle:
    json.dump({"grandchild": os.getpid(), "great": great.pid}, handle)
time.sleep(60)
"""


def _spawn_tree(scenario_path: Path) -> None:
    child = subprocess.Popen(["sleep", "60"])
    inner = Path(str(scenario_path) + ".inner.json")
    subprocess.Popen([sys.executable, "-c", _GRANDCHILD, str(inner)])
    while True:
        try:
            grand = json.loads(inner.read_text(encoding="utf-8"))
            break
        except (OSError, ValueError):
            time.sleep(0.01)
    pids = {"pid": os.getpid(), "child": child.pid, **grand}
    Path(str(scenario_path) + ".pids.json").write_text(json.dumps(pids), encoding="utf-8")


async def main(scenario_path: Path) -> None:
    agent = FakeAgent(scenario_path)
    for line in agent.scenario.get("stderr", []):
        print(line, file=sys.stderr, flush=True)
    if agent.scenario.get("spawn_tree"):
        _spawn_tree(scenario_path)
    if agent.scenario.get("ignore_sigterm"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    await acp.run_agent(agent, observers=[agent.observe_stream])


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
