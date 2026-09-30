"""ciao.os_support.processes: a tree kill reaches grandchildren on every OS.

The trees here are real: a Python leader that starts a Python grandchild,
which proves it is alive by rewriting a heartbeat file. That works the same on
POSIX and Windows, where ``os.kill(pid, 0)`` would itself terminate the pid.
The platform tests pin each branch to its spawn options and, on POSIX, to the
exact ``killpg`` calls the call sites made before the module existed.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from ciao.os_support.processes import ProcessTree, tree_spawn_options

_GRANDCHILD = textwrap.dedent(
    """
    import sys, time
    beat = sys.argv[1]
    while True:
        with open(beat, "w") as handle:
            handle.write(str(time.monotonic()))
        time.sleep(0.05)
    """
)


def _leader(beat: Path, *, then: str) -> list[str]:
    script = textwrap.dedent(
        f"""
        import subprocess, sys, time
        subprocess.Popen([sys.executable, "-c", {_GRANDCHILD!r}, {str(beat)!r}])
        {then}
        """
    )
    return [sys.executable, "-c", script]


def _wait_for_beat(beat: Path) -> None:
    deadline = time.monotonic() + 30
    while not beat.exists() or not beat.read_text():
        if time.monotonic() > deadline:
            pytest.fail("the grandchild never started")
        time.sleep(0.05)


def _beats_stopped(beat: Path) -> bool:
    """True once the heartbeat file has not changed for a second."""
    deadline = time.monotonic() + 15
    last = beat.read_text()
    still_since = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.1)
        current = beat.read_text() if beat.exists() else last
        if current != last:
            last, still_since = current, time.monotonic()
        elif time.monotonic() - still_since >= 1.0:
            return True
    return False


def test_kill_ends_the_leader_and_its_grandchild(tmp_path: Path) -> None:
    beat = tmp_path / "beat"
    leader = subprocess.Popen(_leader(beat, then="time.sleep(300)"), **tree_spawn_options())
    tree = ProcessTree(leader.pid)
    try:
        _wait_for_beat(beat)
        tree.kill()
        assert leader.wait(timeout=15) != 0
        assert _beats_stopped(beat), "the grandchild survived the tree kill"
    finally:
        tree.close()
        if leader.poll() is None:
            leader.kill()


def test_kill_reaches_a_grandchild_after_the_leader_has_exited(tmp_path: Path) -> None:
    beat = tmp_path / "beat"
    leader = subprocess.Popen(_leader(beat, then="sys.exit(0)"), **tree_spawn_options())
    tree = ProcessTree(leader.pid)
    try:
        _wait_for_beat(beat)
        assert leader.wait(timeout=15) == 0
        tree.kill()
        assert _beats_stopped(beat), "the orphaned grandchild survived the tree kill"
    finally:
        tree.close()


def test_terminate_does_not_raise_and_close_leaves_the_tree_running(tmp_path: Path) -> None:
    leader = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"], **tree_spawn_options()
    )
    tree = ProcessTree(leader.pid)
    try:
        tree.close()
        tree.close()  # idempotent
        assert leader.poll() is None, "closing the tracker must not end the tree"
    finally:
        leader.kill()
        leader.wait(timeout=15)

    other = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"], **tree_spawn_options()
    )
    second = ProcessTree(other.pid)
    try:
        second.terminate()
    finally:
        second.kill()
        second.close()
        other.wait(timeout=15)


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX branch is not defined on Windows")
def test_posix_uses_the_same_session_and_killpg_calls_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert tree_spawn_options() == {"start_new_session": True}
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    tree = ProcessTree(4321)
    tree.terminate()
    tree.kill()
    tree.close()
    assert calls == [(4321, signal.SIGTERM), (4321, signal.SIGKILL)]


@pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are the Windows branch")
def test_windows_starts_a_new_process_group() -> None:
    assert tree_spawn_options() == {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}


@pytest.mark.skipif(sys.platform != "win32", reason="KILL_ON_JOB_CLOSE is the Windows branch")
def test_windows_a_tree_that_dies_with_the_engine_ends_when_closed(tmp_path: Path) -> None:
    """The engine exiting closes the job handle; that must end the tree."""
    beat = tmp_path / "beat"
    leader = subprocess.Popen(
        _leader(beat, then="time.sleep(300)"), **tree_spawn_options(dies_with_engine=True)
    )
    tree = ProcessTree(leader.pid, dies_with_engine=True)
    _wait_for_beat(beat)
    tree.close()
    leader.wait(timeout=15)  # it would sleep 300 s; the job's close ended it
    assert _beats_stopped(beat), "the grandchild survived the job closing"


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX branch is not defined on Windows")
def test_posix_a_tree_that_dies_with_the_engine_stays_in_its_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same group as the engine (launchd's group kill reaches it) and one-pid signals."""
    assert tree_spawn_options(dies_with_engine=True) == {}
    group: list[tuple[int, int]] = []
    single: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: group.append((pid, sig)))
    monkeypatch.setattr(os, "kill", lambda pid, sig: single.append((pid, sig)))
    tree = ProcessTree(4321, dies_with_engine=True)
    tree.terminate()
    tree.kill()
    tree.kill_descendants()  # a reaped pid may be someone else's: nothing to signal
    assert group == []
    assert single == [(4321, signal.SIGTERM), (4321, signal.SIGKILL)]

