"""New-chat creation applies per-provider default models and default modes."""

from __future__ import annotations

from pathlib import Path

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.providers.codex import CodexSettings
from ciao.providers.opencode import OpencodeSettings
from ciao.sessions import StateStore
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager


def _opencode_workspace(name: str = "work") -> dict[str, WorkspaceConfig]:
    """A workspace whose default provider is opencode and whose default
    model is empty ("let the provider pick")."""
    return {
        name: WorkspaceConfig(
            name=name,
            vault_root=name,
            default_provider="opencode",
            default_model="",
        )
    }


def _make_manager(tmp_path: Path, config: CiaoConfig) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    return ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )


def _config(tmp_path: Path, **kwargs) -> CiaoConfig:
    runtime = tmp_path / ".runtime"
    return CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        **kwargs,
    )


def test_new_chat_uses_the_requested_model(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        codex=CodexSettings(default_model="gpt-5.6-terra"),
    )
    manager = _make_manager(tmp_path, config)
    project = manager.create_project("Models", workspace="work")

    # An explicit model wins over the provider default.
    chat = manager.create_chat(project.project_id, model="gpt-5.6-sol", provider="codex")
    assert chat.model == "gpt-5.6-sol"


def test_new_chat_resolves_foreign_alias_without_a_default(tmp_path: Path) -> None:
    # "sonnet" is Claude Code's own vocabulary; Codex's real backend rejects
    # it outright. With no operator default configured either, the request
    # resolves to "" (the provider picks its own), not the foreign alias.
    manager = _make_manager(tmp_path, _config(tmp_path))
    project = manager.create_project("No default", workspace="work")

    chat = manager.create_chat(project.project_id, model="sonnet", provider="codex")
    assert chat.model == ""

    # Claude has no operator-settable default; the alias passes through.
    chat = manager.create_chat(project.project_id, model="opus", provider="claude")
    assert chat.model == "opus"


def test_new_chat_resolves_foreign_alias_to_the_operator_default(tmp_path: Path) -> None:
    config = _config(tmp_path, opencode=OpencodeSettings(default_model="anthropic/claude-sonnet-4.5"))
    manager = _make_manager(tmp_path, config)
    project = manager.create_project("Has default", workspace="work")

    chat = manager.create_chat(project.project_id, model="haiku", provider="opencode")
    assert chat.model == "anthropic/claude-sonnet-4.5"


def test_new_chat_uses_app_default_mode_on_every_provider(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path, _config(tmp_path))
    project = manager.create_project("Modes", workspace="work")

    # opencode has no built-in exception: it starts on the env-backed default
    # (auto) like every other provider.
    for provider in ("opencode", "codex", "claude"):
        chat = manager.create_chat(project.project_id, provider=provider)
        assert chat.mode == "auto", provider


def test_per_provider_default_mode_overrides_builtin(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        provider_default_modes={"opencode": "normal", "claude": "plan"},
    )
    manager = _make_manager(tmp_path, config)
    project = manager.create_project("Overrides", workspace="work")

    assert manager.create_chat(project.project_id, provider="opencode").mode == "normal"
    assert manager.create_chat(project.project_id, provider="codex").mode == "auto"
    assert manager.create_chat(project.project_id, provider="claude").mode == "plan"


def test_opencode_default_model_applies_at_creation(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        opencode=OpencodeSettings(default_model="anthropic/claude-sonnet-4-6"),
        workspaces=_opencode_workspace(),
    )
    manager = _make_manager(tmp_path, config)
    project = manager.create_project("Open", workspace="work")

    chat = manager.create_chat(project.project_id, provider="opencode")
    assert chat.model == "anthropic/claude-sonnet-4-6"


def test_empty_default_model_uses_provider_default(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        opencode=OpencodeSettings(default_model="ollama-cloud/kimi-k2.7-code"),
        workspaces=_opencode_workspace(),
    )
    manager = _make_manager(tmp_path, config)
    project = manager.create_project("Default", workspace="work")

    # The workspace leaves the default model empty ("let the provider pick"),
    # but an operator who set a provider default expects the new chat to start
    # on it.
    chat = manager.create_chat(project.project_id)
    assert chat.provider == "opencode"
    assert chat.model == "ollama-cloud/kimi-k2.7-code"


def test_empty_default_model_without_defaults_stays_empty(tmp_path: Path) -> None:
    config = _config(tmp_path, workspaces=_opencode_workspace())
    manager = _make_manager(tmp_path, config)
    project = manager.create_project("No default", workspace="work")

    chat = manager.create_chat(project.project_id)
    assert chat.provider == "opencode"
    assert chat.model == ""


def test_removed_workspace_provider_does_not_carry_its_model_to_claude(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        workspaces={
            "work": WorkspaceConfig(
                name="work",
                vault_root="work",
                default_provider="ollama",
                default_model="qwen3:latest",
            )
        },
    )
    manager = _make_manager(tmp_path, config)
    project = manager.create_project("Recovered", workspace="work")

    chat = manager.create_chat(project.project_id)

    assert chat.provider == "claude"
    assert chat.model == config.claude_default_model
