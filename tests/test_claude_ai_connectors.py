"""The per-workspace claude.ai connectors switch.

A workspace's ``claude_ai_connectors`` (default on) decides whether its Claude
chats load the Claude account's claude.ai connectors. Off sets
``ENABLE_CLAUDEAI_MCP_SERVERS=false`` on the spawned Claude Code process. A
memory pass always carries that variable, whatever the setting. Other providers
never see it.
"""

from __future__ import annotations

import json
from pathlib import Path

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager
from tests.conftest import attach_stub_mcp

ENV_VAR = "ENABLE_CLAUDEAI_MCP_SERVERS"


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


def _manager(tmp_path: Path, *, connectors: bool) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    config.workspaces["work"] = WorkspaceConfig(
        name="work", vault_root="memory-vault/work", claude_ai_connectors=connectors
    )
    manager = ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )
    return attach_stub_mcp(manager)


def _chat(manager: ProjectChatManager, provider: str):
    project = manager.create_project("Notes", workspace="work")
    return manager.create_chat(project.project_id, title="Chat", provider=provider)


def _pass_chat(manager: ProjectChatManager, provider: str):
    """A memory pass in the workspace's Memory project, built as the guardrail tests build it."""
    project = manager._memory_pass.ensure_project("work")
    return manager.create_chat(
        project.project_id,
        title="Memory pass · Pricing rework",
        provider=provider,
        helper={
            "kind": "memory_pass",
            "source_chat_id": "chat-1",
            "archive_policy": "when_clean",
        },
    )


# -- config round-trip --------------------------------------------------------


def test_switch_defaults_on_and_round_trips_through_workspaces_json(tmp_path: Path) -> None:
    _write_registry(
        tmp_path,
        [
            {"name": "personal", "vault_root": "memory-vault/personal"},
            {
                "name": "work",
                "vault_root": "memory-vault/work",
                "claude_ai_connectors": False,
            },
        ],
    )
    config = _config(tmp_path)
    assert config.workspace("personal").claude_ai_connectors is True
    assert config.workspace("work").claude_ai_connectors is False

    config.persist_workspace_registry()
    stored = {
        row["name"]: row
        for row in json.loads((tmp_path / ".runtime" / "workspaces.json").read_text())
    }
    assert stored["personal"]["claude_ai_connectors"] is True
    assert stored["work"]["claude_ai_connectors"] is False

    reloaded = _config(tmp_path)
    assert reloaded.workspace("work").claude_ai_connectors is False
    assert reloaded.workspace("personal").claude_ai_connectors is True


def test_switch_coerces_a_string_from_a_hand_edited_registry(tmp_path: Path) -> None:
    _write_registry(
        tmp_path,
        [
            {"name": "off", "vault_root": "memory-vault/off", "claude_ai_connectors": "false"},
            {"name": "on", "vault_root": "memory-vault/on", "claude_ai_connectors": "true"},
        ],
    )
    config = _config(tmp_path)
    assert config.workspace("off").claude_ai_connectors is False
    assert config.workspace("on").claude_ai_connectors is True


# -- env builder ---------------------------------------------------------------


def test_claude_chat_in_a_connectors_workspace_leaves_the_env_untouched(tmp_path: Path) -> None:
    manager = _manager(tmp_path, connectors=True)
    env = manager._build_extra_env(_chat(manager, "claude"))
    assert ENV_VAR not in env


def test_claude_chat_in_a_workspace_without_connectors_sets_false(tmp_path: Path) -> None:
    manager = _manager(tmp_path, connectors=False)
    env = manager._build_extra_env(_chat(manager, "claude"))
    assert env[ENV_VAR] == "false"


def test_memory_pass_always_sets_false_even_when_connectors_are_allowed(tmp_path: Path) -> None:
    manager = _manager(tmp_path, connectors=True)
    env = manager._build_extra_env(_pass_chat(manager, "claude"))
    assert env[ENV_VAR] == "false"


def test_non_claude_chats_never_get_the_variable(tmp_path: Path) -> None:
    manager = _manager(tmp_path, connectors=False)
    assert ENV_VAR not in manager._build_extra_env(_chat(manager, "opencode"))
    assert ENV_VAR not in manager._build_extra_env(_pass_chat(manager, "opencode"))
