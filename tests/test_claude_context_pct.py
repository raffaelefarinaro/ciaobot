"""Context-window % on the Claude turn footer.

The usage shapes below are copied from a real Claude Agent SDK 0.2.158 turn
(bundled CLI 2.1.280, claude-opus-5-5[1m], four sequential Bash calls): the
turn's ``ResultMessage.usage`` sums every model call, while each
``AssistantMessage.usage`` is one call, and ``model_usage`` lists a haiku side
call ahead of the chat model.
"""

from __future__ import annotations

import pytest
from claude_agent_sdk import AssistantMessage, CLIConnectionError, ResultMessage, TextBlock

from ciao.models import ResultEvent
from ciao.providers.claude import ClaudeProvider

HAIKU = {
    "inputTokens": 939,
    "outputTokens": 11,
    "cacheReadInputTokens": 0,
    "cacheCreationInputTokens": 0,
    "contextWindow": 200000,
    "maxOutputTokens": 32000,
    "canonicalModel": "claude-haiku-4-5",
}


def _opus(window: int = 1_000_000) -> dict[str, object]:
    return {
        "inputTokens": 10,
        "outputTokens": 414,
        "cacheReadInputTokens": 81794,
        "cacheCreationInputTokens": 22846,
        "contextWindow": window,
        "maxOutputTokens": 128000,
        "canonicalModel": "claude-opus-5-5",
    }


def _result(
    *,
    cache_read: int = 81794,
    model_usage: dict[str, object] | None = None,
    iterations: list[dict[str, int]] | None = None,
) -> ResultMessage:
    usage: dict[str, object] = {
        "input_tokens": 10,
        "cache_creation_input_tokens": 22846,
        "cache_read_input_tokens": cache_read,
        "output_tokens": 414,
    }
    if iterations is not None:
        usage["iterations"] = iterations
    return ResultMessage(
        subtype="success",
        duration_ms=1000,
        duration_api_ms=900,
        is_error=False,
        num_turns=5,
        session_id="sess-1",
        result="done",
        usage=usage,
        model_usage=model_usage
        if model_usage is not None
        else {"claude-haiku-4-5-20251001": HAIKU, "claude-opus-5-5[1m]": _opus()},
    )


def _last_call(
    *, cache_read: int = 22742, cache_creation: int = 104, model: str = "claude-opus-5-5"
) -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text="done")],
        model=model,
        usage={
            "input_tokens": 2,
            "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read,
            "output_tokens": 2,
        },
    )


class _FailingClient:
    """``get_context_usage`` as it fails in the live app.

    The provider connects with its prompt stream, so the SDK closes the CLI's
    stdin once the first result arrives and the control request cannot be
    written.
    """

    async def get_context_usage(self):
        raise CLIConnectionError("ProcessTransport is not ready for writing")


class _Client:
    def __init__(self, response: object) -> None:
        self.response = response

    async def get_context_usage(self):
        return self.response


def _event() -> ResultEvent:
    return ResultEvent(
        type="result",
        result="done",
        session_id="sess-1",
        usage={"input_tokens": "10", "output_tokens": "414"},
    )


async def _augment(client: object, result: ResultMessage | None, last: AssistantMessage | None) -> ResultEvent:
    event = _event()
    await ClaudeProvider._augment_with_context_pct(client, event, result, last)  # type: ignore[arg-type]
    return event


@pytest.mark.asyncio
async def test_multi_call_turn_uses_last_call_not_turn_aggregate() -> None:
    """A tool loop whose summed usage exceeds the window is not "100% full".

    The aggregate (10 + 22,846 + 481,794 = 504,650 tokens) is over the haiku
    window listed first in ``model_usage``. The old estimate showed 100.0%;
    the last call read 22,848 tokens of a 1M window.
    """
    event = await _augment(_FailingClient(), _result(cache_read=481794), _last_call())
    assert event.usage["context_pct"] == "2.3%"


@pytest.mark.asyncio
async def test_window_comes_from_the_chat_model_not_the_first_entry() -> None:
    """A 200k haiku side call listed first must not become the denominator."""
    event = await _augment(_FailingClient(), _result(), _last_call(cache_read=150000))
    # 150,106 / 1,000,000, not 150,106 / 200,000 (75.1%).
    assert event.usage["context_pct"] == "15.0%"


@pytest.mark.asyncio
async def test_window_matches_canonical_model_for_a_plain_key() -> None:
    model_usage = {"claude-haiku-4-5-20251001": HAIKU, "claude-sonnet-4-5": {**_opus(200000), "canonicalModel": "claude-sonnet-4-5"}}
    event = await _augment(
        _FailingClient(),
        _result(model_usage=model_usage),
        _last_call(cache_read=49894, model="claude-sonnet-4-5"),
    )
    assert event.usage["context_pct"] == "25.0%"


@pytest.mark.asyncio
async def test_result_iterations_are_used_without_an_assistant_message() -> None:
    """``usage.iterations`` holds the final call's own usage."""
    result = _result(
        iterations=[{"input_tokens": 2, "cache_creation_input_tokens": 104, "cache_read_input_tokens": 22742, "output_tokens": 118}],
    )
    event = await _augment(_FailingClient(), result, None)
    assert event.usage["context_pct"] == "2.3%"


@pytest.mark.asyncio
async def test_context_pct_is_omitted_without_a_per_call_figure() -> None:
    """Only the turn aggregate is known: leave the field off, never guess."""
    event = await _augment(_FailingClient(), _result(cache_read=481794), None)
    assert "context_pct" not in event.usage


@pytest.mark.asyncio
async def test_context_pct_is_omitted_when_the_model_has_no_window() -> None:
    event = await _augment(
        _FailingClient(),
        _result(model_usage={"claude-haiku-4-5-20251001": HAIKU}),
        _last_call(),
    )
    assert "context_pct" not in event.usage


@pytest.mark.asyncio
async def test_context_pct_is_omitted_when_the_call_overflows_its_window() -> None:
    """A single call larger than the matched window means the match is wrong."""
    model_usage = {"claude-opus-5-5": _opus(200000)}
    event = await _augment(_FailingClient(), _result(model_usage=model_usage), _last_call(cache_read=400000))
    assert "context_pct" not in event.usage


@pytest.mark.asyncio
async def test_unexpected_get_context_usage_error_falls_back_to_estimate() -> None:
    class _Broken:
        async def get_context_usage(self):
            raise RuntimeError("Control request timeout: get_context_usage")

    event = await _augment(_Broken(), _result(), _last_call())
    assert event.usage["context_pct"] == "2.3%"


@pytest.mark.asyncio
async def test_cli_percentage_is_preferred_over_the_estimate() -> None:
    response = {"totalTokens": 22848, "maxTokens": 1000000, "rawMaxTokens": 1000000, "percentage": 2}
    event = await _augment(_Client(response), _result(), _last_call(cache_read=500000))
    assert event.usage["context_pct"] == "2.0%"


@pytest.mark.asyncio
async def test_cli_totals_are_used_when_percentage_is_missing() -> None:
    response = {"totalTokens": 50000, "maxTokens": 200000}
    event = await _augment(_Client(response), _result(), _last_call())
    assert event.usage["context_pct"] == "25.0%"


@pytest.mark.asyncio
async def test_unusable_cli_response_falls_back_to_estimate() -> None:
    event = await _augment(_Client({"categories": []}), _result(), _last_call())
    assert event.usage["context_pct"] == "2.3%"


@pytest.mark.asyncio
async def test_streaming_turn_tracks_the_main_agents_last_call(tmp_path, monkeypatch) -> None:
    """End to end: subagent calls and earlier loop iterations do not count."""
    provider = ClaudeProvider(tmp_path)

    class FakeClient(_FailingClient):
        async def connect(self, _payload):
            return None

        async def query(self, _payload):
            return None

        async def receive_response(self):
            yield _last_call(cache_read=13880, cache_creation=0)
            # A Task subagent's call in its own, smaller context.
            sub = _last_call(cache_read=5000, cache_creation=0)
            sub.parent_tool_use_id = "toolu_task"
            yield sub
            yield _last_call(cache_read=299894, cache_creation=104)
            yield _result(cache_read=900000)

    fake = FakeClient()

    async def fake_ensure(_request):
        provider._connected = True
        provider._client = fake  # type: ignore[assignment]
        provider._session_id = "sess-1"
        return fake

    from ciao.models import AgentRequest

    monkeypatch.setattr(provider, "_ensure_connected", fake_ensure)
    monkeypatch.setattr(provider, "_prompt_payload", lambda _req: None)
    request = AgentRequest(prompt="go", model="opus", mode="normal")
    events = [e async for e in provider.run_streaming(request, lambda _handle: None)]
    results = [e for e in events if isinstance(e, ResultEvent)]
    assert len(results) == 1
    # (2 + 104 + 299,894) / 1,000,000
    assert results[0].usage["context_pct"] == "30.0%"
