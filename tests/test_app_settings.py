"""Tests for the runtime app-settings store (Settings → Models tab)."""

from __future__ import annotations

import json

import pytest

from ciao.app_settings import AppSettings, AppSettingsStore


class FakeConfig:
    """Just the fields apply_to_config touches."""

    def __init__(self) -> None:
        self.insights_model = "sonnet"
        self.insights_enabled = True
        self.critique_models = ""
        # Per-provider default models / thinking / routine models; no
        # env-backed defaults.
        self.provider_default_models: dict[str, str] = {}
        self.provider_default_thinking: dict[str, str] = {}
        self.provider_insights_models: dict[str, str] = {}


def test_load_missing_file_gives_defaults(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    assert store.settings == AppSettings()
    assert store.settings.insights_enabled is True


def test_insights_enabled_persists_and_applies(tmp_path):
    path = tmp_path / "app_settings.json"
    store = AppSettingsStore(path)
    config = FakeConfig()

    store.update({"insights_enabled": False})
    store.apply_to_config(config)

    assert config.insights_enabled is False
    assert json.loads(path.read_text())["insights_enabled"] is False
    assert AppSettingsStore(path).settings.insights_enabled is False

    store.update({"insights_enabled": True})
    assert json.loads(path.read_text())["insights_enabled"] is True


def test_insights_enabled_rejects_non_boolean(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    with pytest.raises(ValueError, match="must be a boolean"):
        store.update({"insights_enabled": "false"})


def test_legacy_insights_opt_out_migrates_once(tmp_path):
    path = tmp_path / "app_settings.json"
    store = AppSettingsStore(path)

    assert store.migrate_legacy_insights_enabled(True) is False
    assert json.loads(path.read_text())["insights_enabled"] is False
    assert store.migrate_legacy_insights_enabled(False) is None
    assert json.loads(path.read_text())["insights_enabled"] is False


def test_explicit_insights_setting_wins_over_legacy_migration(tmp_path):
    path = tmp_path / "app_settings.json"
    path.write_text(json.dumps({"insights_enabled": True}))
    store = AppSettingsStore(path)

    assert store.migrate_legacy_insights_enabled(True) is None
    assert store.settings.insights_enabled is True


def test_load_ignores_unknown_keys_and_non_strings(tmp_path):
    path = tmp_path / "app_settings.json"
    path.write_text(
        json.dumps(
            {
                "bogus": "x",
                "critique_models": 42,
                "insights_enabled": "false",
                # Retired with trajectory capture: an old file still carries it.
                "trajectories_enabled": "false",
            }
        )
    )
    store = AppSettingsStore(path)
    assert store.settings.critique_models == ""
    assert store.settings.insights_enabled is True
    assert not hasattr(store.settings, "trajectories_enabled")


def test_load_corrupt_file_gives_defaults(tmp_path):
    path = tmp_path / "app_settings.json"
    path.write_text("{not json")
    assert AppSettingsStore(path).settings == AppSettings()


def test_update_persists_and_roundtrips(tmp_path):
    path = tmp_path / "app_settings.json"
    store = AppSettingsStore(path)
    store.update({"critique_models": "opus,haiku", "ignored": "x"})
    assert json.loads(path.read_text()) == {
        "insights_enabled": True,
        "backup_enabled": True,
        "backup_paused": False,
        "critique_models": "opus,haiku",
    }
    # Fresh instance sees the persisted value.
    assert AppSettingsStore(path).settings.critique_models == "opus,haiku"


def test_update_rejects_a_non_string_value(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    # Engine-value validation went with the cloud engines; type checking is
    # what is left.
    with pytest.raises(ValueError):
        store.update({"critique_models": 3})


@pytest.mark.parametrize(
    ("legacy", "existing", "expected"),
    [
        # A bare id was a Claude model.
        ("haiku", None, {"claude": "haiku"}),
        # opencode was qualified so the global knob did not route through Claude.
        ("opencode:vendor/model", None, {"opencode": "vendor/model"}),
        # A provider that already has its own pick keeps it.
        ("haiku", {"claude": "opus"}, {"claude": "opus"}),
        ("opencode:vendor/model", {"claude": "opus"}, {"claude": "opus", "opencode": "vendor/model"}),
        # Automatic and the retired on-device sentinel carry nothing over.
        ("", None, None),
        ("apple", None, None),
    ],
)
def test_global_insights_model_moves_to_its_provider(tmp_path, legacy, existing, expected):
    path = tmp_path / "app_settings.json"
    raw: dict[str, object] = {"insights_model": legacy}
    if existing is not None:
        raw["provider_insights_models"] = existing
    path.write_text(json.dumps(raw))

    store = AppSettingsStore(path)

    assert store.settings.provider_insights_models == expected
    on_disk = json.loads(path.read_text())
    # Rewritten once, so the retired key does not linger in the file.
    assert "insights_model" not in on_disk
    assert on_disk.get("provider_insights_models") == expected
    config = FakeConfig()
    store.apply_to_config(config)
    assert config.provider_insights_models == (expected or {})


def test_a_file_without_the_global_insights_model_is_not_rewritten(tmp_path):
    path = tmp_path / "app_settings.json"
    path.write_text('{"critique_models": "opus"}')
    AppSettingsStore(path)
    assert path.read_text() == '{"critique_models": "opus"}'


def test_critique_models_override_applies(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    config = FakeConfig()
    store.update({"critique_models": "opus,opencode:fable"})
    store.apply_to_config(config)
    assert config.critique_models == "opus,opencode:fable"
    store.update({"critique_models": ""})
    store.apply_to_config(config)
    assert config.critique_models == ""


def test_provider_routine_models_persist_and_apply(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    config = FakeConfig()

    store.update({
        "provider_insights_models": {"opencode": "anthropic/claude-sonnet-4-6"},
        "provider_default_thinking": {"claude": "high"},
    })
    store.apply_to_config(config)
    assert config.provider_insights_models == {"opencode": "anthropic/claude-sonnet-4-6"}
    assert config.provider_default_thinking == {"claude": "high"}
    path = tmp_path / "app_settings.json"
    assert json.loads(path.read_text()) == {
        "insights_enabled": True,
        "backup_enabled": True,
        "backup_paused": False,
        "provider_insights_models": {"opencode": "anthropic/claude-sonnet-4-6"},
        "provider_default_thinking": {"claude": "high"},
    }
    # Fresh instance sees the persisted maps.
    fresh = AppSettingsStore(path)
    assert fresh.settings.provider_insights_models == {"opencode": "anthropic/claude-sonnet-4-6"}
    assert fresh.settings.provider_default_thinking == {"claude": "high"}


def test_provider_maps_reject_non_objects(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    with pytest.raises(ValueError, match="must be an object"):
        store.update({"provider_default_models": "gpt-5.6-sol"})
    with pytest.raises(ValueError, match="must be an object"):
        store.update({"provider_insights_models": "gpt-5.6-luna"})


def test_provider_maps_load_ignores_junk(tmp_path):
    path = tmp_path / "app_settings.json"
    path.write_text(
        json.dumps(
            {
                "provider_default_models": {
                    "opencode": "anthropic/claude-sonnet-4-6",
                    "bogus": "auto",
                },
                "provider_insights_models": {
                    "opencode": "anthropic/claude-sonnet-4-6",
                    "claude": 42,
                },
            }
        )
    )
    store = AppSettingsStore(path)
    assert store.settings.provider_default_models == {
        "opencode": "anthropic/claude-sonnet-4-6"
    }
    assert store.settings.provider_insights_models == {"opencode": "anthropic/claude-sonnet-4-6"}


def test_default_mode_for_provider_builtin_defaults(tmp_path):
    from ciao.config import CiaoConfig

    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
    )
    # With no operator pin, every provider starts on the app-wide auto default.
    assert config.default_mode_for_provider("opencode") == "auto"
    assert config.default_mode_for_provider("claude") == "auto"


def test_provider_default_modes_roundtrip(tmp_path):
    from ciao.config import CiaoConfig

    path = tmp_path / "app_settings.json"
    store = AppSettingsStore(path)
    # The PWA-facing "manual" persists as stored and applies to the live
    # config as the BridgeMode "normal" the providers understand.
    store.update({"provider_default_modes": {"claude": "manual", "opencode": "bypass"}})
    assert store.settings.provider_default_modes == {
        "claude": "manual",
        "opencode": "bypass",
    }
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
    )
    store.apply_to_config(config)
    assert config.default_mode_for_provider("claude") == "normal"
    assert config.default_mode_for_provider("opencode") == "bypass"


def test_provider_default_modes_reject_bad_values(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    with pytest.raises(ValueError, match="must be one of"):
        store.update({"provider_default_modes": {"claude": "yolo"}})
    with pytest.raises(ValueError, match="must be an object"):
        store.update({"provider_default_modes": "bypass"})
    # Junk on load is dropped, not fatal.
    path = tmp_path / "app_settings.json"
    path.write_text(
        json.dumps({"provider_default_modes": {"claude": "plan", "opencode": "manual", "x": "auto"}})
    )
    fresh = AppSettingsStore(path)
    assert fresh.settings.provider_default_modes == {"opencode": "manual"}


# -- server settings that used to be workspace `.env` variables --------------


def test_server_settings_default_and_apply(tmp_path):
    from pathlib import Path

    store = AppSettingsStore(tmp_path / "app_settings.json")
    config = FakeConfig()
    store.apply_to_config(config)
    assert (config.pwa_host, config.log_level, config.dev_mode, config.app_repo) == (
        "0.0.0.0", "info", False, None,
    )

    store.update({
        "pwa_host": "127.0.0.1",
        "log_level": "DEBUG",
        "dev_mode": True,
        "app_repo": str(tmp_path / "checkout"),
    })
    store.apply_to_config(config)

    # The bind address and the log level are startup-only: the live config
    # keeps what the running server uses until the next start.
    assert (config.pwa_host, config.log_level) == ("0.0.0.0", "info")
    restarted = FakeConfig()
    AppSettingsStore(tmp_path / "app_settings.json").apply_to_config(restarted)
    assert (restarted.pwa_host, restarted.log_level) == ("127.0.0.1", "debug")
    assert config.dev_mode is True
    assert config.app_repo == Path(tmp_path / "checkout").resolve()
    stored = json.loads((tmp_path / "app_settings.json").read_text())
    assert stored["pwa_host"] == "127.0.0.1" and stored["log_level"] == "debug"


@pytest.mark.parametrize(
    "change",
    [
        {"pwa_host": "not a host!"},
        {"log_level": "chatty"},
        {"app_repo": "relative/checkout"},
        {"dev_mode": "yes"},
    ],
)
def test_server_settings_reject_bad_values(tmp_path, change):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    with pytest.raises(ValueError):
        store.update(change)


def test_the_import_marker_cannot_be_patched(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    store.update({"legacy_env_imported": True})
    assert store.settings.legacy_env_imported is False


def test_legacy_env_values_are_imported_once(tmp_path):
    path = tmp_path / "app_settings.json"
    store = AppSettingsStore(path)

    imported = store.import_legacy_env({
        "PWA_HOST": "127.0.0.1",
        "CIAO_LOG_LEVEL": "10",
        "CIAO_DEV_MODE": "true",
        "CIAO_APP_REPO": "/src/ciaobot",
    })

    assert imported == ["PWA_HOST", "CIAO_LOG_LEVEL", "CIAO_DEV_MODE", "CIAO_APP_REPO"]
    s = AppSettingsStore(path).settings
    assert (s.pwa_host, s.log_level, s.dev_mode, s.app_repo) == (
        "127.0.0.1", "debug", True, "/src/ciaobot",
    )
    assert s.legacy_env_imported is True

    # A value cleared in Settings stays cleared while `.env` still says it.
    again = AppSettingsStore(path)
    again.update({"pwa_host": "", "dev_mode": False})
    assert again.import_legacy_env({"PWA_HOST": "127.0.0.1", "CIAO_DEV_MODE": "true"}) == []
    assert AppSettingsStore(path).settings.pwa_host == ""
    assert AppSettingsStore(path).settings.dev_mode is False


def test_the_import_runs_once_even_when_there_is_nothing_to_import(tmp_path):
    path = tmp_path / "app_settings.json"
    assert AppSettingsStore(path).import_legacy_env({}) == []
    # Added to `.env` after the import ran: no longer read, never imported.
    assert AppSettingsStore(path).import_legacy_env({"PWA_HOST": "127.0.0.1"}) == []
    assert AppSettingsStore(path).settings.pwa_host == ""


def test_unusable_legacy_values_are_skipped(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    assert store.import_legacy_env({"PWA_HOST": "bad host", "CIAO_LOG_LEVEL": "chatty"}) == []
    assert store.settings.pwa_host == "" and store.settings.log_level == ""


def test_read_app_settings_writes_nothing(tmp_path):
    from ciao.app_settings import read_app_settings

    path = tmp_path / "app_settings.json"
    path.write_text(json.dumps({"insights_model": "opencode:x", "pwa_host": "127.0.0.1"}))
    before = path.read_text()

    settings = read_app_settings(path)

    assert settings.pwa_host == "127.0.0.1"
    assert path.read_text() == before
    assert read_app_settings(tmp_path / "missing.json") == AppSettings()
