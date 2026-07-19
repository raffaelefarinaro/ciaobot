"""OpenAI Codex CLI provider using the official app-server protocol."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ciao.context.entity_tagger import find_entities, format_entities
from ciao.memory_injector import build_memory_block, system_prompt_payload
from ciao.model_tiers import MODEL_TIERS, canonical_tier, codex_tier_models, is_tier
from ciao.models import (
    AgentRequest,
    AssistantTextDelta,
    BridgeMode,
    PermissionRequestEvent,
    ResultEvent,
    StreamEvent,
    SystemStatusEvent,
    ThinkingEvent,
    TokenUsageEvent,
    ToolUseEvent,
)
from ciao.observability.hooks import _runtime_lines
from ciao.providers.base import (
    ActiveHandle,
    BaseSDKProvider,
    ProviderCapabilities,
    build_prompt,
)
from ciao.providers.stdio_rpc import RpcError, RpcProcessError, StdioJsonRpcPeer
from ciao.tool_path import resolve_tool

logger = logging.getLogger(__name__)

_CONTROL_TIMEOUT = 30.0
_TURN_TIMEOUT = 60.0 * 60.0
_MODEL_CACHE_TTL = 300.0
_MODEL_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_PROTOCOL_CACHE: dict[str, tuple[float, int, bool, str]] = {}

_APP_SERVER_REQUESTS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "applyPatchApproval",
    "execCommandApproval",
}
_ACTIVE_COLLAB_STATES = {"pendingInit", "running"}
_MESSAGE_PHASES = {"commentary", "final_answer"}
_REQUIRED_PROTOCOL_TOKENS = frozenset({
    "thread/start",
    "thread/resume",
    "thread/fork",
    "thread/read",
    "turn/start",
    "turn/steer",
    "turn/interrupt",
    "model/list",
    "skills/list",
    "account/rateLimits/read",
    "item/agentMessage/delta",
    "item/reasoning/summaryTextDelta",
    "item/tool/requestUserInput",
    "item/commandExecution/requestApproval",
    "thread/tokenUsage/updated",
    "account/rateLimits/updated",
    "collabAgentToolCall",
})


@dataclass(frozen=True, slots=True)
class CodexSettings:
    """Operator overrides for the Codex tier aliases.

    Empty string means "no pin": the tier resolves through the automatic
    catalog mapping (:func:`ciao.model_tiers.codex_tier_models`). A pin
    is honored only while its model is still visible in the signed-in
    account's catalog, so removed models degrade gracefully.
    """

    haiku_model: str = ""
    sonnet_model: str = ""
    opus_model: str = ""
    fable_model: str = ""

    def tier_overrides(self) -> dict[str, str]:
        return {tier: getattr(self, f"{tier}_model") for tier in MODEL_TIERS}


def codex_tier_overrides(config: object) -> dict[str, str]:
    """Extract the per-tier Codex pins from a (duck-typed) config object."""
    codex = getattr(config, "codex", None)
    if codex is None:
        return {}
    return {
        tier: str(getattr(codex, f"{tier}_model", "") or "")
        for tier in MODEL_TIERS
    }


def codex_protocol_status(
    binary: str,
    env: Mapping[str, str] | None = None,
    *,
    timeout: float = 12.0,
) -> tuple[bool, str]:
    """Capability-test the installed app-server schema without reading auth data."""
    import subprocess

    try:
        stamp = Path(binary).stat().st_mtime_ns
    except OSError:
        stamp = 0
    cached = _PROTOCOL_CACHE.get(binary)
    if cached and cached[1] == stamp and time.monotonic() - cached[0] < 300:
        return cached[2], cached[3]
    path_env = _codex_path_env(binary)
    merged_env = {**os.environ, **dict(env or {})}
    if "PATH" in path_env:
        existing = merged_env.get("PATH", "")
        merged_env["PATH"] = f"{path_env['PATH']}:{existing}" if existing else path_env["PATH"]
    try:
        with tempfile.TemporaryDirectory(prefix="ciao-codex-schema-") as raw_dir:
            out = Path(raw_dir)
            run = subprocess.run(
                [
                    binary,
                    "app-server",
                    "generate-json-schema",
                    "--experimental",
                    "--out",
                    str(out),
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=merged_env,
            )
            if run.returncode != 0:
                detail = (run.stderr or run.stdout or "schema generation failed").strip()
                result = (False, f"app-server compatibility check failed: {detail[:500]}")
            else:
                remaining = set(_REQUIRED_PROTOCOL_TOKENS)
                for schema in out.rglob("*.json"):
                    try:
                        text = schema.read_text(encoding="utf-8")
                    except OSError:
                        continue
                    remaining = {token for token in remaining if token not in text}
                    if not remaining:
                        break
                if remaining:
                    result = (
                        False,
                        "incompatible app-server; missing: " + ", ".join(sorted(remaining)),
                    )
                else:
                    result = (True, "app-server protocol compatible")
    except (OSError, subprocess.SubprocessError) as exc:
        result = (False, f"app-server compatibility check failed: {exc}")
    _PROTOCOL_CACHE[binary] = (time.monotonic(), stamp, result[0], result[1])
    return result


def codex_collab_agents(thread: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the latest known state for each Codex collaboration child.

    ``collabAgentToolCall.status`` describes the control operation (for
    example, whether ``spawnAgent`` itself completed), not the lifetime of the
    child. The per-receiver ``agentsStates`` map is the authoritative child
    lifecycle signal and is refreshed on later item updates/waits.
    """
    agents: dict[str, dict[str, Any]] = {}
    turns = thread.get("turns")
    for turn_index, turn in enumerate(turns if isinstance(turns, list) else []):
        if not isinstance(turn, Mapping):
            continue
        items = turn.get("items")
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, Mapping) or item.get("type") != "collabAgentToolCall":
                continue
            receivers = item.get("receiverThreadIds")
            states = item.get("agentsStates")
            for receiver in receivers if isinstance(receivers, list) else []:
                agent_id = str(receiver or "")
                if not agent_id:
                    continue
                previous = agents.get(agent_id, {})
                state = states.get(agent_id) if isinstance(states, Mapping) else None
                lifecycle = (
                    str(state.get("status") or "")
                    if isinstance(state, Mapping)
                    else str(previous.get("status") or "")
                )
                # A completed spawn operation only proves the child was
                # created. Until Codex supplies a lifecycle state, expose it
                # as running rather than incorrectly declaring it finished.
                if not lifecycle and item.get("tool") == "spawnAgent":
                    lifecycle = "running"
                agents[agent_id] = {
                    **previous,
                    "agent_id": agent_id,
                    "status": lifecycle or "unknown",
                    "message": (
                        str(state.get("message") or "")
                        if isinstance(state, Mapping)
                        else str(previous.get("message") or "")
                    ),
                    "tool_use_id": str(previous.get("tool_use_id") or item.get("id") or ""),
                    "description": str(item.get("prompt") or previous.get("description") or ""),
                    "turn_index": int(previous.get("turn_index", turn_index)),
                    "sender_thread_id": str(
                        item.get("senderThreadId")
                        or previous.get("sender_thread_id")
                        or ""
                    ),
                }
    return agents


def _agent_message_phase(item: Mapping[str, Any]) -> str | None:
    """Return a valid Codex assistant-message phase, if the item declares one."""
    phase = str(item.get("phase") or "")
    return phase if phase in _MESSAGE_PHASES else None


def codex_running_subagents(thread: Mapping[str, Any]) -> tuple[int, bool]:
    """Return ``(running_count, had_subagents)`` for a Codex thread."""
    agents = codex_collab_agents(thread)
    return (
        sum(1 for agent in agents.values() if agent.get("status") in _ACTIVE_COLLAB_STATES),
        bool(agents),
    )


def _thread_lifecycle_status(
    thread: Mapping[str, Any] | None, fallback: str
) -> str:
    """Refine a collaboration state using the receiver's latest turn."""
    if not isinstance(thread, Mapping):
        return fallback
    turns = thread.get("turns")
    if not isinstance(turns, list) or not turns:
        return fallback
    latest = turns[-1]
    if not isinstance(latest, Mapping):
        return fallback
    turn_status = str(latest.get("status") or "")
    if turn_status in {"inProgress", "in_progress", "running"}:
        return "running"
    if turn_status == "failed":
        return "errored"
    if turn_status == "interrupted":
        return "interrupted"
    if turn_status == "completed":
        return "completed"
    return fallback


def resolve_codex_binary(env: Mapping[str, str] | None = None) -> str | None:
    source = env if env is not None else os.environ
    explicit = str(source.get("CIAO_CODEX_BIN", "")).strip()
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return str(path.resolve())
        return None
    resolved = resolve_tool("codex")
    if resolved and "cmux-cli-shims" not in resolved:
        return resolved
    for candidate in (
        Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
        Path.home() / "Applications/ChatGPT.app/Contents/Resources/codex",
    ):
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def _codex_path_env(binary: str) -> dict[str, str]:
    """Return an env fragment that puts *binary*'s directory on ``PATH``.

    When the binary was resolved from a fallback location (e.g. inside
    ChatGPT.app), the child process needs the directory on ``PATH`` so
    that ``codex`` can locate itself for internal sub-invocations.
    """
    bin_dir = Path(binary).resolve().parent
    paths = [str(bin_dir)]
    node_bin = bin_dir / "cua_node" / "bin"
    if node_bin.is_dir():
        paths.append(str(node_bin))
    return {"PATH": os.pathsep.join(paths)}


def codex_login_status(
    env: Mapping[str, str] | None = None,
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Return a bounded, credential-free Codex install/auth status."""
    import subprocess

    binary = resolve_codex_binary(env)
    if not binary:
        return {
            "name": "codex",
            "ok": False,
            "auth": "missing",
            "command": "npm install -g @openai/codex@latest",
            "detail": "Codex CLI is not installed or is not visible on PATH.",
        }
    path_env = _codex_path_env(binary)
    merged_env = {**os.environ, **dict(env or {})}
    if "PATH" in path_env:
        existing = merged_env.get("PATH", "")
        merged_env["PATH"] = f"{path_env['PATH']}:{existing}" if existing else path_env["PATH"]

    try:
        version_run = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=merged_env,
        )
        version_lines = (version_run.stdout or version_run.stderr).strip().splitlines()
        version = version_lines[-1] if version_lines else "installed"
    except (OSError, subprocess.SubprocessError):
        version = "installed"
    try:
        login = subprocess.run(
            [binary, "login", "status"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=merged_env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "name": "codex",
            "ok": False,
            "auth": "error",
            "command": "ciao auth codex",
            "detail": f"{version}; login status failed: {exc}",
        }
    output = "\n".join(part for part in (login.stdout, login.stderr) if part).strip()
    logged_in = login.returncode == 0 and "logged in" in output.lower()
    if logged_in:
        compatible, protocol_detail = codex_protocol_status(binary, merged_env)
        if not compatible:
            return {
                "name": "codex",
                "ok": False,
                "auth": "incompatible",
                "command": "codex update",
                "detail": f"{version}; {protocol_detail}",
            }
    else:
        protocol_detail = ""
    return {
        "name": "codex",
        "ok": logged_in,
        "auth": "chatgpt" if logged_in and "chatgpt" in output.lower() else ("oauth" if logged_in else "missing"),
        "command": "ciao auth codex",
        "detail": "; ".join(
            part for part in (version, output or "login required", protocol_detail) if part
        ),
        "version": version,
        "account": "ChatGPT account" if logged_in and "chatgpt" in output.lower() else ("OpenAI API" if logged_in else ""),
        "protocol": protocol_detail,
    }


def _mode_settings(mode: BridgeMode) -> tuple[str, str, str]:
    if mode == "plan":
        return "read-only", "on-request", "user"
    if mode == "bypass":
        return "danger-full-access", "never", "user"
    if mode == "auto":
        return "workspace-write", "on-request", "auto_review"
    return "workspace-write", "on-request", "user"


def _safe_json(value: object, *, limit: int = 2000) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value[:limit]
    try:
        return json.dumps(value, ensure_ascii=False, default=str)[:limit]
    except (TypeError, ValueError):
        return str(value)[:limit]


def _usage_payload(token_usage: Mapping[str, Any] | None) -> tuple[dict[str, str], int, int]:
    if not isinstance(token_usage, Mapping):
        return {}, 0, 0
    last = token_usage.get("last")
    total = token_usage.get("total")
    last = last if isinstance(last, Mapping) else {}
    total = total if isinstance(total, Mapping) else {}
    input_tokens = int(last.get("inputTokens") or 0)
    output_tokens = int(last.get("outputTokens") or 0)
    payload = {
        "input_tokens": str(input_tokens),
        "output_tokens": str(output_tokens),
        "cached_input_tokens": str(int(last.get("cachedInputTokens") or 0)),
        "reasoning_output_tokens": str(int(last.get("reasoningOutputTokens") or 0)),
        "total_tokens": str(int(last.get("totalTokens") or 0)),
    }
    context_window = token_usage.get("modelContextWindow")
    total_tokens = int(total.get("totalTokens") or 0)
    if isinstance(context_window, int) and context_window > 0:
        payload["context_window"] = str(context_window)
        payload["context_pct"] = f"{min(100.0, total_tokens / context_window * 100):.1f}%"
    return payload, input_tokens, output_tokens


def _quota_payload(snapshot: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(snapshot, Mapping):
        return {}
    primary = snapshot.get("primary")
    primary = primary if isinstance(primary, Mapping) else {}
    used = primary.get("usedPercent")
    quota: dict[str, str] = {}
    if used is not None:
        try:
            percent = float(used)
            quota["utilization"] = f"{percent / 100:.3f}"
            quota["status"] = "exceeded" if percent >= 100 else "allowed"
        except (TypeError, ValueError):
            pass
    if primary.get("resetsAt") is not None:
        quota["resetsAt"] = str(primary["resetsAt"])
    if primary.get("windowDurationMins") is not None:
        quota["windowDurationMins"] = str(primary["windowDurationMins"])
    if snapshot.get("limitId"):
        quota["rateLimitType"] = str(snapshot["limitId"])
    if snapshot.get("planType"):
        quota["planType"] = str(snapshot["planType"])
    return quota


def _thread_item_tool_events(item: Mapping[str, Any]) -> list[ToolUseEvent]:
    item_type = str(item.get("type") or "")
    item_id = str(item.get("id") or "") or None
    if item_type == "commandExecution":
        command = str(item.get("command") or "")[:2000]
        from ciao.web.chat_broker import extract_file_touches
        touches = extract_file_touches("Bash", {"command": command})
        return [ToolUseEvent(
            type="assistant",
            tool_name="Bash",
            tool_input=command,
            tool_use_id=item_id,
            file_touches=touches or None,
        )]
    if item_type == "fileChange":
        events: list[ToolUseEvent] = []
        for change in item.get("changes") or []:
            if not isinstance(change, Mapping):
                continue
            kind = str(change.get("kind") or "update").lower()
            name = "Write" if kind in {"add", "create"} else "Edit"
            action = "created" if name == "Write" else "edited"
            path = str(change.get("path") or "")[:2000]
            events.append(ToolUseEvent(
                type="assistant",
                tool_name=name,
                tool_input=path,
                tool_use_id=item_id,
                file_touches=[{"file_path": path, "action": action}] if path else None,
            ))
        return events
    if item_type == "mcpToolCall":
        server = str(item.get("server") or "mcp")
        tool = str(item.get("tool") or "tool")
        return [ToolUseEvent(
            type="assistant",
            tool_name=f"mcp__{server}__{tool}",
            tool_input=_safe_json(item.get("arguments")),
            tool_use_id=item_id,
        )]
    if item_type == "dynamicToolCall":
        return [ToolUseEvent(
            type="assistant",
            tool_name=str(item.get("tool") or "tool"),
            tool_input=_safe_json(item.get("arguments")),
            tool_use_id=item_id,
        )]
    if item_type == "collabAgentToolCall":
        return [ToolUseEvent(
            type="assistant",
            tool_name="Agent",
            tool_input=str(item.get("prompt") or item.get("tool") or "")[:2000],
            tool_use_id=item_id,
            parent_tool_use_id=str(item.get("senderThreadId") or "") or None,
        )]
    if item_type == "webSearch":
        return [ToolUseEvent(
            type="assistant",
            tool_name="WebSearch",
            tool_input=str(item.get("query") or "")[:2000],
            tool_use_id=item_id,
        )]
    if item_type in {"imageView", "imageGeneration"}:
        return [ToolUseEvent(
            type="assistant",
            tool_name="ImageView" if item_type == "imageView" else "ImageGeneration",
            tool_input=_safe_json(item),
            tool_use_id=item_id,
        )]
    return []


class CodexActiveHandle(ActiveHandle):
    def __init__(self, provider: "CodexProvider", thread_id: str, turn_id: str) -> None:
        self.provider = provider
        self.thread_id = thread_id
        self.turn_id = turn_id

    async def stop(self) -> None:
        await self.provider._interrupt(self.thread_id, self.turn_id)


class CodexProvider(BaseSDKProvider):
    """Persistent per-chat Codex app-server client."""

    capabilities = ProviderCapabilities(
        resume=True,
        fork=True,
        images=True,
        stop=True,
        steer=True,
        permissions=True,
        structured_questions=True,
        dynamic_models=True,
        thinking_levels=True,
        usage=True,
        quota=True,
        subagents=True,
        background_subagents=True,
        subagent_messages=True,
        session_history=True,
        schedule_unattended=True,
    )

    def __init__(
        self,
        workspace_root: Path,
        *,
        config: object | None = None,
        command: Sequence[str] | None = None,
        developer_instructions: str | None = None,
        ephemeral: bool = False,
    ) -> None:
        super().__init__(workspace_root, config=config)
        self._command = list(command) if command is not None else None
        self._developer_instructions = developer_instructions
        self._ephemeral = ephemeral
        self._peer: StdioJsonRpcPeer | None = None
        self._session_id: str | None = None
        self._turn_id: str | None = None
        self._effective_model = ""
        self._quota: dict[str, str] = {}
        self._usage: dict[str, str] = {}
        self._permission_requests: dict[str, tuple[int | str, str, dict[str, Any]]] = {}
        self._question_requests: dict[
            str, tuple[int | str, str, dict[str, Any]]
        ] = {}
        self._request_counter = 0
        self._turn_tool_item_ids: set[str] = set()
        self._peer_mcp_token = ""

    @property
    def current_session_id(self) -> str | None:
        return self._session_id

    @property
    def can_drain(self) -> bool:
        return False

    def _resolved_command(self, request: AgentRequest | None = None) -> list[str]:
        if self._command is not None:
            return list(self._command)
        binary = resolve_codex_binary()
        if not binary:
            raise FileNotFoundError(
                "Codex CLI not found. Install Codex and run `ciao auth codex`."
            )
        overrides: list[str] = []
        if request is not None and request.mcp_url and request.mcp_token:
            overrides = [
                "-c", f"mcp_servers.ciaobot.url={json.dumps(request.mcp_url)}",
                "-c", "mcp_servers.ciaobot.bearer_token_env_var=\"CIAO_MCP_SESSION_TOKEN\"",
                "-c", "mcp_servers.ciaobot.enabled=true",
                "-c", f"mcp_servers.ciaobot.required={'true' if request.mcp_required else 'false'}",
                # The app-server needs the token, model-created shell commands do not.
                "-c", "shell_environment_policy.exclude=[\"CIAO_MCP_SESSION_TOKEN\"]",
            ]
        return [binary, *overrides, "app-server", "--stdio"]

    def _memory_instructions(self, request: AgentRequest | None = None) -> str:
        if self._developer_instructions is not None:
            return self._developer_instructions
        cfg = self.config
        memory = ""
        if bool(getattr(cfg, "memory_enabled", True)):
            memory = build_memory_block(
                memory_char_limit=int(getattr(cfg, "memory_char_limit", 2200)),
                user_char_limit=int(getattr(cfg, "user_char_limit", 1375)),
            )
        payload = system_prompt_payload(
            memory,
            control_surface=request.control_surface if request is not None else "legacy",
        ) or {}
        return str(payload.get("append") or "")

    def _runtime_context(self, request: AgentRequest) -> str:
        lines = _runtime_lines(self.workspace_root, request.extra_env or {})
        vault_root = Path(getattr(self.config, "vault_root", self.workspace_root / "memory-vault"))
        workspace = str((request.extra_env or {}).get("CIAO_ACTIVE_WORKSPACE") or "")
        try:
            entities = format_entities(
                find_entities(request.prompt, vault_root, workspace=workspace)
            )
        except Exception:  # noqa: BLE001 - context enrichment is fail-open
            logger.debug("Codex entity context failed", exc_info=True)
            entities = ""
        sections = ["<ciao-runtime>\n" + "\n".join(lines) + "\n</ciao-runtime>"]
        if entities:
            sections.append("<ciao-entities>\n" + entities + "\n</ciao-entities>")
        return "\n".join(sections)

    def _prompt_text(self, request: AgentRequest) -> str:
        prompt = build_prompt(request)
        context = self._runtime_context(request)
        marker = "[CIAO_CONTEXT_END]"
        if prompt.startswith("[CIAO_CONTEXT_BEGIN]\n") and marker in prompt:
            return prompt.replace(marker, context + "\n" + marker, 1)
        return f"[CIAO_CONTEXT_BEGIN]\n{context}\n{marker}\n\n{prompt}"

    async def _ensure_peer(self, request: AgentRequest) -> StdioJsonRpcPeer:
        if (
            self._peer is not None
            and self._peer.running
            and request.mcp_token != self._peer_mcp_token
        ):
            await self.disconnect()
        if self._peer is not None and self._peer.running:
            return self._peer
        command = self._resolved_command(request)
        env = {**_codex_path_env(command[0]), **(request.extra_env or {})}
        if request.mcp_token:
            env["CIAO_MCP_SESSION_TOKEN"] = request.mcp_token
        self._peer = StdioJsonRpcPeer(
            command,
            cwd=self.workspace_root,
            env=env,
            name="codex app-server",
        )
        await self._peer.start()
        await self._peer.request(
            "initialize",
            {
                "clientInfo": {"name": "ciaobot", "title": "Ciaobot", "version": "0.4"},
                "capabilities": {"experimentalApi": True},
            },
            timeout=_CONTROL_TIMEOUT,
        )
        await self._peer.notify("initialized", {})
        self._peer_mcp_token = request.mcp_token
        return self._peer

    async def _ensure_thread(self, request: AgentRequest) -> str:
        requested_model = request.model
        if is_tier(requested_model):
            catalog = await self.model_catalog(self.workspace_root)
            requested_model = codex_tier_models(
                catalog, overrides=codex_tier_overrides(self.config)
            ).get(canonical_tier(requested_model), "")
        peer = await self._ensure_peer(request)
        sandbox, approval, reviewer = _mode_settings(request.mode)
        params = {
            "cwd": str(self.workspace_root),
            "model": requested_model or None,
            "approvalPolicy": approval,
            "approvalsReviewer": reviewer,
            "sandbox": sandbox,
            "developerInstructions": self._memory_instructions(request),
        }
        requested_session = request.resume_session or self._session_id
        method = (
            "thread/fork"
            if requested_session and request.fork_session
            else ("thread/resume" if requested_session else "thread/start")
        )
        if requested_session:
            params["threadId"] = requested_session
        if method in {"thread/start", "thread/fork"}:
            params["ephemeral"] = self._ephemeral
        try:
            response = await peer.request(method, params, timeout=_CONTROL_TIMEOUT)
        except RpcError:
            if not requested_session:
                raise
            logger.warning("Codex thread %s could not resume; starting fresh", requested_session)
            response = await peer.request(
                "thread/start",
                {key: value for key, value in params.items() if key != "threadId"},
                timeout=_CONTROL_TIMEOUT,
            )
        thread = response.get("thread") if isinstance(response, dict) else None
        thread_id = str(thread.get("id") or "") if isinstance(thread, dict) else ""
        if not thread_id:
            raise RpcProcessError("Codex app-server returned no thread id")
        self._session_id = thread_id
        self._effective_model = str(response.get("model") or requested_model or request.model)
        self._remember_settings(request)
        try:
            limits = await peer.request("account/rateLimits/read", {}, timeout=10.0)
            snapshot = limits.get("rateLimits") if isinstance(limits, dict) else None
            self._quota = _quota_payload(snapshot)
        except RpcError:
            logger.debug("Codex rate-limit snapshot unavailable", exc_info=True)
        return thread_id

    def _inputs(self, request: AgentRequest) -> list[dict[str, Any]]:
        inputs: list[dict[str, Any]] = [
            {"type": "text", "text": self._prompt_text(request)}
        ]
        inputs.extend(
            {"type": "localImage", "path": str(image.path), "detail": "auto"}
            for image in request.images
        )
        return inputs

    async def _interrupt(self, thread_id: str, turn_id: str) -> None:
        peer = self._peer
        if peer is None or not peer.running:
            return
        try:
            await peer.request(
                "turn/interrupt",
                {"threadId": thread_id, "turnId": turn_id},
                timeout=10.0,
            )
        except RpcError:
            logger.debug("Codex turn interrupt failed", exc_info=True)

    async def steer(self, request: AgentRequest) -> bool:
        peer = self._peer
        if (
            peer is None
            or not peer.running
            or not self._session_id
            or not self._turn_id
        ):
            return False
        try:
            await peer.request(
                "turn/steer",
                {
                    "threadId": self._session_id,
                    "expectedTurnId": self._turn_id,
                    "input": self._inputs(request),
                },
                timeout=_CONTROL_TIMEOUT,
            )
            return True
        except RpcError:
            logger.info("Codex active turn rejected steer", exc_info=True)
            return False

    def send_permission_response(self, request_id: str, approved: bool) -> bool:
        pending = self._permission_requests.pop(request_id, None)
        peer = self._peer
        if pending is None or peer is None or not peer.running:
            return False
        rpc_id, method, params = pending
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "applyPatchApproval",
            "execCommandApproval",
        }:
            result: dict[str, Any] = {"decision": "accept" if approved else "decline"}
        else:
            requested = params.get("permissions")
            permissions = requested if approved and isinstance(requested, dict) else {
                "fileSystem": None,
                "network": None,
            }
            result = {"permissions": permissions, "scope": "turn"}
        asyncio.create_task(peer.respond(rpc_id, result=result))
        return True

    def send_question_response(
        self, request_id: str, answers: Mapping[str, Sequence[str]]
    ) -> bool:
        pending = self._question_requests.pop(request_id, None)
        peer = self._peer
        if pending is None or peer is None or not peer.running:
            return False
        rpc_id, method, params = pending
        if method == "mcpServer/elicitation/request":
            if params.get("mode") == "url":
                selected = [
                    str(value).lower()
                    for values in answers.values()
                    for value in values
                ]
                result = {
                    "action": "cancel" if "cancel" in selected or not selected else "accept"
                }
            else:
                schema = params.get("requestedSchema")
                properties = schema.get("properties") if isinstance(schema, Mapping) else {}
                content: dict[str, Any] = {}
                for question_id, values in answers.items():
                    raw_values = [str(value) for value in values]
                    field = properties.get(question_id) if isinstance(properties, Mapping) else {}
                    content[str(question_id)] = (
                        raw_values
                        if isinstance(field, Mapping) and field.get("type") == "array"
                        else (raw_values[0] if raw_values else "")
                    )
                result = {
                    "action": "accept" if content else "decline",
                    "content": content or None,
                }
        else:
            result = {
                "answers": {
                    str(question_id): {"answers": [str(value) for value in values]}
                    for question_id, values in answers.items()
                }
            }
        asyncio.create_task(peer.respond(rpc_id, result=result))
        return True

    def _server_request_event(self, message: Mapping[str, Any]) -> StreamEvent | None:
        method = str(message.get("method") or "")
        rpc_id = message.get("id")
        params = message.get("params")
        params = dict(params) if isinstance(params, Mapping) else {}
        if rpc_id is None:
            return None
        self._request_counter += 1
        public_id = f"codex-{self._request_counter}"
        if method in _APP_SERVER_REQUESTS:
            self._permission_requests[public_id] = (rpc_id, method, params)
            if method in {"item/commandExecution/requestApproval", "execCommandApproval"}:
                tool_name = "Bash"
                tool_input = str(params.get("command") or params.get("reason") or "")
            elif method in {"item/fileChange/requestApproval", "applyPatchApproval"}:
                tool_name = "Edit"
                tool_input = str(params.get("reason") or params.get("grantRoot") or "")
            else:
                tool_name = "Permissions"
                tool_input = _safe_json(params.get("permissions"))
            return PermissionRequestEvent(
                type="system",
                message=str(params.get("reason") or f"Approve use of {tool_name}?"),
                tool_name=tool_name,
                tool_input=tool_input[:2000],
                request_id=public_id,
            )
        if method == "item/tool/requestUserInput":
            self._question_requests[public_id] = (rpc_id, method, params)
            questions = params.get("questions")
            payload = {"questions": questions if isinstance(questions, list) else []}
            return ToolUseEvent(
                type="assistant",
                tool_name="AskUserQuestion",
                tool_input=json.dumps(payload, ensure_ascii=False),
                tool_use_id=str(params.get("itemId") or "") or None,
                request_id=public_id,
            )
        if method == "mcpServer/elicitation/request":
            self._question_requests[public_id] = (rpc_id, method, params)
            questions: list[dict[str, Any]] = []
            if params.get("mode") == "url":
                questions.append({
                    "id": "action",
                    "header": "Authorization",
                    "question": (
                        f"{params.get('message') or 'Complete authorization'}\n"
                        f"{params.get('url') or ''}"
                    ).strip(),
                    "isOther": False,
                    "isSecret": False,
                    "options": [
                        {"label": "Done", "description": "Authorization completed."},
                        {"label": "Cancel", "description": "Cancel this request."},
                    ],
                })
            else:
                schema = params.get("requestedSchema")
                properties = schema.get("properties") if isinstance(schema, Mapping) else {}
                for field_id, raw_field in (
                    properties.items() if isinstance(properties, Mapping) else []
                ):
                    field = raw_field if isinstance(raw_field, Mapping) else {}
                    raw_options = field.get("enum") or field.get("oneOf")
                    if field.get("type") == "array":
                        items = field.get("items")
                        if isinstance(items, Mapping):
                            raw_options = items.get("enum") or items.get("anyOf")
                    options: list[dict[str, str]] = []
                    for option in raw_options if isinstance(raw_options, list) else []:
                        if isinstance(option, Mapping):
                            label = str(option.get("title") or option.get("const") or "")
                            description = str(option.get("description") or "")
                        else:
                            label, description = str(option), ""
                        if label:
                            options.append({"label": label, "description": description})
                    questions.append({
                        "id": str(field_id),
                        "header": str(field.get("title") or field_id),
                        "question": str(
                            field.get("description") or params.get("message") or field_id
                        ),
                        "multiSelect": field.get("type") == "array",
                        "isOther": not bool(options),
                        "isSecret": field.get("format") == "password",
                        "options": options,
                    })
            return ToolUseEvent(
                type="assistant",
                tool_name="AskUserQuestion",
                tool_input=json.dumps({"questions": questions}, ensure_ascii=False),
                request_id=public_id,
            )
        return None

    def _notification_events(
        self,
        message: Mapping[str, Any],
        message_phases: Mapping[str, str] | None = None,
    ) -> list[StreamEvent]:
        method = str(message.get("method") or "")
        params = message.get("params")
        params = params if isinstance(params, Mapping) else {}
        if method == "item/agentMessage/delta":
            item_id = str(params.get("itemId") or "")
            phase = (message_phases or {}).get(item_id)
            return [AssistantTextDelta(
                type="assistant",
                text=str(params.get("delta") or ""),
                phase=phase if phase in _MESSAGE_PHASES else None,
            )]
        if method == "item/reasoning/summaryTextDelta":
            return [ThinkingEvent(type="assistant", text=str(params.get("delta") or ""))]
        if method == "item/started":
            item = params.get("item")
            events = _thread_item_tool_events(item) if isinstance(item, Mapping) else []
            item_id = str(item.get("id") or "") if isinstance(item, Mapping) else ""
            if item_id and events:
                self._turn_tool_item_ids.add(item_id)
            return events
        if method == "item/completed":
            item = params.get("item")
            if not isinstance(item, Mapping):
                return []
            item_id = str(item.get("id") or "")
            if item_id and item_id in self._turn_tool_item_ids:
                return []
            events = _thread_item_tool_events(item)
            if item_id and events:
                self._turn_tool_item_ids.add(item_id)
            return events
        if method == "thread/tokenUsage/updated":
            usage, input_tokens, output_tokens = _usage_payload(params.get("tokenUsage"))
            self._usage = usage
            return [TokenUsageEvent(
                type="system",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )]
        if method == "account/rateLimits/updated":
            self._quota.update(_quota_payload(params.get("rateLimits")))
            return [SystemStatusEvent(type="system", status="rate_limit")]
        if method == "model/rerouted":
            self._effective_model = str(
                params.get("toModel") or params.get("model") or self._effective_model
            )
            return [SystemStatusEvent(type="system", status="model_rerouted")]
        if method == "error" and not bool(params.get("willRetry")):
            error = params.get("error")
            message_text = str(error.get("message") or error) if isinstance(error, Mapping) else str(error)
            return [SystemStatusEvent(type="system", status=f"error:{message_text}")]
        return []

    async def run_streaming(
        self,
        request: AgentRequest,
        register_handle: Callable[[ActiveHandle | None], None],
    ) -> AsyncGenerator[StreamEvent, None]:
        peer = await self._ensure_peer(request)
        thread_id = await self._ensure_thread(request)
        sandbox, approval, reviewer = _mode_settings(request.mode)
        turn_response = await peer.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": self._inputs(request),
                "cwd": str(self.workspace_root),
                "model": self._effective_model or None,
                "effort": request.thinking_level or None,
                "approvalPolicy": approval,
                "approvalsReviewer": reviewer,
            },
            timeout=_CONTROL_TIMEOUT,
        )
        turn = turn_response.get("turn") if isinstance(turn_response, dict) else None
        turn_id = str(turn.get("id") or "") if isinstance(turn, dict) else ""
        if not turn_id:
            raise RpcProcessError("Codex app-server returned no turn id")
        self._turn_id = turn_id
        self._turn_tool_item_ids.clear()
        register_handle(CodexActiveHandle(self, thread_id, turn_id))
        message_parts: dict[str, list[str]] = {}
        message_phases: dict[str, str] = {}
        message_order: list[str] = []
        error_text = ""
        terminal_status = ""
        try:
            while True:
                message = await peer.next_message(timeout=_TURN_TIMEOUT)
                if message.get("_process_exit"):
                    raise RpcProcessError(str(message.get("error") or "Codex exited"))
                if "method" in message and "id" in message:
                    event = self._server_request_event(message)
                    if event is not None:
                        yield event
                    else:
                        await peer.respond(
                            message["id"],
                            error={"code": -32601, "message": "Unsupported client request"},
                        )
                    continue
                method = str(message.get("method") or "")
                params = message.get("params")
                params = params if isinstance(params, Mapping) else {}

                completed_item = params.get("item")
                if method in {"item/started", "item/completed"} and isinstance(
                    completed_item, Mapping
                ) and completed_item.get("type") == "agentMessage":
                    item_id = str(completed_item.get("id") or "")
                    if item_id:
                        if item_id not in message_parts:
                            message_parts[item_id] = []
                            message_order.append(item_id)
                        phase = _agent_message_phase(completed_item)
                        if phase:
                            message_phases[item_id] = phase

                if method == "item/agentMessage/delta":
                    item_id = str(params.get("itemId") or "") or "__legacy__"
                    if item_id not in message_parts:
                        message_parts[item_id] = []
                        message_order.append(item_id)
                    delta = str(params.get("delta") or "")
                    message_parts[item_id].append(delta)
                elif method == "item/completed":
                    if (
                        isinstance(completed_item, Mapping)
                        and completed_item.get("type") == "agentMessage"
                    ):
                        item_id = str(completed_item.get("id") or "") or "__legacy__"
                        if item_id not in message_parts:
                            message_parts[item_id] = []
                            message_order.append(item_id)
                        fallback_text = str(completed_item.get("text") or "")
                        if fallback_text and not message_parts[item_id]:
                            message_parts[item_id].append(fallback_text)
                            yield AssistantTextDelta(
                                type="assistant",
                                text=fallback_text,
                                phase=message_phases.get(item_id),
                            )
                if method == "error" and not bool(params.get("willRetry")):
                    raw_error = params.get("error")
                    error_text = (
                        str(raw_error.get("message") or raw_error)
                        if isinstance(raw_error, Mapping)
                        else str(raw_error)
                    )
                for event in self._notification_events(message, message_phases):
                    yield event
                if method != "turn/completed":
                    continue
                turn_payload = params.get("turn")
                turn_payload = turn_payload if isinstance(turn_payload, Mapping) else {}
                if str(turn_payload.get("id") or "") != turn_id:
                    continue
                terminal_status = str(turn_payload.get("status") or "")
                turn_error = turn_payload.get("error")
                if isinstance(turn_error, Mapping):
                    error_text = str(turn_error.get("message") or error_text)
                break
        finally:
            register_handle(None)
            self._turn_id = None
            # A server request cannot survive turn teardown. Decline it so the
            # app-server never carries a stale approval into the next turn.
            for public_id in list(self._permission_requests):
                self.send_permission_response(public_id, False)
            for public_id, pending in list(self._question_requests.items()):
                self._question_requests.pop(public_id, None)
                rpc_id, method, _params = pending
                if self._peer is not None and self._peer.running:
                    asyncio.create_task(self._peer.respond(
                        rpc_id,
                        result=(
                            {"action": "cancel"}
                            if method == "mcpServer/elicitation/request"
                            else {"answers": {}}
                        ),
                    ))

        is_error = terminal_status == "failed" or bool(error_text)
        final_parts = [
            "".join(message_parts[item_id]).strip()
            for item_id in message_order
            if message_phases.get(item_id) != "commentary"
        ]
        result_text = error_text if is_error else "\n\n".join(
            part for part in final_parts if part
        )
        if terminal_status == "interrupted" and not result_text:
            result_text = "Interrupted by user"
        yield ResultEvent(
            type="result",
            result=result_text,
            session_id=thread_id,
            is_error=is_error,
            effective_model=self._effective_model or request.model,
            usage=dict(self._usage),
            quota=dict(self._quota),
            cost_usd=None,
        )

    async def disconnect(self) -> None:
        peer = self._peer
        self._peer = None
        self._turn_id = None
        self._permission_requests.clear()
        self._question_requests.clear()
        self._peer_mcp_token = ""
        self._reset_settings()
        if peer is not None:
            await peer.close()

    @classmethod
    async def model_catalog(
        cls,
        workspace_root: Path,
        *,
        command: Sequence[str] | None = None,
        force: bool = False,
    ) -> list[dict[str, Any]]:
        if command is None:
            binary = resolve_codex_binary()
            if not binary:
                return []
            command = [binary, "app-server", "--stdio"]
        key = "\0".join(str(part) for part in command)
        cached = _MODEL_CACHE.get(key)
        if cached and not force and time.monotonic() - cached[0] < _MODEL_CACHE_TTL:
            return [dict(item) for item in cached[1]]
        peer = StdioJsonRpcPeer(
            command, cwd=workspace_root, name="codex model catalog",
            env=_codex_path_env(command[0]),
        )
        try:
            await peer.start()
            await peer.request(
                "initialize",
                {
                    "clientInfo": {"name": "ciaobot", "title": "Ciaobot", "version": "0.4"},
                    "capabilities": {"experimentalApi": True},
                },
                timeout=_CONTROL_TIMEOUT,
            )
            await peer.notify("initialized", {})
            catalog: list[dict[str, Any]] = []
            cursor: str | None = None
            for _page in range(20):
                response = await peer.request(
                    "model/list",
                    {"cursor": cursor, "limit": 100, "includeHidden": True},
                    timeout=_CONTROL_TIMEOUT,
                )
                data = response.get("data") if isinstance(response, dict) else None
                catalog.extend(
                    dict(item) for item in data or [] if isinstance(item, Mapping)
                )
                next_cursor = (
                    response.get("nextCursor") if isinstance(response, dict) else None
                )
                if not isinstance(next_cursor, str) or not next_cursor:
                    break
                cursor = next_cursor
            _MODEL_CACHE[key] = (time.monotonic(), catalog)
            return [dict(item) for item in catalog]
        except RpcError:
            logger.warning("Codex model discovery failed", exc_info=True)
            return []
        finally:
            await peer.close()

    @classmethod
    async def read_thread(
        cls,
        workspace_root: Path,
        thread_id: str,
        *,
        command: Sequence[str] | None = None,
    ) -> dict[str, Any] | None:
        if not thread_id:
            return None
        if command is None:
            binary = resolve_codex_binary()
            if not binary:
                return None
            command = [binary, "app-server", "--stdio"]
        peer = StdioJsonRpcPeer(
            command, cwd=workspace_root, name="codex history",
            env=_codex_path_env(command[0]),
        )
        try:
            await peer.start()
            await peer.request(
                "initialize",
                {
                    "clientInfo": {"name": "ciaobot", "title": "Ciaobot", "version": "0.4"},
                    "capabilities": {"experimentalApi": True},
                },
                timeout=_CONTROL_TIMEOUT,
            )
            await peer.notify("initialized", {})
            response = await peer.request(
                "thread/read",
                {"threadId": thread_id, "includeTurns": True},
                timeout=_CONTROL_TIMEOUT,
            )
            thread = response.get("thread") if isinstance(response, dict) else None
            return dict(thread) if isinstance(thread, Mapping) else None
        except RpcError:
            logger.info("Codex thread %s is not readable", thread_id, exc_info=True)
            return None
        finally:
            await peer.close()

    @classmethod
    async def read_collab_tree(
        cls,
        workspace_root: Path,
        parent: Mapping[str, Any],
        *,
        command: Sequence[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Read a bounded Codex collaboration tree with parent relationships."""
        pending = [
            {**item, "parent_agent_id": "", "root_turn_index": item.get("turn_index", 0)}
            for item in codex_collab_agents(parent).values()
        ]
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        while pending and len(seen) < limit:
            batch: list[dict[str, Any]] = []
            while pending and len(batch) < 16 and len(seen) + len(batch) < limit:
                item = pending.pop(0)
                agent_id = str(item.get("agent_id") or "")
                if not agent_id or agent_id in seen:
                    continue
                seen.add(agent_id)
                batch.append(item)
            if not batch:
                continue
            if command is None:
                threads = await asyncio.gather(*[
                    cls.read_thread(workspace_root, str(item["agent_id"]))
                    for item in batch
                ])
            else:
                threads = await asyncio.gather(*[
                    cls.read_thread(
                        workspace_root, str(item["agent_id"]), command=command
                    )
                    for item in batch
                ])
            for item, thread in zip(batch, threads):
                row = {
                    **item,
                    "status": _thread_lifecycle_status(
                        thread, str(item.get("status") or "unknown")
                    ),
                    "thread": thread,
                }
                result.append(row)
                if thread is None:
                    continue
                agent_id = str(item["agent_id"])
                root_turn_index = int(item.get("root_turn_index") or 0)
                for child in codex_collab_agents(thread).values():
                    pending.append({
                        **child,
                        "parent_agent_id": agent_id,
                        "root_turn_index": root_turn_index,
                    })
        return result


def codex_collab_tree_counts(tree: Sequence[Mapping[str, Any]]) -> tuple[int, bool]:
    """Return running and observed counts for ``read_collab_tree`` output."""
    return (
        sum(1 for item in tree if item.get("status") in _ACTIVE_COLLAB_STATES),
        bool(tree),
    )
