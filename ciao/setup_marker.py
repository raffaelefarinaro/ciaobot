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

`.runtime/welcome-pending` is a separate file (an ISO timestamp): setup writes
it only when it generated the PWA password itself, so Home can show that
password once on the host. Dismissing the card or changing the password
deletes it, which is why it is not a line of the receipt above (that one
drives the catch-up grace).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ciao.os_support.locks import lock_exclusive, lock_shared, unlock

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
VAULT_INDEX_OWNERSHIP_FILENAME = "vault-index-ownership.json"


def is_set_up_workspace(root: Path) -> bool:
    """Whether ``root`` is a workspace `ciao setup` has provisioned."""
    if (root / RUNTIME_DIR_NAME / WORKSPACE_REGISTRY_FILENAME).is_file():
        return True
    return _has_legacy_workspaces_env(root)


def _has_legacy_workspaces_env(root: Path) -> bool:
    """Pre-1.0 path: a `.env` with a non-empty ``CIAO_WORKSPACES``.

    Such an install has no registry until its first start on a release that
    imports the variable, so an upgrade must still recognise it. Delete
    together with the other pre-1.0 imports (``import_legacy_workspaces_env``).
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


class VaultIndexOwnershipUnreadable(OSError):
    """The persisted ownership map exists but is not a complete valid document."""


@contextmanager
def vault_index_ownership_lock(
    runtime_root: Path, *, exclusive: bool = True, blocking: bool = True,
) -> Iterator[None]:
    """Serialize ownership updates and readers that need a stable snapshot."""
    runtime_root.mkdir(parents=True, exist_ok=True)
    lock_path = runtime_root / ".vault-index-ownership.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        (lock_exclusive if exclusive else lock_shared)(fd, blocking=blocking)
        yield
    finally:
        unlock(fd)
        os.close(fd)


def _read_vault_index_ownership_strict(runtime_root: Path) -> dict[str, str]:
    path = runtime_root / VAULT_INDEX_OWNERSHIP_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except UnicodeDecodeError as exc:
        raise VaultIndexOwnershipUnreadable(
            None, f"ownership map is not valid UTF-8: {path}"
        ) from exc
    except OSError as exc:
        raise VaultIndexOwnershipUnreadable(
            getattr(exc, "errno", None),
            f"ownership map is unreadable: {path}",
        ) from exc
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise VaultIndexOwnershipUnreadable(
            getattr(exc, "errno", None), f"ownership map is truncated or malformed: {path}"
        ) from exc
    rows = payload.get("roots") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise VaultIndexOwnershipUnreadable(
            None, f"ownership map has no roots list: {path}"
        )
    recorded: dict[str, str] = {}
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("path"), str)
            or not row["path"]
            or row.get("owner") not in {"ciao", "external"}
        ):
            raise VaultIndexOwnershipUnreadable(
                None, f"ownership map has an invalid row: {path}"
            )
        recorded[row["path"]] = row["owner"]
    return recorded


def _atomic_write_vault_index_ownership(
    path: Path, recorded: dict[str, str],
) -> None:
    payload = {
        "roots": [
            {"path": recorded_path, "owner": recorded_owner}
            for recorded_path, recorded_owner in sorted(recorded.items())
        ]
    }
    fd, temp_name = tempfile.mkstemp(
        prefix=".vault-index-ownership-", suffix=".tmp", dir=path.parent,
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(json.dumps(payload, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def write_vault_index_ownership(
    runtime_root: Path, vault_roots: Sequence[Path], *, owner: str,
) -> Path:
    """Persist setup's authority for each aggregate index destination."""
    if owner not in {"ciao", "external"}:
        raise ValueError("vault index owner must be ciao or external")
    runtime_root.mkdir(parents=True, exist_ok=True)
    with vault_index_ownership_lock(runtime_root):
        path = runtime_root / VAULT_INDEX_OWNERSHIP_FILENAME
        if path.exists():
            _read_vault_index_ownership_strict(runtime_root)
        _atomic_write_vault_index_ownership(
            path,
            {str(root.expanduser().resolve()): owner for root in vault_roots},
        )
    return path


def record_vault_index_ownership(
    runtime_root: Path, root: Path, *, owner: str,
) -> Path:
    """Record one explicit ownership decision without dropping other rows."""
    if owner not in {"ciao", "external"}:
        raise ValueError("vault index owner must be ciao or external")
    runtime_root.mkdir(parents=True, exist_ok=True)
    with vault_index_ownership_lock(runtime_root):
        path = runtime_root / VAULT_INDEX_OWNERSHIP_FILENAME
        recorded = _read_vault_index_ownership_strict(runtime_root)
        recorded[str(Path(root).expanduser().resolve())] = owner
        _atomic_write_vault_index_ownership(path, recorded)
    return path


def read_vault_index_ownership_status(
    runtime_root: Path,
) -> tuple[dict[str, str], bool]:
    """The persisted rows and whether the map may be trusted.

    Missing is trusted-but-empty (legacy ambiguity for the caller to decide).
    A truncated, malformed, invalid-UTF-8 or unreadable map is untrusted and
    yields no grants.
    """
    if not runtime_root.is_dir():
        return {}, True
    try:
        with vault_index_ownership_lock(runtime_root, exclusive=False):
            return _read_vault_index_ownership_strict(runtime_root), True
    except OSError:
        return {}, False


def read_vault_index_ownership(runtime_root: Path) -> dict[str, str]:
    """Return the persisted ownership rows, or no grants when unreadable."""
    return read_vault_index_ownership_status(runtime_root)[0]


def read_vault_index_owned_roots(runtime_root: Path) -> frozenset[str]:
    """Return Ciao-owned index paths from setup's persisted ownership map."""
    return frozenset(
        path
        for path, owner in read_vault_index_ownership(runtime_root).items()
        if owner == "ciao"
    )


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


WELCOME_PENDING_FILENAME = "welcome-pending"


def welcome_pending_path(runtime_root: Path) -> Path:
    return runtime_root / WELCOME_PENDING_FILENAME


def write_welcome_pending(runtime_root: Path, *, now: datetime | None = None) -> Path:
    """Record that setup generated the PWA password and nobody has acknowledged it yet."""
    runtime_root.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    path = welcome_pending_path(runtime_root)
    path.write_text(f"{stamp}\n", encoding="utf-8", newline="")
    return path


def welcome_pending(runtime_root: Path) -> bool:
    return welcome_pending_path(runtime_root).is_file()


def clear_welcome_pending(runtime_root: Path) -> None:
    welcome_pending_path(runtime_root).unlink(missing_ok=True)
