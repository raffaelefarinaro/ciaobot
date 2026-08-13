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


def test_system_speaker_refuses_without_the_sidecar(monkeypatch):
    monkeypatch.setattr(voice, "apple_speech_available", lambda: False)
    with pytest.raises(ValueError, match="one-line installer"):
        voice.SystemSpeaker("", "en-US")


def test_system_voices_is_a_list():
    assert isinstance(voice.system_voices(), list)


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




def test_voice_settings_default_to_on_device(tmp_path):
    """One engine now, so there is nothing to select. Empty voice means "the
    best installed voice for the locale" -- naming one would pick a voice that
    need not exist on the machine."""
    config = _config(tmp_path=tmp_path)
    assert config.tts_local_voice == ""
    assert config.transcription_locale == "en-US"
    assert not hasattr(config, "tts_engine")
    assert not hasattr(config, "tts_cloud_voice")
    assert not hasattr(config, "openai_api_key")


def test_voice_env_overrides(tmp_path):
    config = _config(
        {"CIAO_TTS_LOCAL_VOICE": "com.apple.voice.x", "CIAO_TRANSCRIPTION_LOCALE": "it-IT"},
        tmp_path,
    )
    assert config.tts_local_voice == "com.apple.voice.x"
    assert config.transcription_locale == "it-IT"


async def test_synthesize_speech_reports_the_reason_when_unavailable(tmp_path, monkeypatch):
    """No cloud fallback left, so the failure has to name what is wrong."""
    monkeypatch.setattr(voice, "apple_speech_available", lambda: False)
    monkeypatch.setattr("sys.platform", "darwin")
    pcm = _pcm(tmp_path)

    with pytest.raises(ValueError) as exc_info:
        await pcm.synthesize_speech("Hello there")
    assert "one-line installer" in str(exc_info.value)


async def test_synthesize_speech_is_free_and_uses_the_configured_voice(tmp_path, monkeypatch):
    monkeypatch.setattr(voice, "apple_speech_available", lambda: True)

    class FakeSpeaker:
        mime_type = "audio/wav"

        def __init__(self, voice_id, locale):
            assert voice_id == ""
            assert locale == "en-US"

        async def speak(self, text):
            assert "Hello" in text
            return b"RIFFfake"

    monkeypatch.setattr(voice, "SystemSpeaker", FakeSpeaker)
    pcm = _pcm(tmp_path)

    audio, mime, cost = await pcm.synthesize_speech("Hello **there**")
    assert (audio, mime, cost) == (b"RIFFfake", "audio/wav", 0.0)


async def test_synthesize_speech_rejects_empty_text(tmp_path):
    """Checked before availability, so a blank message says the useful thing
    even on a machine where the synthesizer is not usable."""
    pcm = _pcm(tmp_path)
    with pytest.raises(ValueError, match="Nothing to read aloud"):
        await pcm.synthesize_speech("   \n\n   ")
