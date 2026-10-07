"""``tasks_changed`` on ``/ws/events``: the board's moves reach the whole app.

The task board is read on ``/tasks`` only, but the sidebar's review count and
Home's "needs you" tier read the same records everywhere. They re-read when the
engine publishes ``{"type": "tasks_changed", "workspace": <name>}``, so these pin
the one choke point that does it: both stores call back after every write that
landed, and the control plane turns that into one event per workspace.

Real stores and a real control plane over throwaway vaults; the chat manager is
the delegation suite's recording fake with an events hub attached.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ciao.control_plane import ControlPlaneError
from ciao.task_attempts import TaskAttemptStore
from ciao.task_board import TaskBoardError, TaskBoardStore
from tests.test_task_delegation import (
    _act,
    _agent_says_done,
    _create,
    _delegate,
    _end_turns,
    _world,
)


class _Hub:
    """The ``EventsHub`` surface the control plane uses, recorded with its thread."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []
        self.threads: list[int] = []

    def publish(self, payload: dict[str, Any]) -> None:
        self.published.append(payload)
        self.threads.append(threading.get_ident())

    def tasks_changed(self) -> list[dict[str, Any]]:
        return [p for p in self.published if p.get("type") == "tasks_changed"]


def _clock() -> datetime:
    return datetime.now(UTC)


async def _drain() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


# ── The stores call back after a write, and only then ───────────────────


def test_task_store_calls_back_after_each_landed_write(tmp_path: Path) -> None:
    calls: list[str] = []
    store = TaskBoardStore(
        workspace="personal",
        vault_root=tmp_path / "vault",
        runtime_dir=tmp_path / "runtime",
        clock=_clock,
        on_change=lambda: calls.append("changed"),
    )
    created = store.create(title="One")
    assert calls == ["changed"]

    store.get(created.record.id)
    store.list()
    assert calls == ["changed"], "a read is not a change"

    updated = store.update(
        created.record.id, expected_revision=created.revision, changes={"title": "Two"},
        actor="user",
    )
    assert calls == ["changed", "changed"]

    with pytest.raises(TaskBoardError):
        store.update(
            created.record.id, expected_revision=created.revision, changes={"title": "Stale"},
            actor="user",
        )
    assert len(calls) == 2, "a refused write announces nothing"

    store.delete(created.record.id, expected_revision=updated.revision)
    assert len(calls) == 3


def test_attempt_store_calls_back_after_each_landed_write(tmp_path: Path) -> None:
    calls: list[str] = []
    store = TaskAttemptStore(
        workspace="personal",
        runtime_dir=tmp_path / "runtime",
        clock=_clock,
        on_change=lambda: calls.append("changed"),
    )
    attempt = store.start(task_id="a" * 32, chat_id="c1", task_revision="b" * 64).attempt
    assert calls == ["changed"]
    store.get(attempt.attempt_id)
    store.live_by_task()
    assert calls == ["changed"]
    store.finish(attempt.attempt_id, "needs_you")
    assert calls == ["changed", "changed"]


# ── The control plane publishes one event per workspace ────────────────


async def test_route_writes_publish_tasks_changed_with_the_workspace_name(
    tmp_path: Path,
) -> None:
    plane, pcm = _world(tmp_path)
    hub = _Hub()
    pcm.events = hub

    task = _create(plane, title="File it")
    await _drain()
    assert hub.tasks_changed() == [{"type": "tasks_changed", "workspace": "personal"}]

    updated = plane.workspace_task_update(
        "personal",
        task["id"],
        expected_revision=task["revision"],
        changes={"title": "Renamed"},
        actor="user",
    )
    await _drain()
    assert len(hub.tasks_changed()) == 2

    plane.workspace_task_delete("personal", task["id"], expected_revision=updated["revision"])
    await _drain()
    assert len(hub.tasks_changed()) == 3

    other = _create(plane, "work", title="Elsewhere")
    await _drain()
    assert hub.tasks_changed()[-1] == {"type": "tasks_changed", "workspace": "work"}
    assert other["id"]


async def test_a_refused_write_publishes_nothing(tmp_path: Path) -> None:
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="File it")
    await _drain()
    hub = _Hub()
    pcm.events = hub

    with pytest.raises(ControlPlaneError):
        plane.workspace_task_update(
            "personal",
            task["id"],
            expected_revision="stale",
            changes={"title": "Nope"},
            actor="user",
        )
    plane.workspace_task_list("personal")
    await _drain()
    assert hub.tasks_changed() == []


async def test_the_writes_one_gesture_makes_coalesce_into_one_event(
    tmp_path: Path,
) -> None:
    """A delegation writes the attempt, links the task and binds the revision —
    several store writes in one loop callback, announced once."""
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Hand it over")
    await _drain()
    hub = _Hub()
    pcm.events = hub

    _delegate(plane, task)
    assert hub.tasks_changed() == [], "published on the loop, not inside the write"
    await _drain()
    assert hub.tasks_changed() == [{"type": "tasks_changed", "workspace": "personal"}]


async def test_a_write_in_a_worker_thread_publishes_on_the_loop(tmp_path: Path) -> None:
    """The routes run store calls through ``asyncio.to_thread``; the hub's queues
    want the loop thread, so the event is marshalled back to it."""
    plane, pcm = _world(tmp_path)
    hub = _Hub()
    pcm.events = hub

    await asyncio.to_thread(plane.workspace_task_create, "personal", title="Threaded")
    await _drain()
    assert hub.tasks_changed() == [{"type": "tasks_changed", "workspace": "personal"}]
    assert hub.threads == [threading.get_ident()]


async def test_a_turn_settling_needs_you_publishes(tmp_path: Path) -> None:
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Needs an approval")
    _delegate(plane, task)
    pcm.get_chat("chat-1").pending_permission = "Write /notes.md"
    await _drain()
    hub = _Hub()
    pcm.events = hub

    await _end_turns(pcm)

    assert plane.workspace_task_get("personal", task["id"])["attempt_state"] == "needs_you"
    assert hub.tasks_changed() == [{"type": "tasks_changed", "workspace": "personal"}]


async def test_a_turn_reported_done_moving_to_review_publishes(tmp_path: Path) -> None:
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Finish the work")
    _delegate(plane, task)
    _agent_says_done(plane)
    await _drain()
    hub = _Hub()
    pcm.events = hub

    await _end_turns(pcm)

    assert plane.workspace_task_get("personal", task["id"])["status"] == "in_review"
    assert hub.tasks_changed(), "the move to In review is announced"
    assert all(e["workspace"] == "personal" for e in hub.tasks_changed())


async def test_an_attempt_gesture_publishes(tmp_path: Path) -> None:
    plane, pcm = _world(tmp_path)
    task = _create(plane, title="Stop me")
    outcome = _delegate(plane, task)
    await _drain()
    hub = _Hub()
    pcm.events = hub

    await _act(plane, outcome["attempt"]["attempt_id"], "stop", task_id=task["id"])
    await _drain()

    assert hub.tasks_changed() == [{"type": "tasks_changed", "workspace": "personal"}]
