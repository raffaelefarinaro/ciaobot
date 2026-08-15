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
from typing import Any

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


def _coerce_bool(raw: object) -> bool | None:
    """Read a stored boolean toggle, tolerating hand-edited truthy strings."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(raw, int) and raw in (0, 1):
        return bool(raw)
    return None


def _tier_settings(config: object, descriptor: object) -> Any:
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

    ``provider_routing`` is the exception: the set of runtime providers is
    dynamic, so it nests a per-provider tier map rather than a scalar per
    provider and tier.
    """

    # Model used by the chat title generator. Overrides the workspace's
    # haiku-tier default when set.
    title_model: str = ""
    # Model used by post-archive session-insights extraction.
    insights_model: str = ""

    # Apple Intelligence (the on-device "Local (free)" model) is a beta feature,
    # off by default. None = "no override, use the config/env default" (false);
    # True enables it, False disables it even when the env default is on.
    apple_intelligence_enabled: bool | None = None

    # BCP-47 language for the on-device voice engines.
    transcription_locale: str = ""
    # macOS voice identifier for read-aloud; empty means the sidecar picks the
    # best installed voice for the locale.
    tts_local_voice: str = ""
    # Comma-separated list of models for the adversarial_review MCP tool.
    critique_models: str = ""
    # Per-runtime-provider tier pins, keyed by provider id then tier. There is
    # no env-backed default: a missing entry means the tier resolves through
    # that provider's automatic catalog mapping (for Codex: luna→haiku,
    # terra→sonnet, sol→opus/fable).
    #
    # A nested map rather than four scalars per provider, so a new provider
    # costs a registry entry instead of four fields threaded through the
    # settings route and the PWA.
    provider_routing: dict[str, dict[str, str]] | None = None


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
        nested_fields = {"provider_routing"}
        string_fields = {
            field.name
            for field in fields(AppSettings)
            if field.name not in nested_fields
        }
        settings = AppSettings()
        for key, value in raw.items():
            if key in string_fields and isinstance(value, str):
                setattr(settings, key, value.strip())
        if "apple_intelligence_enabled" in raw:
            settings.apple_intelligence_enabled = _coerce_bool(
                raw["apple_intelligence_enabled"]
            )
        provider_routing = _merge_legacy_tier_pins(
            raw, _clean_tier_routes(raw.get("provider_routing"))
        )
        if provider_routing:
            settings.provider_routing = provider_routing
        return settings

    def _save(self) -> None:
        payload = {}
        for key, value in asdict(self.settings).items():
            if key == "apple_intelligence_enabled":
                # A tri-state bool: persist only an explicit override so the
                # env default keeps working when the field is left untouched.
                if value is not None:
                    payload[key] = value
            elif value:
                payload[key] = value
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
            if key == "apple_intelligence_enabled":
                # The one boolean knob: the PWA sends true/false; anything
                # else is a client bug, not a value to persist.
                if not isinstance(value, bool):
                    raise ValueError("apple_intelligence_enabled must be a boolean")
                setattr(self.settings, key, value)
                continue
            if key == "provider_routing":
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
                "apple_intelligence_enabled": config.apple_intelligence_enabled,

                "transcription_locale": config.transcription_locale,
                "tts_local_voice": config.tts_local_voice,
                "critique_models": config.critique_models,
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

        # Beta feature, off by default: the env default, unless the operator
        # has explicitly toggled it in Settings. Mirror it onto the sidecar so
        # availability checks and `respond` agree without threading config.
        config.apple_intelligence_enabled = (
            s.apple_intelligence_enabled
            if s.apple_intelligence_enabled is not None
            else d["apple_intelligence_enabled"]
        )
        from ciao import native_sidecar

        native_sidecar.set_apple_intelligence_enabled(config.apple_intelligence_enabled)

        config.transcription_locale = (
            s.transcription_locale or d["transcription_locale"]
        )
        config.tts_local_voice = s.tts_local_voice or d["tts_local_voice"]
        config.critique_models = s.critique_models or d["critique_models"]
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
