"""Pins the behaviour of every Claude-only provider-id branch in the web hosts (#3407).

Each test records what a site does for claude, codex and opencode. The branches
move to ``ProviderDescriptor`` fields, and these assertions must hold before and
after that move. Codex falls on the non-claude side of every branch.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao.claude_trim import CONNECTORS_TRIM_KEY
from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.sessions import StateStore
from ciao.transcripts import ChatContext, TranscriptStore
from ciao.web import chat_service, transcript_service
from ciao.web.project_chats import ChatInfo, ProjectChatManager
from tests.conftest import attach_stub_mcp

PROVIDERS = ("claude", "codex", "opencode")
ENV_VAR = "ENABLE_CLAUDEAI_MCP_SERVERS"


def _bare_manager() -> ProjectChatManager:
    return ProjectChatManager.__new__(ProjectChatManager)


def _real_manager(tmp_path: Path, *, connectors: bool) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    trim = {} if connectors else {CONNECTORS_TRIM_KEY: True}
    config.workspaces["work"] = WorkspaceConfig(
        name="work", vault_root="memory-vault/work", claude_trim=trim
    )
    manager = ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )
    return attach_stub_mcp(manager)


# 1. disallowed_tools_for_chat / claude_settings_for_chat (Claude Code workspace layer).
#    claude: workspace denylist and settings layer. codex, opencode: [] and None.
@pytest.mark.parametrize("provider", PROVIDERS)
def test_workspace_cli_layer_applies_to_claude_only(provider: str) -> None:
    manager = _bare_manager()
    manager._projects = {}
    manager._config = SimpleNamespace(
        disallowed_tools_for_workspace=lambda workspace: ["WebSearch"],
        claude_settings_for_workspace=lambda workspace: {"permissions": {"allow": []}},
    )
    chat = SimpleNamespace(provider=provider, project_id="none")
    expected_tools = ["WebSearch"] if provider == "claude" else []
    expected_settings = {"permissions": {"allow": []}} if provider == "claude" else None
    assert manager.disallowed_tools_for_chat(chat) == expected_tools
    assert manager.claude_settings_for_chat(chat) == expected_settings


# 2. _model_for_provider: Claude tier aliases pass through normalised. On codex and
#    opencode a tier alias is replaced by the provider's operator default. A concrete
#    model id passes through unchanged on every provider.
@pytest.mark.parametrize("provider", PROVIDERS)
def test_tier_alias_resolution_depends_on_provider(provider: str) -> None:
    manager = _bare_manager()
    manager._config = SimpleNamespace(
        default_model_for_provider=lambda p, workspace=None: f"{p}-default"
    )
    if provider == "claude":
        assert manager._model_for_provider("sonnet", provider, "work") == (
            chat_service._normalize_tier("sonnet")
        )
    else:
        assert manager._model_for_provider("sonnet", provider, "work") == f"{provider}-default"
    assert manager._model_for_provider("vendor/model-x", provider, "work") == "vendor/model-x"


# 3. _build_extra_env: ENABLE_CLAUDEAI_MCP_SERVERS=false only on a trimmed Claude chat.
@pytest.mark.parametrize("provider", PROVIDERS)
def test_claude_ai_connectors_env_only_for_trimmed_claude(tmp_path: Path, provider: str) -> None:
    manager = _real_manager(tmp_path, connectors=False)
    project = manager.create_project("Notes", workspace="work")
    chat = manager.create_chat(project.project_id, title="Chat", provider=provider)
    env = manager._build_extra_env(chat)
    if provider == "claude":
        assert env[ENV_VAR] == "false"
    else:
        assert ENV_VAR not in env


# 4. auto_title_if_default: only a provider with no session-event title does the native
#    read. Claude gets its title from a session_info_update event, so it skips the read.
@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_native_title_read_skipped_only_for_claude(provider: str) -> None:
    manager = _bare_manager()
    manager._config = SimpleNamespace()
    manager._save = lambda **_kwargs: None
    manager._titling = set()
    chat = ChatInfo(
        chat_id="chat-1", project_id="p", title="New Chat", provider=provider,
        session_id="sess-1", title_origin="default",
    )
    manager._chats = {"chat-1": chat}
    calls: list[str] = []

    async def fake_adopt(chat_id, is_target):
        calls.append(chat_id)
        return (False, None)

    async def fake_llm(chat, user_text, assistant_text):
        return "LLM Title"

    manager._adopt_native_title = fake_adopt
    manager._llm_chat_title = fake_llm
    assert await manager.auto_title_if_default("chat-1", "hello") == "LLM Title"
    assert calls == ([] if provider == "claude" else ["chat-1"])


# 5. Recovery of orphaned active chats. Claude needs its session blob, or a recorded
#    audit trail. Codex and opencode need the audit trail alone.
def _recovery_manager(tmp_path: Path, provider: str, *, audit: str, blob: bool):
    manager = _bare_manager()
    transcript = {
        "turns": [{"prompt": "hi", "mode": "normal"}],
        "session_id": "sess-1",
        "provider": provider,
    }
    manager._transcripts = SimpleNamespace(
        all_current_transcripts=lambda: [("chat-orphan", provider, transcript)]
    )
    manager._chats = {}
    manager._audited_chat_status = lambda chat_id: audit
    manager._extract_context_target = lambda text: ("work", "")
    manager._resolve_project_reference = lambda workspace, ref: "project-1"
    manager._claude_session_exists = lambda session_id, agent_root=None: blob
    manager._config = SimpleNamespace(
        claude_mode="normal", claude_default_model="sonnet", workspace_root=tmp_path
    )
    manager._events = SimpleNamespace(publish=lambda event: None)
    manager._save = lambda **_kwargs: None
    return manager


@pytest.mark.parametrize(
    ("provider", "audit", "blob", "recovered"),
    [
        ("claude", "absent", True, True),
        ("claude", "absent", False, False),
        ("claude", "present", False, True),
        ("codex", "absent", True, False),
        ("codex", "absent", False, False),
        ("codex", "present", False, True),
        ("opencode", "absent", False, False),
        ("opencode", "present", False, True),
    ],
)
def test_orphan_recovery_evidence_by_provider(
    tmp_path: Path, provider: str, audit: str, blob: bool, recovered: bool
) -> None:
    manager = _recovery_manager(tmp_path, provider, audit=audit, blob=blob)
    manager._recover_orphaned_active_chats()
    assert ("chat-orphan" in manager._chats) is recovered


# 6. Reconciling half-archived chats. A Claude chat whose session blob still exists is
#    left alone. Codex and opencode are healed whenever an archive sits on disk.
def _half_archived_manager(tmp_path: Path, provider: str, *, blob: bool):
    manager = _bare_manager()
    archive = tmp_path / "chats" / "chat-half" / provider
    archive.mkdir(parents=True)
    (archive / "2026-01-01.md").write_text("# archived", encoding="utf-8")
    manager._transcripts = SimpleNamespace(
        current_path=lambda ctx, p="claude": tmp_path / "missing.md",
        archive_dir=lambda ctx, p="claude": archive,
    )
    manager._config = SimpleNamespace(
        workspace_root=tmp_path, chats_roots=lambda: [tmp_path / "chats"]
    )
    manager._claude_session_exists = lambda session_id, agent_root=None: blob
    manager._agent_root_for_chat = lambda chat_id: tmp_path
    manager._chats = {
        "chat-half": ChatInfo(
            chat_id="chat-half", project_id="p", title="T", provider=provider,
            session_id="sess-1", archived=False,
        )
    }
    manager._save = lambda **_kwargs: None
    return manager


@pytest.mark.parametrize(
    ("provider", "blob", "healed"),
    [
        ("claude", True, False),
        ("claude", False, True),
        ("codex", True, True),
        ("codex", False, True),
        ("opencode", True, True),
    ],
)
def test_half_archived_heal_depends_on_session_blob_for_claude_only(
    tmp_path: Path, provider: str, blob: bool, healed: bool
) -> None:
    manager = _half_archived_manager(tmp_path, provider, blob=blob)
    manager._reconcile_half_archived_chats()
    assert manager._chats["chat-half"].archived is healed


# 7. _assemble_chat_messages: a non-claude chat with a transcript serves the local
#    transcript. A claude chat does not, and reads its session lineage instead.
@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_history_source_is_local_transcript_only_for_non_claude(
    tmp_path: Path, monkeypatch, provider: str
) -> None:
    async def no_acp_rows(*_args, **_kwargs):
        return []

    monkeypatch.setattr(transcript_service, "_acp_history_rows", no_acp_rows)
    current = [{"role": "assistant", "text": "LOCAL"}]
    pcm = SimpleNamespace(
        _transcripts=SimpleNamespace(current_messages=lambda ctx, p: current),
        _agent_root_for_chat=lambda chat_id: tmp_path,
    )
    config = SimpleNamespace(workspace_root=tmp_path)
    chat = SimpleNamespace(
        chat_id="chat-1", session_id="sess-1", provider=provider,
        previous_session_ids=[], handover_messages=[{"role": "system", "text": "HANDOVER"}],
        user_turn_timings={}, turn_verification={},
    )
    rows = await transcript_service._assemble_chat_messages(pcm, config, chat)
    if provider == "claude":
        assert current[0] not in rows
    else:
        assert rows == [{"role": "system", "text": "HANDOVER"}, *current]


def test_every_runtime_provider_is_pinned() -> None:
    from ciao import provider_registry

    assert {item.id for item in provider_registry.descriptors()} == set(PROVIDERS)
