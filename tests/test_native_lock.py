"""The native-install run lock: descriptor rebind and read-only inspection (#2401)."""

from __future__ import annotations

import errno
import json
import os
import stat
import subprocess
import sys
import threading
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


def _valid_record_bytes(pid: int, *, pad: int = 0) -> bytes:
    payload: dict[str, object] = {
        "schema": native_lock.LOCK_SCHEMA_VERSION,
        "run_id": RUN_ID,
        "pid": pid,
    }
    if pad:
        payload["pad"] = "x" * pad
    return json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"


def test_inspect_never_raises_on_an_unreadable_record(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    _make_lock_dir(state, OTHER_RUN_ID)
    oversized = _valid_record_bytes(os.getpid(), pad=2000)
    assert len(oversized) > native_lock.MAX_LOCK_RECORD_BYTES
    _record_path(state).write_bytes(oversized)

    status = native_lock.inspect_native_lock(state)

    assert status.state == "unsafe"
    assert status.run_id is None
    assert status.pid is None
    assert status.pid_alive is None


# Item 1: the reported identity comes from the one validated read.
def test_inspect_reports_identity_only_from_the_validated_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state_dir(tmp_path)
    _make_lock_dir(state, RUN_ID)
    real_read = native_lock._read_bounded_fd
    reads = 0

    def second_read_lies(fd: int, limit: int) -> bytes | None:
        nonlocal reads
        reads += 1
        data = real_read(fd, limit)
        if reads == 1:
            return data
        return _valid_record_bytes(0)

    monkeypatch.setattr(native_lock, "_read_bounded_fd", second_read_lies)

    status = native_lock.inspect_native_lock(state)

    assert reads == 1
    assert status == native_lock.NativeLockStatus(
        "busy", RUN_ID, os.getpid(), True
    )


# Item 2: Windows never probes a pid.
def test_inspect_refuses_on_win32_without_probing_a_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state_dir(tmp_path)
    _make_lock_dir(state, RUN_ID)

    def no_probe(pid: int, sig: int) -> None:
        raise AssertionError("os.kill must not run on win32")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "kill", no_probe)

    status = native_lock.inspect_native_lock(state)

    assert status == native_lock.NativeLockStatus("unsafe", None, None, None)


# Item 4: hostile shapes, each with its actual expected state.
@pytest.mark.parametrize(
    "case",
    [
        pytest.param("symlink-rec", id="symlink-rec"),
        pytest.param("hardlink", id="hardlink"),
        pytest.param("mode-0644", id="mode-0644"),
        pytest.param("extra-entry", id="extra-entry"),
        pytest.param("symlink-state", id="symlink-state"),
        pytest.param("mode-0755", id="mode-0755"),
    ],
)
def test_inspect_refuses_hostile_shapes_without_mutation(
    tmp_path: Path, case: str
) -> None:
    if case == "symlink-state":
        target = _state_dir(tmp_path)
        _make_lock_dir(target, RUN_ID)
        inspected = tmp_path / "state-link"
        os.symlink(target, inspected)
    else:
        inspected = _state_dir(tmp_path)
        if case == "mode-0755":
            os.chmod(inspected, 0o755)
        else:
            _make_lock_dir(inspected, RUN_ID)
            record = _record_path(inspected)
            if case == "symlink-rec":
                record.unlink()
                os.symlink(tmp_path / "elsewhere", record)
            elif case == "hardlink":
                os.link(record, tmp_path / "second-name")
            elif case == "mode-0644":
                os.chmod(record, 0o644)
            elif case == "extra-entry":
                (_lock_path(inspected) / "extra").write_bytes(b"x")

    before = _tree(inspected)
    status = native_lock.inspect_native_lock(inspected)

    assert _tree(inspected) == before
    assert status == native_lock.NativeLockStatus("unsafe", None, None, None)


def test_inspect_refuses_a_fifo_record_without_blocking(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    lock_dir = _lock_path(state)
    lock_dir.mkdir(mode=0o700)
    os.chmod(lock_dir, 0o700)
    os.mkfifo(_record_path(state), 0o600)
    results: list[native_lock.NativeLockStatus] = []
    worker = threading.Thread(
        target=lambda: results.append(native_lock.inspect_native_lock(state)),
        daemon=True,
    )

    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive(), "inspection blocked on a FIFO record"
    assert [status.state for status in results] == ["unsafe"]


# Item 4: each _pid_alive branch.
@pytest.mark.parametrize(
    "pid",
    [pytest.param(0, id="zero"), pytest.param(-1, id="neg-one")],
)
def test_pid_alive_never_probes_a_non_positive_pid(
    pid: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_probe(sent: int, sig: int) -> None:
        raise AssertionError("a non-positive pid must not be probed")

    monkeypatch.setattr(os, "kill", no_probe)

    assert native_lock._pid_alive(pid) is None


@pytest.mark.parametrize(
    "pid",
    [pytest.param(2**40, id="2e40"), pytest.param(2**70, id="2e70")],
)
def test_pid_alive_is_none_when_the_pid_overflows(pid: int) -> None:
    assert native_lock._pid_alive(pid) is None


def test_pid_alive_reports_the_current_process_alive() -> None:
    assert native_lock._pid_alive(os.getpid()) is True


@pytest.mark.skipif(os.geteuid() == 0, reason="as root pid 1 is probed, not refused")
def test_pid_alive_reports_init_alive_under_permission_error() -> None:
    assert native_lock._pid_alive(1) is True


def test_pid_alive_reports_a_reaped_child_dead() -> None:
    assert native_lock._pid_alive(_reaped_pid()) is False


# Item 5: no descriptor survives a refused take or acquire.
def _record_fds(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    opened: list[int] = []
    real_open = os.open

    def recording_open(*args: object, **kwargs: object) -> int:
        descriptor = real_open(*args, **kwargs)  # type: ignore[call-overload]
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(os, "open", recording_open)
    return opened


def _assert_closed(descriptors: list[int]) -> None:
    assert descriptors, "the scenario opened no descriptor to check"
    for descriptor in descriptors:
        with pytest.raises(OSError) as caught:
            os.fstat(descriptor)
        assert caught.value.errno == errno.EBADF


@pytest.mark.parametrize(
    "case",
    [
        pytest.param("swapped", id="swapped"),
        pytest.param("chmod", id="chmod"),
        pytest.param("busy", id="busy"),
    ],
)
def test_refused_acquisitions_leave_no_descriptor_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    state = _state_dir(tmp_path)
    if case == "busy":
        _make_lock_dir(state, OTHER_RUN_ID)
    opened = _record_fds(monkeypatch)
    prepared = native_lock.prepare_native_state_dir(state, trusted_root=tmp_path)
    if case == "swapped":
        state.rename(tmp_path / "aside")
        state.mkdir(mode=0o700)
        os.chmod(state, 0o700)
        expected = "state_dir_unsafe"
    elif case == "chmod":
        os.chmod(state, 0o755)
        expected = "state_dir_unsafe"
    else:
        expected = "lock_busy"

    with pytest.raises(NativeLockError) as caught:
        NativeRunLock.acquire(prepared, run_id=RUN_ID)

    assert caught.value.reason == expected
    _assert_closed(opened)


def test_take_closes_the_descriptor_on_any_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state_dir(tmp_path)
    opened = _record_fds(monkeypatch)
    prepared = native_lock.prepare_native_state_dir(state, trusted_root=tmp_path)

    def interrupted(descriptor: int) -> os.stat_result:
        raise RuntimeError("interrupted mid-check")

    monkeypatch.setattr(native_lock, "_fstat", interrupted)

    with pytest.raises(RuntimeError):
        prepared.take()
    _assert_closed(opened)


def test_take_refuses_a_symlink_at_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state_dir(tmp_path)
    opened = _record_fds(monkeypatch)
    prepared = native_lock.prepare_native_state_dir(state, trusted_root=tmp_path)
    state.rename(tmp_path / "aside")
    os.symlink(tmp_path / "aside", state)

    with pytest.raises(NativeLockError) as caught:
        prepared.take()

    assert caught.value.reason == "state_dir_unsafe"
    _assert_closed(opened)


def test_take_refuses_a_held_descriptor_with_another_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state_dir(tmp_path)
    info = os.stat(state)
    opened = _record_fds(monkeypatch)
    descriptor = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
    handle = native_lock.NativeStateDir(
        state, descriptor, (info.st_dev, info.st_ino + 1)
    )

    with pytest.raises(NativeLockError) as caught:
        handle.take()

    assert caught.value.reason == "state_dir_unsafe"
    _assert_closed(opened)


def test_take_refuses_a_held_non_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    regular = tmp_path / "regular"
    regular.write_text("not a directory", encoding="utf-8")
    info = os.stat(regular)
    opened = _record_fds(monkeypatch)
    descriptor = os.open(regular, os.O_RDONLY)
    handle = native_lock.NativeStateDir(regular, descriptor, (info.st_dev, info.st_ino))

    with pytest.raises(NativeLockError) as caught:
        handle.take()

    assert caught.value.reason == "state_dir_unsafe"
    _assert_closed(opened)
