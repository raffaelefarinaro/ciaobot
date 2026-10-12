from __future__ import annotations

from types import SimpleNamespace

import pytest

from ciao.web.project_chats import ProjectChatManager


def _manager(chats, providers, workspaces):
    manager = ProjectChatManager.__new__(ProjectChatManager)
    manager._chats = chats
    manager._providers = providers
    manager._workspace_for_chat = lambda chat_id: workspaces[chat_id]
    return manager


@pytest.mark.asyncio
async def test_runtime_mcp_status_is_workspace_scoped():
    class OpenCode:
        has_live_server = True

        async def read_mcp_status(self):
            return {"notion": "connected"}

    chats = {
        "chat-a": SimpleNamespace(provider="opencode"),
        "chat-b": SimpleNamespace(provider="opencode"),
    }
    providers = {
        chat_id: SimpleNamespace(_provider=OpenCode()) for chat_id in chats
    }
    manager = _manager(chats, providers, {"chat-a": "work", "chat-b": "personal"})

    rows = await manager.provider_mcp_runtime_connections("work", {"opencode": ["notion"]})

    assert rows == {
        "opencode": {
            "notion": [{"state": "connected", "provider": "opencode", "chat_id": "chat-a"}]
        }
    }


@pytest.mark.asyncio
async def test_runtime_mcp_status_failure_is_unknown_and_safe():
    class OpenCode:
        has_live_server = True

        async def read_mcp_status(self):
            raise TimeoutError("credential=must-not-escape")

    manager = _manager(
        {"chat": SimpleNamespace(provider="opencode")},
        {"chat": SimpleNamespace(_provider=OpenCode())},
        {"chat": "work"},
    )

    rows = await manager.provider_mcp_runtime_connections("work", {"opencode": ["notion"]})

    assert rows == {
        "opencode": {
            "notion": [{
                "state": "unknown", "provider": "opencode", "chat_id": "chat",
                "error": "Could not read provider MCP status.",
            }]
        }
    }
    assert "credential" not in repr(rows)


@pytest.mark.asyncio
async def test_no_live_provider_is_unchecked():
    class OpenCode:
        has_live_server = False

    manager = _manager(
        {"chat": SimpleNamespace(provider="opencode")},
        {"chat": SimpleNamespace(_provider=OpenCode())},
        {"chat": "work"},
    )

    assert await manager.provider_mcp_runtime_connections("work", {"opencode": ["notion"]}) == {
        "opencode": {"notion": []}
    }


@pytest.mark.asyncio
async def test_runtime_only_account_connector_is_included_without_configured_names():
    class Claude:
        _connected = True

        async def read_mcp_status(self):
            return {"account-connector": "connected"}

    manager = _manager(
        {"chat": SimpleNamespace(provider="claude")},
        {"chat": SimpleNamespace(_provider=Claude())},
        {"chat": "work"},
    )

    rows = await manager.provider_mcp_runtime_connections(
        "work", {"claude": [], "opencode": []}
    )

    assert rows["claude"]["account-connector"] == [
        {"state": "connected", "provider": "claude", "chat_id": "chat"}
    ]
