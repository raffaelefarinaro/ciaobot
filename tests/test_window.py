from __future__ import annotations

import os
from pathlib import Path

from ciao import window


def test_active_window_pid_none_when_no_lock(tmp_path: Path) -> None:
    assert window._active_window_pid(tmp_path) is None


def test_active_window_pid_ignores_own_pid(tmp_path: Path) -> None:
    lock = window._lock_path(tmp_path)
    lock.parent.mkdir(parents=True)
    lock.write_text(str(os.getpid()), encoding="utf-8")

    # A window never focuses itself, so its own PID does not count as "active".
    assert window._active_window_pid(tmp_path) is None


def test_active_window_pid_ignores_dead_pid(tmp_path: Path) -> None:
    dead_pid = _find_dead_pid()
    lock = window._lock_path(tmp_path)
    lock.parent.mkdir(parents=True)
    lock.write_text(str(dead_pid), encoding="utf-8")

    assert window._active_window_pid(tmp_path) is None


def test_active_window_pid_ignores_garbage(tmp_path: Path) -> None:
    lock = window._lock_path(tmp_path)
    lock.parent.mkdir(parents=True)
    lock.write_text("not-a-pid", encoding="utf-8")

    assert window._active_window_pid(tmp_path) is None


def test_active_window_pid_reports_live_pid(tmp_path: Path, monkeypatch) -> None:
    # Pretend a different process holds the lock by shifting our own PID.
    live_pid = os.getpid()
    monkeypatch.setattr(window.os, "getpid", lambda: live_pid + 1)
    lock = window._lock_path(tmp_path)
    lock.parent.mkdir(parents=True)
    lock.write_text(str(live_pid), encoding="utf-8")

    assert window._active_window_pid(tmp_path) == live_pid


def test_run_window_rejects_non_http(tmp_path: Path) -> None:
    assert window.run_window("file:///etc/passwd", tmp_path) == 1


def test_run_window_focuses_existing_instead_of_opening(tmp_path: Path, monkeypatch) -> None:
    focused: list[int] = []
    monkeypatch.setattr(window, "_active_window_pid", lambda ws: 4321)
    monkeypatch.setattr(window, "_focus_running_window", lambda pid: focused.append(pid) or True)

    # Returns without importing/starting webview because a window already exists.
    assert window.run_window("http://localhost:8443/", tmp_path) == 0
    assert focused == [4321]


def test_clear_lock_only_removes_own_lock(tmp_path: Path) -> None:
    lock = window._lock_path(tmp_path)
    lock.parent.mkdir(parents=True)
    lock.write_text("999999", encoding="utf-8")

    window._clear_lock(tmp_path)  # not our PID, must be left alone
    assert lock.exists()

    lock.write_text(str(os.getpid()), encoding="utf-8")
    window._clear_lock(tmp_path)
    assert not lock.exists()


def _find_dead_pid() -> int:
    for candidate in range(999999, 990000, -1):
        if not window._pid_alive(candidate):
            return candidate
    raise AssertionError("could not find a dead PID for the test")
