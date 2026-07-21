"""Unit tests for the runtime issue report (ciao.debug_report)."""

from __future__ import annotations

from pathlib import Path

from ciao import job_runs
from ciao.debug_report import (
    build_issue_report,
    format_issue_report,
    recent_job_failures,
)
from ciao.job_runs import JobRun
from ciao.startup_triage import TRIAGE_SCHEDULE_ID


def _write_error_log(workspace: Path, text: str) -> None:
    runtime = workspace / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "server_errors.log").write_text(text, encoding="utf-8")


def test_empty_report_has_no_issues(tmp_path: Path) -> None:
    report = build_issue_report(tmp_path)
    assert report["has_issues"] is False
    assert report["failed_jobs"] == []
    assert report["error_log_lines"] == 0
    assert report["report_text"] == "(no runtime issues logged)"


def test_report_includes_error_log_and_failed_jobs(tmp_path: Path) -> None:
    _write_error_log(tmp_path, "2026-07-06 ERROR ciao.web: boom\n")
    job_runs.record_run(JobRun(
        job="schedule_dispatch", label="Scheduled dispatch",
        started_at="2026-07-05T10:00:00+00:00",
        ended_at="2026-07-05T10:00:05+00:00",
        status="error", error="stream exploded",
    ))
    job_runs.record_run(JobRun(
        job="title", label="Title generation",
        started_at="2026-07-05T11:00:00+00:00",
        ended_at="2026-07-05T11:00:01+00:00",
        status="ok",
    ))

    report = build_issue_report(tmp_path)

    assert report["has_issues"] is True
    assert report["error_log_lines"] == 1
    assert [f["job"] for f in report["failed_jobs"]] == ["schedule_dispatch"]
    assert "stream exploded" in report["report_text"]
    assert "boom" in report["report_text"]
    assert report["error_log_path"].endswith(".runtime/server_errors.log")


def test_failures_sorted_newest_first_and_capped(tmp_path: Path) -> None:
    for hour in (9, 11, 10):
        job_runs.record_run(JobRun(
            job="vault_index", label="Vault index refresh",
            started_at=f"2026-07-05T{hour:02d}:00:00+00:00",
            ended_at=f"2026-07-05T{hour:02d}:00:01+00:00",
            status="error", error=f"fail at {hour}",
        ))
    failures = recent_job_failures(limit=2)
    assert [f["error"] for f in failures] == ["fail at 11", "fail at 10"]


def _record_triage_and_real_failure() -> None:
    job_runs.record_run(JobRun(
        job="schedule_dispatch", label="Scheduled dispatch",
        started_at="2026-07-20T21:21:49+00:00",
        ended_at="2026-07-20T21:31:12+00:00",
        status="error", error="## Triage Summary — nothing to file",
        extra={"schedule_id": TRIAGE_SCHEDULE_ID, "chat_id": "chat-abc"},
    ))
    job_runs.record_run(JobRun(
        job="schedule_dispatch", label="Scheduled dispatch",
        started_at="2026-07-20T22:00:00+00:00",
        ended_at="2026-07-20T22:00:05+00:00",
        status="error", error="real dispatch failure",
        extra={"schedule_id": "sched-real", "chat_id": "chat-xyz"},
    ))


def test_schedule_failures_included_by_default(tmp_path: Path) -> None:
    """The shared aggregator surfaces a broken triage run to the human debug
    report — exclusion is opt-in, not the default."""
    _record_triage_and_real_failure()
    errors = [f["error"] for f in recent_job_failures()]
    assert "real dispatch failure" in errors
    assert any("Triage Summary" in e for e in errors)


def test_triage_own_run_excluded_when_requested(tmp_path: Path) -> None:
    """Triage-dispatch callers pass their own schedule id so the triage never
    re-triages its own recorded summary, while other failures still show."""
    _record_triage_and_real_failure()
    failures = recent_job_failures(exclude_schedule_ids={TRIAGE_SCHEDULE_ID})
    errors = [f["error"] for f in failures]
    assert "real dispatch failure" in errors
    assert all("Triage Summary" not in e for e in errors)


def test_format_report_only_errors_no_jobs(tmp_path: Path) -> None:
    _write_error_log(tmp_path, "ERROR a\nERROR b\n")
    report = build_issue_report(tmp_path)
    text = format_issue_report(report)
    assert "Server error log tail" in text
    assert "Failed background jobs" not in text
