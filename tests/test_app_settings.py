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
        # No env-backed defaults: empty = automatic catalog mapping.
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


def test_codex_tier_pins_apply_and_clear(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    config = FakeConfig()

    store.update({"codex_sonnet_model": "gpt-5.6-sol", "codex_haiku_model": "gpt-5.6-terra"})
    store.apply_to_config(config)
    assert config.codex.sonnet_model == "gpt-5.6-sol"
    assert config.codex.haiku_model == "gpt-5.6-terra"
    assert config.codex.tier_overrides() == {
        "haiku": "gpt-5.6-terra",
        "sonnet": "gpt-5.6-sol",
        "opus": "",
        "fable": "",
    }

    # Clearing a pin restores the automatic (empty) default.
    store.update({"codex_sonnet_model": "", "codex_haiku_model": ""})
    store.apply_to_config(config)
    assert config.codex == CodexSettings()
