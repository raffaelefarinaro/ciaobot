"""Tests for GET/PATCH /api/settings/routines (Settings → Models tab)."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

import pytest

from ciao.app_settings import AppSettingsStore
from ciao.config import CiaoConfig
from ciao.web.routes_api import settings_routines


@pytest.fixture(autouse=True)
def mock_discoveries(monkeypatch):
    monkeypatch.setattr(
        "ciao.providers.ollama.discover_cloud_models",
        lambda settings, timeout_s=4.0: (),
    )
    monkeypatch.setattr(
        "ciao.providers.openrouter.discover_models",
        lambda settings, timeout_s=4.0, anthropic_only=False: (),
    )


def _make_client(tmp_path, env_extra: dict[str, str] | None = None):
    env = {
        "PWA_AUTH_TOKEN": "t",
        "CIAO_WORKSPACE": str(tmp_path),
        "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
        "CIAO_OLLAMA_MODELS": "kimi-k2.7-code:cloud",
        "CIAO_OLLAMA_API_KEY": "sk-cloud",
        "CIAO_OLLAMA_LOCAL_MODELS": "gemma4:12b-it-qat",
        # Keep tests off the network: the settings GET re-discovers local
        # daemon models when this is enabled.
        "CIAO_OLLAMA_LOCAL_DISCOVERY": "0",
        "OPENAI_API_KEY": "sk-openai",
    }
    env.update(env_extra or {})
    config = CiaoConfig.from_env(env)
    store = AppSettingsStore(tmp_path / ".runtime" / "app_settings.json")
    store.apply_to_config(config)
    app = Starlette(
        routes=[
            Route(
                "/api/settings/routines",
                settings_routines,
                methods=["GET", "PATCH"],
            )
        ]
    )
    app.state.config = config
    app.state.app_settings = store
    return TestClient(app), config


def test_get_returns_effective_models_and_options(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    client, config = _make_client(tmp_path)
    data = client.get("/api/settings/routines").json()
    assert data["title_model"] == ""  # no override stored
    # Ollama configured → free-tier title model is the effective default.
    assert data["title_model_effective"] == config.haiku_model_for_workspace("personal")
    assert data["insights_model_effective"] == config.sonnet_model_for_workspace("personal")
    assert data["alias_tiers"]["ollama"]["sonnet"] == config.ollama.sonnet_model
    assert data["alias_tiers"]["ollama"]["fable"] == "glm-5.2:cloud"
    assert "codex" not in data["alias_tiers"]
    assert data["tier_defaults"]["ollama"]["sonnet"] == "kimi-k2.7-code:cloud"
    assert data["tier_defaults"]["openrouter"]["fable"] == "anthropic/claude-fable-latest"
    assert data["model_options"]["ollama_cloud"] == [
        "kimi-k2.7-code:cloud",
        "deepseek-v4-flash:cloud",
        "glm-5.2:cloud",
        "gemma4:e2b-it-qat",
    ]
    assert data["model_options"]["ollama_local"] == ["gemma4:12b-it-qat"]
    assert data["workspace_context"] == {
        "workspace_root": str(config.workspace_root),
        "vault_root": str(config.vault_root),
    }
    assert data["transcription"]["engine"] == "cloud"
    assert data["transcription"]["cloud_available"] is True
    assert data["speech"]["engine"] == "cloud"
    assert data["speech"]["cloud_available"] is True
    assert data["speech"]["cloud_voice"] == "nova"
    assert data["speech"]["local_voice"] == "af_heart"


def test_get_returns_apfel_effective_when_installed(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda cmd: "/opt/homebrew/bin/apfel" if cmd == "apfel" else None)
    client, config = _make_client(tmp_path)
    data = client.get("/api/settings/routines").json()
    assert data["title_model"] == ""  # no override stored
    assert data["title_model_effective"] == "apfel"


def test_patch_applies_to_live_config_and_persists(tmp_path):
    client, config = _make_client(tmp_path)
    resp = client.patch(
        "/api/settings/routines",
        json={"title_model": "gemma4:12b-it-qat", "insights_model": "haiku"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["title_model_effective"] == "gemma4:12b-it-qat"
    assert data["insights_model_effective"] == "haiku"
    # Live config updated, no restart needed.
    assert config.title_model_override == "gemma4:12b-it-qat"
    assert config.insights_model_override == "haiku"
    # Persisted: a fresh store sees the values.
    fresh = AppSettingsStore(tmp_path / ".runtime" / "app_settings.json")
    assert fresh.settings.title_model == "gemma4:12b-it-qat"


def test_patch_applies_tier_model_overrides(tmp_path):
    client, config = _make_client(tmp_path, {"OPENROUTER_API_KEY": "sk-or"})
    resp = client.patch(
        "/api/settings/routines",
        json={
            "ollama_haiku_model": "llama3.1:latest",
            "openrouter_sonnet_model": "anthropic/claude-sonnet-4.6",
            "ollama_fable_model": "minimax-m3:cloud",
            "openrouter_fable_model": "anthropic/claude-fable-5",
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ollama_haiku_model"] == "llama3.1:latest"
    assert data["openrouter_sonnet_model"] == "anthropic/claude-sonnet-4.6"
    assert data["ollama_fable_model"] == "minimax-m3:cloud"
    assert data["openrouter_fable_model"] == "anthropic/claude-fable-5"
    assert data["alias_tiers"]["ollama"]["haiku"] == "llama3.1:latest"
    assert data["alias_tiers"]["openrouter"]["sonnet"] == "anthropic/claude-sonnet-4.6"
    assert data["alias_tiers"]["ollama"]["fable"] == "minimax-m3:cloud"
    assert data["alias_tiers"]["openrouter"]["fable"] == "anthropic/claude-fable-5"
    assert config.ollama.haiku_model == "llama3.1:latest"
    assert config.openrouter.sonnet_model == "anthropic/claude-sonnet-4.6"
    assert config.ollama.fable_model == "minimax-m3:cloud"
    assert config.openrouter.fable_model == "anthropic/claude-fable-5"


def test_patch_applies_codex_tier_pins(tmp_path):
    client, config = _make_client(tmp_path)
    resp = client.patch(
        "/api/settings/routines",
        json={"codex_sonnet_model": "gpt-5.6-sol", "codex_haiku_model": "gpt-5.6-terra"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["codex_sonnet_model"] == "gpt-5.6-sol"
    assert data["codex_haiku_model"] == "gpt-5.6-terra"
    assert data["codex_opus_model"] == ""
    # Codex effective tiers need the account catalog, so they live in
    # /api/models, not here.
    assert "codex" not in data["alias_tiers"]
    assert config.codex.sonnet_model == "gpt-5.6-sol"
    fresh = AppSettingsStore(tmp_path / ".runtime" / "app_settings.json")
    assert fresh.settings.codex_sonnet_model == "gpt-5.6-sol"


def test_patch_clearing_restores_defaults(tmp_path):
    client, config = _make_client(tmp_path)
    client.patch("/api/settings/routines", json={"insights_model": "haiku"})
    client.patch("/api/settings/routines", json={"insights_model": ""})
    assert config.insights_model_override == ""


def test_patch_rejects_bad_engine(tmp_path):
    client, config = _make_client(tmp_path)
    resp = client.patch(
        "/api/settings/routines", json={"transcription_engine": "telepathy"}
    )
    assert resp.status_code == 400
    assert config.transcription_engine == "cloud"


def test_patch_engine_local(tmp_path):
    client, config = _make_client(tmp_path)
    resp = client.patch(
        "/api/settings/routines", json={"transcription_engine": "local"}
    )
    assert resp.status_code == 200
    assert config.transcription_engine == "local"
    assert resp.json()["transcription"]["engine"] == "local"


def test_patch_tts_engine_local(tmp_path):
    client, config = _make_client(tmp_path)
    resp = client.patch("/api/settings/routines", json={"tts_engine": "local"})
    assert resp.status_code == 200
    assert config.tts_engine == "local"
    assert resp.json()["speech"]["engine"] == "local"


def test_patch_rejects_bad_tts_engine(tmp_path):
    client, config = _make_client(tmp_path)
    resp = client.patch("/api/settings/routines", json={"tts_engine": "megaphone"})
    assert resp.status_code == 400
    assert config.tts_engine == "cloud"


def test_route_503s_without_store(tmp_path):
    client, _config = _make_client(tmp_path)
    client.app.state.app_settings = None
    assert client.get("/api/settings/routines").status_code == 503
