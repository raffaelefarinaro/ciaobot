"""ciao.os_support.terminal_posix: a real controlling terminal on POSIX.

Everything here is a real pty with a real shell behind it: the tests spawn the
adapter, drive a real ``$SHELL``, and read what the terminal actually says.
The only things patched are the seams that must not reach a real process (the
session-membership check, the signal, the process listing) and the two seams
the races need (a pause inside an I/O call, the tree constructor).

The tests run on POSIX only. Windows has no equivalent adapter yet (#1692 says
so); the module still imports there, which the Windows import gate checks.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from ciao.os_support import terminal_posix

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the POSIX terminal adapter is not defined on Windows"
)

CHILD_HELPER = Path(__file__).resolve().parents[1] / "ciao" / "os_support" / "terminal_child.py"


@pytest.fixture(autouse=True)
def _posix_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the tests against ``/bin/sh``, which exists on macOS and Linux.

    The adapter runs the user's ``$SHELL``, and a line editor with its own
    idea of what a terminal is (zsh's) redraws the prompt while a test types
    into it. ``/bin/sh`` has no editor of its own, so what the test types is
    what the shell reads. ``default_shell()`` still follows ``$SHELL``, which
    its own test proves.
    """
    monkeypatch.setenv("SHELL", "/bin/sh")


def _drain(term: terminal_posix.PosixTerminalProcess, seconds: float) -> bytes:
    """Read whatever the terminal says for ``seconds``."""
    out = b""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        chunk = term.read(65536)
        if chunk is None:
            time.sleep(0.02)
            continue
        if chunk == b"":
            break
        out += chunk
    return out


def _send_and_drain(
    term: terminal_posix.PosixTerminalProcess, data: bytes, seconds: float = 2.0
) -> bytes:
    """Type ``data`` into the terminal and collect the answer."""
    term.write(data)
    return _drain(term, seconds)


def _wait_for(term: terminal_posix.PosixTerminalProcess, pattern: bytes, seconds: float) -> bytes:
    """Collect output until ``pattern`` shows up, or the deadline passes."""
    seen = b""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and pattern not in seen:
        chunk = term.read(65536)
        if chunk is None:
            time.sleep(0.02)
            continue
        if chunk == b"":
            break
        seen += chunk
    assert pattern in seen, f"{pattern!r} never showed up in {seen!r}"
    return seen


def _spawn(tmp_path: Path, **kwargs: object) -> terminal_posix.PosixTerminalProcess:
    return terminal_posix.spawn_terminal(cwd=tmp_path, **kwargs)  # type: ignore[arg-type]


def _state(pid: int) -> str:
    """The process state the kernel reports: "R", "S", "T", "Z", or ""."""
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip()


def _running(pid: int) -> bool:
    """Whether ``pid`` is a live process (not a zombie, not gone)."""
    state = _state(pid)
    if not state:
        return False
    return not state.startswith("Z")


def _wait_until_stopped(pid: int, seconds: float) -> None:
    """Wait until ``pid`` is no longer running."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _running(pid):
            return
        time.sleep(0.05)
    raise AssertionError(f"the process {pid} was still running")


def _open_fd_count() -> int:
    """How many descriptors this process holds open."""
    return len(os.listdir("/dev/fd"))


def _wait_for_file(path: Path, seconds: float) -> None:
    """Wait until the file exists and is not empty."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists() and path.read_text().strip():
            return
        time.sleep(0.05)
    raise AssertionError(f"{path} never appeared")


def test_real_controlling_terminal_and_initial_size(tmp_path: Path) -> None:
    """The shell owns a real tty, and the size the adapter asked for is the
    size the tty reports."""
    term = _spawn(tmp_path)
    try:
        assert term.pid > 0
        # The shell is the leader of its own session and process group.
        assert os.getsid(term.pid) == term.pid
        assert os.getpgid(term.pid) == term.pid
        # /dev/tty opens inside the shell, and the window is the one asked for.
        out = _send_and_drain(term, b"stty size < /dev/tty\n", 3.0)
        assert b"24 80" in out, out
        tty = _send_and_drain(term, b"tty < /dev/tty\n", 3.0)
        assert b"/dev/" in tty, tty
        assert b"not found" not in tty, tty
    finally:
        term.close()


def test_input_output_preserves_unicode_and_raw_bytes(tmp_path: Path) -> None:
    """Unicode and raw bytes survive the round trip unchanged, and a marker
    shows up exactly once.

    The marker is spelled with octal escapes, so the command the shell echoes
    back does not contain it: one occurrence means one line of output.
    """
    term = _spawn(tmp_path)
    try:
        term.write(b"printf '\\115\\101\\122\\113\\303\\251\\360\\237\\231\\202\\n'\n")
        out = _wait_for(term, "MARKé\U0001f642".encode("utf-8"), 15)
        assert out.count(b"MARK") == 1, out
        assert "MARKé\U0001f642".encode("utf-8") in out
        # A raw byte the terminal does not interpret, and no NUL handling.
        term.write(b"printf '\\122\\101\\127-\\200\\001\\n'\n")
        out = _wait_for(term, b"RAW-", 15)
        assert b"RAW-\x80\x01" in out, out
    finally:
        term.close()


def test_resize_updates_stty_size(tmp_path: Path) -> None:
    """A resize reaches the kernel, so the shell's own stty reports it."""
    term = _spawn(tmp_path, cols=80, rows=24)
    try:
        assert b"24 80" in _send_and_drain(term, b"stty size < /dev/tty\n", 3.0)
        term.resize(120, 40)
        out = _send_and_drain(term, b"stty size < /dev/tty\n", 3.0)
        assert b"40 120" in out, out
    finally:
        term.close()


@pytest.mark.parametrize(
    "ready_delay",
    [pytest.param(0, id="immediate"), pytest.param(9, id="delayed")],
)
def test_ctrl_c_interrupts_foreground_job_not_shell(
    tmp_path: Path, ready_delay: int
) -> None:
    """0x03 goes to the foreground job through the line discipline; the shell
    survives it and answers the next command.

    The answer is allowed to be late: a shell under load can take seconds to
    reach the ready command, and the marker is the observable it answers with.
    """
    term = _spawn(tmp_path)
    foreground_pid = tmp_path / f"foreground-{secrets.token_hex(6)}.pid"
    try:
        term.write(f"sh -c 'echo $$ > {foreground_pid}; exec sleep 300'\n".encode())
        _wait_for_file(foreground_pid, 15)
        pid = int(foreground_pid.read_text().strip())
        assert _running(pid)

        term.write(b"\x03")
        # The foreground job is the one that ends; the shell is not.
        _wait_until_stopped(pid, 15)
        assert term.poll() is None, "Ctrl-C must not kill the shell"

        ready = b"echo READY-$((6*7))\n"
        term.write(b"sleep 9\n" + ready if ready_delay else ready)
        out = _wait_for(term, b"READY-42", 15)
        assert b"READY-42" in out, out
    finally:
        term.close()


def test_close_kills_foreground_background_and_stopped_job_groups(tmp_path: Path) -> None:
    """Every job group the terminal owns ends with it: the running background
    job, the stopped job, and the foreground one."""
    term = _spawn(tmp_path)
    run = secrets.token_hex(6)
    background_pid = tmp_path / f"background-{run}.pid"
    stopped_pid = tmp_path / f"stopped-{run}.pid"
    background = stopped = 0
    try:
        term.write(f"sh -c 'echo $$ > {background_pid}; exec sleep 300' &\n".encode())
        term.write(f"sh -c 'echo $$ > {stopped_pid}; exec sleep 300'\n".encode())
        _wait_for_file(background_pid, 15)
        _wait_for_file(stopped_pid, 15)
        background = int(background_pid.read_text().strip())
        stopped = int(stopped_pid.read_text().strip())
        term.write(b"\x1a")  # suspend the foreground job
        time.sleep(1.0)

        # The jobs are in groups of their own, all inside the terminal's session.
        assert os.getsid(background) == term.pid
        assert os.getsid(stopped) == term.pid
        assert os.getpgid(background) == background
        assert os.getpgid(stopped) == stopped
        assert os.getpgid(background) != os.getpgid(stopped)
        assert _running(background)
        assert _running(stopped)
    finally:
        term.close()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and (_running(background) or _running(stopped)):
        time.sleep(0.1)
    assert not _running(background), "the background job survived the close"
    assert not _running(stopped), "the stopped job survived the close"


def test_close_reaps_leader_closes_fds_and_is_idempotent(tmp_path: Path) -> None:
    """The leader is reaped, the descriptors are closed, and a second close is
    a no-op."""
    before = _open_fd_count()
    term = _spawn(tmp_path)
    pid = term.pid
    master = term._master
    assert master is not None
    term.close()
    assert term.poll() is not None
    # The leader is a child of this process, so it is gone from the table.
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)
    # The pty master is closed, and no descriptor was left behind.
    with pytest.raises(OSError):
        os.fstat(master)
    assert _open_fd_count() == before
    term.close()  # idempotent
    assert term.poll() is not None
    assert _open_fd_count() == before


def test_the_master_stays_open_until_an_in_flight_read_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A call that is using the descriptor keeps it alive: the close waits for
    it, so the number cannot be closed under a running read and handed to
    something else."""
    term = _spawn(tmp_path)
    master = term._master
    assert master is not None
    inside = threading.Event()
    release = threading.Event()
    real_read = os.read

    def pausing_read(fd: int, size: int) -> bytes:
        # The pause happens while the call holds the lock, before the real
        # syscall, so the descriptor is in use the whole time.
        if fd == master:
            inside.set()
            release.wait(20)
        return real_read(fd, size)

    monkeypatch.setattr(terminal_posix.os, "read", pausing_read)

    reader = threading.Thread(target=lambda: term.read(65536))
    reader.start()
    assert inside.wait(10), "the read never reached the descriptor"

    closer = threading.Thread(target=term.close)
    closer.start()
    time.sleep(0.5)

    # The read holds the descriptor: the close cannot have taken it away.
    os.fstat(master)
    assert closer.is_alive(), "the close did not wait for the read"

    release.set()
    reader.join(20)
    closer.join(20)
    assert not reader.is_alive()
    assert not closer.is_alive()
    # Now the descriptor is gone.
    with pytest.raises(OSError):
        os.fstat(master)


def test_io_after_close_refuses(tmp_path: Path) -> None:
    """Once the terminal is closed, no call may use the descriptor again."""
    term = _spawn(tmp_path)
    term.close()
    assert term.read(65536) == b""
    with pytest.raises(OSError):
        term.write(b"x")
    with pytest.raises(OSError):
        term.resize(80, 24)


def test_a_second_close_waits_for_the_first_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second close does not report completion while the first cleanup is
    still running."""
    term = _spawn(tmp_path)
    listing = threading.Event()
    release = threading.Event()

    def slow_listing() -> list[int]:
        listing.set()
        release.wait(20)
        return []

    monkeypatch.setattr(terminal_posix, "_list_process_ids", slow_listing)

    first: list[object] = []

    def first_close() -> None:
        try:
            term.close()
            first.append("ok")
        except Exception as exc:  # noqa: BLE001 - the outcome is what matters
            first.append(exc)

    thread = threading.Thread(target=first_close)
    thread.start()
    assert listing.wait(10), "the first close never started its cleanup"

    second_done = threading.Event()

    def second_close() -> None:
        try:
            term.close()
        except Exception:  # noqa: BLE001 - the outcome is what matters
            pass
        second_done.set()

    other = threading.Thread(target=second_close)
    other.start()
    time.sleep(0.5)
    assert not second_done.is_set(), "the second close reported while the first was running"

    release.set()
    thread.join(20)
    other.join(20)
    assert second_done.is_set()
    assert first == ["ok"]


def test_a_failed_close_is_reported_again_and_never_silently_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cleanup that could not finish is a failure every time, not a success
    on the second call."""
    term = _spawn(tmp_path)
    job_pid = tmp_path / f"job-{secrets.token_hex(6)}.pid"
    term.write(f"sh -c 'echo $$ > {job_pid}; exec sleep 300' &\n".encode())
    _wait_for_file(job_pid, 15)

    def refuse(pgid: int, sig: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(terminal_posix, "_signal_group", refuse)
    monkeypatch.setattr(terminal_posix, "_group_is_only_zombies", lambda pgid: False)

    with pytest.raises(OSError):
        term.close()
    with pytest.raises(OSError):
        term.close()


def test_a_live_group_that_cannot_be_killed_is_reported_as_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EPERM on a group that still holds live members is a failure, not
    proof that the job ended. No group outside the session is signalled."""
    term = _spawn(tmp_path)
    job_pid = tmp_path / f"job-{secrets.token_hex(6)}.pid"
    term.write(f"sh -c 'echo $$ > {job_pid}; exec sleep 300' &\n".encode())
    _wait_for_file(job_pid, 15)
    job = int(job_pid.read_text().strip())
    session_groups = set(
        terminal_posix._owned_job_groups(term.pid, terminal_posix._list_process_ids())
    )
    assert os.getpgid(job) in session_groups

    signalled: list[int] = []

    def refuse(pgid: int, sig: int) -> None:
        signalled.append(pgid)
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(terminal_posix, "_signal_group", refuse)
    monkeypatch.setattr(terminal_posix, "_group_is_only_zombies", lambda pgid: False)

    with pytest.raises(OSError):
        term.close()

    # Only the terminal's own groups were even attempted.
    assert set(signalled) <= session_groups
    assert signalled, "no group was signalled at all"


def test_a_group_that_holds_only_zombies_is_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exceptional macOS case: a group that answers EPERM but holds only
    zombies is over, and the close still succeeds."""
    term = _spawn(tmp_path)
    job_pid = tmp_path / f"job-{secrets.token_hex(6)}.pid"
    term.write(f"sh -c 'echo $$ > {job_pid}; exec sleep 300' &\n".encode())
    _wait_for_file(job_pid, 15)

    def refuse(pgid: int, sig: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(terminal_posix, "_signal_group", refuse)
    monkeypatch.setattr(terminal_posix, "_group_is_only_zombies", lambda pgid: True)

    term.close()
    assert term.poll() is not None


def _fake_ps_listing(lines: list[str], returncode: int = 0) -> Any:
    """A ``subprocess.run`` stand-in that answers with a process listing."""

    def run(cmd: list[str], **kwargs: Any) -> Any:
        return subprocess.CompletedProcess(cmd, returncode, stdout="\n".join(lines))

    return run


def test_the_zombie_proof_needs_the_members_of_the_group_it_is_asked_about(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A listing that does not mention the group proves nothing about it.

    Other groups in the listing are not evidence about this one, and a group
    that is simply not there has not been observed to be a zombie either.
    """
    monkeypatch.setattr(
        terminal_posix.subprocess,
        "run",
        _fake_ps_listing(["  100 S", "  200 S", "  300 Z"]),
    )
    # The group is not in the listing at all: its absence is not proof.
    assert terminal_posix._group_is_only_zombies(400) is False
    # It is in the listing and it is a zombie.
    assert terminal_posix._group_is_only_zombies(300) is True
    # It is in the listing and it is alive.
    assert terminal_posix._group_is_only_zombies(100) is False


def test_the_zombie_proof_refuses_a_mixed_or_empty_or_broken_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One live member among the zombies is enough to refuse, and a listing
    that failed or said nothing proves nothing."""
    monkeypatch.setattr(
        terminal_posix.subprocess,
        "run",
        _fake_ps_listing(["  100 Z", "  100 S"]),
    )
    assert terminal_posix._group_is_only_zombies(100) is False

    monkeypatch.setattr(terminal_posix.subprocess, "run", _fake_ps_listing([]))
    assert terminal_posix._group_is_only_zombies(100) is False

    monkeypatch.setattr(
        terminal_posix.subprocess, "run", _fake_ps_listing(["  100 Z"], returncode=1)
    )
    assert terminal_posix._group_is_only_zombies(100) is False

    monkeypatch.setattr(
        terminal_posix.subprocess, "run", _fake_ps_listing(["not a listing at all"])
    )
    assert terminal_posix._group_is_only_zombies(100) is False


def test_a_shell_that_cannot_be_reaped_is_reported_as_a_failure() -> None:
    """A shell that stays unreaped is reported, not claimed to have ended."""

    class Unreapable:
        """A child that never ends, not even after the kill."""

        def wait(self, timeout: float | None = None) -> int:
            raise subprocess.TimeoutExpired("sh", timeout)

        def kill(self) -> None:
            return None

    assert terminal_posix._reap(Unreapable()) == ["the shell could not be reaped"]  # type: ignore[arg-type]


def test_a_close_reports_a_shell_that_could_not_be_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The close says so when the shell could not be reaped, and says it
    again the next time."""
    term = _spawn(tmp_path)

    def failing_reap(process: subprocess.Popen[bytes]) -> list[str]:
        try:
            process.kill()
            process.wait(timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return ["the shell could not be reaped"]

    monkeypatch.setattr(terminal_posix, "_reap", failing_reap)

    with pytest.raises(OSError):
        term.close()
    with pytest.raises(OSError):
        term.close()


def test_close_reports_a_failed_enumeration_but_still_ends_the_leader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the processes cannot be listed, the leader's own group is still
    ended, the resources are released, and the failure is not swallowed."""
    term = _spawn(tmp_path)
    pid = term.pid

    def failing_listing() -> list[int]:
        raise OSError("listing the processes failed")

    monkeypatch.setattr(terminal_posix, "_list_process_ids", failing_listing)
    with pytest.raises(OSError):
        term.close()
    # The leader is gone even though the listing failed.
    assert term.poll() is not None
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


def test_a_failed_spawn_ends_the_jobs_the_child_already_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child that already exec'd the shell and started a job of its own is
    undone by the same verified-session cleanup a normal close uses.

    The job ignores SIGHUP, so closing the pty does not end it: only the
    cleanup that reaches the whole session can.
    """
    before = _open_fd_count()
    # pytest reuses its temporary directories, so the files the child writes
    # are named per run: a stale pid from an earlier run would be read here.
    run = secrets.token_hex(6)
    job_pid = tmp_path / f"job-{run}.pid"
    runner = tmp_path / f"runner-{run}.sh"
    runner.write_text(
        "#!/bin/sh\n"
        "trap '' HUP\n"
        f"echo $$ > {job_pid}\n"
        "exec sleep 300\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)
    seen: dict[str, int] = {}

    def failing_step(fd: int) -> None:
        # The shell is on the pty already; make it start a job of its own,
        # then let the setup fail.
        os.write(fd, f"{runner} &\n".encode())
        _wait_for_file(job_pid, 20)
        seen["job"] = int(job_pid.read_text().strip())
        seen["job_session"] = os.getsid(seen["job"])
        seen["job_group"] = os.getpgid(seen["job"])
        raise OSError("the terminal could not be finished")

    monkeypatch.setattr(terminal_posix, "_set_nonblocking", failing_step)
    with pytest.raises(OSError):
        terminal_posix.spawn_terminal(cwd=tmp_path)

    job = seen["job"]
    # The job lives in the terminal's session, but in a group of its own: the
    # leader's group alone would not have reached it.
    assert seen["job_session"] != os.getsid(os.getpid()), "the job must be in the terminal's session"
    assert seen["job_group"] != seen["job_session"], "the job must be in a group of its own"

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and _running(job):
        time.sleep(0.1)
    assert not _running(job), "the job the shell started survived the failed spawn"
    assert _open_fd_count() == before


def test_a_failed_spawn_with_a_broken_tree_constructor_still_ends_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tree is built inside the spawn transaction: when that fails, the
    child and its session still go, and every descriptor is released."""
    before = _open_fd_count()
    run = secrets.token_hex(6)
    job_pid = tmp_path / f"job-{run}.pid"
    runner = tmp_path / f"runner-{run}.sh"
    runner.write_text(
        "#!/bin/sh\n"
        "trap '' HUP\n"
        f"echo $$ > {job_pid}\n"
        "exec sleep 300\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)
    seen: dict[str, int] = {}
    master: list[int] = []

    class FailingTree:
        """A tree that cannot be built, so the spawn has to undo the child."""

        def __init__(self, pid: int, **kwargs: object) -> None:
            # The child is already on the pty: make it start a job of its
            # own before the transaction gives up. The job ignores SIGHUP, so
            # closing the pty cannot be what ends it.
            os.write(master[0], f"{runner} &\n".encode())
            _wait_for_file(job_pid, 20)
            seen["job"] = int(job_pid.read_text().strip())
            raise OSError("the process tree could not be created")

    real_set_nonblocking = terminal_posix._set_nonblocking

    def capture_master(fd: int) -> None:
        master.append(fd)
        real_set_nonblocking(fd)

    monkeypatch.setattr(terminal_posix, "_set_nonblocking", capture_master)
    monkeypatch.setattr(terminal_posix, "ProcessTree", FailingTree)
    with pytest.raises(OSError):
        terminal_posix.spawn_terminal(cwd=tmp_path)

    job = seen["job"]
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and _running(job):
        time.sleep(0.1)
    assert not _running(job), "the job survived the failed spawn"
    assert _open_fd_count() == before


def test_a_failed_spawn_with_a_broken_terminal_constructor_still_ends_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The terminal object is built inside the spawn transaction too: when
    that fails, the child and its session still go, and every descriptor is
    released."""
    before = _open_fd_count()
    run = secrets.token_hex(6)
    job_pid = tmp_path / f"job-{run}.pid"
    runner = tmp_path / f"runner-{run}.sh"
    runner.write_text(
        "#!/bin/sh\n"
        "trap '' HUP\n"
        f"echo $$ > {job_pid}\n"
        "exec sleep 300\n",
        encoding="utf-8",
    )
    runner.chmod(0o755)
    seen: dict[str, int] = {}
    master: list[int] = []

    class FailingTerminal:
        """A terminal that cannot be built, so the spawn has to undo the child."""

        def __init__(self, **kwargs: object) -> None:
            # The child is already on the pty: make it start a job of its
            # own before the transaction gives up. The job ignores SIGHUP, so
            # closing the pty cannot be what ends it.
            os.write(master[0], f"{runner} &\n".encode())
            _wait_for_file(job_pid, 20)
            seen["job"] = int(job_pid.read_text().strip())
            raise OSError("the terminal could not be built")

    real_set_nonblocking = terminal_posix._set_nonblocking

    def capture_master(fd: int) -> None:
        master.append(fd)
        real_set_nonblocking(fd)

    monkeypatch.setattr(terminal_posix, "_set_nonblocking", capture_master)
    monkeypatch.setattr(terminal_posix, "PosixTerminalProcess", FailingTerminal)
    with pytest.raises(OSError):
        terminal_posix.spawn_terminal(cwd=tmp_path)

    job = seen["job"]
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and _running(job):
        time.sleep(0.1)
    assert not _running(job), "the job survived the failed spawn"
    assert _open_fd_count() == before


def test_spawn_failure_releases_all_allocated_fds_and_started_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure after the child exists leaves no descriptor behind and no
    child running."""
    before = _open_fd_count()

    def failing_step(fd: int) -> None:
        raise OSError("the terminal could not be finished")

    # The step that runs once the child is started and the pty is open.
    monkeypatch.setattr(terminal_posix, "_set_nonblocking", failing_step)
    with pytest.raises(OSError):
        terminal_posix.spawn_terminal(cwd=tmp_path)
    assert _open_fd_count() == before
    # No shell was left behind: the one that started is gone and reaped.
    assert _running_children() == []


def _running_children() -> list[str]:
    """The commands of the processes this test's own children are running."""
    result = subprocess.run(
        ["/bin/ps", "-o", "ppid=,command="],
        capture_output=True,
        text=True,
        timeout=10,
    )
    mine = str(os.getpid())
    out = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0] == mine and "terminal_child" in parts[1]:
            out.append(parts[1])
    return out


def test_missing_folder_invalid_shell_and_bool_or_out_of_range_sizes_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bad input is refused before anything is allocated."""
    before = _open_fd_count()
    not_a_folder = tmp_path / "not-a-folder"
    not_a_folder.write_text("x", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        terminal_posix.spawn_terminal(cwd=tmp_path / "nope")
    with pytest.raises(NotADirectoryError):
        terminal_posix.spawn_terminal(cwd=not_a_folder)
    with pytest.raises(ValueError):
        terminal_posix.spawn_terminal(cwd=tmp_path, cols=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        terminal_posix.spawn_terminal(cwd=tmp_path, rows=False)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        terminal_posix.spawn_terminal(cwd=tmp_path, cols=19)
    with pytest.raises(ValueError):
        terminal_posix.spawn_terminal(cwd=tmp_path, cols=501)
    with pytest.raises(ValueError):
        terminal_posix.spawn_terminal(cwd=tmp_path, rows=4)
    with pytest.raises(ValueError):
        terminal_posix.spawn_terminal(cwd=tmp_path, rows=201)
    assert _open_fd_count() == before

    # A $SHELL that is not an executable program is refused too.
    monkeypatch.setenv("SHELL", "/bin/does-not-exist")
    with pytest.raises(OSError):
        terminal_posix.spawn_terminal(cwd=tmp_path)
    monkeypatch.setenv("SHELL", "sh")
    with pytest.raises(OSError):
        terminal_posix.spawn_terminal(cwd=tmp_path)
    assert _open_fd_count() == before


def test_default_shell_follows_the_users_shell_and_falls_back_to_bin_sh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user's shell is the one that runs; ``/bin/sh`` is the default."""
    monkeypatch.setenv("SHELL", "/bin/dash")
    assert terminal_posix.default_shell() == ["/bin/dash", "-l", "-i"]
    monkeypatch.delenv("SHELL", raising=False)
    assert terminal_posix.default_shell() == ["/bin/sh", "-l", "-i"]


def test_environment_reuses_secret_filter_and_adds_term_without_stale_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The environment is the canonical one (secrets out), with a terminal
    type and no inherited size."""
    monkeypatch.setenv("SOME_API_KEY", "secret-value")
    monkeypatch.setenv("COLUMNS", "123")
    monkeypatch.setenv("LINES", "45")
    env = terminal_posix.terminal_environment()
    assert env["TERM"] == "xterm-256color"
    assert "COLUMNS" not in env
    assert "LINES" not in env
    assert "SOME_API_KEY" not in env
    assert env["PATH"], "the shell needs a PATH"


def test_read_none_vs_eof_and_partial_nonblocking_write_semantics(tmp_path: Path) -> None:
    """An idle terminal has no data (None), a finished one is EOF (b''), and a
    write says how much it took."""
    term = _spawn(tmp_path)
    try:
        # The shell is quiet after its prompt: there is nothing to read.
        _drain(term, 1.5)
        assert term.read(65536) is None
        term.write(b"echo MARK\n")
        assert b"MARK" in _wait_for(term, b"MARK", 15)
        # A write of a bounded chunk is accepted; the size it reports is the
        # number of bytes it took.
        written = term.write(b"x" * 10)
        assert 0 <= written <= 10
        # A non-positive read size is a programming error, not empty output.
        with pytest.raises(ValueError):
            term.read(0)
        with pytest.raises(ValueError):
            term.read(-1)
        # A frame larger than the bound is refused rather than buffered.
        with pytest.raises(ValueError):
            term.write(b"x" * (65536 + 1))
    finally:
        term.close()
    # The shell is gone: the master reports EOF.
    assert term.read(65536) == b""


def test_cleanup_refuses_stale_or_foreign_session_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process that is not in the terminal's session is never signalled, and
    a group whose representative left the session is left alone."""
    signalled: list[tuple[int, int]] = []
    asked: list[int] = []

    def fake_signal_group(pgid: int, sig: int) -> None:
        signalled.append((pgid, sig))

    def fake_session_of(pid: int) -> int:
        asked.append(pid)
        if pid == 100:
            return 4242
        if pid == 101:
            # The first time it is asked, pid 101 is in the session; the second
            # time it has left it, so the re-check must refuse the group.
            return 4242 if asked.count(101) == 1 else 1
        return 1

    def fake_group_of(pid: int) -> int:
        return 100 if pid == 100 else 200

    monkeypatch.setattr(terminal_posix, "_signal_group", fake_signal_group)
    monkeypatch.setattr(terminal_posix, "_session_of", fake_session_of)
    monkeypatch.setattr(terminal_posix, "_group_of", fake_group_of)

    groups = terminal_posix._owned_job_groups(session_id=4242, candidates=[100, 101, 300, 301])
    assert groups == {100: 100, 200: 101}

    signalled.clear()
    terminal_posix._kill_verified_groups(session_id=4242, groups=groups)
    # Group 200's representative left the session between the listing and the
    # signal, so only the group that still proves it is signalled.
    assert signalled == [(100, 9)]


def test_helper_is_inert_on_import_and_uses_no_fork_or_preexec() -> None:
    """The helper is a plain module: importing it does nothing, and it holds
    no fork or preexec machinery."""
    source = CHILD_HELPER.read_text(encoding="utf-8")
    assert "pty.fork" not in source
    assert "preexec_fn" not in source
    assert "setsid(" not in source
    assert "import ciao" not in source

    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", f"import runpy; runpy.run_path({str(CHILD_HELPER)!r})"],
        capture_output=True,
        timeout=30,
        env={"PATH": os.environ.get("PATH", "")},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == b""
    assert result.stderr == b""


def test_helper_writes_no_traceback_when_the_terminal_cannot_be_claimed() -> None:
    """The helper fails with a status, not with a traceback in the user's
    terminal."""
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(CHILD_HELPER), "/bin/sh"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == terminal_posix.HELPER_CLAIM_FAILED
    assert result.stderr == b""
    assert b"Traceback" not in result.stderr


def test_helper_writes_no_traceback_when_the_program_cannot_be_run() -> None:
    """The same when the program itself cannot be started: the status says so
    and nothing about the program is written."""
    master, slave = os.openpty()
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-S", str(CHILD_HELPER), "/bin/does-not-exist"],
            stdin=slave,
            capture_output=True,
            timeout=30,
            start_new_session=True,
        )
    finally:
        os.close(master)
        os.close(slave)
    assert result.returncode == terminal_posix.HELPER_EXEC_FAILED
    assert result.stderr == b""
    assert b"Traceback" not in result.stderr
