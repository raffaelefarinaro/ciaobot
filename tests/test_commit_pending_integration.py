"""End-to-end tests for ``commit_pending`` against real temporary repositories.

``commit_pending`` used to ignore every return code: a failed ``add``, a failed
``commit`` (missing identity, a full disk, a hostile pre-commit hook) all
reported "created a commit", and the flows above it went on to push as if the
work were safely recorded. These tests run real git in throwaway repos under
tmp_path with a local bare origin and no network, so the claims are about git's
actual behaviour rather than about a mocked return code.

The lock/lock-file tests belong to ``tests/test_git_mutation.py``; this file is
about what ``commit_pending`` itself does to the checkout.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ciao.local_session import (
    GitOperationError,
    commit_pending,
    sync_branch,
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


def _identify(repo: Path) -> None:
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "user.email", "t@e.com")


def _repo(path: Path, *, identify: bool = True) -> Path:
    """A one-commit repository. ``identify=False`` leaves git unable to commit."""
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    if identify:
        _identify(path)
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(path, "add", "README.md")
    if identify:
        _git(path, "commit", "-q", "-m", "seed")
    return path


@pytest.fixture
def _no_inherited_git_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Make the repository genuinely unable to commit, for the identity test.

    ``commit_pending``'s subprocess inherits this process's environment, so a
    ``GIT_AUTHOR_NAME`` left in the shell would make the identity-failure test
    pass for the wrong reason. Deleting the variables is not enough: git
    auto-derives an identity from the hostname, so ``user.useConfigOnly`` is
    what actually removes the fallback.
    """
    for var in (
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL", "EMAIL",
    ):
        monkeypatch.delenv(var, raising=False)
    config = tmp_path / "gitconfig-no-identity"
    config.write_text("[user]\n\tuseConfigOnly = true\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def _commits(repo: Path) -> list[str]:
    return _git(repo, "log", "--format=%s").splitlines()


# ── the two normal outcomes ──────────────────────────────────────────────────


async def test_clean_tree_creates_no_commit(tmp_path: Path) -> None:
    """Nothing pending must not manufacture an empty commit.

    The old check asked ``git status --porcelain``; the index is now the
    authority, so a tree that staging leaves unchanged cannot produce a commit
    even when the worktree has churn in it.
    """
    repo = _repo(tmp_path / "repo")
    before = _head(repo)

    assert await commit_pending(repo, branch="main") is False

    assert _head(repo) == before
    assert _commits(repo) == ["seed"]


async def test_ignored_only_tree_creates_no_commit(tmp_path: Path) -> None:
    """Ignored runtime churn is not pending work.

    A workspace with ``.runtime/`` ignored is the common case, and a commit
    built from "the worktree looks dirty" would either be empty or would scoop
    up state the user never meant to version.
    """
    repo = _repo(tmp_path / "repo")
    (repo / ".gitignore").write_text(".runtime/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore runtime")
    before = _head(repo)
    (repo / ".runtime").mkdir()
    (repo / ".runtime" / "job_runs.jsonl").write_text("{}\n", encoding="utf-8")

    assert await commit_pending(repo, branch="main") is False

    assert _head(repo) == before


async def test_pending_files_are_actually_committed(tmp_path: Path) -> None:
    """The whole point: real work, in one commit, on the current branch."""
    repo = _repo(tmp_path / "repo")
    (repo / "README.md").write_text("edited\n", encoding="utf-8")
    (repo / "memory-vault").mkdir()
    (repo / "memory-vault" / "note.md").write_text("a note\n", encoding="utf-8")
    before = _head(repo)

    assert await commit_pending(repo, branch="main") is True

    assert _head(repo) != before
    assert _commits(repo)[0] == "main session commit " + _commits(repo)[0].split(" ", 3)[-1]
    assert _git(repo, "status", "--porcelain") == ""
    assert _git(repo, "show", "--name-only", "--format=", "HEAD").split() == [
        "README.md", "memory-vault/note.md",
    ]
    assert workspace_branch(repo) == "main"


# ── failures are reported, not swallowed ─────────────────────────────────────


async def test_missing_identity_surfaces_a_commit_failure(
    tmp_path: Path, _no_inherited_git_identity: None
) -> None:
    """A repo git cannot commit in reported the old way: as success.

    Identity is the everyday shape of this (a fresh workspace, a container
    without ``user.email``), and it used to return True with nothing committed —
    which is how a session's work could be reported saved and then lost.
    """
    repo = _repo(tmp_path / "repo", identify=False)
    _git(repo, "commit", "-q", "-m", "seed", "--author", "T <t@e.com>")
    before = _head(repo)
    (repo / "note.md").write_text("work\n", encoding="utf-8")

    with pytest.raises(GitOperationError) as excinfo:
        await commit_pending(repo, branch="main")

    assert excinfo.value.step == "commit"
    assert "email" in excinfo.value.detail.lower() or "identity" in excinfo.value.detail.lower()
    # Nothing was recorded: the staged work is still there to retry with.
    assert _head(repo) == before
    assert _git(repo, "status", "--porcelain") != ""


async def test_a_refused_preexisting_index_lock_never_touches_it(
    tmp_path: Path,
) -> None:
    """The user's own git owns that lock file.

    Removing it is the tempting shortcut and the destructive one: on a shared
    volume the lock may belong to a real ``git commit`` on another machine, and
    deleting it lets two git processes write the same index.
    """
    repo = _repo(tmp_path / "repo")
    (repo / "note.md").write_text("work\n", encoding="utf-8")
    lock_file = repo / ".git" / "index.lock"
    lock_file.write_text("", encoding="utf-8")
    before = _head(repo)

    with pytest.raises(GitOperationError) as excinfo:
        await commit_pending(repo, branch="main")

    assert excinfo.value.step == "preflight"
    assert "index lock" in excinfo.value.detail
    assert lock_file.exists(), "a foreign index.lock must never be removed"
    assert lock_file.read_text(encoding="utf-8") == ""
    assert _head(repo) == before


async def test_an_in_progress_merge_is_refused_not_aborted(tmp_path: Path) -> None:
    """A merge Ciaobot did not start belongs to whoever started it."""
    repo = _repo(tmp_path / "repo")
    (repo / ".git" / "MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")

    with pytest.raises(GitOperationError) as excinfo:
        await commit_pending(repo, branch="main")

    assert excinfo.value.step == "preflight"
    assert "merge is in progress" in excinfo.value.detail
    assert (repo / ".git" / "MERGE_HEAD").exists()


# ── the sync flow around it ──────────────────────────────────────────────────


async def test_sync_still_works_end_to_end_against_a_local_bare_remote(
    tmp_path: Path,
) -> None:
    """Serialization and the stricter commit checks must not cost the feature
    its job: a pending note is committed, pushed, and readable from origin."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare", "-b", "main")
    seed = _repo(tmp_path / "seed")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "-u", "origin", "main")
    local = tmp_path / "local"
    _git(tmp_path, "clone", "-q", str(origin), str(local))
    _identify(local)
    (local / "memory-vault").mkdir()
    (local / "memory-vault" / "note.md").write_text("a note\n", encoding="utf-8")

    result = await sync_branch(local, branch="main")

    assert result["ok"] is True
    assert result["merged"] is True
    assert result["pushed"] is True
    check = tmp_path / "check"
    _git(tmp_path, "clone", "-q", str(origin), str(check))
    assert (check / "memory-vault" / "note.md").read_text(encoding="utf-8") == "a note\n"
