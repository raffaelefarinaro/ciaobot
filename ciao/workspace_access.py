"""Detect a macOS privacy (TCC) denial of the workspace before the engine starts, and record it outside the workspace.

This runs first on every engine start, so it imports the standard library only
(``ciao.install_receipt`` is imported lazily and is itself light).
"""

from __future__ import annotations

import errno
import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

ACCESS_DENIED_EXIT = 78  # EX_CONFIG: not RESTART_EXIT_CODE (75), not the host's 1/2/130/143
DENIAL_FILE_NAME = "workspace-access-denied.txt"
_GENERIC_WORKSPACE = "your Ciaobot workspace folder"


def denial_path() -> Path:
    """A per-user file outside every workspace. ``scripts/install-engine.sh`` reads
    the same path (``$HOME/.local/state/ciaobot/workspace-access-denied.txt``);
    keep both in sync with ``DENIAL_FILE_NAME``."""
    from ciao.install_receipt import default_receipt_path

    return default_receipt_path().parent / DENIAL_FILE_NAME


def denial_message(workspace: str) -> str:
    return (
        f"macOS denied Ciaobot Server access to {workspace}: allow it in System Settings "
        "→ Privacy & Security → Files and Folders (Ciaobot Server). "
        "Ciaobot starts on its own a few seconds later."
    )


def read_denial(path: Path | None = None) -> str:
    """The recorded message, or "" when there is none or it cannot be read."""
    try:
        return (path or denial_path()).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def record_denial(message: str, path: Path | None = None) -> None:
    """Best effort: the probe result must not depend on being able to record it.

    Written like the install receipt: a private temp file renamed into place, so
    a symlink planted at the record path is replaced, never followed.
    """
    from ciao.os_support.files import replace_file
    from ciao.os_support.private import make_private_dir, mkstemp_private

    target = path or denial_path()
    tmp: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        make_private_dir(target.parent)
        fd, tmp_name = mkstemp_private(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
        tmp = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(message + "\n")
        replace_file(tmp, target)
    except OSError:
        pass
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def clear_denial(path: Path | None = None) -> None:
    try:
        (path or denial_path()).unlink(missing_ok=True)
    except OSError:
        pass


def check_workspace_access(
    *,
    platform: str | None = None,
    getcwd: Callable[[], str] = os.getcwd,
    scandir: Callable[[str], Any] = os.scandir,
    environ: Mapping[str, str] | None = None,
    path: Path | None = None,
) -> str | None:
    """darwin only: the denial message (recorded) or None (record cleared).

    A TCC denial is ``EPERM``; plain mode-bit denials are ``EACCES`` and propagate.
    """
    if (platform or sys.platform) != "darwin":
        return None
    env = os.environ if environ is None else environ
    try:
        cwd = getcwd()
        with scandir(cwd):
            pass
    except PermissionError as exc:
        if exc.errno != errno.EPERM:
            raise
        workspace = str(env.get("CIAO_WORKSPACE") or "").strip() or _GENERIC_WORKSPACE
        message = denial_message(workspace)
        record_denial(message, path)
        return message
    clear_denial(path)
    return None
