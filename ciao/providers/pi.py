"""Pi (coding agent) provider integration.

Spawns ``pi --mode rpc`` as a subprocess and speaks JSONL over stdin/stdout.
Maps Pi events to Ciao's StreamEvent types for UI consumption.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Pi providers that resolve natively (no models.json entry needed). Sourced from
# pi-ai's models.generated.js (built-in catalogue) plus the ``pi-ollama-cloud``
# extension package (installed via ~/.pi/agent/settings.json). The plain
# ``ollama`` provider is intentionally NOT in this set: Ciao writes its config
# into ~/.pi/agent/models.json so Pi can resolve ollama.com models we list.
_BUILT_IN_PROVIDERS = frozenset((
    "amazon-bedrock",
    "anthropic",
    "azure-openai-responses",
    "cerebras",
    "cloudflare-ai-gateway",
    "cloudflare-workers-ai",
    "deepseek",
    "fireworks",
    "google",
    "google-vertex",
    "groq",
    "huggingface",
    "kimi-coding",
    "minimax",
    "minimax-cn",
    "mistral",
    "moonshotai",
    "moonshotai-cn",
    "ollama-cloud",
    "openai",
    "openai-codex",
    "opencode",
    "opencode-go",
    "openrouter",
    "together",
    "vercel-ai-gateway",
    "xai",
    "xiaomi",
    "zai",
))


_LOCAL_PROVIDER = "ollama-local"


def _resolve_provider_and_model(
    model: str, default_provider: str, local_models: tuple[str, ...] = ()
) -> tuple[str, str]:
    """Split a ``provider/model`` id into Pi CLI arguments.

    Models registered under built-in Pi providers (e.g. ``openai-codex/gpt-5.5``,
    ``openrouter/anthropic/claude-3.5-sonnet``) must be dispatched with the
    matching ``--provider`` flag; routing them through the default ``ollama``
    provider sends a request to ollama.com asking for a model literally named
    ``openai-codex/gpt-5.5`` and returns 404. Bare model ids (e.g.
    ``kimi-k2.7-code:cloud``) fall through to ``default_provider`` so Ciao's own
    ollama models.json registration keeps working. Openrouter ids that
    themselves contain a slash (``openrouter/anthropic/claude-3.5-sonnet``)
    split only on the first slash, preserving the sub-path for Pi.

    ``local_models`` (models installed on the local Ollama daemon) dispatch
    under the dedicated ``ollama-local`` models.json provider so they reach
    the daemon even when the default provider points at ollama.com.
    """
    if "/" in model:
        prefix, rest = model.split("/", 1)
        if prefix in _BUILT_IN_PROVIDERS:
            return prefix, rest
    if model in local_models:
        return _LOCAL_PROVIDER, model
    return default_provider, model

from ciao.models import (
    AgentRequest,
    AssistantTextDelta,
    PermissionRequestEvent,
    ResultEvent,
    StreamEvent,
    SystemStatusEvent,
    ThinkingEvent,
    ToolUseEvent,
)
from ciao.providers.base import ActiveHandle, BaseProvider

logger = logging.getLogger(__name__)

# asyncio's StreamReader defaults to a 64 KiB readline buffer. Pi tool-result
# events embed file contents and bash output verbatim, so a single ``read``
# or ``bash`` line can easily exceed that and crash readline() with
# ``LimitOverrunError: Separator is found, but chunk is longer than limit``.
# 10 MiB matches the upper bound on a single Pi turn we've observed.
_PI_STREAM_LIMIT = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class PiSettings:
    """Resolved Pi provider configuration."""

    models: tuple[str, ...] = ()
    provider: str = "ollama"
    base_url: str = "http://localhost:11434"
    default_model: str = ""
    # Models served by the local Ollama daemon (auto-discovered at startup,
    # see ciao.main). Dispatched under the dedicated ``ollama-local``
    # models.json provider so they can coexist with the cloud provider.
    local_models: tuple[str, ...] = ()


@dataclass
class PiHandle(ActiveHandle):
    """Handle that aborts Pi's current turn without killing the process.

    Pi RPC honours ``{"type":"abort"}`` on stdin: it stops the current agent
    loop but keeps the subprocess alive for the next prompt. The base
    ``ActiveHandle`` is a no-op, which is why the UI Stop button used to do
    nothing for Pi turns.
    """

    process: asyncio.subprocess.Process | None = None

    async def stop(self) -> None:
        proc = self.process
        if proc is None or proc.returncode is not None or proc.stdin is None:
            return
        try:
            proc.stdin.write(b'{"type":"abort"}\n')
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, ProcessLookupError):
            logger.debug("Pi abort write failed; process likely already dead")


def is_pi_model(model: str, settings: PiSettings) -> bool:
    """True when ``model`` should be routed through Pi."""
    if not model or not settings.models:
        return False
    return model in settings.models


def session_dir_root() -> Path:
    """Root directory Pi writes per-chat session logs under.

    Mirrors the resolution in ``PiProvider`` and the web route helper:
    ``CIAO_PI_SESSION_DIR`` override, else ``~/.pi/agent/sessions``.
    """
    override = os.environ.get("CIAO_PI_SESSION_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".pi" / "agent" / "sessions"


def delete_pi_session_dir(chat_id: str) -> bool:
    """Delete a chat's Pi session log directory (``<root>/<chat_id>``).

    The symmetric counterpart to ``delete_session`` for the Claude SDK:
    archiving a chat should reclaim its Pi logs the same way it reclaims
    the Claude JSONL, instead of letting ``~/.pi/agent/sessions`` grow
    forever. No-op when ``chat_id`` is empty or the directory is absent.
    Returns True when a directory was removed.
    """
    if not chat_id:
        return False
    chat_dir = session_dir_root() / chat_id
    if not chat_dir.is_dir():
        return False
    shutil.rmtree(chat_dir, ignore_errors=True)
    return True


def ensure_models_json(
    pi_settings: PiSettings,
    ollama_base_url: str,
    ollama_api_key: str,
    extra_models: tuple[str, ...] = (),
    *,
    local_models: tuple[str, ...] = (),
    local_url: str = "http://localhost:11434",
    path: Path | None = None,
) -> Path:
    """Write ``~/.pi/agent/models.json`` so ``--provider ollama`` resolves.

    The Pi 0.74+ fork (``@earendil-works/pi-coding-agent``) dropped the
    built-in ``ollama`` provider and now requires custom providers to be
    registered via models.json. We reconstruct the equivalent config from
    Ciao's own OllamaSettings + PiSettings: an Anthropic-Messages-compatible
    endpoint pointed at the same Ollama cloud URL, listing the union of
    ``CIAO_OLLAMA_MODELS`` and ``CIAO_PI_MODELS`` so every model Ciao
    surfaces under the Pi bucket can be resolved.

    We use ``api: "anthropic-messages"`` (not ``openai-completions``) because
    ollama.com's OpenAI-compat endpoint does not pass images through to some
    cloud models (kimi-k2.7-code:cloud returns "temporarily overloaded" instead of
    accepting the image), while its Anthropic-compat endpoint does. ``authHeader:
    true`` tells pi-ai to send ``Authorization: Bearer <apiKey>`` instead of
    the default ``x-api-key`` header (which ollama.com rejects as unauthorized).

    Local Ollama daemon use case keeps working: when the operator didn't set a
    cloud key, we fall back to the OpenAI-compat path because the daemon
    doesn't speak Anthropic.

    ``local_models`` (auto-discovered from the local Ollama daemon) get a
    second ``ollama-local`` provider entry — OpenAI-compat at
    ``local_url``/v1 — so local and cloud models can coexist:
    ``_resolve_provider_and_model`` dispatches them with
    ``--provider ollama-local`` while everything else keeps the default
    provider.

    Idempotent. Returns the path written.
    """
    if path is None:
        path = Path.home() / ".pi" / "agent" / "models.json"
    providers: dict[str, Any] = {}
    if local_models:
        local_base = local_url.rstrip("/")
        if not local_base.endswith("/v1"):
            local_base = local_base + "/v1"
        providers[_LOCAL_PROVIDER] = {
            "baseUrl": local_base,
            "api": "openai-completions",
            "apiKey": "ollama",
            "compat": {
                "supportsDeveloperRole": False,
                "supportsReasoningEffort": False,
            },
            "models": [{"id": m} for m in local_models],
        }
    # Built-in providers don't need a models.json entry because Pi resolves them natively.
    if pi_settings.provider in _BUILT_IN_PROVIDERS:
        if not providers:
            return path
        config = {"providers": providers}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        logger.info(
            "Wrote Pi models.json: %d local models under provider %s",
            len(local_models), _LOCAL_PROVIDER,
        )
        return path
    models = list(dict.fromkeys([*pi_settings.models, *extra_models]))
    base = ollama_base_url.rstrip("/")
    cloud = "ollama.com" in base
    provider_cfg: dict[str, Any]
    if cloud:
        # Strip a trailing /v1 — Anthropic SDK appends /v1/messages itself.
        if base.endswith("/v1"):
            base = base[:-3]
        provider_cfg = {
            "baseUrl": base,
            "api": "anthropic-messages",
            "apiKey": ollama_api_key or "ollama",
            "authHeader": True,
            "compat": {
                # ollama.com's Anthropic endpoint doesn't accept pi's per-tool
                # eager_input_streaming field — set false so pi falls back to
                # the legacy fine-grained-tool-streaming beta header instead.
                "supportsEagerToolInputStreaming": False,
            },
            "models": [{"id": m, "input": ["text", "image"]} for m in models],
        }
    else:
        # Local Ollama daemon: OpenAI-compat at /v1, no Anthropic endpoint.
        if not base.endswith("/v1"):
            base = base + "/v1"
        provider_cfg = {
            "baseUrl": base,
            "api": "openai-completions",
            "apiKey": ollama_api_key or "ollama",
            "compat": {
                "supportsDeveloperRole": False,
                "supportsReasoningEffort": False,
            },
            "models": [{"id": m} for m in models],
        }
    providers[pi_settings.provider] = provider_cfg
    config = {"providers": providers}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    logger.info(
        "Wrote Pi models.json: %d models under provider %s via %s (+%d local)",
        len(models), pi_settings.provider, provider_cfg["api"], len(local_models),
    )
    return path


async def run_pi_oneshot(
    prompt: str,
    *,
    system_prompt: str,
    model: str,
    settings: PiSettings,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout_s: float = 60.0,
) -> str:
    """Run a one-shot Pi prompt and return the assistant's text.

    Spawns ``pi --mode rpc --provider <settings.provider> --model <model>
    --no-session``, sends a single prompt envelope, reads stdout events
    until ``agent_end``, then terminates. No chat session is created and
    no permission gating is wired — callers must pass prompts the model
    can answer directly.

    Pi RPC has no first-class system-prompt flag, so the system prompt
    is inlined under an ``Instructions:`` preamble. Empty string is a
    valid return value; the caller decides how to handle it.
    """
    provider, model_id = _resolve_provider_and_model(
        model, settings.provider, settings.local_models
    )
    args = [
        "pi",
        "--mode", "rpc",
        "--provider", provider,
        "--model", model_id,
        "--no-session",
    ]
    full_env = {**os.environ, **(env or {})}
    composed = f"Instructions:\n{system_prompt}\n\n{prompt}"

    logger.info("Spawning Pi one-shot: %s", " ".join(args))
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd is not None else None,
        env=full_env,
        limit=_PI_STREAM_LIMIT,
    )

    async def _drive() -> str:
        process.stdin.write((json.dumps({"type": "prompt", "message": composed}) + "\n").encode())
        await process.stdin.drain()
        parts: list[str] = []
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            try:
                event = json.loads(line.decode())
            except json.JSONDecodeError:
                logger.debug("Pi one-shot stdout unparsable: %s", line.decode().rstrip())
                continue
            etype = event.get("type")
            if etype == "message_update":
                ame = event.get("assistantMessageEvent", {})
                if ame.get("type") == "text_delta":
                    parts.append(ame.get("delta", ""))
            elif etype == "agent_end":
                break
        return "".join(parts).strip()

    try:
        text = await asyncio.wait_for(_drive(), timeout=timeout_s)
    except TimeoutError:
        logger.warning("Pi one-shot timed out after %.1fs; killing process", timeout_s)
        process.kill()
        # Drain stdout/stderr so the child doesn't stay zombie. Without
        # this the asgi shutdown can wedge on the open pipe fd, mirroring
        # the leak seen with Claude SDK subprocess transports.
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except TimeoutError:
            pass
        raise
    finally:
        if process.returncode is None:
            try:
                process.stdin.write(b'{"type":"abort"}\n')
                await process.stdin.drain()
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except (TimeoutError, BrokenPipeError, ProcessLookupError):
                pass
            if process.returncode is None:
                process.kill()
                # Bounded drain: collect any pending stdout/stderr in the
                # background so we don't block on a full pipe buffer.
                async def _drain() -> None:
                    try:
                        while True:
                            chunk = await process.stdout.read(4096)
                            if not chunk:
                                break
                    except Exception:
                        pass
                    try:
                        while True:
                            chunk = await process.stderr.read(4096)
                            if not chunk:
                                break
                    except Exception:
                        pass
                try:
                    await asyncio.wait_for(_drain(), timeout=1.0)
                except TimeoutError:
                    pass
                await process.wait()
    return text


def _convert_pi_event(pi_event: dict[str, Any], provider: PiProvider) -> StreamEvent | None:
    """Map a single Pi RPC event to a Ciao StreamEvent."""
    etype = pi_event.get("type")

    if etype == "message_update":
        ame = pi_event.get("assistantMessageEvent", {})
        ame_type = ame.get("type")
        if ame_type == "text_delta":
            return AssistantTextDelta(type="assistant_text_delta", text=ame.get("delta", ""))
        if ame_type == "thinking_delta":
            return ThinkingEvent(type="thinking", text=ame.get("delta", ""))

    if etype == "tool_execution_start":
        args = pi_event.get("args", {})
        tool_name = pi_event.get("toolName", "")
        input_summary = ""
        if isinstance(args, dict):
            # File-touch tools: extract just the path so downstream
            # file_touch detection (chat_broker.extract_file_touch) works
            # cleanly, matching Claude's _summarize_tool_input behaviour.
            if tool_name in ("Write", "write", "Edit", "edit", "MultiEdit"):
                for key in ("path", "file_path"):
                    value = args.get(key)
                    if isinstance(value, str):
                        input_summary = value
                        break
            elif tool_name in ("NotebookEdit", "notebook_edit"):
                value = args.get("notebook_path")
                if isinstance(value, str):
                    input_summary = value
            if not input_summary:
                for key, value in args.items():
                    if isinstance(value, str):
                        input_summary = f"{key}: {value}"
                        break
        return ToolUseEvent(
            type="tool_use",
            tool_name=tool_name,
            tool_input=input_summary,
            tool_use_id=pi_event.get("toolCallId"),
        )

    if etype == "agent_end":
        messages = pi_event.get("messages", [])
        return ResultEvent(type="result", result="", is_error=False)

    if etype == "extension_ui_request":
        method = pi_event.get("method", "")
        req_id = pi_event.get("id", "")
        if method in ("confirm", "select", "input"):
            title = pi_event.get("title", "")
            message = pi_event.get("message", "")
            if method == "select":
                options = pi_event.get("options", [])
                opts_str = ", ".join(options) if options else ""
                msg = f"{title}: {opts_str}" if title else opts_str
            else:
                msg = f"{title}: {message}" if title and message else (title or message or "Pi requests input")
            return PermissionRequestEvent(
                type="permission_request",
                message=msg,
                tool_name=f"pi_{method}",
                tool_input=msg,
                request_id=req_id,
            )

    if etype in ("auto_retry_start", "auto_retry_end", "compaction_start", "compaction_end"):
        return SystemStatusEvent(type="system_status", status=etype)

    # Events we intentionally ignore (queue_update, extension_error, etc.)
    return None


class PiProvider(BaseProvider):
    """Provider that speaks to Pi via its RPC mode over stdin/stdout."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        config: PiSettings | None = None,
    ) -> None:
        super().__init__(workspace_root, config=config)
        self._settings: PiSettings = config or PiSettings()
        self._process: asyncio.subprocess.Process | None = None
        # Pi writes one JSONL per session under ``~/.pi/agent/sessions/
        # <chat_id>/``. Archiving or deleting a chat reaps that directory
        # via ``delete_pi_session_dir`` so the tree doesn't grow forever.
        # ``CIAO_PI_SESSION_DIR`` overrides the root for tests.
        override = os.environ.get("CIAO_PI_SESSION_DIR", "").strip()
        if override:
            self._session_dir: Path = Path(override).expanduser()
        else:
            self._session_dir = Path.home() / ".pi" / "agent" / "sessions"
        self._active_handle: ActiveHandle | None = None

    def _session_file_for_chat(self, chat_id: str) -> Path | None:
        """Return the most recent session file for a chat, if any."""
        chat_dir = self._session_dir / chat_id
        if not chat_dir.exists():
            return None
        files = sorted(chat_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        return files[0] if files else None

    def _build_pi_args(self, model: str, chat_id: str, thinking_level: str = "") -> list[str]:
        """Build the ``pi`` CLI argument list."""
        provider, model_id = _resolve_provider_and_model(
            model, self._settings.provider, self._settings.local_models
        )
        args = [
            "pi",
            "--mode", "rpc",
            "--provider", provider,
            "--model", model_id,
        ]
        if thinking_level:
            args.extend(["--thinking", thinking_level])
        chat_session_dir = self._session_dir / chat_id
        chat_session_dir.mkdir(parents=True, exist_ok=True)
        args.extend(["--session-dir", str(chat_session_dir)])

        session_file = self._session_file_for_chat(chat_id)
        if session_file:
            args.extend(["--session", str(session_file)])
        # No flag for the first turn — Pi creates a fresh session under
        # ``--session-dir`` so the transcript persists and the messages
        # endpoint can replay it on reload. ``--no-session`` would make
        # the turn ephemeral and leave reloads blank.

        return args

    def _build_prompt_command(self, request: AgentRequest) -> dict[str, Any]:
        """Build the JSON prompt command for Pi RPC."""
        cmd: dict[str, Any] = {"type": "prompt", "message": request.prompt}
        if request.images:
            cmd["images"] = [
                {
                    "type": "image",
                    "data": base64.b64encode(img.path.read_bytes()).decode("ascii"),
                    "mimeType": img.mime_type,
                }
                for img in request.images
            ]
        return cmd

    async def run_streaming(
        self,
        request: AgentRequest,
        register_handle: Callable[[ActiveHandle | None], None],
    ) -> AsyncGenerator[StreamEvent, None]:
        """Spawn Pi, send the prompt, and yield mapped events."""
        chat_id = request.extra_env.get("CIAO_CHAT_ID", "unknown")
        args = self._build_pi_args(request.model, chat_id, request.thinking_level)
        env = {**os.environ, **(request.extra_env or {})}

        logger.info("Spawning Pi: %s", " ".join(args))
        self._process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.workspace_root),
            env=env,
            limit=_PI_STREAM_LIMIT,
        )

        handle = PiHandle(process=self._process)
        self._active_handle = handle
        register_handle(handle)

        prompt_cmd = self._build_prompt_command(request)
        self._process.stdin.write((json.dumps(prompt_cmd) + "\n").encode())
        await self._process.stdin.drain()

        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break
                try:
                    pi_event = json.loads(line.decode())
                except json.JSONDecodeError:
                    logger.warning("Pi stdout unparsable: %s", line.decode().rstrip())
                    continue
                ciao_event = _convert_pi_event(pi_event, self)
                if ciao_event is not None:
                    yield ciao_event
                # Pi RPC keeps stdout open after ``agent_end`` (idle, waiting
                # for the next prompt). If we stay in readline() the async
                # generator never returns, the drive loop in ProjectChatManager
                # never reaches its queue-drain step, and queued follow-ups
                # are stranded. Break here so stream_chat() completes; the
                # next turn re-spawns Pi with ``--session`` resuming from the
                # JSONL we just wrote.
                if pi_event.get("type") == "agent_end":
                    break
        finally:
            register_handle(None)
            self._active_handle = None
            await self._terminate_process()

    async def steer(self, request: AgentRequest) -> bool:
        """Send a steering message to the active Pi process."""
        if self._process is None or self._process.returncode is not None:
            return False
        cmd = {"type": "steer", "message": request.prompt}
        try:
            self._process.stdin.write((json.dumps(cmd) + "\n").encode())
            await self._process.stdin.drain()
            return True
        except (BrokenPipeError, ProcessLookupError):
            return False

    def send_permission_response(self, request_id: str, approved: bool, value: str | None = None) -> bool:
        """Send an extension_ui_response back to Pi.

        Synchronous because callers (e.g. ``ProjectChatManager.respond_permission``)
        may be in a non-async context.
        """
        if self._process is None or self._process.returncode is not None:
            return False
        resp: dict[str, Any] = {"type": "extension_ui_response", "id": request_id}
        if value is not None:
            resp["value"] = value
        else:
            resp["confirmed"] = approved
        try:
            self._process.stdin.write((json.dumps(resp) + "\n").encode())
            return True
        except (BrokenPipeError, ProcessLookupError):
            return False

    async def _terminate_process(self) -> None:
        """Close stdin and wait for Pi to exit; force-kill on timeout.

        Called from ``run_streaming``'s finally so each Ciao "turn" maps to a
        fresh Pi subprocess. The next turn re-spawns Pi with ``--session``
        resuming from the JSONL transcript, which keeps multi-turn context
        intact across spawns at the cost of a few hundred ms per turn.
        """
        proc = self._process
        if proc is None or proc.returncode is not None:
            self._process = None
            return
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except (TimeoutError, ProcessLookupError, BrokenPipeError):
            pass
        if proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
        self._process = None

    async def disconnect(self) -> None:
        """Send abort and terminate the Pi process."""
        await self._terminate_process()
