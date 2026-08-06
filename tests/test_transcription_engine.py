"""Dictation config and the Apple on-device path (the only engine)."""

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


def test_locale_env_override(tmp_path):
    config = _config({"CIAO_TRANSCRIPTION_LOCALE": "it-IT"}, tmp_path)
    assert config.transcription_locale == "it-IT"


def test_apple_transcriber_refuses_when_dictation_is_unavailable(monkeypatch):
    monkeypatch.setattr(voice, "apple_dictation_available", lambda: False)
    monkeypatch.setattr(
        voice, "dictation_unavailable_reason", lambda: "requires macOS 26 or newer"
    )
    with pytest.raises(ValueError, match="requires macOS 26 or newer"):
        voice.AppleDictationTranscriber("en-US")


def test_availability_probes_return_bools():
    """They shell out to the sidecar, so they must never raise — Settings calls
    them on every load, including on Linux where there is no sidecar at all."""
    assert isinstance(voice.apple_dictation_available(), bool)
    assert isinstance(voice.apple_speech_available(), bool)
    assert isinstance(voice.dictation_unavailable_reason(), str)


def test_probe_is_empty_without_a_sidecar(monkeypatch):
    monkeypatch.setattr(voice, "sidecar_path", lambda: None)
    voice.reset_voice_probe_cache()
    try:
        assert voice.apple_dictation_available() is False
        assert voice.apple_speech_available() is False
        assert "Ciaobot.app" in voice.dictation_unavailable_reason() or (
            "macOS" in voice.dictation_unavailable_reason()
        )
    finally:
        voice.reset_voice_probe_cache()


def test_sidecar_path_honours_the_dev_override(tmp_path, monkeypatch):
    binary = tmp_path / "ciaobot-native"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setenv("CIAO_NATIVE_SIDECAR", str(binary))
    assert voice.sidecar_path() == binary
    monkeypatch.setenv("CIAO_NATIVE_SIDECAR", str(tmp_path / "missing"))
    assert voice.sidecar_path() is None


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
    assert "gemma4:12b-it-qat" in config.claude_models




def test_dictation_settings_have_no_engine_choice(tmp_path):
    """Cloud transcription is gone with the openai dependency, so there is one
    engine and nothing to select."""
    config = _config(tmp_path=tmp_path)
    assert config.transcription_locale == "en-US"
    assert not hasattr(config, "transcription_engine")
    assert not hasattr(config, "transcription_model")
    assert not hasattr(config, "openai_api_key")


async def test_transcribe_voice_reports_the_reason_when_unavailable(tmp_path, monkeypatch):
    from ciao.sessions import StateStore
    from ciao.transcripts import TranscriptStore
    from ciao.web.project_chats import ProjectChatManager

    monkeypatch.setattr(voice, "apple_dictation_available", lambda: False)
    monkeypatch.setattr(
        voice, "dictation_unavailable_reason", lambda: "requires macOS 26 or newer"
    )
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    pcm = ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )
    audio = tmp_path / "test.wav"
    audio.touch()

    with pytest.raises(ValueError) as exc_info:
        await pcm.transcribe_voice(audio)
    # No cloud fallback left, so the message has to name what is wrong.
    assert "requires macOS 26 or newer" in str(exc_info.value)


async def test_transcribe_voice_is_free(tmp_path, monkeypatch):
    from ciao.sessions import StateStore
    from ciao.transcripts import TranscriptStore
    from ciao.web.project_chats import ProjectChatManager

    monkeypatch.setattr(voice, "apple_dictation_available", lambda: True)

    class FakeTranscriber:
        def __init__(self, locale):
            assert locale == "en-US"

        async def transcribe(self, path):
            return "transcribed text"

    monkeypatch.setattr(voice, "AppleDictationTranscriber", FakeTranscriber)
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
    )
    pcm = ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    )
    audio = tmp_path / "test.wav"
    audio.touch()

    text, cost = await pcm.transcribe_voice(audio)
    assert (text, cost) == ("transcribed text", 0.0)
