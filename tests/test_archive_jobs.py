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


def _config(tmp_path: Path, *, insights_enabled: bool = True) -> CiaoConfig:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        insights_enabled=insights_enabled,
    )


def _manager(tmp_path: Path, *, insights_enabled: bool = True) -> ProjectChatManager:
    config = _config(tmp_path, insights_enabled=insights_enabled)
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
    job.mark("insights", aj.SUCCEEDED)
    job.mark("project_doc_update", aj.SKIPPED, "no canonical project doc")
    job.mark("trajectory", aj.SKIPPED, "no session input")
    job.mark("memory_proposals", aj.FAILED, "model unavailable")
    job.save()

    reloaded = aj.load_job(tmp_path / ".runtime", job.job_id)
    assert reloaded is not None
    assert reloaded.status_of("insights") == aj.SUCCEEDED
    assert reloaded.stages["memory_proposals"].reason == "model unavailable"
    assert reloaded.state == "incomplete"
    assert reloaded.unfinished() == ["memory_proposals"]


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


def test_resumable_excludes_dependents_of_an_exhausted_insights(tmp_path: Path) -> None:
    """An exhausted insights must not keep re-launching its dependents.

    `project_doc_update` and `memory_proposals` need the insights text; with
    `insights` out of automatic budget they would launch on every startup,
    immediately skip for lack of output, and loop forever.
    """
    archive = _archive(tmp_path)
    job = _job(tmp_path, archive)
    for _ in range(aj.MAX_AUTO_ATTEMPTS):
        job.mark("insights", aj.RUNNING)
        job.mark("insights", aj.FAILED, "boom")

    # Dependents are pending but not eligible while insights is exhausted.
    assert job.status_of("project_doc_update") == aj.PENDING
    assert job.resumable() == []
    # The explicit retry still resets and reaches everything.
    job.reset_failed(include_blocked=True)
    assert "insights" in job.resumable()
    assert job.stage("insights").attempts == 0


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


def test_insights_append_is_skipped_when_evidence_cannot_persist(
    tmp_path: Path, monkeypatch
) -> None:
    """No durable evidence means no archive mutation."""
    archive = _archive(tmp_path)

    async def fake_call(body: str, model: str, **kwargs: object) -> str:
        return "## Decisions\n- Chose sqlite. [idx=1]\n"

    monkeypatch.setattr(insights, "_call_text_model", fake_call)
    job = _job(tmp_path, archive)
    monkeypatch.setattr(job, "save", lambda: False)
    inputs = _job_inputs(tmp_path, archive)

    asyncio.run(insights.run_archive_pipeline(job, inputs, stages=["insights"]))

    assert job.status_of("insights") == aj.FAILED
    # The archive was not mutated without its recovery evidence.
    assert "## Session insights" not in archive.read_text(encoding="utf-8")


def test_resumable_excludes_blocked_and_exhausted_stages(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    job = _job(tmp_path, archive)
    job.mark("project_doc_update", aj.SKIPPED, "no canonical project doc")
    job.mark("trajectory", aj.SKIPPED, "no session input")
    job.block("insights", "archive content changed since the job was created")
    for _ in range(aj.MAX_AUTO_ATTEMPTS):
        job.mark("memory_proposals", aj.RUNNING)
        job.mark("memory_proposals", aj.FAILED, "boom")

    # A blocked precondition excludes the whole job from an automatic resume,
    # and an exhausted stage is not retried on every boot either.
    assert job.resumable() == []
    assert set(job.unfinished()) == {"insights", "memory_proposals"}

    # An explicit retry clears both the block and the exhaustion.
    job.reset_failed(include_blocked=True)
    assert job.status_of("insights") == aj.PENDING
    assert job.status_of("memory_proposals") == aj.PENDING
    assert set(job.resumable()) == {"insights", "memory_proposals"}


def test_resumable_excludes_dependents_of_an_exhausted_insights(tmp_path: Path) -> None:
    """An exhausted insights must not keep re-launching its dependents.

    `project_doc_update` and `memory_proposals` need the insights text; with
    `insights` out of automatic budget they would launch on every startup,
    immediately skip for lack of output, and loop forever.
    """
    archive = _archive(tmp_path)
    job = _job(tmp_path, archive)
    for _ in range(aj.MAX_AUTO_ATTEMPTS):
        job.mark("insights", aj.RUNNING)
        job.mark("insights", aj.FAILED, "boom")

    # Dependents are pending but not eligible while insights is exhausted.
    assert job.status_of("project_doc_update") == aj.PENDING
    assert job.resumable() == []
    # The explicit retry resets the budget, so insights is eligible again (and
    # its dependents with it).
    job.reset_failed(include_blocked=True)
    assert job.stage("insights").attempts == 0
    assert "insights" in job.resumable()


# ── Stage resume: failure after each stage ────────────────────────────────


def test_insights_stage_appends_and_settles(tmp_path: Path, monkeypatch) -> None:
    archive = _archive(tmp_path)
    _patch_insights_model(monkeypatch, "## Decisions\n- Chose sqlite. [idx=1]\n")
    job = _job(tmp_path, archive)
    inputs = _job_inputs(tmp_path, archive)

    asyncio.run(insights.run_archive_pipeline(job, inputs, stages=["insights"]))

    assert job.status_of("insights") == aj.SUCCEEDED
    assert "## Session insights" in archive.read_text(encoding="utf-8")


def test_insights_failure_is_recorded_and_resumes(tmp_path: Path, monkeypatch) -> None:
    archive = _archive(tmp_path)

    async def failing(body: str, model: str, **kwargs: object) -> str:
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(insights, "_call_text_model", failing)
    monkeypatch.setattr(insights, "_RETRY_DELAY_S", 0)
    job = _job(tmp_path, archive)
    inputs = _job_inputs(tmp_path, archive)

    asyncio.run(insights.run_archive_pipeline(job, inputs, stages=["insights"]))

    assert job.status_of("insights") == aj.FAILED
    assert job.state == "incomplete"
    assert "insights" in job.resumable()

    # A successful second attempt resumes and settles the same manifest.
    _patch_insights_model(monkeypatch, "## Errors\n- x. [idx=2]\n")
    job.reset_failed()
    asyncio.run(insights.run_archive_pipeline(job, inputs, stages=["insights"]))
    assert job.status_of("insights") == aj.SUCCEEDED


def test_regression_insights_saved_proposals_not_run_is_recoverable(
    tmp_path: Path, monkeypatch
) -> None:
    """The ticket's first step: insights exist, but the later stages did not run."""
    archive = _stamped_archive(tmp_path)
    vault = tmp_path / "vault"
    (vault / "Workspace").mkdir(parents=True, exist_ok=True)
    guide = tmp_path / "CLAUDE.md"
    guide.write_text("# guide\n", encoding="utf-8")

    # Extraction must NOT be re-run: the section is already there.
    async def must_not_call(*args: object, **kwargs: object) -> str:
        raise AssertionError("extraction re-ran on an archive that already had insights")

    monkeypatch.setattr(insights, "_call_model", must_not_call)
    monkeypatch.setattr(insights, "_call_text_model", must_not_call)
    job = _job(tmp_path, archive)
    inputs = _job_inputs(
        tmp_path,
        archive,
        proposal_vault_root=vault,
        guide_path=guide,
        memory_proposals_enabled=True,
    )

    asyncio.run(insights.run_archive_pipeline(job, inputs, stages=["insights", "memory_proposals"]))

    assert job.status_of("insights") == aj.SKIPPED
    assert job.status_of("memory_proposals") == aj.SUCCEEDED


def test_project_fold_runs_even_when_insights_are_skipped(
    tmp_path: Path, monkeypatch
) -> None:
    archive = _stamped_archive(
        tmp_path,
        "## Decisions\n- Chose sqlite over postgres because local-first.\n",
    )
    doc = tmp_path / "Project.md"
    doc.write_text("# Project\n\n## Open loops\n- a\n", encoding="utf-8")

    async def fake_oneshot(prompt: object, **kwargs: object) -> str:
        return "# Project\n\n## Open loops\n- done\n"

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", fake_oneshot)
    job = _job(tmp_path, archive)
    inputs = _job_inputs(
        tmp_path,
        archive,
        project_doc_path=str(doc),
        memory_proposals_enabled=False,
    )
    job.mark("insights", aj.SKIPPED, "already present")

    asyncio.run(insights.run_archive_pipeline(job, inputs, stages=["project_doc_update"]))

    assert job.status_of("project_doc_update") == aj.SUCCEEDED
    assert job.inputs["doc_fold_wrote"] is True


def test_trajectory_runs_independently_of_a_failed_insights_stage(
    tmp_path: Path, monkeypatch
) -> None:
    archive = _archive(tmp_path)

    async def failing(body: str, model: str, **kwargs: object) -> str:
        raise RuntimeError("provider down")

    monkeypatch.setattr(insights, "_call_text_model", failing)
    monkeypatch.setattr(insights, "_RETRY_DELAY_S", 0)

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

    asyncio.run(
        insights.run_archive_pipeline(
            job, inputs, stages=["insights", "trajectory"]
        )
    )

    assert job.status_of("insights") == aj.FAILED
    # The trajectory is independent and must still run, exactly as the old
    # `finally` guaranteed.
    assert job.status_of("trajectory") == aj.SUCCEEDED
    assert written


# ── Provider/write failures stay retryable, not success ───────────────────


def test_project_fold_provider_failure_is_retryable(tmp_path: Path, monkeypatch) -> None:
    """A provider timeout/failure must not settle the fold as succeeded.

    `update_project_doc` returns False for both a legit no-op and a failure;
    the stage must stay failed so a retry can fold the doc.
    """
    archive = _stamped_archive(
        tmp_path,
        "## Decisions\n- Chose sqlite over postgres because local-first.\n",
    )
    doc = tmp_path / "Project.md"
    doc.write_text("# Project\n\n## Open loops\n- a\n", encoding="utf-8")

    async def boom(prompt: object, **kwargs: object) -> str:
        raise RuntimeError("upstream down")

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", boom)
    job = _job(tmp_path, archive)
    job.mark("insights", aj.SUCCEEDED)
    inputs = _job_inputs(
        tmp_path,
        archive,
        project_doc_path=str(doc),
        memory_proposals_enabled=False,
    )

    asyncio.run(insights.run_archive_pipeline(job, inputs, stages=["project_doc_update"]))

    assert job.status_of("project_doc_update") == aj.FAILED
    assert "project_doc_update" in job.resumable()
    # The doc was never updated.
    assert doc.read_text(encoding="utf-8") == "# Project\n\n## Open loops\n- a\n"


def test_project_fold_no_change_is_still_success(tmp_path: Path, monkeypatch) -> None:
    """A NO_CHANGES sentinel is a legitimate no-op, not a failure."""
    archive = _stamped_archive(
        tmp_path,
        "## Decisions\n- Chose sqlite over postgres because local-first.\n",
    )
    doc = tmp_path / "Project.md"
    doc.write_text("# Project\n\n## Open loops\n- a\n", encoding="utf-8")

    async def no_changes(prompt: object, **kwargs: object) -> str:
        return "NO_CHANGES"

    monkeypatch.setattr("ciao.providers.oneshot.run_oneshot", no_changes)
    job = _job(tmp_path, archive)
    job.mark("insights", aj.SUCCEEDED)
    inputs = _job_inputs(
        tmp_path,
        archive,
        project_doc_path=str(doc),
        memory_proposals_enabled=False,
    )

    asyncio.run(insights.run_archive_pipeline(job, inputs, stages=["project_doc_update"]))

    assert job.status_of("project_doc_update") == aj.SUCCEEDED


def test_trajectory_write_failure_is_retryable(tmp_path: Path, monkeypatch) -> None:
    archive = _archive(tmp_path)

    async def fake_call(body: str, model: str, **kwargs: object) -> str:
        return "## Errors\n- x.\n"

    def boom(trajectory: dict) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(insights, "_call_text_model", fake_call)
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

    asyncio.run(
        insights.run_archive_pipeline(job, inputs, stages=["insights", "trajectory"])
    )

    assert job.status_of("insights") == aj.SUCCEEDED
    assert job.status_of("trajectory") == aj.FAILED
    assert "trajectory" in job.resumable()


def test_proposals_write_failure_is_retryable(tmp_path: Path, monkeypatch) -> None:
    archive = _stamped_archive(
        tmp_path,
        "## Decisions\n- Chose X over Y because reasons. [review]\n",
    )
    vault = tmp_path / "vault"
    (vault / "Workspace").mkdir(parents=True, exist_ok=True)

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("queue unwritable")

    monkeypatch.setattr(
        "ciao.memory_proposals.append_proposals", boom
    )
    job = _job(tmp_path, archive)
    job.mark("insights", aj.SUCCEEDED)
    inputs = _job_inputs(
        tmp_path,
        archive,
        proposal_vault_root=vault,
        memory_proposals_enabled=True,
    )

    asyncio.run(insights.run_archive_pipeline(job, inputs, stages=["memory_proposals"]))

    assert job.status_of("memory_proposals") == aj.FAILED
    assert "memory_proposals" in job.resumable()


def test_proposals_empty_archive_is_still_success(tmp_path: Path) -> None:
    """Nothing to queue is a legitimate no-op, not a failure."""
    archive = _stamped_archive(tmp_path, "## Errors\n- just a one-off. [idx=1]\n")
    vault = tmp_path / "vault"
    (vault / "Workspace").mkdir(parents=True, exist_ok=True)
    job = _job(tmp_path, archive)
    job.mark("insights", aj.SUCCEEDED)
    inputs = _job_inputs(
        tmp_path,
        archive,
        proposal_vault_root=vault,
        memory_proposals_enabled=True,
    )

    asyncio.run(insights.run_archive_pipeline(job, inputs, stages=["memory_proposals"]))

    assert job.status_of("memory_proposals") == aj.SUCCEEDED


def test_insights_disabled_settles_both_dependent_stages_skipped(
    tmp_path: Path,
) -> None:
    """With extraction disabled, the fold and proposals are not left pending.

    They were intentionally never planned (there is no insights text to consume);
    leaving them pending made the manifest read `incomplete` and offered a retry
    for stages that can never run for this chat.
    """
    archive = _archive(tmp_path)
    manager = _manager(tmp_path, insights_enabled=False)
    project = manager.create_project("Work", workspace="work")
    chat = manager.create_chat(project.project_id, title="A chat")
    chat.archived = True
    chat.archive_path = str(archive.relative_to(tmp_path))

    async def drive() -> object:
        manager.run_archive_postprocess(
            chat.chat_id,
            ArchiveOutcome(
                path=archive, session_id="sess-1", turn_count=1,
                filtered_jsonl="line",
            ),
            chat,
            project,
        )
        await asyncio.sleep(0)
        return manager._archive_jobs[chat.chat_id]

    job = asyncio.run(drive())
    assert job.status_of("insights") == aj.SKIPPED
    assert job.status_of("project_doc_update") == aj.SKIPPED
    assert job.status_of("memory_proposals") == aj.SKIPPED



# ── Idempotency: no duplicates on retry ───────────────────────────────────


def test_retry_does_not_duplicate_proposal_rows(tmp_path: Path) -> None:
    archive = _stamped_archive(
        tmp_path,
        "## Decisions\n- Chose X over Y because reasons. [review]\n",
    )
    vault = tmp_path / "vault"
    (vault / "Workspace").mkdir(parents=True, exist_ok=True)
    job = _job(tmp_path, archive)
    inputs = _job_inputs(
        tmp_path,
        archive,
        proposal_vault_root=vault,
        memory_proposals_enabled=True,
    )

    asyncio.run(insights.run_archive_pipeline(job, inputs, stages=["memory_proposals"]))
    job.reset_failed()
    job.mark("memory_proposals", aj.PENDING)
    asyncio.run(insights.run_archive_pipeline(job, inputs, stages=["memory_proposals"]))

    queue = (vault / "Workspace" / "Memory-Proposals.md").read_text(encoding="utf-8")
    assert queue.count("Chose X over Y because reasons.") == 1


def test_retry_does_not_duplicate_region_entries(tmp_path: Path) -> None:
    archive = _stamped_archive(
        tmp_path,
        "## User corrections\n"
        "- Avoid em dashes; use commas instead. Durable rule: "
        "Avoid em dashes; use commas instead. [memory]\n",
    )
    vault = tmp_path / "vault"
    (vault / "Workspace").mkdir(parents=True, exist_ok=True)
    from ciao import memory_tool as mt

    guide = tmp_path / "CLAUDE.md"
    guide.write_text("# guide\n", encoding="utf-8")
    mt.ensure_regions(guide)
    job = _job(tmp_path, archive)
    inputs = _job_inputs(
        tmp_path,
        archive,
        proposal_vault_root=vault,
        guide_path=guide,
        memory_proposals_enabled=True,
    )

    asyncio.run(insights.run_archive_pipeline(job, inputs, stages=["memory_proposals"]))
    job.reset_failed()
    job.mark("memory_proposals", aj.PENDING)
    asyncio.run(insights.run_archive_pipeline(job, inputs, stages=["memory_proposals"]))

    entries, _ = mt.read_region(guide, "memory")
    matching = [e for e in entries if "em dashes" in e]
    assert len(matching) == 1


def test_retry_does_not_double_count_learning_recurrence(tmp_path: Path) -> None:
    from ciao import memory_proposals as mp

    vault = tmp_path / "vault"
    (vault / "Workspace").mkdir(parents=True, exist_ok=True)
    learning = "Rebuild the PWA bundle before testing."
    assert mp.append_learning(vault, learning, source="chat-1") is True
    # Re-observing the same learning increments once; a retry of the pipeline
    # re-runs the same call, so the count is 2, not 3 or 4.
    assert mp.append_learning(vault, learning, source="chat-1") is True
    text = (vault / "Workspace" / "Learnings.md").read_text(encoding="utf-8")
    assert text.count("(x2)") == 1
    assert "(x3)" not in text


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

    # The archive changes under a job whose insights never landed.
    archive.write_text("# chat\n\nsomething else entirely\n", encoding="utf-8")

    asyncio.run(manager._run_job(chat.chat_id, job, inputs, stages=["insights"]))

    assert job.status_of("insights") == aj.BLOCKED
    assert "changed" in job.blocked_reason
    assert manager.get_chat(chat.chat_id).postprocess.get("state") == "blocked"


def test_missing_archive_blocks_the_job(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    job = _job(tmp_path, archive)
    inputs = _job_inputs(tmp_path, archive)
    archive.unlink()

    asyncio.run(insights.run_archive_pipeline(job, inputs, stages=["insights"]))

    assert job.status_of("insights") == aj.BLOCKED
    assert "missing" in job.stage("insights").reason


# ── Crash between the insights append and the stage mark ──────────────────


def test_resume_accepts_the_pipelines_own_insights_append(tmp_path: Path) -> None:
    """A crash after `_append_section` must not look like an external edit.

    The manifest records the pre-insights revision plus the exact hash of the
    section it appended. The resume authenticates the on-disk section against
    that evidence and runs the later stages instead of blocking the job.
    """
    archive = _archive(tmp_path, "# chat\n\nbody\n")
    recorded = aj.archive_content_revision(archive)
    # Simulate the crash: the section is appended, the stage never settles.
    appended = insights._append_section(archive, "## Decisions\n- Chose X.\n")
    assert appended
    append_hash = aj.text_revision(appended)

    assert (
        aj.resume_revision_matches(archive, recorded, expected_append_revision=append_hash)
        is True
    )

    manager = _manager(tmp_path)
    project = manager.create_project("Work", workspace="work")
    chat = manager.create_chat(project.project_id, title="A chat")
    chat.archived = True
    chat.archive_path = str(archive.relative_to(tmp_path))
    inputs = _job_inputs(tmp_path, archive, chat_id=chat.chat_id)
    job = _job(tmp_path, archive, chat_id=chat.chat_id)
    job.content_revision = recorded
    job.insights_append_revision = append_hash
    job.started = True
    job.mark("insights", aj.RUNNING)
    job.save()

    asyncio.run(manager._run_job(chat.chat_id, job, inputs, stages=["insights"]))

    # Not blocked: insights is recognized as already appended.
    assert job.status_of("insights") == aj.SKIPPED
    assert job.blocked_reason == ""


def test_resume_refuses_an_edited_insights_tail(tmp_path: Path) -> None:
    """An edit confined to the appended body must not pass the crash check.

    The pre-insights prefix still matches the manifest revision, but the
    appended section no longer hashes to the recorded output, so the resume
    blocks instead of letting the fold/proposals consume edited content.
    """
    archive = _archive(tmp_path, "# chat\n\nbody\n")
    recorded = aj.archive_content_revision(archive)
    appended = insights._append_section(archive, "## Decisions\n- Chose X.\n")
    append_hash = aj.text_revision(appended)

    # Edit only the appended insights body.
    archive.write_text(
        archive.read_text(encoding="utf-8").replace("Chose X.", "Chose Y."),
        encoding="utf-8",
    )

    assert aj.resume_revision_matches(archive, recorded) is False
    assert (
        aj.resume_revision_matches(archive, recorded, expected_append_revision=append_hash)
        is False
    )


def test_resume_refuses_a_prefix_match_without_append_evidence(tmp_path: Path) -> None:
    """No recorded append hash means a prefix match cannot be trusted."""
    archive = _archive(tmp_path, "# chat\n\nbody\n")
    recorded = aj.archive_content_revision(archive)
    insights._append_section(archive, "## Decisions\n- Chose X.\n")

    assert aj.resume_revision_matches(archive, recorded) is False


def test_resume_blocks_a_genuine_external_edit(tmp_path: Path) -> None:
    archive = _archive(tmp_path, "# chat\n\nbody\n")
    recorded = aj.archive_content_revision(archive)
    archive.write_text("# chat\n\ncompletely different content\n", encoding="utf-8")

    assert aj.resume_revision_matches(archive, recorded) is False


def test_downstream_resume_blocks_when_the_archive_changed_after_insights(
    tmp_path: Path,
) -> None:
    """A downstream-only resume must validate the post-insights revision.

    Insights succeeded but the fold/proposals never ran. Editing the archive
    before the retry must block, not fold the changed content into derived
    state.
    """
    archive = _stamped_archive(tmp_path)
    manager = _manager(tmp_path)
    project = manager.create_project("Work", workspace="work")
    chat = manager.create_chat(project.project_id, title="A chat")
    chat.archived = True
    chat.archive_path = str(archive.relative_to(tmp_path))
    inputs = _job_inputs(tmp_path, archive, chat_id=chat.chat_id)
    job = manager._new_job_for_chat(chat, inputs)
    job.mark("insights", aj.SUCCEEDED)
    job.post_insights_revision = aj.archive_content_revision(archive)
    job.save()

    # The archived transcript (or its insights) is edited after insights.
    archive.write_text(
        archive.read_text(encoding="utf-8") + "\n## Decisions\n- tampered\n",
        encoding="utf-8",
    )

    asyncio.run(manager._run_job(chat.chat_id, job, inputs, stages=["memory_proposals"]))

    assert job.state == "blocked"
    assert "changed" in job.blocked_reason


def test_downstream_resume_proceeds_when_the_archive_is_unchanged(
    tmp_path: Path,
) -> None:
    archive = _stamped_archive(tmp_path)
    vault = tmp_path / "vault"
    (vault / "Workspace").mkdir(parents=True, exist_ok=True)
    manager = _manager(tmp_path)
    project = manager.create_project("Work", workspace="work")
    chat = manager.create_chat(project.project_id, title="A chat")
    chat.archived = True
    chat.archive_path = str(archive.relative_to(tmp_path))
    inputs = _job_inputs(
        tmp_path,
        archive,
        chat_id=chat.chat_id,
        proposal_vault_root=vault,
        memory_proposals_enabled=True,
    )
    job = manager._new_job_for_chat(chat, inputs)
    job.mark("insights", aj.SUCCEEDED)
    job.post_insights_revision = aj.archive_content_revision(archive)
    job.save()

    asyncio.run(manager._run_job(chat.chat_id, job, inputs, stages=["memory_proposals"]))

    assert job.status_of("memory_proposals") == aj.SUCCEEDED


def test_startup_resume_resets_an_interrupted_final_attempt(tmp_path: Path) -> None:
    """A crash during the last automatic attempt must stay retryable."""
    manager = _manager(tmp_path)
    project = manager.create_project("Work", workspace="work")
    chat = manager.create_chat(project.project_id, title="A chat")
    archive = _archive(tmp_path)
    chat.archived = True
    chat.archive_path = str(archive.relative_to(tmp_path))
    manager._save()
    inputs = _job_inputs(tmp_path, archive, chat_id=chat.chat_id)
    job = manager._new_job_for_chat(chat, inputs)
    stage = job.stage("insights")
    stage.status = aj.RUNNING
    stage.attempts = aj.MAX_AUTO_ATTEMPTS
    job.save()

    started = asyncio.run(manager.resume_interrupted_jobs(max_concurrency=1))

    # The stage was eligible: an un-reset interrupted final attempt would have
    # been excluded by `resumable()` and nothing would start.
    assert started == 1
    reloaded = aj.load_job(manager._runtime_root, job.job_id)
    assert reloaded is not None
    assert reloaded.status_of("insights") in (aj.PENDING, aj.RUNNING)
    # An explicit retry resets an exhausted *pending* stage too, so it can
    # always recover a stage the automatic budget gave up on.
    reloaded.stage("insights").status = aj.PENDING
    reloaded.stage("insights").attempts = aj.MAX_AUTO_ATTEMPTS
    reloaded.reset_failed(include_blocked=True)
    assert reloaded.stage("insights").attempts == 0
    assert "insights" in reloaded.resumable()


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




def test_missing_workspace_owner_settles_proposals_as_skipped(
    tmp_path: Path, monkeypatch
) -> None:
    archive = _stamped_archive(tmp_path)
    job = _job(tmp_path, archive)
    # No workspace at all (a General chat): there is no queue to write to.
    inputs = _job_inputs(
        tmp_path,
        archive,
        proposal_vault_root=None,
        memory_proposals_enabled=True,
    )
    inputs["trajectory_meta"] = {"chat_id": "chat-1", "workspace": ""}

    asyncio.run(insights.run_archive_pipeline(job, inputs, stages=["memory_proposals"]))

    assert job.status_of("memory_proposals") == aj.SKIPPED
    assert "owner" in job.stage("memory_proposals").reason


def test_broken_workspace_owner_blocks_rather_than_skips(tmp_path: Path) -> None:
    """A workspace that exists but whose vault cannot resolve is recoverable."""
    archive = _stamped_archive(tmp_path)
    job = _job(tmp_path, archive)
    inputs = _job_inputs(
        tmp_path,
        archive,
        proposal_vault_root=None,
        memory_proposals_enabled=True,
    )

    asyncio.run(insights.run_archive_pipeline(job, inputs, stages=["memory_proposals"]))

    assert job.status_of("memory_proposals") == aj.BLOCKED
    assert "owner" in job.blocked_reason
    assert job.state == "blocked"


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
    job.block("insights", "archive content changed since the job was created")
    job.save()

    started = asyncio.run(manager.resume_interrupted_jobs(max_concurrency=1))

    assert started == 0


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
    job.mark("insights", aj.SUCCEEDED)
    job.save()

    resp = _client(manager).get(f"/api/chats/{chat.chat_id}/archive-job")

    assert resp.status_code == 200
    body = resp.json()["job"]
    assert body["steps"]["insights"]["status"] == "ok"
    assert "memory_proposals" in body["unfinished"]


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
    stages = [n for n in aj.PIPELINE_STAGES if n in job.stages]
    assert len(stages) >= 2, "fixture needs at least two planned stages"
    job.mark(stages[0], aj.FAILED, "provider error")
    job.stage(stages[0]).attempts = aj.MAX_AUTO_ATTEMPTS
    job.mark(stages[1], aj.RUNNING)
    job.save()
    assert aj.load_job(manager._runtime_root, job.job_id).state == aj.RUNNING

    # The chat row is gone, so the pass rewrites the stages and skips the job.
    manager._chats.pop(chat.chat_id)
    started = asyncio.run(manager.resume_interrupted_jobs(max_concurrency=1))

    assert started == 0
    reloaded = aj.load_job(manager._runtime_root, job.job_id)
    assert reloaded is not None
    assert reloaded.status_of(stages[1]) == aj.PENDING
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
