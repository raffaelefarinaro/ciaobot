from __future__ import annotations

from pathlib import Path

from ciao import sync_skills


def _write(path: Path, text: str = "content\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_sync_workspace_skills_mirrors_custom_skills(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write(workspace / "skills" / "demo" / "SKILL.md", "# Demo\n")
    _write(workspace / ".claude" / "commands" / "remember.md", "Remember $ARGUMENTS\n")

    result = sync_skills.sync_workspace_skills(
        workspace,
        refresh_upstream=False,
    )

    claude_skill = workspace / ".claude" / "skills" / "demo"
    assert claude_skill.is_symlink()
    assert claude_skill.resolve() == (workspace / "skills" / "demo").resolve()
    assert result.custom_installed == 1


def test_sync_workspace_skills_prunes_orphaned_custom_links(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write(workspace / "skills" / "kept" / "SKILL.md")
    (workspace / ".claude" / "skills").mkdir(parents=True)
    # A broken custom-skill symlink (target gone) is recognised by the
    # /skills/<name> path in its readlink target and pruned.
    (workspace / ".claude" / "skills" / "stale").symlink_to(
        workspace / "skills" / "stale"
    )

    result = sync_skills.sync_workspace_skills(
        workspace,
        refresh_upstream=False,
    )

    assert not (workspace / ".claude" / "skills" / "stale").exists()
    assert result.custom_pruned == 1


def test_sync_workspace_skills_mirrors_subagents_and_commands(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write(workspace / "subagents" / "research.md", "# Research\n")
    _write(workspace / "commands" / "remember.md", "# Remember\n")
    _write(workspace / ".claude" / "agents" / "stock.md", "# Stock\n")

    result = sync_skills.sync_workspace_skills(
        workspace,
        refresh_upstream=False,
    )

    agent_link = workspace / ".claude" / "agents" / "research.md"
    command_link = workspace / ".claude" / "commands" / "remember.md"
    assert agent_link.is_symlink()
    assert agent_link.resolve() == (workspace / "subagents" / "research.md").resolve()
    assert command_link.is_symlink()
    assert command_link.resolve() == (workspace / "commands" / "remember.md").resolve()
    assert (workspace / ".claude" / "agents" / "stock.md").is_file()
    assert result.agents_installed == 1
    assert result.commands_installed == 1


def test_sync_installs_stock_skills_with_marker(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    installed = workspace / ".claude" / "skills" / "ciao-schedules"
    assert (installed / "SKILL.md").is_file()
    assert (installed / sync_skills.STOCK_SKILL_MARKER).is_file()
    assert not installed.is_symlink()
    assert result.stock_installed >= 5  # the packaged generic skill set


def test_workspace_skill_shadows_stock_skill(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write(workspace / "skills" / "vault-read" / "SKILL.md", "# My override\n")

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    link = workspace / ".claude" / "skills" / "vault-read"
    assert link.is_symlink()
    assert link.resolve() == (workspace / "skills" / "vault-read").resolve()
    # A later sync (override still present) keeps the symlink, no stock copy.
    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)
    assert link.is_symlink()


def test_stale_stock_skill_copy_is_pruned(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    stale = workspace / ".claude" / "skills" / "no-longer-packaged"
    _write(stale / "SKILL.md", "# Old stock skill\n")
    (stale / sync_skills.STOCK_SKILL_MARKER).touch()
    user_dir = workspace / ".claude" / "skills" / "hand-made"
    _write(user_dir / "SKILL.md", "# Hand made, no marker\n")

    result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    assert not stale.exists()
    assert (user_dir / "SKILL.md").is_file()  # unmarked dirs are untouched
    assert result.stock_pruned == 1


def test_sync_installs_stock_agents_with_marker(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    memory = workspace / ".claude" / "agents" / "memory.md"
    assert memory.is_file()
    assert sync_skills._is_managed_stock_agent(memory)
    assert "vault-read" in memory.read_text(encoding="utf-8")
    assert result.stock_agents_installed == 4


def test_sync_refreshes_managed_stock_agent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    memory = workspace / ".claude" / "agents" / "memory.md"
    _write(memory, "# Old memory agent\n")
    sync_skills._mark_stock_agent(memory)

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    assert "vault-read" in memory.read_text(encoding="utf-8")


def test_stale_stock_agent_copy_is_pruned(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    stale = workspace / ".claude" / "agents" / "no-longer-packaged.md"
    _write(stale, "# Old stock agent\n")
    sync_skills._mark_stock_agent(stale)
    hand_made = workspace / ".claude" / "agents" / "hand-made.md"
    _write(hand_made, "# Hand made, no marker\n")

    result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    assert not stale.exists()
    assert hand_made.is_file()
    assert result.stock_agents_pruned == 1


def test_legacy_removed_stock_agent_is_pruned_without_marker(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    removed = workspace / ".claude" / "agents" / "comment-analyzer.md"
    _write(removed, "# Legacy dev agent\n")

    result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    assert not removed.exists()
    assert result.stock_agents_pruned == 1


def test_subagent_shadows_stock_agent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    custom = workspace / "subagents" / "memory.md"
    _write(custom, "# Custom memory\n")
    stock = workspace / ".claude" / "agents" / "memory.md"
    _write(stock, "# Packaged memory\n")
    sync_skills._mark_stock_agent(stock)

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    link = workspace / ".claude" / "agents" / "memory.md"
    assert link.is_symlink()
    assert link.resolve() == custom.resolve()
    assert custom.read_text(encoding="utf-8") == "# Custom memory\n"
