"""Claude and Codex over ACP: Claude's spec, Ciaobot's disable options on the wire, the slash
capsule and the one-shot options (#3138), and the shipped Codex spec and login command (#3139).

Options are built from a real ``CiaoConfig`` (the same methods ``build_agent_request`` calls),
and the wire tests read the scripted agent's calls log, so a guard is observed where it acts.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, get_args

import pytest
from acp.exceptions import RequestError

from ciao import provider_registry, setup_status
from ciao.claude_trim import claude_settings_layer, trim_tool_denies
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.models import AgentRequest, BridgeMode, ResultEvent, SystemStatusEvent
from ciao.provider_registry import ProviderDescriptor
from ciao.provider_service import ProviderService
from ciao.providers import oneshot
from ciao.providers.acp import agents
from ciao.providers.acp import oneshot as acp_oneshot
from ciao.providers.acp.agents import (
    CLAUDE,
    CLAUDE_ONESHOT_OPTIONS,
    CODEX,
    ClaudeAcpProvider,
    CodexAcpProvider,
    claude_session_options,
)
from ciao.providers.acp.connection import AcpConnection
from ciao.providers.acp.permissions import mode_choices, pick_value
from ciao.providers.acp.provider import AcpProvider
from ciao.providers.acp.spec import AgentSpec
from ciao.providers.base import native_slash_prompt
from tests.acp_helpers import read_calls, write_scenario

# The scripted agent that stands in for claude-agent-acp; installed per test.
FAKE_SPEC: AgentSpec | None = None

_TEXT = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}
_SCHEDULING = ("CronCreate", "CronDelete", "CronList", "ScheduleWakeup", "RemoteTrigger")


class ClaudeOnFake(ClaudeAcpProvider):
    """The Claude provider, with its spec replaced by the scripted agent's."""

    spec: ClassVar[AgentSpec]


def _install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **scenario: Any) -> tuple[SimpleNamespace, Path]:
    spec, calls = write_scenario(tmp_path, **scenario)
    monkeypatch.setattr(sys.modules[__name__], "FAKE_SPEC", spec)
    monkeypatch.setattr(ClaudeOnFake, "spec", spec, raising=False)
    # The user's CLI is not the test's concern: the spawn env is checked on its own below.
    monkeypatch.setattr(setup_status, "claude_cli_path", lambda: "")
    descriptor = ProviderDescriptor(
        id="fake", label="Fake agent", short_label="Fake", cli_label="Fake",
        factory_path="tests.test_acp_agents:ClaudeOnFake",
        acp_spec_path="tests.test_acp_agents:FAKE_SPEC",
    )
    monkeypatch.setattr(provider_registry, "_DESCRIPTORS", (descriptor,))
    monkeypatch.setattr(provider_registry, "_BY_ID", {"fake": descriptor})
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    config = SimpleNamespace(workspace_root=workspace, state_path=tmp_path / "runtime" / "state.json")
    return config, calls


def _config(tmp_path: Path) -> CiaoConfig:
    config = CiaoConfig.from_env({
        "PWA_AUTH_TOKEN": "t",
        "CIAO_WORKSPACE": str(tmp_path),
        "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
    })
    config.workspaces["work"] = WorkspaceConfig(name="work", vault_root=str(tmp_path / "work"))
    return config


def _chat_request(config: CiaoConfig, workspace: str = "work", **fields: Any) -> AgentRequest:
    """A Claude request carrying the disable options the way ``build_agent_request`` sets them."""
    values: dict[str, Any] = {
        "prompt": "hello",
        "model": "opus",
        "provider": "claude",
        "mode": "auto",
        "disallowed_tools": config.disallowed_tools_for_workspace(workspace),
        "claude_settings": config.claude_settings_for_workspace(workspace),
    }
    values.update(fields)
    return AgentRequest(**values)


def _request(prompt: str = "hello", **fields: Any) -> AgentRequest:
    return AgentRequest(prompt=prompt, model="", mode="auto", provider="fake", **fields)


async def _turn(service: ProviderService, request: AgentRequest) -> list[Any]:
    async def collect() -> list[Any]:
        return [event async for event in service.execute_streaming(request)]

    return await asyncio.wait_for(collect(), timeout=10)


def _methods(calls: Path, method: str) -> list[dict[str, Any]]:
    return [call["params"] for call in read_calls(calls) if call["method"] == method]


def _count(calls: Path, method: str) -> int:
    return len(_methods(calls, method))


def _text_turn(text: str) -> dict[str, Any]:
    return {"steps": [{"update": {**_TEXT, "content": {"type": "text", "text": text}}}]}


# -- spec and modes ----------------------------------------------------------------------


def test_claude_mode_table_covers_every_bridge_mode() -> None:
    assert set(CLAUDE.modes) == {"normal", "plan", "auto", "bypass"}


def test_claude_auto_falls_back_to_accept_edits() -> None:
    choice = mode_choices(CLAUDE, "auto")[0]
    assert pick_value(choice, ["default", "acceptEdits"]) == "acceptEdits"
    assert pick_value(choice, ["auto", "acceptEdits"]) == "auto"


def test_claude_spec_env_disables_auto_memory_and_artifact() -> None:
    assert dict(CLAUDE.env) == {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "CLAUDE_CODE_DISABLE_ARTIFACT": "1"}


def test_claude_executable_env_points_at_user_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup_status, "claude_cli_path", lambda: "/opt/bin/claude")
    assert ClaudeAcpProvider.spawn_env() == {"CLAUDE_CODE_EXECUTABLE": "/opt/bin/claude"}
    monkeypatch.setattr(setup_status, "claude_cli_path", lambda: "")
    assert ClaudeAcpProvider.spawn_env() == {}


# -- disable options (parity rows 1-8) -------------------------------------------------


def test_claude_meta_carries_trim_tool_denies_with_locked_scheduling(tmp_path: Path) -> None:
    options = claude_session_options(_chat_request(_config(tmp_path)))

    denies = options["disallowedTools"]
    assert denies[: len(trim_tool_denies({}))] == trim_tool_denies({})
    for name in _SCHEDULING:
        assert name in denies


def test_claude_meta_appends_workspace_extra_denies_after_trim(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.workspaces["work"].disallowed_tools = ["mcp__x", "NotATrimTool"]

    denies = claude_session_options(_chat_request(config))["disallowedTools"]

    trim = trim_tool_denies({})
    assert denies[: len(trim)] == trim
    assert denies[len(trim):] == ["mcp__x", "NotATrimTool"]


def test_claude_meta_settings_is_an_object_not_a_string(tmp_path: Path) -> None:
    settings = claude_session_options(_chat_request(_config(tmp_path)))["settings"]

    assert isinstance(settings, dict)
    assert "skillOverrides" in settings


@pytest.mark.parametrize("overrides", [{}, {"setting:disableWorkflows": False}])
def test_claude_meta_settings_layer_matches_trim_catalog(tmp_path: Path, overrides: dict[str, bool]) -> None:
    config = _config(tmp_path)
    config.workspaces["work"].claude_trim = overrides

    settings = claude_session_options(_chat_request(config))["settings"]

    assert settings == claude_settings_layer(overrides)


def test_claude_meta_env_only_carries_connectors_off(tmp_path: Path) -> None:
    config = _config(tmp_path)

    off = claude_session_options(_chat_request(config, extra_env={"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}))
    on = claude_session_options(_chat_request(config))

    assert off["env"] == {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}
    assert "env" not in on


def test_claude_meta_has_no_credential_or_memory_pass_denies(tmp_path: Path) -> None:
    denies = claude_session_options(_chat_request(_config(tmp_path)))["disallowedTools"]

    for entry in denies:
        for forbidden in (".env", ".runtime", "secrets", "mcp__"):
            assert forbidden not in entry


def test_claude_oneshot_meta_disables_tools_skills_settings_mcp() -> None:
    assert ClaudeAcpProvider.oneshot_meta() == {
        "claudeCode": {"options": {"tools": [], "skills": [], "settingSources": [], "strictMcpConfig": True}}
    }
    assert dict(CLAUDE_ONESHOT_OPTIONS) == ClaudeAcpProvider.oneshot_meta()["claudeCode"]["options"]


@pytest.mark.parametrize("disallowed", [[], ["Bash", "mcp__x"]])
def test_codex_and_opencode_session_meta_is_empty(disallowed: list[str]) -> None:
    # The base hook: an agent with no disable options of its own sends no _meta.
    request = AgentRequest(prompt="hi", model="m", mode="auto", provider="other", disallowed_tools=disallowed)

    assert AcpProvider.session_meta(None, request) == {}  # type: ignore[arg-type]


# -- one-shot through the registry -----------------------------------------------------


@pytest.mark.asyncio
async def test_claude_oneshot_runs_the_acp_agent_with_no_tools_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_run(spec: AgentSpec, prompt: str, **kwargs: Any) -> str:
        seen.update(spec=spec, prompt=prompt, **kwargs)
        return "answer"

    monkeypatch.setattr(acp_oneshot, "run_acp_oneshot", fake_run)
    monkeypatch.setattr(setup_status, "claude_cli_path", lambda: "/opt/bin/claude")

    result = await oneshot.run_oneshot("hi", system_prompt="sys", model="opus[1m]")

    assert result == "answer"
    assert seen["spec"] is CLAUDE
    assert seen["model"] == "opus"  # the fast-mode suffix is dropped for one-shots
    assert seen["session_meta"] == ClaudeAcpProvider.oneshot_meta()
    assert seen["env"]["CLAUDE_CODE_EXECUTABLE"] == "/opt/bin/claude"


# -- the slash capsule (parity row 14) -------------------------------------------------


def test_native_slash_prompt_splits_only_the_recorded_capsule() -> None:
    capsule = "[ctx]\n"

    split = AgentRequest(prompt=capsule + "/loop 5m check", model="m", mode="auto", injected_context_prefix=capsule)
    assert native_slash_prompt(split) == ("/loop 5m check", capsule)

    plain = AgentRequest(prompt=capsule + "hello", model="m", mode="auto", injected_context_prefix=capsule)
    assert native_slash_prompt(plain) is None

    # Capsule-shaped text the user typed, with no recorded prefix, stays user text.
    typed = AgentRequest(prompt="[ctx]\n/loop 5m", model="m", mode="auto", injected_context_prefix="")
    assert native_slash_prompt(typed) is None
    # Only the recorded prefix is cut: a slash command found after another cut is not a command.
    misaligned = AgentRequest(prompt="/b /loop", model="m", mode="auto", injected_context_prefix="/a\n")
    assert native_slash_prompt(misaligned) is None


def _provider_for_blocks(tmp_path: Path) -> ClaudeAcpProvider:
    return ClaudeAcpProvider(tmp_path)


def test_slash_prompt_sends_capsule_as_second_block(tmp_path: Path) -> None:
    capsule = "[ctx]\nworkspace=work\n"
    request = AgentRequest(
        prompt=capsule + "/loop 5m", model="m", mode="auto", provider="claude", injected_context_prefix=capsule,
    )
    blocks = _provider_for_blocks(tmp_path)._blocks(SimpleNamespace(agent_capabilities={}), request)

    assert blocks == [{"type": "text", "text": "/loop 5m"}, {"type": "text", "text": capsule}]


def test_slash_capsule_is_an_embedded_resource_when_the_agent_takes_one(tmp_path: Path) -> None:
    capsule = "[ctx]\n"
    request = AgentRequest(
        prompt=capsule + "/compact-now", model="m", mode="auto", provider="claude", injected_context_prefix=capsule,
    )
    caps = {"promptCapabilities": {"embeddedContext": True}}
    blocks = _provider_for_blocks(tmp_path)._blocks(SimpleNamespace(agent_capabilities=caps), request)

    assert blocks[0] == {"type": "text", "text": "/compact-now"}
    assert blocks[1] == {
        "type": "resource",
        "resource": {"uri": "ciaobot://context", "mimeType": "text/plain", "text": capsule},
    }


def test_plain_prompt_keeps_the_capsule_in_one_block(tmp_path: Path) -> None:
    capsule = "[ctx]\n"
    request = AgentRequest(
        prompt=capsule + "hello", model="m", mode="auto", provider="claude", injected_context_prefix=capsule,
    )
    blocks = _provider_for_blocks(tmp_path)._blocks(SimpleNamespace(agent_capabilities={}), request)

    assert blocks == [{"type": "text", "text": capsule + "hello"}]


@pytest.mark.asyncio
async def test_slash_turn_reaches_the_agent_as_two_blocks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config, calls = _install(
        monkeypatch, tmp_path, sessions=["a"], prompts=[_text_turn("ok")], spec_slash_capsule_as_block=True,
    )
    service = ProviderService(config)  # type: ignore[arg-type]
    capsule = "[ctx]\n"

    await _turn(service, AgentRequest(
        prompt=capsule + "/loop 5m", model="", mode="auto", provider="fake", injected_context_prefix=capsule,
    ))

    prompt = _methods(calls, "session/prompt")[-1]["prompt"]
    assert [block["text"] for block in prompt] == ["/loop 5m", capsule]


# -- wire: the options reach the agent, and a change reloads the same session (rows 1-8) ----


@pytest.mark.asyncio
async def test_spawn_env_carries_the_user_cli_into_the_agent_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, _calls = _install(monkeypatch, tmp_path, sessions=["a"])
    monkeypatch.setattr(setup_status, "claude_cli_path", lambda: "/opt/bin/claude")
    spawned: dict[str, str] = {}
    original = AcpConnection.open

    async def recording(spec: AgentSpec, *, cwd: Path, env: dict[str, str], client: Any) -> Any:
        spawned.update(env)
        return await original(spec, cwd=cwd, env=env, client=client)

    monkeypatch.setattr(AcpConnection, "open", staticmethod(recording))
    service = ProviderService(config)  # type: ignore[arg-type]
    request = _chat_request(_config(tmp_path))
    request.provider = "fake"

    await _turn(service, request)

    assert spawned["CLAUDE_CODE_EXECUTABLE"] == "/opt/bin/claude"


@pytest.mark.asyncio
async def test_claude_meta_on_session_new_matches_the_request(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config, calls = _install(monkeypatch, tmp_path, sessions=["a"])
    service = ProviderService(config)  # type: ignore[arg-type]
    request = _chat_request(_config(tmp_path), extra_env={"ENABLE_CLAUDEAI_MCP_SERVERS": "false"})
    request.provider = "fake"

    await _turn(service, request)

    new = _methods(calls, "session/new")[0]
    assert new["_meta"] == {"claudeCode": {"options": claude_session_options(request)}}
    assert new["_meta"]["claudeCode"]["options"]["env"] == {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}


@pytest.mark.asyncio
async def test_claude_meta_sent_on_load_and_resume_too(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config, calls = _install(monkeypatch, tmp_path, sessions=["a"], initialize={"loadSession": True})
    base = _config(tmp_path)
    first = _chat_request(base)
    first.provider = "fake"
    await _turn(ProviderService(config), first)  # type: ignore[arg-type]

    # A new host (a restarted engine) resumes the chat: a new connection loads the saved session.
    resumed = _chat_request(base, resume_session="a")
    resumed.provider = "fake"
    await _turn(ProviderService(config), resumed)  # type: ignore[arg-type]

    loads = _methods(calls, "session/load")
    assert len(loads) == 1
    assert loads[0]["sessionId"] == "a"
    assert loads[0]["_meta"] == {"claudeCode": {"options": claude_session_options(resumed)}}


@pytest.mark.asyncio
async def test_trim_change_reloads_session_with_new_meta(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="ciao.providers.acp.provider")
    config, calls = _install(monkeypatch, tmp_path, sessions=["a"], initialize={"loadSession": True})
    service = ProviderService(config)  # type: ignore[arg-type]
    base = _config(tmp_path)

    first = _chat_request(base)
    first.provider = "fake"
    await _turn(service, first)
    base.workspaces["work"].disallowed_tools = ["mcp__x"]
    second = _chat_request(base, resume_session="a")
    second.provider = "fake"
    await _turn(service, second)

    assert _count(calls, "initialize") == 1  # same connection
    assert _count(calls, "session/new") == 1  # same session, never a new one
    loads = _methods(calls, "session/load")
    assert len(loads) == 1
    assert loads[0]["sessionId"] == "a"
    assert loads[0]["_meta"] == {"claudeCode": {"options": claude_session_options(second)}}
    assert loads[0]["_meta"]["claudeCode"]["options"]["disallowedTools"][-1] == "mcp__x"
    assert service.current_session_id == "a"
    assert "Reloading fake session a: options changed" in caplog.text


@pytest.mark.asyncio
async def test_trim_change_waits_for_late_turn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config, calls = _install(monkeypatch, tmp_path, sessions=["a"], initialize={"loadSession": True},
                             spec_quiet_period_s=30)
    service = ProviderService(config)  # type: ignore[arg-type]
    base = _config(tmp_path)

    first = _chat_request(base)
    first.provider = "fake"
    await _turn(service, first)
    provider = service.provider
    assert provider is not None
    session = provider._session  # the open session; a background update starts a late turn
    assert session is not None
    session.on_update({
        "sessionId": "a", "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": "still working"},
    })
    assert provider.background_work_outstanding is True

    base.workspaces["work"].disallowed_tools = ["mcp__x"]
    second = _chat_request(base, resume_session="a")
    second.provider = "fake"
    await _turn(service, second)

    # The change waits: reloading now would close the late turn that is still running.
    assert _count(calls, "session/load") == 0
    assert _count(calls, "session/new") == 1


_MODEL_OPTION = {
    "id": "model", "name": "Model", "category": "model", "type": "select",
    "currentValue": "claude-sonnet-4-6",
    "options": [{"value": "claude-sonnet-4-6", "name": "Sonnet"}, {"value": "claude-opus-4-8", "name": "Opus"}],
}


@pytest.mark.asyncio
async def test_tier_alias_sets_the_offered_model_it_names(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config, calls = _install(monkeypatch, tmp_path, sessions=["a"], config_options=[_MODEL_OPTION])
    service = ProviderService(config)  # type: ignore[arg-type]
    request = _chat_request(_config(tmp_path))
    request.provider = "fake"

    await _turn(service, request)

    sets = [call for call in read_calls(calls) if call["method"] == "session/set_config_option"]
    assert [(call["params"]["configId"], call["params"]["value"]) for call in sets] == [
        ("model", "claude-opus-4-8"),
    ]


@pytest.mark.asyncio
async def test_unknown_model_value_is_not_sent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config, calls = _install(monkeypatch, tmp_path, sessions=["a"], config_options=[_MODEL_OPTION])
    service = ProviderService(config)  # type: ignore[arg-type]
    request = _chat_request(_config(tmp_path), model="gpt-5.6")
    request.provider = "fake"

    events = await _turn(service, request)

    assert [call for call in read_calls(calls) if call["method"] == "session/set_config_option"] == []
    assert any(
        isinstance(event, SystemStatusEvent) and "does not offer the model 'gpt-5.6'" in event.status
        for event in events
    )


# -- fail-closed mode, connectors, credentials, delete (review fixes on C1) ------------------

_MODE_OPTION_DEFAULT = {
    "id": "mode", "name": "Mode", "category": "mode", "type": "select", "currentValue": "default",
    "options": [{"value": "default", "name": "Default"}, {"value": "bypassPermissions", "name": "Bypass"}],
}
_MODE_BYPASS_ONLY = {
    "id": "mode", "name": "Mode", "category": "mode", "type": "select", "currentValue": "bypassPermissions",
    "options": [{"value": "bypassPermissions", "name": "Bypass"}],
}


def _record_spawns(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    spawned: list[dict[str, str]] = []
    original = AcpConnection.open

    async def recording(spec: AgentSpec, *, cwd: Path, env: dict[str, str], client: Any) -> Any:
        spawned.append(dict(env))
        return await original(spec, cwd=cwd, env=env, client=client)

    monkeypatch.setattr(AcpConnection, "open", staticmethod(recording))
    return spawned


@pytest.mark.asyncio
async def test_failed_switch_to_a_strict_mode_ends_the_turn_before_the_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A chat moved from bypass to normal must not run in bypass when the switch fails."""
    config, calls = _install(monkeypatch, tmp_path, sessions=["a"], spec_modes=CLAUDE.modes, config_options=[_MODE_OPTION_DEFAULT])
    service = ProviderService(config)  # type: ignore[arg-type]
    first = _chat_request(_config(tmp_path), mode="bypass")
    first.provider = "fake"
    await _turn(service, first)
    assert _count(calls, "session/prompt") == 1

    async def refuse(config_id: str, session_id: str, value: str, **_kwargs: Any) -> None:
        raise RequestError(-32602, "adapter detail /Users/alice/settings.json")

    provider = service.provider
    assert provider is not None
    monkeypatch.setattr(provider._connection.conn, "set_config_option", refuse)  # type: ignore[union-attr]
    # Same session: a resume id keeps the live (warm) session, without which a turn opens a new one.
    second = _chat_request(_config(tmp_path), mode="normal", resume_session="a")
    second.provider = "fake"
    events = await _turn(service, second)

    results = [event for event in events if isinstance(event, ResultEvent)]
    assert [(r.is_error, r.result) for r in results] == [
        (True, "fake could not set the requested mode; the turn did not run."),
    ]
    assert "alice" not in repr(events)
    assert "alice" not in caplog.text  # the abort is logged with a fixed sentence, not adapter text
    assert _count(calls, "session/prompt") == 1  # nothing ran after the failed switch


@pytest.mark.asyncio
async def test_fresh_session_without_the_strict_mode_value_ends_the_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, calls = _install(monkeypatch, tmp_path, sessions=["a"], spec_modes=CLAUDE.modes, config_options=[_MODE_BYPASS_ONLY])
    service = ProviderService(config)  # type: ignore[arg-type]
    request = _chat_request(_config(tmp_path), mode="normal")
    request.provider = "fake"

    events = await _turn(service, request)

    assert [(e.is_error, e.result) for e in events if isinstance(e, ResultEvent)] == [
        (True, "fake could not set the requested mode; the turn did not run."),
    ]
    assert _count(calls, "session/prompt") == 0


@pytest.mark.asyncio
async def test_bypass_switch_failure_is_status_only_and_the_turn_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, calls = _install(
        monkeypatch, tmp_path, sessions=["a"], spec_modes=CLAUDE.modes, config_options=[_MODE_OPTION_DEFAULT],
        config_error="adapter detail /Users/alice/settings.json",
    )
    service = ProviderService(config)  # type: ignore[arg-type]
    request = _chat_request(_config(tmp_path), mode="bypass")
    request.provider = "fake"

    events = await _turn(service, request)

    assert SystemStatusEvent(type="system", status="fake could not set mode.") in events
    assert [e for e in events if isinstance(e, ResultEvent) and e.is_error] == []
    assert _count(calls, "session/prompt") == 1


@pytest.mark.asyncio
async def test_connectors_switched_back_on_reload_the_session_without_a_process_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ENABLE_CLAUDEAI_MCP_SERVERS", raising=False)
    config, calls = _install(
        monkeypatch, tmp_path, sessions=["a"], initialize={"loadSession": True},
        record_env=["ENABLE_CLAUDEAI_MCP_SERVERS"],
    )
    spawned = _record_spawns(monkeypatch)
    service = ProviderService(config)  # type: ignore[arg-type]
    base = _config(tmp_path)

    off = _chat_request(base, extra_env={"ENABLE_CLAUDEAI_MCP_SERVERS": "false"})
    off.provider = "fake"
    await _turn(service, off)
    assert _methods(calls, "session/new")[0]["_meta"]["claudeCode"]["options"]["env"] == {
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    }

    base.workspaces["work"].claude_trim = {"setting:claude_ai_connectors": True}
    on = _chat_request(base, resume_session="a")
    on.provider = "fake"
    await _turn(service, on)

    # Same connection, same session, reloaded with the connectors' env dropped from _meta.
    assert _count(calls, "initialize") == 1
    assert _count(calls, "session/new") == 1
    loads = _methods(calls, "session/load")
    assert len(loads) == 1
    assert "env" not in loads[0]["_meta"]["claudeCode"]["options"]
    assert loads[0]["_meta"] == {"claudeCode": {"options": claude_session_options(on)}}
    # The variable never reached the process: not in the spawn env, not in the agent's own env.
    assert spawned and all("ENABLE_CLAUDEAI_MCP_SERVERS" not in env for env in spawned)
    initialize = _methods(calls, "initialize")[0]
    assert initialize["env"]["ENABLE_CLAUDEAI_MCP_SERVERS"] is None


_ANTHROPIC_KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
_OTHER_SECRET_KEYS = ("OPENAI_API_KEY", "GITHUB_TOKEN")


def test_spawn_env_allowlists_exactly_the_anthropic_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(setup_status, "claude_cli_path", lambda: "")
    for key in _ANTHROPIC_KEYS:
        monkeypatch.setenv(key, f"value-for-{key.lower()}")
    for key in _OTHER_SECRET_KEYS:
        monkeypatch.setenv(key, f"value-for-{key.lower()}")

    env = ClaudeAcpProvider.spawn_env()

    assert {key: env[key] for key in _ANTHROPIC_KEYS} == {
        key: f"value-for-{key.lower()}" for key in _ANTHROPIC_KEYS
    }
    for key in _OTHER_SECRET_KEYS:
        assert key not in env


def test_spawn_env_leaves_unset_anthropic_credentials_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup_status, "claude_cli_path", lambda: "")
    for key in _ANTHROPIC_KEYS:
        monkeypatch.delenv(key, raising=False)

    assert ClaudeAcpProvider.spawn_env() == {}


@pytest.mark.asyncio
async def test_anthropic_credentials_reach_the_agent_process_and_other_secrets_do_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for key in _ANTHROPIC_KEYS:
        monkeypatch.setenv(key, f"value-for-{key.lower()}")
    for key in _OTHER_SECRET_KEYS:
        monkeypatch.setenv(key, f"value-for-{key.lower()}")
    config, calls = _install(monkeypatch, tmp_path, sessions=["a"], record_env=[*_ANTHROPIC_KEYS, *_OTHER_SECRET_KEYS])
    request = _chat_request(_config(tmp_path))
    request.provider = "fake"

    await _turn(ProviderService(config), request)  # type: ignore[arg-type]

    env = _methods(calls, "initialize")[0]["env"]
    assert {key: env[key] for key in _ANTHROPIC_KEYS} == {
        key: f"value-for-{key.lower()}" for key in _ANTHROPIC_KEYS
    }
    assert {key: env[key] for key in _OTHER_SECRET_KEYS} == {key: None for key in _OTHER_SECRET_KEYS}


@pytest.mark.asyncio
async def test_delete_session_reports_that_the_journal_was_deleted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from ciao.providers.acp.session import journal_file
    from ciao.providers.claude import ClaudeProvider

    runtime = tmp_path / "runtime"
    journal = journal_file(runtime, CLAUDE.id, "sess-1")
    journal.parent.mkdir(parents=True)
    journal.write_text("{}\n", encoding="utf-8")

    async def sdk_nothing(cls: Any, _workspace: Path, _session_id: str, *, runtime_root: Path) -> None:
        return None

    monkeypatch.setattr(ClaudeProvider, "delete_session", classmethod(sdk_nothing))
    assert await ClaudeAcpProvider.delete_session(tmp_path, "sess-1", runtime_root=runtime) is True
    assert not journal.exists()
    assert await ClaudeAcpProvider.delete_session(tmp_path, "sess-1", runtime_root=runtime) is None


def test_codex_mode_table_covers_every_bridge_mode() -> None:
    assert set(CODEX.modes) == set(get_args(BridgeMode))
    assert all(choices for choices in CODEX.modes.values())


def test_codex_spec_names_its_adapter_and_config_ids() -> None:
    assert CODEX.id == "codex"
    assert CODEX.binary == "codex-acp"
    assert CODEX.model_config_id == "model"
    assert CODEX.thought_config_id == "reasoning_effort"
    assert CodexAcpProvider.spec is CODEX


def test_codex_auth_command_builds_login_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agents, "codex_binary", lambda: "/opt/tools/codex")
    assert agents.codex_auth_command() == ["/opt/tools/codex", "login"]
    assert agents.codex_auth_command(device_auth=True) == [
        "/opt/tools/codex",
        "login",
        "--device-auth",
    ]


def test_codex_auth_command_raises_when_codex_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agents, "codex_binary", lambda: None)
    with pytest.raises(FileNotFoundError):
        agents.codex_auth_command()
