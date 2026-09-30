"""``ciao supervise``: run the engine as a child and relaunch it on the restart exit code."""

from __future__ import annotations

import collections
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

from ciao.config import RESTART_EXIT_CODE
from ciao.os_support.processes import ProcessTree, tree_spawn_options

CRASH_LOOP_MAX_RESTARTS = 5      # restart exits tolerated inside the window
CRASH_LOOP_WINDOW_S = 60.0
BACKOFF_INITIAL_S = 2.0
BACKOFF_MAX_S = 60.0
STOP_GRACE_S = 30.0              # after forwarding a stop, kill the tree if the child lingers


def default_child_argv(extra_args: Sequence[str] = ()) -> list[str]:
    """argv for the engine child: works without the console-script shim."""
    return [sys.executable, "-m", "ciao.cli", "run", "--supervised", *extra_args]


def backoff_delay(recent_restarts: int) -> float:
    """Seconds to wait before a relaunch, given restart exits inside the window.

    0.0 up to CRASH_LOOP_MAX_RESTARTS; beyond that BACKOFF_INITIAL_S doubling per
    extra restart, capped at BACKOFF_MAX_S.
    """
    excess = recent_restarts - CRASH_LOOP_MAX_RESTARTS
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
    ``returncode`` on POSIX) is reported as ``128 - returncode``.
    ``child_argv`` replaces ``default_child_argv(extra_args)`` (test seam);
    ``sleep`` defaults to waiting on the stop event so a stop request cuts a
    backoff short; ``clock`` is the crash-loop clock.
    """
    stop = threading.Event()
    tree: ProcessTree | None = None
    timer: threading.Timer | None = None
    argv = list(child_argv) if child_argv is not None else default_child_argv(extra_args)
    # Event.wait(timeout) takes the delay as its first positional arg, so the
    # same call serves both the injected seam and the real wait.
    wait: Callable[[float], Any] = sleep if sleep is not None else stop.wait

    def _on_signal(signum: int, _frame: Any) -> None:
        nonlocal timer
        stop.set()
        current = tree
        if current is None:
            return
        try:
            current.terminate()
        except (OSError, ProcessLookupError):
            pass
        # current.kill is bound to this tree, not to the name in the loop body.
        timer = threading.Timer(STOP_GRACE_S, current.kill)
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

    restarts: collections.deque[float] = collections.deque()
    try:
        while True:
            if stop.is_set():
                return 130
            proc = subprocess.Popen(argv, **tree_spawn_options(dies_with_engine=True))
            tree = ProcessTree(proc.pid, dies_with_engine=True)
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
            if stop.is_set() or code != RESTART_EXIT_CODE:
                return code if code >= 0 else 128 - code
            now = clock()
            restarts.append(now)
            while restarts and now - restarts[0] > CRASH_LOOP_WINDOW_S:
                restarts.popleft()
            delay = backoff_delay(len(restarts))
            print(
                "Engine requested a restart; relaunching "
                f"(restart {len(restarts)} in the last {int(CRASH_LOOP_WINDOW_S)}s)",
                file=sys.stderr,
                flush=True,
            )
            if delay > 0:
                print(f"Backing off {delay:g}s before relaunching.", file=sys.stderr, flush=True)
                wait(delay)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)