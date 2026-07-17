from __future__ import annotations

import pytest

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

import ciao.providers.oneshot as oneshot


def _fake_query(captured: dict):
    async def fake_query(*, prompt: str, options):
        captured["model"] = options.model
        captured["prompt"] = prompt
        captured["options"] = options
        if False:  # pragma: no cover - make this an async generator
            yield None

    return fake_query


def _result(**kwargs) -> ResultMessage:
    """Build a minimal ResultMessage; callers override the error fields."""
    base = dict(
        subtype="success",
        duration_ms=10,
        duration_api_ms=5,
        is_error=False,
        num_turns=1,
        session_id="s",
        result=None,
    )
    base.update(kwargs)
    return ResultMessage(**base)


def _text_result(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="haiku")


def _script_query(scripts: list[list[object]], calls: list[int]):
    """Return a fake ``query`` that yields the next scripted message list on
    each call. ``calls`` records how many times query was invoked."""

    async def fake_query(*, prompt: str, options):
        idx = calls[0]
        calls[0] += 1
        messages = scripts[min(idx, len(scripts) - 1)]
        for msg in messages:
            yield msg

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


@pytest.mark.asyncio
async def test_run_oneshot_disables_tools_and_filesystem_discovery(
    monkeypatch,
) -> None:
    """Titles/insights must not load Claude Code tools, skills, or MCP."""
    captured: dict = {}
    monkeypatch.setattr(oneshot, "query", _fake_query(captured))

    await oneshot.run_oneshot("hello", system_prompt="sys", model="haiku")
    options = captured["options"]
    assert options.tools == []
    assert options.skills == []
    assert options.setting_sources == []
    assert options.strict_mcp_config is True
    assert options.max_turns == 1


def test_result_error_detail_composes_available_fields() -> None:
    msg = _result(
        subtype="error_during_execution",
        is_error=True,
        api_error_status=502,
        stop_reason="error",
        result="upstream exploded",
    )
    detail, status = oneshot._result_error_detail(msg)
    assert status == 502
    assert "subtype=error_during_execution" in detail
    assert "status=502" in detail
    assert "stop_reason=error" in detail
    assert "upstream exploded" in detail


def test_result_error_detail_marks_empty_body() -> None:
    # The Ollama Cloud flake: is_error with no status, body, or reason.
    detail, status = oneshot._result_error_detail(
        _result(subtype="", is_error=True)
    )
    assert status is None
    assert "empty error result" in detail


def test_is_transient_classification() -> None:
    # Empty body / gateway flake -> retry.
    assert oneshot._is_transient("empty error result (no status or body)", None)
    assert oneshot._is_transient("status=502; unexpected EOF", 502)
    # Auth / subscription / bad-model -> do not retry.
    assert not oneshot._is_transient("authentication_error: invalid x-api-key", 401)
    assert not oneshot._is_transient("credit balance too low", None)
    assert not oneshot._is_transient(
        "There's an issue with the selected model (apfel)", None
    )


@pytest.mark.asyncio
async def test_run_oneshot_raises_oneshot_error_with_detail(monkeypatch) -> None:
    calls = [0]
    err = _result(subtype="error", is_error=True, api_error_status=401,
                  result="authentication_error")
    monkeypatch.setattr(oneshot, "query", _script_query([[err]], calls))

    with pytest.raises(oneshot.OneShotError) as excinfo:
        await oneshot.run_oneshot("hi", system_prompt="s", model="haiku")
    exc = excinfo.value
    assert exc.status == 401
    assert "authentication_error" in exc.detail
    assert exc.transient is False
    # Non-transient: only one attempt, no retry.
    assert calls[0] == 1


@pytest.mark.asyncio
async def test_run_oneshot_retries_transient_empty_body(monkeypatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(oneshot.asyncio, "sleep", fake_sleep)

    calls = [0]
    # First attempt: contentless is_error (the Ollama Cloud flake).
    # Retry: a real assistant answer.
    scripts = [
        [_result(subtype="error", is_error=True)],
        [_text_result("Recovered Title"), _result(is_error=False)],
    ]
    monkeypatch.setattr(oneshot, "query", _script_query(scripts, calls))

    out = await oneshot.run_oneshot(
        "hi", system_prompt="s", model="haiku", retry_backoff_s=0.1
    )
    assert out == "Recovered Title"
    assert calls[0] == 2  # one retry
    assert sleeps == [0.1]  # backoff applied once


@pytest.mark.asyncio
async def test_run_oneshot_exhausts_retries_then_raises(monkeypatch) -> None:
    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(oneshot.asyncio, "sleep", fake_sleep)

    calls = [0]
    scripts = [[_result(subtype="error", is_error=True)]]  # always empty-body error
    monkeypatch.setattr(oneshot, "query", _script_query(scripts, calls))

    with pytest.raises(oneshot.OneShotError) as excinfo:
        await oneshot.run_oneshot(
            "hi", system_prompt="s", model="haiku", max_retries=2
        )
    assert excinfo.value.transient is True
    assert calls[0] == 3  # initial + 2 retries


@pytest.mark.asyncio
async def test_run_oneshot_no_retry_when_disabled(monkeypatch) -> None:
    calls = [0]
    scripts = [[_result(subtype="error", is_error=True)]]
    monkeypatch.setattr(oneshot, "query", _script_query(scripts, calls))

    with pytest.raises(oneshot.OneShotError):
        await oneshot.run_oneshot(
            "hi", system_prompt="s", model="haiku", max_retries=0
        )
    assert calls[0] == 1
