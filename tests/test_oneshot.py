from __future__ import annotations

import pytest

import ciao.providers.oneshot as oneshot


@pytest.fixture
def acp_calls(monkeypatch):
    """Replace the ACP one-shot. Each call takes the next scripted value, or raises it."""
    from ciao import setup_status
    from ciao.providers.acp import oneshot as acp_oneshot

    monkeypatch.setattr(setup_status, "claude_cli_path", lambda: "")
    calls: list[dict] = []
    script: list = []

    async def fake_run(spec, prompt, **kwargs):
        calls.append({"spec": spec, "prompt": prompt, **kwargs})
        item = script[min(len(calls), len(script)) - 1]
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(acp_oneshot, "run_acp_oneshot", fake_run)
    return calls, script


@pytest.mark.asyncio
async def test_run_oneshot_strips_fast_mode_suffix(acp_calls) -> None:
    calls, script = acp_calls
    script.append("ok")

    await oneshot.run_oneshot("hello", system_prompt="sys", model="claude-opus-4-8[1m]")

    assert calls[0]["model"] == "claude-opus-4-8"


@pytest.mark.asyncio
async def test_run_oneshot_passes_plain_models_through(acp_calls) -> None:
    calls, script = acp_calls
    script.append("ok")

    await oneshot.run_oneshot("hello", system_prompt="sys", model="haiku")

    assert calls[0]["model"] == "haiku"


@pytest.mark.asyncio
async def test_run_oneshot_runs_claude_over_acp_with_its_no_tools_meta(acp_calls) -> None:
    from ciao.providers.acp.agents import ClaudeAcpProvider

    calls, script = acp_calls
    script.append("ok")

    assert await oneshot.run_oneshot("hello", system_prompt="sys", model="haiku") == "ok"

    assert calls[0]["session_meta"] == ClaudeAcpProvider.oneshot_meta()


@pytest.mark.asyncio
async def test_run_oneshot_raises_oneshot_error_with_detail(acp_calls) -> None:
    calls, script = acp_calls
    script.append(oneshot.OneShotError("claude one-shot request failed.", transient=False))

    with pytest.raises(oneshot.OneShotError) as excinfo:
        await oneshot.run_oneshot("hi", system_prompt="s", model="haiku")

    assert excinfo.value.detail == "claude one-shot request failed."
    assert excinfo.value.transient is False
    # Non-transient: only one attempt, no retry.
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_run_oneshot_retries_transient_failure(monkeypatch, acp_calls) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(oneshot.asyncio, "sleep", fake_sleep)
    calls, script = acp_calls
    script.extend([oneshot.OneShotError("flake", transient=True), "Recovered Title"])

    out = await oneshot.run_oneshot("hi", system_prompt="s", model="haiku", retry_backoff_s=0.1)

    assert out == "Recovered Title"
    assert len(calls) == 2  # one retry
    assert sleeps == [0.1]  # backoff applied once


@pytest.mark.asyncio
async def test_run_oneshot_exhausts_retries_then_raises(monkeypatch, acp_calls) -> None:
    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(oneshot.asyncio, "sleep", fake_sleep)
    calls, script = acp_calls
    script.append(oneshot.OneShotError("flake", transient=True))  # always transient

    with pytest.raises(oneshot.OneShotError) as excinfo:
        await oneshot.run_oneshot("hi", system_prompt="s", model="haiku", max_retries=2)

    assert excinfo.value.transient is True
    assert len(calls) == 3  # initial + 2 retries


@pytest.mark.asyncio
async def test_run_oneshot_no_retry_when_disabled(acp_calls) -> None:
    calls, script = acp_calls
    script.append(oneshot.OneShotError("flake", transient=True))

    with pytest.raises(oneshot.OneShotError):
        await oneshot.run_oneshot("hi", system_prompt="s", model="haiku", max_retries=0)

    assert len(calls) == 1


# ── opencode one-shot ───────────────────────────────────────────────────
# opencode runs its one-shots over `opencode acp` like every other ACP provider.


@pytest.mark.asyncio
async def test_run_oneshot_runs_opencode_over_acp(acp_calls) -> None:
    from ciao.providers.acp.agents import OPENCODE

    calls, script = acp_calls
    script.append("A Title")

    out = await oneshot.run_oneshot(
        "name this chat",
        system_prompt="Reply with only the title.",
        model="anthropic/claude-haiku-4.5",
        provider="opencode",
    )

    assert out == "A Title"
    assert calls[0]["spec"] is OPENCODE
    assert calls[0]["model"] == "anthropic/claude-haiku-4.5"
    assert calls[0]["session_meta"] == {}


@pytest.mark.asyncio
async def test_run_oneshot_opencode_empty_reply_is_not_an_error(acp_calls) -> None:
    """An empty reply is returned as-is; the caller decides what empty means."""
    calls, script = acp_calls
    script.append("")

    assert await oneshot.run_oneshot("hi", system_prompt="s", model="x/y", provider="opencode") == ""
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_run_oneshot_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unknown one-shot provider"):
        await oneshot.run_oneshot(
            "hi", system_prompt="s", model="haiku", provider="nope"
        )
