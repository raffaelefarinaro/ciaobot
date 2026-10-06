"""Marker recording when setup provisioned a brand-new workspace.

`setup_workspace` writes `.runtime/setup-completed-at` only when it creates a
workspace's `.env` from scratch (a first-time install, not a rerun over an
existing one). Startup reads it to hold system-routine catch-up for
`SETUP_CATCH_UP_GRACE`: a brand-new install should be greeted by its
onboarding chat, not by four parallel routine chats, so within the grace
window the routines wait for their next regular tick instead of replaying the
missed occurrence at startup.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

SETUP_MARKER_FILENAME = "setup-completed-at"
# How long after setup the startup catch-up stays quiet for system routines.
SETUP_CATCH_UP_GRACE = timedelta(hours=24)

# The runtime directory inside a workspace. Fixed: setup no longer records it in
# `.env`, and the service definitions render `<workspace>/.runtime`.
RUNTIME_DIR_NAME = ".runtime"
# The workspace registry. Every `ciao setup` path writes it (fresh, rerun,
# nested-vault adoption), so its presence is what "this folder is a set-up
# Ciaobot workspace" means. The marker below is only written on a first-time
# setup, and a `.env` is not specific to Ciaobot.
WORKSPACE_REGISTRY_FILENAME = "workspaces.json"


def is_set_up_workspace(root: Path) -> bool:
    """Whether ``root`` is a workspace `ciao setup` has provisioned."""
    return (root / RUNTIME_DIR_NAME / WORKSPACE_REGISTRY_FILENAME).is_file()


def marker_path(runtime_root: Path) -> Path:
    return runtime_root / SETUP_MARKER_FILENAME


def write_setup_marker(
    runtime_root: Path, *, now: datetime | None = None
) -> Path:
    """Record setup completion as an ISO UTC timestamp, one line."""
    runtime_root.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    path = marker_path(runtime_root)
    path.write_text(f"{stamp}\n", encoding="utf-8", newline="")
    return path


def read_setup_marker(runtime_root: Path) -> datetime | None:
    """Return the recorded setup timestamp, or None when absent/unreadable."""
    path = marker_path(runtime_root)
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return datetime.fromisoformat(raw)
    except FileNotFoundError:
        return None
    except (ValueError, OSError):
        logger.exception("Failed to read setup marker at %s", path)
        return None


def catch_up_grace_active(
    runtime_root: Path, *, now: datetime | None = None
) -> bool:
    """True within `SETUP_CATCH_UP_GRACE` of a first-time setup."""
    stamp = read_setup_marker(runtime_root)
    if stamp is None:
        return False
    current = now or datetime.now(UTC)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return current - stamp < SETUP_CATCH_UP_GRACE