from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ciao.cli import ensure_vault_git, ensure_workspace_git, setup_workspace


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


def test_ensure_vault_git_initializes_fresh_vault(tmp_path: Path) -> None:
    vault = tmp_path / "brain"
    vault.mkdir()
    (vault / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")

    ensure_vault_git(vault)

    assert _git(vault, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
    assert _git(vault, "branch", "--show-current").stdout.strip() == "main"

    log = _git(vault, "log", "--oneline")
    assert "Initialize Ciaobot vault" in log.stdout
    assert len(log.stdout.strip().splitlines()) == 1

    gitignore = (vault / ".gitignore").read_text(encoding="utf-8")
    for entry in (".DS_Store", ".obsidian/workspace*"):
        assert entry in gitignore.splitlines()

    tracked = _git(vault, "ls-files").stdout.splitlines()
    assert "MEMORY.md" in tracked
    assert ".gitignore" in tracked


def test_ensure_vault_git_appends_gitignore_to_existing_vault_repo(tmp_path: Path) -> None:
    vault = tmp_path / "notes"
    vault.mkdir()
    assert _git(vault.parent, "init", "-b", "trunk", str(vault)).returncode == 0
    (vault / ".gitignore").write_text("# mine\n.DS_Store\n", encoding="utf-8")

    ensure_vault_git(vault)

    # No commit was created and the branch is untouched.
    assert _git(vault, "rev-parse", "HEAD").returncode != 0
    assert _git(vault, "branch", "--show-current").stdout.strip() == "trunk"

    lines = (vault / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["# mine", ".DS_Store"]
    assert lines.count(".DS_Store") == 1
    assert ".obsidian/workspace*" in lines


def test_ensure_vault_git_leaves_nested_vault_untouched(tmp_path: Path) -> None:
    """A vault inside the workspace repo (the default layout) is never
    double-initialized and gets no nested .gitignore."""
    ws = tmp_path / "ws"
    vault = ws / "memory-vault"
    vault.mkdir(parents=True)
    (vault / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
    ensure_workspace_git(ws)

    ensure_vault_git(vault)

    assert not (vault / ".git").exists()
    assert not (vault / ".gitignore").exists()
    assert _git(ws, "status", "--porcelain").stdout.strip() == ""


def test_ensure_vault_git_skips_without_git_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    vault = tmp_path / "brain"
    vault.mkdir()
    monkeypatch.setattr("ciao.cli.shutil.which", lambda name: None)

    def fail_run(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("git must not be invoked when the binary is missing")

    monkeypatch.setattr("ciao.cli.subprocess.run", fail_run)

    ensure_vault_git(vault)

    assert not (vault / ".git").exists()
    assert "skipping vault git init" in capsys.readouterr().err


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

    # Default layout: the vault lives inside the workspace repo and is
    # tracked there — no second repo is created.
    assert not (ws / "memory-vault" / ".git").exists()
    assert "memory-vault/MEMORY.md" in tracked


def test_setup_workspace_git_inits_external_vault(tmp_path: Path) -> None:
    """A vault outside the workspace gets its own repo with the vault
    .gitignore, and .env records the absolute vault path."""
    ws = tmp_path / "workspace"
    vault = tmp_path / "brain"
    setup_workspace(
        ws,
        vault_root=vault,
        launch_agents_dir=tmp_path / "LaunchAgents",
        app_dir=tmp_path / "Applications",
    )

    assert _git(vault, "branch", "--show-current").stdout.strip() == "main"
    log = _git(vault, "log", "--oneline")
    assert "Initialize Ciaobot vault" in log.stdout

    gitignore = (vault / ".gitignore").read_text(encoding="utf-8")
    for entry in (".DS_Store", ".obsidian/workspace*"):
        assert entry in gitignore.splitlines()

    tracked = _git(vault, "ls-files").stdout.splitlines()
    assert "MEMORY.md" in tracked

    env_text = (ws / ".env").read_text(encoding="utf-8")
    assert f"CIAO_VAULT_ROOT={vault}" in env_text
    # The workspace repo neither tracks nor contains the external vault.
    ws_tracked = _git(ws, "ls-files").stdout.splitlines()
    assert not any(path.startswith("memory-vault/") for path in ws_tracked)


def test_setup_workspace_rerun_honors_existing_env_vault_root(
    tmp_path: Path,
) -> None:
    """Re-running setup with a wrong/blank vault_root must not relocate the
    vault: the existing .env's CIAO_VAULT_ROOT wins, so scaffolding is not
    re-scattered at the argument's location."""
    ws = tmp_path / "workspace"
    setup_workspace(
        ws,
        vault_root="brain-a",
        launch_agents_dir=tmp_path / "LaunchAgents",
        app_dir=tmp_path / "Applications",
    )
    assert (ws / "brain-a" / "MEMORY.md").is_file()
    env_before = (ws / ".env").read_text(encoding="utf-8")

    # Re-run with a bogus vault_root; .env already exists so it is the source
    # of truth for where the vault lives.
    setup_workspace(
        ws,
        vault_root="wrong-b",
        launch_agents_dir=tmp_path / "LaunchAgents",
        app_dir=tmp_path / "Applications",
    )

    # Scaffolding stays in the real vault and is not re-scattered under wrong-b.
    assert (ws / "brain-a" / "MEMORY.md").is_file()
    assert not (ws / "wrong-b").exists()
    # .env is left untouched and still points at the original vault.
    assert (ws / ".env").read_text(encoding="utf-8") == env_before
    assert "CIAO_VAULT_ROOT=brain-a" in env_before
