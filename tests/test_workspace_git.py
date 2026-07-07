from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ciao.cli import ensure_workspace_git, setup_workspace


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True
    )


def test_ensure_workspace_git_initializes_fresh_dir(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "CLAUDE.md").write_text("hi\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")

    ensure_workspace_git(root)

    assert _git(root, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
    assert _git(root, "branch", "--show-current").stdout.strip() == "main"

    log = _git(root, "log", "--oneline")
    assert log.returncode == 0
    assert "Initialize Ciaobot workspace" in log.stdout
    assert len(log.stdout.strip().splitlines()) == 1

    gitignore = (root / ".gitignore").read_text(encoding="utf-8")
    for entry in (".env", ".runtime/", ".claude/", "*.log"):
        assert entry in gitignore.splitlines()

    tracked = _git(root, "ls-files").stdout.splitlines()
    assert "CLAUDE.md" in tracked
    assert ".gitignore" in tracked
    assert ".env" not in tracked


def test_ensure_workspace_git_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    ensure_workspace_git(root)
    ensure_workspace_git(root)

    log = _git(root, "log", "--oneline")
    assert len(log.stdout.strip().splitlines()) == 1


def test_ensure_workspace_git_leaves_existing_repo_alone(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    assert _git(root.parent, "init", "-b", "trunk", str(root)).returncode == 0
    (root / ".gitignore").write_text("# mine\nnode_modules/\n.env\n", encoding="utf-8")

    ensure_workspace_git(root)

    # No commit was created and the branch is untouched.
    assert _git(root, "rev-parse", "HEAD").returncode != 0
    assert _git(root, "branch", "--show-current").stdout.strip() == "trunk"

    gitignore = (root / ".gitignore").read_text(encoding="utf-8")
    lines = gitignore.splitlines()
    assert lines[:3] == ["# mine", "node_modules/", ".env"]
    assert lines.count(".env") == 1
    for entry in (".runtime/", ".claude/", "*.log"):
        assert entry in lines


def test_ensure_workspace_git_skips_without_git_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr("ciao.cli.shutil.which", lambda name: None)

    def fail_run(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("git must not be invoked when the binary is missing")

    monkeypatch.setattr("ciao.cli.subprocess.run", fail_run)

    ensure_workspace_git(root)

    assert not (root / ".git").exists()
    assert "skipping workspace git init" in capsys.readouterr().err


def test_setup_workspace_creates_git_repo_without_committing_env(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "workspace"
    setup_workspace(
        ws,
        launch_agents_dir=tmp_path / "LaunchAgents",
        app_dir=tmp_path / "Applications",
    )

    assert (ws / ".env").is_file()
    assert _git(ws, "branch", "--show-current").stdout.strip() == "main"
    log = _git(ws, "log", "--oneline")
    assert "Initialize Ciaobot workspace" in log.stdout

    tracked = _git(ws, "ls-files").stdout.splitlines()
    assert ".env" not in tracked
    assert not any(path.startswith(".runtime/") for path in tracked)
    assert not any(path.startswith(".claude/") for path in tracked)
    assert "CLAUDE.md" in tracked
    assert _git(ws, "status", "--porcelain").stdout.strip() == ""
