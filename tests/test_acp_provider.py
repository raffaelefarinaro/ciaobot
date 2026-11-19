"""AcpProvider over the scripted ACP agent: turns, reuse, permissions, persisted sessions (#3137).

Every turn goes through ``ProviderService`` (the host path), not the class directly, except
where a test is about a classmethod or a provider seam the host reaches only by name.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from ciao import provider_registry
from ciao.models import (
    COMPACT_COMMAND,
    AgentRequest,
    ImageAttachment,
    PermissionRequestEvent,
    ResultEvent,
    StreamEvent,
    SystemStatusEvent,
)
from ciao.provider_registry import ProviderDescriptor
from ciao.provider_service import ProviderService
from ciao.providers.acp.connection import AgentStartError
from ciao.providers.acp.provider import AcpProvider, start_failure_reason
from ciao.providers.acp.session import SessionJournal, journal_file
from ciao.providers.acp.spec import AgentSpec
from tests.acp_helpers import read_calls, write_scenario

# Target of the fake descriptor's ``acp_spec_path``; each test installs its own spec here.
FAKE_SPEC: AgentSpec | None = None

_TEXT = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}


class FakeAcpProvider(AcpProvider):
    """The smallest ACP subclass: ``spec`` is installed per test, and ``meta`` drives ``_meta``."""

    meta: ClassVar[dict[str, Any]] = {}

    def session_meta(self, request: AgentRequest) -> dict[str, Any]:
        return dict(type(self).meta)


def _install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **scenario: Any) -> tuple[SimpleNamespace, Path]:
    """Register the fake agent as provider ``fake``; return ``(config, calls_log)``."""
    spec, calls = write_scenario(tmp_path, **scenario)
    descriptor = ProviderDescriptor(
        id="fake",
        label="Fake agent",
        short_label="Fake",
        cli_label="Fake",
        factory_path="tests.test_acp_provider:FakeAcpProvider",
        acp_spec_path="tests.test_acp_provider:FAKE_SPEC",
    )
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "FAKE_SPEC", spec)
    monkeypatch.setattr(FakeAcpProvider, "spec", spec, raising=False)
    monkeypatch.setattr(FakeAcpProvider, "meta", {}, raising=False)
    monkeypatch.setattr(provider_registry, "_DESCRIPTORS", (descriptor,))
    monkeypatch.setattr(provider_registry, "_BY_ID", {"fake": descriptor})
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    config = SimpleNamespace(workspace_root=workspace, state_path=tmp_path / "runtime" / "state.json")
    return config, calls


def _request(prompt: str = "hello", **fields: Any) -> AgentRequest:
    return AgentRequest(prompt=prompt, model="fake-model", mode="auto", provider="fake", **fields)


async def _turn(service: ProviderService, request: AgentRequest) -> list[StreamEvent]:
    return await asyncio.wait_for(_collect(service, request), timeout=10)


async def _collect(service: ProviderService, request: AgentRequest) -> list[StreamEvent]:
    return [event async for event in service.execute_streaming(request)]


async def _until(predicate: Any, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition never held")
        await asyncio.sleep(0.01)


def _methods(calls: Path, method: str) -> list[dict[str, Any]]:
    return [call["params"] for call in read_calls(calls) if call["method"] == method]


def _count(calls: Path, method: str) -> int:
    return len(_methods(calls, method))


def _prompt_text(calls: Path) -> str:
    prompt = _methods(calls, "session/prompt")[-1]["prompt"]
    return prompt[0]["text"]


def _text_turn(text: str) -> dict[str, Any]:
    return {"steps": [{"update": {**_TEXT, "content": {"type": "text", "text": text}}}]}


# -- turns through the host path ------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_service_runs_fake_turn_to_result_with_session_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, _ = _install(monkeypatch, tmp_path, sessions=["sess-1"], prompts=[_text_turn("done")])
    service = ProviderService(config)  # type: ignore[arg-type]

    events = await _turn(service, _request())

    result = events[-1]
    assert isinstance(result, ResultEvent)
    assert result.is_error is False
    assert result.result == "done"
    assert result.session_id == "sess-1"
    assert service.current_session_id == "sess-1"


@pytest.mark.asyncio
async def test_no_resume_always_opens_new_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config, calls = _install(monkeypatch, tmp_path, sessions=["a", "b"])
    service = ProviderService(config)  # type: ignore[arg-type]

    await _turn(service, _request())
    second = await _turn(service, _request())

    assert _count(calls, "session/new") == 2
    assert _count(calls, "session/load") == 0
    assert second[-1].session_id == "b"  # a fresh chat never inherits the live session


@pytest.mark.asyncio
async def test_resume_same_session_reuses_connection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config, calls = _install(monkeypatch, tmp_path, sessions=["a"])
    service = ProviderService(config)  # type: ignore[arg-type]

    await _turn(service, _request())
    await _turn(service, _request(resume_session="a"))

    assert _count(calls, "initialize") == 1
    assert _count(calls, "session/new") == 1
    assert _count(calls, "session/load") == 0


@pytest.mark.asyncio
async def test_rotated_token_respawns_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config, calls = _install(monkeypatch, tmp_path, sessions=["a"])
    service = ProviderService(config)  # type: ignore[arg-type]

    await _turn(service, _request(extra_env={"CIAO_AGENT_TOKEN": "first"}))
    await _turn(service, _request(resume_session="a", extra_env={"CIAO_AGENT_TOKEN": "rotated"}))

    assert _count(calls, "initialize") == 2


@pytest.mark.asyncio
async def test_replaced_session_prepends_stable_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # No loadSession/resume capability, so resuming "gone" falls back to session/new: replaced.
    config, calls = _install(monkeypatch, tmp_path, sessions=["fresh"])
    service = ProviderService(config)  # type: ignore[arg-type]

    await _turn(service, _request(resume_session="gone", stable_context_prefix="[context]\n"))

    assert _prompt_text(calls).startswith("[context]\n")


@pytest.mark.asyncio
async def test_compact_sends_bare_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config, calls = _install(monkeypatch, tmp_path, sessions=["a"])
    service = ProviderService(config)  # type: ignore[arg-type]

    await _turn(service, _request("a long capsule that must not be sent", compact=True))

    assert _prompt_text(calls) == COMPACT_COMMAND


@pytest.mark.asyncio
@pytest.mark.parametrize(("capabilities", "expect_image"), [({"promptCapabilities": {"image": True}}, True), ({}, False)])
async def test_images_sent_only_when_agent_supports_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capabilities: dict[str, Any], expect_image: bool
) -> None:
    config, calls = _install(monkeypatch, tmp_path, sessions=["a"], initialize=capabilities)
    picture = tmp_path / "pic.png"
    picture.write_bytes(b"\x89PNG-test")
    image = ImageAttachment(path=picture, mime_type="image/png", original_filename="pic.png")
    service = ProviderService(config)  # type: ignore[arg-type]

    await _turn(service, _request(images=[image]))

    blocks = _methods(calls, "session/prompt")[-1]["prompt"]
    kinds = [block["type"] for block in blocks]
    assert ("image" in kinds) is expect_image


@pytest.mark.asyncio
async def test_stop_handle_cancels_and_result_is_stopped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config, _ = _install(monkeypatch, tmp_path, sessions=["a"], prompts=[{"steps": [{"await_cancel": True}]}])
    service = ProviderService(config)  # type: ignore[arg-type]
    events: list[StreamEvent] = []

    async def run() -> None:
        async for event in service.execute_streaming(_request()):
            events.append(event)

    task = asyncio.create_task(run())
    await _until(lambda: service.active_handle() is not None)
    assert await service.stop_active() is True
    await asyncio.wait_for(task, timeout=10)

    result = events[-1]
    assert isinstance(result, ResultEvent)
    assert result.stopped is True
    assert result.is_error is False


@pytest.mark.asyncio
async def test_respond_permission_through_service_contract(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    steps = [{
        "permission": {
            "toolCall": {"toolCallId": "call-1", "title": "Bash", "kind": "execute", "rawInput": {"command": "ls"}},
            "options": [
                {"optionId": "once", "name": "Once", "kind": "allow_once"},
                {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
            ],
        },
    }]
    config, calls = _install(monkeypatch, tmp_path, sessions=["a"], prompts=[{"steps": steps}])
    service = ProviderService(config)  # type: ignore[arg-type]
    events: list[StreamEvent] = []

    async def run() -> None:
        async for event in service.execute_streaming(_request()):
            events.append(event)

    task = asyncio.create_task(run())
    await _until(lambda: any(isinstance(event, PermissionRequestEvent) for event in events))
    asking = next(event for event in events if isinstance(event, PermissionRequestEvent))
    provider = service.provider
    assert provider is not None
    assert provider.has_permission_request(asking.request_id, asking.session_id)

    answer = await provider.respond_permission(asking.request_id, True, session_id=asking.session_id)
    await asyncio.wait_for(task, timeout=10)

    assert answer.ok is True
    outcomes = _methods(calls, "permission_outcome")
    assert outcomes == [{"outcome": {"outcome": "selected", "optionId": "once"}}]


@pytest.mark.asyncio
async def test_unattended_request_rejects_without_card(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    steps = [{
        "permission": {
            "toolCall": {"toolCallId": "call-1", "title": "Bash"},
            "options": [
                {"optionId": "once", "name": "Once", "kind": "allow_once"},
                {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
            ],
        },
    }]
    config, calls = _install(monkeypatch, tmp_path, sessions=["a"], prompts=[{"steps": steps}])
    service = ProviderService(config)  # type: ignore[arg-type]

    # Without the rejection the fake waits forever for an answer, so the timeout is the failure mode.
    events = await asyncio.wait_for(_collect(service, _request(unattended=True)), timeout=10)

    assert not any(isinstance(event, PermissionRequestEvent) for event in events)
    assert any(isinstance(event, SystemStatusEvent) and "Denied" in event.status for event in events)
    assert _methods(calls, "permission_outcome") == [{"outcome": {"outcome": "selected", "optionId": "reject"}}]


@pytest.mark.asyncio
async def test_drain_events_yields_late_turn_result_with_origin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, _ = _install(
        monkeypatch, tmp_path, sessions=["a"],
        after_prompt=[{"update": {**_TEXT, "content": {"type": "text", "text": "late words"}}}],
        after_prompt_delay_ms=20, spec_quiet_period_s=0.2,
    )
    service = ProviderService(config)  # type: ignore[arg-type]
    await _turn(service, _request())
    await _until(lambda: service.background_work_outstanding)

    async def first_result() -> ResultEvent:
        async for event in service.drain_events():
            if isinstance(event, ResultEvent):
                return event
        raise AssertionError("late stream ended without a result")

    late = await asyncio.wait_for(first_result(), timeout=5)
    assert late.origin == "task-notification"
    assert late.result == "late words"


@pytest.mark.asyncio
async def test_disconnect_twice_is_safe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config, _ = _install(monkeypatch, tmp_path, sessions=["a"])
    service = ProviderService(config)  # type: ignore[arg-type]
    await _turn(service, _request())
    provider = service.provider
    assert provider is not None

    await provider.disconnect()
    await provider.disconnect()

    assert provider.is_connected is False
    assert provider.current_session_id is None


# -- persisted sessions and start failures ----------------------------------------------


@pytest.mark.asyncio
async def test_delete_session_removes_journal_without_spawning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, _ = _install(monkeypatch, tmp_path)
    workspace: Path = config.workspace_root
    path = journal_file(workspace / ".runtime", "fake", "sess-9")
    SessionJournal(path).user("hi")

    async def no_spawn(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("delete_session must not spawn the agent")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_spawn)

    assert await FakeAcpProvider.delete_session(workspace, "sess-9") is True
    assert not path.exists()
    assert await FakeAcpProvider.delete_session(workspace, "sess-9") is None


@pytest.mark.asyncio
async def test_read_thread_returns_journal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config, _ = _install(monkeypatch, tmp_path)
    workspace: Path = config.workspace_root
    assert await FakeAcpProvider.read_thread(workspace, "sess-9") == {}

    journal = SessionJournal(journal_file(workspace / ".runtime", "fake", "sess-9"))
    journal.user("hi")
    journal.user("again")

    thread = await FakeAcpProvider.read_thread(workspace, "sess-9")
    assert [row["text"] for row in thread["acp_journal"]] == ["hi", "again"]


@pytest.mark.asyncio
async def test_agent_start_error_becomes_error_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "SECRET_STDERR_TOKEN_123"
    config, _ = _install(
        monkeypatch, tmp_path, exit_during_initialize=True,
        stderr=[f"boom {secret} while reading /Users/alice/.ssh/id_rsa"],
    )
    service = ProviderService(config)  # type: ignore[arg-type]
    caplog.set_level(logging.WARNING, logger="ciao.providers.acp.provider")

    events = await _turn(service, _request())

    assert len(events) == 1
    event = events[0]
    assert isinstance(event, ResultEvent)
    assert event.is_error is True
    # The PWA never sees raw adapter output: the event carries only the sanitized reason.
    assert secret not in event.result
    assert "/Users/alice" not in event.result
    # ...and the raw tail reaches the log, so the failure stays diagnosable.
    assert secret in caplog.text


def test_start_failure_reason_masks_paths_and_caps_length() -> None:
    masked = start_failure_reason(AgentStartError("binary /opt/tools/agent-bin is missing\nsecond line"))
    assert masked == "binary <path> is missing"

    long = start_failure_reason(AgentStartError("x" * 500))
    assert len(long) <= 200
    assert long.endswith("…")

    assert start_failure_reason(AgentStartError("")) == "The agent did not start."


# -- session metadata reload ------------------------------------------------------------


@pytest.mark.asyncio
async def test_changed_session_meta_reloads_same_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config, calls = _install(monkeypatch, tmp_path, sessions=["a"], initialize={"loadSession": True})
    service = ProviderService(config)  # type: ignore[arg-type]
    first_meta = {"claudeCode": {"options": {"disallowedTools": ["CronCreate"]}}}
    second_meta = {"claudeCode": {"options": {"disallowedTools": ["ScheduleWakeup"]}}}

    FakeAcpProvider.meta = first_meta
    await _turn(service, _request())
    FakeAcpProvider.meta = second_meta
    await _turn(service, _request(resume_session="a"))

    assert _count(calls, "initialize") == 1  # same connection
    assert _count(calls, "session/new") == 1  # same session id, never a new one
    loads = _methods(calls, "session/load")
    assert len(loads) == 1
    assert loads[0]["sessionId"] == "a"
    assert loads[0]["_meta"] == second_meta
    assert service.current_session_id == "a"


@pytest.mark.asyncio
async def test_changed_session_meta_waits_for_late_turn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config, calls = _install(monkeypatch, tmp_path, sessions=["a"], initialize={"loadSession": True},
                             spec_quiet_period_s=30)
    service = ProviderService(config)  # type: ignore[arg-type]
    FakeAcpProvider.meta = {"claudeCode": {"options": {"a": 1}}}
    await _turn(service, _request())
    provider = service.provider
    assert provider is not None
    session = provider._session  # the open session, to start a late turn the way a background update would
    assert session is not None
    session.on_update({
        "sessionId": "a", "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": "still working"},
    })
    assert provider.background_work_outstanding is True

    FakeAcpProvider.meta = {"claudeCode": {"options": {"a": 2}}}
    await _turn(service, _request(resume_session="a"))

    # The change waits: reloading now would close the late turn that is still running.
    assert _count(calls, "session/load") == 0
    assert _count(calls, "session/new") == 1

    # The next turn that starts with no late turn open applies the new options.
    await _turn(service, _request(resume_session="a"))
    loads = _methods(calls, "session/load")
    assert len(loads) == 1
    assert loads[0]["_meta"] == {"claudeCode": {"options": {"a": 2}}}


# -- registry hook ----------------------------------------------------------------------


def test_descriptor_acp_spec_resolves_module_attr(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = AgentSpec(id="fake", binary="fake-binary")
    monkeypatch.setattr(sys.modules[__name__], "FAKE_SPEC", spec)
    acp_descriptor = ProviderDescriptor(
        id="fake", label="F", short_label="F", cli_label="F",
        factory_path="tests.test_acp_provider:FakeAcpProvider",
        acp_spec_path="tests.test_acp_provider:FAKE_SPEC",
    )
    plain = ProviderDescriptor(id="plain", label="P", short_label="P", cli_label="P", factory_path="x:Y")

    assert acp_descriptor.acp_spec() is spec
    assert plain.acp_spec() is None


def test_shipped_providers_do_not_run_over_acp() -> None:
    assert all(item.acp_spec() is None for item in provider_registry.descriptors())

