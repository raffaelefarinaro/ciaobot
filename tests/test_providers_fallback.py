"""Tests for provider-level SDK upgrades: fallback model + runtime context."""

from __future__ import annotations

from types import SimpleNamespace

from ciao.models import AgentRequest
from ciao.model_tiers import (
    is_capability_error,
    next_tier_for_failure,
    tier_order,
)
from ciao.providers.base import build_runtime_context
from ciao.providers.claude import _fallback_model_for


def test_fallback_model_downgrades_tier() -> None:
    assert _fallback_model_for("opus") == "sonnet"
    assert _fallback_model_for("claude-opus-4-7") == "sonnet"
    assert _fallback_model_for("sonnet") == "haiku"
    assert _fallback_model_for("claude-sonnet-4-6") == "haiku"


def test_fallback_model_returns_none_for_cheapest_tier() -> None:
    # Haiku has nowhere cheaper to go, so we must not fall back to self.
    assert _fallback_model_for("haiku") is None
    assert _fallback_model_for("claude-haiku-4-5") is None
    assert _fallback_model_for("") is None


def test_runtime_context_includes_today(monkeypatch) -> None:
    monkeypatch.delenv("CIAO_WORKSPACE", raising=False)
    # Set in every agent process Ciaobot spawns, so a suite run from inside a
    # chat inherited it and the "no env" case was never actually reached.
    monkeypatch.delenv("CIAO_ACTIVE_WORKSPACE", raising=False)
    monkeypatch.delenv("CIAO_ACTIVE_PROJECT", raising=False)
    monkeypatch.delenv("CIAO_CHAT_ID", raising=False)
    monkeypatch.delenv("GWS_PROFILE", raising=False)
    request = AgentRequest(prompt="test", model="opus", mode="bypass")
    ctx = build_runtime_context(request)
    assert ctx.startswith("today=")
    assert "workspace=" not in ctx  # no env, so no workspace line


def test_runtime_context_reads_env(monkeypatch) -> None:
    monkeypatch.setenv("CIAO_WORKSPACE", "/repo")
    monkeypatch.setenv("CIAO_ACTIVE_WORKSPACE", "work")
    monkeypatch.setenv("GWS_PROFILE", "work")
    monkeypatch.setenv("CIAO_ACTIVE_PROJECT", "2026-q2-foo")
    request = AgentRequest(prompt="test", model="opus", mode="bypass")
    ctx = build_runtime_context(request)
    assert "workspace=work" in ctx
    assert "workspace=/repo" not in ctx
    assert "active_project=2026-q2-foo" in ctx
    # GWS_PROFILE matches workspace, so it should not be duplicated.
    assert ctx.count("gws_profile") == 0


# ── Auto tier-fallback (ciao/model_tiers.py) ────────────────────────────


def test_tier_order_is_cheapest_to_most_capable() -> None:
    # The ladder is walked via index deltas: -1 (down/cheaper) and +1
    # (up/more capable). Index 0 is the cheapest, index 3 is the most
    # capable.
    assert tier_order() == ("haiku", "sonnet", "opus", "fable")


def test_is_capability_error_matches_image_input() -> None:
    # The exact error text from the screenshot that triggered this work.
    assert is_capability_error(
        "API Error: 400 this model does not support image input (ref: 365b601b)"
    )


def test_is_capability_error_matches_tool_use_and_context() -> None:
    assert is_capability_error("this model does not support tool use")
    assert is_capability_error("unsupported capability: function_calling")
    assert is_capability_error("context length exceeded")
    assert is_capability_error("max context length is 200000 tokens")


def test_is_capability_error_rejects_rate_limit_and_auth() -> None:
    # Rate limits, auth, content filters, and 5xx are NOT capability
    # errors. The auto-retry is narrow by design; these need operator
    # attention, not silent retry against the next tier.
    assert not is_capability_error("API Error: 429 Rate Limit Exceeded")
    assert not is_capability_error("unauthorized: invalid api key")
    assert not is_capability_error("content policy violation")
    assert not is_capability_error("internal server error")
    assert not is_capability_error("")


def test_next_tier_walks_down_then_up() -> None:
    # Tier aliases are provider-agnostic, so the ladder needs no config: the
    # provider running the chat resolves the returned alias itself.
    assert next_tier_for_failure("fable") == "opus"
    assert next_tier_for_failure("opus") == "sonnet"
    assert next_tier_for_failure("sonnet") == "haiku"
    # haiku sits at the bottom, so escalating is the only direction left.
    assert next_tier_for_failure("haiku") == "sonnet"


def test_next_tier_returns_none_for_a_pinned_concrete_model() -> None:
    """A chat pinned to a concrete id opted into that model.

    Swapping it for a neighbouring tier would broaden scope the user declined,
    so the ladder only applies to bare tier aliases.
    """
    assert next_tier_for_failure("claude-opus-4-8") is None
    assert next_tier_for_failure("anthropic/claude-sonnet-4-6") is None
    assert next_tier_for_failure("some-local-model") is None
    assert next_tier_for_failure("") is None


def test_next_tier_is_case_and_whitespace_insensitive() -> None:
    assert next_tier_for_failure("  Opus  ") == "sonnet"
