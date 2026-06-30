"""Tests for local Ollama daemon support: routing, discovery, Pi dispatch."""

from __future__ import annotations

import json

from ciao.providers.ollama import (
    OllamaSettings,
    discover_local_models,
    is_local_ollama_model,
    is_ollama_model,
    ollama_env_for_model,
    routine_env_for_model,
)
from ciao.providers.pi import (
    PiSettings,
    _resolve_provider_and_model,
    ensure_models_json,
)

CLOUD = OllamaSettings(
    models=("kimi-k2.7-code:cloud", "ministral-3:3b"),
    base_url="https://ollama.com",
    api_key="sk-cloud",
    local_models=("gemma4:12b-it-qat",),
    local_url="http://localhost:11434",
)


# ── Routing ──────────────────────────────────────────────────────────────


def test_local_and_cloud_models_both_count_as_ollama():
    assert is_ollama_model("kimi-k2.7-code:cloud", CLOUD)
    assert is_ollama_model("gemma4:12b-it-qat", CLOUD)
    assert not is_ollama_model("opus", CLOUD)
    assert is_local_ollama_model("gemma4:12b-it-qat", CLOUD)
    assert not is_local_ollama_model("kimi-k2.7-code:cloud", CLOUD)


def test_env_routes_local_to_daemon_with_literal_token():
    env = ollama_env_for_model("gemma4:12b-it-qat", CLOUD)
    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:11434"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "ollama"


def test_env_routes_cloud_to_cloud():
    env = ollama_env_for_model("kimi-k2.7-code:cloud", CLOUD)
    assert env["ANTHROPIC_BASE_URL"] == "https://ollama.com"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-cloud"


def test_env_empty_for_unrouted_model():
    assert ollama_env_for_model("opus", CLOUD) == {}


def test_routine_env_skips_anthropic_models():
    assert routine_env_for_model("haiku", CLOUD) == {}
    assert routine_env_for_model("claude-opus-4-8", CLOUD) == {}
    assert routine_env_for_model("", CLOUD) == {}


def test_routine_env_not_gated_on_allowlist():
    # Insights default model works even with an empty cloud allowlist.
    bare = OllamaSettings(base_url="https://ollama.com", api_key="sk")
    env = routine_env_for_model("deepseek-v4-flash:cloud", bare)
    assert env["ANTHROPIC_BASE_URL"] == "https://ollama.com"


def test_routine_env_prefers_local():
    env = routine_env_for_model("gemma4:12b-it-qat", CLOUD)
    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:11434"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "ollama"


# ── Discovery ────────────────────────────────────────────────────────────


def test_discover_local_models_parses_tags(monkeypatch):
    payload = {
        "models": [
            {"name": "gemma4:12b-it-qat"},
            {"name": "gemma4:12b-it-qat"},  # dupe dropped
            {"name": ""},  # empty dropped
            "garbage",  # non-dict dropped
        ]
    }

    class FakeResponse:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda url, timeout: FakeResponse()
    )
    assert discover_local_models("http://localhost:11434") == ("gemma4:12b-it-qat",)


def test_discover_local_models_unreachable(monkeypatch):
    def boom(url, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert discover_local_models("http://localhost:11434") == ()


def test_refresh_local_ollama_models_merges_and_syncs(tmp_path, monkeypatch):
    """Live re-discovery merges new daemon models into pickers and Pi."""
    from ciao.config import CiaoConfig, refresh_local_ollama_models

    config = CiaoConfig.from_env(
        {
            "PWA_AUTH_TOKEN": "t",
            "CIAO_WORKSPACE": str(tmp_path),
            "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
            "CIAO_OLLAMA_MODELS": "kimi-k2.7-code:cloud",
            "CIAO_OLLAMA_API_KEY": "sk",
        }
    )
    monkeypatch.setattr(
        "ciao.providers.ollama.discover_local_models",
        lambda url, timeout_s=2.0: ("gemma4:4b", "kimi-k2.7-code:cloud"),
    )
    written = {}
    monkeypatch.setattr(
        "ciao.providers.pi.ensure_models_json",
        lambda *a, **kw: written.update(kw) or (tmp_path / "models.json"),
    )

    assert refresh_local_ollama_models(config) is True
    # Cloud-allowlisted id keeps cloud routing; only the new local id lands.
    assert config.ollama.local_models == ("gemma4:4b",)
    assert config.pi.local_models == ("gemma4:4b",)
    assert "gemma4:4b" in config.claude_models
    assert written["local_models"] == ("gemma4:4b",)
    # Second run: nothing new, no rewrite.
    assert refresh_local_ollama_models(config) is False


def test_refresh_local_ollama_models_respects_disable(tmp_path, monkeypatch):
    from ciao.config import CiaoConfig, refresh_local_ollama_models

    config = CiaoConfig.from_env(
        {
            "PWA_AUTH_TOKEN": "t",
            "CIAO_WORKSPACE": str(tmp_path),
            "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
            "CIAO_OLLAMA_LOCAL_DISCOVERY": "0",
        }
    )

    def boom(url, timeout_s=2.0):
        raise AssertionError("discovery must not run when disabled")

    monkeypatch.setattr("ciao.providers.ollama.discover_local_models", boom)
    assert refresh_local_ollama_models(config) is False


# ── Pi dispatch ──────────────────────────────────────────────────────────


def test_pi_resolver_routes_local_models():
    provider, model = _resolve_provider_and_model(
        "gemma4:12b-it-qat", "ollama", ("gemma4:12b-it-qat",)
    )
    assert (provider, model) == ("ollama-local", "gemma4:12b-it-qat")
    # Built-in prefixes still win over the local set.
    provider, model = _resolve_provider_and_model(
        "openai/gpt-4o", "ollama", ("openai/gpt-4o",)
    )
    assert (provider, model) == ("openai", "gpt-4o")
    # Non-local bare ids keep the default provider.
    provider, model = _resolve_provider_and_model("kimi-k2.7-code:cloud", "ollama", ())
    assert (provider, model) == ("ollama", "kimi-k2.7-code:cloud")


def test_models_json_writes_both_providers(tmp_path):
    path = tmp_path / "models.json"
    ensure_models_json(
        PiSettings(models=("kimi-k2.7-code:cloud",), provider="ollama"),
        ollama_base_url="https://ollama.com",
        ollama_api_key="sk-cloud",
        local_models=("gemma4:12b-it-qat",),
        local_url="http://localhost:11434",
        path=path,
    )
    config = json.loads(path.read_text())
    providers = config["providers"]
    assert set(providers) == {"ollama", "ollama-local"}
    local = providers["ollama-local"]
    assert local["baseUrl"] == "http://localhost:11434/v1"
    assert local["api"] == "openai-completions"
    assert local["models"] == [{"id": "gemma4:12b-it-qat"}]
    # Cloud provider untouched.
    assert providers["ollama"]["api"] == "anthropic-messages"


def test_models_json_local_only_with_builtin_default_provider(tmp_path):
    # Default provider is built-in (no entry needed), but local models still
    # need the ollama-local provider registered.
    path = tmp_path / "models.json"
    ensure_models_json(
        PiSettings(models=(), provider="openai"),
        ollama_base_url="https://ollama.com",
        ollama_api_key="sk",
        local_models=("gemma4:12b-it-qat",),
        path=path,
    )
    config = json.loads(path.read_text())
    assert set(config["providers"]) == {"ollama-local"}


def test_local_to_cloud_ollama_switch_rejected_mid_chat(tmp_path):
    """Local and cloud Ollama models are distinct spawn kinds: the CLI's
    ANTHROPIC_BASE_URL is fixed at spawn, so switching between them
    mid-chat must be rejected like any cross-provider swap."""
    import pytest

    from ciao.config import CiaoConfig
    from ciao.sessions import StateStore
    from ciao.transcripts import TranscriptStore
    from ciao.web.project_chats import ProjectChatManager

    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True)
    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        ollama=CLOUD,
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    pcm = ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )
    project = pcm.create_project("local-switch", workspace="personal")
    chat = pcm.create_chat(project.project_id, model="kimi-k2.7-code:cloud")
    chat.user_turn_count = 1

    with pytest.raises(ValueError):
        pcm.update_chat(chat.chat_id, model="gemma4:12b-it-qat")

    # Fresh chat: switch is fine (no subprocess spawned yet).
    fresh = pcm.create_chat(project.project_id, model="kimi-k2.7-code:cloud")
    updated = pcm.update_chat(fresh.chat_id, model="gemma4:12b-it-qat")
    assert updated is not None and updated.model == "gemma4:12b-it-qat"


def test_models_json_builtin_provider_no_locals_writes_nothing(tmp_path):
    path = tmp_path / "models.json"
    ensure_models_json(
        PiSettings(models=(), provider="openai"),
        ollama_base_url="https://ollama.com",
        ollama_api_key="sk",
        path=path,
    )
    assert not path.exists()
