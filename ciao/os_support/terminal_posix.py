"""A real POSIX terminal: a pty, the shell that owns it, and a bounded end.

This is the internal adapter behind a future browser terminal. It owns one
pty, one shell and the process groups that shell starts, and it exposes them
as bytes: ``read`` never blocks, ``write`` says how much it took, ``resize``
reaches the kernel's window size, and ``close`` ends everything the terminal
owns. Nothing here is exposed over HTTP yet (#1692).

How the pieces fit:

- The engine is threaded, so it cannot ``fork`` and hand a child a
  controlling terminal the way a terminal emulator does. Instead the engine
  opens a pty, spawns a *fresh* helper interpreter with
  ``start_new_session=True`` (so it is a session leader with no terminal yet),
  and the helper claims the pty slave with ``TIOCSCTTY`` and then ``exec``s
  the shell. The helper is not a wrapper around the shell: it *becomes* the
  shell, so the pid, the session and the process group stay the ones the
  engine created, and the shell's own job groups are the ones the engine
  later cleans up.
- The shell is interactive, so job control is on and every job it starts gets
  its own process group. That is why ``close`` cannot signal the leader's
  group alone: a background job and a stopped job live in groups of their
  own. Every candidate is authorized by ``os.getsid(candidate) == pid`` and
  re-checked immediately before the signal, so a process that left the
  session (or a pid the system reused) is never killed. The engine's own
  group is not in that session and is never signalled.
- The environment is the canonical one the native terminal already uses
  (``host_terminal._terminal_env``: the engine's environment minus secrets),
  with ``TERM`` added and the inherited ``COLUMNS``/``LINES`` removed so a
  stale size cannot override the window. There is no second secret filter
  here, and no fallback for one: the same list is reused.

Everything that can be refused is refused before a descriptor or a child
exists: a folder that is not there (:class:`FileNotFoundError`), a size that
is not an exact int in range (:class:`ValueError`), a ``$SHELL`` that is not
an executable program (:class:`OSError`).

The module is safe to import on Windows, where none of this exists: the POSIX
implementation lives under ``sys.platform != "win32"`` and there is no fake
class or stub for the other platform. Windows gets its own real adapter
(a separate issue).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

if sys.platform != "win32":
    import errno
    import termios

    from ciao.os_support.processes import ProcessTree

    MIN_COLS, MAX_COLS = 20, 500
    MIN_ROWS, MAX_ROWS = 5, 200
    MAX_READ = 65536
    MAX_WRITE = 65536
    #: The process lister, by absolute path: a PATH lookup here would let the
    #: environment point the cleanup at something else.
    PS_PATH = Path("/bin/ps")
    PS_TIMEOUT = 2.0
    REAP_TIMEOUT = 5.0
    CHILD_HELPER = Path(__file__).resolve().with_name("terminal_child.py")

    def _check_size(cols: int, rows: int) -> None:
        """Refuse a size that is not an exact int in range."""
        for name, value, low, high in (
            ("cols", cols, MIN_COLS, MAX_COLS),
            ("rows", rows, MIN_ROWS, MAX_ROWS),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an int, not {type(value).__name__}")
            if not low <= value <= high:
                raise ValueError(f"{name} must be between {low} and {high}")

    def _check_folder(cwd: Path) -> Path:
        """Refuse a working directory that is not a folder there is."""
        folder = Path(cwd)
        if not folder.exists():
            raise FileNotFoundError(f"no such folder: {folder}")
        if not folder.is_dir():
            raise NotADirectoryError(f"not a folder: {folder}")
        return folder

    def default_shell() -> list[str]:
        """The shell a terminal runs: the user's ``$SHELL``, else ``/bin/sh``.

        A ``$SHELL`` that is not an absolute executable program is refused:
        running something else in its place would run a program the user did
        not choose.
        """
        raw = os.environ.get("SHELL", "").strip() or "/bin/sh"
        if not os.path.isabs(raw) or not os.path.isfile(raw) or not os.access(raw, os.X_OK):
            raise OSError(f"the shell is not an executable program: {raw}")
        return [raw, "-l", "-i"]

    def terminal_environment() -> dict[str, str]:
        """The environment a terminal session gets: the engine's, minus secrets."""
        from ciao.host_terminal import _terminal_env

        env = _terminal_env()
        env["TERM"] = "xterm-256color"
        # A size inherited from the engine's own terminal would contradict the
        # window the adapter was asked for.
        env.pop("COLUMNS", None)
        env.pop("LINES", None)
        return env

    def _session_of(pid: int) -> int:
        """The session a pid belongs to."""
        return os.getsid(pid)

    def _group_of(pid: int) -> int:
        """The process group a pid belongs to."""
        return os.getpgid(pid)

    def _signal_group(pgid: int, sig: int) -> None:
        """Signal a whole process group."""
        os.killpg(pgid, sig)

    def _list_process_ids() -> list[int]:
        """Every pid on the machine, as integers."""
        try:
            result = subprocess.run(
                [str(PS_PATH), "-axo", "pid="],
                capture_output=True,
                timeout=PS_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise OSError("listing the processes timed out") from exc
        if result.returncode != 0:
            raise OSError("listing the processes failed")
        pids: list[int] = []
        for line in result.stdout.splitlines():
            text = line.strip()
            if text.isdigit():
                pids.append(int(text))
        return pids

    def _owned_job_groups(session_id: int, candidates: Iterable[int]) -> dict[int, int]:
        """The process groups inside ``session_id``, each with a representative.

        A candidate belongs to the session when the kernel says so. The
        representative is the pid the group was seen through, and it is what
        is checked again before anything is signalled.
        """
        groups: dict[int, int] = {}
        for pid in candidates:
            try:
                if _session_of(pid) != session_id:
                    continue
                pgid = _group_of(pid)
            except (ProcessLookupError, PermissionError):
                continue
            groups.setdefault(pgid, pid)
        return groups

    def _kill_verified_groups(session_id: int, groups: Mapping[int, int]) -> None:
        """SIGKILL each group whose representative still proves the session owns it.

        The check is repeated immediately before the signal: a process that
        left the session between the listing and the signal, or a pid the
        system reused, is not ours to kill.
        """
        for pgid, representative in groups.items():
            try:
                if _session_of(representative) != session_id:
                    continue
                if _group_of(representative) != pgid:
                    continue
            except (ProcessLookupError, PermissionError):
                continue
            try:
                _signal_group(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                # The group is already gone, or holds only a zombie (macOS
                # answers EPERM for that): the job ended either way.
                continue

    def _close_quietly(fd: int) -> None:
        """Close a descriptor, ignoring one that is already closed."""
        try:
            os.close(fd)
        except OSError:
            pass

    def _kill_child(process: subprocess.Popen[bytes]) -> None:
        """End a child that must not live, and reap it."""
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=REAP_TIMEOUT)
        except subprocess.TimeoutExpired:
            pass

    def _spawn_process(argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        """Start the helper that becomes the shell.

        A named step rather than a bare ``subprocess.Popen`` call, so the
        failure path that runs after a child exists can be exercised with a
        real child.
        """
        return subprocess.Popen(argv, **kwargs)

    def _set_nonblocking(fd: int) -> None:
        """The master must never block a thread of the engine."""
        os.set_blocking(fd, False)

    class PosixTerminalProcess:
        """A shell on its own pty, and the job groups it owns."""

        def __init__(
            self,
            *,
            pid: int,
            master: int,
            process: subprocess.Popen[bytes],
            tree: ProcessTree,
        ) -> None:
            self.pid = pid
            self._master: int | None = master
            self._process = process
            self._tree = tree
            self._closed = False

        def read(self, size: int = MAX_READ) -> bytes | None:
            """What the terminal has to say: bytes, nothing yet, or EOF.

            ``None`` means the terminal is quiet for now (the reader should
            come back), ``b""`` means the shell is gone and no more will come.
            """
            if size <= 0:
                raise ValueError("size must be positive")
            master = self._master
            if master is None:
                return b""
            try:
                return os.read(master, min(size, MAX_READ))
            except BlockingIOError:
                return None
            except OSError as exc:
                if exc.errno == errno.EIO:
                    # The last slave closed: the terminal is over.
                    return b""
                raise

        def write(self, data: bytes) -> int:
            """Type ``data`` into the terminal; returns how many bytes it took.

            A terminal that cannot take the bytes right now takes none of
            them: the caller retries, and nothing is buffered here.
            """
            if len(data) > MAX_WRITE:
                raise ValueError(f"at most {MAX_WRITE} bytes per write")
            master = self._master
            if master is None:
                raise OSError("the terminal is closed")
            try:
                return os.write(master, data)
            except BlockingIOError:
                return 0

        def resize(self, cols: int, rows: int) -> None:
            """Tell the kernel the new window size, so the shell can redraw."""
            _check_size(cols, rows)
            master = self._master
            if master is None:
                raise OSError("the terminal is closed")
            termios.tcsetwinsize(master, (rows, cols))

        def poll(self) -> int | None:
            """The shell's exit code, or ``None`` while it runs."""
            return self._process.poll()

        def close(self) -> None:
            """End every job group this terminal owns, reap the shell, release the pty.

            Idempotent. The shell's own group and the groups of the jobs it
            started are all ended, after the kernel has confirmed that each
            of them still belongs to this terminal's session.
            """
            if self._closed:
                return
            self._closed = True
            failure: OSError | None = None
            try:
                self._kill_owned_groups()
            except OSError as exc:
                failure = exc
            self._release()
            if failure is not None:
                raise failure

        def _kill_owned_groups(self) -> None:
            session_id = self.pid
            try:
                candidates = _list_process_ids()
            except OSError as exc:
                # Without the listing only the leader's own group can still be
                # proven, so it goes first and the failure is not swallowed.
                if self._leader_group_is_ours():
                    self._kill_leader_group()
                raise OSError("the terminal's processes could not be listed") from exc
            groups = _owned_job_groups(session_id, candidates)
            _kill_verified_groups(session_id, groups)
            if self._leader_group_is_ours():
                # The leader is still ours (not reaped, not reused), so its own
                # group can be ended through the tree that tracks it.
                self._kill_leader_group()

        def _leader_group_is_ours(self) -> bool:
            """Whether the leader's pid still proves it leads this session."""
            try:
                return _session_of(self.pid) == self.pid
            except (ProcessLookupError, PermissionError):
                return False

        def _kill_leader_group(self) -> None:
            try:
                self._tree.kill()
            except OSError:
                pass

        def _release(self) -> None:
            """Close the pty, reap the shell, and let go of the tree."""
            master, self._master = self._master, None
            if master is not None:
                _close_quietly(master)
            try:
                self._process.wait(timeout=REAP_TIMEOUT)
            except subprocess.TimeoutExpired:
                try:
                    self._process.kill()
                except OSError:
                    pass
                try:
                    self._process.wait(timeout=REAP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    pass
            self._tree.close()

    def spawn_terminal(*, cwd: Path, cols: int = 80, rows: int = 24) -> PosixTerminalProcess:
        """Start the user's shell on a fresh pty, in ``cwd``.

        The shell becomes the leader of its own session, owns the pty as its
        controlling terminal, and can start jobs of its own. Everything it
        starts is ended by :meth:`PosixTerminalProcess.close`.
        """
        _check_size(cols, rows)
        folder = _check_folder(cwd)
        shell_argv = default_shell()
        if not PS_PATH.is_file() or not os.access(PS_PATH, os.X_OK):
            raise OSError(f"{PS_PATH} is missing")
        env = terminal_environment()
        master, slave = os.openpty()
        process: subprocess.Popen[bytes] | None = None
        try:
            termios.tcsetwinsize(slave, (rows, cols))
            process = _spawn_process(
                [sys.executable, "-I", "-S", str(CHILD_HELPER), *shell_argv],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=str(folder),
                env=env,
                start_new_session=True,
                close_fds=True,
            )
            _close_quietly(slave)
            slave = -1
            _set_nonblocking(master)
        except BaseException:
            # Nothing may outlive a failed spawn: no descriptor, no child.
            if slave != -1:
                _close_quietly(slave)
            if process is not None:
                _kill_child(process)
            _close_quietly(master)
            raise
        return PosixTerminalProcess(
            pid=process.pid,
            master=master,
            process=process,
            tree=ProcessTree(process.pid),
        )
