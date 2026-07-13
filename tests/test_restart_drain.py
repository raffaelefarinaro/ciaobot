from __future__ import annotations

from types import SimpleNamespace

import pytest

from ciao.main import _wait_for_chat_drain
from ciao.web.chat_broker import ChatStream, ChatStreamBroker
from ciao.web.project_chats import ProjectChatManager


class _SequencedManager:
    def __init__(self, states: list[list[str]]) -> None:
        self.states = list(states)
        self.calls = 0

    def active_chat_ids(self) -> list[str]:
        self.calls += 1
        if self.states:
            return self.states.pop(0)
        return []


@pytest.mark.asyncio
async def test_restart_wait_requires_stable_idle_window() -> None:
    manager = _SequencedManager(
        [["chat-running"], [], ["chat-synthesis"], [], [], []]
    )

    await _wait_for_chat_drain(
        manager,  # type: ignore[arg-type]
        poll_interval=0,
        idle_polls_required=3,
    )

    assert manager.calls == 6


def _bare_manager() -> ProjectChatManager:
    manager = object.__new__(ProjectChatManager)
    manager._broker = ChatStreamBroker()
    manager._background_agents_last = {}
    manager._pending_subagent_watchers = {}
    manager._restart_draining = False
    return manager


def test_active_chat_ids_include_unpolled_subagent_watchers() -> None:
    manager = _bare_manager()
    manager._broker.register("streaming", ChatStream())
    manager._background_agents_last = {"agents": 2}
    manager._pending_subagent_watchers = {
        "watcher": SimpleNamespace(done=lambda: False),
        "settled": SimpleNamespace(done=lambda: True),
    }

    assert manager.active_chat_ids() == ["agents", "streaming", "watcher"]


def test_restart_drain_rejects_new_turns_but_keeps_existing_stream() -> None:
    manager = _bare_manager()
    existing = ChatStream("already running")
    manager._broker.register("running", existing)
    manager.begin_restart_drain()

    assert manager.start_stream("running", "ignored") is existing
    with pytest.raises(RuntimeError, match="waiting for active chats"):
        manager.start_stream("idle", "new work")
