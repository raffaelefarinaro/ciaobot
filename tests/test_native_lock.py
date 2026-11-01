"""The native-install run lock: descriptor rebind and read-only inspection (#2401)."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from ciao import native_lock
from ciao.native_lock import NativeLockError, NativeRunLock
from tests.test_native_installer import (
    OTHER_RUN_ID,
    RUN_ID,
    _lock_path,
    _lock_reason,
    _make_lock_dir,
    _record_path,
    _state_dir,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the native installer is a macOS path; Windows keeps the legacy installer",
)


def test_acquire_rebinds_the_prepared_descriptor(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    prepared = native_lock.prepare_native_state_dir(state, trusted_root=tmp_path)
    before = os.stat(state)

    lock = NativeRunLock.acquire(prepared, run_id=RUN_ID)

    assert lock.path == _lock_path(state)
    lock_parent = os.stat(lock.path.parent)
    assert (lock_parent.st_dev, lock_parent.st_ino) == (before.st_dev, before.st_ino)
    lock.release()
    assert not _lock_path(state).exists()


def test_a_state_dir_swapped_after_preparation_is_refused(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    prepared = native_lock.prepare_native_state_dir(state, trusted_root=tmp_path)
    aside = tmp_path / "aside"
    state.rename(aside)
    state.mkdir(mode=0o700)
    os.chmod(state, 0o700)

    with pytest.raises(NativeLockError) as caught:
        NativeRunLock.acquire(prepared, run_id=RUN_ID)

    assert caught.value.reason == "state_dir_unsafe"
    assert not _lock_path(state).exists()
    assert not _lock_path(aside).exists()


def test_a_state_dir_made_unprivate_after_preparation_is_refused(
    tmp_path: Path,
) -> None:
    state = _state_dir(tmp_path)
    prepared = native_lock.prepare_native_state_dir(state, trusted_root=tmp_path)
    os.chmod(state, 0o755)

    with pytest.raises(NativeLockError) as caught:
        NativeRunLock.acquire(prepared, run_id=RUN_ID)

    assert caught.value.reason == "state_dir_unsafe"
    assert not _lock_path(state).exists()
    assert stat.S_IMODE(os.stat(state).st_mode) == 0o755


def test_a_prepared_handle_is_single_use(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    prepared = native_lock.prepare_native_state_dir(state, trusted_root=tmp_path)
    lock = NativeRunLock.acquire(prepared, run_id=RUN_ID)
    lock.release()

    assert (
        _lock_reason(NativeRunLock.acquire, prepared, run_id=RUN_ID)
        == "state_dir_unsafe"
    )

    prepared.close()
    prepared.close()
    with pytest.raises(NativeLockError) as caught:
        prepared.take()
    assert caught.value.reason == "state_dir_unsafe"


def test_an_invalid_run_id_leaves_the_handle_untaken(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    prepared = native_lock.prepare_native_state_dir(state, trusted_root=tmp_path)

    assert (
        _lock_reason(NativeRunLock.acquire, prepared, run_id="NOT-A-UUID")
        == "invalid_run_id"
    )

    descriptor = prepared.take()
    try:
        assert descriptor >= 0
    finally:
        os.close(descriptor)
    assert not _lock_path(state).exists()


def test_acquire_refuses_a_non_handle(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)

    assert (
        _lock_reason(NativeRunLock.acquire, state, run_id=RUN_ID)
        == "state_dir_unsafe"
    )
    assert not _lock_path(state).exists()


def _tree(root: Path) -> list[tuple[str, int, bytes | None]] | None:
    """Every entry under ``root`` with its mode and file bytes, never following links."""
    if not os.path.lexists(root):
        return None
    rows: list[tuple[str, int, bytes | None]] = [
        (".", stat.S_IMODE(os.lstat(root).st_mode), None)
    ]
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted(dirnames + filenames):
            full = Path(dirpath) / name
            info = os.lstat(full)
            content = full.read_bytes() if stat.S_ISREG(info.st_mode) else None
            rows.append(
                (str(full.relative_to(root)), stat.S_IMODE(info.st_mode), content)
            )
    return rows


def _lock_dir_with_pid(state: Path, pid: int) -> None:
    """A complete lock record that names ``pid``, as a killed earlier run leaves it."""
    lock_dir = _lock_path(state)
    lock_dir.mkdir(mode=0o700)
    os.chmod(lock_dir, 0o700)
    payload = {"schema": native_lock.LOCK_SCHEMA_VERSION, "run_id": RUN_ID, "pid": pid}
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
    descriptor = os.open(_record_path(state), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, data)
    finally:
        os.close(descriptor)


def _reaped_pid() -> int:
    """The pid of a child that has exited and been reaped, so nothing owns it."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


@pytest.mark.parametrize(
    "case",
    [
        pytest.param("absent", id="absent"),
        pytest.param("no-state-dir", id="no-state-dir"),
        pytest.param("busy-live", id="busy-live"),
        pytest.param("busy-dead", id="busy-dead"),
        pytest.param("empty-dir", id="empty-dir"),
        pytest.param("symlink", id="symlink"),
    ],
)
def test_inspect_reports_absent_busy_unknown_unsafe_without_mutation(
    tmp_path: Path, case: str
) -> None:
    state = tmp_path / "state"
    if case == "busy-live":
        state = _state_dir(tmp_path)
        _make_lock_dir(state, RUN_ID)
        expected = ("busy", RUN_ID, os.getpid(), True)
    elif case == "busy-dead":
        state = _state_dir(tmp_path)
        dead_pid = _reaped_pid()
        _lock_dir_with_pid(state, dead_pid)
        expected = ("busy", RUN_ID, dead_pid, False)
    elif case == "empty-dir":
        state = _state_dir(tmp_path)
        _lock_path(state).mkdir(mode=0o700)
        expected = ("unknown", None, None, None)
    elif case == "symlink":
        state = _state_dir(tmp_path)
        target = tmp_path / "target"
        target.mkdir(mode=0o700)
        os.symlink(target, _lock_path(state))
        expected = ("unsafe", None, None, None)
    elif case == "no-state-dir":
        expected = ("absent", None, None, None)
    else:
        state = _state_dir(tmp_path)
        expected = ("absent", None, None, None)

    before = _tree(state)
    status = native_lock.inspect_native_lock(state)

    assert _tree(state) == before
    assert (status.state, status.run_id, status.pid, status.pid_alive) == expected


def test_inspect_never_raises_on_an_unreadable_record(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    _make_lock_dir(state, OTHER_RUN_ID)
    _record_path(state).write_bytes(b"x" * 2048)

    status = native_lock.inspect_native_lock(state)

    assert status.state in {"unknown", "unsafe"}
    assert status.run_id is None
    assert status.pid is None
    assert status.pid_alive is None
