"""One-shot model calls via the Claude Agent SDK.

Replaces the former ``run_pi_oneshot`` Pi-subprocess path. The upstream
(Anthropic / Ollama / OpenRouter) is chosen by the caller through the
``env`` dict -- the same ``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_AUTH_TOKEN``
env injection used for chats -- so this helper is backend-agnostic and
needs no provider switch of its own.
"""

from __future__ import annotations

import asyncio
import logging

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
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
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
        return "".join(parts).strip()

    return await asyncio.wait_for(_collect(), timeout=timeout_s)
