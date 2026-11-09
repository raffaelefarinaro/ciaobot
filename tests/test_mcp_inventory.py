from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from ciao import agent_paths
from ciao.web.mcp_inventory import _jsonc_to_json, _shell_prefix, provider_inventory

_key = agent_paths.claude_config_project_key


def _assume_no_git_above(tmp_path: Path) -> None:
    if any((parent / ".git").exists() for parent in tmp_path.resolve().parents):
        pytest.skip("tmp_path is inside a git repo")


class _NoRuntime:
    async def provider_mcp_runtime_connections(self, _workspace, _names):
        return {"claude": {}, "opencode": {}}


@pytest.fixture(autouse=True)
def isolate_native_user_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "native-home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)


def test_jsonc_parser_preserves_urls_and_removes_trailing_commas() -> None:
    assert _jsonc_to_json('{/* comment */"url":"https://host/path//part",}') == '{"url":"https://host/path//part"}'


@pytest.mark.asyncio
async def test_provider_inventory_merges_scopes_safely_and_marks_denied_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    (root / ".mcp.json").write_text(
        '{"mcpServers":{"allowed":{"command":"npx","args":["secret"],"env":{"TOKEN":"secret"}},"blocked":{"command":"x"}}}',
        encoding="utf-8",
    )
    (home / ".claude.json").write_text(
        '{"mcpServers":{"global":{"url":"https://private.invalid"}}}',
        encoding="utf-8",
    )
    (root / "opencode.jsonc").write_text(
        '{"mcp":{"servers":{"disabled":{"type":"local","command":"x","disabled":true}}}}',
        encoding="utf-8",
    )

    class Manager:
        async def provider_mcp_runtime_connections(self, workspace, names):
            assert workspace == "work"
            return {
                "claude": {"allowed": [{"state": "connected", "provider": "claude", "chat_id": "c1"}], "session-only": [{"state": "unknown", "provider": "claude", "chat_id": "c2"}], "claude.ai Salesloft": [{"state": "needs_auth", "provider": "claude", "chat_id": "c2"}]},
                "opencode": {},
            }

    payload = await provider_inventory(
        workspace="work", agent_root=root,
        explicitly_denied={"blocked"}, manager=Manager(), claude_available=True,
        opencode_available=False,
    )
    claude, opencode = payload["providers"]
    by_name = {row["name"]: row for row in claude["servers"]}
    assert by_name["allowed"]["allowed"] is None
    assert by_name["blocked"]["allowed"] is False
    assert by_name["global"]["allowed"] is None
    assert by_name["session-only"]["scope"] == "account"
    # A claude.ai connector is connected on claude.ai, not with a CLI command.
    assert by_name["claude.ai Salesloft"]["scope"] == "claudeai"
    assert by_name["allowed"]["connections"][0]["chat_id"] == "c1"
    assert opencode["servers"][0]["enabled"] is False
    encoded = repr(payload)
    assert "private.invalid" not in encoded
    assert "secret" not in encoded
    assert "TOKEN" not in encoded


@pytest.mark.asyncio
async def test_inventory_marks_invalid_native_config_unknown_and_keeps_unknown_connections(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "opencode.jsonc").write_text('{ invalid', encoding="utf-8")

    class Manager:
        async def provider_mcp_runtime_connections(self, _workspace, _names):
            return {"claude": {}, "opencode": {}}

    payload = await provider_inventory(
        workspace="work", agent_root=root,
        explicitly_denied=set(), manager=Manager(), claude_available=False,
        opencode_available=True,
    )
    opencode = payload["providers"][1]
    assert opencode["inventory_state"] == "unknown"
    assert opencode["servers"] == []
    assert opencode["available"] is True


@pytest.mark.asyncio
async def test_inventory_reads_only_the_selected_workspace_root(tmp_path: Path) -> None:
    work = tmp_path / "work"
    personal = tmp_path / "personal"
    work.mkdir()
    personal.mkdir()
    (work / ".mcp.json").write_text(
        '{"mcpServers":{"work-server":{"command":"x"}}}', encoding="utf-8"
    )
    (personal / ".mcp.json").write_text(
        '{"mcpServers":{"personal-server":{"command":"x"}}}', encoding="utf-8"
    )

    class Manager:
        async def provider_mcp_runtime_connections(self, _workspace, _names):
            return {"claude": {}, "opencode": {}}

    async def names(root: Path) -> set[str]:
        payload = await provider_inventory(
            workspace=root.name, agent_root=root,
            explicitly_denied=set(), manager=Manager(), claude_available=True,
            opencode_available=True,
        )
        return {row["name"] for row in payload["providers"][0]["servers"]}

    assert await names(work) == {"work-server"}
    assert await names(personal) == {"personal-server"}


@pytest.mark.asyncio
async def test_claude_project_local_config_overrides_project_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    (root / ".mcp.json").write_text(
        '{"mcpServers":{"shared":{"command":"project-command"}}}', encoding="utf-8"
    )
    (home / ".claude.json").write_text(
        json.dumps({"projects": {_key(root): {"mcpServers": {"shared": {"type": "http", "url": "https://local.invalid"}}}}}),
        encoding="utf-8",
    )

    class Manager:
        async def provider_mcp_runtime_connections(self, _workspace, _names):
            return {"claude": {}, "opencode": {}}

    payload = await provider_inventory(
        workspace="work", agent_root=root,
        explicitly_denied=set(), manager=Manager(), claude_available=True,
        opencode_available=True,
    )
    row = payload["providers"][0]["servers"][0]
    assert row["name"] == "shared"
    assert row["scope"] == "local"
    assert row["kind"] == "http"
    assert row["allowed"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("nt", [False, True])
async def test_payload_carries_shell_prefix_and_no_example_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nt: bool
) -> None:
    root = tmp_path / ("it's \u2019work\u2018" if nt else "my work")
    root.mkdir()
    payload = await provider_inventory(
        workspace="personal", agent_root=root, explicitly_denied=set(), manager=None,
        claude_available=True, opencode_available=True,
    )
    # Patching os.name breaks pathlib, so only the prefix builder runs under it.
    # Both cases patch it: the parametrization is about the target platform, not
    # about the host running the suite, and a Windows runner is `nt` by default.
    monkeypatch.setattr("ciao.web.mcp_inventory.os.name", "nt" if nt else "posix")
    payload["shell"] = _shell_prefix(root)

    if nt:
        quoted = "".join(
            ch * 2 if ch in "'\u2018\u2019\u201a\u201b" else ch for ch in str(root)
        )
        assert payload["shell"] == {
            "kind": "powershell", "cd_prefix": f"Set-Location -LiteralPath '{quoted}'; ",
        }
    else:
        assert payload["shell"] == {
            "kind": "posix", "cd_prefix": f"cd -- {shlex.quote(str(root))} && ",
        }
    for provider in payload["providers"]:
        assert "setup" not in provider


@pytest.mark.asyncio
async def test_claude_rows_read_disabled_servers_from_the_project_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_PATH", raising=False)
    home = tmp_path / "home2"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    root = repo / "ws"
    root.mkdir()
    (root / ".mcp.json").write_text(
        '{"mcpServers":{"notion":{"command":"x"},"linear":{"command":"x"}}}', encoding="utf-8"
    )
    (home / ".claude.json").write_text(
        json.dumps({
            "disabledMcpServers": ["top"],
            "projects": {_key(repo): {"disabledMcpServers": ["notion", "claude.ai Gmail", "plugin:tg:tg"]}},
        }),
        encoding="utf-8",
    )

    payload = await provider_inventory(
        workspace="work", agent_root=root,
        explicitly_denied=set(), manager=_NoRuntime(), claude_available=True,
        opencode_available=True,
    )
    by_name = {row["name"]: row for row in payload["providers"][0]["servers"]}
    assert by_name["notion"]["enabled"] is False
    assert by_name["notion"]["applies_to"] == _key(repo)
    assert by_name["linear"]["enabled"] is True
    assert by_name["claude.ai Gmail"] == {
        "name": "claude.ai Gmail", "scope": "claudeai", "source": "provider", "kind": "remote",
        "enabled": False, "allowed": None, "applies_to": _key(repo), "connections": [],
    }
    assert by_name["plugin:tg:tg"]["scope"] == "account"
    assert by_name["top"]["enabled"] is False


@pytest.mark.asyncio
async def test_claude_local_servers_resolve_for_a_subfolder_after_rekeying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_PATH", raising=False)
    home = tmp_path / "home2"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    root = repo / "ws"
    root.mkdir()
    (home / ".claude.json").write_text(
        json.dumps({"projects": {_key(repo): {"mcpServers": {"local1": {"command": "x"}}}}}),
        encoding="utf-8",
    )

    payload = await provider_inventory(
        workspace="work", agent_root=root,
        explicitly_denied=set(), manager=_NoRuntime(), claude_available=True,
        opencode_available=True,
    )
    by_name = {row["name"]: row for row in payload["providers"][0]["servers"]}
    assert by_name["local1"]["scope"] == "local"


@pytest.mark.asyncio
async def test_opencode_v2_enabled_key_is_ignored_and_rows_carry_their_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENCODE_CONFIG_CONTENT", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "opencode.json").write_text(
        json.dumps({"mcp": {"servers": {
            "a": {"type": "local", "command": ["x"], "enabled": False},
            "b": {"type": "local", "command": ["x"], "disabled": True},
        }}}),
        encoding="utf-8",
    )
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "opencode.json").write_text(
        json.dumps({"mcp": {"c": {"type": "local", "command": ["x"], "enabled": False}}}),
        encoding="utf-8",
    )

    payload = await provider_inventory(
        workspace="work", agent_root=root,
        explicitly_denied=set(), manager=_NoRuntime(), claude_available=False,
        opencode_available=True,
    )
    by_name = {row["name"]: row for row in payload["providers"][1]["servers"]}
    assert by_name["a"]["enabled"] is True
    assert by_name["b"]["enabled"] is False
    assert by_name["a"]["applies_to"] == str(root / "opencode.json")
    assert by_name["b"]["applies_to"] == str(root / "opencode.json")

    payload = await provider_inventory(
        workspace="legacy", agent_root=legacy,
        explicitly_denied=set(), manager=_NoRuntime(), claude_available=False,
        opencode_available=True,
    )
    by_name = {row["name"]: row for row in payload["providers"][1]["servers"]}
    assert by_name["c"]["enabled"] is False


@pytest.mark.asyncio
async def test_managed_claude_row_has_no_applies_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_PATH", raising=False)
    from ciao.web import mcp_inventory

    real_candidates = mcp_inventory._candidate_paths
    managed = tmp_path / "managed.json"
    managed.write_text('{"mcpServers":{"corp":{"command":"x"}}}', encoding="utf-8")

    def with_managed(agent_root: Path, provider: str):
        found = real_candidates(agent_root, provider)
        if provider == "claude":
            found.append((managed, "managed", "managed", False))
        return found

    monkeypatch.setattr("ciao.web.mcp_inventory._candidate_paths", with_managed)
    root = tmp_path / "workspace"
    root.mkdir()
    payload = await provider_inventory(
        workspace="work", agent_root=root,
        explicitly_denied=set(), manager=_NoRuntime(), claude_available=True,
        opencode_available=False,
    )
    by_name = {row["name"]: row for row in payload["providers"][0]["servers"]}
    assert by_name["corp"]["applies_to"] is None
