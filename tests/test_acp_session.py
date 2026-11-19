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
from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse

from ciao.models import PermissionRequestEvent, ResultEvent, SystemStatusEvent
from ciao.providers.acp import connection as connection_module
from ciao.providers.acp.connection import AcpConnection
from ciao.providers.acp.permissions import PermissionBroker
from ciao.providers.acp.session import AcpSession, SessionJournal, journal_path
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
async def test_apply_unknown_mode_reports_status_not_error(tmp_path: Path) -> None:
    config = [_select("mode", "default", ["default", "acceptEdits"])]
    async with _started(tmp_path, config_options=config) as harness:
        events = await harness.session.apply(mode="plan", model="", thinking="")
        assert events == [SystemStatusEvent(
            type="system",
            status="fake does not offer a mode for 'plan'; keeping default.",
        )]
        assert not any(call["method"] == "session/set_config_option" for call in read_calls(harness.calls))


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
    config = [_select("mode", "default", ["default", "acceptEdits"])]
    async with _started(
        tmp_path,
        config_options=config,
        config_error=f"bad value {secret} in /Users/alice/settings.json",
    ) as harness:
        events = await harness.session.apply(mode="auto", model="", thinking="")
        assert events == [SystemStatusEvent(type="system", status="fake could not set mode.")]
        assert secret not in repr(events)
        assert "/Users/alice" not in repr(events)
        assert secret in caplog.text


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
