"""Model capability tiers shared across providers.

Ciaobot uses Claude Code's family names — haiku / sonnet / opus (and
fable) — as the provider-neutral tier vocabulary. Every provider maps
those names onto its own models, so schedules, routines, and chats can
say "sonnet" regardless of which backend serves the request.
"""

from __future__ import annotations

MODEL_TIERS = ("haiku", "sonnet", "opus", "fable")


def canonical_tier(value: str) -> str:
    """Normalize a tier name; non-tier values pass through unchanged."""
    return (value or "").strip().lower()


def is_tier(value: str) -> bool:
    return canonical_tier(value) in MODEL_TIERS
