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

    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100
    _KILLED_EXIT_CODE = 1

    def tree_spawn_options() -> dict[str, Any]:
        """Keyword arguments for ``Popen``/``create_subprocess_exec``."""
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}

    class ProcessTree:
        """A child spawned with ``tree_spawn_options()`` and all it starts."""

        def __init__(self, pid: int) -> None:
            self.pid = pid
            job = _CreateJobObjectW(None, None)
            if not job:
                raise ctypes.WinError(ctypes.get_last_error())
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

        def close(self) -> None:
            """Stop tracking the tree; the processes keep running."""
            if self._job is not None:
                _CloseHandle(self._job)
                self._job = None

else:

    def tree_spawn_options() -> dict[str, Any]:
        """Keyword arguments for ``Popen``/``create_subprocess_exec``."""
        return {"start_new_session": True}

    class ProcessTree:
        """A child spawned with ``tree_spawn_options()`` and all it starts."""

        def __init__(self, pid: int) -> None:
            self.pid = pid

        def terminate(self) -> None:
            """Ask the tree to stop: ``SIGTERM`` to the process group."""
            os.killpg(self.pid, signal.SIGTERM)

        def kill(self) -> None:
            """End every process in the tree: ``SIGKILL`` to the process group."""
            os.killpg(self.pid, signal.SIGKILL)

        def close(self) -> None:
            """Stop tracking the tree; nothing to release on POSIX."""
