"""One-shot model calls via the Claude Agent SDK.

The upstream (Anthropic / Ollama / OpenRouter) is chosen by the caller
through the ``env`` dict -- the same ``ANTHROPIC_BASE_URL`` /
``ANTHROPIC_AUTH_TOKEN`` env injection used for chats -- so this helper is
backend-agnostic and needs no provider switch of its own.
"""

from __future__ import annotations

import asyncio
import logging

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

logger = logging.getLogger(__name__)


async def run_oneshot(
    prompt: str,
    *,
    system_prompt: str,
    model: str,
    env: dict[str, str] | None = None,
    timeout_s: float = 120.0,
) -> str:
    """Run a single-turn model call and return the assistant's text.

    Empty string is a valid return (the model had nothing to say); the
    caller decides how to handle it. ``timeout_s`` wraps the whole call
    via :func:`asyncio.wait_for`.
    """
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=system_prompt,
        setting_sources=[],
        skills=[],
        max_turns=1,
        env=env or {},
    )

    async def _collect() -> str:
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
                        raise RuntimeError(error_text or f"Assistant error: {msg.error}")
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            parts.append(block.text)
                elif isinstance(msg, ResultMessage):
                    if msg.is_error:
                        raise RuntimeError(msg.result or "One-shot query failed")
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

    return await asyncio.wait_for(_collect(), timeout=timeout_s)
