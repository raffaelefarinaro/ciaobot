from __future__ import annotations

import pytest

import ciao.providers.oneshot as oneshot


def _fake_query(captured: dict):
    async def fake_query(*, prompt: str, options):
        captured["model"] = options.model
        captured["prompt"] = prompt
        if False:  # pragma: no cover - make this an async generator
            yield None

    return fake_query


@pytest.mark.asyncio
async def test_run_oneshot_strips_fast_mode_suffix(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(oneshot, "query", _fake_query(captured))

    await oneshot.run_oneshot(
        "hello", system_prompt="sys", model="claude-opus-4-8[1m]"
    )
    assert captured["model"] == "claude-opus-4-8"


@pytest.mark.asyncio
async def test_run_oneshot_passes_plain_models_through(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(oneshot, "query", _fake_query(captured))

    await oneshot.run_oneshot("hello", system_prompt="sys", model="haiku")
    assert captured["model"] == "haiku"
