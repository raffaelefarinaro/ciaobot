"""New-chat creation applies tier pins and per-provider default modes."""

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


def test_new_chat_uses_the_pinned_model_for_its_tier(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        codex=CodexSettings(sonnet_model="gpt-5.6-terra", haiku_model="gpt-5.6-luna"),
    )
    manager = _make_manager(tmp_path, config)
    project = manager.create_project("Pins", workspace="work")

    chat = manager.create_chat(project.project_id, model="sonnet", provider="codex")
    assert chat.model == "gpt-5.6-terra"

    chat = manager.create_chat(project.project_id, model="haiku", provider="codex")
    assert chat.model == "gpt-5.6-luna"


def test_new_chat_keeps_alias_without_a_pin(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path, _config(tmp_path))
    project = manager.create_project("No pins", workspace="work")

    chat = manager.create_chat(project.project_id, model="sonnet", provider="codex")
    assert chat.model == "sonnet"

    # Claude has no operator-settable tier pins; the alias passes through.
    chat = manager.create_chat(project.project_id, model="opus", provider="claude")
    assert chat.model == "opus"


def test_new_chat_pin_resolves_only_bare_aliases(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        codex=CodexSettings(sonnet_model="gpt-5.6-terra"),
    )
    manager = _make_manager(tmp_path, config)
    project = manager.create_project("Concrete", workspace="work")

    # An explicitly chosen concrete model is not overridden by the pin.
    chat = manager.create_chat(
        project.project_id, model="gpt-5.6-sol", provider="codex"
    )
    assert chat.model == "gpt-5.6-sol"


def test_new_opencode_chat_defaults_to_approval_enforcing_mode(tmp_path: Path) -> None:
    manager = _make_manager(tmp_path, _config(tmp_path))
    project = manager.create_project("Modes", workspace="work")

    # New opencode chats require approval by default; bypass is explicit.
    chat = manager.create_chat(project.project_id, provider="opencode")
    assert chat.mode == "normal"

    # Other providers keep the env-backed default (auto).
    chat = manager.create_chat(project.project_id, provider="claude")
    assert chat.mode == "auto"


def test_per_provider_default_mode_overrides_builtin(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        provider_default_modes={"opencode": "auto", "claude": "plan"},
    )
    manager = _make_manager(tmp_path, config)
    project = manager.create_project("Overrides", workspace="work")

    assert manager.create_chat(project.project_id, provider="opencode").mode == "auto"
    assert manager.create_chat(project.project_id, provider="codex").mode == "auto"
    assert manager.create_chat(project.project_id, provider="claude").mode == "plan"


def test_opencode_tier_pin_applies_at_creation(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        opencode=OpencodeSettings(sonnet_model="anthropic/claude-sonnet-4-6"),
    )
    manager = _make_manager(tmp_path, config)
    project = manager.create_project("Open", workspace="work")

    chat = manager.create_chat(project.project_id, model="sonnet", provider="opencode")
    assert chat.model == "anthropic/claude-sonnet-4-6"


def test_empty_default_model_uses_pinned_sonnet(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        opencode=OpencodeSettings(sonnet_model="ollama-cloud/kimi-k2.7-code"),
        workspaces=_opencode_workspace(),
    )
    manager = _make_manager(tmp_path, config)
    project = manager.create_project("Pinned default", workspace="work")

    # The workspace leaves the default model empty ("let the provider pick"),
    # but an operator who pinned tier routing expects the new chat to start
    # on the pinned default-tier model, not on whatever the provider picks.
    chat = manager.create_chat(project.project_id)
    assert chat.provider == "opencode"
    assert chat.model == "ollama-cloud/kimi-k2.7-code"


def test_empty_default_model_without_pins_stays_empty(tmp_path: Path) -> None:
    config = _config(tmp_path, workspaces=_opencode_workspace())
    manager = _make_manager(tmp_path, config)
    project = manager.create_project("No pins", workspace="work")

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
