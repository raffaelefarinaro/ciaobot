from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from ciao import install_discovery, macos_service, windows_service


def _fresh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str = "darwin") -> tuple[dict[str, str], Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(install_discovery, "_platform", lambda: platform)
    monkeypatch.setattr(install_discovery, "_discover_macos", lambda base: None)
    monkeypatch.setattr(macos_service, "server_job_registered", lambda: False)
    monkeypatch.setattr(windows_service, "engine_task_registered", lambda: False)
    return {}, home


def test_verified_absence_allows_fresh_entry_without_writes(monkeypatch, tmp_path):
    env, home = _fresh(monkeypatch, tmp_path)
    before = sorted(p.relative_to(home) for p in home.rglob("*"))
    install_discovery.require_fresh_installer_entry(environ=env, cwd=tmp_path)
    assert sorted(p.relative_to(home) for p in home.rglob("*")) == before


def test_configured_service_definition_refuses_bootstrap(monkeypatch, tmp_path):
    env, home = _fresh(monkeypatch, tmp_path)
    live = home / "Library/LaunchAgents"
    live.mkdir(parents=True)
    (live / f"{macos_service.SERVER_LABEL}.plist").write_text("bad")
    with pytest.raises(install_discovery.InstallerEntryRefused):
        install_discovery.require_fresh_installer_entry(environ=env, cwd=tmp_path)


def test_stale_malformed_and_symlinked_definition_refuse_bootstrap(monkeypatch, tmp_path):
    env, home = _fresh(monkeypatch, tmp_path)
    live = home / "Library/LaunchAgents"
    live.mkdir(parents=True)
    (live / f"{macos_service.SERVER_LABEL}.plist").symlink_to(tmp_path / "missing")
    with pytest.raises(install_discovery.InstallerEntryRefused):
        install_discovery.require_fresh_installer_entry(environ=env, cwd=tmp_path)


def test_registered_engine_without_definition_refuses_bootstrap(monkeypatch, tmp_path):
    env, _ = _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(macos_service, "server_job_registered", lambda: True)
    with pytest.raises(install_discovery.InstallerEntryRefused):
        install_discovery.require_fresh_installer_entry(environ=env, cwd=tmp_path)


def test_service_query_failure_is_not_freshness(monkeypatch, tmp_path):
    env, _ = _fresh(monkeypatch, tmp_path)
    def fail(): raise RuntimeError("query failed")
    monkeypatch.setattr(macos_service, "server_job_registered", fail)
    with pytest.raises(install_discovery.InstallerEntryRefused):
        install_discovery.require_fresh_installer_entry(environ=env, cwd=tmp_path)


def test_export_directory_override_cannot_hide_live_definition(monkeypatch, tmp_path):
    env, home = _fresh(monkeypatch, tmp_path)
    monkeypatch.setenv("CIAO_LAUNCH_AGENTS_DIR", str(tmp_path / "export"))
    live = home / "Library/LaunchAgents"
    live.mkdir(parents=True)
    (live / f"{macos_service.SERVER_LABEL}.plist").touch()
    with pytest.raises(install_discovery.InstallerEntryRefused):
        install_discovery.require_fresh_installer_entry(environ=env, cwd=tmp_path)


def test_configured_cwd_environment_and_legacy_default_root_refuse(monkeypatch, tmp_path):
    env, home = _fresh(monkeypatch, tmp_path)
    install_discovery  # workspace check is exercised using an established registry
    (tmp_path / ".runtime").mkdir()
    (tmp_path / ".runtime/workspaces.json").touch()
    with pytest.raises(install_discovery.InstallerEntryRefused):
        install_discovery.require_fresh_installer_entry(environ=env, cwd=tmp_path)
    for key in ("CIAO_WORKSPACE", "CIAO_WORKSPACES", "CIAO_RUNTIME_ROOT", "CIAO_MEMORY_DIR"):
        with pytest.raises(install_discovery.InstallerEntryRefused):
            install_discovery.require_fresh_installer_entry(environ={key: "/redirect"}, cwd=tmp_path.parent)
    (home / "Ciaobot").mkdir()
    (home / "Ciaobot/.runtime").mkdir()
    (home / "Ciaobot/.runtime/workspaces.json").touch()
    with pytest.raises(install_discovery.InstallerEntryRefused):
        install_discovery.require_fresh_installer_entry(environ={}, cwd=tmp_path.parent)


def _deny(path: Path) -> Path:
    path.chmod(0o000)
    return path


def _restore(path: Path) -> None:
    path.chmod(0o700)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root ignores directory permissions")
def test_unreadable_launch_agents_directory_refuses_instead_of_reporting_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env, home = _fresh(monkeypatch, tmp_path)
    live = home / "Library/LaunchAgents"
    live.mkdir(parents=True)
    (live / f"{macos_service.SERVER_LABEL}.plist").write_text("bad")
    _deny(live)
    try:
        with pytest.raises(install_discovery.InstallerEntryRefused):
            install_discovery.require_fresh_installer_entry(environ=env, cwd=tmp_path)
    finally:
        _restore(live)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root ignores directory permissions")
def test_unreadable_legacy_dotenv_refuses_instead_of_reporting_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env, _ = _fresh(monkeypatch, tmp_path)
    dotenv = tmp_path / ".env"
    dotenv.write_text("CIAO_WORKSPACES=/somewhere\n", encoding="utf-8")
    _deny(dotenv)
    try:
        with pytest.raises(install_discovery.InstallerEntryRefused):
            install_discovery.require_fresh_installer_entry(environ=env, cwd=tmp_path)
    finally:
        _restore(dotenv)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root ignores directory permissions")
def test_unreadable_registry_directory_refuses_instead_of_reporting_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env, _ = _fresh(monkeypatch, tmp_path)
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / "workspaces.json").write_text("{}", encoding="utf-8")
    _deny(runtime)
    try:
        with pytest.raises(install_discovery.InstallerEntryRefused):
            install_discovery.require_fresh_installer_entry(environ=env, cwd=tmp_path)
    finally:
        _restore(runtime)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root ignores directory permissions")
def test_unreadable_task_directory_refuses_instead_of_reporting_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env, _ = _fresh(monkeypatch, tmp_path, platform="win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    service = windows_service.live_task_dir()
    service.mkdir(parents=True)
    (service / windows_service.TASK_FILE_NAME).write_text("bad")
    _deny(service)
    try:
        with pytest.raises(install_discovery.InstallerEntryRefused):
            install_discovery.require_fresh_installer_entry(environ=env, cwd=tmp_path)
    finally:
        _restore(service)


@pytest.mark.skipif(sys.platform == "win32", reason="chmod cannot deny reads on Windows")
def test_unreadable_dotenv_is_not_silently_absent_for_the_strict_helper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The permissive legacy reader still returns ``{}``; the guard does not."""
    from ciao.macos_service import read_dotenv

    dotenv = tmp_path / ".env"
    dotenv.write_text("CIAO_WORKSPACES=/somewhere\n", encoding="utf-8")
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        _deny(dotenv)
        try:
            assert read_dotenv(dotenv) == {}
            with pytest.raises(install_discovery.InstallerEntryRefused):
                install_discovery._strict_set_up_workspace(tmp_path)
        finally:
            _restore(dotenv)


def test_unreadable_task_query_refuses_bootstrap(monkeypatch, tmp_path):
    env, _ = _fresh(monkeypatch, tmp_path, platform="win32")

    def no_output():
        raise windows_service.WindowsServiceError("Scheduled task query returned no output")

    monkeypatch.setattr(windows_service, "engine_task_registered", no_output)
    with pytest.raises(install_discovery.InstallerEntryRefused):
        install_discovery.require_fresh_installer_entry(environ=env, cwd=tmp_path)
