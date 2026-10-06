"""Marker recording when setup provisioned a brand-new workspace.

`setup_workspace` writes `.runtime/setup-completed-at` only on a first-time
setup (a fresh `.env` or no registry yet), not on a rerun over an existing
install. Startup reads it to hold system-routine catch-up for
`SETUP_CATCH_UP_GRACE`: a brand-new install should be greeted by its
onboarding chat, not by four parallel routine chats, so within the grace
window the routines wait for their next regular tick instead of replaying the
missed occurrence at startup.

Its second line is the vault mode setup chose (`scratch` or `existing`), which
only the first-run onboarding chat reads. It used to be `CIAO_VAULT_MODE` in
`.env`, a permanent variable for a one-shot decision.
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
    if (root / RUNTIME_DIR_NAME / WORKSPACE_REGISTRY_FILENAME).is_file():
        return True
    return _has_legacy_workspaces_env(root)


def _has_legacy_workspaces_env(root: Path) -> bool:
    """Pre-1.0 path: a `.env` with a non-empty ``CIAO_WORKSPACES``.

    Such an install has no registry until its first start on a release that
    imports the variable, so an upgrade must still recognise it. Delete
    together with the other pre-1.0 imports (``import_legacy_workspaces_env``
    and the installer's matching check in `scripts/install-engine.sh`).
    """
    from ciao.macos_service import read_dotenv

    return bool(read_dotenv(root / ".env").get("CIAO_WORKSPACES", "").strip())


def marker_path(runtime_root: Path) -> Path:
    return runtime_root / SETUP_MARKER_FILENAME


VAULT_MODES = ("scratch", "existing")


def write_setup_marker(
    runtime_root: Path, *, now: datetime | None = None, vault_mode: str = "scratch"
) -> Path:
    """Record setup completion (an ISO UTC timestamp) and the vault mode."""
    runtime_root.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    mode = vault_mode if vault_mode in VAULT_MODES else "scratch"
    path = marker_path(runtime_root)
    path.write_text(f"{stamp}\n{mode}\n", encoding="utf-8", newline="")
    return path


def _marker_lines(runtime_root: Path) -> list[str]:
    return marker_path(runtime_root).read_text(encoding="utf-8").strip().splitlines()


def read_setup_vault_mode(runtime_root: Path) -> str:
    """The vault mode the first-time setup chose; ``scratch`` when unrecorded."""
    try:
        lines = _marker_lines(runtime_root)
    except OSError:
        return "scratch"
    mode = lines[1].strip().lower() if len(lines) > 1 else ""
    return mode if mode in VAULT_MODES else "scratch"


def read_setup_marker(runtime_root: Path) -> datetime | None:
    """Return the recorded setup timestamp, or None when absent/unreadable."""
    path = marker_path(runtime_root)
    try:
        lines = _marker_lines(runtime_root)
        return datetime.fromisoformat(lines[0].strip() if lines else "")
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