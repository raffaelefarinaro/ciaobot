"""Tests for the ciao.critique adversarial-review engine (no network)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

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
    """Each panel model is called through run_oneshot with routing env."""
    from ciao.config import CiaoConfig

    config = CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t", "OPENROUTER_API_KEY": "sk-or"})

    captured: dict = {}

    async def fake_oneshot(prompt, *, system_prompt, model, env, timeout_s=120.0, provider="claude", cwd=None):
        captured["model"] = model
        captured["env"] = env
        captured["provider"] = provider
        return json.dumps({"verdict": "ship", "confidence": 5, "summary": "solid"})

    monkeypatch.setattr("ciao.critique.run_oneshot", fake_oneshot)
    result = asyncio.run(crt._review_one("anthropic/claude-haiku-4.5", "x", "prompt", config, 60.0))
    assert result.ok is True
    assert result.review == {"verdict": "ship", "confidence": 5, "summary": "solid"}
    assert captured["model"] == "anthropic/claude-haiku-4.5"
    assert captured["provider"] == "claude"
    assert captured["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-or"


def test_review_one_routes_codex_entry_through_codex_provider(monkeypatch) -> None:
    """A ``codex:`` panel entry dispatches via the Codex provider, no routing env."""
    from ciao.config import CiaoConfig

    config = CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t", "OPENROUTER_API_KEY": "sk-or"})

    captured: dict = {}

    async def fake_oneshot(prompt, *, system_prompt, model, env, timeout_s=120.0, provider="claude", cwd=None):
        captured["model"] = model
        captured["env"] = env
        captured["provider"] = provider
        captured["cwd"] = cwd
        return json.dumps({"verdict": "revise", "confidence": 4})

    monkeypatch.setattr("ciao.critique.run_oneshot", fake_oneshot)
    result = asyncio.run(crt._review_one("codex:fable", "x", "prompt", config, 60.0))
    assert result.ok is True
    # The prefix is stripped before dispatch, but preserved for display/ordering.
    assert captured["model"] == "fable"
    assert result.model == "codex:fable"
    assert captured["provider"] == "codex"
    assert captured["env"] == {}
    assert captured["cwd"] == config.workspace_root


def test_review_one_records_failure_on_exception(monkeypatch) -> None:
    from ciao.config import CiaoConfig

    config = CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t"})

    async def boom(prompt, *, system_prompt, model, env, timeout_s=120.0, provider="claude", cwd=None):
        raise OSError("no upstream")

    monkeypatch.setattr("ciao.critique.run_oneshot", boom)
    result = asyncio.run(crt._review_one("anthropic/claude-haiku-4.5", "x", "p", config, 60.0))
    assert result.ok is False
    assert "no upstream" in (result.error or "")


def test_print_panel_uses_openrouter_default_when_key_set(monkeypatch) -> None:
    import sys
    from contextlib import redirect_stdout
    import io

    monkeypatch.setattr(crt, "is_anthropic_available", lambda: False)
    monkeypatch.setattr(crt, "is_codex_available", lambda: False)
    monkeypatch_env = {"PWA_AUTH_TOKEN": "t", "OPENROUTER_API_KEY": "sk-or"}
    # CiaoConfig.from_env reads os.environ; patch it.
    import os
    old = os.environ.copy()
    os.environ.update(monkeypatch_env)
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = crt.main(["--print-panel"])
    finally:
        os.environ.clear()
        os.environ.update(old)
    assert rc == 0
    assert "anthropic/claude-opus-latest" in buf.getvalue()


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


def test_default_critique_panel_prioritizes_anthropic(monkeypatch) -> None:
    from ciao.config import CiaoConfig

    config = CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t", "OPENROUTER_API_KEY": "sk-or"})
    # Mock is_anthropic_available to return True
    monkeypatch.setattr(crt, "is_anthropic_available", lambda: True)
    monkeypatch.setattr(crt, "is_codex_available", lambda: False)

    panel = crt.default_critique_panel(config)
    assert panel == ["opus", "fable"]


def test_default_critique_panel_uses_openrouter_when_anthropic_unavailable(monkeypatch) -> None:
    from ciao.config import CiaoConfig

    config = CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t", "OPENROUTER_API_KEY": "sk-or"})
    # Mock is_anthropic_available to return False
    monkeypatch.setattr(crt, "is_anthropic_available", lambda: False)
    monkeypatch.setattr(crt, "is_codex_available", lambda: False)

    panel = crt.default_critique_panel(config)
    assert panel == ["anthropic/claude-opus-latest", "anthropic/claude-fable-latest"]


def test_default_critique_panel_includes_ollama_when_available(monkeypatch) -> None:
    from ciao.config import CiaoConfig

    config = CiaoConfig.from_env({
        "PWA_AUTH_TOKEN": "t",
        "CIAO_OLLAMA_API_KEY": "sk-ollama",
    })
    monkeypatch.setattr(crt, "is_anthropic_available", lambda: True)
    monkeypatch.setattr(crt, "is_codex_available", lambda: False)

    panel = crt.default_critique_panel(config)
    assert "minimax-m3:cloud" in panel
    assert "glm-5.2:cloud" in panel


def test_default_critique_panel_appends_codex_fable_when_signed_in(monkeypatch) -> None:
    from ciao.config import CiaoConfig

    config = CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t"})
    monkeypatch.setattr(crt, "is_anthropic_available", lambda: True)
    monkeypatch.setattr(crt, "is_codex_available", lambda: True)

    panel = crt.default_critique_panel(config)
    assert panel == ["opus", "fable", "codex:fable"]


def test_default_critique_panel_omits_codex_when_signed_out(monkeypatch) -> None:
    from ciao.config import CiaoConfig

    config = CiaoConfig.from_env({"PWA_AUTH_TOKEN": "t"})
    monkeypatch.setattr(crt, "is_anthropic_available", lambda: True)
    monkeypatch.setattr(crt, "is_codex_available", lambda: False)

    panel = crt.default_critique_panel(config)
    assert "codex:fable" not in panel
