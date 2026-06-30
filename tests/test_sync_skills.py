from __future__ import annotations

from pathlib import Path

from ciao import sync_agents_to_pi, sync_skills


def _write(path: Path, text: str = "content\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_sync_workspace_skills_mirrors_custom_skills_commands_and_agents(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    pi_root = tmp_path / "pi"
    _write(workspace / "skills" / "demo" / "SKILL.md", "# Demo\n")
    _write(workspace / ".claude" / "commands" / "remember.md", "Remember $ARGUMENTS\n")
    _write(
        workspace / "subagents" / "helper.md",
        "---\nname: helper\ndescription: Helps.\ntools: Read, Grep, Glob, Bash\n---\n\nHelp.\n",
    )

    result = sync_skills.sync_workspace_skills(
        workspace,
        pi_root=pi_root,
        refresh_upstream=False,
    )

    claude_skill = workspace / ".claude" / "skills" / "demo"
    assert claude_skill.is_symlink()
    assert claude_skill.resolve() == (workspace / "skills" / "demo").resolve()
    assert (pi_root / "skills" / "demo").is_symlink()
    assert (pi_root / "skills" / "demo").resolve() == claude_skill.resolve()
    assert (pi_root / "prompts" / "remember.md").is_symlink()
    assert (pi_root / "prompts" / "remember.md").resolve() == (
        workspace / ".claude" / "commands" / "remember.md"
    ).resolve()
    rendered_agent = pi_root / "agents" / "helper.md"
    assert rendered_agent.is_file()
    rendered_text = rendered_agent.read_text(encoding="utf-8")
    assert sync_agents_to_pi.MANAGED_MARKER in rendered_text
    assert "tools: read, grep, find, ls, bash" in rendered_text
    assert result.custom_installed == 1
    assert result.pi_skills_linked == 1
    assert result.pi_prompts_linked == 1
    assert result.pi_agents_sources == 1


def test_sync_workspace_skills_prunes_only_managed_entries(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    pi_root = tmp_path / "pi"
    _write(workspace / ".claude" / "skills" / "kept-upstream" / "SKILL.md")
    (pi_root / "skills").mkdir(parents=True)
    (pi_root / "skills" / "stale").symlink_to(tmp_path / "missing")
    _write(pi_root / "prompts" / "manual.md", "manual\n")
    (pi_root / "agents").mkdir(parents=True)
    _write(
        pi_root / "agents" / "stale.md",
        f"---\nname: stale\n---\n\n{sync_agents_to_pi.MANAGED_MARKER}\n",
    )

    result = sync_skills.sync_workspace_skills(
        workspace,
        pi_root=pi_root,
        refresh_upstream=False,
    )

    assert not (pi_root / "skills" / "stale").exists()
    assert (pi_root / "prompts" / "manual.md").is_file()
    assert not (pi_root / "agents" / "stale.md").exists()
    assert result.pi_skills_pruned == 1
    assert result.pi_agents_pruned == 1
