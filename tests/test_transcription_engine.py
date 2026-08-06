"""Transcription engine config and the Apple on-device dictation path."""

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
    assert config.transcription_locale == "en-US"
    assert config.transcription_model == "gpt-transcribe"


def test_engine_env_selection(tmp_path):
    config = _config({"CIAO_TRANSCRIPTION_ENGINE": "local"}, tmp_path)
    assert config.transcription_engine == "local"


def test_engine_env_garbage_falls_back_to_cloud(tmp_path):
    config = _config({"CIAO_TRANSCRIPTION_ENGINE": "telepathy"}, tmp_path)
    assert config.transcription_engine == "cloud"


def test_locale_env_override(tmp_path):
    config = _config({"CIAO_TRANSCRIPTION_LOCALE": "it-IT"}, tmp_path)
    assert config.transcription_locale == "it-IT"


def test_cloud_model_env_override(tmp_path):
    config = _config(
        {"CIAO_TRANSCRIPTION_MODEL": "gpt-4o-mini-transcribe"}, tmp_path
    )
    assert config.transcription_model == "gpt-4o-mini-transcribe"


@pytest.mark.asyncio
async def test_voice_transcriber_uses_config_model(tmp_path, monkeypatch):
    """Regression: VoiceTranscriber must keep config so model isn't AttributeError."""
    from types import SimpleNamespace
    from pathlib import Path

    captured: dict = {}

    class FakeResponse:
        text = "hello from cloud"

    class FakeTranscriptions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return FakeResponse()

    class FakeAudio:
        transcriptions = FakeTranscriptions()

    class FakeClient:
        audio = FakeAudio()

        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(voice, "AsyncOpenAI", FakeClient)
    config = SimpleNamespace(
        openai_api_key="sk-test",
        transcription_model="gpt-transcribe",
    )
    transcriber = voice.VoiceTranscriber(config)
    audio_path = Path(tmp_path) / "clip.webm"
    audio_path.write_bytes(b"fake-audio")
    text = await transcriber.transcribe(audio_path)
    assert text == "hello from cloud"
    assert captured["model"] == "gpt-transcribe"
    assert captured["response_format"] == "json"


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
    binary = tmp_path / "ciaobot-speech"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setenv("CIAO_SPEECH_SIDECAR", str(binary))
    assert voice.sidecar_path() == binary
    monkeypatch.setenv("CIAO_SPEECH_SIDECAR", str(tmp_path / "missing"))
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


async def test_transcribe_voice_local_not_installed(tmp_path, monkeypatch):
    from pathlib import Path
    from ciao.web.project_chats import ProjectChatManager
    from ciao.sessions import StateStore
    from ciao.transcripts import TranscriptStore

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
        transcription_engine="local",
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    pcm = ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )

    audio_path = tmp_path / "test.webm"
    audio_path.touch()

    with pytest.raises(ValueError) as exc_info:
        await pcm.transcribe_voice(audio_path)
    assert "requires macOS 26 or newer" in str(exc_info.value)
    assert "Settings → Models" in str(exc_info.value)


async def test_transcribe_voice_local_fails(tmp_path, monkeypatch):
    from pathlib import Path
    from ciao.web.project_chats import ProjectChatManager
    from ciao.sessions import StateStore
    from ciao.transcripts import TranscriptStore

    monkeypatch.setattr(voice, "apple_dictation_available", lambda: True)

    class FailingTranscriber:
        def __init__(self, locale):
            pass

        async def transcribe(self, path):
            raise RuntimeError("the recording could not be read")

    monkeypatch.setattr(voice, "AppleDictationTranscriber", FailingTranscriber)

    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        transcription_engine="local",
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    pcm = ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )

    audio_path = tmp_path / "test.webm"
    audio_path.touch()

    with pytest.raises(ValueError) as exc_info:
        await pcm.transcribe_voice(audio_path)
    assert "On-device dictation failed" in str(exc_info.value)
    assert "the recording could not be read" in str(exc_info.value)
    assert "Settings → Models" in str(exc_info.value)


async def test_transcribe_voice_cloud_model_and_cost(tmp_path, monkeypatch):
    from pathlib import Path
    from ciao.web.project_chats import ProjectChatManager
    from ciao.sessions import StateStore
    from ciao.transcripts import TranscriptStore

    monkeypatch.setattr(voice, "apple_dictation_available", lambda: False)

    class FakeTranscriber:
        def __init__(self, config):
            self._config = config

        async def transcribe(self, path):
            return "transcribed text"

    monkeypatch.setattr(voice, "VoiceTranscriber", FakeTranscriber)

    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        transcription_engine="cloud",
        transcription_model="gpt-transcribe",
        openai_api_key="sk-test",
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    pcm = ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )

    audio_path = tmp_path / "test.webm"
    # 160000 bytes ≈ 10 s at the 16000 B/s OGG duration heuristic.
    audio_path.write_bytes(b"x" * 160000)

    text, cost = await pcm.transcribe_voice(audio_path)
    assert text == "transcribed text"
    # gpt-transcribe at $0.0045/min: 10 s → 10/60 * 0.0045.
    assert cost == pytest.approx(10 / 60 * 0.0045)
