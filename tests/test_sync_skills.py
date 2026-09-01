from __future__ import annotations

import subprocess
import json
from pathlib import Path
from types import SimpleNamespace

from ciao import sync_skills


def _write(path: Path, text: str = "content\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_upstream_skills_removed(tmp_path: Path) -> None:
    # GitHub/upstream sync surface has been removed per the simplification plan.
    assert not hasattr(sync_skills, "_update_upstream_skills")
    assert not hasattr(sync_skills, "_refresh_upstream_skills")
    assert not hasattr(sync_skills, "SKILLS_NPX_TIMEOUT")


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
    marker.write_text(hashlib.sha256(old).hexdigest(), encoding="utf-8")

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
    marker.write_text(hashlib.sha256(b"something else\n").hexdigest(), encoding="utf-8")

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
        hashlib.sha256(gone_bytes).hexdigest(), encoding="utf-8"
    )
    gone_edited = commands_dir / "gone-edited.md"
    gone_edited.write_bytes(b"# My edit of a stale command\n")
    (commands_dir / "gone-edited.md.ciao-stock-command").write_text(
        hashlib.sha256(b"different bytes\n").hexdigest(), encoding="utf-8"
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
    (commands_dir / "remember.md").symlink_to(other)

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
    target.write_text("SECRET=1", encoding="utf-8")
    for present in (True, False):
        canonical = commands_dir / "remember.md"
        if present:
            _write(canonical, "# Pre-existing\n")
        else:
            canonical.unlink(missing_ok=True)
        marker = commands_dir / "remember.md.ciao-stock-command"
        if marker.exists() or marker.is_symlink():
            marker.unlink()
        marker.symlink_to(target)

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
        encoding="utf-8",
    )
    canonical = workspace / ".agents" / "skills" / "upstream"
    _write(canonical / "SKILL.md", "# Upstream\n")
    result = sync_skills.sync_workspace_skills(workspace)
    # Upstream still present (not auto-pruned via lock), but no npx called
    assert (canonical / "SKILL.md").is_file()
    assert result.upstream_updated == 0
    assert result.upstream_pruned == 0


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
