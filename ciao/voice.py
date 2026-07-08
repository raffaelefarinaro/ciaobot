"""Voice helpers: transcription (hear) and speech synthesis (speak).

Two engines each, selected independently:

* Hear **cloud** — OpenAI ``gpt-4o-mini-transcribe`` (needs ``OPENAI_API_KEY``).
* Hear **local** — `mlx-whisper <https://pypi.org/project/mlx-whisper/>`_ on
  Apple Silicon, free and offline. Optional dependency
  (``pip install 'ciao[voice-local]'``).
* Speak **cloud** — OpenAI ``gpt-4o-mini-tts`` (same ``OPENAI_API_KEY``).
* Speak **local** — `kokoro-onnx <https://pypi.org/project/kokoro-onnx/>`_,
  the 82M-parameter open-weight Kokoro model on ONNX Runtime, free and
  offline. Optional dependency (``pip install 'ciao[tts-local]'``); the
  first playback downloads the model files (~340 MB) into the cache dir.

Engine selection lives in ``CiaoConfig.transcription_engine`` /
``CiaoConfig.tts_engine`` (env defaults ``CIAO_TRANSCRIPTION_ENGINE`` /
``CIAO_TTS_ENGINE``, runtime-overridable from the PWA Settings → Models tab).
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import logging
import re
import wave
from pathlib import Path

from openai import AsyncOpenAI

from ciao.config import BridgeConfig

logger = logging.getLogger(__name__)


def mlx_whisper_available() -> bool:
    """True when the optional ``mlx_whisper`` package is importable."""
    return importlib.util.find_spec("mlx_whisper") is not None


def kokoro_available() -> bool:
    """True when the optional ``kokoro_onnx`` package is importable."""
    return importlib.util.find_spec("kokoro_onnx") is not None


class VoiceTranscriber:
    """OpenAI-backed voice transcription."""

    def __init__(self, config: BridgeConfig) -> None:
        if not config.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required for voice transcription")
        self._client = AsyncOpenAI(api_key=config.openai_api_key)

    async def transcribe(self, path: Path) -> str:
        """Transcribe one saved audio file."""
        with path.open("rb") as handle:
            response = await self._client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                file=handle,
            )
        text = getattr(response, "text", "").strip()
        if not text:
            raise ValueError("Voice transcription returned empty text")
        return text


class MlxWhisperTranscriber:
    """Local mlx-whisper transcription (Apple Silicon, free, offline).

    ``mlx_whisper.transcribe`` is synchronous and saturates the GPU for a
    few seconds, so it runs in a worker thread. The first call per model
    downloads the checkpoint from Hugging Face into the local cache.
    """

    def __init__(self, model: str) -> None:
        if not mlx_whisper_available():
            raise ValueError(
                "mlx-whisper is not installed; "
                "run: pip install 'ciao[voice-local]'"
            )
        self._model = model

    async def transcribe(self, path: Path) -> str:
        import mlx_whisper

        def _run() -> str:
            result = mlx_whisper.transcribe(
                str(path), path_or_hf_repo=self._model
            )
            return (result.get("text") or "").strip()

        text = await asyncio.to_thread(_run)
        if not text:
            raise ValueError("Voice transcription returned empty text")
        return text


# ── Speech synthesis (speak) ─────────────────────────────────────────────

# OpenAI caps speech input at 4096 chars; Kokoro gets the same budget so
# both engines speak the same excerpt of a long message.
MAX_SPEECH_CHARS = 4096

_KOKORO_RELEASE = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
)
KOKORO_MODEL_URL = f"{_KOKORO_RELEASE}/kokoro-v1.0.onnx"
KOKORO_VOICES_URL = f"{_KOKORO_RELEASE}/voices-v1.0.bin"

# Kokoro voice ids encode the language in their first letter
# (``af_heart`` → American English, ``im_nicola`` → Italian, …).
_KOKORO_LANGS = {
    "a": "en-us",
    "b": "en-gb",
    "e": "es",
    "f": "fr-fr",
    "h": "hi",
    "i": "it",
    "j": "ja",
    "p": "pt-br",
    "z": "cmn",
}


def speech_text(markdown: str) -> str:
    """Reduce assistant markdown to something worth reading aloud.

    Drops code blocks, tables, and formatting markers; keeps link labels.
    Truncates to ``MAX_SPEECH_CHARS`` at a sentence-ish boundary.
    """
    text = re.sub(r"```.*?```", " Code omitted. ", markdown, flags=re.DOTALL)
    text = re.sub(r"^\s*\|.*\|\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s{0,3}>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_~]{1,3}(\S(?:.*?\S)?)[*_~]{1,3}", r"\1", text)
    text = re.sub(r"^\s*([-*_]\s*){3,}$", "", text, flags=re.MULTILINE)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    if len(text) > MAX_SPEECH_CHARS:
        cut = text[:MAX_SPEECH_CHARS]
        boundary = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("\n"))
        if boundary > MAX_SPEECH_CHARS // 2:
            cut = cut[: boundary + 1]
        text = cut.strip()
    return text


class OpenAISpeaker:
    """OpenAI-backed speech synthesis (``gpt-4o-mini-tts``)."""

    mime_type = "audio/mpeg"

    def __init__(self, config: BridgeConfig) -> None:
        if not config.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required for speech synthesis")
        self._client = AsyncOpenAI(api_key=config.openai_api_key)
        self._voice = config.tts_cloud_voice

    async def speak(self, text: str) -> bytes:
        """Synthesize one utterance; returns MP3 bytes."""
        response = await self._client.audio.speech.create(
            model="gpt-4o-mini-tts",
            voice=self._voice,
            input=text,
            response_format="mp3",
        )
        data = response.content
        if not data:
            raise ValueError("Speech synthesis returned no audio")
        return data


def kokoro_cache_dir() -> Path:
    """Directory holding the downloaded Kokoro model files."""
    return Path.home() / ".cache" / "ciaobot" / "kokoro"


def _download_kokoro_file(url: str, target: Path) -> None:
    """Stream one model file to disk; atomic via a .part rename."""
    import httpx

    part = target.with_suffix(target.suffix + ".part")
    logger.info("Downloading Kokoro model file %s", url)
    with httpx.stream(
        "GET", url, follow_redirects=True, timeout=httpx.Timeout(600, connect=30)
    ) as response:
        response.raise_for_status()
        with part.open("wb") as handle:
            for chunk in response.iter_bytes(1 << 20):
                handle.write(chunk)
    part.replace(target)


def ensure_kokoro_models() -> tuple[Path, Path]:
    """Download the Kokoro model + voices files if missing; returns paths."""
    cache = kokoro_cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    model = cache / "kokoro-v1.0.onnx"
    voices = cache / "voices-v1.0.bin"
    if not model.exists():
        _download_kokoro_file(KOKORO_MODEL_URL, model)
    if not voices.exists():
        _download_kokoro_file(KOKORO_VOICES_URL, voices)
    return model, voices


# Loaded Kokoro instance, kept across requests: constructing it maps the
# ~310 MB ONNX graph, too slow to redo per utterance.
_kokoro_instance = None


class KokoroSpeaker:
    """Local Kokoro TTS (82M open-weight model on ONNX Runtime, free, offline).

    ``Kokoro.create`` is synchronous and CPU-heavy for a few seconds, so it
    runs in a worker thread. The first call downloads the model files
    (~340 MB) from the kokoro-onnx GitHub release into the cache dir.
    """

    mime_type = "audio/wav"

    def __init__(self, voice: str) -> None:
        if not kokoro_available():
            raise ValueError(
                "kokoro-onnx is not installed; "
                "run: pip install 'ciao[tts-local]'"
            )
        self._voice = voice
        self._lang = _KOKORO_LANGS.get(voice[:1], "en-us")

    async def speak(self, text: str) -> bytes:
        """Synthesize one utterance; returns WAV bytes."""

        def _run() -> bytes:
            global _kokoro_instance
            import numpy as np
            from kokoro_onnx import Kokoro

            if _kokoro_instance is None:
                model, voices = ensure_kokoro_models()
                _kokoro_instance = Kokoro(str(model), str(voices))
            samples, sample_rate = _kokoro_instance.create(
                text, voice=self._voice, speed=1.0, lang=self._lang
            )
            pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(sample_rate)
                wav.writeframes(pcm.tobytes())
            return buffer.getvalue()

        data = await asyncio.to_thread(_run)
        if not data:
            raise ValueError("Speech synthesis returned no audio")
        return data
