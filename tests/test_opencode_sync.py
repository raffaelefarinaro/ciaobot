"""opencode workspace-asset projection.

opencode discovers `.claude/skills`, `.agents/skills`, `AGENTS.md`, and
`CLAUDE.md` natively (verified against opencode 1.18), so only subagents,
commands, and MCP servers are generated. These tests cover the generated
files, idempotence, marker-only pruning, and the credential guard.
"""

from __future__ import annotations

import json
from pathlib import Path

from ciao.sync_skills import (
    OPENCODE_GENERATED_MARKER,
    OPENCODE_MANAGED_MCPS_FILE,
    _install_opencode_agents,
    _install_opencode_commands,
    _install_opencode_mcps,
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


# ── MCP servers ─────────────────────────────────────────────────────────


def _write_mcp(root: Path, servers: dict) -> None:
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


def test_remote_and_local_servers_are_projected(tmp_path):
    root = _workspace(tmp_path)
    _write_mcp(root, {
        "remote-thing": {
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer ${THING_TOKEN}"},
        },
        "local-thing": {"command": "npx", "args": ["-y", "thing-mcp"]},
    })

    assert _install_opencode_mcps(root) == (2, 0)

    config = json.loads((root / "opencode.json").read_text(encoding="utf-8"))
    assert config["mcp"]["remote-thing"] == {
        "type": "remote",
        "url": "https://example.com/mcp",
        "enabled": True,
        "headers": {"Authorization": "Bearer {env:THING_TOKEN}"},
    }
    assert config["mcp"]["local-thing"]["command"] == ["npx", "-y", "thing-mcp"]


def test_the_ciaobot_control_plane_server_is_excluded(tmp_path):
    """Its URL and token are per-chat and injected at spawn, never written out."""
    root = _workspace(tmp_path)
    _write_mcp(root, {"ciaobot": {"url": "http://127.0.0.1:1/mcp"}})

    assert _install_opencode_mcps(root) == (0, 0)
    assert "mcp" not in json.loads((root / "opencode.json").read_text(encoding="utf-8"))


def test_literal_secrets_are_dropped_not_copied(tmp_path):
    root = _workspace(tmp_path)
    _write_mcp(root, {
        "thing": {
            "command": "npx",
            "env": {"GOOD": "${GOOD_KEY}", "LEAKED": "literal-secret-value"},
        }
    })

    _install_opencode_mcps(root)

    rendered = (root / "opencode.json").read_text(encoding="utf-8")
    assert "literal-secret-value" not in rendered
    assert "${GOOD_KEY}" in rendered


def test_user_declared_servers_survive(tmp_path):
    root = _workspace(tmp_path)
    (root / "opencode.json").write_text(
        json.dumps({"mcp": {"mine": {"type": "local", "command": ["mine"]}}}), encoding="utf-8"
    )
    _write_mcp(root, {"thing": {"command": "npx"}})

    _install_opencode_mcps(root)

    config = json.loads((root / "opencode.json").read_text(encoding="utf-8"))
    assert config["mcp"]["mine"] == {"type": "local", "command": ["mine"]}
    assert "thing" in config["mcp"]


def test_a_user_name_collision_is_not_overwritten(tmp_path):
    root = _workspace(tmp_path)
    (root / "opencode.json").write_text(
        json.dumps({"mcp": {"thing": {"type": "local", "command": ["mine"]}}}), encoding="utf-8"
    )
    _write_mcp(root, {"thing": {"command": "theirs"}})

    _install_opencode_mcps(root)

    config = json.loads((root / "opencode.json").read_text(encoding="utf-8"))
    assert config["mcp"]["thing"]["command"] == ["mine"]


def test_a_user_name_collision_survives_repeated_syncs(tmp_path):
    """The collision guard must not expire after one run.

    The sidecar records what Ciaobot *owns*, and a skipped collision is not
    owned. Recording every desired name instead made the second sync treat the
    user's entry as previously-managed: it was overwritten, and pruned outright
    once `.mcp.json` dropped the name.
    """
    root = _workspace(tmp_path)
    (root / "opencode.json").write_text(
        json.dumps({"mcp": {"thing": {"type": "local", "command": ["mine"]}}}), encoding="utf-8"
    )
    _write_mcp(root, {"thing": {"command": "theirs"}})

    _install_opencode_mcps(root)
    _install_opencode_mcps(root)

    config = json.loads((root / "opencode.json").read_text(encoding="utf-8"))
    assert config["mcp"]["thing"]["command"] == ["mine"]
    sidecar = root / ".opencode" / OPENCODE_MANAGED_MCPS_FILE
    assert json.loads(sidecar.read_text(encoding="utf-8")) == []

    # And dropping it upstream must not prune the user's own entry.
    _write_mcp(root, {})
    _install_opencode_mcps(root)
    config = json.loads((root / "opencode.json").read_text(encoding="utf-8"))
    assert config["mcp"]["thing"]["command"] == ["mine"]


def test_removing_a_server_prunes_only_the_managed_one(tmp_path):
    root = _workspace(tmp_path)
    (root / ".opencode").mkdir(parents=True, exist_ok=True)
    (root / "opencode.json").write_text(
        json.dumps({"mcp": {"mine": {"type": "local", "command": ["mine"]}}}), encoding="utf-8"
    )
    _write_mcp(root, {"thing": {"command": "npx"}})
    _install_opencode_mcps(root)

    _write_mcp(root, {})
    installed, pruned = _install_opencode_mcps(root)

    assert (installed, pruned) == (0, 1)
    config = json.loads((root / "opencode.json").read_text(encoding="utf-8"))
    assert config["mcp"] == {"mine": {"type": "local", "command": ["mine"]}}


def test_managed_names_are_tracked_in_the_sidecar(tmp_path):
    """JSON has no comment syntax, so ownership lives beside the config."""
    root = _workspace(tmp_path)
    _write_mcp(root, {"thing": {"command": "npx"}})
    _install_opencode_mcps(root)

    sidecar = root / ".opencode" / OPENCODE_MANAGED_MCPS_FILE
    assert json.loads(sidecar.read_text(encoding="utf-8")) == ["thing"]


def test_mcp_sync_is_idempotent(tmp_path):
    root = _workspace(tmp_path)
    _write_mcp(root, {"thing": {"command": "npx"}})
    assert _install_opencode_mcps(root) == (1, 0)
    before = (root / "opencode.json").stat().st_mtime_ns
    assert _install_opencode_mcps(root) == (1, 0)
    assert (root / "opencode.json").stat().st_mtime_ns == before


def test_invalid_mcp_json_is_skipped_not_fatal(tmp_path):
    root = _workspace(tmp_path)
    (root / ".mcp.json").write_text("{not json", encoding="utf-8")
    assert _install_opencode_mcps(root) == (0, 0)


def test_invalid_opencode_json_is_left_alone(tmp_path):
    root = _workspace(tmp_path)
    (root / "opencode.json").write_text("{not json", encoding="utf-8")
    _write_mcp(root, {"thing": {"command": "npx"}})

    assert _install_opencode_mcps(root) == (0, 0)
    assert (root / "opencode.json").read_text(encoding="utf-8") == "{not json"
