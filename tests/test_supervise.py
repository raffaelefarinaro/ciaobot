"""ciao.supervise: a restart exit relaunches, anything else ends the supervisor.

The children here are real processes: a script written to ``tmp_path`` that
appends a line per launch to a counter file and exits with the code that launch
number names. Time is injected (``sleep``/``clock``), so no test sleeps.
"""

from __future__ import annotations

import os
import signal
import sys
import textwrap
import threading
import time
from itertools import count
from pathlib import Path
from typing import Callable

import pytest

import ciao.supervise as supervise_module
from ciao.config import RESTART_EXIT_CODE
from ciao.supervise import (
    BACKOFF_INITIAL_S,
    BACKOFF_MAX_S,
    CRASH_LOOP_MAX_RESTARTS,
    CRASH_LOOP_WINDOW_S,
    backoff_delay,
    default_child_argv,
    supervise,
)

_COUNTER_SCRIPT = textwrap.dedent(
    """
    import pathlib, sys
    counter, codes = pathlib.Path(sys.argv[1]), [int(c) for c in sys.argv[2:]]
    launches = int(counter.read_text()) if counter.exists() else 0
    counter.write_text(str(launches + 1))
    sys.exit(codes[launches])
    """
)


def _counter_child(tmp_path: Path, *codes: int) -> list[str]:
    """argv for a child that exits ``codes[n]`` on its ``n``-th launch (0-based)."""
    counter = tmp_path / "launches"
    script = tmp_path / "counter.py"
    script.write_text(_COUNTER_SCRIPT, encoding="utf-8")
    return [sys.executable, str(script), str(counter), *(str(code) for code in codes)]


def _launches(tmp_path: Path) -> int:
    counter = tmp_path / "launches"
    return int(counter.read_text()) if counter.exists() else 0


def _recording_sleep() -> tuple[list[float], Callable[[float], None]]:
    slept: list[float] = []

    def _sleep(delay: float) -> None:
        slept.append(delay)

    return slept, _sleep


def _await_file(path: Path, timeout: float = 10.0) -> None:
    """Block until ``path`` exists, so a failed child cannot hang the suite."""
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not path.exists():
        raise AssertionError(f"{path.name} never appeared")


def test_restart_code_then_zero_launches_twice(tmp_path: Path) -> None:
    slept, sleep = _recording_sleep()

    code = supervise(child_argv=_counter_child(tmp_path, RESTART_EXIT_CODE, 0), sleep=sleep)

    assert code == 0
    assert _launches(tmp_path) == 2
    assert slept == []


def test_non_restart_nonzero_exit_is_returned_without_relaunch(tmp_path: Path) -> None:
    slept, sleep = _recording_sleep()

    code = supervise(child_argv=_counter_child(tmp_path, 3), sleep=sleep)

    assert code == 3
    assert _launches(tmp_path) == 1
    assert slept == []


def test_zero_exit_is_returned_without_relaunch(tmp_path: Path) -> None:
    slept, sleep = _recording_sleep()

    code = supervise(child_argv=_counter_child(tmp_path, 0), sleep=sleep)

    assert code == 0
    assert _launches(tmp_path) == 1
    assert slept == []


def test_crash_loop_backs_off(tmp_path: Path) -> None:
    """Past CRASH_LOOP_MAX_RESTARTS consecutive restart exits each wait, doubling."""
    slept, sleep = _recording_sleep()

    code = supervise(
        child_argv=_counter_child(tmp_path, *([RESTART_EXIT_CODE] * 7), 0),
        sleep=sleep,
        clock=lambda: 1000.0,
    )

    assert code == 0
    assert _launches(tmp_path) == 8
    assert slept == [BACKOFF_INITIAL_S, BACKOFF_INITIAL_S * 2]


def test_restarts_outside_the_window_do_not_back_off(tmp_path: Path) -> None:
    """A child that ran longer than the window did real work before asking to
    restart, so its exit is not one link in a crash loop and the count starts over."""
    slept, sleep = _recording_sleep()
    ticks = count(0.0, CRASH_LOOP_WINDOW_S + 1.0)

    code = supervise(
        child_argv=_counter_child(tmp_path, *([RESTART_EXIT_CODE] * 8), 0),
        sleep=sleep,
        clock=lambda: next(ticks),
    )

    assert code == 0
    assert _launches(tmp_path) == 9
    assert slept == []


def test_backoff_keeps_growing_across_a_long_crash_loop(tmp_path: Path) -> None:
    """The backoff counts consecutive restarts, not the ones inside a window, so
    a loop that outlives the window still climbs to the cap instead of falling
    back to no wait at all."""
    now = [0.0]
    slept: list[float] = []

    def _clock() -> float:
        return now[0]

    def _sleep(delay: float) -> None:
        slept.append(delay)
        now[0] += delay

    code = supervise(
        child_argv=_counter_child(tmp_path, *([RESTART_EXIT_CODE] * 12), 0),
        sleep=_sleep,
        clock=_clock,
    )

    assert code == 0
    assert _launches(tmp_path) == 13
    assert slept == [2.0, 4.0, 8.0, 16.0, 32.0, BACKOFF_MAX_S, BACKOFF_MAX_S]


def test_backoff_delay_is_capped() -> None:
    assert backoff_delay(CRASH_LOOP_MAX_RESTARTS) == 0.0
    assert backoff_delay(CRASH_LOOP_MAX_RESTARTS + 1) == BACKOFF_INITIAL_S
    assert backoff_delay(10_000) == BACKOFF_MAX_S


def test_default_child_argv_uses_module_path() -> None:
    assert default_child_argv(["x"]) == [
        sys.executable,
        "-m",
        "ciao.cli",
        "run",
        "--supervised",
        "x",
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigterm_is_forwarded_to_the_child(tmp_path: Path) -> None:
    """The child shares the supervisor's process group, so a stop sent to the
    supervisor never reaches it on its own: the supervisor has to forward it."""
    ready, got_term = tmp_path / "ready", tmp_path / "got-term"
    script = tmp_path / "child.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import pathlib, signal, sys, time
            got_term = pathlib.Path({str(got_term)!r})
            def _term(_signum, _frame):
                got_term.write_text("term")
                sys.exit(0)
            signal.signal(signal.SIGTERM, _term)
            pathlib.Path({str(ready)!r}).write_text("ready")
            while True:
                time.sleep(0.05)
            """
        ),
        encoding="utf-8",
    )
    before = signal.getsignal(signal.SIGTERM)

    def _stop_when_ready() -> None:
        _await_file(ready)
        os.kill(os.getpid(), signal.SIGTERM)

    poller = threading.Thread(target=_stop_when_ready, daemon=True)
    poller.start()
    try:
        code = supervise(child_argv=[sys.executable, str(script)])
    finally:
        poller.join(timeout=10)

    assert code == 0
    assert got_term.exists()
    assert signal.getsignal(signal.SIGTERM) == before


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_restart_code_after_stop_returns_zero(tmp_path: Path) -> None:
    """A child that asks for a restart while the stop is being forwarded is not
    relaunched, and the stop wins over the restart code: a service manager
    reading 75 would take the stop for a failed run."""
    counter, ready, got_term = tmp_path / "launches", tmp_path / "ready", tmp_path / "got-term"
    script = tmp_path / "child.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import pathlib, signal, sys, time
            counter = pathlib.Path({str(counter)!r})
            launches = int(counter.read_text()) if counter.exists() else 0
            counter.write_text(str(launches + 1))
            def _term(_signum, _frame):
                pathlib.Path({str(got_term)!r}).write_text("term")
                sys.exit(75)
            signal.signal(signal.SIGTERM, _term)
            pathlib.Path({str(ready)!r}).write_text("ready")
            while True:
                time.sleep(0.05)
            """
        ),
        encoding="utf-8",
    )

    def _stop_when_ready() -> None:
        _await_file(ready)
        os.kill(os.getpid(), signal.SIGTERM)

    poller = threading.Thread(target=_stop_when_ready, daemon=True)
    poller.start()
    try:
        code = supervise(child_argv=[sys.executable, str(script)])
    finally:
        poller.join(timeout=10)

    assert code == 0
    assert got_term.exists()
    assert _launches(tmp_path) == 1


def test_no_stdio_redirects_the_child_into_the_runtime_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under pythonw.exe there is no console: without this the child's output is lost."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    code = supervise(
        child_argv=[
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr)",
        ]
    )

    assert code == 0
    logs = tmp_path / ".runtime"
    assert "out" in (logs / "ciao.stdout.log").read_text(encoding="utf-8")
    assert "err" in (logs / "ciao.stderr.log").read_text(encoding="utf-8")


def test_a_console_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """macOS and Linux run this under launchd/systemd with real stdio: no log files."""
    monkeypatch.chdir(tmp_path)

    code = supervise(child_argv=[sys.executable, "-c", "print('hi')"])

    assert code == 0
    assert not (tmp_path / ".runtime").exists()


def test_a_child_is_killed_when_the_process_tree_cannot_be_tracked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job object can fail to open after the child is spawned, and a live but
    untracked engine must not outlive the supervisor that failed to claim it."""
    killed: list[bool] = []

    class _UntrackedChild:
        pid = 4321

        def __init__(self, argv: list[str], **options: object) -> None:
            self.argv = argv

        def kill(self) -> None:
            killed.append(True)

        def wait(self) -> int:
            return -9

    class _UnclaimableTree:
        def __init__(self, pid: int, *, dies_with_engine: bool = False) -> None:
            raise OSError("no job object")

    monkeypatch.setattr(supervise_module.subprocess, "Popen", _UntrackedChild)
    monkeypatch.setattr(supervise_module, "ProcessTree", _UnclaimableTree)

    with pytest.raises(OSError):
        supervise(child_argv=["engine"])

    assert killed == [True]
