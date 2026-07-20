"""Dedicated error log file for server errors.

A RotatingFileHandler is wired into the root logger so every ERROR+
record lands here.  A weekly schedule can tail the file, feed it to
Pi, and clear it after a successful run.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)

ERROR_LOG_NAME = "server_errors.log"
ERROR_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
ERROR_LOG_BACKUP_COUNT = 3


def setup_error_logging(workspace_root: Path) -> None:
    """Attach a rotating file handler for ERROR+ to the root logger."""
    log_dir = workspace_root / ".runtime"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / ERROR_LOG_NAME

    root = logging.getLogger()
    # Guard against duplicate handlers on reload / multiple calls
    for h in root.handlers:
        if isinstance(h, logging.handlers.RotatingFileHandler) and getattr(h, "baseFilename", "") == str(log_path):
            return

    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=ERROR_LOG_MAX_BYTES,
        backupCount=ERROR_LOG_BACKUP_COUNT,
    )
    handler.setLevel(logging.ERROR)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)


def _is_benign_sdk_write_error(exc: BaseException | None) -> bool:
    """True for the Claude Agent SDK's harmless closed-transport write error.

    The SDK answers control requests (permission / hook callbacks) on
    fire-and-forget tasks it owns. When the CLI subprocess transport has
    already closed, ``Query._handle_control_request`` raises
    ``CLIConnectionError('ProcessTransport is not ready for writing')`` on both
    the success and the error write path. Matched by type name + message so we
    don't have to import the SDK here.
    """
    if exc is None:
        return False
    if type(exc).__name__ != "CLIConnectionError":
        return False
    return "not ready for writing" in str(exc).lower()


def install_asyncio_noise_filter() -> None:
    """Demote a known-benign orphaned-task error out of ``server_errors.log``.

    Nobody awaits the SDK's control-request task, so when it dies on a closed
    transport asyncio's default handler logs it at ERROR ("Task exception was
    never retrieved") — flooding the error log even though the visible turn
    already completed fine. Install a loop exception handler that demotes just
    that signature to debug and delegates everything else to the previous
    handler. Requires a running loop. See issue #163.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    previous = loop.get_exception_handler()

    def handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
        if _is_benign_sdk_write_error(context.get("exception")):
            logger.debug(
                "Suppressed benign Claude SDK control-task error: %s",
                context.get("exception"),
            )
            return
        if previous is not None:
            previous(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(handler)


def tail_error_log(workspace_root: Path, lines: int = 200) -> str:
    """Return the last *lines* of the error log, or an empty string."""
    log_path = workspace_root / ".runtime" / ERROR_LOG_NAME
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as f:
            last = deque(f, maxlen=max(1, lines))
        return "".join(last)
    except OSError:
        return ""


def clear_error_log(workspace_root: Path) -> None:
    """Truncate the error log so the next week starts fresh."""
    log_path = workspace_root / ".runtime" / ERROR_LOG_NAME
    try:
        log_path.write_text("", encoding="utf-8")
    except OSError:
        pass
