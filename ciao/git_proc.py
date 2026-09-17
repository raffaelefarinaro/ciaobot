"""Timeout-safe git subprocess spawning shared by the git helpers.

``git_sync`` and ``local_session`` both run git in the background and both
need the same thing on timeout: kill git *and* whatever it forked, reap the
child, and tear the pipes down.

The naive version — ``proc.kill()`` then return — leaks two file descriptors
per timeout and never releases them (issue #470). ``asyncio.wait_for`` cancels
``communicate()`` mid-read, and ``git push`` over SSH has already forked a
grandchild ``ssh`` that inherits the stdout/stderr pipe write ends. SIGKILL to
git alone leaves that grandchild alive holding the write ends, so the parent's
read ends never close and the transport is never torn down. Against an
unreachable remote the 30s backup loop exhausted a 256-fd launchd limit in
about an hour, after which every subprocess spawn failed with EMFILE.

The fix is to put git in its own process group (``start_new_session``) so the
group kill takes the grandchild with it, then await the child and close the
transport explicitly. The new session also means git has no controlling
terminal; both callers are server-side background paths with no TTY to prompt
on anyway, and a credential prompt there already failed rather than blocked.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

logger = logging.getLogger(__name__)

#: Stderr detail returned when a git command exceeds its timeout. Callers match
#: on this to decide whether a remote is unreachable, so keep it stable.
GIT_TIMEOUT_DETAIL = "git command timed out"

# Bounds the post-SIGKILL reap. SIGKILL is not catchable, so this only fires if
# the child is wedged in uninterruptible I/O; returning anyway beats hanging the
# caller, and the transport is closed either way.
_REAP_TIMEOUT = 5.0


async def _reap(proc: asyncio.subprocess.Process) -> None:
    """Kill ``proc``'s process group, wait for it, and close its pipes."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        # Already gone, or we could not address the group — fall back to the
        # child alone. Any grandchild then outlives it, but closing the
        # transport below still releases our own descriptors.
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=_REAP_TIMEOUT)
    except (asyncio.TimeoutError, ProcessLookupError):
        logger.warning("git subprocess %s did not exit after SIGKILL", proc.pid)
    finally:
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            transport.close()


async def run_git(
    workspace: Path, *args: str, timeout: float | None = None
) -> tuple[int, str, str]:
    """Run ``git *args`` in ``workspace``; return (returncode, stdout, stderr).

    On timeout returns ``(-1, "", GIT_TIMEOUT_DETAIL)``. Output is decoded but
    not trimmed — callers apply their own whitespace convention.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(workspace),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    if timeout is not None:
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await _reap(proc)
            return (-1, "", GIT_TIMEOUT_DETAIL)
    else:
        out, err = await proc.communicate()
    return (
        proc.returncode or 0,
        out.decode(errors="replace"),
        err.decode(errors="replace"),
    )
