"""ciao.os_support.locks keeps flock semantics on POSIX and Windows.

The behavioural tests run on every OS the CI matrix covers, so each platform's
implementation is exercised where it runs; the flag test pins the POSIX branch
to the exact ``flock`` calls the call sites made before the module existed.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from ciao.os_support import locks
from ciao.os_support.locks import lock_exclusive, unlock


def _open(path: Path):
    return path.open("a+", encoding="utf-8")


def test_a_second_handle_cannot_take_a_held_lock(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    with _open(path) as holder, _open(path) as contender:
        lock_exclusive(holder.fileno())
        with pytest.raises(BlockingIOError):
            lock_exclusive(contender.fileno(), blocking=False)
        unlock(holder.fileno())
        lock_exclusive(contender.fileno(), blocking=False)
        unlock(contender.fileno())


def test_closing_the_handle_releases_the_lock(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    holder = _open(path)
    lock_exclusive(holder.fileno())
    holder.close()
    with _open(path) as contender:
        lock_exclusive(contender.fileno(), blocking=False)
        unlock(contender.fileno())


def test_unlocking_an_unheld_lock_is_a_no_op(tmp_path: Path) -> None:
    with _open(tmp_path / "x.lock") as handle:
        unlock(handle.fileno())


def test_the_locked_file_stays_readable_and_unchanged_in_size(tmp_path: Path) -> None:
    """instance_lock reports the owner by reading the lock file it cannot take."""
    path = tmp_path / "server.lock"
    path.write_bytes(b'{"pid": 1}\n')
    with _open(path) as holder:
        lock_exclusive(holder.fileno())
        assert path.read_bytes() == b'{"pid": 1}\n'
        assert path.stat().st_size == len(b'{"pid": 1}\n')
        unlock(holder.fileno())


def test_a_blocking_acquire_waits_for_the_holder(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    acquired = threading.Event()
    holder = _open(path)
    lock_exclusive(holder.fileno())

    def contend() -> None:
        with _open(path) as handle:
            lock_exclusive(handle.fileno())
            acquired.set()
            unlock(handle.fileno())

    worker = threading.Thread(target=contend)
    worker.start()
    time.sleep(0.2)
    assert not acquired.is_set()
    unlock(holder.fileno())
    holder.close()
    worker.join(timeout=10)
    assert acquired.is_set()


def test_the_lock_excludes_another_process(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                f"""
                import sys
                from ciao.os_support.locks import lock_exclusive
                handle = open({str(path)!r}, "a+", encoding="utf-8")
                lock_exclusive(handle.fileno())
                print("locked", flush=True)
                sys.stdin.readline()
                """
            ),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdin is not None
        assert child.stdout.readline().strip() == "locked"
        with _open(path) as handle:
            with pytest.raises(BlockingIOError):
                lock_exclusive(handle.fileno(), blocking=False)
            child.stdin.write("\n")
            child.stdin.flush()
            assert child.wait(timeout=30) == 0
            lock_exclusive(handle.fileno(), blocking=False)
            unlock(handle.fileno())
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX branch is not defined on Windows")
def test_posix_makes_the_same_flock_calls_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(locks.fcntl, "flock", lambda fd, op: calls.append((fd, op)))
    lock_exclusive(7)
    lock_exclusive(7, blocking=False)
    unlock(7)
    assert calls == [
        (7, fcntl.LOCK_EX),
        (7, fcntl.LOCK_EX | fcntl.LOCK_NB),
        (7, fcntl.LOCK_UN),
    ]
