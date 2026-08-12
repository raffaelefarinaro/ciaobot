"""Safe cleanup for bundles installed by the one-line installer."""
from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from ciao import desktop_build, desktop_install


def _native_bundle(root: Path) -> Path:
    executable = root / desktop_install.APP_BUNDLE_NAME / "Contents" / "MacOS"
    executable.mkdir(parents=True)
    (executable / desktop_build.APP_EXECUTABLE_NAME).write_bytes(b"native app")
    return executable.parent.parent


def test_uninstall_removes_only_the_native_app(tmp_path: Path) -> None:
    bundle = _native_bundle(tmp_path)

    result = desktop_install.uninstall_desktop_app(app_dir=tmp_path)

    assert result == {
        "removed": True,
        "path": str(tmp_path / desktop_install.APP_BUNDLE_NAME),
        "removed_agents": [],
    }
    assert not bundle.exists()


def test_uninstall_leaves_a_browser_pwa_alone(tmp_path: Path) -> None:
    app = tmp_path / desktop_install.APP_BUNDLE_NAME / "Contents" / "MacOS"
    app.mkdir(parents=True)
    (app / "app_mode_loader").write_text("browser pwa", encoding="utf-8")

    with pytest.raises(desktop_install.InstallError, match="not the Ciaobot desktop app"):
        desktop_install.uninstall_desktop_app(app_dir=tmp_path)

    assert app.exists()


def test_uninstall_boots_out_and_removes_installer_agents(tmp_path: Path) -> None:
    bundle = _native_bundle(tmp_path)
    engine = bundle / "Contents" / "Resources" / "ciao-runtime" / "bin" / "ciao"
    engine.parent.mkdir(parents=True)
    engine.write_bytes(b"engine")
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    desktop_plist = agents / "Ciaobot.plist"
    server_plist = agents / "com.ciao.server.plist"
    with desktop_plist.open("wb") as stream:
        plistlib.dump({"ProgramArguments": [str(bundle / "Contents" / "MacOS" / desktop_build.APP_EXECUTABLE_NAME)]}, stream)
    with server_plist.open("wb") as stream:
        plistlib.dump({"ProgramArguments": [str(engine), "run"]}, stream)
    calls: list[list[str]] = []

    def runner(args: list[str], **kwargs):
        calls.append(args)
        return desktop_install.subprocess.CompletedProcess(args, 0, "", "")

    result = desktop_install.uninstall_desktop_app(
        app_dir=tmp_path,
        launch_agents_dir=agents,
        uid=501,
        runner=runner,
    )

    assert result["removed_agents"] == [str(desktop_plist), str(server_plist)]
    assert calls == [
        ["launchctl", "bootout", "gui/501/Ciaobot"],
        ["launchctl", "bootout", "gui/501/com.ciao.server"],
    ]
    assert not desktop_plist.exists()
    assert not server_plist.exists()

def test_uninstall_reports_an_empty_directory(tmp_path: Path) -> None:
    assert desktop_install.uninstall_desktop_app(app_dir=tmp_path) == {
        "removed": False,
        "path": str(tmp_path / desktop_install.APP_BUNDLE_NAME),
    }
