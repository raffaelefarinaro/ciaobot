"""Workspace and vault repos store exact bytes on every OS (#696 C9).

Git for Windows ships ``core.autocrlf=true`` in its system config, which made
the engine store a CRLF note as an LF blob. These tests force that setting the
way such a machine has it, so the guard runs on macOS and Linux CI too.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ciao.cli import ensure_workspace_git
from ciao.local_session import commit_pending


@pytest.fixture
def autocrlf_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """A git whose machine config says ``core.autocrlf=true``, as Git for Windows does.

    Environment config overrides every config file and is overridden only by an
    explicit ``git -c``, which is exactly what the engine must pass.
    """
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "T")
    _git(path, "config", "user.email", "t@e.com")
    (path / "README.md").write_bytes(b"seed\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "seed")
    return path


async def test_a_crlf_note_is_committed_byte_for_byte(tmp_path: Path, autocrlf_machine: None) -> None:
    repo = _repo(tmp_path / "repo")
    note = repo / "note.md"
    note.write_bytes(b"line one\r\nline two\r\n")

    assert await commit_pending(repo, branch="main") is True

    blob = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-p", "HEAD:note.md"],
        check=True, capture_output=True,
    ).stdout
    assert blob == note.read_bytes()
    # And the engine's own view agrees nothing is left to commit.
    assert await commit_pending(repo, branch="main") is False


def test_a_new_workspace_repo_stores_exact_bytes(tmp_path: Path, autocrlf_machine: None) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_bytes(b"# Agents\r\n")

    ensure_workspace_git(workspace)

    assert _git(workspace, "config", "--local", "--get", "core.autocrlf").strip() == "false"
    blob = subprocess.run(
        ["git", "-C", str(workspace), "cat-file", "-p", "HEAD:AGENTS.md"],
        check=True, capture_output=True,
    ).stdout
    assert blob == b"# Agents\r\n"


def test_an_existing_workspace_repo_is_given_the_setting(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "workspace")

    ensure_workspace_git(repo)

    assert _git(repo, "config", "--local", "--get", "core.autocrlf").strip() == "false"


def test_a_repo_that_chose_its_own_setting_keeps_it(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "workspace")
    _git(repo, "config", "--local", "core.autocrlf", "input")

    ensure_workspace_git(repo)

    assert _git(repo, "config", "--local", "--get", "core.autocrlf").strip() == "input"
