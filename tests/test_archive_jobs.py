"""Resumable post-archive pipeline: archive-job manifest acceptance tests.

Covers the AI-02 acceptance criteria:

* a failure injected after each stage is recoverable independently, including
  once insights already exist;
* a retry does not duplicate region entries, proposal rows, learning recurrence
  counts or project updates;
* deleting an archived chat tombstones its job and cannot resurrect it;
* missing ownership, changed archive content, model unavailability and terminal
  failures produce explicit recoverable or blocked states.

The pipeline itself is exercised against a synthetic vault/guide; no model call
is ever made (the extraction function is monkeypatched where it is reached).
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

from ciao import archive_jobs as aj
from ciao import insights
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ArchiveOutcome, ProjectChatManager

INSIGHTS_STAMP = "<!-- ciao:session-insights -->"


# ── Fixtures ──────────────────────────────────────────────────────────────


def _config(tmp_path: Path) -> CiaoConfig:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )


def _manager(tmp_path: Path) -> ProjectChatManager:
    config = _config(tmp_path)
    runtime = config.state_path.parent
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )


def _archive(tmp_path: Path, body: str = "# chat\n\nbody\n") -> Path:
    archive = tmp_path / "archive.md"
    archive.write_text(body, encoding="utf-8")
    return archive


def _stamped_archive(tmp_path: Path, insights_body: str = "- a fact\n") -> Path:
    return _archive(
        tmp_path,
        f"# chat\n\n{INSIGHTS_STAMP}\n## Session insights\n\n{insights_body}",
    )


def _job_inputs(
    tmp_path: Path,
    archive: Path,
    *,
    config: CiaoConfig | None = None,
    model: str = "test-model",
    **overrides: object,
) -> dict:
    config = config or _config(tmp_path)
    inputs: dict = {
        "archive_path": archive,
        "config": config,
        "model": model,
        "provider": "claude",
        "session_id": "sess-1",
        "filtered_jsonl": "",
        "text_mode": True,
        "trajectory_meta": {"chat_id": "chat-1", "workspace": "work"},
        "workspace_root": tmp_path,
        "vault_root": tmp_path / "vault",
        "proposal_vault_root": None,
        "guide_path": None,
        "trajectories_enabled": False,
        "memory_proposals_enabled": False,
        "project_doc_path": "",
    }
    inputs.update(overrides)
    return inputs


def _job(tmp_path: Path, archive: Path, **kwargs: object) -> aj.ArchiveJob:
    return aj.create_job(
        tmp_path / ".runtime",
        chat_id=str(kwargs.pop("chat_id", "chat-1")),
        archive_path=str(archive),
        content_revision_value=aj.archive_content_revision(archive),
    )


def _patch_insights_model(monkeypatch: pytest.MonkeyPatch, output: str) -> None:
    async def fake_call(filtered_jsonl: str, model: str, **kwargs: object) -> str:
        return output

    async def fake_text_call(body: str, model: str, **kwargs: object) -> str:
        return output

    monkeypatch.setattr(insights, "_call_model", fake_call)
    monkeypatch.setattr(insights, "_call_text_model", fake_text_call)


# ── Manifest basics ───────────────────────────────────────────────────────


def test_manifest_round_trips_and_survives_a_reload(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    job = _job(tmp_path, archive)
    job.mark("trajectory", aj.FAILED, "provider unavailable")
    job.save()

    reloaded = aj.load_job(tmp_path / ".runtime", job.job_id)
    assert reloaded is not None
    assert reloaded.status_of("trajectory") == aj.FAILED
    assert reloaded.stages["trajectory"].reason == "provider unavailable"
    assert reloaded.state == "incomplete"
    assert reloaded.unfinished() == ["trajectory"]


def test_version_mismatch_does_not_resume_a_stale_manifest(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    job = _job(tmp_path, archive)
    job.save()
    path = aj.job_path(tmp_path / ".runtime", job.job_id)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["pipeline_version"] = aj.PIPELINE_VERSION + 1
    path.write_text(json.dumps(raw), encoding="utf-8")

    assert aj.load_job(tmp_path / ".runtime", job.job_id) is None


def test_tombstone_cannot_be_cleared_by_a_late_write(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    job = _job(tmp_path, archive)
    job.save()
    aj.tombstone_job(tmp_path / ".runtime", job.job_id, reason="chat deleted")

    # A racing task finishes and tries to save its (live) job.
    job.mark("insights", aj.SUCCEEDED)
    job.save()

    reloaded = aj.load_job(tmp_path / ".runtime", job.job_id)
    assert reloaded is not None
    assert reloaded.tombstoned is True
    assert reloaded.state == aj.TOMBSTONED


def test_save_reports_a_write_failure(tmp_path: Path, monkeypatch) -> None:
    """A manifest write failure must be visible to the caller.

    The insights stage refuses to append when its pre-append evidence cannot be
    persisted, so `save()` has to report the failure instead of swallowing it.
    """
    archive = _archive(tmp_path)
    job = _job(tmp_path, archive)
    monkeypatch.setattr(aj, "_write_raw", lambda path, payload: False)
    assert job.save() is False


def test_load_job_returns_none_for_a_non_numeric_version(tmp_path: Path) -> None:
    """A malformed-but-valid manifest must not abort recovery.

    `list_jobs`/startup recovery iterate every file; one nonnumeric version
    value raising ValueError would prevent every other job from resuming.
    """
    archive = _archive(tmp_path)
    job = _job(tmp_path, archive)
    job.save()
    path = aj.job_path(tmp_path / ".runtime", job.job_id)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["pipeline_version"] = "not-a-number"
    path.write_text(json.dumps(raw), encoding="utf-8")

    assert aj.load_job(tmp_path / ".runtime", job.job_id) is None
    # And the directory listing still yields the other valid jobs.
    good = _job(tmp_path, archive, chat_id="chat-2")
    good.save()
    listed = {j.job_id for j in aj.list_jobs(tmp_path / ".runtime")}
    assert good.job_id in listed


def test_settled_job_drops_the_session_payload(tmp_path: Path) -> None:
    """A settled manifest must not retain the full filtered session JSONL."""
    archive = _archive(tmp_path)
    job = _job(tmp_path, archive)
    job.inputs["filtered_jsonl"] = "x" * 5000
    job.mark("insights", aj.SUCCEEDED)
    job.mark("trajectory", aj.SKIPPED, "no session input")
    job.save()

    reloaded = aj.load_job(tmp_path / ".runtime", job.job_id)
    assert reloaded is not None
    assert "filtered_jsonl" not in reloaded.inputs


def test_unfinished_job_keeps_the_session_payload(tmp_path: Path) -> None:
    """An unfinished insights/trajectory keeps the payload for the retry."""
    archive = _archive(tmp_path)
    job = _job(tmp_path, archive)
    job.inputs["filtered_jsonl"] = "payload"
    job.mark("insights", aj.FAILED, "boom")
    job.save()

    reloaded = aj.load_job(tmp_path / ".runtime", job.job_id)
    assert reloaded is not None
    assert reloaded.inputs.get("filtered_jsonl") == "payload"


def test_blocked_raw_input_stage_keeps_the_session_payload(tmp_path: Path) -> None:
    """A blocked insights/trajectory still needs the raw input.

    Startup blocks these stages when the archive is missing; the missing-file
    path supports a later restore, after which an explicit retry resets them to
    pending. Dropping the payload then would leave the trajectory permanently
    skipped and non-text-mode insights running against an empty transcript.
    """
    archive = _archive(tmp_path)
    job = _job(tmp_path, archive)
    job.inputs["filtered_jsonl"] = "payload"
    job.block("insights", "archive file is missing")
    job.block("trajectory", "archive file is missing")
    job.save()

    reloaded = aj.load_job(tmp_path / ".runtime", job.job_id)
    assert reloaded is not None
    assert reloaded.inputs.get("filtered_jsonl") == "payload"


def test_resumable_excludes_a_blocked_stage(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    job = _job(tmp_path, archive)
    job.block("trajectory", "archive content changed since the job was created")

    # A blocked precondition excludes the whole job from an automatic resume.
    assert job.state == "blocked"
    assert job.resumable() == []
    assert job.unfinished() == ["trajectory"]

    # An explicit retry clears the block.
    job.reset_failed(include_blocked=True)
    assert job.status_of("trajectory") == aj.PENDING
    assert job.resumable() == ["trajectory"]


def test_resumable_excludes_an_exhausted_stage(tmp_path: Path) -> None:
    """A model that fails terminally is not re-asked on every boot."""
    archive = _archive(tmp_path)
    job = _job(tmp_path, archive)
    for _ in range(aj.MAX_AUTO_ATTEMPTS):
        job.mark("trajectory", aj.RUNNING)
        job.mark("trajectory", aj.FAILED, "boom")

    assert job.unfinished() == ["trajectory"]
    assert job.resumable() == []

    # An explicit retry clears the exhaustion, so the user can still recover it.
    job.reset_failed(include_blocked=True)
    assert job.resumable() == ["trajectory"]
    assert job.stage("trajectory").attempts == 0


# ── The one stage ─────────────────────────────────────────────────────────


def test_trajectory_stage_writes_and_settles(tmp_path: Path, monkeypatch) -> None:
    archive = _archive(tmp_path)

    written: list[dict] = []

    def fake_trajectory(**kwargs: object) -> Path:
        written.append(kwargs)
        return tmp_path / "traj.json"

    monkeypatch.setattr(
        "ciao.trajectory_builder.build_and_persist_trajectory", fake_trajectory
    )
    job = _job(tmp_path, archive)
    inputs = _job_inputs(
        tmp_path,
        archive,
        session_id="sess-1",
        filtered_jsonl="line",
        trajectories_enabled=True,
    )

    asyncio.run(insights.run_archive_pipeline(job, inputs))

    assert job.status_of("trajectory") == aj.SUCCEEDED
    assert job.state == "done"
    assert written


def test_trajectory_stage_skips_without_session_input(
    tmp_path: Path, monkeypatch
) -> None:
    """Archive time is the only moment the raw JSONL exists, so no input, no record."""
    archive = _archive(tmp_path)

    def fail(**kwargs: object) -> Path:
        raise AssertionError("the trajectory must not be built without a session")

    monkeypatch.setattr(
        "ciao.trajectory_builder.build_and_persist_trajectory", fail
    )
    job = _job(tmp_path, archive)
    inputs = _job_inputs(tmp_path, archive, trajectories_enabled=False)

    asyncio.run(insights.run_archive_pipeline(job, inputs))

    assert job.status_of("trajectory") == aj.SKIPPED
    assert job.state == "done"


# ── Provider/write failures stay retryable, not success ───────────────────


def test_trajectory_write_failure_is_retryable(tmp_path: Path, monkeypatch) -> None:
    archive = _archive(tmp_path)

    def boom(trajectory: dict) -> Path:
        raise OSError("disk full")

    # Patch the internal write so the real `build_and_persist_trajectory`
    # catches it and reports the failure through `error_out`.
    monkeypatch.setattr("ciao.trajectory_builder.write_trajectory", boom)
    job = _job(tmp_path, archive)
    inputs = _job_inputs(
        tmp_path,
        archive,
        session_id="sess-1",
        filtered_jsonl="line",
        trajectories_enabled=True,
    )

    asyncio.run(insights.run_archive_pipeline(job, inputs))

    assert job.status_of("trajectory") == aj.FAILED
    assert "trajectory" in job.resumable()
    assert job.state == "incomplete"


def test_live_archive_plans_only_the_trajectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trajectory capture off means nothing to run, so no manifest at all."""
    manager = _manager(tmp_path)
    manager._config.trajectories_enabled = False
    project = manager.create_project("Work", workspace="work")
    chat = manager.create_chat(project.project_id, title="A private chat")
    archive = _archive(tmp_path)
    chat.archived = True
    chat.archive_path = str(archive.relative_to(tmp_path))
    manager._save()

    manager.run_archive_postprocess(
        chat.chat_id,
        ArchiveOutcome(archive, "sess-private", 1, '{"idx":1}'),
        chat,
        project,
    )

    job = aj.load_job(
        manager._runtime_root,
        aj.new_job_id(chat.chat_id, chat.archive_path),
    )
    assert job is None


@pytest.mark.asyncio
async def test_live_archive_plans_exactly_the_trajectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A new manifest carries the trajectory and nothing else."""
    manager = _manager(tmp_path)
    project = manager.create_project("Work", workspace="work")
    chat = manager.create_chat(project.project_id, title="A chat")
    archive = _archive(tmp_path)
    chat.archived = True
    chat.archive_path = str(archive.relative_to(tmp_path))
    manager._save()

    async def fake_pipeline(job: object, inputs: dict, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("ciao.insights.run_archive_pipeline", fake_pipeline)
    manager.run_archive_postprocess(
        chat.chat_id,
        ArchiveOutcome(archive, "sess-1", 1, '{"idx":1}'),
        chat,
        project,
    )
    await asyncio.sleep(0)

    job = aj.load_job(
        manager._runtime_root,
        aj.new_job_id(chat.chat_id, chat.archive_path),
    )
    assert job is not None
    assert list(job.stages) == ["trajectory"]
    assert list(aj.manifest_view(job)["steps"]) == ["trajectory"]
    # The postprocess record declares the same one-step plan, so a surface
    # cannot say "3 steps" for a pipeline that has one.
    assert manager.get_chat(chat.chat_id).postprocess["expected"] == ["trajectory"]


# ── Deletion / tombstone ──────────────────────────────────────────────────


def test_deleting_an_archived_chat_tombstones_and_cannot_resurrect(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    project = manager.create_project("Work", workspace="work")
    chat = manager.create_chat(project.project_id, title="A chat")
    archive = _archive(tmp_path)
    chat.archived = True
    chat.archive_path = str(archive.relative_to(tmp_path))
    manager._save()

    inputs = _job_inputs(tmp_path, archive, chat_id=chat.chat_id)
    job = manager._new_job_for_chat(chat, inputs)
    assert aj.load_job(manager._runtime_root, job.job_id).tombstoned is False

    assert manager.delete_chat(chat.chat_id) is True

    reloaded = aj.load_job(manager._runtime_root, job.job_id)
    assert reloaded is not None
    assert reloaded.tombstoned is True
    # A racing finish after the delete must not resurrect it.
    job.mark("insights", aj.SUCCEEDED)
    job.save()
    assert aj.load_job(manager._runtime_root, job.job_id).tombstoned is True


def test_resume_skips_a_tombstoned_job(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    job = _job(tmp_path, archive)
    job.save()
    aj.tombstone_job(tmp_path / ".runtime", job.job_id, reason="chat deleted")

    called: list[str] = []

    async def fake_pipeline(*args: object, **kwargs: object) -> None:
        called.append("ran")

    inputs = _job_inputs(tmp_path, archive)
    result = asyncio.run(insights.run_archive_pipeline(job, inputs))

    assert called == []
    assert result.tombstoned is True


def test_delete_cancels_the_in_flight_archive_task(tmp_path: Path) -> None:
    """Deleting a chat must cancel a stage that is mid-model-call.

    The tombstone flag alone only stops the *next* stage; a task already
    awaiting a model call would resume and write derived state for a deleted
    chat. `_cancel_archive_job` must cancel the retained task.
    """
    import asyncio as _asyncio

    manager = _manager(tmp_path)
    project = manager.create_project("Work", workspace="work")
    chat = manager.create_chat(project.project_id, title="A chat")
    archive = _archive(tmp_path)
    chat.archived = True
    chat.archive_path = str(archive.relative_to(tmp_path))
    manager._save()
    inputs = _job_inputs(tmp_path, archive, chat_id=chat.chat_id)
    job = manager._new_job_for_chat(chat, inputs)
    manager._archive_jobs[chat.chat_id] = job

    cancelled: list[bool] = []

    async def _never_finishes() -> None:
        try:
            await _asyncio.sleep(3600)
        except _asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def drive() -> None:
        task = _asyncio.create_task(_never_finishes())
        manager._archive_tasks[chat.chat_id] = task
        await _asyncio.sleep(0)
        manager.delete_chat(chat.chat_id)
        await _asyncio.sleep(0)
        assert task.cancelled() or task.cancelling()

    _asyncio.run(drive())

    assert cancelled == [True]
    assert chat.chat_id not in manager._archive_tasks


# ── Blocked / recoverable states ──────────────────────────────────────────


def test_changed_archive_content_blocks_before_resume(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    project = manager.create_project("Work", workspace="work")
    chat = manager.create_chat(project.project_id, title="A chat")
    archive = _archive(tmp_path)
    chat.archived = True
    chat.archive_path = str(archive.relative_to(tmp_path))
    inputs = _job_inputs(tmp_path, archive, chat_id=chat.chat_id)
    job = manager._new_job_for_chat(chat, inputs)

    # The archive changes under a job whose trajectory never landed.
    archive.write_text("# chat\n\nsomething else entirely\n", encoding="utf-8")

    asyncio.run(manager._run_job(chat.chat_id, job, inputs, stages=["trajectory"]))

    assert job.status_of("trajectory") == aj.BLOCKED
    assert "changed" in job.blocked_reason
    assert manager.get_chat(chat.chat_id).postprocess.get("state") == "blocked"


def test_missing_archive_blocks_the_job(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    job = _job(tmp_path, archive)
    inputs = _job_inputs(tmp_path, archive)
    archive.unlink()

    asyncio.run(insights.run_archive_pipeline(job, inputs, stages=["trajectory"]))

    assert job.status_of("trajectory") == aj.BLOCKED
    assert "missing" in job.stage("trajectory").reason


# ── Crash between the insights append and the stage mark ──────────────────


# A manifest written before #627 recorded the archive revision from *before* the
# pipeline appended its insights section, and the hash of the section it was
# about to write. Its archive carries that section now, so without the append
# authentication a resume would read the file as externally edited and block.
_APPENDED = (
    "\n\n<!-- ciao:session-insights -->\n## Session insights\n\n"
    "## Decisions\n- Chose X.\n"
)
# `resume_revision_matches` reads the section from the stamp on, so the recorded
# hash covers exactly that slice — not the blank lines the append prepended.
_APPENDED_SECTION = _APPENDED.lstrip("\n")


def _crashed_after_append(tmp_path: Path, *, edit_tail: str = "") -> tuple[Path, str, str]:
    """An archive with the pipeline's own section on it, plus the manifest's evidence.

    The recorded hash is always of the *un*edited section, so `edit_tail`
    reproduces the case the hash exists to catch: a body someone changed after
    the crash.
    """
    archive = _archive(tmp_path, "# chat\n\nbody\n")
    recorded = aj.archive_content_revision(archive)
    tail = _APPENDED.replace("Chose X.", edit_tail) if edit_tail else _APPENDED
    archive.write_text(archive.read_text(encoding="utf-8") + tail, encoding="utf-8")
    return archive, recorded, aj.text_revision(_APPENDED_SECTION)


def test_resume_accepts_the_pipelines_own_insights_append(tmp_path: Path) -> None:
    """A crash after the append must not look like an external edit."""
    archive, recorded, append_hash = _crashed_after_append(tmp_path)

    assert (
        aj.resume_revision_matches(archive, recorded, expected_append_revision=append_hash)
        is True
    )

    manager = _manager(tmp_path)
    project = manager.create_project("Work", workspace="work")
    chat = manager.create_chat(project.project_id, title="A chat")
    chat.archived = True
    chat.archive_path = str(archive.relative_to(tmp_path))
    inputs = _job_inputs(
        tmp_path,
        archive,
        chat_id=chat.chat_id,
        session_id="sess-1",
        filtered_jsonl="line",
        trajectories_enabled=True,
    )
    # A legacy manifest: the removed `insights` stage row is still there, and the
    # job is mid-pipeline, so the trajectory is exactly what has to run.
    job = _job(tmp_path, archive, chat_id=chat.chat_id)
    job.stages["insights"] = aj.StageState(status=aj.RUNNING, attempts=1)
    job.content_revision = recorded
    job.insights_append_revision = append_hash
    job.started = True
    job.save()

    assert job.resumable() == ["trajectory"]

    asyncio.run(manager._run_job(chat.chat_id, job, inputs, stages=["trajectory"]))

    # Not blocked: the append authenticated, and the trajectory settled.
    assert job.blocked_reason == ""
    assert job.status_of("trajectory") == aj.SUCCEEDED


def test_resume_refuses_an_edited_insights_tail(tmp_path: Path) -> None:
    """An edit confined to the appended body must not pass the crash check.

    The pre-insights prefix still matches the manifest revision, but the
    appended section no longer hashes to the recorded output, so the resume
    blocks instead of running against edited content.
    """
    archive, recorded, append_hash = _crashed_after_append(
        tmp_path, edit_tail="Chose Y."
    )

    assert aj.resume_revision_matches(archive, recorded) is False
    assert (
        aj.resume_revision_matches(archive, recorded, expected_append_revision=append_hash)
        is False
    )


def test_resume_refuses_a_prefix_match_without_append_evidence(tmp_path: Path) -> None:
    """No recorded append hash means a prefix match cannot be trusted."""
    archive, recorded, _hash = _crashed_after_append(tmp_path)

    assert aj.resume_revision_matches(archive, recorded) is False


def test_a_legacy_manifest_settles_instead_of_reading_incomplete(
    tmp_path: Path,
) -> None:
    """A manifest written by the four-stage pipeline must not strand the UI.

    Every method on `ArchiveJob` iterates `PIPELINE_STAGES`, never the raw
    `stages` dict, so the three removed rows a legacy file still carries are
    inert: the job reports done, nothing is resumable, and the PWA's retry
    affordance is not offered for work that no longer exists.
    """
    raw = {
        "manifest_version": aj.MANIFEST_VERSION,
        "pipeline_version": aj.PIPELINE_VERSION,
        "job_id": "legacy",
        "chat_id": "chat-1",
        "archive_path": "Chats/chat-1/claude/session.md",
        "state": "incomplete",
        "started": True,
        "stages": {
            "insights": {"status": "succeeded", "attempts": 1, "reason": ""},
            "project_doc_update": {"status": "succeeded", "attempts": 1, "reason": ""},
            "trajectory": {"status": "pending", "attempts": 0, "reason": ""},
            "memory_proposals": {"status": "failed", "attempts": 3, "reason": "boom"},
        },
        "inputs": {},
    }
    runtime = tmp_path / ".runtime"
    path = aj.job_path(runtime, "legacy")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw), encoding="utf-8")

    job = aj.load_job(runtime, "legacy")
    assert job is not None
    # The stale rows are still readable, so nothing is silently lost...
    assert set(job.stages) >= {"insights", "project_doc_update", "memory_proposals"}
    # ...but only the trajectory counts.
    assert job.resumable() == ["trajectory"]
    assert job.unfinished() == ["trajectory"]
    assert job.state == "incomplete"
    job.mark("trajectory", aj.SUCCEEDED)
    assert job.state == "done"
    assert job.resumable() == []
    assert job.unfinished() == []
    assert list(aj.manifest_view(job)["steps"]) == ["trajectory"]


def test_resume_blocks_a_genuine_external_edit(tmp_path: Path) -> None:
    archive = _archive(tmp_path, "# chat\n\nbody\n")
    recorded = aj.archive_content_revision(archive)
    archive.write_text("# chat\n\ncompletely different content\n", encoding="utf-8")

    assert aj.resume_revision_matches(archive, recorded) is False


def test_startup_resume_resets_an_interrupted_final_attempt(tmp_path: Path) -> None:
    """A crash during the last automatic attempt must stay retryable."""
    manager = _manager(tmp_path)
    project = manager.create_project("Work", workspace="work")
    chat = manager.create_chat(project.project_id, title="A chat")
    archive = _archive(tmp_path)
    chat.archived = True
    chat.archive_path = str(archive.relative_to(tmp_path))
    manager._save()
    inputs = _job_inputs(
        tmp_path,
        archive,
        chat_id=chat.chat_id,
        session_id="sess-1",
        filtered_jsonl="line",
        trajectories_enabled=True,
    )
    job = manager._new_job_for_chat(chat, inputs)
    stage = job.stage("trajectory")
    stage.status = aj.RUNNING
    stage.attempts = aj.MAX_AUTO_ATTEMPTS
    job.save()

    started = asyncio.run(manager.resume_interrupted_jobs(max_concurrency=1))

    # The stage was eligible: an un-reset interrupted final attempt would have
    # been excluded by `resumable()` and nothing would start.
    assert started == 1
    reloaded = aj.load_job(manager._runtime_root, job.job_id)
    assert reloaded is not None
    assert reloaded.status_of("trajectory") == aj.SUCCEEDED
    # The reset also gave it a fresh budget, so this was attempt 1 of a new run
    # rather than the fourth of the old one.
    assert reloaded.stage("trajectory").attempts == 1
    assert reloaded.state == "done"


def test_startup_resume_defers_trajectory_capture_while_disabled(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    manager._config.trajectories_enabled = False
    project = manager.create_project("Work", workspace="work")
    chat = manager.create_chat(project.project_id, title="A chat")
    archive = _archive(tmp_path)
    chat.archived = True
    chat.archive_path = str(archive.relative_to(tmp_path))
    manager._save()
    inputs = _job_inputs(
        tmp_path,
        archive,
        chat_id=chat.chat_id,
        trajectories_enabled=True,
    )
    job = manager._new_job_for_chat(chat, inputs)
    for stage in ("insights", "project_doc_update", "memory_proposals"):
        job.mark(stage, aj.SKIPPED, "not under test")
    job.save()

    started = asyncio.run(manager.resume_interrupted_jobs(max_concurrency=1))

    assert started == 0
    reloaded = aj.load_job(manager._runtime_root, job.job_id)
    assert reloaded is not None
    assert reloaded.status_of("trajectory") == aj.PENDING


def test_startup_resume_blocks_a_missing_archive(tmp_path: Path) -> None:
    """A missing archive settles blocked and is surfaced, not left stale."""
    manager = _manager(tmp_path)
    project = manager.create_project("Work", workspace="work")
    chat = manager.create_chat(project.project_id, title="A chat")
    archive = _archive(tmp_path)
    chat.archived = True
    chat.archive_path = str(archive.relative_to(tmp_path))
    manager._save()
    inputs = _job_inputs(tmp_path, archive, chat_id=chat.chat_id)
    job = manager._new_job_for_chat(chat, inputs)
    job.mark("insights", aj.RUNNING)
    job.save()
    archive.unlink()

    started = asyncio.run(manager.resume_interrupted_jobs(max_concurrency=1))

    assert started == 0
    reloaded = aj.load_job(manager._runtime_root, job.job_id)
    assert reloaded is not None
    assert reloaded.state == "blocked"
    assert "missing" in reloaded.blocked_reason
    postprocess = manager.get_chat(chat.chat_id).postprocess
    assert postprocess.get("state") == "blocked"
    assert postprocess.get("blocked_reason")


def test_startup_resumed_task_is_cancellable_by_delete(tmp_path: Path) -> None:
    """A startup-resumed job must be retained so a delete can cancel it.

    A resumed stage awaiting `update_project_doc` would otherwise write the
    canonical doc after the chat was deleted, because `_cancel_archive_job`
    only cancels `_archive_tasks`.
    """
    import asyncio as _asyncio

    manager = _manager(tmp_path)
    project = manager.create_project("Work", workspace="work")
    chat = manager.create_chat(project.project_id, title="A chat")
    archive = _archive(tmp_path)
    chat.archived = True
    chat.archive_path = str(archive.relative_to(tmp_path))
    manager._save()
    inputs = _job_inputs(tmp_path, archive, chat_id=chat.chat_id)
    job = manager._new_job_for_chat(chat, inputs)
    # Force a resumable job and a never-finishing run so we can inspect the
    # retained handle.
    job.mark("insights", aj.SUCCEEDED)
    job.post_insights_revision = aj.archive_content_revision(archive)
    job.save()

    cancelled: list[bool] = []

    async def _never_finishes(_chat_id: str, _job: object, _inputs: object, **kw: object) -> None:
        try:
            await _asyncio.sleep(3600)
        except _asyncio.CancelledError:
            cancelled.append(True)
            raise

    manager._run_job = _never_finishes  # type: ignore[assignment]

    async def drive() -> None:
        await manager.resume_interrupted_jobs(max_concurrency=1)
        await _asyncio.sleep(0)
        assert chat.chat_id in manager._archive_tasks
        manager.delete_chat(chat.chat_id)
        await _asyncio.sleep(0)

    _asyncio.run(drive())

    assert cancelled == [True]
    assert chat.chat_id not in manager._archive_tasks


def test_startup_resume_makes_interrupted_stages_retryable(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    project = manager.create_project("Work", workspace="work")
    chat = manager.create_chat(project.project_id, title="A chat")
    archive = _archive(tmp_path)
    chat.archived = True
    chat.archive_path = str(archive.relative_to(tmp_path))
    manager._save()
    inputs = _job_inputs(tmp_path, archive, chat_id=chat.chat_id)
    job = manager._new_job_for_chat(chat, inputs)
    # Simulate a crash mid-stage: the manifest says running.
    job.mark("insights", aj.RUNNING)
    job.mark("memory_proposals", aj.PENDING)
    job.save()

    ran: list[str] = []

    async def fake_pipeline(job_arg: object, inputs_arg: dict, **kwargs: object) -> None:
        ran.append("ran")

    import ciao.insights as _insights

    original = _insights.run_archive_pipeline
    _insights.run_archive_pipeline = fake_pipeline  # type: ignore[assignment]
    try:
        started = asyncio.run(manager.resume_interrupted_jobs(max_concurrency=1))
    finally:
        _insights.run_archive_pipeline = original  # type: ignore[assignment]

    assert started == 1
    assert ran == ["ran"]


def test_startup_resume_leaves_blocked_jobs_alone(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    project = manager.create_project("Work", workspace="work")
    chat = manager.create_chat(project.project_id, title="A chat")
    archive = _archive(tmp_path)
    chat.archived = True
    chat.archive_path = str(archive.relative_to(tmp_path))
    manager._save()
    inputs = _job_inputs(tmp_path, archive, chat_id=chat.chat_id)
    job = manager._new_job_for_chat(chat, inputs)
    job.block("trajectory", "archive content changed since the job was created")
    job.save()

    started = asyncio.run(manager.resume_interrupted_jobs(max_concurrency=1))

    assert started == 0
    reloaded = aj.load_job(manager._runtime_root, job.job_id)
    assert reloaded is not None
    assert reloaded.status_of("trajectory") == aj.BLOCKED


# ── Routes ────────────────────────────────────────────────────────────────


def _client(manager: ProjectChatManager):
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from ciao.web.routes_api import chat_archive_job, chat_retry_insights

    app = Starlette(
        routes=[
            Route(
                "/api/chats/{chat_id}/retry-insights",
                chat_retry_insights,
                methods=["POST"],
            ),
            Route(
                "/api/chats/{chat_id}/archive-job",
                chat_archive_job,
                methods=["GET"],
            ),
        ]
    )
    app.state.project_chat_manager = manager
    return TestClient(app)


def test_archive_job_route_reports_the_manifest(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    project = manager.create_project("Work", workspace="work")
    chat = manager.create_chat(project.project_id, title="A chat")
    archive = _archive(tmp_path)
    chat.archived = True
    chat.archive_path = str(archive.relative_to(tmp_path))
    inputs = _job_inputs(tmp_path, archive, chat_id=chat.chat_id)
    job = manager._new_job_for_chat(chat, inputs)
    job.mark("trajectory", aj.SUCCEEDED)
    job.save()

    resp = _client(manager).get(f"/api/chats/{chat.chat_id}/archive-job")

    assert resp.status_code == 200
    body = resp.json()["job"]
    assert body["steps"]["trajectory"]["status"] == "ok"
    assert list(body["steps"]) == ["trajectory"]
    assert body["unfinished"] == []


def test_archive_job_route_is_null_for_an_unprocessed_chat(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    project = manager.create_project("Work", workspace="work")
    chat = manager.create_chat(project.project_id, title="A chat")

    resp = _client(manager).get(f"/api/chats/{chat.chat_id}/archive-job")

    assert resp.status_code == 200
    assert resp.json() == {"job": None}


def test_archive_job_route_404s_unknown_chat(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    resp = _client(manager).get("/api/chats/ghost/archive-job")
    assert resp.status_code == 404


def test_retry_route_409s_a_non_archived_chat(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    project = manager.create_project("Work", workspace="work")
    chat = manager.create_chat(project.project_id, title="A chat")

    resp = _client(manager).post(f"/api/chats/{chat.chat_id}/retry-insights")

    assert resp.status_code == 409
    assert resp.json()["error"] == "chat is not archived"


def test_retry_route_reports_complete_when_nothing_is_unfinished(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    project = manager.create_project("Work", workspace="work")
    chat = manager.create_chat(project.project_id, title="A chat")
    archive = _stamped_archive(tmp_path)
    chat.archived = True
    chat.archive_path = str(archive.relative_to(tmp_path))
    inputs = _job_inputs(tmp_path, archive, chat_id=chat.chat_id)
    job = manager._new_job_for_chat(chat, inputs)
    for name in aj.PIPELINE_STAGES:
        job.mark(name, aj.SKIPPED, "nothing to do")
    job.save()

    resp = _client(manager).post(f"/api/chats/{chat.chat_id}/retry-insights")

    assert resp.status_code == 200
    assert resp.json()["status"] == "complete"



def test_deleting_a_chat_tombstones_a_job_not_in_the_process_cache(
    tmp_path: Path,
) -> None:
    """The on-disk manifest lookup must actually run on delete.

    `_cancel_archive_job` looked the chat up in `self._chats`, but `delete_chat`
    pops it first, so the lookup always returned None and both fallbacks (the
    manifest reload and the "no manifest yet" tombstone) were dead code. A job
    whose manifest is on disk but not cached — e.g. one a startup resume
    skipped as not resumable — survived the delete untombstoned.
    """
    manager = _manager(tmp_path)
    project = manager.create_project("Work", workspace="work")
    chat = manager.create_chat(project.project_id, title="A chat")
    archive = _archive(tmp_path)
    chat.archived = True
    chat.archive_path = str(archive.relative_to(tmp_path))
    manager._save()

    inputs = _job_inputs(tmp_path, archive, chat_id=chat.chat_id)
    job = manager._new_job_for_chat(chat, inputs)
    # Simulate a fresh process: the manifest is on disk, nothing is cached.
    manager._archive_jobs.clear()
    assert aj.load_job(manager._runtime_root, job.job_id).tombstoned is False

    assert manager.delete_chat(chat.chat_id) is True

    reloaded = aj.load_job(manager._runtime_root, job.job_id)
    assert reloaded is not None
    assert reloaded.tombstoned is True


def test_deleting_a_chat_tombstones_before_any_manifest_exists(
    tmp_path: Path,
) -> None:
    """The "no manifest yet" race guard the docstring promises must fire."""
    manager = _manager(tmp_path)
    project = manager.create_project("Work", workspace="work")
    chat = manager.create_chat(project.project_id, title="A chat")
    archive = _archive(tmp_path)
    chat.archived = True
    chat.archive_path = str(archive.relative_to(tmp_path))
    manager._save()

    job_id = aj.new_job_id(chat.chat_id, chat.archive_path)
    assert aj.load_job(manager._runtime_root, job_id) is None

    assert manager.delete_chat(chat.chat_id) is True

    tombstoned = aj.load_job(manager._runtime_root, job_id)
    assert tombstoned is not None
    assert tombstoned.tombstoned is True


def test_resume_recomputes_state_for_a_job_it_does_not_resume(
    tmp_path: Path,
) -> None:
    """A job the resume pass skips must not persist the dead process's state.

    `resume_interrupted_jobs` rewrites stage statuses directly rather than
    through `mark()`, so `_refresh_state()` never ran and the job-level
    `state: "running"` from the process that died was saved as-is. For a job
    this pass does not resume, `/api/chats/{id}/archive-job` then reported a
    pipeline that will never move as still in flight.
    """
    manager = _manager(tmp_path)
    project = manager.create_project("Work", workspace="work")
    chat = manager.create_chat(project.project_id, title="A chat")
    archive = _archive(tmp_path)
    chat.archived = True
    chat.archive_path = str(archive.relative_to(tmp_path))
    manager._save()

    inputs = _job_inputs(tmp_path, archive, chat_id=chat.chat_id)
    job = manager._new_job_for_chat(chat, inputs)
    assert list(job.stages) == ["trajectory"]
    job.mark("trajectory", aj.RUNNING)
    job.save()
    assert aj.load_job(manager._runtime_root, job.job_id).state == aj.RUNNING

    # The chat row is gone, so the pass rewrites the stages and skips the job.
    manager._chats.pop(chat.chat_id)
    started = asyncio.run(manager.resume_interrupted_jobs(max_concurrency=1))

    assert started == 0
    reloaded = aj.load_job(manager._runtime_root, job.job_id)
    assert reloaded is not None
    assert reloaded.status_of("trajectory") == aj.PENDING
    assert reloaded.state != aj.RUNNING
    assert reloaded.state == "incomplete"


def test_deleting_an_archived_chat_removes_its_transcript(tmp_path: Path) -> None:
    """Delete must stick: the archive is the source of truth for discovery.

    `_discover_archived_chats` re-imports any `<logs_root>/Chats/chat-*` that is
    not in the registry, so leaving the transcript behind brought the chat back
    on the next `list_projects()` poll — with the same chat_id and archive_path,
    hence the same `new_job_id`, which the delete had just tombstoned for good.
    The resurrected chat could then never run any archive stage again.
    """
    manager = _manager(tmp_path)
    project = manager.create_project("Work", workspace="work")
    chat = manager.create_chat(project.project_id, title="A chat")

    chat_dir = manager._config.logs_root / "Chats" / chat.chat_id / "claude"
    chat_dir.mkdir(parents=True, exist_ok=True)
    transcript = chat_dir / "2026-09-17T10-00-00.md"
    transcript.write_text(
        "---\ntitle: A chat\nworkspace: work\n---\n\n# A chat\n\nbody\n",
        encoding="utf-8",
    )
    chat.archived = True
    chat.archive_path = str(transcript.relative_to(manager._config.workspace_root))
    manager._save()

    assert manager.delete_chat(chat.chat_id) is True
    assert not (manager._config.logs_root / "Chats" / chat.chat_id).exists()

    # The next poll must not bring it back.
    manager.list_projects()
    assert chat.chat_id not in manager._chats


def test_deleting_a_live_chat_leaves_other_transcripts_alone(tmp_path: Path) -> None:
    """Only the deleted chat's own directory goes."""
    manager = _manager(tmp_path)
    project = manager.create_project("Work", workspace="work")
    keep = manager.create_chat(project.project_id, title="Keep")
    drop = manager.create_chat(project.project_id, title="Drop")

    for chat in (keep, drop):
        d = manager._config.logs_root / "Chats" / chat.chat_id / "claude"
        d.mkdir(parents=True, exist_ok=True)
        (d / "t.md").write_text(
            f"---\ntitle: {chat.title}\nworkspace: work\n---\n\nbody\n",
            encoding="utf-8",
        )
        chat.archived = True
        chat.archive_path = str(
            (d / "t.md").relative_to(manager._config.workspace_root)
        )
    manager._save()

    assert manager.delete_chat(drop.chat_id) is True
    assert not (manager._config.logs_root / "Chats" / drop.chat_id).exists()
    assert (manager._config.logs_root / "Chats" / keep.chat_id).is_dir()


def test_synthesised_tombstone_records_the_chat_it_belongs_to(
    tmp_path: Path,
) -> None:
    """A tombstone with empty fields is an untraceable file nothing collects."""
    manager = _manager(tmp_path)
    project = manager.create_project("Work", workspace="work")
    chat = manager.create_chat(project.project_id, title="A chat")
    archive = _archive(tmp_path)
    chat.archived = True
    chat.archive_path = str(archive.relative_to(tmp_path))
    manager._save()

    job_id = aj.new_job_id(chat.chat_id, chat.archive_path)
    assert aj.load_job(manager._runtime_root, job_id) is None

    assert manager.delete_chat(chat.chat_id) is True

    tombstoned = aj.load_job(manager._runtime_root, job_id)
    assert tombstoned is not None and tombstoned.tombstoned is True
    assert tombstoned.chat_id == chat.chat_id
    assert tombstoned.archive_path == chat.archive_path


def test_started_is_set_once_a_stage_actually_runs(tmp_path: Path) -> None:
    """`started` distinguishes a planned job from one that died mid-pipeline."""
    archive = _archive(tmp_path)
    job = _job(tmp_path, archive)
    assert job.started is False
    # A freshly planned, all-pending job is legitimately "running".
    assert job.state == aj.RUNNING

    stages = [n for n in aj.PIPELINE_STAGES if n in job.stages]
    job.mark(stages[0], aj.RUNNING)
    assert job.started is True
    job.save()
    assert aj.load_job(tmp_path / ".runtime", job.job_id).started is True


def test_a_died_mid_pipeline_job_reads_incomplete_not_running(
    tmp_path: Path,
) -> None:
    """The stuck-spinner case: pending work on a started job is not in flight."""
    archive = _archive(tmp_path)
    job = _job(tmp_path, archive)
    stages = [n for n in aj.PIPELINE_STAGES if n in job.stages]
    job.mark(stages[0], aj.RUNNING)
    # The process dies here; a resume resets the stage to pending.
    job.stage(stages[0]).status = aj.PENDING
    job._refresh_state()
    assert job.state == "incomplete"


def test_started_is_inferred_for_a_manifest_written_before_the_flag(
    tmp_path: Path,
) -> None:
    """Jobs already stuck mid-pipeline must be repaired, not just new ones.

    Every manifest written before `mark` began setting `started` carries
    `started: false`. Reading the flag literally would leave exactly the jobs
    the fix targets showing a spinner forever, so it is inferred from stage
    attempts — which only `mark(..., RUNNING)` increments.
    """
    archive = _archive(tmp_path)
    job = _job(tmp_path, archive)
    stages = [n for n in aj.PIPELINE_STAGES if n in job.stages]
    job.mark(stages[0], aj.RUNNING)
    job.save()

    # Rewrite the manifest the way the previous release left it.
    path = aj.job_path(tmp_path / ".runtime", job.job_id)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["started"] = False
    raw["state"] = aj.RUNNING
    raw["stages"][stages[0]]["status"] = aj.PENDING
    path.write_text(json.dumps(raw), encoding="utf-8")

    reloaded = aj.load_job(tmp_path / ".runtime", job.job_id)
    assert reloaded is not None
    assert reloaded.started is True
    reloaded._refresh_state()
    assert reloaded.state == "incomplete"


def test_a_never_run_manifest_is_still_planned_not_incomplete(
    tmp_path: Path,
) -> None:
    """The inference must not mistake a freshly planned job for a dead one."""
    archive = _archive(tmp_path)
    job = _job(tmp_path, archive)
    job.save()

    reloaded = aj.load_job(tmp_path / ".runtime", job.job_id)
    assert reloaded is not None
    assert reloaded.started is False
    reloaded._refresh_state()
    assert reloaded.state == aj.RUNNING


def test_a_stage_does_not_run_when_its_start_cannot_be_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unwritable manifest must stop the stage before it does any work.

    `job.mark(name, RUNNING)` was followed by an unchecked `job.save()`, so a
    stage whose start could not be recorded ran anyway: the model was called,
    and the project fold, trajectory or proposal write could land while the
    durable manifest still said pending — leaving startup to replay work that
    had already happened.

    The discriminator is the model call, not the final status: the insights
    append already refused without its evidence, so the stage ended FAILED
    either way. What changes is that it now fails *before* doing the work.
    """
    archive = _archive(tmp_path)
    called: list[str] = []

    async def fake_call(body: str, model: str, **kwargs: object) -> str:
        called.append("model")
        return "## Decisions\n- Chose sqlite. [idx=1]\n"

    monkeypatch.setattr(insights, "_call_text_model", fake_call)
    job = _job(tmp_path, archive)
    monkeypatch.setattr(job, "save", lambda: False)
    inputs = _job_inputs(tmp_path, archive)

    asyncio.run(insights.run_archive_pipeline(job, inputs, stages=["insights"]))

    assert job.status_of("insights") == aj.FAILED
    assert called == [], "the stage must not run once its start could not be recorded"


def test_a_manifest_is_written_owner_only(tmp_path: Path) -> None:
    """An unfinished job's manifest carries the conversation itself.

    `filtered_jsonl` holds the transcript with full tool inputs and results, and
    `Path.write_text` under the usual 022 umask created the temp file 0644,
    which `os.replace` preserved — so another local account could read private
    workspace data out of `.runtime/archive_jobs`.
    """
    archive = _archive(tmp_path)
    job = _job(tmp_path, archive)
    assert job.save() is True

    path = aj.job_path(tmp_path / ".runtime", job.job_id)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_manifest_rewrite_does_not_widen_an_existing_temp_file(
    tmp_path: Path,
) -> None:
    """O_CREAT leaves the mode alone, so a stale 0644 temp must be corrected."""
    archive = _archive(tmp_path)
    job = _job(tmp_path, archive)
    path = aj.job_path(tmp_path / ".runtime", job.job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    stale = path.with_name(f".{path.name}.tmp")
    stale.write_text("{}", encoding="utf-8")
    stale.chmod(0o644)

    assert job.save() is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_manifest_write_failure_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`save()` reports a failed write; it must never raise at its callers.

    The whole point of the bool return is that a stage mutation can refuse to
    proceed when its recovery evidence did not persist. Tightening the manifest
    to 0600 moved the write onto `os.open`/`os.fchmod`, which made it possible
    to raise *past* the `except OSError` — so pin the contract against the
    descriptor-level calls the hardening introduced.
    """
    archive = _archive(tmp_path)
    job = _job(tmp_path, archive)

    def boom(*args: object, **kwargs: object) -> int:
        raise OSError(13, "Permission denied")

    path = aj.job_path(tmp_path / ".runtime", job.job_id)
    before = path.read_text(encoding="utf-8") if path.exists() else None

    monkeypatch.setattr(aj.os, "open", boom)

    assert job.save() is False
    # The failed write left the previously persisted manifest alone.
    assert (path.read_text(encoding="utf-8") if path.exists() else None) == before


def test_a_manifest_temp_file_is_cleaned_up_after_a_failed_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-written temp must not be left behind for the next `O_CREAT`.

    The rewrite path deliberately re-chmods a stale temp (see above); leaving
    one lying around after a failure is what made that repair necessary.
    """
    archive = _archive(tmp_path)
    job = _job(tmp_path, archive)
    path = aj.job_path(tmp_path / ".runtime", job.job_id)

    real_fchmod = os.fchmod

    def boom(descriptor: int, mode: int) -> None:
        real_fchmod(descriptor, mode)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(aj.os, "fchmod", boom)

    assert job.save() is False
    assert not path.with_name(f".{path.name}.tmp").exists()


def test_a_changed_archive_blocks_the_pending_stage_not_a_settled_one(
    tmp_path: Path,
) -> None:
    """Blocking must name the stage this resume was asked to run.

    `project_doc_update` was blocked unconditionally once insights had settled,
    whatever `stages` held. Retrying only `memory_proposals` therefore
    overwrote the audit state of a fold that had already succeeded and left the
    genuinely pending stage untouched — and an explicit retry, which clears
    blocks, then reset the fold and could run it a second time.
    """
    manager = _manager(tmp_path)
    project = manager.create_project("Work", workspace="work")
    chat = manager.create_chat(project.project_id, title="A chat")
    archive = _archive(tmp_path)
    chat.archived = True
    chat.archive_path = str(archive.relative_to(tmp_path))
    inputs = _job_inputs(tmp_path, archive, chat_id=chat.chat_id)
    job = manager._new_job_for_chat(chat, inputs)

    # Insights and the fold both landed; only the proposals stage is left.
    job.mark("insights", aj.SUCCEEDED)
    if "project_doc_update" in job.stages:
        job.mark("project_doc_update", aj.SUCCEEDED)
    job.save()

    archive.write_text("# chat\n\nsomething else entirely\n", encoding="utf-8")

    asyncio.run(
        manager._run_job(chat.chat_id, job, inputs, stages=["memory_proposals"])
    )

    if "project_doc_update" in job.stages:
        assert job.status_of("project_doc_update") == aj.SUCCEEDED, (
            "a settled stage must keep its audit state"
        )
    assert job.status_of("memory_proposals") == aj.BLOCKED
