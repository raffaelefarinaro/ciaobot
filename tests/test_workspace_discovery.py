"""Bare-shell workspace discovery: the platform branches and ``trusted_registry``.

The linux cases read a root-owned systemd unit; the app-mode cases pin the
pointer branch that keeps ``trusted_registry`` in step with
``ciao.config.installed_workspace_env`` in bundled-app mode.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from ciao import app_engine, linux_service, workspace_discovery
from ciao.linux_service import render_service


def _trusted_unit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workspace: Path) -> Path:
    unit = tmp_path / "unit" / "ciaobot.service"
    unit.parent.mkdir()
    unit.write_text(
        render_service(workspace=workspace, user="ciaobot", home=Path("/home/ciaobot"), python=Path("/usr/bin/python3")),
        encoding="utf-8",
    )
    unit.chmod(0o644)
    monkeypatch.setattr(linux_service, "UNIT_PATH", unit)
    monkeypatch.setattr(linux_service, "TRUSTED_UNIT_UID", unit.stat().st_uid)
    monkeypatch.setattr(workspace_discovery, "_platform", lambda: "linux")
    monkeypatch.setattr(workspace_discovery, "_discover_macos", lambda base: None)
    return unit


def test_linux_branch_reads_the_unit(tmp_path, monkeypatch):
    install = tmp_path / "install"
    install.mkdir()
    _trusted_unit(tmp_path, monkeypatch, install)

    assert workspace_discovery.discover_workspace({}) == install


def test_linux_branch_ignores_the_shell_environment(tmp_path, monkeypatch):
    install = tmp_path / "install"
    other = tmp_path / "other"
    install.mkdir()
    other.mkdir()
    _trusted_unit(tmp_path, monkeypatch, install)

    assert workspace_discovery.discover_workspace({"CIAO_WORKSPACE": str(other)}) == install


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
def test_linux_branch_falls_through_when_the_workspace_is_unreadable(tmp_path, monkeypatch):
    install = tmp_path / "install"
    install.mkdir()
    _trusted_unit(tmp_path, monkeypatch, install)
    install.chmod(0)
    try:
        assert workspace_discovery.discover_workspace({}) is None
    finally:
        install.chmod(0o700)


def test_linux_branch_missing_unit_falls_through(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_discovery, "_platform", lambda: "linux")
    monkeypatch.setattr(workspace_discovery, "_discover_macos", lambda base: None)

    assert workspace_discovery.discover_workspace({}) is None


def test_linux_branch_refuses_a_stale_unit_workspace(tmp_path, monkeypatch):
    """A unit naming a directory that was deleted is not pinned back into being."""
    gone = tmp_path / "deleted-install"
    _trusted_unit(tmp_path, monkeypatch, gone)

    assert workspace_discovery.discover_workspace({}) is None


def test_linux_branch_refuses_a_workspace_that_is_not_a_directory(tmp_path, monkeypatch):
    """Readable and executable, but a file: ``os.access`` alone would accept it."""
    not_a_dir = tmp_path / "not-a-dir"
    not_a_dir.write_text("x", encoding="utf-8")
    not_a_dir.chmod(0o755)
    _trusted_unit(tmp_path, monkeypatch, not_a_dir)

    assert workspace_discovery.discover_workspace({}) is None


def _darwin_plist_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The LaunchAgent's plist falls back to the caller's ``CIAO_WORKSPACE``."""
    from ciao import macos_service

    monkeypatch.setattr(workspace_discovery, "_platform", lambda: "darwin")
    monkeypatch.setattr(
        macos_service,
        "discover_runtime",
        lambda environ: SimpleNamespace(workspace=environ.get("CIAO_WORKSPACE")),
    )


def test_macos_branch_refuses_a_stale_definition(tmp_path, monkeypatch):
    gone = tmp_path / "deleted-install"
    from ciao import macos_service

    monkeypatch.setattr(workspace_discovery, "_platform", lambda: "darwin")
    monkeypatch.setattr(
        macos_service,
        "discover_runtime",
        lambda environ: SimpleNamespace(workspace=str(gone)),
    )

    assert workspace_discovery.discover_workspace({}) is None


def test_trusted_registry_ignores_a_caller_named_workspace(tmp_path, monkeypatch):
    """An agent cannot name a registry: the caller's CIAO_WORKSPACE is dropped.

    The attacker directory registers ``personal`` at the vault the caller asks
    for. With the caller's variable dropped the plist finds nothing and the
    answer is ``None``; keeping it would adopt the attacker's registry.
    """
    attacker = tmp_path / "attacker"
    vault = attacker / "personal" / "memory-vault"
    vault.mkdir(parents=True)
    (attacker / ".runtime").mkdir()
    (attacker / ".runtime" / "workspaces.json").write_text(
        json.dumps([{"name": "personal", "vault_root": str(vault)}]), encoding="utf-8"
    )
    _darwin_plist_reads_the_environment(monkeypatch)
    monkeypatch.setenv("CIAO_WORKSPACE", str(attacker))
    monkeypatch.delenv("CIAO_RUNTIME_ROOT", raising=False)

    assert workspace_discovery.trusted_registry("personal", vault) is None


def test_trusted_registry_on_linux(tmp_path, monkeypatch):
    install = tmp_path / "install"
    vault = install / "personal" / "memory-vault"
    vault.mkdir(parents=True)
    runtime = install / ".runtime"
    runtime.mkdir()
    (runtime / "workspaces.json").write_text(
        json.dumps([{"name": "personal", "vault_root": str(vault)}]), encoding="utf-8"
    )
    _trusted_unit(tmp_path, monkeypatch, install)
    monkeypatch.delenv("CIAO_WORKSPACE", raising=False)
    monkeypatch.delenv("CIAO_RUNTIME_ROOT", raising=False)

    config = workspace_discovery.trusted_registry("personal", vault)
    assert config is not None
    assert Path(config.workspace_vault_root("personal")).resolve() == vault.resolve()
    assert workspace_discovery.trusted_registry("personal", tmp_path / "elsewhere") is None


# --- app mode: the pointer replaces the LaunchAgent here as well --------------
#
# `ciao.config.installed_workspace_env` answers with the app-owned pointer and
# never reads the plist in bundle mode. `discover_workspace` runs the same
# branch before its platform branches so `trusted_registry`, which calls it
# directly, agrees with the config instead of adopting the 1.x workspace the
# plist still names.


def _plists_a_different_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The plist names a real, different install: dropping the branch shows it."""
    old_install = tmp_path / "old-install"
    old_install.mkdir()
    monkeypatch.setattr(
        workspace_discovery,
        "_discover_macos",
        lambda base: old_install,
    )
    return old_install


def _app_mode_with_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    """Bundle mode on, `read_pointer` naming a real workspace."""
    monkeypatch.setattr("ciao.package_version.running_from_app_bundle", lambda: True)
    workspace = tmp_path / "pointer-ws"
    runtime = tmp_path / "pointer-runtime-named"
    workspace.mkdir()
    runtime.mkdir()
    monkeypatch.setattr(
        "ciao.app_engine.read_pointer",
        lambda: app_engine.WorkspacePointer(workspace=workspace, runtime_root=runtime),
    )
    return workspace, runtime


def test_discover_workspace_bundle_mode_uses_the_app_pointer(tmp_path, monkeypatch):
    workspace, _runtime = _app_mode_with_pointer(tmp_path, monkeypatch)
    _plists_a_different_install(tmp_path, monkeypatch)

    assert workspace_discovery.discover_workspace({}) == workspace


def test_discover_workspace_bundle_mode_bootstraps_when_no_pointer(tmp_path, monkeypatch):
    """Nothing names a workspace in app mode: bootstrap, never the plist."""
    monkeypatch.setattr("ciao.package_version.running_from_app_bundle", lambda: True)
    monkeypatch.setattr("ciao.app_engine.read_pointer", lambda: None)
    _plists_a_different_install(tmp_path, monkeypatch)

    assert workspace_discovery.discover_workspace({}) is None


def test_trusted_registry_uses_app_pointer_in_bundle(tmp_path, monkeypatch):
    """App mode: the registry the pointer's install registered, not the plist's.

    The plist workspace holds a registry that never registers ``personal``, so
    dropping the pointer branch from ``discover_workspace`` changes the answer
    from an adopted config to ``None``.
    """
    workspace, _runtime = _app_mode_with_pointer(tmp_path, monkeypatch)
    _plists_a_different_install(tmp_path, monkeypatch)

    vault = workspace / "personal" / "memory-vault"
    vault.mkdir(parents=True)
    (workspace / ".runtime").mkdir()
    (workspace / ".runtime" / "workspaces.json").write_text(
        json.dumps([{"name": "personal", "vault_root": str(vault)}]), encoding="utf-8"
    )
    monkeypatch.delenv("CIAO_WORKSPACE", raising=False)
    monkeypatch.delenv("CIAO_RUNTIME_ROOT", raising=False)

    config = workspace_discovery.trusted_registry("personal", vault)
    assert config is not None
    assert Path(config.workspace_vault_root("personal")).resolve() == vault.resolve()
