"""Tests for the per-workspace MCP server allowlist.

``allowed_mcp_servers`` names the ``.mcp.json`` servers a workspace may reach;
every other declared server is denied. ``None`` means not-yet-decided and
denies everything (the fail-closed default for anything new). Existing
workspaces are seeded at load from what they can reach today.
"""

from __future__ import annotations

import json
from pathlib import Path

from ciao.config import CiaoConfig, _DEFAULT_HARNESS_DISALLOWED_TOOLS

_MCP_JSON = {
    "mcpServers": {
        "alpha": {"type": "http", "url": "http://example.invalid/alpha"},
        "beta": {"command": "npx", "args": ["beta"]},
    }
}


def _config(tmp_path: Path, **overrides: object) -> CiaoConfig:
    env: dict[str, str] = {
        "PWA_AUTH_TOKEN": "t",
        "CIAO_WORKSPACE": str(tmp_path),
        "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
    }
    for key, value in overrides.items():
        env[key] = str(value)
    return CiaoConfig.from_env(env)


def _write_mcp_json(tmp_path: Path, content: object = _MCP_JSON) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps(content, indent=2), encoding="utf-8"
    )


def _write_registry(tmp_path: Path, entries: list[dict]) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "workspaces.json").write_text(
        json.dumps(entries, indent=2), encoding="utf-8"
    )


def _mcp_denials(tools: list[str]) -> list[str]:
    return [t for t in tools if t.startswith("mcp__")]


def _registry_entries(tmp_path: Path) -> list[dict]:
    path = tmp_path / ".runtime" / "workspaces.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_allowlist_names_one_of_two_servers(tmp_path: Path) -> None:
    _write_mcp_json(tmp_path)
    _write_registry(
        tmp_path,
        [{"name": "personal", "allowed_mcp_servers": ["alpha"]}],
    )
    config = _config(tmp_path)
    denials = _mcp_denials(config.disallowed_tools_for_workspace("personal"))
    assert denials == ["mcp__beta"]


def test_empty_allowlist_denies_every_declared_server(tmp_path: Path) -> None:
    _write_mcp_json(tmp_path)
    _write_registry(
        tmp_path,
        [{"name": "personal", "allowed_mcp_servers": []}],
    )
    config = _config(tmp_path)
    denials = _mcp_denials(config.disallowed_tools_for_workspace("personal"))
    assert sorted(denials) == ["mcp__alpha", "mcp__beta"]


def test_none_on_new_workspace_denies_every_declared_server(tmp_path: Path) -> None:
    # No registry file at all: the legacy fallback creates workspaces in code,
    # so their allowlist stays None and every declared server is denied.
    _write_mcp_json(tmp_path)
    config = _config(tmp_path)
    for name in ("personal", "work"):
        denials = _mcp_denials(config.disallowed_tools_for_workspace(name))
        assert sorted(denials) == ["mcp__alpha", "mcp__beta"]


def test_migration_seeds_personal_and_work_shapes(tmp_path: Path) -> None:
    # personal has no extra disallowed tools, so both declared servers survive.
    # work already denies notion (via mcp__notion), so it is not seeded back in.
    _write_mcp_json(tmp_path)
    _write_registry(
        tmp_path,
        [
            {"name": "personal", "disallowed_tools": None},
            {
                "name": "work",
                "disallowed_tools": ["Bash", "mcp__notion"],
            },
        ],
    )
    config = _config(tmp_path)
    assert config.workspace("personal").allowed_mcp_servers == ["alpha", "beta"]
    assert config.workspace("work").allowed_mcp_servers == ["alpha", "beta"]
    # notion is not declared in this .mcp.json, so nothing about it is seeded;
    # its deny in work's disallowed_tools is untouched.
    assert "mcp__notion" in config.disallowed_tools_for_workspace("work")
    # Both servers remain reachable for personal after seeding.
    assert _mcp_denials(config.disallowed_tools_for_workspace("personal")) == []


def test_migration_does_not_seed_already_denied_server(tmp_path: Path) -> None:
    # beta is already denied by work's disallowed_tools, so the seed must not
    # put it back into the allowlist.
    _write_mcp_json(tmp_path)
    _write_registry(
        tmp_path,
        [{"name": "work", "disallowed_tools": ["mcp__beta"]}],
    )
    config = _config(tmp_path)
    assert config.workspace("work").allowed_mcp_servers == ["alpha"]
    # beta stays denied via the allowlist exclusion.
    denials = _mcp_denials(config.disallowed_tools_for_workspace("work"))
    assert denials == ["mcp__beta"]

def test_migration_is_idempotent(tmp_path: Path) -> None:
    _write_mcp_json(tmp_path)
    _write_registry(
        tmp_path,
        [{"name": "personal", "disallowed_tools": None}],
    )
    config = _config(tmp_path)
    first = config.workspace("personal").allowed_mcp_servers
    second = CiaoConfig.from_env(
        {
            "PWA_AUTH_TOKEN": "t",
            "CIAO_WORKSPACE": str(tmp_path),
            "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
        }
    ).workspace("personal").allowed_mcp_servers
    assert first == ["alpha", "beta"]
    assert second == first


def test_existing_empty_allowlist_is_not_reseeded(tmp_path: Path) -> None:
    # The rejected attempt persisted ``allowed_mcp_servers: []`` into the live
    # registry. ``[]`` is an explicit "reach nothing" opt-out, so the migration
    # must leave it alone (only ``None`` is seeded) and every declared server
    # stays denied. This documents the residue left on a real install.
    _write_mcp_json(tmp_path)
    _write_registry(
        tmp_path,
        [{"name": "personal", "allowed_mcp_servers": []}],
    )
    config = _config(tmp_path)
    assert config.workspace("personal").allowed_mcp_servers == []
    assert sorted(_mcp_denials(config.disallowed_tools_for_workspace("personal"))) == [
        "mcp__alpha",
        "mcp__beta",
    ]


def test_migration_keeps_unrelated_unknown_keys(tmp_path: Path) -> None:
    _write_mcp_json(tmp_path)
    _write_registry(
        tmp_path,
        [
            {
                "name": "personal",
                "vault_root": "memory-vault/personal",
                "disallowed_tools": None,
                "future_field": {"nested": 1},
            }
        ],
    )
    config = _config(tmp_path)
    entries = _registry_entries(tmp_path)
    personal = next(e for e in entries if e["name"] == "personal")
    # The unknown key survives the migration write untouched.
    assert personal["future_field"] == {"nested": 1}
    # And the allowlist was added alongside it.
    assert personal["allowed_mcp_servers"] == ["alpha", "beta"]


def test_missing_mcp_json_denies_nothing_mcp(tmp_path: Path) -> None:
    _write_registry(
        tmp_path,
        [{"name": "personal", "allowed_mcp_servers": ["alpha", "beta"]}],
    )
    config = _config(tmp_path)
    assert _mcp_denials(config.disallowed_tools_for_workspace("personal")) == []


def test_malformed_mcp_json_denies_known_allowlisted_names_by_name(tmp_path: Path) -> None:
    _write_mcp_json(tmp_path, "not json at all")
    _write_registry(
        tmp_path,
        [
            {"name": "personal", "allowed_mcp_servers": ["alpha", "beta"]},
            {"name": "work", "allowed_mcp_servers": ["gamma"]},
        ],
    )
    config = _config(tmp_path)
    denials = _mcp_denials(config.disallowed_tools_for_workspace("personal"))
    # The known universe (every allowlisted name across workspaces) is denied by
    # explicit name, and no glob is emitted.
    assert sorted(denials) == ["mcp__alpha", "mcp__beta", "mcp__gamma"]
    assert not any("*" in d for d in denials)


def test_harness_defaults_still_present_alongside_mcp_denials(tmp_path: Path) -> None:
    _write_mcp_json(tmp_path)
    _write_registry(
        tmp_path,
        [{"name": "personal", "allowed_mcp_servers": ["alpha"]}],
    )
    config = _config(tmp_path)
    tools = config.disallowed_tools_for_workspace("personal")
    for default in _DEFAULT_HARNESS_DISALLOWED_TOOLS:
        assert default in tools
    assert "mcp__beta" in tools


def test_allowlist_round_trips_through_registry(tmp_path: Path) -> None:
    _write_mcp_json(tmp_path)
    _write_registry(
        tmp_path,
        [{"name": "personal", "allowed_mcp_servers": ["alpha"]}],
    )
    config = _config(tmp_path)
    # Rewrite through the normal persistence path, then reload.
    config.persist_workspace_registry()
    reloaded = CiaoConfig.from_env(
        {
            "PWA_AUTH_TOKEN": "t",
            "CIAO_WORKSPACE": str(tmp_path),
            "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
        }
    )
    assert reloaded.workspace("personal").allowed_mcp_servers == ["alpha"]
