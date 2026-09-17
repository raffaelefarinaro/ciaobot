"""Persisted per-archive pipeline manifest for resumable post-processing.

Archiving a chat runs one in-process ``asyncio`` task that extracts session
insights, folds the project doc, captures a trajectory, reconciles bounded
regions and files memory proposals (``ciao/insights.py:extract_and_append``).
The task is not durable: a server crash or a provider failure part-way through
leaves the archive with some stages done and the rest missing, and the old
pipeline could not repair them. ``_has_insights_section`` made the whole
function return early, and the insights retry refused any archive that already
had insights — so a crash after insights but before the project fold or the
memory writes was unrecoverable.

This module is the small durable record that fixes that: one manifest per
archive, keyed by archive identity and pipeline version, holding a
pending/running/succeeded/failed/skipped/blocked state per stage. It is *not* a
queue service — the architecture is local and single-owner, so the manifest is
a JSON file under ``.runtime/archive_jobs/`` plus the in-process recovery pass
that reads it at startup.

Two rules, matching the rest of the codebase:

* Writes are atomic (temp file + ``os.replace``) and idempotent; the effective
  manifest is the latest complete file, so a crash can only lose the last
  update, never corrupt an earlier one.
* A tombstoned job (its archived chat was deleted) can never be resurrected:
  :func:`save_job` refuses to clear the tombstone, and the runner checks it
  before every stage.

Stage content is identified by the archive path plus the pipeline version; the
archive's *content revision* is recorded so a resume can tell "the same archive,
still running" from "the file changed underneath a pending job" and report the
latter as blocked rather than writing derived state beside unknown content.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MANIFEST_VERSION = 1

#: Bump when stage semantics or the recorded inputs change shape. A job written
#: by an older pipeline is not resumed from; the manager re-derives one instead.
PIPELINE_VERSION = 1

PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
SKIPPED = "skipped"
BLOCKED = "blocked"
TOMBSTONED = "tombstoned"

#: Stage outcomes that need nothing further from a run.
TERMINAL = frozenset({SUCCEEDED, SKIPPED})
#: Stage outcomes that settle a run (blocked needs a config/human change).
SETTLED = frozenset({SUCCEEDED, SKIPPED, BLOCKED})
#: Stage outcomes an automatic resume may still act on.
INCOMPLETE = frozenset({PENDING, RUNNING, FAILED})

#: How many automatic attempts a stage gets before a resume stops retrying it
#: on every boot. A model that fails terminally (auth, quota, bad model) would
#: otherwise be re-asked at each startup; an explicit user retry still works.
MAX_AUTO_ATTEMPTS = 3

#: Execution order, shared by the runner, the postprocess UI and the manifest.
#: These are the same ids the per-step job events carry, so a manifest overlay
#: and the live telemetry agree. The best-effort region reconcile runs inside
#: ``memory_proposals`` (it only feeds that stage's write decisions).
PIPELINE_STAGES: tuple[str, ...] = (
    "insights",
    "project_doc_update",
    "trajectory",
    "memory_proposals",
)

_JOBS_DIR = "archive_jobs"


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_revision(path: Path) -> str:
    """Digest one archive's text, or ``""`` when it cannot be read.

    An unreadable archive is not a revision change; the caller treats the empty
    string as "unknown" so a permissions blip does not block a job.
    """
    try:
        return _sha(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""


def text_revision(text: str) -> str:
    """Digest an in-memory string (used for the exact appended insights image)."""
    return _sha(text)


def archive_content_revision(path: Path) -> str:
    """The revision recorded on an archive job.

    Trailing whitespace is stripped so the digest is stable across the exact
    number of newlines an append adds: the pipeline prepends ``\\n\\n`` to the
    existing text, so a raw digest of the pre-insights archive and of the text
    before its own section differ only by whitespace. Normalizing at write time
    lets :func:`resume_revision_matches` recognize the pipeline's own append.
    """
    try:
        return _sha(path.read_text(encoding="utf-8", errors="replace").rstrip())
    except OSError:
        return ""


def _pre_insights_text(text: str) -> str:
    """The archive text before the pipeline's own appended insights section.

    Returns ``text`` unchanged when there is no appended section. Lazy import
    of :mod:`ciao.insights` avoids an import cycle (insights imports this
    module at call time).
    """
    from ciao.insights import locate_insights_section

    location = locate_insights_section(text)
    if location is None:
        return text
    return text[: location[0]]


def _appended_insights_tail(text: str) -> str | None:
    """The exact appended insights section (stamp included), or None.

    Returns ``text[section_start:]`` — the bytes :func:`ciao.insights._append_section`
    writes — so a resume can authenticate the *whole* appended section rather
    than trusting any tail that happens to follow a matching prefix. An edit
    confined to the appended body then fails the check instead of being
    consumed by the fold and proposal stages.
    """
    from ciao.insights import locate_insights_section

    location = locate_insights_section(text)
    if location is None:
        return None
    return text[location[0]:]


def resume_revision_matches(
    path: Path,
    recorded_revision: str,
    *,
    expected_append_revision: str = "",
) -> bool:
    """True when a resume may proceed against the archive on disk.

    A crash can land after ``_append_section`` wrote the insights but before
    the manifest marked the stage succeeded. The archive then differs from the
    recorded revision by the pipeline's own append, which must not be mistaken
    for an external edit and block the job.

    The append is authenticated, not assumed. When ``expected_append_revision``
    is set (the hash of the exact section the pipeline was about to write), a
    matching pre-insights prefix is accepted only if the appended section
    hashes to it. Without that evidence a prefix match is refused: an edit
    confined to the appended body would otherwise pass the check and be folded
    into the project doc and proposals. A manifest written before this field
    existed has no evidence, so its crashed-append window stays blocked rather
    than risking consumption of edited content.
    """
    if not recorded_revision:
        return True
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    # No append happened (or the whole file is unchanged): the recorded digest
    # matches the current text directly. Both raw and trailing-newline variants
    # are compared so a manifest written by an earlier build still resumes.
    if recorded_revision in {_sha(text), _sha(text.rstrip())}:
        return True
    if not expected_append_revision:
        return False
    tail = _appended_insights_tail(text)
    if tail is None:
        return False
    prefix = _pre_insights_text(text)
    prefix_matches = recorded_revision in {_sha(prefix), _sha(prefix.rstrip())}
    return prefix_matches and _sha(tail) == expected_append_revision


def new_job_id(chat_id: str, archive_path: str) -> str:
    """Stable id for one chat's archive job.

    Keyed by the chat and the archive path rather than by content: the archive
    is *meant* to change when insights are appended, so a content-derived id
    would orphan the job at exactly the moment it needs to be resumed.
    """
    return _sha(f"{chat_id}\0{archive_path}\0{PIPELINE_VERSION}")[:20]


def jobs_root(runtime_root: Path) -> Path:
    return Path(runtime_root) / _JOBS_DIR


def job_path(runtime_root: Path, job_id: str) -> Path:
    return jobs_root(runtime_root) / f"{job_id}.json"


@dataclass
class StageState:
    """One stage's durable status inside an archive job."""

    status: str = PENDING
    attempts: int = 0
    reason: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "attempts": self.attempts,
            "reason": self.reason,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: object) -> StageState:
        if not isinstance(raw, dict):
            return cls()
        return cls(
            status=str(raw.get("status", PENDING)),
            attempts=int(raw.get("attempts", 0) or 0),
            reason=str(raw.get("reason", "")),
            updated_at=str(raw.get("updated_at", "")),
        )


@dataclass
class ArchiveJob:
    """The durable manifest for one archived chat's post-processing."""

    job_id: str
    chat_id: str
    archive_path: str
    runtime_root: str
    content_revision: str = ""
    #: The archive revision after the insights section landed. Downstream
    #: stages (project fold, memory proposals) are resumed against this, so an
    #: edit to the archive between insights succeeding and a later resume is
    #: detected instead of being silently consumed. Empty until insights
    #: settles.
    post_insights_revision: str = ""
    #: Hash of the exact section the pipeline appended, set immediately before
    #: ``_append_section`` writes it. A crashed-append resume authenticates the
    #: on-disk section against this, so an edit confined to the appended body
    #: is refused rather than consumed. Empty for a job that never appended.
    insights_append_revision: str = ""
    pipeline_version: int = PIPELINE_VERSION
    manifest_version: int = MANIFEST_VERSION
    created_at: str = ""
    updated_at: str = ""
    state: str = RUNNING
    #: True once the runner has actually begun executing stages. A freshly
    #: created manifest is all-pending and legitimately "running" (planned);
    #: pending stages on a started job mean the process died mid-pipeline.
    started: bool = False
    tombstoned: bool = False
    blocked_reason: str = ""
    last_error: str = ""
    stages: dict[str, StageState] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in PIPELINE_STAGES:
            self.stages.setdefault(name, StageState())
        if not self.created_at:
            self.created_at = _now()
        if not self.updated_at:
            self.updated_at = self.created_at

    # ── Stage state ───────────────────────────────────────────────────────
    def stage(self, name: str) -> StageState:
        return self.stages.setdefault(name, StageState())

    def status_of(self, name: str) -> str:
        return self.stage(name).status

    def is_settled(self, name: str) -> bool:
        return self.status_of(name) in SETTLED

    def unfinished(self, *, include_blocked: bool = True) -> list[str]:
        """Stages a resume would still try, in pipeline order."""
        out: list[str] = []
        for name in PIPELINE_STAGES:
            status = self.status_of(name)
            if status in INCOMPLETE or (include_blocked and status == BLOCKED):
                out.append(name)
        return out

    def resumable(self) -> list[str]:
        """Stages an *automatic* startup resume may act on.

        A blocked stage means the job needs a human or a config change, so the
        whole job is excluded: re-running its dependents on every boot would be
        a silent retry loop around a blocked precondition. A stage that has
        already used up its automatic attempts is excluded too — an explicit
        user retry clears that by resetting the stage to pending.

        A dependent stage is also excluded while the predecessor it needs has
        exhausted its automatic attempts. `project_doc_update` and
        `memory_proposals` both consume the insights text, so with `insights`
        out of budget they would be launched on every startup, immediately skip
        for lack of output, and leave the manifest unchanged forever.
        """
        if any(self.status_of(n) == BLOCKED for n in PIPELINE_STAGES):
            return []
        exhausted = {
            n
            for n in PIPELINE_STAGES
            if self.status_of(n) in INCOMPLETE
            and self.stage(n).attempts >= MAX_AUTO_ATTEMPTS
        }
        if "insights" in exhausted:
            return []
        return [
            n
            for n in PIPELINE_STAGES
            if self.status_of(n) in INCOMPLETE
            and self.stage(n).attempts < MAX_AUTO_ATTEMPTS
        ]

    def unfinished_stages(self) -> list[str]:
        """Every still-incomplete or blocked stage, regardless of attempts."""
        return self.unfinished()

    def prune_settled_inputs(self) -> None:
        """Drop the heavy session payload once no stage can still need it.

        The filtered session JSONL (assistant thinking, full Write/Edit/Bash
        inputs and results) is kept on the manifest only so insights and the
        trajectory can run. Once those stages are settled — or impossible to
        retry — the payload is dead weight that duplicates transcript data on
        every archived chat, so it is removed.

        A *blocked* insights/trajectory still counts as needing it: startup
        blocks those stages when the archive is missing, and the missing-file
        path explicitly supports a later restore, after which an explicit retry
        resets them to pending. Dropping the payload then would leave the retry
        with an empty transcript, permanently skipping the trajectory.
        """
        needs_payload = any(
            self.status_of(n) in (INCOMPLETE | {BLOCKED})
            for n in ("insights", "trajectory")
        )
        if not needs_payload:
            self.inputs.pop("filtered_jsonl", None)

    def mark(self, name: str, status: str, reason: str = "") -> None:
        stage = self.stage(name)
        if status == RUNNING:
            stage.attempts += 1
        stage.status = status
        if reason:
            stage.reason = reason
        stage.updated_at = _now()
        self.last_error = reason if status == FAILED else self.last_error
        self._refresh_state()

    def block(self, name: str, reason: str) -> None:
        """Mark a stage blocked and stamp the job's blocked reason."""
        self.mark(name, BLOCKED, reason)
        self.blocked_reason = reason

    def reset_failed(self, *, include_blocked: bool = False) -> None:
        """Make failed/running stages pending again for a fresh attempt.

        ``include_blocked`` is what an *explicit user retry* sets: a blocked
        stage was waiting for exactly that human action, so clearing the block
        and trying once more is the intent. An automatic startup resume must
        not pass it. A pending stage that has exhausted its automatic budget is
        also reset: otherwise an interrupted final attempt leaves it pending
        but permanently excluded, and a user retry would launch nothing.
        """
        for name in PIPELINE_STAGES:
            status = self.status_of(name)
            stage = self.stage(name)
            exhausted_pending = status == PENDING and stage.attempts >= MAX_AUTO_ATTEMPTS
            if (
                status in (FAILED, RUNNING)
                or exhausted_pending
                or (include_blocked and status == BLOCKED)
            ):
                stage.status = PENDING
                stage.reason = ""
                # An explicit retry earns a fresh automatic budget; otherwise a
                # stage that had exhausted MAX_AUTO_ATTEMPTS would be reset to
                # pending and immediately excluded again.
                stage.attempts = 0
                stage.updated_at = _now()
        self.blocked_reason = ""
        self._refresh_state()

    def _refresh_state(self) -> None:
        if self.tombstoned:
            self.state = TOMBSTONED
            return
        statuses = [self.status_of(n) for n in PIPELINE_STAGES]
        # A blocked or failed stage must not be masked by a later stage that is
        # merely pending: the job cannot make progress until the blocker is
        # resolved, and reporting "running" would hide the retry affordance.
        if RUNNING in statuses:
            self.state = RUNNING
        elif BLOCKED in statuses:
            self.state = "blocked"
        elif FAILED in statuses:
            self.state = "incomplete"
        elif PENDING in statuses:
            # A never-started plan is genuinely running; pending work on a
            # started job means the process died mid-pipeline, so the PWA must
            # offer a resume rather than a spinner nothing will clear.
            self.state = RUNNING if not self.started else "incomplete"
        else:
            self.state = "done"

    # ── Persistence ───────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "pipeline_version": self.pipeline_version,
            "job_id": self.job_id,
            "chat_id": self.chat_id,
            "archive_path": self.archive_path,
            "content_revision": self.content_revision,
            "post_insights_revision": self.post_insights_revision,
            "insights_append_revision": self.insights_append_revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "state": self.state,
            "started": self.started,
            "tombstoned": self.tombstoned,
            "blocked_reason": self.blocked_reason,
            "last_error": self.last_error,
            "stages": {name: state.to_dict() for name, state in self.stages.items()},
            "inputs": self.inputs,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, runtime_root: Path) -> ArchiveJob:
        stages_raw = raw.get("stages")
        stages: dict[str, StageState] = {}
        if isinstance(stages_raw, dict):
            for name, value in stages_raw.items():
                stages[str(name)] = StageState.from_dict(value)
        inputs_raw = raw.get("inputs")
        job = cls(
            job_id=str(raw.get("job_id", "")),
            chat_id=str(raw.get("chat_id", "")),
            archive_path=str(raw.get("archive_path", "")),
            runtime_root=str(runtime_root),
            content_revision=str(raw.get("content_revision", "")),
            post_insights_revision=str(raw.get("post_insights_revision", "")),
            insights_append_revision=str(raw.get("insights_append_revision", "")),
            pipeline_version=int(raw.get("pipeline_version", PIPELINE_VERSION) or 0),
            manifest_version=int(raw.get("manifest_version", MANIFEST_VERSION) or 0),
            created_at=str(raw.get("created_at", "")),
            updated_at=str(raw.get("updated_at", "")),
            state=str(raw.get("state", RUNNING)),
            started=bool(raw.get("started", False)),
            tombstoned=bool(raw.get("tombstoned", False)),
            blocked_reason=str(raw.get("blocked_reason", "")),
            last_error=str(raw.get("last_error", "")),
            stages=stages,
            inputs=dict(inputs_raw) if isinstance(inputs_raw, dict) else {},
        )
        return job

    def save(self) -> bool:
        """Persist the manifest; return True when the write landed.

        Callers that are about to perform a stage mutation rely on the manifest
        being durable *before* the mutation. A direct caller with no runtime
        root (the legacy ``extract_and_append``, tests) is intentionally
        in-memory only and reports success.
        """
        if not self.runtime_root:
            return True
        # Drop the heavy session payload once no stage can still read it, so a
        # settled manifest never keeps a full transcript copy.
        self.prune_settled_inputs()
        return save_job(Path(self.runtime_root), self)


# One lock per manifest file: the runner may update a job from an async task
# while a startup pass or a delete reads it. Single-owner process, so an
# in-process lock is enough; ``os.replace`` covers the crash-atomicity half.
_JOB_LOCKS: dict[str, threading.Lock] = {}
_JOB_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _JOB_LOCKS_GUARD:
        lock = _JOB_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _JOB_LOCKS[key] = lock
        return lock


def load_job(runtime_root: Path, job_id: str) -> ArchiveJob | None:
    """Read one manifest, or None when missing/invalid/out-of-version.

    Every parse and schema step is guarded: a syntactically valid but malformed
    record (a nonnumeric version, a wrong-typed field) returns None here instead
    of raising, so `list_jobs`/startup recovery cannot be aborted by one bad
    file.
    """
    path = job_path(runtime_root, job_id)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("archive jobs: unreadable manifest %s", path)
        return None
    if not isinstance(raw, dict):
        return None
    try:
        if int(raw.get("manifest_version", 0) or 0) != MANIFEST_VERSION:
            return None
        if int(raw.get("pipeline_version", 0) or 0) != PIPELINE_VERSION:
            return None
        return ArchiveJob.from_dict(raw, runtime_root=runtime_root)
    except (ValueError, TypeError):
        logger.warning("archive jobs: malformed manifest %s", path)
        return None


def save_job(runtime_root: Path, job: ArchiveJob) -> bool:
    """Atomically persist a manifest; never clear an existing tombstone.

    Returns True when the write landed, False when it could not be written.
    """
    path = job_path(runtime_root, job.job_id)
    with _lock_for(path):
        if not job.tombstoned:
            existing = _read_raw(path)
            if existing is not None and bool(existing.get("tombstoned", False)):
                # The chat was deleted mid-run; refuse to resurrect the job.
                job.tombstoned = True
                job.state = TOMBSTONED
                job.blocked_reason = str(
                    existing.get("blocked_reason") or "chat deleted"
                )
        job.updated_at = _now()
        return _write_raw(path, job.to_dict())


def _read_raw(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def create_job(
    runtime_root: Path,
    *,
    chat_id: str,
    archive_path: str,
    content_revision_value: str = "",
    inputs: dict[str, Any] | None = None,
) -> ArchiveJob:
    job = ArchiveJob(
        job_id=new_job_id(chat_id, archive_path),
        chat_id=chat_id,
        archive_path=archive_path,
        runtime_root=str(runtime_root),
        content_revision=content_revision_value,
        inputs=dict(inputs or {}),
    )
    job._refresh_state()
    job.save()
    return job


def tombstone_job(
    runtime_root: Path, job_id: str, *, reason: str = "chat deleted"
) -> bool:
    """Mark a job's chat as deleted so no resume can revive it.

    Creates the tombstone even when no manifest exists yet: a running task may
    write its manifest after the delete, and ``save_job`` must then refuse to
    clear the flag. Writes under the file lock directly, rather than through
    :meth:`ArchiveJob.save`, because the lock is not reentrant.
    """
    path = job_path(runtime_root, job_id)
    with _lock_for(path):
        raw = _read_raw(path)
        if raw is None:
            job = ArchiveJob(
                job_id=job_id,
                chat_id="",
                archive_path="",
                runtime_root=str(runtime_root),
                tombstoned=True,
                state=TOMBSTONED,
                blocked_reason=reason,
            )
        else:
            job = ArchiveJob.from_dict(raw, runtime_root=runtime_root)
            job.tombstoned = True
            job.state = TOMBSTONED
            job.blocked_reason = reason
        job.updated_at = _now()
        return _write_raw(path, job.to_dict())


def _write_raw(path: Path, payload: dict[str, Any]) -> bool:
    """Atomic temp-file write of one manifest. Caller holds the file lock.

    Returns True when the replace landed. A write failure is reported rather
    than swallowed so a caller can refuse to perform a stage mutation whose
    recovery evidence did not persist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)
        return True
    except OSError:
        logger.warning("archive jobs: could not write manifest %s", path, exc_info=True)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def list_jobs(runtime_root: Path) -> list[ArchiveJob]:
    """Every valid manifest, oldest first. Unreadable files are skipped."""
    root = jobs_root(runtime_root)
    if not root.is_dir():
        return []
    jobs: list[ArchiveJob] = []
    for path in sorted(root.glob("*.json")):
        if path.name.startswith("."):
            continue
        job = load_job(runtime_root, path.stem)
        if job is not None:
            jobs.append(job)
    jobs.sort(key=lambda j: j.created_at)
    return jobs


def manifest_view(job: ArchiveJob) -> dict[str, Any]:
    """The postprocess-friendly projection of a job.

    ``steps`` uses the same job ids as the live step events, so the manager can
    overlay a settled manifest onto the telemetry the PWA already renders.
    """
    steps: dict[str, dict[str, Any]] = {}
    status_map = {
        SUCCEEDED: "ok",
        SKIPPED: "skipped",
        FAILED: "error",
        BLOCKED: "blocked",
        PENDING: "pending",
        RUNNING: "running",
    }
    for name in PIPELINE_STAGES:
        stage = job.stage(name)
        steps[name] = {
            "status": status_map.get(stage.status, "pending"),
            "reason": stage.reason,
            "attempts": stage.attempts,
        }
    return {
        "job_id": job.job_id,
        "state": job.state,
        "tombstoned": job.tombstoned,
        "blocked_reason": job.blocked_reason,
        "unfinished": job.unfinished(),
        "steps": steps,
        "updated_at": job.updated_at,
    }
