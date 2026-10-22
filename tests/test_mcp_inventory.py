from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from ciao.web.mcp_inventory import _jsonc_to_json, _shell_prefix, provider_inventory


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
                "claude": {"allowed": [{"state": "connected", "provider": "claude", "chat_id": "c1"}], "session-only": [{"state": "unknown", "provider": "claude", "chat_id": "c2"}]},
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
        json.dumps({"projects": {str(root): {"mcpServers": {"shared": {"type": "http", "url": "https://local.invalid"}}}}}),
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
    root = tmp_path / ("it's work" if nt else "my work")
    root.mkdir()
    payload = await provider_inventory(
        workspace="personal", agent_root=root, explicitly_denied=set(), manager=None,
        claude_available=True, opencode_available=True,
    )
    if nt:
        # Patching os.name breaks pathlib, so only the prefix builder runs under it.
        monkeypatch.setattr("ciao.web.mcp_inventory.os.name", "nt")
        payload["shell"] = _shell_prefix(root)

    if nt:
        quoted = str(root).replace("'", "''")
        assert payload["shell"] == {
            "kind": "powershell", "cd_prefix": f"Set-Location -LiteralPath '{quoted}'; ",
        }
    else:
        assert payload["shell"] == {
            "kind": "posix", "cd_prefix": f"cd -- {shlex.quote(str(root))} && ",
        }
    for provider in payload["providers"]:
        assert "setup" not in provider
