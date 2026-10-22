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
- It installs a handler of its own that swallows the control event. The
  helper is attached to the same console and shares the shell's process
  group, so the interrupt reaches the helper too, and the helper has to
  outlive the event it hands on.
- The pseudoconsole's handle reaches the helper as an inherited handle, and
  the helper passes it on in the shell's process attribute list, so the
  shell is attached to the very pseudoconsole the engine drives.
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

    # Process creation flags (winbase.h).
    _EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    # STARTF_USESTDHANDLES (winbase.h): the pseudoconsole replaces the
    # child's console, so the three standard-handle slots stay null.
    _STARTF_USESTDHANDLES = 0x00000100
    # PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE (processthreadsapi.h).
    _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
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

    class _StartupInfoEx(ctypes.Structure):
        """``STARTUPINFOEXW``: a STARTUPINFOW plus its attribute list."""

        _fields_ = [
            ("StartupInfo", _StartupInfo),
            ("lpAttributeList", ctypes.c_void_p),
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
    _InitializeProcThreadAttributeList = _declare(
        "InitializeProcThreadAttributeList",
        wintypes.BOOL,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    )
    _UpdateProcThreadAttribute = _declare(
        "UpdateProcThreadAttribute",
        wintypes.BOOL,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    _DeleteProcThreadAttributeList = _declare(
        "DeleteProcThreadAttributeList", None, ctypes.c_void_p
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

    def _create_the_shell(
        hpcon: int, application: str, command_line: str
    ) -> tuple[int, int]:
        """Create the shell on the pseudoconsole; return its process and thread.

        The shell is created the way the pseudoconsole's own clients are
        created: the pseudoconsole handle goes into the process attribute
        list, the three standard-handle slots stay null, and the
        pseudoconsole replaces the child's console.
        """
        size = ctypes.c_size_t()
        _InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        if not size.value:
            return 0, 0
        attribute_buffer = ctypes.create_string_buffer(size.value)
        attribute_list = int(
            ctypes.cast(attribute_buffer, ctypes.c_void_p).value or 0
        )
        if not _InitializeProcThreadAttributeList(
            ctypes.cast(attribute_buffer, ctypes.c_void_p), 1, 0, ctypes.byref(size)
        ):
            return 0, 0
        process_info = _ProcessInformation()
        try:
            if not _UpdateProcThreadAttribute(
                ctypes.cast(attribute_buffer, ctypes.c_void_p),
                0,
                _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                hpcon,
                ctypes.sizeof(ctypes.c_void_p),
                None,
                None,
            ):
                return 0, 0
            startup = _StartupInfoEx()
            startup.StartupInfo.cb = ctypes.sizeof(_StartupInfoEx)
            startup.lpAttributeList = ctypes.cast(attribute_buffer, ctypes.c_void_p)
            startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
            command = ctypes.create_unicode_buffer(command_line)
            if not _CreateProcessW(
                application,
                command,
                None,
                None,
                False,
                _EXTENDED_STARTUPINFO_PRESENT,
                None,
                None,
                ctypes.cast(ctypes.byref(startup), ctypes.c_void_p),
                ctypes.byref(process_info),
            ):
                return 0, 0
            return int(process_info.hProcess or 0), int(process_info.hThread or 0)
        finally:
            _DeleteProcThreadAttributeList(attribute_list)

    def _wait_for_the_shell(process: int) -> int:
        """Wait for the shell to end; return its exit code."""
        _WaitForSingleObject(process, _INFINITE)
        code = wintypes.DWORD()
        if not _GetExitCodeProcess(process, ctypes.byref(code)):
            return WAIT_FAILED
        return int(code.value)

    def main(argv: list[str]) -> int:
        """Clear the attribute, start the shell, and report its exit code.

        ``argv`` is the pseudoconsole's handle value, the shell's
        executable and the shell's whole command line. The status is the
        shell's own exit code, or one of the failure statuses above: the
        helper never prints anything, because its standard handles are the
        user's terminal.
        """
        if len(argv) != 3:
            return 2
        hpcon_text, application, command_line = argv
        try:
            hpcon = int(hpcon_text)
        except ValueError:
            return 2
        try:
            if not _clear_the_interrupt_attribute():
                return CONTROL_FAILED
            process, thread = _create_the_shell(hpcon, application, command_line)
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
