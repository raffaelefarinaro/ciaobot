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
    # ``max_turns=2`` (not 1) absorbs a stray ``stop_reason=tool_use`` the
    # model occasionally emits under ``tools=[]`` — see the comment in
    # ``_run_claude_oneshot`` for the full rationale.
    assert options.max_turns == 2


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


@pytest.mark.asyncio
async def test_run_oneshot_disables_claude_auto_memory(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(oneshot, "query", _fake_query(captured))

    await oneshot.run_oneshot("hello", system_prompt="sys", model="haiku")
    options = captured["options"]
    assert options.env.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY") == "1"
    assert options.env.get("CLAUDE_CODE_DISABLE_ARTIFACT") == "1"


# ── opencode one-shot ───────────────────────────────────────────────────
# Routines (titles, insights, critique) reach non-Anthropic, non-OpenAI models
# through opencode, so opencode needs a one-shot path of its own.


def _fake_opencode(captured: dict, events: list[object]):
    """Stub OpencodeProvider recording construction and request arguments."""

    class FakeOpencodeProvider:
        def __init__(self, workspace_root, *, developer_instructions="", **_kw):
            captured["workspace_root"] = workspace_root
            captured["developer_instructions"] = developer_instructions
            captured["disconnected"] = False
            captured["deleted"] = False

        @property
        def current_session_id(self):
            return "one-shot-session"

        async def run_streaming(self, request, register_handle):
            captured["request"] = request
            register_handle(None)
            for event in events:
                yield event

        async def disconnect(self):
            captured["disconnected"] = True

        async def delete_current_session(self):
            captured["deleted"] = True
            return True

    return FakeOpencodeProvider


@pytest.mark.asyncio
async def test_run_oneshot_dispatches_to_opencode(monkeypatch, tmp_path) -> None:
    from ciao.models import ResultEvent
    import ciao.providers.opencode as opencode_mod

    captured: dict = {}
    monkeypatch.setattr(
        opencode_mod,
        "OpencodeProvider",
        _fake_opencode(captured, [ResultEvent(type="result", result="A Title")]),
    )

    out = await oneshot.run_oneshot(
        "name this chat",
        system_prompt="Reply with only the title.",
        model="anthropic/claude-haiku-4.5",
        provider="opencode",
        cwd=tmp_path,
    )

    assert out == "A Title"
    # The system prompt has to arrive as developer instructions: opencode takes
    # it in the prompt body's `system` field, not as part of the user prompt.
    assert captured["developer_instructions"] == "Reply with only the title."
    assert captured["request"].provider == "opencode"
    assert captured["request"].model == "anthropic/claude-haiku-4.5"
    # A one-shot must not be able to write.
    assert captured["request"].mode == "plan"
    assert captured["workspace_root"] == tmp_path.resolve()


@pytest.mark.asyncio
async def test_run_oneshot_opencode_always_disconnects(monkeypatch, tmp_path) -> None:
    """The server and session are torn down even when the turn errors."""
    from ciao.models import ResultEvent
    import ciao.providers.opencode as opencode_mod

    captured: dict = {}
    monkeypatch.setattr(
        opencode_mod,
        "OpencodeProvider",
        _fake_opencode(
            captured,
            [ResultEvent(type="result", result="model not found", is_error=True)],
        ),
    )

    with pytest.raises(oneshot.OneShotError) as excinfo:
        await oneshot.run_oneshot(
            "hi", system_prompt="s", model="x/y", provider="opencode", cwd=tmp_path
        )

    assert "model not found" in excinfo.value.detail
    # opencode does not distinguish retriable upstream flakes, so a returned
    # error is terminal -- retrying would double-charge for the same failure.
    assert excinfo.value.transient is False
    assert captured["disconnected"] is True
    assert captured["deleted"] is True


@pytest.mark.asyncio
async def test_run_oneshot_opencode_empty_stream_returns_empty(
    monkeypatch, tmp_path
) -> None:
    """No ResultEvent is not an error -- the caller decides what empty means."""
    import ciao.providers.opencode as opencode_mod

    captured: dict = {}
    monkeypatch.setattr(
        opencode_mod, "OpencodeProvider", _fake_opencode(captured, [])
    )

    out = await oneshot.run_oneshot(
        "hi", system_prompt="s", model="x/y", provider="opencode", cwd=tmp_path
    )
    assert out == ""
    assert captured["disconnected"] is True


@pytest.mark.asyncio
async def test_run_oneshot_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unknown one-shot provider"):
        await oneshot.run_oneshot(
            "hi", system_prompt="s", model="haiku", provider="nope"
        )
