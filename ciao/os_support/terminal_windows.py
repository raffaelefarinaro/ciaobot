"""A native Windows terminal: ConPTY, raw bytes, bounded memory.

Windows has no ``openpty``. The equivalent is the *pseudoconsole* (ConPTY): a
pair of pipes plus a console host that renders a screen into one of them and
reads keystrokes from the other. This module drives it directly through
``ctypes``, so the engine owns the bytes and the memory:

* the child is created **suspended**, assigned to the
  :class:`~ciao.os_support.processes.ProcessTree` job object and only then
  resumed, so no shell instruction can ever run outside the job that owns it;
* the child's ``STARTUPINFO`` carries ``STARTF_USESTDHANDLES`` with the
  three standard-handle slots null, so the pseudoconsole replaces the
  child's console instead of the child inheriting the parent's handles;
* one reader thread drains the output pipe into a **bounded** queue (four
  slots of at most 64 KiB), so a terminal nobody reads from back-pressures
  the console host instead of growing the engine's heap;
* one closer thread waits for the child, caches the exit code and closes
  the pseudoconsole exactly once, so the final frame is still delivered
  and the session leaks no handle.

Everything crossing the boundary is **raw bytes**. ConPTY speaks UTF-8 with
virtual terminal sequences; this module never decodes, re-encodes or logs it.

The Windows-only parts live behind ``if sys.platform == "win32"``, so the
module imports on POSIX without a single fake stub.

Three things are easy to get wrong and are pinned here.

The **child's standard handles**. Without ``STARTF_USESTDHANDLES`` the child
keeps the *parent's* standard handles, so the shell's prompt goes to the
parent's stdout and its input never arrives from the pseudoconsole: the
child then sees the end of that input and exits at once, and the pipe
carries only the console's start-up and tear-down sequences. Microsoft
Terminal sets the same flag (``siEx.StartupInfo.dwFlags =
STARTF_USESTDHANDLES;``), with the three slots left null.

The **pseudoconsole's teardown**. Microsoft's own guidance is that the
application must either close the output pipe *before* calling
``ClosePseudoConsole`` or keep reading from it until that call has returned —
on Windows 11 before 24H2, ``ClosePseudoConsole`` waits for the clients to
disconnect and deadlocks if nobody drains the pipe. This module closes the
output pipe first (the reader has already stopped by then) and joins the
closer thread afterwards, which is the order the documentation asks for. A
rollback closes the pipe without reading it: a read on a pipe whose write
end is open blocks for ever, and there is no reader to hand the bytes to.

The **handles that are still in use**. The pseudoconsole handle is guarded
by one short lock, held through the resize syscall and given up before the
close is called, so a resize can never meet a closed handle and the close
never waits on the lock. A close that could not stop a thread says so and
keeps what it still owns, so it can be tried again rather than reporting a
success that never happened.
"""

from __future__ import annotations

import sys

if sys.platform == "win32":
    import ctypes
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
    # CREATE_NEW_PROCESS_GROUP (0x200) is deliberately not used. The console
    # host turns the 0x03 byte into a Ctrl+C control event, and the operating
    # system delivers that event to the processes of the console host's
    # process group: a child that sits in a group of its own is guaranteed
    # never to receive it. The shell therefore shares the group of the
    # pseudoconsole, which is the group the event is delivered to.

    # PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE (processthreadsapi.h).
    _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016

    # STARTF_USESTDHANDLES (winbase.h). The child of a pseudoconsole must
    # carry this flag: it tells Windows that the standard handles in the
    # STARTUPINFO are the ones to use. Without it the child keeps the
    # parent's standard handles, so the shell's output goes to the parent's
    # stdout and its input never arrives from the pseudoconsole — the child
    # then sees the end of that input and exits at once.
    _STARTF_USESTDHANDLES = 0x00000100

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
        """The absolute path of ``powershell.exe`` on the terminal PATH, or None.

        The canonical terminal PATH and nothing else: a shell that is not
        on the PATH a user's own terminal would resolve is not on this
        machine's terminal PATH, and reaching into ``SystemRoot`` for it
        would be a fallback the plan forbids.
        """
        path = terminal_path()
        if not path:
            return None
        return resolve_executable("powershell.exe", path=path)

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
            self._ended: _End | None = None
            self._end_lock = threading.Lock()
            self._exit_code: int | None = None
            self._stop = threading.Event()
            self._closing = False
            # A close that released everything is the only one that counts
            # as closed; a failed close keeps what it owns and is retryable.
            self._fully_closed = False
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
            # The pseudoconsole handle, and the lock that guards it. The lock
            # is held **through** the resize syscall and released before the
            # pseudoconsole is closed, so a resize and a close can never use
            # the handle at the same time and the close never waits on the
            # lock while it blocks.
            self._hpcon_lock = threading.Lock()
            self._hpcon = hpcon
            self._hpcon_gone = False

        # ── the pseudoconsole handle ───────────────────────────────────────

        def _close_pseudoconsole(self) -> bool:
            """Close the pseudoconsole, once, and say whether it happened.

            The handle is published as gone under the lock that a resize
            holds, so a resize that starts later refuses it, and a resize
            that is running now finishes first: the lock is taken before
            the handle is taken and given up before the close is called, so
            the close never blocks on the lock.
            """
            with self._hpcon_lock:
                if self._hpcon_gone or not self._hpcon:
                    return True
                self._hpcon_gone = True
                hpcon = self._hpcon
                self._hpcon = 0
            _ClosePseudoConsole(hpcon)
            return True

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
                self._queue_end(_End(exc))
                return
            with self._state_lock:
                self._reader_handle = handle
            self._reader_ready.set()
            try:
                complete = self._drain_output()
            except OSError as exc:
                self._queue_end(_End(exc))
                return
            except Exception as exc:  # noqa: BLE001 - a reader must not die silently
                self._queue_end(_End(OSError(f"the terminal reader failed: {exc}")))
                return
            if complete:
                self._queue_end(_End(None))
            else:
                # The session was closed with output still to come. That is
                # not a clean end of the output and must not be reported as
                # one: the caller has to know that bytes are missing.
                self._queue_end(
                    _End(OSError("the terminal was closed with output still pending"))
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
            over then, and the caller is told the output was cut short. The
            queue holds four chunks, so the pending output is at most
            256 KiB whatever the terminal does.
            """
            while True:
                try:
                    self._queue.put(chunk, timeout=0.1)
                    return True
                except queue.Full:
                    if self._stop.is_set():
                        return False

        def _queue_end(self, end: _End) -> None:
            """Say that the output ended.

            The notice is a state of its own, not another item in the queue:
            a queue that is full of output would then have no room for it,
            and the reader would wait for a consumer that may never come.

            The reader queues its last chunk **before** it publishes this,
            so a caller that sees the notice knows the chunk is already in
            the queue and takes it before it reports the end.
            """
            with self._end_lock:
                self._ended = end

        # ── the closer thread ───────────────────────────────────────────────

        def _closer_main(self) -> None:
            """Wait for the child, then close the pseudoconsole exactly once.

            The reader keeps draining while this runs: Microsoft asks for
            either a closed output pipe or a concurrent drain, and the drain
            is what preserves the final frame.

            The pseudoconsole handle is only freed once every call that could
            still be using it has finished, so a resize that started a
            moment earlier can never meet a closed handle.

            This thread owns the process handle while it waits for the
            child, and releases it when it is done: a close that freed it
            would pull the handle out from under this wait. The exit code
            it reads is the only answer :meth:`poll` ever gives, so no
            other call has to touch the process handle at all.
            """
            process = self._process
            if process:
                try:
                    _WaitForSingleObject(process, _INFINITE)
                    code = wintypes.DWORD()
                    if _GetExitCodeProcess(process, ctypes.byref(code)):
                        with self._state_lock:
                            self._exit_code = int(code.value)
                finally:
                    self._process = 0
                    _CloseHandle(process)
            self._close_pseudoconsole()

        # ── reading and writing ─────────────────────────────────────────────

        def read(self, size: int = _READ_BUFFER_SIZE) -> bytes | None:
            """Take the next output bytes, without waiting.

            ``None`` means "nothing queued yet", ``b""`` means the output has
            ended. The bytes come back in the order the terminal produced
            them, and a short request never loses the rest of a chunk.

            The end of the output is only reported once every byte that was
            produced has been handed out. The reader queues its last chunk
            **before** it publishes the end, so the order is: look at the
            queue; if it is empty, look at the end; if the end is there,
            look at the queue once more before answering it. Reading the
            end first is what makes that safe — a published end means no
            further chunk can be queued, so a second empty look is final.
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
                # Nothing queued at the moment the caller looked. The
                # reader may not have produced anything yet, or it may have
                # just queued its last chunk and published the end.
                with self._end_lock:
                    ended = self._ended
                if ended is None:
                    return None
                # The end is published only after the last chunk is queued,
                # so the queue is looked at once more: the end is reported
                # only when the bytes that came before it are out.
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    if ended.error is not None:
                        raise ended.error
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
            # The lock is held through the syscall, so a close that arrives
            # now waits for this call to end and the handle cannot be
            # closed underneath it.
            with self._hpcon_lock:
                if self._hpcon_gone or not self._hpcon:
                    raise OSError("the terminal is closed")
                hr = _ResizePseudoConsole(self._hpcon, _Coord(cols, rows))
            if hr < 0:
                raise OSError(f"resizing the terminal failed: {hr:#010x}")

        def poll(self) -> int | None:
            """The shell's exit code, or None while it is still running.

            The answer comes from the closer thread, which is the only one
            that waits for the child. Nothing here touches the process
            handle, so a poll can never meet a handle that a close has
            already freed.
            """
            with self._state_lock:
                return self._exit_code

        # ── closing ─────────────────────────────────────────────────────────

        def close(self) -> None:
            """End the terminal and release everything it owns.

            The order matters. The job goes first, so the shell and everything
            it started is gone; a write blocked on the input pipe is then
            cancelled; the reader is stopped and joined; the output pipe is
            closed **before** the closer thread's ``ClosePseudoConsole`` is
            waited for, because that call waits for the clients to disconnect
            and deadlocks on a pipe nobody drains; and only then are the
            remaining handles released.

            A close that could not finish says so and stays **retryable**:
            the resources it could not release are still owned here, so a
            second call tries again rather than reporting a success that
            never happened.
            """
            with self._close_lock:
                if self._fully_closed:
                    return
                with self._state_lock:
                    self._closing = True
                self._stop.set()
                problems: list[str] = []
                try:
                    self._tree.kill()
                except OSError as exc:
                    problems.append(f"ending the terminal process tree failed: {exc}")
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
                # The input pipe belongs to a writer that may still be inside
                # WriteFile; closing it under that writer would be a
                # use-after-close, so it is only freed once the writer is
                # gone.
                if self._cancel_writer(problems):
                    self._release(
                        self._input_write, "the terminal input pipe", problems
                    )
                    self._input_write = 0
                else:
                    problems.append(
                        "the terminal input pipe was kept open because a "
                        "write did not finish"
                    )
                if self._closer is None:
                    # The closer thread never ran, so nobody else owns the
                    # pseudoconsole handle or the process handle: this close
                    # is the only owner and must release both.
                    self._close_pseudoconsole()
                    with self._state_lock:
                        process = self._process
                        self._process = 0
                    self._release(process, "the terminal process", problems)
                else:
                    self._join_closer(problems)
                # The reader's thread handle belongs to the reader while it
                # runs: it is what a later close cancels with. It is only
                # released once the thread is gone.
                if stopped:
                    with self._state_lock:
                        reader_handle = self._reader_handle
                        self._reader_handle = None
                    if reader_handle is not None:
                        self._release(
                            reader_handle, "the terminal reader thread", problems
                        )
                # The process handle belongs to the closer thread, which
                # waits on it; the close that waits for that thread has
                # therefore already seen it released.
                self._tree.close()
                with self._state_lock:
                    # Only a close that released everything is a closed
                    # terminal. A failed one keeps what it still owns, and
                    # the next call tries again.
                    self._fully_closed = not problems
                if problems:
                    raise OSError("closing the terminal failed: " + "; ".join(problems))

        def _release(self, handle: int, what: str, problems: list[str]) -> None:
            """Close one handle, recording a refusal instead of hiding it."""
            if not handle:
                return
            if not _CloseHandle(handle):
                error = ctypes.get_last_error()
                problems.append(f"closing {what} failed: {ctypes.WinError(error)}")

        def _cancel_writer(self, problems: list[str]) -> bool:
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

            False when a write is still in flight: the input pipe then
            belongs to that writer and must not be closed under it.
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
                        return True
                if time.monotonic() >= deadline:
                    problems.append("a terminal write did not finish")
                    return False
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
        attribute_initialized = False
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
            # Only an initialized list may be deleted; an allocated buffer
            # that never got initialized must simply be released.
            attribute_initialized = True
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
            # The pseudoconsole replaces the child's console, so the three
            # standard-handle slots stay null; the flag is what tells Windows
            # to use them rather than the parent's handles.
            startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES

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
            # The constructor is the transaction's last step: if it returns,
            # the terminal owns the pipes, the pseudoconsole, the child and
            # the job. If it raises, it never took them, so the rollback
            # below is closing handles that are still the transaction's.
            terminal = WindowsTerminalProcess(
                pid=int(process_info.dwProcessId),
                process=process_handle,
                tree=tree,
                input_write=int(input_write.value or 0),
                output_read=int(output_read.value or 0),
                hpcon=int(hpcon.value or 0),
            )
        except BaseException:
            if process_handle:
                _TerminateProcess(process_handle, 1)
                _WaitForSingleObject(process_handle, 5000)
            if attribute_initialized and attribute_list:
                _DeleteProcThreadAttributeList(attribute_list)
            # The output pipe is closed before the pseudoconsole, and it is
            # **not** read: the pseudoconsole's write end is still open, and
            # a read on a pipe whose write end is open blocks for ever. On
            # Windows older than 11 24H2, ClosePseudoConsole waits for the
            # clients to disconnect, and they only do so once the output
            # pipe is closed — so the pipe goes first, and the pseudoconsole
            # after it. There is nothing to read on a failed spawn: no
            # reader was ever started, and no frame is owed to anybody.
            for handle in reversed(opened):
                _CloseHandle(handle)
            opened.clear()
            if hpcon.value:
                _ClosePseudoConsole(hpcon)
            if tree is not None:
                tree.close()
            raise

        if attribute_initialized and attribute_list:
            _DeleteProcThreadAttributeList(attribute_list)

        try:
            terminal._start()
        except BaseException as exc:
            try:
                terminal.close()
            except OSError as cleanup:
                exc.add_note(f"closing the failed terminal also failed: {cleanup}")
            raise
        return terminal
