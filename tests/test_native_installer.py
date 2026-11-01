"""The native installer's bounded NDJSON event channel (#2340).

The first slice of the native installer is the public progress protocol and
nothing else: no shell, no coordinator, no service, no route and no install.
These tests pin the wire contract on its own, against real inherited pipes —
the descriptor a client passes as ``--events-fd`` — so the properties the Mac
client depends on are proven before any code can emit an event.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from ciao import native_installer as native
from ciao.native_installer import (
    EventError,
    EventWriter,
    NativeLockError,
    NativeRunLock,
)

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


def _lock_path(state: Path) -> Path:
    return state / native.LOCK_DIR_NAME


def _record_path(state: Path) -> Path:
    return _lock_path(state) / native.LOCK_RECORD_NAME


def _record_bytes(run_id: str = RUN_ID, *, pid: int | None = None) -> bytes:
    payload = {
        "schema": native.LOCK_SCHEMA_VERSION,
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
    record = lock_dir / native.LOCK_RECORD_NAME
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
        "from ciao.native_installer import NativeLockError, NativeRunLock\n"
        "try:\n"
        "    lock = NativeRunLock.acquire(Path(sys.argv[1]), run_id=sys.argv[2])\n"
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
            native.prepare_native_state_dir,
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
            native.prepare_native_state_dir,
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

    assert native.prepare_native_state_dir(state, trusted_root=tmp_path) == state
    # The existing component is judged, never chmod'ed.
    assert stat.S_IMODE(os.lstat(existing).st_mode) == 0o755
    intermediate = existing / "intermediate"
    assert stat.S_IMODE(os.lstat(intermediate).st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(state).st_mode) == 0o700
    assert os.lstat(state).st_uid == os.geteuid()


def test_prepare_then_acquire_works_on_a_fresh_chain(tmp_path: Path) -> None:
    state = tmp_path / "fresh" / "ciaobot"
    assert not state.exists()
    assert native.prepare_native_state_dir(state, trusted_root=tmp_path) == state
    lock = NativeRunLock.acquire(state, run_id=RUN_ID)
    lock.release()
    assert not _lock_path(state).exists()


def test_prepare_is_a_noop_for_an_existing_private_state_dir(
    tmp_path: Path,
) -> None:
    state = _state_dir(tmp_path)
    before = os.lstat(state)
    assert native.prepare_native_state_dir(state, trusted_root=tmp_path) == state
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
            native.prepare_native_state_dir,
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
            native.prepare_native_state_dir,
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
            native.prepare_native_state_dir,
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
            native.prepare_native_state_dir,
            link / "state",
            trusted_root=root,
        )
        == "state_dir_unsafe"
    )
    missing = link / "state" / "new"
    assert (
        _lock_reason(
            native.prepare_native_state_dir,
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
            native.prepare_native_state_dir,
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
            native.prepare_native_state_dir,
            link / "state",
            trusted_root=link,
        )
        == "state_dir_unsafe"
    )
    assert not (real / "state").exists()


def test_prepare_defaults_to_home_as_trusted_root() -> None:
    home = Path.home()
    if home == Path("/"):
        pytest.skip("the home directory is the filesystem root")
    target = home.parent / f"caobot-outside-{uuid.uuid4().hex}"
    assert (
        _lock_reason(native.prepare_native_state_dir, target)
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
    real_fstat = native._fstat
    calls = 0

    def bad_fstat(fd: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.EIO, "fstat failed")
        return real_fstat(fd)

    monkeypatch.setattr(native, "_fstat", bad_fstat)
    assert (
        _lock_reason(
            native.prepare_native_state_dir, state, trusted_root=root
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
                native.prepare_native_state_dir,
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
    lock = NativeRunLock.acquire(state, run_id=RUN_ID)
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
            "schema": native.LOCK_SCHEMA_VERSION,
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
            _lock_reason(NativeRunLock.acquire, state, run_id=bad)
            == "invalid_run_id"
        )
    assert list(state.iterdir()) == []


def test_the_lock_refuses_a_non_native_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state_dir(tmp_path)
    monkeypatch.setattr(native.sys, "platform", "win32")
    assert (
        _lock_reason(NativeRunLock.acquire, state, run_id=RUN_ID)
        == "unsupported_platform"
    )
    assert list(state.iterdir()) == []


def test_an_unsafe_or_missing_state_directory_is_refused(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    assert (
        _lock_reason(NativeRunLock.acquire, missing, run_id=RUN_ID)
        == "state_dir_unsafe"
    )

    state = _state_dir(tmp_path)
    os.chmod(state, 0o777)
    assert (
        _lock_reason(NativeRunLock.acquire, state, run_id=RUN_ID)
        == "state_dir_unsafe"
    )
    os.chmod(state, 0o700)

    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    os.chmod(target, 0o700)
    link = tmp_path / "state-link"
    os.symlink(target, link)
    assert (
        _lock_reason(NativeRunLock.acquire, link, run_id=RUN_ID)
        == "state_dir_unsafe"
    )

    regular = tmp_path / "state-file"
    regular.write_text("not a directory", encoding="utf-8")
    assert (
        _lock_reason(NativeRunLock.acquire, regular, run_id=RUN_ID)
        == "state_dir_unsafe"
    )
    assert not _lock_path(state).exists()
    assert link.is_symlink()
    assert target.is_dir()
    assert regular.is_file()


def test_exclusive_acquisition_refuses_a_second_holder(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(state, run_id=RUN_ID)
    try:
        record_before = _record_path(state).read_bytes()
        inode_before = os.lstat(_record_path(state)).st_ino
        assert (
            _lock_reason(
                NativeRunLock.acquire, state, run_id=OTHER_RUN_ID
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
            _lock_reason(NativeRunLock.acquire, state, run_id=RUN_ID)
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
            lock = NativeRunLock.acquire(state, run_id=RUN_ID)
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
        "from ciao.native_installer import NativeLockError, NativeRunLock\n"
        "state = Path(sys.argv[1])\n"
        "run_id = sys.argv[2]\n"
        "start = Path(sys.argv[3])\n"
        "release = Path(sys.argv[4])\n"
        "while not start.exists():\n"
        "    time.sleep(0.01)\n"
        "try:\n"
        "    lock = NativeRunLock.acquire(state, run_id=run_id)\n"
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
    lock = NativeRunLock.acquire(state, run_id=RUN_ID)
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
    record = lock_dir / native.LOCK_RECORD_NAME
    before = (
        record.read_bytes(),
        os.lstat(record).st_ino,
        os.lstat(record).st_mtime_ns,
        os.lstat(lock_dir).st_ino,
    )
    assert (
        _lock_reason(NativeRunLock.acquire, state, run_id=RUN_ID) == "lock_busy"
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
        "from ciao.native_installer import NativeRunLock\n"
        "lock = NativeRunLock.acquire(Path(sys.argv[1]), run_id=sys.argv[2])\n"
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
        _lock_reason(NativeRunLock.acquire, state, run_id=RUN_ID) == "lock_busy"
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
        _lock_reason(NativeRunLock.acquire, state, run_id=RUN_ID)
        == "lock_unknown"
    )
    assert (
        _lock_reason(NativeRunLock.acquire, state, run_id=RUN_ID)
        == "lock_unknown"
    )
    assert lock_dir.is_dir()
    assert list(lock_dir.iterdir()) == []

    lock_dir.rmdir()
    lock_dir = _make_lock_dir(state, OTHER_RUN_ID)
    record = lock_dir / native.LOCK_RECORD_NAME
    record.write_bytes(b'{"schema":1,"run_id":"')
    os.chmod(record, 0o600)
    before = record.read_bytes()
    assert (
        _lock_reason(NativeRunLock.acquire, state, run_id=RUN_ID)
        == "lock_unknown"
    )
    assert (
        _lock_reason(NativeRunLock.acquire, state, run_id=RUN_ID)
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
        if path == native.LOCK_DIR_NAME:
            raise FileNotFoundError(errno.ENOENT, "gone")
        return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(native.os, "stat", missing_stat)
    assert (
        _lock_reason(NativeRunLock.acquire, state, run_id=RUN_ID)
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
        if path == native.LOCK_DIR_NAME:
            raise FileNotFoundError(errno.ENOENT, "gone")
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(native.os, "open", missing_open)
    assert (
        _lock_reason(NativeRunLock.acquire, state, run_id=RUN_ID)
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
        if path == native.LOCK_RECORD_NAME:
            raise FileNotFoundError(errno.ENOENT, "gone")
        return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(native.os, "stat", missing_stat)
    assert (
        _lock_reason(NativeRunLock.acquire, state, run_id=RUN_ID)
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
        if path == native.LOCK_RECORD_NAME:
            raise FileNotFoundError(errno.ENOENT, "gone")
        return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(native.os, "open", missing_open)
    assert (
        _lock_reason(NativeRunLock.acquire, state, run_id=RUN_ID)
        == "lock_unknown"
    )
    assert lock_dir.is_dir()


def test_classification_maps_fstat_failure_to_lock_unsafe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state_dir(tmp_path)
    lock_dir = _make_lock_dir(state, OTHER_RUN_ID)
    real_fstat = native._fstat
    calls = 0

    def bad_fstat(fd: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(errno.EIO, "fstat failed")
        return real_fstat(fd)

    monkeypatch.setattr(native, "_fstat", bad_fstat)
    assert (
        _lock_reason(NativeRunLock.acquire, state, run_id=RUN_ID)
        == "lock_unsafe"
    )
    assert lock_dir.is_dir()


def test_an_incompatible_record_is_unsafe_not_unknown(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    lock_dir = _make_lock_dir(state, OTHER_RUN_ID)
    record = lock_dir / native.LOCK_RECORD_NAME
    record.write_text(
        json.dumps(
            {
                "schema": native.LOCK_SCHEMA_VERSION + 1,
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
        _lock_reason(NativeRunLock.acquire, state, run_id=RUN_ID) == "lock_unsafe"
    )
    assert record.exists()


def test_an_existing_lock_directory_is_never_repaired(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    lock_dir = _make_lock_dir(state, OTHER_RUN_ID)
    os.chmod(lock_dir, 0o755)
    assert (
        _lock_reason(NativeRunLock.acquire, state, run_id=RUN_ID) == "lock_unsafe"
    )
    assert stat.S_IMODE(os.lstat(lock_dir).st_mode) == 0o755


def test_an_existing_record_must_stay_owner_only(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    lock_dir = _make_lock_dir(state, OTHER_RUN_ID)
    record = lock_dir / native.LOCK_RECORD_NAME
    os.chmod(record, 0o644)
    content = record.read_bytes()
    assert (
        _lock_reason(NativeRunLock.acquire, state, run_id=RUN_ID) == "lock_unsafe"
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
        _lock_reason(NativeRunLock.acquire, state, run_id=RUN_ID) == "lock_unsafe"
    )
    assert link.is_symlink()
    assert target.is_dir()


def test_a_hard_linked_record_is_unsafe_and_blocks_cleanup(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(state, run_id=RUN_ID)
    link = state / "record-link"
    os.link(lock.record_path, link)
    assert (
        _lock_reason(NativeRunLock.acquire, state, run_id=OTHER_RUN_ID)
        == "lock_unsafe"
    )
    assert _lock_reason(lock.release) == "lock_cleanup_refused"
    assert lock.record_path.exists()
    assert link.exists()
    assert _lock_path(state).exists()


def test_a_replaced_record_is_not_removed(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(state, run_id=RUN_ID)
    original_inode = os.lstat(lock.record_path).st_ino
    os.unlink(lock.record_path)
    descriptor = os.open(
        lock.record_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    try:
        os.write(descriptor, _record_bytes(RUN_ID))
    finally:
        os.close(descriptor)
    assert os.lstat(lock.record_path).st_ino != original_inode
    assert _lock_reason(lock.release) == "lock_cleanup_refused"
    assert lock.record_path.exists()
    assert _lock_path(state).exists()


def test_a_replaced_lock_directory_is_not_removed(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(state, run_id=RUN_ID)
    original = state / "moved-lock"
    os.rename(lock.path, original)
    replacement = _make_lock_dir(state, OTHER_RUN_ID)
    assert replacement == lock.path
    assert _lock_reason(lock.release) == "lock_cleanup_refused"
    assert (original / native.LOCK_RECORD_NAME).exists()
    assert (lock.path / native.LOCK_RECORD_NAME).exists()


def test_a_symlinked_record_is_not_removed(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(state, run_id=RUN_ID)
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
    lock = NativeRunLock.acquire(state, run_id=RUN_ID)
    os.chmod(lock.record_path, 0o644)
    with pytest.raises(NativeLockError) as caught:
        lock.release()
    assert caught.value.reason == "lock_cleanup_refused"
    assert caught.value.cleanup == native.LockCleanupReport(False, False)
    assert lock.record_path.exists()
    assert stat.S_IMODE(os.lstat(lock.record_path).st_mode) == 0o644
    assert _lock_path(state).is_dir()


def test_release_refuses_a_lock_dir_made_non_private(tmp_path: Path) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(state, run_id=RUN_ID)
    os.chmod(lock.path, 0o755)
    with pytest.raises(NativeLockError) as caught:
        lock.release()
    assert caught.value.reason == "lock_cleanup_refused"
    assert caught.value.cleanup == native.LockCleanupReport(False, False)
    assert lock.record_path.exists()
    assert stat.S_IMODE(os.lstat(lock.path).st_mode) == 0o755


def test_release_maps_fstat_failure_to_cleanup_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(state, run_id=RUN_ID)

    def bad_fstat(fd: int) -> os.stat_result:
        raise OSError(errno.EIO, "fstat failed")

    monkeypatch.setattr(native, "_fstat", bad_fstat)
    with pytest.raises(NativeLockError) as caught:
        lock.release()
    assert caught.value.reason == "lock_cleanup_refused"
    assert caught.value.cleanup == native.LockCleanupReport(False, False)
    assert lock.record_path.exists()
    assert _lock_path(state).is_dir()


def test_release_refuses_same_inode_same_size_record_mutation(
    tmp_path: Path,
) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(state, run_id=RUN_ID)
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
    assert caught.value.cleanup == native.LockCleanupReport(False, False)
    assert lock.record_path.exists()
    assert _lock_path(state).is_dir()


def test_a_foreign_file_inside_the_lock_makes_cleanup_refuse(
    tmp_path: Path,
) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(state, run_id=RUN_ID)
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
        _lock_reason(NativeRunLock.acquire, state, run_id=RUN_ID) == "lock_unsafe"
    )
    assert foreign.read_text(encoding="utf-8") == "keep"
    assert (lock_dir / native.LOCK_RECORD_NAME).exists()


def test_a_record_write_failure_removes_only_the_new_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state_dir(tmp_path)
    sibling = state / "keep.txt"
    sibling.write_text("keep", encoding="utf-8")

    def explode(fd: int, data: bytes) -> None:
        raise OSError("boom")

    with monkeypatch.context() as patch:
        patch.setattr(native, "_write_record", explode)
        assert (
            _lock_reason(NativeRunLock.acquire, state, run_id=RUN_ID)
            == "lock_record_failed"
        )
    assert not _lock_path(state).exists()
    assert sibling.read_text(encoding="utf-8") == "keep"
    assert list(state.iterdir()) == [sibling]

    lock = NativeRunLock.acquire(state, run_id=RUN_ID)
    lock.release()
    assert not _lock_path(state).exists()


def test_the_context_manager_releases_on_success_and_failure(
    tmp_path: Path,
) -> None:
    state = _state_dir(tmp_path)
    with NativeRunLock.acquire(state, run_id=RUN_ID) as lock:
        assert lock.path.is_dir()
    assert not _lock_path(state).exists()

    with pytest.raises(RuntimeError):
        with NativeRunLock.acquire(state, run_id=OTHER_RUN_ID):
            raise RuntimeError("body failed")
    assert not _lock_path(state).exists()


def test_release_refuses_same_thread_reentrancy_without_fd_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state_dir(tmp_path)
    lock = NativeRunLock.acquire(state, run_id=RUN_ID)
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
    lock = NativeRunLock.acquire(state, run_id=RUN_ID)
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
    lock = NativeRunLock.acquire(state, run_id=RUN_ID)

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
    lock = NativeRunLock.acquire(state, run_id=RUN_ID)
    real_rmdir = native.os.rmdir

    def fail_rmdir(*args: object, **kwargs: object) -> None:
        raise OSError("rmdir failed")

    monkeypatch.setattr(native.os, "rmdir", fail_rmdir)
    try:
        with pytest.raises(NativeLockError) as caught:
            lock.release()
        assert caught.value.reason == "lock_cleanup_refused"
        assert caught.value.cleanup == native.LockCleanupReport(True, False)
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
    assert retry.value.cleanup == native.LockCleanupReport(True, False)
    assert (
        _lock_reason(NativeRunLock.acquire, state, run_id=OTHER_RUN_ID)
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
        with NativeRunLock.acquire(state, run_id=RUN_ID):
            raise body_error
    assert caught.value.reason == "lock_cleanup_refused"
    assert caught.value.__cause__ is body_error
    assert caught.value.cleanup == native.LockCleanupReport(True, False)
    assert _lock_path(state).is_dir()
    assert not _record_path(state).exists()


def test_a_failed_acquire_surfaces_cleanup_and_original_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state_dir(tmp_path)

    def explode(fd: int, data: bytes) -> None:
        raise OSError("write failed")

    def no_cleanup(*args: object, **kwargs: object) -> native.LockCleanupReport:
        return native.LockCleanupReport(False, False)

    with monkeypatch.context() as patch:
        patch.setattr(native, "_write_record", explode)
        patch.setattr(native, "_remove_new_lock_best_effort", no_cleanup)
        with pytest.raises(NativeLockError) as caught:
            NativeRunLock.acquire(state, run_id=RUN_ID)
    assert caught.value.reason == "lock_cleanup_refused"
    assert caught.value.original_reason == "lock_record_failed"
    assert caught.value.cleanup == native.LockCleanupReport(False, False)
    assert isinstance(caught.value.__cause__, NativeLockError)
    assert caught.value.__cause__.reason == "lock_record_failed"
    assert _lock_path(state).is_dir()
    assert _record_path(state).is_file()
    assert (
        _lock_reason(NativeRunLock.acquire, state, run_id=OTHER_RUN_ID)
        == "lock_unknown"
    )
