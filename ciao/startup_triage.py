"""Startup error triage: harvest runtime errors into a fix-it chat.

At server startup, when the error log or recent background-job runs
contain failures, a one-off triage prompt is dispatched through the
same pipeline schedules use — so ``{{ISSUE_REPORT}}`` substitution and
the existing clear-after-clean-run behavior apply: the error log is
reset exactly when its content has been handed to a chat that
processed it. No errors means no chat and no model call.

A cooldown marker prevents a crash-looping server from opening a new
triage chat on every boot. The prompt tells the agent it may escalate
Ciaobot's own bugs as GitHub issues when the ``gh`` CLI is available.

This module also caps the launchd service logs (``ciao.stdout.log``
and friends): launchd appends to them forever and nothing else rotates
them. launchd opens these files with ``O_APPEND``, so rewriting them
in place with their own tail is safe while the server is running.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

TRIAGE_SCHEDULE_ID = "startup-error-triage"
TRIAGE_COOLDOWN_S = 12 * 3600.0
TRIAGE_MARKER_NAME = "startup_triage.json"

SERVICE_LOG_NAMES = (
    "ciao.stdout.log",
    "ciao.stderr.log",
    "ciao.menubar.stdout.log",
    "ciao.menubar.stderr.log",
)
SERVICE_LOG_MAX_BYTES = 10 * 1024 * 1024
SERVICE_LOG_KEEP_BYTES = 1 * 1024 * 1024

TRIAGE_PROMPT = """\
Ciaobot found runtime errors from previous sessions at startup. Triage them.

{{ISSUE_REPORT}}

Steps:
1. Group the log lines and failed job runs into distinct issues.
2. Diagnose each one from local evidence first (`.runtime/server_errors.log`, \
`.runtime/job_runs.jsonl`, `.runtime/ciao.stderr.log`, workspace config) — do not speculate.
3. Fix what is fixable locally: workspace state, configuration, stuck runtime files, permissions.
4. When the root cause is a bug in the Ciaobot app itself and the `gh` CLI is \
authenticated, first search for an existing report \
(`gh issue list --repo raffaelefarinaro/ciaobot --search "..."`), then file one if none exists \
(`gh issue create --repo raffaelefarinaro/ciaobot --title "[Agent] ..." --body "..."`) \
with the error excerpt, what triggered it, and the installed version. If `gh` is \
unavailable, end with a short paste-ready issue report instead.
5. Close with a short summary: what was found, what was fixed, what was escalated.

The error log is cleared automatically after this triage completes cleanly.\
"""


def cap_service_logs(
    runtime_dir: Path,
    *,
    max_bytes: int = SERVICE_LOG_MAX_BYTES,
    keep_bytes: int = SERVICE_LOG_KEEP_BYTES,
) -> list[str]:
    """Shrink oversized launchd service logs to their most recent tail.

    Returns the names of the files that were capped.
    """
    capped: list[str] = []
    for name in SERVICE_LOG_NAMES:
        path = runtime_dir / name
        try:
            if not path.is_file() or path.stat().st_size <= max_bytes:
                continue
            data = path.read_bytes()[-keep_bytes:]
            # Start on a whole line so the kept tail reads cleanly.
            newline = data.find(b"\n")
            if 0 <= newline < len(data) - 1:
                data = data[newline + 1:]
            header = (
                b"[log truncated by ciaobot: kept the most recent "
                + str(len(data)).encode()
                + b" bytes]\n"
            )
            path.write_bytes(header + data)
            capped.append(name)
        except OSError:
            logger.warning("Could not cap service log %s", path, exc_info=True)
    if capped:
        logger.info("Capped oversized service logs: %s", ", ".join(capped))
    return capped


def _marker_path(runtime_dir: Path) -> Path:
    return runtime_dir / TRIAGE_MARKER_NAME


def _last_triage_at(runtime_dir: Path) -> datetime | None:
    try:
        raw = json.loads(_marker_path(runtime_dir).read_text(encoding="utf-8"))
        return datetime.fromisoformat(raw["last_dispatched_at"])
    except Exception:
        return None


def _record_triage(runtime_dir: Path, now: datetime) -> None:
    try:
        _marker_path(runtime_dir).write_text(
            json.dumps({"last_dispatched_at": now.isoformat()}),
            encoding="utf-8",
        )
    except OSError:
        logger.warning("Could not write startup-triage marker", exc_info=True)


def _general_project_id(pcm) -> str | None:
    """The General project of the first workspace, or None."""
    try:
        workspaces = list(pcm._workspace_names())
        projects = pcm._projects.values()
    except Exception:
        return None
    for ws in workspaces:
        for project in projects:
            if project.workspace == ws and project.name == "General":
                return project.project_id
    return None


def build_triage_entry(workspace: str, web_project_id: str):
    """A synthetic one-off ScheduleEntry carrying the triage prompt."""
    from ciao.schedules import ScheduleEntry

    return ScheduleEntry(
        schedule_id=TRIAGE_SCHEDULE_ID,
        daily_time_utc="00:00",
        prompt=TRIAGE_PROMPT,
        chat_id=0,
        created_at=datetime.now(UTC).isoformat(),
        frequency="manual",
        web_project_id=web_project_id,
        workspace=workspace,
        title="Startup error triage",
        scope="system",
        editable=False,
        removable=False,
    )


async def run_startup_triage(pcm, config, resolve_target) -> bool:
    """Dispatch a triage chat when startup finds runtime errors.

    Returns True when a triage chat was dispatched.
    """
    import asyncio

    from ciao.debug_report import build_issue_report

    runtime_dir = Path(config.state_path).parent

    report = await asyncio.to_thread(build_issue_report, config.workspace_root)
    if not report.get("error_line_count") and not report.get("failed_jobs"):
        return False

    now = datetime.now(UTC)
    last = _last_triage_at(runtime_dir)
    if last is not None and (now - last).total_seconds() < TRIAGE_COOLDOWN_S:
        logger.info(
            "Startup found runtime errors but a triage chat ran at %s; "
            "waiting out the cooldown",
            last.isoformat(),
        )
        return False

    project_id = _general_project_id(pcm)
    if project_id is None:
        logger.warning("Startup triage skipped: no General project found")
        return False

    workspace = next(iter(pcm._workspace_names()), "") or ""
    entry = build_triage_entry(workspace=workspace, web_project_id=project_id)
    _, model, mode, provider = resolve_target(entry)

    # Record before dispatching so a crash mid-run cannot loop into a new
    # chat every restart.
    _record_triage(runtime_dir, now)
    logger.info(
        "Startup found %s error line(s) and %d failed job run(s); "
        "dispatching a triage chat",
        report.get("error_line_count", 0),
        len(report.get("failed_jobs") or []),
    )
    await pcm.dispatch_schedule(entry, entry.prompt, model, mode, provider)
    return True
