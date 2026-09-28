"""The unattended five-minute backup of this install's durable data.

This replaces the push-only 30s loop, which was wrong in three ways at once.
It decided *once*, at process start, whether there was a repository and an
``origin`` remote, so an install that was configured later (a remote added, a
vault moved into its own repository) never backed up anything until someone
restarted the engine — and then it never asked again. It pushed whatever branch
it found without committing anything, so on a plain data repository the notes
were never in a commit at all and every push uploaded nothing. And on a
developer checkout it pushed the app source and whatever the user happened to
have staged.

What replaced it does one job, on a five-minute cadence, in one place:

- **Readiness is re-evaluated every tick**, never at startup. A root that is not
  a repository, is on a detached HEAD, or has no ``origin`` reports
  ``not_configured`` and is re-checked five minutes later, so configuring a
  remote while the engine runs is enough — there is no restart to ask for.
- **The commit is scoped** to :mod:`ciao.backup_scope` through
  ``local_session.commit_scoped``, so an unattended run commits the durable
  trees and nothing else, and never stages a credential, a cache or the
  application source. A clean scoped tree creates no commit: the failure mode of
  a backup that commits on every tick is a repository that cannot be read.
- **A clean run creates nothing at all.** Unpushed *existing* commits are still
  uploaded — that is the case a push-only loop was good at — but with nothing
  pending and nothing unpushed, the service does no git work beyond reading the
  state, because a backup that rewrites history on every tick is noise in the
  owner's log.

Everything it does runs inside ``git_mutation.repository_mutation``, so a
backup never interleaves git commands with a manual sync, and
``ensure_mutable`` refuses a repository another git operation holds instead of
racing it. There is no second git loop: the commit, the preflight and the push
are the existing ``local_session`` operations, reused whole.

Two successes are kept apart, because they fail apart. A commit that lands
locally is durable work, and a push that fails leaves it exactly where it is —
nothing is reset, nothing is force-pushed, nothing is discarded, and the
status says ``offline`` rather than claiming a backup that did not happen. Only
a push that landed updates the success evidence, so ``last_success_commit`` is
always a commit known to exist on the remote.

The owner can turn the service off, pause it, or ask for a run now, and all
three go through the same durable store (``ciao.app_settings``) and the same
code path: a paused boot simply never reaches the repository, and a manual run
is the same serialized call the loop makes, so the two can never interleave.

Host ownership is the one rule this module does not get to decide. A Mac whose
legacy ``node_state.json`` says it was a *client* of another host must not be a
second writer on the same runtime root, so the service asks that one boot
verdict (``ciao.legacy_node_state``) before touching anything, and says so in
its status rather than quietly backing up where the host already does.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ciao import backup_scope, job_runs, local_session
from ciao.app_settings import AppSettingsStore
from ciao.git_mutation import RepositoryBusyError, ensure_mutable, repository_mutation
from ciao.git_proc import run_git_sync
from ciao.legacy_node_state import LegacyNodeState, writers_armed
from ciao.local_session import GitOperationError

logger = logging.getLogger(__name__)

#: Seconds between unattended backups. A named constant, not a setting and not
#: an env var: the cadence is a property of the service, and five minutes is
#: short enough that a laptop closed for an afternoon has lost at most one
#: interval's worth of notes while long enough that the loop is never the reason
#: a repository is slow.
BACKUP_INTERVAL_S = 300

#: Cadence multiplier while the last failure cannot self-heal at the normal
#: interval — bad credentials, a remote that is not answering, or a branch that
#: has genuinely diverged from ``origin``. It is a multiplier and not a doubling
#: because the bound is the point: the cadence can only ever be the interval or
#: twelve times it (an hour), so an unreachable remote costs one push attempt an
#: hour rather than an unbounded retry storm, and a restored network or a fixed
#: conflict is picked up on the next tick at the latest.
BACKOFF_MULTIPLIER = 12

#: The job id the Automation page has carried this work under since the push
#: loop was introduced. Kept so the row continues rather than resetting, and so
#: ``job_runs_latest.json`` never serves a duplicate.
JOB_ID = "branch_backup"
JOB_LABEL = "Memory backup"

# The states the status can report. Deliberately a closed set: this is what a
# Settings surface renders, so "some new adjective" is not an option.
#
# - ``not_configured`` — the data root is not a repository, is on a detached
#   HEAD, or has no ``origin``. Re-checked every tick, so configuring a remote
#   while the engine runs is enough to become ready without a restart.
# - ``ready`` — configured, and the scoped tree is committed and pushed.
# - ``pending`` — configured, with work waiting: a scoped change to commit, or a
#   commit that never reached the remote.
# - ``running`` — a run is in flight right now (the status endpoint's answer
#   while the loop or a manual trigger holds the lock).
# - ``paused`` — this boot will not run a backup: turned off, paused, or not the
#   host. The three are distinguished in ``reason``.
# - ``offline`` — the commit is safe locally and the push could not reach the
#   remote. Retried on the backoff cadence; no local work is lost.
# - ``needs_attention`` — a failure only the owner can fix: a credential-shaped
#   file inside the scope, a repository another git operation holds, a branch
#   that diverged from ``origin`` (the commit is on a per-commit backup ref, so
#   it is off this machine, but the shared branch still needs a human).
STATE_NOT_CONFIGURED = "not_configured"
STATE_READY = "ready"
STATE_PENDING = "pending"
STATE_RUNNING = "running"
STATE_PAUSED = "paused"
STATE_OFFLINE = "offline"
STATE_NEEDS_ATTENTION = "needs_attention"

#: States a run can end in when the push did not land, split by whether the
#: cadence should back off. A repository another git operation holds is *not* in
#: it: a stray ``index.lock`` clears in seconds, and costing an hour of backup
#: latency for it would be a worse answer than retrying.
_FAILURE_STATES = frozenset({STATE_OFFLINE, STATE_NEEDS_ATTENTION})

#: Paths named in a coverage-gap reason. The gap itself is reported in full by
#: the preflight; a status line is not the place for three hundred app-source
#: paths.
_REASON_PATHS = 3

#: A remote is a line in a status payload, not a log entry.
_REMOTE_MAX_CHARS = 200


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class BackupStatus:
    """What the backup service knows about itself, in one serializable value.

    The persisted half (``enabled``, ``interval_s``, ``last_attempt_at``,
    ``last_success_at``, ``last_success_commit``) survives a restart; the rest
    is read from the repository on every call. ``last_success_at`` and
    ``last_success_commit`` are only ever written by a push that landed, so
    together they are evidence that a named commit exists on the remote — the
    one claim a user cannot check without leaving the app.
    """

    state: str
    #: What an unattended run may commit, in human words (see ``_scope_summary``).
    scope: str = ""
    branch: str = ""
    #: The ``origin`` URL with any credentials removed. Never the raw string.
    remote: str = ""
    enabled: bool = True
    interval_s: int = BACKUP_INTERVAL_S
    #: When the service last *tried* (never updated by a paused or not-the-host
    #: tick, so unpausing or becoming the host runs immediately rather than
    #: after a full interval).
    last_attempt_at: str = ""
    last_success_at: str = ""
    last_success_commit: str = ""
    #: Scoped files waiting to be committed.
    pending_changes: int = 0
    #: Local commits on ``branch`` that ``origin/branch`` does not have.
    pending_commits: int = 0
    #: Plain-language explanation of ``state`` — the failure detail, the gap, or
    #: which of the three reasons a ``paused`` state is reporting.
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        """The status as the API serves it."""
        return asdict(self)


# ── the small git questions ──────────────────────────────────────────────────


def _unpushed_commits(root: Path, branch: str) -> int:
    """Commits on ``branch`` that ``origin/branch`` does not have.

    The remote-tracking ref is a local ref, so this is one cheap ``rev-list``
    with no network — which is what makes it safe to run on every status read.
    A branch the remote has never seen has no ref to count against, and there
    every local commit is genuinely unpushed, so the count falls back to the
    whole branch. It is a heuristic for "is a push worth attempting", never a
    correctness gate: a stale tracking ref can only make the answer wrong by
    asking for a push that git then rejects on its own terms.
    """
    for revision in (f"origin/{branch}..HEAD", "HEAD"):
        rc, out, _err = run_git_sync(Path(root), "rev-list", "--count", revision)
        if rc != 0:
            continue
        try:
            return max(0, int(out.strip()))
        except ValueError:
            logger.info("git rev-list --count %s printed %r", revision, out.strip())
            return 0
    return 0


def _head_sha(root: Path) -> str:
    """The short HEAD sha, or "" when it cannot be resolved."""
    rc, out, _err = run_git_sync(Path(root), "rev-parse", "--short=12", "HEAD")
    return out.strip() if rc == 0 else ""


def _origin_url(root: Path) -> str:
    """``root``'s ``origin`` URL, credential-free, or "" when there is none.

    Asked on every tick rather than cached at startup, because adding a remote
    is the single most common way an install becomes backup-ready later.
    """
    rc, out, _err = run_git_sync(Path(root), "remote", "get-url", "origin")
    return sanitize_remote(out.strip()) if rc == 0 else ""


def sanitize_remote(url: str) -> str:
    """``url`` as a human may read it, with anything credential-shaped removed.

    An ``https`` remote is routinely spelled with a token in it — that is what
    ``gh auth setup-git`` and a hand-typed ``git remote set-url`` leave behind —
    and a status payload, a log line and a job-run record are all places that
    string would then be copied to. The userinfo is dropped whatever it holds,
    because a token is at least as often the *username* as the password, and
    the port survives. The scp-like form (``git@host:path``) keeps its host and
    path and only loses a user that is not the conventional ``git``.
    """
    text = (url or "").strip()
    if not text:
        return ""
    parts = urlsplit(text)
    if parts.scheme and parts.netloc:
        try:
            port = parts.port
        except ValueError:  # a port that is not a number: not a real URL then
            return _REMOTE_PLACEHOLDER
        host = parts.hostname or ""
        netloc = f"{host}:{port}" if port else host
        text = urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))
    elif "@" in text and ":" in text.partition("@")[2]:
        user, _, rest = text.partition("@")
        text = f"{'git' if user == 'git' else _REMOTE_PLACEHOLDER}@{rest}"
    return text[:_REMOTE_MAX_CHARS]


_REMOTE_PLACEHOLDER = "***"


def _scope_summary(config) -> str:
    """The backup scope as one human line, derived from the scope itself.

    Rendered from ``eligible_relpaths`` rather than a second list of names, so
    the status can never describe a different scope than the one
    ``commit_scoped`` enforces. The per-prefix noise (the ``*`` an archived
    workspace adds, the trailing slash) is dropped, and a vault that *is* the
    data root — whose scope is everything under it — is named as such.
    """
    names: list[str] = []
    for prefix in backup_scope.eligible_relpaths(config):
        segments = [part for part in prefix.split("/") if part and part != "*"]
        durable = next((s for s in segments if s in backup_scope.DURABLE_ROOTS), None)
        if durable is None:
            durable = segments[0] if segments else "(the whole data root)"
        if durable not in names:
            names.append(durable)
    return ", ".join(names)


def _coverage_gap(tracked_excluded: list[str]) -> str:
    """One line naming what git already tracks that the scope refuses, or "".

    The paths are truncated: a developer checkout reports its whole application
    source, and a status line is not the place for three hundred of them. The
    full set is in the preflight the run already read.
    """
    if not tracked_excluded:
        return ""
    named = ", ".join(tracked_excluded[:_REASON_PATHS])
    more = len(tracked_excluded) - _REASON_PATHS
    return (
        f"{len(tracked_excluded)} tracked path(s) outside the backup scope "
        f"would not be backed up: {named}"
        + (f" (+{more} more)" if more > 0 else "")
    )


def _coverage_blockers(tracked_excluded: list[str]) -> set[str]:
    """The ``preflight_scoped`` blockers that are exactly the coverage gap.

    ``preflight_scoped`` reports both kinds of blocker in one list, and this
    service must treat them differently: a tracked file outside the scope is a
    gap to *report* (the commit is path-limited, so it cannot ride along, and
    blocking on it would refuse to back up anything on any repository that is
    also a checkout), while a credential inside the scope must stop the run
    before it stages anything. Subtracting the gap's own blockers — which
    ``preflight_scoped`` derives from the ``tracked_excluded`` it returns
    alongside them — leaves exactly the blockers that stop a run.
    """
    return {f"Tracked but outside the backup scope: {rel}" for rel in tracked_excluded}


def _hard_blockers(report: dict) -> list[str]:
    """Preflight blockers that stop a run, with the coverage gap removed."""
    gap = _coverage_blockers(list(report.get("tracked_excluded") or []))
    return [str(b) for b in (report.get("blockers") or []) if str(b) not in gap]


def _commit_message(now: datetime) -> str:
    """The commit message an unattended run writes.

    The same UTC stamp the manual path uses, so a reader comparing the two
    cannot tell from the message which produced the commit — the content is the
    difference, not the wording.
    """
    return f"backup {now.strftime('%Y-%m-%d %H:%M:%SZ')}"


# ── the service ──────────────────────────────────────────────────────────────


class BackupService:
    """The one backup path: the loop, the manual trigger, and the status.

    One instance per process, built in ``main`` and published as
    ``app.state.backup_service`` so the API and the loop cannot end up with two
    services holding two locks. It owns three pieces of state the module needs
    and nothing else: the serialization lock that keeps a manual and a scheduled
    run from overlapping, the flag the status reports as ``running``, and the
    backoff the last failure earned.
    """

    def __init__(
        self,
        config,
        store: AppSettingsStore,
        *,
        node_state: Callable[[], LegacyNodeState],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._store = store
        # The boot's one verdict, asked per tick rather than copied: a state
        # that cannot be understood fails closed, and the whole point of
        # asking again is that the answer is a property of the runtime root, not
        # of the tick.
        self._node_state = node_state
        self._now = now or _utcnow
        self._lock = asyncio.Lock()
        self._running = False
        #: Why the cadence is backed off (``"auth"``, ``"unreachable"``,
        #: ``"diverged"``), or None. Never stale: every run overwrites it.
        self._backoff: str | None = None
        #: Last failure detail and how many times it has repeated, so a
        #: persistent fault is counted rather than re-logged every tick.
        self._last_failure = ""
        self._repeat_failures = 0
        #: Per-run telemetry for the job row. Operational facts only — never
        #: anything read out of the vault.
        self._run_extra: dict[str, Any] = {}

    # ── settings ────────────────────────────────────────────────────────────

    def set_flags(
        self, *, enabled: bool | None = None, paused: bool | None = None
    ) -> None:
        """Turn the service off/on, or pause/resume it. Persists immediately.

        The flags are read from the store on every gate check rather than
        cached, so a change takes effect on the next tick without a restart and
        survives one. They are distinct on purpose: ``enabled`` is the owner's
        standing decision, ``paused`` a hold they can lift.
        """
        changes: dict[str, object] = {}
        if enabled is not None:
            changes["backup_enabled"] = enabled
        if paused is not None:
            changes["backup_paused"] = paused
        if changes:
            self._store.update(changes)

    # ── status ──────────────────────────────────────────────────────────────

    async def status(self) -> BackupStatus:
        """The current state, without mutating the repository.

        Read-only on purpose: this is what a Settings surface polls, and a
        status call that committed or pushed would be a surprising way to read
        a page. It costs the same read-only work a run does before it stages
        anything — one ``git status``, one scope classification, one
        ``rev-list`` — because both answers come from the same call.
        """
        if self._running:
            return self._status(STATE_RUNNING, reason="a backup is running now")
        gate = self._gate_reason()
        if gate:
            return self._status(STATE_PAUSED, reason=gate)
        root = await self._data_root()
        branch, remote = await self._target(root)
        if not branch or not remote:
            return self._status(
                STATE_NOT_CONFIGURED,
                reason=_not_configured_reason(branch, remote),
                branch=branch,
                remote=remote,
            )
        report, unpushed = await self._inspect(root, branch)
        return self._status(
            _read_state(report, unpushed),
            reason=_read_reason(report, unpushed),
            branch=branch,
            remote=remote,
            pending_changes=len(report.get("eligible") or []),
            pending_commits=unpushed,
        )

    # ── the run ─────────────────────────────────────────────────────────────

    async def run_backup(self, *, source: str = "manual") -> BackupStatus:
        """One backup attempt, serialized; returns the state it left behind.

        The scheduled loop and the manual trigger both come through here, so
        there is exactly one implementation of "commit the scope, then push",
        one lock in front of it, and no way for the two to interleave git
        commands or to differ in what they consider a success.

        Never raises. A backup is unattended: an exception escaping into the
        loop would kill the cadence, and one escaping into a route would be a
        500 for something the user can act on no better than a reported state.
        An unexpected fault is therefore a ``needs_attention`` status carrying
        its own type and message, logged once with a traceback.
        """
        async with self._lock:
            self._running = True
            self._run_extra = {}
            try:
                async with job_runs.track(
                    JOB_ID,
                    JOB_LABEL,
                    category="system",
                    extra={"source": source},
                ) as run:
                    status = await self._guarded_run()
                    self._record(run, status, source)
                    return status
            finally:
                self._running = False

    async def _guarded_run(self) -> BackupStatus:
        """:meth:`run_backup`'s body, with the fault boundary around it."""
        try:
            return await self._run_locked()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — an unattended run must survive
            logger.exception("Memory backup failed unexpectedly")
            self._backoff = None
            return self._status(
                STATE_NEEDS_ATTENTION,
                reason=f"{type(exc).__name__}: {exc}",
            )

    async def _run_locked(self) -> BackupStatus:
        """The run itself. The serialization lock is already held."""
        self._backoff = None
        gate = self._gate_reason()
        if gate:
            # Deliberately not an attempt: leaving the attempt stamp alone is
            # what makes the first tick after an unpause (or after this Mac
            # becomes the host) run immediately instead of a full interval
            # later.
            return self._status(STATE_PAUSED, reason=gate)

        now = self._now()
        root = await self._data_root()
        # Readiness is re-read here, at run time, and never cached from
        # startup: a remote added or a vault moved into its own repository while
        # the engine runs has to be picked up by the next tick.
        branch, remote = await self._target(root)
        if not branch or not remote:
            self._store.update({"backup_last_attempt_at": now.isoformat()})
            return self._status(
                STATE_NOT_CONFIGURED,
                reason=_not_configured_reason(branch, remote),
                branch=branch,
                remote=remote,
            )

        async with repository_mutation(root):
            try:
                ensure_mutable(root)
            except RepositoryBusyError as exc:
                # Another git operation owns this checkout. Left alone for its
                # owner, and retried at the normal cadence: a lock clears in
                # seconds, so backing off an hour for it would cost more than it
                # saves.
                self._store.update({"backup_last_attempt_at": now.isoformat()})
                return self._status(
                    STATE_NEEDS_ATTENTION,
                    reason=exc.detail,
                    branch=branch,
                    remote=remote,
                )

            report, unpushed = await self._inspect(root, branch)
            blocked = _hard_blockers(report)
            if blocked:
                # A credential-shaped file inside the scope. Nothing is staged,
                # nothing is pushed, and the offending path is named — the
                # alternative is a backup that carries a credential off the
                # machine every five minutes.
                self._store.update({"backup_last_attempt_at": now.isoformat()})
                return self._status(
                    STATE_NEEDS_ATTENTION,
                    reason="; ".join(blocked),
                    branch=branch,
                    remote=remote,
                    pending_changes=len(report.get("eligible") or []),
                    pending_commits=unpushed,
                )

            committed = False
            eligible = list(report.get("eligible") or [])
            # A coverage gap is a standing condition rather than a transient
            # one: the commit is path-limited, so nothing outside the scope can
            # ride along, but a repository that is also a checkout is only ever
            # partly backed up and the owner has to be told. Derived once here
            # and reported by every successful return, so a read and a run can
            # never disagree about whether the repository is fully covered.
            gap = _coverage_gap(list(report.get("tracked_excluded") or []))
            if eligible:
                try:
                    committed = await local_session.commit_scoped(
                        root,
                        branch=branch,
                        relpaths=eligible,
                        message=_commit_message(now),
                    )
                except GitOperationError as exc:
                    self._store.update({"backup_last_attempt_at": now.isoformat()})
                    return self._status(
                        STATE_NEEDS_ATTENTION,
                        reason=f"{exc.step} failed: {exc.detail}",
                        branch=branch,
                        remote=remote,
                        pending_changes=len(eligible),
                        pending_commits=unpushed,
                    )
            self._run_extra["committed"] = committed

            # Counted after the commit, so the new one is included. Zero means
            # there is genuinely nothing to upload and the push is skipped: a
            # clean run creates nothing.
            unpushed = await asyncio.to_thread(_unpushed_commits, root, branch)
            if not unpushed:
                self._store.update(
                    {
                        "backup_last_attempt_at": now.isoformat(),
                        "backup_remote": remote,
                    }
                )
                return self._status(
                    _landed_state(gap),
                    reason=_landed_reason(committed, gap),
                    branch=branch,
                    remote=remote,
                    pending_commits=0,
                )

            ok, detail = await local_session.push_branch(root, branch=branch)
            self._run_extra["pushed"] = ok
            self._store.update(
                {
                    "backup_last_attempt_at": now.isoformat(),
                    "backup_remote": remote,
                }
            )
            if not ok:
                # The commit is in the local repository and stays there. No
                # reset, no force-push, and no claim of a backup that did not
                # happen: only a landed push writes the success evidence.
                reason = _offline_reason(detail)
                self._backoff = local_session.backoff_reason(detail) or "push"
                return self._status(
                    reason[0],
                    reason=reason[1],
                    branch=branch,
                    remote=remote,
                    pending_commits=unpushed,
                )
            if local_session.is_diverged_backup(detail):
                # A real merge conflict with ``origin/<branch>``: the commit did
                # leave the machine, on a per-commit backup ref, but the shared
                # branch still needs a human. Reported as needing attention
                # (with the ref named) while still counting as a success, and
                # backed off for the same reason an unreachable remote is.
                self._backoff = "diverged"
                self._store.update(
                    {
                        "backup_last_success_at": now.isoformat(),
                        "backup_last_success_commit": await asyncio.to_thread(
                            _head_sha, root
                        ),
                    }
                )
                return self._status(
                    STATE_NEEDS_ATTENTION,
                    reason=detail,
                    branch=branch,
                    remote=remote,
                )
            self._store.update(
                {
                    "backup_last_success_at": now.isoformat(),
                    "backup_last_success_commit": await asyncio.to_thread(
                        _head_sha, root
                    ),
                }
            )
            return self._status(
                _landed_state(gap),
                reason=_landed_reason(committed, gap),
                branch=branch,
                remote=remote,
            )

    def _record(
        self, run: job_runs.RunHandle, status: BackupStatus, source: str
    ) -> None:
        """Fold one run's outcome into its job row, the log, and the counters.

        An identical repeat is a ``skip`` rather than a second error: a remote
        that stayed unreachable for a day would otherwise write the same error
        row every hour, and the Automation page would show a red failure that
        has been true since yesterday rather than a count of how long.
        """
        run.extra.update(self._run_extra)
        run.extra["state"] = status.state
        run.extra["branch"] = status.branch
        run.extra["source"] = source
        run.extra["pending_changes"] = status.pending_changes
        run.extra["pending_commits"] = status.pending_commits
        if status.state in _FAILURE_STATES:
            if status.reason and status.reason == self._last_failure:
                self._repeat_failures += 1
                run.skip("same failure as the previous backup attempt")
                run.extra["repeat_count"] = self._repeat_failures
                logger.debug("Memory backup still failing: %s", status.reason)
            else:
                self._last_failure = status.reason
                self._repeat_failures = 1
                run.status = "error"
                run.error = status.reason
                logger.warning("Memory backup failed: %s", status.reason)
            return
        self._clear_failure(run, status)

    def _clear_failure(self, run: job_runs.RunHandle, status: BackupStatus) -> None:
        """Close the failure episode: a success opens a fresh one next time."""
        if self._last_failure:
            logger.info("Memory backup recovered: %s", status.reason or status.state)
        self._last_failure = ""
        self._repeat_failures = 0
        if status.state == STATE_PAUSED:
            run.skip(status.reason)

    # ── the loop ────────────────────────────────────────────────────────────

    async def backup_loop(
        self, *, stop: asyncio.Event, sleep: Callable[[float], Any] = asyncio.sleep
    ) -> None:
        """Run a backup every :data:`BACKUP_INTERVAL_S` until ``stop`` is set.

        Three properties the loop has that a bare ``while True: sleep; work`` does
        not:

        - **It is overdue-aware at startup.** The first delay is the time left
          in the current interval, so a machine that was asleep (or an engine
          that was restarted) backs up immediately instead of waiting out a full
          interval first — and it still waits when the last attempt was moments
          ago, so a restart loop cannot turn into a push storm.
        - **It backs off, boundedly.** A failure that cannot self-heal at this
          cadence (credentials, an unreachable remote, a genuinely diverged
          branch) buys the hour; everything else retries at the interval, and a
          success clears the backoff.
        - **It re-evaluates every tick.** Readiness, the pause flag and the host
          verdict are all re-read per tick, so a repository configured later
          becomes ready, and a pause takes and lifts, without a restart.

        ``sleep`` is a parameter so a test can drive the cadence on a controlled
        clock; production passes ``asyncio.sleep``.
        """
        while not stop.is_set():
            delay = self._next_delay()
            if delay > 0 and not await self._wait(stop, delay, sleep):
                return
            if stop.is_set():
                return
            try:
                await self.run_backup(source="scheduled")
            except asyncio.CancelledError:
                break
            # No other exception can reach here — ``run_backup`` converts one
            # into a status — so the loop cannot die of a fault in its own work.

    def _next_delay(self) -> float:
        """Seconds until the next attempt: what is left of the interval.

        Zero when there is no attempt on record (a fresh install backs up at
        once) and zero when the last attempt is already older than the interval
        (the overdue resume). With a backoff in force the whole interval is
        multiplied, so the overdue case waits an hour rather than firing every
        five minutes against a remote that is not answering.
        """
        elapsed = self._elapsed_since_attempt()
        interval = BACKUP_INTERVAL_S * (
            BACKOFF_MULTIPLIER if self._backoff else 1
        )
        if elapsed is None:
            return 0.0
        return max(0.0, interval - elapsed)

    def _elapsed_since_attempt(self) -> float | None:
        """Seconds since the last attempt, or None when there is none to read.

        A stamp that cannot be parsed (hand-edited, written by a build whose
        format differs) is treated as no stamp: running one tick early is
        harmless, and refusing to run at all would be not.
        """
        raw = self._store.settings.backup_last_attempt_at
        if not raw:
            return None
        try:
            return max(0.0, (self._now() - datetime.fromisoformat(raw)).total_seconds())
        except ValueError:
            logger.info("Unreadable backup attempt stamp %r; backing up now", raw)
            return None

    @staticmethod
    async def _wait(
        stop: asyncio.Event, delay: float, sleep: Callable[[float], Any]
    ) -> bool:
        """Sleep, or return False as soon as ``stop`` is set.

        A plain ``await sleep(delay)`` cannot be woken: a service being asked to
        shut down would sit out the rest of the interval first, which is up to
        an hour with a backoff in force.
        """
        if stop.is_set():
            return False
        sleeper = asyncio.ensure_future(sleep(delay))
        waiter = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait(
                {sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (sleeper, waiter):
                if not task.done():
                    task.cancel()
        return not stop.is_set()

    # ── shared reads ────────────────────────────────────────────────────────

    async def _data_root(self) -> Path:
        """The repository whose durable data this install backs up."""
        return await asyncio.to_thread(backup_scope.data_root, self._config)

    async def _target(self, root: Path) -> tuple[str, str]:
        """``(branch, remote)`` for ``root`` right now; either may be empty.

        Both are subprocesses, so they go off the loop — a status read is served
        from the same event loop that is running a chat.
        """
        branch = await asyncio.to_thread(local_session.workspace_branch, root)
        remote = await asyncio.to_thread(_origin_url, root)
        return branch or "", remote

    async def _inspect(self, root: Path, branch: str) -> tuple[dict, int]:
        """The scoped preflight and the unpushed count, in one read.

        Both answers come from the same ``preflight_scoped`` call the run stages
        from, so the status can never report a different scope than the commit
        would take.
        """
        report = await local_session.preflight_scoped(self._config, root)
        unpushed = await asyncio.to_thread(_unpushed_commits, root, branch)
        return report, unpushed

    def _gate_reason(self) -> str:
        """Why this boot will not run a backup, or "" when it will.

        Three reasons, one of which is not the owner's to give: a Mac whose
        legacy node state is a client must not become a second writer on a
        runtime root the host already owns. The kind is named rather than
        smoothed over, because "the host does it" is only actionable if the
        owner can see which of the two verdicts applied.
        """
        settings = self._store.settings
        if not settings.backup_enabled:
            return "backup is turned off"
        if settings.backup_paused:
            return "backup is paused"
        state = self._node_state()
        if not writers_armed(state):
            return (
                f"this Mac is not the host (legacy node state: {state.kind}) — "
                "the host owns backup"
            )
        return ""

    def _status(
        self,
        state: str,
        *,
        reason: str = "",
        branch: str = "",
        remote: str = "",
        pending_changes: int = 0,
        pending_commits: int = 0,
    ) -> BackupStatus:
        """Build a status from the live state and the persisted record."""
        settings = self._store.settings
        return BackupStatus(
            state=state,
            scope=_scope_summary(self._config),
            branch=branch,
            remote=remote,
            enabled=bool(settings.backup_enabled),
            interval_s=BACKUP_INTERVAL_S,
            last_attempt_at=settings.backup_last_attempt_at,
            last_success_at=settings.backup_last_success_at,
            last_success_commit=settings.backup_last_success_commit,
            pending_changes=pending_changes,
            pending_commits=pending_commits,
            reason=reason,
        )


# ── the small explanations ──────────────────────────────────────────────────


def _not_configured_reason(branch: str, remote: str) -> str:
    """Why there is nothing to back up to, in the owner's terms."""
    if not branch:
        return "the data root is not a git repository (or is on a detached HEAD)"
    if not remote:
        return "the data root has no 'origin' remote yet"
    return ""


def _offline_reason(detail: str) -> tuple[str, str]:
    """``(state, reason)`` for a push that did not land.

    Only a remote that is unreachable or refused on credentials is reported as
    ``offline`` — the two that clear on their own and are worth retrying on the
    slow cadence. Everything else (a rejected non-fast-forward, a hook refusing
    the push) is something only the owner can fix, and calling it "offline"
    would point the fix at the network.
    """
    reason = local_session.backoff_reason(detail)
    if reason == "unreachable":
        return STATE_OFFLINE, f"origin did not answer: {detail}"
    if reason == "auth":
        return STATE_NEEDS_ATTENTION, f"origin refused the credentials: {detail}"
    return STATE_NEEDS_ATTENTION, detail


def _clean_reason(committed: bool) -> str:
    """What a successful run did, in one line."""
    if committed:
        return "committed and pushed the backup scope"
    return "nothing to back up; the repository is already up to date"


def _landed_state(gap: str) -> str:
    """The state a run that did its work ends in.

    A coverage gap downgrades it: the run succeeded, and the repository is
    still only partly covered by an unattended commit, which is a thing the
    owner has to resolve rather than a thing that repeats itself.
    """
    return STATE_NEEDS_ATTENTION if gap else STATE_READY


def _landed_reason(committed: bool, gap: str) -> str:
    """A landed run's reason, with the coverage gap appended when there is one."""
    if not gap:
        return _clean_reason(committed)
    return f"{_clean_reason(committed)}; {gap}"


def _read_state(report: dict, unpushed: int) -> str:
    """The state a read-only answer supports.

    The coverage gap outranks pending work on purpose: it is the standing
    condition, and the pending counts are reported alongside whatever state this
    returns, so nothing is lost by naming the gap first.
    """
    if _hard_blockers(report) or report.get("tracked_excluded"):
        return STATE_NEEDS_ATTENTION
    if report.get("eligible") or unpushed:
        return STATE_PENDING
    return STATE_READY


def _read_reason(report: dict, unpushed: int) -> str:
    """Why a read-only status is not simply ``ready``."""
    blocked = _hard_blockers(report)
    if blocked:
        return "; ".join(blocked)
    if report.get("tracked_excluded"):
        return _coverage_gap(list(report["tracked_excluded"]))
    if report.get("eligible") or unpushed:
        pending = []
        if report.get("eligible"):
            pending.append(f"{len(report['eligible'])} file(s) to commit")
        if unpushed:
            pending.append(f"{unpushed} commit(s) not yet on origin")
        return "waiting: " + ", ".join(pending)
    return "up to date"
