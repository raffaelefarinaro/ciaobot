from __future__ import annotations

import subprocess
import json
from pathlib import Path
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

    assert sync_skills._update_upstream_skills(tmp_path, ["upstream"], runner=runner)
    assert calls[0]["timeout"] == sync_skills.SKILLS_NPX_TIMEOUT


def test_update_upstream_skills_survives_timeout(tmp_path: Path) -> None:
    def runner(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))

    assert not sync_skills._update_upstream_skills(tmp_path, ["upstream"], runner=runner)


def test_sync_links_agents_guide_to_canonical_claude_guide(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    claude_guide = workspace / "CLAUDE.md"
    _write(claude_guide, "# Shared workspace instructions\n")

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    agents_guide = workspace / "AGENTS.md"
    assert agents_guide.is_symlink()
    assert agents_guide.readlink() == Path("CLAUDE.md")
    assert agents_guide.resolve() == claude_guide.resolve()
    text = agents_guide.read_text(encoding="utf-8")
    assert text.startswith("# Shared workspace instructions\n")
    assert "<!-- ciao:memory:start" in text
    assert "<!-- ciao:profile:start" in text


def test_sync_preserves_custom_agents_guide(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write(workspace / "CLAUDE.md", "# Claude instructions\n")
    agents_guide = workspace / "AGENTS.md"
    _write(agents_guide, "# Custom workspace instructions\n")

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    assert not agents_guide.is_symlink()
    assert agents_guide.read_text(encoding="utf-8") == "# Custom workspace instructions\n"


def test_sync_workspace_skills_mirrors_custom_skills(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write(workspace / "skills" / "demo" / "SKILL.md", "# Demo\n")
    _write(workspace / ".claude" / "commands" / "remember.md", "Remember $ARGUMENTS\n")

    result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    claude_skill = workspace / ".claude" / "skills" / "demo"
    assert claude_skill.is_symlink()
    assert claude_skill.resolve() == (workspace / "skills" / "demo").resolve()
    assert result.custom_installed == 1


def test_sync_restamps_stale_cap_markers(tmp_path: Path) -> None:
    """Sync migrates a former-default cap stamp to the shipped default."""
    import re

    from ciao.memory_tool import ensure_regions

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    guide = workspace / "CLAUDE.md"
    guide.write_text("# Guide\n\n", encoding="utf-8")
    ensure_regions(guide)
    stamped = re.sub(
        r"(<!-- ciao:memory:start cap=)\d+( -->)",
        r"\g<1>2200\g<2>",
        guide.read_text(encoding="utf-8"),
    )
    guide.write_text(stamped, encoding="utf-8")

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    text = guide.read_text(encoding="utf-8")
    assert "cap=2200" not in text
    assert "<!-- ciao:memory:start cap=3000 -->" in text


def test_sync_honors_dotenv_cap_override_over_stale_stamp(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """A limit that lives only in <root>/.env must win over the stamp.

    Standalone sync never loads the dotenv, so without reading it here a
    `CIAO_MEMORY_CHAR_LIMIT=2200` override would be restamped to 3000 and a
    later server start would enforce 2200 against a guide advertising 3000.
    """
    import re

    from ciao.memory_tool import ensure_regions

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # export prefix + quotes: valid python-dotenv syntax the server accepts
    # and a hand-rolled line parser does not.
    (workspace / ".env").write_text(
        "export CIAO_MEMORY_CHAR_LIMIT=\"2200\"\n", encoding="utf-8"
    )
    monkeypatch.delenv("CIAO_MEMORY_CHAR_LIMIT", raising=False)
    guide = workspace / "CLAUDE.md"
    guide.write_text("# Guide\n\n", encoding="utf-8")
    ensure_regions(guide)
    stamped = re.sub(
        r"(<!-- ciao:memory:start cap=)\d+( -->)",
        r"\g<1>2200\g<2>",
        guide.read_text(encoding="utf-8"),
    )
    guide.write_text(stamped, encoding="utf-8")

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    # The .env override says 2200: the marker is already correct and
    # must be left byte-identical.
    assert guide.read_text(encoding="utf-8") == stamped


def test_sync_reads_install_dotenv_for_nested_agent_root(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Setup scaffolds per-workspace agent roots under an install root whose
    ``.env`` sits one level up and is never exported; the walk-up must find
    it or a freshly seeded 3000 stamp gets left contradicting the enforced
    2200.
    """
    import re

    from ciao.memory_tool import ensure_regions

    install = tmp_path / "install"
    workspace = install / "personal"
    workspace.mkdir(parents=True)
    (install / ".env").write_text(
        "export CIAO_MEMORY_CHAR_LIMIT=\"2200\"\n", encoding="utf-8"
    )
    monkeypatch.delenv("CIAO_MEMORY_CHAR_LIMIT", raising=False)
    guide = workspace / "CLAUDE.md"
    guide.write_text("# Guide\n\n", encoding="utf-8")
    ensure_regions(guide)
    # Fresh seeding stamped the shipped default; effective limit is 2200.

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    text = guide.read_text(encoding="utf-8")
    assert "<!-- ciao:memory:start cap=2200 -->" in text
    assert re.search(r"ciao:memory:start cap=3000", text) is None


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
    (workspace / ".claude" / "skills" / "stale").symlink_to(
        workspace / "skills" / "stale"
    )

    result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    assert not (workspace / ".claude" / "skills" / "stale").exists()
    assert result.custom_pruned == 1


def test_sync_workspace_skills_mirrors_subagents_and_commands(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write(workspace / "subagents" / "research.md", "# Research\n")
    _write(workspace / "commands" / "remember.md", "# Remember\n")
    _write(workspace / ".claude" / "agents" / "stock.md", "# Stock\n")

    result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    agent_link = workspace / ".claude" / "agents" / "research.md"
    command_link = workspace / ".claude" / "commands" / "remember.md"
    assert agent_link.is_symlink()
    assert agent_link.resolve() == (workspace / "subagents" / "research.md").resolve()
    assert command_link.is_symlink()
    assert command_link.resolve() == (workspace / "commands" / "remember.md").resolve()
    assert (workspace / ".claude" / "agents" / "stock.md").is_file()
    assert result.agents_installed == 1
    assert result.commands_installed >= 1


def test_sync_workspace_skills_seeds_stock_commands_into_canonical_dir(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    for name in ("remember", "critique", "interrogation"):
        assert (workspace / "commands" / f"{name}.md").is_file()
    assert result.commands_installed >= 3


def test_sync_workspace_skills_migrates_legacy_stock_commands(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    legacy = workspace / ".claude" / "commands" / "remember.md"
    _write(legacy, "# Old stock remember\n")

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    canonical = workspace / "commands" / "remember.md"
    assert canonical.is_file()
    text = canonical.read_text(encoding="utf-8")
    assert "ciao:memory" in text
    assert "# Old stock remember" not in text
    link = workspace / ".claude" / "commands" / "remember.md"
    assert link.is_symlink()
    assert link.resolve() == canonical.resolve()


def test_sync_installs_stock_skills_with_marker(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    installed = workspace / ".claude" / "skills" / "ciao-capabilities"
    assert (installed / "SKILL.md").is_file()
    assert (installed / sync_skills.STOCK_SKILL_MARKER).is_file()
    assert not installed.is_symlink()
    assert result.stock_installed >= 3


def test_gws_stock_skills_skipped_without_a_profile(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = sync_skills._install_stock_skills(workspace, gws_profile="")

    assert not (workspace / ".claude" / "skills" / "gws-gmail").exists()
    assert (
        workspace / ".claude" / "skills" / "ciao-capabilities" / "SKILL.md"
    ).is_file()
    assert result[0] >= 1  # generic skills still installed


def test_gws_stock_skills_installed_with_a_profile(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = sync_skills._install_stock_skills(workspace, gws_profile="personal")

    assert (workspace / ".claude" / "skills" / "gws-gmail" / "SKILL.md").is_file()
    assert result[0] >= 1


def test_gws_stock_skills_unconditional_by_default(tmp_path: Path) -> None:
    """Callers that predate the profile check still install GWS skills."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = sync_skills._install_stock_skills(workspace)

    assert (workspace / ".claude" / "skills" / "gws-gmail" / "SKILL.md").is_file()
    assert result[0] >= 1


def _gws_aware_config(tmp_path: Path, workspaces: dict[str, str]) -> object:
    """A minimal config whose workspaces map names to gws_profiles."""
    from types import SimpleNamespace

    ws = {
        name: SimpleNamespace(name=name, gws_profile=profile)
        for name, profile in workspaces.items()
    }
    return SimpleNamespace(
        workspaces=ws,
        gws_default_profile="",
        workspace=lambda name: ws.get(name),
        workspace_names=lambda: list(ws.keys()),
        workspace_root=tmp_path,
        agent_root=lambda name: tmp_path / name,
    )


def test_any_workspace_has_gws_profile_false_when_none_linked(tmp_path: Path) -> None:
    config = _gws_aware_config(tmp_path, {"personal": "", "work": ""})
    assert sync_skills._any_workspace_has_gws_profile(config) is False


def test_any_workspace_has_gws_profile_true_when_any_linked(tmp_path: Path) -> None:
    config = _gws_aware_config(tmp_path, {"personal": "", "work": "acme"})
    assert sync_skills._any_workspace_has_gws_profile(config) is True


def test_shared_root_resolves_to_none_when_any_workspace_has_gws(tmp_path, monkeypatch) -> None:
    """The pre-re-root shared catalog stays gated only when NO workspace links one.

    An empty workspace name means the shared root serves every workspace, so a
    single linked account must keep the GWS skills installed there.
    """
    config = _gws_aware_config(tmp_path, {"personal": "", "work": "acme"})
    monkeypatch.setattr(sync_skills, "_config_for_root", lambda _root: config)

    # Any workspace has a profile -> no gate (None) -> GWS skills installed.
    assert sync_skills._resolve_workspace_gws_profile(Path("/tmp/root"), "", None) is None


def test_shared_root_gates_when_no_workspace_has_gws(tmp_path, monkeypatch) -> None:
    config = _gws_aware_config(tmp_path, {"personal": "", "work": ""})
    monkeypatch.setattr(sync_skills, "_config_for_root", lambda _root: config)

    assert sync_skills._resolve_workspace_gws_profile(Path("/tmp/root"), "", None) == ""


def test_shared_root_gate_aggregates_all_workspaces(tmp_path) -> None:
    """Unlinking one workspace must not prune the shared catalog while another links one."""
    config = _gws_aware_config(tmp_path, {"personal": "", "work": "acme"})
    # Pre-re-root: every workspace's agent_root is the shared install root.
    config.agent_root = lambda _name: tmp_path
    config.workspace_root = tmp_path

    assert sync_skills.resolve_workspace_skills_gws_gate(config, tmp_path, "personal") is None


def test_shared_root_gate_gates_when_none_linked(tmp_path) -> None:
    config = _gws_aware_config(tmp_path, {"personal": "", "work": ""})
    config.agent_root = lambda _name: tmp_path
    config.workspace_root = tmp_path

    assert sync_skills.resolve_workspace_skills_gws_gate(config, tmp_path, "personal") == ""


def test_workspace_skill_shadows_stock_skill(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write(workspace / "skills" / "web-research" / "SKILL.md", "# My override\n")

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    link = workspace / ".claude" / "skills" / "web-research"
    assert link.is_symlink()
    assert link.resolve() == (workspace / "skills" / "web-research").resolve()


def test_stale_stock_skill_copy_is_pruned(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    stale = workspace / ".claude" / "skills" / "no-longer-packaged"
    _write(stale / "SKILL.md", "# Old stock skill\n")
    (stale / sync_skills.STOCK_SKILL_MARKER).touch()
    user_dir = workspace / ".claude" / "skills" / "hand-made"
    _write(user_dir / "SKILL.md", "# Hand made, no marker\n")

    result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    assert not stale.exists()
    assert (user_dir / "SKILL.md").is_file()
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
    assert calls == [[
        "npx", "-y", "skills", "add", "owner/repo", "--skill", "upstream",
        "--agent", "claude-code", "-y",
    ]]
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
                "skills": {"kept": {"source": "owner/kept", "sourceType": "github"}},
            }
        ),
        encoding="utf-8",
    )
    cache = workspace / ".runtime" / "skills-sync-cache.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(
        json.dumps({
            "heads": {"owner/kept": "same"},
            "skills": {"kept": "owner/kept", "removed": "owner/removed", "stock": "owner/old-stock"},
        }),
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
    monkeypatch.setattr(sync_skills.skills_sync, "remote_heads", lambda _repos: {"owner/kept": "same"})

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
    assert "vault_search" in memory.read_text(encoding="utf-8")
    assert result.stock_agents_installed == 3


def test_sync_refreshes_managed_stock_agent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    memory = workspace / ".claude" / "agents" / "memory.md"
    _write(memory, "# Old memory agent\n")
    sync_skills._mark_stock_agent(memory)

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    assert "vault_search" in memory.read_text(encoding="utf-8")


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


def test_configured_cap_requires_the_exact_key_and_handles_comments(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """The dotenv read must match CIAO_MEMORY_CHAR_LIMIT exactly.

    A hand-rolled prefix match treats CIAO_MEMORY_CHAR_LIMIT_BACKUP as the
    real setting and trips over an inline comment, either of which restamps
    a guide with a cap the server never enforces. python-dotenv parses both
    the way the server's own load_dotenv does.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.delenv("CIAO_MEMORY_CHAR_LIMIT", raising=False)

    (workspace / ".env").write_text(
        "CIAO_MEMORY_CHAR_LIMIT_BACKUP=9999\n", encoding="utf-8"
    )
    assert sync_skills._configured_memory_char_limit(workspace) is None

    (workspace / ".env").write_text(
        "CIAO_MEMORY_CHAR_LIMIT=2200  # keep the old budget\n", encoding="utf-8"
    )
    assert sync_skills._configured_memory_char_limit(workspace) == 2200
