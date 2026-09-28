"""The retired CIAO_WORKSPACES variable: ignored as a source, imported once."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from ciao.config import (
    LEGACY_GWS_PROFILE_IMPORT_MARKER,
    LEGACY_WORKSPACES_IMPORT_MARKER,
    CiaoConfig,
)


def _config(
    tmp_path: Path,
    legacy: object | None = None,
    gws_profile: str = "",
) -> CiaoConfig:
    env = {
        "PWA_AUTH_TOKEN": "test-token",
        "CIAO_WORKSPACE": str(tmp_path),
        "CIAO_RUNTIME_ROOT": str(tmp_path / ".runtime"),
    }
    if legacy is not None:
        env["CIAO_WORKSPACES"] = legacy if isinstance(legacy, str) else json.dumps(legacy)
    if gws_profile:
        env["GWS_PROFILE"] = gws_profile
    return CiaoConfig.from_env(env)


def _registry(tmp_path: Path) -> list[dict]:
    return json.loads((tmp_path / ".runtime" / "workspaces.json").read_text(encoding="utf-8"))


def _write_registry(tmp_path: Path, entries: list[dict]) -> None:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "workspaces.json").write_text(json.dumps(entries), encoding="utf-8")


def _connect_profile(tmp_path: Path, profile: str) -> None:
    directory = tmp_path / "secrets" / f"gws-{profile}"
    directory.mkdir(parents=True)
    (directory / "credentials.json").write_text(
        '{"refresh_token":"must-not-be-copied"}', encoding="utf-8"
    )


LEGACY = [
    {"name": "home", "default_provider": "opencode", "gws_profile": "personal"},
    {"name": "client", "disallowed_tools": ["Bash"]},
]


def test_env_variable_is_not_a_workspace_source(tmp_path: Path) -> None:
    _write_registry(tmp_path, [{"name": "alpha"}])

    config = _config(tmp_path, LEGACY)

    assert config.workspace_names() == ["alpha"]


def test_env_variable_alone_does_not_define_workspaces_before_import(tmp_path: Path) -> None:
    config = _config(tmp_path, LEGACY)

    # Only the bootstrap list; the variable is kept for the one-time import.
    assert "home" not in config.workspace_names()
    assert "client" not in config.workspace_names()
    assert config.legacy_workspaces_env


def test_import_when_registry_missing_replaces_bootstrap_list(tmp_path: Path) -> None:
    config = _config(tmp_path, LEGACY)

    imported = config.import_legacy_workspaces_env()

    assert imported == ["home", "client"]
    assert config.workspace_names() == ["home", "client"]
    entries = {e["name"]: e for e in _registry(tmp_path)}
    assert list(entries) == ["home", "client"]
    assert entries["home"]["default_provider"] == "opencode"
    assert entries["home"]["gws_profile"] == "personal"
    assert entries["client"]["disallowed_tools"] == ["Bash"]
    # None resolved to "every declared server denied" while env-only; keep
    # that instead of letting the allowlist seed widen it on the next start.
    assert entries["home"]["allowed_mcp_servers"] == []
    assert (tmp_path / ".runtime" / LEGACY_WORKSPACES_IMPORT_MARKER).is_file()

    reloaded = _config(tmp_path, LEGACY)
    assert reloaded.workspace_names() == ["home", "client"]


def test_import_never_overwrites_existing_registry_entries(tmp_path: Path) -> None:
    _write_registry(
        tmp_path,
        [{"name": "home", "default_provider": "claude", "gws_profile": "work",
          "allowed_mcp_servers": ["notion"]}],
    )
    config = _config(tmp_path, LEGACY)

    imported = config.import_legacy_workspaces_env()

    assert imported == ["client"]
    entries = {e["name"]: e for e in _registry(tmp_path)}
    assert list(entries) == ["home", "client"]
    assert entries["home"]["default_provider"] == "claude"
    assert entries["home"]["gws_profile"] == "work"
    assert entries["home"]["allowed_mcp_servers"] == ["notion"]


def test_import_is_idempotent_and_does_not_resurrect_removed_workspaces(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, LEGACY)
    assert config.import_legacy_workspaces_env() == ["home", "client"]

    # The owner removes "client" in Settings; the variable is still in .env.
    del config.workspaces["client"]
    config.persist_workspace_registry()

    again = _config(tmp_path, LEGACY)
    assert again.import_legacy_workspaces_env() == []
    assert [e["name"] for e in _registry(tmp_path)] == ["home"]
    assert again.workspace_names() == ["home"]


def test_repeated_imports_do_not_duplicate_entries(tmp_path: Path) -> None:
    for _ in range(3):
        _config(tmp_path, LEGACY).import_legacy_workspaces_env()

    assert [e["name"] for e in _registry(tmp_path)] == ["home", "client"]


def test_every_start_with_the_variable_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _config(tmp_path, LEGACY).import_legacy_workspaces_env()
    caplog.clear()

    with caplog.at_level(logging.WARNING, logger="ciao.config"):
        _config(tmp_path, LEGACY).import_legacy_workspaces_env()

    assert any("CIAO_WORKSPACES" in r.getMessage() for r in caplog.records)


def test_unset_variable_is_a_no_op(tmp_path: Path) -> None:
    _write_registry(tmp_path, [{"name": "alpha"}])
    config = _config(tmp_path)
    before = (tmp_path / ".runtime" / "workspaces.json").read_text(encoding="utf-8")

    assert config.import_legacy_workspaces_env() == []

    assert (tmp_path / ".runtime" / "workspaces.json").read_text(encoding="utf-8") == before
    assert not (tmp_path / ".runtime" / LEGACY_WORKSPACES_IMPORT_MARKER).exists()


def test_invalid_variable_imports_nothing_and_keeps_registry(tmp_path: Path) -> None:
    _write_registry(tmp_path, [{"name": "alpha"}])
    config = _config(tmp_path, "not json")

    assert config.import_legacy_workspaces_env() == []

    assert [e["name"] for e in _registry(tmp_path)] == ["alpha"]
    assert config.workspace_names() == ["alpha"]


def test_setup_rerun_imports_the_variable_before_writing_a_registry(tmp_path: Path) -> None:
    """The upgrade installer reruns setup before the new server first starts.

    Without importing first, setup would write a synthetic single-workspace
    registry and the server's import would then keep it over the variable's
    real entries.
    """
    from ciao.cli import setup_workspace

    (tmp_path / "memory-vault").mkdir()
    (tmp_path / ".env").write_text(
        "PWA_AUTH_TOKEN=t\n"
        "CIAO_WORKSPACE=.\n"
        "CIAO_VAULT_ROOT=memory-vault\n"
        "CIAO_RUNTIME_ROOT=.runtime\n"
        f"CIAO_WORKSPACES='{json.dumps(LEGACY)}'\n",
        encoding="utf-8",
    )

    setup_workspace(tmp_path, auth_token="t", auth_required=False)

    entries = {e["name"]: e for e in _registry(tmp_path)}
    assert list(entries) == ["home", "client"]
    assert entries["client"]["disallowed_tools"] == ["Bash"]
    assert entries["client"]["allowed_mcp_servers"] == []
    assert (tmp_path / ".runtime" / LEGACY_WORKSPACES_IMPORT_MARKER).is_file()


def test_an_archived_workspace_is_not_reimported(tmp_path: Path) -> None:
    _write_registry(tmp_path, [{"name": "personal", "vault_root": "memory-vault/personal"}])
    archive = tmp_path / ".archived-workspaces" / "client-20260901-120000"
    archive.mkdir(parents=True)
    (archive / "archive.json").write_text(
        json.dumps({"name": "client", "status": "archived"}), encoding="utf-8"
    )
    config = _config(tmp_path, LEGACY)

    imported = config.import_legacy_workspaces_env()

    assert imported == ["home"]
    assert "client" not in config.workspaces
    assert {e["name"] for e in _registry(tmp_path)} == {"personal", "home"}


def test_legacy_gws_profile_migrates_only_blank_workspace_links(tmp_path: Path) -> None:
    _write_registry(
        tmp_path,
        [
            {"name": "home", "future_field": "kept"},
            {"name": "client", "gws_profile": "personal"},
        ],
    )
    _connect_profile(tmp_path, "acme")
    config = _config(tmp_path, gws_profile="acme")

    imported = config.import_legacy_gws_profile_env()

    assert imported == ["home"]
    entries = {entry["name"]: entry for entry in _registry(tmp_path)}
    assert entries["home"]["gws_profile"] == "acme"
    assert entries["home"]["future_field"] == "kept"
    assert entries["client"]["gws_profile"] == "personal"
    assert config.workspaces["home"].gws_profile == "acme"
    marker = tmp_path / ".runtime" / LEGACY_GWS_PROFILE_IMPORT_MARKER
    assert marker.is_file()
    assert "must-not-be-copied" not in marker.read_text(encoding="utf-8")


def test_unknown_legacy_gws_profile_is_not_imported_or_consumed(tmp_path: Path) -> None:
    _write_registry(tmp_path, [{"name": "home"}])
    before = (tmp_path / ".runtime" / "workspaces.json").read_text(encoding="utf-8")
    config = _config(tmp_path, gws_profile="../acme")

    assert config.import_legacy_gws_profile_env() == []
    assert (tmp_path / ".runtime" / "workspaces.json").read_text(encoding="utf-8") == before
    assert not (tmp_path / ".runtime" / LEGACY_GWS_PROFILE_IMPORT_MARKER).exists()


def test_legacy_gws_profile_marker_does_not_fill_later_blank_links(tmp_path: Path) -> None:
    _write_registry(tmp_path, [{"name": "home"}])
    _connect_profile(tmp_path, "acme")
    config = _config(tmp_path, gws_profile="acme")
    assert config.import_legacy_gws_profile_env() == ["home"]

    _write_registry(tmp_path, [{"name": "home", "gws_profile": ""}])
    reloaded = _config(tmp_path, gws_profile="acme")

    assert reloaded.import_legacy_gws_profile_env() == []
    assert _registry(tmp_path)[0]["gws_profile"] == ""
