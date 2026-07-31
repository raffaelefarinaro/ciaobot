from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao.config import CiaoConfig
from ciao.control_plane import CiaoControlPlane, ControlPlaneError, McpPrincipal
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import (
    _MAX_ACTIVE_DELEGATES,
    ChatInfo,
    ProjectChatManager,
    RestartDrainingError,
)


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )


class _RecordingManager:
    """Minimal stand-in capturing how a wake was delivered."""

    def __init__(self, *, queue_accepts: bool) -> None:
        self.queue_accepts = queue_accepts
        self.queued: list[tuple[str, str]] = []
        self.started: list[tuple[str, str, bool]] = []

    def queue_message(self, chat_id: str, text: str) -> bool:
        if self.queue_accepts:
            self.queued.append((chat_id, text))
            return True
        return False

    def start_stream(self, chat_id: str, text: str, **kwargs: object) -> None:
        self.started.append((chat_id, text, bool(kwargs.get("unattended", False))))


def _spawn_delegate(
    manager: ProjectChatManager, parent: ChatInfo, *, title: str, batch: str = ""
) -> ChatInfo:
    return manager.create_chat(
        parent.project_id,
        title=title,
        spawned_from_chat_id=parent.chat_id,
        delegation_id=batch,
    )


# ── lineage and serialization ────────────────────────────────────────────


def test_delegate_lineage_survives_a_state_roundtrip(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Delegates", workspace="work")
    parent = manager.create_chat(project.project_id, title="Supervisor")
    child = _spawn_delegate(manager, parent, title="Fix #238", batch="batch-1")
    manager._save()

    reloaded = _make_manager(tmp_path)
    restored = reloaded._chats[child.chat_id]
    assert restored.spawned_from_chat_id == parent.chat_id
    assert restored.delegation_id == "batch-1"
    # The supervisor itself must not look like anyone's delegate.
    assert reloaded._chats[parent.chat_id].spawned_from_chat_id == ""


def test_delegates_for_chat_lists_only_own_children(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Delegates", workspace="work")
    parent = manager.create_chat(project.project_id, title="Supervisor")
    other = manager.create_chat(project.project_id, title="Unrelated")
    a = _spawn_delegate(manager, parent, title="A")
    b = _spawn_delegate(manager, parent, title="B")
    _spawn_delegate(manager, other, title="Not mine")

    ids = [c.chat_id for c in manager.delegates_for_chat(parent.chat_id)]
    assert ids == [a.chat_id, b.chat_id]


def test_delegate_gets_supervisor_env_marker_and_parent_does_not(
    tmp_path: Path,
) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Delegates", workspace="work")
    parent = manager.create_chat(project.project_id, title="Supervisor")
    child = _spawn_delegate(manager, parent, title="Fix #238")

    assert manager._build_extra_env(child)["CIAO_DELEGATE_OF"] == parent.chat_id
    assert "CIAO_DELEGATE_OF" not in manager._build_extra_env(parent)


# ── wake prompt content ──────────────────────────────────────────────────


def test_wake_prompt_reports_failure_and_names_the_child_chat(
    tmp_path: Path,
) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Delegates", workspace="work")
    parent = manager.create_chat(project.project_id, title="Supervisor")
    child = _spawn_delegate(manager, parent, title="Fix #187")

    prompt = manager._build_delegate_wake_prompt(
        parent.chat_id,
        [{
            "chat_id": child.chat_id,
            "title": "Fix #187",
            "delegation_id": "",
            "reply": "Could not reproduce the loop.",
            "had_error": True,
        }],
    )

    assert "1 delegate finished" in prompt
    assert child.chat_id in prompt
    assert "FAILED" in prompt
    assert "Could not reproduce the loop." in prompt
    # The supervisor must be pushed to verify rather than trust the excerpt.
    assert "chat_get" in prompt


def test_wake_prompt_batches_and_notes_siblings_still_running(
    tmp_path: Path,
) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Delegates", workspace="work")
    parent = manager.create_chat(project.project_id, title="Supervisor")
    done_a = _spawn_delegate(manager, parent, title="A")
    done_b = _spawn_delegate(manager, parent, title="B")
    slow = _spawn_delegate(manager, parent, title="C")
    # Pretend the third delegate still has a turn in flight.
    manager.active_chat_ids = lambda: [slow.chat_id]  # type: ignore[method-assign]

    prompt = manager._build_delegate_wake_prompt(
        parent.chat_id,
        [
            {
                "chat_id": done_a.chat_id,
                "title": "A",
                "delegation_id": "",
                "reply": "done a",
                "had_error": False,
            },
            {
                "chat_id": done_b.chat_id,
                "title": "B",
                "delegation_id": "",
                "reply": "done b",
                "had_error": False,
            },
        ],
    )

    assert "2 delegates finished" in prompt
    assert f"1 still running: {slow.chat_id}" in prompt


def test_wake_prompt_handles_a_delegate_that_said_nothing(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Delegates", workspace="work")
    parent = manager.create_chat(project.project_id, title="Supervisor")
    child = _spawn_delegate(manager, parent, title="Silent")

    prompt = manager._build_delegate_wake_prompt(
        parent.chat_id,
        [{
            "chat_id": child.chat_id,
            "title": "Silent",
            "delegation_id": "",
            "reply": "   ",
            "had_error": False,
        }],
    )

    assert "(no final message)" in prompt


# ── coalescing and delivery ──────────────────────────────────────────────


def test_completions_inside_the_window_coalesce_into_one_wake(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "ciao.web.project_chats._DELEGATE_WAKE_WINDOW_SECONDS", 0.05
    )
    manager = _make_manager(tmp_path)
    project = manager.create_project("Delegates", workspace="work")
    parent = manager.create_chat(project.project_id, title="Supervisor")
    a = _spawn_delegate(manager, parent, title="A")
    b = _spawn_delegate(manager, parent, title="B")
    recorder = _RecordingManager(queue_accepts=False)
    manager.queue_message = recorder.queue_message  # type: ignore[method-assign]
    manager.start_stream = recorder.start_stream  # type: ignore[method-assign]

    async def scenario() -> None:
        for child in (a, b):
            manager._queue_delegate_wake(
                parent.chat_id,
                child_chat_id=child.chat_id,
                child_title=child.title,
                delegation_id="",
                reply=f"finished {child.title}",
                had_error=False,
            )
        # Both queued before the window elapses, so only one task should exist.
        assert len(manager._delegate_wake_tasks) == 1
        await asyncio.sleep(0.3)

    asyncio.run(scenario())

    assert len(recorder.started) == 1, recorder.started
    _, text, unattended = recorder.started[0]
    assert "2 delegates finished" in text
    # Bypass mode would let a woken supervisor act without approval cards.
    assert unattended is False
    assert manager._delegate_wake_pending.get(parent.chat_id) in (None, [])


def test_wake_queues_behind_a_live_supervisor_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "ciao.web.project_chats._DELEGATE_WAKE_WINDOW_SECONDS", 0.05
    )
    manager = _make_manager(tmp_path)
    project = manager.create_project("Delegates", workspace="work")
    parent = manager.create_chat(project.project_id, title="Supervisor")
    child = _spawn_delegate(manager, parent, title="A")
    recorder = _RecordingManager(queue_accepts=True)
    manager.queue_message = recorder.queue_message  # type: ignore[method-assign]
    manager.start_stream = recorder.start_stream  # type: ignore[method-assign]

    async def scenario() -> None:
        manager._queue_delegate_wake(
            parent.chat_id,
            child_chat_id=child.chat_id,
            child_title="A",
            delegation_id="",
            reply="done",
            had_error=False,
        )
        await asyncio.sleep(0.3)

    asyncio.run(scenario())

    # Queued as a follow-up; never force-started over the user's own turn.
    assert len(recorder.queued) == 1
    assert recorder.started == []


def test_cold_parent_still_gets_woken_by_starting_a_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gap the subagent synthesis nudge leaves open: no live session."""
    monkeypatch.setattr(
        "ciao.web.project_chats._DELEGATE_WAKE_WINDOW_SECONDS", 0.05
    )
    manager = _make_manager(tmp_path)
    project = manager.create_project("Delegates", workspace="work")
    parent = manager.create_chat(project.project_id, title="Supervisor")
    child = _spawn_delegate(manager, parent, title="A")
    # No provider registered for the parent at all, as after a restart.
    assert manager._providers.get(parent.chat_id) is None
    recorder = _RecordingManager(queue_accepts=False)
    manager.queue_message = recorder.queue_message  # type: ignore[method-assign]
    manager.start_stream = recorder.start_stream  # type: ignore[method-assign]

    async def scenario() -> None:
        manager._queue_delegate_wake(
            parent.chat_id,
            child_chat_id=child.chat_id,
            child_title="A",
            delegation_id="",
            reply="branch pushed",
            had_error=False,
        )
        await asyncio.sleep(0.3)

    asyncio.run(scenario())

    assert len(recorder.started) == 1
    assert "branch pushed" in recorder.started[0][1]


def test_no_wake_when_the_supervisor_is_archived_or_gone(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Delegates", workspace="work")
    parent = manager.create_chat(project.project_id, title="Supervisor")
    child = _spawn_delegate(manager, parent, title="A")
    parent.archived = True

    manager._queue_delegate_wake(
        parent.chat_id,
        child_chat_id=child.chat_id,
        child_title="A",
        delegation_id="",
        reply="done",
        had_error=False,
    )
    manager._queue_delegate_wake(
        "chat-does-not-exist",
        child_chat_id=child.chat_id,
        child_title="A",
        delegation_id="",
        reply="done",
        had_error=False,
    )

    assert manager._delegate_wake_pending == {}
    assert manager._delegate_wake_tasks == {}


def test_wake_survives_a_restart_drain_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "ciao.web.project_chats._DELEGATE_WAKE_WINDOW_SECONDS", 0.05
    )
    manager = _make_manager(tmp_path)
    project = manager.create_project("Delegates", workspace="work")
    parent = manager.create_chat(project.project_id, title="Supervisor")
    child = _spawn_delegate(manager, parent, title="A")

    def _draining(chat_id: str, text: str) -> bool:
        raise RestartDrainingError()

    manager.queue_message = _draining  # type: ignore[method-assign]

    async def scenario() -> None:
        manager._queue_delegate_wake(
            parent.chat_id,
            child_chat_id=child.chat_id,
            child_title="A",
            delegation_id="",
            reply="done",
            had_error=False,
        )
        await asyncio.sleep(0.3)

    # Must not propagate: the delegate's own turn teardown owns this task.
    asyncio.run(scenario())
    assert manager._delegate_wake_tasks == {}


# ── control plane guards ─────────────────────────────────────────────────


def _principal(chat_id: str, project_id: str) -> McpPrincipal:
    return McpPrincipal(
        token_id="t",
        chat_id=chat_id,
        project_id=project_id,
        workspace="work",
        provider="claude",
    )


def _control_plane(manager: ProjectChatManager) -> CiaoControlPlane:
    return CiaoControlPlane(
        SimpleNamespace(workspace=lambda name: object() if name == "work" else None),
        project_chat_manager=manager,
        schedule_manager=SimpleNamespace(),
        loop_manager=SimpleNamespace(),
    )


def test_a_delegate_cannot_spawn_delegates(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Delegates", workspace="work")
    parent = manager.create_chat(project.project_id, title="Supervisor")
    child = _spawn_delegate(manager, parent, title="A")
    plane = _control_plane(manager)

    with pytest.raises(ControlPlaneError) as excinfo:
        plane.delegate_spawn(
            _principal(child.chat_id, project.project_id), prompt="go deeper"
        )

    assert excinfo.value.code == "nested_delegate_forbidden"


def test_delegate_spawn_refuses_past_the_concurrency_cap(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Delegates", workspace="work")
    parent = manager.create_chat(project.project_id, title="Supervisor")
    running = []
    for i in range(_MAX_ACTIVE_DELEGATES):
        running.append(_spawn_delegate(manager, parent, title=f"D{i}").chat_id)
    manager.active_chat_ids = lambda: list(running)  # type: ignore[method-assign]
    plane = _control_plane(manager)

    assert manager.active_delegate_count(parent.chat_id) == _MAX_ACTIVE_DELEGATES
    with pytest.raises(ControlPlaneError) as excinfo:
        plane.delegate_spawn(
            _principal(parent.chat_id, project.project_id), prompt="one more"
        )

    assert excinfo.value.code == "delegate_limit_reached"


def test_finished_delegates_free_their_slot(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Delegates", workspace="work")
    parent = manager.create_chat(project.project_id, title="Supervisor")
    for i in range(_MAX_ACTIVE_DELEGATES):
        _spawn_delegate(manager, parent, title=f"D{i}")
    # Nothing streaming: every delegate has reported, so the cap is clear.
    manager.active_chat_ids = lambda: []  # type: ignore[method-assign]

    assert manager.active_delegate_count(parent.chat_id) == 0


def test_delegate_spawn_rejects_an_empty_prompt(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Delegates", workspace="work")
    parent = manager.create_chat(project.project_id, title="Supervisor")
    manager.active_chat_ids = lambda: []  # type: ignore[method-assign]
    plane = _control_plane(manager)

    with pytest.raises(ControlPlaneError) as excinfo:
        plane.delegate_spawn(
            _principal(parent.chat_id, project.project_id), prompt="   "
        )

    assert excinfo.value.code == "empty_prompt"
