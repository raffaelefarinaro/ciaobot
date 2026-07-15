from __future__ import annotations

import json
import subprocess
from pathlib import Path
import tomllib
from types import SimpleNamespace

from ciao import sync_skills


def _write(path: Path, text: str = "content\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_update_upstream_skills_passes_timeout(tmp_path: Path) -> None:
    calls: list[dict] = []

    def runner(args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(returncode=0)

    assert sync_skills._update_upstream_skills(
        tmp_path, ["upstream"], runner=runner
    ) is True
    assert calls[0]["timeout"] == sync_skills.SKILLS_NPX_TIMEOUT


def test_update_upstream_skills_survives_timeout(tmp_path: Path) -> None:
    def runner(args, **kwargs):
        # A stalled `npx -y skills update` would raise this once bounded; the
        # startup phase must end (return False), not hang or propagate.
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))

    assert sync_skills._update_upstream_skills(
        tmp_path, ["upstream"], runner=runner
    ) is False


def test_sync_links_codex_guide_to_canonical_claude_guide(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    claude_guide = workspace / "CLAUDE.md"
    _write(claude_guide, "# Shared workspace instructions\n")

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    codex_guide = workspace / "AGENTS.md"
    assert codex_guide.is_symlink()
    assert codex_guide.readlink() == Path("CLAUDE.md")
    assert codex_guide.resolve() == claude_guide.resolve()
    assert codex_guide.read_text(encoding="utf-8") == "# Shared workspace instructions\n"


def test_sync_preserves_custom_codex_guide(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write(workspace / "CLAUDE.md", "# Claude instructions\n")
    codex_guide = workspace / "AGENTS.md"
    _write(codex_guide, "# Custom Codex instructions\n")

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    assert not codex_guide.is_symlink()
    assert codex_guide.read_text(encoding="utf-8") == "# Custom Codex instructions\n"


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
    codex_skill = workspace / ".agents" / "skills" / "demo"
    assert codex_skill.is_symlink()
    assert codex_skill.resolve() == (workspace / "skills" / "demo").resolve()
    assert result.custom_installed == 1
    assert result.codex_skills_installed >= 1


def test_sync_preserves_agents_canonical_upstream_skill(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    canonical = workspace / ".agents" / "skills" / "upstream"
    _write(canonical / "SKILL.md", "# Upstream package\n")
    claude_link = workspace / ".claude" / "skills" / "upstream"
    claude_link.parent.mkdir(parents=True)
    claude_link.symlink_to(canonical)

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    assert canonical.is_dir()
    assert not canonical.is_symlink()
    assert (canonical / "SKILL.md").read_text(encoding="utf-8") == "# Upstream package\n"
    assert claude_link.is_symlink()
    assert claude_link.resolve() == canonical.resolve()


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
    assert result.commands_installed >= 1
    command_skill = workspace / ".agents" / "skills" / "ciao-command-remember" / "SKILL.md"
    agent_skill = workspace / ".agents" / "skills" / "ciao-agent-research" / "SKILL.md"
    assert "name: ciao-command-remember" in command_skill.read_text(encoding="utf-8")
    assert "name: ciao-agent-research" in agent_skill.read_text(encoding="utf-8")
    assert result.codex_wrappers_installed >= 2
    native_agent = workspace / ".codex" / "agents" / "research.toml"
    assert "developer_instructions" in native_agent.read_text(encoding="utf-8")
    codex_config = tomllib.loads(
        (workspace / ".codex" / "config.toml").read_text(encoding="utf-8")
    )
    assert codex_config["agents"]["research"]["config_file"] == "agents/research.toml"
    assert result.codex_agents_installed >= 1


def test_codex_native_agent_sync_is_idempotent_and_preserves_user_toml(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    _write(
        workspace / "subagents" / "research.md",
        "---\ndescription: Research carefully\n---\n\n# Research\nRead primary sources.\n",
    )
    _write(
        workspace / ".codex" / "config.toml",
        'model = "account-default"\n\n[agents.user_owned]\n'
        'description = "Keep me"\nconfig_file = "agents/user.toml"\n',
    )

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)
    first = (workspace / ".codex" / "config.toml").read_text(encoding="utf-8")
    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)
    second = (workspace / ".codex" / "config.toml").read_text(encoding="utf-8")

    assert first == second
    parsed = tomllib.loads(second)
    assert parsed["model"] == "account-default"
    assert parsed["agents"]["user_owned"]["description"] == "Keep me"
    assert parsed["agents"]["research"]["description"] == "Research carefully"


def test_codex_native_agent_sync_does_not_override_user_agent_name(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    _write(workspace / "subagents" / "research.md", "# Canonical research\n")
    _write(
        workspace / ".codex" / "config.toml",
        '[agents.research]\ndescription = "User version"\n'
        'config_file = "agents/custom.toml"\n',
    )

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    parsed = tomllib.loads(
        (workspace / ".codex" / "config.toml").read_text(encoding="utf-8")
    )
    assert parsed["agents"]["research"]["config_file"] == "agents/custom.toml"
    assert not (workspace / ".codex" / "agents" / "research.toml").exists()


def test_sync_workspace_skills_seeds_stock_commands_into_canonical_dir(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    remember = workspace / "commands" / "remember.md"
    critique = workspace / "commands" / "critique.md"
    interrogation = workspace / "commands" / "interrogation.md"
    assert remember.is_file()
    assert critique.is_file()
    assert interrogation.is_file()
    assert "ciao memory add --target memory" in remember.read_text(encoding="utf-8")
    assert "1–3 targeted questions" in interrogation.read_text(encoding="utf-8")
    assert "adversarial-review skill" in critique.read_text(encoding="utf-8")

    link = workspace / ".claude" / "commands" / "remember.md"
    assert link.is_symlink()
    assert link.resolve() == remember.resolve()
    assert result.commands_installed >= 3


def test_sync_workspace_skills_migrates_legacy_stock_commands(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    legacy = workspace / ".claude" / "commands" / "remember.md"
    _write(legacy, "# Old stock remember\n")

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    canonical = workspace / "commands" / "remember.md"
    assert canonical.is_file()
    text = canonical.read_text(encoding="utf-8")
    assert "ciao memory add --target memory" in text
    assert "# Old stock remember" not in text
    link = workspace / ".claude" / "commands" / "remember.md"
    assert link.is_symlink()
    assert link.resolve() == canonical.resolve()


def test_sync_installs_stock_skills_with_marker(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    installed = workspace / ".claude" / "skills" / "ciao-automations"
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


def test_disabled_auto_update_restores_missing_locked_skill(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "skills-lock.json").write_text(
        json.dumps(
            {
                "version": 1,
                "skills": {
                    "upstream": {
                        "source": "owner/repo",
                        "sourceType": "github",
                        "skillPath": "skills/upstream/SKILL.md",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def runner(args, **kwargs):
        calls.append(args)
        canonical = workspace / ".agents" / "skills" / "upstream"
        _write(canonical / "SKILL.md", "# Restored\n")
        claude_link = workspace / ".claude" / "skills" / "upstream"
        claude_link.parent.mkdir(parents=True, exist_ok=True)
        claude_link.symlink_to(canonical)
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("CIAO_AUTO_UPDATE_GITHUB_SKILLS", "false")
    monkeypatch.setattr(sync_skills.shutil, "which", lambda _name: "/usr/bin/tool")

    result = sync_skills._refresh_upstream_skills(workspace, runner=runner)

    assert result == (1, 0)
    assert calls == [
        [
            "npx",
            "-y",
            "skills",
            "add",
            "owner/repo",
            "--skill",
            "upstream",
            "--agent",
            "claude-code",
            "-y",
        ]
    ]
    assert (workspace / ".agents" / "skills" / "upstream" / "SKILL.md").is_file()


def test_upstream_refresh_prunes_only_previous_locked_packages(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "skills-lock.json").write_text(
        json.dumps(
            {
                "version": 1,
                "skills": {
                    "kept": {"source": "owner/kept", "sourceType": "github"}
                },
            }
        ),
        encoding="utf-8",
    )
    cache = workspace / ".runtime" / "skills-sync-cache.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(
        json.dumps(
            {
                "heads": {"owner/kept": "same"},
                "skills": {
                    "kept": "owner/kept",
                    "removed": "owner/removed",
                    "stock": "owner/old-stock",
                },
            }
        ),
        encoding="utf-8",
    )
    for name in ("kept", "removed"):
        canonical = workspace / ".agents" / "skills" / name
        _write(canonical / "SKILL.md", f"# {name}\n")
        claude_link = workspace / ".claude" / "skills" / name
        claude_link.parent.mkdir(parents=True, exist_ok=True)
        claude_link.symlink_to(canonical)
    stock = workspace / ".claude" / "skills" / "stock"
    _write(stock / "SKILL.md", "# Stock\n")
    (stock / sync_skills.STOCK_SKILL_MARKER).touch()

    monkeypatch.setenv("CIAO_AUTO_UPDATE_GITHUB_SKILLS", "true")
    monkeypatch.setattr(sync_skills.shutil, "which", lambda _name: "/usr/bin/tool")
    monkeypatch.setattr(
        sync_skills.skills_sync,
        "remote_heads",
        lambda _repos: {"owner/kept": "same"},
    )

    result = sync_skills._refresh_upstream_skills(workspace)

    assert result == (0, 1)
    assert not (workspace / ".agents" / "skills" / "removed").exists()
    assert not (workspace / ".claude" / "skills" / "removed").exists()
    assert (stock / "SKILL.md").is_file()


def test_sync_installs_stock_agents_with_marker(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    memory = workspace / ".claude" / "agents" / "memory.md"
    assert memory.is_file()
    assert sync_skills._is_managed_stock_agent(memory)
    assert "vault-read" in memory.read_text(encoding="utf-8")
    assert result.stock_agents_installed == 3


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
