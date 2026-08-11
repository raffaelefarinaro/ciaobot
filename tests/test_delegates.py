from __future__ import annotations

import asyncio
import json
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


def _make_manager(tmp_path: Path, **config_kwargs: object) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        **config_kwargs,
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


def test_wake_prompt_does_not_claim_chat_get_returns_a_transcript(
    tmp_path: Path,
) -> None:
    """chat_get returns metadata only, and no MCP tool returns messages.

    The first version of this prompt told the supervisor to "read the full
    transcript with chat_get", which silently does nothing useful: it hands back
    a ChatInfo. Caught on the first live delegate run.
    """
    manager = _make_manager(tmp_path)
    project = manager.create_project("Delegates", workspace="work")
    parent = manager.create_chat(project.project_id, title="Supervisor")
    child = _spawn_delegate(manager, parent, title="A")

    prompt = manager._build_delegate_wake_prompt(
        parent.chat_id,
        [{
            "chat_id": child.chat_id,
            "title": "A",
            "delegation_id": "",
            "reply": "all clean",
            "had_error": False,
        }],
    )

    assert "transcript with chat_get" not in prompt
    # It must point at the path that actually works, and say what chat_get is for.
    assert "session_id" in prompt
    assert "JSONL" in prompt
    assert "metadata only" in prompt
    # And it must still push the supervisor to verify rather than trust.
    assert "verify the claimed work yourself" in prompt


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


def test_wake_prompt_reports_a_quota_deferred_delegate_as_deferred_not_failed(
    tmp_path: Path,
) -> None:
    """A provider quota rejection sets had_error AND arms a deferred retry.

    Reporting that as FAILED tells the supervisor the work is dead when it will
    resume on its own, inviting a duplicate re-dispatch. Hit for real on the
    first live run: an Ollama weekly-limit 429 armed a 1h retry.
    """
    manager = _make_manager(tmp_path)
    project = manager.create_project("Delegates", workspace="work")
    parent = manager.create_chat(project.project_id, title="Supervisor")
    child = _spawn_delegate(manager, parent, title="Quota-limited")
    child.retry_status = "pending"
    child.retry_next_at = "2026-07-31T17:05:41Z"

    prompt = manager._build_delegate_wake_prompt(
        parent.chat_id,
        [{
            "chat_id": child.chat_id,
            "title": "Quota-limited",
            "delegation_id": "",
            "reply": "API Error: Request rejected (429) weekly usage limit",
            "had_error": True,
        }],
    )

    assert "DEFERRED, retrying at 2026-07-31T17:05:41Z" in prompt
    assert "FAILED" not in prompt
    assert "it is not dead" in prompt
    assert "Do not re-dispatch" in prompt


def test_wake_prompt_still_says_failed_for_a_genuinely_dead_delegate(
    tmp_path: Path,
) -> None:
    manager = _make_manager(tmp_path)
    project = manager.create_project("Delegates", workspace="work")
    parent = manager.create_chat(project.project_id, title="Supervisor")
    child = _spawn_delegate(manager, parent, title="Broken")
    assert child.retry_status == ""

    prompt = manager._build_delegate_wake_prompt(
        parent.chat_id,
        [{
            "chat_id": child.chat_id,
            "title": "Broken",
            "delegation_id": "",
            "reply": "traceback",
            "had_error": True,
        }],
    )

    assert "FAILED" in prompt
    assert "DEFERRED" not in prompt
    assert "it is not dead" not in prompt


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


# ── result notifications ─────────────────────────────────────────────────


def test_announce_result_ready_skips_delegates(tmp_path: Path, monkeypatch) -> None:
    """Delegate replies must not toast, badge, or push — the parent wake is enough."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Delegates", workspace="work")
    parent = manager.create_chat(project.project_id, title="Supervisor")
    child = _spawn_delegate(manager, parent, title="Fix #238")

    published: list[dict] = []
    pushes: list = []
    monkeypatch.setattr(manager._events, "publish", published.append)
    monkeypatch.setattr(
        manager, "_schedule_push", lambda *a, **k: pushes.append(a)
    )

    manager._announce_result_ready(
        child.chat_id,
        project.project_id,
        child.title,
        "Patched routes_api.py.",
    )
    assert published == []
    assert pushes == []

    manager._announce_result_ready(
        parent.chat_id,
        project.project_id,
        parent.title,
        "Delegates finished; here is the summary.",
    )
    ready = [ev for ev in published if ev.get("type") == "chat_result_ready"]
    assert len(ready) == 1
    assert ready[0]["chat_id"] == parent.chat_id
    assert len(pushes) == 1
    assert pushes[0][0] == parent.chat_id


async def test_delegate_turn_skips_result_announce_but_still_wakes_parent(
    tmp_path: Path, monkeypatch
) -> None:
    """A finishing delegate must wake the supervisor without user-facing alerts."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Delegates", workspace="work")
    parent = manager.create_chat(project.project_id, title="Supervisor")
    child = _spawn_delegate(manager, parent, title="Fix #238")
    # Non-default title so auto-title doesn't spawn a side task.
    child.title = "Fix #238"
    manager._save()

    async def fake_stream_chat(chat_id, prompt, images=None, **_kwargs):
        from ciao.models import ResultEvent

        yield ResultEvent(
            type="result",
            result="Fixed the bug in routes_api.py.",
            session_id="sess-delegate",
            is_error=False,
            effective_model=child.model,
            usage={},
            quota={},
            cost_usd=0.0,
        )

    manager.stream_chat = fake_stream_chat  # type: ignore[assignment]
    manager._push_delay_seconds = 0

    published: list[dict] = []
    pushes: list = []
    wakes: list[dict] = []

    monkeypatch.setattr(manager._events, "publish", published.append)
    monkeypatch.setattr(
        manager, "_schedule_push", lambda *a, **k: pushes.append(a)
    )

    def capture_wake(parent_id, **kwargs):
        wakes.append({"parent_id": parent_id, **kwargs})

    monkeypatch.setattr(manager, "_queue_delegate_wake", capture_wake)

    stream = manager.start_stream(child.chat_id, "fix the bug")
    async for _ in stream.subscribe():
        pass

    assert pushes == []
    assert not any(ev.get("type") == "chat_result_ready" for ev in published)
    assert len(wakes) == 1
    assert wakes[0]["parent_id"] == parent.chat_id
    assert wakes[0]["child_chat_id"] == child.chat_id
    assert "Fixed the bug" in wakes[0]["reply"]


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


def test_delegate_spawn_rejects_an_unconfigured_model(tmp_path: Path) -> None:
    """Issue #259: free-text model ids must be validated at dispatch time.

    ``claude_models`` defaults to ``["opus", "sonnet", "haiku"]``; an unknown
    id like ``deepseek-coder`` must surface as a clear ``invalid_model``
    error so the caller can correct the dispatch, instead of opening a chat
    that silently fails on its first turn.
    """
    manager = _make_manager(tmp_path)
    project = manager.create_project("Delegates", workspace="work")
    parent = manager.create_chat(project.project_id, title="Supervisor")
    manager.active_chat_ids = lambda: []  # type: ignore[method-assign]
    plane = _control_plane(manager)

    with pytest.raises(ControlPlaneError) as excinfo:
        plane.delegate_spawn(
            _principal(parent.chat_id, project.project_id),
            prompt="do the thing",
            model="deepseek-coder",
        )

    assert excinfo.value.code == "invalid_model"
    assert "deepseek-coder" in str(excinfo.value)
    # The error names valid alternatives so the agent can self-correct.
    assert "opus" in str(excinfo.value)
    assert "sonnet" in str(excinfo.value)
    assert "haiku" in str(excinfo.value)


def test_delegate_spawn_accepts_a_configured_model(tmp_path: Path) -> None:
    """A model id that is in the configured set must pass through cleanly."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Delegates", workspace="work")
    parent = manager.create_chat(project.project_id, title="Supervisor")
    manager.active_chat_ids = lambda: []  # type: ignore[method-assign]
    # Stub queue_message so delegate_spawn doesn't fall into the stream
    # path (which needs a live event loop in this sync test).
    manager.queue_message = lambda chat_id, text: True  # type: ignore[method-assign]
    plane = _control_plane(manager)

    result = plane.delegate_spawn(
        _principal(parent.chat_id, project.project_id),
        prompt="do the thing",
        model="haiku",
    )

    assert result["ok"] is True
    assert result["data"]["model"] == "haiku"


def test_create_chat_rejects_an_unconfigured_model(tmp_path: Path) -> None:
    """The validator runs on every ``create_chat`` path, not just delegates."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("Delegates", workspace="work")

    with pytest.raises(ValueError, match="Unknown model 'deepseek-coder'"):
        manager.create_chat(project.project_id, model="deepseek-coder")


def test_create_chat_validates_the_workspace_default_model(tmp_path: Path) -> None:
    """A stale workspace default must be rejected, not silently accepted.

    When ``model`` is omitted, ``chat_model`` resolves to the workspace
    default; validating only the explicit arg let an invalid default create
    a delegate that fails on its first turn (#259).
    """
    manager = _make_manager(tmp_path, claude_default_model="deepseek-coder")
    project = manager.create_project("Delegates", workspace="work")

    with pytest.raises(ValueError, match="Unknown model 'deepseek-coder'"):
        manager.create_chat(project.project_id, title="New Chat")


def test_create_chat_invalid_model_does_not_cleanup_empty_chats(
    tmp_path: Path,
) -> None:
    """A rejected model must not delete unrelated empty chats (#259).

    Validation runs before ``_cleanup_empty_chats``, so a failed create
    request leaves other abandoned drafts untouched.
    """
    manager = _make_manager(tmp_path)
    project = manager.create_project("Delegates", workspace="work")
    empty = manager.create_chat(project.project_id, title="New Chat")

    with pytest.raises(ValueError, match="Unknown model 'deepseek-coder'"):
        manager.create_chat(project.project_id, title="New Chat", model="deepseek-coder")

    assert empty.chat_id in manager._chats


def test_delegate_spawn_preserves_non_model_errors(tmp_path: Path) -> None:
    """An unknown provider must not be relabeled as ``invalid_model`` (#259).

    Only the specific unknown-model failure translates to ``invalid_model``;
    other ``create_chat`` rejections keep their own identity so the MCP
    boundary reports them as ``invalid_request``.
    """
    manager = _make_manager(tmp_path)
    project = manager.create_project("Delegates", workspace="work")
    parent = manager.create_chat(project.project_id, title="Supervisor")
    manager.active_chat_ids = lambda: []  # type: ignore[method-assign]
    plane = _control_plane(manager)

    with pytest.raises(ValueError) as excinfo:
        plane.delegate_spawn(
            _principal(parent.chat_id, project.project_id),
            prompt="do the thing",
            provider="bogus",
        )
    assert not isinstance(excinfo.value, ControlPlaneError)
    assert "Unknown provider 'bogus'" in str(excinfo.value)


def test_create_chat_rejects_unknown_ollama_cloud_model(tmp_path: Path) -> None:
    """A ``:tag``-shaped id not in the Ollama catalog must be rejected.

    The routing shape predicate (``is_ollama_model``) accepts any
    ``:tag``-shaped id when cloud is configured; validation must check
    catalog membership instead so a typo can't create a chat that fails
    on its first turn (#259).
    """
    from ciao.providers.ollama import OllamaSettings

    manager = _make_manager(
        tmp_path,
        ollama=OllamaSettings(
            api_key="test-key",
            models=("kimi-k2.7-code:cloud", "minimax-m3:cloud"),
        ),
    )
    project = manager.create_project("Delegates", workspace="work")

    with pytest.raises(ValueError, match="Unknown model 'typo-does-not-exist:cloud'"):
        manager.create_chat(project.project_id, model="typo-does-not-exist:cloud")

    # A catalog member passes.
    chat = manager.create_chat(project.project_id, model="minimax-m3:cloud")
    assert chat.model == "minimax-m3:cloud"


def test_create_chat_accepts_statically_configured_openrouter_model(
    tmp_path: Path,
) -> None:
    """A statically configured OpenRouter model stays valid without discovery.

    ``CIAO_OPENROUTER_MODELS`` models live in ``config.openrouter.models``
    but are only merged into ``claude_models`` when catalog discovery
    succeeds; the validator must accept them directly so a catalog outage
    doesn't block OpenRouter chat creation (#259).
    """
    from ciao.providers.openrouter import OpenRouterSettings

    manager = _make_manager(
        tmp_path,
        openrouter=OpenRouterSettings(
            api_key="test-key",
            models=("anthropic/claude-sonnet-latest", "meta-llama/llama-4-maverick"),
        ),
    )
    project = manager.create_project("Delegates", workspace="work")

    chat = manager.create_chat(
        project.project_id, model="meta-llama/llama-4-maverick"
    )
    assert chat.model == "meta-llama/llama-4-maverick"


def _write_custom_provider(tmp_path: Path, *, provider_id: str, models: list[str]) -> None:
    """Register a custom provider in the worktree config's tracked file."""
    path = tmp_path / ".ciao" / "custom_providers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "providers": [{
                "id": provider_id,
                "name": provider_id,
                "url": "http://localhost:1234/v1",
                "runner": "claude",
                "models": models,
            }]
        }),
        encoding="utf-8",
    )


def test_create_chat_custom_provider_requires_member_model(tmp_path: Path) -> None:
    """A ``custom:<id>:<model>`` id must name one of the provider's models.

    ``provider_for_model`` only verifies the provider id exists; the
    validator must also check the encoded model against the provider's
    configured list, else the typo reaches the endpoint on the first
    turn (#259).
    """
    _write_custom_provider(tmp_path, provider_id="lm-studio", models=["qwen2.5-coder"])
    manager = _make_manager(tmp_path)
    project = manager.create_project("Delegates", workspace="work")

    with pytest.raises(ValueError, match="Unknown model 'custom:lm-studio:typo'"):
        manager.create_chat(project.project_id, model="custom:lm-studio:typo")

    chat = manager.create_chat(project.project_id, model="custom:lm-studio:qwen2.5-coder")
    assert chat.model == "custom:lm-studio:qwen2.5-coder"


def test_create_chat_custom_provider_without_model_list_accepts_any(
    tmp_path: Path,
) -> None:
    """An empty custom-provider model list means no catalog to check.

    Endpoints like LM Studio serve dynamic model names, so an operator
    may configure a provider without enumerating models; any id for that
    provider stays valid (#259).
    """
    _write_custom_provider(tmp_path, provider_id="lm-studio", models=[])
    manager = _make_manager(tmp_path)
    project = manager.create_project("Delegates", workspace="work")

    chat = manager.create_chat(project.project_id, model="custom:lm-studio:anything")
    assert chat.model == "custom:lm-studio:anything"
