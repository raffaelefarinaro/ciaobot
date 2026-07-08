"""Tests for the speech-synthesis (speak) engine config and dispatch."""

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


def _pcm(tmp_path, **config_overrides):
    from ciao.sessions import StateStore
    from ciao.transcripts import TranscriptStore
    from ciao.web.project_chats import ProjectChatManager

    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        **config_overrides,
    )
    state = StateStore(config.state_path, tmp_path, config.media_root)
    transcripts = TranscriptStore(runtime, tmp_path / "transcripts")
    return ProjectChatManager(
        config,
        state_store=state,
        transcript_store=transcripts,
        path=runtime / "web_projects.json",
    )


def test_tts_engine_defaults_to_cloud(tmp_path):
    config = _config(tmp_path=tmp_path)
    assert config.tts_engine == "cloud"
    assert config.tts_cloud_voice == "nova"
    assert config.tts_local_voice == "af_heart"


def test_tts_engine_env_selection(tmp_path):
    config = _config({"CIAO_TTS_ENGINE": "local"}, tmp_path)
    assert config.tts_engine == "local"


def test_tts_engine_env_garbage_falls_back_to_cloud(tmp_path):
    config = _config({"CIAO_TTS_ENGINE": "telepathy"}, tmp_path)
    assert config.tts_engine == "cloud"


def test_tts_voice_env_overrides(tmp_path):
    config = _config(
        {"CIAO_TTS_CLOUD_VOICE": "alloy", "CIAO_TTS_LOCAL_VOICE": "im_nicola"},
        tmp_path,
    )
    assert config.tts_cloud_voice == "alloy"
    assert config.tts_local_voice == "im_nicola"


def test_kokoro_speaker_requires_package(monkeypatch):
    monkeypatch.setattr(voice, "kokoro_available", lambda: False)
    with pytest.raises(ValueError, match="kokoro-onnx is not installed"):
        voice.KokoroSpeaker("af_heart")


def test_kokoro_available_is_bool():
    assert isinstance(voice.kokoro_available(), bool)


def test_speech_text_strips_markdown():
    text = voice.speech_text(
        "# Title\n\n"
        "Hello **world**, see [the docs](https://example.com) and `inline`.\n\n"
        "```python\nprint('hi')\n```\n\n"
        "| a | b |\n| - | - |\n| 1 | 2 |\n\n"
        "- item one\n"
        "> quoted\n"
    )
    assert "**" not in text
    assert "#" not in text
    assert "https://example.com" not in text
    assert "the docs" in text
    assert "print" not in text
    assert "Code omitted" in text
    assert "|" not in text
    assert "item one" in text
    assert "quoted" in text


def test_speech_text_truncates_long_input():
    text = voice.speech_text("A sentence. " * 2000)
    assert len(text) <= voice.MAX_SPEECH_CHARS
    assert text.endswith(".")


def test_kokoro_lang_follows_voice_prefix(monkeypatch):
    monkeypatch.setattr(voice, "kokoro_available", lambda: True)
    assert voice.KokoroSpeaker("im_nicola")._lang == "it"
    assert voice.KokoroSpeaker("af_heart")._lang == "en-us"
    assert voice.KokoroSpeaker("xx_unknown")._lang == "en-us"


async def test_synthesize_speech_local_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(voice, "kokoro_available", lambda: False)
    pcm = _pcm(tmp_path, tts_engine="local")

    with pytest.raises(ValueError) as exc_info:
        await pcm.synthesize_speech("Hello there")
    assert "kokoro-onnx is not installed" in str(exc_info.value)
    assert "Settings → Models" in str(exc_info.value)


async def test_synthesize_speech_local_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(voice, "kokoro_available", lambda: True)

    class FailingSpeaker:
        mime_type = "audio/wav"

        def __init__(self, voice_id):
            pass

        async def speak(self, text):
            raise RuntimeError("model download interrupted")

    monkeypatch.setattr(voice, "KokoroSpeaker", FailingSpeaker)
    pcm = _pcm(tmp_path, tts_engine="local")

    with pytest.raises(ValueError) as exc_info:
        await pcm.synthesize_speech("Hello there")
    assert "Local speech synthesis failed" in str(exc_info.value)
    assert "model download interrupted" in str(exc_info.value)
    assert "Settings → Models" in str(exc_info.value)


async def test_synthesize_speech_local_success(tmp_path, monkeypatch):
    monkeypatch.setattr(voice, "kokoro_available", lambda: True)

    class FakeSpeaker:
        mime_type = "audio/wav"

        def __init__(self, voice_id):
            assert voice_id == "af_heart"

        async def speak(self, text):
            assert "Hello" in text
            return b"RIFFfake"

    monkeypatch.setattr(voice, "KokoroSpeaker", FakeSpeaker)
    pcm = _pcm(tmp_path, tts_engine="local")

    audio, mime, cost = await pcm.synthesize_speech("Hello **there**")
    assert audio == b"RIFFfake"
    assert mime == "audio/wav"
    assert cost == 0.0


async def test_synthesize_speech_cloud_requires_key(tmp_path):
    pcm = _pcm(tmp_path, tts_engine="cloud")
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        await pcm.synthesize_speech("Hello there")


async def test_synthesize_speech_rejects_empty_text(tmp_path):
    pcm = _pcm(tmp_path, tts_engine="local")
    # An image-only message reduces to nothing speakable.
    with pytest.raises(ValueError, match="Nothing to read aloud"):
        await pcm.synthesize_speech("![screenshot](http://example.com/shot.png)")
