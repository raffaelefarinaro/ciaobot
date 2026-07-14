"""Process-lifetime lock for one Ciaobot backend per runtime directory."""

from __future__ import annotations

import errno
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

try:  # pragma: no cover - Ciaobot's supported server platforms are Unix.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class WorkspaceAlreadyRunningError(RuntimeError):
    """Raised when another backend already owns a runtime directory."""


class WorkspaceInstanceLock:
    """Hold an advisory lock for the full lifetime of a server process.

    The registry's short-lived write lock prevents torn writes, but it cannot
    stop two long-running servers from applying incompatible discovery and
    migration decisions. This lock closes that larger race. The lock file is
    deliberately retained after shutdown: unlinking an advisory-lock file can
    let a third process lock a new inode while an older process still owns the
    original one.
    """

    def __init__(
        self,
        runtime_root: Path,
        *,
        workspace_root: Path,
        port: int,
    ) -> None:
        self.path = Path(runtime_root) / "server.lock"
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.port = int(port)
        self._handle: TextIO | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        if fcntl is None:  # pragma: no cover
            raise RuntimeError("Ciaobot's server lock requires Unix file locking support.")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                handle.close()
                raise
            owner = self._read_metadata(handle)
            handle.close()
            detail = ""
            if owner:
                pid = owner.get("pid", "unknown")
                port = owner.get("port", "unknown")
                started = owner.get("started_at", "unknown time")
                detail = f" (pid {pid}, port {port}, started {started})"
            raise WorkspaceAlreadyRunningError(
                "Another Ciaobot backend is already using runtime directory "
                f"{self.path.parent}{detail}. Stop it before starting another "
                "normal or development server for this workspace."
            ) from None

        self._handle = handle
        try:
            self._write_metadata(
                {
                    "pid": os.getpid(),
                    "port": self.port,
                    "workspace": str(self.workspace_root),
                    "runtime": str(self.path.parent.resolve()),
                    "started_at": _now_iso(),
                    "status": "running",
                }
            )
        except OSError:
            # The advisory lock itself is authoritative; owner metadata is
            # diagnostic and must not prevent the server from holding it.
            logger.exception("Failed to write server-lock metadata %s", self.path)

    @staticmethod
    def _read_metadata(handle: TextIO) -> dict:
        try:
            handle.seek(0)
            value = json.load(handle)
            return value if isinstance(value, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_metadata(self, payload: dict) -> None:
        handle = self._handle
        if handle is None:
            return
        handle.seek(0)
        handle.truncate()
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            try:
                self._write_metadata(
                    {
                        "pid": os.getpid(),
                        "port": self.port,
                        "workspace": str(self.workspace_root),
                        "runtime": str(self.path.parent.resolve()),
                        "stopped_at": _now_iso(),
                        "status": "stopped",
                    }
                )
            except OSError:
                logger.exception("Failed to update server-lock metadata %s", self.path)
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            self._handle = None

    def __enter__(self) -> WorkspaceInstanceLock:
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()
