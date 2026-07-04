"""Tests for transcription engine config and the local mlx-whisper path."""

from __future__ import annotations

import pytest

import ciao.voice as voice
from ciao.config import CiaoConfig


def _config(env_extra: dict[str, str] | None = None, tmp_path=None) -> CiaoConfig:
    env = {
        "PWA_AUTH_TOKEN": "t",
        "CIAO_WORKSPACE": str(tmp_path),
        "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
    }
    env.update(env_extra or {})
    return CiaoConfig.from_env(env)


def test_engine_defaults_to_cloud(tmp_path):
    config = _config(tmp_path=tmp_path)
    assert config.transcription_engine == "cloud"
    assert config.transcription_local_model == "mlx-community/whisper-large-v3-turbo"


def test_engine_env_selection(tmp_path):
    config = _config({"CIAO_TRANSCRIPTION_ENGINE": "local"}, tmp_path)
    assert config.transcription_engine == "local"


def test_engine_env_garbage_falls_back_to_cloud(tmp_path):
    config = _config({"CIAO_TRANSCRIPTION_ENGINE": "telepathy"}, tmp_path)
    assert config.transcription_engine == "cloud"


def test_local_model_env_override(tmp_path):
    config = _config(
        {"CIAO_TRANSCRIPTION_LOCAL_MODEL": "mlx-community/whisper-tiny"}, tmp_path
    )
    assert config.transcription_local_model == "mlx-community/whisper-tiny"


def test_mlx_transcriber_requires_package(monkeypatch):
    monkeypatch.setattr(voice, "mlx_whisper_available", lambda: False)
    with pytest.raises(ValueError, match="mlx-whisper is not installed"):
        voice.MlxWhisperTranscriber("mlx-community/whisper-tiny")


def test_mlx_whisper_available_is_bool():
    assert isinstance(voice.mlx_whisper_available(), bool)


def test_ollama_local_env_parsing(tmp_path):
    config = _config(
        {
            "CIAO_OLLAMA_MODELS": "kimi-k2.7-code:cloud",
            "CIAO_OLLAMA_API_KEY": "sk",
            "CIAO_OLLAMA_LOCAL_MODELS": "gemma4:12b-it-qat",
            "CIAO_OLLAMA_LOCAL_URL": "http://127.0.0.1:11434",
            "CIAO_OLLAMA_LOCAL_DISCOVERY": "false",
        },
        tmp_path,
    )
    assert config.ollama.local_models == ("gemma4:12b-it-qat",)
    assert config.ollama.local_url == "http://127.0.0.1:11434"
    assert config.ollama_local_discovery is False
    # Manually pinned local models join the picker.
    assert "gemma4:12b-it-qat" in config.claude_models
