"""opencode provider over the local HTTP + SSE server.

Unlike Claude (in-process SDK) and Codex (stdio JSON-RPC), opencode ships a
real multi-session HTTP server. Ciaobot runs ``opencode serve`` on an ephemeral
loopback port and drives it over ``httpx``, consuming the ``/event`` SSE stream.

**One server process per active chat.** A shared server is tempting because
opencode isolates chats as sessions, but Ciaobot scopes its control-plane MCP
token per chat, and opencode's MCP configuration is server-wide (``/mcp`` and
``opencode.json``) rather than per-session. A shared server would force one
long-lived token across every chat and lose failure isolation. Per-session
*permission* and *model* are supported and are set on the session instead.

The wire contract is verified against the server's own OpenAPI document at
``/doc`` on startup, so an incompatible build fails closed with a readable
message rather than half-working.

Capability note: opencode has no method that injects a message into a running
turn, so ``steer`` is False and ``ProviderService`` keeps a mid-turn message in
its next-turn queue. Everything else Ciaobot needs — fork, abort, permissions,
structured questions, background subagents as child sessions — is native.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import socket
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ciao.model_tiers import MODEL_TIERS
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
from ciao.providers.base import (
    ActiveHandle,
    BaseSDKProvider,
    ProviderCapabilities,
    build_prompt,
    build_runtime_context,
)
from ciao.tool_path import resolve_tool

logger = logging.getLogger(__name__)

# Operations Ciaobot cannot work without. Checked against the server's own
# OpenAPI paths at connect time so an incompatible build fails closed. This is
# the machine-checkable equivalent of Codex's hand-maintained
# ``_REQUIRED_PROTOCOL_TOKENS``.
REQUIRED_PATHS: frozenset[str] = frozenset({
    "/global/health",
    "/event",
    "/session",
    "/session/{sessionID}",
    "/session/{sessionID}/abort",
    "/session/{sessionID}/children",
    "/session/{sessionID}/fork",
    "/session/{sessionID}/message",
    "/session/{sessionID}/prompt_async",
    "/permission/{requestID}/reply",
    "/question/{requestID}/reply",
    "/question/{requestID}/reject",
})

_SERVER_START_TIMEOUT = 30.0
_REQUEST_TIMEOUT = 30.0
_SHUTDOWN_TIMEOUT = 5.0

# opencode's built-in primary agents, keyed by Ciaobot's BridgeMode. `plan` is
# opencode's own read-only agent; everything else runs `build` and differs only
# in the permission ruleset below.
_MODE_AGENTS: dict[str, str] = {
    "plan": "plan",
    "normal": "build",
    "auto": "build",
    "bypass": "build",
}

# Per-session permission rulesets, as `POST /session` wants them: a *list* of
# {permission, pattern, action} rules, not the `{"*": "ask"}` map used in
# `opencode.json`. The two shapes are different and the API rejects the map
# with a bare 400, so keep this in rule form.
#
# Resolution is last-match-wins, so the wildcard goes first and the specific
# grants follow. Read-only tools are always allowed; `edit` is what separates
# auto from normal. Shell stays on the wildcard in every non-bypass mode, so a
# command still needs approval even in auto.
_READ_ONLY_TOOLS = ("read", "glob", "grep")


def _rules(*entries: tuple[str, str]) -> list[dict[str, str]]:
    return [
        {"permission": permission, "pattern": "*", "action": action}
        for permission, action in entries
    ]


_MODE_PERMISSIONS: dict[str, list[dict[str, str]]] = {
    "plan": _rules(("*", "ask"), *((tool, "allow") for tool in _READ_ONLY_TOOLS)),
    "normal": _rules(("*", "ask")),
    "auto": _rules(
        ("*", "ask"),
        *((tool, "allow") for tool in _READ_ONLY_TOOLS),
        ("edit", "allow"),
    ),
    "bypass": _rules(("*", "allow")),
}


@dataclass(frozen=True, slots=True)
class OpencodeSettings:
    """Operator overrides for the opencode tier aliases.

    Empty string means "no pin": the tier falls through to whatever model the
    session's configured provider resolves. Mirrors ``CodexSettings`` so
    ``AppSettings.provider_routing`` can drive both the same way.
    """

    haiku_model: str = ""
    sonnet_model: str = ""
    opus_model: str = ""
    fable_model: str = ""

    def tier_overrides(self) -> dict[str, str]:
        return {tier: getattr(self, f"{tier}_model") for tier in MODEL_TIERS}


def opencode_tier_overrides(config: object) -> dict[str, str]:
    """Extract the per-tier opencode pins from a (duck-typed) config object."""
    settings = getattr(config, "opencode", None)
    if settings is None:
        return {}
    return {tier: model for tier, model in settings.tier_overrides().items() if model}


def resolve_opencode_binary(env: Mapping[str, str] | None = None) -> str | None:
    """Absolute path to the opencode CLI, or None when it is not installed."""
    source = env if env is not None else os.environ
    explicit = str(source.get("CIAO_OPENCODE_BIN", "")).strip()
    if explicit:
        path = Path(explicit).expanduser()
        return str(path.resolve()) if path.is_file() else None
    return resolve_tool("opencode")


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


def missing_required_paths(spec: Mapping[str, Any]) -> tuple[str, ...]:
    """Required operations absent from a served OpenAPI document."""
    paths = spec.get("paths")
    available = set(paths) if isinstance(paths, Mapping) else set()
    return tuple(sorted(REQUIRED_PATHS - available))


def _sanitize_error(message: object) -> str:
    """First line only: opencode error payloads carry a bundler stack trace."""
    text = str(message or "").strip()
    return text.split("\n", 1)[0].strip()


def error_text(error: Mapping[str, Any] | None) -> str:
    """Human-readable text from a ``session.error`` payload."""
    if not isinstance(error, Mapping):
        return "opencode reported an error"
    data = error.get("data")
    message = data.get("message") if isinstance(data, Mapping) else None
    return _sanitize_error(message) or str(error.get("name") or "opencode error")


def _summarize_tool_input(tool: str, raw: object) -> str:
    """Short, non-secret one-liner describing a tool call for the UI."""
    if not isinstance(raw, Mapping):
        return ""
    for key in ("filePath", "path", "file", "pattern", "query", "command", "description"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:400]
    return json.dumps(raw, ensure_ascii=False)[:200]


def _file_touches(tool: str, raw: object) -> list[dict[str, str]] | None:
    """Paths a tool call writes, for the PWA's file-change cards."""
    if not isinstance(raw, Mapping):
        return None
    action = {"write": "write", "edit": "edit", "patch": "edit"}.get(tool.lower())
    if action is None:
        return None
    path = raw.get("filePath") or raw.get("path")
    if not isinstance(path, str) or not path.strip():
        return None
    return [{"file_path": path.strip(), "action": action}]


def usage_payload(tokens: Mapping[str, Any] | None) -> dict[str, str]:
    """Normalize opencode token counts into Ciaobot's usage fields."""
    if not isinstance(tokens, Mapping):
        return {}
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), Mapping) else {}
    usage: dict[str, str] = {}
    for key, source in (
        ("inputTokens", tokens.get("input")),
        ("outputTokens", tokens.get("output")),
        ("reasoningTokens", tokens.get("reasoning")),
        ("cacheReadTokens", (cache or {}).get("read")),
        ("cacheWriteTokens", (cache or {}).get("write")),
    ):
        if isinstance(source, (int, float)) and source:
            usage[key] = str(int(source))
    return usage


def _token_usage_events(tokens: object) -> list[StreamEvent]:
    """A live token-count event, when the payload carries real counts."""
    if not isinstance(tokens, Mapping):
        return []
    read_in = int(tokens.get("input") or 0)
    read_out = int(tokens.get("output") or 0)
    if not read_in and not read_out:
        return []
    return [TokenUsageEvent(type="token_usage", input_tokens=read_in, output_tokens=read_out)]


def mode_settings(mode: BridgeMode) -> tuple[str, list[dict[str, str]]]:
    """Map a Ciaobot mode onto an opencode (agent, permission ruleset)."""
    key = mode if mode in _MODE_AGENTS else "normal"
    return _MODE_AGENTS[key], [dict(rule) for rule in _MODE_PERMISSIONS[key]]


def split_model(model: str) -> tuple[str, str]:
    """Split ``providerID/modelID`` into its parts.

    opencode addresses models as ``provider/model`` (e.g.
    ``anthropic/claude-sonnet-4-6``). A bare id has no provider, and the
    caller lets opencode fall back to its configured default.
    """
    value = (model or "").strip()
    if not value:
        return "", ""
    provider, sep, rest = value.partition("/")
    if not sep or not rest:
        return "", value
    return provider, rest


class OpencodeActiveHandle(ActiveHandle):
    """Stops the in-flight turn by aborting its session."""

    def __init__(self, provider: "OpencodeProvider", session_id: str) -> None:
        self._provider = provider
        self._session_id = session_id

    async def stop(self) -> None:
        await self._provider.abort_session(self._session_id)


@dataclass(slots=True)
class _PendingRequest:
    """A permission or question request awaiting the operator's reply."""

    request_id: str
    session_id: str
    tool_use_id: str = ""


class OpencodeProvider(BaseSDKProvider):
    """Runs a chat turn against a per-chat ``opencode serve`` process."""

    capabilities = ProviderCapabilities(
        resume=True,
        fork=True,
        images=True,
        stop=True,
        # No upstream method injects into a running turn; a mid-turn message
        # queues for the next one instead.
        steer=False,
        permissions=True,
        structured_questions=True,
        dynamic_models=True,
        # Reasoning effort is per model (opencode calls it a model `variant`),
        # so the level list is narrowed per model from the catalog rather than
        # being a fixed ladder — same arrangement as Codex.
        thinking_levels=True,
        usage=True,
        # opencode is bring-your-own-provider: there is no unified quota or
        # reset-time snapshot to report.
        quota=False,
        subagents=True,
        background_subagents=True,
        subagent_messages=True,
        session_history=True,
        schedule_unattended=True,
    )

    def __init__(self, workspace_root: Path, *, config: object | None = None) -> None:
        super().__init__(workspace_root, config=config)
        self._process: asyncio.subprocess.Process | None = None
        self._client: httpx.AsyncClient | None = None
        self._base_url: str = ""
        self._password: str = ""
        self._session_id: str = ""
        self._permission_requests: dict[str, _PendingRequest] = {}
        self._question_requests: dict[str, _PendingRequest] = {}
        self._tool_calls: dict[str, str] = {}
        self._mcp_token: str = ""
        # Per-turn stream state, reset by `_reset_turn_state`.
        self._emitted: dict[str, int] = {}
        # partID -> part type. `message.part.delta` reports the *field* it is
        # filling, and a ReasoningPart stores its content in `text` just like a
        # TextPart — so `field` alone cannot tell reasoning from prose. The
        # part is always announced by `message.part.updated` before its deltas
        # arrive, which is what makes this lookup reliable.
        self._part_types: dict[str, str] = {}
        self._user_message_id: str = ""
        self._usage: dict[str, str] = {}
        self._cost: float | None = None

    def _reset_turn_state(self) -> None:
        self._emitted.clear()
        self._part_types.clear()
        self._tool_calls.clear()
        self._user_message_id = ""
        self._usage = {}
        self._cost = None

    # ---------------------------------------------------------------- server

    @property
    def current_session_id(self) -> str | None:
        return self._session_id or None

    @property
    def can_drain(self) -> bool:
        """opencode has no between-turns event source to drain."""
        return False

    async def _ensure_server(self, request: AgentRequest) -> httpx.AsyncClient:
        """Start (or reuse) this chat's server and return its HTTP client.

        A changed MCP token forces a full restart: opencode reads MCP
        configuration at server scope, so the running process cannot be
        re-pointed at a new token.
        """
        if self._client is not None and self._process is not None:
            if self._process.returncode is None and request.mcp_token == self._mcp_token:
                return self._client
            await self.disconnect()

        binary = resolve_opencode_binary(request.extra_env or None)
        if not binary:
            raise FileNotFoundError(
                "opencode CLI not found. Install it, or set CIAO_OPENCODE_BIN."
            )

        port = _free_port()
        self._password = secrets.token_urlsafe(24)
        env = {
            **os.environ,
            **(request.extra_env or {}),
            "OPENCODE_SERVER_PASSWORD": self._password,
        }
        if request.mcp_token:
            env["CIAO_MCP_SESSION_TOKEN"] = request.mcp_token
        self._mcp_token = request.mcp_token

        self._process = await asyncio.create_subprocess_exec(
            binary, "serve", "--port", str(port), "--hostname", "127.0.0.1",
            cwd=str(self.workspace_root),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._base_url = f"http://127.0.0.1:{port}"
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            auth=("opencode", self._password),
            # No read timeout: the SSE stream is idle between turns and must
            # not be torn down for being quiet.
            timeout=httpx.Timeout(_REQUEST_TIMEOUT, read=None),
        )
        try:
            await self._await_health()
            await self._verify_contract()
        except BaseException:
            # A server we could not validate is a server nobody will ever
            # shut down; reap it here rather than leaking it for the life of
            # the app.
            await self.disconnect()
            raise
        return self._client

    async def _await_health(self) -> None:
        """Poll ``/global/health`` until the server answers or we give up."""
        assert self._client is not None
        deadline = asyncio.get_running_loop().time() + _SERVER_START_TIMEOUT
        last_error: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            if self._process is not None and self._process.returncode is not None:
                raise RuntimeError(
                    f"opencode serve exited with code {self._process.returncode}"
                )
            try:
                response = await self._client.get("/global/health", timeout=2.0)
                if response.status_code == 200:
                    return
            except httpx.HTTPError as exc:  # not up yet
                last_error = exc
            await asyncio.sleep(0.2)
        raise TimeoutError(f"opencode serve did not become healthy: {last_error}")

    async def _verify_contract(self) -> None:
        """Fail closed when the installed build is missing required operations."""
        assert self._client is not None
        try:
            response = await self._client.get("/doc", timeout=10.0)
            response.raise_for_status()
            spec = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"could not read the opencode API document: {exc}") from exc
        missing = missing_required_paths(spec)
        if missing:
            raise RuntimeError(
                "this opencode build is missing operations Ciaobot needs: "
                + ", ".join(missing)
            )

    async def disconnect(self) -> None:
        """Tear down the server, denying anything still awaiting a reply."""
        for pending in list(self._permission_requests.values()):
            await self._reply_permission(pending, "reject")
        for pending in list(self._question_requests.values()):
            await self._reject_question(pending)
        self._permission_requests.clear()
        self._question_requests.clear()
        self._tool_calls.clear()

        if self._client is not None:
            await self._client.aclose()
            self._client = None
        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=_SHUTDOWN_TIMEOUT)
            except (TimeoutError, asyncio.TimeoutError):
                process.kill()
                await process.wait()
        self._base_url = ""
        self._mcp_token = ""
        self._reset_settings()

    # --------------------------------------------------------------- session

    async def _ensure_session(self, request: AgentRequest) -> str:
        """Resume, fork, or create the session this turn runs in."""
        client = self._client
        assert client is not None
        agent, permission = mode_settings(request.mode)
        provider_id, model_id = split_model(request.model)

        resume = (request.resume_session or "").strip()
        if resume and request.fork_session:
            response = await client.post(f"/session/{resume}/fork", json={})
            if response.status_code < 400:
                self._session_id = str(response.json().get("id") or "")
                if self._session_id:
                    return self._session_id
            logger.warning("opencode fork failed (%s); starting a new session", response.status_code)
        elif resume:
            response = await client.get(f"/session/{resume}")
            if response.status_code < 400:
                self._session_id = resume
                return resume
            logger.info("opencode session %s is gone; starting a new one", resume)

        payload: dict[str, Any] = {"agent": agent, "permission": permission}
        if model_id:
            model: dict[str, Any] = {"id": model_id, "providerID": provider_id}
            if request.thinking_level:
                model["variant"] = request.thinking_level
            payload["model"] = model
        response = await client.post("/session", json=payload)
        response.raise_for_status()
        self._session_id = str(response.json().get("id") or "")
        return self._session_id

    async def abort_session(self, session_id: str) -> None:
        client = self._client
        if client is None or not session_id:
            return
        try:
            await client.post(f"/session/{session_id}/abort")
        except httpx.HTTPError:
            logger.debug("opencode abort failed for %s", session_id, exc_info=True)

    def _prompt_parts(self, request: AgentRequest) -> list[dict[str, Any]]:
        """Text plus any attached images, in opencode's part shape."""
        parts: list[dict[str, Any]] = [{"type": "text", "text": build_prompt(request)}]
        for image in request.images:
            parts.append({
                "type": "file",
                "mime": image.mime_type,
                "filename": image.original_filename,
                "url": image.path.resolve().as_uri(),
            })
        return parts

    async def steer(self, request: AgentRequest) -> bool:
        """Always False: opencode cannot inject into a running turn.

        Returning False (rather than sending a second prompt) is deliberate —
        a second prompt would be queued or would abort the active turn, and
        neither is what steering means. ``ProviderService`` keeps the message
        for the next turn instead.
        """
        return False

    # ------------------------------------------------------------ permissions

    def tool_use_id_for_request(self, request_id: str) -> str:
        pending = self._permission_requests.get(request_id)
        return pending.tool_use_id if pending is not None else ""

    async def _reply_permission(self, pending: _PendingRequest, reply: str) -> bool:
        client = self._client
        if client is None:
            return False
        try:
            response = await client.post(
                f"/permission/{pending.request_id}/reply", json={"reply": reply}
            )
            return response.status_code < 400
        except httpx.HTTPError:
            logger.debug("opencode permission reply failed", exc_info=True)
            return False

    def send_permission_response(self, request_id: str, approved: bool) -> bool:
        pending = self._permission_requests.pop(request_id, None)
        if pending is None or self._client is None:
            return False
        asyncio.create_task(
            self._reply_permission(pending, "once" if approved else "reject")
        )
        return True

    async def _reject_question(self, pending: _PendingRequest) -> bool:
        client = self._client
        if client is None:
            return False
        try:
            response = await client.post(f"/question/{pending.request_id}/reject", json={})
            return response.status_code < 400
        except httpx.HTTPError:
            logger.debug("opencode question reject failed", exc_info=True)
            return False

    def send_question_response(
        self, request_id: str, answers: Mapping[str, Sequence[str]]
    ) -> bool:
        pending = self._question_requests.pop(request_id, None)
        if pending is None or self._client is None:
            return False
        payload = {
            "answers": [
                {"questionID": str(question_id), "values": [str(v) for v in values]}
                for question_id, values in answers.items()
            ]
        }
        if not payload["answers"]:
            asyncio.create_task(self._reject_question(pending))
            return True

        async def _send() -> None:
            client = self._client
            if client is None:
                return
            try:
                await client.post(f"/question/{pending.request_id}/reply", json=payload)
            except httpx.HTTPError:
                logger.debug("opencode question reply failed", exc_info=True)

        asyncio.create_task(_send())
        return True

    # -------------------------------------------------------------- streaming

    def _emit_suffix(self, part_id: str, text: str) -> str:
        """Return only the not-yet-emitted tail of a cumulative part.

        opencode streams the same text twice: incrementally via
        ``message.part.delta`` and cumulatively via ``message.part.updated``
        (which restates the whole part each time). Emitting both would double
        every token, and consuming only one is not safe either — which of the
        two a model produces varies. Tracking how much of each part has already
        been emitted makes either source, or both, come out right.
        """
        already = self._emitted.get(part_id, 0)
        if len(text) <= already:
            return ""
        self._emitted[part_id] = len(text)
        return text[already:]

    def _event_to_stream(self, event: Mapping[str, Any]) -> list[StreamEvent]:
        """Translate one SSE event into zero or more Ciaobot stream events.

        The `message.part.*` family is the live one. The `session.next.*`
        events in the schema are a separate, newer stream that this build does
        not emit for ordinary turns; they are handled too so that a future
        opencode which switches over keeps working.
        """
        kind = str(event.get("type") or "")
        props = event.get("properties")
        props = props if isinstance(props, Mapping) else {}

        if kind == "message.part.delta":
            return self._part_delta(props)
        if kind == "message.part.updated":
            return self._part_updated(props)
        if kind == "message.updated":
            return self._message_updated(props)

        # Newer `session.next.*` stream, kept as a forward-compatible path.
        if kind == "session.next.text.delta":
            text = str(props.get("delta") or "")
            return [AssistantTextDelta(type="text", text=text)] if text else []

        if kind == "session.next.reasoning.delta":
            text = str(props.get("delta") or "")
            return [ThinkingEvent(type="thinking", text=text)] if text else []

        if kind == "session.next.tool.called":
            call_id = str(props.get("callID") or "")
            tool = str(props.get("tool") or "")
            self._tool_calls[call_id] = tool
            return [ToolUseEvent(
                type="tool_use",
                tool_name=tool,
                tool_input=_summarize_tool_input(tool, props.get("input")),
                tool_use_id=call_id or None,
                file_touches=_file_touches(tool, props.get("input")),
            )]

        if kind in {"session.next.tool.success", "session.next.tool.failed"}:
            call_id = str(props.get("callID") or "")
            tool = self._tool_calls.pop(call_id, "")
            detail = error_text(props.get("error")) if kind.endswith("failed") else ""
            return [ToolUseEvent(
                type="tool_result",
                tool_name=tool,
                tool_input=detail,
                tool_use_id=call_id or None,
            )]

        if kind == "session.next.step.ended":
            return _token_usage_events(props.get("tokens"))

        if kind in {"permission.v2.asked", "permission.asked"}:
            return self._permission_event(props)

        if kind in {"question.v2.asked", "question.asked"}:
            return self._question_event(props)

        if kind == "session.status":
            status = props.get("status")
            name = status.get("type") if isinstance(status, Mapping) else None
            if name and name != "idle":
                return [SystemStatusEvent(type="system", status=str(name))]
            return []

        return []

    def _part_delta(self, props: Mapping[str, Any]) -> list[StreamEvent]:
        """Incremental text/reasoning for one part."""
        delta = str(props.get("delta") or "")
        if not delta:
            return []
        part_id = str(props.get("partID") or "")
        field = str(props.get("field") or "text")
        if field not in {"text", "reasoning"}:
            return []
        # Count it against the part so the cumulative update that follows does
        # not replay the same characters.
        self._emitted[part_id] = self._emitted.get(part_id, 0) + len(delta)
        # The part's own type decides, not the field name: reasoning content
        # also arrives in a field called `text`.
        if self._part_types.get(part_id, "text") == "reasoning" or field == "reasoning":
            return [ThinkingEvent(type="thinking", text=delta)]
        return [AssistantTextDelta(type="text", text=delta)]

    def _part_updated(self, props: Mapping[str, Any]) -> list[StreamEvent]:
        """A settled part: text, reasoning, a tool call, or a step boundary."""
        part = props.get("part")
        if not isinstance(part, Mapping):
            return []
        part_type = str(part.get("type") or "")
        part_id = str(part.get("id") or "")
        if part_id and part_type:
            self._part_types[part_id] = part_type

        if part_type in {"text", "reasoning"}:
            # A user part is echoed back on submit; the visible user bubble
            # already exists, so replaying it would duplicate the prompt.
            if part.get("messageID") and part.get("messageID") == self._user_message_id:
                return []
            suffix = self._emit_suffix(part_id, str(part.get("text") or ""))
            if not suffix:
                return []
            if part_type == "reasoning":
                return [ThinkingEvent(type="thinking", text=suffix)]
            return [AssistantTextDelta(type="text", text=suffix)]

        if part_type == "tool":
            return self._tool_part(part)

        if part_type == "step-finish":
            return _token_usage_events(part.get("tokens"))

        return []

    def _tool_part(self, part: Mapping[str, Any]) -> list[StreamEvent]:
        """One tool call, emitted once on start and once on settle."""
        call_id = str(part.get("callID") or part.get("id") or "")
        tool = str(part.get("tool") or "")
        state = part.get("state")
        state = state if isinstance(state, Mapping) else {}
        status = str(state.get("status") or "")
        raw_input = state.get("input")

        if status in {"pending", "running"}:
            if call_id in self._tool_calls:
                return []  # already announced; a running update is not news
            self._tool_calls[call_id] = tool
            return [ToolUseEvent(
                type="tool_use",
                tool_name=tool,
                tool_input=_summarize_tool_input(tool, raw_input),
                tool_use_id=call_id or None,
                file_touches=_file_touches(tool, raw_input),
            )]

        if status in {"completed", "error"}:
            events: list[StreamEvent] = []
            if call_id not in self._tool_calls:
                # A fast tool can settle before any running update arrives, so
                # the call would otherwise never be shown at all.
                events.append(ToolUseEvent(
                    type="tool_use",
                    tool_name=tool,
                    tool_input=_summarize_tool_input(tool, raw_input),
                    tool_use_id=call_id or None,
                    file_touches=_file_touches(tool, raw_input),
                ))
            self._tool_calls.pop(call_id, None)
            detail = _sanitize_error(state.get("error")) if status == "error" else ""
            events.append(ToolUseEvent(
                type="tool_result",
                tool_name=tool,
                tool_input=detail,
                tool_use_id=call_id or None,
            ))
            return events

        return []

    def _message_updated(self, props: Mapping[str, Any]) -> list[StreamEvent]:
        """Track the turn's user message id and its final usage/cost."""
        info = props.get("info")
        if not isinstance(info, Mapping):
            return []
        if info.get("role") == "user":
            self._user_message_id = str(info.get("id") or "")
            return []
        if info.get("role") != "assistant":
            return []
        cost = info.get("cost")
        if isinstance(cost, (int, float)):
            self._cost = float(cost)
        tokens = info.get("tokens")
        if isinstance(tokens, Mapping):
            self._usage = usage_payload(tokens) or self._usage
        return _token_usage_events(tokens)

    def _permission_event(self, props: Mapping[str, Any]) -> list[StreamEvent]:
        """Surface an approval prompt, naming what is actually being approved.

        The live event is `permission.asked`, whose payload is
        ``{permission, patterns, metadata, always, tool:{callID}}``. The
        schema's newer `permission.v2.asked` uses ``{action, resources}``
        instead, so both are read — an approval card that cannot say *what* it
        is approving is worse than useless.
        """
        request_id = str(props.get("id") or "")
        if not request_id:
            return []

        # v1 names the tool in `permission`; v2 names it in `action`.
        tool_name = str(props.get("permission") or props.get("action") or "").strip()
        detail = ""
        metadata = props.get("metadata")
        if isinstance(metadata, Mapping):
            for key in ("command", "filePath", "path", "url", "pattern"):
                value = metadata.get(key)
                if isinstance(value, str) and value.strip():
                    detail = value.strip()
                    break
        if not detail:
            # `patterns` (v1) / `resources` (v2) hold the concrete targets.
            targets = props.get("patterns")
            if not isinstance(targets, list):
                targets = props.get("resources")
            if isinstance(targets, list):
                detail = ", ".join(str(item) for item in targets if str(item).strip())

        tool = props.get("tool")
        call_id = str(tool.get("callID") or "") if isinstance(tool, Mapping) else ""
        self._permission_requests[request_id] = _PendingRequest(
            request_id=request_id,
            session_id=str(props.get("sessionID") or ""),
            tool_use_id=call_id,
        )
        label = tool_name or "run a tool"
        return [PermissionRequestEvent(
            type="permission_request",
            message=f"opencode wants to use {label}" if tool_name else "opencode needs approval",
            tool_name=label,
            tool_input=detail[:400],
            request_id=request_id,
        )]

    def _question_event(self, props: Mapping[str, Any]) -> list[StreamEvent]:
        """Surface a structured question as the PWA's question card."""
        request_id = str(props.get("id") or "")
        questions = props.get("questions")
        if not request_id or not isinstance(questions, list) or not questions:
            return []
        self._question_requests[request_id] = _PendingRequest(
            request_id=request_id,
            session_id=str(props.get("sessionID") or ""),
        )
        payload = {
            "questions": [
                {
                    "question": str(item.get("question") or ""),
                    "header": str(item.get("header") or ""),
                    "multiSelect": bool(item.get("multiple")),
                    "allowCustom": bool(item.get("custom")),
                    "options": [
                        {
                            "label": str(option.get("label") or option.get("value") or ""),
                            "description": str(option.get("description") or ""),
                        }
                        for option in (item.get("options") or [])
                        if isinstance(option, Mapping)
                    ],
                }
                for item in questions
                if isinstance(item, Mapping)
            ]
        }
        return [ToolUseEvent(
            type="tool_use",
            tool_name="AskUserQuestion",
            tool_input=json.dumps(payload, ensure_ascii=False),
            tool_use_id=request_id,
            request_id=request_id,
        )]

    async def run_streaming(
        self,
        request: AgentRequest,
        register_handle: Callable[[ActiveHandle | None], None],
    ) -> AsyncGenerator[StreamEvent, None]:
        client = await self._ensure_server(request)
        session_id = await self._ensure_session(request)
        self._remember_settings(request)
        self._reset_turn_state()
        register_handle(OpencodeActiveHandle(self, session_id))

        agent, _permission = mode_settings(request.mode)
        provider_id, model_id = split_model(request.model)
        body: dict[str, Any] = {"agent": agent, "parts": self._prompt_parts(request)}
        if model_id:
            body["model"] = {"providerID": provider_id, "modelID": model_id}
        # Reasoning effort rides beside the model, not inside it, on prompts.
        if request.thinking_level:
            body["variant"] = request.thinking_level
        runtime = build_runtime_context(request)
        if runtime:
            body["system"] = runtime

        error: str = ""
        saw_output = False

        try:
            async with client.stream("GET", "/event") as stream:
                stream.raise_for_status()
                # Subscribe before prompting: opencode starts emitting as soon
                # as the prompt is accepted, and a late subscriber loses the
                # opening deltas.
                response = await client.post(
                    f"/session/{session_id}/prompt_async", json=body
                )
                if response.status_code >= 400:
                    detail = _sanitize_error(response.text)
                    yield ResultEvent(
                        type="result",
                        result=f"opencode rejected the prompt: {detail}",
                        session_id=session_id,
                        is_error=True,
                    )
                    return

                async for raw in stream.aiter_lines():
                    if not raw.startswith("data: "):
                        continue
                    try:
                        event = json.loads(raw[6:])
                    except ValueError:
                        continue
                    if not isinstance(event, Mapping):
                        continue
                    props = event.get("properties")
                    props = props if isinstance(props, Mapping) else {}
                    # The stream is server-wide; ignore other sessions. Child
                    # sessions (subagents) are read separately via /children.
                    event_session = str(props.get("sessionID") or "")
                    if event_session and event_session != session_id:
                        continue

                    kind = str(event.get("type") or "")
                    if kind == "session.error":
                        # Keep the first error of the turn. opencode emits the
                        # failure once before `session.idle` and often repeats
                        # it afterwards with a bundler stack appended; the
                        # first one is both authoritative and the cleaner text,
                        # and the repeat lands after we have already stopped.
                        error = error or error_text(props.get("error"))
                        continue
                    if kind == "session.idle":
                        break

                    for converted in self._event_to_stream(event):
                        saw_output = saw_output or converted.type in {"text", "tool_use"}
                        yield converted
        except httpx.HTTPError as exc:
            yield ResultEvent(
                type="result",
                result=f"opencode connection failed: {exc}",
                session_id=session_id,
                is_error=True,
            )
            return
        finally:
            register_handle(None)

        yield ResultEvent(
            type="result",
            result=error or "",
            session_id=session_id,
            is_error=bool(error),
            effective_model=request.model,
            # Accumulated from the assistant message's own totals rather than
            # summed per step, so a retried step cannot double-count.
            usage=self._usage,
            cost_usd=self._cost,
            fallback_final=bool(error) and saw_output,
        )

    # ----------------------------------------------------------- provider API

    @classmethod
    async def model_catalog(
        cls, workspace_root: Path, *, force: bool = False
    ) -> list[dict[str, Any]]:
        """Models the signed-in opencode account can currently reach.

        Runs a short-lived server rather than reusing a chat's: the catalog is
        queried from Settings, where no chat is necessarily open.
        """
        async with _EphemeralServer(workspace_root) as client:
            if client is None:
                return []
            try:
                response = await client.get("/provider")
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError):
                return []
        return _catalog_from_providers(payload)

    @classmethod
    async def read_thread(
        cls, workspace_root: Path, session_id: str
    ) -> dict[str, Any]:
        """Session metadata plus its message history, for transcript replay."""
        async with _EphemeralServer(workspace_root) as client:
            if client is None or not session_id:
                return {}
            try:
                info = await client.get(f"/session/{session_id}")
                info.raise_for_status()
                messages = await client.get(f"/session/{session_id}/message")
                messages.raise_for_status()
                return {"info": info.json(), "messages": messages.json()}
            except (httpx.HTTPError, ValueError):
                return {}

    @classmethod
    async def read_collab_tree(
        cls, workspace_root: Path, session_id: str
    ) -> list[dict[str, Any]]:
        """Child sessions — opencode's background subagents."""
        async with _EphemeralServer(workspace_root) as client:
            if client is None or not session_id:
                return []
            try:
                response = await client.get(f"/session/{session_id}/children")
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError):
                return []
        return payload if isinstance(payload, list) else []

    @classmethod
    async def delete_thread(cls, workspace_root: Path, session_id: str) -> bool:
        async with _EphemeralServer(workspace_root) as client:
            if client is None or not session_id:
                return False
            try:
                response = await client.delete(f"/session/{session_id}")
                return response.status_code < 400
            except httpx.HTTPError:
                return False


def _catalog_from_providers(payload: object) -> list[dict[str, Any]]:
    """Flatten ``GET /provider`` into Ciaobot's ``{model, label}`` rows.

    Only *connected* providers contribute: opencode's ``all`` list enumerates
    every backend it knows how to talk to (hundreds), which is a catalog of
    possibilities rather than of models the user can actually run.
    """
    if not isinstance(payload, Mapping):
        return []
    connected = payload.get("connected")
    # An empty `connected` list means "nothing authenticated yet", which is a
    # real state (a fresh install reports exactly that) and must yield no
    # models. Only a *missing* key means the build does not report the
    # distinction, in which case fall back to listing everything.
    filter_connected = isinstance(connected, list)
    connected_ids = {str(item) for item in connected} if filter_connected else set()
    rows: list[dict[str, Any]] = []
    for provider in payload.get("all") or []:
        if not isinstance(provider, Mapping):
            continue
        provider_id = str(provider.get("id") or "")
        if filter_connected and provider_id not in connected_ids:
            continue
        models = provider.get("models")
        entries = models.values() if isinstance(models, Mapping) else (models or [])
        for model in entries:
            if not isinstance(model, Mapping):
                continue
            model_id = str(model.get("id") or "")
            if not model_id:
                continue
            variants = model.get("variants")
            rows.append({
                "model": f"{provider_id}/{model_id}",
                "label": f"{model.get('name') or model_id} ({provider_id})",
                # Reasoning-effort variants this model accepts, e.g.
                # ["low", "medium", "high"]. Empty for models with no effort
                # control; opencode calls the parameter `variant`.
                "variants": sorted(variants) if isinstance(variants, Mapping) else [],
            })
    return rows


class _EphemeralServer:
    """Async context manager running a throwaway ``opencode serve``.

    Used by the classmethod read paths (model catalog, history, child
    sessions), which run outside any chat and so have no long-lived server.
    Yields ``None`` when opencode is not installed, so callers degrade to an
    empty result instead of raising into a settings route.
    """

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root
        self._process: asyncio.subprocess.Process | None = None
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> httpx.AsyncClient | None:
        binary = resolve_opencode_binary()
        if not binary:
            return None
        port = _free_port()
        password = secrets.token_urlsafe(24)
        try:
            self._process = await asyncio.create_subprocess_exec(
                binary, "serve", "--port", str(port), "--hostname", "127.0.0.1",
                cwd=str(self._workspace_root),
                env={**os.environ, "OPENCODE_SERVER_PASSWORD": password},
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return None
        self._client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            auth=("opencode", password),
            timeout=httpx.Timeout(_REQUEST_TIMEOUT),
        )
        deadline = asyncio.get_running_loop().time() + _SERVER_START_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            if self._process.returncode is not None:
                return None
            try:
                response = await self._client.get("/global/health", timeout=2.0)
                if response.status_code == 200:
                    return self._client
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
        return None

    async def __aexit__(self, *_exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=_SHUTDOWN_TIMEOUT)
            except (TimeoutError, asyncio.TimeoutError):
                process.kill()
                await process.wait()


def opencode_login_status(
    env: Mapping[str, str] | None = None, *, timeout: float = 5.0
) -> dict[str, Any]:
    """Bounded, credential-free opencode install/auth status for Settings."""
    import subprocess

    from ciao.setup_status import _provider

    binary = resolve_opencode_binary(env)
    if not binary:
        return _provider(
            name="opencode",
            ok=False,
            auth="missing",
            command="opencode",
            detail="not installed",
            version="not installed",
        )
    version = ""
    try:
        result = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=timeout
        )
        version = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    except (OSError, subprocess.SubprocessError):
        version = ""
    accounts: list[str] = []
    try:
        listed = subprocess.run(
            [binary, "auth", "list"], capture_output=True, text=True, timeout=timeout
        )
        for line in listed.stdout.splitlines():
            text = line.strip()
            # Skip the header and any decorative rules; keep provider rows.
            if text and not text.lower().startswith(("credentials", "─", "-")):
                accounts.append(text)
    except (OSError, subprocess.SubprocessError):
        pass
    connected = bool(accounts)
    return _provider(
        name="opencode",
        ok=connected,
        auth="oauth" if connected else "login_required",
        command="opencode auth login",
        detail=f"{len(accounts)} provider(s) authenticated" if connected else "login required",
        version=version or "unknown",
    )


def opencode_system_skills(env: Mapping[str, str] | None = None) -> list[str]:
    """Skills opencode's own CLI loads. It has no separate bundled catalog."""
    return []
