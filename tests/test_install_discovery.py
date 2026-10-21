"""Linux branch of bare-shell install discovery: the root-owned systemd unit."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from ciao import install_discovery, linux_service
from ciao.linux_service import render_service

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership and modes")


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
    monkeypatch.setattr(install_discovery, "_platform", lambda: "linux")
    monkeypatch.setattr(install_discovery, "_discover_macos", lambda base: None)
    return unit


@posix_only
def test_linux_branch_reads_the_unit(tmp_path, monkeypatch):
    install = tmp_path / "install"
    install.mkdir()
    _trusted_unit(tmp_path, monkeypatch, install)

    assert install_discovery.discover_workspace({}) == install


@posix_only
def test_linux_branch_ignores_the_shell_environment(tmp_path, monkeypatch):
    install = tmp_path / "install"
    other = tmp_path / "other"
    install.mkdir()
    other.mkdir()
    _trusted_unit(tmp_path, monkeypatch, install)

    assert install_discovery.discover_workspace({"CIAO_WORKSPACE": str(other)}) == install


@posix_only
@pytest.mark.skipif(sys.platform != "win32" and os.geteuid() == 0, reason="root reads any directory")
def test_linux_branch_falls_through_when_the_workspace_is_unreadable(tmp_path, monkeypatch):
    install = tmp_path / "install"
    install.mkdir()
    _trusted_unit(tmp_path, monkeypatch, install)
    install.chmod(0)
    try:
        assert install_discovery.discover_workspace({}) is None
    finally:
        install.chmod(0o700)


def test_linux_branch_missing_unit_falls_through(tmp_path, monkeypatch):
    monkeypatch.setattr(install_discovery, "_platform", lambda: "linux")
    monkeypatch.setattr(install_discovery, "_discover_macos", lambda base: None)

    assert install_discovery.discover_workspace({}) is None


@posix_only
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

    config = install_discovery.trusted_registry("personal", vault)
    assert config is not None
    assert Path(config.workspace_vault_root("personal")).resolve() == vault.resolve()
    assert install_discovery.trusted_registry("personal", tmp_path / "elsewhere") is None
