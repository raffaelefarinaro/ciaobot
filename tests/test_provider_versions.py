from __future__ import annotations

import copy
import tomllib
from pathlib import Path

import pytest

from ciao import provider_versions
from ciao.provider_versions import (
    STABLE_VERSION_RE,
    ProviderRecordError,
    compare_versions,
    load_record,
    make_cached_checks,
    release_check,
    update_command,
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


RUNNING = "1.6.1"


def _untested() -> dict:
    record = _tested()
    record["recorded"] = ""
    record["providers"] = {
        "opencode": {"status": "untested", "reason": "not_recorded"},
        "claude": {"status": "untested", "reason": "not_recorded"},
    }
    return record


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("2.0.32", "2.1.0", -1),
        ("2.1.0", "2.1.0", 0),
        ("2.1.1", "2.1.0", 1),
        ("2.1.0-beta.1", "2.1.0", -1),
        ("2.1.0-beta.2", "2.1.0-beta.10", -1),
        ("2.1.0-beta.1", "2.1.0-beta", 1),
        ("2.1", "2.1.0", None),
        ("x", "2.1.0", None),
    ],
)
def test_compare_versions(a: str, b: str, expected: int | None) -> None:
    assert compare_versions(a, b) == expected


@pytest.fixture
def terminal_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ciao.tool_path.resolve_on_terminal_path", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr("ciao.host_terminal.host_platform", lambda: "macos")


@pytest.mark.parametrize(
    ("provider", "installed", "kwargs", "record", "state"),
    [
        ("opencode", "2.0.32", {}, _untested(), "untested"),
        ("claude", "2.1.0", {"bundled": True}, _tested(), "bundled"),
        ("opencode", "1.14.0", {}, _tested(), "unsupported"),
        ("opencode", "2.0.15", {}, _tested(), "unsupported"),
        ("opencode", "3.0.0", {}, _tested(), "unsupported"),
        ("opencode", "garbage", {}, _tested(), "unsupported"),
        ("claude", "garbage", {}, _tested(), "unknown"),
        ("opencode", "2.0.32", {}, _tested(), "below_tested"),
        ("opencode", "2.1.0", {}, _tested(), "current"),
        ("opencode", "2.2.0", {}, _tested(), "current"),
        ("claude", "2.1.0 (Claude Code)", {}, _tested(), "below_tested"),
        ("claude", "2.1.301 (Claude Code)", {}, _tested(), "current"),
    ],
)
def test_release_check_states(
    terminal_paths: None,
    provider: str,
    installed: str,
    kwargs: dict,
    record: dict,
    state: str,
) -> None:
    result = release_check(
        provider, installed, binary="/x/bin", record=record, running_version=RUNNING, **kwargs
    )
    assert result["state"] == state
    if state in {"bundled", "unsupported", "unknown"}:
        assert result["tested"] == record["providers"][provider]["version"]
        assert "command" not in result
    if state == "below_tested":
        assert result["terminal_action"] == f"{provider}-update"
        assert result["installed"] and result["tested"]
        assert result["command"].endswith(
            "upgrade 2.1.0" if provider == "opencode" else "update"
        )
    if state == "current":
        assert set(result) == {"state", "installed", "tested"}
    if state == "untested":
        assert result == {"state": "untested"}


def test_release_check_is_untested_when_the_record_is_from_another_version(
    terminal_paths: None,
) -> None:
    # e.g. a release cut with --skip-provider-versions keeps the previous record.
    for running in ("1.6.2", "1.7.0"):
        assert release_check(
            "opencode", "2.0.32", binary="/x", record=_tested(), running_version=running
        ) == {"state": "untested"}


@pytest.mark.parametrize("tested", ["1.14.0", "2.0.15", "3.0.0"])
def test_release_check_is_untested_when_the_tested_opencode_is_outside_the_contract(
    terminal_paths: None, tested: str
) -> None:
    record = _tested()
    record["providers"]["opencode"]["version"] = tested
    assert release_check(
        "opencode", "2.0.32", binary="/x", record=record, running_version=RUNNING
    ) == {"state": "untested"}


def test_release_check_defaults_to_the_running_ciaobot_version(
    terminal_paths: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ciao.__version__", "1.6.1")
    assert release_check("opencode", "2.0.32", binary="/x", record=_tested())["state"] == "below_tested"
    monkeypatch.setattr("ciao.__version__", "1.6.2")
    assert release_check("opencode", "2.0.32", binary="/x", record=_tested()) == {"state": "untested"}


def test_release_check_survives_a_broken_record(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> dict:
        raise ProviderRecordError("provider record: broken")

    monkeypatch.setattr(provider_versions, "load_record", boom)
    assert release_check("opencode", "2.0.32", running_version=RUNNING) == {"state": "untested"}


def test_update_command_forms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ciao.host_terminal.host_platform", lambda: "macos")
    monkeypatch.setattr("ciao.tool_path.resolve_on_terminal_path", lambda cmd: "/usr/bin/" + cmd)
    monkeypatch.setattr("os.path.samefile", lambda a, b: True)
    assert update_command("opencode", "2.1.0", "/usr/bin/opencode") == "opencode upgrade 2.1.0"
    assert update_command("claude", "2.1.301", "/usr/bin/claude") == "claude update"

    # The terminal resolves a different binary than the one Ciaobot runs.
    monkeypatch.setattr("os.path.samefile", lambda a, b: False)
    assert update_command("opencode", "2.1.0", "/opt/oc/opencode") == "/opt/oc/opencode upgrade 2.1.0"
    monkeypatch.setattr("os.path.samefile", lambda a, b: (_ for _ in ()).throw(OSError("gone")))
    assert update_command("claude", "2.1.301", "/opt/c/claude") == "/opt/c/claude update"

    monkeypatch.setattr("ciao.tool_path.resolve_on_terminal_path", lambda cmd: None)
    spaced = "/Users/a b/bin/opencode"
    assert update_command("opencode", "2.1.0", spaced) == f"'{spaced}' upgrade 2.1.0"
    assert update_command("claude", "2.1.301", spaced) == f"'{spaced}' update"

    monkeypatch.setattr("ciao.host_terminal.host_platform", lambda: "windows")
    win = "C:\\Users\\o'brien\\opencode.exe"
    assert update_command("opencode", "2.1.0", win) == "& 'C:\\Users\\o''brien\\opencode.exe' upgrade 2.1.0"
    assert update_command("claude", "2.1.301", win).endswith("' update")


@pytest.mark.parametrize("tested", ["2.1.0; rm -rf /", "2.1", "$(x)", "2.1.0\n", ""])
def test_update_command_rejects_unvalidated_versions(tested: str) -> None:
    with pytest.raises(ValueError):
        update_command("opencode", tested, "/x/opencode")


def test_update_command_rejects_an_empty_binary(terminal_paths: None) -> None:
    with pytest.raises(ValueError):
        update_command("opencode", "2.1.0", "")


def test_update_command_rejects_unknown_providers() -> None:
    with pytest.raises(ValueError):
        update_command("rm", "2.1.0", "/x")


def test_cached_checks_ttl_and_invalidate(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def fake() -> dict:
        calls.append(1)
        return {"opencode": {"state": "current"}}

    monkeypatch.setattr(provider_versions, "current_checks", fake)
    now = [0.0]
    cached = make_cached_checks(ttl=60.0, clock=lambda: now[0])
    assert cached() == {"opencode": {"state": "current"}}
    now[0] = 59.0
    cached()
    assert len(calls) == 1
    now[0] = 61.0
    cached()
    assert len(calls) == 2
    cached.invalidate()
    cached()
    assert len(calls) == 3
