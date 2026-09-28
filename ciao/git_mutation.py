"""One mutation lock per git repository, keyed by the canonical checkout.

Manual sync, the post-conflict resync, and the background memory backup all
mutate the same repository. They can fire concurrently — the five-minute
backup lands in the middle of a manual sync's fetch/push — and git itself
offers no defence beyond ``.git/index.lock``, which it takes and drops so
fast that a concurrent ``git add -A`` from Ciaobot either waits or fails with
a confusing "unable to create index.lock" error. That race is what this module
removes, and it is the prerequisite the #643 backup service builds on: a third
writer joining later must not need its own locking.

The key is the repository top level, not the path a caller happened to hold, so
a workspace root, a vault root living in its own repo, and any subdirectory
alias of either all serialize against one another while two unrelated
repositories never wait on each other. Resolution runs off-loop (it is a
subprocess) and degrades to the resolved path outside a checkout, so a
non-repository workspace still gets a lock instead of an error.

Reentrancy is by asyncio task: a helper invoked from inside a mutation in the
*same* task reenters without deadlocking, while a child task — a distinct
owner — waits like any other caller. The registry drops an entry as soon as it
has no holder and no waiters, so a process that syncs many repositories does not
accumulate locks for checkouts it has finished with.

This is deliberately the only serialization point for git mutations. It is a
process-local lock, not a file lock: it orders Ciaobot's own operations and says
nothing about the user's own terminal git. :func:`ensure_mutable` is how that
external state is respected — it reports a foreign lock or an in-progress
merge/rebase rather than deleting it, because a stale-looking ``index.lock`` may
belong to a real ``git commit`` on another machine path (NFS, a shared volume)
and deleting it corrupts their operation.

Leaf module by design: it imports nothing from ``ciao`` except the git
subprocess wrapper, which is what lets ``ciao.local_session`` depend on it
without a cycle.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from ciao.git_proc import run_git_sync

logger = logging.getLogger(__name__)


def _resolve(path: Path) -> Path:
    """Absolute, symlink-free form of ``path``, tolerating a missing path.

    ``Path.resolve()`` is non-strict on 3.6+, so a not-yet-created directory
    still canonicalizes; that matters because a fresh install's workspace root
    can be empty and must still key deterministically.
    """
    try:
        return path.resolve()
    except OSError:
        return Path(str(path.absolute()))


def _from_git(workspace: Path, raw: str) -> Path:
    """Resolve git's stdout, which may be relative to the cwd git ran in.

    ``git rev-parse --git-path index.lock`` answers ``.git/index.lock`` — relative
    to the *subprocess* cwd, not to ours. Taking ``Path(raw).resolve()`` directly
    would resolve it against the server's own working directory and silently
    look for a lock file that does not exist there.
    """
    path = Path(raw)
    if not path.is_absolute():
        path = Path(workspace) / path
    return _resolve(path)


def canonical_repository(workspace: Path) -> Path:
    """The checkout ``workspace`` belongs to, resolved to a stable key.

    Subdirectories, symlinked paths, and a bare ``.git`` file all collapse onto
    one key when they name the same work tree. Outside a work tree the resolved
    path is returned, so a non-repository workspace serializes against itself
    rather than raising.
    """
    rc, out, _err = run_git_sync(Path(workspace), "rev-parse", "--show-toplevel")
    if rc != 0 or not out.strip():
        return _resolve(Path(workspace))
    return _from_git(workspace, out.strip())


class RepositoryBusyError(RuntimeError):
    """A foreign git operation holds the repository; do not mutate it.

    Raised by :func:`ensure_mutable` for a preexisting ``index.lock`` or an
    in-progress merge/rebase. Callers convert it into their own return shape
    rather than removing the marker, so the user's own operation finishes on its
    terms.
    """

    def __init__(self, marker: str, reason: str) -> None:
        super().__init__(f"{reason} ({marker})")
        self.marker = marker
        self.reason = reason

    @property
    def detail(self) -> str:
        return str(self)


# Ordered most-urgent first: a real in-progress operation is a harder stop than
# a lock file that may be stale. Names are git-internal paths, resolved with
# ``git rev-parse --git-path`` so a linked worktree finds its own
# ``rebase-merge``/``index.lock`` rather than the main checkout's.
_OPERATION_MARKERS: tuple[tuple[str, str], ...] = (
    ("MERGE_HEAD", "a merge is in progress"),
    ("rebase-merge", "a rebase is in progress"),
    ("rebase-apply", "a rebase is in progress"),
    ("index.lock", "another git operation holds the index lock"),
)


def _git_paths(workspace: Path, names: Sequence[str]) -> list[Path] | None:
    """Absolute locations of git-internal files, or None outside a repository.

    ``git rev-parse --git-path`` is the worktree-aware form: it accounts for
    ``GIT_DIR``, ``GIT_INDEX_FILE``, and a linked worktree's private git
    directory, all of which make ``.git/<name>`` the wrong guess. Every name is
    asked in one invocation — this runs on every public mutation, and four
    spawns to answer a yes/no question is the wrong price for a loop that
    runs every few minutes.
    """
    args: list[str] = []
    for name in names:
        args += ["--git-path", name]
    rc, out, _err = run_git_sync(Path(workspace), "rev-parse", *args)
    if rc != 0:
        return None
    return [_from_git(workspace, line) for line in out.splitlines() if line.strip()]


def _busy_marker(workspace: Path) -> tuple[str, str] | None:
    """The first foreign-operation marker present in ``workspace``, or None."""
    paths = _git_paths(workspace, [name for name, _ in _OPERATION_MARKERS])
    if paths is None:
        return None
    for (name, reason), path in zip(_OPERATION_MARKERS, paths, strict=False):
        if path.exists():
            return (name, reason)
    return None


def ensure_mutable(workspace: Path) -> None:
    """Raise :class:`RepositoryBusyError` if the workspace is not ours to mutate.

    Checked once, at the outer entry to a public mutation — a nested helper
    inside the mutation this same call started is allowed to leave the markers
    its own ``merge``/``abort`` created, which is why the callers below invoke
    this *before* delegating to the operation body.

    Outside a repository there is nothing foreign to wait for, so this is a
    no-op: whether a non-git workspace is an error is the caller's decision.
    """
    busy = _busy_marker(workspace)
    if busy is None:
        return
    name, reason = busy
    logger.info("Refusing to mutate %s: %s", workspace, reason)
    raise RepositoryBusyError(name, reason)


class _RepositoryLock:
    """A reentrant, task-aware lock for one canonical repository.

    ``asyncio.Lock`` alone is not enough: it is not reentrant, so the natural
    shape of these operations (public ``sync_branch`` calling ``commit_pending``
    which also wants the lock) deadlocks. Keying reentrancy on the owning task
    is what makes that shape safe while still making a *child* task wait — the
    distinction that matters, because a child task doing an independent
    mutation must not slip in while the owner is mid-operation.
    """

    __slots__ = ("_lock", "_owner", "_depth", "_waiters")

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task[object] | None = None
        self._depth = 0
        self._waiters = 0

    @property
    def busy(self) -> bool:
        return self._depth > 0 or self._waiters > 0

    async def acquire(self) -> None:
        task = asyncio.current_task()
        if self._owner is not None and self._owner is task:
            self._depth += 1
            return
        if self.busy:
            # Worth a debug line: a backup tick that quietly waits behind a
            # manual sync is otherwise indistinguishable from a hung push.
            logger.debug("Git mutation queued behind another on this repository")
        self._waiters += 1
        try:
            await self._lock.acquire()
        except BaseException:
            # Cancellation while waiting: the underlying lock was never taken,
            # so drop the waiter count or the entry would look busy forever and
            # never be cleaned from the registry.
            self._waiters -= 1
            raise
        self._waiters -= 1
        self._owner = task
        self._depth = 1

    def release(self) -> None:
        if self._depth == 0:
            return
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()


# Per event loop, because an asyncio primitive cannot be shared across loops
# (a second loop raises on first use of a lock created on another). Weak keys
# let a closed loop's whole map go with it instead of pinning a dead loop, and
# the inner dicts stay small because an unreferenced lock is dropped on release.
_LOOP_LOCKS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, _RepositoryLock]
] = weakref.WeakKeyDictionary()


def _lock_for(loop: asyncio.AbstractEventLoop, key: str) -> _RepositoryLock:
    locks = _LOOP_LOCKS.get(loop)
    if locks is None:
        locks = {}
        _LOOP_LOCKS[loop] = locks
    lock = locks.get(key)
    if lock is None:
        lock = _RepositoryLock()
        locks[key] = lock
    return lock


def reset_mutation_locks() -> None:
    """Drop every registered lock. Test isolation hook, like
    ``async_reads.reset_vault_read_executor``; not part of the mutation API."""
    _LOOP_LOCKS.clear()


def active_repositories() -> int:
    """How many repositories currently have a registered lock. Test/debug aid."""
    return sum(len(locks) for locks in _LOOP_LOCKS.values())


@asynccontextmanager
async def repository_mutation(workspace: Path) -> AsyncIterator[Path]:
    """Hold this repository's mutation lock for the duration of the block.

    Every public git mutation in ``ciao.local_session`` runs inside one of
    these. The lock is keyed by the canonical top level, so an alias of the same
    checkout (a subdirectory, a symlinked path) queues behind the mutation
    already running, and a nested call in the same task reenters instead of
    deadlocking. Released in a ``finally``, so a raised error or a cancelled
    holder cannot strand it.

    Yields the canonical repository root, so a caller holding a subdirectory
    still commits and pushes against the checkout that owns it.
    """
    root = await asyncio.to_thread(canonical_repository, Path(workspace))
    key = str(root)
    lock = _lock_for(asyncio.get_running_loop(), key)
    await lock.acquire()
    try:
        yield root
    finally:
        lock.release()
        if not lock.busy:
            # Only this holder can observe `not busy` (every other user is
            # counted in _waiters), and the map lookup guards against a lock
            # replaced between the two statements.
            loop = asyncio.get_running_loop()
            locks = _LOOP_LOCKS.get(loop)
            if locks is not None and locks.get(key) is lock:
                del locks[key]
                if not locks:
                    _LOOP_LOCKS.pop(loop, None)
