"""Process trees: start a child so that everything it starts can be stopped.

A caller spawns with ``**tree_spawn_options()`` and wraps the new pid in a
``ProcessTree`` straight away. ``terminate()`` asks the tree to stop and
``kill()`` ends it, grandchildren included (``git push`` forks ``ssh``).

POSIX is the process group exactly as the call sites used it before this
module existed: ``start_new_session=True`` makes the child a group leader
whose pid is the pgid, and ``terminate``/``kill`` are ``os.killpg`` with
``SIGTERM``/``SIGKILL``. The group outlives its leader, so both still reach
the descendants after the leader has exited.

``dies_with_engine=True`` is for helpers that exist only while the engine
does (the OpenCode server). On POSIX such a child stays in the engine's own
process group, as it always did, so launchd (which kills the job's group when
the engine exits, ``AbandonProcessGroup`` unset) and a terminal's Ctrl-C still
reach it; ``terminate``/``kill`` then signal that one pid.
"""

from __future__ import annotations

import os
import errno
import selectors
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from typing import Any


def isolated_probe_flags() -> list[str]:
    """``-I`` for a child interpreter, plus ``-B`` when this process writes no bytecode.

    ``-I`` ignores PYTHONDONTWRITEBYTECODE, so a child launched from the bundled
    engine must repeat ``-B`` itself or it would write __pycache__ into the
    sealed prefix. A dev or venv process gets plain ``["-I"]``.
    """
    return ["-I", "-B"] if sys.flags.dont_write_bytecode else ["-I"]


def python_isolation_flags() -> list[str]:
    """Interpreter flags a re-exec must repeat to keep this process's isolation.

    Only an isolated interpreter (the bundled engine runs ``-I -B``) gets any:
    ``-I`` ignores the environment, so ``-B`` must be explicit too. Every other
    interpreter returns [] and the re-exec argv is unchanged.
    """
    if not sys.flags.isolated:
        return []
    return isolated_probe_flags()


def pid_alive(pid: int) -> bool:
    """Conservatively check a PID without sending it a signal."""
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, OverflowError):
        return True
    return True


def tree_spawn_options(*, dies_with_engine: bool = False) -> dict[str, Any]:
    """Keyword arguments for ``Popen``/``create_subprocess_exec``."""
    return {} if dies_with_engine else {"start_new_session": True}

class ProcessTree:
    """A child spawned with ``tree_spawn_options()`` and all it starts."""

    def __init__(self, pid: int, *, dies_with_engine: bool = False) -> None:
        self.pid = pid
        self._group = not dies_with_engine

    def terminate(self) -> None:
        """Ask the tree to stop: ``SIGTERM`` to the group, or to the one child."""
        if self._group:
            os.killpg(self.pid, signal.SIGTERM)
        else:
            os.kill(self.pid, signal.SIGTERM)

    def kill(self) -> None:
        """End the tree: ``SIGKILL`` to the group, or to the one child."""
        if self._group:
            os.killpg(self.pid, signal.SIGKILL)
        else:
            os.kill(self.pid, signal.SIGKILL)

    def kill_descendants(self) -> None:
        """After the leader has exited: end whatever it left in the tree.

        The group outlives its leader, so ``killpg`` still reaches it. A
        ``dies_with_engine`` child shares the engine's group instead, and its
        pid may already belong to an unrelated process, so there is nothing
        this can safely signal.
        """
        if self._group:
            os.killpg(self.pid, signal.SIGKILL)

    def close(self) -> None:
        """Stop tracking the tree; nothing to release on POSIX."""


def _reap_process(process: subprocess.Popen[bytes]) -> None:
    try:
        process.wait()
    except Exception:
        pass


def bounded_output(
    argv: Sequence[str], *, timeout: float, max_stdout: int
) -> tuple[int, bytes] | None:
    """Collect bounded stdout; tree cleanup is best effort, not a sandbox."""
    deadline = time.monotonic() + timeout
    process: subprocess.Popen[bytes] | None = None
    tree: ProcessTree | None = None
    selector: selectors.BaseSelector | None = None
    result: tuple[int, bytes] | None = None
    reaped = False
    live = False
    cleanup_failed = False
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0, **tree_spawn_options(),
        )
        tree = ProcessTree(process.pid)
        assert process.stdout is not None
        fd = process.stdout.fileno()
        os.set_blocking(fd, False)
        selector = selectors.DefaultSelector()
        selector.register(fd, selectors.EVENT_READ)
        data = bytearray()
        eof = False
        code: int | None = None
        while True:
            code = process.poll()
            reaped = code is not None
            live = not reaped
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if eof and code is not None:
                result = (code, bytes(data))
                break
            if eof:
                threading.Event().wait(min(remaining, 0.05))
                continue
            assert selector is not None
            if not selector.select(min(remaining, 0.05)):
                continue
            count = min(65536, max_stdout + 1 - len(data))
            chunk = os.read(fd, count)
            if not chunk:
                eof = True
            else:
                data.extend(chunk)
                if len(data) > max_stdout:
                    break
    except Exception:
        result = None
    finally:
        if process is not None:
            # A live poll is the last operation before the group signal: once
            # reaped, the numeric group ID can already belong to someone else.
            if not reaped:
                if not live:
                    try:
                        live = process.poll() is None
                        reaped = not live
                    except Exception:
                        cleanup_failed = True
                        # The group cannot safely be signalled without a live
                        # poll, but Popen still owns the leader's process handle.
                        try:
                            process.kill()
                        except Exception:
                            pass
                if live:
                    if tree is not None:
                        try:
                            tree.kill()
                        except OSError as exc:
                            if exc.errno != errno.ESRCH:
                                cleanup_failed = True
                        except Exception:
                            cleanup_failed = True
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    except Exception:
                        cleanup_failed = True
            if selector is not None:
                try:
                    selector.close()
                except Exception:
                    cleanup_failed = True
            if process.stdout is not None:
                try:
                    process.stdout.close()
                except Exception:
                    cleanup_failed = True
                process.stdout = None
            if not reaped:
                try:
                    process.wait(timeout=1.0)
                    reaped = True
                except Exception:
                    cleanup_failed = True
                    try:
                        threading.Thread(target=_reap_process, args=(process,), daemon=True).start()
                    except Exception:
                        # No bounded way remains to guarantee this leader is reaped.
                        pass
        if tree is not None:
            try:
                tree.close()
            except Exception:
                cleanup_failed = True
    return None if cleanup_failed else result


# ── Handing this process over to another program ───────────────────────────
# A thin CLI wrapper (`ciao gws`) prepares an environment and then becomes the
# real tool, so the caller sees the tool's output and exit code. POSIX does
# that with execve, exactly as the wrapper did before.


def hand_off(executable: str, argv: list[str], env: dict[str, str]) -> int:
    """Replace this process with ``argv`` (``os.execve``); does not return."""
    os.execve(executable, argv, env)
