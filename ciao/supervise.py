"""``ciao supervise``: run the engine as a child and relaunch it on the restart exit code."""

from __future__ import annotations

import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import IO, Any

from ciao.config import RESTART_EXIT_CODE
from ciao.os_support.processes import ProcessTree, tree_spawn_options

CRASH_LOOP_MAX_RESTARTS = 5      # consecutive restart exits tolerated before waiting
CRASH_LOOP_WINDOW_S = 60.0       # a child that outran this did real work: not a crash loop
BACKOFF_INITIAL_S = 2.0
BACKOFF_MAX_S = 60.0
STOP_GRACE_S = 30.0              # after forwarding a stop, kill the tree if the child lingers


def default_child_argv(extra_args: Sequence[str] = ()) -> list[str]:
    """argv for the engine child: works without the console-script shim."""
    return [sys.executable, "-m", "ciao.cli", "run", "--supervised", *extra_args]


def _ensure_stdio(log_dir: Path) -> tuple[IO[str] | None, IO[str] | None]:
    """Under pythonw.exe stdout/stderr are None. Redirect to the runtime logs.

    Returns the opened files (to close) or (None, None) when stdio exists.
    """
    if sys.stderr is not None:
        return None, None
    log_dir.mkdir(parents=True, exist_ok=True)
    out = open(log_dir / "ciao.stdout.log", "a", encoding="utf-8", newline="", buffering=1)
    err = open(log_dir / "ciao.stderr.log", "a", encoding="utf-8", newline="", buffering=1)
    sys.stdout, sys.stderr = out, err
    return out, err


def backoff_delay(consecutive_restarts: int) -> float:
    """Seconds to wait before a relaunch, given consecutive restart-code exits.

    0.0 up to CRASH_LOOP_MAX_RESTARTS; beyond that BACKOFF_INITIAL_S doubling per
    extra restart, capped at BACKOFF_MAX_S.
    """
    excess = consecutive_restarts - CRASH_LOOP_MAX_RESTARTS
    if excess <= 0:
        return 0.0
    # min() cannot clamp an exponent Python has already overflowed, so the
    # doubling is cut at 32 steps: far past BACKOFF_MAX_S, and no overflow.
    return min(BACKOFF_MAX_S, BACKOFF_INITIAL_S * 2.0 ** min(excess - 1, 32))


def supervise(
    extra_args: Sequence[str] = (),
    *,
    child_argv: Sequence[str] | None = None,
    sleep: Callable[[float], Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Run the child until it exits with a code other than RESTART_EXIT_CODE.

    Returns the child's exit code. A child killed by a signal (negative
    ``returncode`` on POSIX) is reported as ``128 - returncode``; a child that
    asked for a restart while a stop was being forwarded reports 0 instead, so a
    service manager does not read the stop as a failed run.
    ``child_argv`` replaces ``default_child_argv(extra_args)`` (test seam);
    ``sleep`` defaults to waiting on the stop event so a stop request cuts a
    backoff short; ``clock`` times each child run, which is what tells a crash
    loop from a long-lived engine that merely asked to restart.
    """
    stop = threading.Event()
    # The task action is `pythonw.exe`, which has no console: sys.stderr is
    # None there and everything the child writes would go nowhere. The log files
    # are the same ones the macOS plist redirects to.
    saved_stdio = (sys.stdout, sys.stderr)
    out, err = _ensure_stdio(Path.cwd() / ".runtime")
    child_stdio: dict[str, Any] = (
        {} if out is None or err is None else {"stdout": out, "stderr": err}
    )
    tree: ProcessTree | None = None
    running: subprocess.Popen[Any] | None = None
    timer: threading.Timer | None = None
    argv = list(child_argv) if child_argv is not None else default_child_argv(extra_args)
    # Event.wait(timeout) takes the delay as its first positional arg, so the
    # same call serves both the injected seam and the real wait.
    wait: Callable[[float], Any] = sleep if sleep is not None else stop.wait

    def _kill_if_running(child: subprocess.Popen[Any], current: ProcessTree) -> None:
        """The grace period is over. A child that already exited is not killed:
        the tree is closed and the pid may now belong to someone else."""
        if child.poll() is None:
            try:
                current.kill()
            except (OSError, ProcessLookupError):
                pass

    def _on_signal(signum: int, _frame: Any) -> None:
        nonlocal timer
        stop.set()
        if timer is not None:
            # A second stop must not leave the first grace timer armed.
            timer.cancel()
            timer = None
        current, child = tree, running
        if current is None or child is None or child.poll() is not None:
            # Nothing is running to forward to. A kill armed here would fire
            # STOP_GRACE_S later against a reaped pid, possibly a recycled one.
            return
        try:
            current.terminate()
        except (OSError, ProcessLookupError):
            pass
        # The child and the tree this timer belongs to are bound as arguments,
        # not read from the loop's names, which move on to the next launch.
        timer = threading.Timer(STOP_GRACE_S, _kill_if_running, args=(child, current))
        timer.daemon = True
        timer.start()

    # Signal handlers only run in the main thread; a caller on another thread
    # gets the relaunch loop without the stop forwarding.
    stop_signals: list[int] = [signal.SIGTERM, signal.SIGINT]
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        stop_signals.append(sigbreak)
    previous: dict[int, Any] = {}
    if threading.current_thread() is threading.main_thread():
        for sig in stop_signals:
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, _on_signal)

    restarts = 0
    try:
        while True:
            if stop.is_set():
                return 130
            started = clock()
            proc = subprocess.Popen(
                argv, **tree_spawn_options(dies_with_engine=True), **child_stdio
            )
            running = proc
            try:
                tree = ProcessTree(proc.pid, dies_with_engine=True)
            except BaseException:
                # The child is live but untracked (on Windows the job object can
                # fail to open or to take it): kill it here rather than leave the
                # engine running with no supervisor in front of it.
                proc.kill()
                proc.wait()
                raise
            if stop.is_set():
                # A stop that landed between Popen and the tree assignment
                # never reached a child through _on_signal.
                _on_signal(signal.SIGTERM, None)
            code = proc.wait()
            if timer is not None:
                timer.cancel()
                timer = None
            tree.close()
            tree = None
            running = None
            if code == RESTART_EXIT_CODE and stop.is_set():
                # The stop reached the child first and it answered with the
                # restart code. The stop wins: 75 here would be a service
                # manager's cue to relaunch, or its record of a failed run.
                return 0
            if stop.is_set() or code != RESTART_EXIT_CODE:
                return code if code >= 0 else 128 - code
            # A child that ran longer than the window did real work before
            # asking to restart, so what preceded it was not a crash loop and
            # the count starts over; any other exit leaves the loop right here,
            # which resets it the same way. Counting consecutive exits, rather
            # than the ones inside a sliding window, is what lets the backoff
            # reach BACKOFF_MAX_S: a delay longer than the window used to age
            # the earlier restarts out of it and drop back to zero.
            restarts = 0 if clock() - started > CRASH_LOOP_WINDOW_S else restarts + 1
            delay = backoff_delay(restarts)
            print(
                f"Engine requested a restart; relaunching (restart {restarts} in a row)",
                file=sys.stderr,
                flush=True,
            )
            if delay > 0:
                print(f"Backing off {delay:g}s before relaunching.", file=sys.stderr, flush=True)
                wait(delay)
    finally:
        if out is not None:
            # The handles are about to close, so the module-level streams have to
            # go back to what they were: ciao.cli keeps writing to sys.stderr after
            # supervise returns, and a closed file would raise on the first write.
            sys.stdout, sys.stderr = saved_stdio
        for handle in (out, err):
            if handle is not None:
                handle.close()
        if timer is not None:
            timer.cancel()
        for sig, handler in previous.items():
            # signal.getsignal returns None for a handler set from C, and
            # signal.signal rejects None.
            if handler is not None:
                signal.signal(sig, handler)
