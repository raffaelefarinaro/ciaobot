"""Tests for the runtime app-settings store (Settings → Models tab)."""

from __future__ import annotations

import json

import pytest

from ciao.app_settings import AppSettings, AppSettingsStore
from ciao.providers.codex import CodexSettings


class FakeConfig:
    """Just the fields apply_to_config touches."""

    def __init__(self) -> None:
        self.title_model_override = ""
        self.insights_model_override = ""
        self.insights_model = "sonnet"
        # Beta feature, off by default (the env default here is False).
        self.apple_intelligence_enabled = False

        self.transcription_locale = "en-US"
        self.tts_local_voice = "af_heart"
        self.critique_models = ""
        # Per-provider default modes; no env-backed default.
        self.provider_default_modes: dict[str, str] = {}
        # Per-provider default models / thinking / routine models; no
        # env-backed defaults.
        self.provider_default_models: dict[str, str] = {}
        self.provider_default_thinking: dict[str, str] = {}
        self.provider_title_models: dict[str, str] = {}
        self.provider_insights_models: dict[str, str] = {}
        # No env-backed defaults: empty = provider's own catalog default.
        self.codex = CodexSettings()


def test_load_missing_file_gives_defaults(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    assert store.settings == AppSettings()


def test_load_ignores_unknown_keys_and_non_strings(tmp_path):
    path = tmp_path / "app_settings.json"
    path.write_text(
        json.dumps(
            {
                "title_model": " gemma4:12b-it-qat ",
                "bogus": "x",
                "insights_model": 42,
            }
        )
    )
    store = AppSettingsStore(path)
    assert store.settings.title_model == "gemma4:12b-it-qat"
    assert store.settings.insights_model == ""


def test_load_corrupt_file_gives_defaults(tmp_path):
    path = tmp_path / "app_settings.json"
    path.write_text("{not json")
    assert AppSettingsStore(path).settings == AppSettings()


def test_update_persists_and_roundtrips(tmp_path):
    path = tmp_path / "app_settings.json"
    store = AppSettingsStore(path)
    store.update({"insights_model": "gemma4:12b-it-qat", "ignored": "x"})
    assert json.loads(path.read_text()) == {"insights_model": "gemma4:12b-it-qat"}
    # Fresh instance sees the persisted value.
    assert AppSettingsStore(path).settings.insights_model == "gemma4:12b-it-qat"


def test_update_rejects_a_non_string_value(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    # Engine-value validation went with the cloud engines; type checking is
    # what is left.
    with pytest.raises(ValueError):
        store.update({"title_model": 3})


def test_apple_intelligence_defaults_to_unset_and_off(tmp_path):
    # No override persisted and no env default: applying leaves it off.
    store = AppSettingsStore(tmp_path / "app_settings.json")
    config = FakeConfig()
    store.apply_to_config(config)
    assert store.settings.apple_intelligence_enabled is None
    assert config.apple_intelligence_enabled is False
    # And nothing was written just by applying.
    assert not (tmp_path / "app_settings.json").exists()


def test_apple_intelligence_toggle_persists_and_roundtrips(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    store.update({"apple_intelligence_enabled": True})
    assert json.loads((tmp_path / "app_settings.json").read_text()) == {
        "apple_intelligence_enabled": True
    }
    assert AppSettingsStore(tmp_path / "app_settings.json").settings.apple_intelligence_enabled is True

    store.update({"apple_intelligence_enabled": False})
    assert AppSettingsStore(tmp_path / "app_settings.json").settings.apple_intelligence_enabled is False


def test_apple_intelligence_update_rejects_a_non_boolean(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    with pytest.raises(ValueError):
        store.update({"apple_intelligence_enabled": "yes"})


def test_apple_intelligence_apply_respects_explicit_toggle(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    config = FakeConfig()

    store.update({"apple_intelligence_enabled": True})
    store.apply_to_config(config)
    assert config.apple_intelligence_enabled is True

    # Explicit False wins over the (False) env default — and also lets an
    # operator force it off when the env default is on.
    store.update({"apple_intelligence_enabled": False})
    store.apply_to_config(config)
    assert config.apple_intelligence_enabled is False


def test_apple_intelligence_apply_uses_env_default_when_not_toggled(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    config = FakeConfig()
    config.apple_intelligence_enabled = True  # e.g. CIAO_APPLE_INTELLIGENCE=1

    store.apply_to_config(config)
    assert config.apple_intelligence_enabled is True


def test_apply_overlays_and_clear_restores_defaults(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    config = FakeConfig()

    store.update({"insights_model": "gemma4:12b-it-qat", "transcription_locale": "it-IT"})
    store.apply_to_config(config)
    assert config.insights_model_override == "gemma4:12b-it-qat"
    assert config.transcription_locale == "it-IT"

    # Clearing restores the env-backed default captured on first apply.
    store.update({"insights_model": "", "transcription_locale": ""})
    store.apply_to_config(config)
    assert config.insights_model_override == ""
    assert config.transcription_locale == "en-US"


def test_tts_overrides_apply_and_clear(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    config = FakeConfig()

    store.update({"tts_local_voice": "im_nicola"})
    store.apply_to_config(config)
    assert config.tts_local_voice == "im_nicola"

    store.update({"tts_local_voice": ""})
    store.apply_to_config(config)
    assert config.tts_local_voice == "af_heart"


def test_title_override_applies(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    config = FakeConfig()
    store.update({"title_model": "ministral-3:3b"})
    store.apply_to_config(config)
    assert config.title_model_override == "ministral-3:3b"
    store.update({"title_model": ""})
    store.apply_to_config(config)
    assert config.title_model_override == ""


def test_critique_models_override_applies(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    config = FakeConfig()
    store.update({"critique_models": "opus,codex:fable"})
    store.apply_to_config(config)
    assert config.critique_models == "opus,codex:fable"
    store.update({"critique_models": ""})
    store.apply_to_config(config)
    assert config.critique_models == ""


def test_codex_default_model_applies_and_clears(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    config = FakeConfig()

    store.update({"provider_default_models": {"codex": "gpt-5.6-sol"}})
    store.apply_to_config(config)
    assert config.codex.default_model == "gpt-5.6-sol"

    # Clearing restores the automatic (empty) default.
    store.update({"provider_default_models": {"codex": ""}})
    store.apply_to_config(config)
    assert config.codex == CodexSettings()


def test_provider_routine_models_persist_and_apply(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    config = FakeConfig()

    store.update({
        "provider_title_models": {"codex": "gpt-5.6-luna"},
        "provider_insights_models": {"opencode": "anthropic/claude-sonnet-4-6"},
        "provider_default_thinking": {"claude": "high"},
    })
    store.apply_to_config(config)
    assert config.provider_title_models == {"codex": "gpt-5.6-luna"}
    assert config.provider_insights_models == {"opencode": "anthropic/claude-sonnet-4-6"}
    assert config.provider_default_thinking == {"claude": "high"}
    path = tmp_path / "app_settings.json"
    assert json.loads(path.read_text()) == {
        "provider_title_models": {"codex": "gpt-5.6-luna"},
        "provider_insights_models": {"opencode": "anthropic/claude-sonnet-4-6"},
        "provider_default_thinking": {"claude": "high"},
    }
    # Fresh instance sees the persisted maps.
    fresh = AppSettingsStore(path)
    assert fresh.settings.provider_title_models == {"codex": "gpt-5.6-luna"}
    assert fresh.settings.provider_insights_models == {"opencode": "anthropic/claude-sonnet-4-6"}
    assert fresh.settings.provider_default_thinking == {"claude": "high"}


def test_provider_maps_reject_non_objects(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    with pytest.raises(ValueError, match="must be an object"):
        store.update({"provider_default_models": "gpt-5.6-sol"})
    with pytest.raises(ValueError, match="must be an object"):
        store.update({"provider_title_models": "gpt-5.6-luna"})


def test_provider_maps_load_ignores_junk(tmp_path):
    path = tmp_path / "app_settings.json"
    path.write_text(
        json.dumps(
            {
                "provider_default_models": {
                    "codex": "gpt-5.6-sol",
                    "bogus": "auto",
                },
                "provider_title_models": {
                    "opencode": "anthropic/claude-sonnet-4-6",
                    "codex": 42,
                },
            }
        )
    )
    store = AppSettingsStore(path)
    assert store.settings.provider_default_models == {"codex": "gpt-5.6-sol"}
    assert store.settings.provider_title_models == {"opencode": "anthropic/claude-sonnet-4-6"}


def test_provider_default_modes_persist_and_apply(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    config = FakeConfig()

    store.update({"provider_default_modes": {"opencode": "bypass", "codex": "plan"}})
    store.apply_to_config(config)
    assert config.provider_default_modes == {"opencode": "bypass", "codex": "plan"}
    path = tmp_path / "app_settings.json"
    assert json.loads(path.read_text()) == {
        "provider_default_modes": {"opencode": "bypass", "codex": "plan"}
    }
    # Fresh instance sees the persisted map.
    assert AppSettingsStore(path).settings.provider_default_modes == {
        "opencode": "bypass",
        "codex": "plan",
    }

    # Clearing a provider's entry (empty string) removes the override.
    store.update({"provider_default_modes": {"opencode": "bypass", "codex": ""}})
    store.apply_to_config(config)
    assert config.provider_default_modes == {"opencode": "bypass"}


def test_provider_default_modes_reject_bad_mode_and_non_objects(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    with pytest.raises(ValueError, match="auto, bypass, normal, plan"):
        store.update({"provider_default_modes": {"opencode": "nope"}})
    with pytest.raises(ValueError, match="must be an object"):
        store.update({"provider_default_modes": "bypass"})


def test_provider_default_modes_load_ignores_junk(tmp_path):
    path = tmp_path / "app_settings.json"
    path.write_text(
        json.dumps(
            {
                "provider_default_modes": {
                    "opencode": "bypass",
                    "codex": "sideways",
                    "bogus": "auto",
                }
            }
        )
    )
    store = AppSettingsStore(path)
    assert store.settings.provider_default_modes == {"opencode": "bypass"}


def test_default_mode_for_provider_builtin_defaults(tmp_path):
    from ciao.config import CiaoConfig

    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
    )
    # New opencode chats require approval by default; bypass is explicit.
    assert config.default_mode_for_provider("opencode") == "normal"
    assert config.default_mode_for_provider("codex") == "auto"
    assert config.default_mode_for_provider("claude") == "auto"

    # An operator pin wins over the built-in default.
    config.provider_default_modes = {"opencode": "auto", "claude": "plan"}
    assert config.default_mode_for_provider("opencode") == "auto"
    assert config.default_mode_for_provider("codex") == "auto"
    assert config.default_mode_for_provider("claude") == "plan"
