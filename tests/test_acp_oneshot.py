"""One-shot calls over ACP: unattended, in an empty directory, through the provider descriptor (#3137)."""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, ClassVar

import pytest

from ciao import provider_registry
from ciao.provider_registry import ProviderDescriptor
from ciao.providers.acp.oneshot import run_acp_oneshot
from ciao.providers.acp.provider import AcpProvider
from ciao.providers.acp.session import AcpSession
from ciao.providers.acp.spec import AgentSpec, ModeChoice
from ciao.providers.oneshot import OneShotError, run_oneshot
from tests.acp_helpers import read_calls, write_scenario

SPEC: AgentSpec | None = None  # target of the fake descriptor's acp_spec_path

_TEXT = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "PONG"}}
_OPTIONS = [
    {
        "id": "mode", "name": "Mode", "category": "mode", "type": "select",
        "currentValue": "default",
        "options": [{"value": "default", "name": "Default"}, {"value": "plan", "name": "Plan"}],
    },
]


class FakeOneshotProvider(AcpProvider):
    spec: ClassVar[AgentSpec]


def _install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **scenario: Any) -> Path:
    # A one-shot runs in plan mode, which is fail-closed: the agent must offer plan.
    scenario.setdefault("config_options", _OPTIONS)
    spec, calls = write_scenario(
        tmp_path,
        spec_modes={"plan": (ModeChoice(config_id="mode", values=("plan",)),)},
        **scenario,
    )
    descriptor = ProviderDescriptor(
        id="fake",
        label="Fake agent",
        short_label="Fake",
        cli_label="Fake",
        factory_path="tests.test_acp_oneshot:FakeOneshotProvider",
        acp_spec_path="tests.test_acp_oneshot:SPEC",
    )
    monkeypatch.setattr(sys.modules[__name__], "SPEC", spec)
    monkeypatch.setattr(FakeOneshotProvider, "spec", spec, raising=False)
    monkeypatch.setattr(
        provider_registry, "_DESCRIPTORS", (*provider_registry.descriptors(), descriptor)
    )
    monkeypatch.setattr(
        provider_registry, "_BY_ID", {**provider_registry._BY_ID, "fake": descriptor}
    )
    return calls


@pytest.mark.asyncio
async def test_oneshot_returns_text_and_rejects_permissions(monkeypatch, tmp_path: Path) -> None:
    calls = _install(monkeypatch, tmp_path, prompts=[{"steps": [
        {"permission": {
            "toolCall": {"toolCallId": "t1", "title": "Run rm", "kind": "execute", "status": "pending"},
            "options": [
                {"optionId": "yes", "name": "Yes", "kind": "allow_once"},
                {"optionId": "no", "name": "No", "kind": "reject_once"},
            ],
        }},
        {"update": _TEXT},
    ]}])
    spec = FakeOneshotProvider.spec

    text = await run_acp_oneshot(spec, "say it", system_prompt="be brief", model="", env={})

    assert text == "PONG"
    outcomes = [call["params"] for call in read_calls(calls) if call["method"] == "permission_outcome"]
    # Unattended: the reject option is chosen, never the allow option.
    assert outcomes == [{"outcome": {"outcome": "selected", "optionId": "no"}}]


@pytest.mark.asyncio
async def test_oneshot_runs_in_empty_temp_dir(monkeypatch, tmp_path: Path) -> None:
    calls = _install(monkeypatch, tmp_path, prompts=[{"steps": [{"update": _TEXT}]}])

    await run_acp_oneshot(FakeOneshotProvider.spec, "hi", system_prompt="", model="", env={})

    cwd = Path(next(c for c in read_calls(calls) if c["method"] == "initialize")["params"]["cwd"])
    assert cwd.name == "work"
    assert cwd.parent.name.startswith("ciaobot-acp-oneshot-")
    assert not cwd.parent.exists()  # the whole temporary tree is gone afterwards


@pytest.mark.asyncio
async def test_oneshot_agent_inherits_provider_credentials_and_not_ciao_internals(
    monkeypatch, tmp_path: Path
) -> None:
    # A one-shot spawns through the same connection as a chat, so it gets the same env policy.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "value-anthropic")
    monkeypatch.setenv("CIAO_ENGINE_SECRET", "engine-internal")
    calls = _install(
        monkeypatch, tmp_path, prompts=[{"steps": [{"update": _TEXT}]}],
        record_env=["ANTHROPIC_API_KEY", "CIAO_ENGINE_SECRET"],
    )

    await run_oneshot("hi", system_prompt="", model="", provider="fake", env={}, max_retries=0)

    env = next(c for c in read_calls(calls) if c["method"] == "initialize")["params"]["env"]
    assert env["ANTHROPIC_API_KEY"] == "value-anthropic"
    assert env["CIAO_ENGINE_SECRET"] is None


@pytest.mark.asyncio
async def test_oneshot_refusal_raises_nontransient(monkeypatch, tmp_path: Path) -> None:
    _install(monkeypatch, tmp_path, prompts=[{"steps": [{"stop": "refusal"}]}])

    with pytest.raises(OneShotError) as raised:
        await run_acp_oneshot(FakeOneshotProvider.spec, "hi", system_prompt="", model="", env={})

    assert raised.value.transient is False


@pytest.mark.asyncio
async def test_oneshot_passes_session_meta(monkeypatch, tmp_path: Path) -> None:
    calls = _install(monkeypatch, tmp_path, prompts=[{"steps": [{"update": _TEXT}]}])
    meta = {"claudeCode": {"options": {"tools": [], "skills": []}}}

    await run_acp_oneshot(
        FakeOneshotProvider.spec, "hi", system_prompt="", model="", env={}, session_meta=meta,
    )

    new = next(call for call in read_calls(calls) if call["method"] == "session/new")
    assert new["params"]["_meta"] == meta
    assert new["params"]["mcpServers"] == []


@pytest.mark.asyncio
async def test_oneshot_applies_plan_mode_and_model(monkeypatch, tmp_path: Path) -> None:
    calls = _install(
        monkeypatch, tmp_path, config_options=_OPTIONS, prompts=[{"steps": [{"update": _TEXT}]}],
    )

    await run_acp_oneshot(FakeOneshotProvider.spec, "hi", system_prompt="", model="", env={})

    set_calls = [c["params"] for c in read_calls(calls) if c["method"] == "session/set_config_option"]
    assert [call.get("value") for call in set_calls] == ["plan"]


@pytest.mark.asyncio
async def test_run_oneshot_dispatches_acp_descriptor(monkeypatch, tmp_path: Path) -> None:
    calls = _install(monkeypatch, tmp_path, prompts=[{"steps": [{"update": _TEXT}]}])

    text = await run_oneshot(
        "hi", system_prompt="", model="", provider="fake", env={}, max_retries=0,
    )

    assert text == "PONG"
    assert any(call["method"] == "session/new" for call in read_calls(calls))



@pytest.mark.asyncio
async def test_oneshot_cleanup_failure_does_not_replace_the_reply(
    monkeypatch, tmp_path: Path
) -> None:
    _install(monkeypatch, tmp_path, prompts=[{"steps": [{"update": _TEXT}]}])

    def denied_rmtree(cls, name, ignore_errors=False):
        # Windows-style cleanup failure: the path is in the error text.
        if not ignore_errors:
            raise PermissionError(f"[WinError 5] Access is denied: '{name}'")
        shutil.rmtree(name, ignore_errors=True)

    monkeypatch.setattr(tempfile.TemporaryDirectory, "_rmtree", classmethod(denied_rmtree))

    text = await run_acp_oneshot(FakeOneshotProvider.spec, "hi", system_prompt="", model="", env={})

    assert text == "PONG"


@pytest.mark.asyncio
async def test_oneshot_transport_error_raises_a_fixed_detail(monkeypatch, tmp_path: Path) -> None:
    _install(monkeypatch, tmp_path, prompts=[{"steps": [{"update": _TEXT}]}])

    async def leaking_apply(self, **kwargs):
        raise ConnectionError("adapter says /Users/someone/secret-path token=abc")

    monkeypatch.setattr(AcpSession, "apply", leaking_apply)

    with pytest.raises(OneShotError) as raised:
        await run_acp_oneshot(FakeOneshotProvider.spec, "hi", system_prompt="", model="", env={})

    assert raised.value.transient is False
    assert str(raised.value) == "fake one-shot request failed."
    assert "secret-path" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True


@pytest.mark.asyncio
async def test_oneshot_throwaway_session_is_deleted_when_the_agent_advertises_delete(
    monkeypatch, tmp_path: Path
) -> None:
    calls = _install(
        monkeypatch, tmp_path, prompts=[{"steps": [{"update": _TEXT}]}],
        initialize={"sessionCapabilities": {"delete": {}, "close": {}}},
    )

    assert await run_acp_oneshot(FakeOneshotProvider.spec, "say it", system_prompt="", model="", env={}) == "PONG"
    new_ids = [c["params"]["sessionId"] for c in read_calls(calls) if c["method"] == "session/new"]
    deleted = [c["params"]["sessionId"] for c in read_calls(calls) if c["method"] == "session/delete"]
    assert new_ids and deleted == new_ids
    assert not [c for c in read_calls(calls) if c["method"] == "session/close"]


@pytest.mark.asyncio
async def test_oneshot_delete_failure_keeps_the_reply(monkeypatch, tmp_path: Path, caplog) -> None:
    _install(
        monkeypatch, tmp_path, prompts=[{"steps": [{"update": _TEXT}]}],
        initialize={"sessionCapabilities": {"delete": {}}},
        delete_error="adapter secret boom",
    )
    caplog.set_level("WARNING")

    assert await run_acp_oneshot(FakeOneshotProvider.spec, "say it", system_prompt="", model="", env={}) == "PONG"
    assert "adapter secret" not in caplog.text
