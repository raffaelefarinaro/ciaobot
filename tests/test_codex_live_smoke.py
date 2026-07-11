"""Opt-in authenticated Codex app-server smoke test.

Normal CI must not spend subscription quota. Run explicitly with:

    CIAO_CODEX_LIVE_SMOKE=1 pytest tests/test_codex_live_smoke.py -q
"""

from __future__ import annotations

import os

import pytest

from ciao.models import AgentRequest, ResultEvent, ToolUseEvent
from ciao.providers.codex import CodexProvider, codex_login_status


@pytest.mark.skipif(
    os.environ.get("CIAO_CODEX_LIVE_SMOKE") != "1",
    reason="set CIAO_CODEX_LIVE_SMOKE=1 to consume authenticated Codex quota",
)
async def test_authenticated_codex_tool_turn(tmp_path) -> None:
    status = codex_login_status()
    assert status["ok"], status["detail"]
    catalog = await CodexProvider.model_catalog(tmp_path, force=True)
    visible = [item for item in catalog if not item.get("hidden")]
    assert visible, "authenticated Codex account returned no visible models"
    selected = next(
        (item for item in visible if item.get("isDefault")), visible[0]
    )
    model = str(selected.get("model") or selected.get("id") or "")
    assert model

    provider = CodexProvider(tmp_path)
    events = []
    try:
        async for event in provider.run_streaming(
            AgentRequest(
                prompt=(
                    "Create a file named codex-smoke.txt in the current workspace "
                    "containing exactly: codex smoke ok\nThen reply with: smoke complete"
                ),
                model=model,
                mode="bypass",
                provider="codex",
            ),
            lambda _handle: None,
        ):
            events.append(event)
    finally:
        await provider.disconnect()

    assert any(isinstance(event, ToolUseEvent) for event in events)
    result = next(event for event in events if isinstance(event, ResultEvent))
    assert result.is_error is False, result.result
    assert "smoke complete" in result.result.lower()
    assert (tmp_path / "codex-smoke.txt").read_text(encoding="utf-8") == "codex smoke ok\n"
