"""The rate-limit telemetry predicate that keeps status pings out of chat history."""

from ciao.rate_limits import is_rate_limit_telemetry


def test_status_pings_are_telemetry() -> None:
    assert is_rate_limit_telemetry("Rate limit: allowed (five_hour)")
    assert is_rate_limit_telemetry("  Rate limit: rejected (five_hour)")


def test_a_hard_limit_error_is_not_telemetry() -> None:
    assert not is_rate_limit_telemetry("Rate limit exceeded (five_hour)")
    assert not is_rate_limit_telemetry("Error: Rate limit exceeded (five_hour)")
    assert not is_rate_limit_telemetry("A note about Rate limit behavior")
