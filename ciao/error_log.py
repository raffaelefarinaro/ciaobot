"""Dedicated error log file for server errors.

A RotatingFileHandler is wired into the root logger so every ERROR+
record lands here.  A weekly schedule can tail the file, feed it to
Pi, and clear it after a successful run.
"""

from __future__ import annotations

import logging
import logging.handlers
from collections import deque
from pathlib import Path

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
