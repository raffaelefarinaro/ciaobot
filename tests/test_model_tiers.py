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
