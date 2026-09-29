"""Tests for ciao/backup_service.py: the unattended five-minute backup.

The service replaced a push-only 30s loop, so the tests are written as the
properties that loop did not have, each of which is a way to lose a user's data
or to claim a backup that did not happen:

- it commits the durable-data scope, so a new/modified/**deleted** note reaches
  the remote, and a file outside the scope does not;
- a clean run creates nothing — no empty commit, and no push either;
- commits that never reached the remote are uploaded even with a clean tree;
- a failed push keeps the local commit and reports the truth, and only a landed
  push updates the success evidence;
- readiness is re-evaluated per tick, so a repository configured later becomes
  ready without a restart, and a pause survives a restart;
- the cadence is five minutes, overdue-aware at startup, and backs off boundedly
  for a failure that cannot self-heal;
- a Mac that is not the host never runs it, and a manual run and a scheduled
  tick cannot overlap.

Everything runs on temporary directories and a local bare remote: no real user
vault, no network, and no real repository is ever written to. The clock is
injected, so the cadence assertions are about what the service asked for rather
than about wall-clock time.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from logging import LogRecord
from pathlib import Path

import pytest

from ciao import backup_scope, backup_service, job_runs, local_session
from ciao.app_settings import AppSettingsStore
from ciao.backup_service import (
    BACKOFF_MULTIPLIER,
    BACKUP_INTERVAL_S,
    BackupService,
    sanitize_remote,
)
from ciao.config import CiaoConfig, reset_reroot_cache
from ciao.git_proc import GIT_TIMEOUT_DETAIL
from ciao.legacy_node_state import LegacyNodeState
from ciao.workspace_reroot import mark_born_per_root


# ── a temporary install ──────────────────────────────────────────────────────


def _git(repo: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
        "HOME": str(repo),
    }
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


class _World:
    """One install: a data repository, optionally with an origin, plus config."""

    def __init__(self, workspace: Path, config: CiaoConfig, remote: Path | None) -> None:
        self.workspace = workspace
        self.config = config
        self.remote = remote

    def note(self, name: str, text: str = "hi\n") -> Path:
        return _write(self.workspace / "memory-vault" / "Notes" / name, text)

    def head(self) -> str:
        return _git(self.workspace, "rev-parse", "HEAD")

    def commit_paths(self, ref: str = "HEAD") -> list[str]:
        out = _git(self.workspace, "show", "--name-status", "--format=", ref)
        return [line.split("\t", 1)[-1] for line in out.splitlines() if line.strip()]

    def remote_head(self) -> str:
        """What the remote's ``main`` points at, or "" when it has no branch."""
        if self.remote is None:
            return ""
        out = subprocess.run(
            ["git", "rev-parse", "refs/heads/main"], cwd=str(self.remote),
            capture_output=True, text=True,
        )
        return out.stdout.strip() if out.returncode == 0 else ""


def _world(tmp_path: Path, *, with_remote: bool = True) -> _World:
    """An install whose durable data lives in its own git repository.

    The seed commit is pushed up front when there is a remote, so a test that
    cares about what the *service* does starts from an already-backed-up
    repository rather than from the first push of the initial commit.
    """
    workspace = tmp_path / "install"
    _write(workspace / "memory-vault" / "Notes" / "day-1.md", "day one\n")
    (workspace / ".runtime").mkdir()
    _git(workspace, "init", "-q", "-b", "main")
    _git(workspace, "config", "user.name", "T")
    _git(workspace, "config", "user.email", "t@e.com")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-q", "-m", "seed")

    remote: Path | None = None
    if with_remote:
        remote = tmp_path / "origin.git"
        _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
        _git(workspace, "remote", "add", "origin", str(remote))
        _git(workspace, "push", "-q", "-u", "origin", "main")

    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=workspace,
        state_path=workspace / ".runtime" / "state.json",
        media_root=workspace / ".runtime" / "media",
        vault_root=workspace / "memory-vault",
    )
    return _World(workspace, config, remote)


class _Clock:
    """A controlled clock: the service asks it for the time, the fake sleep moves it."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 9, 28, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _service(
    world: _World,
    *,
    clock: _Clock | None = None,
    kind: str = "host",
    store: AppSettingsStore | None = None,
) -> BackupService:
    """A service over the world's install, as ``main`` builds one.

    ``kind`` is the boot's legacy node-state verdict; a service can only be
    built with one, because whether this Mac may write at all is never a
    question the service is allowed to answer for itself.
    """
    resolved = store or AppSettingsStore(world.config.state_path.parent / "app_settings.json")
    return BackupService(
        world.config,
        resolved,
        node_state=lambda: LegacyNodeState(kind=kind),
        now=clock,
    )


async def _drive(
    service: BackupService,
    clock: _Clock,
    *,
    ticks: int,
    before_tick: Callable[[int], None] | None = None,
) -> tuple[list[float], list[backup_service.BackupStatus]]:
    """Run the loop on the controlled clock for ``ticks`` sleeps' worth of work.

    The fake sleep records what the service asked to wait for and moves the
    clock by exactly that much, so the delays are the service's own decision
    rather than a wall-clock race. The stop event is set from inside the sleep,
    which is also the only proof that the loop wakes on it rather than sitting
    out the interval. ``before_tick`` runs before every tick but the first, so a
    test can give each later tick something to do.
    """
    stop = asyncio.Event()
    requested: list[float] = []
    statuses: list[backup_service.BackupStatus] = []
    real_run = service.run_backup

    async def recording_run(*, source: str = "manual") -> backup_service.BackupStatus:
        if before_tick is not None and statuses:
            before_tick(len(statuses))
        status = await real_run(source=source)
        statuses.append(status)
        return status

    async def fake_sleep(delay: float) -> None:
        requested.append(delay)
        clock.advance(delay)
        if len(requested) >= ticks:
            stop.set()
        await asyncio.sleep(0)

    service.run_backup = recording_run  # type: ignore[method-assign]
    await service.backup_loop(stop=stop, sleep=fake_sleep)
    return requested, statuses


#: How many runs the refused-tick test lets a loop make before it stops it. A
#: loop that backs off makes this many runs and this many waits; a loop that
#: spins makes them back to back with no wait between, which the assertions turn
#: into a failure instead of a hung suite.
_SPIN_RUN_BUDGET = 3


# ── a fault on demand ────────────────────────────────────────────────────────


def _time_out_nth_push(monkeypatch, *, n: int) -> list[str]:
    """Make the ``n``-th ``git push`` of a test time out, and count the pushes.

    A push that times out is the shape an unreachable remote takes, and
    ``local_session.backoff_reason`` recognises that one string — the honest way
    to test the offline path without waiting out ``GIT_NETWORK_TIMEOUT`` or
    pointing a test at a host that is not there.
    """
    real = local_session.run_git
    seen: list[str] = []

    async def fake_run_git(workspace, *args, timeout=None):
        if args[:1] == ("push",):
            seen.append("push")
            if len(seen) == n:
                return -1, "", GIT_TIMEOUT_DETAIL
        return await real(workspace, *args, timeout=timeout)

    monkeypatch.setattr(local_session, "run_git", fake_run_git)
    return seen


# ── committing the scope ─────────────────────────────────────────────────────


async def test_a_new_note_is_committed_and_pushed(tmp_path: Path) -> None:
    world = _world(tmp_path)
    service = _service(world)
    world.note("day-2.md", "day two\n")

    status = await service.run_backup()

    assert status.state == backup_service.STATE_READY, status.reason
    assert status.branch == "main"
    assert status.pending_changes == 0
    assert world.commit_paths() == ["memory-vault/Notes/day-2.md"]
    assert world.remote_head() == world.head()
    assert status.last_success_commit == _git(
        world.workspace, "rev-parse", "--short=12", "HEAD"
    )


async def test_a_modified_note_is_committed_and_pushed(tmp_path: Path) -> None:
    world = _world(tmp_path)
    service = _service(world)
    world.note("day-1.md", "edited\n")

    status = await service.run_backup()

    assert status.state == backup_service.STATE_READY, status.reason
    assert world.head() == world.remote_head()


async def test_a_deleted_note_is_committed_as_a_deletion(tmp_path: Path) -> None:
    """A deletion is durable work: not committing it means the next checkout
    restores a note the owner removed."""
    world = _world(tmp_path)
    service = _service(world)
    (world.workspace / "memory-vault" / "Notes" / "day-1.md").unlink()

    status = await service.run_backup()

    assert status.state == backup_service.STATE_READY, status.reason
    assert _git(world.workspace, "show", "--name-status", "--format=", "HEAD").split() == [
        "D",
        "memory-vault/Notes/day-1.md",
    ]
    assert world.head() == world.remote_head()


async def test_a_change_outside_the_scope_is_neither_committed_nor_pushed(
    tmp_path: Path,
) -> None:
    """The scope is the whole point: a repository that is also a checkout must
    not have its app source swept into an unattended commit."""
    world = _world(tmp_path)
    _write(world.workspace / "app.py", "# app\n")
    _git(world.workspace, "add", "-A")
    _git(world.workspace, "commit", "-q", "-m", "app source")
    service = _service(world)
    _write(world.workspace / "app.py", "# app, edited\n")
    world.note("day-2.md", "day two\n")

    status = await service.run_backup()

    # The note is still committed — the gap does not stop the scoped work.
    assert "memory-vault/Notes/day-2.md" in world.commit_paths()
    assert world.commit_paths() == ["memory-vault/Notes/day-2.md"]
    # And the gap is reported rather than swallowed: a repository holding
    # application source is only ever partly covered by this service. It is
    # reported on its own field, not as a state — a permanent property of the
    # repository is not a failure of this run, and a `needs_attention` here
    # wrote a permanently red Automation row for a backup that had just done
    # its job (#733).
    assert status.state == backup_service.STATE_READY, status.reason
    assert status.coverage_gap == 1
    # The human sentence still names the paths, under the ready state, for
    # whoever opens the details.
    assert "outside the backup scope" in status.reason
    assert "app.py" in status.reason


async def test_a_credential_in_the_scope_stops_the_run_before_it_stages(
    tmp_path: Path,
) -> None:
    world = _world(tmp_path)
    service = _service(world)
    world.note("secrets.md", "sk-" + "a" * 48 + "\n")
    before = world.head()

    status = await service.run_backup()

    assert status.state == backup_service.STATE_NEEDS_ATTENTION
    assert "OpenAI API key" in status.reason
    # Nothing was staged, committed or pushed, and the file is still there.
    assert world.head() == before
    assert world.remote_head() == before
    assert _git(world.workspace, "status", "--porcelain") != ""
    assert status.last_success_at == ""


# ── the automation store inside the scope (#734) ─────────────────────────────
#
# `.runtime/schedules.json` is the one file carved out of a refused directory:
# it is the durable store every automation is read from and written back to,
# prompts included, and the product is built to restore it from git. A carve-out
# of a directory that exists because it holds credentials is only defensible if
# it does not become a way to commit one, so both halves are pinned here — the
# run that commits the store, and the run a token in a schedule prompt stops.


def _schedules(world: _World, payload: str) -> Path:
    return _write(world.workspace / ".runtime" / "schedules.json", payload)


async def test_the_automation_store_is_committed_and_pushed(tmp_path: Path) -> None:
    world = _world(tmp_path)
    service = _service(world)
    _schedules(world, json.dumps({"schedules": [{"name": "weekly", "prompt": "summarise"}]}))

    status = await service.run_backup()

    assert status.state == backup_service.STATE_READY, status.reason
    assert world.commit_paths() == [".runtime/schedules.json"]
    assert world.remote_head() == world.head()


async def test_a_credential_in_the_automation_store_stops_the_run_before_it_stages(
    tmp_path: Path,
) -> None:
    """The check the carve-out has to pass, and the reason it is safe.

    A schedule prompt is free text an operator or an agent writes, and a header
    pasted into one looks exactly like a note that happens to contain a token.
    The preflight scans every eligible path rather than the durable trees, so
    being eligible is what subjects the store to the scan, not an exemption
    from it — and the run stops before it stages anything, so the token is
    never written into a commit that the next `git add` could carry to the
    remote.
    """
    world = _world(tmp_path)
    service = _service(world)
    _schedules(
        world,
        json.dumps(
            {"schedules": [{"name": "weekly", "prompt": "use sk-" + "a" * 48 + " please"}]}
        ),
    )
    before = world.head()

    status = await service.run_backup()

    assert status.state == backup_service.STATE_NEEDS_ATTENTION
    assert "OpenAI API key" in status.reason
    assert world.head() == before
    assert world.remote_head() == before
    assert _git(world.workspace, "status", "--porcelain") != ""
    assert status.last_success_at == ""


async def test_the_rest_of_the_runtime_root_is_still_refused(tmp_path: Path) -> None:
    """The carve-out is one file, and the preflight report is where that shows:
    the other `.runtime` paths are reported as pending work this run will not
    commit, beside the one it will."""
    world = _world(tmp_path)
    service = _service(world)
    _schedules(world, json.dumps({"schedules": []}))
    _write(world.workspace / ".runtime" / "custom_providers.json", "{}")
    _write(world.workspace / ".runtime" / "node_state.json", "{}")

    report = await local_session.preflight_scoped(world.config, world.workspace)

    assert report["eligible"] == [".runtime/schedules.json"]
    assert sorted(report["excluded"]) == [
        ".runtime/custom_providers.json",
        ".runtime/node_state.json",
    ]
    assert report["ok"] is True


async def test_a_data_repo_that_ignores_the_runtime_root_does_not_back_it_up(
    tmp_path: Path,
) -> None:
    """What the carve-out does *not* reach, pinned so the limit is a fact.

    ``ciao setup`` writes ``.runtime/`` into a fresh workspace's ``.gitignore``
    (``cli._WORKSPACE_GITIGNORE_ENTRIES``), and a repository that ignores a
    file never reports it: ``git status --porcelain`` does not list it, so the
    preflight never sees it at all. On such an install the carve-out changes
    nothing — the automations are still not backed up, and unlike an
    ignored-but-pending path there is not even a coverage gap to report,
    because there is no pending work to report as uncovered.

    The remedy belongs to the operator's own ignore rules (negate
    ``.runtime/schedules.json`` back out, as the issue's reporter has) rather
    than to the scope: the scope says what *may* be committed, and git decides
    what *can* be. Changing the scaffold is a separate decision, because
    ``.gitignore`` also governs the manual sync path's blanket ``git add -A``.
    """
    world = _world(tmp_path)
    _write(world.workspace / ".gitignore", ".runtime/\n")
    _git(world.workspace, "add", "-A")
    _git(world.workspace, "commit", "-q", "-m", "gitignore")
    _schedules(world, json.dumps({"schedules": [{"name": "weekly"}]}))

    # The scope is what changed: the file is eligible, on a repository that
    # never offers it up.
    assert backup_scope.is_eligible(".runtime/schedules.json", world.config) is True
    report = await local_session.preflight_scoped(world.config, world.workspace)
    assert ".runtime/schedules.json" not in report["eligible"]
    assert _git(world.workspace, "status", "--porcelain", "--ignored").splitlines() == [
        "!! .runtime/"
    ]


# ── the coverage gap ─────────────────────────────────────────────────────────
#
# Git already tracks paths the backup scope refuses to commit, so a repository
# that is also a checkout is only ever partly covered. That is a permanent
# property of the repository, not a fault of a run, and it is reported on its
# own field beside the state. It used to be `needs_attention`, which made a
# backup that had just committed and pushed write `status: "error"`, and then
# had every repeat demoted to "same failure as the previous backup attempt" —
# so the true state appeared in telemetry nowhere at all (#733).


def _recorded_runs(tmp_path: Path) -> list[dict]:
    """The job rows this test's runs wrote, oldest first.

    The autouse fixture in ``conftest`` points ``job_runs`` at the test's own
    temp dir, so this is the row an operator reads — and the point of the tests
    below is what a run that succeeded wrote there.
    """
    path = tmp_path / job_runs.JOB_RUNS_NAME
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def test_a_gap_on_two_runs_in_a_row_writes_no_failure_row(tmp_path: Path) -> None:
    """The shape this was reported from: a repository that is also a checkout,
    backed up every five minutes, healthy every time.

    The first run commits a note and pushes it; the second has nothing to do at
    all — the "24 consecutive runs, every one with nothing pending" afternoon.
    Both are successes, and both rows have to say so.
    """
    world = _world(tmp_path)
    _write(world.workspace / "app.py", "# app\n")
    _git(world.workspace, "add", "-A")
    _git(world.workspace, "commit", "-q", "-m", "app source")
    service = _service(world)
    world.note("day-2.md", "day two\n")

    first = await service.run_backup()
    second = await service.run_backup()

    assert [first.state, second.state] == [backup_service.STATE_READY] * 2
    # The second run really is the empty one: nothing left to commit or push.
    assert "nothing to back up" in second.reason
    assert [first.coverage_gap, second.coverage_gap] == [1, 1]
    assert world.head() == world.remote_head()

    rows = _recorded_runs(tmp_path)
    assert [row["status"] for row in rows] == ["ok", "ok"]
    assert [row["error"] for row in rows] == [None, None]
    assert [row["extra"]["state"] for row in rows] == [
        backup_service.STATE_READY
    ] * 2
    # The gap is on the row, so a surface can show a partly covered repository
    # without the row claiming anything broke.
    assert [row["extra"]["coverage_gap"] for row in rows] == [1, 1]
    # And no repeat bookkeeping at all: a gap must not open a "same failure as
    # the previous backup attempt" episode that hides the runs after it.
    assert all("repeat_count" not in row["extra"] for row in rows)
    assert all("skip_reason" not in row["extra"] for row in rows)


async def test_a_gap_on_an_empty_repository_is_still_reported(tmp_path: Path) -> None:
    """Nothing to back up, nothing outstanding, and the backup is reported as
    exactly that — the state the issue called out by its own log line."""
    world = _world(tmp_path)
    _write(world.workspace / "app.py", "# app\n")
    _git(world.workspace, "add", "-A")
    _git(world.workspace, "commit", "-q", "-m", "app source")
    service = _service(world)

    status = await service.run_backup()

    assert status.state == backup_service.STATE_READY, status.reason
    assert status.pending_changes == 0
    assert status.pending_commits == 0
    assert status.coverage_gap == 1


async def test_a_gap_does_not_hide_pending_work_behind_it(tmp_path: Path) -> None:
    """A gap used to outrank everything in a read, so a repository that was both
    behind and partly covered reported the gap and said nothing about the work
    waiting. Pending work leads now, and the gap still travels with it."""
    world = _world(tmp_path)
    _write(world.workspace / "app.py", "# app\n")
    _git(world.workspace, "add", "-A")
    _git(world.workspace, "commit", "-q", "-m", "app source")
    service = _service(world)
    world.note("day-2.md", "day two\n")

    status = await service.status()

    assert status.state == backup_service.STATE_PENDING
    assert status.pending_changes == 1
    assert status.coverage_gap == 1
    assert status.reason.startswith("waiting:")
    # ...and the gap is still said, with the paths, after it.
    assert "outside the backup scope" in status.reason
    assert "app.py" in status.reason


async def test_a_gap_is_logged_once_and_again_only_when_the_count_moves(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The condition is permanent, so a line per run is 288 a day at the
    five-minute cadence, all of it saying nothing new. The first sighting and a
    changed count are the two moments worth a line — and neither is a failure,
    or the failures that share the log with it stop being readable."""
    world = _world(tmp_path)
    _write(world.workspace / "app.py", "# app\n")
    _git(world.workspace, "add", "-A")
    _git(world.workspace, "commit", "-q", "-m", "app source")
    service = _service(world)

    def gap_lines() -> list[LogRecord]:
        return [r for r in caplog.records if "coverage gap" in r.getMessage()]

    with caplog.at_level("INFO", logger="ciao.backup_service"):
        first = await service.run_backup()
        await service.run_backup()
        assert len(gap_lines()) == 1
        assert f"{first.coverage_gap} tracked path(s)" in gap_lines()[0].getMessage()
        # The repository gained more out-of-scope files, so the count moved.
        _write(world.workspace / "main.py", "# main\n")
        _git(world.workspace, "add", "-A")
        _git(world.workspace, "commit", "-q", "-m", "app source, again")
        moved = await service.run_backup()
        await service.run_backup()

    assert [r.levelname for r in gap_lines()] == ["INFO", "INFO"]
    assert moved.coverage_gap > first.coverage_gap
    assert f"{moved.coverage_gap} tracked path(s)" in gap_lines()[-1].getMessage()
    assert [r for r in caplog.records if r.levelname in {"WARNING", "ERROR"}] == []


async def test_a_gap_that_closes_and_returns_at_the_same_count_is_logged_again(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Suppressing the repeat is only honest while the condition holds.

    Remembering the count and nothing else means a gap that went away and came
    back at the same number is a run that never mentions it again — and the run
    that closed it never said so either, so the log claimed a gap was still
    there through the whole stretch where it was not. The remembered count is
    cleared by the run that finds no gap, and the returning one is a new
    sighting.
    """
    world = _world(tmp_path)
    _write(world.workspace / "app.py", "# app\n")
    _git(world.workspace, "add", "app.py")
    _git(world.workspace, "commit", "-q", "-m", "app source")
    service = _service(world)

    def gap_lines() -> list[LogRecord]:
        return [r for r in caplog.records if "coverage gap" in r.getMessage()]

    with caplog.at_level("INFO", logger="ciao.backup_service"):
        first = await service.run_backup()
        assert first.coverage_gap == 1
        assert len(gap_lines()) == 1
        # The out-of-scope file stops being tracked, so the gap closes.
        _git(world.workspace, "rm", "-q", "--cached", "app.py")
        closed = await service.run_backup()
        assert closed.coverage_gap == 0
        # A run with no gap says nothing — there is nothing to report.
        assert len(gap_lines()) == 1
        # And the same file is tracked again, which is a new sighting. It is
        # edited as well as re-added, because re-tracking a file whose content
        # is unchanged is no commit at all.
        _write(world.workspace / "app.py", "# app, back again\n")
        _git(world.workspace, "add", "app.py")
        _git(world.workspace, "commit", "-q", "-m", "app source, again")
        returned = await service.run_backup()
        await service.run_backup()

    assert returned.coverage_gap == first.coverage_gap
    assert [r.levelname for r in gap_lines()] == ["INFO", "INFO"]
    assert f"{returned.coverage_gap} tracked path(s)" in gap_lines()[-1].getMessage()
    # ...and it is still not a line per run: the last run of the stretch is
    # silent.
    assert len(gap_lines()) == 2


# ── clean runs create nothing ────────────────────────────────────────────────


async def test_a_clean_run_creates_no_commit_and_no_push(
    tmp_path: Path, monkeypatch
) -> None:
    world = _world(tmp_path)
    service = _service(world)
    pushes = _time_out_nth_push(monkeypatch, n=1)  # count pushes, never fire
    before = world.head()

    status = await service.run_backup()

    assert status.state == backup_service.STATE_READY, status.reason
    assert "nothing to back up" in status.reason
    assert world.head() == before
    assert pushes == []


async def test_unpushed_commits_are_uploaded_with_a_clean_tree(
    tmp_path: Path,
) -> None:
    """The case a push-only loop was good at: the work is already committed
    (by the user, or by the manual sync) and only the upload is missing."""
    world = _world(tmp_path)
    world.note("day-2.md", "day two\n")
    _git(world.workspace, "add", "-A")
    _git(world.workspace, "commit", "-q", "-m", "by hand")
    hand_made = world.head()
    service = _service(world)

    status = await service.run_backup()

    assert status.state == backup_service.STATE_READY, status.reason
    # No second commit: the service uploaded the one that was already there.
    assert world.head() == hand_made
    assert world.remote_head() == hand_made
    assert "nothing to back up" in status.reason


# ── a push that does not land ────────────────────────────────────────────────


async def test_an_offline_push_keeps_the_local_commit_and_says_so(
    tmp_path: Path, monkeypatch
) -> None:
    world = _world(tmp_path)
    service = _service(world)
    world.note("day-2.md", "day two\n")
    _time_out_nth_push(monkeypatch, n=1)

    status = await service.run_backup()

    assert status.state == backup_service.STATE_OFFLINE
    assert "did not answer" in status.reason
    # The durable work is committed and stays committed — nothing is reset and
    # nothing is thrown away.
    assert "memory-vault/Notes/day-2.md" in world.commit_paths()
    # And no success is claimed for a backup that did not happen.
    assert status.last_success_at == ""
    assert status.last_success_commit == ""
    assert status.pending_commits == 1


async def test_recovery_updates_the_online_success_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    world = _world(tmp_path)
    service = _service(world)
    world.note("day-2.md", "day two\n")
    _time_out_nth_push(monkeypatch, n=1)
    failed = await service.run_backup()
    assert failed.state == backup_service.STATE_OFFLINE
    committed = world.head()

    monkeypatch.undo()
    recovered = await service.run_backup()

    assert recovered.state == backup_service.STATE_READY, recovered.reason
    assert recovered.last_success_at
    assert recovered.last_success_commit == _git(
        world.workspace, "rev-parse", "--short=12", "HEAD"
    )
    assert world.head() == committed  # the offline commit is what went up
    assert world.remote_head() == committed


async def test_a_refused_credential_needs_attention_rather_than_offline(
    tmp_path: Path, monkeypatch
) -> None:
    """`offline` points the fix at the network; a rejected credential does not."""
    world = _world(tmp_path)
    service = _service(world)
    world.note("day-2.md", "day two\n")
    real = local_session.run_git

    async def refuse(workspace, *args, timeout=None):
        if args[:1] == ("push",):
            return 1, "", "fatal: could not read Username for 'https://github.com'"
        return await real(workspace, *args, timeout=timeout)

    monkeypatch.setattr(local_session, "run_git", refuse)

    status = await service.run_backup()

    assert status.state == backup_service.STATE_NEEDS_ATTENTION
    assert "refused the credentials" in status.reason
    assert world.head() != world.remote_head()


# ── readiness, pause, ownership ───────────────────────────────────────────────


async def test_a_repository_configured_later_becomes_ready_without_a_restart(
    tmp_path: Path,
) -> None:
    """The old loop decided once at startup and never asked again, so an install
    that gained a remote while the engine ran never backed up anything."""
    world = _world(tmp_path, with_remote=False)
    clock = _Clock()
    service = _service(world, clock=clock)
    world.note("day-2.md", "day two\n")

    stop = asyncio.Event()
    requested: list[float] = []
    statuses: list[backup_service.BackupStatus] = []
    real_run = service.run_backup

    async def recording_run(*, source: str = "manual"):
        status = await real_run(source=source)
        statuses.append(status)
        if len(statuses) == 1:
            # The owner adds the remote while the engine is running.
            remote = tmp_path / "origin.git"
            _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
            _git(world.workspace, "remote", "add", "origin", str(remote))
        return status

    async def fake_sleep(delay: float) -> None:
        requested.append(delay)
        clock.advance(delay)
        if len(requested) >= 2:
            stop.set()
        await asyncio.sleep(0)

    service.run_backup = recording_run  # type: ignore[method-assign]
    await service.backup_loop(stop=stop, sleep=fake_sleep)

    assert [s.state for s in statuses] == [
        backup_service.STATE_NOT_CONFIGURED,
        backup_service.STATE_READY,
    ]
    assert "no 'origin' remote" in statuses[0].reason
    # The second tick committed the note and pushed it, with no restart and no
    # other service instance.
    assert world.commit_paths() == ["memory-vault/Notes/day-2.md"]
    assert _git(world.workspace, "rev-parse", "refs/remotes/origin/main") == world.head()


async def test_a_pause_survives_a_restart(tmp_path: Path) -> None:
    world = _world(tmp_path)
    clock = _Clock()
    service = _service(world, clock=clock)
    world.note("day-2.md", "day two\n")
    service.set_flags(paused=True)

    # A restart: a fresh store over the same file, and a fresh service.
    restarted = _service(world, clock=clock)
    status = await restarted.run_backup()

    assert status.state == backup_service.STATE_PAUSED
    assert "paused" in status.reason
    assert status.last_attempt_at == ""  # a paused tick is not an attempt
    # Nothing reached the repository: the tree still holds the unpushed note.
    assert "memory-vault/Notes/day-2.md" in _git(
        world.workspace, "status", "--porcelain"
    )

    # Lifting it runs on the next tick, immediately rather than a full
    # interval later, because a paused tick never stamped an attempt.
    restarted.set_flags(paused=False)
    resumed = await restarted.run_backup()
    assert resumed.state == backup_service.STATE_READY, resumed.reason
    assert world.head() == world.remote_head()


async def test_a_turned_off_service_does_not_run(tmp_path: Path) -> None:
    world = _world(tmp_path)
    service = _service(world)
    world.note("day-2.md", "day two\n")
    service.set_flags(enabled=False)

    status = await service.run_backup()

    assert status.state == backup_service.STATE_PAUSED
    assert "turned off" in status.reason
    assert status.enabled is False
    assert _git(world.workspace, "status", "--porcelain") != ""


async def test_a_client_mac_never_runs_the_backup(tmp_path: Path) -> None:
    """A former client of another host must not become a second writer on the
    same runtime root — the same verdict the scheduler asks."""
    for kind in ("client", "invalid"):
        world = _world(tmp_path / kind)
        service = _service(world, kind=kind)
        world.note("day-2.md", "day two\n")

        status = await service.run_backup()

        assert status.state == backup_service.STATE_PAUSED, kind
        assert "not the host" in status.reason
        assert kind in status.reason
        assert _git(world.workspace, "status", "--porcelain") != ""
        assert world.head() == world.remote_head()


# ── cadence ──────────────────────────────────────────────────────────────────


async def test_the_cadence_is_five_minutes_with_a_bounded_backoff(
    tmp_path: Path, monkeypatch
) -> None:
    world = _world(tmp_path)
    clock = _Clock()
    service = _service(world, clock=clock)
    # The second push of the run times out: the service is up for the first
    # tick, offline for the second, and back on the normal cadence for the
    # third. Each tick after the first gets a fresh note, so each has a commit
    # to make and a push to attempt.
    _time_out_nth_push(monkeypatch, n=2)
    world.note("day-2.md", "day two\n")

    requested, statuses = await _drive(
        service,
        clock,
        ticks=3,
        before_tick=lambda tick: world.note(f"day-{tick + 1}.md", f"day {tick + 1}\n"),
    )

    assert [s.state for s in statuses] == [
        backup_service.STATE_READY,
        backup_service.STATE_OFFLINE,
        backup_service.STATE_READY,
    ]
    # First tick runs at once (nothing had ever attempted), then the interval,
    # then the multiplied interval for the unreachable remote, then the
    # interval again once a push lands.
    assert requested == [
        float(BACKUP_INTERVAL_S),
        float(BACKUP_INTERVAL_S * BACKOFF_MULTIPLIER),
        float(BACKUP_INTERVAL_S),
    ]


async def test_a_backup_that_is_overdue_runs_at_startup(tmp_path: Path) -> None:
    """A machine that was asleep, or an engine that was restarted, backs up
    immediately instead of waiting out a full interval first."""
    world = _world(tmp_path)
    clock = _Clock()
    store = AppSettingsStore(world.config.state_path.parent / "app_settings.json")
    world.note("day-2.md", "day two\n")

    # A successful run an hour ago, and one that has not run for longer than an
    # interval: the first delay is zero.
    store.update(
        {
            "backup_last_attempt_at": (clock.now - timedelta(hours=1)).isoformat(),
            "backup_last_success_at": (clock.now - timedelta(hours=1)).isoformat(),
        }
    )
    service = _service(world, clock=clock, store=store)
    requested, statuses = await _drive(service, clock, ticks=1)

    assert requested == [float(BACKUP_INTERVAL_S)]
    assert statuses[0].state == backup_service.STATE_READY, statuses[0].reason

    # The same install, ten seconds after its last attempt, waits the rest of
    # the interval — a restart loop must not become a push storm.
    store.update({"backup_last_attempt_at": (clock.now - timedelta(seconds=10)).isoformat()})
    restarted = _service(world, clock=clock, store=store)
    assert restarted._next_delay() == float(BACKUP_INTERVAL_S) - 10  # noqa: SLF001


async def test_an_unexpected_fault_counts_as_an_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that raises still stamps its attempt, so the loop waits an
    interval instead of re-entering at once and forking git in a tight loop."""
    world = _world(tmp_path)
    clock = _Clock()
    store = AppSettingsStore(world.config.state_path.parent / "app_settings.json")
    store.update(
        {"backup_last_attempt_at": (clock.now - timedelta(hours=1)).isoformat()}
    )
    service = _service(world, clock=clock, store=store)

    async def boom(*_args, **_kwargs):
        raise OSError("E2BIG")

    monkeypatch.setattr(service, "_run_locked", boom)
    status = await service.run_backup()

    assert status.state == backup_service.STATE_NEEDS_ATTENTION
    assert service._next_delay() == float(BACKUP_INTERVAL_S)  # noqa: SLF001


async def test_the_loop_stops_when_asked(tmp_path: Path) -> None:
    world = _world(tmp_path)
    clock = _Clock()
    service = _service(world, clock=clock)
    stop = asyncio.Event()

    async def stop_after_the_first_sleep(delay: float) -> None:
        stop.set()
        await asyncio.sleep(0)

    await asyncio.wait_for(
        service.backup_loop(stop=stop, sleep=stop_after_the_first_sleep), timeout=5
    )


@pytest.mark.parametrize(
    "gate",
    [{"enabled": False}, {"paused": True}, {"kind": "client"}],
    ids=["turned-off", "paused", "not-the-host"],
)
async def test_a_refused_tick_waits_a_full_interval_instead_of_spinning(
    tmp_path: Path, gate: dict
) -> None:
    """A boot that will not back up has to idle at the cadence, not spin.

    All three gate shapes answer the same way — a ``paused`` status with no
    attempt stamped — so they are driven as one. The stamp is what
    ``_next_delay`` derives the wait from, so a refused tick leaves nothing to
    wait against and the delay comes back zero; since a refused tick stays
    refused, zero would repeat forever with nothing yielded between the calls.
    That is worse than idling: every one of those iterations costs a blocking
    ``_scope_summary`` read that forks git. The loop therefore owes a refused
    tick a whole interval of its own.
    """
    world = _world(tmp_path)
    clock = _Clock()
    service = _service(world, clock=clock, kind=gate.get("kind", "host"))
    if "enabled" in gate:
        service.set_flags(enabled=bool(gate["enabled"]))
    if "paused" in gate:
        service.set_flags(paused=bool(gate["paused"]))
    world.note("day-2.md", "day two\n")

    stop = asyncio.Event()
    requested: list[float] = []
    statuses: list[backup_service.BackupStatus] = []
    real_run = service.run_backup

    async def recording_run(*, source: str = "manual") -> backup_service.BackupStatus:
        status = await real_run(source=source)
        statuses.append(status)
        # The spin guard, and the reason the budget is on runs as well as on
        # waits: a loop that does not back off never calls sleep, so a
        # wait-bounded driver would let it run until the suite is killed. One
        # run past the budget is what a healthy loop makes too — the budget is
        # reached on a wait, not on a run — so the assertion, not this, is what
        # tells a backing-off loop from a spinning one.
        if len(statuses) > _SPIN_RUN_BUDGET:
            stop.set()
        return status

    async def fake_sleep(delay: float) -> None:
        requested.append(delay)
        clock.advance(delay)
        if len(requested) >= _SPIN_RUN_BUDGET:
            stop.set()
        await asyncio.sleep(0)

    service.run_backup = recording_run  # type: ignore[method-assign]
    await asyncio.wait_for(
        service.backup_loop(stop=stop, sleep=fake_sleep), timeout=60
    )

    assert [s.state for s in statuses] == (
        [backup_service.STATE_PAUSED] * _SPIN_RUN_BUDGET
    )
    # One wait per tick and every one of them a whole interval: no tick is
    # answered faster than the loop slept, so the loop cannot be spinning.
    assert requested == [float(BACKUP_INTERVAL_S)] * _SPIN_RUN_BUDGET
    # Refused, not attempted — which is what keeps the loop honest, and what the
    # next test pins the consequence of.
    assert [s.last_attempt_at for s in statuses] == [""] * _SPIN_RUN_BUDGET
    # And nothing reached the repository while the loop idled.
    assert _git(world.workspace, "status", "--porcelain") != ""
    assert world.head() == world.remote_head()


async def test_a_pause_lifted_while_the_loop_waits_runs_at_once(
    tmp_path: Path,
) -> None:
    """The refused tick's full interval must not become a delay for the first
    real run after the lift — the reason it stamps no attempt, and the trap a
    fix that simply stamped one would fall into."""
    world = _world(tmp_path)
    clock = _Clock()
    service = _service(world, clock=clock)
    world.note("day-2.md", "day two\n")
    service.set_flags(paused=True)

    delays: list[float] = []
    real_run = service.run_backup

    async def recording_run(*, source: str = "manual"):
        delays.append(service._next_delay())  # noqa: SLF001 — the cadence under test
        return await real_run(source=source)

    service.run_backup = recording_run  # type: ignore[method-assign]
    requested, statuses = await asyncio.wait_for(
        _drive(
            service,
            clock,
            ticks=2,
            # The owner lifts the pause between the first tick and the second.
            before_tick=lambda _tick: service.set_flags(paused=False),
        ),
        timeout=15,
    )

    assert [s.state for s in statuses] == [
        backup_service.STATE_PAUSED,
        backup_service.STATE_READY,
    ]
    # Nothing was left of the interval to serve, so the first tick after the lift
    # ran at once instead of waiting out the refused tick's interval as well.
    assert delays == [0.0, 0.0]
    assert requested == [float(BACKUP_INTERVAL_S)] * 2
    # The note was committed and pushed on that tick, not on a later one.
    assert world.commit_paths() == ["memory-vault/Notes/day-2.md"]
    assert world.head() == world.remote_head()


@pytest.mark.parametrize(
    "stamp", ["not a timestamp", "2026-09-28T11:00:00"], ids=["garbage", "no-offset"]
)
def test_an_attempt_stamp_that_cannot_be_read_runs_now_rather_than_raising(
    tmp_path: Path, stamp: str
) -> None:
    """A stamp with no timezone parses fine and is what makes the subtraction
    raise — in ``_next_delay``, outside anything that could report it and inside
    the loop, so one hand-edited file would kill the cadence. An unreadable
    stamp means "no record", and no record means run now."""
    world = _world(tmp_path)
    clock = _Clock()
    store = AppSettingsStore(world.config.state_path.parent / "app_settings.json")
    store.update({"backup_last_attempt_at": stamp})
    service = _service(world, clock=clock, store=store)

    assert service._elapsed_since_attempt() is None  # noqa: SLF001
    assert service._next_delay() == 0.0  # noqa: SLF001


# ── one serialized path ──────────────────────────────────────────────────────


async def test_a_manual_and_a_scheduled_run_cannot_overlap(
    tmp_path: Path, monkeypatch
) -> None:
    """Both go through one lock, so the second run cannot start while the first
    is inside the repository — whichever order they arrive in."""
    world = _world(tmp_path)
    service = _service(world)
    world.note("day-2.md", "day two\n")
    entered: list[str] = []
    reached_push = asyncio.Event()
    release = asyncio.Event()
    real_push = local_session.push_branch

    async def blocking_push(workspace, *, branch):
        entered.append("push")
        reached_push.set()
        # A note that appears while this run is pushing. The first run has
        # already read the tree, so it is the second run's to commit — which is
        # what makes the pair prove serialization rather than just mutual
        # exclusion.
        world.note("day-3.md", "day three\n")
        await release.wait()
        return await real_push(workspace, branch=branch)

    monkeypatch.setattr(local_session, "push_branch", blocking_push)

    first = asyncio.create_task(service.run_backup(source="scheduled"))
    await asyncio.wait_for(reached_push.wait(), timeout=30)
    second = asyncio.create_task(service.run_backup(source="manual"))
    await asyncio.sleep(0.1)

    # The manual run is waiting its turn, not pushing a second time.
    assert entered == ["push"]
    assert not second.done()

    release.set()
    statuses = await asyncio.wait_for(
        asyncio.gather(first, second), timeout=60
    )
    assert [s.state for s in statuses] == [
        backup_service.STATE_READY,
        backup_service.STATE_READY,
    ]
    assert entered == ["push", "push"]  # serialized, never interleaved
    # And the second run's own work went up too, so nothing was dropped between
    # the two.
    assert world.remote_head() == world.head()
    assert "memory-vault/Notes/day-3.md" in world.commit_paths()


async def test_a_status_read_never_mutates_the_repository(
    tmp_path: Path, monkeypatch
) -> None:
    world = _world(tmp_path)
    service = _service(world)
    world.note("day-2.md", "day two\n")
    pushes = _time_out_nth_push(monkeypatch, n=1)
    before = world.head()

    status = await service.status()

    assert status.state == backup_service.STATE_PENDING
    assert status.pending_changes == 1
    assert status.reason.startswith("waiting:")
    assert world.head() == before
    assert pushes == []


# ── what the status says ─────────────────────────────────────────────────────


async def test_the_status_reports_the_scope_and_the_interval(tmp_path: Path) -> None:
    world = _world(tmp_path)
    service = _service(world)

    status = await service.status()

    assert status.state == backup_service.STATE_READY
    assert status.interval_s == 300
    assert status.enabled is True
    # The scope is rendered from the scope itself, so it cannot describe
    # something other than what `commit_scoped` enforces: the durable trees, the
    # workspace guide, the container holding archived agent roots, and the one
    # file carved out of a refused directory (#734).
    assert status.scope == (
        "memory-vault, skills, subagents, commands, .archived-workspaces, AGENTS.md, "
        ".runtime/schedules.json"
    )
    assert status.remote.endswith("origin.git")
    assert status.as_dict()["state"] == backup_service.STATE_READY


async def test_the_scope_says_bare_top_level_roots_are_not_covered(
    tmp_path: Path,
) -> None:
    """#734, decision 1: the base set is unchanged, so the status has to say so.

    On the per-workspace layout the agent assets live one directory per
    workspace, so a bare ``commands/`` or ``skills/`` at the top of the data
    root is refused while ``personal/commands/`` is not. A scope line that names
    the trees without saying that leaves the owner looking for a ``commands/``
    they have and not finding it in a backup.
    """
    workspace = tmp_path / "install"
    (workspace / ".runtime").mkdir(parents=True)
    (workspace / "personal" / "memory-vault").mkdir(parents=True)
    _git(workspace, "init", "-q", "-b", "main")
    mark_born_per_root(workspace, workspace / ".runtime", ["personal"])
    reset_reroot_cache()
    world = _World(
        workspace,
        CiaoConfig(
            pwa_auth_token="t",
            workspace_root=workspace,
            state_path=workspace / ".runtime" / "state.json",
            media_root=workspace / ".runtime" / "media",
            vault_root=workspace / "memory-vault",
        ),
        None,
    )

    status = await _service(world).status()

    assert "not a top-level skills/ or subagents/ or commands/ folder" in status.scope
    # The trees that *are* covered are still named, so the note narrows the
    # scope rather than replacing it.
    assert "memory-vault" in status.scope
    # The default shared-vault layout has the data root itself as an agent root,
    # so a bare `commands/` there is in scope and the note would be a lie.
    assert "not a top-level" not in (await _service(_world(tmp_path / "shared")).status()).scope


def test_a_remote_is_never_reported_with_a_credential_in_it() -> None:
    """A status payload, a log line and a job-run record all copy this string,
    and an https remote routinely carries a token in its userinfo."""
    assert sanitize_remote("https://ghp_TOKEN1234567890@github.com/o/r.git") == (
        "https://github.com/o/r.git"
    )
    assert sanitize_remote("https://user:token@github.com:8443/o/r.git") == (
        "https://github.com:8443/o/r.git"
    )
    assert sanitize_remote("ssh://git@github.com/o/r.git") == (
        "ssh://github.com/o/r.git"
    )
    # The scp-like form keeps the conventional `git` user and nothing else.
    assert sanitize_remote("git@github.com:o/r.git") == "git@github.com:o/r.git"
    assert sanitize_remote("TOKEN@github.com:o/r.git") == "***@github.com:o/r.git"
    # A local path (a test remote, a shared volume) is already credential-free.
    assert sanitize_remote("/srv/git/origin.git") == "/srv/git/origin.git"
    assert sanitize_remote("") == ""


async def test_the_persisted_record_survives_a_new_store(tmp_path: Path) -> None:
    """The store is the durable half of the status, so a restart has to read the
    same evidence back rather than reporting a blank slate."""
    world = _world(tmp_path)
    clock = _Clock()
    service = _service(world, clock=clock)
    world.note("day-2.md", "day two\n")
    await service.run_backup()

    reread = AppSettingsStore(world.config.state_path.parent / "app_settings.json")
    status = await _service(world, clock=clock, store=reread).status()

    assert status.last_attempt_at == clock.now.isoformat()
    assert status.last_success_at == clock.now.isoformat()
    assert status.last_success_commit == _git(
        world.workspace, "rev-parse", "--short=12", "HEAD"
    )


async def test_the_status_says_where_the_last_run_pushed_to(tmp_path: Path) -> None:
    """``backup_remote`` is the record of where the notes went, and it is the
    only one that survives the remote going away: the live ``remote`` is read
    fresh each call and is empty on every boot that refuses or an install with
    no origin, which is when the owner most wants to know the destination."""
    world = _world(tmp_path)
    service = _service(world)
    world.note("day-2.md", "day two\n")

    landed = await service.run_backup()
    assert landed.last_remote == sanitize_remote(str(world.remote))
    assert landed.last_remote == landed.remote

    _git(world.workspace, "remote", "remove", "origin")
    unconfigured = await service.status()
    assert unconfigured.state == backup_service.STATE_NOT_CONFIGURED
    assert unconfigured.remote == ""
    assert unconfigured.last_remote == sanitize_remote(str(world.remote))

    # A record a user edited by hand is sanitized on the way out as well: no
    # credential reaches a payload just because it survived in the file.
    store = AppSettingsStore(world.config.state_path.parent / "app_settings.json")
    store.update({"backup_remote": "https://ghp_TOKEN1234567890@github.com/o/r.git"})
    edited = await _service(world, store=store).status()
    assert edited.last_remote == "https://github.com/o/r.git"


# ── the setup prompt ────────────────────────────────────────────────────────
#
# One prompt serves two actions (a Settings copy button and the in-app
# dispatch), and it is the only place a user with no Git knowledge is told
# what the backup needs. So the properties under test are the ones whose
# failure is silent: the folder and the scope are the real ones from this
# install, a folder with spaces stays one path, and no credential travels.


def _world_in(
    directory: Path,
    *,
    with_remote: bool = True,
    vault_is_data_root: bool = False,
) -> _World:
    """An install rooted at ``directory``, which may contain spaces.

    ``vault_is_data_root`` is the documented existing-folder install
    (``CIAO_VAULT_ROOT=.``): the data root *is* the vault, so its workspaces sit
    directly under the folder and nothing inside it is merely one durable tree.
    """
    directory.mkdir(parents=True, exist_ok=True)
    vault = directory if vault_is_data_root else directory / "memory-vault"
    # An existing-folder install keeps its workspaces directly under the vault.
    workspace = vault / "personal" if vault_is_data_root else vault
    _write(workspace / "Notes" / "day-1.md", "day one\n")
    (directory / ".runtime").mkdir()
    _git(directory, "init", "-q", "-b", "main")
    _git(directory, "config", "user.name", "T")
    _git(directory, "config", "user.email", "t@e.com")
    _git(directory, "add", "-A")
    _git(directory, "commit", "-q", "-m", "seed")
    remote: Path | None = None
    if with_remote:
        remote = directory.parent / "origin.git"
        _git(directory.parent, "init", "-q", "--bare", "-b", "main", str(remote))
        _git(directory, "remote", "add", "origin", str(remote))
        _git(directory, "push", "-q", "-u", "origin", "main")
    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=directory,
        state_path=directory / ".runtime" / "state.json",
        media_root=directory / ".runtime" / "media",
        vault_root=vault,
    )
    return _World(directory, config, remote)


def test_the_setup_prompt_names_this_install(tmp_path: Path) -> None:
    """The absolute folder and the scope have to be the real ones, or the agent
    sets up a backup of a directory nobody uses."""
    world = _world_in(tmp_path / "ciao install")

    context = backup_service.setup_context(world.config)
    prompt = backup_service.render_setup_prompt(world.config, context)

    assert context["folder"] == str(world.workspace.resolve())
    assert f'"{context["folder"]}"' in prompt
    assert "memory-vault" in context["scope"]
    assert "memory-vault" in prompt
    assert context["branch"] == "main"
    assert "main" in prompt
    # The scope is rendered from the scope itself, so the prompt names the
    # exact paths the unattended commit would take.
    assert "memory-vault/" in prompt
    # ...including the one file carved out of a refused directory, whose
    # matching "never backed up" entry is a glob rather than the bare directory
    # (#734). A prompt that said the whole of `.runtime` was excluded while
    # listing a file inside it as in scope would send the agent the other way.
    assert ".runtime/schedules.json" in prompt
    never = next(
        line for line in prompt.splitlines() if line.startswith("  never backed up:")
    )
    assert ".runtime/*," in never
    assert ".runtime/," not in never


def test_a_folder_with_spaces_stays_one_path(tmp_path: Path) -> None:
    """A data folder with a space in it is the ordinary case on macOS, and a
    prompt that printed it bare would have the agent's first ``cd`` land
    nowhere. The path is quoted, and the spaces are not escaped away."""
    world = _world_in(tmp_path / "My Ciao Install")

    context = backup_service.setup_context(world.config)
    prompt = backup_service.render_setup_prompt(world.config, context)

    assert "My Ciao Install" in context["folder"]
    # Quoted, so a reader pasting it into a shell keeps it whole...
    assert f'"{context["folder"]}"' in prompt
    # ...and never backslash-escaped, which would be a path that does not exist.
    assert "My\\ Ciao\\ Install" not in prompt
    # The instruction to quote is in the prompt itself, so the requirement
    # travels with the path rather than living only in a test.
    assert "spaces" in prompt


def test_the_setup_prompt_carries_no_credential(tmp_path: Path) -> None:
    """An ``origin`` routinely has a token in it. The prompt is copied to a
    clipboard and dispatched into a chat, so a token reaching either is a
    credential in two more places than it started in."""
    world = _world(tmp_path)
    _git(
        world.workspace,
        "remote",
        "set-url",
        "origin",
        "https://ghp_SECRETTOKEN1234567890@github.com/me/notes.git",
    )

    context = backup_service.setup_context(world.config)
    prompt = backup_service.render_setup_prompt(world.config, context)

    assert "ghp_SECRETTOKEN1234567890" not in prompt
    assert "ghp_SECRETTOKEN1234567890" not in str(context)
    # ...and the sanitized URL is there, so the agent knows where to look.
    assert "https://github.com/me/notes.git" in prompt


def test_the_prompt_is_the_same_bytes_for_both_actions(tmp_path: Path) -> None:
    """The copy action and the in-app dispatch must not be able to disagree:
    they are one template, and this is the assertion that keeps them one.

    "The same bytes" means one *template* rendered from the same trusted facts,
    not one shared render: the two routes each read their own
    ``setup_context`` (a page load and a click are different moments, and
    neither should report a repository state another request saw minutes ago),
    so the invariant worth testing is that no second template exists — the two
    calls below differ in nothing but which line calls ``setup_context``.
    """
    world = _world(tmp_path)

    copy_text = backup_service.render_setup_prompt(
        world.config, backup_service.setup_context(world.config)
    )
    # The dispatch renders from a context the caller already holds; the copy
    # path is the same call with the context filled in for it.
    dispatch_text = backup_service.render_setup_prompt(
        world.config, backup_service.setup_context(world.config)
    )

    assert copy_text == dispatch_text
    # A context is an optimization, not a second source of truth: rendering
    # with no context at all produces the same text.
    assert backup_service.render_setup_prompt(world.config) == copy_text


async def test_the_scope_is_the_whole_folder_when_the_vault_is_the_data_root(
    tmp_path: Path,
) -> None:
    """The documented existing-folder install (``CIAO_VAULT_ROOT=.``) has no
    durable tree *inside* the data root: the folder is the vault, so every file
    in it is backed up.

    Its scope prefixes still carry the archived-workspace base, so the shape has
    to be recognised by the presence of the ``./`` sentinel. Compared as a whole
    tuple it reads as "only some trees", and the sentinel is then rendered as a
    bare ``.`` among them — one more name in a list the folder already contains,
    and a prompt telling the agent the rest of the folder is not backed up.
    """
    world = _world_in(tmp_path / "existing notes", vault_is_data_root=True)

    context = backup_service.setup_context(world.config)
    prompt = backup_service.render_setup_prompt(world.config, context)

    assert "./" in context["scope_paths"]
    assert context["scope"] == (
        "every file in the folder (this folder is the memory vault itself)"
    )
    assert f"backed up:              {context['scope']}" in prompt
    assert "only the durable trees inside it" not in prompt
    # The sentinel is a fact about the scope, not a directory in it, so it is
    # never rendered as a name beside the durable trees.
    assert not [line for line in prompt.splitlines() if "." in line.split()]
    # The status renders the same scope from the same prefixes, so an install
    # that backs up everything does not report some of it.
    assert (await _service(world).status()).scope == "the whole data root"


def test_the_prompt_asks_for_identity_and_remote_access_to_be_verified(
    tmp_path: Path,
) -> None:
    """The two failures that stall a guided setup are both quiet: a repository
    with no ``user.name``/``user.email`` refuses its first commit with a message
    that does not name the cause, and a remote this machine cannot authenticate
    to refuses the push the same way. The prompt asks for both checks before the
    push, and still forbids a credential from going anywhere — verifying means
    reading configuration, not revealing it.
    """
    world = _world_in(tmp_path / "install")

    prompt = backup_service.render_setup_prompt(world.config)

    assert "git config user.name" in prompt
    assert "git config user.email" in prompt
    # The remote check is a round trip in its own right, asked for before the
    # push rather than inferred from the push's own output.
    assert "git ls-remote" in prompt
    assert "gh auth status" in prompt
    # ...and the prohibition survives next to the instruction to read that
    # configuration.
    assert "never paste a token" in prompt
    assert "never print, copy, or quote a secret" in prompt


def test_the_context_reports_an_install_that_is_not_configured_yet(
    tmp_path: Path,
) -> None:
    """The whole reason the prompt exists is the unconfigured install, so the
    context has to describe that case in the same shape as a ready one — a
    missing remote reads as an empty fact, not as an error."""
    world = _world_in(tmp_path / "install", with_remote=False)

    context = backup_service.setup_context(world.config)

    assert context["has_repo"] is True
    assert context["parent_repo"] is False
    assert context["has_remote"] is False
    assert context["remote"] == ""
    prompt = backup_service.render_setup_prompt(world.config, context)
    assert "none yet" in prompt
    assert "not a git repository yet" not in prompt


def test_the_context_names_a_data_root_inside_a_parent_repository(
    tmp_path: Path,
) -> None:
    """A data root that is a subdirectory of a larger checkout is the one shape
    where adding a remote here would push far more than the notes. The prompt
    has to say so rather than leaving the agent to discover it at push time."""
    outer = tmp_path / "outer"
    _write(outer / "app.py", "print()\n")
    _git(outer, "init", "-q", "-b", "main")
    _git(outer, "config", "user.name", "T")
    _git(outer, "config", "user.email", "t@e.com")
    _git(outer, "add", "-A")
    _git(outer, "commit", "-q", "-m", "seed")
    # A vault that is not a directory makes `sync_root` fall back to the
    # workspace root — so the data root is `outer/workspace`, a subdirectory of
    # the checkout, which is exactly the parent-repository case.
    workspace = outer / "workspace"
    _write(workspace / "memory-vault" / "note.md", "hello\n")
    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=workspace,
        state_path=workspace / ".runtime" / "state.json",
        media_root=workspace / ".runtime" / "media",
        vault_root=workspace / "not-created-yet",
    )

    context = backup_service.setup_context(config)
    prompt = backup_service.render_setup_prompt(config, context)

    assert context["folder"] == str(workspace.resolve())
    assert context["has_repo"] is True
    assert context["parent_repo"] is True
    assert context["repo_root"] == str(outer.resolve())
    # The prompt names the enclosing repository by its actual path, so the
    # reader can see what else a remote added here would take with it.
    assert f'inside a repository rooted at "{context["repo_root"]}"' in prompt


async def test_a_guided_setup_turns_the_backup_on_once_it_verifies(
    tmp_path: Path,
) -> None:
    """A guided setup is the owner answering a question, so the answer has to
    stick: an install set up through the in-app flow should start backing up,
    not sit configured with the switch off.

    And it is answered at verification, not at dispatch — dispatching arms the
    answer, reading a real repository settles it.
    """
    world = _world(tmp_path)
    _git(world.workspace, "remote", "remove", "origin")
    service = _service(world)
    service.set_flags(enabled=False)

    service.begin_guided_setup()
    # Dispatch alone changes nothing: the setup has not produced a remote yet.
    assert service._store.settings.backup_enabled is False
    unconfigured = await service.status()
    assert unconfigured.state == backup_service.STATE_NOT_CONFIGURED
    assert service._store.settings.backup_enabled is False

    # The agent's work lands: the remote is connected and the commit is on it.
    _git(world.workspace, "remote", "add", "origin", str(world.remote))
    _git(world.workspace, "push", "-q", "-u", "origin", "main")
    verified = await service.status()

    assert verified.state == backup_service.STATE_READY, verified.reason
    assert verified.enabled is True
    reread = AppSettingsStore(world.config.state_path.parent / "app_settings.json")
    assert reread.settings.backup_enabled is True


async def test_a_settled_guided_setup_does_not_latch(tmp_path: Path) -> None:
    """The arming is process state, not a store. If it latched, a later turn-off
    would be undone by the next status read, which is not a thing the owner
    asked for."""
    world = _world(tmp_path)
    service = _service(world)
    service.begin_guided_setup()
    await service.status()

    service.set_flags(enabled=False)
    await service.status()

    assert service._store.settings.backup_enabled is False


async def test_a_status_read_settles_the_setup_without_touching_the_repository(
    tmp_path: Path,
) -> None:
    """``status`` is what a Settings page polls, so it may write a flag (a
    guided setup settling) but must never commit or push. A status call that
    moved the repository would be a surprising way to read a page."""
    world = _world(tmp_path)
    service = _service(world)
    service.set_flags(enabled=False)
    service.begin_guided_setup()
    world.note("day-2.md", "day two\n")
    head_before = world.head()

    await service.status()

    # The settings flag moved; the repository did not.
    assert service._store.settings.backup_enabled is True
    assert world.head() == head_before
    assert "?? memory-vault/Notes/day-2.md" in _git(
        world.workspace, "status", "--porcelain"
    )


async def test_a_guided_setup_never_unpauses_a_paused_backup(tmp_path: Path) -> None:
    """The pause is the owner's own hold. Reading "set up my backup" as "un-pause
    the backup I paused" is the surprise an unattended service must not spring,
    so this is the flag the guided setup refuses to touch — and it is re-read at
    verification, so a pause taken *during* the setup is honoured too."""
    world = _world(tmp_path)
    service = _service(world)
    service.set_flags(enabled=False, paused=True)
    service.begin_guided_setup()

    await service.status()

    assert service._store.settings.backup_paused is True
    assert service._store.settings.backup_enabled is False


async def test_a_pause_taken_during_a_setup_is_respected(tmp_path: Path) -> None:
    """The pause is read when the answer is owed, not when the question was
    asked, so a pause that lands while the agent works is the one that counts."""
    world = _world(tmp_path)
    _git(world.workspace, "remote", "remove", "origin")
    service = _service(world)
    service.set_flags(enabled=False)
    service.begin_guided_setup()

    # The owner pauses while the setup chat is still running.
    service.set_flags(paused=True)
    _git(world.workspace, "remote", "add", "origin", str(world.remote))
    status = await service.status()

    assert status.state == backup_service.STATE_PAUSED
    assert status.reason == "backup is paused"
    assert service._store.settings.backup_enabled is False
