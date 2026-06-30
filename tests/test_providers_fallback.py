"""Tests for provider-level SDK upgrades: fallback model + runtime context."""

from __future__ import annotations

from ciao.models import AgentRequest
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
