"""Route selected models through OpenRouter's Anthropic-compatible API.

OpenRouter exposes an Anthropic Messages-format endpoint at
``https://openrouter.ai/api/v1/messages``. The Claude Agent SDK appends
``/v1/messages`` to ``ANTHROPIC_BASE_URL``, so we point it at
``https://openrouter.ai/api`` and pass the OpenRouter API key as
``ANTHROPIC_AUTH_TOKEN`` (sent as ``Authorization: Bearer``). Model ids
use OpenRouter's ``owner/model`` form (e.g. ``anthropic/claude-haiku-4.5``).

Validated end-to-end through ``claude_agent_sdk.query``: the SDK accepts
the base-URL override and OpenRouter returns a well-formed Anthropic
message stream. The same env-injection pattern as
:mod:`ciao.providers.ollama`, just a different upstream.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, replace

logger = logging.getLogger(__name__)

_ANTHROPIC_ALIASES = frozenset({"opus", "sonnet", "haiku"})

# Shipped defaults: one Anthropic-family model per tier, reachable through
# OpenRouter's Anthropic-compatible endpoint. Operators override via
# the per-tier CIAO_OPENROUTER_HAIKU_MODEL, _SONNET_MODEL, _OPUS_MODEL env vars.
# These double as the picker entries when the
# OpenRouter backend is available.
_DEFAULT_HAIKU = "anthropic/claude-haiku-4.5"
_DEFAULT_SONNET = "anthropic/claude-sonnet-4.5"
_DEFAULT_OPUS = "anthropic/claude-opus-4.8"


@dataclass(frozen=True, slots=True)
class OpenRouterSettings:
    """Resolved OpenRouter routing configuration."""

    api_key: str = ""
    base_url: str = "https://openrouter.ai/api"
    # Per-tier model ids (owner/model). Used when a chat/automation is
    # configured with an Anthropic alias but routed through OpenRouter.
    haiku_model: str = _DEFAULT_HAIKU
    sonnet_model: str = _DEFAULT_SONNET
    opus_model: str = _DEFAULT_OPUS
    # Models explicitly allowlisted by the operator (owner/model ids). When
    # empty, the picker falls back to the three tier defaults above plus
    # whatever ``discover_models`` returns.
    models: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        """True when an OpenRouter API key is configured."""
        return bool(self.api_key)


def is_openrouter_model(model: str, settings: OpenRouterSettings) -> bool:
    """True when ``model`` should be routed through OpenRouter.

    A model routes through OpenRouter when it carries an ``owner/model`` id
    that is not a bare Anthropic alias and not an Ollama-style id (no
    ``:cloud``/``:tag`` suffix). Tier-default ids (e.g.
    ``anthropic/claude-haiku-4.5``) always count.
    """
    if not model:
        return False
    if model in _ANTHROPIC_ALIASES:
        return False
    if model in settings.models:
        return True
    # owner/model shape, but not an Ollama tag id
    if "/" in model and ":" not in model:
        return True
    return False


def alias_model(alias: str, settings: OpenRouterSettings) -> str:
    """Resolve an Anthropic tier alias to an OpenRouter model id."""
    aliases = {
        "haiku": settings.haiku_model,
        "sonnet": settings.sonnet_model,
        "opus": settings.opus_model,
    }
    return aliases.get((alias or "").lower(), alias)


def _env_overrides(base_url: str, api_key: str) -> dict[str, str]:
    return {
        "ANTHROPIC_AUTH_TOKEN": api_key,
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_BASE_URL": base_url,
    }


def openrouter_env_for_model(
    model: str, settings: OpenRouterSettings
) -> dict[str, str]:
    """Return env overrides to merge into ``AgentRequest.extra_env``.

    Empty dict when the model should not route through OpenRouter (e.g. a
    bare Anthropic alias, or an Ollama id). The caller is responsible for
    resolving aliases to concrete OpenRouter ids before calling this.
    """
    if not settings.available or not is_openrouter_model(model, settings):
        return {}
    return _env_overrides(settings.base_url, settings.api_key)


def routine_env_for_model(model: str, settings: OpenRouterSettings) -> dict[str, str]:
    """Env injection for routine one-shot calls (insights, titles, etc.).

    Not gated on the ``models`` allowlist: a tier-default id routes through
    OpenRouter even when the operator hasn't listed it explicitly.
    """
    if not settings.available or not model:
        return {}
    if model in _ANTHROPIC_ALIASES:
        return {}
    # owner/model shape routes through OpenRouter; Ollama tag ids do not.
    if "/" in model and ":" not in model:
        return _env_overrides(settings.base_url, settings.api_key)
    return {}


def discover_models(
    settings: OpenRouterSettings, *, timeout_s: float = 4.0, anthropic_only: bool = True
) -> tuple[str, ...]:
    """List models from OpenRouter's ``/api/v1/models`` endpoint.

    Returns ``()`` when the backend is unavailable or the request fails.
    When ``anthropic_only`` is true, restricts to ``anthropic/*`` ids so the
    picker only offers models that match the alias family and are known to
    work through the Anthropic-compatible endpoint.
    """
    if not settings.available:
        return ()
    url = settings.base_url.rstrip("/") + "/v1/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {settings.api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as response:
            payload = json.loads(response.read().decode())
    except (OSError, urllib.error.URLError, TimeoutError, ValueError):
        return ()
    models = []
    for entry in payload.get("data", []) or []:
        mid = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(mid, str) or not mid:
            continue
        if anthropic_only and not mid.startswith("anthropic/"):
            continue
        models.append(mid)
    return tuple(dict.fromkeys(models))


def merge_discovered(settings: OpenRouterSettings, discovered: tuple[str, ...]) -> OpenRouterSettings:
    """Return a new settings with discovered models merged into the allowlist."""
    merged = tuple(dict.fromkeys([*settings.models, *discovered]))
    return replace(settings, models=merged)
