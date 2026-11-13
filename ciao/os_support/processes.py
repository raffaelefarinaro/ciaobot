"""Process trees: start a child so that everything it starts can be stopped.

A caller spawns with ``**tree_spawn_options()`` and wraps the new pid in a
``ProcessTree`` straight away. ``terminate()`` asks the tree to stop and
``kill()`` ends it, grandchildren included (``git push`` forks ``ssh``).

POSIX is the process group exactly as the call sites used it before this
module existed: ``start_new_session=True`` makes the child a group leader
whose pid is the pgid, and ``terminate``/``kill`` are ``os.killpg`` with
``SIGTERM``/``SIGKILL``. The group outlives its leader, so both still reach
the descendants after the leader has exited.

Windows has no process groups that can be signalled from outside a console.
The child is started with ``CREATE_NEW_PROCESS_GROUP`` and assigned to a Job
Object as soon as it exists; everything it starts afterwards joins the job,
and ``kill()`` is ``TerminateJobObject``. ``terminate()`` sends
``CTRL_BREAK_EVENT`` to the child's group, which only reaches it when it
shares this process's console; without one there is no graceful stop, and
``terminate`` does nothing, so the ``kill()`` every caller issues after its
grace period is what ends the tree. The job does not kill on close: like a
POSIX session, a tree outlives the ``ProcessTree`` that tracked it.

``dies_with_engine=True`` is for helpers that exist only while the engine
does (the OpenCode server). On POSIX such a child stays in the engine's own
process group, as it always did, so launchd (which kills the job's group when
the engine exits, ``AbandonProcessGroup`` unset) and a terminal's Ctrl-C still
reach it; ``terminate``/``kill`` then signal that one pid. On Windows its Job
Object is ``KILL_ON_JOB_CLOSE``: the job handle closes when the engine exits,
crash included, and takes the whole tree with it.

The assignment happens after the child exists, so a grandchild started in the
microseconds between ``CreateProcess`` and ``AssignProcessToJobObject`` would
escape the job. Neither git nor a shell starts children that fast. Closing the
window needs a suspended spawn (create suspended, assign, resume), which
``asyncio.create_subprocess_exec`` cannot do. A child that has already exited
when the assignment runs is refused by Windows (``ERROR_ACCESS_DENIED``); its
tree is then empty rather than an error.
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

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def _declare(name: str, restype: Any, *argtypes: Any) -> Any:
        func = getattr(_kernel32, name)
        func.argtypes = list(argtypes)
        func.restype = restype
        return func

    _CreateJobObjectW = _declare(
        "CreateJobObjectW", wintypes.HANDLE, ctypes.c_void_p, wintypes.LPCWSTR
    )
    _OpenProcess = _declare(
        "OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD
    )
    _AssignProcessToJobObject = _declare(
        "AssignProcessToJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE
    )
    _TerminateJobObject = _declare(
        "TerminateJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.UINT
    )
    _CloseHandle = _declare("CloseHandle", wintypes.BOOL, wintypes.HANDLE)
    _SetInformationJobObject = _declare(
        "SetInformationJobObject", wintypes.BOOL,
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    )
    _GetExitCodeProcess = _declare(
        "GetExitCodeProcess", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)
    )
    _PeekNamedPipe = _declare(
        "PeekNamedPipe", wintypes.BOOL, wintypes.HANDLE, ctypes.c_void_p,
        wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    )

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _ERROR_ACCESS_DENIED = 5
    _STILL_ACTIVE = 259
    _KILLED_EXIT_CODE = 1

    def _has_exited(process: Any) -> bool:
        """True when ``process`` (an open handle) has already terminated."""
        code = wintypes.DWORD()
        if not _GetExitCodeProcess(process, ctypes.byref(code)):
            return False
        return code.value != _STILL_ACTIVE

    def tree_spawn_options(*, dies_with_engine: bool = False) -> dict[str, Any]:
        """Keyword arguments for ``Popen``/``create_subprocess_exec``."""
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}

    class ProcessTree:
        """A child spawned with ``tree_spawn_options()`` and all it starts."""

        def __init__(self, pid: int, *, dies_with_engine: bool = False) -> None:
            self.pid = pid
            job = _CreateJobObjectW(None, None)
            if not job:
                raise ctypes.WinError(ctypes.get_last_error())
            if dies_with_engine:
                limits = _ExtendedLimits()
                limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                if not _SetInformationJobObject(
                    job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    ctypes.byref(limits), ctypes.sizeof(limits),
                ):
                    error = ctypes.get_last_error()
                    _CloseHandle(job)
                    raise ctypes.WinError(error)
            process = _OpenProcess(
                _PROCESS_SET_QUOTA | _PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION,
                False, pid,
            )
            if not process:
                error = ctypes.get_last_error()
                _CloseHandle(job)
                raise ctypes.WinError(error)
            try:
                if not _AssignProcessToJobObject(job, process):
                    error = ctypes.get_last_error()
                    # A child that has already exited cannot join a job: Windows
                    # answers ERROR_ACCESS_DENIED. Its tree is then empty, and
                    # kill() on an empty job ends nothing, which is right.
                    if not (error == _ERROR_ACCESS_DENIED and _has_exited(process)):
                        _CloseHandle(job)
                        raise ctypes.WinError(error)
            finally:
                _CloseHandle(process)
            self._job: int | None = job

        def terminate(self) -> None:
            """Ask the tree to stop; see the module docstring."""
            try:
                os.kill(self.pid, signal.CTRL_BREAK_EVENT)
            except OSError:
                pass  # no shared console: the caller's kill() ends the tree

        def kill(self) -> None:
            """End every process in the tree."""
            if self._job is None:
                raise OSError("the process tree is closed")
            if not _TerminateJobObject(self._job, _KILLED_EXIT_CODE):
                raise ctypes.WinError(ctypes.get_last_error())

        def kill_descendants(self) -> None:
            """After the leader has exited: end whatever it left in the tree."""
            self.kill()

        def close(self) -> None:
            """Stop tracking the tree; the processes keep running."""
            if self._job is not None:
                _CloseHandle(self._job)
                self._job = None

else:

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
        if sys.platform != "win32":
            os.set_blocking(fd, False)
            selector = selectors.DefaultSelector()
            selector.register(fd, selectors.EVENT_READ)
        else:
            import msvcrt

            handle = msvcrt.get_osfhandle(fd)
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
            if sys.platform == "win32":
                available = wintypes.DWORD()
                if not _PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
                    error = ctypes.get_last_error()
                    if error == 109:  # ERROR_BROKEN_PIPE
                        eof = True
                        continue
                    raise ctypes.WinError(error)
                if not available.value:
                    threading.Event().wait(min(remaining, 0.05))
                    continue
                count = min(available.value, max_stdout + 1 - len(data))
            else:
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
# that with execve, exactly as the wrapper did before. Windows has no exec: its
# os.execve starts a new process and ends this one at once with exit code 0,
# so the caller reads success before the tool has run. There the child is
# started, waited for and its exit code returned; Ctrl+C reaches the child
# from the shared console, so the wait keeps going until the child decides.
if sys.platform == "win32":

    def hand_off(executable: str, argv: list[str], env: dict[str, str]) -> int:
        """Run ``argv`` in this process's place and return its exit code."""
        with subprocess.Popen(argv, executable=executable, env=env) as child:
            while True:
                try:
                    return child.wait()
                except KeyboardInterrupt:
                    continue

else:

    def hand_off(executable: str, argv: list[str], env: dict[str, str]) -> int:
        """Replace this process with ``argv`` (``os.execve``); does not return."""
        os.execve(executable, argv, env)
