"""Tests for the slash-command discovery used by the PWA picker."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ciao.config import CiaoConfig, WorkspaceConfig, reset_reroot_cache
from ciao.web import commands as commands_module
from ciao.web.commands import (
    Command,
    _parse_frontmatter,
    _workspace_root,
    list_commands,
    list_picker_entries,
    list_provider_command_entries,
    list_skill_entries,
)
from ciao.workspace_reroot import write_receipt


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


def test_provider_command_entries_reads_native_command_dir(tmp_path: Path) -> None:
    _write_cmd(
        tmp_path / ".opencode" / "commands",
        "opencode-only",
        "---\ndescription: opencode native command\n---\n",
    )
    entries = list_provider_command_entries(tmp_path, "opencode")
    assert [entry.name for entry in entries] == ["opencode-only"]
    assert entries[0].source == "provider"


def test_provider_command_entries_ignores_unknown_provider(tmp_path: Path) -> None:
    assert list_provider_command_entries(tmp_path, "unknown") == []


def test_picker_merges_provider_commands_and_deduplicates(tmp_path: Path) -> None:
    _write_cmd(
        tmp_path / ".claude" / "commands",
        "shared",
        "---\ndescription: canonical\n---\n",
    )
    _write_cmd(
        tmp_path / ".opencode" / "commands",
        "shared",
        "---\ndescription: opencode native\n---\n",
    )
    _write_cmd(
        tmp_path / ".opencode" / "commands",
        "opencode-only",
        "---\ndescription: opencode only\n---\n",
    )

    commands, skills = list_picker_entries(tmp_path, "opencode")

    assert [command.name for command in commands] == ["opencode-only", "shared"]
    # Canonical command wins on name collision with the provider's native one.
    shared = next(command for command in commands if command.name == "shared")
    assert shared.description == "canonical"
    assert shared.source == "project"
    assert skills == []


def test_list_skill_entries_uses_current_provider_install_targets(tmp_path: Path) -> None:
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
    # opencode can discover both the shared catalog and Claude's projection.
    assert [skill.name for skill in list_skill_entries(tmp_path, "opencode")] == [
        "both", "claude-only"
    ]


def test_picker_merges_provider_page_skills_and_deduplicates_workspace_skills(
    tmp_path: Path, monkeypatch
) -> None:
    _write_cmd(
        tmp_path / "skills" / "shared",
        "SKILL",
        "---\ndescription: Workspace description\n---\n",
    )
    (tmp_path / ".agents" / "skills" / "shared").mkdir(parents=True)
    (tmp_path / ".agents" / "skills" / "shared" / "SKILL.md").write_text(
        (tmp_path / "skills" / "shared" / "SKILL.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        commands_module,
        "_discover_provider_skill_names",
        lambda provider: ["browser", "shared"] if provider == "opencode" else [],
    )

    commands, skills = list_picker_entries(tmp_path, "opencode")

    assert commands == []
    assert [skill.name for skill in skills] == ["browser", "shared"]
    assert skills[0].description == "Loaded by opencode"
    assert skills[1].description == "Workspace description"


def _rerooted_config(tmp_path: Path, workspaces: dict[str, str]) -> CiaoConfig:
    runtime_root = tmp_path / ".runtime"
    write_receipt(runtime_root, {"status": "migrated", "workspaces": list(workspaces)})
    reset_reroot_cache()
    return CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime_root / "state.json",
        media_root=runtime_root / "media",
        workspaces={
            name: WorkspaceConfig(name=name, vault_root=root)
            for name, root in workspaces.items()
        },
    )


def _request(config: CiaoConfig, workspace: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(config=config)),
        query_params=SimpleNamespace(get=lambda key, default="": {"workspace": workspace}.get(key, default)),
    )


def test_workspace_root_uses_agent_root_after_rerooting(tmp_path: Path) -> None:
    """A rerooted install must resolve the picker's root per-chat-workspace.

    Before this, `_workspace_root` always returned the bare install root, so a
    custom skill living under a workspace's own `skills/` (moved there by the
    re-rooting migration) never showed up in that workspace's slash picker.
    """
    config = _rerooted_config(tmp_path, {"personal": "personal", "work": "work"})
    _write_cmd(tmp_path / "work" / "skills" / "humanizer", "SKILL", "---\ndescription: Humanize text\n---\n")

    assert _workspace_root(_request(config, "work")) == tmp_path / "work"
    skills = list_skill_entries(_workspace_root(_request(config, "work")), "claude")
    assert [s.name for s in skills] == []  # not mirrored into .claude/skills, but root resolves correctly

    (tmp_path / "work" / ".claude" / "skills" / "humanizer").mkdir(parents=True)
    (tmp_path / "work" / ".claude" / "skills" / "humanizer" / "SKILL.md").write_text(
        (tmp_path / "work" / "skills" / "humanizer" / "SKILL.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    skills = list_skill_entries(_workspace_root(_request(config, "work")), "claude")
    assert [s.name for s in skills] == ["humanizer"]


def test_workspace_root_falls_back_when_workspace_unknown_or_missing(tmp_path: Path) -> None:
    config = _rerooted_config(tmp_path, {"personal": "personal", "work": "work"})

    # No workspace param -> primary workspace's agent root, not the bare install root.
    assert _workspace_root(_request(config)) == tmp_path / "personal"
    # Unknown workspace name -> bare install root, never raises.
    assert _workspace_root(_request(config, "no-such-workspace")) == tmp_path
