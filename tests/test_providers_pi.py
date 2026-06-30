"""Tests for Pi provider config and integration."""

from __future__ import annotations

from pathlib import Path

from ciao.providers.pi import PiSettings, is_pi_model


def test_pi_settings_defaults() -> None:
    settings = PiSettings()
    assert settings.models == ()
    assert settings.provider == "ollama"
    assert settings.base_url == "http://localhost:11434"
    assert settings.default_model == ""


def test_is_pi_model_matches_allowlist() -> None:
    settings = PiSettings(models=("qwen3-coder", "kimi-k2.7-code"))
    assert is_pi_model("qwen3-coder", settings)
    assert is_pi_model("kimi-k2.7-code", settings)
    assert not is_pi_model("opus", settings)
    assert not is_pi_model("", settings)


def test_is_pi_model_empty_allowlist() -> None:
    settings = PiSettings(models=())
    assert not is_pi_model("qwen3-coder", settings)


def test_ciao_config_parses_pi_env(monkeypatch) -> None:
    """CIAO_PI_MODELS / CIAO_PI_PROVIDER / CIAO_PI_OLLAMA_URL feed PiSettings."""
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("CIAO_PI_MODELS", "qwen3-coder,kimi-k2.7-code")
    monkeypatch.setenv("CIAO_PI_PROVIDER", "ollama")
    monkeypatch.setenv("CIAO_PI_OLLAMA_URL", "http://ollama.box:11434")
    monkeypatch.setenv("CIAO_PI_DEFAULT_MODEL", "qwen3-coder")
    from ciao.config import CiaoConfig
    config = CiaoConfig.from_env()
    assert config.pi.models == ("qwen3-coder", "kimi-k2.7-code")
    assert config.pi.provider == "ollama"
    assert config.pi.base_url == "http://ollama.box:11434"
    assert config.pi.default_model == "qwen3-coder"
    assert "qwen3-coder" in config.claude_models
    assert "kimi-k2.7-code" in config.claude_models


def test_ciao_config_pi_disabled_by_default(monkeypatch) -> None:
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.delenv("CIAO_PI_MODELS", raising=False)
    monkeypatch.delenv("CIAO_OLLAMA_MODELS", raising=False)
    from ciao.config import CiaoConfig
    config = CiaoConfig.from_env()
    assert config.pi.models == ()
    assert config.claude_models == ["opus", "sonnet", "haiku"]


def test_provider_service_dispatches_pi_when_provider_is_pi(tmp_path: Path) -> None:
    from ciao.config import CiaoConfig
    from ciao.providers.pi import PiSettings
    from ciao.provider_service import ProviderService
    config = CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=tmp_path / "state.json",
        media_root=tmp_path / "media",
        pi=PiSettings(models=("qwen3-coder",)),
    )
    svc = ProviderService(config)
    provider = svc._ensure_provider("pi")
    from ciao.providers.pi import PiProvider
    assert isinstance(provider, PiProvider)


def test_provider_service_dispatches_claude_when_provider_is_claude(tmp_path: Path) -> None:
    from ciao.config import CiaoConfig
    from ciao.provider_service import ProviderService
    config = CiaoConfig(
        pwa_auth_token="test",
        workspace_root=tmp_path,
        state_path=tmp_path / "state.json",
        media_root=tmp_path / "media",
    )
    svc = ProviderService(config)
    provider = svc._ensure_provider("claude")
    from ciao.providers.claude import ClaudeProvider
    assert isinstance(provider, ClaudeProvider)


def test_update_chat_rejects_claude_to_pi_with_history(tmp_path: Path, monkeypatch) -> None:
    """A chat with history can't be flipped from Claude to Pi mid-stream."""
    from ciao.config import CiaoConfig
    from ciao.providers.pi import PiSettings
    from ciao.sessions import StateStore
    from ciao.transcripts import TranscriptStore
    from ciao.web.project_chats import ProjectChatManager
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.delenv("CIAO_OLLAMA_MODELS", raising=False)
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        pi=PiSettings(models=("qwen3-coder",)),
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    pcm = ProjectChatManager(
        config, state_store=state, transcript_store=transcripts, path=runtime / "web_projects.json"
    )
    project = pcm.create_project("cross", workspace="personal")
    chat = pcm.create_chat(project.project_id, model="opus", provider="claude")
    chat.user_turn_count = 1
    chat.session_id = "sess-existing"
    try:
        pcm.update_chat(chat.chat_id, model="qwen3-coder", provider="pi")
    except ValueError as exc:
        assert "close this chat" in str(exc).lower()
        return
    raise AssertionError("expected ValueError on cross-provider switch")


def test_update_chat_allows_pi_to_pi_with_history(tmp_path: Path, monkeypatch) -> None:
    """Same Pi model swap is fine."""
    from ciao.config import CiaoConfig
    from ciao.providers.pi import PiSettings
    from ciao.sessions import StateStore
    from ciao.transcripts import TranscriptStore
    from ciao.web.project_chats import ProjectChatManager
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.delenv("CIAO_OLLAMA_MODELS", raising=False)
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        pi=PiSettings(models=("qwen3-coder", "kimi-k2.7-code")),
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    pcm = ProjectChatManager(
        config, state_store=state, transcript_store=transcripts, path=runtime / "web_projects.json"
    )
    project = pcm.create_project("same-pi", workspace="personal")
    chat = pcm.create_chat(project.project_id, model="qwen3-coder", provider="pi")
    chat.user_turn_count = 2
    updated = pcm.update_chat(chat.chat_id, model="kimi-k2.7-code")
    assert updated is not None
    assert updated.model == "kimi-k2.7-code"
    assert updated.provider == "pi"


def test_build_extra_env_injects_chat_id(tmp_path: Path, monkeypatch) -> None:
    from ciao.config import CiaoConfig
    from ciao.providers.pi import PiSettings
    from ciao.sessions import StateStore
    from ciao.transcripts import TranscriptStore
    from ciao.web.project_chats import ProjectChatManager
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.delenv("CIAO_OLLAMA_MODELS", raising=False)
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        pi=PiSettings(models=("qwen3-coder",)),
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    pcm = ProjectChatManager(
        config, state_store=state, transcript_store=transcripts, path=runtime / "web_projects.json"
    )
    project = pcm.create_project("env-test", workspace="personal")
    chat = pcm.create_chat(project.project_id, model="qwen3-coder")
    env = pcm._build_extra_env(chat)
    assert env["CIAO_CHAT_ID"] == chat.chat_id


def test_create_chat_infers_pi_provider_for_pi_native_model(tmp_path: Path, monkeypatch) -> None:
    """Pi-native models (in CIAO_PI_MODELS, not in CIAO_OLLAMA_MODELS) land on Pi by default."""
    from ciao.config import CiaoConfig
    from ciao.providers.pi import PiSettings
    from ciao.sessions import StateStore
    from ciao.transcripts import TranscriptStore
    from ciao.web.project_chats import ProjectChatManager
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.delenv("CIAO_OLLAMA_MODELS", raising=False)
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        pi=PiSettings(models=("qwen3-coder",)),
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    pcm = ProjectChatManager(
        config, state_store=state, transcript_store=transcripts, path=runtime / "web_projects.json"
    )
    project = pcm.create_project("infer", workspace="personal")
    chat = pcm.create_chat(project.project_id, model="qwen3-coder")
    assert chat.provider == "pi"
    other = pcm.create_chat(project.project_id, model="opus")
    assert other.provider == "claude"


def test_legacy_chat_load_defaults_provider_to_claude(tmp_path: Path, monkeypatch) -> None:
    """A chat persisted before the `provider` field migrates to claude by default."""
    import json
    from ciao.config import CiaoConfig
    from ciao.sessions import StateStore
    from ciao.transcripts import TranscriptStore
    from ciao.web.project_chats import ProjectChatManager
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    legacy_path = runtime / "web_projects.json"
    legacy_path.write_text(json.dumps({
        "projects": {"p1": {"name": "P", "workspace": "personal"}},
        "chats": {
            "c1": {
                "project_id": "p1",
                "title": "Legacy chat",
                "model": "opus",
                "user_turn_count": 1,
                "session_id": "sess-x",
                "created_at": "2026-05-09",
            }
        },
    }))
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    pcm = ProjectChatManager(
        config, state_store=state, transcript_store=transcripts, path=legacy_path
    )
    chat = pcm.get_chat("c1")
    assert chat is not None
    assert chat.provider == "claude"


def test_list_models_endpoint_buckets(tmp_path: Path, monkeypatch) -> None:
    """Three picker buckets: claude_work, claude_personal, pi_personal."""
    monkeypatch.setenv("PWA_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("CIAO_OLLAMA_MODELS", "kimi-k2.7-code:cloud,deepseek-v4-flash:cloud")
    monkeypatch.setenv("CIAO_PI_MODELS", "qwen3-coder,kimi-k2.7-code:cloud")
    from starlette.requests import Request
    from ciao.config import CiaoConfig
    from ciao.web.routes_api import list_models
    config = CiaoConfig.from_env()
    scope = {"type": "http", "method": "GET", "path": "/api/models", "headers": []}
    class _App:
        class state:
            pass
    _App.state.config = config
    request = Request(scope)
    request._app = _App  # type: ignore[attr-defined]
    scope["app"] = _App
    import asyncio
    body = asyncio.run(list_models(request)).body
    import json
    data = json.loads(body)
    buckets = data["provider_models"]
    # Pi bucket includes Pi-natives first, then Ollama (deduped).
    assert buckets["pi_personal"] == ["qwen3-coder", "kimi-k2.7-code:cloud", "deepseek-v4-flash:cloud"]
    # Claude Work is Anthropic-only; Pi-natives and Ollama models stay out.
    assert "opus" in buckets["claude_work"]
    assert "qwen3-coder" not in buckets["claude_work"]
    assert "kimi-k2.7-code:cloud" not in buckets["claude_work"]
    # Claude Personal is exactly the Ollama set.
    assert set(buckets["claude_personal"]) == {"kimi-k2.7-code:cloud", "deepseek-v4-flash:cloud"}
    # ollama_models is exposed so the UI can derive a chat's bucket.
    assert data["ollama_models"] == ["kimi-k2.7-code:cloud", "deepseek-v4-flash:cloud"]


def test_upgrade_pi_skipped_when_npm_missing(monkeypatch) -> None:
    """No npm on PATH → upgrade_pi returns a no-op result."""
    import asyncio
    from ciao import upgrade as upgrade_mod
    monkeypatch.setattr(upgrade_mod.shutil, "which", lambda name: None)
    result = asyncio.run(upgrade_mod.upgrade_pi())
    assert result.success is False
    assert result.changed is False
    assert "npm not found" in result.stderr


def test_upgrade_pi_extensions_skipped_when_pi_missing(monkeypatch) -> None:
    """No pi on PATH → upgrade_pi_extensions returns []."""
    import asyncio
    from ciao import upgrade as upgrade_mod
    monkeypatch.setattr(upgrade_mod.shutil, "which", lambda name: None)
    result = asyncio.run(upgrade_mod.upgrade_pi_extensions())
    assert result == []
