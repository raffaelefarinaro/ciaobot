"""Aggregate runtime issues into a single self-fix report.

Combines the rotating server error log (``ciao.error_log``) with recent
failed background-job runs (``ciao.job_runs``) so the agent can triage
and fix its own runtime problems.  Consumed by the dev-mode
``GET /api/debug/issues`` endpoint (the "Fix issues in chat" button) and
by the ``{{ISSUE_REPORT}}`` schedule placeholder.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ciao import job_runs
from ciao.error_log import ERROR_LOG_NAME, tail_error_log
from ciao.startup_triage import TRIAGE_SCHEDULE_ID

DEFAULT_LOG_LINES = 200
DEFAULT_MAX_FAILED_JOBS = 20


def recent_job_failures(limit: int = DEFAULT_MAX_FAILED_JOBS) -> list[dict]:
    """Return recent failed job runs, newest first, capped at *limit*.

    Runs from the startup triage's own schedule dispatch are excluded: the
    triage records its summary as the schedule run's outcome, and when that
    run is flagged an error the summary text lands in the ``error`` field.
    Feeding it back here would make each triage re-triage its own prior
    output on the next boot (a self-referential loop).
    """
    failures: list[dict] = []
    for job, info in job_runs.load_runs(limit_per_job=10).items():
        for run in info.get("recent") or []:
            if run.get("status") != "error":
                continue
            extra = run.get("extra")
            if isinstance(extra, dict) and extra.get("schedule_id") == TRIAGE_SCHEDULE_ID:
                continue
            failures.append({
                "job": job,
                "label": run.get("label") or job,
                "ended_at": run.get("ended_at") or run.get("started_at") or "",
                "error": run.get("error") or "(no error message recorded)",
            })
    failures.sort(key=_failure_ts, reverse=True)
    return failures[:limit]


def _failure_ts(failure: dict) -> str:
    raw = failure.get("ended_at")
    if isinstance(raw, str) and raw:
        try:
            datetime.fromisoformat(raw)
            return raw
        except ValueError:
            pass
    return ""


def build_issue_report(
    workspace_root: Path,
    *,
    log_lines: int = DEFAULT_LOG_LINES,
    max_failed_jobs: int = DEFAULT_MAX_FAILED_JOBS,
) -> dict:
    """Collect current runtime issues into a JSON-friendly report."""
    error_log = tail_error_log(workspace_root, log_lines)
    failed_jobs = recent_job_failures(max_failed_jobs)
    error_line_count = sum(1 for line in error_log.splitlines() if line.strip())
    report = {
        "error_log": error_log,
        "error_log_lines": error_line_count,
        "error_log_path": str(workspace_root / ".runtime" / ERROR_LOG_NAME),
        "failed_jobs": failed_jobs,
        "has_issues": bool(error_log.strip() or failed_jobs),
    }
    report["report_text"] = format_issue_report(report)
    return report


def format_issue_report(report: dict) -> str:
    """Render the report as text suitable for embedding in a chat prompt."""
    if not report.get("has_issues"):
        return "(no runtime issues logged)"

    parts: list[str] = []
    failed_jobs = report.get("failed_jobs") or []
    if failed_jobs:
        parts.append("## Failed background jobs (newest first)")
        for f in failed_jobs:
            when = f.get("ended_at") or "unknown time"
            parts.append(f"- [{when}] {f.get('label')}: {f.get('error')}")

    error_log = (report.get("error_log") or "").strip()
    if error_log:
        path = report.get("error_log_path") or "server_errors.log"
        parts.append(f"## Server error log tail ({path})")
        parts.append("```\n" + error_log + "\n```")

    return "\n\n".join(parts)
