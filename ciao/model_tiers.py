"""Model capability tiers shared across providers.

Ciaobot uses Claude Code's family names — haiku / sonnet / opus (and
fable) — as the provider-neutral tier vocabulary. Every provider maps
those names onto its own models, so schedules, routines, and chats can
say "sonnet" regardless of which backend serves the request.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

MODEL_TIERS = ("haiku", "sonnet", "opus", "fable")

_WORD_BREAK = re.compile(r"[-/_.\[\]\s]+")


def canonical_tier(value: str) -> str:
    """Normalize a tier name; non-tier values pass through unchanged."""
    return (value or "").strip().lower()


def is_tier(value: str) -> bool:
    return canonical_tier(value) in MODEL_TIERS


def resolve_tier(alias: str, offered: Sequence[str]) -> str | None:
    """The offered model id that a tier alias names, or None.

    A tier matches an offered id that has the tier as one of its words, so
    ``opus`` matches ``claude-opus-4-8`` but not ``claude-opusx``. Among the
    matches the first of these wins:

    1. an offered id equal to the tier name (``opus``);
    2. the first id without a bracketed variant (``claude-opus-4-8`` over
       ``claude-opus-4-8[1m]``);
    3. the first match in offered order (a variant, when only variants are offered).

    A value that is not a tier resolves to None: a concrete id is matched exactly by
    the caller, and is never mapped to a tier.
    """
    tier = canonical_tier(alias)
    if tier not in MODEL_TIERS:
        return None
    matches = [model for model in offered if tier in _WORD_BREAK.split(model.lower())]
    for model in matches:
        if model.lower() == tier:
            return model
    for model in matches:
        if "[" not in model:
            return model
    return matches[0] if matches else None
