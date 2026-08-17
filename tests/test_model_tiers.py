from ciao.model_tiers import canonical_tier, is_tier


def test_tier_names_are_the_claude_families() -> None:
    for name in ("haiku", "sonnet", "opus", "fable"):
        assert is_tier(name)
        assert canonical_tier(name.upper()) == name
    assert not is_tier("gpt-5.6-terra")
