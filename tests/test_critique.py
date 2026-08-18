"""Tests for the ciao.critique adversarial-review engine (no network)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import ciao.critique as crt


def test_extract_json_parses_fenced_object() -> None:
    raw = '```json\n{"verdict": "revise", "confidence": 3}\n```'
    parsed = crt.extract_json(raw)
    assert parsed == {"verdict": "revise", "confidence": 3}


def test_extract_json_finds_first_object_in_prose() -> None:
    parsed = crt.extract_json('Here is my review:\n{"verdict": "ship"}\nThanks.')
    assert parsed == {"verdict": "ship"}


def test_extract_json_returns_none_on_garbage() -> None:
    assert crt.extract_json("no json here") is None


def test_aggregate_counts_verdicts_and_severities() -> None:
    results = [
        crt.ModelResult("a", 1.0, True, review={"verdict": "ship", "confidence": 5, "issues": [{"severity": "minor"}]}),
        crt.ModelResult("b", 1.0, True, review={"verdict": "revise", "confidence": 3, "issues": [{"severity": "blocking"}]}),
        crt.ModelResult("c", 1.0, False, error="timeout"),
    ]
    agg = crt.aggregate(results)
    assert agg["model_count"] == 3
    assert agg["ok_count"] == 2
    assert agg["verdicts"] == {"ship": 1, "revise": 1}
    assert agg["by_severity"] == {"minor": 1, "blocking": 1}
    # blocking sorts before minor
    assert agg["issues"][0]["severity"] == "blocking"


def test_render_markdown_includes_failures_and_verdicts() -> None:
    results = [
        crt.ModelResult("a", 1.0, True, review={"verdict": "ship", "confidence": 5, "summary": "ok", "issues": []}),
        crt.ModelResult("b", 1.0, False, error="timeout"),
    ]
    md = crt.render_markdown("plan.md", results, crt.aggregate(results))
    assert "# Adversarial review: plan.md" in md
    assert "Failed models" in md
    assert "`b`" in md and "timeout" in md
    assert "verdict: **ship**" in md


def test_review_one_routes_via_oneshot(monkeypatch) -> None:
    """An unprefixed panel entry runs through Claude Code."""
    from ciao.config import CiaoConfig

    config = CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t"})

    captured: dict = {}

    async def fake_oneshot(prompt, *, system_prompt, model, timeout_s=120.0, provider="claude", cwd=None):
        captured["model"] = model
        captured["provider"] = provider
        captured["cwd"] = cwd
        return json.dumps({"verdict": "ship", "confidence": 5, "summary": "solid"})

    monkeypatch.setattr("ciao.critique.run_oneshot", fake_oneshot)
    result = asyncio.run(crt._review_one("opus", "x", "prompt", config, 60.0))
    assert result.ok is True
    assert result.review == {"verdict": "ship", "confidence": 5, "summary": "solid"}
    assert captured["model"] == "opus"
    assert captured["provider"] == "claude"
    # Only the app-server providers need a working directory.
    assert captured["cwd"] is None


@pytest.mark.parametrize(
    ("entry", "provider"),
    [("codex:fable", "codex"), ("opencode:fable", "opencode")],
)
def test_review_one_routes_prefixed_entry_to_its_provider(
    monkeypatch, entry, provider
) -> None:
    """A provider prefix dispatches there and is stripped from the model id."""
    from ciao.config import CiaoConfig

    config = CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t"})

    captured: dict = {}

    async def fake_oneshot(prompt, *, system_prompt, model, timeout_s=120.0, provider="claude", cwd=None):
        captured["model"] = model
        captured["provider"] = provider
        captured["cwd"] = cwd
        return json.dumps({"verdict": "revise", "confidence": 4})

    monkeypatch.setattr("ciao.critique.run_oneshot", fake_oneshot)
    result = asyncio.run(crt._review_one(entry, "x", "prompt", config, 60.0))
    assert result.ok is True
    assert captured["model"] == "fable"
    assert captured["provider"] == provider
    # The prefix is stripped before dispatch but kept for display/ordering.
    assert result.model == entry
    assert captured["cwd"] == config.workspace_root


def test_review_one_records_failure_on_exception(monkeypatch) -> None:
    from ciao.config import CiaoConfig

    config = CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t"})

    async def boom(prompt, *, system_prompt, model, timeout_s=120.0, provider="claude", cwd=None):
        raise OSError("no upstream")

    monkeypatch.setattr("ciao.critique.run_oneshot", boom)
    result = asyncio.run(crt._review_one("opus", "x", "p", config, 60.0))
    assert result.ok is False
    assert "no upstream" in (result.error or "")


def test_resolve_critique_panel_uses_settings_override() -> None:
    from ciao.config import CiaoConfig

    config = CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t"})
    config.critique_models = "model-a,model-b"
    assert crt.resolve_critique_panel(config) == ["model-a", "model-b"]


def test_resolve_critique_panel_cli_override_wins() -> None:
    from ciao.config import CiaoConfig

    config = CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t"})
    config.critique_models = "model-a,model-b"
    assert crt.resolve_critique_panel(config, override="only-this") == ["only-this"]


def _panel(
    monkeypatch,
    *,
    codex: bool,
    opencode: bool,
    anthropic: bool = True,
    codex_model: str = "",
    opencode_model: str = "",
) -> list[str]:
    """Resolve the default panel with each vendor probe pinned."""
    from ciao.config import CiaoConfig

    monkeypatch.setattr(crt, "is_anthropic_available", lambda: anthropic)
    monkeypatch.setattr(crt, "is_codex_available", lambda: codex)
    monkeypatch.setattr(crt, "is_opencode_available", lambda: opencode)
    config = CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t"})
    config.codex = SimpleNamespace(default_model=codex_model)
    config.opencode = SimpleNamespace(default_model=opencode_model)
    return crt.default_critique_panel(config)


def test_default_critique_panel_is_anthropic_only_when_nothing_else_signed_in(
    monkeypatch,
) -> None:
    assert _panel(monkeypatch, codex=False, opencode=False) == ["opus", "fable"]


def test_default_critique_panel_drops_anthropic_when_it_is_not_signed_in(
    monkeypatch,
) -> None:
    """Every entry is gated, Anthropic included."""
    panel = _panel(monkeypatch, codex=True, opencode=True, anthropic=False)
    assert panel == ["codex:", "opencode:"]


def test_default_critique_panel_never_resolves_empty(monkeypatch) -> None:
    """With nothing signed in, report a real auth error rather than review nothing."""
    panel = _panel(monkeypatch, codex=False, opencode=False, anthropic=False)
    assert panel == ["opus", "fable"]


def test_default_critique_panel_adds_one_voice_per_signed_in_vendor(
    monkeypatch,
) -> None:
    """The point of the panel is disagreement, so prefer vendor diversity.

    Three Anthropic models would largely agree with each other; Codex and
    opencode each contribute a genuinely different model.
    """
    assert _panel(monkeypatch, codex=True, opencode=True) == [
        "opus",
        "fable",
        "codex:",
        "opencode:",
    ]


def test_default_critique_panel_uses_per_provider_default_models(
    monkeypatch,
) -> None:
    """Codex/opencode entries carry the operator's per-provider default model.

    A tier alias like ``fable`` is only meaningful to Claude Code; the other
    providers must get a concrete model id (or none, to use their own default).
    """
    assert _panel(
        monkeypatch,
        codex=True,
        opencode=True,
        codex_model="gpt-5.6-sol",
        opencode_model="ollama-cloud/deepseek-v4-flash",
    ) == [
        "opus",
        "fable",
        "codex:gpt-5.6-sol",
        "opencode:ollama-cloud/deepseek-v4-flash",
    ]


def test_default_critique_panel_omits_vendors_that_are_signed_out(monkeypatch) -> None:
    """An unavailable provider would put a guaranteed failure in the panel."""
    codex_only = _panel(monkeypatch, codex=True, opencode=False)
    assert codex_only == ["opus", "fable", "codex:"]

    opencode_only = _panel(monkeypatch, codex=False, opencode=True)
    assert opencode_only == ["opus", "fable", "opencode:"]
