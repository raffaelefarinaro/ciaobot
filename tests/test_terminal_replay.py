"""Bounded raw-byte terminal replay (the #1705 foundation).

Pure unit tests over one in-memory ring: no process, no socket, no
files, no clock. The reference model here is a plain ``bytearray`` that
keeps every byte ever appended — the simplest possible statement of
"the ring must hold exactly the last N bytes" — so the ring's own
storage is checked against something that cannot share its bug.

Every test names the behavior it pins, and the module under test is the
only thing that decides offsets, truncation and refusal.
"""

from __future__ import annotations

import dataclasses
import logging
import random

import pytest

from ciao.terminal_replay import (
    TERMINAL_REPLAY_BYTES,
    TERMINAL_REPLAY_CHUNK_BYTES,
    TERMINAL_REPLAY_MAX_OFFSET,
    ReplaySlice,
    ReplaySnapshot,
    TerminalReplayBuffer,
    TerminalReplayError,
)

#: The bytes a terminal actually produces: a NUL, an invalid UTF-8
#: sequence, a half escape sequence, a printable run.
TERMINAL_NOISE = b"\x00\xff\xfe\x1b[3\x1b]0;tit\x08\r\n"


def _reason(callable_, *args, **kwargs) -> str:
    """The reason of the refusal ``callable_`` raises.

    Deliberately untyped: it is called with arguments the callers know
    are unusable, and the point of the test is that the module refuses
    them anyway.
    """
    with pytest.raises(TerminalReplayError) as caught:
        callable_(*args, **kwargs)
    return caught.value.reason


def _reference(appended: bytes) -> bytes:
    """The bytes a correct ring must still hold: the last 256 KiB."""
    return appended[-TERMINAL_REPLAY_BYTES:]


def _filled(count: int, seed: int = 7) -> bytes:
    """``count`` deterministic pseudo-random bytes."""
    return random.Random(seed).randbytes(count)


def _append_all(
    buffer: TerminalReplayBuffer, payload: bytes, appended: bytearray | None = None
) -> None:
    """Append ``payload`` in chunks the buffer accepts.

    One append is bounded at 64 KiB, so a longer payload arrives in
    several appends — the way a reader would deliver it. When
    ``appended`` is given it accumulates the same bytes, so it is the
    reference model of the whole stream.
    """
    for start in range(0, len(payload), TERMINAL_REPLAY_CHUNK_BYTES):
        chunk = payload[start : start + TERMINAL_REPLAY_CHUNK_BYTES]
        buffer.append(chunk)
        if appended is not None:
            appended += chunk


# ── Empty state ───────────────────────────────────────────────────────────


def test_a_fresh_buffer_is_empty_and_names_no_bytes() -> None:
    buffer = TerminalReplayBuffer()

    assert buffer.seq == 0
    assert buffer.oldest == 0

    snapshot = buffer.snapshot(0)
    assert (snapshot.start, snapshot.end) == (0, 0)
    assert snapshot.data == b""
    assert snapshot.truncated is False

    read = buffer.read(0)
    assert (read.start, read.end) == (0, 0)
    assert read.data == b""


def test_seq_and_oldest_are_read_only() -> None:
    buffer = TerminalReplayBuffer()

    with pytest.raises(AttributeError):
        setattr(buffer, "seq", 5)
    with pytest.raises(AttributeError):
        setattr(buffer, "oldest", 5)


def test_appending_an_empty_chunk_is_a_no_op() -> None:
    buffer = TerminalReplayBuffer()

    assert buffer.append(b"") == (0, 0)
    assert (buffer.seq, buffer.oldest) == (0, 0)


def test_result_metadata_is_frozen() -> None:
    buffer = TerminalReplayBuffer()
    buffer.append(b"abc")
    read = buffer.read(0)
    snapshot = buffer.snapshot(0)

    assert isinstance(read, ReplaySlice)
    assert isinstance(snapshot, ReplaySnapshot)
    assert dataclasses.is_dataclass(read)
    for result in (read, snapshot):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(result, "start", 1)


def test_a_result_repr_never_shows_the_terminal_contents() -> None:
    buffer = TerminalReplayBuffer()
    buffer.append(b"SECRET-TERMINAL-CONTENT")
    read = buffer.read(0)
    snapshot = buffer.snapshot(0)

    for result in (read, snapshot):
        rendered = repr(result)
        assert "SECRET-TERMINAL-CONTENT" not in rendered
        assert "data=" not in rendered
    # The metadata is still there, so the repr is not simply empty.
    assert "start=0" in repr(read)


# ── Appending ─────────────────────────────────────────────────────────────


def test_appends_advance_the_span_by_exactly_the_byte_count() -> None:
    buffer = TerminalReplayBuffer()

    first = buffer.append(b"abc")
    second = buffer.append(TERMINAL_NOISE)

    assert first == (0, 3)
    assert second == (3, 3 + len(TERMINAL_NOISE))
    assert buffer.seq == 3 + len(TERMINAL_NOISE)
    assert buffer.oldest == 0


def test_appended_bytes_are_kept_exactly_and_never_decoded() -> None:
    buffer = TerminalReplayBuffer()
    payload = TERMINAL_NOISE + b"caf\xc3\xa9" + b"\x80\x81"

    buffer.append(payload)

    assert buffer.read(0).data == payload
    assert buffer.snapshot(0).data == payload


def test_random_bytes_round_trip_without_a_decode_step() -> None:
    buffer = TerminalReplayBuffer()
    payload = _filled(100_000)

    _append_all(buffer, payload)

    assert buffer.snapshot(0).data == payload
    assert buffer.read(0, 65536).data == payload[:65536]
    assert buffer.read(70_000).data == payload[70_000:]


def test_an_oversized_chunk_is_refused_without_touching_the_buffer() -> None:
    buffer = TerminalReplayBuffer()
    buffer.append(b"kept")
    before = buffer.read(0).data

    reason = _reason(buffer.append, b"x" * (TERMINAL_REPLAY_CHUNK_BYTES + 1))

    assert reason == "chunk_too_large"
    assert (buffer.seq, buffer.oldest) == (4, 0)
    assert buffer.read(0).data == before


def test_a_chunk_of_exactly_the_maximum_size_is_accepted() -> None:
    buffer = TerminalReplayBuffer()

    buffer.append(b"x" * TERMINAL_REPLAY_CHUNK_BYTES)

    assert buffer.seq == TERMINAL_REPLAY_CHUNK_BYTES


# ── The ring against a reference model ────────────────────────────────────


@pytest.mark.parametrize(
    "chunk_sizes",
    [
        [0],
        [1],
        [TERMINAL_REPLAY_CHUNK_BYTES - 1],
        [TERMINAL_REPLAY_CHUNK_BYTES],
        [0, 1, 65535, 65536],
        [1] * 40,
        [65536, 1, 65535, 2],
        [300_000 // 7] * 7,
    ],
)
def test_the_ring_holds_exactly_the_last_256_kib(chunk_sizes: list[int]) -> None:
    buffer = TerminalReplayBuffer()
    appended = bytearray()
    for index, size in enumerate(chunk_sizes):
        chunk = _filled(size, seed=index)
        start, end = buffer.append(chunk)
        appended += chunk
        assert (start, end) == (len(appended) - size, len(appended))

    assert buffer.seq == len(appended)
    assert buffer.oldest == max(0, len(appended) - TERMINAL_REPLAY_BYTES)
    snapshot = buffer.snapshot(buffer.oldest)
    assert snapshot.data == bytes(_reference(bytes(appended)))
    assert snapshot.truncated is False


def test_a_read_never_returns_more_than_the_limit() -> None:
    buffer = TerminalReplayBuffer()
    payload = _filled(200_000, seed=3)
    _append_all(buffer, payload)

    for offset in range(0, buffer.seq, 4096):
        for limit in (1, 100, 65536):
            read = buffer.read(offset, limit)
            assert len(read.data) <= limit
            assert read.end - read.start == len(read.data)
            assert read.data == payload[offset:][:limit]


def test_reads_across_the_ring_boundary_return_the_right_bytes() -> None:
    buffer = TerminalReplayBuffer()
    appended = bytearray()
    for index in range(8):
        chunk = _filled(TERMINAL_REPLAY_CHUNK_BYTES, seed=index)
        buffer.append(chunk)
        appended += chunk

    # The window is exactly full, so the ring boundary sits inside it.
    retained = bytes(_reference(bytes(appended)))
    for offset in range(buffer.oldest, buffer.oldest + TERMINAL_REPLAY_BYTES, 65_533):
        expected = retained[offset - buffer.oldest :][:TERMINAL_REPLAY_CHUNK_BYTES]
        assert buffer.read(offset, TERMINAL_REPLAY_CHUNK_BYTES).data == expected


# ── Capacity and eviction ─────────────────────────────────────────────────


def test_filling_the_ring_keeps_the_first_byte() -> None:
    buffer = TerminalReplayBuffer()
    for _ in range(4):
        buffer.append(b"x" * TERMINAL_REPLAY_CHUNK_BYTES)

    assert buffer.seq == 4 * TERMINAL_REPLAY_CHUNK_BYTES
    assert buffer.oldest == 0
    assert buffer.snapshot(0).truncated is False
    assert len(buffer.snapshot(0).data) == TERMINAL_REPLAY_BYTES


def test_one_byte_past_capacity_evicts_exactly_one_byte() -> None:
    buffer = TerminalReplayBuffer()
    for index in range(4):
        buffer.append(_filled(TERMINAL_REPLAY_CHUNK_BYTES, seed=index))
    buffer.append(b"!")

    assert buffer.seq == 4 * TERMINAL_REPLAY_CHUNK_BYTES + 1
    assert buffer.oldest == 1


def test_a_stale_initial_snapshot_is_truncated_and_exact() -> None:
    buffer = TerminalReplayBuffer()
    appended = bytearray()
    for index in range(4):
        chunk = _filled(TERMINAL_REPLAY_CHUNK_BYTES, seed=index)
        buffer.append(chunk)
        appended += chunk
    buffer.append(b"!")
    appended += b"!"

    snapshot = buffer.snapshot(0)

    assert snapshot.truncated is True
    assert snapshot.start == buffer.oldest == 1
    assert snapshot.end == buffer.seq
    assert snapshot.data == bytes(appended)[1:]


def test_a_valid_retained_snapshot_starts_at_the_asked_offset() -> None:
    buffer = TerminalReplayBuffer()
    appended = bytearray()
    for index in range(4):
        chunk = _filled(TERMINAL_REPLAY_CHUNK_BYTES, seed=index)
        buffer.append(chunk)
        appended += chunk
    buffer.append(b"!")
    appended += b"!"

    snapshot = buffer.snapshot(buffer.oldest + 1000)

    assert snapshot.truncated is False
    assert snapshot.start == buffer.oldest + 1000
    assert snapshot.end == buffer.seq
    assert snapshot.data == bytes(appended)[1001:]


def test_a_stale_live_read_is_a_slow_consumer_not_a_clamp() -> None:
    buffer = TerminalReplayBuffer()
    for index in range(4):
        buffer.append(_filled(TERMINAL_REPLAY_CHUNK_BYTES, seed=index))
    buffer.append(b"!")

    assert _reason(buffer.read, 0) == "slow_consumer"


def test_a_read_from_the_oldest_retained_byte_succeeds() -> None:
    buffer = TerminalReplayBuffer()
    for index in range(4):
        buffer.append(_filled(TERMINAL_REPLAY_CHUNK_BYTES, seed=index))
    buffer.append(b"!")

    read = buffer.read(buffer.oldest)

    assert read.start == buffer.oldest
    assert read.end == buffer.oldest + TERMINAL_REPLAY_CHUNK_BYTES
    assert len(read.data) == TERMINAL_REPLAY_CHUNK_BYTES


def test_an_empty_append_at_capacity_does_not_evict() -> None:
    buffer = TerminalReplayBuffer()
    for index in range(4):
        buffer.append(_filled(TERMINAL_REPLAY_CHUNK_BYTES, seed=index))
    oldest = buffer.oldest
    seq = buffer.seq

    assert buffer.append(b"") == (seq, seq)
    assert (buffer.seq, buffer.oldest) == (seq, oldest)


# ── Refusals ──────────────────────────────────────────────────────────────


class _IntSubclass(int):
    """An ``int`` that is not an ``int``.

    ``isinstance`` admits this, so a check written with ``isinstance``
    would let a subclass with overridden arithmetic through. The
    contract is the exact type, not a shape.
    """

    def __lt__(self, other: object) -> bool:
        return False

    def __gt__(self, other: object) -> bool:
        return False

    def __add__(self, other: int) -> int:
        return 0


@pytest.mark.parametrize(
    "offset",
    [
        -1,
        -1000,
        True,
        False,
        1.0,
        0.5,
        TERMINAL_REPLAY_MAX_OFFSET + 1,
        2**53,
        _IntSubclass(0),
        _IntSubclass(3),
    ],
)
def test_an_unusable_offset_is_refused(offset: object) -> None:
    buffer = TerminalReplayBuffer()
    buffer.append(b"abc")

    assert _reason(buffer.snapshot, offset) == "invalid_offset"
    assert _reason(buffer.read, offset) == "invalid_offset"


def test_an_offset_in_the_future_is_refused() -> None:
    buffer = TerminalReplayBuffer()
    buffer.append(b"abc")

    assert _reason(buffer.snapshot, 4) == "invalid_offset"
    assert _reason(buffer.read, 4) == "invalid_offset"


def test_an_unusable_offset_is_refused_before_a_slow_consumer() -> None:
    # The order of the refusals is part of the contract: a caller that
    # sent a bad offset must be told the offset is bad, not told it is
    # behind, or it will "fix" a working cursor.
    buffer = TerminalReplayBuffer()
    for index in range(4):
        buffer.append(_filled(TERMINAL_REPLAY_CHUNK_BYTES, seed=index))
    buffer.append(b"!")

    assert _reason(buffer.read, -1) == "invalid_offset"
    assert _reason(buffer.read, True) == "invalid_offset"
    assert _reason(buffer.read, buffer.seq + 1) == "invalid_offset"
    # The offset is valid but stale: now it is the slow consumer.
    assert _reason(buffer.read, 0) == "slow_consumer"


def test_an_unusable_limit_is_refused_before_a_slow_consumer() -> None:
    buffer = TerminalReplayBuffer()
    for index in range(4):
        buffer.append(_filled(TERMINAL_REPLAY_CHUNK_BYTES, seed=index))
    buffer.append(b"!")

    assert _reason(buffer.read, 0, 0) == "invalid_limit"
    assert _reason(buffer.read, 0, TERMINAL_REPLAY_CHUNK_BYTES + 1) == "invalid_limit"
    # The limit is valid but the offset is stale: now it is the slow
    # consumer.
    assert _reason(buffer.read, 0) == "slow_consumer"


def test_an_offset_at_the_sequence_end_is_accepted_and_empty() -> None:
    buffer = TerminalReplayBuffer()
    buffer.append(b"abc")

    read = buffer.read(3)

    assert (read.start, read.end) == (3, 3)
    assert read.data == b""


@pytest.mark.parametrize(
    "limit",
    [
        0,
        -1,
        True,
        False,
        1.0,
        65536.5,
        65537,
        10**9,
        _IntSubclass(0),
        _IntSubclass(1024),
    ],
)
def test_an_unusable_limit_is_refused(limit: object) -> None:
    buffer = TerminalReplayBuffer()
    buffer.append(b"abc")

    assert _reason(buffer.read, 0, limit) == "invalid_limit"


@pytest.mark.parametrize("limit", [1, TERMINAL_REPLAY_CHUNK_BYTES])
def test_the_limit_boundaries_are_accepted(limit: int) -> None:
    buffer = TerminalReplayBuffer()
    buffer.append(b"abc")

    read = buffer.read(0, limit)

    assert read.data == b"abc"[:limit]
    assert read.end == min(3, limit)


def test_the_error_message_is_the_reason_and_nothing_else() -> None:
    buffer = TerminalReplayBuffer()
    buffer.append(b"SECRET-TERMINAL-CONTENT")

    with pytest.raises(TerminalReplayError) as caught:
        buffer.read(0, 0)

    assert str(caught.value) == "invalid_limit"
    assert "SECRET-TERMINAL-CONTENT" not in str(caught.value)
    assert caught.value.reason == "invalid_limit"


# ── The absolute sequence ceiling ─────────────────────────────────────────


def test_an_append_that_would_exhaust_the_sequence_is_refused() -> None:
    # A whitebox fixture: the sequence is placed near the JavaScript-safe
    # ceiling so the refusal can be exercised without appending
    # petabytes. There is no production setter for it, on purpose — the
    # ceiling is a property of the arithmetic, not a knob.
    buffer = TerminalReplayBuffer()
    buffer.append(b"kept")
    buffer._seq = TERMINAL_REPLAY_MAX_OFFSET - 4
    buffer._oldest = max(0, buffer._seq - TERMINAL_REPLAY_BYTES)
    storage_before = bytes(buffer._storage)
    seq_before = buffer.seq
    oldest_before = buffer.oldest

    reason = _reason(buffer.append, b"12345")

    assert reason == "sequence_exhausted"
    assert (buffer.seq, buffer.oldest) == (seq_before, oldest_before)
    assert bytes(buffer._storage) == storage_before


def test_an_append_that_lands_exactly_on_the_ceiling_is_accepted() -> None:
    buffer = TerminalReplayBuffer()
    buffer._seq = TERMINAL_REPLAY_MAX_OFFSET - 3
    buffer._oldest = max(0, buffer._seq - TERMINAL_REPLAY_BYTES)

    buffer.append(b"123")

    assert buffer.seq == TERMINAL_REPLAY_MAX_OFFSET


def test_an_offset_past_the_javascript_safe_ceiling_is_refused() -> None:
    # The ceiling is a real guard, not a consequence of the sequence
    # check. The whitebox fixture puts the sequence past the ceiling —
    # a state append() refuses to reach, so it can only be staged — and
    # an offset inside the sequence but past the ceiling must still be
    # refused: the browser could not carry it exactly.
    buffer = TerminalReplayBuffer()
    buffer.append(b"abc")
    buffer._seq = TERMINAL_REPLAY_MAX_OFFSET + 10
    buffer._oldest = max(0, buffer._seq - TERMINAL_REPLAY_BYTES)

    assert _reason(buffer.snapshot, TERMINAL_REPLAY_MAX_OFFSET + 1) == "invalid_offset"
    assert _reason(buffer.read, TERMINAL_REPLAY_MAX_OFFSET + 1) == "invalid_offset"


# ── Ownership of the bytes handed out ─────────────────────────────────────


def test_a_result_does_not_change_when_the_buffer_is_appended_to() -> None:
    buffer = TerminalReplayBuffer()
    buffer.append(b"first")
    read = buffer.read(0)
    snapshot = buffer.snapshot(0)

    buffer.append(b"second")

    assert read.data == b"first"
    assert snapshot.data == b"first"
    assert (read.end, snapshot.end) == (5, 5)


def test_a_result_cannot_be_used_to_mutate_the_buffer() -> None:
    buffer = TerminalReplayBuffer()
    buffer.append(b"abc")
    read = buffer.read(0)
    snapshot = buffer.snapshot(0)

    # The payload is immutable bytes and the record is frozen, so there is
    # no handle back into the ring's storage.
    assert isinstance(read.data, bytes)
    assert isinstance(snapshot.data, bytes)
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(read, "data", b"!")
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(snapshot, "data", b"!")
    assert buffer.read(0).data == b"abc"
    assert buffer.snapshot(0).data == b"abc"


# ── Bounded storage ───────────────────────────────────────────────────────


def test_repeated_one_byte_appends_keep_one_fixed_storage_array() -> None:
    buffer = TerminalReplayBuffer()

    for _ in range(TERMINAL_REPLAY_BYTES + 1000):
        buffer.append(b"x")

    assert len(buffer._storage) == TERMINAL_REPLAY_BYTES
    # One flat array and two integers: no chunk registry, no per-chunk
    # objects, no list of retained pieces.
    containers = [
        name
        for name, value in vars(buffer).items()
        if isinstance(value, (list, dict, set, tuple, bytearray))
        and not (name == "_storage")
    ]
    assert containers == []


def test_terminal_contents_are_never_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    buffer = TerminalReplayBuffer()
    buffer.append(b"SECRET-TERMINAL-CONTENT")

    with caplog.at_level(logging.DEBUG):
        for call in (
            lambda: buffer.read(0, 0),
            lambda: buffer.read(-1),
            lambda: buffer.append(b"x" * (TERMINAL_REPLAY_CHUNK_BYTES + 1)),
        ):
            with pytest.raises(TerminalReplayError):
                call()

    assert "SECRET-TERMINAL-CONTENT" not in caplog.text
    assert caplog.records == []
