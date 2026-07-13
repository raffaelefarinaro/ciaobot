"""Model capability tiers shared across providers.

Ciaobot uses Claude Code's family names — haiku / sonnet / opus (and
fable) — as the provider-neutral tier vocabulary. Every provider maps
those names onto its own models, so schedules, routines, and chats can
say "sonnet" regardless of which backend serves the request.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

MODEL_TIERS = ("haiku", "sonnet", "opus", "fable")
CODEX_FABLE_THINKING_LEVEL = "ultra"

# OpenAI ships tiered model families whose names line up with Claude's:
# luna is the fast/affordable tier, terra the balanced everyday model,
# sol the flagship (e.g. gpt-5.6-luna / gpt-5.6-terra / gpt-5.6-sol).
CODEX_TIER_FAMILIES = {
    "haiku": "luna",
    "sonnet": "terra",
    "opus": "sol",
    "fable": "sol",
}


def canonical_tier(value: str) -> str:
    """Normalize a tier name; non-tier values pass through unchanged."""
    return (value or "").strip().lower()


def is_tier(value: str) -> bool:
    return canonical_tier(value) in MODEL_TIERS


def tier_model(value: str, *, haiku: str, sonnet: str, opus: str, fable: str = "") -> str:
    """Resolve a tier name to a provider model id; other values pass through."""
    return {
        "haiku": haiku,
        "sonnet": sonnet,
        "opus": opus,
        "fable": fable or opus,
    }.get(canonical_tier(value), value)


def _name_segments(model_id: str) -> set[str]:
    return set(re.split(r"[-_./:@ ]+", model_id.lower()))


def codex_tier_models(
    catalog: Sequence[Mapping[str, object]],
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Map Ciaobot's tiers to Codex models visible to the account.

    Matches by family name first (haiku→luna, sonnet→terra, opus→sol),
    then falls back to catalog heuristics — compact names for haiku, the
    catalog default for opus — so a renamed catalog still resolves.
    Sparse catalogs intentionally converge on the nearest available model.

    ``overrides`` maps tier names to operator-pinned model ids. A pin
    wins only while its model is still visible in the catalog, so a
    stale override falls back to the automatic mapping instead of
    sending an unknown model id to the app-server.
    """
    visible = [item for item in catalog if not item.get("hidden")]
    ids = [
        str(item.get("model") or item.get("id") or "")
        for item in visible
        if str(item.get("model") or item.get("id") or "")
    ]
    if not ids:
        return {}

    def by_family(tier: str) -> str:
        family = CODEX_TIER_FAMILIES[tier]
        return next((m for m in ids if family in _name_segments(m)), "")

    default = next(
        (
            str(item.get("model") or item.get("id") or "")
            for item in visible
            if item.get("isDefault")
        ),
        ids[0],
    )
    flagship = by_family("opus") or default
    compact = by_family("haiku") or next(
        (m for m in ids if "mini" in m.lower() or "nano" in m.lower()),
        "",
    )
    standard = by_family("sonnet") or next(
        (
            m
            for m in ids
            if m != flagship
            and "mini" not in m.lower()
            and "nano" not in m.lower()
        ),
        flagship,
    )
    tiers = {
        "haiku": compact or standard,
        "sonnet": standard,
        "opus": flagship,
        "fable": flagship,
    }
    for tier, pinned in (overrides or {}).items():
        tier = canonical_tier(tier)
        pinned = (pinned or "").strip()
        if tier in tiers and pinned in ids:
            tiers[tier] = pinned
    return tiers


# Tier ordering used by the auto-fallback ladder. ``fable`` is the most
# capable slot, ``haiku`` the cheapest. Walking in either direction picks
# the nearest neighbor on the ladder, so a failing primary is retried
# against the closest tier that is more or less capable.
_TIER_ORDER: tuple[str, ...] = ("haiku", "sonnet", "opus", "fable")

# Suffixes / patterns in error text that indicate a model is incapable of
# handling the input (not rate-limited, not auth-failed, not a backend bug).
# The full 4xx/5xx text still propagates to logs at WARNING before the
# retry is attempted, so nothing is hidden — only the *trigger* for the
# auto-retry is narrow.
_CAPABILITY_ERROR_PATTERNS: tuple[str, ...] = (
    "does not support image input",
    "does not support image",
    "does not support tool",
    "does not support function",
    "unsupported capability",
    "context length exceeded",
    "max context length",
    "context_length_exceeded",
)


def tier_order() -> tuple[str, ...]:
    """Return the tier ladder from cheapest to most capable."""
    return _TIER_ORDER


def _tier_slot_for_model(model: str) -> str | None:
    """Return which tier slot ``model`` corresponds to, or None.

    Matches both bare aliases (``opus``, ``fable``) and the per-backend
    configured model ids for Ollama/OpenRouter. Used by
    :func:`next_tier_for_failure` to find the failing model's position on
    the ladder.
    """
    if not model:
        return None
    low = model.lower().strip()
    if low in _TIER_ORDER:
        return low
    return None


def _tier_slot_for_configured(
    model: str, config: object, backend: str
) -> str | None:
    """Match a configured per-backend model id to its tier slot.

    Looks up the failing model against the operator-configured per-tier
    ids for the resolved backend (Ollama, OpenRouter). Returns the tier
    name (``"haiku"``/``"sonnet"``/...) when found, ``None`` otherwise.
    """
    if not model or not config:
        return None
    settings = getattr(config, backend, None) if backend in ("ollama", "openrouter") else None
    if settings is None:
        return None
    for tier in _TIER_ORDER:
        configured = getattr(settings, f"{tier}_model", "")
        if configured and configured == model:
            return tier
    return None


def is_capability_error(result_text: str) -> bool:
    """True when the error text describes a model capability gap.

    Narrow by design: only matches patterns that mean the model itself
    cannot handle the input (no image support, no tool use, context
    overflow). Rate limits, auth failures, content filters, and generic
    5xx errors are NOT matched — they need operator attention, not
    silent retry against the next tier.
    """
    if not result_text:
        return False
    text = result_text.lower()
    return any(needle in text for needle in _CAPABILITY_ERROR_PATTERNS)


def next_tier_for_failure(model: str, config: object) -> str | None:
    """Pick the next model id to try when ``model`` failed with a capability error.

    Walks the tier ladder in both directions, picking the nearest neighbor
    with a preference for the cheaper slot:
    ``fable`` fails → ``opus``; ``opus`` fails → ``sonnet``; ``sonnet``
    fails → ``haiku``; ``haiku`` fails → ``sonnet`` (escalate, the only
    available direction). Returns the resolved model id for the caller's
    intended backend, or ``None`` when the failing model isn't on the
    ladder (e.g. a one-off id with no other tier configured).

    ``config`` is the runtime :class:`ciao.config.CiaoConfig`; the helper
    reads ``config.ollama`` and ``config.openrouter`` for per-backend tier
    resolution and falls back to bare tier names for the Anthropic path.
    A backend with fewer than two distinct tier slots returns ``None`` so
    a single-model config does not retry the failing model against itself.
    """
    # Detect intended backend from the failing model's shape. Defaults to
    # anthropic so a bare-alias caller still walks the ladder.
    from ciao.providers.routing import intended_backend
    backend = intended_backend(model)

    # Match the failing model to a tier slot. First try the bare-alias
    # fast path (works for Anthropic-direct and bare tier names), then
    # fall back to the configured per-backend model id lookup (Ollama
    # and OpenRouter use concrete model ids, not the bare aliases).
    slot = _tier_slot_for_model(model) or _tier_slot_for_configured(
        model, config, backend
    )
    if slot is None:
        return None
    idx = _TIER_ORDER.index(slot)

    # Pick the nearest neighbor. Default order is "down" (cheaper tier
    # first), then "up" (more capable). The cheaper slot is the natural
    # fallback for capability errors (e.g. a non-vision model could not
    # read the image, so we try a different non-vision model that's
    # cheaper and known to support images). The up direction is the
    # safety net for the bottom of the ladder: a failing haiku can only
    # escalate to sonnet.
    candidates: list[str] = []
    for delta in (-1, 1):
        neighbor_idx = idx + delta
        if 0 <= neighbor_idx < len(_TIER_ORDER):
            candidates.append(_TIER_ORDER[neighbor_idx])

    # Resolve each candidate to a model id under the caller's intended
    # backend. Bare aliases and Claude-direct ids pass through as-is.
    ollama = getattr(config, "ollama", None)
    openrouter = getattr(config, "openrouter", None)

    def _resolve(backend: str, tier: str) -> str:
        if backend == "ollama" and ollama is not None:
            return tier_model(
                tier,
                haiku=getattr(ollama, "haiku_model", "haiku"),
                sonnet=getattr(ollama, "sonnet_model", "sonnet"),
                opus=getattr(ollama, "opus_model", "opus"),
                fable=getattr(ollama, "fable_model", "fable"),
            )
        if backend == "openrouter" and openrouter is not None:
            return tier_model(
                tier,
                haiku=getattr(openrouter, "haiku_model", "haiku"),
                sonnet=getattr(openrouter, "sonnet_model", "sonnet"),
                opus=getattr(openrouter, "opus_model", "opus"),
                fable=getattr(openrouter, "fable_model", "fable"),
            )
        return tier  # anthropic-direct: bare alias

    for tier in candidates:
        candidate = _resolve(backend, tier)
        if candidate and candidate != model:
            return candidate
    return None
