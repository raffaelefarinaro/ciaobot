"""Tests for ``ciao.job_runs`` (the background-job recorder)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from ciao import job_runs as jr


def _read_lines(tmp_path: Path) -> list[dict]:
    path = tmp_path / jr.JOB_RUNS_NAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ── record_run / load_runs ───────────────────────────────────────────────


def test_record_and_load_groups_by_job(tmp_path: Path) -> None:
    jr.record_run(jr.JobRun(job="title", label="Title", status="ok", duration_ms=10))
    jr.record_run(jr.JobRun(job="title", label="Title", status="error",
                            duration_ms=20, error="boom"))
    jr.record_run(jr.JobRun(job="trajectory", label="Trajectory capture", status="ok",
                            duration_ms=30))

    grouped = jr.load_runs()
    assert set(grouped) == {"title", "trajectory"}

    title = grouped["title"]
    # newest-first: the error run is most recent
    assert title["last_run"]["status"] == "error"
    assert title["recent"][0]["status"] == "error"
    assert title["stats"]["total_runs"] == 2
    assert title["stats"]["success_rate"] == 0.5
    assert title["stats"]["avg_duration_ms"] == 15
    assert title["stats"]["last_error"]["error"] == "boom"

    assert grouped["trajectory"]["stats"]["last_error"] is None


def test_recent_capped_per_job(tmp_path: Path) -> None:
    for i in range(15):
        jr.record_run(jr.JobRun(job="title", label="Title", duration_ms=i))
    grouped = jr.load_runs(limit_per_job=5)
    assert len(grouped["title"]["recent"]) == 5
    # most recent (i=14) is first
    assert grouped["title"]["recent"][0]["duration_ms"] == 14


def test_load_runs_uses_latest_index_when_history_missing(tmp_path: Path) -> None:
    jr.record_run(jr.JobRun(
        job="trajectory",
        label="Trajectory capture",
        status="ok",
        started_at="2026-07-02T06:00:00+00:00",
        ended_at="2026-07-02T06:00:02+00:00",
        duration_ms=2000,
    ))
    (tmp_path / jr.JOB_RUNS_NAME).write_text("", encoding="utf-8")

    grouped = jr.load_runs()

    assert grouped["trajectory"]["last_run"]["status"] == "ok"
    assert grouped["trajectory"]["last_run"]["ended_at"] == "2026-07-02T06:00:02+00:00"


def test_trim_preserves_latest_line_for_each_job(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(jr, "MAX_BYTES", 400)
    monkeypatch.setattr(jr, "KEEP_LINES", 3)
    jr.record_run(jr.JobRun(
        job="trajectory",
        label="Trajectory capture",
        status="ok",
        started_at="2026-07-02T06:00:00+00:00",
        ended_at="2026-07-02T06:00:02+00:00",
    ))

    for i in range(30):
        jr.record_run(jr.JobRun(
            job="branch_backup",
            label="Device-branch backup",
            category="system",
            duration_ms=i,
        ))

    rows = _read_lines(tmp_path)
    assert any(row["job"] == "trajectory" for row in rows)


# ── track (async) ────────────────────────────────────────────────────────


async def test_track_records_ok_with_duration(tmp_path: Path) -> None:
    async with jr.track("title", "Title", model="haiku", provider="claude") as h:
        h.extra["chat_id"] = "abc"
    rows = _read_lines(tmp_path)
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"
    assert rows[0]["model"] == "haiku"
    assert rows[0]["provider"] == "claude"
    assert rows[0]["extra"]["chat_id"] == "abc"
    assert rows[0]["duration_ms"] >= 0


async def test_track_records_error_and_reraises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        async with jr.track("trajectory", "Trajectory capture"):
            raise ValueError("nope")
    rows = _read_lines(tmp_path)
    assert rows[0]["status"] == "error"
    assert "ValueError: nope" in rows[0]["error"]


async def test_track_skip(tmp_path: Path) -> None:
    async with jr.track("title", "Title") as h:
        h.skip("already named")
    rows = _read_lines(tmp_path)
    assert rows[0]["status"] == "skipped"
    assert rows[0]["extra"]["skip_reason"] == "already named"


def test_track_sync_records(tmp_path: Path) -> None:
    with jr.track_sync("memory_proposals", "Memory proposals") as h:
        h.extra["proposal_count"] = 3
    rows = _read_lines(tmp_path)
    assert rows[0]["status"] == "ok"
    assert rows[0]["extra"]["proposal_count"] == 3


# ── rotation / fail-open ─────────────────────────────────────────────────


def test_trim_when_large(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(jr, "MAX_BYTES", 200)
    monkeypatch.setattr(jr, "KEEP_LINES", 5)
    for i in range(100):
        jr.record_run(jr.JobRun(job="title", label="Title", duration_ms=i))
    rows = _read_lines(tmp_path)
    # trimmed down to roughly KEEP_LINES (plus the final append)
    assert len(rows) <= 6


def test_record_run_fail_open(monkeypatch) -> None:
    # An unwritable target must not raise.
    jr.configure("/proc/nonexistent-ciao/does/not/exist")
    jr.record_run(jr.JobRun(job="title", label="Title"))  # should not raise


# ── startup phases ───────────────────────────────────────────────────────


@dataclass
class _Phase:
    name: str
    status: str
    message: str = ""
    started_at: str = "2026-06-08T10:00:00+00:00"
    finished_at: str = "2026-06-08T10:00:02+00:00"


def test_record_startup_phase_maps_and_skips(tmp_path: Path) -> None:
    jr.record_startup_phase(_Phase("update_skills", "done", "Skills already current."))
    jr.record_startup_phase(_Phase("refresh_vault_index", "failed", "index refresh failed"))
    jr.record_startup_phase(_Phase("connect_pi", "done"))  # not a tracked job

    rows = _read_lines(tmp_path)
    jobs = {r["job"]: r for r in rows}
    assert set(jobs) == {"skills_update", "vault_index"}
    assert jobs["skills_update"]["duration_ms"] == 2000
    assert jobs["skills_update"]["extra"]["summary"] == "Skills already current."
    assert jobs["vault_index"]["status"] == "error"
    assert jobs["vault_index"]["error"] == "index refresh failed"


# ── automation_summary ───────────────────────────────────────────────────


def test_summary_includes_never_run_jobs(tmp_path: Path) -> None:
    jr.record_run(jr.JobRun(job="trajectory", label="Trajectory capture", status="ok",
                           duration_ms=5))
    summary = {item["job"]: item for item in jr.automation_summary()}
    # every registry job is present, except bulk variants (nested under their
    # parent) and pipeline steps (nested under the job that owns the pipeline)
    # — a never-installed schedule only filters when the caller says so
    for spec in jr.REGISTRY:
        if spec.parent or spec.step_of:
            assert spec.job not in summary
        else:
            assert spec.job in summary
    assert summary["trajectory"]["last_run"]["status"] == "ok"
    # a job that never ran has empty stats
    assert summary["skill_evolution"]["last_run"] is None
    assert summary["skill_evolution"]["stats"]["total_runs"] == 0
    # categories carried through
    assert summary["vault_index"]["category"] == "system"
    assert summary["trajectory"]["uses_model"] is False
    assert summary["vault_index"]["uses_model"] is False
    assert summary["vault_index"]["produces_outcome"] is False
    # every row can answer "when does this run?"
    assert summary["trajectory"]["trigger"]
    # the trajectory is a top-level row now, not a step of a pipeline group
    assert not summary["trajectory"]["pipeline_label"]
    assert not summary["trajectory"].get("steps")
    # no bulk variants remain: the insights backfill was retired in #627, and
    # the key is omitted entirely rather than shipped empty
    assert "sub_jobs" not in summary["trajectory"]


@pytest.mark.parametrize("retired", sorted(jr.RETIRED_JOBS))
def test_summary_hides_retired_jobs(tmp_path: Path, retired: str) -> None:
    """A job removed from the code must not linger on the Automation page."""
    jr.record_run(jr.JobRun(job=retired, label=retired, status="ok",
                            category="system", duration_ms=5))
    jr.record_run(jr.JobRun(job="trajectory", label="Trajectory capture", status="ok",
                           duration_ms=5))

    assert retired not in {item["job"] for item in jr.automation_summary()}
    # the record itself is untouched on disk, and readable on request
    assert retired in {r["job"] for r in _read_lines(tmp_path)}
    assert retired in jr.load_runs(keep_retired=True)


def test_summary_hides_jobs_whose_only_schedule_is_not_installed(tmp_path: Path) -> None:
    installed = {"system-skill-evolution"}
    summary = {
        item["job"]: item
        for item in jr.automation_summary(installed_schedules=installed)
    }
    assert "skill_evolution" in summary
    # jobs with another trigger stay visible even without their schedule
    assert "trajectory" in summary
    assert "vault_index" in summary  # also runs on startup


# ── Live state: in-flight registry + publisher ────────────────────────────


def test_inflight_registers_during_the_block_and_clears_after() -> None:
    """The recorder only wrote on completion, so nothing could say "running"."""
    assert jr.inflight_runs() == []
    with jr.track_sync("trajectory", "Trajectory capture", extra={"chat_id": "c1"}):
        live = jr.inflight_runs()
        assert [(r["job"], r["chat_id"]) for r in live] == [("trajectory", "c1")]
        assert live[0]["started_at"]
    assert jr.inflight_runs() == []


def test_inflight_clears_even_when_the_job_raises() -> None:
    with pytest.raises(RuntimeError):
        with jr.track_sync("trajectory", "Trajectory capture"):
            raise RuntimeError("boom")
    # A crashed job that stayed "running" forever would pin a spinner on.
    assert jr.inflight_runs() == []


def test_publisher_sees_start_and_finish_with_status() -> None:
    events: list[dict] = []
    jr.set_publisher(events.append)
    with jr.track_sync("trajectory", "Trajectory capture",
                       extra={"chat_id": "c9"}) as run:
        run.extra["path"] = "/x.json"
    assert [e["event"] for e in events] == ["started", "finished"]
    assert events[0]["job"] == "trajectory"
    assert events[0]["chat_id"] == "c9"
    assert events[1]["status"] == "ok"
    # Extras collected inside the block ride along on the finish event, which is
    # how a surface learns *what* the step produced.
    assert events[1]["extra"]["path"] == "/x.json"


def test_publisher_reports_a_failed_step() -> None:
    events: list[dict] = []
    jr.set_publisher(events.append)
    with pytest.raises(ValueError), jr.track_sync("trajectory", "Trajectory capture"):
        raise ValueError("nope")
    assert events[-1]["status"] == "error"
    assert "nope" in events[-1]["error"]


def test_publisher_marks_a_skipped_step() -> None:
    events: list[dict] = []
    jr.set_publisher(events.append)
    with jr.track_sync("memory_proposals", "Memory proposals") as run:
        run.skip("nothing to propose")
    assert events[-1]["status"] == "skipped"


def test_a_broken_publisher_never_breaks_the_job() -> None:
    def explode(_event: dict) -> None:
        raise RuntimeError("subscriber is broken")

    jr.set_publisher(explode)
    with jr.track_sync("trajectory", "Trajectory capture") as run:
        run.extra["ok"] = True
    # The durable record is what matters; a bad subscriber must not cost it.
    assert jr.load_runs()["trajectory"]["last_run"]["extra"] == {"ok": True}


@pytest.mark.asyncio
async def test_async_track_reports_live_state_too() -> None:
    events: list[dict] = []
    jr.set_publisher(events.append)
    assert jr.inflight_runs() == []
    async with jr.track("trajectory", "Trajectory capture", extra={"chat_id": "c2"}):
        assert [r["chat_id"] for r in jr.inflight_runs()] == ["c2"]
    assert jr.inflight_runs() == []
    assert [e["event"] for e in events] == ["started", "finished"]


def test_summary_marks_a_running_job(tmp_path: Path) -> None:
    with jr.track_sync("trajectory", "Trajectory capture"):
        summary = {item["job"]: item for item in jr.automation_summary()}
        assert summary["trajectory"]["running"] is True
        assert summary["skill_evolution"]["running"] is False
