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

    Matches bare aliases only (``opus``, ``fable``, ...). A concrete provider
    model id has no place on the ladder -- ``claude-opus-4-8`` returns None --
    so the capability fallback applies to tier-pinned chats and leaves an
    explicitly pinned model alone.
    """
    if not model:
        return None
    low = model.lower().strip()
    if low in _TIER_ORDER:
        return low
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


def next_tier_for_failure(model: str) -> str | None:
    """Pick the next tier to try when ``model`` failed with a capability error.

    Walks the tier ladder in both directions, preferring the cheaper slot:
    ``fable`` fails → ``opus``; ``opus`` → ``sonnet``; ``sonnet`` → ``haiku``;
    ``haiku`` → ``sonnet`` (escalate, the only direction left). Returns
    ``None`` when the failing model is not a bare tier alias, so a chat pinned
    to a concrete model id is never silently swapped to a different one.

    Tier aliases are provider-agnostic: whichever provider runs the chat
    resolves the returned alias against its own catalog.
    """
    slot = _tier_slot_for_model(model)
    if slot is None:
        return None
    idx = _TIER_ORDER.index(slot)
    # Cheaper first: a capability error usually means "this model cannot do
    # that", and the neighbour below is the natural retry. Escalating is the
    # safety net for the bottom of the ladder.
    for delta in (-1, 1):
        neighbor = idx + delta
        if 0 <= neighbor < len(_TIER_ORDER):
            candidate = _TIER_ORDER[neighbor]
            if candidate != model:
                return candidate
    return None
