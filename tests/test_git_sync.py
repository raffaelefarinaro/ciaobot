"""Tests for ciao.git_sync (startup pull + auto-commit semantics)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ciao.git_sync import sync_workspace


def _git_env(repo: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@e.com",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@e.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "HOME": str(repo),
        }
    )
    return env


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, env=_git_env(repo)
    )


def _init_repo(repo: Path, *, with_remote: bool = False) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    if with_remote:
        bare = repo.parent / f"{repo.name}-origin"
        bare.mkdir()
        _git(bare, "init", "-q", "--bare", "-b", "main")
        _git(repo, "remote", "add", "origin", str(bare))
        _git(repo, "push", "-q", "-u", "origin", "main")


async def test_startup_sync_commits_untracked_files(tmp_path: Path) -> None:
    """The auto-commit must stage brand-new files, not only edited ones.

    `git add -u` only stages paths git already tracks, so notes and project
    folders never added by hand sat out of every automatic backup forever
    (the 2026-08-30 work-notes gap): everything reported the run a success
    while the files were simply never committed.
    """
    repo = tmp_path / "ws"
    _init_repo(repo, with_remote=True)
    # One edited tracked file and one brand-new untracked file.
    (repo / "README.md").write_text("edited\n", encoding="utf-8")
    note = repo / "journal" / "2026-08-30.md"
    note.parent.mkdir()
    note.write_text("# the day\n", encoding="utf-8")

    await sync_workspace(repo)

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo), check=True, capture_output=True, text=True, env=_git_env(repo),
    ).stdout.strip()
    assert status == "", f"startup sync left files uncommitted: {status!r}"
    # Both changes are in the commit.
    shown = subprocess.run(
        ["git", "show", "--stat", "--format=", "HEAD"],
        cwd=str(repo), check=True, capture_output=True, text=True, env=_git_env(repo),
    ).stdout
    assert "README.md" in shown
    assert "journal/2026-08-30.md" in shown


async def test_startup_sync_never_stages_gitignored_paths(tmp_path: Path) -> None:
    """`add -A` respects .gitignore: runtime scratch stays out of the repo."""
    repo = tmp_path / "ws"
    _init_repo(repo, with_remote=True)
    (repo / ".gitignore").write_text(".runtime/\nsecrets/\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "gitignore")
    _git(repo, "push", "-q")
    (repo / ".runtime").mkdir()
    (repo / ".runtime" / "state.json").write_text("{}", encoding="utf-8")
    (repo / "secrets").mkdir()
    (repo / "secrets" / "key.pem").write_text("not a real key\n", encoding="utf-8")

    await sync_workspace(repo)

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo), check=True, capture_output=True, text=True, env=_git_env(repo),
    ).stdout.strip()
    assert status == "", status
    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=str(repo), check=True, capture_output=True, text=True, env=_git_env(repo),
    ).stdout.splitlines()
    assert not any(".runtime/" in f or f.startswith("secrets/") for f in tracked)


async def test_startup_sync_repairs_missing_secret_ignore(tmp_path: Path) -> None:
    repo = tmp_path / "ws"
    _init_repo(repo, with_remote=True)
    (repo / "secrets").mkdir()
    (repo / "secrets" / "refresh-token.json").write_text("token\n", encoding="utf-8")

    result = await sync_workspace(repo)

    assert result is None
    assert "secrets/" in (repo / ".gitignore").read_text(encoding="utf-8")
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=str(repo), check=True, capture_output=True, text=True, env=_git_env(repo)
    ).stdout.splitlines()
    assert not any(path.startswith("secrets/") for path in tracked)


async def test_startup_sync_clean_tree_reports_no_commit(tmp_path: Path) -> None:
    repo = tmp_path / "ws"
    _init_repo(repo, with_remote=True)
    await sync_workspace(repo)
    log = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=str(repo), check=True, capture_output=True, text=True, env=_git_env(repo),
    ).stdout.strip()
    # The first startup repairs the protective ignore file and commits it.
    assert log == "2"
