"""Dedicated error and debug log files for the server.

Two RotatingFileHandlers are wired into the root logger:

- ``server_errors.log`` always receives every ERROR+ record. A schedule can
  tail the file, feed it to an error-triage automation, and clear it after a
  successful run.
- ``server_debug.log`` receives everything DEBUG+ when ``CIAO_LOG_LEVEL=debug``
  is set, giving verbose runtime detail (provider stderr noise, lifecycle
  events) that survives console rotation and is surfaced through the debug
  issue report so the agent can inspect its own behavior.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)

ERROR_LOG_NAME = "server_errors.log"
ERROR_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
ERROR_LOG_BACKUP_COUNT = 3

DEBUG_LOG_NAME = "server_debug.log"
DEBUG_LOG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
DEBUG_LOG_BACKUP_COUNT = 2


def resolve_log_level() -> int:
    """Resolve the effective root-logger level from ``CIAO_LOG_LEVEL``.

    Accepts standard level names (case-insensitive) or numeric values;
    anything unrecognized falls back to INFO with a warning so a typo can
    never silence or flood logging by accident.
    """
    raw = os.environ.get("CIAO_LOG_LEVEL", "").strip()
    if not raw:
        return logging.INFO
    if raw.isdigit():
        return int(raw)
    resolved = logging.getLevelName(raw.upper())
    if isinstance(resolved, int):
        return resolved
    logger.warning(
        "Unrecognized CIAO_LOG_LEVEL %r; falling back to INFO", raw
    )
    return logging.INFO


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


def setup_debug_logging(workspace_root: Path, level: int | None = None) -> None:
    """Attach a rotating DEBUG file handler when verbose logging is enabled.

    A no-op unless the resolved level (``CIAO_LOG_LEVEL``, default INFO) is
    DEBUG or finer, so default installs keep exactly today's behavior. The
    root logger must already be at that level for records to reach the
    handler; ``main()`` configures that from the same env var.
    """
    effective = resolve_log_level() if level is None else level
    if effective > logging.DEBUG:
        return

    root = logging.getLogger()
    if root.level > effective:
        root.setLevel(effective)

    log_dir = workspace_root / ".runtime"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / DEBUG_LOG_NAME
    for h in root.handlers:
        if isinstance(h, logging.handlers.RotatingFileHandler) and getattr(h, "baseFilename", "") == str(log_path):
            return

    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=DEBUG_LOG_MAX_BYTES,
        backupCount=DEBUG_LOG_BACKUP_COUNT,
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    logger.info("Debug logging enabled (%s)", log_path)


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
    return _tail_log(workspace_root / ".runtime" / ERROR_LOG_NAME, lines)


def tail_debug_log(workspace_root: Path, lines: int = 200) -> str:
    """Return the last *lines* of the debug log, or an empty string.

    Empty unless ``CIAO_LOG_LEVEL=debug`` produced a log this session.
    """
    return _tail_log(workspace_root / ".runtime" / DEBUG_LOG_NAME, lines)


def _tail_log(log_path: Path, lines: int) -> str:
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
