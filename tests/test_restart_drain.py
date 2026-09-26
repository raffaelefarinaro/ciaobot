from __future__ import annotations

from types import SimpleNamespace

import pytest

from ciao.main import _wait_for_chat_drain
from ciao.web.chat_broker import ChatStream, ChatStreamBroker, EventsHub
from ciao.web.project_chats import ProjectChatManager, RestartDrainingError
from ciao.web.subagent_watchers import SubagentWatchers


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
    manager._restart_draining = False
    manager._events = EventsHub()
    # The subagent watchers own the running counts and the live watcher
    # tasks, so a hand-built manager has to carry the collaborator; the two
    # assignments below write through to its state.
    manager._subagents = SubagentWatchers(manager)
    manager._background_agents_last = {}
    manager._pending_subagent_watchers = {}
    # Read by a cancelled drain's wake replay; wired after construction on a
    # real manager, so the hand-built one has to carry it.
    manager._background_runner = None
    # The cancelled-drain replay also scans the chat registry for CLI tasks.
    # Without it, the scan raises AttributeError and the replay's blanket
    # ``except`` swallows it — the tests below would pass through a logged
    # traceback while covering nothing.
    manager._chats = {}
    return manager


class _FakeRunner:
    """Only the surface a cancelled drain's replay touches."""

    def __init__(self, *, replays: int = 2, boom: Exception | None = None) -> None:
        self._replays = replays
        self._boom = boom
        self.replayed = 0
        self.started = 0

    def replay_pending_wakes(self) -> list[str]:
        self.replayed += 1
        if self._boom is not None:
            raise self._boom
        return [f"run-{n}" for n in range(self._replays)]

    def start(self) -> list[str]:
        self.started += 1
        return []


def test_active_chat_ids_include_unpolled_subagent_watchers() -> None:
    manager = _bare_manager()
    manager._broker.register("streaming", ChatStream())
    manager._background_agents_last = {"agents": 2}
    manager._pending_subagent_watchers = {
        "watcher": SimpleNamespace(done=lambda: False),
        "settled": SimpleNamespace(done=lambda: True),
    }

    assert manager.active_chat_ids() == ["agents", "streaming", "watcher"]


def test_begin_restart_drain_publishes_server_restarting() -> None:
    manager = _bare_manager()
    captured: list[dict] = []
    manager._events.publish = captured.append  # type: ignore[method-assign]

    manager.begin_restart_drain()
    manager.begin_restart_drain()  # idempotent — one event only

    assert manager._restart_draining is True
    assert captured == [{
        "type": "server_restarting",
        "message": "Ciaobot is waiting for active chats to finish before restarting",
    }]


def test_restart_drain_rejects_new_turns_but_keeps_existing_stream() -> None:
    manager = _bare_manager()
    existing = ChatStream("already running")
    manager._broker.register("running", existing)
    manager.begin_restart_drain()

    assert manager.start_stream("running", "ignored") is existing
    with pytest.raises(RestartDrainingError, match="waiting for active chats"):
        manager.start_stream("idle", "new work")


def _admission_refusal(manager: ProjectChatManager) -> BaseException | None:
    """Whatever ``start_stream`` raises for a new turn on a bare manager.

    A hand-built manager lacks the collaborators a real stream needs, so a
    failure *past* the admission check is expected here; what is under test is
    whether that failure is the drain refusal.
    """
    try:
        manager.start_stream("idle", "new work")
    except BaseException as exc:  # noqa: BLE001 — its type is the assertion
        return exc
    return None


# An update whose drain times out cancels it. Without this the running engine
# would keep refusing turns forever, which is a far worse outcome than the
# update it was meant to enable, and the PWA's restart overlay would stick on
# a perfectly healthy engine.
def test_cancel_restart_drain_reopens_admission() -> None:
    manager = _bare_manager()
    captured: list[dict] = []
    manager._events.publish = captured.append  # type: ignore[method-assign]

    manager.begin_restart_drain()
    assert isinstance(_admission_refusal(manager), RestartDrainingError)

    manager.cancel_restart_drain()
    manager.cancel_restart_drain()  # idempotent — one event only

    # Asserted before the reopen probe below: that probe now gets *past* the
    # drain check and opens a real stream, which announces itself, so reading
    # the drain's own events after it would mix the two.
    assert manager._restart_draining is False
    assert [event["type"] for event in captured] == [
        "server_restarting",
        "server_restart_cancelled",
    ]
    # Admission is genuinely open again, not just flagged off: the turn the
    # drain refused is admitted now, and admitting it starts a stream.
    assert not isinstance(_admission_refusal(manager), RestartDrainingError)
    assert [event["type"] for event in captured] == [
        "server_restarting",
        "server_restart_cancelled",
        "chat_streaming_started",
    ]


# The update drain routes have to tell *whose* drain this is — the update's own
# cancel reopens admission, a Settings restart's must not — and they read it
# through this rather than the private flag.
def test_restart_draining_is_readable_without_the_private_flag() -> None:
    manager = _bare_manager()
    assert manager.restart_draining is False

    manager.begin_restart_drain()
    assert manager.restart_draining is True

    manager.cancel_restart_drain()
    assert manager.restart_draining is False


# A drain defers a background run's wake for "the next start". A cancelled
# drain has no next start, so without the replay the run stays marked and its
# chat never learns the command finished.
def test_cancel_restart_drain_replays_deferred_wakes() -> None:
    manager = _bare_manager()
    runner = _FakeRunner()
    manager._background_runner = runner

    manager.begin_restart_drain()
    manager.cancel_restart_drain()

    assert runner.replayed == 1

    # Nothing was draining, so nothing was deferred: the cancel is a no-op
    # and must not touch the runner at all.
    idle = _bare_manager()
    idle_runner = _FakeRunner()
    idle._background_runner = idle_runner
    idle.cancel_restart_drain()
    assert idle_runner.replayed == 0


# ``BackgroundRunner.start`` also runs resolve_orphans() and prune(), which are
# boot-time jobs: they would resolve runs that are still live in this process.
def test_cancel_restart_drain_replays_without_calling_start() -> None:
    manager = _bare_manager()
    runner = _FakeRunner()
    manager._background_runner = runner

    manager.begin_restart_drain()
    manager.cancel_restart_drain()

    assert runner.replayed == 1
    assert runner.started == 0


# The cancel is the one thing guaranteed to reopen admission. A replay that
# fails must not take the reopen down with it.
def test_resume_after_cancelled_drain_is_total() -> None:
    manager = _bare_manager()
    manager._background_runner = _FakeRunner(boom=RuntimeError("store is gone"))
    captured: list[dict] = []
    manager._events.publish = captured.append  # type: ignore[method-assign]

    manager.begin_restart_drain()
    manager.cancel_restart_drain()

    assert manager._restart_draining is False
    assert [event["type"] for event in captured] == [
        "server_restarting",
        "server_restart_cancelled",
    ]
