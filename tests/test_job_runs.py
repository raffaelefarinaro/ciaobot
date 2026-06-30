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
    jr.record_run(jr.JobRun(job="insights", label="Insights", status="ok",
                            duration_ms=30))

    grouped = jr.load_runs()
    assert set(grouped) == {"title", "insights"}

    title = grouped["title"]
    # newest-first: the error run is most recent
    assert title["last_run"]["status"] == "error"
    assert title["recent"][0]["status"] == "error"
    assert title["stats"]["total_runs"] == 2
    assert title["stats"]["success_rate"] == 0.5
    assert title["stats"]["avg_duration_ms"] == 15
    assert title["stats"]["last_error"]["error"] == "boom"

    assert grouped["insights"]["stats"]["last_error"] is None


def test_recent_capped_per_job(tmp_path: Path) -> None:
    for i in range(15):
        jr.record_run(jr.JobRun(job="title", label="Title", duration_ms=i))
    grouped = jr.load_runs(limit_per_job=5)
    assert len(grouped["title"]["recent"]) == 5
    # most recent (i=14) is first
    assert grouped["title"]["recent"][0]["duration_ms"] == 14


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
        async with jr.track("insights", "Insights"):
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
    jr.record_startup_phase(_Phase("sync_workspace", "done"))
    jr.record_startup_phase(_Phase("rebuild_pwa", "failed", "npm build failed"))
    jr.record_startup_phase(_Phase("connect_pi", "done"))  # not a tracked job

    rows = _read_lines(tmp_path)
    jobs = {r["job"]: r for r in rows}
    assert set(jobs) == {"startup_sync", "pwa_rebuild"}
    assert jobs["startup_sync"]["category"] == "system"
    assert jobs["startup_sync"]["duration_ms"] == 2000
    assert jobs["pwa_rebuild"]["status"] == "error"
    assert jobs["pwa_rebuild"]["error"] == "npm build failed"


# ── automation_summary ───────────────────────────────────────────────────


def test_summary_includes_never_run_jobs(tmp_path: Path) -> None:
    jr.record_run(jr.JobRun(job="title", label="Title", status="ok", duration_ms=5))
    summary = {item["job"]: item for item in jr.automation_summary()}
    # every registry job is present
    for spec in jr.REGISTRY:
        assert spec.job in summary
    assert summary["title"]["last_run"]["status"] == "ok"
    # a job that never ran has empty stats
    assert summary["skill_evolution"]["last_run"] is None
    assert summary["skill_evolution"]["stats"]["total_runs"] == 0
    # categories carried through
    assert summary["startup_sync"]["category"] == "system"
