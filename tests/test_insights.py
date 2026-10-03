"""What is left of ``ciao.insights``: model resolution for the memory pass and
the context-overflow classifier the schedule attention check shares."""

from __future__ import annotations

import asyncio

from ciao import insights


def test_insights_timeout_is_generous() -> None:
    # The old flat 120s was below the 214-253s this path really takes.
    assert insights._DEFAULT_TIMEOUT_S > 200


def test_context_overflow_is_distinguished_from_a_transient_timeout() -> None:
    overflow = Exception(
        "API Error 400 Message too long: 262183 > 125952 maximum context length"
    )
    assert insights.is_context_overflow(overflow)
    assert insights.is_context_overflow(Exception("context_length_exceeded"))
    # Transient failures must stay retryable.
    assert not insights.is_context_overflow(asyncio.TimeoutError())
    assert not insights.is_context_overflow(Exception("429 rate limit"))


def test_routed_provider_prefix_selects_that_provider() -> None:
    assert insights._resolve_insights_call(None, "opencode:gpt-5") == (
        "gpt-5",
        "opencode",
        None,
    )
    assert insights._resolve_insights_call(None, "sonnet") == ("sonnet", "claude", None)
