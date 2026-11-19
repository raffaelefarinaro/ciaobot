"""Tests for the per-request runtime context every provider appends (the fallback model went with the SDK driver, #3138 C2b)."""

from __future__ import annotations

from ciao.models import AgentRequest
from ciao.providers.base import build_runtime_context


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
