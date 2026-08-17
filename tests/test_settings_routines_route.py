"""Tests for GET/PATCH /api/settings/routines (Settings → Models tab)."""

from __future__ import annotations

import json

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

import pytest

from ciao.app_settings import AppSettingsStore
from ciao.config import CiaoConfig
from ciao.web.routes_api import settings_routines


@pytest.fixture(autouse=True)
def reset_apple_beta_flag():
    """Keep the module-level beta flag from leaking between route tests."""
    from ciao import native_sidecar

    native_sidecar.reset_probe_cache()
    yield
    native_sidecar.reset_probe_cache()


def _make_client(tmp_path, env_extra: dict[str, str] | None = None):
    env = {
        "PWA_AUTH_TOKEN": "t",
        "CIAO_WORKSPACE": str(tmp_path),
        "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
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
    monkeypatch.setattr("shutil.which", lambda cmd, path=None: None)
    client, config = _make_client(tmp_path)
    data = client.get("/api/settings/routines").json()
    # Automatic resolves to the workspace's default model.
    assert data["insights_model_effective"] == config.claude_default_model
    # The Claude model list is the vocabulary the selectors offer.
    assert data["model_options"]["anthropic"] == list(config.claude_models)
    assert data["backends"] == {"anthropic": True}
    assert data["workspace_context"] == {
        "workspace_root": str(config.workspace_root),
        "vault_root": str(config.vault_root),
    }
    # Voice is on-device only: availability and a reason, no engine to pick.
    assert data["transcription"]["locale"] == "en-US"
    assert isinstance(data["transcription"]["available"], bool)
    assert isinstance(data["transcription"]["unavailable_reason"], str)
    assert isinstance(data["speech"]["available"], bool)
    # Empty local voice = "best installed voice for the locale"; the picker is
    # populated from the machine rather than a hardcoded default.
    assert data["speech"]["local_voice"] == ""
    assert isinstance(data["speech"]["local_voices"], list)


def test_get_insights_effective_is_default_not_apfel_when_no_override(
    monkeypatch, tmp_path,
):
    # Apple Intelligence is an explicit option, never the Automatic default.
    monkeypatch.setattr("shutil.which", lambda cmd, path=None: None)
    client, config = _make_client(tmp_path)
    data = client.get("/api/settings/routines").json()
    assert data["insights_model"] == ""
    assert data["insights_model_effective"] != "apfel"
    assert data["insights_model_effective"] == config.claude_default_model


def test_get_insights_effective_is_apfel_when_explicitly_chosen(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr("shutil.which", lambda cmd, path=None: None)
    client, config = _make_client(tmp_path)
    resp = client.patch("/api/settings/routines", json={"insights_model": "apfel"})
    assert resp.status_code == 200
    assert resp.json()["insights_model_effective"] == "apfel"


def test_patch_applies_to_live_config_and_persists(tmp_path):
    client, config = _make_client(tmp_path)
    resp = client.patch(
        "/api/settings/routines",
        json={"insights_model": "haiku"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["insights_model_effective"] == "haiku"
    # Live config updated, no restart needed.
    assert config.insights_model_override == "haiku"
    # Persisted: a fresh store sees the values.
    fresh = AppSettingsStore(tmp_path / ".runtime" / "app_settings.json")
    assert fresh.settings.insights_model == "haiku"


def test_patch_applies_provider_default_models(tmp_path):
    """The per-provider default-model map sets the new-chat default."""
    client, config = _make_client(tmp_path)
    resp = client.patch(
        "/api/settings/routines",
        json={"provider_default_models": {"codex": "gpt-5.6-sol"}},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["provider_default_models"] == {"codex": "gpt-5.6-sol"}
    assert config.provider_default_models == {"codex": "gpt-5.6-sol"}
    fresh = AppSettingsStore(tmp_path / ".runtime" / "app_settings.json")
    assert fresh.settings.provider_default_models == {"codex": "gpt-5.6-sol"}


def test_patch_applies_provider_routine_models(tmp_path):
    """Per-provider insights models are stored and applied."""
    client, config = _make_client(tmp_path)
    resp = client.patch(
        "/api/settings/routines",
        json={
            "provider_insights_models": {"opencode": "anthropic/claude-sonnet-4-6"},
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["provider_insights_models"] == {"opencode": "anthropic/claude-sonnet-4-6"}
    assert config.provider_insights_models == {"opencode": "anthropic/claude-sonnet-4-6"}


def test_patch_applies_provider_default_thinking(tmp_path):
    """The per-provider default thinking map is stored and applied."""
    client, config = _make_client(tmp_path)
    resp = client.patch(
        "/api/settings/routines",
        json={"provider_default_thinking": {"claude": "high"}},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["provider_default_thinking"] == {"claude": "high"}
    assert config.provider_default_thinking == {"claude": "high"}


def test_patch_clearing_restores_defaults(tmp_path):
    client, config = _make_client(tmp_path)
    client.patch("/api/settings/routines", json={"insights_model": "haiku"})
    client.patch("/api/settings/routines", json={"insights_model": ""})
    assert config.insights_model_override == ""


def test_route_503s_without_store(tmp_path):
    client, _config = _make_client(tmp_path)
    client.app.state.app_settings = None
    assert client.get("/api/settings/routines").status_code == 503


def test_automatic_routines_report_every_workspace_not_just_the_primary(
    monkeypatch, tmp_path
):
    """Automatic resolves per workspace, so one model must not be presented as global.

    resolve_insights_model reads the chat's workspace, so *_effective (the
    primary workspace's answer) is wrong for every other workspace. The UI
    needs the whole map to say so.
    """
    monkeypatch.setattr("shutil.which", lambda cmd, path=None: None)
    client, config = _make_client(tmp_path)

    data = client.get("/api/settings/routines").json()
    names = config.workspace_names()
    assert names, "fixture should register at least one workspace"

    key = "insights_model_by_workspace"
    assert set(data[key]) == set(names), f"{key} must cover every workspace"
    for name in names:
        assert data[key][name] == config.claude_default_model


def test_an_override_clears_the_per_workspace_maps(monkeypatch, tmp_path):
    """With an explicit override one model really does apply everywhere."""
    monkeypatch.setattr("shutil.which", lambda cmd, path=None: None)
    client, _config = _make_client(tmp_path)

    client.patch(
        "/api/settings/routines",
        json={"insights_model": "gemma4:12b-it-qat"},
    )
    data = client.get("/api/settings/routines").json()

    assert data["insights_model_effective"] == "gemma4:12b-it-qat"
    # Empty signals "not workspace-dependent" to the UI.
    assert data["insights_model_by_workspace"] == {}


def test_patch_persists_the_voice_locale_and_voice(tmp_path):
    """What is left to configure once the engine choice is gone: the language
    the on-device engines use, and which installed voice reads aloud."""
    client, config = _make_client(tmp_path)
    resp = client.patch(
        "/api/settings/routines",
        json={"transcription_locale": "it-IT", "tts_local_voice": "com.apple.voice.x"},
    )
    assert resp.status_code == 200
    assert config.transcription_locale == "it-IT"
    assert config.tts_local_voice == "com.apple.voice.x"
    assert not hasattr(config, "transcription_engine")
    assert not hasattr(config, "tts_engine")


def test_apple_intelligence_is_beta_and_off_by_default(tmp_path):
    """GET reports the flag plus the beta marker, with the option unavailable."""
    client, config = _make_client(tmp_path)
    data = client.get("/api/settings/routines").json()
    assert data["apple_intelligence_beta"] is True
    assert data["apple_intelligence_enabled"] is False
    assert config.apple_intelligence_enabled is False


def test_patch_enables_apple_intelligence_and_syncs_the_sidecar(tmp_path):
    from ciao import native_sidecar

    native_sidecar.set_apple_intelligence_enabled(False)
    client, config = _make_client(tmp_path)
    resp = client.patch("/api/settings/routines", json={"apple_intelligence_enabled": True})
    assert resp.status_code == 200
    data = resp.json()
    assert data["apple_intelligence_enabled"] is True
    # Live config updated and the sidecar latch follows it, no restart needed.
    assert config.apple_intelligence_enabled is True
    assert native_sidecar.apple_intelligence_enabled() is True
    # Persisted: a fresh store sees the toggle.
    fresh = AppSettingsStore(tmp_path / ".runtime" / "app_settings.json")
    assert fresh.settings.apple_intelligence_enabled is True


def test_patch_rejects_a_non_boolean_apple_toggle(tmp_path):
    client, _config = _make_client(tmp_path)
    resp = client.patch("/api/settings/routines", json={"apple_intelligence_enabled": "yes"})
    assert resp.status_code == 400
