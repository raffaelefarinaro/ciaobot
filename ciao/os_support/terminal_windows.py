"""A native Windows terminal: ConPTY, raw bytes, bounded memory.

Windows has no ``openpty``. The equivalent is the *pseudoconsole* (ConPTY): a
pair of pipes plus a console host that renders a screen into one of them and
reads keystrokes from the other. This module drives it directly through
``ctypes``, so the engine owns the bytes and the memory:

* the child is created **suspended**, assigned to the
  :class:`~ciao.os_support.processes.ProcessTree` job object and only then
  resumed, so no shell instruction can ever run outside the job that owns it;
* one reader thread drains the output pipe into a **bounded** queue (four
  slots of at most 64 KiB), so a terminal nobody reads from back-pressures
  the console host instead of growing the engine's heap;
* one closer thread waits for the child and closes the pseudoconsole exactly
  once, so the final frame is still delivered and the session leaks no
  handle.

Everything crossing the boundary is **raw bytes**. ConPTY speaks UTF-8 with
virtual terminal sequences; this module never decodes, re-encodes or logs it.

The Windows-only parts live behind ``if sys.platform == "win32"``, so the
module imports on POSIX without a single fake stub.

The pseudoconsole's teardown is the delicate part. Microsoft's own guidance is
that the application must either close the output pipe *before* calling
``ClosePseudoConsole`` or keep reading from it until that call has returned —
on Windows 11 before 24H2, ``ClosePseudoConsole`` waits for the clients to
disconnect and deadlocks if nobody drains the pipe. This module closes the
output pipe first (the reader has already stopped by then) and joins the
closer thread afterwards, which is the order the documentation asks for.
"""

from __future__ import annotations

import sys

if sys.platform == "win32":
    import ctypes
    import os
    import queue
    import subprocess
    import threading
    import time
    from ctypes import wintypes
    from pathlib import Path
    from typing import Any

    from ciao.os_support.processes import ProcessTree
    from ciao.os_support.tool_path import resolve_executable, terminal_path

    # ── Win32 constants ─────────────────────────────────────────────────────
    # Process creation flags (winbase.h).
    _CREATE_SUSPENDED = 0x00000004
    _EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    _CREATE_UNICODE_ENVIRONMENT = 0x00000400
    # CREATE_NEW_PROCESS_GROUP (0x200) is deliberately not used: a terminal's
    # Ctrl+C is an input byte, not a console control event, so the child needs
    # no process group of its own.

    # PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE (processthreadsapi.h).
    _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016

    # Error codes (winerror.h).
    _ERROR_BROKEN_PIPE = 109
    _ERROR_NO_DATA = 232
    _ERROR_OPERATION_ABORTED = 995
    _ERROR_NOT_FOUND = 1168

    # Process and thread state (winbase.h, synchapi.h).
    _STILL_ACTIVE = 259
    _INFINITE = 0xFFFFFFFF

    # Access rights (winnt.h). CancelSynchronousIo needs THREAD_TERMINATE.
    _THREAD_TERMINATE = 0x0001
    _SYNCHRONIZE = 0x00100000

    # ── The adapter's own bounds ────────────────────────────────────────────
    # One read fills at most this much, and one write sends at most this much.
    _READ_BUFFER_SIZE = 65536
    _MAX_WRITE = 65536
    # The output queue holds this many chunks: at most 256 KiB of pending
    # terminal output, ever.
    _OUTPUT_QUEUE_SLOTS = 4
    # How long a close waits for a thread that will not come back.
    _CLOSE_TIMEOUT = 10.0
    # How long the reader gets to report that it started.
    _READER_START_TIMEOUT = 10.0

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def _declare(name: str, restype: Any, *argtypes: Any) -> Any:
        """Bind one Kernel32 function with its SDK signature."""
        func = getattr(_kernel32, name)
        func.argtypes = list(argtypes)
        func.restype = restype
        return func

    class _Coord(ctypes.Structure):
        """``COORD``: a console size in characters."""

        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

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

    _CreatePipe = _declare(
        "CreatePipe",
        wintypes.BOOL,
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    _ReadFile = _declare(
        "ReadFile",
        wintypes.BOOL,
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_char),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    )
    _WriteFile = _declare(
        "WriteFile",
        wintypes.BOOL,
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_char),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    )
    _CreatePseudoConsole = _declare(
        "CreatePseudoConsole",
        ctypes.c_int32,
        _Coord,
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    )
    _ResizePseudoConsole = _declare(
        "ResizePseudoConsole", ctypes.c_int32, ctypes.c_void_p, _Coord
    )
    _ClosePseudoConsole = _declare("ClosePseudoConsole", None, ctypes.c_void_p)
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
    _ResumeThread = _declare("ResumeThread", wintypes.DWORD, wintypes.HANDLE)
    _TerminateProcess = _declare(
        "TerminateProcess", wintypes.BOOL, wintypes.HANDLE, wintypes.UINT
    )
    _GetExitCodeProcess = _declare(
        "GetExitCodeProcess",
        wintypes.BOOL,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    _WaitForSingleObject = _declare(
        "WaitForSingleObject", wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD
    )
    _OpenThread = _declare(
        "OpenThread", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD
    )
    _CancelSynchronousIo = _declare(
        "CancelSynchronousIo", wintypes.BOOL, wintypes.HANDLE
    )
    _CloseHandle = _declare("CloseHandle", wintypes.BOOL, wintypes.HANDLE)

    # ── Small helpers ───────────────────────────────────────────────────────

    def _validated_dimension(value: int, name: str, low: int, high: int) -> int:
        """A terminal size: a real integer (not a bool) inside the bounds."""
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be a whole number between {low} and {high}")
        if not low <= value <= high:
            raise ValueError(f"{name} must be between {low} and {high}")
        return value

    def _validated_folder(cwd: Path) -> Path:
        """The working directory the shell starts in; it must already exist."""
        folder = Path(cwd)
        if not folder.exists():
            raise FileNotFoundError(str(folder))
        if not folder.is_dir():
            raise NotADirectoryError(str(folder))
        return folder

    def _environment_block(env: dict[str, str]) -> bytes:
        """A Unicode environment block for ``CreateProcessW``.

        The SDK wants the block sorted by name, each entry ``NAME=VALUE``
        followed by a NUL, and the whole block closed by a second NUL — four
        zero bytes in UTF-16, which is what "terminated by two NUL characters"
        means here.
        """
        entries = []
        for key in sorted(env, key=lambda name: (name.upper(), name)):
            if "=" in key:
                # "=" separates the name from the value, so it cannot be in
                # the name (CreateProcessW documentation). A name that breaks
                # that rule is a bug in whoever built the environment, not
                # something to drop quietly.
                raise ValueError(f"{key!r} is not a usable environment name")
            entries.append(f"{key}={env[key]}\0")
        text = "".join(entries) + "\0"
        return text.encode("utf-16-le")

    def _resolve_powershell() -> str | None:
        """The absolute path of ``powershell.exe``, or None when it is not there."""
        path = terminal_path()
        if path:
            found = resolve_executable("powershell.exe", path=path)
            if found:
                return found
        # The same binary at the location Windows installs it in, for a
        # machine whose logon PATH does not name that directory.
        system = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or ""
        if system:
            candidate = (
                Path(system)
                / "System32"
                / "WindowsPowerShell"
                / "v1.0"
                / "powershell.exe"
            )
            if candidate.is_file():
                return str(candidate)
        return None

    def _thread_handle(thread_id: int) -> int:
        """A handle to one thread, so a close can cancel its pending I/O.

        ``CancelSynchronousIo`` needs a handle with ``THREAD_TERMINATE``, and
        it cancels the *thread's* pending synchronous call — so the handle has
        to name the thread that is doing the I/O.
        """
        handle = _OpenThread(_THREAD_TERMINATE | _SYNCHRONIZE, False, thread_id)
        if not handle:
            error = ctypes.get_last_error()
            raise OSError(
                f"opening the terminal thread failed: {ctypes.WinError(error)}"
            )
        return int(handle)

    # ── The public interface ────────────────────────────────────────────────

    def default_shell() -> list[str]:
        """The shell a terminal starts: PowerShell, with no logo and no profile.

        ``-NoProfile`` keeps the session predictable: no hidden startup
        script, no network access behind the user's back, and the same
        environment every time.
        """
        found = _resolve_powershell()
        if found is None:
            raise FileNotFoundError(
                "powershell.exe is not on this machine's terminal PATH"
            )
        return [found, "-NoLogo", "-NoProfile"]

    def terminal_environment() -> dict[str, str]:
        """The environment a terminal's shell runs with.

        The engine's own environment minus the secrets a terminal must not
        see (the same filter the host terminal window uses), plus ``TERM`` for
        the programs that look at it. A stale ``COLUMNS``/``LINES`` is
        dropped: the pseudoconsole reports the real size.
        """
        from ciao.host_terminal import _terminal_env

        env = _terminal_env()
        env["TERM"] = "xterm-256color"
        env.pop("COLUMNS", None)
        env.pop("LINES", None)
        return env

    class _End:
        """How the reader stopped: at the end of the output, or with a fault.

        ``error`` is None for a clean end of the output and an ``OSError``
        when the output was cut short, so a caller is never told the output
        was complete when it was not.
        """

        __slots__ = ("error",)

        def __init__(self, error: OSError | None) -> None:
            self.error = error

    class WindowsTerminalProcess:
        """A running terminal: the shell, its pseudoconsole and its pipes.

        The reader thread owns the output pipe, the closer thread owns the
        pseudoconsole handle, and this object owns the job, the process
        handle and the two parent-side pipe ends.

        One caller at a time may :meth:`read`; :meth:`write` may be called
        from any thread and is serialised.
        """

        def __init__(
            self,
            *,
            pid: int,
            process: int,
            tree: ProcessTree,
            input_write: int,
            output_read: int,
            hpcon: int,
        ) -> None:
            self.pid = pid
            self._process = process
            self._tree = tree
            self._input_write = input_write
            self._output_read = output_read
            self._hpcon = hpcon
            self._queue: queue.Queue[bytes] = queue.Queue(maxsize=_OUTPUT_QUEUE_SLOTS)
            self._remainder = b""
            self._end: _End | None = None
            self._exit_code: int | None = None
            self._stop = threading.Event()
            self._closing = False
            self._closed = False
            # Short critical sections only: never held across a blocking call.
            self._state_lock = threading.Lock()
            # Serialises writes; a close never waits on it before cancelling.
            self._write_lock = threading.Lock()
            self._close_lock = threading.Lock()
            self._writer_handle: int | None = None
            self._writer_inflight = threading.Event()
            self._reader_handle: int | None = None
            self._reader: threading.Thread | None = None
            self._closer: threading.Thread | None = None
            self._reader_ready = threading.Event()
            self._reader_error: OSError | None = None

        # ── starting ────────────────────────────────────────────────────────

        def _start(self) -> None:
            """Start the reader and the closer; report a reader that failed."""
            reader = threading.Thread(
                target=self._reader_main, name="ciao-terminal-reader", daemon=True
            )
            self._reader = reader
            reader.start()
            if not self._reader_ready.wait(_READER_START_TIMEOUT):
                raise OSError("the terminal reader thread did not start")
            if self._reader_error is not None:
                raise self._reader_error
            self._closer = threading.Thread(
                target=self._closer_main, name="ciao-terminal-closer", daemon=True
            )
            self._closer.start()

        # ── the reader thread ───────────────────────────────────────────────

        def _reader_main(self) -> None:
            """Drain the output pipe into the bounded queue until it ends."""
            try:
                handle = _thread_handle(threading.get_native_id())
            except OSError as exc:
                self._reader_error = exc
                self._reader_ready.set()
                self._end = _End(exc)
                return
            with self._state_lock:
                self._reader_handle = handle
            self._reader_ready.set()
            try:
                complete = self._drain_output()
            except OSError as exc:
                self._end = _End(exc)
                return
            except Exception as exc:  # noqa: BLE001 - a reader must not die silently
                self._end = _End(OSError(f"the terminal reader failed: {exc}"))
                return
            if complete:
                self._end = _End(None)
            else:
                # The session was closed with output still to come. That is
                # not a clean end of the output and must not be reported as
                # one: the caller has to know that bytes are missing.
                self._end = _End(
                    OSError("the terminal was closed with output still pending")
                )

        def _drain_output(self) -> bool:
            """Read the output pipe until it breaks, ends or is cancelled.

            True when the output ended on its own, False when the session was
            closed before the output was complete.
            """
            buffer = ctypes.create_string_buffer(_READ_BUFFER_SIZE)
            count = wintypes.DWORD()
            while True:
                count.value = 0
                ok = _ReadFile(
                    self._output_read,
                    ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)),
                    _READ_BUFFER_SIZE,
                    ctypes.byref(count),
                    None,
                )
                if not ok:
                    error = ctypes.get_last_error()
                    if error in (_ERROR_BROKEN_PIPE, _ERROR_NO_DATA):
                        return True
                    if error == _ERROR_OPERATION_ABORTED and self._stop.is_set():
                        return False
                    raise OSError(
                        f"reading the terminal output failed: {ctypes.WinError(error)}"
                    )
                if count.value == 0:
                    return True
                # Only the bytes that were read: the buffer is a fixed
                # 64 KiB, and copying all of it per read would be waste.
                chunk = ctypes.string_at(buffer, count.value)
                if not self._queue_chunk(chunk):
                    return False

        def _queue_chunk(self, chunk: bytes) -> bool:
            """Put one chunk in the queue, waiting for room rather than dropping it.

            The only way to give up is a close in progress: the session is
            over then, and the caller is told the output was cut short.
            """
            while True:
                try:
                    self._queue.put(chunk, timeout=0.1)
                    return True
                except queue.Full:
                    if self._stop.is_set():
                        return False

        # ── the closer thread ───────────────────────────────────────────────

        def _closer_main(self) -> None:
            """Wait for the child, then close the pseudoconsole exactly once.

            The reader keeps draining while this runs: Microsoft asks for
            either a closed output pipe or a concurrent drain, and the drain
            is what preserves the final frame.
            """
            _WaitForSingleObject(self._process, _INFINITE)
            code = wintypes.DWORD()
            if _GetExitCodeProcess(self._process, ctypes.byref(code)):
                with self._state_lock:
                    self._exit_code = int(code.value)
            _ClosePseudoConsole(self._hpcon)

        # ── reading and writing ─────────────────────────────────────────────

        def read(self, size: int = _READ_BUFFER_SIZE) -> bytes | None:
            """Take the next output bytes, without waiting.

            ``None`` means "nothing queued yet", ``b""`` means the output has
            ended. The bytes come back in the order the terminal produced
            them, and a short request never loses the rest of a chunk.
            """
            if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
                raise ValueError("size must be a positive whole number")
            limit = min(size, _READ_BUFFER_SIZE)
            if self._remainder:
                chunk = self._remainder[:limit]
                self._remainder = self._remainder[limit:]
                return chunk
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                end = self._end
                if end is None:
                    return None
                if end.error is not None:
                    raise end.error
                return b""
            if len(item) <= limit:
                return item
            self._remainder = item[limit:]
            return item[:limit]

        def write(self, data: bytes) -> int:
            """Send input bytes to the terminal; returns how many were written.

            A short write is allowed: the caller keeps the rest and sends it
            again. The call blocks on the pipe, so it belongs off the event
            loop. A close cancels a write that is stuck, which is why the
            write is not held behind a lock a close would have to wait for.
            """
            if not isinstance(data, (bytes, bytearray, memoryview)):
                raise TypeError("data must be bytes")
            payload = bytes(data)
            if len(payload) > _MAX_WRITE:
                raise ValueError(f"at most {_MAX_WRITE} bytes can be written at once")
            if not payload:
                return 0
            with self._write_lock:
                with self._state_lock:
                    if self._closing:
                        raise OSError("the terminal is closed")
                # The handle has to name this thread, because
                # CancelSynchronousIo cancels one thread's pending call.
                handle = _thread_handle(threading.get_native_id())
                with self._state_lock:
                    if self._closing:
                        _CloseHandle(handle)
                        raise OSError("the terminal is closed")
                    # The writer owns this handle from here on, and only the
                    # writer closes it — a close may cancel the write but
                    # must never close the handle out from under it.
                    self._writer_handle = handle
                    self._writer_inflight.set()
                try:
                    return self._write_nowait(payload)
                finally:
                    with self._state_lock:
                        self._writer_inflight.clear()
                        if self._writer_handle == handle:
                            self._writer_handle = None
                        # Closed under the same lock a close takes when it
                        # cancels, so the two can never race on the handle.
                        _CloseHandle(handle)

        def _write_nowait(self, payload: bytes) -> int:
            """One ``WriteFile`` on the input pipe."""
            buffer = ctypes.create_string_buffer(payload, len(payload))
            written = wintypes.DWORD()
            ok = _WriteFile(
                self._input_write,
                ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)),
                len(payload),
                ctypes.byref(written),
                None,
            )
            if ok:
                return int(written.value)
            error = ctypes.get_last_error()
            if error in (_ERROR_BROKEN_PIPE, _ERROR_NO_DATA):
                raise OSError("the terminal input pipe is closed")
            if error == _ERROR_OPERATION_ABORTED and self._closing:
                raise OSError("the terminal is closed")
            raise OSError(f"writing to the terminal failed: {ctypes.WinError(error)}")

        # ── the terminal's state ────────────────────────────────────────────

        def resize(self, cols: int, rows: int) -> None:
            """Tell the pseudoconsole a new size, in characters."""
            _validated_dimension(cols, "cols", 20, 500)
            _validated_dimension(rows, "rows", 5, 200)
            with self._state_lock:
                if self._closing:
                    raise OSError("the terminal is closed")
                hpcon = self._hpcon
            hr = _ResizePseudoConsole(hpcon, _Coord(cols, rows))
            if hr < 0:
                raise OSError(f"resizing the terminal failed: {hr:#010x}")

        def poll(self) -> int | None:
            """The shell's exit code, or None while it is still running."""
            with self._state_lock:
                cached = self._exit_code
            if cached is not None:
                return cached
            if not self._process:
                return None
            code = wintypes.DWORD()
            if not _GetExitCodeProcess(self._process, ctypes.byref(code)):
                error = ctypes.get_last_error()
                raise OSError(
                    f"reading the terminal exit code failed: {ctypes.WinError(error)}"
                )
            if code.value == _STILL_ACTIVE:
                return None
            return int(code.value)

        # ── closing ─────────────────────────────────────────────────────────

        def close(self) -> None:
            """End the terminal and release everything it owns.

            The order matters. The job goes first, so the shell and everything
            it started is gone; a write blocked on the input pipe is then
            cancelled; the reader is stopped and joined; the output pipe is
            closed **before** the closer thread's ``ClosePseudoConsole`` is
            waited for, because that call waits for the clients to disconnect
            and deadlocks on a pipe nobody drains; and only then are the
            remaining handles released. Calling it twice does nothing the
            second time.
            """
            with self._close_lock:
                if self._closed:
                    return
                self._closed = True
                with self._state_lock:
                    self._closing = True
                self._stop.set()
                problems: list[str] = []
                try:
                    self._tree.kill()
                except OSError as exc:
                    problems.append(f"ending the terminal process tree failed: {exc}")
                self._cancel_writer(problems)
                stopped = self._stop_reader(problems)
                if stopped:
                    # The reader has stopped, so the output pipe can go now.
                    self._release(
                        self._output_read, "the terminal output pipe", problems
                    )
                    self._output_read = 0
                else:
                    # A reader still inside ReadFile owns that handle; closing
                    # it under it would be a use-after-close.
                    problems.append(
                        "the terminal output pipe was kept open because its "
                        "reader did not stop"
                    )
                # No write can be in flight any more.
                self._release(self._input_write, "the terminal input pipe", problems)
                self._input_write = 0
                if self._closer is None:
                    # The closer thread never ran, so nobody else owns the
                    # pseudoconsole handle.
                    _ClosePseudoConsole(self._hpcon)
                else:
                    self._join_closer(problems)
                # The reader has stopped by now, so its thread handle is
                # safe to release.
                with self._state_lock:
                    reader_handle = self._reader_handle
                    self._reader_handle = None
                if reader_handle is not None:
                    self._release(reader_handle, "the terminal reader thread", problems)
                self._release(self._process, "the terminal process", problems)
                self._process = 0
                self._tree.close()
                if problems:
                    raise OSError("closing the terminal failed: " + "; ".join(problems))

        def _release(self, handle: int, what: str, problems: list[str]) -> None:
            """Close one handle, recording a refusal instead of hiding it."""
            if not handle:
                return
            if not _CloseHandle(handle):
                error = ctypes.get_last_error()
                problems.append(f"closing {what} failed: {ctypes.WinError(error)}")

        def _cancel_writer(self, problems: list[str]) -> None:
            """Cancel a write that is blocked on the input pipe.

            The write is cancelled rather than waited for: the lock that
            serialises writes is held by the blocked writer, and a close that
            waited on it first could not reach the writer to cancel it. The
            cancellation is retried, because a writer that had not entered
            ``WriteFile`` yet answers ERROR_NOT_FOUND and would otherwise
            block for ever.

            The cancellation happens **under** the state lock, and so does the
            writer's release of the handle, so the two can never act on a
            handle the other has already closed.
            """
            deadline = time.monotonic() + _CLOSE_TIMEOUT
            while True:
                with self._state_lock:
                    handle = self._writer_handle
                    inflight = self._writer_inflight.is_set()
                    if handle is not None and inflight:
                        if not _CancelSynchronousIo(handle):
                            error = ctypes.get_last_error()
                            # ERROR_NOT_FOUND means the writer had no I/O
                            # pending at that instant; the cancellation is
                            # retried on the next pass.
                            if error != _ERROR_NOT_FOUND:
                                problems.append(
                                    "cancelling a terminal write failed: "
                                    f"{ctypes.WinError(error)}"
                                )
                    if not inflight:
                        return
                if time.monotonic() >= deadline:
                    problems.append("a terminal write did not finish")
                    return
                time.sleep(0.02)

        def _stop_reader(self, problems: list[str]) -> bool:
            """Cancel the reader's pending read and wait for the thread to end.

            The reader owns its thread handle and closes it when it ends, so
            the cancellation is issued while the handle is certainly open and
            the reader is joined before that handle is released.
            """
            reader = self._reader
            if reader is None:
                return True
            deadline = time.monotonic() + _CLOSE_TIMEOUT
            while reader.is_alive():
                with self._state_lock:
                    handle = self._reader_handle
                    if handle is not None:
                        if not _CancelSynchronousIo(handle):
                            error = ctypes.get_last_error()
                            # ERROR_NOT_FOUND only means the reader had no
                            # I/O pending at that instant; it may start one
                            # right after, so the cancellation is retried.
                            if error != _ERROR_NOT_FOUND:
                                problems.append(
                                    "cancelling the terminal reader failed: "
                                    f"{ctypes.WinError(error)}"
                                )
                if time.monotonic() >= deadline:
                    break
                reader.join(0.05)
            if reader.is_alive():
                problems.append("the terminal reader thread did not stop")
                return False
            return True

        def _join_closer(self, problems: list[str]) -> None:
            """Wait for the closer thread; it owns the pseudoconsole handle."""
            closer = self._closer
            if closer is None:
                return
            closer.join(_CLOSE_TIMEOUT)
            if closer.is_alive():
                problems.append("the terminal closer thread did not finish")

    # ── starting a terminal ────────────────────────────────────────────────

    def spawn_terminal(
        *, cwd: Path, cols: int = 80, rows: int = 24
    ) -> WindowsTerminalProcess:
        """Start a shell in a pseudoconsole and return the running terminal.

        The child is created suspended, given to a
        :class:`~ciao.os_support.processes.ProcessTree` job and only then
        resumed, so the shell is never outside the job that owns it. Any
        failure along the way kills what was created and closes every handle
        the transaction opened.
        """
        folder = _validated_folder(cwd)
        _validated_dimension(cols, "cols", 20, 500)
        _validated_dimension(rows, "rows", 5, 200)
        argv = default_shell()
        env = terminal_environment()

        input_read = wintypes.HANDLE()
        input_write = wintypes.HANDLE()
        output_read = wintypes.HANDLE()
        output_write = wintypes.HANDLE()
        hpcon = ctypes.c_void_p()
        attribute_buffer: ctypes.Array[ctypes.c_char] | None = None
        attribute_list = 0
        process_info = _ProcessInformation()
        tree: ProcessTree | None = None
        opened: list[int] = []
        process_handle = 0
        thread_handle = 0

        try:
            if not _CreatePipe(
                ctypes.byref(input_read), ctypes.byref(input_write), None, 0
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            opened.extend([int(input_read.value or 0), int(input_write.value or 0)])
            if not _CreatePipe(
                ctypes.byref(output_read), ctypes.byref(output_write), None, 0
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            opened.extend([int(output_read.value or 0), int(output_write.value or 0)])

            hr = _CreatePseudoConsole(
                _Coord(cols, rows), input_read, output_write, 0, ctypes.byref(hpcon)
            )
            if hr < 0:
                raise OSError(f"creating the pseudoconsole failed: {hr:#010x}")

            size = ctypes.c_size_t()
            _InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
            if not size.value:
                raise OSError("the process attribute list has no size")
            attribute_buffer = ctypes.create_string_buffer(size.value)
            attribute_list = int(
                ctypes.cast(attribute_buffer, ctypes.c_void_p).value or 0
            )
            if not _InitializeProcThreadAttributeList(
                ctypes.cast(attribute_buffer, ctypes.c_void_p),
                1,
                0,
                ctypes.byref(size),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if not _UpdateProcThreadAttribute(
                ctypes.cast(attribute_buffer, ctypes.c_void_p),
                0,
                _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                hpcon.value,
                ctypes.sizeof(ctypes.c_void_p),
                None,
                None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())

            startup = _StartupInfoEx()
            startup.StartupInfo.cb = ctypes.sizeof(_StartupInfoEx)
            startup.lpAttributeList = ctypes.cast(attribute_buffer, ctypes.c_void_p)

            block = _environment_block(env)
            environment = ctypes.create_string_buffer(block, len(block))
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))

            if not _CreateProcessW(
                argv[0],
                command,
                None,
                None,
                False,
                _CREATE_SUSPENDED
                | _EXTENDED_STARTUPINFO_PRESENT
                | _CREATE_UNICODE_ENVIRONMENT,
                ctypes.cast(environment, ctypes.c_void_p),
                str(folder),
                ctypes.cast(ctypes.byref(startup), ctypes.c_void_p),
                ctypes.byref(process_info),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            process_handle = int(process_info.hProcess or 0)
            thread_handle = int(process_info.hThread or 0)
            opened.extend([process_handle, thread_handle])

            # The pseudoconsole keeps its own copies of these; ours would
            # keep the pipes alive and hide a broken channel.
            setup_input_read = int(input_read.value or 0)
            setup_output_write = int(output_write.value or 0)
            _CloseHandle(setup_input_read)
            opened.remove(setup_input_read)
            input_read.value = None
            _CloseHandle(setup_output_write)
            opened.remove(setup_output_write)
            output_write.value = None

            # The job owns the child before a single instruction of it runs.
            tree = ProcessTree(int(process_info.dwProcessId), dies_with_engine=True)

            if _ResumeThread(process_info.hThread) == 0xFFFFFFFF:
                error = ctypes.get_last_error()
                _TerminateProcess(process_info.hProcess, 1)
                _WaitForSingleObject(process_info.hProcess, 5000)
                raise ctypes.WinError(error)
            _CloseHandle(thread_handle)
            opened.remove(thread_handle)
            thread_handle = 0
        except BaseException:
            if process_handle:
                _TerminateProcess(process_handle, 1)
                _WaitForSingleObject(process_handle, 5000)
            if hpcon.value:
                _ClosePseudoConsole(hpcon)
            if attribute_list:
                _DeleteProcThreadAttributeList(attribute_list)
            for handle in reversed(opened):
                _CloseHandle(handle)
            if tree is not None:
                tree.close()
            raise

        _DeleteProcThreadAttributeList(attribute_list)

        terminal = WindowsTerminalProcess(
            pid=int(process_info.dwProcessId),
            process=process_handle,
            tree=tree,
            input_write=int(input_write.value or 0),
            output_read=int(output_read.value or 0),
            hpcon=int(hpcon.value or 0),
        )
        try:
            terminal._start()
        except BaseException as exc:
            try:
                terminal.close()
            except OSError as cleanup:
                exc.add_note(f"closing the failed terminal also failed: {cleanup}")
            raise
        return terminal
