"""Tests for how MCP servers reach a workspace's Claude chats.

Every server declared for a workspace (its own agent-root ``.mcp.json``, or the
shared one) loads in its chats; only an ``mcp__<server>`` entry in the
workspace's ``disallowed_tools`` blocks one.
"""

from __future__ import annotations

import json
from pathlib import Path

from ciao.config import CiaoConfig

_MCP_JSON = {
    "mcpServers": {
        "alpha": {"type": "http", "url": "http://example.invalid/alpha"},
        "beta": {"command": "npx", "args": ["beta"]},
    }
}


def _config(tmp_path: Path) -> CiaoConfig:
    return CiaoConfig.from_env(
        {
            "PWA_AUTH_TOKEN": "t",
            "CIAO_WORKSPACE": str(tmp_path),
            "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
        }
    )


def _write_registry(tmp_path: Path, entries: list[dict]) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "workspaces.json").write_text(json.dumps(entries), encoding="utf-8")


def _write_mcp_json(root: Path, content: object = _MCP_JSON) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".mcp.json").write_text(json.dumps(content), encoding="utf-8")


def _mcp_denials(tools: list[str]) -> list[str]:
    return [t for t in tools if t.startswith("mcp__")]


def test_every_declared_server_reaches_the_chat(tmp_path: Path) -> None:
    _write_mcp_json(tmp_path)
    _write_registry(tmp_path, [{"name": "personal"}])
    config = _config(tmp_path)

    assert _mcp_denials(config.disallowed_tools_for_workspace("personal")) == []


def test_a_server_added_to_the_agent_root_is_not_blocked(tmp_path: Path) -> None:
    """The regression: a newly declared server used to be denied by default."""
    _write_mcp_json(tmp_path / "work")
    _write_registry(tmp_path, [{"name": "work"}])
    config = _config(tmp_path)

    assert config.agent_root("work") == config.workspace_root / "work"
    assert _mcp_denials(config.disallowed_tools_for_workspace("work")) == []


def test_disallowed_tools_still_blocks_one_server(tmp_path: Path) -> None:
    _write_mcp_json(tmp_path)
    _write_registry(
        tmp_path, [{"name": "personal", "disallowed_tools": ["mcp__beta"]}]
    )
    config = _config(tmp_path)

    assert _mcp_denials(config.disallowed_tools_for_workspace("personal")) == [
        "mcp__beta"
    ]


def test_a_stale_allowlist_key_is_ignored_and_dropped(tmp_path: Path) -> None:
    _write_mcp_json(tmp_path)
    _write_registry(
        tmp_path, [{"name": "personal", "allowed_mcp_servers": ["alpha"]}]
    )
    config = _config(tmp_path)

    assert _mcp_denials(config.disallowed_tools_for_workspace("personal")) == []
    config.persist_workspace_registry()
    entries = json.loads(
        (tmp_path / ".runtime" / "workspaces.json").read_text(encoding="utf-8")
    )
    assert "allowed_mcp_servers" not in entries[0]
