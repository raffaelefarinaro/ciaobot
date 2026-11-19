from __future__ import annotations

import pytest

import ciao.providers.oneshot as oneshot


@pytest.mark.asyncio
async def test_opencode_oneshot_uses_build_with_deny_all_permissions(
    monkeypatch, tmp_path
) -> None:
    from ciao.models import ResultEvent
    from ciao.providers.opencode import OpencodeProvider

    captured: dict = {}

    async def fake_run(self, request, on_handle):
        captured["request"] = request
        captured["settings"] = self._session_settings(request)
        captured["tools_enabled"] = self._tools_enabled
        captured["calls"] = captured.get("calls", 0) + 1
        yield ResultEvent(type="result", result="ok")

    async def fake_delete(self):
        pass

    async def fake_disconnect(self):
        pass

    monkeypatch.setattr(OpencodeProvider, "run_streaming", fake_run)
    monkeypatch.setattr(OpencodeProvider, "delete_current_session", fake_delete)
    monkeypatch.setattr(OpencodeProvider, "disconnect", fake_disconnect)

    assert await oneshot._run_opencode_oneshot(
        "prompt", system_prompt="system", model="model", env=None, cwd=tmp_path
    ) == "ok"
    request = captured["request"]
    agent, permissions = captured["settings"]
    assert request.mode == "auto"
    assert agent == "build"
    assert permissions == [{"action": "*", "resource": "*", "effect": "deny"}]
    assert captured["tools_enabled"] is False
    assert captured["calls"] == 1
    assert request.mode != "plan"


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
# Routines (titles, insights) reach non-Anthropic, non-OpenAI models
# through opencode, so opencode needs a one-shot path of its own.


def _fake_opencode(captured: dict, events: list[object]):
    """Stub OpencodeProvider recording construction and request arguments."""

    class FakeOpencodeProvider:
        def __init__(
            self,
            workspace_root,
            *,
            developer_instructions="",
            tools_enabled=True,
            **_kw,
        ):
            captured["workspace_root"] = workspace_root
            captured["developer_instructions"] = developer_instructions
            captured["tools_enabled"] = tools_enabled
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
    # Build avoids the Plan plugin; the explicit deny-all rules still disable
    # every tool, including writes.
    assert captured["request"].mode == "auto"
    assert captured["workspace_root"] != tmp_path.resolve()
    assert not captured["workspace_root"].exists()
    assert captured["tools_enabled"] is False


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
