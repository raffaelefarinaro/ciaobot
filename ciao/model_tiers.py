"""Provider-neutral model capability tiers and legacy alias migration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

MODEL_TIERS = ("river", "lake", "sea", "ocean")

LEGACY_TO_TIER = {
    "haiku": "river",
    "sonnet": "lake",
    "opus": "sea",
    "fable": "ocean",
}
TIER_TO_CLAUDE = {value: key for key, value in LEGACY_TO_TIER.items()}
TIER_TO_CODEX_ALIAS = {
    "river": "luna",
    "lake": "terra",
    "sea": "sol",
}


def canonical_tier(value: str) -> str:
    """Return a canonical tier for either a new or legacy alias."""
    normalized = (value or "").strip().lower()
    if normalized in MODEL_TIERS:
        return normalized
    return LEGACY_TO_TIER.get(normalized, normalized)


def claude_alias(value: str) -> str:
    """Resolve a canonical tier to the alias understood by Claude Code."""
    tier = canonical_tier(value)
    return TIER_TO_CLAUDE.get(tier, value)


def codex_alias(value: str) -> str:
    """Return Ciaobot's public Codex alias for a canonical tier."""
    tier = canonical_tier(value)
    return TIER_TO_CODEX_ALIAS.get(tier, value)


def tier_model(value: str, *, river: str, lake: str, sea: str, ocean: str = "") -> str:
    """Resolve either generation of tier name to a provider model id."""
    tier = canonical_tier(value)
    return {
        "river": river,
        "lake": lake,
        "sea": sea,
        "ocean": ocean or sea,
    }.get(tier, value)


def codex_tier_models(catalog: Sequence[Mapping[str, object]]) -> dict[str, str]:
    """Map Ciaobot's three Codex tiers to models visible to the account.

    Codex does not expose stable Luna/Terra/Sol model ids. Those are Ciaobot
    aliases, resolved from the live catalog: Luna prefers a compact model,
    Sol follows Codex's default, and Terra takes the next full-size option.
    Sparse catalogs intentionally converge on the nearest available model.
    """
    visible = [item for item in catalog if not item.get("hidden")]
    ids = [
        str(item.get("model") or item.get("id") or "")
        for item in visible
        if str(item.get("model") or item.get("id") or "")
    ]
    if not ids:
        return {}
    default = next(
        (
            str(item.get("model") or item.get("id") or "")
            for item in visible
            if item.get("isDefault")
        ),
        ids[0],
    )
    compact = next(
        (model for model in ids if "mini" in model.lower() or "nano" in model.lower()),
        "",
    )
    standard = next(
        (
            model
            for model in ids
            if model != default
            and "mini" not in model.lower()
            and "nano" not in model.lower()
        ),
        default,
    )
    return {"river": compact or standard, "lake": standard, "sea": default}
