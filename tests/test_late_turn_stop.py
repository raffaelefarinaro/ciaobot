"""Stop on a late (wake) turn cancels the agent and discards what the chat was waiting on (#3460).

The late-turn cases run the real ACP path: the fake agent subprocess, the real
ProviderService and AcpProvider, and the ProjectChatManager Stop path. A late
turn is one the agent starts after its prompt has returned, which is how a
background shell's wake or a native task notification arrives.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ciao.models import ShellFinishedEvent
from tests.acp_helpers import read_calls
from tests.test_acp_provider import _install
from tests.test_chat_stop_force_close import _make_manager, _wait_for

from .conftest import attach_stub_mcp


def _shell(call_id: str) -> ShellFinishedEvent:
    return ShellFinishedEvent(
        type="shell_finished", tool_use_id=call_id, command="make", duration_s=6.0,
        exit_code=None, ended_at=0.0,
    )


def _cancels(calls: Path) -> list[dict]:
    return [call for call in read_calls(calls) if call["method"] == "session/cancel"]


def _late_text(text: str) -> dict:
    return {"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}}


async def test_stop_on_a_late_turn_sends_cancel_once_and_clears_late_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _config, calls = _install(
        monkeypatch, tmp_path,
        prompts=[{"steps": [_late_text("first")]}],
        after_prompt=[_late_text("late words")],
        after_prompt_delay_ms=20,
        spec_quiet_period_s=30,
    )
    manager = _make_manager(tmp_path / "mgr")
    monkeypatch.setattr("ciao.web.project_chats.supported_providers", lambda: ["fake"], raising=False)
    attach_stub_mcp(manager)
    project = manager.create_project("late", workspace="work")
    chat = manager.create_chat(project.project_id, provider="fake")
    manager._STOP_GRACE_S = 0.5

    stream = manager.start_stream(chat.chat_id, "go")

    async def consume() -> None:
        async for _event in stream.subscribe():
            pass

    consumer = asyncio.create_task(consume())
    try:
        await _wait_for(lambda: stream.done)
        await _wait_for(lambda: manager._streaming.has_live_drain(chat.chat_id))
        provider = manager._providers[chat.chat_id].provider
        await _wait_for(lambda: provider.background_work_outstanding)

        assert await asyncio.wait_for(manager.stop_chat(chat.chat_id), timeout=10) is True
        await _wait_for(lambda: manager._broker.get(chat.chat_id) is None)

        # The fake agent logs from its own process: wait until the cancel is on disk.
        await _wait_for(lambda: len(_cancels(calls)) >= 1)
        assert len(_cancels(calls)) == 1
        assert provider.background_work_outstanding is False
        assert provider.current_session_id is not None
        assert provider._session is not None and provider._session.late_turn_open is False

        # A second Stop finds nothing running: it reports False and sends no second cancel.
        assert await asyncio.wait_for(manager.stop_chat(chat.chat_id), timeout=10) is False
        assert len(_cancels(calls)) == 1
    finally:
        consumer.cancel()
        service = manager._providers.get(chat.chat_id)
        if service is not None:
            await service.disconnect()


async def test_stop_with_only_a_held_shell_notice_discards_it(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("held", workspace="work")
    chat = manager.create_chat(project.project_id, title="held", provider="claude")

    manager._note_shell_finished(chat.chat_id, _shell("call-1"))
    assert manager._shell_finished_pending[chat.chat_id]
    assert (chat.chat_id, "call-1") in manager._shell_notice_due

    assert await asyncio.wait_for(manager.stop_chat(chat.chat_id), timeout=5) is False

    assert chat.chat_id not in manager._shell_finished_pending
    assert chat.chat_id not in manager._shell_finished_waiting
    assert chat.chat_id not in manager._shell_finished_flushes
    assert manager._shell_notice_due == {}


async def test_stop_with_nothing_running_changes_nothing(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("idle", workspace="work")
    chat = manager.create_chat(project.project_id, title="idle", provider="claude")
    before = (dict(manager._shell_finished_pending), dict(manager._shell_notice_due))

    assert await asyncio.wait_for(manager.stop_chat(chat.chat_id), timeout=5) is False
    assert await asyncio.wait_for(manager.stop_chat(chat.chat_id), timeout=5) is False

    assert (dict(manager._shell_finished_pending), dict(manager._shell_notice_due)) == before
    assert manager._broker.get(chat.chat_id) is None


async def test_missing_provider_stop_cancels_the_turn_and_clears_the_stream(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    manager._STOP_GRACE_S = 0.5
    project = manager.create_project("missing", workspace="work")
    chat = manager.create_chat(project.project_id, title="missing", provider="claude")

    started = asyncio.Event()

    async def hanging_stream_chat(chat_id, prompt, images=None, **_kwargs):
        started.set()
        await asyncio.Event().wait()
        yield  # pragma: no cover — never reached

    manager.stream_chat = hanging_stream_chat  # type: ignore[assignment]

    stream = manager.start_stream(chat.chat_id, "initial")
    consumer = asyncio.create_task(_drain(stream))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        assert manager._providers.get(chat.chat_id) is None
        assert manager._broker.get(chat.chat_id) is stream

        assert await asyncio.wait_for(manager.stop_chat(chat.chat_id), timeout=5) is True

        assert manager._broker.get(chat.chat_id) is None
        assert stream.turn_task is None or stream.turn_task.done()
    finally:
        consumer.cancel()


async def _drain(stream) -> None:
    async for _event in stream.subscribe():
        pass
