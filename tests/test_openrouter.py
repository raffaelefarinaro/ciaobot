"""Tests for OpenRouter backend routing via Anthropic-compatible env injection."""

from __future__ import annotations

from ciao.config import CiaoConfig
from ciao.providers.openrouter import (
    OpenRouterSettings,
    alias_model,
    is_openrouter_model,
    openrouter_env_for_model,
    routine_env_for_model,
)
from ciao.providers.routing import routing_env_for_model, routing_routine_env_for_model


def _settings(api_key: str = "sk-or") -> OpenRouterSettings:
    return OpenRouterSettings(api_key=api_key)


def test_is_openrouter_model_owner_slash_shape() -> None:
    s = _settings()
    assert is_openrouter_model("anthropic/claude-haiku-4.5", s)
    assert is_openrouter_model("openai/gpt-5", s)
    # bare aliases are Anthropic, not OpenRouter
    assert not is_openrouter_model("haiku", s)
    # Ollama tag ids are not OpenRouter
    assert not is_openrouter_model("kimi-k2.7-code:cloud", s)
    assert not is_openrouter_model("", s)


def test_env_injection_points_at_anthropic_compat_endpoint() -> None:
    env = openrouter_env_for_model("anthropic/claude-haiku-4.5", _settings())
    assert env["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-or"
    assert env["ANTHROPIC_API_KEY"] == ""


def test_env_includes_tier_remaps() -> None:
    """OpenRouter-routed CLIs get tier-alias + control-plane remaps so
    subagents and the auto-mode classifier resolve to OpenRouter-served
    ids instead of claude-* ids OpenRouter doesn't serve in that form."""
    env = openrouter_env_for_model("anthropic/claude-haiku-latest", _settings())
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "anthropic/claude-haiku-latest"
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "anthropic/claude-sonnet-latest"
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "anthropic/claude-opus-latest"
    assert env["ANTHROPIC_DEFAULT_FABLE_MODEL"] == "anthropic/claude-opus-latest"
    assert env["ANTHROPIC_SMALL_FAST_MODEL"] == "anthropic/claude-haiku-latest"
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "anthropic/claude-sonnet-latest"
    assert env["CLAUDE_CODE_AUTO_MODE_MODEL"] == "anthropic/claude-haiku-latest"
    assert env["CLAUDE_CODE_BG_CLASSIFIER_MODEL"] == "anthropic/claude-haiku-latest"


def test_env_empty_when_no_api_key() -> None:
    s = OpenRouterSettings(api_key="")
    assert openrouter_env_for_model("anthropic/claude-haiku-4.5", s) == {}
    assert not s.available


def test_env_empty_for_non_openrouter_model() -> None:
    assert openrouter_env_for_model("kimi-k2.7-code:cloud", _settings()) == {}
    assert openrouter_env_for_model("haiku", _settings()) == {}


def test_routine_env_not_gated_on_allowlist() -> None:
    # Tier default id routes even with an empty models allowlist.
    env = routine_env_for_model("anthropic/claude-sonnet-4.5", _settings())
    assert env["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"


def test_alias_resolves_to_tier_default() -> None:
    s = _settings()
    assert alias_model("haiku", s) == "anthropic/claude-haiku-latest"
    assert alias_model("sonnet", s) == "anthropic/claude-sonnet-latest"
    assert alias_model("opus", s) == "anthropic/claude-opus-latest"
    # unknown alias passes through unchanged
    assert alias_model("weird", s) == "weird"


def test_routing_helper_prefers_ollama_then_openrouter() -> None:
    config = CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t", "OPENROUTER_API_KEY": "sk-or"})
    # Ollama tag id -> ollama env (empty here since no ollama configured, but
    # must not accidentally route to OpenRouter)
    assert routing_env_for_model("kimi-k2.7-code:cloud", config) == {}
    # OpenRouter owner/model id -> OpenRouter env
    env = routing_env_for_model("anthropic/claude-haiku-4.5", config)
    assert env["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-or"


def test_routing_routine_helper_openrouter() -> None:
    config = CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t", "OPENROUTER_API_KEY": "sk-or"})
    env = routing_routine_env_for_model("anthropic/claude-haiku-4.5", config)
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-or"
    # bare alias stays on Anthropic (no overrides)
    assert routing_routine_env_for_model("haiku", config) == {}


def test_config_loads_openrouter_env() -> None:
    config = CiaoConfig.from_env({
        "PWA_AUTH_TOKEN": "t",
        "OPENROUTER_API_KEY": "sk-or-xyz",
        "CIAO_OPENROUTER_SONNET_MODEL": "openai/gpt-5",
        "CIAO_OPENROUTER_BASE_URL": "https://relay.example/api",
    })
    assert config.openrouter.available is True
    assert config.openrouter.api_key == "sk-or-xyz"
    assert config.openrouter.sonnet_model == "openai/gpt-5"
    assert config.openrouter.base_url == "https://relay.example/api"
    # haiku/opus keep shipped defaults
    assert config.openrouter.haiku_model == "anthropic/claude-haiku-latest"


def test_list_models_exposes_openrouter_backend() -> None:
    import asyncio
    from starlette.requests import Request
    from ciao.web.routes_api import list_models

    config = CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t", "OPENROUTER_API_KEY": "sk-or"})
    scope = {"type": "http", "method": "GET", "path": "/api/models", "headers": []}

    class _App:
        class state:
            pass

    _App.state.config = config
    scope["app"] = _App
    data = json.loads(asyncio.run(list_models(Request(scope))).body)
    assert data["backends"]["openrouter"] is True
    assert "openrouter" in data["provider_models"]
    assert data["alias_tiers"]["openrouter"]["lake"] == "anthropic/claude-sonnet-latest"


import json  # noqa: E402  (used by test_list_models above)


# ── resolve_with_fallback (graceful degradation) ────────────────────────

from ciao.providers.routing import resolve_with_fallback, intended_backend  # noqa: E402


def test_intended_backend_from_shape() -> None:
    assert intended_backend("anthropic/claude-haiku-4.5") == "openrouter"
    assert intended_backend("kimi-k2.7-code:cloud") == "ollama"
    assert intended_backend("opus") == "anthropic"
    assert intended_backend("claude-opus-4-8") == "anthropic"


def test_resolve_no_fallback_when_backend_available() -> None:
    config = CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t", "OPENROUTER_API_KEY": "sk-or"})
    model, env, note = resolve_with_fallback("anthropic/claude-haiku-4.5", config)
    assert model == "anthropic/claude-haiku-4.5"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-or"
    assert note is None


def test_resolve_falls_back_when_openrouter_not_configured() -> None:
    # No OpenRouter key, no Ollama: an owner/model id can't route, so fall
    # back to an Anthropic alias and surface a note.
    config = CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t"})
    model, env, note = resolve_with_fallback("anthropic/claude-haiku-4.5", config)
    assert model in {"sonnet", "haiku", "opus"}
    assert env == {}  # Anthropic alias -> no env overrides
    assert note is not None
    assert "fell back" in note
    assert "openrouter" in note


def test_resolve_falls_back_to_openrouter_when_ollama_missing() -> None:
    # Ollama id but no Ollama key; OpenRouter is configured -> fall back to
    # the OpenRouter sonnet tier.
    config = CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t", "OPENROUTER_API_KEY": "sk-or"})
    model, env, note = resolve_with_fallback("kimi-k2.7-code:cloud", config)
    assert model == config.openrouter.sonnet_model
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-or"
    assert note is not None
    assert "ollama" in note
