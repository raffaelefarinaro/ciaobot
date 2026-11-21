"""Stop on a late (wake) turn cancels the agent and drops only late/wake-turn state (#3460).

The late-turn cases run the real ACP path: the fake agent subprocess, the real
ProviderService and AcpProvider, and the ProjectChatManager Stop path. A late
turn is one the agent starts after its prompt has returned, which is how a
background shell's wake or a native task notification arrives.

A Stop of a foreground turn drops nothing the background work still needs:
tracked shells and held shell notices stay as they were.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ciao.models import ShellFinishedEvent
from ciao.web.chat_broker import ChatStream
from tests.acp_helpers import read_calls
from tests.test_acp_provider import _install
from tests.test_chat_stop_force_close import _make_manager, _wait_for

from .conftest import attach_stub_mcp


def _shell(call_id: str) -> ShellFinishedEvent:
    return ShellFinishedEvent(
        type="shell_finished", tool_use_id=call_id, command="make", duration_s=6.0,
        exit_code=None, ended_at=0.0,
    )


def _late_text(text: str) -> dict:
    return {"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}}


def _cancels(calls: Path) -> list[dict]:
    return [call for call in read_calls(calls) if call["method"] == "session/cancel"]


class _ContinuingProvider:
    """A provider whose own continuation claims shell notices; it has no turn to stop here."""

    continues_after_shell = True

    async def stop_active(self) -> bool:
        return False


async def _drain(stream: Any) -> None:
    async for _event in stream.subscribe():
        pass


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
    recorded: list[dict[str, Any]] = []
    monkeypatch.setattr(manager._transcripts, "record_turn", lambda request, **kwargs: recorded.append(kwargs))

    stream = manager.start_stream(chat.chat_id, "go")
    consumer = asyncio.create_task(_drain(stream))
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
        assert provider._session is not None and provider._session.late_turn_open is False

        # The partial reply is recorded once, as a stopped wake turn that wakes nobody.
        # (The foreground turn "first" is recorded by its own path, not counted here.)
        wake_turns = [kwargs for kwargs in recorded if kwargs.get("origin") == "background_run"]
        assert [kwargs["response_text"] for kwargs in wake_turns] == ["late words"]
        assert wake_turns[0]["is_partial"] is True
        assert manager._broker.get(chat.chat_id) is None

        # A second Stop finds nothing running: it reports False and sends no second cancel.
        assert await asyncio.wait_for(manager.stop_chat(chat.chat_id), timeout=10) is False
        assert len(_cancels(calls)) == 1
    finally:
        consumer.cancel()
        service = manager._providers.get(chat.chat_id)
        if service is not None:
            await service.disconnect()


async def test_stop_drops_a_claimed_notice_so_no_wake_follows(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("claim", workspace="work")
    chat = manager.create_chat(project.project_id, title="claim", provider="claude")
    manager._providers[chat.chat_id] = _ContinuingProvider()  # type: ignore[assignment]

    manager._note_shell_finished(chat.chat_id, _shell("call-1"))
    # The continuation claims the held notice (the session offers its ends; here none, the drain's held one).
    assert manager._continuation_started(chat.chat_id, []) is not None
    assert manager._claimed_notices[chat.chat_id]

    assert await asyncio.wait_for(manager.stop_chat(chat.chat_id), timeout=5) is False

    assert chat.chat_id not in manager._claimed_notices
    assert chat.chat_id not in manager._claimed_shell_ids
    # The next late close finds nothing to adopt or deliver: no wake after Stop.
    assert manager._take_claimed_notice(chat.chat_id, adopt=False) is None
    assert manager._broker.get(chat.chat_id) is None


async def test_stop_with_only_a_held_shell_notice_discards_it(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("held", workspace="work")
    chat = manager.create_chat(project.project_id, title="held", provider="claude")

    manager._note_shell_finished(chat.chat_id, _shell("call-1"))
    assert (chat.chat_id, "call-1") in manager._shell_notice_due

    assert await asyncio.wait_for(manager.stop_chat(chat.chat_id), timeout=5) is False

    assert chat.chat_id not in manager._shell_finished_pending
    assert chat.chat_id not in manager._shell_finished_waiting
    assert chat.chat_id not in manager._shell_finished_flushes
    assert manager._shell_notice_due == {}


async def test_foreground_stop_keeps_held_shell_notices(tmp_path: Path) -> None:
    # A Stop of the foreground reasoning does not cost the running shell its wake.
    manager = _make_manager(tmp_path)
    project = manager.create_project("fg", workspace="work")
    chat = manager.create_chat(project.project_id, title="fg", provider="claude")

    manager._note_shell_finished(chat.chat_id, _shell("call-1"))
    manager._broker.register(chat.chat_id, ChatStream(background=False))

    assert await asyncio.wait_for(manager.stop_chat(chat.chat_id), timeout=5) is False

    assert [e.tool_use_id for e in manager._shell_finished_pending[chat.chat_id]] == ["call-1"]
    assert (chat.chat_id, "call-1") in manager._shell_notice_due


async def test_stop_with_nothing_running_changes_nothing(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("idle", workspace="work")
    chat = manager.create_chat(project.project_id, title="idle", provider="claude")
    before = (dict(manager._shell_finished_pending), dict(manager._shell_notice_due))

    assert await asyncio.wait_for(manager.stop_chat(chat.chat_id), timeout=5) is False
    assert await asyncio.wait_for(manager.stop_chat(chat.chat_id), timeout=5) is False

    assert (dict(manager._shell_finished_pending), dict(manager._shell_notice_due)) == before
    assert manager._broker.get(chat.chat_id) is None


@pytest.mark.parametrize("suppress", ["brief", "stuck"])
async def test_missing_provider_stop_is_bounded_when_the_turn_suppresses_cancel(
    tmp_path: Path, suppress: str
) -> None:
    manager = _make_manager(tmp_path)
    manager._STOP_GRACE_S = 0.5
    project = manager.create_project("missing", workspace="work")
    chat = manager.create_chat(project.project_id, title="missing", provider="claude")
    started = asyncio.Event()
    release = asyncio.Event()

    async def suppressing_stream_chat(chat_id, prompt, images=None, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # A turn that swallows the cancel: either it finishes soon, or it ignores every
            # cancel until the test releases it.
            if suppress == "brief":
                await asyncio.sleep(0.1)
                return
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    pass
            return
        yield  # pragma: no cover

    manager.stream_chat = suppressing_stream_chat  # type: ignore[assignment]
    stream = manager.start_stream(chat.chat_id, "initial")
    consumer = asyncio.create_task(_drain(stream))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        assert manager._providers.get(chat.chat_id) is None

        # Bounded: the Stop returns within its grace even though the turn ignores the cancel.
        assert await asyncio.wait_for(manager.stop_chat(chat.chat_id), timeout=5) is True
        assert manager._broker.get(chat.chat_id) is None
    finally:
        release.set()
        consumer.cancel()
