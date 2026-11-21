"""Claude and Codex over ACP: Claude's spec, Ciaobot's disable options on the wire, the slash
capsule and the one-shot options (#3138), and the shipped Codex spec and login command (#3139).

Options are built from a real ``CiaoConfig`` (the same methods ``build_agent_request`` calls),
and the wire tests read the scripted agent's calls log, so a guard is observed where it acts.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
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
from ciao.models import (
    AgentRequest,
    AssistantTextDelta,
    BridgeMode,
    ResultEvent,
    SystemStatusEvent,
    ToolUseEvent,
)
from ciao.provider_registry import ProviderDescriptor
from ciao.provider_service import ProviderService
from ciao.providers import oneshot
from ciao.providers.acp import agents
from ciao.providers.acp import oneshot as acp_oneshot
from ciao.providers.acp.agents import (
    CLAUDE,
    CLAUDE_ONESHOT_OPTIONS,
    CODEX,
    OPENCODE,
    ClaudeAcpProvider,
    CodexAcpProvider,
    OpencodeAcpProvider,
    claude_session_options,
)
from ciao.providers.acp.connection import AcpConnection
from ciao.providers.acp.normalize import UpdateNormalizer
from ciao.providers.acp.permissions import PermissionBroker, mode_choices, pick_value
from ciao.providers.acp.provider import AcpProvider, AgentClient
from ciao.providers.acp.spec import AgentSpec, ModeChoice
from ciao.providers.base import QuestionResponseResult, native_slash_prompt
from tests.acp_helpers import read_calls, write_scenario

# The scripted agent that stands in for claude-agent-acp; installed per test.
FAKE_SPEC: AgentSpec | None = None

_TEXT = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}
_SCHEDULING = ("RemoteTrigger",)
_LOOP_FAMILY = ("CronCreate", "CronDelete", "CronList", "ScheduleWakeup")


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
    assert dict(CLAUDE.env) == {
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_ARTIFACT": "1",
        "CLAUDE_CODE_DISABLE_CRON": "1",
    }


def _user_cli(tmp_path: Path) -> str:
    """A real executable file, never run: the env passes only an absolute, executable CLI."""
    path = tmp_path / "bin" / "claude"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def test_claude_executable_env_points_at_user_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cli = _user_cli(tmp_path)
    monkeypatch.setattr(setup_status, "claude_cli_path", lambda: cli)
    assert ClaudeAcpProvider.spawn_env() == {"CLAUDE_CODE_EXECUTABLE": cli}
    monkeypatch.setattr(setup_status, "claude_cli_path", lambda: "")
    assert ClaudeAcpProvider.spawn_env() == {}


# -- disable options (parity rows 1-8) -------------------------------------------------


def test_claude_meta_carries_every_loop_family_deny_with_no_switch(tmp_path: Path) -> None:
    config = _config(tmp_path)
    # A stored map that names the removed loop switches cannot un-deny them.
    config.workspaces["work"].claude_trim = {"tool:CronCreate": False, "skill:loop": False}

    options = claude_session_options(_chat_request(config))

    denies = options["disallowedTools"]
    assert denies[: len(trim_tool_denies({}))] == trim_tool_denies({})
    for name in (*_LOOP_FAMILY, "Skill(loop)"):
        assert name in denies
    assert options["settings"]["skillOverrides"]["loop"] == "off"


def test_claude_meta_carries_trim_tool_denies_for_routines(tmp_path: Path) -> None:
    denies = claude_session_options(_chat_request(_config(tmp_path)))["disallowedTools"]

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


def test_claude_meta_pre_approves_no_ciao_commands(tmp_path: Path) -> None:
    """No `ciao …` shell prefix and no ciaobot MCP tool is pre-approved: an allow prefix is a
    shell-suffix bypass risk, so auto mode keeps a card on every ciao shell command."""
    options = claude_session_options(_chat_request(_config(tmp_path)))
    serialized = json.dumps(options)

    assert "allowedTools" not in options
    assert "allow" not in options.get("settings", {})
    assert "Bash(ciao" not in serialized
    assert "mcp__ciaobot" not in serialized


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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_run(spec: AgentSpec, prompt: str, **kwargs: Any) -> str:
        seen.update(spec=spec, prompt=prompt, **kwargs)
        return "answer"

    monkeypatch.setattr(acp_oneshot, "run_acp_oneshot", fake_run)
    cli = _user_cli(tmp_path)
    monkeypatch.setattr(setup_status, "claude_cli_path", lambda: cli)

    result = await oneshot.run_oneshot("hi", system_prompt="sys", model="opus[1m]")

    assert result == "answer"
    assert seen["spec"] is CLAUDE
    assert seen["model"] == "opus"  # the fast-mode suffix is dropped for one-shots
    assert seen["session_meta"] == ClaudeAcpProvider.oneshot_meta()
    assert seen["env"]["CLAUDE_CODE_EXECUTABLE"] == cli


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
    cli = _user_cli(tmp_path)
    monkeypatch.setattr(setup_status, "claude_cli_path", lambda: cli)
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

    assert spawned["CLAUDE_CODE_EXECUTABLE"] == cli


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


_PROVIDER_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN")


def test_spawn_env_is_the_user_cli_and_no_credential_filter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cli = _user_cli(tmp_path)
    monkeypatch.setattr(setup_status, "claude_cli_path", lambda: cli)
    for key in _PROVIDER_KEYS:
        monkeypatch.setenv(key, f"value-for-{key.lower()}")

    # Credentials reach the agent through the inherited engine env, not through spawn_env.
    assert ClaudeAcpProvider.spawn_env() == {"CLAUDE_CODE_EXECUTABLE": cli}


@pytest.mark.asyncio
async def test_agent_process_inherits_provider_credentials_and_not_ciao_internals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for key in _PROVIDER_KEYS:
        monkeypatch.setenv(key, f"value-for-{key.lower()}")
    monkeypatch.setenv("CIAO_ENGINE_SECRET", "engine-internal")
    monkeypatch.setenv("PWA_AUTH_TOKEN", "pwa-login-token")
    config, calls = _install(
        monkeypatch, tmp_path, sessions=["a"],
        record_env=[*_PROVIDER_KEYS, "CIAO_ENGINE_SECRET", "PWA_AUTH_TOKEN", "CIAO_AGENT_URL", "CIAO_AGENT_TOKEN"],
    )
    request = _chat_request(
        _config(tmp_path),
        extra_env={"CIAO_AGENT_URL": "http://127.0.0.1:9/agent", "CIAO_AGENT_TOKEN": "turn-bearer"},
    )
    request.provider = "fake"

    await _turn(ProviderService(config), request)  # type: ignore[arg-type]

    env = _methods(calls, "initialize")[0]["env"]
    # (a) provider credentials set in the engine's environment reach the adapter process
    assert {key: env[key] for key in _PROVIDER_KEYS} == {
        key: f"value-for-{key.lower()}" for key in _PROVIDER_KEYS
    }
    # (b) Ciaobot's own internals from os.environ do not; only what extra_env passes does
    assert env["CIAO_ENGINE_SECRET"] is None
    assert env["PWA_AUTH_TOKEN"] is None
    assert env["CIAO_AGENT_URL"] == "http://127.0.0.1:9/agent"
    assert env["CIAO_AGENT_TOKEN"] == "turn-bearer"


@pytest.mark.asyncio
async def test_engine_connectors_env_is_withheld_from_the_claude_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ENABLE_CLAUDEAI_MCP_SERVERS", "true")
    config, calls = _install(monkeypatch, tmp_path, sessions=["a"], record_env=["ENABLE_CLAUDEAI_MCP_SERVERS"])
    # The fake agent takes Claude's spec policy, so this checks CLAUDE.unset_env end to end.
    monkeypatch.setattr(ClaudeOnFake, "spec", dataclasses.replace(ClaudeOnFake.spec, unset_env=CLAUDE.unset_env))
    request = _chat_request(_config(tmp_path))
    request.provider = "fake"

    await _turn(ProviderService(config), request)  # type: ignore[arg-type]

    assert _methods(calls, "initialize")[0]["env"]["ENABLE_CLAUDEAI_MCP_SERVERS"] is None


SESSION = "11111111-2222-4333-8444-555555555599"


@pytest.fixture
def cli_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The Claude CLI's own session file for SESSION, under a temporary config dir."""
    from ciao import claude_sessions

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    key = claude_sessions.project_key_for_directory(str(workspace))
    project = tmp_path / "claude-config" / "projects" / key
    project.mkdir(parents=True)
    path = project / f"{SESSION}.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_delete_session_removes_journal_and_cli_file(
    tmp_path: Path, cli_session: Path
) -> None:
    from ciao.providers.acp.session import journal_file

    runtime = tmp_path / "runtime"
    journal = journal_file(runtime, CLAUDE.id, SESSION)
    journal.parent.mkdir(parents=True)
    journal.write_text("{}\n", encoding="utf-8")
    workspace = tmp_path / "ws"

    assert await ClaudeAcpProvider.delete_session(workspace, SESSION, runtime_root=runtime) is True
    assert not journal.exists()
    assert not cli_session.exists()
    assert await ClaudeAcpProvider.delete_session(workspace, SESSION, runtime_root=runtime) is None


@pytest.mark.asyncio
async def test_delete_session_counts_a_cli_file_alone(tmp_path: Path, cli_session: Path) -> None:
    runtime = tmp_path / "runtime"
    assert await ClaudeAcpProvider.delete_session(tmp_path / "ws", SESSION, runtime_root=runtime) is True
    assert not cli_session.exists()


class _FakeOpencodeConnection:
    """Stands in for ``opencode acp``: records the cleanup calls and advertises ``delete``."""

    def __init__(self, calls: list[str], *, delete_advertised: bool = True, delete_error=None) -> None:
        self.calls = calls
        self.agent_capabilities = {"sessionCapabilities": {"delete": {}}} if delete_advertised else {}
        self._delete_error = delete_error
        self.conn = self

    async def delete_session(self, *, session_id: str) -> None:
        self.calls.append(f"session/delete:{session_id}")
        if self._delete_error is not None:
            raise self._delete_error

    async def close(self) -> None:
        self.calls.append("close")


def _fake_opencode_open(monkeypatch: pytest.MonkeyPatch, calls: list[str], connection=None, error=None):
    async def open_fake(spec, *, cwd, env, client):
        calls.append(f"open:{spec.id}:{cwd.name}")
        if error is not None:
            raise error
        return connection

    monkeypatch.setattr(agents.AcpConnection, "open", open_fake)


@pytest.mark.asyncio
async def test_opencode_delete_session_asks_the_agent_to_delete_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """opencode keeps the session in its own store: the agent deletes it, not only the journal."""
    from ciao.providers.acp.session import journal_file

    runtime = tmp_path / "runtime"
    journal = journal_file(runtime, OPENCODE.id, SESSION)
    journal.parent.mkdir(parents=True)
    journal.write_text("{}\n", encoding="utf-8")
    calls: list[str] = []
    _fake_opencode_open(monkeypatch, calls, _FakeOpencodeConnection(calls))

    result = await OpencodeAcpProvider.delete_session(tmp_path / "ws", SESSION, runtime_root=runtime)

    assert result is True
    assert not journal.exists()
    assert calls == ["open:opencode:ws", f"session/delete:{SESSION}", "close"]


def _write_opencode_journal(runtime: Path) -> Path:
    from ciao.providers.acp.session import journal_file

    journal = journal_file(runtime, OPENCODE.id, SESSION)
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("{}\n", encoding="utf-8")
    return journal


@pytest.mark.asyncio
async def test_opencode_delete_session_without_the_capability_only_drops_the_journal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = tmp_path / "rt"
    journal = _write_opencode_journal(runtime)
    calls: list[str] = []
    _fake_opencode_open(monkeypatch, calls, _FakeOpencodeConnection(calls, delete_advertised=False))

    assert await OpencodeAcpProvider.delete_session(tmp_path / "ws", SESSION, runtime_root=runtime) is True
    assert not journal.exists()
    assert calls == ["open:opencode:ws", "close"]


@pytest.mark.asyncio
async def test_opencode_delete_session_refusal_is_quiet_and_still_closes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runtime = tmp_path / "rt"
    _write_opencode_journal(runtime)
    calls: list[str] = []
    connection = _FakeOpencodeConnection(calls, delete_error=RuntimeError(f"denied {SESSION}"))
    _fake_opencode_open(monkeypatch, calls, connection)

    with caplog.at_level("WARNING", logger="ciao.providers.acp.agents"):
        result = await OpencodeAcpProvider.delete_session(tmp_path / "ws", SESSION, runtime_root=runtime)

    assert result is True  # the journal was removed
    assert calls[-1] == "close"
    assert "could not delete a session (RuntimeError)" in caplog.text
    assert SESSION not in caplog.text


@pytest.mark.asyncio
async def test_opencode_delete_session_start_failure_keeps_the_journal_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from ciao.providers.acp.connection import AgentStartError
    from ciao.providers.acp.session import journal_file

    runtime = tmp_path / "runtime"
    journal = journal_file(runtime, OPENCODE.id, SESSION)
    journal.parent.mkdir(parents=True)
    journal.write_text("{}\n", encoding="utf-8")
    calls: list[str] = []
    _fake_opencode_open(monkeypatch, calls, error=AgentStartError("opencode was not found on PATH."))

    with caplog.at_level("WARNING", logger="ciao.providers.acp.agents"):
        result = await OpencodeAcpProvider.delete_session(tmp_path / "ws", SESSION, runtime_root=runtime)

    assert result is True  # the journal was removed
    assert not journal.exists()
    assert calls == ["open:opencode:ws"]
    assert "could not start to delete a session" in caplog.text


def _opencode_on_fake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **scenario: object) -> Path:
    """Point OpenCode's cleanup at the scripted ACP agent; returns the calls-log path."""
    (tmp_path / "ws").mkdir(exist_ok=True)
    spec, calls = write_scenario(tmp_path, **scenario)
    # No version gate: the scripted agent reports no version, and the gate would probe the binary.
    fake = dataclasses.replace(OPENCODE, binary=spec.binary, args=spec.args, version_gate=None)
    monkeypatch.setattr(OpencodeAcpProvider, "spec", fake)
    return calls


_DELETE_CAPABLE = {"sessionCapabilities": {"delete": {}}}


@pytest.mark.asyncio
async def test_opencode_delete_session_gives_up_on_a_hung_session_delete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An agent that never answers session/delete cannot hold the caller: it times out and closes."""
    from ciao.providers.base import CleanupTimedOut

    monkeypatch.setattr(agents, "_DELETE_TIMEOUT_S", 3.0)
    runtime = tmp_path / "rt"
    journal = _write_opencode_journal(runtime)
    calls = _opencode_on_fake(monkeypatch, tmp_path, initialize=_DELETE_CAPABLE, delete_hang=True)
    closed: list[bool] = []
    real_close = agents.AcpConnection.close

    async def recording_close(self) -> None:
        closed.append(True)
        await real_close(self)

    monkeypatch.setattr(agents.AcpConnection, "close", recording_close)

    with caplog.at_level("WARNING", logger="ciao.providers.acp.agents"):
        with pytest.raises(CleanupTimedOut):
            await asyncio.wait_for(
                OpencodeAcpProvider.delete_session(tmp_path / "ws", SESSION, runtime_root=runtime), 20
            )

    methods = [c["method"] for c in read_calls(calls)]
    assert "session/delete" in methods
    assert closed == [True]  # the agent is closed even though its delete was cancelled
    assert not journal.exists()
    assert "TimeoutError" in caplog.text
    assert SESSION not in caplog.text


@pytest.mark.asyncio
async def test_opencode_delete_session_gives_up_when_the_agent_never_initializes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from ciao.providers.base import CleanupTimedOut

    monkeypatch.setattr(agents, "_DELETE_TIMEOUT_S", 3.0)
    runtime = tmp_path / "rt"
    journal = _write_opencode_journal(runtime)
    _opencode_on_fake(monkeypatch, tmp_path, initialize_delay_ms=60_000)

    with caplog.at_level("WARNING", logger="ciao.providers.acp.agents"):
        with pytest.raises(CleanupTimedOut):
            await asyncio.wait_for(
                OpencodeAcpProvider.delete_session(tmp_path / "ws", SESSION, runtime_root=runtime), 20
            )

    assert not journal.exists()
    assert "TimeoutError" in caplog.text
    assert SESSION not in caplog.text


@pytest.mark.asyncio
async def test_opencode_absent_session_spawns_nothing_and_warns_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No journal means the chat never reached a prompt, or is already gone: no agent process."""

    async def must_not_spawn(spec, *, cwd, env, client):
        raise AssertionError("an absent session must not spawn the agent")

    monkeypatch.setattr(agents.AcpConnection, "open", must_not_spawn)
    with caplog.at_level("DEBUG", logger="ciao.providers.acp.agents"):
        result = await OpencodeAcpProvider.delete_session(tmp_path / "ws", SESSION, runtime_root=tmp_path / "rt")

    assert result is None
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


@pytest.mark.asyncio
async def test_opencode_not_found_session_delete_is_debug_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runtime = tmp_path / "rt"
    journal = _write_opencode_journal(runtime)
    calls = _opencode_on_fake(monkeypatch, tmp_path, initialize=_DELETE_CAPABLE, delete_not_found=True)

    with caplog.at_level("DEBUG", logger="ciao.providers.acp.agents"):
        result = await OpencodeAcpProvider.delete_session(tmp_path / "ws", SESSION, runtime_root=runtime)

    assert result is True
    assert not journal.exists()
    assert "session/delete" in [c["method"] for c in read_calls(calls)]
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert any(r.levelno == logging.DEBUG and "no such session" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_opencode_other_session_delete_refusal_warns_with_the_type_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runtime = tmp_path / "rt"
    _write_opencode_journal(runtime)
    _opencode_on_fake(monkeypatch, tmp_path, initialize=_DELETE_CAPABLE, delete_error="secret-adapter-text")

    with caplog.at_level("DEBUG", logger="ciao.providers.acp.agents"):
        result = await OpencodeAcpProvider.delete_session(tmp_path / "ws", SESSION, runtime_root=runtime)

    assert result is True
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ["opencode could not delete a session (RequestError)."]
    assert "secret-adapter-text" not in caplog.text


@pytest.mark.asyncio
async def test_opencode_below_floor_delete_is_quiet_and_logs_the_gate_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A version-refused install: the gate logs its own warning, the delete adds none."""
    runtime = tmp_path / "rt"
    journal = _write_opencode_journal(runtime)
    (tmp_path / "ws").mkdir(exist_ok=True)
    spec, calls = write_scenario(tmp_path, agent_info={"name": "opencode", "version": "2.0.16"})
    monkeypatch.setattr(OpencodeAcpProvider, "spec", dataclasses.replace(OPENCODE, binary=spec.binary, args=spec.args))

    with caplog.at_level("WARNING"):
        result = await asyncio.wait_for(
            OpencodeAcpProvider.delete_session(tmp_path / "ws", SESSION, runtime_root=runtime), 20
        )

    assert result is True  # the journal went; the refused agent is simply not asked
    assert not journal.exists()
    assert "initialize" in [c["method"] for c in read_calls(calls)]
    assert "session/delete" not in [c["method"] for c in read_calls(calls)]
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == ["opencode agent refused: its version is below the supported floor."]


@pytest.mark.asyncio
async def test_delete_session_warns_without_the_path_when_the_cli_file_cannot_go(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from ciao import claude_sessions

    def refuse(session_id: str, directory: str | None) -> bool:
        raise PermissionError(f"denied: {tmp_path / 'secret-dir'}")

    monkeypatch.setattr(claude_sessions, "delete_session_file", refuse)
    with caplog.at_level("WARNING", logger="ciao.providers.acp.agents"):
        result = await ClaudeAcpProvider.delete_session(tmp_path, SESSION, runtime_root=tmp_path / "rt")
    assert result is None
    assert "left in place" in caplog.text
    assert SESSION not in caplog.text
    assert "secret-dir" not in caplog.text


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


# -- opencode over `opencode acp` (#3140, D1) ------------------------------------------

_CHILD = "opencode/session/child_update"


def test_opencode_spec_spawns_opencode_acp() -> None:
    assert (OPENCODE.binary, OPENCODE.args) == ("opencode", ("acp",))
    assert dict(OPENCODE.client_meta) == {"opencode/child-session-updates": True}
    descriptor = provider_registry.get("opencode")
    assert descriptor is not None
    assert descriptor.acp_spec() is OPENCODE
    assert descriptor.factory() is OpencodeAcpProvider


def test_opencode_mode_table() -> None:
    assert set(OPENCODE.modes) == {"normal", "plan", "auto", "bypass"}
    assert mode_choices(OPENCODE, "plan") == (ModeChoice("mode", ("plan",)),)
    for mode in ("normal", "auto", "bypass"):
        assert mode_choices(OPENCODE, mode) == (ModeChoice("mode", ("build",)),)
    assert (OPENCODE.model_config_id, OPENCODE.thought_config_id) == ("model", "effort")


def _child_status(child: str, status: Any, *, parent: str = "ses_root", title: str = "Explore") -> dict[str, Any]:
    return {
        "rootSessionId": "ses_root", "childSessionId": child, "parentSessionId": parent,
        "depth": 1, "title": title, "type": "status", "status": status,
    }


def _child_update(child: str, body: dict[str, Any], *, parent: str = "ses_root") -> dict[str, Any]:
    return {
        "rootSessionId": "ses_root", "childSessionId": child, "parentSessionId": parent,
        "depth": 1, "type": "update", "update": body,
    }


_CHILD_FIXTURE = Path(__file__).parent / "fixtures" / "acp" / "opencode_child_updates.jsonl"


def _fixture_payloads() -> list[dict[str, Any]]:
    return [json.loads(line) for line in _CHILD_FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_opencode_child_update_nests_under_child_tool(tmp_path: Path) -> None:
    provider = OpencodeAcpProvider(tmp_path)
    normalizer = UpdateNormalizer(OPENCODE, "ses_root")
    events: list[Any] = []
    for payload in _fixture_payloads():
        events.extend(provider.ext_update(normalizer, _CHILD, payload))

    # The fixture's four parent-state updates (usage, model, mode, compaction) are dropped.
    assert [
        (event.type, getattr(event, "tool_use_id", None), event.parent_tool_use_id) for event in events
    ] == [
        ("tool_use", "child:ses_child", None),
        ("text", None, "child:ses_child"),
        ("tool_use", "ses_child:call_1", "child:ses_child"),
        ("tool_result", "ses_child:call_1", "child:ses_child"),
        ("tool_result", "child:ses_child", None),
    ]
    node = events[0]
    assert (node.tool_name, node.tool_input) == ("Subagent", "Explore")
    assert events[1].text == "NESTED"
    assert events[2].tool_name == "bash"
    assert events[-1].is_error is False
    assert normalizer.usage == {}
    assert normalizer.current_values == {}
    assert normalizer.compacted is False


def test_opencode_grandchild_nests_under_its_child(tmp_path: Path) -> None:
    provider = OpencodeAcpProvider(tmp_path)
    normalizer = UpdateNormalizer(OPENCODE, "ses_root")
    provider.ext_update(normalizer, _CHILD, _child_status("ses_child", "running"))
    # A grandchild names its parent child, so it nests under that child's node.
    grand = provider.ext_update(normalizer, _CHILD, {
        **_child_status("ses_grand", "running", parent="ses_child"), "depth": 2,
    })
    assert grand[0].tool_use_id == "child:ses_grand"
    assert grand[0].parent_tool_use_id == "child:ses_child"


def test_opencode_child_failure_is_an_error_result(tmp_path: Path) -> None:
    provider = OpencodeAcpProvider(tmp_path)
    normalizer = UpdateNormalizer(OPENCODE, "ses_root")
    provider.ext_update(normalizer, _CHILD, _child_status("ses_child", "running"))
    failed = provider.ext_update(normalizer, _CHILD, _child_status("ses_child", "failed"))
    assert [(event.type, event.is_error) for event in failed] == [("tool_result", True)]


_DROPPED_CHILD_UPDATES = [
    pytest.param({"sessionUpdate": "usage_update", "used": 900, "size": 1000,
                  "cost": {"amount": "9.99", "currency": "USD"}}, id="usage_update"),
    pytest.param({"sessionUpdate": "config_option_update", "configOptions": [
        {"id": "model", "currentValue": "child-model", "options": []}]}, id="config_option_update"),
    pytest.param({"sessionUpdate": "current_mode_update", "currentModeId": "plan"}, id="current_mode_update"),
    pytest.param({"sessionUpdate": "compaction_update", "compactionId": "c1", "status": "completed"},
                 id="compaction_update"),
    pytest.param({"sessionUpdate": "plan", "entries": []}, id="plan"),
]


@pytest.mark.parametrize("body", _DROPPED_CHILD_UPDATES)
def test_opencode_child_drops_parent_state_updates(
    tmp_path: Path, body: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    provider = OpencodeAcpProvider(tmp_path)
    normalizer = UpdateNormalizer(OPENCODE, "ses_root")
    normalizer.current_values.update({"model": "chat-model", "mode": "build"})
    provider.ext_update(normalizer, _CHILD, _child_status("ses_child", "running"))
    caplog.set_level(logging.DEBUG, logger="ciao.providers.acp.agents")
    for _ in range(2):
        assert provider.ext_update(normalizer, _CHILD, _child_update("ses_child", body)) == []
    # The chat's own gauges, model, mode and compaction are untouched by the child.
    assert normalizer.usage == {}
    assert normalizer.current_values == {"model": "chat-model", "mode": "build"}
    assert normalizer.compacted is False
    # One DEBUG line per kind, however many times it arrives.
    kind = body["sessionUpdate"]
    assert [r.getMessage() for r in caplog.records].count(f"dropped child session update kind {kind}") == 1


def test_opencode_child_guards_drop_foreign_unknown_and_malformed(tmp_path: Path) -> None:
    provider = OpencodeAcpProvider(tmp_path)
    normalizer = UpdateNormalizer(OPENCODE, "ses_root")
    # A child of another session never reaches this chat.
    foreign = {**_child_status("ses_child", "running"), "rootSessionId": "ses_other"}
    assert provider.ext_update(normalizer, _CHILD, foreign) == []
    # Any other extension notification is ignored.
    assert provider.ext_update(normalizer, "opencode/other", _child_status("ses_child", "running")) == []
    # A non-string status is not a terminal status: the child opens and nothing ends it.
    events = provider.ext_update(normalizer, _CHILD, _child_status("ses_child", ["completed"]))
    assert [event.type for event in events] == ["tool_use"]


def test_opencode_child_title_is_capped_and_stripped_of_control_characters(tmp_path: Path) -> None:
    provider = OpencodeAcpProvider(tmp_path)
    normalizer = UpdateNormalizer(OPENCODE, "ses_root")
    opened = provider.ext_update(normalizer, _CHILD, _child_status("ses_child", "running", title="Ex\x1b[2Jplore\x00" + "x" * 500))
    title = opened[0].tool_input
    assert len(title) == 200
    assert not any(ch in title for ch in "\x00\x1b")
    assert title.startswith("Ex[2Jplore")


@pytest.mark.parametrize("child", ["ses\x00child", "x" * 129, ""])
def test_opencode_child_with_unusable_id_is_dropped(tmp_path: Path, child: str) -> None:
    provider = OpencodeAcpProvider(tmp_path)
    normalizer = UpdateNormalizer(OPENCODE, "ses_root")
    assert provider.ext_update(normalizer, _CHILD, _child_status(child, "running")) == []


def test_opencode_child_id_at_the_length_cap_is_kept(tmp_path: Path) -> None:
    provider = OpencodeAcpProvider(tmp_path)
    normalizer = UpdateNormalizer(OPENCODE, "ses_root")
    child = "c" * 128
    opened = provider.ext_update(normalizer, _CHILD, _child_status(child, "running"))
    assert opened[0].tool_use_id == f"child:{child}"


_CHILD_LIMIT_LOG = "OpenCode child sessions over 256 live at once are not shown"


def test_opencode_child_cap_counts_live_children_and_logs_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    provider = OpencodeAcpProvider(tmp_path)
    normalizer = UpdateNormalizer(OPENCODE, "ses_root")
    caplog.set_level(logging.DEBUG, logger="ciao.providers.acp.agents")
    for index in range(256):
        assert provider.ext_update(normalizer, _CHILD, _child_status(f"ses_{index}", "running"))
    # The 257th live child has no node, and no event for it is emitted.
    assert provider.ext_update(normalizer, _CHILD, _child_status("ses_overflow", "running")) == []
    assert provider.ext_update(normalizer, _CHILD, _child_update("ses_overflow", _TEXT)) == []
    assert provider.ext_update(normalizer, _CHILD, _child_status("ses_overflow2", "running")) == []
    # One DEBUG line for the whole overflow, and it names no child id.
    lines = [r.getMessage() for r in caplog.records if r.name == "ciao.providers.acp.agents"]
    assert lines == [_CHILD_LIMIT_LOG]
    # Children already open keep working.
    assert provider.ext_update(normalizer, _CHILD, _child_status("ses_0", "completed"))[0].type == "tool_result"


def test_opencode_finished_children_free_their_slot(tmp_path: Path) -> None:
    provider = OpencodeAcpProvider(tmp_path)
    normalizer = UpdateNormalizer(OPENCODE, "ses_root")
    # 300 children one after another, each finished before the next starts: all of them nest.
    for index in range(300):
        child = f"ses_seq_{index}"
        opened = provider.ext_update(normalizer, _CHILD, _child_status(child, "running"))
        assert [event.tool_use_id for event in opened] == [f"child:{child}"]
        nested = provider.ext_update(normalizer, _CHILD, _child_update(child, _TEXT))
        assert [event.parent_tool_use_id for event in nested] == [f"child:{child}"]
        ended = provider.ext_update(normalizer, _CHILD, _child_status(child, "completed"))
        assert [(event.type, event.tool_use_id) for event in ended] == [("tool_result", f"child:{child}")]


@pytest.mark.asyncio
async def test_opencode_children_left_running_do_not_fill_the_next_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = OpencodeAcpProvider(tmp_path)
    normalizer = UpdateNormalizer(OPENCODE, "ses_root")

    async def turn_with_children_left_running(_self: Any, _request: Any, _register: Any) -> Any:
        for index in range(256):
            assert provider.ext_update(normalizer, _CHILD, _child_status(f"ses_run_{index}", "running"))
        if False:  # pragma: no cover - makes this an async generator with no events
            yield

    async def turn_with_one_more_child(_self: Any, _request: Any, _register: Any) -> Any:
        yield provider.ext_update(normalizer, _CHILD, _child_status("ses_next", "running"))[0]

    monkeypatch.setattr(AcpProvider, "run_streaming", turn_with_children_left_running)
    assert [event async for event in provider.run_streaming(None, lambda _h: None)] == []
    # The turn ended with 256 children still running: the next turn's child still gets a node.
    monkeypatch.setattr(AcpProvider, "run_streaming", turn_with_one_more_child)
    events = [event async for event in provider.run_streaming(None, lambda _h: None)]
    assert [(event.type, event.tool_use_id) for event in events] == [("tool_use", "child:ses_next")]


def test_plain_update_child_meta_names_the_same_node_as_the_ext_path(tmp_path: Path) -> None:
    provider = OpencodeAcpProvider(tmp_path)
    normalizer = UpdateNormalizer(OPENCODE, "ses_root")
    provider.ext_update(normalizer, _CHILD, _child_status("ses_child", "running"))
    plain = normalizer.feed({
        **_TEXT,
        "_meta": {"opencode/child-session": {"id": "ses_child", "parentID": "ses_root", "depth": 1}},
    })
    assert [event.parent_tool_use_id for event in plain] == ["child:ses_child"]
    # Unusable ids are not used as a parent at all.
    bad = normalizer.feed({**_TEXT, "_meta": {"opencode/child-session": {"id": "ses\x00child"}}})
    assert [event.parent_tool_use_id for event in bad] == [None]


@pytest.mark.asyncio
async def test_opencode_permission_option_ids_once_always_reject() -> None:
    options = [
        {"optionId": "once", "kind": "allow_once", "name": "Once"},
        {"optionId": "always", "kind": "allow_always", "name": "Always"},
        {"optionId": "reject", "kind": "reject_once", "name": "Reject"},
    ]
    for approved, expected in ((True, "once"), (False, "reject")):
        broker = PermissionBroker(lambda _event: None)
        task = asyncio.create_task(broker.request(
            "ses_root", {"toolCallId": "call-1", "title": "bash"}, options,
        ))
        for _ in range(20):
            if broker.has("call-1"):
                break
            await asyncio.sleep(0)
        assert broker.respond("call-1", approved, session_id="ses_root") == QuestionResponseResult(ok=True)
        assert await task == {"outcome": "selected", "optionId": expected}


class _RecordingSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def on_ext(self, method: str, payload: Any, hook: Any) -> None:
        self.calls.append((method, payload))


@pytest.mark.asyncio
async def test_ext_notification_reaches_the_live_session() -> None:
    client = AgentClient(ext_hook=lambda _n, _m, _p: [])
    session = _RecordingSession()
    client.session = session  # type: ignore[assignment]
    await client.ext_notification(_CHILD, {"rootSessionId": "ses_root"})
    assert session.calls == [(_CHILD, {"rootSessionId": "ses_root"})]
    # No hook, no delivery: a client without an extension hook drops the notification.
    unhooked = AgentClient()
    unhooked.session = session  # type: ignore[assignment]
    await unhooked.ext_notification(_CHILD, {"rootSessionId": "ses_root"})
    assert len(session.calls) == 1


def test_claude_acp_provider_has_no_sdk_import_or_native_title_read() -> None:
    # The title comes from the protocol (session_info_update) and the session file is read
    # by claude_sessions, so the ACP agents module must not import the SDK-era provider.
    from ciao.providers.acp import agents

    source = Path(agents.__file__).read_text(encoding="utf-8")
    assert "ciao.providers.claude" not in source
    assert "native_title_candidates" not in ClaudeAcpProvider.__dict__


@pytest.mark.asyncio
async def test_claude_acp_provider_reads_no_native_title(tmp_path: Path) -> None:
    assert await ClaudeAcpProvider.native_title_candidates(tmp_path, SESSION, runtime_root=tmp_path) == ()


@pytest.mark.asyncio
async def test_delete_session_runs_the_blocking_file_delete_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # delete_session_file runs a git subprocess and file removal; it must not block the loop.
    import threading

    from ciao import claude_sessions

    ran_on: list[int] = []

    def record(session_id: str, directory: str | None) -> bool:
        ran_on.append(threading.get_ident())
        return False

    monkeypatch.setattr(claude_sessions, "delete_session_file", record)
    await ClaudeAcpProvider.delete_session(tmp_path, SESSION, runtime_root=tmp_path / "rt")

    assert ran_on, "the CLI session delete was not attempted"
    assert ran_on[0] != threading.get_ident()


# -- opencode subagent run, replayed from the captured wire frames (#3140, subagents fix) -----

_SUBAGENT_RUN = Path(__file__).parent / "fixtures" / "acp" / "opencode_subagent_run.jsonl"
_RUN_ROOT = "ses_e0815d817ffeh7L7ocNuPWytBu"
_RUN_CALL = "call_6dad53e1b72a40bab9ed61ca"
_RUN_CHILD = "ses_e0815c31bffezDWIb2jb1MkEwf"


def _replay(provider: OpencodeAcpProvider, normalizer: UpdateNormalizer, lines: list[dict[str, Any]]) -> list[Any]:
    events: list[Any] = []
    for message in lines:
        if message.get("method") == "session/update":
            events.extend(normalizer.feed(message["params"]["update"]))
        elif message.get("method") == _CHILD:
            events.extend(provider.ext_update(normalizer, _CHILD, message["params"]))
    return events


def _run_lines() -> list[dict[str, Any]]:
    return [json.loads(line) for line in _SUBAGENT_RUN.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_opencode_subagent_run_nests_the_child_under_its_call(tmp_path: Path) -> None:
    events = _replay(OpencodeAcpProvider(tmp_path), UpdateNormalizer(OPENCODE, _RUN_ROOT), _run_lines())
    tool_events = [event for event in events if isinstance(event, ToolUseEvent)]
    # The subagent is one node: no `Subagent` node opens for a child that a launch claimed.
    assert not [event for event in tool_events if event.tool_name == "Subagent"]
    calls = [event for event in tool_events if event.tool_use_id == _RUN_CALL]
    # Announced once with no input, then once with the description: the rendered input changed.
    assert [(event.type, event.tool_name, event.tool_input) for event in calls if event.type == "tool_use"] == [
        ("tool_use", "Agent", ""),
        ("tool_use", "Agent", "[general] Sleep 5 then reply done"),
    ]
    # The launch acknowledgement is not the completion; the terminal child status is.
    results = [(event.background, event.subagent_status, event.is_error) for event in calls if event.type == "tool_result"]
    assert results == [(True, "", False), (False, "completed", False)]
    child_calls = [event for event in tool_events if (event.tool_use_id or "").startswith(_RUN_CHILD)]
    assert {event.tool_name for event in child_calls if event.type == "tool_use"} == {"shell"}
    assert all(event.parent_tool_use_id == _RUN_CALL for event in child_calls)
    child_text = [event.text for event in events if isinstance(event, AssistantTextDelta) and event.parent_tool_use_id == _RUN_CALL]
    assert child_text == ["done"]
    # The prompt the subagent was given is not journalled.
    assert not any("Your only job" in event.tool_input for event in tool_events if isinstance(event.tool_input, str))


def test_opencode_launch_session_id_links_a_child_the_title_does_not_name(tmp_path: Path) -> None:
    provider = OpencodeAcpProvider(tmp_path)
    normalizer = UpdateNormalizer(OPENCODE, "ses_root")
    normalizer.feed({"sessionUpdate": "tool_call", "toolCallId": "call_a", "name": "subagent", "kind": "think",
                     "title": "subagent", "rawInput": {"agent": "general", "description": "Scan repo"}})
    ack = normalizer.feed({"sessionUpdate": "tool_call_update", "toolCallId": "call_a", "status": "completed",
                           "rawOutput": {"metadata": {"sessionID": "ses_b", "status": "running"}}})
    assert [(event.type, event.background) for event in ack] == [("tool_result", True)]
    # The child's title does not match the description: only its launch's sessionID can link it.
    frame = {"rootSessionId": "ses_root", "childSessionId": "ses_b", "parentSessionId": "ses_root", "depth": 1,
             "title": "Something else", "type": "update",
             "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}}
    events = provider.ext_update(normalizer, _CHILD, frame)
    assert [(event.type, event.parent_tool_use_id) for event in events] == [("text", "call_a")]
    done = provider.ext_update(normalizer, _CHILD, _child_status("ses_b", "failed", title="Something else"))
    assert [(event.type, event.tool_use_id, event.subagent_status, event.is_error) for event in done] == [
        ("tool_result", "call_a", "failed", True),
    ]
    assert not any(getattr(event, "tool_use_id", "") == "child:ses_b" for event in events + done)


def test_repeated_subagent_update_does_not_claim_a_later_child(tmp_path: Path) -> None:
    provider = OpencodeAcpProvider(tmp_path)
    normalizer = UpdateNormalizer(OPENCODE, "ses_root")
    normalizer.feed({"sessionUpdate": "tool_call", "toolCallId": "call_r", "name": "subagent", "kind": "think",
                     "title": "subagent", "rawInput": {}})
    launch = {"sessionUpdate": "tool_call_update", "toolCallId": "call_r", "status": "in_progress",
              "rawInput": {"agent": "general", "description": "Scan"}}
    normalizer.feed(launch)
    provider.ext_update(normalizer, _CHILD, _child_status("ses_first", "created", title="Scan"))
    # The same in_progress update arrives again after the first child has claimed the call.
    normalizer.feed(dict(launch))
    # A second child with the same title belongs to no call: it keeps its own node.
    second = provider.ext_update(normalizer, _CHILD, _child_status("ses_second", "created", title="Scan"))
    assert [(event.type, event.tool_name, event.tool_use_id) for event in second] == [
        ("tool_use", "Subagent", "child:ses_second"),
    ]


def test_opencode_spec_routes_the_unprefixed_child_notification() -> None:
    # The SDK routes only `_`-prefixed notifications to ext_notification; this is the one name
    # the connection adds, exactly as opencode sends it.
    assert OPENCODE.extension_notifications == (_CHILD,)


def test_codex_spec_routes_auth_status_update() -> None:
    assert CODEX.extension_notifications == ("auth/status_update",)


def test_codex_auth_status_update_is_logged_at_debug_only(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    provider = CodexAcpProvider(Path("."))
    normalizer = UpdateNormalizer(CODEX, "ses_root")
    # Well-formed, and a malformed one with the wrong types: neither changes any events.
    assert provider.ext_update(normalizer, "auth/status_update", {
        "state": "authenticated", "accounts": [{"label": "ChatGPT Secret Label", "active": True}],
    }) == []
    assert provider.ext_update(normalizer, "auth/status_update", {"state": 5, "accounts": "none"}) == []
    lines = [r.getMessage() for r in caplog.records if r.name == "ciao.providers.acp.agents"]
    assert any("authenticated" in line for line in lines)
    # Account labels are display-only and never logged.
    assert not any("ChatGPT Secret Label" in line for line in lines)
    assert all(r.levelno == logging.DEBUG for r in caplog.records if r.name == "ciao.providers.acp.agents")
