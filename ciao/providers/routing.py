"""Backend-agnostic model routing: pick the right env injection for a model.

A single model id resolves to exactly one upstream (Ollama, OpenRouter, or
Anthropic passthrough) based on its shape, so calling both helpers and
merging is safe -- only one ever returns non-empty overrides.

* Ollama ids carry a ``:tag``/``:cloud`` suffix (e.g. ``kimi-k2.7-code:cloud``)
  or are in the local-daemon set.
* OpenRouter ids are ``owner/model`` without a ``:`` (e.g.
  ``anthropic/claude-haiku-4.5``).
* Bare Anthropic aliases (``opus``/``sonnet``/``haiku``) and ``claude-*`` ids
  get no overrides (Anthropic subscription path).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ciao.providers.ollama import ollama_env_for_model, routine_env_for_model as _ollama_routine
from ciao.providers.openrouter import openrouter_env_for_model, routine_env_for_model as _or_routine

if TYPE_CHECKING:
    from ciao.config import CiaoConfig


def routing_env_for_model(model: str, config: "CiaoConfig") -> dict[str, str]:
    """Env overrides to route ``model`` through its configured upstream."""
    env = ollama_env_for_model(model, config.ollama)
    if env:
        return env
    return openrouter_env_for_model(model, config.openrouter)


def routing_routine_env_for_model(model: str, config: "CiaoConfig") -> dict[str, str]:
    """Env overrides for routine one-shot calls (not gated on allowlists)."""
    env = _ollama_routine(model, config.ollama)
    if env:
        return env
    return _or_routine(model, config.openrouter)


def intended_backend(model: str) -> str:
    """Which backend a model id shape implies: openrouter / ollama / anthropic."""
    if not model:
        return "anthropic"
    if model in {"opus", "sonnet", "haiku"} or model.startswith("claude-"):
        return "anthropic"
    if ":" in model:
        return "ollama"
    if "/" in model:
        return "openrouter"
    return "anthropic"


def _backend_available(backend: str, config: "CiaoConfig") -> bool:
    if backend == "openrouter":
        return config.openrouter.available
    if backend == "ollama":
        oll = config.ollama
        return bool(oll.local_models) or (bool(oll.api_key) and oll.api_key != "ollama")
    return True  # anthropic is always considered available


def resolve_with_fallback(
    model: str, config: "CiaoConfig", *, default_model: str = ""
) -> tuple[str, dict[str, str], str | None]:
    """Resolve a model to an (effective_model, env, note) triple.

    When the model's intended backend isn't configured, fall back to an
    available backend and return a human-readable ``note`` explaining the
    fallback (suitable for logging into ``job_runs``). When no fallback
    is needed, ``note`` is ``None``.
    """
    backend = intended_backend(model)
    if _backend_available(backend, config):
        return model, routing_routine_env_for_model(model, config), None

    # Fall back to the first available backend, preferring the operator's
    # default_model if it lands on an available backend.
    fallback = default_model
    if not fallback or not _backend_available(intended_backend(fallback), config):
        if _backend_available("openrouter", config):
            fallback = config.openrouter.sonnet_model
        elif _backend_available("ollama", config):
            fallback = config.ollama.sonnet_model
        else:
            fallback = "sonnet"
    env = routing_routine_env_for_model(fallback, config)
    note = f"fell back to {fallback} because {backend} not configured"
    return fallback, env, note
