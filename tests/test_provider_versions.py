from __future__ import annotations

import copy
import tomllib
from pathlib import Path

import pytest

from ciao.provider_versions import (
    STABLE_VERSION_RE,
    ProviderRecordError,
    load_record,
    parse_cli_version,
    validate_record,
)

from ciao.provider_versions import tested_version as _tested_version

REPO = Path(__file__).resolve().parent.parent


def _tested() -> dict:
    return {
        "schema": 1,
        "ciaobot": "1.6.1",
        "recorded": "2026-10-20",
        "providers": {
            "opencode": {
                "status": "tested",
                "version": "2.1.0",
                "package": "@opencode/cli",
            },
            "claude": {
                "status": "tested",
                "version": "2.1.301",
                "source": "claude-agent-sdk 0.2.168",
            },
        },
    }


def test_shipped_record_is_valid() -> None:
    record = load_record()
    assert validate_record(record) is record
    for provider in ("opencode", "claude"):
        version = _tested_version(record, provider)
        assert version is None or STABLE_VERSION_RE.fullmatch(version)


def test_untested_seed_shape_is_valid() -> None:
    seed = {
        "schema": 1,
        "ciaobot": "1.6.0",
        "recorded": "",
        "providers": {
            "opencode": {"status": "untested", "reason": "not_recorded"},
            "claude": {"status": "untested", "reason": "not_recorded"},
        },
    }
    assert validate_record(seed) is seed
    assert _tested_version(seed, "opencode") is None
    assert _tested_version(seed, "claude") is None


def test_tested_record_round_trips() -> None:
    record = _tested()
    assert validate_record(record) is record
    assert _tested_version(record, "opencode") == "2.1.0"
    assert _tested_version(record, "claude") == "2.1.301"


def test_pyproject_packages_provider_record() -> None:
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert "provider-versions.json" in data["tool"]["setuptools"]["package-data"]["ciao.stock"]


def _mutate(fn):
    record = copy.deepcopy(_tested())
    fn(record)
    return record


@pytest.mark.parametrize(
    "record",
    [
        _mutate(lambda r: r["providers"].pop("claude")),
        _mutate(lambda r: r.update(extra=1)),
        _mutate(lambda r: r["providers"].update(other={"status": "untested"})),
        _mutate(lambda r: r["providers"]["opencode"].pop("version")),
        _mutate(
            lambda r: r["providers"]["opencode"].update(
                status="untested", reason="not_installed"
            )
        ),
        _mutate(lambda r: r["providers"]["opencode"].update(version="2.1")),
        _mutate(lambda r: r["providers"]["opencode"].update(version="2.1.0-beta.1")),
        _mutate(
            lambda r: r["providers"]["opencode"].update(
                status="untested", reason="bogus"
            )
            or r["providers"]["opencode"].pop("version")
        ),
        _mutate(lambda r: r.update(recorded="")),
        _mutate(lambda r: r["providers"]["opencode"].update(version="2.1.0\n")),
        _mutate(lambda r: r.update(recorded="yesterday")),
        _mutate(lambda r: r.update(schema=True)),
        _mutate(lambda r: r.update(schema=1.0)),
        "not a dict",
    ],
)
def test_validate_record_rejects_bad_shapes(record) -> None:
    with pytest.raises(ProviderRecordError):
        validate_record(record)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("opencode v2.0.32", "2.0.32"),
        ("2.1.301 (Claude Code)", "2.1.301"),
        ("0.0.0-dev-21296", "0.0.0-dev-21296"),
        ("garbage", None),
    ],
)
def test_parse_cli_version(text: str, expected: str | None) -> None:
    assert parse_cli_version(text) == expected
