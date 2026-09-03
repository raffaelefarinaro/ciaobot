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
from ciao.macos_service import default_launch_agents_dir


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
        launch_agents_dir or default_launch_agents_dir()
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


# Line the one-line installer writes into ~/.local/bin/ciao so both sides can
# tell its shim apart from a `ciao` belonging to some other project.
SHIM_MARKER = "# Ciaobot shim (managed by the Ciaobot installer)"


def _remove_installer_shim(*, destination: Path, shim_path: Path | None = None) -> str:
    """Delete the installer's ``ciao`` shim when it points at this bundle.

    Only a file carrying the installer's marker *and* naming this bundle is
    touched: an unrelated ``ciao`` in ``~/.local/bin`` belongs to its owner,
    and a shim naming a different bundle belongs to that install.
    """
    shim = shim_path or (Path.home() / ".local" / "bin" / "ciao")
    try:
        content = shim.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    engine = destination / "Contents" / "Resources" / "ciao-runtime" / "bin" / "ciao"
    # The full quoted exec target, not a substring of the path: a bundle
    # directory can be the prefix of a longer one.
    if SHIM_MARKER not in content or f'"{engine}"' not in content:
        return ""
    try:
        shim.unlink()
    except OSError:
        return ""
    return str(shim)


def uninstall_desktop_app(
    *,
    app_dir: Path,
    launch_agents_dir: Path | None = None,
    uid: int | None = None,
    runner: Callable[..., Any] = subprocess.run,
    shim_path: Path | None = None,
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
    # Only after the bundle is actually gone: a failed rmtree leaves the app
    # installed, and deleting the shim first would take away the `ciao`
    # command while the install it points at is still there.
    removed_shim = _remove_installer_shim(destination=destination, shim_path=shim_path)
    result: dict[str, Any] = {
        "removed": True,
        "path": str(destination),
        "removed_agents": removed_agents,
    }
    if removed_shim:
        result["removed_shim"] = removed_shim
    return result
