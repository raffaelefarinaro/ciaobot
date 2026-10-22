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
import queue
import re
import subprocess
import sys
import threading
import time
import warnings
from pathlib import Path
from typing import Any

import pytest

# Every test that touches the native adapter needs Windows; the one that
# pins the CI contract does not, so the marker is applied per test rather
# than to the whole module.
_windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="the native ConPTY adapter is the Windows branch"
)

from ciao.os_support import terminal_child_windows as tcw  # noqa: E402
from ciao.os_support import terminal_windows as tw  # noqa: E402

#: The helper that clears the terminal's interrupt attribute and starts the
#: shell. The path is spelled out rather than read from the adapter, so the
#: file is found on a machine that is not Windows too.
CHILD_HELPER = (
    Path(__file__).resolve().parents[1]
    / "ciao"
    / "os_support"
    / "terminal_child_windows.py"
)


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
        # Set to make a read block until the output pipe is closed, the way
        # a real pipe with its write end open does.
        self.read_until_closed = threading.Event()
        # Set while the output pipe is still open: the reader waits for
        # :attr:`output_release` before it reports the end of the output.
        self.end_of_output = threading.Event()
        self.output_release = threading.Event()
        # Set to hold the reader just before it reports the end of the
        # output, so a test can put the final chunk in the queue first.
        self.pause_before_end = threading.Event()
        self.pause_release = threading.Event()
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
        if self.read_until_closed.is_set():
            # A real pipe with its write end still open: the read blocks
            # until every writer is gone. This is what a rollback that reads
            # before it closes the pipe would hit.
            self.output_closed.wait(10)
            ctypes.set_last_error(109)  # ERROR_BROKEN_PIPE
            return 0
        if self.end_of_output.is_set():
            # The pipe is still open: the read waits for the test to bring
            # the end of the output.
            self.output_release.wait(10)
        if not self.read_chunks:
            if self.pause_before_end.is_set():
                # The reader has read everything it was given and is held
                # just before it reports that the output ended.
                self.pause_release.wait(10)
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
        if self._fail("InitializeProcThreadAttributeList"):
            ctypes.set_last_error(8)  # ERROR_NOT_ENOUGH_MEMORY
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
        # The STARTUPINFOEX the adapter passed, read back through the pointer
        # it gave: that is what the child really sees.
        info = ctypes.cast(startup, ctypes.POINTER(tw._StartupInfoEx)).contents
        self.spawned.append(
            {
                "application": application,
                "command": command.value,
                "flags": flags,
                "cwd": cwd,
                "inherit": inherit,
                "dw_flags": int(info.StartupInfo.dwFlags),
                "h_std_input": int(info.StartupInfo.hStdInput or 0),
                "h_std_output": int(info.StartupInfo.hStdOutput or 0),
                "h_std_error": int(info.StartupInfo.hStdError or 0),
                "startup_cb": int(info.StartupInfo.cb),
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

    def SetHandleInformation(self, handle: Any, mask: int, flags: int) -> int:
        self._record("SetHandleInformation", handle, mask, flags)
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


class _QueueThatEndsDuringAnEmptyCheck:
    """A queue that queues the last chunk and publishes the end on one look.

    It models the one interleaving the reader can produce and a caller
    cannot see coming: the reader queues its final chunk and publishes the
    end of the output *after* the caller has looked and found nothing, but
    before the caller has finished deciding what an empty queue means.

    The producer order is the real one — the chunk first, the end after it
    — so a caller that answers "ended" without looking at the queue again
    is answering it while the last chunk is queued.
    """

    def __init__(self, owner: Any, *, on_call: int, final: bytes) -> None:
        self._owner = owner
        self._on_call = on_call
        self._final = final
        self._calls = 0
        self._pending: list[bytes] = []

    def get_nowait(self) -> bytes:
        self._calls += 1
        if self._calls == self._on_call:
            self._pending.append(self._final)
            self._owner._ended = tw._End(None)
            raise queue.Empty
        if not self._pending:
            raise queue.Empty
        return self._pending.pop(0)


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
        "SetHandleInformation",
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
def test_the_child_standard_handles_point_at_the_pseudoconsole(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The child's standard handles must be the pseudoconsole's, not ours.

    This is the regression for the native failure in #1694: without
    ``STARTF_USESTDHANDLES`` the child keeps the *parent's* standard handles,
    so the shell's prompt went to the parent's stdout and its input never
    came from the pseudoconsole. The child then saw the end of that input
    and exited at once, and the pipe carried only the console's start-up and
    tear-down sequences.

    Microsoft Terminal does the same thing
    (``siEx.StartupInfo.dwFlags = STARTF_USESTDHANDLES;``) with the three
    standard-handle slots left null, because the pseudoconsole replaces the
    child's console rather than its file descriptors.
    """
    fake = _FakeWin32()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        spawned = fake.spawned[0]
        assert spawned["dw_flags"] & tw._STARTF_USESTDHANDLES, (
            "the child must be told to use the pseudoconsole's standard "
            "handles, or it inherits the parent's"
        )
        # The pseudoconsole replaces the child's console, so the three slots
        # stay null: there is no file descriptor to hand over.
        assert spawned["h_std_input"] == 0
        assert spawned["h_std_output"] == 0
        assert spawned["h_std_error"] == 0
        # The struct size is the extended one, as the SDK asks.
        assert spawned["startup_cb"] == ctypes.sizeof(tw._StartupInfoEx)
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
    finally:
        terminal.close()


@_windows_only
def test_spawn_names_the_shell_explicitly_and_quotes_the_whole_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The shell's executable and its whole argv travel to the helper.

    The adapter no longer creates the shell itself: a fresh process has to
    clear the ignore-Ctrl+C attribute before the shell is created, or the
    shell inherits the engine's. The shell's name therefore goes to the
    helper, which creates the shell with it — still explicit, still fully
    quoted, and never guessed from a PATH.
    """
    fake = _FakeWin32()
    _install_fake(monkeypatch, fake)
    argv = _shell(monkeypatch)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        spawned = fake.spawned[0]
        command = spawned["command"]
        assert argv[0] in command, "the shell's executable must be named"
        assert "-NoLogo" in command
        assert "-NoProfile" in command
        assert str(tw.CHILD_HELPER) in command, "the helper must be named"
        assert spawned["cwd"] == str(tmp_path)
        assert (
            spawned["inherit"] == 1
        ), "the helper must inherit the pseudoconsole's handle"
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
def test_a_failed_spawn_never_reads_the_output_pipe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The rollback closes the pipe; it never reads from it.

    A pipe whose write end is still open blocks the reader, so a rollback
    that read before it closed would wait for ever: the pseudoconsole's
    write end is exactly such a writer. Nothing has to be read on a failed
    spawn — there is no reader to hand the bytes to — so the rollback only
    closes the pipe, and then the pseudoconsole.
    """
    fake = _FakeWin32()
    fake.pseudo_blocks = True
    fake.resume_fails = True
    # A read on this pipe blocks until the pipe is closed.
    fake.read_until_closed.set()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    with pytest.raises(OSError):
        tw.spawn_terminal(cwd=tmp_path)

    assert "ReadFile" not in _names(fake), (
        "the rollback must not read from the output pipe; the read would "
        "block until the pipe is closed"
    )
    assert fake.pseudoconsole_closed == 1, "the pseudoconsole must be closed"
    # The fake's ClosePseudoConsole only returns once the output pipe is
    # closed, so a rollback that closed the pseudoconsole first would have
    # waited out its timeout here.
    assert (
        fake.output_closed.is_set()
    ), "the output pipe must be closed before the pseudoconsole is closed"
    pipe_closed = [
        index
        for index, (name, args) in enumerate(fake.calls)
        if name == "CloseHandle" and int(args[0]) == fake.output_read
    ]
    pseudo_closed = [
        index
        for index, (name, _) in enumerate(fake.calls)
        if name == "ClosePseudoConsole"
    ]
    assert pipe_closed and pseudo_closed
    assert (
        pipe_closed[0] < pseudo_closed[0]
    ), "the output pipe must be closed before the pseudoconsole"


@_windows_only
def test_a_failed_spawn_closes_every_handle_exactly_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The rollback releases each handle it opened, and no handle twice."""
    fake = _FakeWin32()
    fake.resume_fails = True
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    with pytest.raises(OSError):
        tw.spawn_terminal(cwd=tmp_path)

    closed = fake.closed_handles
    assert len(closed) == len(set(closed)), "a handle was closed twice"
    assert set(fake.handles) - set(closed) == set(), (
        "handles were left open: " f"{sorted(set(fake.handles) - set(closed))}"
    )


@_windows_only
def test_a_failed_attribute_list_is_never_deleted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A list that was never initialized is not deleted.

    ``DeleteProcThreadAttributeList`` on a buffer that was only allocated
    is a call into an uninitialized structure. The list is deleted only
    when it was really initialized.
    """
    fake = _FakeWin32()
    fake.fail_at = "InitializeProcThreadAttributeList"
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    with pytest.raises(OSError):
        tw.spawn_terminal(cwd=tmp_path)

    assert "DeleteProcThreadAttributeList" not in _names(
        fake
    ), "an attribute list that was never initialized must not be deleted"


@_windows_only
def test_an_initialized_attribute_list_is_deleted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A list that was initialized is deleted exactly once."""
    fake = _FakeWin32()
    fake.fail_at = "CreateProcessW"
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    with pytest.raises(OSError):
        tw.spawn_terminal(cwd=tmp_path)

    deleted = [
        name for name, _ in fake.calls if name == "DeleteProcThreadAttributeList"
    ]
    assert deleted == [
        "DeleteProcThreadAttributeList"
    ], "an initialized attribute list must be deleted exactly once"


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
def test_the_shell_is_resolved_on_the_canonical_terminal_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PowerShell comes from the terminal PATH, and from nowhere else.

    A shell that is not on the PATH the user's own terminal resolves is
    not on this machine's terminal PATH: reaching into ``SystemRoot`` for
    it would be a fallback the plan forbids, and it would run a program
    the user's terminal would not have run.
    """
    monkeypatch.setattr(tw, "terminal_path", lambda: r"C:\Windows\System32")
    monkeypatch.setattr(
        tw, "resolve_executable", lambda cmd, *, path: r"C:\Windows\System32\pwsh.exe"
    )
    assert tw.default_shell() == [
        r"C:\Windows\System32\pwsh.exe",
        "-NoLogo",
        "-NoProfile",
    ]

    # The resolution is asked for the one command, on the one path.
    asked: list[tuple[str, str]] = []

    def resolve(cmd: str, *, path: str) -> str | None:
        asked.append((cmd, path))
        return None

    monkeypatch.setattr(tw, "resolve_executable", resolve)
    assert tw.default_shell.__doc__ is not None
    with pytest.raises(FileNotFoundError):
        tw.default_shell()
    assert asked == [
        ("powershell.exe", r"C:\Windows\System32")
    ], "the shell must be looked for on the canonical terminal PATH only"


@_windows_only
def test_a_close_never_claims_success_while_a_thread_is_alive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A close that could not stop a thread does not report a closed terminal.

    The close waits for the reader and the closer thread, and it says so
    when one of them is still running. It must not claim the terminal is
    closed while a handle it still owns is out there.
    """
    fake = _FakeWin32()
    fake.block_read.set()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)
    monkeypatch.setattr(tw, "_CLOSE_TIMEOUT", 0.2)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        monkeypatch.setattr(tw, "_CancelSynchronousIo", lambda handle: 0)
        with pytest.raises(OSError):
            terminal.close()
        assert not terminal._fully_closed, (
            "a close that left a thread running must not report a closed " "terminal"
        )
        # Nothing is left claimed as closed while the reader still owns
        # its handle and its pipe.
        assert terminal._reader.is_alive()
        assert terminal._reader_handle, "the reader must still own its handle"
        assert terminal._output_read, "the reader must still own its pipe"
    finally:
        fake.io_release.set()
        monkeypatch.setattr(tw, "_CancelSynchronousIo", fake.CancelSynchronousIo)
        terminal.close()
    assert terminal._fully_closed


@_windows_only
def test_the_process_handle_is_released_exactly_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The closer thread owns the process handle and releases it once.

    The closer thread waits for the child with that handle, so nobody else
    may free it; when the wait is over the closer releases it. A handle
    that were released twice, or not at all, would show up here.
    """
    fake = _FakeWin32()
    # The child has not exited, so the closer thread still holds the
    # process handle while it waits.
    fake.block_wait.set()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    process_handle = terminal._process
    assert process_handle, "the terminal must own a process handle"
    try:
        terminal.close()
        closed = fake.closed_handles
        assert (
            closed.count(process_handle) == 1
        ), "the process handle must be released exactly once"
    finally:
        fake.wait_release.set()
        terminal.close()


@_windows_only
def test_a_close_with_a_full_queue_does_not_strand_the_reader(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A close finishes even when nobody ever drained the output.

    The queue is full, the reader is waiting for room in it, and the close
    has to end the reader rather than wait for a consumer that will never
    come. The reader notices that the session is over and gives up the
    chunk it was holding, so the close can go on.
    """
    fake = _FakeWin32()
    chunk = b"x" * 4096
    fake.read_chunks = [chunk] * 40
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        # The queue fills up and the reader waits for room.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if terminal._queue.qsize() >= tw._OUTPUT_QUEUE_SLOTS:
                break
            time.sleep(0.02)
        assert terminal._queue.qsize() >= tw._OUTPUT_QUEUE_SLOTS

        # The close must finish: the reader is stopped, not left waiting
        # for room in a queue nobody is going to drain.
        terminal.close()
        assert not terminal._reader.is_alive(), "the reader must have stopped"
        assert terminal._fully_closed, "the close must have finished"
    finally:
        terminal.close()


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
            if terminal._queue.qsize() >= tw._OUTPUT_QUEUE_SLOTS:
                break
            time.sleep(0.02)
        # The queue holds four chunks of output and nothing else: the
        # pending output is at most 256 KiB.
        assert (
            terminal._queue.qsize() == tw._OUTPUT_QUEUE_SLOTS
        ), "the queue must not grow past its bound"
        assert terminal._queue.maxsize == tw._OUTPUT_QUEUE_SLOTS
        time.sleep(0.5)
        # At most four chunks came out of the pipe: the reader is
        # back-pressured, not dropping.
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
def test_a_resize_after_the_pseudoconsole_is_gone_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A resize never meets a pseudoconsole that has been closed.

    The closer thread closes the pseudoconsole when the child is gone. A
    resize that started a moment earlier must not use the handle after
    that, so the handle is only handed out while it is alive.
    """
    fake = _FakeWin32()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        # The pseudoconsole is closed, as a natural exit does.
        terminal._close_pseudoconsole()
        assert fake.pseudoconsole_closed == 1

        with pytest.raises(OSError):
            terminal.resize(100, 30)
        # Nothing was resized: the closed handle was never used.
        assert "ResizePseudoConsole" not in _names(fake)
    finally:
        terminal.close()


@_windows_only
def test_a_resize_in_flight_keeps_the_pseudoconsole_alive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pseudoconsole is not closed while a resize is using it.

    The handle is freed only once every call that could still be using it
    has finished, so a close that arrives in the middle of a resize does
    not pull the handle out from under it.
    """
    fake = _FakeWin32()
    # The child has not exited, so the closer thread is still waiting and the
    # pseudoconsole is still alive.
    fake.block_wait.set()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        # A resize is in flight: it holds the pseudoconsole lock for the
        # duration of the syscall, and it is still running when the close
        # arrives.
        resized = threading.Event()
        finish = threading.Event()
        closed_while_resizing: list[bool] = []

        def slow_resize() -> None:
            # The resize is inside the syscall, holding the lock.
            time.sleep(0.3)
            closed_while_resizing.append(fake.pseudoconsole_closed > 0)
            resized.set()
            finish.wait(5)

        original = tw._ResizePseudoConsole

        def resize(hpcon: Any, size: Any) -> int:
            slow_resize()
            return original(hpcon, size)

        monkeypatch.setattr(tw, "_ResizePseudoConsole", resize)
        resizer = threading.Thread(target=lambda: terminal.resize(100, 30), daemon=True)
        resizer.start()
        assert resized.wait(5), "the resize must be running"

        # The closer cannot free the handle while the resize holds the lock.
        closed = threading.Thread(target=terminal._close_pseudoconsole, daemon=True)
        closed.start()
        time.sleep(0.3)
        assert closed_while_resizing == [
            False
        ], "the pseudoconsole was closed while a resize was using it"
        assert fake.pseudoconsole_closed == 0

        finish.set()
        resizer.join(5)
        closed.join(5)
        assert (
            fake.pseudoconsole_closed == 1
        ), "the pseudoconsole must be closed once the resize ended"
    finally:
        fake.wait_release.set()
        terminal.close()


@_windows_only
def test_a_resize_after_a_natural_exit_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A resize that comes after the shell exited is refused.

    The closer thread closes the pseudoconsole when the child is gone, and
    a resize that arrives after that has no pseudoconsole to resize.
    """
    fake = _FakeWin32()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        # The shell exits and the closer closes the pseudoconsole.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and fake.pseudoconsole_closed == 0:
            time.sleep(0.02)
        assert fake.pseudoconsole_closed == 1, "the pseudoconsole must be closed"

        with pytest.raises(OSError):
            terminal.resize(100, 30)
        assert "ResizePseudoConsole" not in _names(
            fake
        ), "a closed pseudoconsole must not be resized"
    finally:
        terminal.close()


@_windows_only
def test_a_poll_answers_from_the_closer_and_never_from_a_freed_handle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A poll never touches the process handle.

    The closer thread is the only one that waits for the child, and it
    caches the exit code. A poll reads that cache, so it can never meet a
    process handle that a close has already freed.
    """
    fake = _FakeWin32()
    # The child has not exited, so the exit code is still the running one.
    fake.block_wait.set()
    fake.exit_code = 259  # STILL_ACTIVE
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        assert terminal.poll() is None, "the child is still running"
        # The poll did not ask the operating system anything.
        assert "GetExitCodeProcess" not in _names(
            fake
        ), "a poll must not call GetExitCodeProcess; the closer caches it"
        # A poll after the close answers from the cached exit code.
        terminal.close()
        terminal.poll()
    finally:
        fake.wait_release.set()
        terminal.close()


@_windows_only
def test_a_failed_cancellation_keeps_what_it_owns_and_is_retryable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A close that could not finish says so, and can be tried again.

    A close that frees the handles a live thread still owns would use them
    after the fact. When the cancellation does not work, the close says so
    and keeps what it still owns, and the next call tries again rather
    than reporting a success that never happened.
    """
    fake = _FakeWin32()
    fake.block_read.set()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)
    monkeypatch.setattr(tw, "_CLOSE_TIMEOUT", 0.2)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        # The cancellation is ignored, so the reader stays inside ReadFile.
        monkeypatch.setattr(tw, "_CancelSynchronousIo", lambda handle: 0)
        with pytest.raises(OSError) as failure:
            terminal.close()
        assert "reader did not stop" in str(failure.value)
        assert (
            not terminal._fully_closed
        ), "a close that could not finish must not report itself as closed"
        # The output pipe belongs to the reader that did not stop, so it was
        # not closed under it.
        assert terminal._output_read, "the output pipe must still be owned"
    finally:
        # The reader is let go, and the cancellation works again, so the
        # retry can finish the close.
        fake.io_release.set()
        monkeypatch.setattr(tw, "_CancelSynchronousIo", fake.CancelSynchronousIo)
        terminal.close()
    assert terminal._fully_closed, "the retry must finish the close"


@_windows_only
def test_a_reader_that_did_not_stop_keeps_its_thread_handle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A live reader keeps its thread handle, so a retry can cancel it.

    The thread handle is what ``CancelSynchronousIo`` needs, and the reader
    thread owns it while it runs. A close that could not stop the reader
    must therefore keep the handle: freeing it would leave a later close
    with nothing to cancel with, and the reader would stay inside its read
    for ever.
    """
    fake = _FakeWin32()
    fake.block_read.set()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)
    monkeypatch.setattr(tw, "_CLOSE_TIMEOUT", 0.2)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    reader_handle = terminal._reader_handle
    assert reader_handle, "the reader must have a thread handle"
    try:
        # The cancellation is ignored, so the reader stays inside ReadFile.
        monkeypatch.setattr(tw, "_CancelSynchronousIo", lambda handle: 0)
        with pytest.raises(OSError):
            terminal.close()

        # The reader is alive, so its handle and its pipe are still owned.
        assert terminal._reader.is_alive(), "the reader must still be running"
        assert (
            terminal._reader_handle == reader_handle
        ), "the thread handle of a live reader must not be freed"
        assert terminal._output_read, "the output pipe must still be owned"
        assert (
            reader_handle not in fake.closed_handles
        ), "the thread handle of a live reader must not be closed"
    finally:
        fake.io_release.set()
        monkeypatch.setattr(tw, "_CancelSynchronousIo", fake.CancelSynchronousIo)
        terminal.close()
    # The retry reclaims it: the reader ended, so the handle may go.
    assert not terminal._reader.is_alive()
    assert (
        reader_handle in fake.closed_handles
    ), "the retry must release the reader's thread handle"
    assert terminal._fully_closed, "the retry must finish the close"


@_windows_only
def test_a_close_that_finished_does_not_run_twice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A close that finished is a no-op the second time."""
    fake = _FakeWin32()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    terminal.close()
    closed_once = list(fake.closed_handles)
    terminal.close()

    assert fake.closed_handles == closed_once, "a second close must close nothing"
    assert fake.pseudoconsole_closed == 1, "the pseudoconsole is closed once"


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


@_windows_only
def test_the_final_bytes_come_before_the_end_of_the_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The end of the output is only reported once every byte is out.

    The reader queues its last chunk and only then says that the output
    ended. A caller that finds the queue empty can still be one step
    behind the reader, which queues the final chunk in between. The end
    must therefore never be reported while a chunk is still queued: the
    final bytes come first, the end after them.

    The notice goes into the same queue as the bytes, so the two are one
    ordered stream and the window cannot exist.
    """
    fake = _FakeWin32()
    fake.end_of_output.set()
    fake.pause_before_end.set()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        # The reader is waiting for the end of the output, so the queue is
        # empty and nothing has ended yet.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and terminal.read() is not None:
            time.sleep(0.02)
        assert terminal.read() is None, "the output has not ended yet"

        # The reader reads the last bytes and queues them, then stops just
        # before it reports that the output ended. This is the window in
        # which the final bytes used to be stranded.
        final = b"THE-LAST-BYTES"
        fake.read_chunks.append(final)
        fake.output_release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and terminal._queue.qsize() == 0:
            time.sleep(0.02)
        assert terminal._queue.qsize() >= 1, "the final bytes were not queued"

        # The reader reports the end now.
        fake.pause_release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and terminal._ended is None:
            time.sleep(0.02)
        assert terminal._ended is not None, "the end was not published"

        # The bytes come first, the end after them.
        assert (
            terminal.read(65536) == final
        ), "the final bytes must be returned before the end of the output"
        assert terminal.read(65536) == b"", "and only then the end"
    finally:
        fake.pause_release.set()
        terminal.close()


@_windows_only
def test_the_end_is_read_before_the_queue_is_looked_at_again() -> None:
    """A producer that publishes between two looks cannot cause a false end.

    The reader queues its last chunk and only then publishes the end of the
    output. The dangerous interleaving is therefore: the caller looks, the
    queue is empty; the reader queues the chunk and publishes the end; the
    caller answers. A caller that looks at the queue twice *before* it looks
    at the end answers "ended" while the final chunk is still queued, which
    loses the last bytes of every terminal that ends while it is being read.

    The queue here produces exactly that interleaving, on a fixed call
    number, so the ordering is pinned without a thread and without timing:
    the first look reports nothing, the second publishes, and a correct
    read returns the bytes before it returns the end.
    """
    terminal = object.__new__(tw.WindowsTerminalProcess)
    terminal._remainder = b""
    terminal._end_lock = threading.Lock()
    terminal._ended = None
    terminal._queue = _QueueThatEndsDuringAnEmptyCheck(
        terminal, on_call=2, final=b"THE-LAST-BYTES"
    )

    assert terminal.read(65536) is None, "nothing has been produced yet"
    assert (
        terminal.read(65536) == b"THE-LAST-BYTES"
    ), "the last chunk is returned before the end of the output"
    assert terminal.read(65536) == b"", "and only then the end"


@_windows_only
def test_a_reader_that_could_not_start_releases_the_process_handle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A terminal that never started must not keep the child's handle.

    The closer thread is what normally releases the process handle, because
    it is the only thing that waits on the child. When the reader cannot
    start, the closer is never started either, so the failed spawn's close
    is the only owner left — and it has to release the handle rather than
    leave a child it can no longer wait for.
    """
    fake = _FakeWin32()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)
    # The reader cannot name its own thread, so it reports the failure and
    # the closer thread is never started.
    monkeypatch.setattr(tw, "_OpenThread", lambda access, inherit, thread_id: 0)

    with pytest.raises(OSError):
        tw.spawn_terminal(cwd=tmp_path)

    process_handles = [
        handle for handle, name in fake.handles.items() if name == "process"
    ]
    assert len(process_handles) == 1, "the child was created"
    process_handle = process_handles[0]
    assert (
        fake.closed_handles.count(process_handle) == 1
    ), "the process handle must be released exactly once"
    assert fake.pseudoconsole_closed == 1, "the pseudoconsole must be closed once"
    # The pipes the terminal owned went with it, once each.
    input_read, input_write = fake.pipes[0]
    output_read, output_write = fake.pipes[1]
    for handle in (input_write, output_read):
        assert (
            fake.closed_handles.count(handle) == 1
        ), f"pipe handle {handle} must be released exactly once"
    assert input_write not in (0, None) and output_read not in (0, None)


@_windows_only
def test_a_terminal_that_could_not_be_built_closes_the_transaction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A constructor that fails leaves no handle, pipe or job behind.

    The terminal is the last step of the transaction and the thing that
    takes ownership of what the transaction built. If it cannot be built,
    nothing owns those handles, so the spawn must close them itself —
    including the child it has already created and the job holding it.
    """

    class _ExplodingTerminal:
        def __init__(self, **kwargs: Any) -> None:
            raise RuntimeError("the terminal could not be built")

    fake = _FakeWin32()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)
    monkeypatch.setattr(tw, "WindowsTerminalProcess", _ExplodingTerminal)

    with pytest.raises(RuntimeError):
        tw.spawn_terminal(cwd=tmp_path)

    assert fake.pseudoconsole_closed == 1, "the pseudoconsole must be closed once"
    handles = [
        handle
        for handle, name in fake.handles.items()
        if name in {"pipe-read", "pipe-write", "process"}
    ]
    for handle in handles:
        assert (
            fake.closed_handles.count(handle) == 1
        ), f"handle {handle} must be closed exactly once, not leaked or doubled"


@_windows_only
def test_a_chunk_queued_in_the_window_is_not_stranded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A chunk that arrives after the caller's empty check is still returned.

    The caller checks the queue, finds it empty, and the reader queues its
    last chunk a moment later. That chunk must not be left behind an
    end-of-output notice, and it must not be reported as the end either.
    """
    fake = _FakeWin32()
    fake.end_of_output.set()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        # The reader is held inside its read, before it has produced
        # anything: the queue is empty and nothing has ended.
        fake.pause_before_end.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and terminal.read() is not None:
            time.sleep(0.02)
        assert terminal.read() is None, "the output has not ended yet"

        # The reader produces the last bytes and reports the end while the
        # caller is reading. The caller's read must notice the chunk.
        final = b"THE-LAST-BYTES"
        fake.read_chunks.append(final)
        fake.output_release.set()
        fake.pause_release.set()

        collected = b""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            item = terminal.read(65536)
            if item is None:
                time.sleep(0.02)
                continue
            if item == b"":
                break
            collected += item
        assert (
            collected == final
        ), f"the final bytes were stranded behind the end of the output: {collected!r}"
    finally:
        fake.pause_release.set()
        terminal.close()


@_windows_only
def test_the_end_of_the_output_is_never_reported_before_the_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The end is only reported when every byte that was produced is out.

    The reader queues its last chunk and only then says that the output
    ended. A caller that finds the queue empty and reads that notice can
    still be one step behind the reader, which queues the final chunk in
    between. The end must therefore never be reported while a chunk is
    still queued: the final bytes come first, the end after them.
    """
    fake = _FakeWin32()
    fake.end_of_output.set()
    fake.pause_before_end.set()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        # The reader is waiting for the end of the output, so the queue is
        # empty and nothing has ended yet.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and terminal.read() is not None:
            time.sleep(0.02)
        assert terminal.read() is None, "the output has not ended yet"

        # The reader reads the last bytes and queues them, then stops just
        # before it reports that the output ended. This is the window in
        # which the final bytes used to be stranded.
        final = b"THE-LAST-BYTES"
        fake.read_chunks.append(final)
        fake.output_release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and terminal._queue.qsize() == 0:
            time.sleep(0.02)
        assert terminal._queue.qsize() >= 1, "the final bytes were not queued"

        # The reader reports the end now.
        fake.pause_release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and terminal._ended is None:
            time.sleep(0.02)
        assert terminal._ended is not None, "the end must be published now"

        # The bytes come first, the end after them.
        assert (
            terminal.read(65536) == final
        ), "the final bytes must be returned before the end of the output"
        assert terminal.read(65536) == b"", "and only then the end"
    finally:
        fake.pause_release.set()
        terminal.close()


@_windows_only
def test_a_chunk_that_arrives_in_the_window_is_not_stranded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A chunk queued after the caller's empty check is still handed out.

    The caller checks the queue, finds it empty, and only then looks at the
    notice that the output ended. The reader may queue its last chunk in
    between, and that chunk must not be left behind an end-of-output
    notice. The reader takes the same lock for the chunk and for the
    notice, so the two are never seen out of order.
    """
    fake = _FakeWin32()
    fake.end_of_output.set()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        # The reader is held inside its read, before it has produced
        # anything: the queue is empty and nothing has ended.
        fake.pause_before_end.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and terminal.read() is not None:
            time.sleep(0.02)
        assert terminal.read() is None, "the output has not ended yet"

        # The caller is inside its read at the moment the reader produces
        # the last bytes and reports the end. The caller's own read is the
        # one that has to notice the chunk.
        final = b"THE-LAST-BYTES"
        fake.read_chunks.append(final)
        fake.output_release.set()
        fake.pause_release.set()

        collected = b""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            item = terminal.read(65536)
            if item is None:
                time.sleep(0.02)
                continue
            if item == b"":
                break
            collected += item
        assert (
            collected == final
        ), f"the final bytes were stranded behind the end of the output: {collected!r}"
    finally:
        fake.pause_release.set()
        terminal.close()


@_windows_only
def test_a_natural_exit_still_delivers_the_final_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The last frame of a shell that exits is not lost.

    The closer thread closes the pseudoconsole when the child is gone, and
    the console host may still have a final frame to give. The reader keeps
    draining while that happens, so the frame is delivered and the end of
    the output follows it.
    """
    fake = _FakeWin32()
    fake.read_chunks = [b"BEFORE-EXIT", b"AFTER-EXIT"]
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        collected = b""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            item = terminal.read(65536)
            if item is None:
                time.sleep(0.02)
                continue
            if item == b"":
                break
            collected += item
        assert (
            collected == b"BEFORE-EXITAFTER-EXIT"
        ), f"the output was cut short: {collected!r}"
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
    assert (
        fake.output_closed.is_set()
    ), "the output pipe was not closed before the closer thread returned"


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


def _marker(label: str) -> tuple[str, str]:
    """A marker the shell has to compute, and the text it produces.

    A literal marker in the typed command is echoed back by the terminal
    before the shell has run anything, so a test that waits for it passes
    even when the shell produced nothing. The marker here is a list of
    character codes the test computes and the shell has to turn back into
    characters, so the bytes the test waits for only appear if the shell
    really ran the command.

    Returns the code to type and the text to wait for.
    """
    codes = ",".join(f"[char]{ord(char)}" for char in label)
    coded = f"({codes})"
    produced = "CIAO" + "".join(label) + "-END"
    assert coded != produced, "the coded marker must differ from its output"
    return coded, produced


def _emit(code: str) -> bytes:
    """The PowerShell that turns the code points back into text.

    ``-join`` over an array of characters works in Windows PowerShell 5.1 as
    well as in PowerShell 7, and it needs no pipeline and no script block:
    a ``-replace`` with ``$1`` is not a per-match capture in any of them.

    The line ends with a bare carriage return, never ``\\r\\n``: PowerShell
    reads CR as Enter, while LF is "insert a newline" in the line editor.
    A stray LF therefore opens a ``>>`` continuation line, and every command
    typed afterwards becomes part of that block instead of running.
    """
    return (f"$m = 'CIAO' + (-join @({code})) + '-END'; Write-Output $m\r").encode(
        "utf-8"
    )


def _emit_unicode(codes: tuple[int, ...]) -> bytes:
    """The PowerShell that writes the given code points as one string."""
    listed = ",".join(f"[char]{code}" for code in codes)
    return (f"$m = (-join @({listed})); Write-Output $m\r").encode("utf-8")


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
    """A real pseudoconsole, a real shell, real bytes.

    The marker is computed by the shell, so the bytes the test waits for
    can only be there if the shell really ran the command. A terminal that
    echoed the command back would show the code points, not the text.
    """
    coded, expected = _marker("ONE")
    terminal = tw.spawn_terminal(cwd=tmp_path, cols=100, rows=30)
    try:
        terminal.write(_emit(coded))
        produced = _read_until(terminal, expected.encode("utf-8"))
        assert expected.encode("utf-8") in produced
    finally:
        terminal.close()


@_windows_only
def test_a_real_terminal_carries_unicode(tmp_path: Path) -> None:
    """The bytes on the wire are UTF-8, and the shell's own output survives.

    The text is built from code points the test computes, so the terminal
    cannot pass by echoing what was typed: the typed text is the code
    points, and the output is the characters.
    """
    codes = (0x00E9, 0x2013, 0x00FC, 0x2603, 0x00E0)
    text = "".join(chr(code) for code in codes)
    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        terminal.write(_emit_unicode(codes))
        produced = _read_until(terminal, text.encode("utf-8"))
        assert (
            text.encode("utf-8") in produced
        ), f"the unicode text did not survive: {produced[-400:]!r}"
    finally:
        terminal.close()


@_windows_only
def test_a_real_terminal_reports_the_window_size_after_a_resize(
    tmp_path: Path,
) -> None:
    """A resize reaches the shell, which then reports the new size."""
    ready_code, ready = _marker("GO")
    size_code, size = _marker("SIZE")
    terminal = tw.spawn_terminal(cwd=tmp_path, cols=80, rows=24)
    try:
        # Let the shell come up before the resize, so the size it reports is
        # the one the pseudoconsole was told, not the one it started with.
        terminal.write(_emit(ready_code))
        _read_until(terminal, ready.encode("utf-8"))
        terminal.resize(120, 40)
        terminal.write(
            (
                "$w = $Host.UI.RawUI.WindowSize; $m = 'CIAO' + "
                f"(-join @({size_code})) + "
                "'-END ' + $w.Width + 'x' + $w.Height; Write-Output $m\r"
            ).encode("utf-8")
        )
        produced = _read_until(terminal, size.encode("utf-8"))
        assert (
            b"120x40" in produced
        ), f"the shell did not report the new size: {produced[-400:]!r}"
    finally:
        terminal.close()


@_windows_only
def test_a_real_ctrl_c_cancels_the_pending_line_and_the_shell_lives(
    tmp_path: Path,
) -> None:
    """Ctrl+C reaches the program's console read, and the shell survives.

    The byte is typed at a line that is **not** submitted: the shell is
    waiting for the rest of it. ``0x03`` aborts that console read, so the
    pending line is dropped and the shell returns to its prompt. A shell
    that never saw the byte would keep the line, and the next command would
    be appended to it instead of running on its own.

    The dropped line is a command whose **computed** output differs from
    what was typed, so the assertion cannot be satisfied by the echo: if
    the line ran, its marker appears; the test requires that it does not.
    """
    ready_code, ready = _marker("GO")
    dropped_code, dropped = _marker("DROPPED")
    coded, expected = _marker("AFTER")
    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        terminal.write(_emit(ready_code))
        _read_until(terminal, ready.encode("utf-8"))

        # A complete command with no Enter after it: the line editor holds
        # it, waiting for more input.
        partial = _emit(dropped_code)
        assert partial.endswith(b"\r")
        terminal.write(partial[:-1])
        _read_until(terminal, b"[char]", timeout=10)

        terminal.write(b"\x03")
        # The prompt comes back when the read has been aborted.
        _read_until(terminal, b"> ", timeout=10)

        terminal.write(_emit(coded))
        produced = _read_until(terminal, expected.encode("utf-8"), timeout=10)
        assert expected.encode("utf-8") in produced
        assert (
            dropped.encode("utf-8") not in produced
        ), "the cancelled line must not run: the shell kept it in its buffer"
        assert terminal.poll() is None, "the shell must survive the interrupt"
    finally:
        terminal.close()


# ── the interrupt gap (#1721) ───────────────────────────────────────────────


def _console_state_command(label: str) -> bytes:
    """The command that reports the console's input state.

    Two values come back, in this order:

    * the marker, followed by ``[Console]::IsInputRedirected`` — whether the
      shell's *standard* input is the console's own ``CONIN$`` (False) or
      something else (True). It cannot fail, so the marker always arrives
      and the test always knows the command ran;
    * ``[Console]::TreatControlCAsInput`` — the console's
      ``ENABLE_PROCESSED_INPUT`` flag read backwards (True means the flag is
      off, and the console host then hands 0x03 to the shell as an ordinary
      character instead of raising a Ctrl+C control event for it). Reading
      it needs a console handle, so it may fail; the shell then prints an
      error and the test records the flag as unreadable.

    The marker is written as code points and turned back into text by the
    shell, so the text the test waits for cannot be the echo of the command.
    The command stays short on purpose: a shell that reads the line in the
    console's cooked mode stops at 254 characters.
    """
    codes = ",".join(f"[char]{ord(char)}" for char in label)
    command = (
        f"Write-Output ('CIAO' + (-join @({codes})) + '-END ' + "
        "[Console]::IsInputRedirected); "
        "Write-Output ([Console]::TreatControlCAsInput)"
    )
    return (command + "\r").encode("utf-8")


def _nested_console_state_command(label: str) -> bytes:
    """A child shell that reports the state and then waits.

    The child samples the console while the parent shell is busy with it,
    which is the state a foreground program runs under. The command is
    quoted for the outer shell, which passes it to the child as one
    argument, and the child's own path is absolute so the terminal's PATH
    cannot decide whether it starts at all.
    """
    codes = ",".join(f"[char]{ord(char)}" for char in label)
    inner = (
        f"Write-Output ('CIAO' + (-join @({codes})) + '-END ' + "
        "[Console]::IsInputRedirected); "
        "Write-Output ([Console]::TreatControlCAsInput); Start-Sleep 300"
    )
    shell = tw._resolve_powershell()
    assert shell, "powershell.exe is not on the machine's terminal PATH"
    # The call operator: the path has spaces in it, and the outer shell is
    # PowerShell, which needs to be told that the quoted path is a program.
    return f'& "{shell}" -NoProfile -Command "{inner}"\r'.encode("utf-8")


def _console_state(produced: bytes, marker: bytes) -> tuple[str, str]:
    """The two values the shell reported: redirected, and the input flag.

    ``redirected`` is ``True``/``False`` (the shell's standard input is not
    the console's) and ``processed`` is ``on``/``off``/``unreadable`` (the
    console's ``ENABLE_PROCESSED_INPUT``).
    """
    text = produced.split(marker, 1)[1].decode("utf-8", "replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    redirected = lines[0] if lines else ""
    processed = "unreadable"
    for line in lines[1:]:
        if line in ("True", "False"):
            # .NET reads the flag backwards: TreatControlCAsInput is True
            # when ENABLE_PROCESSED_INPUT is off.
            processed = "off" if line == "True" else "on"
            break
    return redirected, processed


def _drain(terminal: tw.WindowsTerminalProcess, seconds: float) -> bytes:
    """Take what the terminal has produced, until it goes quiet.

    The quiet is what makes this safe to use before looking for the
    prompt: a prompt left over from an earlier command would otherwise be
    mistaken for the one the interrupt brings back.
    """
    collected = b""
    deadline = time.monotonic() + seconds
    quiet = 0.0
    while time.monotonic() < deadline:
        item = terminal.read(65536)
        if item is None:
            time.sleep(0.05)
            quiet += 0.05
            if quiet >= 0.3:
                break
            continue
        if item == b"":
            break
        collected += item
        quiet = 0.0
    return collected


def _wait_for(
    terminal: tw.WindowsTerminalProcess, needle: bytes, timeout: float
) -> bytes:
    """Collect output until ``needle`` shows up; empty when it does not."""
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
    return b""


def _collect_until(
    terminal: tw.WindowsTerminalProcess, needles: tuple[bytes, ...], timeout: float
) -> bytes:
    """Collect output until one of ``needles`` shows up, or the window ends.

    Unlike :func:`_drain` this does not stop when the terminal goes quiet: a
    marker that is on its way must not be missed because the terminal had not
    produced anything *yet*.
    """
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
        if any(needle in collected for needle in needles):
            break
    return collected


@_windows_only
def test_the_console_state_behind_the_interrupt_is_measured(tmp_path: Path) -> None:
    """Measure what decides whether 0x03 interrupts a running program.

    Windows' console host turns the 0x03 byte into a Ctrl+C control event
    only when the console's input mode has ``ENABLE_PROCESSED_INPUT`` set
    (the console host's ``HandleGenericKeyEvent`` asks
    ``IsInProcessedInputMode()`` first), and the event is then delivered to
    every process attached to the console. The mode belongs to the
    *client*: the console host never writes it, the only writer is the
    client's ``SetConsoleMode``, and the client's own line editor is what
    clears the flag while it owns the prompt.

    So the two things that decide the outcome are the console's input mode
    and the shell's own standard handles. This test reports both, from
    inside the pseudoconsole, and reports what the 0x03 byte did to a
    running program. It asserts that the measurement is possible and that
    the shell survives; the numbers are the evidence #1721 needs, and they
    are reported as a warning so they land in the CI log.
    """
    label = "S"
    marker = f"CIAO{label}-END ".encode("utf-8")
    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        # The state the shell's own line editor works under.
        terminal.write(_console_state_command(label))
        produced = _read_until(terminal, marker) + _drain(terminal, 2.0)
        at_prompt = _console_state(produced, marker)
        assert at_prompt[0] in (
            "True",
            "False",
        ), f"the shell did not report its console state: {produced[-400:]!r}"

        # The state a foreground program runs under: a child shell samples
        # it and then waits, so the parent is busy with a program.
        terminal.write(_nested_console_state_command(label))
        produced = _read_until(terminal, marker, timeout=30) + _drain(terminal, 2.0)
        in_foreground = _console_state(produced, marker)
        assert in_foreground[0] in (
            "True",
            "False",
        ), f"the child shell did not report the console state: {produced[-400:]!r}"

        # The byte, and what it did to the running program. The queue is
        # emptied first: a prompt left over from an earlier command would
        # look like the one the interrupt brings back.
        _drain(terminal, 2.0)
        terminal.write(b"\x03")
        interrupted = bool(_wait_for(terminal, b"> ", timeout=20))

        report = (
            f"#1721 console state: at the prompt, standard input redirected "
            f"{at_prompt[0]}, ENABLE_PROCESSED_INPUT {at_prompt[1]}; during "
            f"a foreground program, standard input redirected "
            f"{in_foreground[0]}, ENABLE_PROCESSED_INPUT {in_foreground[1]}; "
            f"0x03 ended the running program: {interrupted}"
        )
        print(report)
        warnings.warn(report, stacklevel=1)
        assert terminal.poll() is None, "the shell must survive the interrupt"
    finally:
        terminal.close()


# ── the interrupt path itself, measured (#1721) ─────────────────────────────
#
# The console host turns the bytes on the pseudoconsole's input pipe into
# console input records. An ETX (0x03) is matched while that stream is
# decoded: the host synthesises a Ctrl+C key event and, when the console's
# input mode has ENABLE_PROCESSED_INPUT set, raises a CTRL_C_EVENT. The
# event is then delivered by the operating system to the processes of the
# console host's *process group* (Microsoft's own summary of the mechanism:
# "In order for Ctrl-C to work, thus, the pseudoconsole process must be in
# the same process group as the processes you are trying to control").
#
# Two links of that chain are invisible from the adapter, and they are what
# the probe below measures:
#
#   * does the byte reach the console as an ordinary character, or does the
#     host turn it into the control event? A reader that reads the console
#     tells the two apart: it reads the byte as a character when the host
#     left it alone, and its read is aborted when the host raised the
#     event;
#   * is the event delivered to the process? The same reader registers a
#     Console.CancelKeyPress handler, which tells that apart too: it runs
#     only when the event was raised *and* reached the process.

# The interrupt the console host asks for: the win32-input-mode key record
# for Ctrl+C, exactly what Windows Terminal sends. The host advertises
# win32-input-mode at start-up (`CSI ? 9001 h`) and decodes these records
# into real key events, so the record is the other way into the same gate a
# raw ETX takes. The record is `CSI Vk ; Sc ; Uc ; Kd ; Cs ; Rc _`: the
# virtual key 'C' (67), its scan code (30), the character 0x03, the key
# down, the left control key, and one repeat.
_WIN32_CTRL_C = b"\x1b[67;30;3;1;2;1_"


# The script the probe runs. It does three things, in this order:
#
# * it registers a ``Console.CancelKeyPress`` handler that cancels the
#   interrupt and prints a marker. The handler runs only when the host
#   raised a control event **and** delivered it to this process;
# * it prints a "ready" marker, so the test knows the handler is
#   registered and the read below is pending before the interrupt is
#   written;
# * it reads one key from the console. When the byte reached the console
#   as an ordinary character the read returns it and the reader prints its
#   code; when the host raised the interrupt the read is aborted, the read
#   throws, and the reader says so.
#
# The markers are literals, and the script is the terminal's **shell**: the
# terminal never echoes a command it was not given, so only the script's
# own output can carry them.
_PROBE_SCRIPT = (
    "[Console]::add_CancelKeyPress({ param($sender, $event) "
    "$event.Cancel = $true; [Console]::Out.WriteLine('CIAO-HANDLER'); "
    "[Console]::Out.Flush() }); "
    "[Console]::Out.WriteLine('CIAO-READY'); [Console]::Out.Flush(); "
    "try { $key = [Console]::ReadKey($true); "
    "[Console]::Out.WriteLine('CIAO-CHAR-' + [int][char]$key.KeyChar) } "
    "catch { [Console]::Out.WriteLine('CIAO-READ-ABORTED'); "
    "[Console]::Out.Flush() }"
)


def _probe_shell() -> list[str]:
    """The shell that runs the probe.

    The probe **is** the terminal's shell, not a child of it: a child would
    race the shell that started it, because the interrupt reaches the whole
    console and the shell aborts its own pipeline - and kills the child -
    while the child is still writing its marker.

    The script is handed to the shell with ``-Command`` and not with
    ``-File``: the execution policy of the machine decides whether a
    script file may run at all, and it has nothing to say about a command.
    """
    shell = tw._resolve_powershell()
    assert shell, "powershell.exe is not on the machine's terminal PATH"
    return [shell, "-NoProfile", "-Command", _PROBE_SCRIPT]


def _interrupt_seen_by_a_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupt: bytes
) -> tuple[int, int, int]:
    """What the console host did with the interrupt, seen from a reader.

    Returns ``(char_read, read_aborted, handler_ran)``, the three answers
    a reader can give:

    * ``char_read``: the reader got the byte as an ordinary character, so
      the host raised no control event at all;
    * ``read_aborted``: the host raised the event and the read it aborted
      never returned, but the event did not reach the process;
    * ``handler_ran``: the host raised the event *and* the operating
      system delivered it, which is what the ``CancelKeyPress`` handler
      says.
    """
    # The patch is scoped to this spawn: the scenarios that follow need the
    # terminal's ordinary shell back.
    with monkeypatch.context() as patch:
        patch.setattr(tw, "default_shell", _probe_shell)
        terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        # The reader is up, its handler is registered and its read is
        # pending: the interrupt has to arrive after that, or the shell
        # would read it instead.
        _read_until(terminal, b"CIAO-READY", timeout=30)

        terminal.write(interrupt)
        produced = _collect_until(
            terminal,
            (b"CIAO-HANDLER", b"CIAO-CHAR-", b"CIAO-READ-ABORTED"),
            timeout=20,
        )
        char = int(b"CIAO-CHAR-" in produced)
        aborted = int(b"CIAO-READ-ABORTED" in produced)
        handler = int(b"CIAO-HANDLER" in produced)
        return char, aborted, handler
    finally:
        terminal.close()


def _interrupt_ends_a_running_program(tmp_path: Path, interrupt: bytes) -> int:
    """Whether the interrupt ended a running program.

    ``ping`` is the program: it neither reads the console nor installs a
    handler, so the only thing that can end it is the interrupt itself. The
    shell's prompt coming back is the sign that it ended.
    """
    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        terminal.write(b"ping -n 31 127.0.0.1\r")
        _read_until(terminal, b"Reply from", timeout=30)
        # The queue is emptied first: a prompt left over from an earlier
        # command would look like the one the interrupt brings back.
        _drain(terminal, 1.0)

        terminal.write(interrupt)
        ended = bool(_wait_for(terminal, b"> ", timeout=20))
        assert terminal.poll() is None, "the shell must survive the interrupt"
        return int(ended)
    finally:
        terminal.close()


@_windows_only
def test_the_interrupt_is_measured_against_the_console_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measure the two links of the interrupt chain the adapter cannot see.

    The bytes on the pseudoconsole's input pipe are decoded by the console
    host, and the host raises the control event and the operating system
    delivers it. The adapter sees neither step, so this measures them from
    inside the pseudoconsole:

    * a reader tells whether the byte arrived as an ordinary character (the
      host left it alone) or whether the read was aborted (the host raised
      the event);
    * the same reader registers a ``Console.CancelKeyPress`` handler, which
      runs only when the event was raised *and* delivered to the process.

    A third scenario asks the question the user asks: does the interrupt
    end a running program? ``ping`` is the program, because it neither
    reads the console nor installs a handler, and a nested shell cannot
    answer it, because a shell cancels Ctrl+C itself.

    The raw ETX and the win32-input-mode key record are measured side by
    side, because the host decodes both and dispatches both through the
    same gate: if the two disagree, the encoding is the difference and the
    adapter can act on it, and if they agree, the difference is further
    down the chain and the adapter cannot.

    The numbers are reported as a warning so they land in the CI log; the
    test asserts that the measurement was possible, not what the numbers
    say.
    """
    raw_char, raw_aborted, raw_handler = _interrupt_seen_by_a_reader(
        tmp_path, monkeypatch, b"\x03"
    )
    raw_ended = _interrupt_ends_a_running_program(tmp_path, b"\x03")
    record_char, record_aborted, record_handler = _interrupt_seen_by_a_reader(
        tmp_path, monkeypatch, _WIN32_CTRL_C
    )
    record_ended = _interrupt_ends_a_running_program(tmp_path, _WIN32_CTRL_C)

    report = (
        f"#1721 interrupt probe: the raw 0x03 reached the reader as a "
        f"character: {raw_char}; the raw 0x03 aborted the reader's read: "
        f"{raw_aborted}; the raw 0x03 ran the reader's CancelKeyPress "
        f"handler: {raw_handler}; the raw 0x03 ended a running program: "
        f"{raw_ended}; the win32 key record reached the reader as a "
        f"character: {record_char}; the win32 key record aborted the "
        f"reader's read: {record_aborted}; the win32 key record ran the "
        f"reader's CancelKeyPress handler: {record_handler}; the win32 key "
        f"record ended a running program: {record_ended}"
    )
    print(report)
    warnings.warn(report, stacklevel=1)


# ── the attribute that decides the delivery (#1721) ──────────────────────────
#
# The chain that carries an interrupt into a pseudoconsole has four links,
# and the measurements above pinned three of them: the console host turns
# the 0x03 byte into a Ctrl+C key event, the host raises a CTRL_C_EVENT
# because the console's input mode has ENABLE_PROCESSED_INPUT set, and the
# operating system then decides whether that event reaches the process.
#
# That last decision is made from a per-process attribute: the "ignore
# Ctrl+C" flag. SetConsoleCtrlHandler(NULL, TRUE) sets it,
# SetConsoleCtrlHandler(NULL, FALSE) clears it, and **every child inherits
# it from its parent**. A process that has it set is skipped when the
# event is delivered: no handler runs, no read is aborted, and the process
# is not even terminated by the default handler — it simply never hears
# about the event.
#
# The engine inherits the attribute from whatever started it (a
# POSIX-flavoured shell sets it, so that its own Ctrl+C arrives as a
# signal), and a shell the engine created would inherit it from the engine.
# The probe below puts the attribute in a known state and measures what the
# operating system then does with the event, so the attribute is either the
# blocker or it is not.

# The probe that measures the attribute. It is a Python program rather than
# a PowerShell script: it needs a control handler of its own, and ctypes
# gives it one without a compiler in the way.
#
# Two states, and only the attribute differs between them:
#
# * "ignored": the handler is registered first and the attribute is set
#   afterwards, so the attribute is the last word and the event must not
#   reach the handler;
# * "cleared": the attribute is set and then cleared, and the handler is
#   registered afterwards, so the attribute is clear and the event must
#   reach it.
#
# The order is deliberate. A probe that only ever cleared the attribute
# would say nothing about what the attribute does, and a probe that
# registered its handler after setting the attribute would have the
# attribute set twice over in the "ignored" case.
_IGNORE_ATTRIBUTE_PROBE = '''"""The probe that measures the ignore-Ctrl+C attribute."""

import ctypes
import sys
import time

KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
KERNEL32.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, ctypes.c_int]
KERNEL32.SetConsoleCtrlHandler.restype = ctypes.c_int

HANDLER = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_ulong)

#: How long the probe waits for the interrupt. The test waits for the
#: marker that ends the wait, so the answer is never a timeout.
WAIT_SECONDS = 6


def _on_control(control_type):
    """The control handler: the event reached this process."""
    sys.stdout.write("CIAO-HANDLER\\n")
    sys.stdout.flush()
    return 1


def main():
    mode = sys.argv[1]
    callback = HANDLER(_on_control)
    if mode == "cleared":
        KERNEL32.SetConsoleCtrlHandler(None, True)
        KERNEL32.SetConsoleCtrlHandler(None, False)
    KERNEL32.SetConsoleCtrlHandler(callback, True)
    if mode == "ignored":
        KERNEL32.SetConsoleCtrlHandler(None, True)
    sys.stdout.write("CIAO-READY\\n")
    sys.stdout.flush()
    time.sleep(WAIT_SECONDS)
    sys.stdout.write("CIAO-DONE\\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _interrupt_with_the_ignore_attribute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> tuple[int, bool]:
    """Whether the probe's handler ran, with the attribute in that state.

    Returns ``(ran, measured)``: whether the control handler ran, and
    whether the probe answered at all — either the handler ran, or the
    probe reached the end of its wait without it. A run that never got the
    interrupt is still an answer; a run that never answered is not.
    """
    probe = tmp_path / f"ignore-attribute-{mode}.py"
    probe.write_text(_IGNORE_ATTRIBUTE_PROBE, encoding="utf-8")
    argv = [sys.executable, "-I", "-S", str(probe), mode]
    with monkeypatch.context() as patch:
        patch.setattr(tw, "default_shell", lambda: list(argv))
        terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        _read_until(terminal, b"CIAO-READY", timeout=60)
        terminal.write(b"\x03")
        produced = _collect_until(
            terminal, (b"CIAO-HANDLER", b"CIAO-DONE"), timeout=30
        )
        ran = b"CIAO-HANDLER" in produced
        return int(ran), ran or b"CIAO-DONE" in produced
    finally:
        terminal.close()


@_windows_only
def test_the_ignore_ctrl_c_attribute_decides_whether_the_event_is_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measure the attribute that decides whether the interrupt arrives.

    The console host raises the control event, and the operating system
    then decides whether it reaches the process: a process that has the
    "ignore Ctrl+C" attribute is skipped, and one that does not is not.
    The attribute is inherited by every child, so the engine's own decides
    what a shell it starts inherits.

    The probe measures the attribute directly: it puts it in a known
    state, writes the 0x03 byte, and reports whether its control handler
    ran. The two states differ in the attribute and in nothing else, so the
    difference between the two numbers is the attribute's doing.

    The numbers are reported as a warning so they land in the CI log; the
    test asserts that the measurement was possible, not what the numbers
    say.
    """
    ignored, ignored_measured = _interrupt_with_the_ignore_attribute(
        tmp_path, monkeypatch, "ignored"
    )
    cleared, cleared_measured = _interrupt_with_the_ignore_attribute(
        tmp_path, monkeypatch, "cleared"
    )
    assert ignored_measured and cleared_measured, (
        "the probe never answered: the attribute cannot be measured"
    )
    report = (
        f"#1721 ignore-attribute experiment: with the ignore-Ctrl+C attribute "
        f"set, the 0x03 ran the reader's CancelKeyPress handler: {ignored}; "
        f"with the attribute cleared, it ran: {cleared}"
    )
    print(report)
    warnings.warn(report, stacklevel=1)


@_windows_only
def test_ctrl_c_ends_a_running_program_and_the_shell_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The interrupt reaches the shell and ends the program the shell runs.

    Three things hold at once, and each of them is a different link of the
    chain:

    * the shell's own control handler runs, so the operating system
      delivered the event to the shell — the helper clears the
      ignore-Ctrl+C attribute the shell would otherwise have inherited
      from the engine;
    * the program the shell runs ends, so the event reached the program as
      well: ``ping`` neither reads the console nor installs a handler, so
      nothing but the event itself can end it;
    * the shell survives, so the event did not take the shell down with it.

    The byte is still the byte the user typed: ``0x03``, written to the
    terminal's input, unchanged.
    """
    char, aborted, handler = _interrupt_seen_by_a_reader(
        tmp_path, monkeypatch, b"\x03"
    )
    assert handler == 1, (
        "the shell must receive the console control event: the helper "
        f"clears the ignore-Ctrl+C attribute for it (read: {char}, "
        f"aborted: {aborted})"
    )
    ended = _interrupt_ends_a_running_program(tmp_path, b"\x03")
    assert ended == 1, "the interrupt must end the running program"


# ── the helper that starts the shell (#1721) ─────────────────────────────────


def test_the_helper_imports_nothing_from_the_engine() -> None:
    """The helper is a plain module: it imports nothing from ciao.

    It runs under ``-I -S`` as a process of its own, and the engine's
    packages are not on its way in: a helper that imported the engine
    would read the engine's configuration on the way to a terminal.
    """
    source = CHILD_HELPER.read_text(encoding="utf-8")
    assert "import ciao" not in source
    assert "from ciao" not in source


@_windows_only
def test_the_helper_reports_a_status_and_never_a_traceback(tmp_path: Path) -> None:
    """A helper that cannot start the shell says so with a status.

    The helper's standard handles are the user's terminal, so a traceback
    would land in it. A failure is a status, and only a status — and the
    status says which step failed.
    """
    missing = str(tmp_path / "no-such-shell.exe")
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(CHILD_HELPER), "0", missing, missing],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode in (tcw.CONTROL_FAILED, tcw.CREATE_FAILED), (
        f"the helper must fail with a status, not a crash: {result.returncode}"
    )
    assert result.stdout == b""
    assert result.stderr == b""
    assert b"Traceback" not in result.stderr


@_windows_only
def test_the_helper_refuses_an_argument_it_cannot_use() -> None:
    """A helper that cannot read its arguments says so with a status."""
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(CHILD_HELPER), "not-a-handle"],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 2, "the helper must refuse a bad argument"
    assert result.stdout == b""
    assert result.stderr == b""


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
def test_the_pseudoconsole_handle_is_inheritable_for_the_helper_and_not_after(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pseudoconsole's handle reaches the helper, and nobody else.

    The helper attaches the shell to the pseudoconsole, so it needs the
    pseudoconsole's handle — and a handle reaches a child only when it is
    inheritable. The flag is set before the child is created and taken back
    right after it, so the engine's other children never inherit the
    pseudoconsole and the handle is not left inheritable for the rest of
    the engine's life.
    """
    fake = _FakeWin32()
    _install_fake(monkeypatch, fake)
    _shell(monkeypatch)

    terminal = tw.spawn_terminal(cwd=tmp_path)
    try:
        names = _names(fake)
        flags = [
            (index, args)
            for index, (name, args) in enumerate(fake.calls)
            if name == "SetHandleInformation"
        ]
        assert len(flags) == 2, "the flag is set for the spawn and taken back"
        (set_at, set_args), (cleared_at, cleared_args) = flags
        assert set_args[1] == tw._HANDLE_FLAG_INHERIT, "the handle must be inheritable"
        assert set_args[2] == tw._HANDLE_FLAG_INHERIT
        assert cleared_args[1] == tw._HANDLE_FLAG_INHERIT
        assert cleared_args[2] == 0, "the flag must be taken back"
        created = names.index("CreateProcessW")
        assert set_at < created < cleared_at, (
            "the handle must be inheritable for the one spawn and not after it"
        )
    finally:
        terminal.close()


@_windows_only
def test_a_real_close_kills_the_background_child(tmp_path: Path) -> None:
    """Closing the terminal ends the shell and what it started."""
    marker = tmp_path / "child.pid"
    coded, expected = _marker("READY")
    terminal = tw.spawn_terminal(cwd=tmp_path)
    child_pid = 0
    try:
        terminal.write(_emit(coded))
        _read_until(terminal, expected.encode("utf-8"))
        # A plain child of the shell, so it joins the shell's job: the job
        # the adapter put the shell in.
        terminal.write(
            (
                "$p = Start-Process -PassThru powershell -ArgumentList "
                "'-NoProfile','-Command','Start-Sleep 300'; "
                f"Set-Content -Path '{marker}' -Value $p.Id\r"
            ).encode("utf-8")
        )
        deadline = time.monotonic() + 30
        child_pid = 0
        while time.monotonic() < deadline and child_pid == 0:
            # `Set-Content` can leave the file present but unreadable for a
            # moment — a file another process has open exists before its
            # content can be read — so existence is not readiness: read in
            # the loop and treat a refusal as "not written yet".
            try:
                text = marker.read_text(encoding="utf-8").strip()
            except (PermissionError, FileNotFoundError):
                time.sleep(0.1)
                continue
            child_pid = int(text) if text else 0
            if child_pid == 0:
                time.sleep(0.1)
        assert child_pid, "the background child never started"
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

    Two warm-up cycles first: the first terminal opens handles that are
    cached for the rest of the process (the console host's channels, the
    module's thread handle), and a single one does not reach that steady
    state — the second terminal caches the rest. Counting those as a leak
    would be wrong, and the measured cycles stay flat once they are in.

    The count is read once it has settled, not the instant the last close
    returns: from Windows 11 24H2 on ``ClosePseudoConsole`` returns before
    the console host has finished disconnecting, so a sample taken
    immediately afterwards still counts handles that are about to go. The
    bound itself is unchanged — a terminal that really leaks keeps the
    count high and still fails.
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

    def settled_handle_count(timeout: float = 10.0) -> int:
        """The count once two consecutive samples agree, or the deadline."""
        previous = handle_count()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.1)
            current = handle_count()
            if current == previous:
                return current
            previous = current
        return previous

    # Two warm-up cycles first: the first terminal opens handles that are
    # cached for the rest of the process (the console host's channels, the
    # module's thread handle), and a single one does not reach that steady
    # state — the second terminal caches the rest. Counting those as a leak
    # would be wrong, and the measured cycles stay flat once they are in.
    warm_coded, warm_expected = _marker("WARM")
    for _ in range(2):
        warm = tw.spawn_terminal(cwd=tmp_path)
        try:
            warm.write(_emit(warm_coded))
            _read_until(warm, warm_expected.encode("utf-8"))
        finally:
            warm.close()
        settled_handle_count()

    before = settled_handle_count()
    cycle_coded, cycle_expected = _marker("CYCLE")
    counts = []
    for _ in range(3):
        terminal = tw.spawn_terminal(cwd=tmp_path)
        try:
            terminal.write(_emit(cycle_coded))
            _read_until(terminal, cycle_expected.encode("utf-8"))
        finally:
            terminal.close()
        # Every handle the adapter opened for this terminal is released
        # before the next one starts: this is the invariant the adapter
        # owns, and it is exact.
        assert terminal._output_read == 0, "the output pipe was not released"
        assert terminal._input_write == 0, "the input pipe was not released"
        assert terminal._reader_handle is None, "the reader handle was not released"
        assert terminal._process == 0, "the process handle was not released"
        assert not terminal._hpcon, "the pseudoconsole was not released"
        assert terminal._fully_closed, "the terminal did not finish closing"
        assert (
            getattr(terminal._tree, "_job", None) is None
        ), "the job object was not released"
        # The pseudoconsole unwinds in the console host, so let it settle
        # between cycles instead of stacking three teardowns together.
        counts.append(settled_handle_count())
    after = settled_handle_count()
    assert after == counts[-1], "a settled count must be stable"

    # Three measured cycles must not grow the count relative to each other.
    # A one-time cache increase before the steady state is not a leak; a
    # terminal that keeps a handle adds its handles on every cycle, so the
    # slope is what fails here.
    growth = counts[-1] - counts[0]
    assert growth <= 2, (
        "the handle count grew across steady-state cycles: "
        f"before {before}, per cycle {counts}; a terminal leaked"
    )


def test_a_computed_marker_is_not_what_was_typed() -> None:
    """The marker the real tests wait for is not the text that was typed.

    A terminal echoes the command back before the shell has run anything,
    so a test that waits for a literal marker passes even when the shell
    produced nothing. The real tests therefore wait for text the shell has
    to compute, and this pins that the two differ.
    """
    coded, expected = _marker("ONE")
    assert coded != expected, "the coded marker must differ from its output"
    assert coded in _emit(coded).decode("utf-8"), "the typed text carries the code"
    assert expected not in _emit(coded).decode(
        "utf-8"
    ), "the text the test waits for must not be in what was typed"


def test_the_native_commands_run_in_windows_powershell_5_1() -> None:
    """The commands the real tests type work in Windows PowerShell 5.1.

    ``-replace`` with ``$1`` is not a per-match capture in PowerShell, and a
    script-block replacement is a PowerShell 6 feature that 5.1 does not
    have. A code-point array joined with ``-join`` works in both, so that is
    what the tests use. The Unicode text is the same: the code points are
    joined into one string, not quoted into the source.
    """
    coded, _ = _marker("ONE")
    command = _emit(coded).decode("utf-8")
    assert (
        "-replace" not in command
    ), "-replace with $1 is not a per-match capture in PowerShell"
    assert "$1" not in command
    assert "-join" in command, "the code points are joined into one string"
    assert "[char]" in command
    # No pipeline: the array is joined as it is, so the command cannot
    # depend on how PowerShell reads a comma-separated list.
    assert "|" not in command, "the command must not need a pipeline"

    unicode_command = _emit_unicode((0x00E9, 0x2013)).decode("utf-8")
    assert "-join" in unicode_command
    assert "|" not in unicode_command
    assert (
        "[char]0x" not in unicode_command
    ), "the code points must not be quoted into the source text"
    assert (
        "0x00E9" not in unicode_command
    ), "the typed text must be the decimal code points, not the characters"
    # The characters themselves are what the test waits for.
    assert "é" not in unicode_command


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
    # A test that hangs on a pipe must fail the job, not hold the runner.
    assert "timeout-minutes:" in step, "the focused native step needs a bound"
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
    # The job summary names the focused step's result and the tests that
    # failed in it: a red native step has to say which test was red.
    summary = windows.split("    - name: Summarize", 1)[1]
    assert (
        "windows-terminal.txt" in summary
    ), "the summary must report the focused native step's result"
    # The measurements of the interrupt are reported as warnings, and the
    # job summary has to carry them: a number in the log of a step nobody
    # reads is not a measurement anybody can use.
    assert "UserWarning: #1721" in summary, (
        "the summary must carry the #1721 measurements of the interrupt"
    )
    failing = summary.split("windows-failures.txt", 1)[0]
    assert (
        "windows-terminal.txt" in failing
    ), "the failing-test list must come from the focused native log too"
    # The names the summary prints have to be the test names, not a
    # half-stripped line: `tests/test_x.py::test_y` is what a reader needs.
    assert (
        "s#" in failing
    ), "the summary must strip the file prefix from the failing test names"
