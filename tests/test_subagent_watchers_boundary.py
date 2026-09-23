"""The subagent watchers own their state and run without a ProjectChatManager.

Extracted from `ProjectChatManager` in #499. Two things are worth pinning
beyond the behaviour tests in `tests/test_chat_subagents.py`, which drive the
watcher through the manager exactly as before:

* the collaborator is drivable against a stub host, which is what "owns its
  state explicitly" has to mean in practice, and
* the manager's remaining attributes are views onto the collaborator's state,
  not second copies — a copy is how the constants in #500 drifted.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao.subagent_tracking import SubagentInfo
from ciao.web.subagent_watchers import NUDGE_DECLINED, SubagentWatchers


class _StubHost:
    """Everything `SubagentWatcherHost` declares, and nothing more."""

    def __init__(self) -> None:
        self._chats: dict[str, object] = {}
        self._config = SimpleNamespace(workspace_root=Path("/tmp"))
        self.published: list[dict] = []
        self._events = SimpleNamespace(publish=self.published.append)
        self._restart_draining = False
        self.saves = 0
        self.flushed: list[str] = []
        self.wakes: list[tuple[str, int]] = []

    def _save(self, *, reason: str = "registry_mutation") -> None:
        self.saves += 1

    def _agent_root_for_chat(self, chat_id: str) -> Path:
        return Path("/tmp")

    def _parked_announce_token(self, chat_id: str) -> int | None:
        return None

    def _flush_result_announce(self, chat_id: str, token: int | None = None) -> bool:
        self.flushed.append(chat_id)
        return True

    def _arm_parked_announce_deadline(self, chat_id: str, token: int) -> None:
        raise AssertionError("no park in these tests")

    def _cli_owner_alive(self, chat_id: str) -> bool:
        return False

    def _is_interim_subagent_text(self, text: str) -> bool:
        return False

    async def _nudge_synthesis_after_subagents(
        self, chat_id: str, awaiting_user_answer: bool = False,
        already_reported: bool = False,
    ):
        return NUDGE_DECLINED

    def _deliver_wake(self, parent, prompt: str, *, count: int) -> str:
        self.wakes.append((parent.chat_id, count))
        return "queued"

    async def _watch_subagent_completion(self, chat_id: str, project_id: str) -> None:
        await self.watchers.watch(chat_id, project_id)

    async def _watch_subagent_completion_inner(
        self, chat_id: str, project_id: str, handed_to_drain: list[bool]
    ) -> None:
        await self.watchers.watch_inner(chat_id, project_id, handed_to_drain)


def _host() -> _StubHost:
    host = _StubHost()
    host.watchers = SubagentWatchers(host)  # type: ignore[attr-defined]
    return host


def test_publishing_a_count_needs_no_manager() -> None:
    host = _host()
    watchers: SubagentWatchers = host.watchers  # type: ignore[attr-defined]

    watchers.publish_count("chat-1", "proj-1", 2)

    assert host.published == [{
        "type": "chat_subagents_ready",
        "chat_id": "chat-1",
        "project_id": "proj-1",
        "remaining": 2,
        "nudged": False,
    }]
    assert watchers.running_counts() == {"chat-1": 2}
    assert watchers.running_count("chat-1") == 2

    # A zero is remembered but drops out of the >0 view the sidebar reads.
    watchers.publish_count("chat-1", "proj-1", 0)
    assert watchers.running_counts() == {}
    assert watchers.running_count("chat-1") == 0


def test_wake_bookkeeping_is_the_collaborators_own() -> None:
    host = _host()
    watchers: SubagentWatchers = host.watchers  # type: ignore[attr-defined]
    tasks = [SubagentInfo(agent_id="t1", subagent_type="Monitor", description="log")]
    parent = SimpleNamespace(chat_id="chat-1", project_id="proj-1")

    assert watchers.unwoken_tasks("chat-1", tasks) == tasks
    watchers.wake_for_dead_cli_tasks(parent, "proj-1", tasks)
    assert host.wakes == [("chat-1", 1)]

    # Recorded before delivery, so a failed wake is never re-armed.
    assert watchers.unwoken_tasks("chat-1", tasks) == []
    watchers.wake_for_dead_cli_tasks(parent, "proj-1", tasks)
    assert host.wakes == [("chat-1", 1)]

    # Per chat, not global: another chat's identical task still wakes.
    other = SimpleNamespace(chat_id="chat-2", project_id="proj-1")
    watchers.wake_for_dead_cli_tasks(other, "proj-1", tasks)
    assert host.wakes == [("chat-1", 1), ("chat-2", 1)]


@pytest.mark.asyncio
async def test_a_watcher_replaces_the_previous_one_for_its_chat() -> None:
    host = _host()
    watchers: SubagentWatchers = host.watchers  # type: ignore[attr-defined]
    # No chat registered, so `watch_inner` returns immediately and the outer
    # watcher runs its cleanup — which is the part under test.
    watchers.start("chat-1", "proj-1")
    first = watchers.watchers["chat-1"]
    watchers.start("chat-1", "proj-1")
    second = watchers.watchers["chat-1"]

    assert second is not first
    await asyncio.gather(first, second, return_exceptions=True)
    assert watchers.watching_chat_ids() == set()


def test_the_manager_exposes_the_collaborators_state_not_a_copy() -> None:
    """`ProjectChatManager` keeps the old attribute names as views."""
    from ciao.web.project_chats import ProjectChatManager

    manager = object.__new__(ProjectChatManager)
    manager._subagents = SubagentWatchers(manager)  # type: ignore[arg-type]

    manager._subagents.publish_count = lambda *a, **k: None  # type: ignore[method-assign]
    manager._subagents.last_counts["chat-1"] = 3
    assert manager._background_agents_last["chat-1"] == 3

    manager._background_agents_last = {"chat-2": 1}
    assert manager._subagents.last_counts == {"chat-2": 1}
    assert manager._subagents.running_count("chat-2") == 1

    manager._pending_subagent_watchers["chat-3"] = SimpleNamespace(done=lambda: False)
    assert manager._subagents.watching_chat_ids() == {"chat-3"}

    manager._cli_task_wakes_sent.add(("chat-4", "t1"))
    assert manager._subagents.wakes_sent == {("chat-4", "t1")}
