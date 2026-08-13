"""Unified recorder for background-job runs.

Every background automation (title generation, insights extraction,
schedule dispatch, startup tasks, ...) wraps its work in :func:`track`
(async) or :func:`track_sync` (sync) so the Automation page can show, per
job: last run, duration, model/provider, and the error text on failure.

Records append to ``.runtime/job_runs.jsonl`` (one JSON object per line)
with a coarse size guard. A compact latest-run index is also maintained so
rare jobs do not disappear from Settings when high-frequency jobs rotate the
history log. Everything here is **fail-open**: a recorder error must never
break the job it is wrapping. The schema deliberately omits token/cost fields
to keep instrumentation cheap.

Mirrors the spirit of the old ``api_costs.py`` recorder and the rotating
log pattern in ``ciao/error_log.py``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

JOB_RUNS_NAME = "job_runs.jsonl"
JOB_RUNS_LATEST_NAME = "job_runs_latest.json"
MAX_BYTES = 2 * 1024 * 1024  # trim the log once it passes ~2 MB
KEEP_LINES = 2000            # lines retained after a trim

_runtime_dir_override: Path | None = None


def configure(runtime_dir: Path | str) -> None:
    """Pin the runtime directory. Called once at server startup so the
    recorder writes to the same ``.runtime`` the rest of the config uses,
    regardless of the process cwd. Tests can point it at a temp dir."""
    global _runtime_dir_override
    _runtime_dir_override = Path(runtime_dir)


def _runtime_dir() -> Path:
    if _runtime_dir_override is not None:
        return _runtime_dir_override
    raw = (
        os.environ.get("CIAO_RUNTIME_ROOT")
        or os.environ.get("TELEGRAM_BRIDGE_RUNTIME_ROOT")
        or ".runtime"
    )
    return Path(raw).resolve()


def _log_path() -> Path:
    return _runtime_dir() / JOB_RUNS_NAME


def _latest_path() -> Path:
    return _runtime_dir() / JOB_RUNS_LATEST_NAME


# ── Job registry ───────────────────────────────────────────────────────────
# Stable ids + labels shared by the instrumentation sites and the API view,
# so a job that has never run still shows up on the Automation page.


@dataclass(frozen=True)
class JobSpec:
    job: str
    label: str
    category: str  # "content" | "system"
    description: str = ""
    # Capabilities are static so the Automation page can explain a never-run
    # job too; they are not inferred from the most recent telemetry record.
    uses_model: bool = False
    produces_outcome: bool = False
    # Plain-language answer to "when does this run?". The page used to show a
    # status badge and nothing else, which left every row unexplained.
    trigger: str = ""
    # System schedule that can run the job on demand ("Run now").
    schedule_id: str = ""
    # True when that schedule is the *only* trigger. Such a job is hidden when
    # its schedule is not installed here — nothing would ever run it, so a
    # permanently idle row is noise (see :func:`automation_summary`). Jobs that
    # also fire on startup or on archive stay visible either way.
    schedule_only: bool = False
    # One-shot migrations: correct to keep for the record, wrong to present as
    # a live automation once they have run.
    one_time: bool = False
    # Bulk/manual variant of another job. Reported nested under the parent so
    # the page has one row per thing the user recognises.
    parent: str = ""


REGISTRY: tuple[JobSpec, ...] = (
    JobSpec("title", "Title generation", "content",
            "Names a chat from its first message.", True, True,
            trigger="When a new chat gets its first message."),
    JobSpec("insights", "Session insights", "content",
            "Extracts durable insights from an archived session transcript.", True, True,
            trigger="When a chat is archived."),
    JobSpec("project_doc_update", "Project doc update", "content",
            "Folds a session's decisions and open loops into the project document.",
            True, True,
            trigger="After session insights, for chats that belong to a project."),
    JobSpec("memory_proposals", "Memory proposals", "content",
            "Proposes durable facts from a session's insights.", False, True,
            trigger=(
                "After session insights. The daily system-memory-curation "
                "schedule then promotes them."
            ),
            schedule_id="system-memory-curation"),
    JobSpec("trajectory", "Trajectory capture", "content",
            "Records a structured trajectory of the session for skill mining.", False, True,
            trigger="When a chat is archived. Feeds Skill evolution."),
    JobSpec("skill_evolution", "Skill evolution", "content",
            "Weekly: proposes skill edits from underperforming trajectories.", True, True,
            trigger="Weekly, via the system-skill-evolution schedule.",
            schedule_id="system-skill-evolution", schedule_only=True),
    JobSpec("dependency_review", "Dependency review", "content",
            "Weekly: reviews tracked dependency releases against the baseline.", True, True,
            trigger="Weekly, via the system-dependency-review schedule.",
            schedule_id="system-dependency-review", schedule_only=True),
    JobSpec("schedule_dispatch", "Scheduled dispatch", "content",
            "Fires scheduled chat turns and evaluates auto-archival.", True, True,
            trigger="Every time a schedule or routine is due."),
    JobSpec("schedule_attention_classifier", "Schedule attention classifier", "content",
            "Decides whether an auto-archive schedule result needs user attention.", True, False,
            trigger="After a scheduled run that auto-archives its chat."),
    JobSpec("background_run", "Background command runs", "system",
            "Runs one command in a tracked subprocess and wakes the chat that "
            "started it.", False, True,
            trigger="When a chat starts one with the background_run_start tool."),
    JobSpec("startup_sync", "Startup git sync", "system",
            "Commits and pulls the workspace on server startup.", False, False,
            trigger="On server startup."),
    JobSpec("vault_index", "Vault index refresh", "system",
            "Regenerates memory-vault/INDEX.md from frontmatter.", False, False,
            trigger="On server startup, and weekly via system-workspace-hygiene.",
            schedule_id="system-workspace-hygiene"),
    JobSpec("skills_update", "Skills update", "system",
            "Updates installed agent skills.", False, False,
            trigger="On server startup."),
    JobSpec("branch_backup", "Device-branch backup", "system",
            "Pushes the per-device working branch for backup.", False, False,
            trigger="Periodically while the server runs."),
    JobSpec("gws_health", "Google Workspace token health", "system",
            "Pings each configured Google profile's token and alerts on revocation.",
            False, False,
            trigger="Periodically while the server runs, for each Google profile."),
    JobSpec("memory_migration", "Legacy memory migration", "system",
            "One-time move of legacy memory files into the CLAUDE.md memory regions.",
            False, True,
            trigger="Once, on the first skills sync after upgrading. A no-op afterwards.",
            one_time=True),
    JobSpec("backfill_insights", "Insights backfill", "system",
            "Runs session insights over every archive that is missing them.", True, True,
            trigger="On server startup, and on demand from this page.",
            parent="insights"),
)

# Jobs that no longer exist in the code. ``job_runs_latest.json`` keeps the
# last run of every job it ever saw, so without this a retired job (e.g. the
# startup PWA rebuild, dropped in favour of the boot screen) haunts the
# Automation page forever with a stale green badge.
RETIRED_JOBS: frozenset[str] = frozenset({
    "pwa_rebuild",       # startup PWA rebuild phase, removed
    "insights_backfill",  # renamed to backfill_insights
})

# StartupTracker phase name -> registry job id (phases not listed are skipped,
# e.g. the connect_* health checks, which are not automations).
STARTUP_PHASE_JOBS: dict[str, str] = {
    "sync_workspace": "startup_sync",
    "refresh_vault_index": "vault_index",
    "update_skills": "skills_update",
    "backfill_insights": "backfill_insights",
}


@dataclass
class JobRun:
    job: str
    label: str
    category: str = "content"
    started_at: str = ""
    ended_at: str = ""
    duration_ms: int = 0
    status: str = "ok"  # "ok" | "error" | "skipped"
    model: str = ""
    provider: str = ""
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def record_run(run: JobRun) -> None:
    """Append one run record. Never raises."""
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _trim_if_large(path)
        payload = asdict(run)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        _write_latest_run(payload)
    except Exception:  # noqa: BLE001 — recording must never break a job
        logger.debug("Failed to record job run %s", run.job, exc_info=True)


def _trim_if_large(path: Path) -> None:
    try:
        if not path.exists() or path.stat().st_size < MAX_BYTES:
            return
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        kept = lines[-KEEP_LINES:]
        kept_jobs = {_line_job(line) for line in kept}
        kept_jobs.discard(None)
        preserved_by_job: dict[str, str] = {}
        for line in lines[:-KEEP_LINES]:
            job = _line_job(line)
            if job and job not in kept_jobs:
                preserved_by_job[job] = line
        with path.open("w", encoding="utf-8") as f:
            f.writelines([*preserved_by_job.values(), *kept])
    except Exception:  # noqa: BLE001
        logger.debug("Failed to trim job-run log", exc_info=True)


def _line_job(line: str) -> str | None:
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        return None
    job = rec.get("job") if isinstance(rec, dict) else None
    return job if isinstance(job, str) and job else None


def _write_latest_run(run: dict[str, Any]) -> None:
    path = _latest_path()
    data = _load_latest_runs()
    job = run.get("job")
    if not isinstance(job, str) or not job:
        return
    data[job] = run
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(path)


def _load_latest_runs() -> dict[str, dict]:
    path = _latest_path()
    try:
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logger.debug("Failed to load latest job-run index", exc_info=True)
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for job, rec in raw.items():
        if isinstance(job, str) and job and isinstance(rec, dict):
            out[job] = rec
    return out


# ── Tracking ─────────────────────────────────────────────────────────────


class RunHandle:
    """Mutable handle yielded by :func:`track`. Callers may set ``extra``,
    mark the run skipped, or override the model after resolving it."""

    def __init__(
        self, job: str, label: str, category: str, model: str, provider: str,
        extra: dict[str, Any] | None,
    ) -> None:
        self.job = job
        self.label = label
        self.category = category
        self.model = model
        self.provider = provider
        self.status = "ok"
        self.error: str | None = None
        self.extra: dict[str, Any] = dict(extra or {})

    def skip(self, reason: str = "") -> None:
        self.status = "skipped"
        if reason:
            self.extra.setdefault("skip_reason", reason)


def _finalize(
    handle: RunHandle, started_at: datetime, started_perf: float,
    exc: BaseException | None,
) -> None:
    duration_ms = int((time.perf_counter() - started_perf) * 1000)
    status = handle.status
    error = handle.error
    if exc is not None:
        status = "error"
        error = f"{type(exc).__name__}: {exc}"[:1000]
    record_run(JobRun(
        job=handle.job,
        label=handle.label,
        category=handle.category,
        started_at=started_at.isoformat(),
        ended_at=datetime.now(UTC).isoformat(),
        duration_ms=duration_ms,
        status=status,
        model=handle.model,
        provider=handle.provider,
        error=error,
        extra=handle.extra,
    ))


@contextmanager
def track_sync(
    job: str, label: str, *, category: str = "content", model: str = "",
    provider: str = "", extra: dict[str, Any] | None = None,
) -> Iterator[RunHandle]:
    """Sync context manager: time the block, record the run, re-raise.

    Place inside the task's existing try/except so the re-raise keeps the
    task's own error handling intact while the recorder logs the run."""
    handle = RunHandle(job, label, category, model, provider, extra)
    started_at = datetime.now(UTC)
    started_perf = time.perf_counter()
    exc: BaseException | None = None
    try:
        yield handle
    except BaseException as e:  # noqa: BLE001 — record then re-raise
        exc = e
        raise
    finally:
        _finalize(handle, started_at, started_perf, exc)


@asynccontextmanager
async def track(
    job: str, label: str, *, category: str = "content", model: str = "",
    provider: str = "", extra: dict[str, Any] | None = None,
) -> AsyncIterator[RunHandle]:
    """Async variant of :func:`track_sync`."""
    handle = RunHandle(job, label, category, model, provider, extra)
    started_at = datetime.now(UTC)
    started_perf = time.perf_counter()
    exc: BaseException | None = None
    try:
        yield handle
    except BaseException as e:  # noqa: BLE001 — record then re-raise
        exc = e
        raise
    finally:
        _finalize(handle, started_at, started_perf, exc)


def record_startup_phase(phase: Any) -> None:
    """Record a finished StartupTracker phase as a system job run.

    Wired as ``tracker.on_finish``. Only phases mapped in
    :data:`STARTUP_PHASE_JOBS` are recorded; health checks are skipped.
    Never raises."""
    try:
        job = STARTUP_PHASE_JOBS.get(getattr(phase, "name", ""))
        if job is None:
            return
        spec = next((s for s in REGISTRY if s.job == job), None)
        label = spec.label if spec else job
        status = "ok" if getattr(phase, "status", "") == "done" else "error"
        started = getattr(phase, "started_at", None) or ""
        ended = getattr(phase, "finished_at", None) or ""
        message = getattr(phase, "message", "") or ""
        record_run(JobRun(
            job=job,
            label=label,
            category="system",
            started_at=started,
            ended_at=ended,
            duration_ms=_duration_ms(started, ended),
            status=status,
            error=message if status == "error" else None,
            extra=(
                {"message": message, "summary": message}
                if message and status == "ok"
                else {}
            ),
        ))
    except Exception:  # noqa: BLE001
        logger.debug("Failed to record startup phase", exc_info=True)


def _duration_ms(started_iso: str, ended_iso: str) -> int:
    try:
        a = datetime.fromisoformat(started_iso)
        b = datetime.fromisoformat(ended_iso)
        return max(0, int((b - a).total_seconds() * 1000))
    except (ValueError, TypeError):
        return 0


# ── Reading ──────────────────────────────────────────────────────────────


def load_runs(limit_per_job: int = 10, *, keep_retired: bool = False) -> dict[str, dict]:
    """Group recorded runs by job id -> {last_run, recent, stats}. The log
    is append-only chronological, so the last line for a job is its most
    recent run. ``recent`` is newest-first and capped at *limit_per_job*."""
    runs_by_job: dict[str, list[dict]] = {}
    try:
        path = _log_path()
        if path.exists():
            with path.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    job = rec.get("job")
                    if not isinstance(job, str) or not job:
                        continue
                    if not keep_retired and job in RETIRED_JOBS:
                        continue
                    runs_by_job.setdefault(job, []).append(rec)
    except Exception:  # noqa: BLE001
        logger.debug("Failed to load job runs", exc_info=True)

    latest = _load_latest_runs()
    if not keep_retired:
        latest = {job: rec for job, rec in latest.items() if job not in RETIRED_JOBS}
    _merge_latest_runs(runs_by_job, latest)

    grouped: dict[str, dict] = {}
    for job, runs in runs_by_job.items():
        recent = runs[-limit_per_job:][::-1]  # newest first
        total = len(runs)
        ok = sum(1 for r in runs if r.get("status") == "ok")
        durations = [
            r["duration_ms"] for r in runs
            if isinstance(r.get("duration_ms"), (int, float))
        ]
        avg_duration = int(sum(durations) / len(durations)) if durations else 0
        last_error = None
        for r in reversed(runs):
            if r.get("status") == "error" and r.get("error"):
                last_error = {
                    "error": r["error"],
                    "ts": r.get("ended_at") or r.get("started_at"),
                }
                break
        grouped[job] = {
            "last_run": recent[0] if recent else None,
            "recent": recent,
            "stats": {
                "total_runs": total,
                "success_rate": round(ok / total, 3) if total else None,
                "avg_duration_ms": avg_duration,
                "last_error": last_error,
            },
        }
    return grouped


def _merge_latest_runs(
    runs_by_job: dict[str, list[dict]],
    latest_by_job: dict[str, dict],
) -> None:
    """Make latest-run rows visible even when the rotating JSONL lost them."""
    for job, latest in latest_by_job.items():
        runs = runs_by_job.setdefault(job, [])
        if not runs:
            runs.append(latest)
            continue
        if any(_same_run(run, latest) for run in runs):
            continue
        last_ts = _run_ts(runs[-1])
        latest_ts = _run_ts(latest)
        if latest_ts is None or last_ts is None or latest_ts > last_ts:
            runs.append(latest)


def _same_run(a: dict, b: dict) -> bool:
    return (
        a.get("job") == b.get("job")
        and a.get("started_at") == b.get("started_at")
        and a.get("ended_at") == b.get("ended_at")
    )


def _run_ts(run: dict) -> datetime | None:
    raw = run.get("ended_at") or run.get("started_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def automation_summary(
    limit_per_job: int = 10,
    *,
    installed_schedules: set[str] | None = None,
) -> list[dict]:
    """Final view for the ``/api/automation`` endpoint: one row per automation
    the user can actually have, merged with its recorded runs.

    Registry jobs appear even when they never ran, so the page can explain
    them. Recorded jobs missing from the registry are kept for forward
    compatibility. Three things are deliberately *not* reported as rows:

    * retired jobs (:data:`RETIRED_JOBS`) — the code that ran them is gone;
    * a schedule-only job whose schedule is not installed here, when
      *installed_schedules* is supplied — nothing can trigger it;
    * bulk variants of another job — reported as the parent's ``sub_jobs``.
    """
    grouped = load_runs(limit_per_job)
    empty_stats = {
        "total_runs": 0,
        "success_rate": None,
        "avg_duration_ms": 0,
        "last_error": None,
    }

    def entry(spec: JobSpec, g: dict | None) -> dict:
        return {
            "job": spec.job,
            "label": spec.label,
            "category": spec.category,
            "description": spec.description,
            "uses_model": spec.uses_model,
            "produces_outcome": spec.produces_outcome,
            "trigger": spec.trigger,
            "schedule_id": spec.schedule_id,
            "schedule_only": spec.schedule_only,
            "one_time": spec.one_time,
            "last_run": g["last_run"] if g else None,
            "recent": g["recent"] if g else [],
            "stats": g["stats"] if g else dict(empty_stats),
        }

    out: list[dict] = []
    by_job: dict[str, dict] = {}
    seen: set[str] = set()
    children: list[tuple[JobSpec, dict | None]] = []
    for spec in REGISTRY:
        seen.add(spec.job)
        g = grouped.get(spec.job)
        if spec.parent:
            children.append((spec, g))
            continue
        if (
            spec.schedule_only
            and installed_schedules is not None
            and spec.schedule_id not in installed_schedules
        ):
            continue
        row = entry(spec, g)
        by_job[spec.job] = row
        out.append(row)
    for spec, g in children:
        parent = by_job.get(spec.parent)
        if parent is None:
            # Parent hidden (or unknown): keep the child visible on its own
            # rather than silently dropping its telemetry.
            out.append(entry(spec, g))
            continue
        parent.setdefault("sub_jobs", []).append(entry(spec, g))
    for job, g in grouped.items():
        if job in seen:
            continue
        category = "content"
        if g["recent"]:
            category = g["recent"][0].get("category") or "content"
        # Unknown jobs are kept visible for forward compatibility. Infer only
        # their observed capabilities because no static JobSpec exists yet.
        out.append({
            "job": job,
            "label": job.replace("_", " ").title(),
            "category": category,
            "description": "",
            "uses_model": any(bool(run.get("model")) for run in g["recent"]),
            "produces_outcome": any(
                bool(run.get("extra")) or bool(run.get("error"))
                for run in g["recent"]
            ),
            "trigger": "",
            "schedule_id": "",
            "schedule_only": False,
            "one_time": False,
            "last_run": g["last_run"],
            "recent": g["recent"],
            "stats": g["stats"],
        })
    return out
