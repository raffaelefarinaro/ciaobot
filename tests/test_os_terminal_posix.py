"""ciao.os_support.terminal_posix: a real controlling terminal on POSIX.

Everything here is a real pty with a real shell behind it: the tests spawn the
adapter, drive a real ``$SHELL``, and read what the terminal actually says.
Nothing is mocked except the one seam that must not signal a foreign process
(the session-membership check), which is patched to answer with a fixed table
and never reaches a real ``kill``.

The tests run on POSIX only. Windows has no equivalent adapter yet (#1692 says
so); the module still imports there, which the Windows import gate checks.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

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


def test_ctrl_c_interrupts_foreground_job_not_shell(tmp_path: Path) -> None:
    """0x03 goes to the foreground job through the line discipline; the shell
    survives it and answers the next command."""
    term = _spawn(tmp_path)
    foreground_pid = tmp_path / "foreground.pid"
    try:
        term.write(f"sh -c 'echo $$ > {foreground_pid}; exec sleep 300'\n".encode())
        deadline = time.monotonic() + 15
        while not foreground_pid.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert foreground_pid.exists(), "the foreground job never started"
        pid = int(foreground_pid.read_text().strip())
        assert _running(pid)

        term.write(b"\x03")
        # The foreground job is the one that ends; the shell is not.
        _wait_until_stopped(pid, 15)
        assert term.poll() is None, "Ctrl-C must not kill the shell"

        out = _send_and_drain(term, b"echo READY-$((6*7))\n", 8.0)
        assert b"READY-42" in out, out
    finally:
        term.close()


def test_close_kills_foreground_background_and_stopped_job_groups(tmp_path: Path) -> None:
    """Every job group the terminal owns ends with it: the running background
    job, the stopped job, and the foreground one."""
    term = _spawn(tmp_path)
    background_pid = tmp_path / "background.pid"
    stopped_pid = tmp_path / "stopped.pid"
    background = stopped = 0
    try:
        term.write(f"sh -c 'echo $$ > {background_pid}; exec sleep 300' &\n".encode())
        term.write(f"sh -c 'echo $$ > {stopped_pid}; exec sleep 300'\n".encode())
        deadline = time.monotonic() + 15
        while (
            not background_pid.exists() or not stopped_pid.exists()
        ) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert background_pid.exists() and stopped_pid.exists(), "the jobs never started"
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
