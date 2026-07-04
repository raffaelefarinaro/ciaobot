"""Runtime-mutable app settings persisted under the runtime root.

The env-backed :class:`ciao.config.CiaoConfig` stays the source of
defaults; this store holds the small set of knobs the PWA Settings →
Models tab can change at runtime (internal-routine models and the voice
transcription engine). Values are applied as an overlay onto the live
config object so call sites keep reading ``config.*`` and PATCHes take
effect without a restart. Empty string means "no override, use the
config/env default".
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

logger = logging.getLogger(__name__)

_VALID_ENGINES = {"", "cloud", "local"}


@dataclass(slots=True)
class AppSettings:
    """One value per overridable knob; empty string = use config default."""

    # Model used by the chat title generator. Overrides both the Ollama
    # free-tier title model and the Anthropic fallback when set.
    title_model: str = ""
    # Model used by post-archive session-insights extraction.
    insights_model: str = ""

    # Voice transcription engine: "cloud" (OpenAI) or "local" (mlx-whisper).
    transcription_engine: str = ""
    # Whisper checkpoint for the local engine (HF repo id).
    transcription_local_model: str = ""
    # Comma-separated list of models for the critique / adversarial-review skill.
    critique_models: str = ""
    # Per-backend tier aliases used when a chat asks for haiku/sonnet/opus
    # while the workspace routes through Ollama or OpenRouter.
    ollama_haiku_model: str = ""
    ollama_sonnet_model: str = ""
    ollama_opus_model: str = ""
    openrouter_haiku_model: str = ""
    openrouter_sonnet_model: str = ""
    openrouter_opus_model: str = ""


class AppSettingsStore:
    """JSON-file-backed store for :class:`AppSettings`."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.settings = self._load()
        # Env-backed defaults captured on the first apply_to_config() call,
        # so clearing an override restores the original value.
        self._defaults: dict[str, str] | None = None

    def _load(self) -> AppSettings:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return AppSettings()
        except (OSError, ValueError):
            logger.warning("Unreadable app settings at %s; using defaults", self._path)
            return AppSettings()
        known = {f.name for f in fields(AppSettings)}
        cleaned = {
            k: v.strip()
            for k, v in raw.items()
            if k in known and isinstance(v, str)
        }
        return AppSettings(**cleaned)

    def _save(self) -> None:
        payload = {k: v for k, v in asdict(self.settings).items() if v}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )

    def update(self, changes: dict[str, str]) -> AppSettings:
        """Validate and persist a partial update; returns the new settings.

        Unknown keys are ignored. Raises ``ValueError`` on a bad engine
        value so the API route can 400 instead of persisting garbage.
        """
        known = {f.name for f in fields(AppSettings)}
        for key, value in changes.items():
            if key not in known:
                continue
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a string")
            value = value.strip()
            if key == "transcription_engine" and value not in _VALID_ENGINES:
                raise ValueError("transcription_engine must be 'cloud' or 'local'")
            setattr(self.settings, key, value)
        self._save()
        return self.settings

    def apply_to_config(self, config) -> None:
        """Overlay settings onto the live ``CiaoConfig`` object.

        The first call snapshots the env-backed values so a later call
        with a cleared (empty) setting restores the original default
        instead of keeping a stale override.
        """
        if self._defaults is None:
            self._defaults = {
                "title_model_override": config.title_model_override,
                "insights_model": config.insights_model,

                "transcription_engine": config.transcription_engine,
                "transcription_local_model": config.transcription_local_model,
                "critique_models": config.critique_models,
                "ollama_haiku_model": config.ollama.haiku_model,
                "ollama_sonnet_model": config.ollama.sonnet_model,
                "ollama_opus_model": config.ollama.opus_model,
                "openrouter_haiku_model": config.openrouter.haiku_model,
                "openrouter_sonnet_model": config.openrouter.sonnet_model,
                "openrouter_opus_model": config.openrouter.opus_model,
            }
        d = self._defaults
        s = self.settings
        config.title_model_override = s.title_model or d["title_model_override"]
        config.insights_model = s.insights_model or d["insights_model"]

        config.transcription_engine = (
            s.transcription_engine or d["transcription_engine"]
        )
        config.transcription_local_model = (
            s.transcription_local_model or d["transcription_local_model"]
        )
        config.critique_models = s.critique_models or d["critique_models"]
        config.ollama = replace(
            config.ollama,
            haiku_model=s.ollama_haiku_model or d["ollama_haiku_model"],
            sonnet_model=s.ollama_sonnet_model or d["ollama_sonnet_model"],
            opus_model=s.ollama_opus_model or d["ollama_opus_model"],
        )
        config.openrouter = replace(
            config.openrouter,
            haiku_model=s.openrouter_haiku_model or d["openrouter_haiku_model"],
            sonnet_model=s.openrouter_sonnet_model or d["openrouter_sonnet_model"],
            opus_model=s.openrouter_opus_model or d["openrouter_opus_model"],
        )
