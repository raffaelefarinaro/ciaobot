from __future__ import annotations

import logging
import subprocess
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao import cli, sync_skills
from ciao.os_support.links import is_link, link_dir, link_source


def _write(path: Path, text: str = "content\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


def _dangling_dir_link(target: Path, link: Path, *, relative_to: Path | None = None) -> None:
    """A directory link whose target is gone (a Windows junction needs it to exist first)."""
    target.mkdir(parents=True, exist_ok=True)
    link.parent.mkdir(parents=True, exist_ok=True)
    link_dir(target, link, relative_to=relative_to)
    target.rmdir()


def _user_file_symlink(link: Path, target: Path) -> None:
    """A file symlink the user made; a Windows account without the privilege cannot."""
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"cannot create a file symlink here: {exc}")


def test_upstream_skills_removed(tmp_path: Path) -> None:
    # GitHub/upstream sync surface has been removed per the simplification plan.
    assert not hasattr(sync_skills, "_update_upstream_skills")
    assert not hasattr(sync_skills, "_refresh_upstream_skills")
    assert not hasattr(sync_skills, "SKILLS_NPX_TIMEOUT")


def test_sync_keeps_the_existing_guide_and_adds_the_regions(tmp_path: Path) -> None:
    """One guide, AGENTS.md, and no symlink beside it."""
    workspace = tmp_path / "workspace"
    guide = workspace / "AGENTS.md"
    _write(guide, "# Shared workspace instructions\n")

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    assert not guide.is_symlink()
    assert not (workspace / "CLAUDE.md").exists()
    text = guide.read_text(encoding="utf-8")
    assert text.startswith("# Shared workspace instructions\n")
    assert "<!-- ciao:memory:start" in text
    assert "<!-- ciao:profile:start" in text


def test_sync_seeds_a_guide_only_when_the_workspace_has_none(tmp_path: Path) -> None:
    """Seeding over an existing guide would discard the memory regions in it."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    guide = workspace / "AGENTS.md"
    assert guide.is_file() and not guide.is_symlink()
    assert not (workspace / "CLAUDE.md").exists()
    assert "<!-- ciao:memory:start" in guide.read_text(encoding="utf-8")


def test_sync_preserves_a_custom_guide(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    guide = workspace / "AGENTS.md"
    _write(guide, "# Custom workspace instructions\n")

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    assert not guide.is_symlink()
    assert guide.read_text(encoding="utf-8").startswith("# Custom workspace instructions\n")


def test_sync_adopts_a_pre_migration_guide_without_copying_it(tmp_path: Path) -> None:
    """A legacy CLAUDE.md is the workspace's guide until the migration renames
    it; sync must not write a second, empty AGENTS.md beside it."""
    workspace = tmp_path / "workspace"
    _write(workspace / "CLAUDE.md", "# Legacy instructions\n")

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    assert not (workspace / "AGENTS.md").exists()
    assert (workspace / "CLAUDE.md").read_text(encoding="utf-8").startswith(
        "# Legacy instructions\n"
    )


def test_sync_workspace_skills_mirrors_custom_skills(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write(workspace / "skills" / "demo" / "SKILL.md", "# Demo\n")
    _write(workspace / ".claude" / "commands" / "remember.md", "Remember $ARGUMENTS\n")

    result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    claude_skill = workspace / ".claude" / "skills" / "demo"
    assert is_link(claude_skill)
    assert claude_skill.resolve() == (workspace / "skills" / "demo").resolve()
    assert result.custom_installed == 1


def test_sync_restamps_stale_cap_markers(tmp_path: Path) -> None:
    """Sync migrates a former-default cap stamp to the shipped default."""
    import re

    from ciao.memory_tool import ensure_regions

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    guide = workspace / "AGENTS.md"
    guide.write_text("# Guide\n\n", encoding="utf-8", newline="")
    ensure_regions(guide)
    stamped = re.sub(
        r"(<!-- ciao:memory:start cap=)\d+( -->)",
        r"\g<1>2200\g<2>",
        guide.read_text(encoding="utf-8"),
    )
    guide.write_text(stamped, encoding="utf-8", newline="")

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    text = guide.read_text(encoding="utf-8")
    assert "cap=2200" not in text
    assert "<!-- ciao:memory:start cap=3000 -->" in text


def test_sync_preserves_agents_canonical_upstream_skill(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    canonical = workspace / ".agents" / "skills" / "upstream"
    _write(canonical / "SKILL.md", "# Upstream package\n")
    claude_link = workspace / ".claude" / "skills" / "upstream"
    claude_link.parent.mkdir(parents=True)
    link_dir(canonical, claude_link)

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    assert canonical.is_dir()
    assert not canonical.is_symlink()
    assert (canonical / "SKILL.md").read_text(encoding="utf-8") == "# Upstream package\n"
    assert is_link(claude_link)
    assert claude_link.resolve() == canonical.resolve()


def test_sync_prunes_legacy_codex_agents_skills_symlinks(tmp_path: Path) -> None:
    """Codex-era `.agents/skills` symlinks are pruned; user content is kept.

    The removed `codex_skills` mirror wrote `.agents/skills/<name>` symlinks
    back into the workspace catalogs and sync no longer maintains them, so
    leftovers — live or broken — are removed and counted. Real directories
    (upstream `skills`-CLI installs, user skills) and symlinks pointing
    anywhere else are left untouched.
    """
    workspace = tmp_path / "workspace"
    _write(workspace / "skills" / "kept" / "SKILL.md", "# Kept\n")
    agents_skills = workspace / ".agents" / "skills"
    agents_skills.mkdir(parents=True)
    # Live Codex-era leftovers in both historical shapes.
    link_dir(workspace / "skills" / "kept", agents_skills / "kept", relative_to=agents_skills)
    _dangling_dir_link(
        workspace / ".claude" / "skills" / "also-kept",
        agents_skills / "also-kept",
        relative_to=agents_skills,
    )
    # Broken leftover: its skill is gone, so sync cannot relink it.
    _dangling_dir_link(
        workspace / "skills" / "stale", agents_skills / "stale", relative_to=agents_skills
    )
    # User-owned content sync must never touch.
    _write(agents_skills / "upstream" / "SKILL.md", "# Upstream package\n")
    _dangling_dir_link(tmp_path / "outside" / "somewhere-else", agents_skills / "elsewhere")
    # Foreign same-name links must survive: same basename and a /skills/
    # segment, but outside this workspace's catalogs.
    _dangling_dir_link(tmp_path / "outside" / "skills" / "foreign", agents_skills / "foreign")

    result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    assert result.legacy_codex_pruned == 3
    remaining = {entry.name for entry in agents_skills.iterdir()}
    assert remaining == {"upstream", "elsewhere", "foreign"}
    assert (agents_skills / "upstream" / "SKILL.md").is_file()
    # The live skill itself still syncs into the maintained catalog.
    assert is_link(workspace / ".claude" / "skills" / "kept")


def test_sync_workspace_skills_prunes_orphaned_custom_links(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write(workspace / "skills" / "kept" / "SKILL.md")
    (workspace / ".claude" / "skills").mkdir(parents=True)
    _dangling_dir_link(workspace / "skills" / "stale", workspace / ".claude" / "skills" / "stale")

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
    assert is_link(agent_link)
    assert link_source(agent_link) == (workspace / "subagents" / "research.md").resolve()
    assert is_link(command_link)
    assert link_source(command_link) == (workspace / "commands" / "remember.md").resolve()
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
    assert is_link(link)
    assert link_source(link) == canonical.resolve()


def _stock_command_bytes(name: str) -> bytes:
    from importlib import resources

    return resources.files("ciao.stock").joinpath(f"commands/{name}").read_bytes()


def test_stock_command_seed_writes_marker(tmp_path: Path) -> None:
    import hashlib

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    canonical = workspace / "commands" / "remember.md"
    assert canonical.is_file()
    marker = workspace / "commands" / "remember.md.ciao-stock-command"
    assert marker.is_file()
    assert marker.read_text(encoding="utf-8").strip() == hashlib.sha256(
        canonical.read_bytes()
    ).hexdigest()
    assert result.stock_commands_seeded == 3


def test_unmodified_stock_command_is_refreshed(tmp_path: Path) -> None:
    import hashlib

    workspace = tmp_path / "workspace"
    canonical = workspace / "commands" / "remember.md"
    old = b"# Old stock remember\n"
    _write(canonical, old.decode("utf-8"))
    marker = workspace / "commands" / "remember.md.ciao-stock-command"
    marker.write_text(hashlib.sha256(old).hexdigest(), encoding="utf-8", newline="")

    result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    text = canonical.read_text(encoding="utf-8")
    assert "ciao:memory" in text
    assert result.stock_commands_refreshed == 1
    assert marker.read_text(encoding="utf-8").strip() == hashlib.sha256(
        canonical.read_bytes()
    ).hexdigest()


def test_edited_stock_command_is_left_alone_and_unmarked(tmp_path: Path) -> None:
    import hashlib

    workspace = tmp_path / "workspace"
    canonical = workspace / "commands" / "remember.md"
    _write(canonical, "# My edit\n")
    marker = workspace / "commands" / "remember.md.ciao-stock-command"
    # Hash of bytes other than what is on disk: simulates a user edit.
    marker.write_text(hashlib.sha256(b"something else\n").hexdigest(), encoding="utf-8", newline="")

    result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    assert canonical.read_text(encoding="utf-8") == "# My edit\n"
    assert not marker.exists()
    assert result.stock_commands_customised == 1

    again = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)
    assert canonical.read_text(encoding="utf-8") == "# My edit\n"
    assert not marker.exists()
    assert again.stock_commands_customised == 1


def test_pre_existing_identical_copy_is_adopted(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    canonical = workspace / "commands" / "remember.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(_stock_command_bytes("remember.md"))

    result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    marker = workspace / "commands" / "remember.md.ciao-stock-command"
    assert marker.is_file()
    assert result.stock_commands_seeded == 2
    assert result.stock_commands_refreshed == 0


def test_unmarked_older_shipped_revision_is_refreshed(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    import hashlib

    workspace = tmp_path / "workspace"
    canonical = workspace / "commands" / "remember.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"# Old shipped remember\n")
    monkeypatch.setitem(
        sync_skills.SHIPPED_STOCK_COMMAND_DIGESTS,
        "remember.md",
        frozenset({sync_skills._digest(b"# Old shipped remember\n")}),
    )

    result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    text = canonical.read_text(encoding="utf-8")
    assert "ciao:memory" in text
    marker = workspace / "commands" / "remember.md.ciao-stock-command"
    assert marker.is_file()
    assert marker.read_text(encoding="utf-8").strip() == hashlib.sha256(
        canonical.read_bytes()
    ).hexdigest()
    assert result.stock_commands_refreshed == 1
    assert result.stock_commands_customised == 0


def test_unmarked_unknown_text_is_still_customised(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    workspace = tmp_path / "workspace"
    canonical = workspace / "commands" / "remember.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"# My own remember\n")
    monkeypatch.setitem(
        sync_skills.SHIPPED_STOCK_COMMAND_DIGESTS,
        "remember.md",
        frozenset({sync_skills._digest(b"# Old shipped remember\n")}),
    )

    result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    assert canonical.read_bytes() == b"# My own remember\n"
    assert not (workspace / "commands" / "remember.md.ciao-stock-command").exists()
    assert result.stock_commands_customised == 1


def test_stock_command_revision_registry_is_current() -> None:
    from importlib import resources

    stock_commands = resources.files("ciao.stock").joinpath("commands")
    for entry in stock_commands.iterdir():
        if not entry.name.endswith(".md"):
            continue
        digest = sync_skills._digest(entry.read_bytes())
        assert digest in sync_skills.SHIPPED_STOCK_COMMAND_DIGESTS[entry.name], (
            f"{entry.name} changed: append the digest {digest} of the new text "
            "to SHIPPED_STOCK_COMMAND_DIGESTS in ciao/sync_skills.py"
        )


def test_pre_existing_different_copy_without_marker_is_customised(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    canonical = workspace / "commands" / "remember.md"
    _write(canonical, "# My own take\n")

    result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    assert canonical.read_text(encoding="utf-8") == "# My own take\n"
    assert not (workspace / "commands" / "remember.md.ciao-stock-command").exists()
    assert result.stock_commands_customised == 1


def test_stale_stock_command_is_pruned_only_when_unmodified(tmp_path: Path) -> None:
    import hashlib

    workspace = tmp_path / "workspace"
    commands_dir = workspace / "commands"
    commands_dir.mkdir(parents=True)
    gone = commands_dir / "gone.md"
    gone_bytes = b"# Once packaged\n"
    gone.write_bytes(gone_bytes)
    (commands_dir / "gone.md.ciao-stock-command").write_text(
        hashlib.sha256(gone_bytes).hexdigest(), encoding="utf-8", newline=""
    )
    gone_edited = commands_dir / "gone-edited.md"
    gone_edited.write_bytes(b"# My edit of a stale command\n")
    (commands_dir / "gone-edited.md.ciao-stock-command").write_text(
        hashlib.sha256(b"different bytes\n").hexdigest(), encoding="utf-8", newline=""
    )

    result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    assert not gone.exists()
    assert not (workspace / "commands" / "gone.md.ciao-stock-command").exists()
    assert gone_edited.read_text(encoding="utf-8") == "# My edit of a stale command\n"
    assert not (workspace / "commands" / "gone-edited.md.ciao-stock-command").exists()
    assert result.stock_commands_pruned == 1


def test_symlinked_stock_command_is_ignored(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    other = workspace / "commands" / "my-own-remember.md"
    _write(other, "# My own command\n")
    commands_dir = workspace / "commands"
    _user_file_symlink(commands_dir / "remember.md", other)

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    assert (commands_dir / "remember.md").is_symlink()
    assert other.read_text(encoding="utf-8") == "# My own command\n"
    assert not (commands_dir / "remember.md.ciao-stock-command").exists()


def test_symlinked_stock_command_marker_is_never_followed(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    commands_dir = tmp_path / "workspace" / "commands"
    commands_dir.mkdir(parents=True)
    target = tmp_path / "secret.env"
    target.write_text("SECRET=1", encoding="utf-8", newline="")
    for present in (True, False):
        canonical = commands_dir / "remember.md"
        if present:
            _write(canonical, "# Pre-existing\n")
        else:
            canonical.unlink(missing_ok=True)
        marker = commands_dir / "remember.md.ciao-stock-command"
        if marker.exists() or marker.is_symlink():
            marker.unlink()
        _user_file_symlink(marker, target)

        sync_skills.sync_workspace_skills(tmp_path / "workspace", refresh_upstream=False)

        assert target.read_text(encoding="utf-8") == "SECRET=1"
        assert marker.is_symlink()
        assert marker.resolve() == target.resolve()
        if present:
            assert canonical.read_text(encoding="utf-8") == "# Pre-existing\n"
        else:
            assert not canonical.exists()


def test_stock_command_marker_written_atomically(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    assert (workspace / "commands" / "remember.md.ciao-stock-command").is_file()
    assert not list((workspace / "commands").glob("*.tmp"))


def test_sync_installs_stock_skills_with_marker(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    installed = workspace / ".claude" / "skills" / "ciao-capabilities"
    assert (installed / "SKILL.md").is_file()
    assert (installed / sync_skills.STOCK_SKILL_MARKER).is_file()
    assert not installed.is_symlink()
    assert result.stock_installed >= 3


def test_workspace_skill_shadows_stock_skill(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write(workspace / "skills" / "web-research" / "SKILL.md", "# My override\n")

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    link = workspace / ".claude" / "skills" / "web-research"
    assert is_link(link)
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


def test_sync_ignores_skills_lock(tmp_path: Path) -> None:
    # skills-lock.json is now inert; sync must not touch .agents/skills for lock entries
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
        encoding="utf-8", newline=""
    )
    canonical = workspace / ".agents" / "skills" / "upstream"
    _write(canonical / "SKILL.md", "# Upstream\n")
    result = sync_skills.sync_workspace_skills(workspace)
    # Upstream still present (not auto-pruned via lock), but no npx called
    assert (canonical / "SKILL.md").is_file()
    assert result.upstream_updated == 0
    assert result.upstream_pruned == 0


def test_sync_installs_memory_skill_without_stock_agents(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    assert (workspace / ".claude" / "skills" / "ciao-memory" / "SKILL.md").is_file()
    assert result.stock_agents_installed == 0
    assert not list((workspace / ".claude" / "agents").glob("*.md"))


def test_sync_prunes_retired_memory_agent_on_upgrade(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    memory = workspace / ".claude" / "agents" / "memory.md"
    _write(memory, "# Old memory agent\n")
    sync_skills._mark_stock_agent(memory)

    _write(
        workspace / ".opencode" / "agents" / "memory.md",
        f"{sync_skills.OPENCODE_GENERATED_MARKER}\n# Old memory projection\n",
    )

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    assert not memory.exists()
    assert not sync_skills._stock_agent_marker(memory).exists()
    assert not (workspace / ".opencode" / "agents" / "memory.md").exists()
    assert (workspace / ".claude" / "skills" / "ciao-memory" / "SKILL.md").exists()


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


def test_sync_prunes_retired_stock_agents_but_preserves_custom_ones(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    for name in ("researcher", "secretary"):
        stock = workspace / ".claude" / "agents" / f"{name}.md"
        _write(stock, f"# Old {name} stock agent\n")
        sync_skills._mark_stock_agent(stock)
        _write(
            workspace / ".opencode" / "agents" / f"{name}.md",
            f"{sync_skills.OPENCODE_GENERATED_MARKER}\n# Old {name} projection\n",
        )

    sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    for name in ("researcher", "secretary"):
        assert not (workspace / ".claude" / "agents" / f"{name}.md").exists()
        assert not (workspace / ".opencode" / "agents" / f"{name}.md").exists()

    custom = workspace / "subagents" / "researcher.md"
    _write(custom, "# My researcher\n")
    result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    assert result.stock_agents_installed == 0
    assert link_source(workspace / ".claude" / "agents" / "researcher.md") == custom.resolve()
    assert custom.read_text(encoding="utf-8") == "# My researcher\n"


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
    assert is_link(link)
    assert link_source(link) == custom.resolve()
    assert custom.read_text(encoding="utf-8") == "# Custom memory\n"


def _boom_guide_unwritable(*a, **k):
    raise RuntimeError("guide unwritable")


def test_sync_reports_memory_region_failure_and_keeps_syncing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """A broken memory step must not raise, and must be reported on the
    result instead of only in the log (#790)."""
    monkeypatch.setattr("ciao.memory_tool.ensure_regions", _boom_guide_unwritable)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with caplog.at_level(logging.ERROR):
        result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    assert result.memory_error == "RuntimeError: guide unwritable"
    # Skill sync still ran to completion.
    assert result.stock_installed > 0
    assert (workspace / ".claude" / "skills").is_dir()
    # Startup triage counts error-log content, so the record must stay.
    assert "memory region ensure/migrate failed" in caplog.text


def test_sync_memory_error_is_none_on_the_normal_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = sync_skills.sync_workspace_skills(workspace, refresh_upstream=False)

    assert result.memory_error is None


def test_main_returns_nonzero_and_warns_when_memory_step_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("ciao.memory_tool.ensure_regions", _boom_guide_unwritable)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    rc = sync_skills.main(["--workspace", str(workspace)])

    assert rc == cli.SETUP_MEMORY_FAILED_RC
    err = capsys.readouterr().err
    assert "memory regions not set up" in err
    assert "guide unwritable" in err


def test_main_returns_zero_on_the_normal_path(
    capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    rc = sync_skills.main(["--workspace", str(workspace)])

    assert rc == 0
    assert "Warning" not in capsys.readouterr().err
