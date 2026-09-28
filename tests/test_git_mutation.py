"""Tests for ciao/git_mutation.py: one mutation lock per repository.

The failure being pinned down is concurrency, not any single git command. Three
Ciaobot writers share a checkout — manual sync, the post-conflict resync, and
the 30s background backup push — and before this module they could interleave
git commands, producing commits nobody staged, pushes of the wrong branch, and
"unable to create index.lock" errors nobody could explain. These tests use
throwaway repositories under tmp_path and an explicit event-loop handshake, so
the interleaving is asserted rather than hoped for: no sleep-based timing.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from ciao.git_mutation import (
    RepositoryBusyError,
    active_repositories,
    canonical_repository,
    ensure_mutable,
    repository_mutation,
    reset_mutation_locks,
)


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


def _repo(path: Path, *, branch: str = "main") -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", branch)
    _git(path, "config", "user.name", "T")
    _git(path, "config", "user.email", "t@e.com")
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "seed")
    return path


@pytest.fixture(autouse=True)
def _fresh_registry() -> None:
    """The registry is process-wide; a lock left by a failing test must not
    leak into the next one."""
    reset_mutation_locks()
    yield
    reset_mutation_locks()


# ── keying ───────────────────────────────────────────────────────────────────


def test_canonical_repository_collapses_aliases_of_one_checkout(tmp_path: Path) -> None:
    """A subdirectory of a checkout is not a second repository.

    The backup loop holds the sync root while a manual sync may hold a vault
    subdirectory; keying by the caller's path instead of the top level would let
    those two race.
    """
    repo = _repo(tmp_path / "repo")
    nested = repo / "memory-vault" / "personal"
    nested.mkdir(parents=True)

    assert canonical_repository(nested) == canonical_repository(repo) == repo.resolve()


def test_canonical_repository_of_a_plain_folder_is_its_resolved_path(tmp_path: Path) -> None:
    """A fresh install's workspace is not a checkout; it still needs a key."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert canonical_repository(plain) == plain.resolve()


def test_repository_mutation_yields_the_canonical_root(tmp_path: Path) -> None:
    """A caller holding a subdirectory commits against the owning checkout."""
    repo = _repo(tmp_path / "repo")
    nested = repo / "memory-vault"
    nested.mkdir()

    async def run() -> Path:
        async with repository_mutation(nested) as root:
            return root

    assert asyncio.run(run()) == repo.resolve()


# ── serialization ────────────────────────────────────────────────────────────


async def test_same_repository_mutations_are_serialized(tmp_path: Path) -> None:
    """Two writers on one checkout must not overlap.

    The first writer parks on an event *while holding* the lock, and the test
    body asserts the second writer has not entered before releasing it. That
    ordering is what makes this a serialization test rather than one that
    happens to pass when the scheduler is slow.
    """
    repo = _repo(tmp_path / "repo")
    held = asyncio.Event()
    release = asyncio.Event()
    second_entered = False
    overlapped = False

    async def first() -> None:
        nonlocal overlapped
        async with repository_mutation(repo):
            held.set()
            await release.wait()
            # Yield so a wrongly-parallel second writer would get a turn here.
            await asyncio.sleep(0)
            overlapped = second_entered

    async def second() -> None:
        nonlocal second_entered
        async with repository_mutation(repo):
            second_entered = True

    first_task = asyncio.create_task(first())
    await held.wait()
    second_task = asyncio.create_task(second())
    # Give the second writer every chance to barge in.
    for _ in range(5):
        await asyncio.sleep(0)
    assert not second_entered, "second writer entered while the first held the lock"
    release.set()
    await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=5)

    assert not overlapped
    assert second_entered


async def test_subdirectory_alias_serializes_with_the_repository_root(tmp_path: Path) -> None:
    """The same checkout reached two ways is still one lock."""
    repo = _repo(tmp_path / "repo")
    alias = repo / "memory-vault"
    alias.mkdir()
    held = asyncio.Event()
    release = asyncio.Event()
    alias_entered = False

    async def by_root() -> None:
        async with repository_mutation(repo):
            held.set()
            await release.wait()

    async def by_alias() -> None:
        nonlocal alias_entered
        async with repository_mutation(alias):
            alias_entered = True

    first_task = asyncio.create_task(by_root())
    await held.wait()
    alias_task = asyncio.create_task(by_alias())
    for _ in range(5):
        await asyncio.sleep(0)
    assert not alias_entered
    release.set()
    await asyncio.wait_for(asyncio.gather(first_task, alias_task), timeout=5)
    assert alias_entered


async def test_distinct_repositories_can_run_concurrently(tmp_path: Path) -> None:
    """A slow push from one checkout must not delay a sync of another.

    The backup loop and a manual sync usually target the same repository, but a
    workspace whose vault lives in its own repo plus a separate install both
    sync on the same host; a global lock would serialize them for no reason.
    """
    first = _repo(tmp_path / "one")
    second = _repo(tmp_path / "two")
    held = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        async with repository_mutation(first):
            held.set()
            await release.wait()

    async def other() -> None:
        async with repository_mutation(second):
            pass

    hold_task = asyncio.create_task(hold())
    await held.wait()
    # Reaches here only if `second` did not wait for the first to let go.
    await asyncio.wait_for(other(), timeout=5)
    release.set()
    await hold_task


# ── reentrancy vs. child tasks ───────────────────────────────────────────────


async def test_nested_same_task_mutation_does_not_deadlock(tmp_path: Path) -> None:
    """A public mutation calling a helper that also takes the lock is the normal
    shape of these flows, not an edge case — it must reenter, not wait on
    itself forever."""
    repo = _repo(tmp_path / "repo")
    reached_inner = False

    async def outer() -> None:
        nonlocal reached_inner
        async with repository_mutation(repo):
            async with repository_mutation(repo):
                reached_inner = True

    await asyncio.wait_for(outer(), timeout=5)
    assert reached_inner


async def test_child_task_cannot_bypass_owner_lock(tmp_path: Path) -> None:
    """Reentrancy is by task, not by call stack depth.

    An ``asyncio.create_task`` inside a mutation is a *different* task doing an
    independent mutation, and it must wait — otherwise a helper that fans work
    out to tasks would defeat the lock entirely.
    """
    repo = _repo(tmp_path / "repo")
    held = asyncio.Event()
    child_entered = False
    release = asyncio.Event()

    async def child() -> None:
        nonlocal child_entered
        async with repository_mutation(repo):
            child_entered = True

    async def owner() -> None:
        async with repository_mutation(repo):
            held.set()
            task = asyncio.create_task(child())
            for _ in range(5):
                await asyncio.sleep(0)
            assert not child_entered, "child task slipped in past the owner"
            release.set()
        # The child is served only after the owner let go.
        await asyncio.wait_for(task, timeout=5)

    await asyncio.wait_for(owner(), timeout=5)
    assert child_entered


# ── release on failure and cancellation ──────────────────────────────────────


async def test_failed_owner_releases_the_lock(tmp_path: Path) -> None:
    """A raised mutation must not strand the repository: the next backup tick
    would otherwise wait forever on a lock nobody holds."""
    repo = _repo(tmp_path / "repo")

    with pytest.raises(RuntimeError):
        async with repository_mutation(repo):
            raise RuntimeError("mutation blew up")

    async with repository_mutation(repo):
        pass
    assert active_repositories() == 0


async def test_cancelled_waiter_does_not_strand_the_lock(tmp_path: Path) -> None:
    """A cancelled *waiter* must leave the registry as clean as if it had never
    asked for the lock — otherwise the entry looks permanently busy and is
    never reclaimed."""
    repo = _repo(tmp_path / "repo")
    held = asyncio.Event()
    release = asyncio.Event()
    acquired = False

    async def owner() -> None:
        async with repository_mutation(repo):
            held.set()
            await release.wait()

    async def waiter() -> None:
        nonlocal acquired
        async with repository_mutation(repo):
            acquired = True

    owner_task = asyncio.create_task(owner())
    await held.wait()
    waiter_task = asyncio.create_task(waiter())
    for _ in range(5):
        await asyncio.sleep(0)
    waiter_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter_task

    # The owner still releases normally afterwards, and nothing was left busy.
    release.set()
    await owner_task
    assert not acquired
    assert active_repositories() == 0


async def test_cancelled_holder_releases_the_lock(tmp_path: Path) -> None:
    """Server shutdown cancels whatever is in flight; the next process start
    must not inherit a stuck lock."""
    repo = _repo(tmp_path / "repo")
    held = asyncio.Event()

    async def owner() -> None:
        async with repository_mutation(repo):
            held.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(owner())
    await held.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with repository_mutation(repo):
        pass
    assert active_repositories() == 0


async def test_idle_entries_are_reclaimed(tmp_path: Path) -> None:
    """A long-lived process must not accumulate a lock per repository it ever
    touched."""
    repos = [_repo(tmp_path / f"repo{i}") for i in range(3)]
    for repo in repos:
        async with repository_mutation(repo):
            assert active_repositories() >= 1
    assert active_repositories() == 0


async def test_a_waiter_keeps_the_entry_until_it_is_served(tmp_path: Path) -> None:
    """A queued writer is a live user of the lock, not a stale entry.

    Dropping the registry entry while a waiter is queued would hand the next
    caller a brand-new lock and let it run beside the queued one.
    """
    repo = _repo(tmp_path / "repo")
    held = asyncio.Event()
    release = asyncio.Event()

    async def owner() -> None:
        async with repository_mutation(repo):
            held.set()
            await release.wait()

    async def waiter() -> None:
        async with repository_mutation(repo):
            pass

    owner_task = asyncio.create_task(owner())
    await held.wait()
    waiter_task = asyncio.create_task(waiter())
    for _ in range(5):
        await asyncio.sleep(0)
    assert active_repositories() == 1
    release.set()
    await asyncio.wait_for(asyncio.gather(owner_task, waiter_task), timeout=5)
    assert active_repositories() == 0


# ── external git state is respected, never removed ───────────────────────────


def test_ensure_mutable_passes_a_clean_repository(tmp_path: Path) -> None:
    _repo(tmp_path / "repo")
    ensure_mutable(tmp_path / "repo")  # no raise


def test_preexisting_index_lock_is_preserved(tmp_path: Path) -> None:
    """A foreign ``index.lock`` means another git is writing the index right now.

    Deleting it — the tempting "fix" — can corrupt that operation (and on a
    shared volume it is another machine's commit). The repository must be
    reported as busy instead, with the marker still in place afterwards.
    """
    repo = _repo(tmp_path / "repo")
    lock_file = repo / ".git" / "index.lock"
    lock_file.write_text("", encoding="utf-8")

    with pytest.raises(RepositoryBusyError) as excinfo:
        ensure_mutable(repo)
    assert "index lock" in str(excinfo.value)
    assert lock_file.exists(), "ensure_mutable must never remove a foreign index lock"


def test_in_progress_merge_is_preserved(tmp_path: Path) -> None:
    """A merge Ciaobot did not start is the user's to finish or abort."""
    repo = _repo(tmp_path / "repo")
    (repo / ".git" / "MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")

    with pytest.raises(RepositoryBusyError) as excinfo:
        ensure_mutable(repo)
    assert "merge is in progress" in str(excinfo.value)
    assert (repo / ".git" / "MERGE_HEAD").exists()


@pytest.mark.parametrize("marker", ["rebase-merge", "rebase-apply"])
def test_in_progress_rebase_is_preserved(tmp_path: Path, marker: str) -> None:
    repo = _repo(tmp_path / "repo")
    state = repo / ".git" / marker
    state.mkdir()
    (state / "git-rebase-todo").write_text("pick deadbeef\n", encoding="utf-8")

    with pytest.raises(RepositoryBusyError) as excinfo:
        ensure_mutable(repo)
    assert "rebase is in progress" in str(excinfo.value)
    assert state.exists(), f"{marker} must be left for the user to finish or abort"


def test_worktree_git_path_is_used_not_the_main_checkout(tmp_path: Path) -> None:
    """A linked worktree keeps its own rebase state.

    ``.git`` there is a *file*, so a hardcoded ``.git/rebase-merge`` probe would
    look at the main checkout and miss the worktree's own operation —
    ``git rev-parse --git-path`` is what makes the check worktree-aware.
    """
    main = _repo(tmp_path / "main")
    worktree = tmp_path / "feature"
    _git(main, "worktree", "add", "-q", "-b", "feature", str(worktree))
    # The premise: `.git` here is a gitdir *pointer file*, not a directory, so
    # the naive `.git/<marker>` probe has nowhere to look.
    assert (worktree / ".git").is_file()

    state = Path(
        subprocess.run(
            ["git", "rev-parse", "--git-path", "rebase-merge"],
            cwd=str(worktree), check=True, capture_output=True, text=True,
        ).stdout.strip()
    )
    main_state = main / ".git" / "rebase-merge"
    assert state.resolve() != main_state.resolve(), (
        f"expected a worktree-private rebase dir, got {state}"
    )
    state.mkdir(parents=True)

    with pytest.raises(RepositoryBusyError):
        ensure_mutable(worktree)
    # The main checkout is untouched and still mutatable.
    ensure_mutable(main)


def test_ensure_mutable_outside_a_repository_is_not_an_error(tmp_path: Path) -> None:
    """A fresh install has no checkout and therefore no external git operation
    to wait for; the caller decides what a non-repository means."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    ensure_mutable(plain)
