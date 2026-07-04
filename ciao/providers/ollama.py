"""Routing selected models through Ollama's Anthropic-compatible API.

Two flavours of upstream are supported, both fronted by the same env
overrides on the spawned ``claude`` CLI:

* **Local daemon (device-linked)**: point ``ANTHROPIC_BASE_URL`` at
  ``http://localhost:11434`` and use the literal token ``"ollama"``. The
  daemon authenticates to the cloud via the SSH key set up through
  ``ollama signin``.
* **Direct cloud**: point ``ANTHROPIC_BASE_URL`` at ``https://ollama.com``
  and pass an Ollama Cloud API key as ``ANTHROPIC_AUTH_TOKEN``. The
  Anthropic SDK turns it into an ``Authorization: Bearer`` header, which
  is what ollama.com accepts (an ``x-api-key`` header returns
  ``unauthorized``).

Both routes reach the same endpoint and the same models. Direct cloud
skips the daemon entirely, which is useful on hosts that already have
an API key and don't want to run ``ollama serve``. The helpers below
decide when to inject the overrides (the requested model is in the
configured allowlist) and what to fill them with (the configured base
URL and auth token).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Anthropic model aliases (and real claude-* ids) must never be rerouted to
# Ollama by the routine helpers below — they belong to the Anthropic
# subscription path.
_ANTHROPIC_ALIASES = frozenset({"opus", "sonnet", "haiku"})


@dataclass(frozen=True, slots=True)
class OllamaSettings:
    """Resolved Ollama routing configuration.

    ``models`` is an explicit allowlist (no prefix matching, no fuzzy
    rules) so a typo in config can't silently send Anthropic traffic to
    Ollama. ``base_url`` and ``api_key`` are forwarded verbatim into
    ``ANTHROPIC_BASE_URL`` and ``ANTHROPIC_AUTH_TOKEN`` respectively.

    The default ``api_key="ollama"`` is the literal token expected by the
    local daemon when device-linked auth is in effect; override it with
    a real Ollama Cloud key to skip the daemon and hit ollama.com
    directly.

    ``cookie`` is an optional ``Cookie`` header value (e.g. copied from
    the browser devtools while logged into ``ollama.com/settings``). When
    set, the usage fetcher scrapes the settings page HTML instead of
    hitting the non-public usage API.
    """

    models: tuple[str, ...] = ()
    base_url: str = "http://localhost:11434"
    api_key: str = "ollama"
    cookie: str = ""
    # Cheap free-tier model used to generate chat titles for Ollama-routed
    # chats. Default ``ministral-3:3b`` because it's the smallest model in
    # the cloud catalog (3B params, free-tier accessible) and titles are
    # ~50 token tasks where any tier is overkill. Override with
    # ``CIAO_OLLAMA_TITLE_MODEL`` if you prefer another small model
    # (e.g. ``gemma3:4b``, ``qwen3:8b``).
    title_model: str = "ministral-3:3b"
    # Per-tier Ollama model overrides. Used when a chat is configured with
    # Anthropic alias names (haiku/sonnet/opus) but the operator wants the
    # request routed through Ollama. Override with ``CIAO_OLLAMA_HAIKU_MODEL``
    # / ``_SONNET_MODEL`` / ``_OPUS_MODEL``.
    haiku_model: str = "deepseek-v4-flash:cloud"
    sonnet_model: str = "kimi-k2.7-code:cloud"
    opus_model: str = "minimax-m3:cloud"
    # Models served by a *local* Ollama daemon, routed independently of the
    # cloud allowlist above so both flavours can coexist (cloud key set →
    # ``base_url`` points at ollama.com while ``local_models`` still go to
    # the daemon at ``local_url``). Populated from ``CIAO_OLLAMA_LOCAL_MODELS``
    # plus startup auto-discovery against ``local_url`` (see
    # :func:`discover_local_models`).
    local_models: tuple[str, ...] = ()
    local_url: str = "http://localhost:11434"


def is_local_ollama_model(model: str, settings: OllamaSettings) -> bool:
    """True when ``model`` is served by the local Ollama daemon."""
    return bool(model) and model in settings.local_models


def _cloud_available(settings: OllamaSettings) -> bool:
    """True when Ollama Cloud is configured (a real API key, not the
    local daemon's literal "ollama" token)."""
    return bool(settings.api_key) and settings.api_key != "ollama"


def _is_ollama_shaped(model: str) -> bool:
    """Ollama ids carry a ``:tag``/``:cloud`` suffix; Anthropic aliases
    and OpenRouter ``owner/model`` ids do not."""
    return ":" in model


def is_ollama_model(model: str, settings: OllamaSettings) -> bool:
    """True when ``model`` should be routed through Ollama (cloud or local).

    Dynamic: a local-daemon model routes through Ollama when it is in
    ``local_models``; any ``:tag``/``:cloud``-shaped id routes through
    Ollama Cloud when a cloud API key is configured. No static allowlist
    required — ``settings.models`` is picker display only.
    """
    if not model:
        return False
    if is_local_ollama_model(model, settings):
        return True
    return _is_ollama_shaped(model) and _cloud_available(settings)


def _env_overrides(base_url: str, api_key: str) -> dict[str, str]:
    return {
        "ANTHROPIC_AUTH_TOKEN": api_key,
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_BASE_URL": base_url,
    }


def ollama_env_for_model(model: str, settings: OllamaSettings) -> dict[str, str]:
    """Return the env overrides to merge into ``AgentRequest.extra_env``.

    Empty dict when the model is not Ollama-routed; callers should merge
    this on top of the existing extra env so the rest of the runtime
    context (GWS profile, etc.) is preserved. Local-daemon models win over
    the cloud allowlist when a model id appears in both.
    """
    if is_local_ollama_model(model, settings):
        # The daemon expects the literal "ollama" token under device-linked
        # auth; a cloud API key would be rejected.
        return _env_overrides(settings.local_url, "ollama")
    if model and _is_ollama_shaped(model) and _cloud_available(settings):
        return _env_overrides(settings.base_url, settings.api_key)
    return {}


def _looks_like_ollama_id(model: str) -> bool:
    """Ollama ids carry a ``:tag``/``:cloud`` suffix; OpenRouter ids are
    ``owner/model`` (no ``:``); Anthropic ids are bare aliases or ``claude-*``."""
    return ":" in model

def routine_env_for_model(model: str, settings: OllamaSettings) -> dict[str, str]:
    """Env overrides for server-side routines (titles, insights).

    Unlike :func:`ollama_env_for_model` this is not gated on the cloud
    allowlist: routine models are fixed at the server level (config or the
    runtime settings store), so the per-chat typo protection isn't relevant.
    Only Ollama-shaped ids (``:tag``/``:cloud`` or local-daemon models)
    route through Ollama; ``owner/model`` ids fall through so the OpenRouter
    backend can claim them. Anthropic aliases and ``claude-*`` ids return
    an empty dict so the routine stays on the subscription path.
    """
    if not model or model in _ANTHROPIC_ALIASES or model.startswith("claude-"):
        return {}
    if is_local_ollama_model(model, settings):
        return _env_overrides(settings.local_url, "ollama")
    if _looks_like_ollama_id(model):
        return _env_overrides(settings.base_url, settings.api_key)
    return {}


def discover_local_models(local_url: str, timeout_s: float = 2.0) -> tuple[str, ...]:
    """List models installed on a local Ollama daemon via ``GET /api/tags``.

    Returns an empty tuple when the daemon is unreachable or the response
    is malformed — discovery is best-effort and must never block startup.
    Synchronous on purpose: called once from ``main`` before the event
    loop is busy, with a short timeout.
    """
    url = local_url.rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, ValueError) as exc:
        logger.info("Ollama local discovery skipped (%s unreachable: %s)", url, exc)
        return ()
    models = payload.get("models")
    if not isinstance(models, list):
        return ()
    names = [
        entry.get("name", "").strip()
        for entry in models
        if isinstance(entry, dict) and entry.get("name", "").strip()
    ]
    return tuple(dict.fromkeys(names))
