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
    # Automatic resolves to the workspace's default model.
    assert data["insights_model_effective"] == config.claude_default_model
    assert data["insights_enabled"] is True
    assert data["trajectories_enabled"] is True
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
    resp = client.patch("/api/settings/routines", json={"insights_model": sentinel})
    assert resp.status_code == 200
    data = resp.json()
    assert data["insights_model"] == ""
    assert data["insights_model_effective"] == config.claude_default_model


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
    assert data["insights_model"] == ""
    assert config.insights_model_override == ""
    assert data["provider_insights_models"] == {"opencode": "x/y"}


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


def test_patch_toggles_trajectories_enabled(tmp_path):
    client, config = _make_client(tmp_path)
    resp = client.patch(
        "/api/settings/routines",
        json={"trajectories_enabled": False},
    )

    assert resp.status_code == 200
    assert resp.json()["trajectories_enabled"] is False
    assert config.trajectories_enabled is False
    fresh = AppSettingsStore(tmp_path / ".runtime" / "app_settings.json")
    assert fresh.settings.trajectories_enabled is False


def test_patch_rejects_non_boolean_trajectories_enabled(tmp_path):
    client, _config = _make_client(tmp_path)
    resp = client.patch(
        "/api/settings/routines",
        json={"trajectories_enabled": "false"},
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


def test_routines_no_longer_reports_the_on_device_model(tmp_path):
    client, _config = _make_client(tmp_path)
    data = client.get("/api/settings/routines").json()
    assert "apple_model_available" not in data
    assert "apple_model_unavailable_reason" not in data
