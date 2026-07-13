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


def test_next_tier_walks_down_then_up_for_anthropic() -> None:
    # Bare tier aliases on the Anthropic-direct path walk the ladder
    # in the order the user specified: fable -> opus -> sonnet -> haiku.
    cfg = SimpleNamespace(ollama=None, openrouter=None)
    assert next_tier_for_failure("fable", cfg) == "opus"
    assert next_tier_for_failure("opus", cfg) == "sonnet"
    assert next_tier_for_failure("sonnet", cfg) == "haiku"
    # haiku at the bottom of the ladder has only one direction left
    # (escalate to sonnet).
    assert next_tier_for_failure("haiku", cfg) == "sonnet"


def test_next_tier_resolves_ollama_configured_ids() -> None:
    # Ollama chats use concrete model ids, not bare aliases. The helper
    # has to look up the failing model against the configured
    # OllamaSettings.{tier}_model to find the right slot. With the
    # user's default (fable=kimi5.2, opus=minimax-m3, sonnet=kimi-k2.7,
    # haiku=deepseek-v4-flash), kimi5.2 failing should retry on
    # minimax-m3 — exactly the screenshot scenario.
    cfg = SimpleNamespace(
        ollama=SimpleNamespace(
            haiku_model="deepseek-v4-flash:cloud",
            sonnet_model="kimi-k2.7-code:cloud",
            opus_model="minimax-m3:cloud",
            fable_model="kimi5.2:cloud",
        ),
        openrouter=None,
    )
    assert next_tier_for_failure("kimi5.2:cloud", cfg) == "minimax-m3:cloud"
    assert next_tier_for_failure("minimax-m3:cloud", cfg) == "kimi-k2.7-code:cloud"
    assert next_tier_for_failure("kimi-k2.7-code:cloud", cfg) == "deepseek-v4-flash:cloud"
    assert next_tier_for_failure("deepseek-v4-flash:cloud", cfg) == "kimi-k2.7-code:cloud"


def test_next_tier_resolves_openrouter_configured_ids() -> None:
    cfg = SimpleNamespace(
        ollama=None,
        openrouter=SimpleNamespace(
            haiku_model="anthropic/claude-haiku-latest",
            sonnet_model="anthropic/claude-sonnet-latest",
            opus_model="anthropic/claude-opus-latest",
            fable_model="anthropic/claude-fable-latest",
        ),
    )
    assert (
        next_tier_for_failure("anthropic/claude-fable-latest", cfg)
        == "anthropic/claude-opus-latest"
    )
    assert (
        next_tier_for_failure("anthropic/claude-opus-latest", cfg)
        == "anthropic/claude-sonnet-latest"
    )
    assert (
        next_tier_for_failure("anthropic/claude-haiku-latest", cfg)
        == "anthropic/claude-sonnet-latest"
    )


def test_next_tier_returns_none_for_unknown_model() -> None:
    # A model id that isn't on the configured ladder (e.g. an ad-hoc
    # Ollama model pulled for a one-off job) has no neighbor to fall
    # back to. Returning None lets the caller surface the original
    # error instead of guessing.
    cfg = SimpleNamespace(
        ollama=SimpleNamespace(
            haiku_model="deepseek-v4-flash:cloud",
            sonnet_model="kimi-k2.7-code:cloud",
            opus_model="minimax-m3:cloud",
            fable_model="kimi5.2:cloud",
        ),
        openrouter=None,
    )
    assert next_tier_for_failure("random-adhoc-model:cloud", cfg) is None
    assert next_tier_for_failure("", cfg) is None
    # claude-* ids (Anthropic-direct concrete ids) are not on the
    # tier ladder — they pass through the SDK's rate-limit fallback
    # path instead.
    assert next_tier_for_failure("claude-opus-4-8", cfg) is None


def test_next_tier_avoids_retrying_same_model() -> None:
    # If the operator pinned two adjacent slots to the same model id
    # (a degenerate but legal config), the helper must not propose
    # the same model as the retry target. The comparison is on the
    # resolved model id, not the slot.
    cfg = SimpleNamespace(
        ollama=SimpleNamespace(
            haiku_model="same:cloud",
            sonnet_model="same:cloud",  # operator mistake: same as haiku
            opus_model="opus-distinct:cloud",
            fable_model="fable-distinct:cloud",
        ),
        openrouter=None,
    )
    # fable-distinct -> should try opus-distinct, not same
    assert (
        next_tier_for_failure("fable-distinct:cloud", cfg) == "opus-distinct:cloud"
    )
    # opus-distinct -> should try sonnet, but sonnet == haiku, and
    # the comparison is "candidate != model", so sonnet's "same:cloud"
    # is fine to return for opus-distinct.
    assert next_tier_for_failure("opus-distinct:cloud", cfg) == "same:cloud"
