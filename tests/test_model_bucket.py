"""Tests for the explicit Claude routing bucket.

The project workspace only *preselects* the bucket; an explicit picker
choice is persisted on the chat and pins routing. Legacy values remain
supported: "work" → Anthropic subscription, "personal" → Ollama. Configured
workspaces may use clearer names: "anthropic" and "ollama".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.providers.ollama import OllamaSettings
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager

OLLAMA = OllamaSettings(
    models=("minimax-m3:cloud", "kimi-k2.7-code:cloud"),
    base_url="https://ollama.com",
    api_key="sk-cloud",
)


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        ollama=OLLAMA,
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )


def test_personal_chat_preselects_personal_bucket_with_resolved_model(tmp_path):
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("p", workspace="personal")
    chat = pcm.create_chat(project.project_id)  # default model = opus alias
    assert chat.model_bucket == "personal"
    # Alias resolved at creation so the picker shows what actually runs.
    assert chat.model == "minimax-m3:cloud"
    env = pcm._build_extra_env(chat)
    assert env["ANTHROPIC_BASE_URL"] == "https://ollama.com"


def test_work_chat_preselects_work_bucket_anthropic(tmp_path):
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("w", workspace="work")
    chat = pcm.create_chat(project.project_id)
    assert chat.model_bucket == "work"
    assert chat.model == "opus"
    assert pcm._runtime_model_for_chat(chat) == "opus"
    env = pcm._build_extra_env(chat)
    assert "ANTHROPIC_BASE_URL" not in env


def test_configured_workspace_preselects_anthropic_bucket(tmp_path):
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        ollama=OLLAMA,
        workspaces={
            "client": WorkspaceConfig(
                name="client",
                vault_root="vaults/client",
                model_bucket="anthropic",
                gws_profile="work",
            )
        },
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    pcm = ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )
    project = pcm.create_project("client", workspace="client")

    chat = pcm.create_chat(project.project_id)

    assert chat.model_bucket == "anthropic"
    assert chat.model == "opus"
    assert pcm._runtime_model_for_chat(chat) == "opus"


def test_configured_workspace_preselects_ollama_bucket(tmp_path):
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        ollama=OLLAMA,
        workspaces={
            "home": WorkspaceConfig(
                name="home",
                vault_root="vaults/home",
                model_bucket="ollama",
                gws_profile="personal",
            )
        },
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    pcm = ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )
    project = pcm.create_project("home", workspace="home")

    chat = pcm.create_chat(project.project_id)

    assert chat.model_bucket == "ollama"
    assert chat.model == "minimax-m3:cloud"
    assert pcm._runtime_model_for_chat(chat) == "minimax-m3:cloud"
    env = pcm._build_extra_env(chat)
    assert env["ANTHROPIC_BASE_URL"] == "https://ollama.com"


def test_configured_custom_bucket_is_allowed_and_defaults_to_anthropic(tmp_path):
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        ollama=OLLAMA,
        workspaces={
            "client": WorkspaceConfig(
                name="client",
                vault_root="vaults/client",
                model_bucket="corporate",
                gws_profile="work",
            )
        },
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    pcm = ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )
    project = pcm.create_project("client", workspace="client")

    chat = pcm.create_chat(project.project_id)

    assert chat.model_bucket == "corporate"
    assert chat.model == "opus"
    assert pcm._runtime_model_for_chat(chat) == "opus"
    assert "ANTHROPIC_BASE_URL" not in pcm._build_extra_env(chat)


def test_explicit_work_bucket_in_personal_workspace_stays_anthropic(tmp_path):
    """Regression case: a personal-workspace chat explicitly set to Claude
    (Work) must use the Anthropic subscription, not the Ollama tier map."""
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("p", workspace="personal")
    chat = pcm.create_chat(
        project.project_id, model="opus", model_bucket="work"
    )
    assert chat.model_bucket == "work"
    assert pcm._runtime_model_for_chat(chat) == "opus"
    env = pcm._build_extra_env(chat)
    assert "ANTHROPIC_BASE_URL" not in env


def test_bucket_switch_mid_chat_requires_handover(tmp_path):
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("p", workspace="personal")
    chat = pcm.create_chat(project.project_id)  # personal bucket, ollama
    chat.user_turn_count = 1

    with pytest.raises(ValueError, match="once a chat has started"):
        pcm.update_chat(chat.chat_id, model="opus", model_bucket="work")

    # Handover path is the sanctioned way and persists the bucket.
    updated = pcm.handover_chat(
        chat.chat_id, provider="claude", model="opus", model_bucket="work"
    )
    assert updated is not None
    assert updated.model_bucket == "work"
    assert pcm._runtime_model_for_chat(updated) == "opus"


def test_bucket_switch_on_fresh_chat_is_fine(tmp_path):
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("p", workspace="personal")
    chat = pcm.create_chat(project.project_id)
    updated = pcm.update_chat(chat.chat_id, model="opus", model_bucket="work")
    assert updated is not None
    assert updated.model_bucket == "work"
    assert pcm._runtime_model_for_chat(updated) == "opus"


def test_bucket_persists_across_reload(tmp_path):
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("p", workspace="personal")
    chat = pcm.create_chat(
        project.project_id,
        title="Bucket reload test",  # non-default title survives the
        model="opus",                # empty-chat sweep on manager init
        model_bucket="work",
    )
    # Fresh manager from the same state file.
    pcm2 = ProjectChatManager(
        pcm._config,
        state_store=pcm._state,
        transcript_store=pcm._transcripts,
        path=pcm._path,
    )
    reloaded = pcm2.get_chat(chat.chat_id)
    assert reloaded is not None
    assert reloaded.model_bucket == "work"
    assert reloaded.to_dict()["model_bucket"] == "work"


def test_invalid_bucket_rejected(tmp_path):
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("p", workspace="personal")
    with pytest.raises(ValueError, match="bucket"):
        pcm.create_chat(project.project_id, model_bucket="corporate")
    chat = pcm.create_chat(project.project_id)
    with pytest.raises(ValueError, match="bucket"):
        pcm.update_chat(chat.chat_id, model_bucket="corporate")


def test_non_claude_provider_clears_bucket(tmp_path):
    pcm = _make_manager(tmp_path)
    project = pcm.create_project("p", workspace="personal")
    chat = pcm.create_chat(project.project_id)
    assert chat.model_bucket == "personal"
    updated = pcm.update_chat(
        chat.chat_id, provider="pi", model="kimi-k2.7-code:cloud"
    )
    assert updated is not None
    assert updated.model_bucket == ""
