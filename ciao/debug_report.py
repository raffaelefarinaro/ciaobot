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

from collections.abc import Collection

from ciao import job_runs
from ciao.error_log import DEBUG_LOG_NAME, ERROR_LOG_NAME, tail_debug_log, tail_error_log

DEFAULT_LOG_LINES = 200
DEFAULT_MAX_FAILED_JOBS = 20


def _is_legacy_no_proposal_failure(run: dict) -> bool:
    """Recognize pre-fix skill-evolution no-proposal rows.

    Older DAG runs recorded the intentional ``has_proposal`` false branch as
    an error. Keep those historical rows out of the issue report too, but
    require the exact DAG/node/error shape so genuine skill-evolution errors
    remain visible.
    """
    extra = run.get("extra")
    dag_label = extra.get("dag") if isinstance(extra, dict) else None
    return (
        isinstance(extra, dict)
        and isinstance(dag_label, str)
        and dag_label.startswith("skillevo:")
        and extra.get("node_id") == "has_proposal"
        and run.get("error") == "no-proposal"
    )


def recent_job_failures(
    limit: int = DEFAULT_MAX_FAILED_JOBS,
    *,
    exclude_schedule_ids: Collection[str] | None = None,
) -> list[dict]:
    """Return recent failed job runs, newest first, capped at *limit*.

    ``exclude_schedule_ids`` drops schedule-dispatch runs whose
    ``extra.schedule_id`` matches. Triage-dispatch callers pass their own
    schedule id here so a triage never re-triages its own past runs: the
    triage records its summary as the run's outcome, and a run flagged an
    error carries that summary in the ``error`` field, which would otherwise
    loop straight back into the next report. Left empty by default so the
    human-facing debug report still surfaces a genuinely broken triage.
    """
    excluded = set(exclude_schedule_ids or ())
    failures: list[dict] = []
    for job, info in job_runs.load_runs(limit_per_job=10).items():
        for run in info.get("recent") or []:
            if run.get("status") != "error":
                continue
            if _is_legacy_no_proposal_failure(run):
                continue
            extra = run.get("extra")
            if isinstance(extra, dict) and extra.get("schedule_id") in excluded:
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
    exclude_schedule_ids: Collection[str] | None = None,
) -> dict:
    """Collect current runtime issues into a JSON-friendly report.

    ``exclude_schedule_ids`` is forwarded to :func:`recent_job_failures`;
    triage-dispatch callers pass their own schedule id to avoid re-triaging
    their own runs.
    """
    error_log = tail_error_log(workspace_root, log_lines)
    debug_log = tail_debug_log(workspace_root, log_lines)
    failed_jobs = recent_job_failures(
        max_failed_jobs, exclude_schedule_ids=exclude_schedule_ids
    )
    error_line_count = sum(1 for line in error_log.splitlines() if line.strip())
    debug_line_count = sum(1 for line in debug_log.splitlines() if line.strip())
    report = {
        "error_log": error_log,
        "error_log_lines": error_line_count,
        "error_log_path": str(workspace_root / ".runtime" / ERROR_LOG_NAME),
        "debug_log": debug_log,
        "debug_log_lines": debug_line_count,
        "debug_log_path": str(workspace_root / ".runtime" / DEBUG_LOG_NAME),
        "failed_jobs": failed_jobs,
        # Only errors and failed jobs count as issues: the debug log is
        # ambient verbose output (empty unless CIAO_LOG_LEVEL=debug), so it
        # must never trip the startup triage on its own.
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

    debug_log = (report.get("debug_log") or "").strip()
    if debug_log:
        path = report.get("debug_log_path") or "server_debug.log"
        parts.append(
            f"## Debug log tail ({path}, verbose CIAO_LOG_LEVEL=debug output)"
        )
        parts.append("```\n" + debug_log + "\n```")

    return "\n\n".join(parts)
