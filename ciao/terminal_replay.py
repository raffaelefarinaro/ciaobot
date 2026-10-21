"""Bounded raw-byte replay for the browser terminal.

This is the *byte contract* the browser terminal will replay from, and
nothing else: no session, no process, no socket, no renderer, no
persistence, and no runtime wiring yet.

Terminal output is a stream of **bytes**, not text — a PTY hands back
whatever the program wrote, with UTF-8 split across a read boundary and
escape sequences cut in half. The unit of position here is therefore
the **absolute byte offset**: the total count of bytes the stream has
produced, never rebased, so a client can name the exact byte it has
consumed and get the rest. Nothing is decoded, re-encoded or
boundary-guessed: a retained suffix may start halfway through a UTF-8
or VT sequence, and preserving the bytes exactly is the point, because
repairing the screen is the renderer's job.

The ring holds a fixed :data:`TERMINAL_REPLAY_BYTES` and nothing else,
so the memory a terminal can hold is knowable in advance. When output
outruns it the oldest bytes are dropped, not copied somewhere: that is
the whole eviction policy.

Two losses are told apart, because they are different problems. A
*stale initial* attach — an offset already evicted — is answered with a
**truncated** snapshot of the retained suffix, so the caller resets the
renderer and is honest about the gap. A *live* read behind the oldest
retained byte is refused as **`slow_consumer`** rather than silently
clamped: a consumer that has lost bytes cannot be handed a stream with
a hole in it, and a second output queue would be exactly the unbounded
memory this module exists to prevent.

Offsets stay representable as JavaScript safe integers
(:data:`TERMINAL_REPLAY_MAX_OFFSET`), because the browser is the other
end of this contract. The module holds no lock and claims no
thread-safety: the manager that owns a terminal serializes its own
operations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

#: How many bytes of terminal output are retained per buffer. Fixed, not
#: configurable: the bound is the contract (a reconnect replays at most
#: this much) and a per-session setting would make the memory ceiling
#: unknowable.
TERMINAL_REPLAY_BYTES = 256 * 1024

#: The largest single chunk that may be appended or read at once. A read
#: is bounded so a slow reader cannot ask for an unbounded copy, and an
#: append is bounded so one writer cannot hand the buffer an unbounded
#: chunk to copy in.
TERMINAL_REPLAY_CHUNK_BYTES = 65536

#: The largest absolute byte offset the buffer will ever name: 2**53-1,
#: the largest integer a JavaScript number represents exactly. The
#: browser is the other end of this contract, so an offset that cannot
#: survive the trip is refused rather than silently rounded.
TERMINAL_REPLAY_MAX_OFFSET = 2**53 - 1

#: The closed vocabulary of refusal reasons. A caller maps a reason to a
#: status without parsing prose, and a reason never carries data.
TerminalReplayReason = Literal[
    "invalid_offset",
    "invalid_limit",
    "chunk_too_large",
    "sequence_exhausted",
    "slow_consumer",
]


class TerminalReplayError(Exception):
    """A typed refusal from the replay buffer.

    ``reason`` is one of :data:`TerminalReplayReason`:

    - ``invalid_offset``: the offset is not a plain ``int`` in
      ``[0, seq]`` — a bool, an ``int`` subclass, a float, negative, in
      the future, or past the JavaScript-safe ceiling.
    - ``invalid_limit``: the read limit is not a plain ``int`` in
      ``[1, TERMINAL_REPLAY_CHUNK_BYTES]``.
    - ``chunk_too_large``: the appended chunk is larger than
      :data:`TERMINAL_REPLAY_CHUNK_BYTES`.
    - ``sequence_exhausted``: appending would push the absolute sequence
      past :data:`TERMINAL_REPLAY_MAX_OFFSET`.
    - ``slow_consumer``: the requested offset is older than the oldest
      retained byte, so the bytes it names no longer exist.

    The message is the reason and nothing else. Terminal output is not
    a credential, but it is the user's screen, and a refusal must not
    put a fragment of it into an error string or a log line.
    """

    def __init__(self, reason: TerminalReplayReason) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ReplaySlice:
    """A half-open byte range ``[start, end)`` and the bytes in it.

    ``data`` is immutable and does not alias the buffer's storage: a
    caller that holds a slice can keep it while the terminal keeps
    writing. The content is excluded from the repr so a diagnostic dump
    does not spill the user's screen into a log.
    """

    start: int
    end: int
    data: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class ReplaySnapshot:
    """The retained bytes from ``start`` to ``end``, plus whether it is whole.

    ``start`` is the offset the snapshot actually begins at: the
    requested offset when that is still retained, otherwise the oldest
    retained byte. ``end`` is always ``seq``.

    ``truncated`` says the bytes before ``start`` are gone: the caller
    asked for an offset that had already been evicted, so the snapshot
    is a suffix of the real stream and the renderer must be reset rather
    than patched. A snapshot that is not truncated begins at the
    requested offset and is whole from there to ``seq``; it does not
    necessarily cover every retained byte.
    """

    start: int
    end: int
    data: bytes = field(repr=False)
    truncated: bool = False


class TerminalReplayBuffer:
    """A fixed-size ring of raw terminal bytes with absolute offsets.

    One flat ``bytearray`` of :data:`TERMINAL_REPLAY_BYTES` bytes, one
    absolute sequence counter, one cached oldest offset. The byte at
    absolute offset ``n`` lives at ``n % TERMINAL_REPLAY_BYTES``.

    Not thread-safe: the owner of a terminal serializes its own
    operations.
    """

    def __init__(self) -> None:
        self._storage = bytearray(TERMINAL_REPLAY_BYTES)
        # The absolute count of bytes ever appended. The next byte to be
        # written sits at this offset.
        self._seq = 0
        # The absolute offset of the oldest byte still retained. It only
        # moves forward: the ring never rewinds.
        self._oldest = 0

    @property
    def seq(self) -> int:
        """The absolute byte count of everything appended so far.

        The next byte appended will be at this offset, and it is the
        end of every current snapshot. A read ends here only when its
        limit reaches this far.
        """
        return self._seq

    @property
    def oldest(self) -> int:
        """The absolute offset of the oldest byte still retained.

        Bytes before this have been overwritten and are gone.
        """
        return self._oldest

    def append(self, data: bytes) -> tuple[int, int]:
        """Append ``data`` and return the half-open range ``(start, end)``.

        The range is absolute and half-open: the bytes that were written
        occupy ``[start, end)``, and ``end - start == len(data)``. An
        empty append is a no-op that returns ``(seq, seq)``.

        A chunk larger than :data:`TERMINAL_REPLAY_CHUNK_BYTES` is
        refused, and so is an append that would push the sequence past
        :data:`TERMINAL_REPLAY_MAX_OFFSET`. Both refusals happen before
        any state changes, so a rejected append leaves the buffer exactly
        as it was.

        :raises TerminalReplayError: with reason ``chunk_too_large`` or
            ``sequence_exhausted``.
        """
        if len(data) > TERMINAL_REPLAY_CHUNK_BYTES:
            raise TerminalReplayError("chunk_too_large")
        if self._seq + len(data) > TERMINAL_REPLAY_MAX_OFFSET:
            raise TerminalReplayError("sequence_exhausted")

        start = self._seq
        if not data:
            return (start, start)

        end = start + len(data)
        consumed = 0
        for position, length in self._ranges(start, len(data)):
            self._storage[position : position + length] = data[
                consumed : consumed + length
            ]
            consumed += length

        self._seq = end
        # Eviction is a consequence of the write, not a separate policy:
        # once the sequence is past the capacity, the oldest bytes are
        # overwritten and the retained window starts moving.
        self._oldest = max(0, end - TERMINAL_REPLAY_BYTES)
        return (start, end)

    def snapshot(self, offset: int) -> ReplaySnapshot:
        """The retained bytes from ``offset`` to ``seq``.

        The snapshot always ends at ``seq``. It begins at ``offset``
        when that offset is still retained, and at the oldest retained
        byte when it is not — the bytes the caller named are gone, and
        the snapshot says so with ``truncated=True`` so the caller knows
        to reset rather than to patch. A snapshot that is not truncated
        starts at the requested offset, not necessarily at ``oldest``.

        :raises TerminalReplayError: with reason ``invalid_offset`` for an
            offset that is not a plain ``int`` in ``[0, seq]`` or past
            the JavaScript-safe ceiling.
        """
        self._check_offset(offset)
        start = max(offset, self._oldest)
        truncated = offset < self._oldest
        data = self._copy(start, self._seq)
        return ReplaySnapshot(
            start=start, end=self._seq, data=data, truncated=truncated
        )

    def read(
        self, offset: int, limit: int = TERMINAL_REPLAY_CHUNK_BYTES
    ) -> ReplaySlice:
        """Read up to ``limit`` bytes from ``offset`` onwards.

        The read is a live read, not an initial attach, so an offset
        that has already been evicted is refused with ``slow_consumer``
        rather than silently clamped: a consumer that is behind has lost
        bytes, and the honest answer is that it must reconnect and
        replay, not that it is quietly given a stream with a hole in it.

        At most ``min(limit, seq - offset)`` bytes are returned, and the
        range is half-open, so ``end`` is ``seq`` only when the limit
        reaches it. An offset at ``seq`` returns empty data and an empty
        range, which is how a consumer waits for more.

        :raises TerminalReplayError: with reason ``invalid_offset``,
            ``invalid_limit`` or ``slow_consumer``.
        """
        self._check_offset(offset)
        # `type(x) is int`, not `isinstance`: a bool is an int in
        # Python, and an int subclass may override the comparison and
        # arithmetic this check relies on. E721 wants isinstance here,
        # but the exact type is the contract.
        if type(limit) is not int:  # noqa: E721 - exact int, not isinstance
            raise TerminalReplayError("invalid_limit")
        if limit < 1 or limit > TERMINAL_REPLAY_CHUNK_BYTES:
            raise TerminalReplayError("invalid_limit")
        if offset < self._oldest:
            raise TerminalReplayError("slow_consumer")

        end = min(self._seq, offset + limit)
        data = self._copy(offset, end)
        return ReplaySlice(start=offset, end=end, data=data)

    def _check_offset(self, offset: int) -> None:
        """Refuse an offset that is not a usable absolute byte position.

        The type is checked exactly, not with ``isinstance``: a bool is
        an ``int`` in Python, and an ``int`` subclass may override the
        comparison and arithmetic this check relies on. Only a plain
        ``int`` is an offset.
        """
        if type(offset) is not int:  # noqa: E721 - exact int, not isinstance
            raise TerminalReplayError("invalid_offset")
        if offset < 0 or offset > self._seq or offset > TERMINAL_REPLAY_MAX_OFFSET:
            raise TerminalReplayError("invalid_offset")

    def _ranges(self, start: int, count: int) -> list[tuple[int, int]]:
        """Where ``count`` bytes from absolute offset ``start`` go.

        The byte at absolute offset ``n`` lives at ``n %
        TERMINAL_REPLAY_BYTES``, so a run that crosses the end of the
        storage is two runs: the tail before the end, then the head
        after it. One rule, stated once, so the write path and the read
        path cannot disagree about where a byte is.
        """
        position = start % TERMINAL_REPLAY_BYTES
        head = min(count, TERMINAL_REPLAY_BYTES - position)
        if head == count:
            return [(position, head)]
        return [(position, head), (0, count - head)]

    def _copy(self, start: int, end: int) -> bytes:
        """The bytes in ``[start, end)``, copied out of the ring.

        The copy is what makes a result safe to keep: the ring is
        overwritten by later appends, and a caller holding a snapshot
        must not see it change underneath. At most two slices are read,
        because a range can straddle the ring's end.
        """
        if end <= start:
            return b""
        copied = b""
        for position, length in self._ranges(start, end - start):
            copied += bytes(self._storage[position : position + length])
        return copied
