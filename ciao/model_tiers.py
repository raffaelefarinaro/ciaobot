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
