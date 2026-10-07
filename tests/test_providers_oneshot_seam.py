"""The ``options_hook`` seam on ``run_oneshot`` (#1012).

Before this, the only way to read the one-shot's ``ClaudeAgentOptions`` was to
patch ``ciao.providers.oneshot.query`` — which works, but binds a test to an
import name rather than to a contract. The seam exists so a caller can assert
the no-tools contract off a real options object: an extraction turn that could
reach a vault note, promote a region or run a command would be a prompt away
from doing so, and "we told it not to" is not evidence.

These tests are about the seam's own shape, so they are worth reading as a
description of it:

* it observes and nothing more — the options are byte-identical with and without
  a hook, so no caller can widen the contract by passing one;
* it runs **before** the turn, so what it reports is the turn that ran;
* it does not swallow anything — a hook that raises propagates, because a caller
  observing a turn must not be able to hide it;
* it is not called on the opencode path, whose deny-all is derived at
  session-create time rather than in an options object, and pretending otherwise
  would be a lie told to a caller watching for one.

No model, provider or network runs here: ``query`` is patched, exactly as it is
in ``tests/test_oneshot.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock

import ciao.providers.oneshot as oneshot


def _capturing_query(captured: dict):
    async def fake_query(*, prompt: str, options):
        captured["prompt"] = prompt
        captured["options"] = options
        yield AssistantMessage(content=[TextBlock(text="ok")], model="haiku")

    return fake_query


def _observed(seen: list[ClaudeAgentOptions]):
    """A hook that records the options it was handed, in order."""

    def hook(options: ClaudeAgentOptions) -> None:
        seen.append(options)

    return hook


@pytest.mark.asyncio
async def test_options_hook_observes_the_no_tools_turn(monkeypatch) -> None:
    """The hook must be able to see the four things that carry the decision."""
    captured: dict = {}
    seen: list[ClaudeAgentOptions] = []
    monkeypatch.setattr(oneshot, "query", _capturing_query(captured))

    await oneshot.run_oneshot(
        "read this transcript",
        system_prompt="sys",
        model="haiku",
        max_turns=3,
        options_hook=_observed(seen),
    )

    assert len(seen) == 1
    options = seen[0]
    assert captured["options"] is options, (
        "the hook must see the options the turn ran with"
    )
    assert options.tools == []
    assert options.setting_sources == []
    assert options.skills == []
    assert options.strict_mcp_config is True
    assert options.max_turns == 3, (
        "max_turns is the caller's, and the hook sees it as passed"
    )
    assert options.system_prompt == "sys"


@pytest.mark.asyncio
async def test_options_hook_runs_before_the_turn(monkeypatch) -> None:
    """A hook that reports options the turn did not run with is worthless."""
    order: list[str] = []
    seen: list[ClaudeAgentOptions] = []

    async def fake_query(*, prompt: str, options):
        order.append("query")
        if False:  # pragma: no cover - make this an async generator
            yield None

    def hook(_options: ClaudeAgentOptions) -> None:
        order.append("hook")
        seen.append(_options)

    monkeypatch.setattr(oneshot, "query", fake_query)
    await oneshot.run_oneshot("hi", system_prompt="s", model="haiku", options_hook=hook)

    assert order == ["hook", "query"]
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_options_hook_changes_nothing_about_the_turn(monkeypatch) -> None:
    """The seam is an observation point: it must not be a policy lever."""
    with_hook: dict = {}
    without_hook: dict = {}
    monkeypatch.setattr(oneshot, "query", _capturing_query(with_hook))

    await oneshot.run_oneshot(
        "hi", system_prompt="s", model="haiku", env={"K": "v"},
        options_hook=_observed([]),
    )

    monkeypatch.setattr(oneshot, "query", _capturing_query(without_hook))
    await oneshot.run_oneshot(
        "hi", system_prompt="s", model="haiku", env={"K": "v"}
    )

    first, second = with_hook["options"], without_hook["options"]
    for field in (
        "model", "system_prompt", "setting_sources", "skills", "tools",
        "strict_mcp_config", "cli_path", "max_turns", "env",
    ):
        assert getattr(first, field) == getattr(second, field), field


@pytest.mark.asyncio
async def test_options_hook_failure_is_not_swallowed(monkeypatch) -> None:
    """A caller watching the turn must not also be able to hide it."""
    captured: dict = {}
    monkeypatch.setattr(oneshot, "query", _capturing_query(captured))

    def hook(_options: ClaudeAgentOptions) -> None:
        raise RuntimeError("options are not what this caller needs")

    with pytest.raises(RuntimeError, match="not what this caller needs"):
        await oneshot.run_oneshot(
            "hi", system_prompt="s", model="haiku", options_hook=hook
        )
    assert "options" not in captured, "the turn must not have started"


@pytest.mark.asyncio
async def test_opencode_oneshot_never_calls_the_claude_options_hook(
    monkeypatch, tmp_path: Path
) -> None:
    """The opencode path has no options object, so it must not fake one.

    Asserting ``tools == []`` against a hook that opencode never calls would be
    a green test that proves nothing about the opencode turn; the deny-all there
    is the session permission list ``mode_settings`` derives, and it is pinned
    in ``tests/test_opencode_provider.py`` instead.
    """
    from ciao.models import ResultEvent
    import ciao.providers.opencode as opencode_mod

    seen: list[ClaudeAgentOptions] = []

    class FakeOpencodeProvider:
        def __init__(self, workspace_root, *, developer_instructions="",
                     tools_enabled=True, **_kw):
            self.tools_enabled = tools_enabled

        async def run_streaming(self, request, register_handle):
            register_handle(None)
            yield ResultEvent(type="result", result="[]")

        async def disconnect(self):
            return None

        async def delete_current_session(self):
            return True

    monkeypatch.setattr(opencode_mod, "OpencodeProvider", FakeOpencodeProvider)

    out = await oneshot.run_oneshot(
        "hi", system_prompt="s", model="x/y", provider="opencode", cwd=tmp_path,
        options_hook=_observed(seen),
    )

    assert out == "[]"
    assert seen == []