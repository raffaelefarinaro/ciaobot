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
    assert data["insights_enabled"] is True
    # Session insights is chosen per provider now; the global knob is gone.
    assert data["provider_insights_models"] == {}
    assert "insights_model" not in data
    assert "insights_model_effective" not in data
    assert "trajectories_enabled" not in data
    # The Claude model list is the vocabulary the selectors offer.
    assert data["model_options"]["anthropic"] == ["opus", "sonnet", "haiku", "fable"]
    assert data["backends"] == {"anthropic": True}
    assert data["workspace_context"] == {
        "workspace_root": str(config.workspace_root),
        "vault_root": str(config.vault_root),
        # After the re-rooting `vault_root` is the emptied shared path — true but
        # useless alone — so the vaults that actually hold notes come with it.
        # One entry, unnamed, before the migration.
        "vault_roots": [
            {"workspace": "", "path": str(config.vault_root)},
        ],
    }


@pytest.mark.parametrize("sentinel", ["apple", "apfel", "Apple"])
def test_patching_the_retired_on_device_model_reads_as_automatic(
    monkeypatch, tmp_path, sentinel,
):
    monkeypatch.setattr("shutil.which", lambda cmd, path=None: None)
    client, config = _make_client(tmp_path)
    resp = client.patch(
        "/api/settings/routines",
        json={"provider_insights_models": {"claude": sentinel}},
    )
    assert resp.status_code == 200
    assert resp.json()["provider_insights_models"] == {}
    assert config.provider_insights_models == {}


def test_a_stored_on_device_model_is_dropped_on_load(tmp_path):
    """An install that picked Apple Intelligence before it was removed must not
    send the sentinel upstream as a literal model id."""
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "app_settings.json").write_text(
        json.dumps({
            "insights_model": "apple",
            "provider_insights_models": {"claude": "apfel", "opencode": "x/y"},
        }),
        encoding="utf-8",
    )
    client, config = _make_client(tmp_path)
    data = client.get("/api/settings/routines").json()
    assert data["provider_insights_models"] == {"opencode": "x/y"}
    assert config.provider_insights_models == {"opencode": "x/y"}


def test_patch_toggles_insights_enabled(tmp_path):
    client, config = _make_client(tmp_path)
    resp = client.patch(
        "/api/settings/routines",
        json={"insights_enabled": False},
    )

    assert resp.status_code == 200
    assert resp.json()["insights_enabled"] is False
    assert config.insights_enabled is False
    fresh = AppSettingsStore(tmp_path / ".runtime" / "app_settings.json")
    assert fresh.settings.insights_enabled is False


def test_patch_rejects_non_boolean_insights_enabled(tmp_path):
    client, _config = _make_client(tmp_path)
    resp = client.patch(
        "/api/settings/routines",
        json={"insights_enabled": "false"},
    )
    assert resp.status_code == 400


def test_patch_applies_provider_default_models(tmp_path):
    """The per-provider default-model map sets the new-chat default."""
    client, config = _make_client(tmp_path)
    resp = client.patch(
        "/api/settings/routines",
        json={"provider_default_models": {"opencode": "anthropic/claude-sonnet-4-6"}},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["provider_default_models"] == {
        "opencode": "anthropic/claude-sonnet-4-6"
    }
    assert config.provider_default_models == {
        "opencode": "anthropic/claude-sonnet-4-6"
    }
    fresh = AppSettingsStore(tmp_path / ".runtime" / "app_settings.json")
    assert fresh.settings.provider_default_models == {
        "opencode": "anthropic/claude-sonnet-4-6"
    }


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
    fresh = AppSettingsStore(tmp_path / ".runtime" / "app_settings.json")
    assert fresh.settings.provider_insights_models == {
        "opencode": "anthropic/claude-sonnet-4-6"
    }

    # Clearing one provider's entry puts it back on Automatic.
    client.patch("/api/settings/routines", json={"provider_insights_models": {}})
    assert config.provider_insights_models == {}


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


def test_route_503s_without_store(tmp_path):
    client, _config = _make_client(tmp_path)
    client.app.state.app_settings = None
    assert client.get("/api/settings/routines").status_code == 503


def test_routines_no_longer_reports_the_on_device_model(tmp_path):
    client, _config = _make_client(tmp_path)
    data = client.get("/api/settings/routines").json()
    assert "apple_model_available" not in data
    assert "apple_model_unavailable_reason" not in data


def test_server_settings_patch_keeps_the_running_values(monkeypatch, tmp_path):
    """The bind address and log level are saved now and used at the next start;
    developer mode applies at once."""
    monkeypatch.setattr("shutil.which", lambda cmd, path=None: None)
    client, config = _make_client(tmp_path)

    before = client.get("/api/settings/routines").json()
    assert before["pwa_host"] == "" and before["log_level"] == ""
    assert before["server_running"] == {"pwa_host": "0.0.0.0", "log_level": "info"}
    assert before["server_defaults"]["log_levels"] == ["debug", "info", "warning", "error"]

    res = client.patch(
        "/api/settings/routines",
        json={"pwa_host": "127.0.0.1", "log_level": "debug", "dev_mode": True},
    )

    assert res.status_code == 200
    data = res.json()
    assert (data["pwa_host"], data["log_level"], data["dev_mode"]) == ("127.0.0.1", "debug", True)
    assert data["server_running"] == {"pwa_host": "0.0.0.0", "log_level": "info"}
    assert config.dev_mode is True
    assert config.pwa_host == "0.0.0.0"


def test_server_settings_patch_rejects_a_bad_host(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda cmd, path=None: None)
    client, _config = _make_client(tmp_path)
    res = client.patch("/api/settings/routines", json={"pwa_host": "two words"})
    assert res.status_code == 400
