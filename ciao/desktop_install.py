"""Safe cleanup for app bundles installed by the release installer.

Installation and updates are deliberately outside the Python package: the
one-line installer downloads a signed release archive and the native Tauri
updater handles in-app updates. Keeping only this cleanup command avoids
maintaining a second archive downloader and signature implementation.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ciao import desktop_build


APP_BUNDLE_NAME = desktop_build.APP_BUNDLE_NAME


class InstallError(Exception):
    """The requested app-bundle cleanup could not proceed safely."""


def uninstall_desktop_app(*, app_dir: Path) -> dict[str, Any]:
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
    try:
        shutil.rmtree(destination)
    except OSError as exc:
        raise InstallError(f"could not remove {destination}: {exc}") from exc
    return {"removed": True, "path": str(destination)}
