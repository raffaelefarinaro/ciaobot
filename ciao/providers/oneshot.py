"""One-shot model calls via the Claude Agent SDK.

The upstream (Anthropic / Ollama / OpenRouter) is chosen by the caller
through the ``env`` dict -- the same ``ANTHROPIC_BASE_URL`` /
``ANTHROPIC_AUTH_TOKEN`` env injection used for chats -- so this helper is
backend-agnostic and needs no provider switch of its own.

Calls are intentionally bare: custom system prompt, no filesystem
settings/skills, no tools, no MCP discovery. Titles, insights, critique,
and similar routines never need the agent tool surface.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

logger = logging.getLogger(__name__)


class OneShotError(RuntimeError):
    """A one-shot model call failed with whatever detail was available.

    ``detail`` carries the composed upstream error (status / subtype /
    stop_reason / body) so callers -- the titler, ``job_runs`` -- can
    surface it instead of a bare "One-shot query failed". ``transient``
    marks failures worth retrying: empty-body / gateway errors from an
    Anthropic-compatible backend (e.g. Ollama Cloud intermittently
    returns contentless ``is_error`` results) succeed on a second try,
    whereas auth / subscription / bad-model errors will fail again.
    """

    def __init__(
        self, detail: str, *, status: int | None = None, transient: bool = False
    ) -> None:
        super().__init__(detail or "one-shot query failed")
        self.detail = detail or "one-shot query failed"
        self.status = status
        self.transient = transient


# HTTP statuses / message markers that mean "this will fail again" -- do not
# retry these. Everything else (empty body, 5xx, unexpected EOF, timeouts on
# the upstream copy) is treated as transient.
_NON_TRANSIENT_STATUSES = frozenset({400, 401, 402, 403, 404, 405, 422})
_NON_TRANSIENT_MARKERS = (
    "authentication",
    "unauthorized",
    "invalid api key",
    "invalid_api_key",
    "x-api-key",
    "subscription",
    "credit balance",
    "insufficient",
    "quota",
    "permission",
    "forbidden",
    "not found",
    "issue with the selected model",
    "invalid model",
    "does not exist",
    "unsupported",
)


def _result_error_detail(msg: ResultMessage) -> tuple[str, int | None]:
    """Compose a human-readable detail string from an error ResultMessage.

    Ollama Cloud's known failure mode is a contentless ``is_error`` result
    (empty body, no status), so the parts are all best-effort and we fall
    back to an explicit "empty error result" marker when nothing is set.
    """
    status = getattr(msg, "api_error_status", None)
    parts: list[str] = []
    subtype = getattr(msg, "subtype", None)
    if subtype and subtype not in ("success", ""):
        parts.append(f"subtype={subtype}")
    if status:
        parts.append(f"status={status}")
    stop_reason = getattr(msg, "stop_reason", None)
    if stop_reason:
        parts.append(f"stop_reason={stop_reason}")
    result = (getattr(msg, "result", None) or "").strip()
    if result:
        parts.append(result)
    for err in getattr(msg, "errors", None) or []:
        text = str(err).strip()
        if text and text not in parts:
            parts.append(text)
    detail = "; ".join(parts) if parts else "empty error result (no status or body)"
    return detail, status


def _is_transient(detail: str, status: int | None) -> bool:
    if status is not None and status in _NON_TRANSIENT_STATUSES:
        return False
    low = detail.lower()
    if any(marker in low for marker in _NON_TRANSIENT_MARKERS):
        return False
    return True


async def _run_claude_oneshot(
    prompt: str,
    *,
    system_prompt: str,
    model: str,
    env: dict[str, str] | None,
) -> str:
    # Titles / insights / critique never need agent tooling. Leaving
    # ``tools`` unset keeps the CLI's default Claude Code tool schemas in
    # the prompt (Bash/Read/Edit/…), which burns tokens for no benefit.
    # ``tools=[]`` maps to ``--tools ""``; ``strict_mcp_config`` with the
    # empty default ``mcp_servers`` also blocks project/user MCP discovery.
    # ``setting_sources=[]`` / ``skills=[]`` already skip CLAUDE.md and
    # skill listings (the useful half of Claude Code ``--bare``).
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=system_prompt,
        setting_sources=[],
        skills=[],
        tools=[],
        strict_mcp_config=True,
        # ``max_turns=2`` (not 1) absorbs a stray ``stop_reason=tool_use`` the
        # model occasionally emits under ``tools=[]`` — a known SDK quirk
        # where the model starts to call a tool, gets nothing back, and the
        # SDK aborts the turn. With 2 turns the model recovers and returns
        # the actual text on the next iteration. Title / critique callers
        # never loop, so this is one extra API call at most.
        max_turns=2,
        env=env or {},
    )
    parts: list[str] = []
    agen = query(prompt=prompt, options=options)
    try:
        async for msg in agen:
            if isinstance(msg, AssistantMessage):
                if getattr(msg, "error", None):
                    error_text = "".join(
                        block.text for block in msg.content
                        if isinstance(block, TextBlock)
                    ).strip()
                    detail = error_text or f"assistant error: {msg.error}"
                    raise OneShotError(detail, transient=_is_transient(detail, None))
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
            elif isinstance(msg, ResultMessage):
                if msg.is_error:
                    detail, status = _result_error_detail(msg)
                    raise OneShotError(
                        detail, status=status,
                        transient=_is_transient(detail, status),
                    )
    finally:
        # Close the SDK generator deterministically. When the loop
        # raises (model error, subprocess exit 1) or wait_for cancels
        # us on timeout, the generator is otherwise left for GC to
        # close in a fire-and-forget task; the SDK's teardown then
        # re-surfaces the ProcessError as a "Task exception was never
        # retrieved" log entry. aclose() is a no-op on an exhausted
        # generator, so the normal path is unaffected.
        try:
            await agen.aclose()
        except Exception:  # noqa: BLE001
            logger.debug("oneshot query generator cleanup raised; already handled")
    return "".join(parts).strip()


async def _run_codex_oneshot(
    prompt: str,
    *,
    system_prompt: str,
    model: str,
    env: dict[str, str] | None,
    cwd: Path | None,
) -> str:
    from ciao.models import AgentRequest, ResultEvent
    from ciao.providers.codex import CodexProvider

    codex = CodexProvider(
        (cwd or Path.cwd()).resolve(),
        developer_instructions=system_prompt,
        ephemeral=True,
    )
    try:
        async for event in codex.run_streaming(
            AgentRequest(
                prompt=prompt,
                model=model,
                mode="plan",
                provider="codex",
                extra_env=env or {},
            ),
            lambda _handle: None,
        ):
            if isinstance(event, ResultEvent):
                if event.is_error:
                    detail = (event.result or "").strip() or "codex one-shot failed"
                    # Codex surfaces its own retriable errors internally; treat
                    # a returned error as terminal here so we don't double-retry.
                    raise OneShotError(detail, transient=False)
                return event.result
        return ""
    finally:
        await codex.disconnect()


async def run_oneshot(
    prompt: str,
    *,
    system_prompt: str,
    model: str,
    env: dict[str, str] | None = None,
    timeout_s: float = 120.0,
    provider: str = "claude",
    cwd: Path | None = None,
    max_retries: int = 1,
    retry_backoff_s: float = 0.5,
) -> str:
    """Run a single-turn model call and return the assistant's text.

    Empty string is a valid return (the model had nothing to say); the
    caller decides how to handle it. ``timeout_s`` wraps each attempt via
    :func:`asyncio.wait_for`.

    On a transient failure (an empty-body / ``is_error`` result, the known
    Ollama Cloud gateway flake) the call is retried up to ``max_retries``
    times with exponential backoff; non-transient failures (auth /
    subscription / bad-model) and timeouts are raised immediately. On
    failure an :class:`OneShotError` carrying the upstream ``detail`` is
    raised so callers can log the real cause instead of a generic string.
    """
    # Chats can run with Claude Code's fast-mode suffix ("[1m]"), but the
    # one-shot API path rejects annotated model ids ("There's an issue with
    # the selected model (claude-opus-4-8[1m])"). Background calls never
    # need fast mode; use the base model.
    if model.endswith("[1m]"):
        model = model[: -len("[1m]")]

    if provider == "codex":
        async def _attempt() -> str:
            return await _run_codex_oneshot(
                prompt,
                system_prompt=system_prompt,
                model=model,
                env=env,
                cwd=cwd,
            )
    elif provider == "claude":
        async def _attempt() -> str:
            # Disable Claude Code's auto memory to avoid double memory layers
            merged_env = dict(env or {})
            merged_env.setdefault("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")
            return await _run_claude_oneshot(
                prompt,
                system_prompt=system_prompt,
                model=model,
                env=merged_env,
            )
    else:
        raise ValueError(f"Unknown one-shot provider '{provider}'")

    attempts = max(0, int(max_retries)) + 1
    last_exc: OneShotError | None = None
    for attempt_no in range(attempts):
        try:
            return await asyncio.wait_for(_attempt(), timeout=timeout_s)
        except OneShotError as exc:
            last_exc = exc
            if not exc.transient or attempt_no >= attempts - 1:
                raise
            delay = retry_backoff_s * (2 ** attempt_no)
            logger.info(
                "one-shot %s (model=%s) attempt %d/%d failed transiently (%s); "
                "retrying in %.1fs",
                provider, model, attempt_no + 1, attempts, exc.detail, delay,
            )
            await asyncio.sleep(delay)
    # Loop only exits via return or raise; this satisfies type-checkers.
    assert last_exc is not None
    raise last_exc
