"""Tests for GET/PATCH /api/settings/routines (Settings → Models tab)."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ciao.app_settings import AppSettingsStore
from ciao.config import CiaoConfig
from ciao.web.routes_api import settings_routines


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
    assert data["title_model_effective"] == config.ollama.title_model
    assert data["insights_model_effective"] == "deepseek-v4-flash:cloud"
    assert data["model_options"]["ollama_cloud"] == ["kimi-k2.7-code:cloud"]
    assert data["model_options"]["ollama_local"] == ["gemma4:12b-it-qat"]
    assert data["transcription"]["engine"] == "cloud"
    assert data["transcription"]["cloud_available"] is True


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
    assert config.insights_model == "haiku"
    # Persisted: a fresh store sees the values.
    fresh = AppSettingsStore(tmp_path / ".runtime" / "app_settings.json")
    assert fresh.settings.title_model == "gemma4:12b-it-qat"


def test_patch_clearing_restores_defaults(tmp_path):
    client, config = _make_client(tmp_path)
    client.patch("/api/settings/routines", json={"insights_model": "haiku"})
    client.patch("/api/settings/routines", json={"insights_model": ""})
    assert config.insights_model == "deepseek-v4-flash:cloud"


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


def test_route_503s_without_store(tmp_path):
    client, _config = _make_client(tmp_path)
    client.app.state.app_settings = None
    assert client.get("/api/settings/routines").status_code == 503
