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
    assert data["title_model"] == ""  # no override stored
    # Automatic resolves to the workspace's tier alias; the provider running
    # the routine resolves that alias against its own catalog.
    assert data["title_model_effective"] == "haiku"
    assert data["insights_model_effective"] == "sonnet"
    # Tier aliases are the whole vocabulary the selectors offer.
    assert data["model_options"]["anthropic"] == ["haiku", "sonnet", "opus", "fable"]
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


def test_get_title_effective_is_haiku_not_apfel_when_no_override(monkeypatch, tmp_path):
    # apfel is opt-in, not the Automatic default: even with the binary on PATH,
    # Automatic resolves to the workspace haiku tier (apfel fails when Apple
    # Intelligence is disabled). See issue: "Automatic: apfel" mislabel.
    monkeypatch.setattr("shutil.which", lambda cmd, path=None: None)
    client, config = _make_client(tmp_path)
    data = client.get("/api/settings/routines").json()
    assert data["title_model"] == ""  # no override stored
    assert data["title_model_effective"] != "apfel"
    assert data["title_model_effective"] == "haiku"


def test_get_title_effective_is_apfel_when_explicitly_chosen(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda cmd, path=None: None)
    client, config = _make_client(tmp_path)
    resp = client.patch("/api/settings/routines", json={"title_model": "apfel"})
    assert resp.status_code == 200
    assert resp.json()["title_model_effective"] == "apfel"


def test_get_insights_effective_is_sonnet_not_apfel_when_no_override(
    monkeypatch, tmp_path,
):
    # Apple Intelligence is an explicit option, never the Automatic default.
    monkeypatch.setattr("shutil.which", lambda cmd, path=None: None)
    client, config = _make_client(tmp_path)
    data = client.get("/api/settings/routines").json()
    assert data["insights_model"] == ""
    assert data["insights_model_effective"] != "apfel"
    assert data["insights_model_effective"] == "sonnet"


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


def test_patch_applies_codex_tier_pins(tmp_path):
    """The legacy flat ``<provider>_<tier>_model`` PATCH shape still works."""
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
    assert data["provider_routing"]["codex"] == {
        "sonnet": "gpt-5.6-sol",
        "haiku": "gpt-5.6-terra",
    }
    # Per-provider effective tiers need the account catalog, so they live in
    # /api/models rather than here; this route only carries the pins.
    assert "alias_tiers" not in data
    assert config.codex.sonnet_model == "gpt-5.6-sol"
    fresh = AppSettingsStore(tmp_path / ".runtime" / "app_settings.json")
    assert fresh.tier_pin("codex", "sonnet") == "gpt-5.6-sol"


def test_patch_applies_provider_routing_map(tmp_path):
    """The canonical nested shape sets the same pins."""
    client, config = _make_client(tmp_path)
    resp = client.patch(
        "/api/settings/routines",
        json={"provider_routing": {"codex": {"sonnet": "gpt-5.6-sol"}}},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["provider_routing"] == {"codex": {"sonnet": "gpt-5.6-sol"}}
    assert data["codex_sonnet_model"] == "gpt-5.6-sol"
    assert config.codex.sonnet_model == "gpt-5.6-sol"
    fresh = AppSettingsStore(tmp_path / ".runtime" / "app_settings.json")
    assert fresh.tier_pin("codex", "sonnet") == "gpt-5.6-sol"


def test_legacy_flat_tier_pins_on_disk_are_migrated(tmp_path):
    """A settings file written before the map still resolves its pins."""
    path = tmp_path / ".runtime" / "app_settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"codex_opus_model": "gpt-5.6-sol"}), encoding="utf-8")

    store = AppSettingsStore(path)

    assert store.tier_pin("codex", "opus") == "gpt-5.6-sol"


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

    resolve_title_model / resolve_insights_model both read the chat's workspace,
    so *_effective (the primary workspace's answer) is wrong for every other
    workspace. The UI needs the whole map to say so.
    """
    monkeypatch.setattr("shutil.which", lambda cmd, path=None: None)
    client, config = _make_client(tmp_path)

    data = client.get("/api/settings/routines").json()
    names = config.workspace_names()
    assert names, "fixture should register at least one workspace"

    for key, expected in (
        ("title_model_by_workspace", "haiku"),
        ("insights_model_by_workspace", "sonnet"),
    ):
        assert set(data[key]) == set(names), f"{key} must cover every workspace"
        for name in names:
            assert data[key][name] == expected


def test_an_override_clears_the_per_workspace_maps(monkeypatch, tmp_path):
    """With an explicit override one model really does apply everywhere."""
    monkeypatch.setattr("shutil.which", lambda cmd, path=None: None)
    client, _config = _make_client(tmp_path)

    client.patch(
        "/api/settings/routines",
        json={"title_model": "gemma4:12b-it-qat", "insights_model": "gemma4:12b-it-qat"},
    )
    data = client.get("/api/settings/routines").json()

    assert data["title_model_effective"] == "gemma4:12b-it-qat"
    assert data["insights_model_effective"] == "gemma4:12b-it-qat"
    # Empty signals "not workspace-dependent" to the UI.
    assert data["title_model_by_workspace"] == {}
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
