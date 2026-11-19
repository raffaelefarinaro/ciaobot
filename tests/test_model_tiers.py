from ciao.model_tiers import canonical_tier, is_tier


def test_tier_names_are_the_claude_families() -> None:
    for name in ("haiku", "sonnet", "opus", "fable"):
        assert is_tier(name)
        assert canonical_tier(name.upper()) == name
    assert not is_tier("gpt-5.6-terra")


def test_resolve_tier_names_the_offered_model_with_that_word() -> None:
    from ciao.model_tiers import resolve_tier

    offered = ["default", "claude-sonnet-4-6", "claude-opus-4-8"]
    assert resolve_tier("opus", offered) == "claude-opus-4-8"
    assert resolve_tier("SONNET", offered) == "claude-sonnet-4-6"


def test_resolve_tier_matches_a_whole_word_only() -> None:
    from ciao.model_tiers import resolve_tier

    assert resolve_tier("opus", ["claude-opusx-1"]) is None
    assert resolve_tier("sonnet", ["sonnet[1m]"]) == "sonnet[1m]"


def test_resolve_tier_takes_the_first_offered_match() -> None:
    from ciao.model_tiers import resolve_tier

    assert resolve_tier("opus", ["claude-opus-4-7", "claude-opus-4-8"]) == "claude-opus-4-7"


def test_resolve_tier_leaves_non_tiers_to_the_caller() -> None:
    from ciao.model_tiers import resolve_tier

    assert resolve_tier("gpt-5.6-terra", ["gpt-5.6-terra"]) is None
    assert resolve_tier("opus", []) is None


def test_resolve_tier_prefers_the_exact_tier_id_over_a_variant() -> None:
    from ciao.model_tiers import resolve_tier

    assert resolve_tier("opus", ["opus[1m]", "opus"]) == "opus"
    # The exact tier id beats a plain full id that also names the tier.
    assert resolve_tier("opus", ["claude-opus-4-8", "opus"]) == "opus"


def test_resolve_tier_prefers_the_plain_id_over_a_bracket_variant() -> None:
    from ciao.model_tiers import resolve_tier

    assert resolve_tier("opus", ["claude-opus-4-8[1m]", "claude-opus-4-8"]) == "claude-opus-4-8"
    assert resolve_tier("sonnet", ["sonnet[1m]", "claude-sonnet-4-6"]) == "claude-sonnet-4-6"


def test_resolve_tier_falls_back_to_a_variant_when_only_variants_are_offered() -> None:
    from ciao.model_tiers import resolve_tier

    assert resolve_tier("sonnet", ["sonnet[1m]"]) == "sonnet[1m]"


def test_tier_of_model_reads_the_tier_word_of_a_full_id() -> None:
    from ciao.model_tiers import tier_of_model

    assert tier_of_model("claude-haiku-4-5") == "haiku"
    assert tier_of_model("claude-opus-4-8[1m]") == "opus"
    assert tier_of_model("gpt-5.6-terra") is None
    assert tier_of_model("claude-hiaku-4-5") is None


def test_resolve_model_keeps_an_exact_offered_id() -> None:
    from ciao.model_tiers import resolve_model

    assert resolve_model("claude-opus-4-7", ["claude-opus-4-7", "claude-opus-4-8"]) == "claude-opus-4-7"


def test_resolve_model_maps_a_legacy_full_id_to_its_offered_tier() -> None:
    from ciao.model_tiers import resolve_model

    offered = ["claude-sonnet-4-6", "claude-opus-4-8"]
    assert resolve_model("claude-opus-4-1", offered) == "claude-opus-4-8"
    assert resolve_model("opus", offered) == "claude-opus-4-8"


def test_resolve_model_returns_none_when_the_tier_is_not_offered() -> None:
    from ciao.model_tiers import resolve_model

    assert resolve_model("claude-haiku-4-5", ["claude-sonnet-4-6"]) is None
    assert resolve_model("claude-hiaku-4-5", ["claude-sonnet-4-6"]) is None
