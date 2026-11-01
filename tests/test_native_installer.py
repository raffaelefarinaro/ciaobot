"""The native installer's bounded NDJSON event channel (#2340).

The first slice of the native installer is the public progress protocol and
nothing else: no shell, no coordinator, no service, no route and no install.
These tests pin the wire contract on its own, against real inherited pipes —
the descriptor a client passes as ``--events-fd`` — so the properties the Mac
client depends on are proven before any code can emit an event.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import inspect
import json
import os
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from ciao import native_handoff
from ciao import native_installer as native
from ciao import native_lock
from ciao import release_manifest, server_host, server_host_install
from ciao.install_discovery import NativeInstallState
from ciao.native_installer import EventError, EventWriter
from ciao.native_lock import NativeLockError, NativeRunLock
from ciao.native_readiness import NativeReadinessProof
from tests.test_server_host_install import APP_NAME, _FakeRunner, _fixture

#: A canonical run id: lowercase, hyphenated, exactly what ``--run-id`` accepts.
RUN_ID = "0f6f7c0e-2f3a-4b4c-8d1e-2a3b4c5d6e7f"
OTHER_RUN_ID = "7a1c2b3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d"


class _HostileInt(int):
    """An int subclass whose comparisons and coercions must never run."""

    def __le__(self, other: object) -> bool:
        raise AssertionError("an int subclass comparison must not run")

    def __lt__(self, other: object) -> bool:
        raise AssertionError("an int subclass comparison must not run")

    def __index__(self) -> int:
        raise AssertionError("an int subclass coercion must not run")


class _HostileFloat(float):
    """A float subclass whose comparisons and coercions must never run."""

    def __le__(self, other: object) -> bool:
        raise AssertionError("a float subclass comparison must not run")

    def __float__(self) -> float:
        raise AssertionError("a float subclass coercion must not run")


class _HostileStr(str):
    """A string subclass whose equality/hash must never run."""

    def __eq__(self, other: object) -> bool:
        raise AssertionError("a string subclass comparison must not run")

    def __hash__(self) -> int:
        raise AssertionError("a string subclass hash must not run")


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the native installer is a macOS path; Windows keeps the legacy installer",
)


def _writer(**kwargs: object) -> tuple[EventWriter, int]:
    """A writer on a fresh pipe, plus the read end to inspect its bytes."""
    read_fd, write_fd = os.pipe()
    return EventWriter(write_fd, run_id=RUN_ID, **kwargs), read_fd  # type: ignore[arg-type]


def _drain(read_fd: int) -> bytes:
    """Every byte up to end-of-file; the writer must already be closed."""
    chunks: list[bytes] = []
    while True:
        chunk = os.read(read_fd, 65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _available(read_fd: int) -> bytes:
    """Whatever is written right now, without waiting for the writer to close."""
    blocking = os.get_blocking(read_fd)
    os.set_blocking(read_fd, False)
    try:
        return os.read(read_fd, 65536)
    except BlockingIOError:
        return b""
    finally:
        os.set_blocking(read_fd, blocking)


def _events(read_fd: int) -> list[dict[str, object]]:
    payload = _drain(read_fd)
    return [json.loads(line) for line in payload.splitlines()]


def _reason(callable_: object, *args: object, **kwargs: object) -> str:
    """The fixed reason of the refusal ``callable_`` raises."""
    with pytest.raises(EventError) as caught:
        callable_(*args, **kwargs)  # type: ignore[operator]
    message = str(caught.value)
    assert message == caught.value.reason
    return caught.value.reason


def _lock_reason(callable_: object, *args: object, **kwargs: object) -> str:
    """The fixed reason of the lock refusal ``callable_`` raises."""
    with pytest.raises(NativeLockError) as caught:
        callable_(*args, **kwargs)  # type: ignore[operator]
    assert str(caught.value) == caught.value.reason
    return caught.value.reason


def _state_dir(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    os.chmod(state, 0o700)
    return state


def _prepared(state: Path) -> native_lock.NativeStateDir:
    """The validated handle acquisition now requires, for an existing test state dir."""
    return native_lock.prepare_native_state_dir(state, trusted_root=state.parent)


def _lock_path(state: Path) -> Path:
    return state / native_lock.LOCK_DIR_NAME


def _record_path(state: Path) -> Path:
    return _lock_path(state) / native_lock.LOCK_RECORD_NAME


def _record_bytes(run_id: str = RUN_ID, *, pid: int | None = None) -> bytes:
    payload = {
        "schema": native_lock.LOCK_SCHEMA_VERSION,
        "run_id": run_id,
        "pid": os.getpid() if pid is None else pid,
    }
    return (
        json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
    )


def _make_lock_dir(state: Path, run_id: str = RUN_ID) -> Path:
    """A well-formed lock directory as a dead earlier process would leave it."""
    lock_dir = _lock_path(state)
    lock_dir.mkdir(mode=0o700)
    os.chmod(lock_dir, 0o700)
    record = lock_dir / native_lock.LOCK_RECORD_NAME
    descriptor = os.open(
        record, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    try:
        os.write(descriptor, _record_bytes(run_id))
    finally:
        os.close(descriptor)
    return lock_dir


def _run_lock_child(
    state: Path, run_id: str
) -> subprocess.CompletedProcess[bytes]:
    root = Path(__file__).resolve().parents[1]
    child = (
        "import sys\n"
        "from pathlib import Path\n"
        "from ciao.native_lock import NativeLockError, NativeRunLock, prepare_native_state_dir\n"
        "try:\n"
        "    lock = NativeRunLock.acquire(prepare_native_state_dir(Path(sys.argv[1]), trusted_root=Path(sys.argv[1]).parent), run_id=sys.argv[2])\n"
        "except NativeLockError as exc:\n"
        "    print('refused', exc.reason, flush=True)\n"
        "    raise SystemExit(7)\n"
        "print('acquired', flush=True)\n"
        "lock.release()\n"
        "print('released', flush=True)\n"
    )
    return subprocess.run(
        [sys.executable, "-c", child, str(state), run_id],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        timeout=30,
    )


# ── Wire format and real inheritance ─────────────────────────────────────


def test_events_are_compact_ndjson_with_exact_key_sets() -> None:
    writer, read_fd = _writer()
    try:
        writer.phase("preflight")
        writer.phase("wizard")
        writer.wizard_ready(51234)
        writer.setup_saved()
        writer.phase("activation")
        writer.phase("readiness")
        writer.service_ready(51234)
        assert writer.terminal == "service_ready"
        assert writer.exit_code == 0
    finally:
        writer.close()

    payload = _drain(read_fd)
    os.close(read_fd)
    lines = payload.splitlines()
    assert len(lines) == 7
    assert payload.endswith(b"\n")
    expected_keys = [
        {"schema", "run_id", "seq", "type", "phase"},
        {"schema", "run_id", "seq", "type", "phase"},
        {"schema", "run_id", "seq", "type", "origin"},
        {"schema", "run_id", "seq", "type"},
        {"schema", "run_id", "seq", "type", "phase"},
        {"schema", "run_id", "seq", "type", "phase"},
        {"schema", "run_id", "seq", "type", "origin"},
    ]
    events = [json.loads(line) for line in lines]
    for line, event, keys in zip(lines, events, expected_keys):
        assert len(line) <= native.MAX_EVENT_BYTES
        assert b": " not in line and b", " not in line
        assert set(event) == keys
        assert event["schema"] == native.SCHEMA_VERSION
        assert event["run_id"] == RUN_ID
    assert [event["seq"] for event in events] == [1, 2, 3, 4, 5, 6, 7]
    assert [event["type"] for event in events] == [
        "phase",
        "phase",
        "wizard_ready",
        "setup_saved",
        "phase",
        "phase",
        "service_ready",
    ]
    assert events[2]["origin"] == "http://127.0.0.1:51234"
    assert events[6]["origin"] == "http://127.0.0.1:51234"


def test_a_real_inherited_fd_carries_events_to_another_process() -> None:
    """The contract is an inherited writable descriptor, not a regular file."""
    root = Path(__file__).resolve().parents[1]
    child = (
        "import sys\n"
        "from ciao.native_installer import EventWriter\n"
        "writer = EventWriter(int(sys.argv[1]), run_id=sys.argv[2], first_seq=2, prior_events=1)\n"
        "writer.phase('preflight')\n"
        "writer.failed('cancelled')\n"
        "writer.close()\n"
        "raise SystemExit(writer.exit_code)\n"
    )
    read_fd, write_fd = os.pipe()
    try:
        process = subprocess.Popen(
            [sys.executable, "-c", child, str(write_fd), RUN_ID],
            pass_fds=(write_fd,),
            cwd=root,
            env={**os.environ, "PYTHONPATH": str(root)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        os.close(write_fd)
        write_fd = -1
        stdout, stderr = process.communicate(timeout=30)
    finally:
        if write_fd >= 0:
            os.close(write_fd)
    try:
        assert process.returncode == 1, (stdout, stderr)
        events = _events(read_fd)
    finally:
        os.close(read_fd)
    assert [event["seq"] for event in events] == [2, 3]
    assert [event["type"] for event in events] == ["phase", "failed"]
    assert events[0]["phase"] == "preflight"
    assert events[1]["code"] == "cancelled"


def test_short_writes_are_completed_into_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer, read_fd = _writer()
    original = writer._write_once

    def three_bytes_at_a_time(fd: int, data: memoryview) -> int:
        return original(fd, data[:3])

    monkeypatch.setattr(writer, "_write_once", three_bytes_at_a_time)
    try:
        writer.phase("download")
    finally:
        writer.close()

    events = _events(read_fd)
    os.close(read_fd)
    assert len(events) == 1
    assert events[0]["phase"] == "download"
    assert events[0]["seq"] == 1


# ── Descriptor validation and channel failures ───────────────────────────


def test_closed_stdio_read_only_and_invalid_descriptors_are_refused() -> None:
    assert _reason(EventWriter, 10**6, run_id=RUN_ID) == "invalid_fd"
    assert _reason(EventWriter, True, run_id=RUN_ID) == "invalid_fd"  # type: ignore[arg-type]
    assert _reason(EventWriter, -1, run_id=RUN_ID) == "invalid_fd"
    assert (
        _reason(EventWriter, _HostileInt(5), run_id=RUN_ID) == "invalid_fd"  # type: ignore[arg-type]
    )
    for stdio_fd in (0, 1, 2):
        assert _reason(EventWriter, stdio_fd, run_id=RUN_ID) == "invalid_fd"

    read_fd, write_fd = os.pipe()
    try:
        assert _reason(EventWriter, read_fd, run_id=RUN_ID) == "read_only_fd"
    finally:
        os.close(read_fd)
        os.close(write_fd)

    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    try:
        assert _reason(EventWriter, write_fd, run_id=RUN_ID) == "invalid_fd"
    finally:
        os.close(read_fd)


def test_a_closed_reader_is_a_fixed_failure_and_the_writer_stops() -> None:
    read_fd, write_fd = os.pipe()
    writer = EventWriter(write_fd, run_id=RUN_ID)
    os.close(read_fd)
    try:
        assert _reason(writer.phase, "preflight") == "channel_closed"
        assert writer.exit_code == 1
        assert writer.terminal is None
        assert _reason(writer.phase, "bootstrap") == "channel_unavailable"
    finally:
        writer.close()


def test_an_os_error_is_not_echoed_into_the_public_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer, read_fd = _writer()
    sentinel = "sk-live-PIPE-SECRET-0123456789"

    def explode(fd: int, data: memoryview) -> int:
        raise OSError(sentinel)

    monkeypatch.setattr(writer, "_write_once", explode)
    try:
        with pytest.raises(EventError) as caught:
            writer.phase("preflight")
        assert caught.value.reason == "channel_closed"
        assert sentinel not in str(caught.value)
        assert sentinel not in repr(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__suppress_context__ is True
    finally:
        writer.close()
        os.close(read_fd)


def test_a_backpressured_pipe_fails_closed_instead_of_hanging() -> None:
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    try:
        while True:
            try:
                os.write(write_fd, b"x" * 4096)
            except BlockingIOError:
                break
        writer = EventWriter(write_fd, run_id=RUN_ID, write_timeout=0.05)
        started = time.monotonic()
        try:
            assert _reason(writer.phase, "preflight") == "channel_blocked"
        finally:
            writer.close()
        assert time.monotonic() - started < 2.0
        assert _reason(writer.phase, "preflight") == "channel_unavailable"
    finally:
        os.close(read_fd)


@pytest.mark.parametrize("error", [ValueError, OverflowError])
def test_select_failures_are_fixed_channel_failures(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    writer, read_fd = _writer()
    sentinel = "select-SECRET-0123456789"

    def no_write(fd: int, data: memoryview) -> int:
        return 0

    def explode(*args: object, **kwargs: object) -> object:
        raise error(sentinel)

    monkeypatch.setattr(writer, "_write_once", no_write)
    monkeypatch.setattr(native.select, "select", explode)
    try:
        with pytest.raises(EventError) as caught:
            writer.phase("preflight")
        assert caught.value.reason == "channel_closed"
        assert sentinel not in str(caught.value)
        assert sentinel not in repr(caught.value)
        assert writer.terminal is None
        assert writer.exit_code == 1
    finally:
        writer.close()
    assert _drain(read_fd) == b""
    os.close(read_fd)


def test_a_partial_line_then_a_failure_closes_and_restores_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    probe_fd = os.dup(write_fd)
    writer = EventWriter(write_fd, run_id=RUN_ID)
    calls = 0

    def flaky(fd: int, data: memoryview) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return os.write(fd, data[:5])
        raise OSError("partial-SECRET-0123456789")

    monkeypatch.setattr(writer, "_write_once", flaky)
    try:
        with pytest.raises(EventError) as caught:
            writer.phase("preflight")
        assert caught.value.reason == "channel_closed"
        assert "partial-SECRET-0123456789" not in str(caught.value)
        assert writer.terminal is None
        assert writer.exit_code == 1
        assert _available(read_fd) == b'{"sch'
        assert os.get_blocking(probe_fd) is True
        assert _reason(writer.phase, "bootstrap") == "channel_unavailable"
    finally:
        writer.close()
        os.close(probe_fd)
        os.close(read_fd)


def test_the_context_manager_closes_and_restores_flags() -> None:
    read_fd, write_fd = os.pipe()
    probe_fd = os.dup(write_fd)
    try:
        with EventWriter(write_fd, run_id=RUN_ID) as writer:
            assert os.get_blocking(probe_fd) is False
            writer.phase("preflight")
            writer.failed("cancelled")
        assert os.get_blocking(probe_fd) is True
        assert writer.exit_code == 1
        os.close(probe_fd)
        probe_fd = -1
        events = _events(read_fd)
        assert [event["type"] for event in events] == ["phase", "failed"]
    finally:
        if probe_fd >= 0:
            os.close(probe_fd)
        os.close(read_fd)
        try:
            os.close(write_fd)
        except OSError:
            pass


def test_a_socket_descriptor_is_accepted_like_a_pipe() -> None:
    left, right = socket.socketpair()
    fd = os.dup(left.fileno())
    writer = EventWriter(fd, run_id=RUN_ID)
    try:
        writer.failed("cancelled")
    finally:
        writer.close()
    left.close()
    right.settimeout(5)
    chunks: list[bytes] = []
    try:
        while True:
            chunk = right.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        right.close()
    events = [json.loads(line) for line in b"".join(chunks).splitlines()]
    assert [event["type"] for event in events] == ["failed"]


@pytest.mark.parametrize("disposition", ["default", "handler"])
def test_non_ignored_sigpipe_dispositions_refuse_before_writing(
    disposition: str,
) -> None:
    root = Path(__file__).resolve().parents[1]
    child = (
        "import os, signal, sys\n"
        "from ciao.native_installer import EventWriter, EventError\n"
        "if sys.argv[1] == 'default':\n"
        "    signal.signal(signal.SIGPIPE, signal.SIG_DFL)\n"
        "else:\n"
        "    signal.signal(signal.SIGPIPE, lambda *args: None)\n"
        "read_fd, write_fd = os.pipe()\n"
        "os.set_blocking(read_fd, False)\n"
        "try:\n"
        "    EventWriter(write_fd, run_id=sys.argv[2])\n"
        "except EventError as exc:\n"
        "    if exc.reason != 'sigpipe_not_ignored':\n"
        "        raise SystemExit(2)\n"
        "    try:\n"
        "        written = len(os.read(read_fd, 1))\n"
        "    except BlockingIOError:\n"
        "        written = 0\n"
        "    print(exc.reason, written, flush=True)\n"
        "    raise SystemExit(7)\n"
        "raise SystemExit(1)\n"
    )
    process = subprocess.run(
        [sys.executable, "-c", child, disposition, RUN_ID],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        timeout=30,
    )
    assert process.returncode == 7, (process.stdout, process.stderr)
    assert process.stdout.strip() == b"sigpipe_not_ignored 0"


def test_ignored_sigpipe_maps_epipe_to_the_fixed_channel_error() -> None:
    root = Path(__file__).resolve().parents[1]
    child = (
        "import os, signal, sys\n"
        "signal.signal(signal.SIGPIPE, signal.SIG_IGN)\n"
        "from ciao.native_installer import EventWriter, EventError\n"
        "read_fd, write_fd = os.pipe()\n"
        "os.close(read_fd)\n"
        "writer = EventWriter(write_fd, run_id=sys.argv[1])\n"
        "try:\n"
        "    writer.phase('preflight')\n"
        "except EventError as exc:\n"
        "    print(exc.reason, flush=True)\n"
        "    raise SystemExit(7)\n"
        "raise SystemExit(1)\n"
    )
    process = subprocess.run(
        [sys.executable, "-c", child, RUN_ID],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        timeout=30,
    )
    assert process.returncode == 7, (process.stdout, process.stderr)
    assert process.stdout.strip() == b"channel_closed"


def test_a_disposition_change_after_construction_refuses_before_the_write() -> None:
    root = Path(__file__).resolve().parents[1]
    child = (
        "import os, signal, sys\n"
        "signal.signal(signal.SIGPIPE, signal.SIG_IGN)\n"
        "from ciao.native_installer import EventWriter, EventError\n"
        "read_fd, write_fd = os.pipe()\n"
        "os.set_blocking(read_fd, False)\n"
        "writer = EventWriter(write_fd, run_id=sys.argv[1])\n"
        "signal.signal(signal.SIGPIPE, signal.SIG_DFL)\n"
        "try:\n"
        "    writer.phase('preflight')\n"
        "except EventError as exc:\n"
        "    try:\n"
        "        written = len(os.read(read_fd, 1))\n"
        "    except BlockingIOError:\n"
        "        written = 0\n"
        "    print(exc.reason, written, flush=True)\n"
        "    raise SystemExit(7)\n"
        "raise SystemExit(1)\n"
    )
    process = subprocess.run(
        [sys.executable, "-c", child, RUN_ID],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        timeout=30,
    )
    assert process.returncode == 7, (process.stdout, process.stderr)
    assert process.stdout.strip() == b"sigpipe_not_ignored 0"


def test_a_pending_unrelated_sigpipe_is_left_pending() -> None:
    root = Path(__file__).resolve().parents[1]
    child = (
        "import os, signal, sys\n"
        "from ciao.native_installer import EventWriter, EventError\n"
        "signal.signal(signal.SIGPIPE, lambda *args: None)\n"
        "signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGPIPE})\n"
        "signal.raise_signal(signal.SIGPIPE)\n"
        "if signal.SIGPIPE not in signal.sigpending():\n"
        "    raise SystemExit(3)\n"
        "read_fd, write_fd = os.pipe()\n"
        "os.set_blocking(read_fd, False)\n"
        "try:\n"
        "    EventWriter(write_fd, run_id=sys.argv[1])\n"
        "except EventError as exc:\n"
        "    if exc.reason != 'sigpipe_not_ignored':\n"
        "        raise SystemExit(2)\n"
        "    pending = signal.SIGPIPE in signal.sigpending()\n"
        "    print(exc.reason, pending, flush=True)\n"
        "    raise SystemExit(7)\n"
        "raise SystemExit(1)\n"
    )
    process = subprocess.run(
        [sys.executable, "-c", child, RUN_ID],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        timeout=30,
    )
    assert process.returncode == 7, (process.stdout, process.stderr)
    assert process.stdout.strip() == b"sigpipe_not_ignored True"


# ── Vocabulary, ordering and terminal invariants ─────────────────────────


def test_only_fixed_phases_and_failure_codes_are_accepted() -> None:
    writer, read_fd = _writer()
    sentinel = "sk-live-EVENT-SECRET-0123456789"
    try:
        assert _reason(writer.phase, sentinel) == "unknown_phase"

        for code in native.FAILURE_CODES:
            other, other_fd = _writer()
            try:
                other.failed(code)
                assert other.terminal == "failed"
            finally:
                other.close()
                os.close(other_fd)
        assert _reason(writer.failed, sentinel) == "unknown_failure_code"
        assert _reason(writer.failed, "Cancelled") == "unknown_failure_code"
        assert _reason(writer.failed, 42) == "unknown_failure_code"  # type: ignore[arg-type]
    finally:
        writer.close()
        assert _drain(read_fd) == b""
        os.close(read_fd)


def test_event_order_is_enforced_before_anything_is_written() -> None:
    writer, read_fd = _writer()
    try:
        assert _reason(writer.wizard_ready, 5000) == "wizard_not_started"
        assert _reason(writer.setup_saved) == "wizard_not_ready"
        assert _reason(writer.service_ready, 5000) == "setup_not_saved"

        writer.phase("wizard")  # a gap: the shell wrote the earlier phases
        assert _reason(writer.phase, "preflight") == "phase_out_of_order"
        assert _reason(writer.phase, "wizard") == "duplicate_phase"
        writer.wizard_ready(5000)
        assert _reason(writer.wizard_ready, 5000) == "wizard_already_ready"
        writer.setup_saved()
        assert _reason(writer.setup_saved) == "setup_already_saved"
        assert _reason(writer.phase, "wizard") == "duplicate_phase"
        writer.phase("activation")
        writer.phase("readiness")
        writer.service_ready(5000)

        assert _reason(writer.phase, "preflight") == "after_terminal"
        assert _reason(writer.failed, "cancelled") == "after_terminal"
        assert _reason(writer.service_ready, 5000) == "after_terminal"
    finally:
        writer.close()
    events = _events(read_fd)
    os.close(read_fd)
    assert [event["type"] for event in events] == [
        "phase",
        "wizard_ready",
        "setup_saved",
        "phase",
        "phase",
        "service_ready",
    ]
    assert [event["seq"] for event in events] == [1, 2, 3, 4, 5, 6]


def test_exactly_one_terminal_event_and_its_exit_code() -> None:
    writer, read_fd = _writer()
    try:
        writer.phase("preflight")
        writer.failed("download_failed")
        assert writer.terminal == "failed"
        assert writer.exit_code == 1
        assert _reason(writer.failed, "internal_error") == "after_terminal"
        assert _reason(writer.phase, "bootstrap") == "after_terminal"
    finally:
        writer.close()
    events = _events(read_fd)
    os.close(read_fd)
    assert [event["type"] for event in events] == ["phase", "failed"]

    unfinished, read_fd = _writer()
    try:
        unfinished.phase("preflight")
        assert unfinished.terminal is None
        assert unfinished.exit_code == 1
    finally:
        unfinished.close()
    os.close(read_fd)


def test_a_reentrant_call_from_a_handler_seam_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handlers must not write; the guard refuses instead of deadlocking."""
    writer, read_fd = _writer()

    def reenter(fd: int, data: memoryview) -> int:
        writer.failed("cancelled")
        raise AssertionError("unreachable")

    original = writer._write_once
    monkeypatch.setattr(writer, "_write_once", reenter)
    try:
        assert _reason(writer.phase, "preflight") == "reentrant_emit"
        assert writer.terminal is None
        monkeypatch.setattr(writer, "_write_once", original)
        writer.failed("cancelled")
        assert writer.terminal == "failed"
    finally:
        writer.close()
    events = _events(read_fd)
    os.close(read_fd)
    assert [event["type"] for event in events] == ["failed"]


def test_a_reentrant_close_from_a_handler_seam_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer, read_fd = _writer()
    original = writer._write_once

    def reenter(fd: int, data: memoryview) -> int:
        writer.close()
        return 0

    monkeypatch.setattr(writer, "_write_once", reenter)
    try:
        assert _reason(writer.phase, "preflight") == "reentrant_emit"
        monkeypatch.setattr(writer, "_write_once", original)
        writer.failed("cancelled")
    finally:
        writer.close()
    events = _events(read_fd)
    os.close(read_fd)
    assert [event["type"] for event in events] == ["failed"]


# ── Identity, origins, limits and untrusted input ────────────────────────


def test_run_id_must_be_a_canonical_lowercase_hyphenated_uuid() -> None:
    read_fd, write_fd = os.pipe()
    try:
        for bad in (
            RUN_ID.upper(),
            "{" + RUN_ID + "}",
            RUN_ID.replace("-", ""),
            "urn:uuid:" + RUN_ID,
            "not-a-uuid",
            "",
        ):
            assert _reason(EventWriter, write_fd, run_id=bad) == "invalid_run_id"

        writer = EventWriter(write_fd, run_id=str(uuid.uuid4()))
        writer.failed("invalid_arguments")
        writer.close()
    finally:
        os.close(read_fd)
        try:
            os.close(write_fd)
        except OSError:
            pass


def test_origins_are_built_only_from_a_real_loopback_port() -> None:
    writer, read_fd = _writer()
    try:
        writer.phase("wizard")
        for bad in (0, 65536, -1, True, "51234", 51234.0, _HostileInt(80)):
            assert _reason(writer.wizard_ready, bad) == "invalid_origin"
        writer.wizard_ready(1)
        writer.setup_saved()
        for bad in (0, 65536, -1, True, "51234", 51234.0, _HostileInt(80)):
            assert _reason(writer.service_ready, bad) == "invalid_origin"
        writer.service_ready(65535)
    finally:
        writer.close()
    events = _events(read_fd)
    os.close(read_fd)
    origins = [event.get("origin") for event in events if "origin" in event]
    assert origins == ["http://127.0.0.1:1", "http://127.0.0.1:65535"]


def test_write_timeouts_must_be_finite_and_bounded() -> None:
    read_fd, write_fd = os.pipe()
    try:
        for bad in (
            0,
            -1,
            float("nan"),
            float("inf"),
            float("-inf"),
            native.MAX_WRITE_TIMEOUT_SECONDS + 1,
            True,
            "5",
            _HostileFloat(5.0),
        ):
            assert (
                _reason(EventWriter, write_fd, run_id=RUN_ID, write_timeout=bad)
                == "invalid_timeout"
            )
        writer = EventWriter(
            write_fd, run_id=RUN_ID, write_timeout=native.MAX_WRITE_TIMEOUT_SECONDS
        )
        writer.failed("cancelled")
        writer.close()
    finally:
        os.close(read_fd)
        try:
            os.close(write_fd)
        except OSError:
            pass


def test_gapped_seq_and_prior_event_count_are_tracked_independently() -> None:
    writer, read_fd = _writer(first_seq=1000, prior_events=30)
    try:
        writer.phase("preflight")
        writer.failed("cancelled")
    finally:
        writer.close()
    events = _events(read_fd)
    os.close(read_fd)
    assert [event["seq"] for event in events] == [1000, 1001]
    assert [event["type"] for event in events] == ["phase", "failed"]


def test_only_the_terminal_event_may_use_the_last_remaining_slot() -> None:
    writer, read_fd = _writer(first_seq=1000, prior_events=native.MAX_EVENTS - 1)
    try:
        assert _reason(writer.phase, "preflight") == "too_many_events"
        writer.failed("cancelled")
        assert writer.exit_code == 1
    finally:
        writer.close()
    events = _events(read_fd)
    os.close(read_fd)
    assert [event["seq"] for event in events] == [1000]
    assert [event["type"] for event in events] == ["failed"]


def test_non_terminal_events_reserve_one_slot_for_the_terminal() -> None:
    writer, read_fd = _writer(first_seq=1000, prior_events=30)
    try:
        writer.phase("preflight")
        assert _reason(writer.phase, "bootstrap") == "too_many_events"
        writer.failed("cancelled")
    finally:
        writer.close()
    events = _events(read_fd)
    os.close(read_fd)
    assert [event["seq"] for event in events] == [1000, 1001]
    assert [event["type"] for event in events] == ["phase", "failed"]


def test_sequence_and_prior_event_counts_are_validated_strictly() -> None:
    read_fd, write_fd = os.pipe()
    try:
        for bad in (0, -1, True, "1", 1.0, _HostileInt(1)):
            assert (
                _reason(EventWriter, write_fd, run_id=RUN_ID, first_seq=bad)
                == "invalid_seq"
            )
        for bad in (-1, True, "0", 0.0, _HostileInt(0)):
            assert (
                _reason(EventWriter, write_fd, run_id=RUN_ID, prior_events=bad)
                == "invalid_prior_events"
            )
        assert (
            _reason(
                EventWriter, write_fd, run_id=RUN_ID, prior_events=native.MAX_EVENTS
            )
            == "sequence_exhausted"
        )
        assert (
            _reason(EventWriter, write_fd, run_id=RUN_ID, first_seq=2, prior_events=2)
            == "invalid_seq"
        )
        writer = EventWriter(
            write_fd, run_id=RUN_ID, first_seq=2, prior_events=1
        )
        writer.failed("cancelled")
        writer.close()
    finally:
        os.close(read_fd)
        try:
            os.close(write_fd)
        except OSError:
            pass


def test_the_total_event_count_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer, read_fd = _writer()
    monkeypatch.setattr(native, "MAX_EVENTS", 2)
    try:
        writer.phase("preflight")
        assert _reason(writer.phase, "bootstrap") == "too_many_events"
        writer.failed("cancelled")
    finally:
        writer.close()
    events = _events(read_fd)
    os.close(read_fd)
    assert [event["type"] for event in events] == ["phase", "failed"]


def test_an_oversized_line_is_refused_before_it_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer, read_fd = _writer()
    monkeypatch.setattr(native, "MAX_EVENT_BYTES", 8)
    try:
        assert _reason(writer.phase, "preflight") == "event_too_large"
        assert writer.terminal is None
        assert writer.exit_code == 1
    finally:
        writer.close()
    assert _drain(read_fd) == b""
    os.close(read_fd)


def test_untrusted_values_are_refused_and_never_echoed() -> None:
    writer, read_fd = _writer()
    sentinel = 'sk-live-SECRET-0123456789","run_id":"attacker'
    try:
        probe_read, probe_write = os.pipe()
        try:
            with pytest.raises(EventError) as caught:
                EventWriter(probe_write, run_id=sentinel)
            assert sentinel not in str(caught.value)
            assert sentinel not in repr(caught.value)
            with pytest.raises(EventError) as caught:
                EventWriter(probe_write, run_id=_HostileStr(RUN_ID))
            assert caught.value.reason == "invalid_run_id"
        finally:
            os.close(probe_read)
            os.close(probe_write)
        for attempt in (
            lambda: writer.phase(sentinel),
            lambda: writer.phase(_HostileStr("preflight")),
            lambda: writer.failed(sentinel),
            lambda: writer.failed(_HostileStr("cancelled")),
            lambda: writer.wizard_ready(sentinel),  # type: ignore[arg-type]
        ):
            with pytest.raises(EventError) as caught:
                attempt()
            assert sentinel not in str(caught.value)
            assert sentinel not in repr(caught.value)
        assert _available(read_fd) == b""

        writer.phase("preflight")
        writer.failed("internal_error")
    finally:
        writer.close()
    events = _events(read_fd)
    os.close(read_fd)
    assert set(events[1]) == {"schema", "run_id", "seq", "type", "code"}
    assert events[1]["code"] == "internal_error"


def test_close_restores_the_inherited_descriptor_flags() -> None:
    read_fd, write_fd = os.pipe()
    probe_fd = os.dup(write_fd)
    try:
        assert os.get_blocking(probe_fd) is True
        writer = EventWriter(write_fd, run_id=RUN_ID)
        try:
            assert os.get_blocking(probe_fd) is False
        finally:
            writer.close()
        assert os.get_blocking(probe_fd) is True
        assert writer.exit_code == 1
    finally:
        os.close(probe_fd)
        os.close(read_fd)
        try:
            os.close(write_fd)
        except OSError:
            pass


# ── Run-bound lock and provenance ─────────────────────────────────────────


def test_prepare_refuses_a_relative_or_dotdot_path(tmp_path: Path) -> None:
    assert (
        _lock_reason(
            native_lock.prepare_native_state_dir,
            Path("relative"),
            trusted_root=tmp_path,
        )
        == "state_dir_unsafe"
    )
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o700)
    os.chmod(parent, 0o700)
    assert (
        _lock_reason(
            native_lock.prepare_native_state_dir,
            parent / ".." / "escaped",
            trusted_root=tmp_path,
        )
        == "state_dir_unsafe"
    )
    assert not (tmp_path / "escaped").exists()


def test_prepare_creates_only_missing_private_state_components(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "existing"
    existing.mkdir(mode=0o700)
    os.chmod(existing, 0o755)
    state = existing / "intermediate" / "ciaobot"

    with native_lock.prepare_native_state_dir(state, trusted_root=tmp_path) as prepared:
        assert prepared.path == state
    # The existing component is judged, never chmod'ed.
    assert stat.S_IMODE(os.lstat(existing).st_mode) == 0o755
    intermediate = existing / "intermediate"
    assert stat.S_IMODE(os.lstat(intermediate).st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(state).st_mode) == 0o700
    assert os.lstat(state).st_uid == os.geteuid()


def test_prepare_then_acquire_works_on_a_fresh_chain(tmp_path: Path) -> None:
    state = tmp_path / "fresh" / "ciaobot"
    assert not state.exists()
    prepared = native_lock.prepare_native_state_dir(state, trusted_root=tmp_path)
    assert prepared.path == state
    lock = NativeRunLock.acquire(prepared, run_id=RUN_ID)
    lock.release()
    assert not _lock_path(state).exists()


def test_prepare_is_a_noop_for_an_existing_private_state_dir(
    tmp_path: Path,
) -> None:
    state = _state_dir(tmp_path)
    before = os.lstat(state)
    with native_lock.prepare_native_state_dir(state, trusted_root=tmp_path) as prepared:
        assert prepared.path == state
    after = os.lstat(state)
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
    assert after.st_mtime_ns == before.st_mtime_ns
    assert stat.S_IMODE(after.st_mode) == 0o700


def test_prepare_refuses_an_existing_unprivate_state_dir_without_repair(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    os.chmod(state, 0o755)
    assert (
        _lock_reason(
            native_lock.prepare_native_state_dir,
            state,
            trusted_root=tmp_path,
        )
        == "state_dir_unsafe"
    )
    assert stat.S_IMODE(os.lstat(state).st_mode) == 0o755


def test_prepare_refuses_a_symlinked_or_writable_ancestor(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.mkdir(mode=0o700)
    os.chmod(target, 0o700)
    link = tmp_path / "link"
    os.symlink(target, link)
    assert (
        _lock_reason(
            native_lock.prepare_native_state_dir,
            link / "state",
            trusted_root=tmp_path,
        )
        == "state_dir_unsafe"
    )
    assert not (target / "state").exists()

    writable = tmp_path / "writable"
    writable.mkdir(mode=0o700)
    os.chmod(writable, 0o777)
    assert (
        _lock_reason(
            native_lock.prepare_native_state_dir,
            writable / "state",
            trusted_root=tmp_path,
        )
        == "state_dir_unsafe"
    )
    assert not (writable / "state").exists()
    os.chmod(writable, 0o700)


def test_prepare_validates_the_whole_trusted_root_chain(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    real = root / "real"
    real.mkdir(mode=0o700)
    os.chmod(real, 0o700)
    state = real / "state"
    state.mkdir(mode=0o700)
    os.chmod(state, 0o700)
    link = root / "link"
    os.symlink(real, link)

    # Both of these follow the symlink on the old nearest-component walk.
    assert (link / "state").is_dir()
    assert (
        _lock_reason(
            native_lock.prepare_native_state_dir,
            link / "state",
            trusted_root=root,
        )
        == "state_dir_unsafe"
    )
    missing = link / "state" / "new"
    assert (
        _lock_reason(
            native_lock.prepare_native_state_dir,
            missing,
            trusted_root=root,
        )
        == "state_dir_unsafe"
    )
    assert not (state / "new").exists()


def test_prepare_refuses_a_target_outside_the_trusted_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    outside = tmp_path / "outside"
    assert (
        _lock_reason(
            native_lock.prepare_native_state_dir,
            outside,
            trusted_root=root,
        )
        == "state_dir_unsafe"
    )
    assert not outside.exists()


def test_prepare_refuses_a_symlinked_trusted_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    os.chmod(real, 0o700)
    link = tmp_path / "root"
    os.symlink(real, link)
    assert (
        _lock_reason(
            native_lock.prepare_native_state_dir,
            link / "state",
            trusted_root=link,
        )
        == "state_dir_unsafe"
    )
    assert not (real / "state").exists()


def test_prepare_defaults_to_home_as_trusted_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # A sibling of home, so it lies outside the default trusted root.
    target = home.parent / f"ciaobot-outside-{uuid.uuid4().hex}"
    assert (
        _lock_reason(native_lock.prepare_native_state_dir, target)
        == "state_dir_unsafe"
    )
    assert not target.exists()


def test_prepare_maps_fstat_failure_to_state_dir_unsafe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    state = root / "state"
    real_fstat = native_lock._fstat
    calls = 0

    def bad_fstat(fd: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.EIO, "fstat failed")
        return real_fstat(fd)

    monkeypatch.setattr(native_lock, "_fstat", bad_fstat)
    assert (
        _lock_reason(
            native_lock.prepare_native_state_dir, state, trusted_root=root
        )
        == "state_dir_unsafe"
    )
    assert state.is_dir()


def test_prepare_umask_failure_leaves_the_created_component(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    previous = os.umask(0o777)
    try:
        assert (
            _lock_reason(
                native_lock.prepare_native_state_dir,
                state,
                trusted_root=tmp_path,
            )
            == "state_dir_unsafe"
        )
    finally:
        os.umask(previous)
    assert state.is_dir()
    assert stat.S_IMODE(os.lstat(state).st_mode) == 0
    os.chmod(state, 0o700)
    state.rmdir()


def test_the_lock_records_run_identity_privately_and_releases(
    tmp_path: Path,
) -> None:
    state = _state_dir(tmp_path)
    sibling = state / "install-receipt.json"
    sibling.write_text("keep", encoding="utf-8")
    lock = NativeRunLock.acquire(_prepared(state), run_id=RUN_ID)
    try:
        assert lock.path == _lock_path(state)
        assert lock.record_path == _record_path(state)
        assert lock.run_id == RUN_ID
        directory = os.lstat(lock.path)
        record = os.lstat(lock.record_path)
        assert stat.S_ISDIR(directory.st_mode)
        assert stat.S_IMODE(directory.st_mode) == 0o700
        assert directory.st_uid == os.geteuid()
        assert stat.S_ISREG(record.st_mode)
        assert stat.S_IMODE(record.st_mode) == 0o600
        assert record.st_nlink == 1
        payload = json.loads(lock.record_path.read_text(encoding="utf-8"))
        assert payload == {
            "schema": native_lock.LOCK_SCHEMA_VERSION,
            "run_id": RUN_ID,
            "pid": os.getpid(),
        }
    finally:
        lock.release()
    assert not _lock_path(state).exists()
    assert sibling.read_text(encoding="utf-8") == "keep"
    lock.release()  # idempotent


def test_invalid_run_id_is_refused_before_creating_anything(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    for bad in (RUN_ID.upper(), "{" + RUN_ID + "}", "not-a-uuid"):
        assert (
            _lock_reason(NativeRunLock.acquire, _prepared(state), run_id=bad)
            == "invalid_run_id"
        )
    assert list(state.iterdir()) == []


def test_the_lock_refuses_a_non_native_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state_dir(tmp_path)
    prepared = _prepared(state)
    monkeypatch.setattr(native.sys, "platform", "win32")
    assert (
        _lock_reason(NativeRunLock.acquire, prepared, run_id=RUN_ID)
        == "unsupported_platform"
    )
    prepared.close()
    assert list(state.iterdir()) == []


def test_an_unsafe_state_directory_is_refused_at_preparation(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    os.chmod(state, 0o777)
    assert (
        _lock_reason(native_lock.prepare_native_state_dir, state, trusted_root=tmp_path)
        == "state_dir_unsafe"
    )
    os.chmod(state, 0o700)

    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    os.chmod(target, 0o700)
    link = tmp_path / "state-link"
    os.symlink(target, link)
    assert (
        _lock_reason(native_lock.prepare_native_state_dir, link, trusted_root=tmp_path)
        == "state_dir_unsafe"
    )

    regular = tmp_path / "state-file"
    regular.write_text("not a directory", encoding="utf-8")
    assert (
        _lock_reason(native_lock.prepare_native_state_dir, regular, trusted_root=tmp_path)
        == "state_dir_unsafe"
    )
    assert not _lock_path(state).exists()
    assert link.is_symlink()
    assert target.is_dir()
    assert regular.is_file()


def test_exclusive_acquisition_refuses_a_second_holder(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(_prepared(state), run_id=RUN_ID)
    try:
        record_before = _record_path(state).read_bytes()
        inode_before = os.lstat(_record_path(state)).st_ino
        assert (
            _lock_reason(
                NativeRunLock.acquire, _prepared(state), run_id=OTHER_RUN_ID
            )
            == "lock_busy"
        )
        assert _record_path(state).read_bytes() == record_before
        assert os.lstat(_record_path(state)).st_ino == inode_before
    finally:
        lock.release()
    assert not _lock_path(state).exists()


def test_acquire_umask_failure_leaves_the_lock_as_evidence(
    tmp_path: Path,
) -> None:
    state = _state_dir(tmp_path)
    previous = os.umask(0o777)
    try:
        assert (
            _lock_reason(NativeRunLock.acquire, _prepared(state), run_id=RUN_ID)
            == "lock_create_failed"
        )
    finally:
        os.umask(previous)
    lock_dir = _lock_path(state)
    assert lock_dir.is_dir()
    assert stat.S_IMODE(os.lstat(lock_dir).st_mode) == 0
    os.chmod(lock_dir, 0o700)
    assert list(lock_dir.iterdir()) == []
    lock_dir.rmdir()


def test_concurrent_threads_cannot_both_acquire(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    barrier = threading.Barrier(2)
    hold = threading.Event()
    results: list[tuple[str, str]] = []

    def attempt() -> None:
        barrier.wait(timeout=5)
        try:
            lock = NativeRunLock.acquire(_prepared(state), run_id=RUN_ID)
        except NativeLockError as exc:
            results.append(("refused", exc.reason))
            return
        results.append(("acquired", lock.run_id))
        try:
            hold.wait(timeout=5)
        finally:
            lock.release()

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    try:
        deadline = time.monotonic() + 5
        while len(results) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(results) == 2
        acquired = [result for result in results if result[0] == "acquired"]
        refused = [result for result in results if result[0] == "refused"]
        assert len(acquired) == 1
        assert len(refused) == 1
        assert refused[0][1] in {"lock_busy", "lock_unknown"}
    finally:
        hold.set()
        for thread in threads:
            thread.join(timeout=10)
    assert not _lock_path(state).exists()


def test_two_real_processes_race_and_exactly_one_acquires(
    tmp_path: Path,
) -> None:
    state = _state_dir(tmp_path)
    root = Path(__file__).resolve().parents[1]
    start = state / "start"
    release = state / "release"
    child = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from ciao.native_lock import NativeLockError, NativeRunLock, prepare_native_state_dir\n"
        "state = Path(sys.argv[1])\n"
        "run_id = sys.argv[2]\n"
        "start = Path(sys.argv[3])\n"
        "release = Path(sys.argv[4])\n"
        "while not start.exists():\n"
        "    time.sleep(0.01)\n"
        "try:\n"
        "    lock = NativeRunLock.acquire(prepare_native_state_dir(state, trusted_root=state.parent), run_id=run_id)\n"
        "except NativeLockError as exc:\n"
        "    print('refused', exc.reason, flush=True)\n"
        "    release.touch()\n"
        "    raise SystemExit(7)\n"
        "print('acquired', flush=True)\n"
        "deadline = time.monotonic() + 10\n"
        "while not release.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "lock.release()\n"
        "raise SystemExit(0)\n"
    )
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                child,
                str(state),
                run_id,
                str(start),
                str(release),
            ],
            cwd=root,
            env={**os.environ, "PYTHONPATH": str(root)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for run_id in (RUN_ID, OTHER_RUN_ID)
    ]
    try:
        start.touch()
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            results.append((process.returncode, stdout, stderr))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    returncodes = sorted(result[0] for result in results)
    assert returncodes == [0, 7], results
    lines = [result[1].strip() for result in results]
    assert sum(line.startswith(b"acquired") for line in lines) == 1, lines
    refused = [line for line in lines if line.startswith(b"refused")][0]
    assert refused.split()[1] in {b"lock_busy", b"lock_unknown"}
    assert not _lock_path(state).exists()


def test_a_competing_process_is_refused_then_can_acquire_after_release(
    tmp_path: Path,
) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(_prepared(state), run_id=RUN_ID)
    try:
        refused = _run_lock_child(state, OTHER_RUN_ID)
        assert refused.returncode == 7, (refused.stdout, refused.stderr)
        assert refused.stdout.strip() == b"refused lock_busy"
        assert _lock_path(state).exists()
    finally:
        lock.release()

    acquired = _run_lock_child(state, OTHER_RUN_ID)
    assert acquired.returncode == 0, (acquired.stdout, acquired.stderr)
    assert acquired.stdout.splitlines() == [b"acquired", b"released"]
    assert not _lock_path(state).exists()


def test_a_dead_runs_valid_lock_is_busy_and_left_untouched(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    lock_dir = _make_lock_dir(state, OTHER_RUN_ID)
    record = lock_dir / native_lock.LOCK_RECORD_NAME
    before = (
        record.read_bytes(),
        os.lstat(record).st_ino,
        os.lstat(record).st_mtime_ns,
        os.lstat(lock_dir).st_ino,
    )
    assert (
        _lock_reason(NativeRunLock.acquire, _prepared(state), run_id=RUN_ID) == "lock_busy"
    )
    after = (
        record.read_bytes(),
        os.lstat(record).st_ino,
        os.lstat(record).st_mtime_ns,
        os.lstat(lock_dir).st_ino,
    )
    assert after == before


def test_a_crashed_process_leaves_a_busy_lock(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    root = Path(__file__).resolve().parents[1]
    child = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "from ciao.native_lock import NativeRunLock, prepare_native_state_dir\n"
        "lock = NativeRunLock.acquire(prepare_native_state_dir(Path(sys.argv[1]), trusted_root=Path(sys.argv[1]).parent), run_id=sys.argv[2])\n"
        "print('acquired', flush=True)\n"
        "os._exit(99)\n"
    )
    process = subprocess.run(
        [sys.executable, "-c", child, str(state), OTHER_RUN_ID],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True,
        timeout=30,
    )
    assert process.returncode == 99, (process.stdout, process.stderr)
    assert process.stdout.strip() == b"acquired"
    assert _lock_path(state).is_dir()
    payload = json.loads(_record_path(state).read_text(encoding="utf-8"))
    assert payload["run_id"] == OTHER_RUN_ID
    assert payload["pid"] != os.getpid()
    assert (
        _lock_reason(NativeRunLock.acquire, _prepared(state), run_id=RUN_ID) == "lock_busy"
    )
    assert _lock_path(state).is_dir()


def test_empty_and_partial_lock_directories_are_unknown_and_left_untouched(
    tmp_path: Path,
) -> None:
    state = _state_dir(tmp_path)
    lock_dir = _lock_path(state)
    lock_dir.mkdir(mode=0o700)
    os.chmod(lock_dir, 0o700)
    assert (
        _lock_reason(NativeRunLock.acquire, _prepared(state), run_id=RUN_ID)
        == "lock_unknown"
    )
    assert (
        _lock_reason(NativeRunLock.acquire, _prepared(state), run_id=RUN_ID)
        == "lock_unknown"
    )
    assert lock_dir.is_dir()
    assert list(lock_dir.iterdir()) == []

    lock_dir.rmdir()
    lock_dir = _make_lock_dir(state, OTHER_RUN_ID)
    record = lock_dir / native_lock.LOCK_RECORD_NAME
    record.write_bytes(b'{"schema":1,"run_id":"')
    os.chmod(record, 0o600)
    before = record.read_bytes()
    assert (
        _lock_reason(NativeRunLock.acquire, _prepared(state), run_id=RUN_ID)
        == "lock_unknown"
    )
    assert (
        _lock_reason(NativeRunLock.acquire, _prepared(state), run_id=RUN_ID)
        == "lock_unknown"
    )
    assert record.read_bytes() == before
    assert lock_dir.is_dir()


def test_a_lock_that_disappears_before_stat_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state_dir(tmp_path)
    lock_dir = _lock_path(state)
    lock_dir.mkdir(mode=0o700)
    os.chmod(lock_dir, 0o700)
    real_stat = native.os.stat

    def missing_stat(path: object, *args: object, **kwargs: object) -> object:
        if path == native_lock.LOCK_DIR_NAME:
            raise FileNotFoundError(errno.ENOENT, "gone")
        return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(native.os, "stat", missing_stat)
    assert (
        _lock_reason(NativeRunLock.acquire, _prepared(state), run_id=RUN_ID)
        == "lock_unknown"
    )
    assert lock_dir.is_dir()


def test_a_lock_that_disappears_before_open_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state_dir(tmp_path)
    lock_dir = _lock_path(state)
    lock_dir.mkdir(mode=0o700)
    os.chmod(lock_dir, 0o700)
    real_open = native.os.open

    def missing_open(path: object, *args: object, **kwargs: object) -> int:
        if path == native_lock.LOCK_DIR_NAME:
            raise FileNotFoundError(errno.ENOENT, "gone")
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(native.os, "open", missing_open)
    assert (
        _lock_reason(NativeRunLock.acquire, _prepared(state), run_id=RUN_ID)
        == "lock_unknown"
    )
    assert lock_dir.is_dir()


def test_a_record_that_disappears_before_stat_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state_dir(tmp_path)
    lock_dir = _make_lock_dir(state, OTHER_RUN_ID)
    real_stat = native.os.stat

    def missing_stat(path: object, *args: object, **kwargs: object) -> object:
        if path == native_lock.LOCK_RECORD_NAME:
            raise FileNotFoundError(errno.ENOENT, "gone")
        return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(native.os, "stat", missing_stat)
    assert (
        _lock_reason(NativeRunLock.acquire, _prepared(state), run_id=RUN_ID)
        == "lock_unknown"
    )
    assert lock_dir.is_dir()


def test_a_record_that_disappears_before_open_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state_dir(tmp_path)
    lock_dir = _make_lock_dir(state, OTHER_RUN_ID)
    real_open = native.os.open

    def missing_open(path: object, *args: object, **kwargs: object) -> int:
        if path == native_lock.LOCK_RECORD_NAME:
            raise FileNotFoundError(errno.ENOENT, "gone")
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(native.os, "open", missing_open)
    assert (
        _lock_reason(NativeRunLock.acquire, _prepared(state), run_id=RUN_ID)
        == "lock_unknown"
    )
    assert lock_dir.is_dir()


def test_classification_maps_fstat_failure_to_lock_unsafe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state_dir(tmp_path)
    lock_dir = _make_lock_dir(state, OTHER_RUN_ID)
    prepared = _prepared(state)
    real_fstat = native_lock._fstat
    calls = 0

    def bad_fstat(fd: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.EIO, "fstat failed")
        return real_fstat(fd)

    monkeypatch.setattr(native_lock, "_fstat", bad_fstat)
    assert (
        _lock_reason(NativeRunLock.acquire, prepared, run_id=RUN_ID)
        == "lock_unsafe"
    )
    assert lock_dir.is_dir()


def test_an_incompatible_record_is_unsafe_not_unknown(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    lock_dir = _make_lock_dir(state, OTHER_RUN_ID)
    record = lock_dir / native_lock.LOCK_RECORD_NAME
    record.write_text(
        json.dumps(
            {
                "schema": native_lock.LOCK_SCHEMA_VERSION + 1,
                "run_id": OTHER_RUN_ID,
                "pid": os.getpid(),
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(record, 0o600)
    assert (
        _lock_reason(NativeRunLock.acquire, _prepared(state), run_id=RUN_ID) == "lock_unsafe"
    )
    assert record.exists()


def test_an_existing_lock_directory_is_never_repaired(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    lock_dir = _make_lock_dir(state, OTHER_RUN_ID)
    os.chmod(lock_dir, 0o755)
    assert (
        _lock_reason(NativeRunLock.acquire, _prepared(state), run_id=RUN_ID) == "lock_unsafe"
    )
    assert stat.S_IMODE(os.lstat(lock_dir).st_mode) == 0o755


def test_an_existing_record_must_stay_owner_only(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    lock_dir = _make_lock_dir(state, OTHER_RUN_ID)
    record = lock_dir / native_lock.LOCK_RECORD_NAME
    os.chmod(record, 0o644)
    content = record.read_bytes()
    assert (
        _lock_reason(NativeRunLock.acquire, _prepared(state), run_id=RUN_ID) == "lock_unsafe"
    )
    assert stat.S_IMODE(os.lstat(record).st_mode) == 0o644
    assert record.read_bytes() == content


def test_a_symlinked_lock_directory_is_unsafe_and_left_alone(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    target = state / "target"
    target.mkdir(mode=0o700)
    os.chmod(target, 0o700)
    link = _lock_path(state)
    os.symlink(target, link)
    assert (
        _lock_reason(NativeRunLock.acquire, _prepared(state), run_id=RUN_ID) == "lock_unsafe"
    )
    assert link.is_symlink()
    assert target.is_dir()


def test_a_hard_linked_record_is_unsafe_and_blocks_cleanup(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(_prepared(state), run_id=RUN_ID)
    link = state / "record-link"
    os.link(lock.record_path, link)
    assert (
        _lock_reason(NativeRunLock.acquire, _prepared(state), run_id=OTHER_RUN_ID)
        == "lock_unsafe"
    )
    assert _lock_reason(lock.release) == "lock_cleanup_refused"
    assert lock.record_path.exists()
    assert link.exists()
    assert _lock_path(state).exists()


def test_a_replaced_record_is_not_removed(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(_prepared(state), run_id=RUN_ID)
    original_inode = os.lstat(lock.record_path).st_ino
    # Create the substitute while the original still exists, then rename it over
    # the record: unlink-then-create lets Linux hand back the freed inode number.
    substitute = tmp_path / "substitute-record"
    descriptor = os.open(substitute, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, _record_bytes(RUN_ID))
    finally:
        os.close(descriptor)
    os.rename(substitute, lock.record_path)
    assert os.lstat(lock.record_path).st_ino != original_inode
    assert _lock_reason(lock.release) == "lock_cleanup_refused"
    assert lock.record_path.exists()
    assert _lock_path(state).exists()


def test_a_replaced_lock_directory_is_not_removed(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(_prepared(state), run_id=RUN_ID)
    original = state / "moved-lock"
    os.rename(lock.path, original)
    replacement = _make_lock_dir(state, OTHER_RUN_ID)
    assert replacement == lock.path
    assert _lock_reason(lock.release) == "lock_cleanup_refused"
    assert (original / native_lock.LOCK_RECORD_NAME).exists()
    assert (lock.path / native_lock.LOCK_RECORD_NAME).exists()


def test_a_symlinked_record_is_not_removed(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(_prepared(state), run_id=RUN_ID)
    target = state / "foreign.txt"
    target.write_text("foreign", encoding="utf-8")
    os.unlink(lock.record_path)
    os.symlink(target, lock.record_path)
    assert _lock_reason(lock.release) == "lock_cleanup_refused"
    assert lock.record_path.is_symlink()
    assert target.read_text(encoding="utf-8") == "foreign"
    assert _lock_path(state).exists()


def test_release_refuses_a_record_made_non_private(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(_prepared(state), run_id=RUN_ID)
    os.chmod(lock.record_path, 0o644)
    with pytest.raises(NativeLockError) as caught:
        lock.release()
    assert caught.value.reason == "lock_cleanup_refused"
    assert caught.value.cleanup == native_lock.LockCleanupReport(False, False)
    assert lock.record_path.exists()
    assert stat.S_IMODE(os.lstat(lock.record_path).st_mode) == 0o644
    assert _lock_path(state).is_dir()


def test_release_refuses_a_lock_dir_made_non_private(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(_prepared(state), run_id=RUN_ID)
    os.chmod(lock.path, 0o755)
    with pytest.raises(NativeLockError) as caught:
        lock.release()
    assert caught.value.reason == "lock_cleanup_refused"
    assert caught.value.cleanup == native_lock.LockCleanupReport(False, False)
    assert lock.record_path.exists()
    assert stat.S_IMODE(os.lstat(lock.path).st_mode) == 0o755


def test_release_maps_fstat_failure_to_cleanup_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(_prepared(state), run_id=RUN_ID)

    def bad_fstat(fd: int) -> os.stat_result:
        raise OSError(errno.EIO, "fstat failed")

    monkeypatch.setattr(native_lock, "_fstat", bad_fstat)
    with pytest.raises(NativeLockError) as caught:
        lock.release()
    assert caught.value.reason == "lock_cleanup_refused"
    assert caught.value.cleanup == native_lock.LockCleanupReport(False, False)
    assert lock.record_path.exists()
    assert _lock_path(state).is_dir()


def test_release_refuses_same_inode_same_size_record_mutation(
    tmp_path: Path,
) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(_prepared(state), run_id=RUN_ID)
    before_stat = os.lstat(lock.record_path)
    before = lock.record_path.read_bytes()
    index = before.index(b'"run_id":"') + len(b'"run_id":"')
    replacement = b"a" if before[index : index + 1] != b"a" else b"b"
    mutated = before[:index] + replacement + before[index + 1 :]
    assert len(mutated) == len(before)
    with open(lock.record_path, "r+b") as handle:
        handle.seek(0)
        handle.write(mutated)
    after_stat = os.lstat(lock.record_path)
    assert (after_stat.st_dev, after_stat.st_ino, after_stat.st_size) == (
        before_stat.st_dev,
        before_stat.st_ino,
        before_stat.st_size,
    )
    assert lock.record_path.read_bytes() == mutated
    with pytest.raises(NativeLockError) as caught:
        lock.release()
    assert caught.value.reason == "lock_cleanup_refused"
    assert caught.value.cleanup == native_lock.LockCleanupReport(False, False)
    assert lock.record_path.exists()
    assert _lock_path(state).is_dir()


def test_a_foreign_file_inside_the_lock_makes_cleanup_refuse(
    tmp_path: Path,
) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(_prepared(state), run_id=RUN_ID)
    foreign = lock.path / "foreign.txt"
    foreign.write_text("keep", encoding="utf-8")
    assert _lock_reason(lock.release) == "lock_cleanup_refused"
    assert foreign.read_text(encoding="utf-8") == "keep"
    assert lock.record_path.exists()
    assert _lock_path(state).exists()


def test_a_foreign_file_inside_an_existing_lock_is_unsafe(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    lock_dir = _make_lock_dir(state, OTHER_RUN_ID)
    foreign = lock_dir / "foreign.txt"
    foreign.write_text("keep", encoding="utf-8")
    assert (
        _lock_reason(NativeRunLock.acquire, _prepared(state), run_id=RUN_ID) == "lock_unsafe"
    )
    assert foreign.read_text(encoding="utf-8") == "keep"
    assert (lock_dir / native_lock.LOCK_RECORD_NAME).exists()


def test_a_record_write_failure_removes_only_the_new_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state_dir(tmp_path)
    sibling = state / "keep.txt"
    sibling.write_text("keep", encoding="utf-8")

    def explode(fd: int, data: bytes) -> None:
        raise OSError("boom")

    with monkeypatch.context() as patch:
        patch.setattr(native_lock, "_write_record", explode)
        assert (
            _lock_reason(NativeRunLock.acquire, _prepared(state), run_id=RUN_ID)
            == "lock_record_failed"
        )
    assert not _lock_path(state).exists()
    assert sibling.read_text(encoding="utf-8") == "keep"
    assert list(state.iterdir()) == [sibling]

    lock = NativeRunLock.acquire(_prepared(state), run_id=RUN_ID)
    lock.release()
    assert not _lock_path(state).exists()


def test_the_context_manager_releases_on_success_and_failure(
    tmp_path: Path,
) -> None:
    state = _state_dir(tmp_path)
    with NativeRunLock.acquire(_prepared(state), run_id=RUN_ID) as lock:
        assert lock.path.is_dir()
    assert not _lock_path(state).exists()

    with pytest.raises(RuntimeError):
        with NativeRunLock.acquire(_prepared(state), run_id=OTHER_RUN_ID):
            raise RuntimeError("body failed")
    assert not _lock_path(state).exists()


def test_release_refuses_same_thread_reentrancy_without_fd_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(_prepared(state), run_id=RUN_ID)
    original = lock._release_checked

    def reenter(parent_fd: int, dir_fd: int) -> None:
        lock.release()
        original(parent_fd, dir_fd)

    monkeypatch.setattr(lock, "_release_checked", reenter)
    assert _lock_reason(lock.release) == "lock_reentrant_release"
    assert _lock_path(state).is_dir()
    # The aborted attempt is not silently retried or reported as success.
    assert _lock_reason(lock.release) == "lock_cleanup_refused"
    assert _lock_path(state).is_dir()


def test_concurrent_release_calls_close_once_without_fd_reuse(
    tmp_path: Path,
) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(_prepared(state), run_id=RUN_ID)
    barrier = threading.Barrier(4)
    results: list[str] = []

    def attempt() -> None:
        barrier.wait(timeout=5)
        try:
            lock.release()
        except NativeLockError as exc:
            results.append(exc.reason)
        else:
            results.append("released")

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert results == ["released"] * 4
    assert not _lock_path(state).exists()


def test_an_interrupted_release_keeps_no_fabricated_cleanup_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(_prepared(state), run_id=RUN_ID)

    def interrupt(parent_fd: int, dir_fd: int) -> None:
        raise KeyboardInterrupt()

    monkeypatch.setattr(lock, "_release_checked", interrupt)
    with pytest.raises(KeyboardInterrupt):
        lock.release()
    with pytest.raises(NativeLockError) as caught:
        lock.release()
    assert caught.value.reason == "lock_cleanup_refused"
    assert caught.value.cleanup is None
    assert _lock_path(state).is_dir()


def test_release_after_record_unlink_and_rmdir_failure_is_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(_prepared(state), run_id=RUN_ID)
    real_rmdir = native.os.rmdir

    def fail_rmdir(*args: object, **kwargs: object) -> None:
        raise OSError("rmdir failed")

    monkeypatch.setattr(native.os, "rmdir", fail_rmdir)
    try:
        with pytest.raises(NativeLockError) as caught:
            lock.release()
        assert caught.value.reason == "lock_cleanup_refused"
        assert caught.value.cleanup == native_lock.LockCleanupReport(True, False)
    finally:
        monkeypatch.setattr(native.os, "rmdir", real_rmdir)
    # The record unlink already happened; the directory is the evidence.
    assert not lock.record_path.exists()
    assert _lock_path(state).is_dir()
    # A retry must not turn the partial cleanup into a quiet success, and it
    # keeps the same removal report for the coordinator.
    with pytest.raises(NativeLockError) as retry:
        lock.release()
    assert retry.value.reason == "lock_cleanup_refused"
    assert retry.value.cleanup == native_lock.LockCleanupReport(True, False)
    assert (
        _lock_reason(NativeRunLock.acquire, _prepared(state), run_id=OTHER_RUN_ID)
        == "lock_unknown"
    )


def test_context_manager_surfaces_body_and_cleanup_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state_dir(tmp_path)
    body_error = RuntimeError("body failed")

    def fail_rmdir(*args: object, **kwargs: object) -> None:
        raise OSError("rmdir failed")

    monkeypatch.setattr(native.os, "rmdir", fail_rmdir)
    with pytest.raises(NativeLockError) as caught:
        with NativeRunLock.acquire(_prepared(state), run_id=RUN_ID):
            raise body_error
    assert caught.value.reason == "lock_cleanup_refused"
    assert caught.value.__cause__ is body_error
    assert caught.value.cleanup == native_lock.LockCleanupReport(True, False)
    assert _lock_path(state).is_dir()
    assert not _record_path(state).exists()


def test_a_failed_acquire_surfaces_cleanup_and_original_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state_dir(tmp_path)

    def explode(fd: int, data: bytes) -> None:
        raise OSError("write failed")

    def no_cleanup(*args: object, **kwargs: object) -> native_lock.LockCleanupReport:
        return native_lock.LockCleanupReport(False, False)

    with monkeypatch.context() as patch:
        patch.setattr(native_lock, "_write_record", explode)
        patch.setattr(native_lock, "_remove_new_lock_best_effort", no_cleanup)
        with pytest.raises(NativeLockError) as caught:
            NativeRunLock.acquire(_prepared(state), run_id=RUN_ID)
    assert caught.value.reason == "lock_cleanup_refused"
    assert caught.value.original_reason == "lock_record_failed"
    assert caught.value.cleanup == native_lock.LockCleanupReport(False, False)
    assert isinstance(caught.value.__cause__, NativeLockError)
    assert caught.value.__cause__.reason == "lock_record_failed"
    assert _lock_path(state).is_dir()
    assert _record_path(state).is_file()
    assert (
        _lock_reason(NativeRunLock.acquire, _prepared(state), run_id=OTHER_RUN_ID)
        == "lock_unknown"
    )


# ── assert_held: the public held-lock predicate (#2340) ─────────────────────


def test_assert_held_accepts_the_unmodified_lock_and_changes_nothing(
    tmp_path: Path,
) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(_prepared(state), run_id=RUN_ID)
    try:
        record = _record_path(state)
        inode = record.stat().st_ino
        before = record.read_bytes()
        lock.assert_held(run_id=RUN_ID)
        assert record.stat().st_ino == inode
        assert record.read_bytes() == before
        assert _lock_reason(lock.assert_held, run_id=OTHER_RUN_ID) == "lock_run_mismatch"
    finally:
        lock.release()


def test_assert_held_refuses_a_released_lock(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(_prepared(state), run_id=RUN_ID)
    lock.release()

    assert _lock_reason(lock.assert_held, run_id=RUN_ID) == "lock_not_held"


def test_assert_held_refuses_changed_record_bytes_without_repairing_them(
    tmp_path: Path,
) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(_prepared(state), run_id=RUN_ID)
    record = _record_path(state)
    record.write_bytes(_record_bytes(OTHER_RUN_ID))
    changed = record.read_bytes()
    try:
        assert _lock_reason(lock.assert_held, run_id=RUN_ID) == "lock_changed"
        assert record.read_bytes() == changed
    finally:
        # The record was substituted, so release must refuse; the test owns the
        # isolated state directory and removes the evidence itself.
        with contextlib.suppress(NativeLockError):
            lock.release()
        shutil.rmtree(state, ignore_errors=True)


def test_assert_held_refuses_a_substituted_lock_directory(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(_prepared(state), run_id=RUN_ID)
    _lock_path(state).rename(state / "moved-lock")
    _lock_path(state).mkdir(mode=0o700)
    os.chmod(_lock_path(state), 0o700)
    try:
        assert _lock_reason(lock.assert_held, run_id=RUN_ID) == "lock_changed"
    finally:
        with contextlib.suppress(NativeLockError):
            lock.release()
        shutil.rmtree(state, ignore_errors=True)


# ── create_fresh_host: the native host slice (#2340) ────────────────────────


def _manifest_with(entry: dict[str, object]) -> dict[str, object]:
    return release_manifest.build_manifest(
        "1.2.3", [entry], created="2026-11-01T00:00:00+00:00"
    )


def _manifest_verifier(
    manifest: dict[str, object],
    *,
    expected_public_key: str = release_manifest.RELEASE_PUBLIC_KEY,
) -> native.ManifestVerifier:
    def verify(data: bytes, signature: str, public_key: str) -> dict[str, object]:
        assert data == b"signed-manifest-bytes"
        assert signature == "signed-manifest-signature"
        assert public_key == expected_public_key
        return manifest

    return verify


@contextlib.contextmanager
def _held_lock(tmp_path: Path) -> object:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(_prepared(state), run_id=RUN_ID)
    try:
        yield lock
    finally:
        with contextlib.suppress(NativeLockError):
            lock.release()


def _fresh_host(
    *,
    lock: NativeRunLock,
    archive: Path,
    entry: dict[str, object],
    runner: object,
    **overrides: object,
) -> native.FreshHostInstall:
    seams: dict[str, object] = {
        "verify_manifest_fn": _manifest_verifier(_manifest_with(entry)),
        "select_entry_fn": release_manifest.select_server_host_artifact,
        "verify_archive_fn": server_host_install.verify_host_archive,
        "install_fn": server_host_install.install_server_host,
        "verify_owned_fn": server_host.verify_owned_host,
    }
    seams.update(overrides)
    return native._create_fresh_host(
        run_lock=lock,
        run_id=RUN_ID,
        manifest_bytes=b"signed-manifest-bytes",
        signature_text="signed-manifest-signature",
        archive_path=archive,
        runner=runner,  # type: ignore[arg-type]
        **seams,  # type: ignore[arg-type]
    )


def _fresh_paths() -> tuple[Path, Path]:
    return server_host.default_bundle_path(), server_host.default_ownership_path()


def test_create_fresh_host_installs_only_a_new_verified_host(
    tmp_path: Path, home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target, record = _fresh_paths()
    runner = _FakeRunner()

    with _held_lock(tmp_path) as lock:
        result = _fresh_host(
            lock=lock,  # type: ignore[arg-type]
            archive=archive,
            entry=entry,
            runner=runner,
        )

    assert result.run_id == RUN_ID
    assert result.bundle_path == target
    assert result.ownership_path == record
    assert result.ownership == server_host.read_host_ownership(record)
    assert target.is_dir()
    assert (
        server_host.verify_owned_host(target, ownership_path=record, runner=runner)
        == result.ownership
    )
    # The only subprocesses are the fixed native codesign probes; nothing starts,
    # stops, bootstraps or quiesces a service.
    assert runner.commands
    assert all(command[0] == server_host.CODESIGN for command in runner.commands)
    joined = " ".join(" ".join(command) for command in runner.commands)
    for forbidden in ("launchctl", "bootout", "bootstrap", "/usr/bin/open", "stop"):
        assert forbidden not in joined


def test_create_fresh_host_refuses_a_same_policy_host_inserted_after_preflight(
    tmp_path: Path, home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target, record = _fresh_paths()
    runner = _FakeRunner()
    inserted: dict[str, object] = {}

    def insert_then_install(*args: object, **kwargs: object) -> object:
        # The helper's own preflight already saw both paths absent. A concurrent
        # same-policy installer wins the target before the real fresh call.
        server_host_install.install_server_host(archive, entry, runner=runner)
        inserted["inode"] = target.stat().st_ino
        inserted["record"] = record.read_bytes()
        return server_host_install.install_server_host(*args, **kwargs)

    with _held_lock(tmp_path) as lock:
        with pytest.raises(server_host_install.ServerHostInstallError) as caught:
            _fresh_host(
                lock=lock,  # type: ignore[arg-type]
                archive=archive,
                entry=entry,
                runner=runner,
                install_fn=insert_then_install,
            )

    assert caught.value.code == server_host_install.HOST_EXISTS
    assert target.stat().st_ino == inserted["inode"]
    assert record.read_bytes() == inserted["record"]
    assert server_host.read_host_ownership(record).bundle_path == os.path.realpath(target)


def test_create_fresh_host_refuses_an_orphan_record_or_journal_before_install(
    tmp_path: Path, home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target, record = _fresh_paths()
    runner = _FakeRunner()
    calls: list[object] = []

    def never_install(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        raise AssertionError("install must not run for pre-existing evidence")

    with _held_lock(tmp_path) as lock:
        # Record-only: a complete host was installed once, then its bundle was
        # removed. The record is partial state and stays byte-identical.
        server_host_install.install_server_host(archive, entry, runner=runner)
        record_bytes = record.read_bytes()
        shutil.rmtree(target)
        with pytest.raises(server_host_install.ServerHostInstallError) as caught:
            _fresh_host(
                lock=lock,  # type: ignore[arg-type]
                archive=archive,
                entry=entry,
                runner=runner,
                install_fn=never_install,
            )
        assert caught.value.code == server_host_install.HOST_EXISTS
        assert not os.path.lexists(target)
        assert record.read_bytes() == record_bytes
        assert calls == []

        # Journal-only: any journal name is evidence, even before it is read.
        record.unlink()
        journal = server_host_install.upgrade_journal_path()
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_bytes(b"journal evidence this run must not settle")
        with pytest.raises(server_host_install.ServerHostInstallError) as caught:
            _fresh_host(
                lock=lock,  # type: ignore[arg-type]
                archive=archive,
                entry=entry,
                runner=runner,
                install_fn=never_install,
            )
        assert caught.value.code == server_host_install.TRANSACTION_CONFLICT
        assert journal.read_bytes() == b"journal evidence this run must not settle"
        assert calls == []


def test_create_fresh_host_refuses_unsafe_bundle_or_record_links(
    tmp_path: Path, home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target, record = _fresh_paths()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target.parent.mkdir(parents=True)
    target.symlink_to(elsewhere, target_is_directory=True)

    calls: list[object] = []

    def never_install(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        raise AssertionError("install must not run for an unsafe target")

    with _held_lock(tmp_path) as lock:
        with pytest.raises(server_host_install.ServerHostInstallError) as caught:
            _fresh_host(
                lock=lock,  # type: ignore[arg-type]
                archive=archive,
                entry=entry,
                runner=_FakeRunner(),
                install_fn=never_install,
            )
        assert caught.value.code == server_host_install.TARGET_UNSAFE
        assert target.is_symlink()
        assert calls == []

        target.unlink()
        record.parent.mkdir(parents=True)
        record.symlink_to(elsewhere / "record.json")
        with pytest.raises(server_host_install.ServerHostInstallError) as caught:
            _fresh_host(
                lock=lock,  # type: ignore[arg-type]
                archive=archive,
                entry=entry,
                runner=_FakeRunner(),
                install_fn=never_install,
            )
        assert caught.value.code == server_host_install.HOST_EXISTS
        assert record.is_symlink()
        assert calls == []


def test_create_fresh_host_refuses_a_changed_postinstall_host(
    tmp_path: Path, home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target, record = _fresh_paths()
    runner = _FakeRunner()
    real_install = server_host_install.install_server_host

    def install_then_tamper(*args: object, **kwargs: object) -> object:
        snapshot = real_install(*args, **kwargs)  # type: ignore[arg-type]
        (target / "Contents" / "Resources" / server_host.ICON_NAME).write_bytes(
            b"changed after the install returned"
        )
        return snapshot

    with _held_lock(tmp_path) as lock:
        with pytest.raises(server_host.ServerHostError) as caught:
            _fresh_host(
                lock=lock,  # type: ignore[arg-type]
                archive=archive,
                entry=entry,
                runner=runner,
                install_fn=install_then_tamper,
            )

    assert caught.value.code in (server_host.NOT_OWNED, server_host.INSPECTION_FAILED)
    # The helper refuses but never deletes a changed host: a concurrent writer
    # may own it, and preserving the state is the retry evidence.
    assert target.is_dir()
    assert record.is_file()


def test_create_fresh_host_refuses_a_returned_and_verified_snapshot_mismatch(
    tmp_path: Path, home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target, record = _fresh_paths()
    runner = _FakeRunner()
    real_verify = server_host.verify_owned_host

    def verify_a_different_snapshot(*args: object, **kwargs: object) -> object:
        real = real_verify(*args, **kwargs)  # type: ignore[arg-type]
        return dataclasses.replace(real, executable_sha256="0" * 64)

    with _held_lock(tmp_path) as lock:
        with pytest.raises(server_host_install.ServerHostInstallError) as caught:
            _fresh_host(
                lock=lock,  # type: ignore[arg-type]
                archive=archive,
                entry=entry,
                runner=runner,
                verify_owned_fn=verify_a_different_snapshot,
            )

    assert caught.value.code == server_host.NOT_OWNED
    assert target.is_dir()
    assert record.is_file()


def test_create_fresh_host_refuses_manifest_failure_before_any_install(
    tmp_path: Path, home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    _archive, _bundle, _entry = _fixture(tmp_path)
    target, record = _fresh_paths()

    with _held_lock(tmp_path) as lock:
        with pytest.raises(release_manifest.SignatureError):
            native.create_fresh_host(
                run_lock=lock,
                run_id=RUN_ID,
                manifest_bytes=b"{}",
                signature_text="not a signature",
                archive_path=tmp_path / "unused.tar.gz",
                runner=_FakeRunner(),
            )
    assert not os.path.lexists(target)
    assert not os.path.lexists(record)


def test_create_fresh_host_refuses_a_manifest_with_no_host(
    tmp_path: Path, home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    target, record = _fresh_paths()
    wheel = {
        "filename": "ciaobot-1.2.3-py3-none-any.whl",
        "kind": "wheel",
        "platform": "any",
        "arch": "any",
        "sha256": "ab" * 32,
        "size": 1024,
    }
    manifest = release_manifest.build_manifest(
        "1.2.3", [wheel], created="2026-11-01T00:00:00+00:00"
    )
    calls: list[object] = []

    def never_install(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        raise AssertionError("install must not run without a host entry")

    with _held_lock(tmp_path) as lock:
        with pytest.raises(ValueError, match="no server host"):
            native._create_fresh_host(
                run_lock=lock,
                run_id=RUN_ID,
                manifest_bytes=b"signed-manifest-bytes",
                signature_text="signed-manifest-signature",
                archive_path=tmp_path / "unused.tar.gz",
                runner=_FakeRunner(),
                verify_manifest_fn=lambda _data, _sig, _key: manifest,
                select_entry_fn=release_manifest.select_server_host_artifact,
                verify_archive_fn=server_host_install.verify_host_archive,
                install_fn=never_install,
                verify_owned_fn=server_host.verify_owned_host,
            )
    assert calls == []
    assert not os.path.lexists(target)
    assert not os.path.lexists(record)


def test_create_fresh_host_requires_a_held_run_lock(
    tmp_path: Path, home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    _archive, _bundle, _entry = _fixture(tmp_path)
    target, record = _fresh_paths()

    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(_prepared(state), run_id=RUN_ID)
    lock.release()
    with pytest.raises(NativeLockError) as caught:
        native.create_fresh_host(
            run_lock=lock,
            run_id=RUN_ID,
            manifest_bytes=b"signed-manifest-bytes",
            signature_text="signed-manifest-signature",
            archive_path=tmp_path / "unused.tar.gz",
            runner=_FakeRunner(),
        )
    assert caught.value.reason == "lock_not_held"
    assert not os.path.lexists(target)
    assert not os.path.lexists(record)


def test_create_fresh_host_refuses_a_substituted_lock_record(
    tmp_path: Path, home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    _archive, _bundle, _entry = _fixture(tmp_path)
    target, record = _fresh_paths()

    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(_prepared(state), run_id=RUN_ID)
    _record_path(state).write_bytes(_record_bytes(OTHER_RUN_ID))
    try:
        with pytest.raises(NativeLockError) as caught:
            native.create_fresh_host(
                run_lock=lock,
                run_id=RUN_ID,
                manifest_bytes=b"signed-manifest-bytes",
                signature_text="signed-manifest-signature",
                archive_path=tmp_path / "unused.tar.gz",
                runner=_FakeRunner(),
            )
        assert caught.value.reason == "lock_changed"
        assert not os.path.lexists(target)
        assert not os.path.lexists(record)
    finally:
        with contextlib.suppress(NativeLockError):
            lock.release()
        shutil.rmtree(state, ignore_errors=True)


def test_create_fresh_host_refuses_a_changed_archive_before_install(
    tmp_path: Path, home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target, record = _fresh_paths()
    archive.write_bytes(b"not the bytes the signed entry selects")
    calls: list[object] = []

    def never_install(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        raise AssertionError("install must not run for a changed archive")

    with _held_lock(tmp_path) as lock:
        with pytest.raises(server_host_install.ServerHostInstallError) as caught:
            _fresh_host(
                lock=lock,  # type: ignore[arg-type]
                archive=archive,
                entry=entry,
                runner=_FakeRunner(),
                install_fn=never_install,
            )
    assert caught.value.code == server_host_install.ARCHIVE_MISMATCH
    assert calls == []
    assert not os.path.lexists(target)
    assert not os.path.lexists(record)


def test_assert_held_refuses_a_substituted_state_directory(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(_prepared(state), run_id=RUN_ID)
    moved = tmp_path / "moved-state"
    state.rename(moved)
    state.mkdir(mode=0o700)
    os.chmod(state, 0o700)
    try:
        assert _lock_reason(lock.assert_held, run_id=RUN_ID) == "lock_changed"
    finally:
        with contextlib.suppress(NativeLockError):
            lock.release()
        shutil.rmtree(moved, ignore_errors=True)
        shutil.rmtree(state, ignore_errors=True)


def test_create_fresh_host_refuses_if_the_run_lock_is_released_during_install(
    tmp_path: Path, home_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    target, record = _fresh_paths()
    runner = _FakeRunner()
    real_install = server_host_install.install_server_host

    with _held_lock(tmp_path) as lock:
        def install_then_release(*args: object, **kwargs: object) -> object:
            snapshot = real_install(*args, **kwargs)  # type: ignore[arg-type]
            lock.release()  # type: ignore[attr-defined]
            return snapshot

        with pytest.raises(NativeLockError) as caught:
            _fresh_host(
                lock=lock,  # type: ignore[arg-type]
                archive=archive,
                entry=entry,
                runner=runner,
                install_fn=install_then_release,
            )

    assert caught.value.reason == "lock_not_held"
    # The host was written before the lock was lost; it is preserved, not
    # reported as this run's success and not silently reinstalled.
    assert target.is_dir()
    assert record.is_file()


def test_create_fresh_host_public_api_has_no_path_or_key_seams() -> None:
    # The public helper is bound to the home-relative canonical host paths and the
    # release public key. Only the private implementation takes test doubles; a
    # caller cannot point it at another ownership record, bundle or key.
    parameters = inspect.signature(native.create_fresh_host).parameters
    for forbidden in ("public_key", "bundle_path", "ownership_path", "entry"):
        assert forbidden not in parameters
    assert "run_lock" in parameters
    assert "manifest_bytes" in parameters
    assert "signature_text" in parameters
    private = inspect.signature(native._create_fresh_host).parameters
    assert "verify_manifest_fn" in private
    assert "install_fn" in private


# ── coordinator lifecycle skeleton (#2340) ─────────────────────────────────


def _handoff(
    run_id: str = RUN_ID,
    *,
    port: int = 54321,
    workspace: Path | None = None,
) -> native_handoff.NativeHandoffResult:
    identity = native_handoff.InterpreterIdentity(
        device=1, inode=2, size=3, mtime_ns=4
    )
    return native_handoff.NativeHandoffResult(
        run_id=run_id,
        startup_id="a" * 32,
        workspace=Path("/tmp/native-workspace")
        if workspace is None
        else workspace,
        service_backend="macos",
        definition_path=Path("/tmp/native-definition.plist"),
        definition_sha256="0" * 64,
        engine_program="/tmp/python",
        engine_program_identity=identity,
        engine_python="/tmp/python",
        engine_python_identity=identity,
        environment={},
        port=port,
    )


def _ownership(tmp_path: Path) -> server_host.HostOwnership:
    return server_host.HostOwnership(
        schema=server_host.SCHEMA_VERSION,
        bundle_path=str(tmp_path / "host"),
        bundle_id=server_host.BUNDLE_ID,
        host_revision=server_host.HOST_REVISION,
        host_protocol=server_host.HOST_PROTOCOL,
        executable_sha256="0" * 64,
        per_arch_cdhashes={},
        bundle_files={},
    )


def _fresh_host_value(tmp_path: Path) -> native.FreshHostInstall:
    return native.FreshHostInstall(
        run_id=RUN_ID,
        ownership=_ownership(tmp_path),
        bundle_path=tmp_path / "host",
        ownership_path=tmp_path / "record",
    )


#: The file whose existence releases a default wizard child to exit 0.
WIZARD_RELEASE_NAME = "wizard-release"


def _bind_script(port: int = 54321) -> str:
    # After its bind record the child holds until the release sentinel (argv[2])
    # exists. The autouse fixture below creates it right after the real
    # listener-ownership check, so the check always sees a live child. The
    # give-up bound only ends a child that nothing releases.
    return (
        "import os, sys, time\n"
        "os.write(int(sys.argv[1]), b'{\"schema\":1,\"port\":%d}\\n')\n"
        "os.close(int(sys.argv[1]))\n"
        "give_up = time.monotonic() + 30\n"
        "while not os.path.exists(sys.argv[2]) and time.monotonic() < give_up:\n"
        "    time.sleep(0.01)\n" % port
    )


@pytest.fixture(autouse=True)
def _release_wizard_after_listener_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Run the real ownership check, then let a default wizard child exit."""
    real_owns_process = native._OwnedChild.owns_process
    release = tmp_path / WIZARD_RELEASE_NAME

    def owns_then_release(self: native._OwnedChild, pid: int) -> bool:
        result = real_owns_process(self, pid)
        release.touch()
        return result

    monkeypatch.setattr(native._OwnedChild, "owns_process", owns_then_release)


def _coordinator_deps(tmp_path: Path, **overrides: object) -> object:
    state = tmp_path / "state"
    values: dict[str, object] = {
        "classify": lambda workspace: NativeInstallState.fresh,
        "prepare_state_dir": lambda: _prepared(_state_dir(tmp_path)),
        "acquire_lock": lambda directory, run_id: NativeRunLock.acquire(
            directory, run_id=run_id
        ),
        "prepare_scratch": lambda: tmp_path / "scratch",
        "create_host": lambda lock, run_id, remaining: _fresh_host_value(tmp_path),
        "recheck_host": lambda host: None,
        "install_engine": lambda remaining: None,
        "wizard_command": lambda fd: (
            sys.executable,
            "-c",
            _bind_script(),
            str(fd),
            str(tmp_path / WIZARD_RELEASE_NAME),
        ),
        "read_handoff": lambda run_id: _handoff(run_id),
        "activate": lambda run_id, remaining: _handoff(run_id),
        "wizard_listener": lambda port, child_pid: frozenset({child_pid}),
        "prove_ready": lambda host, handoff, remaining: NativeReadinessProof(
            ready=False,
            failure="startup_status_invalid",
            port=handoff.port,
            version="0",
        ),
    }
    values.update(overrides)
    return native._CoordinatorDeps(**values)  # type: ignore[arg-type]


@contextlib.contextmanager
def _public_event_pipe():
    """A real pipe whose write end is in the coordinator's 3–9 public range.

    Pytest's fd capture usually holds every descriptor in 3–9. A free one is
    used when available; otherwise the highest public descriptor is borrowed
    for the duration of the call and restored afterwards. The coordinator still
    validates the real 3–9 boundary, so tests never relax it.
    """
    read_fd, write_fd = os.pipe()
    target: int | None = None
    saved: int | None = None
    saved_inheritable = False
    for candidate in range(native.MIN_EVENT_FD, native.MAX_EVENT_FD + 1):
        try:
            saved = os.dup(candidate)
        except OSError:
            target = candidate
            break
        else:
            os.close(saved)
            saved = None
    if target is None:
        target = native.MAX_EVENT_FD
        saved_inheritable = os.get_inheritable(target)
        saved = os.dup(target)
    os.dup2(write_fd, target)
    os.close(write_fd)
    try:
        yield read_fd, target
    finally:
        with contextlib.suppress(OSError):
            os.close(target)
        if saved is not None:
            os.dup2(saved, target, inheritable=saved_inheritable)
            os.close(saved)
        os.close(read_fd)


def _coordinator_request(
    events_fd: int,
    *,
    first_seq: int = 1,
    prior_events: int = 0,
    workspace: Path | None = None,
    expected_workspace: Path | None = None,
) -> native.NativeInstallRequest:
    return native.NativeInstallRequest(
        run_id=RUN_ID,
        events_fd=events_fd,
        first_seq=first_seq,
        prior_events=prior_events,
        workspace=workspace,
        expected_workspace=expected_workspace,
    )


def _run_coordinator(
    tmp_path: Path,
    *,
    deps: object | None = None,
    cancellation: native.NativeCancellation | None = None,
    policy: native._CoordinatorPolicy | None = None,
    workspace: Path | None = None,
    expected_workspace: Path | None = None,
    **overrides: object,
) -> tuple[native.NativeInstallOutcome, list[dict[str, object]], bytes]:
    with _public_event_pipe() as (read_fd, write_fd):
        kwargs: dict[str, object] = {}
        if cancellation is not None:
            kwargs["cancellation"] = cancellation
        if policy is not None:
            kwargs["policy"] = policy
        outcome = native._run_native_install(
            _coordinator_request(
                write_fd,
                workspace=workspace,
                expected_workspace=expected_workspace,
            ),
            deps=_coordinator_deps(tmp_path, **overrides)
            if deps is None
            else deps,
            **kwargs,  # type: ignore[arg-type]
        )
        raw = _drain(read_fd)
    events = [json.loads(line) for line in raw.splitlines()]
    return outcome, events, raw


def test_coordinator_runs_the_fresh_lifecycle_without_claiming_service_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    order: list[str] = []
    held: list[NativeRunLock] = []

    def create_host(
        lock: NativeRunLock, run_id: str, remaining: float
    ) -> native.FreshHostInstall:
        assert run_id == RUN_ID
        assert remaining > 0
        lock.assert_held(run_id=RUN_ID)
        held.append(lock)
        order.append("host")
        return _fresh_host_value(tmp_path)

    def recheck_host(host: native.FreshHostInstall) -> None:
        held[0].assert_held(run_id=RUN_ID)
        order.append("recheck")

    def install_engine(remaining: float) -> None:
        assert remaining > 0
        held[0].assert_held(run_id=RUN_ID)
        order.append("engine")

    outcome, events, raw = _run_coordinator(
        tmp_path,
        create_host=create_host,
        recheck_host=recheck_host,
        install_engine=install_engine,
    )

    assert order == ["host", "recheck", "engine"]
    assert outcome.terminal == "failed"
    assert outcome.failure_code == "service_not_ready"
    assert outcome.exit_code == 1
    assert outcome.host_step_attempted is True
    assert outcome.host_step_completed is True
    assert outcome.activation_attempted is True
    assert outcome.activation_completed is True
    assert outcome.service_may_be_running is True
    assert outcome.selected_workspace == _handoff().workspace
    assert outcome.lock_retained is False
    assert not _lock_path(tmp_path / "state").exists()
    assert b"service_ready" not in raw
    types = [event["type"] for event in events]
    assert types == [
        "phase",
        "phase",
        "wizard_ready",
        "setup_saved",
        "phase",
        "phase",
        "failed",
    ]
    assert [event["seq"] for event in events] == list(range(1, 8))
    assert events[2]["origin"] == "http://127.0.0.1:54321"
    assert events[-1] == {
        "schema": 1,
        "run_id": RUN_ID,
        "seq": 7,
        "type": "failed",
        "code": "service_not_ready",
    }
    assert [
        event["phase"] for event in events if event["type"] == "phase"
    ] == ["install", "wizard", "activation", "readiness"]
    assert all(
        len(line) + 1 <= native.MAX_EVENT_BYTES for line in raw.splitlines()
    )


@pytest.mark.parametrize(
    ("state", "code"),
    [
        (NativeInstallState.configured, "existing_install"),
        (NativeInstallState.partial, "partial_install"),
        (NativeInstallState.unknown, "unknown_state"),
    ],
)
def test_a_non_fresh_classification_refuses_before_any_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: NativeInstallState,
    code: str,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("the coordinator mutated after a non-fresh answer")

    outcome, events, _raw = _run_coordinator(
        tmp_path,
        classify=lambda workspace: state,
        prepare_state_dir=forbidden,
        acquire_lock=forbidden,
        prepare_scratch=forbidden,
        create_host=forbidden,
        install_engine=forbidden,
        wizard_command=forbidden,
    )

    assert outcome.failure_code == code
    assert outcome.host_step_attempted is False
    assert outcome.host_step_completed is False
    assert outcome.activation_attempted is False
    assert outcome.activation_completed is False
    assert [event["type"] for event in events] == ["failed"]
    assert not (tmp_path / "state").exists()


def test_an_unreadable_classification_is_unknown_and_never_echoed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    sentinel = "sk-live-CLASSIFIER-SECRET-0123456789"

    def explode(workspace: Path | None) -> NativeInstallState:
        raise RuntimeError(f"{sentinel} at /Users/someone/private")

    outcome, events, raw = _run_coordinator(tmp_path, classify=explode)

    assert outcome.failure_code == "unknown_state"
    assert sentinel.encode() not in raw
    assert b"/Users/someone/private" not in raw
    assert [event["type"] for event in events] == ["failed"]


def test_a_stale_lock_refuses_before_tool_install_and_preserves_partial_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    state = _state_dir(tmp_path)
    partial = state / "partial-artifact"
    partial.write_text("left by an earlier run", encoding="utf-8")
    stale = NativeRunLock.acquire(_prepared(state), run_id=OTHER_RUN_ID)
    try:
        before = _record_path(state).read_bytes()
        outcome, events, _raw = _run_coordinator(
            tmp_path,
            prepare_state_dir=lambda: _prepared(state),
            create_host=lambda *args: (_ for _ in ()).throw(
                AssertionError("the host step ran over a held lock")
            ),
            install_engine=lambda remaining: (_ for _ in ()).throw(
                AssertionError("the engine install ran over a held lock")
            ),
            wizard_command=lambda fd: (_ for _ in ()).throw(
                AssertionError("the wizard started over a held lock")
            ),
        )
        assert outcome.failure_code == "install_in_progress"
        assert outcome.host_step_attempted is False
        assert outcome.host_step_completed is False
        assert _lock_path(state).is_dir()
        assert _record_path(state).read_bytes() == before
        assert partial.read_text(encoding="utf-8") == "left by an earlier run"
        assert [event["type"] for event in events] == ["failed"]
        assert events[-1]["code"] == "install_in_progress"
    finally:
        stale.release()


def test_a_changed_host_refuses_before_engine_install_and_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    artifact = tmp_path / "host-artifact"
    artifact.write_text("partially installed host", encoding="utf-8")
    engine_calls: list[float] = []

    def changed(host: native.FreshHostInstall) -> None:
        raise native.NativeCoordinatorError("host_changed", "install_failed")

    outcome, events, _raw = _run_coordinator(
        tmp_path,
        recheck_host=changed,
        install_engine=lambda remaining: engine_calls.append(remaining),
    )

    assert outcome.failure_code == "install_failed"
    assert outcome.host_step_attempted is True
    assert outcome.host_step_completed is True
    assert outcome.activation_attempted is False
    assert outcome.activation_completed is False
    assert engine_calls == []
    assert artifact.read_text(encoding="utf-8") == "partially installed host"
    assert not _lock_path(tmp_path / "state").exists()
    assert [event["type"] for event in events] == ["phase", "failed"]
    assert "wizard" not in [
        event.get("phase") for event in events if event["type"] == "phase"
    ]


def test_cancellation_kills_and_reaps_only_the_owned_child_before_unlocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    cancellation = native.NativeCancellation()
    pids: list[int] = []
    spawned = threading.Event()
    real_spawn = native._spawn_owned_child

    def recording_spawn(*args: object, **kwargs: object) -> object:
        child = real_spawn(*args, **kwargs)  # type: ignore[arg-type]
        pids.append(child.pid)
        spawned.set()
        return child

    monkeypatch.setattr(native, "_spawn_owned_child", recording_spawn)
    stubborn = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(60)\n"
    )
    policy = native._CoordinatorPolicy(
        bind_timeout=5.0,
        term_grace=0.2,
        reap_timeout=0.5,
        poll_interval=0.02,
    )

    def cancel_when_spawned() -> None:
        assert spawned.wait(timeout=5)
        time.sleep(0.05)
        cancellation.cancel()

    canceller = threading.Thread(target=cancel_when_spawned, daemon=True)
    canceller.start()
    started = time.monotonic()
    try:
        outcome, events, _raw = _run_coordinator(
            tmp_path,
            wizard_command=lambda fd: (sys.executable, "-c", stubborn),
            cancellation=cancellation,
            policy=policy,
        )
    finally:
        canceller.join(timeout=5)
    elapsed = time.monotonic() - started

    assert pids
    pid = pids[0]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    # SIGTERM was ignored, so the coordinator used its finite reap window and
    # SIGKILL, then waited before releasing the lock.
    assert elapsed < 3.0
    assert outcome.failure_code == "cancelled"
    assert outcome.terminal == "failed"
    assert outcome.lock_retained is False
    assert events[-1]["code"] == "cancelled"
    assert not _lock_path(tmp_path / "state").exists()


def test_cancellation_after_activation_never_stops_or_rolls_back_the_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    cancellation = native.NativeCancellation()

    def activate(
        run_id: str, remaining: float
    ) -> native_handoff.NativeHandoffResult:
        cancellation.cancel()
        return _handoff(run_id)

    outcome, events, raw = _run_coordinator(
        tmp_path,
        activate=activate,
        cancellation=cancellation,
    )

    assert outcome.activation_attempted is True
    assert outcome.activation_completed is True
    assert outcome.service_may_be_running is True
    assert outcome.failure_code == "cancelled"
    assert outcome.terminal == "failed"
    assert b"service_ready" not in raw
    assert events[-1]["type"] == "failed"
    assert events[-1]["code"] == "cancelled"
    assert not _lock_path(tmp_path / "state").exists()
    fields = set(native._CoordinatorDeps.__dataclass_fields__)
    assert not fields & {"stop", "stop_service", "reload", "quiesce", "rollback"}


def test_the_public_event_fd_is_never_inherited_by_the_wizard_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    child_fds_path = tmp_path / "child-fds.json"
    notification_fds: list[int] = []
    script = (
        "import json, os, sys\n"
        "with open(sys.argv[2], 'w', encoding='utf-8') as handle:\n"
        "    json.dump(sorted(int(name) for name in os.listdir('/dev/fd')), handle)\n"
        "os.write(int(sys.argv[1]), b'{\"schema\":1,\"port\":54321}\\n')\n"
        "os.close(int(sys.argv[1]))\n"
    )

    def wizard_command(fd: int) -> tuple[str, ...]:
        notification_fds.append(fd)
        return (sys.executable, "-c", script, str(fd), str(child_fds_path))

    with _public_event_pipe() as (read_fd, write_fd):
        outcome = native._run_native_install(
            _coordinator_request(write_fd),
            deps=_coordinator_deps(
                tmp_path, wizard_command=wizard_command
            ),
        )
        _drain(read_fd)

    assert outcome.failure_code == "service_not_ready"
    child_fds = json.loads(child_fds_path.read_text(encoding="utf-8"))
    assert notification_fds
    assert notification_fds[0] in child_fds
    assert write_fd not in child_fds


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json\n",
        b'{"schema":1,"port":54321,"extra":1}\n',
        b'{"schema":1,"port":0}\n',
        b'{"schema":1,"port":54321,"port":54322}\n',
        b'{"schema":1,"port":54321}\n{"schema":1,"port":54322}\n',
        b"x" * 200,
    ],
)
def test_malformed_private_bind_records_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    script = (
        "import os, sys\n"
        f"os.write(int(sys.argv[1]), {payload!r})\n"
        "os.close(int(sys.argv[1]))\n"
    )
    outcome, events, _raw = _run_coordinator(
        tmp_path,
        wizard_command=lambda fd: (sys.executable, "-c", script, str(fd)),
    )

    assert outcome.failure_code == "wizard_failed"
    assert outcome.activation_attempted is False
    assert outcome.activation_completed is False
    assert "wizard_ready" not in [event["type"] for event in events]
    assert "setup_saved" not in [event["type"] for event in events]
    assert events[-1]["type"] == "failed"
    assert events[-1]["code"] == "wizard_failed"


def test_a_wizard_that_exits_without_binding_is_a_fixed_wizard_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    script = "import os, sys\nos.close(int(sys.argv[1]))\n"
    outcome, events, _raw = _run_coordinator(
        tmp_path,
        wizard_command=lambda fd: (sys.executable, "-c", script, str(fd)),
    )

    assert outcome.failure_code == "wizard_failed"
    assert "wizard_ready" not in [event["type"] for event in events]
    assert events[-1]["code"] == "wizard_failed"


def test_a_failed_engine_install_is_fixed_and_preserves_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    sentinel = "sk-live-ENGINE-SECRET-0123456789"
    host_marker = tmp_path / "host-marker"
    host_marker.write_text("present", encoding="utf-8")

    def fail_install(remaining: float) -> None:
        raise RuntimeError(f"{sentinel} /Users/someone/private")

    outcome, events, raw = _run_coordinator(
        tmp_path, install_engine=fail_install
    )

    assert outcome.failure_code == "install_failed"
    assert outcome.host_step_attempted is True
    assert outcome.host_step_completed is True
    assert outcome.activation_attempted is False
    assert outcome.activation_completed is False
    assert sentinel.encode() not in raw
    assert b"/Users/someone/private" not in raw
    assert host_marker.read_text(encoding="utf-8") == "present"
    assert not _lock_path(tmp_path / "state").exists()
    assert [event["type"] for event in events] == ["phase", "failed"]
    assert events[-1]["code"] == "install_failed"


def test_invalid_requests_are_refused_before_the_descriptor_is_used(
    tmp_path: Path,
) -> None:
    with _public_event_pipe() as (read_fd, event_fd):
        requests = [
            native.NativeInstallRequest(
                run_id="not-a-uuid",
                events_fd=event_fd,
                first_seq=1,
                prior_events=0,
            ),
            native.NativeInstallRequest(
                run_id=RUN_ID, events_fd=2, first_seq=1, prior_events=0
            ),
            native.NativeInstallRequest(
                run_id=RUN_ID, events_fd=-1, first_seq=1, prior_events=0
            ),
            native.NativeInstallRequest(
                run_id=RUN_ID, events_fd=10, first_seq=1, prior_events=0
            ),
            native.NativeInstallRequest(
                run_id=RUN_ID, events_fd=event_fd, first_seq=0, prior_events=0
            ),
            native.NativeInstallRequest(
                run_id=RUN_ID,
                events_fd=event_fd,
                first_seq=1,
                prior_events=native.MAX_EVENTS,
            ),
            native.NativeInstallRequest(
                run_id=RUN_ID, events_fd=event_fd, first_seq=3, prior_events=5
            ),
            native.NativeInstallRequest(
                run_id=RUN_ID,
                events_fd=event_fd,
                first_seq=27,
                prior_events=26,
            ),
            native.NativeInstallRequest(
                run_id=RUN_ID,
                events_fd=event_fd,
                first_seq=1,
                prior_events=0,
                workspace=Path("relative"),
            ),
            native.NativeInstallRequest(
                run_id=RUN_ID,
                events_fd=event_fd,
                first_seq=1,
                prior_events=0,
                expected_workspace=Path("relative"),
            ),
        ]
        for request in requests:
            outcome = native._run_native_install(
                request, deps=_coordinator_deps(tmp_path)
            )
            assert outcome.failure_code == "invalid_arguments"
            assert outcome.terminal is None
        assert _available(read_fd) == b""


def test_the_public_entry_refuses_before_touching_the_descriptor() -> None:
    with _public_event_pipe() as (read_fd, write_fd):
        with pytest.raises(native.NativeCoordinatorError) as caught:
            native.run_native_install(_coordinator_request(write_fd))
        assert caught.value.reason == "coordinator_unavailable"
        assert caught.value.failure_code == "internal_error"
        os.fstat(write_fd)  # the public entry did not take ownership
        assert _available(read_fd) == b""


def test_the_public_entry_validates_the_public_fd_range() -> None:
    with _public_event_pipe() as (_read_fd, _write_fd):
        request = native.NativeInstallRequest(
            run_id=RUN_ID,
            events_fd=10,
            first_seq=1,
            prior_events=0,
        )
        with pytest.raises(native.NativeCoordinatorError) as caught:
            native.run_native_install(request)
        assert caught.value.reason == "invalid_events_fd"
        assert caught.value.failure_code == "invalid_arguments"


def test_sigkill_leaves_the_lock_and_the_owned_child_as_evidence(
    tmp_path: Path,
) -> None:
    state = _state_dir(tmp_path)
    root = Path(__file__).resolve().parents[1]
    script = (
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "from ciao.native_lock import NativeRunLock, prepare_native_state_dir\n"
        "lock = NativeRunLock.acquire(prepare_native_state_dir(Path(sys.argv[1]), trusted_root=Path(sys.argv[1]).parent), run_id=sys.argv[2])\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
        "    start_new_session=True,\n"
        ")\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(state), RUN_ID],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        stdout=subprocess.PIPE,
    )
    child_pid = 0
    try:
        assert process.stdout is not None
        line = process.stdout.readline()
        child_pid = int(line.strip())
        assert _lock_path(state).is_dir()
        process.kill()
        process.wait(timeout=10)
        # SIGKILL ran no cleanup: the lock and the orphaned child remain.
        assert _lock_path(state).is_dir()
        os.kill(child_pid, 0)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        if child_pid:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(child_pid, signal.SIGKILL)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.02)
    assert _lock_path(state).is_dir()


def test_the_coordinator_creates_the_host_with_the_real_fresh_helper(
    tmp_path: Path,
    home_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    archive, _bundle, entry = _fixture(tmp_path)
    runner = _FakeRunner()
    target = server_host.default_bundle_path()
    record = server_host.default_ownership_path()

    def create_host(
        lock: NativeRunLock, run_id: str, remaining: float
    ) -> native.FreshHostInstall:
        assert run_id == RUN_ID
        lock.assert_held(run_id=RUN_ID)
        return _fresh_host(lock=lock, archive=archive, entry=entry, runner=runner)

    def recheck_host(host: native.FreshHostInstall) -> None:
        assert host.bundle_path == target
        assert host.ownership_path == record
        assert (
            server_host.verify_owned_host(
                host.bundle_path,
                ownership_path=host.ownership_path,
                runner=runner,
            )
            == host.ownership
        )

    outcome, events, raw = _run_coordinator(
        tmp_path,
        create_host=create_host,
        recheck_host=recheck_host,
    )

    assert outcome.host_step_attempted is True
    assert outcome.host_step_completed is True
    assert outcome.activation_attempted is True
    assert outcome.activation_completed is True
    assert outcome.failure_code == "service_not_ready"
    assert target.is_dir()
    assert record.is_file()
    assert not _lock_path(tmp_path / "state").exists()
    assert b"service_ready" not in raw
    assert "wizard_ready" in [event["type"] for event in events]
    commands = " ".join(" ".join(command) for command in runner.commands)
    for forbidden in ("launchctl", "bootout", "bootstrap", "/usr/bin/open", "stop"):
        assert forbidden not in commands


def test_a_second_preflight_refuses_a_service_that_appeared_before_host_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    answers = iter(
        [NativeInstallState.fresh, NativeInstallState.configured]
    )
    host_calls: list[object] = []

    def classify(workspace: Path | None) -> NativeInstallState:
        return next(answers)

    def create_host(*args: object) -> native.FreshHostInstall:
        host_calls.append(args)
        return _fresh_host_value(tmp_path)

    outcome, events, _raw = _run_coordinator(
        tmp_path, classify=classify, create_host=create_host
    )

    assert outcome.failure_code == "existing_install"
    assert outcome.host_step_attempted is False
    assert outcome.host_step_completed is False
    assert host_calls == []
    assert not _lock_path(tmp_path / "state").exists()
    assert [event["type"] for event in events] == ["failed"]
    assert events[-1]["code"] == "existing_install"


@pytest.mark.parametrize(
    "value",
    [0, -1, float("inf"), float("nan"), native.COORDINATOR_MAX_TIMEOUT_SECONDS + 1],
)
def test_every_coordinator_deadline_is_finite_and_bounded(value: float) -> None:
    with pytest.raises(native.NativeCoordinatorError) as caught:
        native._CoordinatorPolicy(run_timeout=value)
    assert caught.value.reason == "invalid_timeout"
    assert caught.value.failure_code == "internal_error"


def test_an_expired_run_deadline_refuses_before_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    def slow_classify(workspace: Path | None) -> NativeInstallState:
        time.sleep(0.02)
        return NativeInstallState.fresh

    policy = native._CoordinatorPolicy(
        run_timeout=0.01, poll_interval=0.005
    )
    outcome, events, _raw = _run_coordinator(
        tmp_path,
        classify=slow_classify,
        policy=policy,
    )

    assert outcome.failure_code == "internal_error"
    assert outcome.host_step_attempted is False
    assert outcome.host_step_completed is False
    assert not (tmp_path / "state").exists()
    assert [event["type"] for event in events] == ["failed"]


def test_the_public_coordinator_exposes_no_injection_seams() -> None:
    parameters = inspect.signature(native.run_native_install).parameters
    assert list(parameters) == ["request"]
    assert not {
        "deps",
        "classify",
        "create_host",
        "install_engine",
        "wizard_command",
        "activate",
    } & set(parameters)


def test_a_bind_timeout_is_a_fixed_wizard_failure_and_reaps_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    pids: list[int] = []
    real_spawn = native._spawn_owned_child

    def recording_spawn(*args: object, **kwargs: object) -> object:
        child = real_spawn(*args, **kwargs)  # type: ignore[arg-type]
        pids.append(child.pid)
        return child

    monkeypatch.setattr(native, "_spawn_owned_child", recording_spawn)
    policy = native._CoordinatorPolicy(
        bind_timeout=0.1,
        term_grace=0.2,
        reap_timeout=0.5,
        poll_interval=0.02,
    )
    outcome, events, _raw = _run_coordinator(
        tmp_path,
        wizard_command=lambda fd: (
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
        ),
        policy=policy,
    )

    assert outcome.failure_code == "wizard_failed"
    assert "wizard_ready" not in [event["type"] for event in events]
    assert events[-1]["code"] == "wizard_failed"
    assert pids
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)
    assert not _lock_path(tmp_path / "state").exists()


def test_a_wizard_exit_timeout_is_a_fixed_wizard_failure_and_reaps_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    pids: list[int] = []
    real_spawn = native._spawn_owned_child

    def recording_spawn(*args: object, **kwargs: object) -> object:
        child = real_spawn(*args, **kwargs)  # type: ignore[arg-type]
        pids.append(child.pid)
        return child

    monkeypatch.setattr(native, "_spawn_owned_child", recording_spawn)
    script = (
        "import os, sys, time\n"
        "os.write(int(sys.argv[1]), b'{\"schema\":1,\"port\":54321}\\n')\n"
        "os.close(int(sys.argv[1]))\n"
        "time.sleep(60)\n"
    )
    policy = native._CoordinatorPolicy(
        bind_timeout=2.0,
        wizard_exit_timeout=0.1,
        term_grace=0.2,
        reap_timeout=0.5,
        poll_interval=0.02,
    )
    outcome, events, _raw = _run_coordinator(
        tmp_path,
        wizard_command=lambda fd: (sys.executable, "-c", script, str(fd)),
        policy=policy,
    )

    assert outcome.failure_code == "wizard_failed"
    assert "wizard_ready" in [event["type"] for event in events]
    assert "setup_saved" not in [event["type"] for event in events]
    assert events[-1]["code"] == "wizard_failed"
    assert pids
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)
    assert not _lock_path(tmp_path / "state").exists()


def test_an_unsafe_bootstrap_scratch_is_unknown_and_stops_before_the_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    def unsafe_scratch() -> Path:
        raise native_handoff.NativeHandoffError("the scratch chain is unsafe")

    outcome, events, _raw = _run_coordinator(
        tmp_path, prepare_scratch=unsafe_scratch
    )

    assert outcome.failure_code == "unknown_state"
    assert outcome.host_step_attempted is False
    assert outcome.host_step_completed is False
    assert not _lock_path(tmp_path / "state").exists()
    assert events[-1]["code"] == "unknown_state"


def test_a_host_verification_failure_is_not_reported_as_a_plain_install_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    engine_calls: list[float] = []

    def bad_archive(
        lock: NativeRunLock, run_id: str, remaining: float
    ) -> native.FreshHostInstall:
        raise server_host_install.ServerHostInstallError(
            "the archive does not match its signed entry",
            code=server_host_install.ARCHIVE_MISMATCH,
        )

    outcome, events, _raw = _run_coordinator(
        tmp_path,
        create_host=bad_archive,
        install_engine=lambda remaining: engine_calls.append(remaining),
    )

    assert outcome.failure_code == "verification_failed"
    assert outcome.host_step_attempted is True
    assert outcome.host_step_completed is False
    assert engine_calls == []
    assert events[-1]["code"] == "verification_failed"
    assert not _lock_path(tmp_path / "state").exists()


def test_a_host_step_that_mutates_then_raises_is_still_marked_attempted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    partial_bundle = tmp_path / "partial-host-bundle"
    partial_bundle.write_text("written before the refusal", encoding="utf-8")

    def create_host(
        lock: NativeRunLock, run_id: str, remaining: float
    ) -> native.FreshHostInstall:
        raise server_host_install.ServerHostInstallError(
            "the host write failed after mutation",
            code=server_host_install.INSTALL_FAILED,
        )

    outcome, events, _raw = _run_coordinator(
        tmp_path, create_host=create_host
    )

    assert outcome.failure_code == "install_failed"
    assert outcome.host_step_attempted is True
    assert outcome.host_step_completed is False
    assert outcome.activation_attempted is False
    assert outcome.activation_completed is False
    assert partial_bundle.read_text(encoding="utf-8") == (
        "written before the refusal"
    )
    assert events[-1]["code"] == "install_failed"


@pytest.mark.parametrize("error_kind", ["handoff", "unexpected"])
def test_activation_that_mutates_then_raises_is_marked_attempted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_kind: str
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    definition = tmp_path / "service-definition.plist"
    definition.write_text("activation started", encoding="utf-8")

    def activate(
        run_id: str, remaining: float
    ) -> native_handoff.NativeHandoffResult:
        if error_kind == "handoff":
            raise native_handoff.NativeHandoffError(
                "activation wrote the job and then failed"
            )
        raise RuntimeError("activation failed unexpectedly")

    outcome, events, _raw = _run_coordinator(
        tmp_path, activate=activate
    )

    assert outcome.failure_code == "activation_refused"
    assert outcome.activation_attempted is True
    assert outcome.activation_completed is False
    assert outcome.service_may_be_running is True
    assert definition.read_text(encoding="utf-8") == "activation started"
    assert events[-1]["code"] == "activation_refused"
    fields = set(native._CoordinatorDeps.__dataclass_fields__)
    assert not fields & {"stop", "stop_service", "reload", "quiesce", "rollback"}


def test_an_unexpected_child_cleanup_error_retains_the_lock_and_emits_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    class BadChild:
        pid = 424242

        def terminate_and_reap(self) -> None:
            raise RuntimeError("cleanup failed unexpectedly")

    monkeypatch.setattr(
        native, "_spawn_owned_child", lambda *args, **kwargs: BadChild()
    )
    outcome, events, _raw = _run_coordinator(
        tmp_path,
        wizard_command=lambda fd: (sys.executable, "-c", "pass"),
    )

    # The child may still exist, so the lock is deliberately retained, but the
    # original fixed failure and the terminal event are not skipped.
    assert outcome.failure_code == "wizard_failed"
    assert outcome.lock_retained is True
    assert _lock_path(tmp_path / "state").is_dir()
    assert events[-1]["type"] == "failed"
    assert events[-1]["code"] == "wizard_failed"


def test_an_unexpected_lock_release_error_preserves_the_original_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    class BadLock:
        def assert_held(self, *, run_id: str | None = None) -> None:
            return None

        def release(self) -> None:
            raise RuntimeError("release failed unexpectedly")

    outcome, events, _raw = _run_coordinator(
        tmp_path, acquire_lock=lambda directory, run_id: BadLock()
    )

    # The release is unproven, so the lock is retained; the original
    # service_not_ready failure is preserved rather than overwritten.
    assert outcome.failure_code == "service_not_ready"
    assert outcome.lock_retained is True
    assert events[-1]["type"] == "failed"
    assert events[-1]["code"] == "service_not_ready"


def test_a_delayed_second_bind_record_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    script = (
        "import os, sys, time\n"
        "fd = int(sys.argv[1])\n"
        "os.write(fd, b'{\"schema\":1,\"port\":54321}\\n')\n"
        "time.sleep(0.1)\n"
        "os.write(fd, b'{\"schema\":1,\"port\":54322}\\n')\n"
        "os.close(fd)\n"
    )
    outcome, events, _raw = _run_coordinator(
        tmp_path,
        wizard_command=lambda fd: (sys.executable, "-c", script, str(fd)),
    )

    assert outcome.failure_code == "wizard_failed"
    assert "wizard_ready" not in [event["type"] for event in events]
    assert "setup_saved" not in [event["type"] for event in events]
    assert events[-1]["code"] == "wizard_failed"


def test_a_bind_record_without_prompt_eof_is_refused_and_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    pids: list[int] = []
    real_spawn = native._spawn_owned_child

    def recording_spawn(*args: object, **kwargs: object) -> object:
        child = real_spawn(*args, **kwargs)  # type: ignore[arg-type]
        pids.append(child.pid)
        return child

    monkeypatch.setattr(native, "_spawn_owned_child", recording_spawn)
    script = (
        "import os, sys, time\n"
        "fd = int(sys.argv[1])\n"
        "os.write(fd, b'{\"schema\":1,\"port\":54321}\\n')\n"
        "time.sleep(60)\n"
    )
    policy = native._CoordinatorPolicy(
        bind_timeout=0.2,
        term_grace=0.2,
        reap_timeout=0.5,
        poll_interval=0.02,
    )
    outcome, events, _raw = _run_coordinator(
        tmp_path,
        wizard_command=lambda fd: (sys.executable, "-c", script, str(fd)),
        policy=policy,
    )

    assert outcome.failure_code == "wizard_failed"
    assert "wizard_ready" not in [event["type"] for event in events]
    assert pids
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)
    assert not _lock_path(tmp_path / "state").exists()


def test_cancellation_during_the_wizard_wait_reaps_and_never_activates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    cancellation = native.NativeCancellation()
    pids: list[int] = []
    real_spawn = native._spawn_owned_child

    def recording_spawn(*args: object, **kwargs: object) -> object:
        child = real_spawn(*args, **kwargs)  # type: ignore[arg-type]
        pids.append(child.pid)
        return child

    monkeypatch.setattr(native, "_spawn_owned_child", recording_spawn)
    script = (
        "import os, sys, time\n"
        "fd = int(sys.argv[1])\n"
        "os.write(fd, b'{\"schema\":1,\"port\":54321}\\n')\n"
        "os.close(fd)\n"
        "time.sleep(60)\n"
    )
    policy = native._CoordinatorPolicy(
        bind_timeout=2.0,
        wizard_exit_timeout=5.0,
        term_grace=0.2,
        reap_timeout=0.5,
        poll_interval=0.02,
    )
    with _public_event_pipe() as (read_fd, write_fd):
        outcome_box: list[native.NativeInstallOutcome] = []

        def drive() -> None:
            outcome_box.append(
                native._run_native_install(
                    _coordinator_request(write_fd),
                    deps=_coordinator_deps(
                        tmp_path,
                        wizard_command=lambda fd: (
                            sys.executable,
                            "-c",
                            script,
                            str(fd),
                        ),
                    ),
                    cancellation=cancellation,
                    policy=policy,
                )
            )

        runner = threading.Thread(target=drive, daemon=True)
        runner.start()
        raw = bytearray()
        events: list[dict[str, object]] = []
        wizard_ready_seen = False
        deadline = time.monotonic() + 5
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                readable, _, _ = select.select(
                    [read_fd], [], [], min(remaining, 0.05)
                )
            except OSError:
                break
            if not readable:
                continue
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break
            raw.extend(chunk)
            while b"\n" in raw:
                line, _, rest = raw.partition(b"\n")
                raw = bytearray(rest)
                event = json.loads(line)
                events.append(event)
                if (
                    event["type"] == "wizard_ready"
                    and not wizard_ready_seen
                ):
                    wizard_ready_seen = True
                    cancellation.cancel()
        cancellation.cancel()
        runner.join(timeout=5)
        assert not runner.is_alive()

    outcome = outcome_box[0]
    assert wizard_ready_seen is True
    assert outcome.failure_code == "cancelled"
    assert outcome.activation_attempted is False
    assert outcome.activation_completed is False
    assert pids
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)
    assert not _lock_path(tmp_path / "state").exists()
    assert events[-1]["type"] == "failed"
    assert events[-1]["code"] == "cancelled"


def test_the_bind_wait_is_clamped_to_the_whole_run_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    policy = native._CoordinatorPolicy(
        run_timeout=0.2,
        bind_timeout=5.0,
        term_grace=0.1,
        reap_timeout=0.5,
        poll_interval=0.02,
    )
    started = time.monotonic()
    outcome, events, _raw = _run_coordinator(
        tmp_path,
        wizard_command=lambda fd: (
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
        ),
        policy=policy,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert outcome.failure_code == "wizard_failed"
    assert "wizard_ready" not in [event["type"] for event in events]
    assert not _lock_path(tmp_path / "state").exists()


def test_the_wizard_exit_wait_is_clamped_to_the_whole_run_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    script = (
        "import os, sys, time\n"
        "fd = int(sys.argv[1])\n"
        "os.write(fd, b'{\"schema\":1,\"port\":54321}\\n')\n"
        "os.close(fd)\n"
        "time.sleep(60)\n"
    )
    policy = native._CoordinatorPolicy(
        run_timeout=0.5,
        bind_timeout=5.0,
        wizard_exit_timeout=5.0,
        term_grace=0.1,
        reap_timeout=0.5,
        poll_interval=0.02,
    )
    started = time.monotonic()
    outcome, events, _raw = _run_coordinator(
        tmp_path,
        wizard_command=lambda fd: (sys.executable, "-c", script, str(fd)),
        policy=policy,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert "wizard_ready" in [event["type"] for event in events]
    assert outcome.failure_code == "wizard_failed"
    assert outcome.activation_attempted is False
    assert not _lock_path(tmp_path / "state").exists()


def test_a_workspace_suggestion_is_passed_to_the_classifier_but_never_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    suggestion = tmp_path / "suggested"
    chosen = tmp_path / "chosen-by-wizard"
    seen: list[Path | None] = []

    def classify(workspace: Path | None) -> NativeInstallState:
        seen.append(workspace)
        return NativeInstallState.fresh

    outcome, events, _raw = _run_coordinator(
        tmp_path,
        classify=classify,
        read_handoff=lambda run_id: _handoff(run_id, workspace=chosen),
        activate=lambda run_id, remaining: _handoff(
            run_id, workspace=chosen
        ),
        workspace=suggestion,
    )

    assert seen == [suggestion, suggestion]
    # The wizard may legitimately choose a different workspace; a suggestion is
    # not an exact requirement.
    assert outcome.failure_code == "service_not_ready"
    assert outcome.selected_workspace == chosen
    assert "setup_saved" in [event["type"] for event in events]


def test_an_explicit_expected_workspace_must_match_the_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    chosen = tmp_path / "chosen-by-wizard"
    required = tmp_path / "required-by-caller"

    outcome, events, _raw = _run_coordinator(
        tmp_path,
        read_handoff=lambda run_id: _handoff(run_id, workspace=chosen),
        activate=lambda run_id, remaining: _handoff(
            run_id, workspace=chosen
        ),
        expected_workspace=required,
    )

    assert outcome.failure_code == "wizard_failed"
    assert outcome.selected_workspace == chosen
    assert outcome.activation_attempted is False
    assert "setup_saved" not in [event["type"] for event in events]
    assert events[-1]["code"] == "wizard_failed"


def test_an_explicit_expected_workspace_that_matches_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    chosen = tmp_path / "chosen-by-wizard"

    outcome, _events, _raw = _run_coordinator(
        tmp_path,
        read_handoff=lambda run_id: _handoff(run_id, workspace=chosen),
        activate=lambda run_id, remaining: _handoff(
            run_id, workspace=chosen
        ),
        expected_workspace=chosen,
    )

    assert outcome.failure_code == "service_not_ready"
    assert outcome.selected_workspace == chosen
    assert outcome.activation_completed is True


def test_the_private_notification_fd_is_not_inherited_by_a_wizard_grandchild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    grandchild_fds_path = tmp_path / "grandchild-fds.json"
    script = (
        "import json, os, subprocess, sys\n"
        "fd = int(sys.argv[1])\n"
        "os.set_inheritable(fd, False)\n"
        "os.write(fd, b'{\"schema\":1,\"port\":54321}\\n')\n"
        "lister = (\n"
        "    'import json, os, sys\\n'\n"
        "    'with open(sys.argv[1], \"w\", encoding=\"utf-8\") as handle:\\n'\n"
        "    '    json.dump(sorted(int(n) for n in os.listdir(\"/dev/fd\")), handle)\\n'\n"
        ")\n"
        "grandchild = subprocess.Popen([sys.executable, '-c', lister, sys.argv[2]])\n"
        "grandchild.wait()\n"
        "os.close(fd)\n"
    )
    notification_fds: list[int] = []

    def wizard_command(fd: int) -> tuple[str, ...]:
        notification_fds.append(fd)
        return (
            sys.executable,
            "-c",
            script,
            str(fd),
            str(grandchild_fds_path),
        )

    outcome, _events, _raw = _run_coordinator(
        tmp_path, wizard_command=wizard_command
    )

    assert outcome.failure_code == "service_not_ready"
    assert notification_fds
    grandchild_fds = json.loads(
        grandchild_fds_path.read_text(encoding="utf-8")
    )
    assert notification_fds[0] not in grandchild_fds


def test_a_handoff_for_another_run_refuses_before_setup_saved_and_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    activated: list[str] = []

    def activate(
        run_id: str, remaining: float
    ) -> native_handoff.NativeHandoffResult:
        activated.append(run_id)
        raise AssertionError("activation ran for another run's handoff")

    outcome, events, _raw = _run_coordinator(
        tmp_path,
        read_handoff=lambda run_id: _handoff(OTHER_RUN_ID),
        activate=activate,
    )

    assert outcome.failure_code == "wizard_failed"
    assert outcome.selected_workspace is None
    assert outcome.activation_attempted is False
    assert outcome.activation_completed is False
    assert activated == []
    phases = [
        event.get("phase") for event in events if event["type"] == "phase"
    ]
    assert "activation" not in phases
    assert "setup_saved" not in [event["type"] for event in events]
    assert events[-1]["type"] == "failed"
    assert events[-1]["code"] == "wizard_failed"


@pytest.mark.parametrize("mismatch", ["run_id", "port"])
def test_an_activation_result_mismatch_is_refused_after_the_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mismatch: str
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    def activate(
        run_id: str, remaining: float
    ) -> native_handoff.NativeHandoffResult:
        if mismatch == "run_id":
            return _handoff(OTHER_RUN_ID)
        return _handoff(run_id, port=54322)

    outcome, events, _raw = _run_coordinator(
        tmp_path, activate=activate
    )

    assert outcome.failure_code == "activation_refused"
    assert outcome.activation_attempted is True
    assert outcome.activation_completed is True
    assert outcome.service_may_be_running is True
    assert events[-1]["type"] == "failed"
    assert events[-1]["code"] == "activation_refused"
    fields = set(native._CoordinatorDeps.__dataclass_fields__)
    assert not fields & {"stop", "stop_service", "reload", "quiesce", "rollback"}


def test_a_child_that_exits_nonzero_after_binding_is_a_wizard_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    script = (
        "import os, sys\n"
        "fd = int(sys.argv[1])\n"
        "os.write(fd, b'{\"schema\":1,\"port\":54321}\\n')\n"
        "os.close(fd)\n"
        "raise SystemExit(7)\n"
    )
    outcome, events, _raw = _run_coordinator(
        tmp_path,
        wizard_command=lambda fd: (sys.executable, "-c", script, str(fd)),
    )

    assert "wizard_ready" in [event["type"] for event in events]
    assert "setup_saved" not in [event["type"] for event in events]
    assert outcome.failure_code == "wizard_failed"
    assert outcome.activation_attempted is False
    assert outcome.activation_completed is False
    assert events[-1]["code"] == "wizard_failed"


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        ("lock_busy", "install_in_progress"),
        ("lock_unknown", "install_in_progress"),
        ("lock_changed", "install_in_progress"),
        ("lock_not_held", "install_in_progress"),
        ("lock_run_mismatch", "install_in_progress"),
        ("lock_unsafe", "unknown_state"),
        ("state_dir_unsafe", "unknown_state"),
        ("invalid_run_id", "invalid_arguments"),
        ("lock_cleanup_refused", "internal_error"),
        ("lock_create_failed", "install_failed"),
        ("lock_record_failed", "install_failed"),
    ],
)
def test_lock_refusals_map_to_fixed_codes_at_the_coordinator_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    code: str,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    def acquire(directory: Path, run_id: str) -> NativeRunLock:
        raise NativeLockError(reason)

    outcome, events, _raw = _run_coordinator(
        tmp_path, acquire_lock=acquire
    )

    assert outcome.failure_code == code
    assert outcome.host_step_attempted is False
    assert events[-1]["type"] == "failed"
    assert events[-1]["code"] == code


@pytest.mark.parametrize(
    ("host_code", "code"),
    [
        (server_host_install.HOST_EXISTS, "existing_install"),
        (server_host_install.TRANSACTION_CONFLICT, "partial_install"),
        (server_host_install.TARGET_UNSAFE, "unknown_state"),
        (server_host_install.TRANSACTION_BUSY, "install_in_progress"),
        (server_host_install.ARCHIVE_MISMATCH, "verification_failed"),
        (server_host_install.ARCHIVE_UNSAFE, "verification_failed"),
        (server_host.INVALID_OWNERSHIP, "verification_failed"),
        (server_host.NOT_OWNED, "verification_failed"),
        (server_host_install.POLICY_REFUSED, "verification_failed"),
        (server_host.SIGNATURE_REJECTED, "verification_failed"),
        (server_host.UNSUPPORTED_SCHEMA, "verification_failed"),
        (server_host_install.INSTALL_FAILED, "install_failed"),
    ],
)
def test_host_refusals_map_to_fixed_codes_at_the_coordinator_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    host_code: str,
    code: str,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    def create_host(
        lock: NativeRunLock, run_id: str, remaining: float
    ) -> native.FreshHostInstall:
        raise server_host_install.ServerHostInstallError(
            "host refused", code=host_code
        )

    outcome, events, _raw = _run_coordinator(
        tmp_path, create_host=create_host
    )

    assert outcome.failure_code == code
    assert outcome.host_step_attempted is True
    assert outcome.host_step_completed is False
    assert outcome.activation_attempted is False
    assert events[-1]["type"] == "failed"
    assert events[-1]["code"] == code


def test_a_truly_unreapable_owned_child_retains_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    class NeverReapable:
        pid = 424242

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            time.sleep(min(timeout or 0, 0.001))
            raise subprocess.TimeoutExpired(["child"], timeout)

        def send_signal(self, sig: int) -> None:
            return None

    monkeypatch.setattr(native, "_GETPGID", lambda pid: pid)
    monkeypatch.setattr(native, "_KILLPG", lambda pid, sig: None)
    child = native._OwnedChild(
        NeverReapable(),  # type: ignore[arg-type]
        term_grace=0.01,
        reap_timeout=0.01,
        poll_interval=0.001,
    )
    monkeypatch.setattr(
        native, "_spawn_owned_child", lambda *args, **kwargs: child
    )
    policy = native._CoordinatorPolicy(
        bind_timeout=0.1,
        term_grace=0.01,
        reap_timeout=0.01,
        poll_interval=0.001,
    )
    outcome, events, _raw = _run_coordinator(
        tmp_path,
        wizard_command=lambda fd: (sys.executable, "-c", "pass"),
        policy=policy,
    )

    assert outcome.failure_code == "wizard_failed"
    assert outcome.lock_retained is True
    assert _lock_path(tmp_path / "state").is_dir()
    assert events[-1]["type"] == "failed"
    assert events[-1]["code"] == "wizard_failed"


def test_a_non_darwin_platform_refuses_before_state_or_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("a non-Darwin run touched an install step")

    outcome, events, _raw = _run_coordinator(
        tmp_path,
        prepare_state_dir=forbidden,
        acquire_lock=forbidden,
        prepare_scratch=forbidden,
        create_host=forbidden,
        install_engine=forbidden,
        wizard_command=forbidden,
    )

    assert outcome.failure_code == "unknown_state"
    assert outcome.host_step_attempted is False
    assert outcome.activation_attempted is False
    assert not (tmp_path / "state").exists()
    assert [event["type"] for event in events] == ["failed"]
    assert events[-1]["code"] == "unknown_state"


def test_a_validated_public_fd_is_owned_closed_and_non_inheritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    inheritance: list[bool] = []

    with _public_event_pipe() as (read_fd, write_fd):

        def create_host(
            lock: NativeRunLock, run_id: str, remaining: float
        ) -> native.FreshHostInstall:
            inheritance.append(os.get_inheritable(write_fd))
            return _fresh_host_value(tmp_path)

        outcome = native._run_native_install(
            _coordinator_request(write_fd),
            deps=_coordinator_deps(tmp_path, create_host=create_host),
        )
        with pytest.raises(OSError):
            os.fstat(write_fd)
        _drain(read_fd)

    assert inheritance == [False]
    assert outcome.failure_code == "service_not_ready"


def test_an_invalid_request_leaves_the_public_fd_to_the_caller(
    tmp_path: Path,
) -> None:
    with _public_event_pipe() as (read_fd, event_fd):
        bad = native.NativeInstallRequest(
            run_id="not-a-uuid",
            events_fd=event_fd,
            first_seq=1,
            prior_events=0,
        )
        outcome = native._run_native_install(
            bad, deps=_coordinator_deps(tmp_path)
        )
        assert outcome.failure_code == "invalid_arguments"
        os.fstat(event_fd)  # the caller still owns the descriptor
        assert _available(read_fd) == b""
        with pytest.raises(native.NativeCoordinatorError):
            native.run_native_install(bad)
        os.fstat(event_fd)


def test_a_ready_proof_ends_the_run_with_service_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    proofs: list[native_handoff.NativeHandoffResult] = []

    def prove_ready(host: object, handoff: object, remaining: float) -> object:
        assert isinstance(handoff, native_handoff.NativeHandoffResult)
        proofs.append(handoff)
        return NativeReadinessProof(
            ready=True, failure=None, port=handoff.port, version="1"
        )

    outcome, events, raw = _run_coordinator(tmp_path, prove_ready=prove_ready)

    assert len(proofs) == 1
    assert outcome.terminal == "service_ready"
    assert outcome.exit_code == 0
    assert outcome.failure_code is None
    assert outcome.activation_completed is True
    assert outcome.lock_retained is False
    assert not _lock_path(tmp_path / "state").exists()
    assert [event["type"] for event in events] == [
        "phase",
        "phase",
        "wizard_ready",
        "setup_saved",
        "phase",
        "phase",
        "service_ready",
    ]
    assert [event["seq"] for event in events] == list(range(1, 8))
    assert [
        event["phase"] for event in events if event["type"] == "phase"
    ] == ["install", "wizard", "activation", "readiness"]
    assert events[-1] == {
        "schema": 1,
        "run_id": RUN_ID,
        "seq": 7,
        "type": "service_ready",
        "origin": "http://127.0.0.1:54321",
    }
    assert b"failed" not in raw


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param("listener_missing", id="listener"),
        pytest.param("startup_status_unavailable", id="status"),
        pytest.param("startup_not_ready", id="not-ready"),
    ],
)
def test_readiness_retries_only_transient_answers_within_its_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    answers = [
        NativeReadinessProof(ready=False, failure=failure, port=54321, version="1"),
        NativeReadinessProof(ready=False, failure=failure, port=54321, version="1"),
        NativeReadinessProof(ready=True, failure=None, port=54321, version="1"),
    ]
    calls: list[float] = []

    def prove_ready(host: object, handoff: object, remaining: float) -> object:
        calls.append(remaining)
        return answers[len(calls) - 1]

    outcome, events, _raw = _run_coordinator(
        tmp_path,
        prove_ready=prove_ready,
        policy=native._CoordinatorPolicy(
            readiness_interval=0.01, poll_interval=0.01
        ),
    )

    assert len(calls) == 3
    assert outcome.terminal == "service_ready"
    assert events[-1]["type"] == "service_ready"


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param("startup_pid_mismatch", id="pid"),
        pytest.param("lock_missing", id="lock"),
        pytest.param("executable_mismatch", id="exec"),
        pytest.param("timeout", id="timeout"),
    ],
)
def test_a_non_transient_readiness_answer_is_service_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    calls: list[float] = []

    def prove_ready(host: object, handoff: object, remaining: float) -> object:
        calls.append(remaining)
        return NativeReadinessProof(
            ready=False, failure=failure, port=54321, version="1"
        )

    outcome, events, raw = _run_coordinator(tmp_path, prove_ready=prove_ready)

    assert len(calls) == 1
    assert outcome.terminal == "failed"
    assert outcome.failure_code == "service_not_ready"
    assert events[-1]["code"] == "service_not_ready"
    assert b"service_ready" not in raw


@pytest.mark.parametrize("case", ["budget", "non-proof", "port", "raises"])
def test_readiness_budget_and_bad_answers_are_service_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    sentinel = "sk-live-READINESS-SECRET-0123456789"

    def transient(host: object, handoff: object, remaining: float) -> object:
        assert isinstance(handoff, native_handoff.NativeHandoffResult)
        return NativeReadinessProof(
            ready=False, failure="listener_missing", port=handoff.port, version="1"
        )

    def not_a_proof(host: object, handoff: object, remaining: float) -> object:
        return "ready"

    def wrong_port(host: object, handoff: object, remaining: float) -> object:
        return NativeReadinessProof(ready=True, failure=None, port=9999, version="1")

    def explode(host: object, handoff: object, remaining: float) -> object:
        raise RuntimeError(f"{sentinel} /Users/someone/private")

    prove_ready = {
        "budget": transient,
        "non-proof": not_a_proof,
        "port": wrong_port,
        "raises": explode,
    }[case]
    policy = (
        native._CoordinatorPolicy(
            readiness_timeout=0.05, readiness_interval=0.01, poll_interval=0.01
        )
        if case == "budget"
        else None
    )

    outcome, events, raw = _run_coordinator(
        tmp_path, prove_ready=prove_ready, policy=policy
    )

    assert outcome.terminal == "failed"
    assert outcome.failure_code == "service_not_ready"
    assert events[-1]["code"] == "service_not_ready"
    assert b"service_ready" not in raw
    assert sentinel.encode() not in raw
    assert b"/Users/someone/private" not in raw


def test_cancellation_during_readiness_is_cancelled_and_never_stops_the_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    cancellation = native.NativeCancellation()
    activated: list[str] = []

    def activate(run_id: str, remaining: float) -> native_handoff.NativeHandoffResult:
        activated.append(run_id)
        return _handoff(run_id)

    def prove_ready(host: object, handoff: object, remaining: float) -> object:
        assert isinstance(handoff, native_handoff.NativeHandoffResult)
        cancellation.cancel()
        return NativeReadinessProof(
            ready=False, failure="listener_missing", port=handoff.port, version="1"
        )

    outcome, events, raw = _run_coordinator(
        tmp_path,
        activate=activate,
        prove_ready=prove_ready,
        cancellation=cancellation,
    )

    assert outcome.terminal == "failed"
    assert outcome.failure_code == "cancelled"
    assert outcome.activation_completed is True
    assert outcome.service_may_be_running is True
    assert len(activated) == 1
    assert events[-1]["code"] == "cancelled"
    assert b"service_ready" not in raw
    assert not _lock_path(tmp_path / "state").exists()


def test_a_lock_release_failure_after_readiness_is_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    real_locks: list[NativeRunLock] = []

    class BadRelease:
        def __init__(self, real: NativeRunLock) -> None:
            self._real = real

        def assert_held(self, *, run_id: str | None = None) -> None:
            self._real.assert_held(run_id=run_id)

        def release(self) -> None:
            raise NativeLockError("lock_cleanup_refused")

    def acquire(directory: native_lock.NativeStateDir, run_id: str) -> object:
        real = NativeRunLock.acquire(directory, run_id=run_id)
        real_locks.append(real)
        return BadRelease(real)

    def prove_ready(host: object, handoff: object, remaining: float) -> object:
        assert isinstance(handoff, native_handoff.NativeHandoffResult)
        return NativeReadinessProof(
            ready=True, failure=None, port=handoff.port, version="1"
        )

    try:
        outcome, events, raw = _run_coordinator(
            tmp_path, acquire_lock=acquire, prove_ready=prove_ready
        )
    finally:
        for real in real_locks:
            real.release()

    assert outcome.terminal == "failed"
    assert outcome.failure_code == "internal_error"
    assert outcome.lock_retained is True
    assert outcome.activation_completed is True
    assert events[-1]["code"] == "internal_error"
    assert b"service_ready" not in raw


@pytest.mark.parametrize("case", ["foreign", "shared", "empty", "raises"])
def test_wizard_ready_requires_the_child_to_hold_the_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    child_pids: list[int] = []
    activated: list[str] = []

    def listener(port: int, child_pid: int) -> frozenset[int]:
        child_pids.append(child_pid)
        if case == "foreign":
            return frozenset({os.getpid()})
        if case == "shared":
            return frozenset({child_pid, os.getpid()})
        if case == "empty":
            return frozenset()
        raise RuntimeError("listener lookup failed")

    def activate(run_id: str, remaining: float) -> native_handoff.NativeHandoffResult:
        activated.append(run_id)
        return _handoff(run_id)

    outcome, events, raw = _run_coordinator(
        tmp_path, wizard_listener=listener, activate=activate
    )

    assert outcome.failure_code == "wizard_failed"
    assert outcome.activation_attempted is False
    assert activated == []
    assert [event["type"] for event in events] == ["phase", "phase", "failed"]
    assert b"wizard_ready" not in raw
    assert len(child_pids) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(child_pids[0], 0)


def test_the_wizard_child_that_exited_before_the_listener_check_is_wizard_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    # Released up front, so the child exits right after writing its record.
    (tmp_path / WIZARD_RELEASE_NAME).touch()
    exited: list[int] = []

    def listener(port: int, child_pid: int) -> frozenset[int]:
        # WNOWAIT observes the exit without reaping, so the coordinator's own
        # liveness check still sees a child that has exited.
        os.waitid(os.P_PID, child_pid, os.WEXITED | os.WNOWAIT)
        exited.append(child_pid)
        return frozenset({child_pid})

    outcome, events, raw = _run_coordinator(tmp_path, wizard_listener=listener)

    assert exited
    assert outcome.failure_code == "wizard_failed"
    assert outcome.activation_attempted is False
    assert [event["type"] for event in events] == ["phase", "phase", "failed"]
    assert b"wizard_ready" not in raw


def test_owns_process_accepts_only_the_child_or_its_session_group() -> None:
    script = (
        "import subprocess, sys, time\n"
        "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(grandchild.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        start_new_session=True,
        stdout=subprocess.PIPE,
    )
    grandchild_pid = 0
    try:
        assert process.stdout is not None
        grandchild_pid = int(process.stdout.readline().strip())
        owned = native._OwnedChild(
            process,
            term_grace=1.0,
            reap_timeout=5.0,
            poll_interval=0.01,
        )
        assert owned.owns_process(process.pid) is True
        assert owned.owns_process(grandchild_pid) is True
        assert owned.owns_process(os.getpid()) is False
        assert owned.owns_process(0) is False
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        assert owned.owns_process(process.pid) is False
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
        if grandchild_pid:
            with contextlib.suppress(ProcessLookupError):
                os.kill(grandchild_pid, signal.SIGKILL)


def test_the_coordinator_writer_refuses_the_shell_phases() -> None:
    writer, read_fd = _writer(after_phase="download")
    try:
        for name in ("preflight", "bootstrap", "download"):
            assert _reason(writer.phase, name) == "phase_out_of_order"
        writer.phase("install")
        writer.close()
        assert [event.get("phase") for event in _events(read_fd)] == ["install"]
    finally:
        os.close(read_fd)


def test_an_unknown_after_phase_is_refused_before_any_write() -> None:
    read_fd, write_fd = os.pipe()
    try:
        assert (
            _reason(EventWriter, write_fd, run_id=RUN_ID, after_phase="nope")
            == "unknown_phase"
        )
        assert _available(read_fd) == b""
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.parametrize(
    "target",
    [pytest.param(1, id="stdout"), pytest.param(2, id="stderr")],
)
def test_an_events_fd_aliasing_stdout_or_stderr_is_refused(target: int) -> None:
    import fcntl

    alias = os.dup(target)
    try:
        flags_before = fcntl.fcntl(alias, fcntl.F_GETFL)
        assert _reason(EventWriter, alias, run_id=RUN_ID) == "stdio_alias"
        assert fcntl.fcntl(alias, fcntl.F_GETFL) == flags_before
    finally:
        os.close(alias)


@pytest.mark.parametrize(
    "target",
    [pytest.param(1, id="stdout"), pytest.param(2, id="stderr")],
)
def test_a_coordinator_events_fd_aliasing_stdio_is_refused_before_classify(
    tmp_path: Path, target: int
) -> None:
    calls: list[object] = []

    def forbidden(workspace: Path | None) -> NativeInstallState:
        calls.append(workspace)
        raise AssertionError("the coordinator classified over an aliased channel")

    with _public_event_pipe() as (read_fd, event_fd):
        source = os.dup(target)
        os.dup2(source, event_fd)
        os.close(source)
        outcome = native._run_native_install(
            _coordinator_request(event_fd),
            deps=_coordinator_deps(tmp_path, classify=forbidden),
        )
        raw = _drain(read_fd)

    assert outcome.failure_code == "event_channel_failed"
    assert outcome.terminal is None
    assert calls == []
    assert raw == b""


@pytest.mark.parametrize("field", ["readiness_timeout", "readiness_interval"])
@pytest.mark.parametrize(
    "value",
    [
        pytest.param(0, id="zero"),
        pytest.param(-1, id="neg"),
        pytest.param(float("inf"), id="inf"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(native.COORDINATOR_MAX_TIMEOUT_SECONDS + 1, id="huge"),
    ],
)
def test_readiness_policy_values_are_bounded(field: str, value: float) -> None:
    with pytest.raises(native.NativeCoordinatorError) as caught:
        native._CoordinatorPolicy(**{field: value})
    assert caught.value.reason == "invalid_timeout"
    assert caught.value.failure_code == "internal_error"


def test_a_cancelled_readiness_proof_ends_the_run_as_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    def prove_ready(host: object, handoff: object, remaining: float) -> object:
        assert isinstance(handoff, native_handoff.NativeHandoffResult)
        return NativeReadinessProof(
            ready=False, failure="cancelled", port=handoff.port, version="1"
        )

    outcome, events, raw = _run_coordinator(tmp_path, prove_ready=prove_ready)

    assert outcome.terminal == "failed"
    assert outcome.failure_code == "cancelled"
    assert outcome.activation_completed is True
    assert events[-1]["code"] == "cancelled"
    assert b"service_ready" not in raw
    assert not _lock_path(tmp_path / "state").exists()


def test_a_ready_proof_that_also_carries_a_failure_is_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    def prove_ready(host: object, handoff: object, remaining: float) -> object:
        assert isinstance(handoff, native_handoff.NativeHandoffResult)
        # The dataclass refuses this combination, so build it past __init__.
        proof = object.__new__(NativeReadinessProof)
        object.__setattr__(proof, "ready", True)
        object.__setattr__(proof, "failure", "listener_missing")
        object.__setattr__(proof, "port", handoff.port)
        object.__setattr__(proof, "version", "1")
        return proof

    outcome, events, raw = _run_coordinator(tmp_path, prove_ready=prove_ready)

    assert outcome.terminal == "failed"
    assert outcome.failure_code == "service_not_ready"
    assert b"service_ready" not in raw


def test_a_failed_service_ready_after_a_ready_proof_is_a_channel_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    def ready(host: object, handoff: object, remaining: float) -> object:
        assert isinstance(handoff, native_handoff.NativeHandoffResult)
        return NativeReadinessProof(
            ready=True, failure=None, port=handoff.port, version="1"
        )

    def refuse_service_ready(self: EventWriter, port: int) -> None:
        raise EventError("setup_not_saved")

    monkeypatch.setattr(EventWriter, "service_ready", refuse_service_ready)

    outcome, events, raw = _run_coordinator(tmp_path, prove_ready=ready)

    assert outcome.terminal is None
    assert outcome.failure_code == "event_channel_failed"
    assert outcome.activation_completed is True
    assert not _lock_path(tmp_path / "state").exists()
    types = [event["type"] for event in events]
    assert "service_ready" not in types
    assert "failed" not in types
    assert b"service_ready" not in raw


def test_cancellation_during_a_long_readiness_interval_returns_promptly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    cancellation = native.NativeCancellation()
    timers: list[threading.Timer] = []

    def prove_ready(host: object, handoff: object, remaining: float) -> object:
        assert isinstance(handoff, native_handoff.NativeHandoffResult)
        # The fake schedules the cancel while the coordinator waits out a
        # 60-second interval, so only the sliced wait can observe it.
        timer = threading.Timer(0.2, cancellation.cancel)
        timers.append(timer)
        timer.start()
        return NativeReadinessProof(
            ready=False, failure="listener_missing", port=handoff.port, version="1"
        )

    policy = native._CoordinatorPolicy(readiness_interval=60.0, poll_interval=0.01)
    started = time.monotonic()
    try:
        outcome, events, raw = _run_coordinator(
            tmp_path,
            prove_ready=prove_ready,
            cancellation=cancellation,
            policy=policy,
        )
    finally:
        for timer in timers:
            timer.cancel()
    elapsed = time.monotonic() - started

    assert outcome.failure_code == "cancelled"
    assert events[-1]["code"] == "cancelled"
    assert elapsed < 2.0
