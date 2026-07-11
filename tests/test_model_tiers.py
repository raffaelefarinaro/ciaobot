from ciao.model_tiers import (
    canonical_tier,
    codex_tier_models,
    is_tier,
    tier_model,
)


def test_tier_names_are_the_claude_families() -> None:
    for name in ("haiku", "sonnet", "opus", "fable"):
        assert is_tier(name)
        assert canonical_tier(name.upper()) == name
    assert not is_tier("gpt-5.6-terra")


def test_provider_model_resolution_passes_through_non_tiers() -> None:
    values = {"haiku": "small", "sonnet": "medium", "opus": "large"}
    assert tier_model("haiku", **values) == "small"
    assert tier_model("Sonnet", **values) == "medium"
    assert tier_model("fable", **values) == "large"
    assert tier_model("gpt-5.6-sol", **values) == "gpt-5.6-sol"


def test_codex_tiers_match_openai_family_names() -> None:
    catalog = [
        {"model": "gpt-5.6-terra", "isDefault": True},
        {"model": "gpt-5.6-sol"},
        {"model": "gpt-5.6-luna"},
    ]
    assert codex_tier_models(catalog) == {
        "haiku": "gpt-5.6-luna",
        "sonnet": "gpt-5.6-terra",
        "opus": "gpt-5.6-sol",
        "fable": "gpt-5.6-sol",
    }


def test_codex_tiers_fall_back_to_catalog_heuristics() -> None:
    catalog = [
        {"model": "gpt-pro", "isDefault": True},
        {"model": "gpt-standard"},
        {"model": "gpt-mini"},
    ]
    assert codex_tier_models(catalog) == {
        "haiku": "gpt-mini",
        "sonnet": "gpt-standard",
        "opus": "gpt-pro",
        "fable": "gpt-pro",
    }


def test_codex_tiers_converge_on_sparse_catalogs() -> None:
    assert codex_tier_models([{"model": "gpt-only", "isDefault": True}]) == {
        "haiku": "gpt-only",
        "sonnet": "gpt-only",
        "opus": "gpt-only",
        "fable": "gpt-only",
    }
    assert codex_tier_models([]) == {}
