"""Unit tests for the runtime issue report (ciao.debug_report)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from ciao import job_runs
from ciao.debug_report import (
    build_issue_report,
    format_issue_report,
    recent_job_failures,
)
from ciao.job_runs import JobRun
from ciao.startup_triage import TRIAGE_SCHEDULE_ID


def _ago(**delta: float) -> str:
    """An ISO timestamp relative to now.

    Fixed calendar dates drift past the age cutoff in
    :func:`recent_job_failures` as real time passes, which would turn these
    tests green for the wrong reason.
    """
    return (datetime.now(UTC) - timedelta(**delta)).isoformat()


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
        started_at=_ago(hours=2),
        ended_at=_ago(hours=2),
        status="error", error="stream exploded",
    ))
    job_runs.record_run(JobRun(
        job="title", label="Title generation",
        started_at=_ago(hours=1),
        ended_at=_ago(hours=1),
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
    for hours in (3, 1, 2):
        job_runs.record_run(JobRun(
            job="vault_index", label="Vault index refresh",
            started_at=_ago(hours=hours),
            ended_at=_ago(hours=hours),
            status="error", error=f"fail {hours}h ago",
        ))
    failures = recent_job_failures(limit=2)
    assert [f["error"] for f in failures] == ["fail 1h ago", "fail 2h ago"]


def test_stale_failures_are_dropped(tmp_path: Path) -> None:
    """A one-off failure must stop being reported once it is old enough.

    ``job_runs_latest.json`` keeps a job's last run forever, so before the age
    cutoff a single failure from weeks ago re-opened a triage chat on every
    boot past the 12h cooldown.
    """
    job_runs.record_run(JobRun(
        job="skill_evolution", label="Skill reflection",
        started_at=_ago(days=19), ended_at=_ago(days=19),
        status="error", error="OneShotError: Model not found",
    ))
    job_runs.record_run(JobRun(
        job="background_run", label="Background command run",
        started_at=_ago(hours=4), ended_at=_ago(hours=4),
        status="error", error="timed out",
    ))

    errors = [f["error"] for f in recent_job_failures()]

    assert errors == ["timed out"]


def test_undatable_failure_is_dropped(tmp_path: Path) -> None:
    """A run with no usable timestamp can never age out, so it would be
    re-reported on every boot forever."""
    job_runs.record_run(JobRun(
        job="vault_index", label="Vault index refresh",
        status="error", error="undated boom",
    ))

    assert recent_job_failures() == []


def test_failures_before_since_floor_are_dropped(tmp_path: Path) -> None:
    """The startup triage acknowledges what it already dispatched by passing
    its last dispatch time; failures it already reported must not come back."""
    job_runs.record_run(JobRun(
        job="schedule_dispatch", label="Scheduled dispatch",
        started_at=_ago(hours=30), ended_at=_ago(hours=30),
        status="error", error="already triaged",
    ))
    job_runs.record_run(JobRun(
        job="background_run", label="Background command run",
        started_at=_ago(hours=2), ended_at=_ago(hours=2),
        status="error", error="fresh failure",
    ))

    since = datetime.now(UTC) - timedelta(hours=24)
    errors = [f["error"] for f in recent_job_failures(since=since)]

    assert errors == ["fresh failure"]


def test_report_forwards_the_since_floor(tmp_path: Path) -> None:
    job_runs.record_run(JobRun(
        job="schedule_dispatch", label="Scheduled dispatch",
        started_at=_ago(hours=30), ended_at=_ago(hours=30),
        status="error", error="already triaged",
    ))

    report = build_issue_report(
        tmp_path, failures_since=datetime.now(UTC) - timedelta(hours=24)
    )

    assert report["failed_jobs"] == []
    assert report["has_issues"] is False


def _record_triage_and_real_failure() -> None:
    job_runs.record_run(JobRun(
        job="schedule_dispatch", label="Scheduled dispatch",
        started_at=_ago(hours=3),
        ended_at=_ago(hours=3),
        status="error", error="## Triage Summary — nothing to file",
        extra={"schedule_id": TRIAGE_SCHEDULE_ID, "chat_id": "chat-abc"},
    ))
    job_runs.record_run(JobRun(
        job="schedule_dispatch", label="Scheduled dispatch",
        started_at=_ago(hours=2),
        ended_at=_ago(hours=2),
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


def test_legacy_skill_evolution_no_proposal_is_not_a_runtime_issue(
    tmp_path: Path,
) -> None:
    job_runs.record_run(JobRun(
        job="skill_evolution", label="skillevo:small-skill:has_proposal",
        started_at=_ago(hours=3),
        ended_at=_ago(hours=3),
        status="error", error="no-proposal",
        extra={
            "dag": "skillevo:small-skill",
            "node_id": "has_proposal",
            "kind": "gate",
        },
    ))
    job_runs.record_run(JobRun(
        job="skill_evolution", label="skillevo:small-skill:semantic",
        started_at=_ago(hours=2),
        ended_at=_ago(hours=2),
        status="error", error="model returned drifted",
        extra={
            "dag": "skillevo:small-skill",
            "node_id": "semantic",
            "kind": "gate",
        },
    ))

    failures = recent_job_failures()

    assert [failure["error"] for failure in failures] == [
        "model returned drifted",
    ]


def test_format_report_only_errors_no_jobs(tmp_path: Path) -> None:
    _write_error_log(tmp_path, "ERROR a\nERROR b\n")
    report = build_issue_report(tmp_path)
    text = format_issue_report(report)
    assert "Server error log tail" in text
    assert "Failed background jobs" not in text


def test_debug_log_included_when_present(tmp_path: Path) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "server_debug.log").write_text(
        "2026-08-22 DEBUG ciao.providers.claude: stderr noise\n",
        encoding="utf-8",
    )

    report = build_issue_report(tmp_path)

    # Ambient verbose output is never an issue by itself...
    assert report["has_issues"] is False
    assert report["report_text"] == "(no runtime issues logged)"
    # ...but the raw tail is always in the payload for inspection.
    assert report["debug_log_lines"] == 1
    assert report["debug_log_path"].endswith(".runtime/server_debug.log")
    assert "stderr noise" in report["debug_log"]


def test_debug_log_rendered_alongside_real_issues(tmp_path: Path) -> None:
    _write_error_log(tmp_path, "ERROR boom\n")
    job_runs.record_run(JobRun(
        job="schedule_dispatch", label="Scheduled dispatch",
        started_at=_ago(hours=1),
        ended_at=_ago(hours=1),
        status="error", error="stream exploded",
    ))
    runtime = tmp_path / ".runtime"
    (runtime / "server_debug.log").write_text(
        "DEBUG provider handshake failed\n", encoding="utf-8"
    )

    report = build_issue_report(tmp_path)

    assert report["has_issues"] is True
    text = report["report_text"]
    assert "stream exploded" in text
    assert "Debug log tail" in text
    assert "provider handshake failed" in text


def test_debug_log_absent_is_empty(tmp_path: Path) -> None:
    report = build_issue_report(tmp_path)
    assert report["debug_log"] == ""
    assert report["debug_log_lines"] == 0
    assert "Debug log tail" not in report["report_text"]
