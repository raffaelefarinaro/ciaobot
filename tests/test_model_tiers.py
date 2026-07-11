from ciao.model_tiers import (
    canonical_tier,
    claude_alias,
    codex_tier_models,
    tier_model,
)


def test_legacy_claude_names_migrate_to_public_tiers() -> None:
    assert canonical_tier("haiku") == "river"
    assert canonical_tier("sonnet") == "lake"
    assert canonical_tier("opus") == "sea"
    assert canonical_tier("fable") == "ocean"
    assert claude_alias("ocean") == "fable"


def test_provider_model_resolution_accepts_new_and_legacy_names() -> None:
    values = {"river": "small", "lake": "medium", "sea": "large"}
    assert tier_model("river", **values) == "small"
    assert tier_model("sonnet", **values) == "medium"
    assert tier_model("ocean", **values) == "large"


def test_codex_tiers_use_compact_standard_and_default_catalog_models() -> None:
    catalog = [
        {"model": "gpt-pro", "isDefault": True},
        {"model": "gpt-standard"},
        {"model": "gpt-mini"},
    ]
    assert codex_tier_models(catalog) == {
        "river": "gpt-mini",
        "lake": "gpt-standard",
        "sea": "gpt-pro",
    }
