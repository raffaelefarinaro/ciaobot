"""Process trees: start a child so that everything it starts can be stopped.

A caller spawns with ``**tree_spawn_options()`` and wraps the new pid in a
``ProcessTree`` straight away. ``terminate()`` asks the tree to stop and
``kill()`` ends it, grandchildren included (``git push`` forks ``ssh``; a
background run forks whatever its command does).

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
``asyncio.create_subprocess_exec`` cannot do.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
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
    _KILLED_EXIT_CODE = 1

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
            process = _OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
            if not process:
                error = ctypes.get_last_error()
                _CloseHandle(job)
                raise ctypes.WinError(error)
            try:
                if not _AssignProcessToJobObject(job, process):
                    error = ctypes.get_last_error()
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
