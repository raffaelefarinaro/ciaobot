"""Timeout-safe git subprocess spawning shared by the git helpers.

``local_session`` runs git in the background and needs one thing on timeout: kill git *and* whatever it forked, reap the
child, and tear the pipes down.

The naive version — ``proc.kill()`` then return — leaks two file descriptors
per timeout and never releases them (issue #470). ``asyncio.wait_for`` cancels
``communicate()`` mid-read, and ``git push`` over SSH has already forked a
grandchild ``ssh`` that inherits the stdout/stderr pipe write ends. SIGKILL to
git alone leaves that grandchild alive holding the write ends, so the parent's
read ends never close and the transport is never torn down. Against an
unreachable remote the then-30s backup loop exhausted a 256-fd launchd limit
in about an hour, after which every subprocess spawn failed with EMFILE.

The fix is to put git in its own process tree (``tree_spawn_options``: its own
process group) so the tree kill takes the grandchild with it, then await the
child and close the transport explicitly.
The new session also means git has no controlling terminal; both callers are
server-side background paths with no TTY to prompt on anyway, and a
credential prompt there already failed rather than blocked.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from ciao.os_support.child_env import child_env
from ciao.os_support.processes import bounded_output
from ciao.os_support.processes import ProcessTree, tree_spawn_options

logger = logging.getLogger(__name__)

#: Prepended to every git the engine runs on a workspace or vault repo, so the
#: bytes it commits are the bytes on disk, on every OS (#696). The vault's
#: revisions and hashes assume exactly that, and the engine writes exact bytes
#: (``newline=""``); Git for Windows ships ``core.autocrlf=true`` in its system
#: config, which would store a CRLF note as an LF blob. On macOS and Linux the
#: default is already ``false``. ``ensure_workspace_git`` also writes it into the
#: repo's own config, so the user's git in that repo agrees.
EXACT_BYTES: tuple[str, ...] = ("-c", "core.autocrlf=false")

#: Stderr detail returned when a git command exceeds its timeout. Callers match
#: on this to decide whether a remote is unreachable, so keep it stable.
GIT_TIMEOUT_DETAIL = "git command timed out"

# Bounds the post-SIGKILL reap. SIGKILL is not catchable, so this only fires if
# the child is wedged in uninterruptible I/O; returning anyway beats hanging the
# caller, and the transport is closed either way.
_REAP_TIMEOUT = 5.0


@dataclass(frozen=True)
class GitCapture:
    returncode: int
    stdout: bytes
    stderr: bytes
    truncated: bool = False
    output_limited: bool = False


async def run_git_bounded(
    workspace: Path,
    *args: str,
    timeout: float,
    max_output: int,
    env: dict[str, str],
    stdin: bytes | None = None,
) -> GitCapture:
    """Run a fixed Git read with a cap enforced while its pipes are read."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(workspace), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        **tree_spawn_options(),
    )
    try:
        tree = ProcessTree(proc.pid)
    except OSError:
        proc.kill()
        await proc.wait()
        raise
    if max_output <= 0:
        await asyncio.shield(_reap(proc, tree))
        return GitCapture(-1, b"", b"", True)
    assert proc.stdout is not None and proc.stderr is not None
    assert stdin is None or proc.stdin is not None
    captured = [bytearray(), bytearray()]
    total = 0
    exceeded = asyncio.Event()

    async def drain(stream: asyncio.StreamReader, slot: int) -> None:
        nonlocal total
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                return
            room = max_output + 1 - total
            if room > 0:
                take = chunk[:room]
                captured[slot].extend(take)
                total += len(take)
            if total > max_output:
                exceeded.set()
                return

    readers = [asyncio.create_task(drain(proc.stdout, 0)), asyncio.create_task(drain(proc.stderr, 1))]
    wait_task = asyncio.create_task(proc.wait())
    cap_task = asyncio.create_task(exceeded.wait())

    async def send_input() -> None:
        if stdin is None or proc.stdin is None:
            return
        try:
            proc.stdin.write(stdin)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.stdin.close()
            try:
                await proc.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

    input_task = asyncio.create_task(send_input()) if stdin is not None else None
    try:
        done, _ = await asyncio.wait(
            [wait_task, cap_task],
            timeout=timeout, return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            await asyncio.shield(_reap(proc, tree))
            return GitCapture(-1, b"", b"git command timed out")
        if exceeded.is_set():
            await asyncio.shield(_reap(proc, tree))
            return GitCapture(-1, bytes(captured[0][:max_output]), bytes(captured[1]), True)
        await asyncio.gather(*readers)
        if exceeded.is_set():
            tree.close()
            return GitCapture(-1, bytes(captured[0][:max_output]), bytes(captured[1]), True)
        if input_task is not None:
            await input_task
        tree.close()
        return GitCapture(proc.returncode or 0, bytes(captured[0]), bytes(captured[1]))
    except BaseException:
        await asyncio.shield(_reap(proc, tree))
        raise
    finally:
        cleanup_tasks = (wait_task, cap_task)
        for task in cleanup_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)
        if input_task is not None:
            if not input_task.done():
                input_task.cancel()
            await asyncio.gather(input_task, return_exceptions=True)
        for reader in readers:
            if not reader.done():
                reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)


async def run_git_stream(
    workspace: Path,
    *args: str,
    timeout: float,
    max_stderr: int,
    env: dict[str, str],
    on_stdout: Callable[[bytes], Awaitable[None]],
) -> GitCapture:
    """Drain stdout incrementally without retaining it; stderr remains capped."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(workspace), env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        **tree_spawn_options(),
    )
    try:
        tree = ProcessTree(proc.pid)
    except OSError:
        proc.kill()
        await proc.wait()
        raise
    assert proc.stdout is not None and proc.stderr is not None
    stdout, stderr = proc.stdout, proc.stderr
    stderr_data = bytearray()
    exceeded = asyncio.Event()

    async def stream_stdout() -> None:
        while chunk := await stdout.read(8192):
            await on_stdout(chunk)

    async def drain_stderr() -> None:
        while chunk := await stderr.read(8192):
            room = max_stderr + 1 - len(stderr_data)
            if room > 0:
                stderr_data.extend(chunk[:room])
            if len(stderr_data) > max_stderr:
                exceeded.set()
                return

    stdout_task = asyncio.create_task(stream_stdout())
    stderr_task = asyncio.create_task(drain_stderr())
    wait_task = asyncio.create_task(proc.wait())
    cap_task = asyncio.create_task(exceeded.wait())
    try:
        deadline = asyncio.get_running_loop().time() + timeout
        while not wait_task.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                await asyncio.shield(_reap(proc, tree))
                return GitCapture(-1, b"", b"git command timed out")
            done, _ = await asyncio.wait(
                [wait_task, stdout_task, cap_task], timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                await asyncio.shield(_reap(proc, tree))
                return GitCapture(-1, b"", b"git command timed out")
            if exceeded.is_set():
                await asyncio.shield(_reap(proc, tree))
                return GitCapture(-1, b"", bytes(stderr_data[:max_stderr]), True)
            if stdout_task.done() and (error := stdout_task.exception()) is not None:
                await asyncio.shield(_reap(proc, tree))
                raise error
        await asyncio.gather(stdout_task, stderr_task)
        if exceeded.is_set():
            tree.close()
            return GitCapture(-1, b"", bytes(stderr_data[:max_stderr]), True)
        tree.close()
        return GitCapture(proc.returncode or 0, b"", bytes(stderr_data))
    except BaseException:
        await asyncio.shield(_reap(proc, tree))
        raise
    finally:
        for task in (wait_task, cap_task, stdout_task, stderr_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(wait_task, cap_task, stdout_task, stderr_task, return_exceptions=True)


async def _reap(proc: asyncio.subprocess.Process, tree: ProcessTree) -> None:
    """Kill ``proc``'s process tree, wait for it, and close its pipes.

    Exception-safe by design: this runs on timeout/cancel cleanup paths whose
    callers promise a stable ``(-1, "", GIT_TIMEOUT_DETAIL)`` result, so a
    failing kill or close must never escape and replace it. It closes the
    tree itself, last, because a shielded reap can outlive its caller.
    """
    try:
        tree.kill()
    except (ProcessLookupError, PermissionError, OSError):
        # Already gone, or we could not address the group — fall back to the
        # child alone. Any grandchild then outlives it, but closing the
        # transport below still releases our own descriptors.
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=_REAP_TIMEOUT)
    except (asyncio.TimeoutError, ProcessLookupError):
        logger.warning("git subprocess %s did not exit after SIGKILL", proc.pid)
    finally:
        tree.close()
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            try:
                transport.close()
            except Exception:
                logger.warning(
                    "error closing git transport for pid %s", proc.pid,
                    exc_info=True,
                )


def run_git_sync(
    workspace: Path, *args: str, stdin: str | None = None
) -> tuple[int, str, str]:
    """Run ``git *args`` in ``workspace`` synchronously; (rc, stdout, stderr).

    The read-only counterpart of :func:`run_git`, for the quick questions
    ("which branch is this checkout on?", "is this inside a repo?", "where is
    the index lock?", "which of these paths is git ignoring?") that must be
    answerable from a synchronous context — property accessors, and the off-loop
    thread that resolves a canonical repository before taking a mutation lock.
    Output is decoded but not trimmed; callers apply their own whitespace
    convention.

    ``stdin`` is written to the child when given, for the commands that read
    their arguments from it rather than argv (``git check-ignore -z --stdin``
    only accepts ``-z`` in that form, and a path list is exactly the payload too
    large to trust to ``ARG_MAX``). Left unset, the child inherits this
    process's stdin, which is what every read-only question wants.

    A spawn failure (no ``git`` on PATH, unusable cwd) is reported as
    ``(1, "", str(exc))`` rather than raised: every caller of this helper asks
    git a yes/no question and answers "no" on failure.
    """
    try:
        proc = subprocess.run(
            ["git", *EXACT_BYTES, *args],
            cwd=str(workspace),
            capture_output=True,
            text=True, encoding="utf-8",
            input=stdin,
            env=child_env(),
        )
    except OSError as exc:
        return 1, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def run_git_sync_bounded(
    workspace: Path,
    *args: str,
    timeout: float,
    max_output: int,
    env: dict[str, str],
) -> GitCapture:
    """Run a synchronous Git read with a pipe-timeout and a hard stdout cap."""
    result = bounded_output(
        ["git", *EXACT_BYTES, *args],
        timeout=timeout,
        max_stdout=max_output,
        cwd=workspace,
        env=env,
        report_limit=True,
    )
    if result is None:
        return GitCapture(-1, b"", b"git command timed out or exceeded its output limit", True)
    if len(result) == 3:
        return GitCapture(result[0], result[1], b"git output exceeded its limit", True, result[2])
    return GitCapture(result[0], result[1], b"")


async def run_git(
    workspace: Path,
    *args: str,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run ``git *args`` in ``workspace``; return (returncode, stdout, stderr).

    On timeout returns ``(-1, "", GIT_TIMEOUT_DETAIL)``. Output is decoded but
    not trimmed — callers apply their own whitespace convention. ``env`` replaces
    the default child environment when given.
    """
    proc = await asyncio.create_subprocess_exec(
        "git",
        *EXACT_BYTES,
        *args,
        cwd=str(workspace),
        env=child_env() if env is None else env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **tree_spawn_options(),
    )
    try:
        tree = ProcessTree(proc.pid)
    except OSError:
        # A git that cannot be killed as a tree would bring back #470.
        proc.kill()
        await proc.wait()
        raise
    if timeout is not None:
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await _reap(proc, tree)
            return (-1, "", GIT_TIMEOUT_DETAIL)
        except (asyncio.CancelledError, Exception):
            # Cancellation (e.g. server shutdown) or a communicate() failure
            # must reap the child too, or the same descriptor leak as the
            # timeout path recurs (issue #470). Shield the reap so a second
            # cancel arriving mid-cleanup does not interrupt it.
            await asyncio.shield(_reap(proc, tree))
            raise
        tree.close()
    else:
        try:
            out, err = await proc.communicate()
        finally:
            tree.close()
    return (
        proc.returncode or 0,
        out.decode(errors="replace"),
        err.decode(errors="replace"),
    )
