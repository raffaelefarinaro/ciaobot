"""Tests for threading a resolved agent root into the provider factory.

Phase P7 of the agent-roots work order: the agent root arrives at the provider
factory as data instead of the workspace global. ``CiaoConfig.agent_root``
still returns ``workspace_root`` for every workspace, so these tests assert the
no-behaviour-change guarantee: the resolved value today equals ``workspace_root``
no matter which workspace the chat's project names. The assertion that the two
are equal is the one that fails loudly and intentionally when the re-rooting
release flips ``agent_root``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.provider_registry import ProviderDescriptor
from ciao.provider_service import ProviderService
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ChatInfo, ProjectChatManager


def _make_manager(tmp_path: Path, config: CiaoConfig | None = None) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    if config is None:
        config = CiaoConfig(
            pwa_auth_token="test-token",
            workspace_root=tmp_path,
            state_path=runtime / "state.json",
            media_root=runtime / "media",
        )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )


def _make_custom_workspace_config(tmp_path: Path) -> CiaoConfig:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        workspaces={
            "home": WorkspaceConfig(
                name="home",
                vault_root="memory-vault/home",
                gws_profile="personal",
            ),
            "client": WorkspaceConfig(
                name="client",
                vault_root="vaults/client",
                gws_profile="work",
            ),
        },
    )


@pytest.fixture
def spy_factory(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture the root handed to the provider factory for the duration."""
    module = __import__(
        "ciao.provider_service", fromlist=["provider_registry"]
    )
    real_require = module.provider_registry.require

    def fake_require(name: str):
        descriptor = real_require(name)
        return ProviderDescriptor(
            id=descriptor.id,
            label=descriptor.label,
            short_label=descriptor.short_label,
            cli_label=descriptor.cli_label,
            factory_path=f"{__name__}:_SpyProvider",
        )

    # Cleared per test: a shared dict that survives between tests would let an
    # assertion read the PREVIOUS test's root and pass even when the factory was
    # never called at all.
    _CAPTURED.clear()
    monkeypatch.setattr(module.provider_registry, "require", fake_require)
    return _CAPTURED


_CAPTURED: dict[str, Any] = {}


class _SpyProvider:
    """Records the root it is constructed with, then stands in for the real one."""

    def __init__(self, root: Path, *, config: Any = None) -> None:
        _CAPTURED["root"] = root
        _CAPTURED["config"] = config
        self.workspace_root = root


def test_chat_project_workspace_resolves_that_workspaces_agent_root(
    tmp_path: Path, spy_factory: dict[str, Any]
) -> None:
    config = _make_custom_workspace_config(tmp_path)
    manager = _make_manager(tmp_path, config)

    project = manager.create_project("Feature", workspace="client")
    chat = manager.create_chat(project.project_id, provider="claude")

    manager._get_provider(chat.chat_id)

    # The root that reached the factory is this workspace's agent root, which
    # today equals workspace_root (the no-behaviour-change guarantee).
    assert spy_factory["root"] == config.agent_root("client")
    assert spy_factory["root"] == config.workspace_root


def test_project_without_workspace_falls_back_to_primary_workspace(
    tmp_path: Path, spy_factory: dict[str, Any]
) -> None:
    cfg = _make_custom_workspace_config(tmp_path)
    manager = _make_manager(tmp_path, cfg)

    project = manager.create_project("Home", workspace="home")
    project.workspace = ""
    chat = manager.create_chat(project.project_id, provider="claude")

    manager._get_provider(chat.chat_id)

    assert spy_factory["root"] == cfg.agent_root(cfg.primary_workspace())
    assert spy_factory["root"] == cfg.workspace_root


def test_chat_without_project_resolves_primary_workspace_agent_root(
    tmp_path: Path, spy_factory: dict[str, Any]
) -> None:
    cfg = _make_custom_workspace_config(tmp_path)
    manager = _make_manager(tmp_path, cfg)

    # A chat whose project_id is absent from the manager's project map.
    manager._chats["chat-null"] = ChatInfo(chat_id="chat-null", project_id="missing-project")
    manager._get_provider("chat-null")

    assert spy_factory["root"] == cfg.agent_root(cfg.primary_workspace())
    assert spy_factory["root"] == cfg.workspace_root


def test_provider_service_without_agent_root_uses_workspace_root(
    tmp_path: Path, spy_factory: dict[str, Any]
) -> None:
    cfg = _make_custom_workspace_config(tmp_path)
    service = ProviderService(cfg, provider="claude")

    service._ensure_provider("claude")

    assert spy_factory["root"] == cfg.workspace_root
