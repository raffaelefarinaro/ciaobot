"""opencode workspace-asset projection.

OpenCode discovers `.claude/skills`, `.agents/skills`, and `AGENTS.md`
natively (verified against OpenCode 2.0.16), so only subagents and commands are generated. MCP configurations remain provider-owned. These tests cover
files, idempotence, marker-only pruning, and preservation of native configs.
"""

from __future__ import annotations

import json
from pathlib import Path

from ciao.sync_skills import (
    OPENCODE_GENERATED_MARKER,
    _install_opencode_agents,
    _install_opencode_commands,
    _mirror_dir_symlinks,
)


def _workspace(tmp_path: Path) -> Path:
    for name in ("subagents", "commands", ".claude/agents", ".claude/commands"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _canonical_subagent(root: Path, name: str, description: str, body: str) -> None:
    (root / "subagents" / f"{name}.md").write_text(
        f"---\ndescription: {description}\n---\n\n{body}\n", encoding="utf-8"
    )
    _mirror_dir_symlinks(
        root / "subagents", root / ".claude" / "agents",
        glob_pattern="*.md", prune_regular=False,
    )


def _canonical_command(root: Path, name: str, description: str, body: str) -> None:
    (root / "commands" / f"{name}.md").write_text(
        f"---\ndescription: {description}\n---\n\n{body}\n", encoding="utf-8"
    )
    _mirror_dir_symlinks(
        root / "commands", root / ".claude" / "commands",
        glob_pattern="*.md", prune_regular=False,
    )


# ── subagents ───────────────────────────────────────────────────────────


def test_subagent_is_projected_with_subagent_mode(tmp_path):
    root = _workspace(tmp_path)
    _canonical_subagent(root, "researcher", "Researches things.", "You research.")

    assert _install_opencode_agents(root) == (1, 0)

    text = (root / ".opencode" / "agents" / "researcher.md").read_text(encoding="utf-8")
    assert "mode: subagent" in text
    assert '"Researches things."' in text
    assert "You research." in text
    assert OPENCODE_GENERATED_MARKER in text


def test_subagent_projection_omits_tools_and_model(tmp_path):
    """Omission inherits the parent's, which beats translating Claude tool names."""
    root = _workspace(tmp_path)
    _canonical_subagent(root, "researcher", "Researches.", "Body.")
    _install_opencode_agents(root)

    text = (root / ".opencode" / "agents" / "researcher.md").read_text(encoding="utf-8")
    assert "tools:" not in text
    assert "model:" not in text


def test_subagent_without_a_body_is_skipped(tmp_path):
    root = _workspace(tmp_path)
    (root / "subagents" / "empty.md").write_text("---\ndescription: Nothing.\n---\n", encoding="utf-8")
    _mirror_dir_symlinks(
        root / "subagents", root / ".claude" / "agents",
        glob_pattern="*.md", prune_regular=False,
    )
    assert _install_opencode_agents(root) == (0, 0)


def test_subagent_sync_is_idempotent(tmp_path):
    root = _workspace(tmp_path)
    _canonical_subagent(root, "researcher", "Researches.", "Body.")
    assert _install_opencode_agents(root) == (1, 0)
    target = root / ".opencode" / "agents" / "researcher.md"
    before = target.stat().st_mtime_ns
    assert _install_opencode_agents(root) == (1, 0)
    # Unchanged content must not be rewritten: sync runs on every startup and
    # needless mtime churn shows up as spurious file-watcher activity.
    assert target.stat().st_mtime_ns == before


def test_removing_the_source_prunes_the_projection(tmp_path):
    root = _workspace(tmp_path)
    _canonical_subagent(root, "researcher", "Researches.", "Body.")
    _install_opencode_agents(root)

    (root / "subagents" / "researcher.md").unlink()
    (root / ".claude" / "agents" / "researcher.md").unlink()

    assert _install_opencode_agents(root) == (0, 1)
    assert not (root / ".opencode" / "agents" / "researcher.md").exists()


def test_a_user_authored_agent_is_never_touched(tmp_path):
    root = _workspace(tmp_path)
    target_root = root / ".opencode" / "agents"
    target_root.mkdir(parents=True)
    mine = target_root / "mine.md"
    mine.write_text("---\ndescription: Hand written.\n---\n\nMine.\n", encoding="utf-8")

    _canonical_subagent(root, "researcher", "Researches.", "Body.")
    installed, pruned = _install_opencode_agents(root)

    assert (installed, pruned) == (1, 0)
    assert mine.read_text(encoding="utf-8").endswith("Mine.\n")


def test_a_name_collision_leaves_the_user_file_in_force(tmp_path):
    root = _workspace(tmp_path)
    target_root = root / ".opencode" / "agents"
    target_root.mkdir(parents=True)
    (target_root / "researcher.md").write_text("Hand written.\n", encoding="utf-8")

    _canonical_subagent(root, "researcher", "Researches.", "Body.")

    assert _install_opencode_agents(root) == (0, 0)
    assert (target_root / "researcher.md").read_text(encoding="utf-8") == "Hand written.\n"


# ── commands ────────────────────────────────────────────────────────────


def test_command_is_projected_with_arguments_intact(tmp_path):
    root = _workspace(tmp_path)
    _canonical_command(root, "relprobe", "Cuts a release.", "Release $ARGUMENTS now.")

    assert _install_opencode_commands(root) == (1, 0)

    text = (root / ".opencode" / "commands" / "relprobe.md").read_text(encoding="utf-8")
    # opencode uses the same $ARGUMENTS placeholder, so no conversion happens.
    assert "Release $ARGUMENTS now." in text
    assert OPENCODE_GENERATED_MARKER in text


def test_command_sync_prunes_only_generated_files(tmp_path):
    root = _workspace(tmp_path)
    target_root = root / ".opencode" / "commands"
    target_root.mkdir(parents=True)
    (target_root / "handwritten.md").write_text("Mine.\n", encoding="utf-8")
    _canonical_command(root, "relprobe", "Cuts a release.", "Body.")
    _install_opencode_commands(root)

    (root / "commands" / "relprobe.md").unlink()
    (root / ".claude" / "commands" / "relprobe.md").unlink()
    installed, pruned = _install_opencode_commands(root)

    assert (installed, pruned) == (0, 1)
    assert (target_root / "handwritten.md").exists()


# MCP configs already projected by older installs become provider-owned.
def test_skill_sync_preserves_native_mcp_configs(tmp_path, monkeypatch):
    from ciao.sync_skills import sync_workspace_skills

    root = _workspace(tmp_path)
    native = root / "opencode.json"
    claude = root / ".mcp.json"
    sidecar = root / ".opencode" / ".ciao-managed-mcps.json"
    sidecar.parent.mkdir(exist_ok=True)
    sidecar.write_text('["notion"]', encoding="utf-8")
    native.write_text('{"mcp":{"notion":{"type":"local","command":["mine"]}}}', encoding="utf-8")
    claude.write_text('{"mcpServers":{"notion":{"command":"different"}}}', encoding="utf-8")
    before = (native.read_bytes(), claude.read_bytes())
    monkeypatch.setenv("CIAO_MEMORY_DIR", str(tmp_path / "memory"))

    sync_workspace_skills(root, refresh_upstream=False, gws_profile="", workspace_name="test")
    sync_workspace_skills(root, refresh_upstream=False, gws_profile="", workspace_name="test")

    assert (native.read_bytes(), claude.read_bytes()) == before
    assert not sidecar.exists()
