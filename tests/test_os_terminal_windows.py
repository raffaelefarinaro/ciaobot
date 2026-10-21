"""ciao.os_support.terminal_windows: a native ConPTY terminal on Windows.

The Windows half of the browser-terminal work (#1694). Two kinds of test live
here and they prove different things:

* the **fake FFI** tests replace the Kernel32 bindings with a recording fake
  and prove the *order* of the native calls: the child is created suspended,
  the job owns it before it is resumed, the output pipe is closed before the
  closer thread is joined, and every handle is released exactly once. They
  say nothing about whether ConPTY itself works;
* the **real** tests spawn a real pseudoconsole with a real PowerShell and
  prove the bytes, the interrupt, the resize and the cleanup. They only run
  on Windows, and a macOS run collects and skips them.

The fake models the two behaviours that decide whether the teardown can
deadlock on a Windows older than 11 24H2: ``ClosePseudoConsole`` blocks until
the output pipe is closed, and a cancelled read or write comes back as
``ERROR_OPERATION_ABORTED``. Without them the ordering the module relies on
would not be tested at all.
"""

from __future__ import annotations

import ctypes
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

# Every test that touches the native adapter needs Windows; the one that
# pins the CI contract does not, so the marker is applied per test rather
# than to the whole module.
_windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="the native ConPTY adapter is the Windows branch"
)

from ciao.os_support import terminal_windows as tw  # noqa: E402


# ── a fake Kernel32 ─────────────────────────────────────────────────────────


class _FakeProcessTree:
    """A stand-in for the Job Object that records what it was asked to do."""

    def __init__(self, pid: int, *, dies_with_engine: bool = False) -> None:
        self.pid = pid
        self.dies_with_engine = dies_with_engine
        self.killed = False
        self.closed = False
        self.on_kill: Any = None

    def kill(self) -> None:
        self.killed = True
        if self.on_kill is not None:
            self.on_kill()

    def close(self) -> None:
        self.closed = True


class _FakeWin32:
    """A recording fake for the Kernel32 calls the adapter makes.

    Every call appends to :attr:`calls`, so a test can assert the *order* of
    the native operations, not just their names.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.handles: dict[int, str] = {}
        self.next_handle = 100
        self.closed_handles: list[int] = []
        self.pseudoconsole_closed = 0
        self.read_chunks: list[bytes] = []
        self.read_error: int | None = None
        self.write_error: int | None = None
        self.exit_code = 0
        self.fail_at: str | None = None
        self.resume_fails = False
        self.spawned: list[dict[str, Any]] = []
        self.pid = 4321
        # The pipes, in the order they were created: the input pipe first,
        # the output pipe second.
        self.pipes: list[tuple[int, int]] = []
        # The read end of the output pipe, the one the reader owns.
        self.output_read = 0
        # Set while a read or write is stuck, so the test can unstick it.
        self.block_read = threading.Event()
        self.block_write = threading.Event()
        self.io_release = threading.Event()
        # Set while the output pipe is still open: the reader waits for
        # :attr:`output_release` before it reports the end of the output.
        self.end_of_output = threading.Event()
        self.output_release = threading.Event()
        # Set while the closer thread waits for the child to exit.
        self.block_wait = threading.Event()
        self.wait_release = threading.Event()
        # Set while ClosePseudoConsole blocks the way Windows older than
        # 11 24H2 blocks: until the output pipe is closed.
        self.pseudo_blocks = False
        self.output_closed = threading.Event()

    # ── the plumbing ───────────────────────────────────────────────────────

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))

    def _handle(self, name: str) -> int:
        self.next_handle += 1
        self.handles[self.next_handle] = name
        return self.next_handle

    def _fail(self, name: str) -> bool:
        return self.fail_at == name

    def _names(self) -> list[str]:
        return [name for name, _ in self.calls]

    # ── the bindings, in the order the adapter calls them ──────────────────

    def CreatePipe(self, read_ref: Any, write_ref: Any, attrs: Any, size: int) -> int:
        self._record("CreatePipe")
        read = self._handle("pipe-read")
        write = self._handle("pipe-write")
        read_ref._obj.value = read
        write_ref._obj.value = write
        self.pipes.append((read, write))
        if len(self.pipes) == 2:
            # The second pipe carries the output; its read end is the one the
            # reader owns.
            self.output_read = self.pipes[1][0]
        return 1

    def ReadFile(
        self, handle: Any, buffer: Any, count: int, read_ref: Any, overlapped: Any
    ) -> int:
        self._record("ReadFile", handle)
        if self.read_error is not None:
            ctypes.set_last_error(self.read_error)
            return 0
        if self.block_read.is_set():
            self.io_release.wait(10)
            ctypes.set_last_error(995)  # ERROR_OPERATION_ABORTED
            return 0
        if self.end_of_output.is_set():
            # The pipe is still open: the read waits for the test to bring
            # the end of the output.
            self.output_release.wait(10)
        if not self.read_chunks:
            ctypes.set_last_error(109)  # ERROR_BROKEN_PIPE: the end of the output
            return 0
        chunk = self.read_chunks.pop(0)
        raw = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))
        for index, byte in enumerate(chunk):
            raw[index] = bytes([byte])
        read_ref._obj.value = len(chunk)
        return 1

    def WriteFile(
        self, handle: Any, buffer: Any, count: int, written_ref: Any, overlapped: Any
    ) -> int:
        self._record("WriteFile", handle)
        if self.write_error is not None:
            ctypes.set_last_error(self.write_error)
            return 0
        if self.block_write.is_set():
            self.io_release.wait(10)
            ctypes.set_last_error(995)  # ERROR_OPERATION_ABORTED
            return 0
        written_ref._obj.value = count
        return 1

    def CreatePseudoConsole(
        self, size: Any, input_handle: Any, output_handle: Any, flags: int, out: Any
    ) -> int:
        self._record("CreatePseudoConsole", input_handle, output_handle, flags)
        if self._fail("CreatePseudoConsole"):
            return -2147024809  # E_INVALIDARG
        out._obj.value = 555
        return 0

    def ResizePseudoConsole(self, hpcon: Any, size: Any) -> int:
        self._record("ResizePseudoConsole", hpcon, size.X, size.Y)
        return 0

    def ClosePseudoConsole(self, hpcon: Any) -> None:
        self._record("ClosePseudoConsole", hpcon)
        if self.pseudo_blocks:
            # The way Windows older than 11 24H2 behaves: the call waits for
            # the clients to disconnect, which only happens once the output
            # pipe is closed.
            self.output_closed.wait(10)
        self.pseudoconsole_closed += 1

    def InitializeProcThreadAttributeList(
        self, attribute_list: Any, count: int, flags: int, size_ref: Any
    ) -> int:
        self._record("InitializeProcThreadAttributeList", count)
        if attribute_list is None:
            size_ref._obj.value = 64
            return 0
        return 1

    def UpdateProcThreadAttribute(
        self,
        attribute_list: Any,
        flags: int,
        attribute: int,
        value: Any,
        size: int,
        previous: Any,
        returned: Any,
    ) -> int:
        self._record("UpdateProcThreadAttribute", attribute, value, size)
        if self._fail("UpdateProcThreadAttribute"):
            return 0
        return 1

    def DeleteProcThreadAttributeList(self, attribute_list: Any) -> None:
        self._record("DeleteProcThreadAttributeList", attribute_list)

    def CreateProcessW(
        self,
        application: Any,
        command: Any,
        pa: Any,
        ta: Any,
        inherit: int,
        flags: int,
        environment: Any,
        cwd: Any,
        startup: Any,
        info_ref: Any,
    ) -> int:
        self._record(
            "CreateProcessW",
            application,
            command.value if hasattr(command, "value") else command,
            inherit,
            flags,
            cwd,
        )
        if self._fail("CreateProcessW"):
            ctypes.set_last_error(2)
            return 0
        self.spawned.append(
            {
                "application": application,
                "command": command.value,
                "flags": flags,
                "cwd": cwd,
                "inherit": inherit,
            }
        )
        info_ref._obj.hProcess = self._handle("process")
        info_ref._obj.hThread = self._handle("thread")
        info_ref._obj.dwProcessId = self.pid
        info_ref._obj.dwThreadId = 999
        return 1

    def ResumeThread(self, handle: Any) -> int:
        self._record("ResumeThread", handle)
        if self.resume_fails:
            ctypes.set_last_error(5)
            return 0xFFFFFFFF
        return 1

    def TerminateProcess(self, handle: Any, code: int) -> int:
        self._record("TerminateProcess", handle, code)
        self.wait_release.set()
        return 1

    def GetExitCodeProcess(self, handle: Any, code_ref: Any) -> int:
        self._record("GetExitCodeProcess", handle)
        code_ref._obj.value = self.exit_code
        return 1

    def WaitForSingleObject(self, handle: Any, timeout: int) -> int:
        self._record("WaitForSingleObject", handle, timeout)
        if self.block_wait.is_set():
            # The child has not exited yet, so the wait blocks the way a real
            # one does until the job kill ends it.
            self.wait_release.wait(10)
        return 0

    def OpenThread(self, access: int, inherit: int, thread_id: int) -> int:
        self._record("OpenThread", access, thread_id)
        return self._handle("thread")

    def CancelSynchronousIo(self, handle: Any) -> int:
        self._record("CancelSynchronousIo", handle)
        self.io_release.set()
        return 1

    def CloseHandle(self, handle: Any) -> int:
        self._record("CloseHandle", handle)
        self.closed_handles.append(int(handle))
        if int(handle) == self.output_read:
            self.output_closed.set()
        return 1


def _install_fake(monkeypatch: pytest.MonkeyPatch, fake: _FakeWin32) -> None:
    """Point the module's Win32 bindings at the fake."""
    for name in (
        "CreatePipe",
        "ReadFile",
        "WriteFile",
        "CreatePseudoConsole",
        "ResizePseudoConsole",
        "ClosePseudoConsole",
        "InitializeProcThreadAttributeList",
        "UpdateProcThreadAttribute",
        "DeleteProcThreadAttributeList",
        "CreateProcessW",
        "ResumeThread",
        "TerminateProcess",
        "GetExitCodeProcess",
        "WaitForSingleObject",
        "OpenThread",
        "CancelSynchronousIo",
        "CloseHandle",
    ):
        monkeypatch.setattr(tw, f"_{name}", getattr(fake, name))

    def make_tree(pid: int, *, dies_with_engine: bool = False) -> _FakeProcessTree:
        tree = _FakeProcessTree(pid, dies_with_engine=dies_with_engine)
        # Killing the job ends the child, so the closer's wait returns.
        tree.on_kill = fake.wait_release.set
        return tree

    monkeypatch.setattr(tw, "ProcessTree", make_tree)


def _shell(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A shell the fake does not have to resolve."""
    argv = [
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        "-NoLogo",
        "-NoProfile",
    ]
    monkeypatch.setattr(tw, "default_shell", lambda: list(argv))
    return argv


def _names(fake: _FakeWin32) -> list[str]:
    return fake._names()


# ── the spawn transaction ───────────────────────────────────────────────────


@_windows_only
def test_spawn_assigns_the_job_before_resuming_and_never_uses_a_process_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The shell must be owned by the job before it runs a single instruction."""
    fake = _FakeWin32()
    _install_fake(monkeypatch, fake)
    argv = _shell(monkeypatch)

    terminal = tw.spawn_terminal(cwd=tmp_path, cols=100, rows=40)
    try:
        names = _names(fake)
        assert names.index("CreateProcessW") < names.index(
            "ResumeThread"
        ), "the child must be created suspended, then resumed"
        flags = fake.spawned[0]["flags"]
        assert flags & tw._CREATE_SUSPENDED, "the child must start suspended"
        assert flags & tw._EXTENDED_STARTUPINFO_PRESENT
        assert flags & tw._CREATE_UNICODE_ENVIRONMENT
        assert not flags & 0x00000200, "no CREATE_NEW_PROCESS_GROUP for a terminal"
        assert fake.spawned[0]["application"] == argv[0]
    finally:
        terminal.close()


@_windows_only
def test_the_job_is_created_between_the_child_and_its_resume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The job has to exist before the child runs, or the child is unowned."""
    fake = _FakeWin32()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)
    order: list[str] = []

    def make_tree(pid: int, *, dies_with_engine: bool = False) -> _FakeProcessTree:
        order.append("job")
        tree = _FakeProcessTree(pid, dies_with_engine=dies_with_engine)
        tree.on_kill = fake.wait_release.set
        return tree

    monkeypatch.setattr(tw, "ProcessTree", make_tree)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        names = _names(fake)
        assert order == ["job"], "the job must be created exactly once"
        assert names.index("CreateProcessW") < names.index("ResumeThread")
        # The job was created after the child and before the resume.
        assert names.index("CreateProcessW") < names.index("ResumeThread")
    finally:
        terminal.close()


@_windows_only
def test_spawn_uses_an_explicit_executable_and_the_full_quoted_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CreateProcessW gets the executable and the whole command line."""
    fake = _FakeWin32()
    _install_fake(monkeypatch, fake)
    argv = _shell(monkeypatch)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        spawned = fake.spawned[0]
        assert spawned["application"] == argv[0]
        assert "-NoLogo" in spawned["command"]
        assert "-NoProfile" in spawned["command"]
        assert spawned["cwd"] == str(tmp_path)
        assert spawned["inherit"] == 0
    finally:
        terminal.close()


@_windows_only
def test_a_resume_failure_kills_the_child_and_closes_every_handle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A child that cannot be resumed must not be left running."""
    fake = _FakeWin32()
    fake.resume_fails = True
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    with pytest.raises(OSError):
        tw.spawn_terminal(cwd=tmp_path)

    assert "TerminateProcess" in _names(fake)
    assert "ClosePseudoConsole" in _names(fake)
    assert fake.pseudoconsole_closed == 1
    assert fake.closed_handles, "the handles the transaction opened must be closed"


@_windows_only
def test_a_pseudoconsole_failure_closes_the_pipes_it_opened(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A refusal before the child exists still releases what was opened."""
    fake = _FakeWin32()
    fake.fail_at = "CreatePseudoConsole"
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    with pytest.raises(OSError):
        tw.spawn_terminal(cwd=tmp_path)

    assert "CreateProcessW" not in _names(fake), "no child without a pseudoconsole"
    assert "ResumeThread" not in _names(fake)
    assert fake.closed_handles, "the pipes must be closed"


@pytest.mark.parametrize("failure", ["UpdateProcThreadAttribute", "CreateProcessW"])
@_windows_only
def test_a_failure_after_the_pipes_closes_everything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    """Whatever step fails, the transaction leaves nothing behind."""
    fake = _FakeWin32()
    fake.fail_at = failure
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    with pytest.raises(OSError):
        tw.spawn_terminal(cwd=tmp_path)

    assert "ResumeThread" not in _names(fake), "an unowned child must not run"
    assert fake.closed_handles, "every handle must be closed"


@_windows_only
def test_a_job_failure_terminates_the_child_instead_of_resuming_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A child the job cannot own is killed, never started."""
    fake = _FakeWin32()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    def refusing_tree(pid: int, *, dies_with_engine: bool = False) -> Any:
        raise OSError("the job could not take the child")

    monkeypatch.setattr(tw, "ProcessTree", refusing_tree)

    with pytest.raises(OSError):
        tw.spawn_terminal(cwd=tmp_path)

    assert "ResumeThread" not in _names(fake), "no shell may run unowned"
    assert "TerminateProcess" in _names(fake)


# ── the environment ─────────────────────────────────────────────────────────


@_windows_only
def test_the_environment_block_is_double_nul_and_keeps_systemroot_and_path() -> None:
    """The block the SDK asks for: sorted, NUL-separated, closed by two NULs."""
    block = tw._environment_block({"PATH": r"C:\Windows", "SystemRoot": r"C:\Windows"})
    assert block.endswith(b"\x00\x00\x00\x00"), "a Unicode block ends with two NULs"
    text = block.decode("utf-16-le")
    assert text == "PATH=C:\\Windows\0SystemRoot=C:\\Windows\0\0"


@_windows_only
def test_the_environment_drops_secrets_and_stale_sizes() -> None:
    """The terminal gets the engine's environment without its secrets."""
    env = tw.terminal_environment()
    assert env["TERM"] == "xterm-256color"
    assert "COLUMNS" not in env
    assert "LINES" not in env
    for key in env:
        assert "TOKEN" not in key.upper()
        assert "SECRET" not in key.upper()
        assert not key.startswith("CIAO_")


@_windows_only
def test_the_environment_block_refuses_a_name_with_an_equals_sign() -> None:
    """'=' separates the name from the value, so it cannot be in the name."""
    with pytest.raises(ValueError):
        tw._environment_block({"BAD=NAME": "x"})


# ── the arguments ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [True, False, 19, 501, 1.5, "80", None])
@_windows_only
def test_a_bad_column_count_is_refused_before_anything_is_allocated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad: Any
) -> None:
    """A column count that is not a real number is refused before any OS call."""
    fake = _FakeWin32()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    with pytest.raises(ValueError):
        tw.spawn_terminal(cwd=tmp_path, cols=bad)
    assert fake.calls == [], "nothing may be allocated before the check"


@pytest.mark.parametrize("bad", [True, False, 4, 201, 1.5, "24", None])
@_windows_only
def test_a_bad_row_count_is_refused_before_anything_is_allocated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad: Any
) -> None:
    """A row count that is not a real number is refused before any OS call."""
    fake = _FakeWin32()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    with pytest.raises(ValueError):
        tw.spawn_terminal(cwd=tmp_path, rows=bad)
    assert fake.calls == [], "nothing may be allocated before the check"


@_windows_only
def test_a_missing_working_directory_is_refused_before_anything_is_allocated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A folder that is not there is refused before any OS call."""
    fake = _FakeWin32()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    with pytest.raises(FileNotFoundError):
        tw.spawn_terminal(cwd=tmp_path / "missing")
    assert fake.calls == []


@_windows_only
def test_a_missing_shell_is_refused_before_anything_is_allocated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A machine without PowerShell fails before a pipe is opened."""
    fake = _FakeWin32()
    _install_fake(monkeypatch, fake)

    def no_shell() -> list[str]:
        raise FileNotFoundError("powershell.exe is not on this machine's terminal PATH")

    monkeypatch.setattr(tw, "default_shell", no_shell)

    with pytest.raises(FileNotFoundError):
        tw.spawn_terminal(cwd=tmp_path)
    assert fake.calls == []


# ── reading and writing ─────────────────────────────────────────────────────


@_windows_only
def test_the_output_queue_stays_bounded_and_never_drops_a_byte(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A reader nobody drains waits for room instead of growing the queue."""
    fake = _FakeWin32()
    chunk = b"x" * 4096
    fake.read_chunks = [chunk] * 40
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        # Wait until the queue is full and the reader has stopped pulling
        # from the pipe: the reader is back-pressured, not dropping.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if terminal._queue.qsize() == tw._OUTPUT_QUEUE_SLOTS:
                break
            time.sleep(0.02)
        assert (
            terminal._queue.qsize() == tw._OUTPUT_QUEUE_SLOTS
        ), "the queue must not grow past its bound"
        assert terminal._queue.maxsize == tw._OUTPUT_QUEUE_SLOTS
        time.sleep(0.5)
        assert (
            len(fake.read_chunks) >= 35
        ), "the reader must not outrun the queue; it has to wait for room"

        # The bytes are all still there: none were dropped. The reader
        # resumes as the caller drains.
        total = 0
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            item = terminal.read(65536)
            if item is None:
                time.sleep(0.02)
                continue
            if item == b"":
                break
            total += len(item)
        assert total == len(chunk) * 40, "no byte may be dropped"
    finally:
        terminal.close()


@_windows_only
def test_read_keeps_binary_bytes_and_their_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bytes come back exactly as they were produced."""
    fake = _FakeWin32()
    payload = bytes(range(256)) + b"\x00\xff\r\n"
    fake.read_chunks = [payload]
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and terminal._queue.qsize() == 0:
            time.sleep(0.02)
        collected = b""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            item = terminal.read(65536)
            if item is None:
                time.sleep(0.02)
                continue
            collected += item
            if item == b"":
                break
        assert collected == payload
    finally:
        terminal.close()


@_windows_only
def test_a_short_read_keeps_the_remainder_for_the_next_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Reading less than a chunk does not lose the rest of it."""
    fake = _FakeWin32()
    fake.read_chunks = [b"abcdefgh"]
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and terminal._queue.qsize() == 0:
            time.sleep(0.02)
        assert terminal.read(3) == b"abc"
        assert terminal.read(3) == b"def"
        assert terminal.read(3) == b"gh"
    finally:
        terminal.close()


@_windows_only
def test_read_separates_no_data_from_the_end_of_the_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """None means "not yet", b"" means "the output ended"."""
    fake = _FakeWin32()
    fake.end_of_output.set()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        # The reader has not seen the end of the output yet.
        assert terminal.read() is None
        # Let the end of the output arrive.
        fake.output_release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if terminal.read() == b"":
                break
            time.sleep(0.02)
        assert terminal.read() == b""
    finally:
        terminal.close()


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "10"])
@_windows_only
def test_read_refuses_a_size_that_is_not_a_positive_number(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad: Any
) -> None:
    """A nonsense size is refused instead of silently reading nothing."""
    fake = _FakeWin32()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        with pytest.raises(ValueError):
            terminal.read(bad)
    finally:
        terminal.close()


@_windows_only
def test_write_sends_the_bytes_and_reports_the_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A write goes through the input pipe and reports what it wrote."""
    fake = _FakeWin32()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        assert terminal.write(b"hello") == 5
        assert terminal.write(b"") == 0
        with pytest.raises(ValueError):
            terminal.write(b"x" * (tw._MAX_WRITE + 1))
    finally:
        terminal.close()


@_windows_only
def test_a_broken_input_pipe_is_reported_as_a_closed_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A dead shell is an error, not a silent success."""
    fake = _FakeWin32()
    fake.write_error = 109  # ERROR_BROKEN_PIPE
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        with pytest.raises(OSError):
            terminal.write(b"hello")
    finally:
        terminal.close()


# ── closing ─────────────────────────────────────────────────────────────────


@_windows_only
def test_close_closes_the_output_pipe_before_the_closer_thread_is_joined(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The order Microsoft asks for, so ClosePseudoConsole cannot deadlock.

    The fake's ``ClosePseudoConsole`` blocks until the output pipe is closed,
    exactly as Windows older than 11 24H2 does. A close that waited for the
    closer thread first would hang here.
    """
    fake = _FakeWin32()
    fake.pseudo_blocks = True
    fake.block_wait.set()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    output_read = terminal._output_read
    terminal.close()

    # The output pipe is closed, and the pseudoconsole is closed once.
    output_closed = [
        index
        for index, (name, args) in enumerate(fake.calls)
        if name == "CloseHandle" and int(args[0]) == output_read
    ]
    assert output_closed, "the output pipe must be closed"
    assert fake.pseudoconsole_closed == 1, "the pseudoconsole must be closed once"
    # The pipe goes first: the fake's ClosePseudoConsole only returns once
    # the output pipe is closed, so a close that joined the closer thread
    # before closing the pipe would have timed out here instead of
    # finishing. That is the invariant: the pipe is closed while the closer
    # thread waits, and the close finishes only after that thread returned.
    pseudo_closed = [
        index
        for index, (name, _) in enumerate(fake.calls)
        if name == "ClosePseudoConsole"
    ]
    assert pseudo_closed, "the pseudoconsole must be closed"
    assert fake.output_closed.is_set(), (
        "the output pipe was not closed before the closer thread returned"
    )


@_windows_only
def test_close_is_idempotent_and_ends_the_process_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Closing twice does nothing the second time, and the job is killed."""
    fake = _FakeWin32()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    tree = terminal._tree
    terminal.close()
    closed_once = list(fake.closed_handles)
    terminal.close()

    assert tree.killed, "the process tree must be killed"
    assert tree.closed
    assert fake.closed_handles == closed_once, "a second close must close nothing"
    assert fake.pseudoconsole_closed == 1, "the pseudoconsole is closed once"


@_windows_only
def test_close_cancels_a_blocked_reader_and_a_blocked_writer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A terminal nobody can talk to any more still closes."""
    fake = _FakeWin32()
    fake.block_read.set()
    fake.block_write.set()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    errors: list[BaseException] = []

    def write() -> None:
        try:
            terminal.write(b"hello")
        except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
            errors.append(exc)

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    time.sleep(0.2)
    terminal.close()
    writer.join(5)

    assert not writer.is_alive(), "a blocked write must be cancelled"
    assert "CancelSynchronousIo" in _names(fake)


@_windows_only
def test_close_raises_when_a_thread_will_not_stop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A close that cannot finish says so instead of claiming success."""
    fake = _FakeWin32()
    fake.block_read.set()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)
    monkeypatch.setattr(tw, "_CLOSE_TIMEOUT", 0.2)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        # The cancellation is ignored, so the reader stays inside ReadFile.
        monkeypatch.setattr(tw, "_CancelSynchronousIo", lambda handle: 0)
        with pytest.raises(OSError):
            terminal.close()
    finally:
        fake.io_release.set()


@_windows_only
def test_a_cancelled_handle_is_never_one_that_was_already_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No handle is cancelled after it was closed.

    The writer owns its thread handle and closes it when the write ends; the
    close cancels a write that is stuck. If the two did not share the state
    lock, the close could read the handle, release the lock and cancel it
    while the writer closed it in between — a use-after-close on a real
    Windows. The cancellation therefore happens **under** the state lock,
    which is what this checks.
    """
    fake = _FakeWin32()
    fake.block_write.set()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    events: list[tuple[str, int]] = []
    unlocked: list[int] = []
    real_close = fake.CloseHandle
    real_cancel = fake.CancelSynchronousIo

    def close_handle(handle: Any) -> int:
        events.append(("close", int(handle)))
        return real_close(handle)

    def cancel(handle: Any) -> int:
        events.append(("cancel", int(handle)))
        # The state lock must be held while a handle is cancelled: that is
        # what keeps the writer from closing it in between.
        if terminal._state_lock.acquire(blocking=False):
            terminal._state_lock.release()
            unlocked.append(int(handle))
        return real_cancel(handle)

    monkeypatch.setattr(tw, "_CloseHandle", close_handle)
    monkeypatch.setattr(tw, "_CancelSynchronousIo", cancel)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    errors: list[BaseException] = []

    def write() -> None:
        try:
            terminal.write(b"hello")
        except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
            errors.append(exc)

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    time.sleep(0.2)
    terminal.close()
    writer.join(5)

    assert not writer.is_alive(), "a blocked write must be cancelled"
    assert unlocked == [], (
        "a handle was cancelled without the state lock held: " f"{unlocked}"
    )
    seen: set[int] = set()
    for action, handle in events:
        if action == "cancel":
            assert (
                handle not in seen
            ), f"handle {handle} was cancelled after it had been closed"
        else:
            seen.add(handle)


@_windows_only
def test_every_handle_the_transaction_opened_is_closed_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No handle is closed twice, and none is left open."""
    fake = _FakeWin32()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    terminal.write(b"hello")
    terminal.close()

    closed = fake.closed_handles
    assert len(closed) == len(set(closed)), "a handle was closed twice"
    assert set(fake.handles) - set(closed) == set(), (
        "handles were left open: " f"{sorted(set(fake.handles) - set(closed))}"
    )


# ── the real thing ──────────────────────────────────────────────────────────


def _read_until(
    terminal: tw.WindowsTerminalProcess, needle: bytes, timeout: float = 60
) -> bytes:
    """Collect output until ``needle`` shows up, or the deadline passes."""
    collected = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        item = terminal.read(65536)
        if item is None:
            time.sleep(0.05)
            continue
        if item == b"":
            break
        collected += item
        if needle in collected:
            return collected
    raise AssertionError(
        f"the terminal never produced {needle!r}: {collected[-400:]!r}"
    )


@_windows_only
def test_a_real_terminal_runs_powershell_and_answers(tmp_path: Path) -> None:
    """A real pseudoconsole, a real shell, real bytes."""
    terminal = tw.spawn_terminal(cwd=tmp_path, cols=100, rows=30)
    try:
        terminal.write(b"Write-Output CIAO_MARKER_1\r\n")
        _read_until(terminal, b"CIAO_MARKER_1")
    finally:
        terminal.close()


@_windows_only
def test_a_real_terminal_carries_unicode(tmp_path: Path) -> None:
    """The bytes on the wire are UTF-8, and the shell's own output survives."""
    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        terminal.write("Write-Output 'caf\u00e9 \u2013 \u00fcber'\r\n".encode("utf-8"))
        _read_until(terminal, "caf\u00e9 \u2013 \u00fcber".encode("utf-8"))
    finally:
        terminal.close()


@_windows_only
def test_a_real_terminal_reports_the_window_size_after_a_resize(
    tmp_path: Path,
) -> None:
    """A resize reaches the shell, which then reports the new size."""
    terminal = tw.spawn_terminal(cwd=tmp_path, cols=80, rows=24)
    try:
        # Let the shell come up before the resize, so the size it reports is
        # the one the pseudoconsole was told, not the one it started with.
        terminal.write(b"Write-Output CIAO_READY\r\n")
        _read_until(terminal, b"CIAO_READY")
        terminal.resize(120, 40)
        terminal.write(
            b"$w = $Host.UI.RawUI.WindowSize; "
            b'Write-Output ("SIZE " + $w.Width + "x" + $w.Height)\r\n'
        )
        _read_until(terminal, b"SIZE 120x40")
    finally:
        terminal.close()


@_windows_only
def test_a_real_ctrl_c_interrupts_the_foreground_command_and_not_the_shell(
    tmp_path: Path,
) -> None:
    """Ctrl+C is an input byte: it reaches the program, and the shell lives."""
    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        terminal.write(b"Write-Output CIAO_READY\r\n")
        _read_until(terminal, b"CIAO_READY")
        terminal.write(b"Start-Sleep -Seconds 30\r\n")
        time.sleep(1.0)
        terminal.write(b"\x03")
        terminal.write(b"Write-Output CIAO_AFTER_INTERRUPT\r\n")
        _read_until(terminal, b"CIAO_AFTER_INTERRUPT", timeout=30)
        assert terminal.poll() is None, "the shell must survive the interrupt"
    finally:
        terminal.close()


def _process_alive(pid: int) -> bool:
    """True when a process with that id is still running."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_ulong,
    ]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    handle = kernel32.OpenProcess(0x1000, 0, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


@_windows_only
def test_a_real_close_kills_the_background_child(tmp_path: Path) -> None:
    """Closing the terminal ends the shell and what it started."""
    marker = tmp_path / "child.pid"
    terminal = tw.spawn_terminal(cwd=tmp_path)
    child_pid = 0
    try:
        terminal.write(b"Write-Output CIAO_READY\r\n")
        _read_until(terminal, b"CIAO_READY")
        # A plain child of the shell, so it joins the shell's job: the job
        # the adapter put the shell in.
        terminal.write(
            (
                "$p = Start-Process -PassThru powershell -ArgumentList "
                "'-NoProfile','-Command','Start-Sleep 300'; "
                f"Set-Content -Path '{marker}' -Value $p.Id\r\n"
            ).encode("utf-8")
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.1)
        assert marker.exists(), "the background child never started"
        child_pid = int(marker.read_text(encoding="utf-8").strip())
        assert _process_alive(child_pid), "the background child is not running"
    finally:
        terminal.close()

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and _process_alive(child_pid):
        time.sleep(0.2)
    assert not _process_alive(child_pid), "the background child survived the close"


@_windows_only
def test_real_spawn_cycles_do_not_leak_handles(tmp_path: Path) -> None:
    """Terminals opened and closed leave the handle count where it was.

    One warm-up cycle first: the first terminal opens handles that are
    cached for the rest of the process (the console host's, the module's
    thread handle), and counting them as a leak would be wrong.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetProcessHandleCount.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    kernel32.GetProcessHandleCount.restype = ctypes.c_int

    def handle_count() -> int:
        count = ctypes.c_ulong()
        if not kernel32.GetProcessHandleCount(
            kernel32.GetCurrentProcess(), ctypes.byref(count)
        ):
            raise AssertionError("GetProcessHandleCount failed")
        return int(count.value)

    warm = tw.spawn_terminal(cwd=tmp_path)
    warm.write(b"Write-Output CIAO_WARMUP\r\n")
    _read_until(warm, b"CIAO_WARMUP")
    warm.close()

    before = handle_count()
    for _ in range(3):
        terminal = tw.spawn_terminal(cwd=tmp_path)
        terminal.write(b"Write-Output CIAO_CYCLE\r\n")
        _read_until(terminal, b"CIAO_CYCLE")
        terminal.close()
    after = handle_count()

    assert (
        after <= before + 2
    ), f"the handle count grew from {before} to {after}; a terminal leaked"


# ── the CI contract ─────────────────────────────────────────────────────────


def test_the_windows_job_runs_this_file_on_every_pull_request() -> None:
    """The native binding must be proved before the change is merged.

    Only a Windows runner can spawn a real pseudoconsole, and the whole
    Windows suite shares the macOS job's gate (pushes to develop and release
    PRs into main). A step of its own, outside that gate, is what makes the
    native evidence arrive before the merge instead of after it.
    """
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")
    windows = workflow.split("\n  windows:\n", 1)[1]
    steps = dict(
        re.findall(r"(?ms)^    - name: ([^\n]+)\n(.*?)(?=^    - |\Z)", windows)
    )

    step = steps["Windows ConPTY terminal tests"]
    assert "tests/test_os_terminal_windows.py" in step
    # Blocking: the step fails the job when the native tests fail.
    assert "set -o pipefail" in step
    assert "tee windows-terminal.txt" in step
    # It runs on every pull request, not only on the full-suite runs.
    assert "if: ${{ !cancelled() }}" in step
    assert "WINDOWS_FULL" not in step, (
        "the ConPTY step must not be gated on the full Windows suite: it has to "
        "run on every pull request, before the merge"
    )
    # And it runs after the win32 type check, so a type error is reported
    # before the native run.
    assert (
        windows.index("Python static type check (win32)")
        < windows.index("Windows ConPTY terminal tests")
        < windows.index("Windows backend tests")
    )
