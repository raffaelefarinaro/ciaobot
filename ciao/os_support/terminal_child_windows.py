"""The helper that starts the terminal's shell with the interrupt cleared.

Windows has no ``exec``: a child process cannot *become* the shell the way
the POSIX helper does. What Windows has instead is an inheritable attribute
— the "ignore Ctrl+C" flag — that decides whether the operating system
delivers a console control event to a process at all. The flag is set with
``SetConsoleCtrlHandler(NULL, TRUE)``, cleared with
``SetConsoleCtrlHandler(NULL, FALSE)``, and **every child inherits it from
its parent**.

That attribute is why this helper exists. The engine inherits it from
whatever started it — a POSIX-flavoured shell on a build machine sets it, so
that its own Ctrl+C arrives as a signal and not as a console control event —
and the shell a terminal spawns inherits it from the engine. The operating
system then skips that shell when the console host delivers the interrupt:
no handler runs, no read is aborted, and the shell survives an event it
never received. Nothing in the shell can undo that, because the attribute
belongs to the process and is read by the operating system, not by the
program.

Only a fresh process can clear the attribute *for the shell* without
changing the engine's own: the helper clears it in itself, creates the
shell, and the shell inherits the cleared state. The engine's attribute is
never touched, so the engine keeps behaving exactly as it did.

The rules it follows, and why:

- It is attached to the pseudoconsole, so it has a console to set the
  attribute on, and it is the shell's parent, so the shell inherits the
  attribute in the state the helper left it.
- The shell inherits the helper's **console**. A process attached to a
  console passes it on to its children, and the shell is created neither
  with ``CREATE_NEW_CONSOLE`` nor with ``DETACHED_PROCESS``, so the shell
  lands on the very pseudoconsole the helper is attached to. The
  pseudoconsole's handle therefore never has to reach the helper, and
  nothing is inherited by anybody: the handle is not an ordinary kernel
  handle and cannot be made inheritable in the first place.
- It installs a handler of its own that swallows the control event. The
  helper is attached to the same console and shares the shell's process
  group, so the interrupt reaches the helper too, and the helper has to
  outlive the event it hands on.
- It imports nothing from ``ciao`` and runs under ``-I -S``, so no user or
  site initialisation runs and no credential is read on the way in.
- It writes nothing to stdout or stderr, and it never prints a traceback:
  the pseudoconsole carries the shell's output, and a traceback here would
  land in the user's terminal. A failure is a status, and only a status.

The Windows-only parts live behind ``if sys.platform == "win32"``, so the
module imports on POSIX without a single fake stub.
"""

from __future__ import annotations

import sys

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    from typing import Any

    #: The control handling could not be set up: the ignore-Ctrl+C attribute
    #: was not cleared, or the helper's own handler was not installed.
    CONTROL_FAILED = 125
    #: The shell could not be created.
    CREATE_FAILED = 126
    #: The shell could not be waited for.
    WAIT_FAILED = 127

    # STARTF_USESTDHANDLES (winbase.h): the shell's three standard-handle
    # slots stay null, so the console's own handles are used and not the
    # engine's.
    _STARTF_USESTDHANDLES = 0x00000100
    # Process and thread state (winbase.h, synchapi.h).
    _INFINITE = 0xFFFFFFFF

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def _declare(name: str, restype: Any, *argtypes: Any) -> Any:
        """Bind one Kernel32 function with its SDK signature."""
        func = getattr(_kernel32, name)
        func.argtypes = list(argtypes)
        func.restype = restype
        return func

    class _StartupInfo(ctypes.Structure):
        """``STARTUPINFOW`` exactly as the SDK declares it."""

        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class _ProcessInformation(ctypes.Structure):
        """``PROCESS_INFORMATION``: the new process and its first thread."""

        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    _SetConsoleCtrlHandler = _declare(
        "SetConsoleCtrlHandler", wintypes.BOOL, ctypes.c_void_p, wintypes.BOOL
    )
    _CreateProcessW = _declare(
        "CreateProcessW",
        wintypes.BOOL,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.POINTER(_ProcessInformation),
    )
    _WaitForSingleObject = _declare(
        "WaitForSingleObject", wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD
    )
    _GetExitCodeProcess = _declare(
        "GetExitCodeProcess", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)
    )
    _CloseHandle = _declare("CloseHandle", wintypes.BOOL, wintypes.HANDLE)

    #: The handler the helper installs for itself. It has to outlive the
    #: call that registers it: a callback that is collected is a callback
    #: the operating system may call into freed memory.
    _swallow_handler: Any = None

    def _clear_the_interrupt_attribute() -> bool:
        """Clear the ignore-Ctrl+C attribute and swallow the helper's events.

        The attribute is cleared **first**: it is what the shell inherits,
        and it is inherited at the moment the shell is created. The helper's
        own handler is installed afterwards, so that the helper survives the
        control event it is about to hand on to the shell: it is attached to
        the same console and shares the shell's process group, so the
        interrupt reaches the helper as well.
        """
        global _swallow_handler

        def swallow(control_type: int) -> int:
            # Returning TRUE says the event was handled. The helper must
            # not die of the interrupt it passes on.
            return 1

        callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)(swallow)
        _swallow_handler = callback
        # A null handler with FALSE restores normal processing of CTRL+C,
        # and the attribute is inherited by child processes.
        if not _SetConsoleCtrlHandler(None, False):
            return False
        if not _SetConsoleCtrlHandler(callback, True):
            return False
        return True

    def _create_the_shell(application: str, command_line: str) -> tuple[int, int]:
        """Create the shell; return its process and thread handles.

        The shell inherits the helper's console, which is the pseudoconsole
        the engine attached the helper to: a process attached to a console
        passes that console on to its children, and the shell is created
        neither with ``CREATE_NEW_CONSOLE`` nor with ``DETACHED_PROCESS``.
        The pseudoconsole's handle is therefore not needed here, and no
        handle is inherited by anybody.

        The three standard-handle slots stay null, exactly as the adapter
        leaves them for the helper: the shell's input and output are the
        console's own, and not the engine's.
        """
        startup = _StartupInfo()
        startup.cb = ctypes.sizeof(_StartupInfo)
        startup.dwFlags = _STARTF_USESTDHANDLES
        process_info = _ProcessInformation()
        command = ctypes.create_unicode_buffer(command_line)
        if not _CreateProcessW(
            application,
            command,
            None,
            None,
            False,
            0,
            None,
            None,
            ctypes.cast(ctypes.byref(startup), ctypes.c_void_p),
            ctypes.byref(process_info),
        ):
            return 0, 0
        return int(process_info.hProcess or 0), int(process_info.hThread or 0)

    def _wait_for_the_shell(process: int) -> int:
        """Wait for the shell to end; return its exit code."""
        _WaitForSingleObject(process, _INFINITE)
        code = wintypes.DWORD()
        if not _GetExitCodeProcess(process, ctypes.byref(code)):
            return WAIT_FAILED
        return int(code.value)

    def main(argv: list[str]) -> int:
        """Clear the attribute, start the shell, and report its exit code.

        ``argv`` is the shell's executable and the shell's whole command
        line. The status is the shell's own exit code, or one of the
        failure statuses above: the helper never prints anything, because
        its standard handles are the user's terminal.
        """
        if len(argv) != 2:
            return 2
        application, command_line = argv
        try:
            if not _clear_the_interrupt_attribute():
                return CONTROL_FAILED
            process, thread = _create_the_shell(application, command_line)
            if not process:
                return CREATE_FAILED
            try:
                code = _wait_for_the_shell(process)
            finally:
                _CloseHandle(process)
                if thread:
                    _CloseHandle(thread)
        except Exception:
            # Nothing may be printed here: the helper's standard handles are
            # the user's terminal, and a traceback would land in it. A
            # failure is a status, and only a status.
            return CREATE_FAILED
        return code

    if __name__ == "__main__":
        raise SystemExit(main(sys.argv[1:]))
