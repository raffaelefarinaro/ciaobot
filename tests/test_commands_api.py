"""Tests for the slash-command discovery used by the PWA picker."""

from __future__ import annotations

from pathlib import Path

from ciao.web.commands import Command, _parse_frontmatter, list_commands, list_skill_entries


def _write_cmd(dir_path: Path, name: str, frontmatter: str, body: str = "body") -> None:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / f"{name}.md").write_text(f"{frontmatter}{body}\n", encoding="utf-8")


def test_frontmatter_parses_simple_keys() -> None:
    text = """---
description: Hello
argument-hint: <name>
---
body
"""
    fm = _parse_frontmatter(text)
    assert fm == {"description": "Hello", "argument-hint": "<name>"}


def test_frontmatter_missing_returns_empty_dict() -> None:
    assert _parse_frontmatter("no frontmatter here\n") == {}


def test_list_commands_reads_project_dir(tmp_path: Path, monkeypatch) -> None:
    # Point $HOME at tmp so the user-level scan stays empty for this test.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _write_cmd(
        tmp_path / ".claude" / "commands",
        "brief",
        "---\ndescription: Morning briefing\n---\n",
    )
    _write_cmd(
        tmp_path / ".claude" / "commands",
        "triage",
        "---\ndescription: Gmail triage\nargument-hint: <inbox>\n---\n",
    )
    cmds = list_commands(tmp_path)
    names = [c.name for c in cmds]
    assert names == ["brief", "triage"]
    by_name = {c.name: c for c in cmds}
    assert by_name["brief"].description == "Morning briefing"
    assert by_name["triage"].argument_hint == "<inbox>"
    assert all(c.source == "project" for c in cmds)


def test_project_wins_over_user_on_collision(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    _write_cmd(
        home / ".claude" / "commands",
        "shared",
        "---\ndescription: user-level\n---\n",
    )
    _write_cmd(
        tmp_path / ".claude" / "commands",
        "shared",
        "---\ndescription: project-level\n---\n",
    )
    cmds = list_commands(tmp_path)
    assert len(cmds) == 1
    assert cmds[0].description == "project-level"
    assert cmds[0].source == "project"


def test_missing_dirs_return_empty(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "nonexistent-home"))
    assert list_commands(tmp_path / "no-project") == []


def test_list_skill_entries_filters_to_provider_install_target(tmp_path: Path) -> None:
    _write_cmd(
        tmp_path / "skills" / "claude-only",
        "SKILL",
        "---\ndescription: Claude workflow\n---\n",
    )
    _write_cmd(
        tmp_path / "skills" / "both",
        "SKILL",
        "---\ndescription: Shared workflow\n---\n",
    )
    (tmp_path / ".claude" / "skills" / "claude-only").mkdir(parents=True)
    (tmp_path / ".claude" / "skills" / "claude-only" / "SKILL.md").write_text(
        (tmp_path / "skills" / "claude-only" / "SKILL.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / ".agents" / "skills" / "both").mkdir(parents=True)
    (tmp_path / ".agents" / "skills" / "both" / "SKILL.md").write_text(
        (tmp_path / "skills" / "both" / "SKILL.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    assert [skill.name for skill in list_skill_entries(tmp_path, "claude")] == ["claude-only"]
    assert [skill.name for skill in list_skill_entries(tmp_path, "codex")] == ["both"]
