"""Git sync helpers for the Ciaobot server.

Handles pulling latest changes on startup and merging before push.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


async def _git(workspace: Path, *args: str, timeout: float | None = None) -> tuple[int, str, str]:
    """Run a git command and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if timeout is not None:
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            return (-1, "", "git command timed out")
    else:
        stdout, stderr = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout.decode(errors="replace").strip(),
        stderr.decode(errors="replace").strip(),
    )


async def _is_git_repo(workspace: Path) -> bool:
    rc, out, _ = await _git(workspace, "rev-parse", "--is-inside-work-tree")
    return rc == 0 and out == "true"


async def sync_workspace(workspace: Path) -> str | None:
    """Pull latest changes. Called on startup.

    Returns a status message to send to the user, or None if nothing changed.
    """
    if not await _is_git_repo(workspace):
        return None

    # Auto-commit local changes so pull can handle them cleanly.
    rc, status_out, _ = await _git(workspace, "status", "--porcelain")
    has_changes = rc == 0 and bool(status_out.strip())

    if has_changes:
        await _git(workspace, "add", "-u")
        rc, _, err = await _git(
            workspace, "commit", "-m", "auto-commit before startup sync",
        )
        if rc != 0:
            logger.warning("Startup sync: auto-commit failed: %s", err)
            return f"Startup sync: failed to auto-commit local changes.\n{err}"

    # A fresh branch may not have an upstream yet (the backup-push loop sets
    # one with ``push -u`` once it succeeds). A bare ``git pull`` then
    # hard-fails with "no tracking information"; skip it rather than surface
    # that as a startup error.
    rc, _, _ = await _git(workspace, "rev-parse", "--abbrev-ref", "@{u}")
    if rc != 0:
        logger.info("Startup sync: branch has no upstream yet; skipping pull.")
        return None

    # Pull (merge-based, handles merge commits cleanly) with a 10s timeout
    rc, pull_out, pull_err = await _git(workspace, "pull", timeout=10.0)

    if rc != 0:
        logger.warning("Startup sync: pull failed: %s", pull_err)
        return f"Startup sync: pull failed.\n{pull_err}"

    # Only notify if new commits were pulled
    if "Already up to date" in pull_out:
        logger.info("Startup sync: already up to date.")
        return None

    logger.info("Startup sync: %s", pull_out)
    return f"Startup sync: pulled latest changes.\n{pull_out}"


async def pull_before_push(workspace: Path) -> tuple[bool, str]:
    """Pull before pushing. Used by snapshot to avoid push failures.

    Returns (success, error_message).
    """
    rc, out, err = await _git(workspace, "pull")
    if rc != 0:
        return False, err
    return True, ""
