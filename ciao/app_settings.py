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

from ciao import provider_registry

logger = logging.getLogger(__name__)

_TIERS = ("haiku", "sonnet", "opus", "fable")


def _clean_tier_routes(raw: object) -> dict[str, dict[str, str]]:
    """Normalize a ``{provider: {tier: model}}`` map, dropping junk."""
    if not isinstance(raw, dict):
        return {}
    return {
        str(provider_id): {
            str(tier): str(model).strip()
            for tier, model in routes.items()
            if str(tier) in _TIERS and isinstance(model, str) and model.strip()
        }
        for provider_id, routes in raw.items()
        if isinstance(routes, dict)
    }


def _tier_settings(config: object, descriptor: object) -> object | None:
    """This provider's per-tier settings dataclass on ``config``, if present.

    Returns ``None`` when the provider declares no tier pins, or when the
    config object simply does not carry them — settings overlays run against
    duck-typed configs in tests and during bootstrap, and a missing attribute
    means "nothing to pin", not a failure.
    """
    attr = getattr(descriptor, "tier_settings_attr", "")
    if not attr:
        return None
    return getattr(config, attr, None)


def _merge_legacy_tier_pins(
    raw: dict, routing: dict[str, dict[str, str]]
) -> dict[str, dict[str, str]]:
    """Fold pre-``provider_routing`` ``<provider>_<tier>_model`` keys in.

    Settings files written before the per-provider map stored Codex pins as
    four flat scalars. They are read once here and re-persisted in the nested
    shape on the next save; an explicit ``provider_routing`` entry wins.
    """
    merged = {key: dict(value) for key, value in routing.items()}
    for descriptor in provider_registry.descriptors():
        if not descriptor.tier_settings_attr:
            continue
        for tier in _TIERS:
            value = raw.get(f"{descriptor.id}_{tier}_model")
            if not isinstance(value, str) or not value.strip():
                continue
            merged.setdefault(descriptor.id, {}).setdefault(tier, value.strip())
    return merged



@dataclass(slots=True)
class AppSettings:
    """One value per overridable knob; empty string = use config default.

    ``provider_routing`` and ``custom_routing`` are the exceptions: the set of
    runtime providers and of user-defined providers are both dynamic, so they
    nest a per-provider tier map rather than a scalar per provider and tier.
    """

    # Model used by the chat title generator. Overrides both the Ollama
    # free-tier title model and the Anthropic fallback when set.
    title_model: str = ""
    # Model used by post-archive session-insights extraction.
    insights_model: str = ""

    # BCP-47 language for the on-device voice engines.
    transcription_locale: str = ""
    # macOS voice identifier for read-aloud; empty means the sidecar picks the
    # best installed voice for the locale.
    tts_local_voice: str = ""
    # Comma-separated list of models for the adversarial_review MCP tool.
    critique_models: str = ""
    # Per-backend tier aliases used when a chat asks for haiku/sonnet/opus/fable
    # while the workspace routes through Ollama or OpenRouter.
    ollama_haiku_model: str = ""
    ollama_sonnet_model: str = ""
    ollama_opus_model: str = ""
    ollama_fable_model: str = ""
    openrouter_haiku_model: str = ""
    openrouter_sonnet_model: str = ""
    openrouter_opus_model: str = ""
    openrouter_fable_model: str = ""
    # Per-runtime-provider tier pins, keyed by provider id then tier. Unlike
    # Ollama/OpenRouter there is no env-backed default: a missing entry means
    # the tier resolves through that provider's automatic catalog mapping
    # (for Codex: luna→haiku, terra→sonnet, sol→opus/fable).
    #
    # A nested map rather than four scalars per provider, so a new provider
    # costs a registry entry instead of four fields threaded through the
    # settings route and the PWA.
    provider_routing: dict[str, dict[str, str]] | None = None
    # Per-custom-provider tier routes. Keys are provider ids and values map
    # haiku/sonnet/opus/fable to concrete ``custom:<id>:<model>`` ids.
    custom_routing: dict[str, dict[str, str]] | None = None


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
        nested_fields = {"custom_routing", "provider_routing"}
        string_fields = {
            field.name
            for field in fields(AppSettings)
            if field.name not in nested_fields
        }
        settings = AppSettings()
        for key, value in raw.items():
            if key in string_fields and isinstance(value, str):
                setattr(settings, key, value.strip())
        custom_routing = _clean_tier_routes(raw.get("custom_routing"))
        if custom_routing:
            settings.custom_routing = custom_routing
        provider_routing = _merge_legacy_tier_pins(
            raw, _clean_tier_routes(raw.get("provider_routing"))
        )
        if provider_routing:
            settings.provider_routing = provider_routing
        return settings

    def _save(self) -> None:
        payload = {k: v for k, v in asdict(self.settings).items() if v}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )

    def update(self, changes: dict[str, object]) -> AppSettings:
        """Validate and persist a partial update; returns the new settings.

        Unknown keys are ignored. Raises ``ValueError`` on a bad engine
        value so the API route can 400 instead of persisting garbage.

        Legacy flat ``<provider>_<tier>_model`` tier pins are still accepted
        and folded into ``provider_routing``, so a client written against the
        pre-map settings API keeps working.
        """
        known = {f.name for f in fields(AppSettings)}
        legacy_pins = self._legacy_pin_updates(changes)
        if legacy_pins:
            merged = {
                key: dict(value)
                for key, value in (self.settings.provider_routing or {}).items()
            }
            for provider_id, routes in legacy_pins.items():
                target = merged.setdefault(provider_id, {})
                for tier, model in routes.items():
                    # An empty value clears the pin back to automatic.
                    if model:
                        target[tier] = model
                    else:
                        target.pop(tier, None)
                # Drop a provider that has no pins left, so clearing the last
                # one persists as absence rather than an empty object.
                if not target:
                    del merged[provider_id]
            self.settings.provider_routing = merged
        for key, value in changes.items():
            if key not in known:
                continue
            if key in {"custom_routing", "provider_routing"}:
                if not isinstance(value, dict):
                    raise ValueError(f"{key} must be an object")
                if any(not isinstance(routes, dict) for routes in value.values()):
                    raise ValueError(f"{key} entries must be objects")
                setattr(self.settings, key, _clean_tier_routes(value))
                continue
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a string")
            value = value.strip()
            setattr(self.settings, key, value)
        self._save()
        return self.settings

    @staticmethod
    def _legacy_pin_updates(changes: dict[str, object]) -> dict[str, dict[str, str]]:
        """Extract ``<provider>_<tier>_model`` keys from a legacy PATCH body."""
        found: dict[str, dict[str, str]] = {}
        for descriptor in provider_registry.descriptors():
            if not descriptor.tier_settings_attr:
                continue
            for tier in _TIERS:
                key = f"{descriptor.id}_{tier}_model"
                if key not in changes:
                    continue
                value = changes[key]
                if not isinstance(value, str):
                    raise ValueError(f"{key} must be a string")
                found.setdefault(descriptor.id, {})[tier] = value.strip()
        return found

    def tier_pin(self, provider: str, tier: str) -> str:
        """Operator-pinned model for a provider tier; "" means automatic."""
        return (self.settings.provider_routing or {}).get(provider, {}).get(tier, "")

    def tier_model_defaults(self) -> dict[str, dict[str, str]]:
        """Return the env-backed tier models captured before overrides."""
        defaults = self._defaults or {}
        return {
            provider: {
                tier: defaults.get(f"{provider}_{tier}_model", "")
                for tier in ("haiku", "sonnet", "opus", "fable")
            }
            for provider in ("ollama", "openrouter")
        }

    def apply_to_config(self, config) -> None:
        """Overlay settings onto the live ``CiaoConfig`` object.

        The first call snapshots the env-backed values so a later call
        with a cleared (empty) setting restores the original default
        instead of keeping a stale override.
        """
        if self._defaults is None:
            self._defaults = {
                "title_model_override": config.title_model_override,
                "insights_model_override": config.insights_model_override,

                "transcription_locale": config.transcription_locale,
                "tts_local_voice": config.tts_local_voice,
                "critique_models": config.critique_models,
                "ollama_haiku_model": config.ollama.haiku_model,
                "ollama_sonnet_model": config.ollama.sonnet_model,
                "ollama_opus_model": config.ollama.opus_model,
                "ollama_fable_model": config.ollama.fable_model,
                "openrouter_haiku_model": config.openrouter.haiku_model,
                "openrouter_sonnet_model": config.openrouter.sonnet_model,
                "openrouter_opus_model": config.openrouter.opus_model,
                "openrouter_fable_model": config.openrouter.fable_model,
            }
            for descriptor in provider_registry.descriptors():
                # A config object that predates this provider (or a duck-typed
                # stand-in) simply has no pins to snapshot.
                current = _tier_settings(config, descriptor)
                if current is None:
                    continue
                for tier in _TIERS:
                    self._defaults[f"{descriptor.id}_{tier}_model"] = getattr(
                        current, f"{tier}_model", ""
                    )
        d = self._defaults
        s = self.settings
        config.title_model_override = s.title_model or d["title_model_override"]
        config.insights_model_override = s.insights_model or d["insights_model_override"]

        config.transcription_locale = (
            s.transcription_locale or d["transcription_locale"]
        )
        config.tts_local_voice = s.tts_local_voice or d["tts_local_voice"]
        config.critique_models = s.critique_models or d["critique_models"]
        config.ollama = replace(
            config.ollama,
            haiku_model=s.ollama_haiku_model or d["ollama_haiku_model"],
            sonnet_model=s.ollama_sonnet_model or d["ollama_sonnet_model"],
            opus_model=s.ollama_opus_model or d["ollama_opus_model"],
            fable_model=s.ollama_fable_model or d["ollama_fable_model"],
        )
        config.openrouter = replace(
            config.openrouter,
            haiku_model=s.openrouter_haiku_model or d["openrouter_haiku_model"],
            sonnet_model=s.openrouter_sonnet_model or d["openrouter_sonnet_model"],
            opus_model=s.openrouter_opus_model or d["openrouter_opus_model"],
            fable_model=s.openrouter_fable_model or d["openrouter_fable_model"],
        )
        routing = s.provider_routing or {}
        for descriptor in provider_registry.descriptors():
            current = _tier_settings(config, descriptor)
            if current is None:
                continue
            routes = routing.get(descriptor.id, {})
            setattr(
                config,
                descriptor.tier_settings_attr,
                replace(
                    current,
                    **{
                        f"{tier}_model": routes.get(tier)
                        or d.get(f"{descriptor.id}_{tier}_model", "")
                        for tier in _TIERS
                    },
                ),
            )
        config.custom_routing = s.custom_routing or {}
