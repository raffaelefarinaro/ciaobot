"""Tests for ciao/local_session.py: the current-branch git sync flow.

Ciaobot never creates or switches local branches: it works on whatever branch
the workspace checkout is on and syncs it via ``sync_branch`` (commit + pull +
push; conflict -> hand off to a chat). Non-git workspaces skip gracefully. The
safety rule the tests pin down: never discard local work, never touch other
branches.

The last section covers the unattended path — ``commit_scoped`` and
``preflight_scoped`` — which shares every one of those rules and adds one of
its own: it commits a named scope, so an unrelated change the user had staged
must survive it untouched.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from types import SimpleNamespace

import pytest

from ciao.config import CiaoConfig, reset_reroot_cache
from ciao.git_proc import GIT_TIMEOUT_DETAIL
from ciao.workspace_reroot import mark_born_per_root
from ciao.local_session import (
    PREFLIGHT_STEP,
    GitOperationError,
    LocalSessionManager,
    backoff_reason,
    commit_pending,
    commit_scoped,
    has_origin_remote,
    is_git_repo,
    preflight_scoped,
    repo_toplevel,
    resync_branch,
    sync_branch,
    sync_root,
    workspace_branch,
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


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _identify(repo: Path) -> None:
    """Pin a repo-local identity so async commits in local_session work."""
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "user.email", "t@e.com")


def _make_world(tmp_path: Path, *, branch: str = "main") -> tuple[Path, Path]:
    """Bare origin + a clone checked out on ``branch`` with one commit."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare", "-b", "main")
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _identify(seed)
    _write(seed / "README.md", "seed\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "-u", "origin", "main")
    local = tmp_path / "local"
    _git(tmp_path, "clone", "-q", str(origin), str(local))
    _identify(local)
    if branch != "main":
        # The user's checkout may sit on any branch; Ciaobot works there as-is.
        _git(local, "checkout", "-q", "-b", branch)
        _git(local, "push", "-q", "-u", "origin", branch)
    return local, origin


def _branches(repo: Path) -> set[str]:
    return set(_git(repo, "branch", "--format=%(refname:short)").split())


def _advance_origin(tmp_path: Path, origin: Path, name: str, *, branch: str = "main") -> None:
    """Push a new commit to origin/<branch> from a throwaway clone."""
    other = tmp_path / name
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    _identify(other)
    if branch != "main":
        _git(other, "checkout", "-q", branch)
    _write(other / f"{name}.md", f"{name}\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", name)
    _git(other, "push", "-q")


# ── workspace_branch / has_origin_remote ────────────────────────────────────


def test_workspace_branch_none_when_not_a_git_repo(tmp_path: Path) -> None:
    assert workspace_branch(tmp_path) is None
    assert is_git_repo(tmp_path) is False
    assert has_origin_remote(tmp_path) is False


def test_workspace_branch_reports_current_branch(tmp_path: Path) -> None:
    local, _ = _make_world(tmp_path)
    assert workspace_branch(local) == "main"
    assert is_git_repo(local) is True
    assert has_origin_remote(local) is True


def test_workspace_branch_none_on_detached_head(tmp_path: Path) -> None:
    local, _ = _make_world(tmp_path)
    _git(local, "checkout", "-q", "--detach", "HEAD")
    assert workspace_branch(local) is None


def test_has_origin_remote_false_without_origin(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    assert has_origin_remote(repo) is False


# ── sync_root ────────────────────────────────────────────────────────────────


def _config_stub(*, workspace: Path, vault: Path) -> SimpleNamespace:
    return SimpleNamespace(workspace_root=workspace, vault_root=vault)


def test_sync_root_picks_standalone_vault_repo(tmp_path: Path) -> None:
    """Git follows the vault: a vault with its own repo wins over the workspace."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    vault = tmp_path / "brain"
    vault.mkdir()
    _git(vault, "init", "-q", "-b", "main")

    root = sync_root(_config_stub(workspace=workspace, vault=vault))

    assert root == repo_toplevel(vault) == vault.resolve()


def test_sync_root_vault_inside_workspace_repo_targets_workspace(tmp_path: Path) -> None:
    """Default layout: the vault lives inside the workspace repo, so sync
    keeps targeting the workspace root (same repo either way)."""
    workspace = tmp_path / "ws"
    vault = workspace / "memory-vault"
    vault.mkdir(parents=True)
    _git(workspace, "init", "-q", "-b", "main")

    root = sync_root(_config_stub(workspace=workspace, vault=vault))

    assert root == workspace.resolve()


def test_sync_root_falls_back_to_workspace_root(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    # Vault directory does not exist yet.
    missing = _config_stub(workspace=workspace, vault=tmp_path / "nope")
    assert sync_root(missing) == workspace

    # Vault exists but is not (in) a git repository.
    plain = tmp_path / "plain-vault"
    plain.mkdir()
    assert sync_root(_config_stub(workspace=workspace, vault=plain)) == workspace


# ── sync_branch ──────────────────────────────────────────────────────────────


async def test_sync_branch_commits_pulls_and_pushes(tmp_path: Path) -> None:
    local, origin = _make_world(tmp_path)
    _write(local / "memory-vault" / "note.md", "a note\n")

    result = await sync_branch(local, branch="main")
    assert result["ok"] is True
    assert result["merged"] is True
    assert result["pushed"] is True
    assert result["deploy_needed"] is False
    # Still on the same branch; nothing else was created.
    assert workspace_branch(local) == "main"
    assert _branches(local) == {"main"}
    # origin/main advanced with the note.
    check = tmp_path / "check"
    _git(tmp_path, "clone", "-q", str(origin), str(check))
    assert (check / "memory-vault" / "note.md").exists()


async def test_sync_branch_works_on_non_main_branch_as_is(tmp_path: Path) -> None:
    local, origin = _make_world(tmp_path, branch="feature-x")
    _write(local / "wip.md", "work in progress\n")

    result = await sync_branch(local, branch="feature-x")
    assert result["ok"] is True and result["merged"] is True
    # Never checked out or created any other branch.
    assert workspace_branch(local) == "feature-x"
    assert _branches(local) == {"feature-x", "main"}
    check = tmp_path / "check"
    _git(tmp_path, "clone", "-q", "-b", "feature-x", str(origin), str(check))
    assert (check / "wip.md").exists()


async def test_sync_branch_pulls_remote_work_first(tmp_path: Path) -> None:
    local, origin = _make_world(tmp_path)
    _advance_origin(tmp_path, origin, "elsewhere")
    _write(local / "local.md", "local\n")

    result = await sync_branch(local, branch="main")
    assert result["ok"] is True and result["merged"] is True
    assert (local / "elsewhere.md").exists()  # remote work merged in
    assert (local / "local.md").exists()  # local work kept


async def test_sync_branch_pushes_branch_missing_on_origin(tmp_path: Path) -> None:
    # A branch that exists only locally has nothing to pull; sync just pushes it.
    local, origin = _make_world(tmp_path)
    _git(local, "checkout", "-q", "-b", "only-local")
    _write(local / "new.md", "new\n")

    result = await sync_branch(local, branch="only-local")
    assert result["ok"] is True and result["merged"] is True
    assert workspace_branch(local) == "only-local"
    check = tmp_path / "check"
    _git(tmp_path, "clone", "-q", "-b", "only-local", str(origin), str(check))
    assert (check / "new.md").exists()


async def test_sync_branch_conflict_hands_off_without_switching(tmp_path: Path) -> None:
    local, origin = _make_world(tmp_path)
    _advance_origin(tmp_path, origin, "remote-edit")
    # Make the remote edit conflict with a local one on the same file.
    other = tmp_path / "conflicting"
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    _identify(other)
    _write(other / "README.md", "remote version\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "remote readme")
    _git(other, "push", "-q")
    _write(local / "README.md", "local version\n")

    result = await sync_branch(local, branch="main")
    assert result["ok"] is True
    assert result["merged"] is False
    assert result["conflict"] is True
    assert result["branch"] == "main"
    # Conflict left in place for the resolution chat; still on the same branch.
    assert workspace_branch(local) == "main"
    assert "<<<<<<<" in (local / "README.md").read_text()


async def test_sync_branch_push_failure(tmp_path: Path, monkeypatch) -> None:
    local, _ = _make_world(tmp_path)
    _write(local / "note.md", "x\n")

    import ciao.local_session
    orig_git = ciao.local_session._git

    async def mock_git(workspace, *args, **kwargs):
        if args[:2] == ("push", "-u"):
            return 1, "", "fatal: push rejected"
        return await orig_git(workspace, *args, **kwargs)

    monkeypatch.setattr(ciao.local_session, "_git", mock_git)

    result = await sync_branch(local, branch="main")
    assert result["ok"] is False
    assert result["step"] == "push"
    assert "push rejected" in result["error"]
    assert workspace_branch(local) == "main"


# ── resync_branch ────────────────────────────────────────────────────────────


async def test_resync_merges_origin_into_current_branch(tmp_path: Path) -> None:
    local, origin = _make_world(tmp_path)
    _advance_origin(tmp_path, origin, "chatmerge")
    _write(local / "README.md", "locally edited, uncommitted\n")  # dirty tree

    ok, _ = await resync_branch(local, branch="main")
    assert ok is True
    assert workspace_branch(local) == "main"
    assert (local / "chatmerge.md").exists()  # origin's commit pulled in


async def test_resync_preserves_unpushed_local_commit(tmp_path: Path) -> None:
    local, origin = _make_world(tmp_path)
    _write(local / "snapshot.md", "post-sync snapshot\n")
    _git(local, "add", "-A")
    _git(local, "commit", "-q", "-m", "snapshot")
    _advance_origin(tmp_path, origin, "chatmerge")

    ok, _ = await resync_branch(local, branch="main")
    assert ok is True
    assert (local / "snapshot.md").exists()  # local work kept
    assert (local / "chatmerge.md").exists()  # origin brought in


async def test_resync_conflict_aborts_cleanly(tmp_path: Path) -> None:
    local, origin = _make_world(tmp_path)
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    _identify(other)
    _write(other / "README.md", "remote version\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "remote readme")
    _git(other, "push", "-q")
    _write(local / "README.md", "local version\n")

    ok, detail = await resync_branch(local, branch="main")
    assert ok is False
    assert "conflict" in detail
    # Merge aborted: no conflict markers, no MERGE_HEAD, still on the branch.
    assert workspace_branch(local) == "main"
    assert "<<<<<<<" not in (local / "README.md").read_text()
    assert "MERGE_HEAD" not in _git(local, "status")


async def test_resync_ok_when_branch_missing_on_origin(tmp_path: Path) -> None:
    local, _ = _make_world(tmp_path)
    _git(local, "checkout", "-q", "-b", "only-local")

    ok, detail = await resync_branch(local, branch="only-local")
    assert ok is True
    assert "no remote branch" in detail


# ── failed steps are reported as failures (issue #674) ──────────────────────
#
# commit_pending used to discard every return code, so a failed add/commit read
# as "a commit was created" and sync went on to push. Each case below asserts
# both halves of the fix: the right step is named, and nothing after the
# failure is attempted.


def _record_git(monkeypatch, *, fail: dict[str, tuple[int, str, str]] | None = None) -> list[tuple[str, ...]]:
    """Record every git verb the flow runs, optionally failing chosen verbs."""
    import ciao.local_session

    calls: list[tuple[str, ...]] = []
    orig_git = ciao.local_session._git

    async def mock_git(workspace, *args, **kwargs):
        calls.append(args)
        if fail and args[0] in fail:
            return fail[args[0]]
        return await orig_git(workspace, *args, **kwargs)

    monkeypatch.setattr(ciao.local_session, "_git", mock_git)
    return calls


def _verbs(calls: list[tuple[str, ...]]) -> list[str]:
    return [args[0] for args in calls]


async def test_sync_branch_reports_a_failed_add_and_stops(tmp_path: Path, monkeypatch) -> None:
    local, _ = _make_world(tmp_path)
    _write(local / "note.md", "x\n")
    calls = _record_git(monkeypatch, fail={"add": (128, "", "fatal: unable to create index.lock")})

    result = await sync_branch(local, branch="main")

    assert result["ok"] is False
    assert result["step"] == "add"
    assert "index.lock" in result["error"]
    # Nothing that mutates further may run after the staging failure.
    assert _verbs(calls) == ["add"]
    assert _git(local, "status", "--porcelain") != ""


async def test_sync_branch_reports_a_failed_status_check(tmp_path: Path, monkeypatch) -> None:
    """``git diff --cached --quiet`` exits >1 on a real error, which is not the
    same as "nothing to commit" and must not commit on."""
    local, _ = _make_world(tmp_path)
    _write(local / "note.md", "x\n")
    calls = _record_git(monkeypatch, fail={"diff": (128, "", "fatal: bad object HEAD")})

    result = await sync_branch(local, branch="main")

    assert result["ok"] is False
    assert result["step"] == "status"
    assert "bad object" in result["error"]
    assert _verbs(calls) == ["add", "diff"]


async def test_sync_branch_reports_a_failed_commit_and_never_pushes(
    tmp_path: Path, monkeypatch
) -> None:
    local, origin = _make_world(tmp_path)
    _write(local / "note.md", "x\n")
    remote_before = _git(origin, "rev-parse", "main")
    calls = _record_git(monkeypatch, fail={"commit": (1, "", "fatal: empty commit message")})

    result = await sync_branch(local, branch="main")

    assert result["ok"] is False
    assert result["step"] == "commit"
    assert "empty commit" in result["error"]
    # No fetch, no pull, no push: the uncommitted work must stay local and
    # retryable, not be reported as synced.
    assert "push" not in _verbs(calls)
    assert _git(origin, "rev-parse", "main") == remote_before


async def test_sync_branch_reports_a_failed_fetch_before_pulling(
    tmp_path: Path, monkeypatch
) -> None:
    """A pull against a fetch that never landed merges a stale origin ref."""
    local, _ = _make_world(tmp_path)
    _write(local / "note.md", "x\n")
    calls = _record_git(monkeypatch, fail={"fetch": (1, "", "fatal: could not read from remote")})

    result = await sync_branch(local, branch="main")

    assert result["ok"] is False
    assert result["step"] == "fetch"
    assert "could not read" in result["error"]
    assert "pull" not in _verbs(calls)
    assert "push" not in _verbs(calls)


async def test_sync_branch_refuses_a_preexisting_index_lock(tmp_path: Path) -> None:
    """A foreign lock is an explicit failure, not a race and not a deletion."""
    local, _ = _make_world(tmp_path)
    _write(local / "note.md", "x\n")
    lock_file = local / ".git" / "index.lock"
    lock_file.write_text("", encoding="utf-8")

    result = await sync_branch(local, branch="main")

    assert result["ok"] is False
    assert result["step"] == "preflight"
    assert "index lock" in result["error"]
    assert lock_file.exists()


async def test_resync_reports_a_failed_commit_without_merging(
    tmp_path: Path, monkeypatch
) -> None:
    """Merging origin on top of an uncommitted tree is not what was asked."""
    local, origin = _make_world(tmp_path)
    _advance_origin(tmp_path, origin, "remote-only")
    _write(local / "local.md", "local\n")
    calls = _record_git(monkeypatch, fail={"commit": (1, "", "fatal: identity unknown")})

    ok, detail = await resync_branch(local, branch="main")

    assert ok is False
    assert detail.startswith("commit failed:")
    assert "identity unknown" in detail
    assert "merge" not in _verbs(calls)
    assert not (local / "remote-only.md").exists()
    assert _git(local, "status", "--porcelain") != ""


async def test_resync_refuses_an_in_progress_merge(tmp_path: Path) -> None:
    local, _ = _make_world(tmp_path)
    (local / ".git" / "MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")

    ok, detail = await resync_branch(local, branch="main")

    assert ok is False
    assert "merge is in progress" in detail
    assert (local / ".git" / "MERGE_HEAD").exists()


async def test_push_branch_refuses_a_preexisting_index_lock(tmp_path: Path) -> None:
    """The backup loop reports the refusal through its own (ok, detail) shape."""
    from ciao.local_session import push_branch

    local, origin = _make_world(tmp_path)
    (local / ".git" / "index.lock").write_text("", encoding="utf-8")
    remote_before = _git(origin, "rev-parse", "main")

    ok, detail = await push_branch(local, branch="main")

    assert ok is False
    assert "index lock" in detail
    assert _git(origin, "rev-parse", "main") == remote_before


async def test_two_public_mutations_of_one_repository_serialize(
    tmp_path: Path, monkeypatch
) -> None:
    """The integration the lock exists for: two public operations on one
    checkout never overlap. Overlap is recorded, not timed."""
    local, _ = _make_world(tmp_path)
    _write(local / "note.md", "x\n")
    import ciao.local_session
    from ciao.local_session import push_branch

    orig_git = ciao.local_session._git
    active = 0
    overlapped = False
    both_done = asyncio.Event()

    async def slow_git(workspace, *args, **kwargs):
        nonlocal active, overlapped
        active += 1
        overlapped = overlapped or active > 1
        try:
            # Real git, but every call yields first so a wrongly-parallel
            # writer would be observed here rather than being missed.
            await asyncio.sleep(0.01)
            return await orig_git(workspace, *args, **kwargs)
        finally:
            active -= 1
            if active == 0:
                both_done.set()

    monkeypatch.setattr(ciao.local_session, "_git", slow_git)

    results = await asyncio.gather(
        commit_pending(local, branch="main"),
        push_branch(local, branch="main"),
    )

    assert not overlapped
    assert results[0] is True  # the commit
    assert results[1][0] is True  # the push of it
    assert both_done.is_set()


async def test_manager_reports_a_preexisting_index_lock_in_its_own_envelope(
    tmp_path: Path,
) -> None:
    """A refusal must reach the route as data. The manager's existing error
    shapes (a dict for handback, ``ok``/``detail`` for resync) are what
    ``local_handback``/``local_resync`` map to a status code, so a preflight
    refusal has to arrive in those shapes rather than as a raised error."""
    local, _ = _make_world(tmp_path)
    _write(local / "note.md", "x\n")
    (local / ".git" / "index.lock").write_text("", encoding="utf-8")
    mgr = LocalSessionManager(workspace=local, runtime_root=tmp_path / "rt")

    handback = await mgr.commit_and_sync()
    assert handback["ok"] is False
    assert handback["step"] == "preflight"

    resync = await mgr.resync()
    assert resync["ok"] is False
    assert "index lock" in resync["detail"]


# ── LocalSessionManager ──────────────────────────────────────────────────────


def test_manager_status_non_git_workspace(tmp_path: Path) -> None:
    mgr = LocalSessionManager(workspace=tmp_path, runtime_root=tmp_path / "rt")
    assert mgr.branch is None
    assert mgr.status() == {
        "git_repo": False,
        "branch": None,
        "dirty": False,
        "dev_mode": False,
    }


def test_manager_status_reports_current_branch(tmp_path: Path) -> None:
    local, _ = _make_world(tmp_path, branch="feature-x")
    _write(local / "dirty.md", "dirty\n")
    mgr = LocalSessionManager(workspace=local, runtime_root=tmp_path / "rt", dev_mode=True)
    assert mgr.status() == {
        "git_repo": True,
        "branch": "feature-x",
        "dirty": True,
        "dev_mode": True,
    }


async def test_manager_sync_skips_non_git_workspace(tmp_path: Path) -> None:
    mgr = LocalSessionManager(workspace=tmp_path, runtime_root=tmp_path / "rt")
    result = await mgr.commit_and_sync()
    assert result["ok"] is False
    assert result["step"] == "branch"
    assert "not a git repository" in result["error"]

    resync = await mgr.resync()
    assert resync["ok"] is False
    assert "not a git repository" in resync["detail"]


# ── push_branch ──────────────────────────────────────────────────────────────


def test_backup_ref_name_derived_from_branch_and_sha() -> None:
    from ciao.local_session import backup_ref_name

    assert backup_ref_name("develop", "20b76d38abc") == "backup/develop-20b76d38abc"


def test_is_diverged_backup_only_matches_the_fallback_marker() -> None:
    from ciao.local_session import is_diverged_backup

    assert is_diverged_backup("[diverged-backup] branch 'main' diverged...") is True
    assert is_diverged_backup("pushed") is False
    assert is_diverged_backup("fatal: Authentication failed") is False
    assert is_diverged_backup("") is False
    assert is_diverged_backup(None) is False


async def test_push_branch_recovers_from_non_fast_forward_via_automerge(tmp_path: Path) -> None:
    from ciao.local_session import push_branch

    local, origin = _make_world(tmp_path, branch="main")
    # Advance origin remotely
    _advance_origin(tmp_path, origin, "remote-commit", branch="main")
    # Create local commit without pushing (causes non-fast-forward divergence)
    _write(local / "local-commit.md", "local work\n")
    _git(local, "add", "-A")
    _git(local, "commit", "-q", "-m", "local commit")

    # push_branch should detect non-fast-forward rejection, fetch + auto-merge origin/main, and push
    ok, detail = await push_branch(local, branch="main")
    assert ok is True
    assert (local / "remote-commit.md").exists()

    # Remote origin should now contain both local and remote commits
    check = tmp_path / "check"
    _git(tmp_path, "clone", "-q", str(origin), str(check))
    assert (check / "remote-commit.md").exists()
    assert (check / "local-commit.md").exists()


def _make_conflicting_world(tmp_path: Path, *, branch: str = "main") -> tuple[Path, Path]:
    """A world where origin and local diverge with a real content conflict."""
    local, origin = _make_world(tmp_path, branch=branch)
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    _identify(other)
    _write(other / "README.md", "remote version\n")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "remote readme")
    _git(other, "push", "-q")

    _write(local / "README.md", "local version\n")
    _git(local, "add", "-A")
    _git(local, "commit", "-q", "-m", "local readme")
    return local, origin


def _remote_refs(repo: Path, pattern: str) -> list[str]:
    out = _git(repo, "ls-remote", "origin", pattern)
    return [line for line in out.splitlines() if line.strip()]


async def test_push_branch_merge_conflict_falls_back_to_backup_ref(tmp_path: Path) -> None:
    from ciao.local_session import is_diverged_backup, push_branch

    local, _ = _make_conflicting_world(tmp_path, branch="main")
    local_sha = _git(local, "rev-parse", "HEAD")

    # push_branch attempts auto-merge, hits a real conflict, aborts cleanly,
    # and falls back to a per-commit backup ref instead of a bare error.
    ok, detail = await push_branch(local, branch="main")
    assert ok is True
    assert is_diverged_backup(detail)
    assert "conflict" in detail.lower()

    # Merge was aborted: no conflict markers, no MERGE_HEAD, still on main,
    # local's own commit (not a merge commit) is still HEAD.
    assert "<<<<<<<" not in (local / "README.md").read_text()
    assert "MERGE_HEAD" not in _git(local, "status")
    assert workspace_branch(local) == "main"
    assert _git(local, "rev-parse", "HEAD") == local_sha

    # The commit landed on origin under a backup ref, not on main.
    refs = _remote_refs(local, "refs/heads/backup/main-*")
    assert len(refs) == 1
    assert refs[0].split()[0] == local_sha


async def test_push_branch_backup_ref_fallback_is_idempotent(tmp_path: Path) -> None:
    """Repeated ticks against the same diverged HEAD must not pile up refs."""
    from ciao.local_session import push_branch

    local, _ = _make_conflicting_world(tmp_path, branch="main")

    ok1, detail1 = await push_branch(local, branch="main")
    ok2, detail2 = await push_branch(local, branch="main")
    assert ok1 is True and ok2 is True
    assert detail1 == detail2  # same HEAD, same backup ref, same message

    refs = _remote_refs(local, "refs/heads/backup/main-*")
    assert len(refs) == 1  # not two


async def test_push_branch_conflict_and_backup_ref_push_both_fail(tmp_path: Path, monkeypatch) -> None:
    from ciao.local_session import push_branch

    local, _ = _make_conflicting_world(tmp_path, branch="main")

    import ciao.local_session
    orig_git = ciao.local_session._git

    async def mock_git(workspace, *args, **kwargs):
        if args[:1] == ("push",) and "refs/heads/backup/" in " ".join(args):
            return 1, "", "fatal: could not read Username"
        return await orig_git(workspace, *args, **kwargs)

    monkeypatch.setattr(ciao.local_session, "_git", mock_git)

    ok, detail = await push_branch(local, branch="main")
    assert ok is False
    assert "conflict" in detail.lower()
    assert "backup-ref fallback also failed" in detail
    # Merge was still aborted cleanly even though the fallback push failed.
    assert "<<<<<<<" not in (local / "README.md").read_text()
    assert "MERGE_HEAD" not in _git(local, "status")


async def test_push_branch_skips_backup_ref_when_merge_abort_fails(tmp_path: Path, monkeypatch) -> None:
    """If merge --abort can't be confirmed clean, never attempt the fallback push."""
    from ciao.local_session import push_branch

    local, _ = _make_conflicting_world(tmp_path, branch="main")

    import ciao.local_session
    orig_git = ciao.local_session._git
    backup_push_calls = []

    async def mock_git(workspace, *args, **kwargs):
        if args[:2] == ("merge", "--abort"):
            return 1, "", "fatal: There is no merge to abort"
        if args[:1] == ("push",) and "refs/heads/backup/" in " ".join(args):
            backup_push_calls.append(args)
        return await orig_git(workspace, *args, **kwargs)

    monkeypatch.setattr(ciao.local_session, "_git", mock_git)

    ok, detail = await push_branch(local, branch="main")
    assert ok is False
    assert "may still be mid-merge" in detail
    assert backup_push_calls == []  # never risked a push while abort was unconfirmed


async def test_push_branch_auth_failure_unaffected_by_conflict_fallback(tmp_path: Path, monkeypatch) -> None:
    """Auth failures never match the non-fast-forward markers, so push_branch
    returns the raw error untouched — the branch-backup loop's existing
    dedup + auth-backoff handling (main.py) is unaffected by this change."""
    from ciao.local_session import is_diverged_backup, push_branch

    local, _ = _make_world(tmp_path)
    _write(local / "note.md", "x\n")

    import ciao.local_session
    orig_git = ciao.local_session._git

    async def mock_git(workspace, *args, **kwargs):
        if args[:2] == ("push", "-u"):
            return 1, "", "fatal: Authentication failed for 'https://example/repo.git'"
        return await orig_git(workspace, *args, **kwargs)

    monkeypatch.setattr(ciao.local_session, "_git", mock_git)

    ok, detail = await push_branch(local, branch="main")
    assert ok is False
    assert "authentication failed" in detail.lower()
    assert is_diverged_backup(detail) is False



# ── network timeouts ─────────────────────────────────────────────────────────


def _network_git_call_timeouts(module_path: Path) -> list[tuple[str, str]]:
    """Every ``_git(..., "push"|"fetch"|"pull", ...)`` call and its timeout arg.

    Read from the source rather than exercised at runtime: the failure being
    guarded against is a *hung* remote, which no unit test can produce without
    actually waiting for the ceiling it is checking.
    """
    import ast

    calls: list[tuple[str, str]] = []
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "_git"):
            continue
        verbs = [
            a.value
            for a in node.args
            if isinstance(a, ast.Constant) and isinstance(a.value, str)
        ]
        if not any(v in {"push", "fetch", "pull"} for v in verbs):
            continue
        timeout = next(
            (ast.unparse(kw.value) for kw in node.keywords if kw.arg == "timeout"),
            None,
        )
        calls.append((verbs[0], timeout or "<none>"))
    return calls


def test_network_git_calls_use_the_shared_ceiling() -> None:
    """A 10s ceiling made a momentary network stall look like a sync failure.

    Pin every network git call in the background sync module to
    ``GIT_NETWORK_TIMEOUT`` so a new call site cannot quietly reintroduce a
    tighter one.
    """
    import ciao.local_session

    assert ciao.local_session.GIT_NETWORK_TIMEOUT == 60.0

    calls = _network_git_call_timeouts(Path(ciao.local_session.__file__))
    assert calls, "no network git calls found in ciao.local_session"
    for verb, timeout in calls:
        assert timeout == "GIT_NETWORK_TIMEOUT", (
            f"ciao.local_session: git {verb} uses timeout={timeout}"
        )


# ── backoff classification (issue #470) ──────────────────────────────────────


def test_backoff_reason_none_for_an_ordinary_failure() -> None:
    assert backoff_reason("error: failed to push some refs") is None
    assert backoff_reason("") is None


def test_backoff_reason_flags_credential_failures() -> None:
    for detail in (
        "fatal: could not read Username for 'https://github.com'",
        "remote: Authentication failed for 'https://example.com'",
        "remote: Invalid username or token",
        "git@host: Permission denied (publickey).",
    ):
        assert backoff_reason(detail) == "auth", detail


def test_backoff_reason_flags_a_timeout_as_unreachable() -> None:
    """A timed-out push means an unreachable remote, not a transient blip.

    Before #470 this returned None, so the loop kept retrying every 30s
    forever against a host that was never going to answer.
    """
    assert backoff_reason(GIT_TIMEOUT_DETAIL) == "unreachable"
    assert backoff_reason("Git Command Timed Out") == "unreachable"


def test_backoff_reason_prefers_auth_over_timeout() -> None:
    """Credentials are the actionable half; the message must say so."""
    detail = f"authentication failed; {GIT_TIMEOUT_DETAIL}"
    assert backoff_reason(detail) == "auth"


# ── scoped commit / preflight (unattended backup) ────────────────────────────
#
# The manual path above stages the whole tree, which is what the person who
# pressed the button asked for. These commit a named scope instead, so the
# property under test is the negative one: whatever the scope does not name
# survives it. An unattended run that ate the user's staging, or swept a
# credential into a commit, would be worse than no backup at all.


def _install_config(workspace: Path) -> CiaoConfig:
    return CiaoConfig(
        pwa_auth_token="t",
        workspace_root=workspace,
        state_path=workspace / ".runtime" / "state.json",
        media_root=workspace / ".runtime" / "media",
    )


def _make_data_repo(tmp_path: Path) -> Path:
    """A repository holding nothing but durable data, so ``tracked_excluded`` is
    empty until a test puts something in it. The shared ``_make_world`` seeds a
    README, which the scope rightly reports as out of scope — correct, and noise
    in a test about something else."""
    repo = tmp_path / "data"
    _write(repo / "memory-vault" / "note.md", "a note\n")
    _git(repo, "init", "-q", "-b", "main")
    _identify(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _per_root_config(workspace: Path, *names: str) -> CiaoConfig:
    """A config for an install that has completed the per-workspace re-rooting.

    One agent root per registered workspace, so the vault lives at
    ``<install>/<name>/memory-vault`` rather than at the install root. Built
    through ``from_env`` because the workspace registry is only read there.
    """
    runtime = workspace / ".runtime"
    (runtime / "migration").mkdir(parents=True, exist_ok=True)
    mark_born_per_root(workspace, runtime, list(names))
    (runtime / "workspaces.json").write_text(
        json.dumps({"workspaces": [{"name": name, "vault_root": name} for name in names]}),
        encoding="utf-8",
    )
    reset_reroot_cache()
    return CiaoConfig.from_env(
        {
            "PWA_AUTH_TOKEN": "t",
            "CIAO_WORKSPACE": str(workspace),
            "CIAO_RUNTIME_ROOT": str(runtime),
            "CIAO_VAULT_ROOT": str(workspace / "memory-vault"),
        }
    )


async def test_commit_scoped_commits_only_the_named_paths(tmp_path: Path) -> None:
    local, _ = _make_world(tmp_path)
    _write(local / "memory-vault" / "note.md", "a note\n")
    # The user staged something of their own before the backup ran.
    _write(local / "wip.md", "unrelated\n")
    _git(local, "add", "wip.md")

    committed = await commit_scoped(
        local,
        branch="main",
        relpaths=["memory-vault/note.md"],
        message="backup 2026-09-28T00:00:00Z",
    )

    assert committed is True
    assert _git(local, "show", "--name-only", "--format=", "HEAD").split() == [
        "memory-vault/note.md"
    ]
    # The user's own staged file is still staged, still uncommitted, and the
    # unscoped working-tree file was never touched.
    assert _git(local, "status", "--porcelain") == "A  wip.md"


async def test_commit_scoped_is_a_no_op_on_a_clean_scoped_tree(tmp_path: Path) -> None:
    local, _ = _make_world(tmp_path)
    _write(local / "memory-vault" / "note.md", "a note\n")
    _git(local, "add", "-A")
    _git(local, "commit", "-q", "-m", "seed")
    before = _git(local, "rev-parse", "HEAD")
    # Dirty, but outside the scope: a backup must not manufacture a commit for it.
    _write(local / "wip.md", "unrelated\n")

    committed = await commit_scoped(
        local, branch="main", relpaths=["memory-vault/note.md"], message="backup"
    )

    assert committed is False
    assert _git(local, "rev-parse", "HEAD") == before
    assert _git(local, "status", "--porcelain") == "?? wip.md"


async def test_commit_scoped_records_a_deleted_note(tmp_path: Path) -> None:
    """Deleting a note is a durable change, so it has to be committed and not
    reported as nothing to do."""
    local, _ = _make_world(tmp_path)
    _write(local / "memory-vault" / "note.md", "a note\n")
    _git(local, "add", "-A")
    _git(local, "commit", "-q", "-m", "seed")
    (local / "memory-vault" / "note.md").unlink()

    committed = await commit_scoped(
        local, branch="main", relpaths=["memory-vault/note.md"], message="backup"
    )

    assert committed is True
    assert _git(local, "show", "--name-status", "--format=", "HEAD").split() == [
        "D", "memory-vault/note.md"
    ]


async def test_commit_scoped_reports_a_failed_stage_and_never_commits(
    tmp_path: Path, monkeypatch
) -> None:
    local, _ = _make_world(tmp_path)
    _write(local / "memory-vault" / "note.md", "a note\n")
    calls = _record_git(monkeypatch, fail={"add": (128, "", "fatal: unable to create index.lock")})

    with pytest.raises(GitOperationError) as raised:
        await commit_scoped(
            local, branch="main", relpaths=["memory-vault/note.md"], message="backup"
        )

    assert raised.value.step == "add"
    assert "index.lock" in raised.value.detail
    assert "commit" not in _verbs(calls)
    assert _git(local, "status", "--porcelain") != ""


async def test_commit_scoped_reports_a_failed_commit(
    tmp_path: Path, monkeypatch
) -> None:
    local, _ = _make_world(tmp_path)
    _write(local / "memory-vault" / "note.md", "a note\n")
    _record_git(monkeypatch, fail={"commit": (1, "", "fatal: empty commit message")})

    with pytest.raises(GitOperationError) as raised:
        await commit_scoped(
            local, branch="main", relpaths=["memory-vault/note.md"], message=""
        )

    assert raised.value.step == "commit"
    assert "empty commit" in raised.value.detail
    # The work stays staged, so the next run still sees it as pending.
    assert _git(local, "diff", "--cached", "--name-only") == "memory-vault/note.md"


async def test_commit_scoped_refuses_a_branch_it_did_not_examine(tmp_path: Path) -> None:
    """The caller is about to push ``branch``; committing on the one the
    checkout is actually on would be pushed later under the wrong name."""
    local, _ = _make_world(tmp_path, branch="feature-x")
    _write(local / "memory-vault" / "note.md", "a note\n")

    with pytest.raises(GitOperationError) as raised:
        await commit_scoped(
            local, branch="main", relpaths=["memory-vault/note.md"], message="backup"
        )

    assert raised.value.step == "branch"
    assert "feature-x" in raised.value.detail
    assert _git(local, "status", "--porcelain") != ""


async def test_commit_scoped_refuses_a_preexisting_index_lock(tmp_path: Path) -> None:
    local, _ = _make_world(tmp_path)
    _write(local / "memory-vault" / "note.md", "a note\n")
    lock_file = local / ".git" / "index.lock"
    lock_file.write_text("", encoding="utf-8")

    with pytest.raises(GitOperationError) as raised:
        await commit_scoped(
            local, branch="main", relpaths=["memory-vault/note.md"], message="backup"
        )

    assert raised.value.step == PREFLIGHT_STEP
    assert "index lock" in raised.value.detail
    assert lock_file.exists()


async def test_commit_scoped_ignores_a_path_git_cannot_match(tmp_path: Path) -> None:
    """A status that has moved on since it was read leaves pathspecs that
    match nothing, and `git add` treats that as an error rather than a no-op.
    A file deleted in the meantime is a different case: it is still a real
    change and is committed."""
    local, _ = _make_world(tmp_path)
    _write(local / "memory-vault" / "note.md", "a note\n")

    committed = await commit_scoped(
        local,
        branch="main",
        relpaths=["memory-vault/never-existed.md", "memory-vault/note.md"],
        message="backup",
    )

    assert committed is True
    assert _git(local, "show", "--name-only", "--format=", "HEAD").split() == [
        "memory-vault/note.md"
    ]


async def test_commit_scoped_refuses_a_pathspec_that_escapes_the_tree(
    tmp_path: Path,
) -> None:
    """The confinement is in the function, not only in the scope that calls
    it: a `..` pathspec is dropped rather than handed to git."""
    local, _ = _make_world(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("not mine\n", encoding="utf-8")

    committed = await commit_scoped(
        local, branch="main", relpaths=["../outside.md", "/etc/hosts"], message="backup"
    )

    assert committed is False
    assert _git(local, "log", "--oneline").count("\n") == 0  # only the seed commit


async def test_preflight_scoped_reports_a_tracked_env_as_a_blocker(
    tmp_path: Path,
) -> None:
    """`.gitignore` never un-commits anything, so a credential that is already
    tracked has to be raised: the manual sync path still stages the whole tree
    and would carry it off the machine."""
    local = _make_data_repo(tmp_path)
    config = _install_config(local)
    _write(local / ".env", "API_KEY=secret\n")
    _git(local, "add", "-f", ".env")
    _git(local, "commit", "-q", "-m", "oops")

    result = await preflight_scoped(config, local)

    assert result["ok"] is False
    assert result["tracked_excluded"] == [".env"]
    assert any(".env" in blocker for blocker in result["blockers"])


async def test_preflight_scoped_scans_the_eligible_set_only(tmp_path: Path) -> None:
    """One scanner, one set of rules — and it is never handed a file the
    commit could not contain anyway."""
    local = _make_data_repo(tmp_path)
    config = _install_config(local)
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQ...\n-----END RSA PRIVATE KEY-----\n"
    # Outside the scope: refused before it is ever opened, so its contents are
    # nobody's business in a backup.
    _write(local / ".env", pem)
    # Inside the scope: this one is about to be committed.
    _write(local / "memory-vault" / "cert.pem", pem)

    result = await preflight_scoped(config, local)

    assert result["eligible"] == ["memory-vault/cert.pem"]
    assert result["excluded"] == [".env"]
    assert any("cert.pem" in blocker for blocker in result["blockers"])
    assert not any(".env" in blocker for blocker in result["blockers"])


async def test_preflight_scoped_stages_nothing(tmp_path: Path) -> None:
    """It answers a question; it must not change the answer's subject. A
    preflight that staged the tree would be the backup it is meant to vet."""
    local = _make_data_repo(tmp_path)
    config = _install_config(local)
    _write(local / "memory-vault" / "second.md", "another\n")

    result = await preflight_scoped(config, local)

    assert result["ok"] is True
    assert result["eligible"] == ["memory-vault/second.md"]
    assert result["tracked_excluded"] == []
    assert _git(local, "diff", "--cached", "--name-only") == ""


async def test_a_tests_folder_inside_a_workspace_vault_is_not_a_fixture(
    tmp_path: Path,
) -> None:
    """The fixture exemption is keyed on the first path segment, which was
    correct while the vault sat at the workspace root and is not after the
    re-rooting: `<install>/<workspace>/memory-vault/tests/` is a folder of
    notes, and a credential filed in it has to be reported by both preflights.
    """
    local = _make_data_repo(tmp_path)
    config = _per_root_config(local, "personal")
    _write(
        local / "personal" / "memory-vault" / "tests" / "server.key",
        "-----BEGIN PRIVATE KEY-----\nnope\n",
    )
    mgr = LocalSessionManager(workspace=local, runtime_root=tmp_path / "rt")

    scoped = await preflight_scoped(config, local)
    manual = await mgr.preflight()

    # In scope here (the vault belongs to the personal agent root), so the
    # scanner has to see it; and it does in the manual preflight too.
    assert scoped["eligible"] == ["personal/memory-vault/tests/server.key"]
    assert any("server.key" in blocker for blocker in scoped["blockers"])
    assert any("server.key" in blocker for blocker in manual["blockers"])
