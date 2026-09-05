"""Tests for ciao/local_session.py: the current-branch git sync flow.

Ciaobot never creates or switches local branches: it works on whatever branch
the workspace checkout is on and syncs it via ``sync_branch`` (commit + pull +
push; conflict -> hand off to a chat). Non-git workspaces skip gracefully. The
safety rule the tests pin down: never discard local work, never touch other
branches.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from types import SimpleNamespace

from ciao.local_session import (
    LocalSessionManager,
    has_origin_remote,
    is_git_repo,
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
    tighter one. (``ciao.git_sync`` is deliberately excluded — it runs before
    the server binds and keeps its own shorter ceiling; see
    ``test_startup_sync_keeps_its_own_shorter_ceiling``.)
    """
    import ciao.local_session

    assert ciao.local_session.GIT_NETWORK_TIMEOUT == 60.0

    calls = _network_git_call_timeouts(Path(ciao.local_session.__file__))
    assert calls, "no network git calls found in ciao.local_session"
    for verb, timeout in calls:
        assert timeout == "GIT_NETWORK_TIMEOUT", (
            f"ciao.local_session: git {verb} uses timeout={timeout}"
        )


def test_startup_sync_keeps_its_own_shorter_ceiling() -> None:
    """Startup sync is awaited before the server binds, so it is not a loop.

    Giving it the 60s background ceiling would make an unreachable remote hold
    a cold start for a minute on a blank app; the next backup tick syncs
    anyway. Pinned so a future "share the constant" tidy-up cannot quietly
    6x the worst-case boot.
    """
    import ciao.git_sync

    assert ciao.git_sync.GIT_STARTUP_TIMEOUT == 10.0

    calls = _network_git_call_timeouts(Path(ciao.git_sync.__file__))
    assert calls, "no network git calls found in ciao.git_sync"
    for verb, timeout in calls:
        assert timeout == "GIT_STARTUP_TIMEOUT", (
            f"ciao.git_sync: git {verb} uses timeout={timeout}"
        )
