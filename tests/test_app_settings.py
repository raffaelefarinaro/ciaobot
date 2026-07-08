"""Tests for the runtime app-settings store (Settings → Models tab)."""

from __future__ import annotations

import json

import pytest

from ciao.app_settings import AppSettings, AppSettingsStore
from ciao.providers.ollama import OllamaSettings
from ciao.providers.openrouter import OpenRouterSettings


class FakeConfig:
    """Just the fields apply_to_config touches."""

    def __init__(self) -> None:
        self.title_model_override = ""
        self.insights_model = "deepseek-v4-flash:cloud"

        self.transcription_engine = "cloud"
        self.transcription_local_model = "mlx-community/whisper-large-v3-turbo"
        self.tts_engine = "cloud"
        self.tts_cloud_voice = "nova"
        self.tts_local_voice = "af_heart"
        self.critique_models = ""
        self.ollama = OllamaSettings(
            haiku_model="deepseek-v4-flash:cloud",
            sonnet_model="kimi-k2.7-code:cloud",
            opus_model="minimax-m3:cloud",
        )
        self.openrouter = OpenRouterSettings(
            api_key="sk-or",
            haiku_model="anthropic/claude-haiku-4.5",
            sonnet_model="anthropic/claude-sonnet-4.5",
            opus_model="anthropic/claude-opus-4.8",
        )


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
        store.update({"tts_engine": "telepathy"})
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


def test_tts_overrides_apply_and_clear(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    config = FakeConfig()

    store.update({"tts_engine": "local", "tts_local_voice": "im_nicola"})
    store.apply_to_config(config)
    assert config.tts_engine == "local"
    assert config.tts_local_voice == "im_nicola"

    store.update({"tts_engine": "", "tts_local_voice": ""})
    store.apply_to_config(config)
    assert config.tts_engine == "cloud"
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
    store.update({"critique_models": "openrouter/anthropic/claude-3.7-sonnet"})
    store.apply_to_config(config)
    assert config.critique_models == "openrouter/anthropic/claude-3.7-sonnet"
    store.update({"critique_models": ""})
    store.apply_to_config(config)
    assert config.critique_models == ""


def test_tier_model_overrides_apply_and_clear(tmp_path):
    store = AppSettingsStore(tmp_path / "app_settings.json")
    config = FakeConfig()

    store.update(
        {
            "ollama_sonnet_model": "qwen3:8b",
            "openrouter_opus_model": "anthropic/claude-opus-4.9",
        }
    )
    store.apply_to_config(config)
    assert config.ollama.sonnet_model == "qwen3:8b"
    assert config.openrouter.opus_model == "anthropic/claude-opus-4.9"

    store.update({"ollama_sonnet_model": "", "openrouter_opus_model": ""})
    store.apply_to_config(config)
    assert config.ollama.sonnet_model == "kimi-k2.7-code:cloud"
    assert config.openrouter.opus_model == "anthropic/claude-opus-4.8"
