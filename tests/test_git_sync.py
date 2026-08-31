"""Tests for ciao.git_sync (startup pull + auto-commit semantics)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ciao.git_sync import (
    WORKSPACE_GITIGNORE_ENTRIES,
    _protected_path,
    sync_workspace,
)


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


async def test_startup_sync_rejects_modified_tracked_secret(tmp_path: Path) -> None:
    repo = tmp_path / "ws"
    _init_repo(repo, with_remote=True)
    secret = repo / "secrets" / "token.json"
    secret.parent.mkdir()
    secret.write_text("old\n", encoding="utf-8")
    _git(repo, "add", "-f", "secrets/token.json")
    _git(repo, "commit", "-q", "-m", "fixture secret")
    secret.write_text("new\n", encoding="utf-8")

    result = await sync_workspace(repo)

    assert result is not None
    assert "secrets/token.json" in result
    shown = subprocess.run(
        ["git", "show", "--format=", "HEAD"], cwd=str(repo), check=True,
        capture_output=True, text=True, env=_git_env(repo),
    ).stdout
    assert "+new\n" not in shown


async def test_startup_sync_rejects_nested_tracked_env_file(tmp_path: Path) -> None:
    repo = tmp_path / "ws"
    _init_repo(repo, with_remote=True)
    env_file = repo / "config" / ".env.local"
    env_file.parent.mkdir()
    env_file.write_text("old\n", encoding="utf-8")
    _git(repo, "add", "-f", "config/.env.local")
    _git(repo, "commit", "-q", "-m", "fixture env")
    env_file.write_text("new\n", encoding="utf-8")

    result = await sync_workspace(repo)

    assert result is not None
    assert "config/.env.local" in result


@pytest.mark.parametrize("path", [".npmrc", ".netrc", "config/.pypirc"])
def test_protected_config_names_are_rejected(path: str) -> None:
    assert _protected_path(path) is True


async def test_startup_sync_rejects_rename_into_protected_location(tmp_path: Path) -> None:
    """A tracked file renamed into `secrets/` must not be auto-committed.

    Porcelain rename output carries both sides; the destination basename
    alone (``token.json``) is not protected, so the rename side must be
    parsed and checked too.
    """
    repo = tmp_path / "ws"
    _init_repo(repo, with_remote=True)
    source = repo / "normal.json"
    source.write_text("old\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    (repo / "secrets").mkdir()
    _git(repo, "mv", "normal.json", "secrets/token.json")

    result = await sync_workspace(repo)

    assert result is not None
    assert "secrets/token.json" in result
    shown = subprocess.run(
        ["git", "show", "--format=", "HEAD"], cwd=str(repo), check=True,
        capture_output=True, text=True, env=_git_env(repo),
    ).stdout
    assert "secrets/token.json" not in shown


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


async def test_startup_sync_repairs_every_credential_ignore(tmp_path: Path) -> None:
    """The repair list must cover the same paths workspace setup writes.

    It used to be a three-entry subset (.env, secrets/, .runtime/), so a
    workspace that predated the setup guard — exactly the case this repair
    exists for — kept `.claude/`, `.codex/` and friends untracked-but-not-
    ignored. Once startup sync moved from `git add -u` to `git add -A`, the
    next sync staged and pushed `.claude/.credentials.json` and
    `.codex/auth.json` to the workspace remote.
    """
    repo = tmp_path / "ws"
    _init_repo(repo, with_remote=True)
    for directory, name in (
        (".claude", ".credentials.json"),
        (".opencode", "state.json"),
        (".agents", "notes.md"),
    ):
        (repo / directory).mkdir()
        (repo / directory / name).write_text("secret\n", encoding="utf-8")
    (repo / "opencode.json").write_text("{}\n", encoding="utf-8")
    (repo / "run.log").write_text("noise\n", encoding="utf-8")

    await sync_workspace(repo)

    ignored = (repo / ".gitignore").read_text(encoding="utf-8")
    for entry in WORKSPACE_GITIGNORE_ENTRIES:
        assert entry in ignored, entry
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=str(repo), check=True, capture_output=True, text=True, env=_git_env(repo)
    ).stdout.splitlines()
    assert [
        path
        for path in tracked
        if path.startswith((".claude/", ".opencode/", ".agents/"))
        or path in {"opencode.json", "run.log"}
    ] == []


def test_setup_and_sync_share_one_ignore_list() -> None:
    """Two lists that must agree are one list."""
    from ciao import cli

    assert cli._WORKSPACE_GITIGNORE_ENTRIES is WORKSPACE_GITIGNORE_ENTRIES


@pytest.mark.parametrize(
    "path",
    [
        "sub/secrets/token.json",
        "deep/nested/secrets/api-key",
        ".ssh/config",
        "home/.aws/credentials",
        "keys/id_rsa",
        "keys/id_ed25519",
    ],
)
def test_protected_paths_are_caught_anywhere_in_the_tree(path: str) -> None:
    """`add -A` sweeps untracked files, so a root-prefix check is not enough."""
    assert _protected_path(path) is True


@pytest.mark.parametrize(
    "path", ["notes/secretive-plan.md", "src/main.py", "docs/.env.example"]
)
def test_ordinary_paths_stay_committable(path: str) -> None:
    assert _protected_path(path) is False


@pytest.mark.parametrize(
    "path",
    [
        ".codex/auth.json",
        ".claude/.credentials.json",
        ".opencode/auth.json",
        "old/.codex/auth.json",
    ],
)
def test_provider_credentials_are_protected_even_when_tracked(path: str) -> None:
    """.gitignore does not hide files git already tracks.

    An older workspace that committed a provider token before any of these
    guards existed still reports it as modified on every sync, and `add -A`
    would stage and push it. Matching the credential filename catches it
    regardless of which provider directory it sits in, or whether that
    directory is ignored at all - `.codex/` deliberately is not.
    """
    assert _protected_path(path) is True


def test_codex_is_not_in_the_ignore_list() -> None:
    """Codex is retired: sync_skills only prunes what older versions left."""
    assert ".codex/" not in WORKSPACE_GITIGNORE_ENTRIES


async def test_untracked_credential_in_an_unignored_dir_blocks_the_commit(
    tmp_path: Path,
) -> None:
    """Codex is off the ignore list, so its stale files reach the guard.

    git collapses a wholly untracked directory to one `?? .codex/` entry, which
    hid every filename inside it from `_protected_path` — the sync then staged
    the token it was meant to refuse. Listing untracked files individually is
    what makes the filename check reachable.
    """
    repo = tmp_path / "ws"
    _init_repo(repo, with_remote=True)
    (repo / ".codex").mkdir()
    (repo / ".codex" / "auth.json").write_text('{"token": "x"}\n', encoding="utf-8")

    result = await sync_workspace(repo)

    assert result is not None
    assert ".codex/auth.json" in result
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=str(repo), check=True, capture_output=True, text=True, env=_git_env(repo)
    ).stdout.splitlines()
    assert ".codex/auth.json" not in tracked


async def test_a_negation_that_re_includes_a_credential_is_repaired(
    tmp_path: Path,
) -> None:
    """A guard line being present is not the same as the path being ignored.

    `.claude/` followed by `!.claude/` reads as fully guarded to a membership
    check — the line is right there — while git reports the token inside as
    unignored, so `add -A` stages it and the backup loop pushes it to origin.
    Asking git for the effective status is what closes this; the repair works
    because gitignore resolves by last match, so the re-appended entry
    outranks the negation.

    (A negation *inside* an excluded directory, `.claude/` plus
    `!.claude/.credentials.json`, is not a bypass: git cannot re-include a
    file whose parent directory is excluded. The un-excluded directory is.)
    """
    repo = tmp_path / "ws"
    _init_repo(repo, with_remote=True)
    (repo / ".gitignore").write_text(
        ".env\nsecrets/\n.runtime/\n.claude/\n!.claude/\n"
        ".agents/\n.opencode/\nopencode.json\n*.log\n",
        encoding="utf-8",
    )
    (repo / ".claude").mkdir()
    (repo / ".claude" / ".credentials.json").write_text("{}\n", encoding="utf-8")

    await sync_workspace(repo)

    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", ".claude/.credentials.json"],
        cwd=str(repo), capture_output=True, env=_git_env(repo),
    ).returncode
    assert ignored == 0, "the token is still not ignored after the repair"
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=str(repo), check=True, capture_output=True, text=True, env=_git_env(repo)
    ).stdout.splitlines()
    assert ".claude/.credentials.json" not in tracked


async def test_an_intact_gitignore_is_not_rewritten_every_startup(
    tmp_path: Path,
) -> None:
    """The effective check must not append a duplicate on every boot."""
    repo = tmp_path / "ws"
    _init_repo(repo, with_remote=True)
    (repo / ".gitignore").write_text(
        "\n".join(WORKSPACE_GITIGNORE_ENTRIES) + "\n", encoding="utf-8"
    )

    await sync_workspace(repo)
    first = (repo / ".gitignore").read_text(encoding="utf-8")
    await sync_workspace(repo)

    assert (repo / ".gitignore").read_text(encoding="utf-8") == first


@pytest.mark.parametrize(
    "path", [".envrc", "project/.envrc", ".envrc.local", ".direnv/dump"]
)
def test_direnv_credentials_are_protected(path: str) -> None:
    """direnv exports API keys from `.envrc`, which `add -A` sweeps up.

    The `.env` / `.env.*` rules do not reach it — the name has no dot before
    `rc` — so an untracked `.envrc` was committed and pushed like any other
    new file.
    """
    assert _protected_path(path) is True
