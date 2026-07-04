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
