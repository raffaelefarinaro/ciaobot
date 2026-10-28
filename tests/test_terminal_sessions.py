"""Bounded terminal sessions: the manager that owns every shell (#1736).

The manager is exercised against a typed fake process and an injected
clock, so every race here is decided by the test rather than by
timing: the fake never blocks, never spawns a child and never reads a
real pty, and the clock only moves when the test moves it.

What is pinned, beyond the shapes:

* **Capacity is reserved before the spawn.** A fifth create is refused
  while four shells are still being started, so a burst of creates
  cannot start a fifth process.
* **A spawn that fails, is cancelled or is overtaken by a shutdown**
  closes the process it produced and releases the slot. Nothing is
  left running and nothing leaks.
* **A close is never abandoned.** A cancelled ``end()`` still closes
  the shell and is retryable; a cancelled ``shutdown()`` still closes
  every shell; a shell is closed exactly once.
* **One writer for the pending input.** A write is accepted, not
  delivered: the caller never waits for a terminal that will not take
  its bytes, and a write that is cancelled cannot duplicate the input.
  A terminal that will not take its input cannot stop its output.
* **A controller is a connection, not a browser.** Two tabs of the same
  browser are two controllers; the second one has to take over
  explicitly, and a stale detach, write or resize from the first one
  cannot touch the session afterwards.
* **Output is raw bytes at absolute offsets.** The ring is the only
  place output lives, an evicted offset is refused as ``slow_consumer``
  rather than queued for, and an initial attach with a stale offset
  reports truncation instead of silently starting in the wrong place.
* **Nothing about the terminal's contents is written or logged.**
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from pathlib import Path

import pytest

from ciao.os_support.terminal import TERMINAL_READ_BYTES
from ciao.terminal_replay import TERMINAL_REPLAY_BYTES
from ciao.terminal_sessions import (
    TERMINAL_DETACHED_TTL_S,
    TERMINAL_MAX_COLS,
    TERMINAL_MAX_PENDING_WRITE_BYTES,
    TERMINAL_MAX_ROWS,
    TERMINAL_MAX_SESSIONS,
    TERMINAL_MIN_COLS,
    TERMINAL_MIN_ROWS,
    TerminalSessionError,
    TerminalSessionManager,
    _READER_IDLE_S,
    _READER_MIN_IDLE_S,
)

#: Bytes a terminal really produces: a NUL, an invalid UTF-8 sequence,
#: a half escape sequence, a printable run, and a UTF-8 sequence cut in
#: half at the end.
TERMINAL_NOISE = b"\x00\xff\xfe\x1b[3\x1b]0;tit\x08\r\n\xe2\x82"


class FakeClock:
    """A monotonic clock the test moves by hand. No sleeps, no wall time."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class FakeProcess:
    """A terminal that answers from a buffer, deterministically.

    It is not a pty and it never blocks on its own: ``read`` hands back
    what was fed to it and then reports that the terminal is quiet, so
    the reader's polling loop is driven by the test, not by a shell.

    It *can* be made to block, because the races that matter are the
    ones where the terminal is slow to take its input: a write can be
    held inside its worker thread with a gate, and the close can be
    held the same way. The gates are consumed one write at a time, so
    a test can freeze the manager in a known state.
    """

    def __init__(self, pid: int = 1) -> None:
        self.pid = pid
        self._inbox = bytearray()
        self._final = bytearray()
        self._quiet_after_exit = 0
        self.written = bytearray()
        self.closed = False
        self.exit_code: int | None = None
        self.eof = False
        #: How many bytes one write takes. ``None`` means "all of them",
        #: ``0`` means "none right now".
        self.write_takes: int | None = None
        self.resizes: list[tuple[int, int]] = []
        #: How many times the shell was closed. A shell is closed once.
        self.close_count = 0
        #: The thread ids of the calls that must not run on the event
        #: loop: the spawn, the write, the resize and the close.
        self.write_threads: list[int] = []
        self.close_threads: list[int] = []
        self.resize_threads: list[int] = []
        #: Gates that hold a write inside its worker thread, one per
        #: write, in order.
        self.write_gates: list[threading.Event] = []
        self.write_started = threading.Event()
        #: A gate that holds the close inside its worker thread.
        self.close_gate: threading.Event | None = None
        self.close_started = threading.Event()
        #: What a write raises, when the test wants a failed terminal.
        self.write_error: BaseException | None = None

    def feed(self, data: bytes) -> None:
        """Make ``data`` the terminal's next output."""
        self._inbox.extend(data)

    def finish(self, exit_code: int = 0, final: bytes = b"") -> None:
        """End the terminal's output and record its exit code.

        ``final`` is the output a shell produces *after* it has exited
        but before the terminal is drained. The terminal answers
        "nothing yet" once before handing those bytes over, so a reader
        that stopped on the exit code would lose them — which is
        exactly the race the manager has to get right.
        """
        self.exit_code = exit_code
        self._final.extend(final)
        self._quiet_after_exit = 1
        self.eof = True

    def read(self, size: int = TERMINAL_READ_BYTES) -> bytes | None:
        if self.closed:
            return b""
        if self._inbox:
            chunk = bytes(self._inbox[:size])
            del self._inbox[:size]
            return chunk
        if self._final:
            if self._quiet_after_exit > 0:
                self._quiet_after_exit -= 1
                return None
            chunk = bytes(self._final[:size])
            del self._final[:size]
            return chunk
        return b"" if self.eof else None

    def write(self, data: bytes) -> int:
        if self.closed:
            raise OSError("the terminal is closed")
        if self.write_takes is None:
            took = len(data)
        else:
            took = min(self.write_takes, len(data))
        self.write_threads.append(threading.get_ident())
        self.write_started.set()
        if self.write_gates:
            gate = self.write_gates.pop(0)
            gate.wait(timeout=10)
        if self.write_error is not None:
            raise self.write_error
        self.written.extend(data[:took])
        return took

    def resize(self, cols: int, rows: int) -> None:
        if self.closed:
            raise OSError("the terminal is closed")
        self.resize_threads.append(threading.get_ident())
        self.resizes.append((cols, rows))

    def poll(self) -> int | None:
        return self.exit_code

    def close(self) -> None:
        self.close_threads.append(threading.get_ident())
        self.close_count += 1
        self.close_started.set()
        if self.close_gate is not None:
            self.close_gate.wait(timeout=10)
        self.closed = True


class FakeFactory:
    """A process factory the test controls: how many, and how slow.

    The gate holds a spawn inside its worker thread, which is how the
    tests reach the window between "the slot is reserved" and "the shell
    exists" without racing anything.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.processes: list[FakeProcess] = []
        self.gate: threading.Event | None = None
        #: A gate that every process this factory makes inherits, so a
        #: test can hold a close inside its worker thread.
        self.close_gate: threading.Event | None = None
        self.error: BaseException | None = None
        self._lock = threading.Lock()

    def __call__(self, *, cwd: Path, cols: int = 80, rows: int = 24) -> FakeProcess:
        with self._lock:
            self.calls += 1
            index = self.calls
        if self.gate is not None:
            self.gate.wait(timeout=10)
        if self.error is not None:
            raise self.error
        process = FakeProcess(pid=4_000 + index)
        process.close_gate = self.close_gate
        with self._lock:
            self.processes.append(process)
        return process

    @property
    def process(self) -> FakeProcess:
        """The only process this factory has made."""
        assert len(self.processes) == 1, self.processes
        return self.processes[0]


class HugePayload(bytes):
    """Bytes that claim to be huge and refuse to be copied.

    The payload is refused for its *size*, before it is copied, so the
    copy must never happen: ``bytes()`` on this raises.
    """

    def __new__(cls, size: int) -> HugePayload:
        return super().__new__(cls, b"")

    def __init__(self, size: int) -> None:
        self._size = size

    def __len__(self) -> int:
        return self._size

    def __bytes__(self) -> bytes:
        raise AssertionError("the payload was copied")


async def _wait_until(
    predicate, timeout: float = 5.0, what: str = "condition"
) -> None:
    """Wait for ``predicate`` to hold, or fail the test."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"{what} never became true")


def _manager(
    factory: FakeFactory | None = None, clock: FakeClock | None = None
) -> TerminalSessionManager:
    return TerminalSessionManager(
        clock=clock or FakeClock(), factory=factory or FakeFactory()
    )


async def _reason(callable_, *args, **kwargs) -> str:
    """The reason of the refusal ``callable_`` raises.

    The call may be a coroutine function, so the result is awaited when
    it is awaitable: the point of the test is that the module refuses
    the call, whichever shape it has.
    """
    with pytest.raises(TerminalSessionError) as caught:
        result = callable_(*args, **kwargs)
        if inspect.isawaitable(result):
            await result
    return caught.value.reason


def _terminal_tasks(prefix: str = "terminal-") -> list[str]:
    """The names of the manager's tasks that are still running."""
    return [
        task.get_name()
        for task in asyncio.all_tasks()
        if task.get_name().startswith(prefix)
    ]


# ── the bounds ────────────────────────────────────────────────────────────


def test_the_bounds_are_the_ones_the_contract_states() -> None:
    """The numbers are the contract's, not a value the test picked."""
    assert TERMINAL_MAX_SESSIONS == 4
    assert TERMINAL_DETACHED_TTL_S == 900
    assert TERMINAL_MAX_PENDING_WRITE_BYTES == 65536
    assert TERMINAL_MIN_COLS == 20
    assert TERMINAL_MAX_COLS == 500
    assert TERMINAL_MIN_ROWS == 5
    assert TERMINAL_MAX_ROWS == 200


# ── capacity ──────────────────────────────────────────────────────────────


async def test_create_reserves_capacity_before_spawning_and_refuses_fifth_session(
    tmp_path: Path,
) -> None:
    """The slot is taken before the shell exists, so a fifth is refused."""
    factory = FakeFactory()
    gate = threading.Event()
    factory.gate = gate
    manager = _manager(factory)

    creates = [
        asyncio.create_task(manager.create(tmp_path, "chat", f"chat-{index}"))
        for index in range(TERMINAL_MAX_SESSIONS)
    ]
    # All four spawns are inside their worker threads, so none of them
    # has produced a shell yet.
    await _wait_until(
        lambda: factory.calls == TERMINAL_MAX_SESSIONS, what="four spawns started"
    )
    assert manager.retained_count() == TERMINAL_MAX_SESSIONS

    assert (
        await _reason(manager.create, tmp_path, "chat", "chat-extra")
        == "capacity"
    )

    gate.set()
    infos = await asyncio.gather(*creates)
    assert len(infos) == TERMINAL_MAX_SESSIONS
    assert manager.retained_count() == TERMINAL_MAX_SESSIONS
    assert factory.calls == TERMINAL_MAX_SESSIONS

    await manager.shutdown()


async def test_spawn_failure_or_cancel_closes_child_and_releases_reserved_slot(
    tmp_path: Path,
) -> None:
    """A spawn that fails or is cancelled leaves no shell and no slot."""
    factory = FakeFactory()
    factory.error = OSError("the shell could not start")
    manager = _manager(factory)

    assert await _reason(manager.create, tmp_path, "chat", "chat-1") == "spawn_failed"
    assert manager.retained_count() == 0

    # The reserved slot was released: the next create is admitted.
    factory.error = None
    info = await manager.create(tmp_path, "chat", "chat-2")
    assert manager.retained_count() == 1
    await manager.shutdown()
    assert factory.process.closed is True
    assert info.session_id != ""

    # The same for a create that is cancelled while the shell is starting.
    cancel_factory = FakeFactory()
    gate = threading.Event()
    cancel_factory.gate = gate
    cancel_manager = _manager(cancel_factory)

    create = asyncio.create_task(cancel_manager.create(tmp_path, "chat", "chat-3"))
    await _wait_until(lambda: cancel_factory.calls == 1, what="the spawn started")
    create.cancel()
    with pytest.raises(asyncio.CancelledError):
        await create

    assert cancel_manager.retained_count() == 0
    gate.set()
    # The process that arrives after the cancellation is closed, not leaked.
    await _wait_until(
        lambda: bool(cancel_factory.processes)
        and cancel_factory.processes[0].closed,
        what="the late process to be closed",
    )
    await cancel_manager.shutdown()


async def test_shutdown_during_spawn_observes_and_closes_the_eventual_process(
    tmp_path: Path,
) -> None:
    """A shutdown that races a spawn closes what the spawn produces."""
    factory = FakeFactory()
    gate = threading.Event()
    factory.gate = gate
    manager = _manager(factory)

    create = asyncio.create_task(manager.create(tmp_path, "chat", "chat-1"))
    await _wait_until(lambda: factory.calls == 1, what="the spawn started")

    shutdown = asyncio.create_task(manager.shutdown())
    await asyncio.sleep(0.05)
    gate.set()
    await shutdown

    assert bool(factory.processes)
    assert factory.processes[0].closed is True

    # The create that was overtaken is refused, not left hanging.
    with pytest.raises(TerminalSessionError) as caught:
        await create
    assert caught.value.reason == "closed"


# ── scoping ───────────────────────────────────────────────────────────────


async def test_list_is_scoped_to_exact_target_id_and_kind(tmp_path: Path) -> None:
    """A chat session and a project session with the same id are two."""
    manager = _manager()

    chat = await manager.create(tmp_path, "chat", "abc")
    project = await manager.create(tmp_path, "project", "abc")
    other = await manager.create(tmp_path, "chat", "abcd")

    listed = manager.list_for("chat", "abc")
    assert [info.session_id for info in listed] == [chat.session_id]
    assert [info.session_id for info in manager.list_for("project", "abc")] == [
        project.session_id
    ]
    assert [info.session_id for info in manager.list_for("chat", "abcd")] == [
        other.session_id
    ]
    assert manager.list_for("chat", "nothing") == []

    await manager.shutdown()


# ── controlling ───────────────────────────────────────────────────────────


async def test_attach_detach_attach_never_spawns_a_second_shell(
    tmp_path: Path,
) -> None:
    """A session is reattached, not restarted."""
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    first = manager.attach(info.session_id, "conn-1")
    assert first.next_offset == 0
    assert first.info.attached is True

    detached = manager.detach(info.session_id, "conn-1")
    assert detached.attached is False
    assert detached.detached_since is not None

    second = manager.attach(info.session_id, "conn-2")
    assert second.next_offset == 0
    assert factory.calls == 1

    await manager.shutdown()


async def test_same_browser_tabs_still_need_explicit_controller_takeover(
    tmp_path: Path,
) -> None:
    """Two tabs are two controllers, even with the same grant behind them."""
    manager = _manager()

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")

    assert await _reason(manager.attach, info.session_id, "conn-2") == "taken_over"

    taken = manager.attach(info.session_id, "conn-2", takeover=True)
    assert taken.generation == 1
    assert taken.info.attached is True

    # The displaced connection is not the controller any more.
    assert (
        await _reason(manager.write_input, info.session_id, "conn-1", b"x")
        == "taken_over"
    )
    assert manager.get(info.session_id).attached is True

    await manager.shutdown()


async def test_stale_detach_write_and_resize_cannot_affect_new_controller(
    tmp_path: Path,
) -> None:
    """The old connection's calls cannot reach the session any more."""
    manager = _manager()

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")
    manager.attach(info.session_id, "conn-2", takeover=True)

    assert await _reason(manager.detach, info.session_id, "conn-1") == "taken_over"
    assert (
        await _reason(manager.write_input, info.session_id, "conn-1", b"x")
        == "taken_over"
    )
    assert (
        await _reason(manager.resize, info.session_id, "conn-1", 100, 40)
        == "taken_over"
    )

    # The successor still holds the session, and it still works.
    assert manager.get(info.session_id).attached is True
    assert await manager.write_input(info.session_id, "conn-2", b"x") == 1

    await manager.shutdown()


async def test_a_detached_session_has_no_controller_to_detach_from(
    tmp_path: Path,
) -> None:
    """A detach without a controller is refused, not a no-op."""
    manager = _manager()

    info = await manager.create(tmp_path, "chat", "chat-1")

    assert await _reason(manager.detach, info.session_id, "conn-1") == "not_controller"

    await manager.shutdown()


async def test_the_session_info_carries_the_contract_envelope(
    tmp_path: Path,
) -> None:
    """The record names the folder, the window and the byte position."""
    manager = _manager()

    info = await manager.create(tmp_path, "chat", "chat-1")
    assert info.folder == tmp_path
    assert (info.cols, info.rows) == (80, 24)
    assert info.seq == 0
    assert info.replay_start == 0

    manager.attach(info.session_id, "conn-1")
    await manager.resize(info.session_id, "conn-1", 100, 40)

    resized = manager.get(info.session_id)
    assert (resized.cols, resized.rows) == (100, 40)

    await manager.shutdown()


# ── output ────────────────────────────────────────────────────────────────


async def test_output_keeps_raw_bytes_and_absolute_half_open_offsets(
    tmp_path: Path,
) -> None:
    """The bytes are the bytes, and the offsets are half-open."""
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")

    factory.process.feed(TERMINAL_NOISE)
    await _wait_until(
        lambda: manager.get(info.session_id).seq == len(TERMINAL_NOISE),
        what="the output to land",
    )

    first = manager.read(info.session_id, "conn-1", 0)
    assert first.data == TERMINAL_NOISE
    assert (first.start, first.end) == (0, len(TERMINAL_NOISE))

    # A UTF-8 sequence split across two reads is not repaired: the
    # bytes stay exactly as the terminal produced them.
    factory.process.feed(b"\xac")
    await _wait_until(
        lambda: manager.get(info.session_id).seq == len(TERMINAL_NOISE) + 1,
        what="the second half to land",
    )
    whole = manager.read(info.session_id, "conn-1", 0)
    assert whole.data == TERMINAL_NOISE + b"\xac"
    assert whole.end == len(TERMINAL_NOISE) + 1

    await manager.shutdown()


async def test_replay_is_bounded_and_old_initial_cursor_reports_truncation(
    tmp_path: Path,
) -> None:
    """The retained window is bounded, and a stale attach says so."""
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")

    total = TERMINAL_REPLAY_BYTES + 4_096
    payload = bytes((index * 7) % 251 for index in range(total))
    factory.process.feed(payload)
    await _wait_until(
        lambda: manager.get(info.session_id).seq == total,
        what="the output to land",
    )

    attachment = manager.attach(info.session_id, "conn-1", 0)
    assert attachment.snapshot.truncated is True
    assert attachment.snapshot.start == total - TERMINAL_REPLAY_BYTES
    assert attachment.snapshot.end == total
    assert len(attachment.snapshot.data) == TERMINAL_REPLAY_BYTES
    assert attachment.snapshot.data == payload[-TERMINAL_REPLAY_BYTES:]

    # Nothing beyond the window was kept.
    state = manager.get(info.session_id)
    assert state.seq == total
    assert state.replay_start == total - TERMINAL_REPLAY_BYTES

    await manager.shutdown()


async def test_replay_reports_truncation_only_for_an_evicted_offset(
    tmp_path: Path,
) -> None:
    """A replay from a retained offset is whole; from an evicted one it is not."""
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")

    total = TERMINAL_REPLAY_BYTES + 1_024
    factory.process.feed(bytes(total))
    await _wait_until(
        lambda: manager.get(info.session_id).seq == total, what="the output to land"
    )

    evicted = manager.replay(info.session_id, "conn-1", 0)
    assert evicted.truncated is True
    assert evicted.start == total - TERMINAL_REPLAY_BYTES
    assert evicted.end == total

    retained = manager.replay(info.session_id, "conn-1", total - 16)
    assert retained.truncated is False
    assert retained.start == total - 16
    assert retained.end == total
    assert len(retained.data) == 16

    await manager.shutdown()


async def test_invalid_future_negative_boolean_or_unsafe_offset_is_refused(
    tmp_path: Path,
) -> None:
    """A bad offset is refused before the session is touched."""
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")

    # Some output has landed, so the sequence is past 1: a bool of True
    # would look like a perfectly good offset (1) if the type were not
    # checked exactly. It is refused anyway.
    factory.process.feed(b"abc")
    await _wait_until(
        lambda: manager.get(info.session_id).seq == 3, what="the output"
    )

    for bad in (-1, True, False, 4, 2**53, 2**53 + 1):
        assert (
            await _reason(manager.attach, info.session_id, "conn-1", bad)
            == "invalid_offset"
        )

    # The refused attaches changed nothing: the session is still free.
    assert manager.get(info.session_id).attached is False
    assert manager.attach(info.session_id, "conn-1", 0).next_offset == 3

    # The valid offsets are the ones the stream actually has.
    assert manager.attach(info.session_id, "conn-1", 3).next_offset == 3

    await manager.shutdown()


async def test_snapshot_then_live_output_has_no_gap_or_duplicate_under_concurrent_append(
    tmp_path: Path,
) -> None:
    """The initial replay and the live reads cover the stream once."""
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")

    factory.process.feed(b"first-")
    await _wait_until(
        lambda: manager.get(info.session_id).seq == 6, what="the first bytes"
    )

    attachment = manager.attach(info.session_id, "conn-1", 0)
    assert attachment.snapshot.data == b"first-"
    assert attachment.next_offset == 6

    tail = b"".join(f"-{index}".encode() for index in range(10))
    total = 6 + len(tail)

    async def _feeder() -> None:
        for index in range(10):
            factory.process.feed(f"-{index}".encode())
            await asyncio.sleep(0.005)

    feeder = asyncio.create_task(_feeder())
    collected = bytearray(attachment.snapshot.data)
    offset = attachment.next_offset
    await _wait_until(
        lambda: manager.get(info.session_id).seq == total,
        what="the tail to land",
    )
    while offset < total:
        piece = manager.read(info.session_id, "conn-1", offset)
        assert piece.start == offset
        collected.extend(piece.data)
        offset = piece.end
    await feeder

    assert bytes(collected) == b"first-" + tail

    await manager.shutdown()


async def test_slow_consumer_loses_access_instead_of_growing_output_queue(
    tmp_path: Path,
) -> None:
    """A client that fell behind is refused; nothing is queued for it."""
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")

    total = TERMINAL_REPLAY_BYTES + 1_024
    factory.process.feed(bytes(total))
    await _wait_until(
        lambda: manager.get(info.session_id).seq == total,
        what="the output to land",
    )

    assert (
        await _reason(manager.read, info.session_id, "conn-1", 0) == "slow_consumer"
    )

    state = manager.get(info.session_id)
    # The retained window is exactly the ring's window: no backlog was
    # kept for the client that fell behind.
    assert state.seq - state.replay_start == TERMINAL_REPLAY_BYTES

    await manager.shutdown()


async def test_a_read_limit_is_a_usable_chunk_size_or_it_is_refused(
    tmp_path: Path,
) -> None:
    """A bad limit is refused; a small one gives exactly that many bytes."""
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")

    factory.process.feed(b"0123456789")
    await _wait_until(
        lambda: manager.get(info.session_id).seq == 10, what="the output to land"
    )

    for bad in (0, 65537, True):
        assert (
            await _reason(manager.read, info.session_id, "conn-1", 0, bad)
            == "invalid_limit"
        )

    piece = manager.read(info.session_id, "conn-1", 0, 4)
    assert piece.data == b"0123"
    assert (piece.start, piece.end) == (0, 4)

    await manager.shutdown()


# ── input ─────────────────────────────────────────────────────────────────


async def test_partial_nonblocking_writes_are_retried_with_bounded_pending_bytes(
    tmp_path: Path,
) -> None:
    """Bytes the terminal refuses stay pending, and the backlog is bounded."""
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")
    factory.process.write_takes = 4

    assert await manager.write_input(info.session_id, "conn-1", b"0123456789") == 10
    await _wait_until(
        lambda: manager.get(info.session_id).pending_write_bytes == 0,
        what="the pending bytes to be delivered",
    )
    assert bytes(factory.process.written) == b"0123456789"

    # A terminal that takes nothing piles up nothing: the backlog has a
    # ceiling and a payload past it is refused.
    stuck_factory = FakeFactory()
    stuck_manager = _manager(stuck_factory)
    stuck = await stuck_manager.create(tmp_path, "chat", "chat-2")
    stuck_manager.attach(stuck.session_id, "conn-1")
    stuck_factory.process.write_takes = 0

    assert (
        await _reason(
            stuck_manager.write_input,
            stuck.session_id,
            "conn-1",
            b"x" * (TERMINAL_MAX_PENDING_WRITE_BYTES + 1),
        )
        == "capacity"
    )
    assert stuck_manager.get(stuck.session_id).pending_write_bytes == 0

    await stuck_manager.shutdown()
    await manager.shutdown()


async def test_the_pending_ceiling_admits_exactly_its_size_and_refuses_one_more(
    tmp_path: Path,
) -> None:
    """The backlog is bounded to the byte, not to a rounded number."""
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")
    # The terminal takes nothing, so everything stays pending.
    factory.process.write_takes = 0

    accepted = b"x" * TERMINAL_MAX_PENDING_WRITE_BYTES
    assert await manager.write_input(info.session_id, "conn-1", accepted) == len(accepted)
    await _wait_until(
        lambda: manager.get(info.session_id).pending_write_bytes
        == TERMINAL_MAX_PENDING_WRITE_BYTES,
        what="the backlog to fill up",
    )

    assert (
        await _reason(manager.write_input, info.session_id, "conn-1", b"x")
        == "capacity"
    )
    assert manager.get(info.session_id).pending_write_bytes == (
        TERMINAL_MAX_PENDING_WRITE_BYTES
    )

    await manager.shutdown()


async def test_pending_input_is_delivered_in_order_across_several_drains(
    tmp_path: Path,
) -> None:
    """The queue is FIFO: what is pending goes out before what is new."""
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")
    # The terminal takes three bytes at a time, so the backlog drains
    # over several turns.
    factory.process.write_takes = 3

    assert await manager.write_input(info.session_id, "conn-1", b"abcdef") == 6
    assert await manager.write_input(info.session_id, "conn-1", b"ghij") == 4

    await _wait_until(
        lambda: manager.get(info.session_id).pending_write_bytes == 0,
        what="the backlog to drain",
    )
    assert bytes(factory.process.written) == b"abcdefghij"

    await manager.shutdown()


async def test_the_flusher_puts_back_exactly_the_bytes_the_terminal_refused(
    tmp_path: Path,
) -> None:
    """A partial write keeps the remainder — and only the remainder."""
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")
    factory.process.write_takes = 3
    first = threading.Event()
    second = threading.Event()
    factory.process.write_gates = [first, second]

    assert await manager.write_input(info.session_id, "conn-1", b"abcdefgh") == 8
    await _wait_until(
        lambda: factory.process.write_started.is_set(), what="the write to start"
    )
    # The first write goes out and takes three of the eight bytes; the
    # second write is held, so the state is frozen and observable.
    first.set()
    await _wait_until(
        lambda: manager.get(info.session_id).pending_write_bytes == 5,
        what="the remainder to be pending",
    )
    assert bytes(factory.process.written) == b"abc"

    second.set()
    await _wait_until(
        lambda: manager.get(info.session_id).pending_write_bytes == 0,
        what="the rest to be delivered",
    )
    assert bytes(factory.process.written) == b"abcdefgh"

    await manager.shutdown()


async def test_a_write_is_accepted_without_waiting_for_the_terminal(
    tmp_path: Path,
) -> None:
    """The caller never waits for a write that can block on a full pipe."""
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")
    gate = threading.Event()
    factory.process.write_gates = [gate]

    write = asyncio.create_task(
        manager.write_input(info.session_id, "conn-1", b"hello")
    )
    await asyncio.sleep(0.05)

    # The write is inside the terminal's pipe, and the caller is not
    # waiting for it.
    assert factory.process.write_started.is_set()
    assert write.done()

    gate.set()
    await _wait_until(
        lambda: bytes(factory.process.written) == b"hello", what="the write to land"
    )

    await manager.shutdown()


async def test_a_cancelled_write_does_not_duplicate_the_input(
    tmp_path: Path,
) -> None:
    """A write that is cancelled is not written twice.

    The bytes leave the queue before the write, and exactly the bytes
    the terminal did not take go back. A cancelled write therefore
    neither duplicates the input nor loses it.
    """
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")
    gate = threading.Event()
    factory.process.write_gates = [gate]

    # The write is accepted, and the flusher takes the bytes into the
    # terminal's pipe.
    assert await manager.write_input(info.session_id, "conn-1", b"UNIQUE") == 6
    await _wait_until(
        lambda: factory.process.write_started.is_set(), what="the write to start"
    )

    # The flusher is cancelled while the write is in flight.
    flusher = next(
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("terminal-flusher-")
    )
    flusher.cancel()
    await asyncio.sleep(0.05)

    gate.set()
    await _wait_until(
        lambda: bytes(factory.process.written) == b"UNIQUE", what="the write to land"
    )
    # Give a second flush every chance to write the same bytes again.
    await asyncio.sleep(0.3)
    assert bytes(factory.process.written) == b"UNIQUE"

    await manager.shutdown()


async def test_a_cancelled_write_puts_back_only_the_refused_bytes(
    tmp_path: Path,
) -> None:
    """A cancelled write keeps the remainder — and only the remainder."""
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")
    factory.process.write_takes = 3
    gate = threading.Event()
    factory.process.write_gates = [gate]

    assert await manager.write_input(info.session_id, "conn-1", b"abcdefgh") == 8
    await _wait_until(
        lambda: factory.process.write_started.is_set(), what="the write to start"
    )

    flusher = next(
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("terminal-flusher-")
    )
    flusher.cancel()
    await asyncio.sleep(0.05)
    gate.set()

    # The terminal took three of the eight bytes; the other five are
    # pending again, not written twice and not lost.
    await _wait_until(
        lambda: manager.get(info.session_id).pending_write_bytes == 5,
        what="the remainder to be pending",
    )
    assert bytes(factory.process.written) == b"abc"

    await manager.shutdown()


async def test_output_keeps_flowing_while_a_write_is_blocked(
    tmp_path: Path,
) -> None:
    """A terminal that will not take its input cannot stop its output."""
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")
    gate = threading.Event()
    factory.process.write_gates = [gate]

    await manager.write_input(info.session_id, "conn-1", b"input")
    await _wait_until(
        lambda: factory.process.write_started.is_set(), what="the write to start"
    )

    # The write is still blocked inside the terminal's pipe.
    factory.process.feed(b"output")
    await _wait_until(
        lambda: manager.get(info.session_id).seq == len(b"output"),
        what="the output to land",
    )
    assert manager.read(info.session_id, "conn-1", 0).data == b"output"

    gate.set()
    await manager.shutdown()


async def test_a_stale_write_cannot_reach_the_session_after_a_takeover(
    tmp_path: Path,
) -> None:
    """The displaced connection's bytes never reach the shell.

    The controller check and the enqueue are one step, with no await
    in between: a write is either refused, or it is already part of
    the session's input. There is no queued write that a takeover
    could promote to the successor's shell.
    """
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")
    gate = threading.Event()
    factory.process.write_gates = [gate]

    # The first write is blocked inside the terminal's pipe.
    first = asyncio.create_task(
        manager.write_input(info.session_id, "conn-1", b"first")
    )
    await _wait_until(
        lambda: factory.process.write_started.is_set(), what="the write to start"
    )

    # A second write from the same connection is accepted at once: it
    # is not queued behind the blocked write, so the controller check
    # and the enqueue cannot drift apart.
    second = asyncio.create_task(
        manager.write_input(info.session_id, "conn-1", b"second")
    )
    await asyncio.sleep(0.05)
    assert second.done(), "the write waited for the terminal"

    # The successor takes over.
    manager.attach(info.session_id, "conn-2", takeover=True)

    # The displaced connection is refused, and its bytes never land.
    assert (
        await _reason(manager.write_input, info.session_id, "conn-1", b"stale")
        == "taken_over"
    )

    gate.set()
    await first
    await _wait_until(
        lambda: bytes(factory.process.written).startswith(b"first"),
        what="the write to land",
    )
    await asyncio.sleep(0.2)
    assert b"stale" not in bytes(factory.process.written)

    await manager.shutdown()


async def test_a_huge_payload_is_refused_without_being_copied(
    tmp_path: Path,
) -> None:
    """The size is checked before the payload is copied."""
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")

    payload = HugePayload(64 * 1024 * 1024)
    assert (
        await _reason(manager.write_input, info.session_id, "conn-1", payload)
        == "capacity"
    )
    # The payload was never copied, and the terminal never saw it.
    assert factory.process.write_threads == []
    assert manager.get(info.session_id).pending_write_bytes == 0

    await manager.shutdown()


async def test_input_that_is_not_bytes_is_refused(tmp_path: Path) -> None:
    """The input is bytes, and nothing else is."""
    manager = _manager()

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")

    assert (
        await _reason(manager.write_input, info.session_id, "conn-1", "text")
        == "invalid_input"
    )
    assert (
        await _reason(manager.write_input, info.session_id, "conn-1", 42)
        == "invalid_input"
    )

    await manager.shutdown()


async def test_a_failed_write_marks_the_session_failed(tmp_path: Path) -> None:
    """A terminal that refuses the input says so in the record."""
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")
    factory.process.write_error = OSError("the terminal broke")

    assert await manager.write_input(info.session_id, "conn-1", b"x") == 1
    await _wait_until(
        lambda: manager.get(info.session_id).failed, what="the session to fail"
    )
    assert manager.get(info.session_id).failed is True

    await manager.shutdown()


async def test_resize_bounds_and_bool_are_refused_before_process_call(
    tmp_path: Path,
) -> None:
    """A bad size never reaches the process."""
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")

    for cols, rows in (
        (True, 24),
        (80, True),
        (0, 24),
        (19, 24),
        (501, 24),
        (80, 0),
        (80, 4),
        (80, 201),
        (80.0, 24),
    ):
        assert (
            await _reason(manager.resize, info.session_id, "conn-1", cols, rows)
            == "invalid_size"
        )
    assert factory.process.resizes == []

    await manager.resize(info.session_id, "conn-1", 100, 40)
    assert factory.process.resizes == [(100, 40)]

    await manager.shutdown()


async def test_the_process_calls_run_off_the_event_loop(tmp_path: Path) -> None:
    """The write, the resize and the close never run on the loop."""
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")
    await manager.write_input(info.session_id, "conn-1", b"x")
    await manager.resize(info.session_id, "conn-1", 100, 40)
    await _wait_until(
        lambda: manager.get(info.session_id).pending_write_bytes == 0,
        what="the write to land",
    )
    await manager.end(info.session_id)

    loop_thread = threading.get_ident()
    assert factory.process.write_threads
    assert factory.process.resize_threads
    assert factory.process.close_threads
    for thread in (
        *factory.process.write_threads,
        *factory.process.resize_threads,
        *factory.process.close_threads,
    ):
        assert thread != loop_thread


# ── lifetimes ─────────────────────────────────────────────────────────────


async def test_detached_ttl_is_exact_and_output_does_not_extend_it(
    tmp_path: Path,
) -> None:
    """The detached deadline is exact, and output does not move it."""
    clock = FakeClock()
    factory = FakeFactory()
    manager = _manager(factory, clock)

    info = await manager.create(tmp_path, "chat", "chat-1")

    clock.advance(TERMINAL_DETACHED_TTL_S - 1)
    assert await manager.reap_expired() == []
    assert manager.retained_count() == 1

    # Output does not extend the deadline.
    factory.process.feed(b"noise")
    await _wait_until(
        lambda: manager.get(info.session_id).seq == 5, what="the output"
    )
    clock.advance(1)

    assert await manager.reap_expired() == [info.session_id]
    assert manager.retained_count() == 0
    assert factory.process.closed is True

    await manager.shutdown()


async def test_attached_session_is_not_expired_by_detach_sweeper(
    tmp_path: Path,
) -> None:
    """An attached session is the route's business, not the sweeper's."""
    clock = FakeClock()
    manager = _manager(clock=clock)

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")

    clock.advance(TERMINAL_DETACHED_TTL_S * 10)
    assert await manager.reap_expired() == []
    assert manager.retained_count() == 1
    assert manager.get(info.session_id).attached is True

    # Detaching starts the clock again, from the detach.
    manager.detach(info.session_id, "conn-1")
    clock.advance(TERMINAL_DETACHED_TTL_S - 1)
    assert await manager.reap_expired() == []
    clock.advance(1)
    assert await manager.reap_expired() == [info.session_id]

    await manager.shutdown()


async def test_the_sweeper_prunes_by_itself(tmp_path: Path) -> None:
    """The sweeper does the pruning; the test does not have to."""
    clock = FakeClock()
    factory = FakeFactory()
    manager = _manager(factory, clock)
    await manager.start()

    info = await manager.create(tmp_path, "chat", "chat-1")
    assert manager.retained_count() == 1

    clock.advance(TERMINAL_DETACHED_TTL_S)
    await _wait_until(
        lambda: manager.retained_count() == 0, what="the sweeper to prune"
    )
    # The shell the sweeper pruned is closed, not left running.
    await _wait_until(
        lambda: factory.process.closed, what="the pruned shell to be closed"
    )
    assert info.session_id != ""

    await manager.shutdown()


async def test_the_sweeper_never_prunes_an_attached_session(
    tmp_path: Path,
) -> None:
    """A watched shell is the route's business, not the sweeper's."""
    clock = FakeClock()
    manager = _manager(clock=clock)
    await manager.start()

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")

    clock.advance(TERMINAL_DETACHED_TTL_S * 10)
    # The sweeper runs on its own; give it two of its ticks.
    await asyncio.sleep(1.2)

    assert manager.retained_count() == 1
    assert manager.get(info.session_id).attached is True

    await manager.shutdown()


async def test_an_attach_that_lands_in_the_prune_window_survives(
    tmp_path: Path,
) -> None:
    """The decision and the removal happen under one lock.

    An attach that arrives while the sweeper is deciding cannot have
    its session closed under it: either the attach is refused because
    the record is already gone, or the sweeper sees the controller and
    leaves the session alone.
    """
    clock = FakeClock()
    manager = _manager(clock=clock)

    info = await manager.create(tmp_path, "chat", "chat-1")
    clock.advance(TERMINAL_DETACHED_TTL_S)

    attach = asyncio.create_task(
        asyncio.to_thread(manager.attach, info.session_id, "conn-1")
    )
    reap = asyncio.create_task(manager.reap_expired())
    await asyncio.gather(attach, reap, return_exceptions=True)

    if manager.retained_count() == 1:
        # The attach won the race: the session is alive and held.
        assert manager.get(info.session_id).attached is True
    else:
        # The prune won it: the record is gone and the attach was
        # refused, never half-applied.
        assert await _reason(manager.attach, info.session_id, "conn-1") == "not_found"

    await manager.shutdown()


async def test_an_exited_attached_session_expires_and_frees_its_slot(
    tmp_path: Path,
) -> None:
    """A shell that exited has nothing left to control, so it ages out."""
    clock = FakeClock()
    factory = FakeFactory()
    manager = _manager(factory, clock)

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")

    factory.process.finish(exit_code=0)
    await _wait_until(
        lambda: manager.get(info.session_id).exited, what="the shell to end"
    )

    # The controller was released, and the deadline started.
    state = manager.get(info.session_id)
    assert state.attached is False
    assert state.detached_since is not None

    clock.advance(TERMINAL_DETACHED_TTL_S)
    assert await manager.reap_expired() == [info.session_id]
    assert manager.retained_count() == 0

    # The slot is free again.
    again = await manager.create(tmp_path, "chat", "chat-2")
    assert again.session_id != info.session_id
    assert manager.retained_count() == 1

    await manager.shutdown()


async def test_an_exited_shell_still_holds_its_slot(tmp_path: Path) -> None:
    """An exited shell keeps its output and its status, so it keeps its slot."""
    factory = FakeFactory()
    manager = _manager(factory)

    infos = [
        await manager.create(tmp_path, "chat", f"chat-{index}")
        for index in range(TERMINAL_MAX_SESSIONS)
    ]
    factory.processes[0].finish(exit_code=0)
    await _wait_until(
        lambda: manager.get(infos[0].session_id).exited, what="the shell to end"
    )

    assert manager.retained_count() == TERMINAL_MAX_SESSIONS
    assert (
        await _reason(manager.create, tmp_path, "chat", "chat-extra") == "capacity"
    )

    await manager.shutdown()


async def test_natural_exit_drains_final_output_reaps_process_and_retains_status(
    tmp_path: Path,
) -> None:
    """A shell that ends keeps its last bytes and its exit code."""
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")

    # The shell exits, and the bytes it produced on its way out are
    # still in the terminal. A reader that stopped on the exit code
    # would lose them; this one drains them first.
    factory.process.feed(b"final bytes")
    factory.process.finish(exit_code=3, final=b" trailing")
    await _wait_until(
        lambda: manager.get(info.session_id).exited, what="the shell to end"
    )

    state = manager.get(info.session_id)
    assert state.exit_code == 3
    assert state.seq == len(b"final bytes trailing")
    assert (
        manager.read(info.session_id, "conn-1", 0).data == b"final bytes trailing"
    )
    assert factory.process.closed is True

    # The record is retained: the output and the status are still there.
    assert manager.retained_count() == 1

    await manager.shutdown()


async def test_a_write_to_a_shell_that_exited_is_refused(tmp_path: Path) -> None:
    """An ended shell takes no more input."""
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")
    factory.process.finish(exit_code=0)
    await _wait_until(
        lambda: manager.get(info.session_id).exited, what="the shell to end"
    )

    assert (
        await _reason(manager.write_input, info.session_id, "conn-1", b"x")
        == "closed"
    )

    await manager.shutdown()


# ── ending ────────────────────────────────────────────────────────────────


async def test_end_and_shutdown_join_tasks_and_close_every_process(
    tmp_path: Path,
) -> None:
    """Ending and shutting down leave no process and no task behind."""
    factory = FakeFactory()
    manager = _manager(factory)

    # The sweeper starts once, however often it is asked.
    await manager.start()
    await manager.start()

    first = await manager.create(tmp_path, "chat", "chat-1")
    second = await manager.create(tmp_path, "chat", "chat-2")

    await manager.end(first.session_id)
    assert factory.processes[0].closed is True
    assert manager.retained_count() == 1
    assert await _reason(manager.end, first.session_id) == "not_found"

    await manager.shutdown()
    assert factory.processes[1].closed is True
    assert manager.retained_count() == 0

    # A create after the shutdown is refused rather than half-started.
    assert await _reason(manager.create, tmp_path, "chat", "chat-3") == "closed"

    leftover = [
        task.get_name()
        for task in asyncio.all_tasks()
        if task.get_name().startswith("terminal-")
    ]
    assert leftover == []


async def test_an_end_flushes_the_pending_bytes_and_joins_the_flusher(
    tmp_path: Path,
) -> None:
    """The end gives the pending bytes a last chance, then stops the flusher.

    The flusher is the only writer of the queue, so it is joined before
    the end returns: no task of the session outlives it.
    """
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")
    factory.process.write_takes = 0

    assert await manager.write_input(info.session_id, "conn-1", b"last") == 4
    await _wait_until(
        lambda: manager.get(info.session_id).pending_write_bytes == 4,
        what="the bytes to be pending",
    )

    # The terminal starts taking bytes again, and the end hands them
    # over before it closes the shell.
    factory.process.write_takes = None
    await manager.end(info.session_id)
    assert bytes(factory.process.written) == b"last"
    assert _terminal_tasks("terminal-flusher-") == []

    await manager.shutdown()


async def test_a_shell_is_closed_exactly_once(tmp_path: Path) -> None:
    """A shell is closed once, whichever way the session ends."""
    factory = FakeFactory()
    manager = _manager(factory)
    # A natural exit closes the shell.
    natural = await manager.create(tmp_path, "chat", "chat-1")
    factory.processes[0].finish(exit_code=0)
    await _wait_until(
        lambda: manager.get(natural.session_id).exited, what="the shell to end"
    )
    assert factory.processes[0].close_count == 1
    # Ending it afterwards does not close it again.
    await manager.end(natural.session_id)
    assert factory.processes[0].close_count == 1

    # An end closes the shell once.
    ended = await manager.create(tmp_path, "chat", "chat-2")
    await manager.end(ended.session_id)
    assert factory.processes[1].close_count == 1

    # A shutdown closes the shell once.
    await manager.create(tmp_path, "chat", "chat-3")
    await manager.shutdown()
    assert factory.processes[2].close_count == 1


async def test_a_concurrent_end_and_shutdown_close_the_shell_once(
    tmp_path: Path,
) -> None:
    """Two closers of the same shell produce one close, not two."""
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    gate = threading.Event()
    factory.process.close_gate = gate

    ending = asyncio.create_task(manager.end(info.session_id))
    shutting_down = asyncio.create_task(manager.shutdown())
    await _wait_until(
        lambda: factory.process.close_started.is_set(), what="the close to start"
    )
    gate.set()
    await ending
    await shutting_down

    assert factory.process.close_count == 1

    await manager.shutdown()


async def test_a_cancelled_end_still_closes_the_shell_and_can_be_retried(
    tmp_path: Path,
) -> None:
    """A cancelled end is not a lost end: the shell is closed and the record stays."""
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    gate = threading.Event()
    factory.process.close_gate = gate

    ending = asyncio.create_task(manager.end(info.session_id))
    await _wait_until(
        lambda: factory.process.close_started.is_set(), what="the close to start"
    )
    ending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await ending

    # The record outlived the cancelled end: it is still there, and the
    # shell is still on its way out.
    assert manager.retained_count() == 1

    gate.set()
    await _wait_until(
        lambda: factory.process.closed, what="the close to complete"
    )

    # The end is retryable, and the retry does not close the shell twice.
    await manager.end(info.session_id)
    assert manager.retained_count() == 0
    assert factory.process.close_count == 1

    await manager.shutdown()


async def test_a_cancelled_shutdown_still_closes_every_shell(
    tmp_path: Path,
) -> None:
    """A shutdown that was cancelled leaves nothing running."""
    factory = FakeFactory()
    manager = _manager(factory)

    first = await manager.create(tmp_path, "chat", "chat-1")
    second = await manager.create(tmp_path, "chat", "chat-2")
    gate = threading.Event()
    for process in factory.processes:
        process.close_gate = gate

    shutdown = asyncio.create_task(manager.shutdown())
    await _wait_until(
        lambda: factory.processes[0].close_started.is_set(),
        what="the first close to start",
    )
    shutdown.cancel()
    with pytest.raises(asyncio.CancelledError):
        await shutdown

    gate.set()
    await _wait_until(
        lambda: all(process.closed for process in factory.processes),
        what="every shell to be closed",
    )
    await _wait_until(
        lambda: manager.retained_count() == 0, what="the registry to be cleared"
    )

    assert first.session_id != second.session_id
    assert _terminal_tasks() == []

    await manager.shutdown()


async def test_end_joins_the_reader_it_cancelled(tmp_path: Path) -> None:
    """The reader is stopped and joined before the end returns."""
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    readers = [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("terminal-reader-")
    ]
    assert len(readers) == 1

    await manager.end(info.session_id)
    assert readers[0].done()

    await manager.shutdown()


async def test_the_reader_stops_when_the_session_is_closed(
    tmp_path: Path,
) -> None:
    """A closed session has no reader polling its shell any more.

    The reader is the task that touches the process, so a session that
    is closed must have stopped it: the reader cannot race the close
    for the same shell.
    """
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    reader = next(
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith("terminal-reader-")
    )
    assert not reader.done()

    await manager.end(info.session_id)
    assert reader.done()
    # The shell was closed once, and the reader did not close it too.
    assert factory.process.close_count == 1

    await manager.shutdown()


async def test_the_reader_polls_quickly_after_output_and_backs_off_while_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A busy terminal is polled fast; a quiet one backs off to the cap."""
    script: list[bytes | None] = [b"a"] + [None] * 6 + [b"b", None, b""]

    class ScriptedProcess(FakeProcess):
        def read(self, size: int = TERMINAL_READ_BYTES) -> bytes | None:
            return script.pop(0) if script else b""

    class ScriptedFactory(FakeFactory):
        def __call__(self, *, cwd: Path, cols: int = 80, rows: int = 24):
            process = ScriptedProcess(pid=4_001)
            self.processes.append(process)
            return process

    # Only the reader task's sleeps are recorded; every other caller
    # (the sweeper, the flusher, the test's own waits) sleeps for real.
    real_sleep = asyncio.sleep
    delays: list[float] = []

    async def recording_sleep(delay: float, *args, **kwargs):
        task = asyncio.current_task()
        if task is not None and task.get_name().startswith("terminal-reader-"):
            if delay > 0:
                delays.append(delay)
            return await real_sleep(0)
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr("ciao.terminal_sessions.asyncio.sleep", recording_sleep)

    manager = _manager(ScriptedFactory())
    info = await manager.create(tmp_path, "chat", "chat-1")
    await _wait_until(
        lambda: manager.get(info.session_id).exited, what="the shell to end"
    )

    assert delays == pytest.approx(
        [_READER_MIN_IDLE_S * 2**n for n in range(5)]
        + [_READER_IDLE_S, _READER_MIN_IDLE_S]
    )
    assert _READER_MIN_IDLE_S * 2**4 < _READER_IDLE_S

    await manager.shutdown()


async def test_shutdown_joins_the_finalizer_of_a_cancelled_create(
    tmp_path: Path,
) -> None:
    """The tail of a cancelled create is joined by the shutdown."""
    factory = FakeFactory()
    gate = threading.Event()
    factory.gate = gate
    manager = _manager(factory)

    create = asyncio.create_task(manager.create(tmp_path, "chat", "chat-1"))
    await _wait_until(lambda: factory.calls == 1, what="the spawn to start")
    create.cancel()
    with pytest.raises(asyncio.CancelledError):
        await create

    # The spawn is released first, so the shutdown does not have to
    # wait for it: the process arrives, the spawn closes it.
    gate.set()
    await manager.shutdown()

    assert _terminal_tasks("terminal-session-finalizer") == []
    await _wait_until(
        lambda: bool(factory.processes) and factory.processes[0].closed,
        what="the late process to be closed",
    )


async def test_start_after_shutdown_leaves_no_sweeper(tmp_path: Path) -> None:
    """A sweeper nobody will cancel is a task that outlives the manager."""
    manager = _manager()

    await manager.start()
    await manager.shutdown()

    assert await _reason(manager.start) == "closed"
    assert _terminal_tasks("terminal-session-sweeper") == []


# ── silence ───────────────────────────────────────────────────────────────


async def test_terminal_contents_are_not_written_or_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The user's screen never reaches a record, an error or a log."""
    factory = FakeFactory()
    manager = _manager(factory)

    secret = "s3cr3t-terminal-output"
    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")

    # The log window is opened *before* the secret is fed, so the
    # reader is covered too.
    with caplog.at_level(logging.DEBUG, logger="ciao.terminal_sessions"):
        factory.process.feed(secret.encode())
        await _wait_until(
            lambda: manager.get(info.session_id).seq == len(secret),
            what="the output to land",
        )
        await manager.write_input(info.session_id, "conn-1", b"x")
        await manager.end(info.session_id)
        await manager.shutdown()

    assert secret not in caplog.text
    # The record names the session, not the screen.
    assert secret not in repr(info)
    assert secret not in repr(manager.list_for("chat", "chat-1"))

    # A refusal carries its reason and nothing else.
    refusal = TerminalSessionError("taken_over")
    assert str(refusal) == "taken_over"
    assert secret not in str(refusal)

    # Nothing was written to the folder the session was opened for.
    assert list(tmp_path.iterdir()) == []


# ── the rest of the surface ───────────────────────────────────────────────


async def test_a_target_this_manager_cannot_hold_is_refused(tmp_path: Path) -> None:
    """Only a chat and a project are targets."""
    manager = _manager()

    assert (
        await _reason(manager.create, tmp_path, "workspace", "x")
        == "invalid_target"
    )
    assert await _reason(manager.create, tmp_path, "chat", "  ") == "invalid_target"
    assert manager.retained_count() == 0

    await manager.shutdown()


async def test_waiting_for_output_wakes_when_the_terminal_says_something(
    tmp_path: Path,
) -> None:
    """The wait is on the ring, and it ends when the shell speaks."""
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")

    assert await manager.wait_for_output(info.session_id, "conn-1", 0, 0.05) is False

    factory.process.feed(b"hello")
    assert await manager.wait_for_output(info.session_id, "conn-1", 0, 2.0) is True
    assert manager.read(info.session_id, "conn-1", 0).data == b"hello"

    await manager.shutdown()


async def test_a_waiter_is_released_by_a_takeover(tmp_path: Path) -> None:
    """A displaced waiter finds out at once, not at its timeout."""
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")

    loop = asyncio.get_running_loop()
    started = loop.time()
    waiter = asyncio.create_task(
        manager.wait_for_output(info.session_id, "conn-1", 0, timeout=5.0)
    )
    await asyncio.sleep(0.05)
    manager.attach(info.session_id, "conn-2", takeover=True)

    with pytest.raises(TerminalSessionError) as caught:
        await waiter
    assert caught.value.reason == "taken_over"
    # It was released at once, not slept out.
    assert loop.time() - started < 1.0

    await manager.shutdown()


async def test_a_waiter_is_released_by_the_end_of_its_session(
    tmp_path: Path,
) -> None:
    """A waiter of a session that is gone finds out at once."""
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")

    loop = asyncio.get_running_loop()
    started = loop.time()
    waiter = asyncio.create_task(
        manager.wait_for_output(info.session_id, "conn-1", 0, timeout=5.0)
    )
    await asyncio.sleep(0.05)
    await manager.end(info.session_id)

    with pytest.raises(TerminalSessionError) as caught:
        await waiter
    assert caught.value.reason == "not_found"
    assert loop.time() - started < 1.0

    await manager.shutdown()


async def test_a_terminal_that_fails_ends_the_session_and_reports_it(
    tmp_path: Path,
) -> None:
    """A read that raises ends the session, and says that it failed."""
    factory = FakeFactory()
    manager = _manager(factory)

    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")

    def _explode() -> bytes:
        raise OSError("the terminal broke")

    factory.process.read = _explode  # type: ignore[method-assign]
    await _wait_until(
        lambda: manager.get(info.session_id).exited, what="the session to end"
    )

    state = manager.get(info.session_id)
    assert state.failed is True
    assert factory.process.closed is True

    await manager.shutdown()


async def test_a_real_shell_speaks_through_the_manager(tmp_path: Path) -> None:
    """One real process, driven end to end, on the platforms that have one.

    The fake proves the rules; this proves the wiring: a real shell on a
    real pty (or pseudoconsole), real bytes, through the real ring and
    the real offsets. The line ending is the one both a POSIX shell and
    PowerShell accept, so the same smoke runs on every platform.

    What is proved is the wiring — bytes travelled from a real terminal
    into the ring, and the ring named them with absolute, contiguous
    offsets. What the shell echoes is the shell's business.
    """
    manager = TerminalSessionManager()
    info = await manager.create(tmp_path, "chat", "chat-1")
    manager.attach(info.session_id, "conn-1")

    await manager.write_input(info.session_id, "conn-1", b"echo ciao-smoke\r\n")

    collected = bytearray()
    offset = 0
    deadline = asyncio.get_running_loop().time() + 30.0
    while b"ciao-smoke" not in collected:
        piece = manager.read(info.session_id, "conn-1", offset)
        assert piece.start == offset
        collected.extend(piece.data)
        offset = piece.end
        if asyncio.get_running_loop().time() > deadline:
            break
        await asyncio.sleep(0.05)

    assert b"ciao-smoke" in bytes(collected)
    # The offsets were absolute and contiguous: the ring named the
    # bytes once, from zero.
    assert offset == len(collected)
    assert manager.get(info.session_id).seq >= offset

    await manager.shutdown()
