"""The installer entry guard for the legacy installer bootstrap.

Bare-shell workspace discovery (``discover_workspace``, ``trusted_registry``)
lives in ``ciao.workspace_discovery``. ``require_fresh_installer_entry`` refuses
installer bootstrap unless the machine is demonstrably fresh.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from ciao.workspace_discovery import _platform


class InstallerEntryRefused(RuntimeError):
    """Installer bootstrap cannot prove that this is a fresh installation."""


def _strict_exists(path: Path) -> bool:
    """Whether ``path`` exists, refusing when that cannot be established.

    ``os.path.lexists`` reports ``False`` for an unreadable parent directory, so
    an install whose definition merely cannot be seen would look fresh. Only a
    definite "not there" is absence. A dangling symlink still counts as present.
    """
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        raise InstallerEntryRefused(f"Could not inspect {path}: {exc}") from exc
    return True


def _strict_set_up_workspace(root: Path) -> bool:
    """``is_set_up_workspace`` for the guard: I/O errors refuse, never read as absent.

    This is the legacy bootstrap guard's reader and keeps its ``read_text``
    semantics on purpose.
    """
    from ciao.setup_marker import RUNTIME_DIR_NAME, WORKSPACE_REGISTRY_FILENAME

    if _strict_exists(root / RUNTIME_DIR_NAME / WORKSPACE_REGISTRY_FILENAME):
        return True
    env_path = root / ".env"
    try:
        text = env_path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return False
    except (OSError, UnicodeDecodeError) as exc:
        raise InstallerEntryRefused(f"Could not read {env_path}: {exc}") from exc
    from ciao.macos_service import parse_dotenv

    return bool(parse_dotenv(text).get("CIAO_WORKSPACES", "").strip())


def require_fresh_installer_entry(*, environ: Mapping[str, str], cwd: Path) -> None:
    """Raise unless installer bootstrap is demonstrably safe. Read-only."""
    platform = _platform()
    if platform != "darwin":
        raise InstallerEntryRefused(f"Installer bootstrap is unsupported on {platform}")
    if any(environ.get(key, "").strip() for key in (
        "CIAO_WORKSPACE", "CIAO_WORKSPACES", "CIAO_RUNTIME_ROOT", "CIAO_MEMORY_DIR",
        "CIAO_VAULT_DIR", "CIAO_VAULT_ROOT",
    )):
        raise InstallerEntryRefused("Inherited workspace or storage redirect is present")
    if _strict_set_up_workspace(cwd):
        raise InstallerEntryRefused("Current directory is an existing Ciaobot workspace")
    home_root = Path.home() / "Ciaobot"
    if _strict_set_up_workspace(home_root):
        raise InstallerEntryRefused("Default Ciaobot workspace already exists")
    try:
        from ciao import macos_service
        definition = macos_service.live_launch_agents_dir() / f"{macos_service.SERVER_LABEL}.plist"
        if _strict_exists(definition):
            raise InstallerEntryRefused("Live LaunchAgent definition already exists")
        registered = macos_service.server_job_registered()
        if registered:
            raise InstallerEntryRefused("Engine service is already registered")
    except InstallerEntryRefused:
        raise
    except Exception as exc:
        raise InstallerEntryRefused(f"Could not verify fresh installation state: {exc}") from exc
