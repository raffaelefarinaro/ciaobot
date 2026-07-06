"""Voice transcription helpers.

Two engines:

* **cloud** — OpenAI ``gpt-4o-mini-transcribe`` (needs ``OPENAI_API_KEY``).
* **local** — `mlx-whisper <https://pypi.org/project/mlx-whisper/>`_ on
  Apple Silicon, free and offline. Optional dependency
  (``pip install 'ciao[voice-local]'``).

Engine selection lives in ``CiaoConfig.transcription_engine`` (env default
``CIAO_TRANSCRIPTION_ENGINE``, runtime-overridable from the PWA Settings →
Models tab).
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from pathlib import Path

from openai import AsyncOpenAI

from ciao.config import BridgeConfig

logger = logging.getLogger(__name__)


def mlx_whisper_available() -> bool:
    """True when the optional ``mlx_whisper`` package is importable."""
    return importlib.util.find_spec("mlx_whisper") is not None


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
