"""What is left of ``ciao.insights``: model resolution for the memory pass and
the deterministic-rejection classifiers the schedule attention check shares."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

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


def test_a_refused_model_is_told_apart_from_an_overflow_or_a_timeout() -> None:
    # opencode's free tier, verbatim from #1066. A one-shot call outside
    # opencode's own client is what draws this refusal.
    refused = Exception(
        "Error from provider (Console): OpenCode's free tier can only be "
        "used from within OpenCode"
    )
    assert insights.is_model_refused(refused)
    # The other two classifications stay distinct: a refusal is a config
    # problem, an overflow is a payload, a timeout is tail latency.
    assert not insights.is_model_refused(asyncio.TimeoutError())
    assert not insights.is_model_refused(Exception("429 rate limit"))
    assert not insights.is_model_refused(Exception("Message too long: 262183 > max"))
    assert not insights.is_context_overflow(refused)
    assert not insights.is_context_overflow(Exception("free tier"))


def test_the_two_classifications_are_not_mutually_exclusive() -> None:
    """A caller must order the checks; a refusal does not exclude an overflow.

    The predicates are text matches, so an arbitrary message can satisfy both.
    This is written down because the schedule classifier relies on it: it
    checks the overflow first, and an overflow is the one the operator can act
    on by trimming the payload.
    """
    both = Exception(
        "OpenCode's free tier can only be used from within OpenCode; "
        "context_length_exceeded"
    )
    assert insights.is_model_refused(both)
    assert insights.is_context_overflow(both)


def test_a_providers_insights_model_wins_over_its_default() -> None:
    defaults = {"claude": "sonnet", "opencode": "vendor/default"}
    config = SimpleNamespace(
        provider_insights_models={"opencode": "vendor/insights"},
        default_model_for_workspace=lambda workspace, provider: defaults[provider],
    )
    assert insights.resolve_insights_model(config, "work", "opencode") == "vendor/insights"
    # A provider without its own pick reads the session with its chat default.
    assert insights.resolve_insights_model(config, "work", "claude") == "sonnet"


def test_automatic_insights_inherits_the_source_model() -> None:
    config = SimpleNamespace(
        provider_insights_models={},
        default_model_for_workspace=lambda workspace, provider: "default-model",
    )
    for provider, model in (("claude", "opus"), ("opencode", "vendor/chat-model")):
        assert insights.resolve_insights_model(
            config, "work", provider, source_model=model
        ) == model
        config.provider_insights_models[provider] = "chosen-insights"
        assert insights.resolve_insights_model(
            config, "work", provider, source_model=model
        ) == "chosen-insights"


def _helper_config(overrides: dict[str, str] | None = None) -> SimpleNamespace:
    defaults = {"claude": "opus", "opencode": "vendor/default"}
    return SimpleNamespace(
        provider_insights_models=dict(overrides or {}),
        default_model_for_workspace=lambda workspace, provider: defaults[provider],
    )


def test_claude_helpers_resolve_to_haiku_whatever_the_source_chat_runs() -> None:
    config = _helper_config()
    assert insights.HELPER_MODEL_CLAUDE == "haiku"
    # Classifier path: source chat on Opus. Reconcile path: workspace default Opus.
    assert insights.resolve_helper_model(config, "work", "claude", source_model="opus") == "haiku"
    assert insights.resolve_helper_model(config, "work", "claude") == "haiku"


def test_a_settings_insights_override_wins_over_the_helper_tier() -> None:
    config = _helper_config({"claude": "sonnet", "opencode": "vendor/insights"})
    assert insights.resolve_helper_model(config, "work", "claude", source_model="opus") == "sonnet"
    assert insights.resolve_helper_model(config, "work", "opencode") == "vendor/insights"


def test_opencode_helpers_keep_the_current_resolution() -> None:
    config = _helper_config()
    assert insights.resolve_helper_model(
        config, "work", "opencode", source_model="vendor/chat"
    ) == "vendor/chat"
    assert insights.resolve_helper_model(config, "work", "opencode") == "vendor/default"


def test_the_memory_pass_and_doc_folds_keep_the_source_or_default_model() -> None:
    # resolve_insights_model is unchanged: the memory pass and doc folds still
    # inherit the source chat's model (or the workspace default) on Claude.
    config = _helper_config()
    assert insights.resolve_insights_model(config, "work", "claude", source_model="opus") == "opus"
    assert insights.resolve_insights_model(config, "work", "claude") == "opus"
