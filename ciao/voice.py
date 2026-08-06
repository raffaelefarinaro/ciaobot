"""Voice helpers: transcription (hear) and speech synthesis (speak).

Two engines each, selected independently:

* Hear **cloud** — OpenAI ``gpt-transcribe`` (needs ``OPENAI_API_KEY``;
  model overridable via ``CIAO_TRANSCRIPTION_MODEL``).
* Hear **local** — Apple's on-device dictation (macOS 26+), through the
  ``ciaobot-native`` sidecar bundled in ``Ciaobot.app``.
* Speak **cloud** — OpenAI ``gpt-4o-mini-tts`` (same ``OPENAI_API_KEY``).
* Speak **local** — ``AVSpeechSynthesizer``, through the same sidecar.

The local engines used to be mlx-whisper and kokoro-onnx: optional pip installs
that pulled model weights on first use, 340 MB in Kokoro's case. Both are gone.
Apple ships equivalents inside the OS, so local voice now costs no download and
no dependency — see ``desktop/native/main.swift`` for why a Swift sidecar is
required rather than calling those frameworks from Python.

The trade is reach, not just size. Dictation needs macOS 26 or newer, and both
local engines need ``Ciaobot.app`` installed, so on Linux, Windows, and older
macOS there is no local option at all and only the cloud engines appear. The
availability probes below are what Settings uses to hide what cannot work.

Engine selection lives in ``CiaoConfig.transcription_engine`` /
``CiaoConfig.tts_engine`` (env defaults ``CIAO_TRANSCRIPTION_ENGINE`` /
``CIAO_TTS_ENGINE``, runtime-overridable from the PWA Settings → Models tab).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from openai import AsyncOpenAI

from ciao import native_sidecar
from ciao.config import BridgeConfig

logger = logging.getLogger(__name__)

# The sidecar plumbing (locating the binary, probing, running a subcommand)
# is shared with chat titles, so it lives in ciao/native_sidecar.py.
SIDECAR_EXIT_UNSUPPORTED_OS = native_sidecar.EXIT_UNSUPPORTED_OS
SIDECAR_EXIT_LOCALE_UNAVAILABLE = native_sidecar.EXIT_LOCALE_UNAVAILABLE
SIDECAR_EXIT_AUDIO_UNREADABLE = native_sidecar.EXIT_AUDIO_UNREADABLE
SIDECAR_EXIT_EMPTY_RESULT = native_sidecar.EXIT_EMPTY_RESULT

sidecar_path = native_sidecar.sidecar_path


def reset_voice_probe_cache() -> None:
    """Forget the cached sidecar probe, for tests and after installing the app."""
    native_sidecar.reset_probe_cache()


def apple_dictation_available() -> bool:
    """True when on-device dictation can actually run here.

    False on non-macOS, without the app bundle, before macOS 26, and when the
    user has no dictation language installed — each of which the sidecar
    reports rather than this module guessing.
    """
    return bool(native_sidecar.section("hear").get("available"))


def apple_speech_available() -> bool:
    """True when the system synthesizer can be used for playback."""
    return bool(native_sidecar.section("speak").get("available"))


def dictation_unavailable_reason() -> str:
    """Why local dictation is off, phrased for Settings. Empty when available."""
    return native_sidecar.unavailable_reason("hear", subject="on-device dictation")


async def _run_sidecar(
    args: list[str], *, stdin: bytes | None = None
) -> tuple[int, bytes, str]:
    """Run one sidecar subcommand, as a ValueError-raising wrapper."""
    try:
        return await native_sidecar.run(args, stdin=stdin)
    except native_sidecar.SidecarError as exc:
        raise ValueError(str(exc)) from exc


class VoiceTranscriber:
    """OpenAI-backed voice transcription."""

    def __init__(self, config: BridgeConfig) -> None:
        if not config.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required for voice transcription")
        self._config = config
        self._client = AsyncOpenAI(api_key=config.openai_api_key)

    async def transcribe(self, path: Path) -> str:
        """Transcribe one saved audio file."""
        with path.open("rb") as handle:
            response = await self._client.audio.transcriptions.create(
                model=self._config.transcription_model,
                file=handle,
                response_format="json",
            )
        text = getattr(response, "text", "").strip()
        if not text:
            raise ValueError("Voice transcription returned empty text")
        return text


class AppleDictationTranscriber:
    """On-device transcription via Apple's dictation models (macOS 26+).

    Runs the bundled sidecar against the recording the PWA already saved. It
    uses ``DictationTranscriber``, which reuses the assets the OS downloaded for
    system dictation, so there is no model to fetch and no first-run delay.
    """

    def __init__(self, locale: str = "en-US") -> None:
        if not apple_dictation_available():
            raise ValueError(
                f"on-device dictation is unavailable: {dictation_unavailable_reason()}"
            )
        self._locale = locale or "en-US"

    async def transcribe(self, path: Path) -> str:
        code, out, err = await _run_sidecar(
            ["hear", str(path), "--locale", self._locale]
        )
        if code == SIDECAR_EXIT_UNSUPPORTED_OS:
            raise ValueError("On-device dictation requires macOS 26 or newer")
        if code == SIDECAR_EXIT_LOCALE_UNAVAILABLE:
            raise ValueError(
                err or f"No dictation language is installed for {self._locale}"
            )
        if code == SIDECAR_EXIT_AUDIO_UNREADABLE:
            raise ValueError("The recording could not be read")
        if code == SIDECAR_EXIT_EMPTY_RESULT:
            raise ValueError("Voice transcription returned empty text")
        if code != 0:
            raise ValueError(err or "Voice transcription failed")
        text = out.decode("utf-8", "replace").strip()
        if not text:
            raise ValueError("Voice transcription returned empty text")
        return text


# ── Speech synthesis (speak) ─────────────────────────────────────────────

# OpenAI caps speech input at 4096 chars; the system synthesizer gets the same
# budget so both engines speak the same excerpt of a long message.
MAX_SPEECH_CHARS = 4096


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


def system_voices() -> list[dict[str, str]]:
    """Installed synthesizer voices, best quality first.

    Empty when the sidecar is unavailable. Settings uses this to populate the
    voice picker; the ordering is the synthesizer's own preference, so the first
    entry is what an unset voice resolves to.
    """
    voices = native_sidecar.section("speak").get("voices") or []
    return [voice for voice in voices if isinstance(voice, dict)]


class SystemSpeaker:
    """Local speech synthesis via ``AVSpeechSynthesizer``, through the sidecar.

    Voice selection is by identifier or name; leaving it empty lets the sidecar
    pick the highest-quality installed voice for the locale (premium, then
    enhanced, then default). That ordering matters: Apple's better voices are an
    opt-in download in System Settings and Siri's voices are not exposed to
    third-party apps at all, so most machines only have the default tier today
    and will silently improve if that changes.
    """

    mime_type = "audio/wav"

    def __init__(self, voice: str = "", locale: str = "en-US") -> None:
        if not apple_speech_available():
            raise ValueError(
                "system speech synthesis is unavailable; "
                "install the desktop app with `ciao desktop install`"
            )
        self._voice = (voice or "").strip()
        self._locale = locale or "en-US"

    async def speak(self, text: str) -> bytes:
        """Synthesize one utterance; returns WAV bytes."""
        args = ["speak", "--locale", self._locale]
        if self._voice:
            args += ["--voice", self._voice]
        code, out, err = await _run_sidecar(args, stdin=text.encode("utf-8"))
        if code == SIDECAR_EXIT_LOCALE_UNAVAILABLE:
            raise ValueError(err or f"No installed voice matches {self._locale}")
        if code == SIDECAR_EXIT_EMPTY_RESULT:
            raise ValueError("Speech synthesis returned no audio")
        if code != 0:
            raise ValueError(err or "Speech synthesis failed")
        if not out:
            raise ValueError("Speech synthesis returned no audio")
        return out
