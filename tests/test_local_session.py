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
