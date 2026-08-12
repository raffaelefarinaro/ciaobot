"""Safe cleanup for app bundles installed by the release installer.

Installation and updates are deliberately outside the Python package: the
one-line installer downloads a signed release archive and the native Tauri
updater handles in-app updates. Keeping only this cleanup command avoids
maintaining a second archive downloader and signature implementation.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ciao import desktop_build


APP_BUNDLE_NAME = desktop_build.APP_BUNDLE_NAME


class InstallError(Exception):
    """The requested app-bundle cleanup could not proceed safely."""


def _plist_executable(path: Path) -> Path | None:
    """Return an agent executable when its plist is valid."""
    try:
        with path.open("rb") as stream:
            payload = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException, ValueError, TypeError):
        return None
    arguments = payload.get("ProgramArguments") if isinstance(payload, dict) else None
    if (
        not isinstance(arguments, list)
        or not arguments
        or not isinstance(arguments[0], str)
    ):
        return None
    return Path(arguments[0]).expanduser()


def _remove_installer_launch_agents(
    *,
    destination: Path,
    launch_agents_dir: Path | None = None,
    uid: int | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> list[str]:
    """Boot out and delete agents whose executables belong to this bundle."""
    launch_dir = (
        launch_agents_dir or Path.home() / "Library" / "LaunchAgents"
    ).expanduser()
    resolved_uid = getattr(os, "getuid", lambda: 0)() if uid is None else uid
    specs = (
        (
            "Ciaobot",
            "Ciaobot.plist",
            destination / "Contents" / "MacOS" / desktop_build.APP_EXECUTABLE_NAME,
        ),
        (
            "com.ciao.server",
            "com.ciao.server.plist",
            destination / "Contents" / "Resources" / "ciao-runtime" / "bin" / "ciao",
        ),
    )
    removed: list[str] = []
    for label, filename, executable in specs:
        plist_path = launch_dir / filename
        actual = _plist_executable(plist_path) if plist_path.is_file() else None
        if actual is None or actual.resolve() != executable.resolve():
            continue
        try:
            runner(
                ["launchctl", "bootout", f"gui/{resolved_uid}/{label}"],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            # The plist still needs removing when launchctl is unavailable
            # (for example, when this cleanup is exercised off macOS).
            pass
        try:
            plist_path.unlink()
        except OSError:
            continue
        removed.append(str(plist_path))
    return removed


def uninstall_desktop_app(
    *,
    app_dir: Path,
    launch_agents_dir: Path | None = None,
    uid: int | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Remove a Ciaobot.app bundle without deleting a browser-installed PWA."""

    destination = Path(app_dir).expanduser() / APP_BUNDLE_NAME
    if not destination.exists():
        return {"removed": False, "path": str(destination)}
    if not (
        destination / "Contents" / "MacOS" / desktop_build.APP_EXECUTABLE_NAME
    ).is_file():
        raise InstallError(
            f"{destination} is not the Ciaobot desktop app "
            f"(no Contents/MacOS/{desktop_build.APP_EXECUTABLE_NAME}); leaving it alone"
        )
    removed_agents = _remove_installer_launch_agents(
        destination=destination,
        launch_agents_dir=launch_agents_dir,
        uid=uid,
        runner=runner,
    )
    try:
        shutil.rmtree(destination)
    except OSError as exc:
        raise InstallError(f"could not remove {destination}: {exc}") from exc
    return {
        "removed": True,
        "path": str(destination),
        "removed_agents": removed_agents,
    }
