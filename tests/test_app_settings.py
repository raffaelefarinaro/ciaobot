"""Tests for the runtime app-settings store (Settings → Models tab)."""

from __future__ import annotations

import json

import pytest

from ciao.app_settings import AppSettings, AppSettingsStore


class FakeConfig:
    """Just the fields apply_to_config touches."""

    def __init__(self) -> None:
        self.title_model_override = ""
        self.insights_model = "deepseek-v4-flash:cloud"
        self.transcription_engine = "cloud"
        self.transcription_local_model = "mlx-community/whisper-large-v3-turbo"
        self.critique_models = ""


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


def test_update_rejects_bad_engine(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    with pytest.raises(ValueError):
        store.update({"transcription_engine": "telepathy"})
    with pytest.raises(ValueError):
        store.update({"title_model": 3})


def test_apply_overlays_and_clear_restores_defaults(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    config = FakeConfig()

    store.update({"insights_model": "gemma4:12b-it-qat", "transcription_engine": "local"})
    store.apply_to_config(config)
    assert config.insights_model == "gemma4:12b-it-qat"
    assert config.transcription_engine == "local"

    # Clearing restores the env-backed default captured on first apply.
    store.update({"insights_model": "", "transcription_engine": ""})
    store.apply_to_config(config)
    assert config.insights_model == "deepseek-v4-flash:cloud"
    assert config.transcription_engine == "cloud"


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
    store.update({"critique_models": "openrouter/anthropic/claude-3.7-sonnet"})
    store.apply_to_config(config)
    assert config.critique_models == "openrouter/anthropic/claude-3.7-sonnet"
    store.update({"critique_models": ""})
    store.apply_to_config(config)
    assert config.critique_models == ""
