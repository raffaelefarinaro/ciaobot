"""Regression guard for #3670: the scheduler needs a tz database without the OS.

The installer pins uv's standalone Python, which carries no zoneinfo data, and
minimal containers have no /usr/share/zoneinfo. `ZoneInfo("UTC")` then raises
on every scheduler tick unless the `tzdata` package is installed. The decision
is to depend on `tzdata` (PyPA-maintained) rather than add a fallback.
"""

from __future__ import annotations

import tomllib
import zoneinfo
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_tzdata_is_a_pinned_runtime_dependency() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    pins = [d for d in project["dependencies"] if d.lower().startswith("tzdata")]
    assert len(pins) == 1 and pins[0].startswith("tzdata=="), pins
    lock = (ROOT / "uv.lock").read_text()
    assert 'name = "tzdata"' in lock
    assert '{ name = "tzdata", specifier = "' + pins[0].removeprefix("tzdata") in lock


def test_utc_resolves_from_the_package_without_system_zoneinfo() -> None:
    original = zoneinfo.TZPATH
    zoneinfo.reset_tzpath(to=[])  # simulate a host with no /usr/share/zoneinfo
    try:
        zone = zoneinfo.ZoneInfo.no_cache("UTC")
        assert str(zone) == "UTC"
        from ciao.schedules import DEFAULT_TIMEZONE

        assert str(zoneinfo.ZoneInfo.no_cache(DEFAULT_TIMEZONE)) == DEFAULT_TIMEZONE
    except zoneinfo.ZoneInfoNotFoundError:
        pytest.fail("UTC is not resolvable without system tzdata; tzdata package missing")
    finally:
        zoneinfo.reset_tzpath(to=list(original))
