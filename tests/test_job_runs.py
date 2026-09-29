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
    jr.record_run(jr.JobRun(job="vault_index", label="Vault index refresh", status="ok",
                            duration_ms=30))

    grouped = jr.load_runs()
    assert set(grouped) == {"title", "vault_index"}

    title = grouped["title"]
    # newest-first: the error run is most recent
    assert title["last_run"]["status"] == "error"
    assert title["recent"][0]["status"] == "error"
    assert title["stats"]["total_runs"] == 2
    assert title["stats"]["success_rate"] == 0.5
    assert title["stats"]["avg_duration_ms"] == 15
    assert title["stats"]["last_error"]["error"] == "boom"

    assert grouped["vault_index"]["stats"]["last_error"] is None


def test_recent_capped_per_job(tmp_path: Path) -> None:
    for i in range(15):
        jr.record_run(jr.JobRun(job="title", label="Title", duration_ms=i))
    grouped = jr.load_runs(limit_per_job=5)
    assert len(grouped["title"]["recent"]) == 5
    # most recent (i=14) is first
    assert grouped["title"]["recent"][0]["duration_ms"] == 14


def test_load_runs_uses_latest_index_when_history_missing(tmp_path: Path) -> None:
    jr.record_run(jr.JobRun(
        job="vault_index",
        label="Vault index refresh",
        status="ok",
        started_at="2026-07-02T06:00:00+00:00",
        ended_at="2026-07-02T06:00:02+00:00",
        duration_ms=2000,
    ))
    (tmp_path / jr.JOB_RUNS_NAME).write_text("", encoding="utf-8")

    grouped = jr.load_runs()

    assert grouped["vault_index"]["last_run"]["status"] == "ok"
    assert grouped["vault_index"]["last_run"]["ended_at"] == "2026-07-02T06:00:02+00:00"


def test_trim_preserves_latest_line_for_each_job(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(jr, "MAX_BYTES", 400)
    monkeypatch.setattr(jr, "KEEP_LINES", 3)
    jr.record_run(jr.JobRun(
        job="vault_index",
        label="Vault index refresh",
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
    assert any(row["job"] == "vault_index" for row in rows)


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


# ── Retired jobs ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("retired", sorted(jr.RETIRED_JOBS))
def test_retired_jobs_are_hidden_unless_asked_for(tmp_path: Path, retired: str) -> None:
    """A job removed from the code must not linger in what readers see."""
    jr.record_run(jr.JobRun(job=retired, label=retired, status="ok",
                            category="system", duration_ms=5))
    jr.record_run(jr.JobRun(job="vault_index", label="Vault index refresh",
                            status="ok", duration_ms=5))

    assert retired not in jr.load_runs()
    assert "vault_index" in jr.load_runs()
    # the record itself is untouched on disk, and readable on request
    assert retired in {r["job"] for r in _read_lines(tmp_path)}
    assert retired in jr.load_runs(keep_retired=True)


def test_a_retired_job_leaves_no_stale_latest_row(tmp_path: Path) -> None:
    """`job_runs_latest.json` keeps a job's last run forever, so a retired id
    must be filtered from it too, not only from the rotating log."""
    jr.record_run(jr.JobRun(job="trajectory", label="Trajectory capture",
                            status="ok", duration_ms=5))
    (tmp_path / jr.JOB_RUNS_NAME).write_text("", encoding="utf-8")
    assert "trajectory" in json.loads(
        (tmp_path / jr.JOB_RUNS_LATEST_NAME).read_text()
    )

    assert "trajectory" not in jr.load_runs()
    assert "trajectory" in jr.load_runs(keep_retired=True)
