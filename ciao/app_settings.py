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
from typing import Any, Callable

from ciao import provider_registry

logger = logging.getLogger(__name__)


def _clean_provider_map(raw: object) -> dict[str, str]:
    """Normalize a ``{provider: value}`` map, dropping junk."""
    if not isinstance(raw, dict):
        return {}
    known = set(provider_registry.provider_ids())
    return {
        str(provider_id): str(value).strip()
        for provider_id, value in raw.items()
        if str(provider_id) in known and isinstance(value, str) and value.strip()
    }


# Execution (permission) modes a provider default may pin. ``manual`` is the
# PWA-facing name for the BridgeMode ``normal`` (ask for every action);
# ``plan`` stays chat-only and is not a settings default.
_MODES = ("manual", "auto", "bypass")


def _clean_default_modes(raw: object) -> dict[str, str]:
    """Normalize a ``{provider: mode}`` map, dropping junk and bad modes."""
    if not isinstance(raw, dict):
        return {}
    known = set(provider_registry.provider_ids())
    return {
        str(provider_id): str(mode).strip()
        for provider_id, mode in raw.items()
        if str(provider_id) in known
        and isinstance(mode, str)
        and mode.strip() in _MODES
    }


# Every per-provider nested map, with the cleaner that normalizes it. One
# table rather than a `nested_fields` set, a parallel key tuple, and a third
# `if key in {...}` branch in `update`: those three had already drifted —
# `provider_default_modes` was missing from the set, so it landed in
# `string_fields` and survived a reload only because the dict failed the
# incidental `isinstance(value, str)` check. A new knob added to one list and
# not the others is silently dropped on every restart, with no error anywhere.
_NESTED_CLEANERS: dict[str, Callable[[object], dict[str, str]]] = {
    "provider_default_models": _clean_provider_map,
    "provider_default_thinking": _clean_provider_map,
    "provider_insights_models": _clean_provider_map,
    "provider_default_modes": _clean_default_modes,
}


def _default_model_settings(config: object, descriptor: object) -> Any:
    """This provider's default-model settings dataclass on ``config``, if present.

    Returns ``None`` when the provider declares no default-model setting, or
    when the config object simply does not carry it — settings overlays run
    against duck-typed configs in tests and during bootstrap, and a missing
    attribute means "nothing to set", not a failure.
    """
    attr = getattr(descriptor, "default_model_settings_attr", "")
    if not attr:
        return None
    return getattr(config, attr, None)



@dataclass(slots=True)
class AppSettings:
    """One value per overridable knob; empty string = use config default.

    The per-provider maps are the exception: the set of runtime providers is
    dynamic, so each nests a per-provider value rather than a scalar per
    provider.
    """

    # Model used by post-archive session-insights extraction.
    insights_model: str = ""

    # BCP-47 language for the on-device voice engines.
    transcription_locale: str = ""
    # macOS voice identifier for read-aloud; empty means the sidecar picks the
    # best installed voice for the locale.
    tts_local_voice: str = ""
    # Comma-separated list of models for the adversarial_review MCP tool.
    critique_models: str = ""
    # Per-runtime-provider default model for new chats, keyed by provider id.
    # There is no env-backed default: a missing entry means the provider's own
    # catalog default applies.
    #
    # A nested map rather than a scalar per provider, so a new provider costs a
    # registry entry instead of a field threaded through the settings route and
    # the PWA.
    provider_default_models: dict[str, str] | None = None

    # Per-provider default execution (permission) mode for new chats. Missing
    # entry = the env-backed ``claude_mode`` (auto), for every provider. Same
    # nested-map rationale as ``provider_default_models``.
    provider_default_modes: dict[str, str] | None = None

    # Per-provider default thinking level for new chats. Missing entry = the
    # provider's own default ("auto").
    provider_default_thinking: dict[str, str] | None = None

    # Per-provider session-insights model. Missing entry = the provider's
    # balanced default.
    provider_insights_models: dict[str, str] | None = None


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
        string_fields = {
            field.name
            for field in fields(AppSettings)
            if field.name not in _NESTED_CLEANERS
        }
        settings = AppSettings()
        for key, value in raw.items():
            if key in string_fields and isinstance(value, str):
                setattr(settings, key, value.strip())
        for key, cleaner in _NESTED_CLEANERS.items():
            cleaned = cleaner(raw.get(key))
            if cleaned:
                setattr(settings, key, cleaned)
        return settings

    def _save(self) -> None:
        payload = {}
        for key, value in asdict(self.settings).items():
            if value:
                payload[key] = value
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )

    def update(self, changes: dict[str, object]) -> AppSettings:
        """Validate and persist a partial update; returns the new settings.

        Unknown keys are ignored. Raises ``ValueError`` on a bad engine
        value so the API route can 400 instead of persisting garbage.
        """
        known = {f.name for f in fields(AppSettings)}
        for key, value in changes.items():
            if key not in known:
                continue
            if key in _NESTED_CLEANERS and key != "provider_default_modes":
                if not isinstance(value, dict):
                    raise ValueError(f"{key} must be an object")
                setattr(self.settings, key, _NESTED_CLEANERS[key](value))
                continue
            if key == "provider_default_modes":
                if not isinstance(value, dict):
                    raise ValueError(f"{key} must be an object")
                unknown = sorted(
                    str(mode)
                    for mode in value.values()
                    if not isinstance(mode, str)
                    or (mode.strip() and mode.strip() not in _MODES)
                )
                if unknown:
                    raise ValueError(
                        f"{key} entries must be one of {', '.join(_MODES)}"
                    )
                setattr(self.settings, key, _clean_default_modes(value))
                continue
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a string")
            value = value.strip()
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
                "insights_model_override": config.insights_model_override,
                "transcription_locale": config.transcription_locale,
                "tts_local_voice": config.tts_local_voice,
                "critique_models": config.critique_models,
            }
            for descriptor in provider_registry.descriptors():
                # A config object that predates this provider (or a duck-typed
                # stand-in) simply has no default to snapshot.
                current = _default_model_settings(config, descriptor)
                if current is None:
                    continue
                self._defaults[f"{descriptor.id}_default_model"] = getattr(
                    current, "default_model", ""
                )
        d = self._defaults
        s = self.settings
        config.insights_model_override = s.insights_model or d["insights_model_override"]

        config.transcription_locale = (
            s.transcription_locale or d["transcription_locale"]
        )
        config.tts_local_voice = s.tts_local_voice or d["tts_local_voice"]
        config.critique_models = s.critique_models or d["critique_models"]
        # Per-provider default models / thinking / routine models have no
        # env-backed default; absence means "use the provider's own default".
        config.provider_default_models = dict(s.provider_default_models or {})
        config.provider_default_thinking = dict(s.provider_default_thinking or {})
        config.provider_insights_models = dict(s.provider_insights_models or {})
        # Modes carry the PWA-facing "manual" through as the BridgeMode
        # "normal" the providers understand.
        config.provider_default_modes = {
            provider: ("normal" if mode == "manual" else mode)
            for provider, mode in (s.provider_default_modes or {}).items()
        }
        for descriptor in provider_registry.descriptors():
            current = _default_model_settings(config, descriptor)
            if current is None:
                continue
            default = (s.provider_default_models or {}).get(descriptor.id, "")
            setattr(
                config,
                descriptor.default_model_settings_attr,
                replace(
                    current,
                    default_model=default
                    or d.get(f"{descriptor.id}_default_model", ""),
                ),
            )
