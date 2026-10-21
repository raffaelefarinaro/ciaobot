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
  re-checked immediately before the signal, which keeps a process that left
  the session from being signalled. The check and the signal are not atomic,
  so a pid the system reused can still slip through in between: the window
  is narrow, not closed. The engine's own group is not in that session and
  is never signalled.
- The environment is the canonical one the native terminal already uses
  (``host_terminal._terminal_env``: the engine's environment minus secrets),
  with ``TERM`` added and the inherited ``COLUMNS``/``LINES`` removed so a
  stale size cannot override the window. There is no second secret filter
  here, and no fallback for one: the same list is reused.

The descriptor has a life of its own: the I/O calls and the close share one
lock, so a descriptor can never be closed while a call is using it and its
number can never be handed to something else in between. The close itself is
serialized, so a second caller waits for the first one instead of reporting
a completion that has not happened yet.

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
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

if sys.platform != "win32":
    import errno
    import termios

    from ciao.os_support.processes import ProcessTree
    from ciao.os_support.terminal_child import CLAIM_FAILED as HELPER_CLAIM_FAILED
    from ciao.os_support.terminal_child import EXEC_FAILED as HELPER_EXEC_FAILED

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

    def _group_is_only_zombies(pgid: int) -> bool:
        """Whether the group holds nothing but zombies, so it is already over.

        The proof that an EPERM meant "the group is already dead" rather than
        "the group is alive and we may not kill it". It is about *this* group
        and nothing else: the members of ``pgid`` have to be in the listing,
        and every one of them has to be a zombie. A group that is not in the
        listing has not been observed to be a zombie, and a listing that
        failed or said nothing proves nothing; the caller reports a failure
        in both cases.
        """
        try:
            result = subprocess.run(
                [str(PS_PATH), "-axo", "pgid=,stat="],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=PS_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode != 0:
            return False
        states: list[str] = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) != 2 or not parts[0].isdigit():
                continue
            if int(parts[0]) == pgid:
                states.append(parts[1])
        if not states:
            return False
        return all(state.startswith("Z") for state in states)

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

    def _kill_verified_groups(session_id: int, groups: Mapping[int, int]) -> list[str]:
        """SIGKILL each group whose representative still proves the session owns it.

        The check is repeated immediately before the signal. Returns what
        could not be ended, as messages; an empty list means every group was
        ended or was already gone.
        """
        failures: list[str] = []
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
            except ProcessLookupError:
                # The group is gone: normal disappearance.
                continue
            except PermissionError:
                # macOS answers EPERM for a group that holds only zombies, but
                # it is also what a live group we may not kill answers. Only
                # the first is proof that the job ended.
                if not _group_is_only_zombies(pgid):
                    failures.append("a process group could not be ended")
        return failures

    def _kill_leader_group(session_id: int, tree: ProcessTree) -> list[str]:
        """End the leader's own group, while the leader still proves it owns it.

        A leader that already exited and was reaped may have had its pid
        reused, so the proof is asked for again right here.
        """
        try:
            if _session_of(session_id) != session_id:
                return []
        except (ProcessLookupError, PermissionError):
            return []
        try:
            tree.kill()
        except ProcessLookupError:
            return []
        except PermissionError:
            if not _group_is_only_zombies(session_id):
                return ["the shell's process group could not be ended"]
        except OSError:
            return ["the shell's process group could not be ended"]
        return []

    def _reap(process: subprocess.Popen[bytes]) -> list[str]:
        """Wait for the leader to end; report it when it does not.

        A shell that is still there after the kill is not claimed to have
        ended: the caller is told.
        """
        try:
            process.wait(timeout=REAP_TIMEOUT)
            return []
        except subprocess.TimeoutExpired:
            pass
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=REAP_TIMEOUT)
        except subprocess.TimeoutExpired:
            return ["the shell could not be reaped"]
        return []

    def _end_session(session_id: int, tree: ProcessTree | None) -> list[str]:
        """End every process group in the session, the leader's own group included.

        Returns what could not be ended, as messages; the caller decides
        whether that is a failure. The normal close and the rollback of a
        spawn that failed after the child existed share this one path: a
        helper that already exec'd the user's shell may have started jobs of
        its own, and those live in the session, not in the leader's group.
        """
        failures: list[str] = []
        try:
            candidates = _list_process_ids()
        except OSError:
            candidates = []
            failures.append("the processes could not be listed")
        groups = _owned_job_groups(session_id, candidates)
        if tree is not None:
            # The leader's own group belongs to the tree, which ends it only
            # while the leader still proves it owns the session.
            groups.pop(session_id, None)
        failures += _kill_verified_groups(session_id, groups)
        if tree is not None:
            failures += _kill_leader_group(session_id, tree)
        return failures

    def _close_quietly(fd: int) -> None:
        """Close a descriptor, ignoring one that is already closed."""
        try:
            os.close(fd)
        except OSError:
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
            tree: ProcessTree | None = None,
        ) -> None:
            self.pid = pid
            self._master: int | None = master
            self._process = process
            self._tree = tree
            # One lock for the descriptor: an I/O call and the close that
            # releases it must not overlap. It is never held across the
            # blocking cleanup.
            self._io_lock = threading.Lock()
            # One lock for the close: a second caller waits for the first one
            # instead of reporting a completion that has not happened.
            self._close_lock = threading.Lock()
            self._failure: OSError | None = None
            self._closed = False

        def read(self, size: int = MAX_READ) -> bytes | None:
            """What the terminal has to say: bytes, nothing yet, or EOF.

            ``None`` means the terminal is quiet for now (the reader should
            come back), ``b""`` means the shell is gone and no more will come.
            """
            if size <= 0:
                raise ValueError("size must be positive")
            with self._io_lock:
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
            with self._io_lock:
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
            with self._io_lock:
                master = self._master
                if master is None:
                    raise OSError("the terminal is closed")
                termios.tcsetwinsize(master, (rows, cols))

        def poll(self) -> int | None:
            """The shell's exit code, or ``None`` while it runs."""
            return self._process.poll()

        def close(self) -> None:
            """End every job group this terminal owns, reap the shell, release the pty.

            Idempotent: a second call reports the same outcome as the first.
            A cleanup that could not finish is reported as a failure every
            time, never as a success.
            """
            with self._close_lock:
                if self._failure is not None:
                    raise self._failure
                if self._closed:
                    return
                self._closed = True
                failures: list[str] = []
                try:
                    failures = _end_session(self.pid, self._tree)
                except OSError:
                    failures.append("the terminal's processes could not be ended")
                # The pty is released next, under the lock the I/O calls take:
                # no call can be using the descriptor while it closes, and
                # the number cannot be handed to something else in between.
                # It also unblocks a shell that is waiting on the terminal,
                # which is what lets the reap below finish.
                self._release_master()
                failures += _reap(self._process)
                if self._tree is not None:
                    self._tree.close()
                if failures:
                    self._failure = OSError(
                        "the terminal could not be closed cleanly: " + "; ".join(failures)
                    )
                    raise self._failure

        def _release_master(self) -> None:
            """Close the pty master, under the lock the I/O calls take."""
            with self._io_lock:
                master, self._master = self._master, None
                if master is not None:
                    _close_quietly(master)

    def spawn_terminal(*, cwd: Path, cols: int = 80, rows: int = 24) -> PosixTerminalProcess:
        """Start the user's shell on a fresh pty, in ``cwd``.

        The shell becomes the leader of its own session, owns the pty as its
        controlling terminal, and can start jobs of its own. Every job group
        that stays in the session is ended by
        :meth:`PosixTerminalProcess.close`.
        """
        _check_size(cols, rows)
        folder = _check_folder(cwd)
        shell_argv = default_shell()
        if not PS_PATH.is_file() or not os.access(PS_PATH, os.X_OK):
            raise OSError(f"{PS_PATH} is missing")
        env = terminal_environment()
        master, slave = os.openpty()
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
        except BaseException:
            # No child: only the descriptors to release.
            _close_quietly(slave)
            _close_quietly(master)
            raise
        # The child exists from here on, and it may already have exec'd the
        # user's shell and started jobs of its own. Every failure below undoes
        # it through the same verified-session cleanup a normal close uses,
        # the terminal object included: it is built here, not after.
        tree: ProcessTree | None = None
        try:
            _close_quietly(slave)
            slave = -1
            _set_nonblocking(master)
            tree = ProcessTree(process.pid)
            return PosixTerminalProcess(
                pid=process.pid,
                master=master,
                process=process,
                tree=tree,
            )
        except BaseException as exc:
            failures: list[str] = []
            try:
                failures = _end_session(process.pid, tree)
            except OSError:
                failures.append("the terminal's processes could not be ended")
            finally:
                # The pty goes before the reap: a shell that is waiting on
                # the terminal cannot end until the terminal is gone.
                if slave != -1:
                    _close_quietly(slave)
                _close_quietly(master)
            failures += _reap(process)
            if failures:
                exc.add_note("the terminal's cleanup also failed: " + "; ".join(failures))
            raise
