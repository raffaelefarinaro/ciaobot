"""Route-level tests for ``POST /api/admin/snapshot`` (issue #680).

``admin_snapshot`` was the one git writer outside ``git_mutation``: it ran its
own add/status/commit/push on ``config.workspace_root`` while manual sync, the
post-conflict resync and the background backup all wrote the same checkout, and
it discarded the commit's return code. A rejected commit was reported as
``ok: true`` and then pushed, and a failing ``git status`` — which writes to
stderr and leaves stdout empty — read as "Nothing to commit".

Each test below runs real git in a throwaway repository under ``tmp_path`` with
a local bare origin, so the claims are about git's actual behaviour; only the
individual steps that must fail are replaced. ``subprocess.run`` is patched
through ``routes_api`` and passes everything it is not asked to fail straight
to the real one, which matters: ``repository_mutation`` resolves its lock key
with ``git rev-parse --show-toplevel``, and a recorder that swallowed that
would key the lock on nothing. The cancellation case pauses its commit inside
the route's own thread rather than replacing it — the claim there is about
ordering between a git child that is still running and a released lock, not
about any command's exit code.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ciao.git_mutation import repository_mutation, reset_mutation_locks
from ciao.web import routes_api
from ciao.web.routes_api import admin_snapshot


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


def _repo(path: Path, *, origin: Path | None = None) -> Path:
    """A one-commit repository, optionally pushed to a local bare origin."""
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "T")
    _git(path, "config", "user.email", "t@e.com")
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "seed")
    if origin is not None:
        _git(path, "remote", "add", "origin", str(origin))
        _git(path, "push", "-q", "-u", "origin", "main")
    return path


def _bare_origin(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "--bare", "-b", "main")
    return path


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


class _Request:
    """The minimum ``admin_snapshot`` reads: a body and the app state it uses.

    ``local_session_manager`` is None, so the secret preflight — a whole separate
    feature, covered by its own tests — is out of the way and every case here
    is about the git transaction itself.
    """

    def __init__(self, workspace: Path) -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(
            config=SimpleNamespace(workspace_root=workspace),
            local_session_manager=None,
        ))

    async def json(self) -> dict:
        return {}


def _body(response: Any) -> dict:
    return json.loads(response.body)


# The read-only git the mutation lock and the foreign-marker check make on
# their own; see `_verbs`.
_LOCK_READS = ("rev-parse",)


def _record_git(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail: dict[str, tuple[int, str, str]] | None = None,
) -> list[tuple[str, ...]]:
    """Record the git verbs the route runs, optionally failing chosen ones.

    Every other invocation — including the ``git rev-parse`` calls
    ``repository_mutation`` and ``ensure_mutable`` make to key the lock and find
    the index lock — goes to the real ``subprocess.run``, so this cannot make a
    plain folder look like a checkout or hide a repository the route must
    refuse.
    """
    calls: list[tuple[str, ...]] = []
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and len(cmd) > 1 and cmd[0] == "git":
            verb = cmd[1]
            calls.append(tuple(cmd[1:]))
            if fail and verb in fail:
                rc, out, err = fail[verb]
                return subprocess.CompletedProcess(cmd, rc, out, err)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(routes_api.subprocess, "run", fake_run)
    return calls


def _verbs(calls: list[tuple[str, ...]]) -> list[str]:
    """The git verbs the *route* ran.

    ``repository_mutation`` resolves its lock key and ``ensure_mutable`` locates
    the index lock with ``git rev-parse``, so those reads are recorded as well;
    the assertions below are about the route's own mutations, and dropping a
    named constant is clearer than repeating the filter at each call site.
    """
    return [args[0] for args in calls if args[0] not in _LOCK_READS]


async def _wait_thread_event(event: threading.Event, message: str) -> None:
    """Wait for an event the route's own git thread sets, without blocking it.

    The route runs git in a worker thread, so a step that pauses there has to
    be released from this side too; the bounded wait is what turns "the test
    hung" into a named failure.
    """
    assert await asyncio.to_thread(event.wait, 30), message


@pytest.fixture(autouse=True)
def _fresh_registry() -> None:
    """The mutation registry is process-wide; a lock left by a failing test
    must not leak into the next one."""
    reset_mutation_locks()
    yield
    reset_mutation_locks()


# ── a failed step is a failure, and nothing after it runs ────────────────────


async def test_snapshot_failed_commit_does_not_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit git refused is not a snapshot.

    A missing identity, a full disk or a hostile pre-commit hook all land here,
    and this used to return 200 with ``Snapshot committed and pushed`` over a
    HEAD that had not moved.
    """
    repo = _repo(tmp_path / "repo", origin=_bare_origin(tmp_path / "origin.git"))
    (repo / "note.md").write_text("work\n", encoding="utf-8")
    before = _head(repo)
    calls = _record_git(
        monkeypatch, fail={"commit": (1, "", "fatal: cannot commit: hook declined")}
    )

    response = await admin_snapshot(_Request(repo))

    assert response.status_code == 500
    body = _body(response)
    assert "git commit failed" in body["error"]
    assert "hook declined" in body["error"]
    # The push is the damaging half: it would publish a snapshot that is not
    # in the repository, so it must never run.
    assert "push" not in _verbs(calls)
    assert _head(repo) == before
    assert _git(tmp_path / "origin.git", "log", "--format=%s").splitlines() == ["seed"]


async def test_snapshot_failed_status_does_not_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing ``git status`` is not a clean tree.

    git reports the error on stderr and leaves stdout empty, which is exactly
    what the old ``if not status.stdout`` read as "Nothing to commit" — a 200
    claiming there was nothing to snapshot while the repository was broken.
    """
    repo = _repo(tmp_path / "repo")
    (repo / "note.md").write_text("work\n", encoding="utf-8")
    before = _head(repo)
    calls = _record_git(
        monkeypatch, fail={"status": (128, "", "fatal: bad object HEAD")}
    )

    response = await admin_snapshot(_Request(repo))

    assert response.status_code == 500
    body = _body(response)
    assert "git status failed" in body["error"]
    assert "bad object" in body["error"]
    assert body.get("message") != "Nothing to commit"
    # Nothing that records or publishes work may follow a status we could not
    # read, so the pending note is still pending.
    assert _verbs(calls) == ["add", "status"]
    assert _head(repo) == before


# ── it is the fourth writer in one checkout, so it serializes ────────────────


async def test_snapshot_waits_for_same_repository_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock is the same one sync, resync and the backup take.

    The other writer parks on an event *while holding* the repository, and the
    test asserts the snapshot has not staged anything before releasing it, so
    this fails on a real overlap rather than on a slow scheduler. It also pins
    the cost of waiting: after the release the snapshot completes normally.
    """
    repo = _repo(tmp_path / "repo", origin=_bare_origin(tmp_path / "origin.git"))
    (repo / "note.md").write_text("work\n", encoding="utf-8")
    calls = _record_git(monkeypatch)
    held = asyncio.Event()
    release = asyncio.Event()

    async def other_writer() -> None:
        async with repository_mutation(repo):
            held.set()
            await release.wait()

    writer = asyncio.create_task(other_writer())
    await held.wait()
    snapshot = asyncio.create_task(admin_snapshot(_Request(repo)))
    for _ in range(5):
        await asyncio.sleep(0)
    assert "add" not in _verbs(calls), "snapshot staged while another writer held the repository"
    assert not snapshot.done()

    release.set()
    response = await asyncio.wait_for(snapshot, timeout=30)
    await asyncio.wait_for(writer, timeout=30)

    assert response.status_code == 200
    assert _body(response) == {"ok": True, "message": "Snapshot committed and pushed"}
    assert _verbs(calls) == ["add", "status", "commit", "push"]
    assert _git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["note.md"]
    assert _git(tmp_path / "origin.git", "log", "--format=%s").splitlines()[0].startswith(
        "pwa snapshot "
    )


async def test_snapshot_refuses_foreign_git_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A git operation Ciaobot did not start keeps its lock.

    Deleting a stale-looking ``index.lock`` is the tempting shortcut and the
    destructive one: on a shared volume it may belong to a real ``git commit``
    on another machine, and removing it lets two git processes write the same
    index. So the snapshot reports the refusal and leaves the marker alone.
    """
    repo = _repo(tmp_path / "repo")
    (repo / "note.md").write_text("work\n", encoding="utf-8")
    marker = repo / ".git" / "index.lock"
    marker.write_text("", encoding="utf-8")
    before = _head(repo)
    calls = _record_git(monkeypatch)

    response = await admin_snapshot(_Request(repo))

    assert response.status_code == 500
    assert "index lock" in _body(response)["error"]
    assert marker.exists(), "a foreign index.lock must never be removed"
    assert marker.read_text(encoding="utf-8") == ""
    assert not {"add", "commit", "push"} & set(_verbs(calls))
    assert _head(repo) == before


# ── cancellation is not a shorter transaction ─────────────────────────────


async def test_snapshot_cancellation_waits_for_git_before_unlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled snapshot still owns the repository until git has exited.

    ``asyncio.to_thread`` cannot be recalled once its worker thread is running,
    so cancelling the route used to unwind it at once: ``repository_mutation``
    released, the backup or a manual sync took the checkout, and the abandoned
    commit carried on writing the index next to it. The commit here is paused
    inside the route's own thread, which is the window a real slow commit
    opens; the test cancels the route inside that window, starts a second
    writer, and requires it to stay locked out until the commit is released.
    """
    repo = _repo(tmp_path / "repo", origin=_bare_origin(tmp_path / "origin.git"))
    (repo / "note.md").write_text("work\n", encoding="utf-8")
    entered = threading.Event()
    release = threading.Event()
    calls: list[tuple[str, ...]] = []
    real_run = subprocess.run

    def pausing_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and len(cmd) > 1 and cmd[0] == "git":
            calls.append(tuple(cmd[1:]))
            if cmd[1] == "commit":
                # Bounded, so a failing assertion cannot leave this thread
                # parked after the test has given up on it.
                entered.set()
                release.wait(30)
                return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(routes_api.subprocess, "run", pausing_run)
    attempting = asyncio.Event()
    acquired = asyncio.Event()

    async def other_writer() -> None:
        attempting.set()
        async with repository_mutation(repo):
            acquired.set()

    snapshot = asyncio.create_task(admin_snapshot(_Request(repo)))
    await _wait_thread_event(entered, "the snapshot never reached its commit")

    writer = asyncio.create_task(other_writer())
    snapshot.cancel()
    await asyncio.wait_for(attempting.wait(), timeout=10)
    # Wall clock, not a scheduler yield: the writer has to get as far as the
    # lock before "still locked out" means anything. A second is generous for
    # the ``git rev-parse`` it resolves its key with, and the loop stops early
    # if the lock is handed over early — which is the failure.
    for _ in range(20):
        if acquired.is_set():
            break
        await asyncio.sleep(0.05)
    assert _verbs(calls) == ["add", "status", "commit"]
    assert not acquired.is_set(), (
        "another writer took the repository while the cancelled snapshot's git was running"
    )
    assert not snapshot.done(), "the route returned before its git step exited"

    # A second cancel, landing in the route's cleanup wait instead of in the
    # original await. It must not detach the thread either, or the wait it is
    # doing is exactly what gets skipped. The pause before it is delivery
    # latency for the first cancel — the route is a few statements into its
    # cleanup by then, and git is still parked.
    await asyncio.sleep(0.05)
    snapshot.cancel()
    for _ in range(20):
        if acquired.is_set():
            break
        await asyncio.sleep(0.05)
    assert not acquired.is_set(), (
        "a repeated cancellation released the repository while git was still running"
    )

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(snapshot, timeout=30)
    # The lock comes free only now, and only for a writer already queued on it.
    await asyncio.wait_for(writer, timeout=30)
    assert acquired.is_set()
    verbs = _verbs(calls)
    assert verbs == ["add", "status", "commit"], "a snapshot step ran after cancellation"
    assert "push" not in verbs
    assert _git(tmp_path / "origin.git", "log", "--format=%s").splitlines() == ["seed"]
