"""Tests for RateLimitStore persistence."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ciao.rate_limits import (
    RateLimitStore,
    default_store_path,
    is_rate_limit_telemetry,
)


@dataclass
class _FakeInfo:
    """Stand-in for claude_agent_sdk.types.RateLimitInfo."""

    rate_limit_type: str
    status: str = "allowed"
    utilization: float | None = 0.5
    resets_at: int | None = 1_700_000_000
    overage_status: str | None = None
    overage_resets_at: int | None = None
    overage_disabled_reason: str | None = None


def test_default_path_goes_under_runtime_root(tmp_path: Path) -> None:
    assert default_store_path(tmp_path) == tmp_path / "rate_limits.json"


def test_update_persists_per_bucket(tmp_path: Path) -> None:
    store = RateLimitStore(path=tmp_path / "rate_limits.json")
    store.update(_FakeInfo(rate_limit_type="five_hour", utilization=0.72))
    store.update(_FakeInfo(rate_limit_type="seven_day_opus", utilization=0.44))
    payload = store.load()
    assert set(payload["buckets"].keys()) == {"five_hour", "seven_day_opus"}
    assert payload["buckets"]["five_hour"]["utilization"] == 0.72
    assert payload["buckets"]["seven_day_opus"]["utilization"] == 0.44
    assert payload["last_updated"] is not None


def test_update_overwrites_same_bucket(tmp_path: Path) -> None:
    store = RateLimitStore(path=tmp_path / "rate_limits.json")
    store.update(_FakeInfo(rate_limit_type="five_hour", utilization=0.10))
    store.update(_FakeInfo(rate_limit_type="five_hour", utilization=0.90))
    payload = store.load()
    assert payload["buckets"]["five_hour"]["utilization"] == 0.90
    assert len(payload["buckets"]) == 1


def test_update_skips_info_without_type(tmp_path: Path) -> None:
    store = RateLimitStore(path=tmp_path / "rate_limits.json")
    store.update(_FakeInfo(rate_limit_type=""))
    store.update(object())  # not a RateLimitInfo-like thing
    assert store.load()["buckets"] == {}


def test_load_missing_returns_empty_payload(tmp_path: Path) -> None:
    store = RateLimitStore(path=tmp_path / "nonexistent.json")
    payload = store.load()
    assert payload == {"buckets": {}, "last_updated": None}


def test_rate_limit_telemetry_matches_only_status_prefix() -> None:
    assert is_rate_limit_telemetry("Rate limit: allowed (five_hour)")
    assert is_rate_limit_telemetry("  Rate limit: rejected (five_hour)")
    assert not is_rate_limit_telemetry("Rate limit exceeded (five_hour)")
    assert not is_rate_limit_telemetry("Error: Rate limit exceeded (five_hour)")
    assert not is_rate_limit_telemetry("A note about Rate limit behavior")
