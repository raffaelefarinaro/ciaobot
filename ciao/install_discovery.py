"""Bare-shell install discovery: which installed engine is this shell talking to.

A bare-shell invocation (`ciao health get` from any directory) has no
``CIAO_WORKSPACE``, so ``ciao.config.installed_workspace_env`` has to find the
install the operator actually has rather than manufacture a bootstrap workspace
beside it. Before this module that question was answered by the macOS
LaunchAgent alone, which on Windows meant the engine's own logon task was
ignored and the bare shell fell back to the cwd. Linux reads the root-owned
systemd unit the installer wrote.

This is the one answer to that question, and both branches are read-only: they
read the service definition the engine wrote and mint nothing. A definition
naming a directory that is no longer there is stale and is not trusted — the
``is_dir()`` guard lives here so that rule has one home — because pinning it
would recreate what the operator deleted.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ciao.config import CiaoConfig


def _platform() -> str:
    """``sys.platform``, read fresh through a seam tests can force a branch on."""
    return sys.platform


def discover_workspace(base: Mapping[str, str]) -> Path | None:
    """The installed workspace this shell should use, or ``None`` if there is none.

    ``base`` is the caller's environment; the macOS branch needs it because the
    LaunchAgent's plist falls back to ``CIAO_WORKSPACE`` from it. The Windows
    branch reads the task definition the engine wrote and never consults the
    shell environment: the installed workspace is recorded there, and a live
    ``schtasks`` query would be a second source of truth this read-only seam does
    not need.

    The dispatch is platform-aware: on Windows the engine's own logon task is
    the install record, so it is read first; on Linux it is the systemd unit,
    trusted only when root-owned and not group- or world-writable, and never the
    shell environment. Everywhere else — and on Windows
    when no task was found — the macOS LaunchAgent read runs, which is the
    behaviour-before-this-seam call every platform made and the call the
    existing tests patch. On a real Windows machine there is no plist, so that
    read yields no workspace and the `is_dir()` guard leaves the mapping
    unchanged: no fallback install is invented.
    """
    if _platform() == "win32":
        discovered = _discover_windows()
        if discovered is not None:
            return discovered
        # Fall through rather than return None: the macOS read is the one the
        # seam must preserve on every platform, and on Windows it simply finds
        # no plist. Returning early here would bypass the patch target the
        # existing discovery tests install and break them on the Windows job.
    elif _platform().startswith("linux"):
        discovered = _discover_linux()
        if discovered is not None:
            return discovered
        # Same fall-through as Windows: a bare shell as another user, or an
        # install with no unit, keeps the behaviour it had before this branch.
    return _discover_macos(base)


def _discover_macos(base: Mapping[str, str]) -> Path | None:
    # Imported here, not at module scope: `ciao.macos_service` reads the plist,
    # and the tests patch `ciao.macos_service.discover_runtime`.
    from ciao.macos_service import discover_runtime

    try:
        discovered = discover_runtime(environ=dict(base))
    except Exception:  # noqa: BLE001 - plist missing/unreadable
        return None
    if not discovered or not discovered.workspace:
        return None
    workspace = Path(discovered.workspace)
    return workspace if workspace.is_dir() else None


def _discover_windows() -> Path | None:
    # Imported here for symmetry with the macOS branch and to keep this module
    # importable without pulling in the Task Scheduler surface.
    from ciao import windows_service

    definition = windows_service.live_task_dir() / windows_service.TASK_FILE_NAME
    workspace = windows_service.task_workspace(definition)
    if workspace is None:
        return None
    return workspace if workspace.is_dir() else None


def _discover_linux() -> Path | None:
    from ciao import linux_service

    workspace = linux_service.unit_workspace(linux_service.UNIT_PATH)
    if workspace is None or not workspace.is_dir():
        return None
    # A workspace this user cannot read (0700 under the service account) is not
    # this shell's install: fall through rather than point at it.
    return workspace if os.access(workspace, os.R_OK | os.X_OK) else None


def trusted_registry(name: str | None, vault: Path) -> CiaoConfig | None:
    """The installed engine's registry, if it registers *name* at exactly *vault*.

    The handover gate decides what an unattended agent may write into the
    always-loaded regions, so it must not read a registry the agent can name.
    ``--workspace``, ``CIAO_WORKSPACE`` and ``CIAO_RUNTIME_ROOT`` are caller
    inputs and are dropped here: the install comes only from the service
    definition (:func:`discover_workspace`), and nothing
    is guessed when there is none. ``None`` when no install is found, the name
    is not registered there, or the registered vault is not the one in hand.
    """
    if name is None:
        return None
    from ciao.config import CiaoConfig, installed_workspace_env

    base = {
        key: value
        for key, value in os.environ.items()
        if key not in ("CIAO_WORKSPACE", "CIAO_RUNTIME_ROOT")
    }
    installed = discover_workspace(base)
    if installed is None:
        return None
    # A placeholder keeps a read-only resolution from minting a session secret.
    env_source = installed_workspace_env({**base, "CIAO_WORKSPACE": str(installed)})
    if not env_source.get("PWA_AUTH_TOKEN", "").strip():
        env_source["PWA_AUTH_TOKEN"] = "memory-proposals"
    config = CiaoConfig.from_env(env_source)
    if config.workspace(name) is None:
        return None
    try:
        registered_vault = Path(config.workspace_vault_root(name)).resolve()
    except (OSError, ValueError):
        return None
    return config if registered_vault == vault.resolve() else None
