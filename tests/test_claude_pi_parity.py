"""Parity check between Claude (`.claude/`) and Pi (`~/.pi/agent/`) catalogs.

Pi 0.74+ auto-discovers skills from `~/.pi/agent/skills/`, prompt templates
from `~/.pi/agent/prompts/`, and subagents from `~/.pi/agent/agents/`.
`scripts/install-custom-skills.sh` mirrors all three from the canonical
source folders (`skills/`, `subagents/`, `commands/`) into `.claude/` and
Pi: skills + commands via symlinks (same format), agents via a Python
translator (`sync-claude-agents-to-pi.py`) because tool-name casing and the
frontmatter shape differ.

These tests are the tripwire: if a skill / command / agent lands in
`.claude/` but the install script is not re-run, Pi sessions silently drop
that capability. Skipped when `~/.pi/agent/` is not present.

Ciao uses @tintinweb/pi-subagents as a Claude Code-like Agent primitive and
keeps the actual agent catalog in `subagents/` (symlinked into `.claude/agents/`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_SKILLS = REPO_ROOT / ".claude" / "skills"
CLAUDE_COMMANDS = REPO_ROOT / ".claude" / "commands"
CLAUDE_AGENTS = REPO_ROOT / ".claude" / "agents"
PI_SKILLS = Path.home() / ".pi" / "agent" / "skills"
PI_PROMPTS = Path.home() / ".pi" / "agent" / "prompts"
PI_AGENTS = Path.home() / ".pi" / "agent" / "agents"
MANAGED_MARKER = "# ciao-managed:"


def _pi_installed() -> bool:
    return (Path.home() / ".pi" / "agent").is_dir() and CLAUDE_SKILLS.is_dir()


pi_required = pytest.mark.skipif(
    not _pi_installed(),
    reason="Pi not installed or local .claude skills folder missing in this workspace",
)


def _entry_names(directory: Path) -> set[str]:
    if not directory.is_dir():
        return set()
    return {entry.name for entry in directory.iterdir() if not entry.name.startswith(".")}


@pi_required
def test_pi_skills_mirror_matches_claude_skills() -> None:
    """Every `.claude/skills/*` entry must appear in `~/.pi/agent/skills/`."""
    claude = _entry_names(CLAUDE_SKILLS)
    pi = _entry_names(PI_SKILLS)
    missing_in_pi = claude - pi
    stale_in_pi = pi - claude
    assert not missing_in_pi, (
        f"Skills present in .claude/skills/ but missing from ~/.pi/agent/skills/: "
        f"{sorted(missing_in_pi)}. Re-run scripts/install-custom-skills.sh."
    )
    assert not stale_in_pi, (
        f"Stale entries in ~/.pi/agent/skills/ not in .claude/skills/: "
        f"{sorted(stale_in_pi)}. Re-run scripts/install-custom-skills.sh to prune."
    )


@pi_required
def test_pi_prompts_mirror_matches_claude_commands() -> None:
    """Every `.claude/commands/*.md` must appear as `~/.pi/agent/prompts/<name>.md`.

    The two formats are compatible: same frontmatter (`description`,
    `argument-hint`), same body with `$ARGUMENTS` / `$1` placeholders.
    """
    claude = {p.name for p in CLAUDE_COMMANDS.glob("*.md")}
    pi = {p.name for p in PI_PROMPTS.glob("*.md")} if PI_PROMPTS.is_dir() else set()
    missing_in_pi = claude - pi
    # We only own the symlinks we created. Hand-authored Pi-only prompts are
    # allowed to live alongside, so we don't enforce strict equality.
    assert not missing_in_pi, (
        f"Commands present in .claude/commands/ but missing from "
        f"~/.pi/agent/prompts/: {sorted(missing_in_pi)}. "
        f"Re-run scripts/install-custom-skills.sh."
    )


@pi_required
def test_pi_mirrored_prompts_are_symlinks_to_repo() -> None:
    """Mirrored prompts must be symlinks pointing inside the repo.

    A regular file would mean the install script's symlink got clobbered,
    which would silently freeze the prompt at whatever revision was copied
    when the file appeared. Symlink targets must resolve back into
    `.claude/commands/` so edits in the repo land immediately in Pi.
    """
    expected_names = {p.name for p in CLAUDE_COMMANDS.glob("*.md")}
    for name in expected_names:
        link = PI_PROMPTS / name
        assert link.is_symlink(), (
            f"~/.pi/agent/prompts/{name} should be a symlink to "
            f".claude/commands/{name}, but it is a regular file. "
            f"Delete it and re-run scripts/install-custom-skills.sh."
        )
        target = link.resolve()
        try:
            target.relative_to(REPO_ROOT)
        except ValueError:
            pytest.fail(
                f"~/.pi/agent/prompts/{name} symlink resolves to {target}, "
                f"outside the repo. Expected a path under {REPO_ROOT}."
            )


@pi_required
def test_pi_agents_mirror_matches_claude_agents() -> None:
    """Every `.claude/agents/*.md` must produce a converted file in `~/.pi/agent/agents/`.

    The converter writes a `# ciao-managed:` marker on the first line so we
    can distinguish our outputs from hand-authored Pi-only agents.
    """
    claude = {p.name for p in CLAUDE_AGENTS.glob("*.md")}
    pi = {p.name for p in PI_AGENTS.glob("*.md")} if PI_AGENTS.is_dir() else set()
    missing_in_pi = claude - pi
    assert not missing_in_pi, (
        f"Agents present in .claude/agents/ but missing from "
        f"~/.pi/agent/agents/: {sorted(missing_in_pi)}. "
        f"Re-run scripts/install-custom-skills.sh."
    )


@pi_required
def test_pi_mirrored_agents_start_with_frontmatter() -> None:
    """Pi subagents only discover files whose YAML frontmatter starts at byte 0."""
    for src in CLAUDE_AGENTS.glob("*.md"):
        out = PI_AGENTS / src.name
        text = out.read_text(encoding="utf-8")
        assert text.startswith("---\n"), (
            f"~/.pi/agent/agents/{src.name} must start with frontmatter. "
            "A leading managed marker makes pi-subagents skip it."
        )


@pi_required
def test_pi_mirrored_agents_carry_managed_marker_and_pi_tools() -> None:
    """Each mirrored agent should keep the `# ciao-managed:` marker and use
    lowercase Pi tool names. Catches drift if someone edits the Pi-side file
    by hand or if the converter regresses."""
    for src in CLAUDE_AGENTS.glob("*.md"):
        out = PI_AGENTS / src.name
        text = out.read_text(encoding="utf-8")
        assert MANAGED_MARKER in text, (
            f"~/.pi/agent/agents/{src.name} is missing the managed marker. "
            f"Either it was hand-edited (rename it to keep it) or the converter "
            f"regressed. Re-run scripts/sync-claude-agents-to-pi.py."
        )
        # Frontmatter tools must be lowercase Pi-compatible names. We don't
        # require any specific set, but we forbid the Title-Case Claude names
        # leaking through.
        for forbidden in ("Read", "Grep", "Glob", "Bash", "Edit", "Write"):
            assert f"tools: {forbidden}" not in text and f", {forbidden}" not in text, (
                f"~/.pi/agent/agents/{src.name} contains a Claude-style tool "
                f"name ({forbidden!r}). The converter should have lowercased it."
            )


@pi_required
def test_pi_mirrored_agents_use_tintinweb_frontmatter() -> None:
    """Ciao-generated Pi agents use @tintinweb/pi-subagents frontmatter."""
    for src in CLAUDE_AGENTS.glob("*.md"):
        out = PI_AGENTS / src.name
        text = out.read_text(encoding="utf-8")
        assert "prompt_mode: replace" in text
        assert "skills: false" in text
        assert "inherit_context:" not in text
        assert "systemPromptMode:" not in text
        assert "inheritProjectContext:" not in text
        assert "inheritSkills:" not in text
