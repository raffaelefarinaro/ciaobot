"""Post-archive pipeline visibility.

Archiving a chat starts one background task that extracts insights, folds the
project doc, saves a trajectory and files memory proposals. None of that used to
be visible anywhere in the app. These tests cover the state the PWA reads: what
is running now, and what an archived chat reports about itself afterwards.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ciao import job_runs as jr
from ciao.config import CiaoConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager, _restored_postprocess


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


def _chat(manager: ProjectChatManager) -> str:
    project = manager.create_project("Work", workspace="work")
    return manager.create_chat(project.project_id, title="A chat").chat_id


# ── Lifecycle ─────────────────────────────────────────────────────────────


def test_begin_marks_the_chat_as_being_tidied(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)

    manager._begin_postprocess(chat_id, ["insights", "trajectory"])

    chat = manager.get_chat(chat_id)
    assert chat.postprocess["state"] == "running"
    # The first step is named up front so a surface has something to say
    # immediately, before any step event has landed.
    assert chat.postprocess["step"] == "insights"
    assert chat.postprocess["expected"] == ["insights", "trajectory"]
    assert manager.postprocessing_chat_ids() == [chat_id]


def test_end_settles_the_record_and_persists_it(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)

    manager._begin_postprocess(chat_id, ["insights"])
    manager._end_postprocess(chat_id)

    chat = manager.get_chat(chat_id)
    assert chat.postprocess["state"] == "done"
    assert chat.postprocess["step"] == ""
    assert manager.postprocessing_chat_ids() == []
    # Persisted, because an archived chat opened next month should still be able
    # to report what was learned from it. The run log rotates; this does not.
    payload = json.loads(
        (tmp_path / ".runtime" / "web_projects.json").read_text(encoding="utf-8")
    )
    assert payload["chats"][chat_id]["postprocess"]["state"] == "done"


def test_step_events_fold_into_the_chat_record(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    manager.attach_job_runs_publisher()
    manager._begin_postprocess(chat_id, ["insights", "memory_proposals"])

    with jr.track_sync("insights", "Session insights", extra={"chat_id": chat_id}):
        assert manager.get_chat(chat_id).postprocess["step"] == "insights"
    with jr.track_sync(
        "memory_proposals", "Memory proposals", extra={"chat_id": chat_id}
    ) as run:
        run.extra["proposals"] = 3

    steps = manager.get_chat(chat_id).postprocess["steps"]
    assert steps["insights"]["status"] == "ok"
    # The count is what the archived chat reports back ("3 memory proposals").
    assert steps["memory_proposals"]["extra"]["proposals"] == 3


def test_a_failed_step_is_recorded_rather_than_swallowed(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    manager.attach_job_runs_publisher()
    manager._begin_postprocess(chat_id, ["insights"])

    try:
        with jr.track_sync("insights", "Session insights", extra={"chat_id": chat_id}):
            raise RuntimeError("model unavailable")
    except RuntimeError:
        pass

    steps = manager.get_chat(chat_id).postprocess["steps"]
    assert steps["insights"]["status"] == "error"


def test_events_for_unknown_chats_are_ignored(tmp_path: Path) -> None:
    """Most tracked jobs are not per-chat; they must not create phantom state."""
    manager = _make_manager(tmp_path)
    manager.attach_job_runs_publisher()

    with jr.track_sync("startup_sync", "Startup git sync", category="system"):
        pass
    with jr.track_sync("insights", "Session insights", extra={"chat_id": "ghost"}):
        pass

    assert manager.postprocessing_chat_ids() == []


def test_tracked_postprocess_settles_even_when_the_task_raises(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    manager._begin_postprocess(chat_id, ["insights"])

    async def failing() -> None:
        raise RuntimeError("pipeline blew up")

    async def drive() -> None:
        try:
            await manager._tracked_postprocess(chat_id, failing())
        except RuntimeError:
            pass

    asyncio.run(drive())

    # A crashed pipeline that stayed "running" would pulse forever.
    assert manager.get_chat(chat_id).postprocess["state"] == "done"
    assert manager.postprocessing_chat_ids() == []


# ── Restore across restarts ───────────────────────────────────────────────


def test_a_running_record_is_downgraded_on_load() -> None:
    """The pipeline is an in-process task: it died with the old process."""
    restored = _restored_postprocess({
        "state": "running",
        "step": "insights",
        "steps": {"insights": {"status": "ok", "extra": {}}},
    })
    assert restored["state"] == "done"
    assert restored["step"] == ""
    assert restored["interrupted"] is True
    # Whatever did land is kept: it is still true and still worth showing.
    assert restored["steps"]["insights"]["status"] == "ok"


def test_a_settled_record_survives_load_unchanged() -> None:
    original = {"state": "done", "steps": {"trajectory": {"status": "ok"}}}
    assert _restored_postprocess(original) == original
    assert "interrupted" not in _restored_postprocess(original)


def test_missing_or_junk_records_load_as_empty() -> None:
    assert _restored_postprocess(None) == {}
    assert _restored_postprocess({}) == {}
    assert _restored_postprocess("nonsense") == {}


def test_a_restart_mid_pipeline_leaves_no_chat_pulsing(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path)
    chat_id = _chat(manager)
    manager._begin_postprocess(chat_id, ["insights"])
    manager._save()

    reloaded = _make_manager(tmp_path)

    assert reloaded.postprocessing_chat_ids() == []
    assert reloaded.get_chat(chat_id).postprocess["state"] == "done"
