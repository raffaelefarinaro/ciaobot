"""ACP session owner: queue, permissions, config, late turns, journal (#3137)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import stat
import sys
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import acp
import pytest
from acp.exceptions import RequestError
from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse

from ciao.models import ContextCompactedEvent, PermissionRequestEvent, ResultEvent, SystemStatusEvent
from ciao.providers.acp import connection as connection_module
from ciao.providers.acp.connection import AcpConnection
from ciao.providers.acp.permissions import PermissionBroker
from ciao.providers.acp.session import AcpSession, ModeNotApplied, SessionJournal, journal_path
from ciao.providers.acp.spec import ModeChoice
from tests.acp_helpers import read_calls, write_scenario

_MODES = {
    "auto": (ModeChoice("mode", ("auto", "acceptEdits", "default")),),
    "normal": (ModeChoice("mode", ("default",)),),
    "plan": (ModeChoice("mode", ("plan",)),),
    "bypass": (ModeChoice("mode", ("bypassPermissions",)),),
}
_WAKE = "The agent continued after background work finished."


def _select(option_id: str, current: str, values: list[str]) -> dict[str, Any]:
    return {
        "id": option_id,
        "name": option_id,
        "type": "select",
        "currentValue": current,
        "options": [{"value": value, "name": value} for value in values],
    }


def _text(text: str) -> dict[str, Any]:
    return {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}


class _Client(acp.Client):
    def __init__(self, broker: PermissionBroker) -> None:
        self.broker = broker
        self.connection: AcpConnection | None = None
        self.session: AcpSession | None = None

    async def request_permission(
        self, session_id: str, tool_call: Any, options: Any, **kwargs: Any
    ) -> RequestPermissionResponse:
        call = tool_call.model_dump(by_alias=True, exclude_none=True)
        stored = [option.model_dump(by_alias=True, exclude_none=True) for option in options]
        outcome = await self.broker.request(session_id, call, stored)
        if outcome.get("outcome") == "selected":
            return RequestPermissionResponse(
                outcome=AllowedOutcome(option_id=str(outcome.get("optionId")))
            )
        return RequestPermissionResponse(outcome=DeniedOutcome())

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        payload = update.model_dump(by_alias=True, exclude_none=True)
        payload["sessionId"] = session_id
        session = self.session
        if session is None and self.connection is not None:
            pending = getattr(self.connection, "_pending_session", None)
            if isinstance(pending, AcpSession):
                session = pending
        if session is not None:
            session.on_update(payload)


class _Harness:
    def __init__(
        self,
        connection: AcpConnection,
        session: AcpSession,
        broker: PermissionBroker,
        calls: Path,
        replaced: bool,
    ) -> None:
        self.connection = connection
        self.session = session
        self.broker = broker
        self.calls = calls
        self.replaced = replaced

    async def close(self) -> None:
        await self.session.close()
        await self.connection.close()


@pytest.fixture(autouse=True)
def _fast_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connection_module, "_SHUTDOWN_TIMEOUT", 0.5)


@asynccontextmanager
async def _started(
    directory: Path,
    *,
    resume: str | None = None,
    additional_dirs: tuple[str, ...] = (),
    session_meta: Mapping[str, Any] | None = None,
    **scenario: Any,
) -> AsyncIterator[_Harness]:
    directory.mkdir(parents=True, exist_ok=True)
    scenario.setdefault("spec_modes", _MODES)
    spec, calls = write_scenario(directory, **scenario)
    broker = PermissionBroker(lambda _event: None)
    client = _Client(broker)
    connection = await AcpConnection.open(spec, cwd=directory, env={}, client=client)
    client.connection = connection
    try:
        session, replaced = await AcpSession.open(
            connection,
            spec,
            cwd=directory,
            resume=resume,
            additional_dirs=additional_dirs,
            journal_dir=directory / "runtime",
            broker=broker,
            session_meta=session_meta,
        )
    except BaseException:
        await connection.close()
        raise
    client.session = session
    harness = _Harness(connection, session, broker, calls, replaced)
    try:
        yield harness
    finally:
        await harness.close()


async def _collect(
    harness: _Harness,
    text: str = "hello",
    *,
    unattended: bool = False,
    approve: bool = False,
) -> list[Any]:
    events: list[Any] = []
    async for event in harness.session.prompt(
        [{"type": "text", "text": text}],
        display=text,
        unattended=unattended,
    ):
        events.append(event)
        if approve and isinstance(event, PermissionRequestEvent):
            harness.broker.respond(event.request_id, True, session_id=event.session_id)
    return events


def _methods(path: Path) -> list[str]:
    return [call["method"] for call in read_calls(path)]


@pytest.mark.asyncio
async def test_permission_round_trip_reaches_agent(tmp_path: Path) -> None:
    steps = [{
        "permission": {
            "toolCall": {
                "toolCallId": "call-1",
                "title": "Bash",
                "kind": "execute",
                "rawInput": {"command": "ls"},
            },
            "options": [
                {"optionId": "always", "name": "Always", "kind": "allow_always"},
                {"optionId": "once", "name": "Once", "kind": "allow_once"},
                {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
            ],
        },
    }]
    async with _started(tmp_path, prompts=[{"steps": steps}]) as harness:
        events = await _collect(harness, approve=True)
        outcome = next(call for call in read_calls(harness.calls) if call["method"] == "permission_outcome")
        assert outcome["params"]["outcome"] == {"outcome": "selected", "optionId": "once"}
        assert any(isinstance(event, PermissionRequestEvent) for event in events)
        assert isinstance(events[-1], ResultEvent)
        assert events[-1].is_error is False


@pytest.mark.asyncio
async def test_cancel_answers_pending_permission_cancelled_then_cancels_session(tmp_path: Path) -> None:
    steps = [{
        "permission": {
            "toolCall": {"toolCallId": "call-1", "title": "Bash"},
            "options": [
                {"optionId": "once", "name": "Once", "kind": "allow_once"},
                {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
            ],
        },
    }]
    async with _started(tmp_path, prompts=[{"steps": steps}]) as harness:
        task = asyncio.create_task(_collect(harness))
        for _ in range(100):
            if harness.broker.has("call-1"):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("permission was never asked")
        await harness.session.cancel()
        await asyncio.wait_for(task, timeout=5)
        calls = read_calls(harness.calls)
        outcome_at = next(i for i, call in enumerate(calls) if call["method"] == "permission_outcome")
        cancel_at = next(i for i, call in enumerate(calls) if call["method"] == "session/cancel")
        assert outcome_at < cancel_at
        assert calls[outcome_at]["params"]["outcome"]["outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_yields_stopped_result(tmp_path: Path) -> None:
    async with _started(tmp_path, prompts=[{"steps": [{"await_cancel": True}]}]) as harness:
        task = asyncio.create_task(_collect(harness, "stop-me"))
        for _ in range(100):
            if "session/prompt" in _methods(harness.calls):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("prompt never started")
        await harness.session.cancel()
        events = await asyncio.wait_for(task, timeout=5)
        result = events[-1]
        assert isinstance(result, ResultEvent)
        assert result.stopped is True
        assert result.is_error is False


@pytest.mark.asyncio
async def test_prompts_are_serialized(tmp_path: Path) -> None:
    prompts = [
        {"steps": [{"sleep_ms": 400}, {"stop": "end_turn"}]},
        {"steps": [{"stop": "end_turn"}]},
    ]
    async with _started(tmp_path, prompts=prompts) as harness:
        first = asyncio.create_task(_collect(harness, "one"))
        await asyncio.sleep(0.05)
        second = asyncio.create_task(_collect(harness, "two"))
        await asyncio.sleep(0.05)
        assert _methods(harness.calls).count("session/prompt") == 1
        await asyncio.wait_for(first, timeout=5)
        await asyncio.wait_for(second, timeout=5)
        assert _methods(harness.calls).count("session/prompt") == 2


@pytest.mark.asyncio
async def test_updates_before_prompt_response_are_all_delivered(tmp_path: Path) -> None:
    steps = [{"update_detached": _text("x")} for _ in range(200)]
    steps.append({"stop": "end_turn"})
    async with _started(tmp_path, prompts=[{"steps": steps}]) as harness:
        events = await _collect(harness)
        texts = [event for event in events if getattr(event, "type", "") == "text"]
        assert len(texts) == 200
        assert isinstance(events[-1], ResultEvent)
        assert events[-1].result == "x" * 200


@pytest.mark.asyncio
async def test_late_updates_open_and_close_after_quiet_period(tmp_path: Path) -> None:
    async with _started(tmp_path, spec_quiet_period_s=0.2) as harness:
        await _collect(harness)
        harness.session.on_update({
            "sessionId": harness.session.session_id,
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "still working"},
        })
        assert harness.session.late_turn_open

        async def watch() -> ResultEvent:
            async for event in harness.session.late_events():
                if isinstance(event, ResultEvent):
                    return event
            raise AssertionError("late stream ended")

        event = await asyncio.wait_for(watch(), timeout=1.5)
        assert event.origin == "task-notification"
        assert event.wake_notice == _WAKE
        assert event.result == "still working"
        assert harness.session.late_turn_open is False


@pytest.mark.asyncio
async def test_compaction_yields_one_context_compacted_event(tmp_path: Path) -> None:
    async with _started(tmp_path, spec_quiet_period_s=0.2) as harness:
        await _collect(harness)
        session_id = harness.session.session_id
        harness.session.on_update({
            "sessionId": session_id,
            "sessionUpdate": "compaction_update",
            "compactionId": "c",
            "status": "completed",
        })
        harness.session.on_update({
            "sessionId": session_id,
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "after compaction"},
        })
        seen: list[Any] = []

        async def watch() -> None:
            async for event in harness.session.late_events():
                seen.append(event)
                if isinstance(event, ResultEvent):
                    return

        await asyncio.wait_for(watch(), timeout=1.5)
        # The flag resets after one yield: the text update after the compaction carries no second marker.
        assert len([e for e in seen if isinstance(e, ContextCompactedEvent)]) == 1


@pytest.mark.asyncio
async def test_late_turn_with_only_usage_closes_silently(tmp_path: Path) -> None:
    async with _started(tmp_path, spec_quiet_period_s=0.2) as harness:
        seen: list[Any] = []

        async def watch() -> None:
            async for event in harness.session.late_events():
                seen.append(event)

        watcher = asyncio.create_task(watch())
        harness.session.on_update({
            "sessionId": harness.session.session_id,
            "sessionUpdate": "usage_update",
            "used": 10,
            "size": 100,
        })
        assert harness.session.late_turn_open
        await asyncio.sleep(0.6)
        assert harness.session.late_turn_open is False
        assert not any(isinstance(event, ResultEvent) for event in seen)
        watcher.cancel()
        with_suppress = asyncio.CancelledError
        try:
            await watcher
        except with_suppress:
            pass


@pytest.mark.asyncio
async def test_update_while_result_is_consumed_opens_late_turn(tmp_path: Path) -> None:
    """An update or permission that lands while the result is consumed must not sit unread."""
    async with _started(tmp_path, spec_quiet_period_s=0.2) as harness:
        permission: asyncio.Task[dict[str, Any]] | None = None
        async for event in harness.session.prompt(
            [{"type": "text", "text": "hello"}],
            display="hello",
            unattended=False,
        ):
            if isinstance(event, ResultEvent):
                harness.session.on_update({
                    "sessionId": harness.session.session_id,
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "after"},
                })
                permission = asyncio.create_task(harness.broker.request(
                    harness.session.session_id,
                    {"toolCallId": "late-call", "title": "Bash"},
                    [{"optionId": "once", "kind": "allow_once", "name": "Once"}],
                ))
                # Run the permission emit while the prompt generator is still
                # suspended on the result, which is the stranded-queue window.
                await asyncio.sleep(0)
        assert permission is not None
        assert harness.session._queue.empty()
        assert harness.session.late_turn_open

        async def watch() -> ResultEvent:
            async for event in harness.session.late_events():
                if isinstance(event, PermissionRequestEvent):
                    harness.broker.respond(event.request_id, True, session_id=event.session_id)
                if isinstance(event, ResultEvent):
                    return event
            raise AssertionError("late stream ended")

        event = await asyncio.wait_for(watch(), timeout=1.5)
        assert event.origin == "task-notification"
        assert event.wake_notice == _WAKE
        assert event.result == "after"
        assert await permission == {"outcome": "selected", "optionId": "once"}
        assert harness.session.late_turn_open is False


@pytest.mark.asyncio
async def test_close_finishes_open_late_turn(tmp_path: Path) -> None:
    async with _started(tmp_path, spec_quiet_period_s=30) as harness:
        harness.session.on_update({
            "sessionId": harness.session.session_id,
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "background"},
        })
        assert harness.session.late_turn_open

        async def watch() -> ResultEvent:
            async for event in harness.session.late_events():
                if isinstance(event, ResultEvent):
                    return event
            raise AssertionError("late stream ended")

        watcher = asyncio.create_task(watch())
        await asyncio.sleep(0)
        await harness.session.close()
        event = await asyncio.wait_for(watcher, timeout=1)
        assert event.origin == "task-notification"
        assert event.wake_notice == _WAKE
        assert event.result == "background"
        assert harness.session.late_turn_open is False


@pytest.mark.asyncio
async def test_foreground_prompt_closes_open_late_turn_first(tmp_path: Path) -> None:
    async with _started(tmp_path, spec_quiet_period_s=5) as harness:
        harness.session.on_update({
            "sessionId": harness.session.session_id,
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "background"},
        })
        assert harness.session.late_turn_open

        async def watch() -> ResultEvent:
            async for event in harness.session.late_events():
                if isinstance(event, ResultEvent):
                    return event
            raise AssertionError("late stream ended")

        watcher = asyncio.create_task(watch())
        await asyncio.sleep(0)
        events = await asyncio.wait_for(_collect(harness, "next"), timeout=5)
        late = await asyncio.wait_for(watcher, timeout=1)
        assert late.origin == "task-notification"
        assert late.wake_notice == _WAKE
        assert late.result == "background"
        assert harness.session.late_turn_open is False
        assert isinstance(events[-1], ResultEvent)
        assert events[-1].origin == ""


@pytest.mark.asyncio
async def test_update_for_other_session_is_ignored(tmp_path: Path) -> None:
    async with _started(tmp_path) as harness:
        harness.session.on_update({
            "sessionId": "someone-else",
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "secret"},
        })
        assert harness.session.late_turn_open is False
        harness.session.on_update({
            "sessionId": harness.session.session_id,
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "mine"},
        })
        assert harness.session.late_turn_open is True


@pytest.mark.asyncio
async def test_apply_sets_only_changed_options(tmp_path: Path) -> None:
    config = [
        _select("mode", "default", ["default", "acceptEdits", "plan"]),
        _select("model", "m1", ["m1", "m2"]),
    ]
    async with _started(tmp_path, config_options=config) as harness:
        await harness.session.apply(mode="auto", model="m2", thinking="")
        changed = [call for call in read_calls(harness.calls) if call["method"] == "session/set_config_option"]
        assert {(call["params"]["configId"], call["params"]["value"]) for call in changed} == {
            ("mode", "acceptEdits"),
            ("model", "m2"),
        }
        await harness.session.apply(mode="auto", model="m2", thinking="")
        again = [call for call in read_calls(harness.calls) if call["method"] == "session/set_config_option"]
        assert again == changed


@pytest.mark.asyncio
async def test_apply_unoffered_strict_mode_raises_and_sends_nothing(tmp_path: Path) -> None:
    config = [_select("mode", "default", ["default", "acceptEdits"])]
    async with _started(tmp_path, config_options=config) as harness:
        with pytest.raises(ModeNotApplied):
            await harness.session.apply(mode="plan", model="", thinking="")
        assert not any(call["method"] == "session/set_config_option" for call in read_calls(harness.calls))


@pytest.mark.asyncio
async def test_apply_unoffered_bypass_reports_status_and_keeps_current(tmp_path: Path) -> None:
    config = [_select("mode", "default", ["default", "acceptEdits"])]
    async with _started(tmp_path, config_options=config) as harness:
        events = await harness.session.apply(mode="bypass", model="", thinking="")
        assert events == [SystemStatusEvent(
            type="system",
            status="fake does not offer a mode for 'bypass'.",
        )]
        assert not any(call["method"] == "session/set_config_option" for call in read_calls(harness.calls))


@pytest.mark.asyncio
async def test_warm_session_mode_switch_failure_raises_before_any_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chat moved from bypass to normal must never run in bypass when the switch fails."""
    config = [_select("mode", "default", ["default", "bypassPermissions"])]
    async with _started(tmp_path, config_options=config) as harness:
        assert await harness.session.apply(mode="bypass", model="", thinking="") == []

        async def refuse(config_id: str, session_id: str, value: str, **_kwargs: Any) -> None:
            raise RequestError(-32602, "adapter says /Users/alice/settings.json is bad")

        monkeypatch.setattr(harness.connection.conn, "set_config_option", refuse)
        with pytest.raises(ModeNotApplied) as raised:
            await harness.session.apply(mode="normal", model="", thinking="")
        assert "alice" not in str(raised.value)
        # The adapter's error is not chained: a traceback logged for this would carry it.
        assert raised.value.__context__ is None
        assert not any(call["method"] == "session/prompt" for call in read_calls(harness.calls))


@pytest.mark.asyncio
async def test_failed_bypass_switch_reports_status_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "SECRET_CONFIG_789"
    caplog.set_level(logging.WARNING, logger="ciao.providers.acp.session")
    config = [_select("mode", "default", ["default", "bypassPermissions"])]
    async with _started(
        tmp_path,
        config_options=config,
        config_error=f"bad value {secret} in /Users/alice/settings.json",
    ) as harness:
        events = await harness.session.apply(mode="bypass", model="", thinking="")
        assert events == [SystemStatusEvent(type="system", status="fake could not set mode.")]
        assert secret not in repr(events)
        assert "/Users/alice" not in repr(events)
        assert secret not in caplog.text
        assert "could not set mode (RequestError)" in caplog.text


@pytest.mark.asyncio
async def test_failed_strict_switch_on_a_fresh_session_raises_with_no_adapter_text(
    tmp_path: Path,
) -> None:
    secret = "SECRET_CONFIG_790"
    # The adapter starts in bypass; auto resolves to default, which is a real switch.
    config = [_select("mode", "bypassPermissions", ["default", "bypassPermissions"])]
    async with _started(
        tmp_path,
        config_options=config,
        config_error=f"bad value {secret} in /Users/alice/settings.json",
    ) as harness:
        with pytest.raises(ModeNotApplied) as raised:
            await harness.session.apply(mode="auto", model="", thinking="")
        assert secret not in str(raised.value)
        assert "alice" not in str(raised.value)


_LEGACY_MODELS = [
    {"value": "claude-sonnet-4-6", "name": "Sonnet"},
    {"value": "claude-opus-4-8", "name": "Opus"},
]


def _model_select(options: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "id": "model", "name": "model", "type": "select", "currentValue": "claude-sonnet-4-6",
        "options": options,
    }


def _model_set_calls(calls: Path) -> list[tuple[str, str]]:
    return [
        (c["params"]["configId"], c["params"]["value"])
        for c in read_calls(calls)
        if c["method"] == "session/set_config_option" and c["params"]["configId"] == "model"
    ]


@pytest.mark.asyncio
async def test_bare_tier_name_maps_to_its_offered_model(tmp_path: Path) -> None:
    config = [_select("mode", "default", ["default", "bypassPermissions"]), _model_select(_LEGACY_MODELS)]
    async with _started(tmp_path, config_options=config) as harness:
        events = await harness.session.apply(mode="normal", model="opus", thinking="")
        assert _model_set_calls(harness.calls) == [("model", "claude-opus-4-8")]
        assert events == []


@pytest.mark.asyncio
async def test_offered_full_id_is_set_exactly_even_with_a_bracket_variant(tmp_path: Path) -> None:
    offered = [*_LEGACY_MODELS, {"value": "claude-opus-4-8[1m]", "name": "Opus 1M"}]
    config = [_select("mode", "default", ["default", "bypassPermissions"]), _model_select(offered)]
    async with _started(tmp_path, config_options=config) as harness:
        events = await harness.session.apply(mode="normal", model="claude-opus-4-8[1m]", thinking="")
        assert _model_set_calls(harness.calls) == [("model", "claude-opus-4-8[1m]")]
        assert events == []


@pytest.mark.asyncio
async def test_unoffered_full_id_is_not_guessed_to_a_tier_variant(tmp_path: Path) -> None:
    """A full id the agent does not offer is never mapped to a tier's model: nothing is set."""
    config = [_select("mode", "default", ["default", "bypassPermissions"]), _model_select(_LEGACY_MODELS)]
    async with _started(tmp_path, config_options=config) as harness:
        events = await harness.session.apply(mode="normal", model="claude-opus-4-8[1m]", thinking="")
        assert _model_set_calls(harness.calls) == []
        assert [e.status for e in events] == ["fake does not offer the model 'claude-opus-4-8[1m]'."]


@pytest.mark.asyncio
async def test_unoffered_model_status_echoes_at_most_80_characters(tmp_path: Path) -> None:
    """The chat's model string is user-controlled, so the PWA-visible status line is capped."""
    config = [_select("mode", "default", ["default", "bypassPermissions"]), _model_select(_LEGACY_MODELS)]
    long_model = "z" * 300
    async with _started(tmp_path, config_options=config) as harness:
        events = await harness.session.apply(mode="normal", model=long_model, thinking="")
        assert _model_set_calls(harness.calls) == []
        assert [e.status for e in events] == [f"fake does not offer the model '{'z' * 80}'."]


@pytest.mark.asyncio
async def test_bare_tier_with_only_a_variant_offered_is_not_set(tmp_path: Path) -> None:
    """A bare tier is never mapped to a long-context variant: the default stands and one line says so."""
    config = [_select("mode", "default", ["default", "bypassPermissions"]),
              _model_select([{"value": "sonnet[1m]", "name": "Sonnet 1M"}])]
    async with _started(tmp_path, config_options=config) as harness:
        events = await harness.session.apply(mode="normal", model="sonnet", thinking="")
        assert _model_set_calls(harness.calls) == []
        assert [e.status for e in events] == ["fake does not offer the model 'sonnet'."]


@pytest.mark.asyncio
async def test_unoffered_model_is_reported_once_per_session(tmp_path: Path) -> None:
    config = [_select("mode", "default", ["default", "bypassPermissions"]), _model_select(_LEGACY_MODELS)]
    async with _started(tmp_path, config_options=config) as harness:
        first = await harness.session.apply(mode="normal", model="gpt-5.6", thinking="")
        second = await harness.session.apply(mode="normal", model="gpt-5.6", thinking="")
        assert first == [SystemStatusEvent(type="system", status="fake does not offer the model 'gpt-5.6'.")]
        assert second == []


@pytest.mark.asyncio
async def test_unoffered_models_share_one_status_line_per_session(tmp_path: Path) -> None:
    config = [_select("mode", "default", ["default", "bypassPermissions"]), _model_select(_LEGACY_MODELS)]
    async with _started(tmp_path, config_options=config) as harness:
        first = await harness.session.apply(mode="normal", model="haiku", thinking="")
        second = await harness.session.apply(mode="normal", model="gpt-5.6", thinking="")
        third = await harness.session.apply(mode="normal", model="claude-opus-4-1", thinking="")
        assert [e.status for e in first] == ["fake does not offer the model 'haiku'."]
        assert second == []
        assert third == []
        assert _model_set_calls(harness.calls) == []


@pytest.mark.asyncio
async def test_tier_without_an_offered_match_is_reported_once_and_not_sent(tmp_path: Path) -> None:
    config = [_select("mode", "default", ["default", "bypassPermissions"]), _model_select(_LEGACY_MODELS)]
    async with _started(tmp_path, config_options=config) as harness:
        first = await harness.session.apply(mode="normal", model="haiku", thinking="")
        second = await harness.session.apply(mode="normal", model="haiku", thinking="")
        assert [e.status for e in first] == ["fake does not offer the model 'haiku'."]
        assert second == []
        assert not any(c["method"] == "session/set_config_option" for c in read_calls(harness.calls))


@pytest.mark.asyncio
async def test_load_failure_creates_new_session_and_reports_replaced(tmp_path: Path) -> None:
    async with _started(
        tmp_path,
        resume="gone",
        initialize={"loadSession": True},
        load_error="missing",
        sessions=["fresh-1"],
    ) as harness:
        assert harness.replaced is True
        assert harness.session.session_id == "fresh-1"
        methods = _methods(harness.calls)
        assert methods.index("session/load") < methods.index("session/new")


@pytest.mark.asyncio
async def test_load_replay_rebuilds_missing_journal_but_not_live_queue(tmp_path: Path) -> None:
    async with _started(
        tmp_path,
        resume="old-session",
        initialize={"loadSession": True},
        load_replay=[_text("from-before-the-switch")],
    ) as harness:
        assert harness.replaced is False
        assert harness.session.session_id == "old-session"
        assert harness.session.late_turn_open is False
        path = journal_path(tmp_path / "runtime", "fake", "old-session")
        rows = SessionJournal.read(path)
        assert any(row.get("text") == "from-before-the-switch" for row in rows)
        events = await _collect(harness, "now")
        assert all(getattr(event, "text", None) != "from-before-the-switch" for event in events)


@pytest.mark.asyncio
async def test_session_meta_sent_on_new_load_and_resume(tmp_path: Path) -> None:
    meta = {"claudeCode": {"options": {"disallowedTools": ["CronCreate"]}}}
    expected_key = json.dumps(meta, sort_keys=True)

    async def meta_of(directory: Path, method: str, **kwargs: Any) -> None:
        async with _started(directory, session_meta=meta, **kwargs) as harness:
            assert harness.session.meta_key == expected_key
            logged = next(call["params"]["_meta"] for call in read_calls(harness.calls) if call["method"] == method)
            assert logged == meta

    await meta_of(tmp_path / "new", "session/new")
    await meta_of(
        tmp_path / "load",
        "session/load",
        resume="old",
        initialize={"loadSession": True},
    )
    await meta_of(
        tmp_path / "resume",
        "session/resume",
        resume="old",
        initialize={"loadSession": False, "sessionCapabilities": {"resume": {}}},
    )


@pytest.mark.asyncio
async def test_resume_when_load_is_unsupported(tmp_path: Path) -> None:
    async with _started(
        tmp_path,
        resume="kept",
        initialize={"loadSession": False, "sessionCapabilities": {"resume": {}}},
    ) as harness:
        assert harness.replaced is False
        assert harness.session.session_id == "kept"
        methods = _methods(harness.calls)
        assert "session/resume" in methods
        assert "session/new" not in methods
        assert "session/load" not in methods


@pytest.mark.asyncio
async def test_additional_directories_sent_only_when_advertised(tmp_path: Path) -> None:
    async with _started(
        tmp_path / "yes",
        additional_dirs=("/work/extra",),
        initialize={"sessionCapabilities": {"additionalDirectories": {}}},
    ) as harness:
        params = next(call["params"] for call in read_calls(harness.calls) if call["method"] == "session/new")
        assert params["additionalDirectories"] == ["/work/extra"]
    async with _started(tmp_path / "no", additional_dirs=("/work/extra",), initialize={}) as harness:
        params = next(call["params"] for call in read_calls(harness.calls) if call["method"] == "session/new")
        assert params["additionalDirectories"] is None


@pytest.mark.asyncio
async def test_crash_mid_turn_yields_fixed_error_and_logs_stderr(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "SECRET_CRASH_TOKEN_456"
    caplog.set_level(logging.WARNING, logger="ciao.providers.acp.session")
    steps = [{"update": _text("partial")}, {"crash": True}]
    async with _started(
        tmp_path,
        stderr=[f"adapter {secret} read /Users/alice/.config/agent.toml"],
        prompts=[{"steps": steps}],
    ) as harness:
        events = await _collect(harness)
        assert any(getattr(event, "text", None) == "partial" for event in events)
        result = events[-1]
        assert isinstance(result, ResultEvent)
        assert result.is_error is True
        # Raw adapter stderr never reaches a stream event; the event is a fixed sentence.
        assert result.result == "fake stopped unexpectedly."
        for event in events:
            assert secret not in repr(event)
            assert "/Users/alice" not in repr(event)
        # ...and the tail stays diagnosable in the log.
        assert secret in caplog.text


@pytest.mark.asyncio
async def test_failed_config_set_logs_exception_and_reports_fixed_status(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "SECRET_CONFIG_789"
    caplog.set_level(logging.WARNING, logger="ciao.providers.acp.session")
    config = [_select("mode", "default", ["default", "bypassPermissions"])]
    async with _started(
        tmp_path,
        config_options=config,
        config_error=f"bad value {secret} in /Users/alice/settings.json",
    ) as harness:
        events = await harness.session.apply(mode="bypass", model="", thinking="")
        assert events == [SystemStatusEvent(type="system", status="fake could not set mode.")]
        assert secret not in repr(events)
        assert "/Users/alice" not in repr(events)
        assert secret not in caplog.text
        assert "could not set mode (RequestError)" in caplog.text


def test_journal_skips_torn_last_line(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    journal = SessionJournal(path)
    journal.user("one")
    journal.event({"type": "text_delta", "text": "two"})
    assert SessionJournal.read(path) == [
        {"type": "user", "text": "one", "turn_origin": ""},
        {"type": "text_delta", "text": "two"},
    ]
    with path.open("ab") as handle:
        handle.write(b'{"type": "user", "text": "torn"}')
    assert SessionJournal.read(path) == [
        {"type": "user", "text": "one", "turn_origin": ""},
        {"type": "text_delta", "text": "two"},
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_journal_path_is_hashed_and_private(tmp_path: Path) -> None:
    session_id = "session/1"
    path = journal_path(tmp_path, "claude", session_id)
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    assert path.name == f"{digest}.jsonl"
    assert path.parent.name == "claude"
    assert path.parent.parent.name == "acp-journal"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700


# -- AcpSession.on_ext and discard, without a live agent ------------------------------------------

class _NoConnection:
    alive = True
    agent_capabilities: Mapping[str, Any] = {}


def _bare_session(tmp_path: Path, spec: Any = None) -> AcpSession:
    from ciao.providers.acp.agents import OPENCODE

    return AcpSession(
        _NoConnection(),  # type: ignore[arg-type]
        spec or OPENCODE,
        session_id="ses_root",
        config_options=[],
        journal=SessionJournal(tmp_path / "journal.jsonl"),
        broker=PermissionBroker(lambda _event: None),
    )


def _recording_hook(events: list[Any] | None = None):
    calls: list[tuple[str, Any]] = []

    def hook(_normalizer: Any, method: str, payload: Any) -> list[Any]:
        calls.append((method, payload))
        return list(events or [])

    return hook, calls


def _text_event(text: str) -> Any:
    from ciao.models import AssistantTextDelta

    return AssistantTextDelta(type="text", text=text, parent_tool_use_id=None)


@pytest.mark.asyncio
async def test_on_ext_during_replay_is_not_delivered(tmp_path: Path) -> None:
    session = _bare_session(tmp_path)
    hook, calls = _recording_hook([_text_event("replayed")])
    session._replaying = True
    session.on_ext("opencode/session/child_update", {"rootSessionId": "ses_root"}, hook)
    assert calls == []
    assert session._queue.empty() and session._late_queue.empty()
    assert session.late_turn_open is False
    session._replaying = False
    session.on_ext("opencode/session/child_update", {"rootSessionId": "ses_root"}, hook)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [["not", "a", "mapping"], "text", None, 7])
async def test_on_ext_non_mapping_payload_is_ignored(tmp_path: Path, payload: Any) -> None:
    session = _bare_session(tmp_path)
    hook, calls = _recording_hook([_text_event("x")])
    session.on_ext("opencode/session/child_update", payload, hook)
    assert calls == []
    assert session._queue.empty() and session._late_queue.empty()
    assert session.late_turn_open is False


@pytest.mark.asyncio
async def test_on_ext_during_a_prompt_goes_to_the_turn_queue(tmp_path: Path) -> None:
    session = _bare_session(tmp_path)
    hook, _calls = _recording_hook([_text_event("in turn")])
    session._prompt_active = True
    session.on_ext("opencode/session/child_update", {"rootSessionId": "ses_root"}, hook)
    assert session._queue.get_nowait().text == "in turn"
    assert session._late_queue.empty()
    assert session.late_turn_open is False


@pytest.mark.asyncio
async def test_on_ext_after_a_turn_opens_a_late_turn(tmp_path: Path) -> None:
    session = _bare_session(tmp_path)
    hook, _calls = _recording_hook([_text_event("late")])
    session.on_ext("opencode/session/child_update", {"rootSessionId": "ses_root"}, hook)
    assert session.late_turn_open is True
    assert session._queue.empty()
    assert session._late_queue.get_nowait().text == "late"
    session._close_late()


@pytest.mark.asyncio
async def test_child_compaction_through_on_ext_is_not_reported_by_the_parent(tmp_path: Path) -> None:
    from ciao.models import ContextCompactedEvent
    from ciao.providers.acp.agents import OpencodeAcpProvider

    provider = OpencodeAcpProvider(tmp_path)
    session = _bare_session(tmp_path)
    session._prompt_active = True
    compaction = {
        "rootSessionId": "ses_root", "childSessionId": "ses_child", "parentSessionId": "ses_root",
        "depth": 1, "type": "update",
        "update": {"sessionUpdate": "compaction_update", "compactionId": "c1", "status": "completed"},
    }
    session.on_ext("opencode/session/child_update", compaction, provider.ext_update)
    # The parent's next update must not report the child's compaction.
    session.on_update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "p"}})
    queued = []
    while not session._queue.empty():
        queued.append(session._queue.get_nowait())
    assert not any(isinstance(event, ContextCompactedEvent) for event in queued)


@pytest.mark.asyncio
async def test_discard_deletes_when_the_agent_advertises_delete(tmp_path: Path) -> None:
    deleted: list[str] = []
    closed: list[str] = []

    class _Conn:
        alive = True
        agent_capabilities = {"sessionCapabilities": {"delete": {}, "close": {}}}

        class conn:  # noqa: N801 — stands in for the SDK connection
            @staticmethod
            async def delete_session(session_id: str, **_kwargs: Any) -> None:
                deleted.append(session_id)

            @staticmethod
            async def close_session(session_id: str, **_kwargs: Any) -> None:
                closed.append(session_id)

    session = _bare_session(tmp_path)
    session._connection = _Conn()  # type: ignore[assignment]
    await session.discard()
    assert (deleted, closed) == (["ses_root"], [])


@pytest.mark.asyncio
async def test_discard_closes_when_delete_is_not_advertised_and_never_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    closed: list[str] = []

    class _Conn:
        alive = True
        agent_capabilities = {"sessionCapabilities": {"close": {}}}

        class conn:  # noqa: N801
            @staticmethod
            async def close_session(session_id: str, **_kwargs: Any) -> None:
                closed.append(session_id)

            @staticmethod
            async def delete_session(session_id: str, **_kwargs: Any) -> None:  # pragma: no cover
                raise AssertionError("delete is not advertised")

    session = _bare_session(tmp_path)
    session._connection = _Conn()  # type: ignore[assignment]
    await session.discard()
    assert closed == ["ses_root"]

    class _Failing:
        alive = True
        agent_capabilities = {"sessionCapabilities": {"delete": {}}}

        class conn:  # noqa: N801
            @staticmethod
            async def delete_session(session_id: str, **_kwargs: Any) -> None:
                raise RuntimeError("/private/secret-path failed with token sk-123")

    session._connection = _Failing()  # type: ignore[assignment]
    caplog.set_level(logging.WARNING)
    await session.discard()  # no raise
    assert "secret-path" not in caplog.text and "sk-123" not in caplog.text
    assert "RuntimeError" in caplog.text
