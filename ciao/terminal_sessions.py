"""Bounded terminal sessions: one owner for every shell the browser opens.

This is the *manager* the browser terminal will sit on, and nothing else:
no route, no WebSocket, no auth check, no app wiring. It owns the
processes, the replay rings, the controller identity and the detached
lifetimes, and it keeps all four bounded.

The shape of the problem is that a shell must survive a browser
detaching, but nothing about it may grow. So:

- **Sessions are bounded.** At most :data:`TERMINAL_MAX_SESSIONS`
  sessions are retained at once — *in total*, exited shells included,
  because an exited shell still holds its output and its status for the
  UI. A fifth create is refused while the slot is still reserved for a
  spawn that has not come back yet, so a burst of creates cannot start a
  fifth shell.
- **Output is bounded.** Each session owns one
  :class:`~ciao.terminal_replay.TerminalReplayBuffer`, which is the only
  place output lives: a fixed 256 KiB window, absolute byte offsets, no
  second queue and no backlog. This module defines no capacity of its
  own — the ring's numbers belong to :mod:`ciao.terminal_replay` — it
  only asks the ring for a snapshot and for live slices.
- **Lifetimes are bounded.** A session that nobody is attached to
  survives :data:`TERMINAL_DETACHED_TTL_S` seconds and is then pruned.
  Output never extends that deadline: a shell that talks forever does
  not get to live forever. A shell that has *exited* has nothing left
  to control, so it releases its controller and ages out like any other
  detached session, keeping its final bytes and its exit code until it
  does.
- **Input is bounded.** A terminal that will not take the bytes leaves
  them *pending*, and the pending backlog has a ceiling. A caller that
  would push past the ceiling is refused rather than allowed to grow a
  buffer inside the engine. One *flusher* task owns that backlog: it
  is the only writer of it, so a terminal that will not take its input
  cannot stop its output, and a write that is cancelled can neither
  duplicate nor lose the bytes.

Two identities are kept apart on purpose. A **grant** (a password
unlock) says *this browser may use the terminal at all*, and it lives
in :mod:`ciao.terminal_auth`. A **controller** says *this particular
attachment owns the session right now*, and it lives here: one random
``connection_id`` per attachment, minted by the route, never the grant
fingerprint and never the browser. Two tabs of the same browser are two
controllers, so a second tab cannot silently steal the first tab's
shell; it has to take over explicitly, which replaces the controller
and displaces the old attachment.

Everything that touches a process happens off the event loop: the spawn,
the close, the write and the resize. The reader is the exception, and
it is deliberate — the process API is non-blocking by contract
(``read`` returns ``None`` when the terminal is quiet), so the reader
polls it inline and sleeps between ticks instead of parking a thread
per session.

A close is never abandoned, and a record outlives its close.
:meth:`TerminalSessionManager.end` closes the shell *first* and removes
the record only afterwards, so an end that was cancelled can be retried;
the close itself runs in a manager-owned task that a cancelled caller
cannot cancel, and the process is marked closed only once the close has
actually completed.

Refusals are typed. :class:`TerminalSessionError` carries a ``reason``
from a closed vocabulary and its message is that reason and nothing
else: a terminal's output is the user's screen, and a refusal must
never put a fragment of it into an exception, a log line or a record.
The records this module keeps (session ids, target ids, byte counts,
deadlines) are safe to show; the bytes are not in them.

The clock is injected, so the detached lifetime is a fact a test can
move rather than wait for: :meth:`TerminalSessionManager.reap_expired`
does the pruning the sweeper does, on demand.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ciao.os_support.terminal import (
    TERMINAL_READ_BYTES,
    TerminalProcess,
    spawn_terminal,
)
from ciao.terminal_replay import (
    TERMINAL_REPLAY_CHUNK_BYTES,
    ReplaySlice,
    ReplaySnapshot,
    TerminalReplayBuffer,
    TerminalReplayError,
)

logger = logging.getLogger(__name__)

#: How many terminal sessions are retained at once, in total. A session
#: that has exited still counts: it keeps its final output and its exit
#: status until it is pruned, so it cannot be a way around the bound.
TERMINAL_MAX_SESSIONS = 4

#: How long a detached session survives before it is pruned, in seconds.
#: A session that nobody has attached to starts detached, so a shell
#: nobody ever looks at is reclaimed. The deadline is fixed: output does
#: not push it back.
TERMINAL_DETACHED_TTL_S = 900

#: The most input bytes one session may hold pending, waiting for the
#: terminal to accept them. The bound is the same 64 KiB chunk the
#: adapters take in one write and the replay buffer takes in one
#: append, so the number is stated once, in :mod:`ciao.terminal_replay`.
TERMINAL_MAX_PENDING_WRITE_BYTES = TERMINAL_REPLAY_CHUNK_BYTES

#: The window size bounds, in characters. They are the bounds the
#: platform adapters enforce themselves, restated here so a bad size is
#: refused before a process is touched.
TERMINAL_MIN_COLS = 20
TERMINAL_MAX_COLS = 500
TERMINAL_MIN_ROWS = 5
TERMINAL_MAX_ROWS = 200

#: How often the sweeper looks for expired sessions. The issue asks for
#: a sweep at least once a second; this is twice that.
_SWEEP_INTERVAL_S = 0.5

#: How long the reader waits before polling a quiet terminal again.
_READER_IDLE_S = 0.02

#: How long the flusher waits before it retries bytes the terminal did
#: not take. The flusher is woken by an input, so this only matters for
#: bytes that are already pending and stuck.
_FLUSH_IDLE_S = 0.02

#: How long an end or a shutdown waits for the flusher to hand over the
#: pending bytes before it is stopped. The write can block on a full
#: pipe, so the wait is bounded.
_FLUSH_WAIT_S = 0.5

#: How long a cancelled write waits for the answer of the write that is
#: already in the terminal's pipe. The write cannot be taken back, so
#: the flusher waits for the count rather than guessing: a guess would
#: either duplicate the bytes or lose them.
_WRITE_WAIT_S = 1.0

#: How long a shutdown waits for a spawn that is still in flight. The
#: thread behind it cannot be stopped, so the wait is bounded; the
#: process it produces is closed by the spawn itself.
_SPAWN_WAIT_S = 5.0

#: How many random bytes a session id carries. It names a session; it
#: grants nothing.
_SESSION_ID_BYTES = 16

#: The kinds of thing a session can be opened against.
TerminalTargetKind = Literal["chat", "project"]

#: The closed vocabulary of refusal reasons. A caller maps a reason to a
#: status without parsing prose.
TerminalSessionReason = Literal[
    "not_found",
    "not_controller",
    "taken_over",
    "capacity",
    "invalid_offset",
    "invalid_limit",
    "slow_consumer",
    "invalid_target",
    "invalid_size",
    "invalid_input",
    "spawn_failed",
    "closed",
    "io_failed",
]

#: How a refusal from the replay ring reads in this module's vocabulary.
#: The ring's own reasons are already the ones a client cares about;
#: the ones that cannot happen through this module's surface fall back
#: to a generic refusal rather than inventing a name.
_REPLAY_REASONS: dict[str, TerminalSessionReason] = {
    "invalid_offset": "invalid_offset",
    "invalid_limit": "invalid_limit",
    "slow_consumer": "slow_consumer",
}


class TerminalSessionError(Exception):
    """A typed refusal from the session manager.

    ``reason`` is one of :data:`TerminalSessionReason`:

    - ``not_found``: no session carries that id.
    - ``not_controller``: the session has no controller, so the
      connection asking is not one.
    - ``taken_over``: the session is controlled by a different
      connection, and the caller did not ask for a takeover.
    - ``capacity``: the manager is full, or the input backlog is full.
    - ``invalid_offset``: the offset is not a usable absolute byte
      position — negative, a bool, in the future, or past the
      JavaScript-safe ceiling.
    - ``invalid_limit``: the read limit is not a usable chunk size.
    - ``slow_consumer``: the caller's offset is older than the oldest
      byte still retained, so the bytes it names are gone.
    - ``invalid_target``: the target kind or id is not one this
      manager can hold a session for.
    - ``invalid_size``: the window size is not an exact int in range.
    - ``invalid_input``: the input is not bytes.
    - ``spawn_failed``: the shell could not be started.
    - ``closed``: the session has ended, or the manager is shutting
      down.
    - ``io_failed``: the terminal refused an input, a resize or a
      close.

    The message is the reason and nothing else. Terminal output is not
    a credential, but it is the user's screen, and a refusal must not
    put a fragment of it into an error string or a log line.
    """

    def __init__(self, reason: TerminalSessionReason) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class TerminalSessionInfo:
    """What a session is, in terms that are safe to show or serialize.

    It names the session, the thing the session was opened for, the
    folder it was opened in and the window it has, who holds it (as a
    fact — *attached* or not, never the connection id itself, which
    would let a bystander impersonate the controller), when it was
    created, when it became detached, whether the shell has exited and
    with which code, whether the terminal failed, and how much output
    there is.

    ``folder``, ``cols`` and ``rows`` are the manager's own facts: it
    is the one place that opens the shell and the one place that
    resizes it, so it is the one place that knows the size.

    It holds no output. ``seq`` is the absolute byte position the
    stream has reached and ``replay_start`` is the offset of the
    oldest byte still retained; the bytes themselves live in the ring
    and are asked for by offset.
    """

    session_id: str
    target_kind: str
    target_id: str
    folder: Path
    cols: int
    rows: int
    attached: bool
    generation: int
    created_at: float
    detached_since: float | None
    exited: bool
    exit_code: int | None
    failed: bool
    seq: int
    replay_start: int
    pending_write_bytes: int


@dataclass(frozen=True, slots=True)
class TerminalSessionAttachment:
    """The result of attaching: the session, and where to read from.

    ``snapshot`` is the initial replay: the retained bytes from the
    requested offset to the sequence at the moment of the attach, with
    ``truncated`` saying whether the bytes before its start are gone.
    ``next_offset`` is where the live stream continues, which is the
    snapshot's end — so the snapshot and the live reads that follow it
    cover the stream exactly once, with no gap and no repeat.
    """

    info: TerminalSessionInfo
    snapshot: ReplaySnapshot
    next_offset: int
    generation: int


@dataclass(slots=True)
class _Session:
    """One terminal session: the process, the ring, and who holds it."""

    session_id: str
    target_kind: str
    target_id: str
    folder: Path
    created_at: float
    replay: TerminalReplayBuffer
    controller: str | None = None
    #: The connection that was holding the session when its shell
    #: exited. The controller is released then — there is nothing left
    #: to control — but the last holder may still read the final
    #: bytes and the exit code until the record expires.
    last_controller: str | None = None
    generation: int = 0
    detached_since: float | None = None
    exited: bool = False
    #: The shell is gone, or is going: the process cannot take input
    #: any more. It is set as soon as the shell is known to be gone.
    shell_gone: bool = False
    exit_code: int | None = None
    #: Whether the terminal refused an input, a resize or a read, so
    #: the session ended early rather than by the shell's own end.
    failed: bool = False
    spawn_failed: bool = False
    process: TerminalProcess | None = None
    process_closed: bool = False
    spawn: asyncio.Task[None] | None = None
    reader: asyncio.Task[None] | None = None
    flusher: asyncio.Task[None] | None = None
    #: The bytes waiting for the terminal, and the bytes the flusher
    #: has taken out of the queue and is writing right now. The two
    #: together are the pending backlog the ceiling applies to.
    pending: bytearray = field(default_factory=bytearray)
    inflight: int = 0
    #: Wakes the flusher when there is input to write.
    flush_wake: asyncio.Event = field(default_factory=asyncio.Event)
    #: Wakes the readers of the output.
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    close_requested: bool = False
    #: The session is over: it was ended, shut down or pruned. A waiter
    #: on the output is released instead of sleeping out its timeout.
    ended: bool = False
    cols: int = 80
    rows: int = 24
    #: The tasks that close the session and its process. Both are
    #: manager-owned and shielded, so a cancelled caller cannot
    #: abandon them and neither of them runs twice.
    finish_task: asyncio.Task[None] | None = None
    close_task: asyncio.Task[None] | None = None


class TerminalSessionManager:
    """One owner for every terminal session in the engine.

    The manager holds at most :data:`TERMINAL_MAX_SESSIONS` sessions,
    each with one process and one bounded replay ring, and it is the
    only thing that decides who controls a session and how long a
    detached one survives.

    The clock and the process factory are injected, so the whole
    manager is testable without a pty and without waiting for a clock:
    a test supplies a fake process and moves the clock by hand.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        factory: Callable[..., TerminalProcess] = spawn_terminal,
        cols: int = 80,
        rows: int = 24,
    ) -> None:
        self._clock = clock
        self._factory = factory
        self._cols = cols
        self._rows = rows
        self._sessions: dict[str, _Session] = {}
        # One lock for the registry. It guards the capacity check and
        # the reservation, so a burst of creates cannot slip past a
        # full manager.
        self._lock = asyncio.Lock()
        self._spawns: set[asyncio.Task[None]] = set()
        self._readers: set[asyncio.Task[None]] = set()
        self._flushers: set[asyncio.Task[None]] = set()
        self._finalizers: set[asyncio.Task[None]] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._sweeper: asyncio.Task[None] | None = None
        self._shutdown_owner: asyncio.Task[None] | None = None
        self._closing = False

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the expiry sweeper. Idempotent.

        The sweeper is what prunes the sessions nobody is attached to.
        It runs in the active event loop and does nothing but ask
        :meth:`reap_expired` to do the work, so a test can prune by
        hand instead of waiting for it.

        A manager that is shutting down refuses: a sweeper nobody will
        cancel is a task that outlives the manager.
        """
        if self._closing:
            raise TerminalSessionError("closed")
        if self._sweeper is not None and not self._sweeper.done():
            return
        self._sweeper = asyncio.create_task(
            self._sweep_loop(), name="terminal-session-sweeper"
        )

    async def shutdown(self) -> None:
        """Stop admission, close every process, join every task.

        Admission stops first, so a create that arrives during a
        shutdown is refused rather than half-started. Then every
        session is closed: the process is closed off the event loop, the
        reader is cancelled and joined, and the flusher is given a last
        chance to hand over the pending bytes before it is stopped. A
        spawn that is still in flight is waited for (bounded — the
        thread behind it cannot be stopped) and the process it produces
        is closed by the spawn itself, so nothing is left running.

        The registry is cleared *after* the shells are closed, and the
        whole close runs in a manager-owned task: a shutdown that is
        cancelled still closes every shell, and a shutdown that is
        retried is safe.
        """
        self._closing = True
        await asyncio.shield(self._shutdown_task())

    def _shutdown_task(self) -> asyncio.Task[None]:
        """The one task that does the shutdown, reused by every caller.

        The task is kept, not replaced: a second shutdown waits for the
        first one instead of running a second close over the same
        shells.
        """
        task = self._shutdown_owner
        if task is None:
            task = self._manager_task(
                self._shutdown_sessions(), "terminal-session-shutdown"
            )
            self._shutdown_owner = task
        return task

    async def _shutdown_sessions(self) -> None:
        """Close every session, then clear the registry."""
        sweeper = self._sweeper
        self._sweeper = None
        if sweeper is not None:
            sweeper.cancel()
            await asyncio.wait({sweeper})

        async with self._lock:
            sessions = list(self._sessions.values())

        for session in sessions:
            session.close_requested = True
            session.ended = True
            session.wake.set()

        # A spawn that is still in flight is waited for: the thread
        # behind it cannot be cancelled, and the process it produces is
        # closed by the spawn itself once it sees the close request.
        # Every live spawn is waited for, not only the ones whose
        # session is still in the registry: a create that was
        # abandoned may still have one running.
        spawns = {task for task in self._spawns if not task.done()}
        if spawns:
            await asyncio.wait(spawns, timeout=_SPAWN_WAIT_S)

        # Every session is closed before the registry is cleared: a
        # record never outlives a close that did not happen.
        for session in sessions:
            await self._finish_session(session)

        readers = {task for task in self._readers if not task.done()}
        for reader in readers:
            reader.cancel()
        for reader in readers:
            await asyncio.wait({reader})
        self._readers.clear()

        # A finalizer started by a cancelled create is joined too, so
        # no task of this manager outlives the shutdown.
        while self._finalizers:
            finalizers = {task for task in self._finalizers if not task.done()}
            if not finalizers:
                break
            await asyncio.wait(finalizers, timeout=_SPAWN_WAIT_S)
        self._finalizers.clear()

        # The registry goes only now: every shell above is closed.
        async with self._lock:
            for session in sessions:
                self._sessions.pop(session.session_id, None)

        # Anything this manager still owns is joined, so no task of it
        # outlives the shutdown.
        current = asyncio.current_task()
        pending = {
            task for task in self._tasks if task is not current and not task.done()
        }
        if pending:
            await asyncio.wait(pending, timeout=_SPAWN_WAIT_S)

    async def reap_expired(self) -> list[str]:
        """Prune the detached sessions whose deadline has passed.

        A session that nobody is attached to is pruned
        :data:`TERMINAL_DETACHED_TTL_S` after it became detached. A
        session that *is* attached to a live shell is not pruned here:
        the route owns the unlock watchdog, and this timer is not a
        second one. A session whose shell has exited is the exception —
        there is nothing left to control, so it is released and ages
        out like any other detached one, keeping its final bytes and
        its exit code until it does.

        The decision and the removal happen under one lock, and the
        deadline is re-checked there, so an attach that arrives in
        between cannot have its session closed under it. The shells
        themselves are closed outside the lock.

        Returns the ids that were pruned.
        """
        async with self._lock:
            now = self._clock()
            expired: list[_Session] = []
            for session in list(self._sessions.values()):
                if session.controller is not None and not session.shell_gone:
                    # Somebody is watching a live shell: the route
                    # owns its lifetime, not this timer. A shell that
                    # exited has no watcher — the reader released the
                    # controller — so it ages out like any other
                    # detached record, whoever may look at it.
                    continue
                detached_since = session.detached_since
                if detached_since is None:
                    continue
                if now < detached_since + TERMINAL_DETACHED_TTL_S:
                    continue
                expired.append(session)
                self._sessions.pop(session.session_id, None)
        pruned: list[str] = []
        for session in expired:
            # The session is over the moment it is decided to be
            # gone: a waiter is released at once.
            session.ended = True
            session.wake.set()
            await self._finish_session(session)
            pruned.append(session.session_id)
        return pruned

    async def _sweep_loop(self) -> None:
        """Prune expired sessions until the manager is shut down."""
        while True:
            await asyncio.sleep(_SWEEP_INTERVAL_S)
            try:
                await self.reap_expired()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "terminal session sweep failed: %s", type(exc).__name__
                )

    # ── creating ───────────────────────────────────────────────────────────

    async def create(
        self,
        folder: Path,
        target_kind: TerminalTargetKind,
        target_id: str,
    ) -> TerminalSessionInfo:
        """Open a shell in ``folder`` and return the session it belongs to.

        The slot is reserved *before* the shell is started, so a
        concurrent create sees the reservation and is refused rather
        than starting a fifth process. The spawn itself runs off the
        event loop.

        A spawn that fails, or a create that is cancelled, releases the
        reserved slot and closes the process that arrives late, if one
        does. The refusal is typed: ``invalid_target`` for a target
        this manager cannot hold a session for, ``capacity`` when the
        manager is full, ``spawn_failed`` when the shell could not be
        started, and ``closed`` when a shutdown got there first.
        """
        if target_kind not in ("chat", "project"):
            raise TerminalSessionError("invalid_target")
        if not isinstance(target_id, str) or not target_id.strip():
            raise TerminalSessionError("invalid_target")

        async with self._lock:
            if self._closing:
                raise TerminalSessionError("closed")
            if len(self._sessions) >= TERMINAL_MAX_SESSIONS:
                raise TerminalSessionError("capacity")
            now = self._clock()
            session = _Session(
                session_id=secrets.token_urlsafe(_SESSION_ID_BYTES),
                target_kind=target_kind,
                target_id=target_id,
                folder=Path(folder),
                created_at=now,
                replay=TerminalReplayBuffer(),
                # A session starts detached: nobody is attached to it
                # yet, so its deadline starts now.
                detached_since=now,
                cols=self._cols,
                rows=self._rows,
            )
            self._sessions[session.session_id] = session

        spawn = asyncio.create_task(
            self._spawn(session), name=f"terminal-spawn-{session.session_id}"
        )
        session.spawn = spawn
        self._spawns.add(spawn)
        spawn.add_done_callback(self._spawn_done)

        try:
            # The spawn cannot be cancelled — the thread behind it
            # keeps going — so it is shielded: a cancelled create
            # detaches the caller, not the spawn.
            await asyncio.shield(spawn)
        except asyncio.CancelledError:
            self._abandon(session)
            raise
        except Exception:
            self._abandon(session)
            raise TerminalSessionError("spawn_failed") from None

        if session.spawn_failed:
            self._abandon(session)
            raise TerminalSessionError("spawn_failed")
        if session.close_requested or self._closing:
            self._abandon(session)
            raise TerminalSessionError("closed")
        return self._info(session)

    async def _spawn(self, session: _Session) -> None:
        """Start the process off the loop and hand it to the session.

        A factory that refuses is recorded, not raised: the caller of
        ``create`` turns it into a typed refusal. A process that
        arrives after the session was abandoned — a cancelled create, a
        shutdown — is closed here rather than leaked.
        """
        try:
            process = await asyncio.to_thread(
                self._factory,
                cwd=session.folder,
                cols=self._cols,
                rows=self._rows,
            )
        except Exception:
            session.spawn_failed = True
            return
        session.process = process
        if session.close_requested or self._closing:
            await self._close_session_process(session)
            return
        reader = asyncio.create_task(
            self._reader(session), name=f"terminal-reader-{session.session_id}"
        )
        session.reader = reader
        self._readers.add(reader)
        reader.add_done_callback(self._reader_done)
        flusher = asyncio.create_task(
            self._flusher(session), name=f"terminal-flusher-{session.session_id}"
        )
        session.flusher = flusher
        self._flushers.add(flusher)
        flusher.add_done_callback(self._flusher_done)

    def _spawn_done(self, task: asyncio.Task[None]) -> None:
        """Observe a finished spawn: the task is done, the slot is not."""
        self._spawns.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:  # pragma: no cover - defensive
            logger.warning("terminal spawn failed: %s", type(exc).__name__)

    def _reader_done(self, task: asyncio.Task[None]) -> None:
        """Observe a finished reader, so nothing is reported unretrieved."""
        self._readers.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:  # pragma: no cover - defensive
            logger.warning("terminal reader failed: %s", type(exc).__name__)

    def _flusher_done(self, task: asyncio.Task[None]) -> None:
        """Observe a finished flusher, so nothing is reported unretrieved."""
        self._flushers.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:  # pragma: no cover - defensive
            logger.warning("terminal flusher failed: %s", type(exc).__name__)

    def _abandon(self, session: _Session) -> None:
        """Give up on a session: release the slot and stop the process.

        Synchronous on purpose — it is called from a cancellation
        handler, where an await may not survive. The process itself is
        closed by whoever ends up holding it: the spawn, if it has not
        returned yet, or the finalizer, which waits for the spawn and
        closes what it finds.
        """
        session.close_requested = True
        session.ended = True
        self._sessions.pop(session.session_id, None)
        reader = session.reader
        if reader is not None:
            reader.cancel()
        # The spawn may already have produced a process. The spawn
        # itself checks the close request, but the process can arrive
        # between that check and here, so the close is requested again
        # and the finalizer closes whatever the spawn left behind.
        self._track(self._finalize(session))

    def _track(self, coroutine: Coroutine[Any, Any, None]) -> None:
        """Run a finalizer in the background and keep a reference to it."""
        task = asyncio.create_task(coroutine, name="terminal-session-finalizer")
        self._finalizers.add(task)
        task.add_done_callback(self._finalizer_done)

    def _finalizer_done(self, task: asyncio.Task[None]) -> None:
        """Observe a finished finalizer, so nothing is left unretrieved."""
        self._finalizers.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:  # pragma: no cover - defensive
            logger.warning("terminal finalizer failed: %s", type(exc).__name__)

    async def _finalize(self, session: _Session) -> None:
        """Wait for a session's spawn and close whatever it produced.

        This is the tail of a cancelled create: the spawn is still
        running, and the process it will produce must not be left
        behind. The spawn is awaited, the reader is cancelled and
        joined, and the process is closed off the event loop.
        """
        spawn = session.spawn
        if spawn is not None and not spawn.done():
            await asyncio.wait({spawn}, timeout=_SPAWN_WAIT_S)
        reader = session.reader
        if reader is not None:
            reader.cancel()
            await asyncio.wait({reader})
        await self._final_flush(session)
        await self._close_session_process(session)
        await self._stop_flusher(session)

    # ── reading the registry ───────────────────────────────────────────────

    def get(self, session_id: str) -> TerminalSessionInfo:
        """The record of one session.

        :raises TerminalSessionError: with reason ``not_found``.
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise TerminalSessionError("not_found")
        return self._info(session)

    def list_for(
        self, target_kind: TerminalTargetKind, target_id: str
    ) -> list[TerminalSessionInfo]:
        """The sessions opened for exactly this kind and this id.

        Scoped exactly: a project session and a chat session that
        happen to share an id are two different sessions, and neither
        appears in the other's list. No other target's sessions are
        exposed.
        """
        if target_kind not in ("chat", "project"):
            raise TerminalSessionError("invalid_target")
        return [
            self._info(session)
            for session in self._sessions.values()
            if session.target_kind == target_kind and session.target_id == target_id
        ]

    def retained_count(self) -> int:
        """How many sessions are retained, in total."""
        return len(self._sessions)

    def _info(self, session: _Session) -> TerminalSessionInfo:
        return TerminalSessionInfo(
            session_id=session.session_id,
            target_kind=session.target_kind,
            target_id=session.target_id,
            folder=session.folder,
            cols=session.cols,
            rows=session.rows,
            attached=session.controller is not None,
            generation=session.generation,
            created_at=session.created_at,
            detached_since=session.detached_since,
            exited=session.exited,
            exit_code=session.exit_code,
            failed=session.failed,
            seq=session.replay.seq,
            replay_start=session.replay.oldest,
            pending_write_bytes=len(session.pending) + session.inflight,
        )

    # ── controlling ────────────────────────────────────────────────────────

    def attach(
        self,
        session_id: str,
        connection_id: str,
        offset: int = 0,
        *,
        takeover: bool = False,
    ) -> TerminalSessionAttachment:
        """Take control of a session and get the initial replay.

        The controller is the ``connection_id`` — a random id the route
        mints per attachment. It is not a grant, not a cookie and not a
        browser: two tabs of the same browser are two controllers.

        A session controlled by a *different* connection is refused
        with ``taken_over`` unless the caller asks for a takeover. A
        takeover replaces the controller, bumps the generation and wakes
        the old attachment, so it discovers it has been displaced
        instead of waiting forever for output it may no longer read.

        The offset is checked *before* anything is changed: a negative
        offset, a bool, an offset in the future or one past the
        JavaScript-safe ceiling is refused with ``invalid_offset`` and
        the session is left exactly as it was.

        The snapshot and the live offset that follows it are taken in
        one step, so the bytes between the snapshot's end and the first
        live read are neither lost nor repeated. The snapshot is taken
        *before* the controller is changed, so a refused attach leaves
        the session untouched.
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise TerminalSessionError("not_found")
        if not isinstance(connection_id, str) or not connection_id.strip():
            raise TerminalSessionError("invalid_input")

        # The offset is checked before the controller is changed, so a
        # refused attach leaves the session untouched.
        if type(offset) is not int:  # noqa: E721 - exact int, not isinstance
            raise TerminalSessionError("invalid_offset")
        if offset < 0 or offset > session.replay.seq:
            raise TerminalSessionError("invalid_offset")

        displaced = (
            session.controller is not None and session.controller != connection_id
        )
        if displaced and not takeover:
            raise TerminalSessionError("taken_over")

        # The snapshot is taken before anything is changed: a refused
        # snapshot leaves the session exactly as it was.
        try:
            snapshot = session.replay.snapshot(offset)
        except TerminalReplayError as exc:
            raise TerminalSessionError(_replay_reason(exc.reason)) from None

        if displaced:
            # The old attachment is displaced: its generation is over,
            # and it is woken so it can find out.
            session.generation += 1
            session.wake.set()

        session.controller = connection_id
        session.last_controller = connection_id
        if not session.shell_gone:
            # A live session stops counting down while somebody is
            # watching it. A session whose shell is gone keeps its
            # deadline: there is nothing left to control, so it ages
            # out instead of living on.
            session.detached_since = None
        return TerminalSessionAttachment(
            info=self._info(session),
            snapshot=snapshot,
            next_offset=snapshot.end,
            generation=session.generation,
        )

    def detach(self, session_id: str, connection_id: str) -> TerminalSessionInfo:
        """Give up control of a session and start its detached clock.

        A detach from a connection that is not the controller is
        refused — with ``taken_over`` when somebody else holds the
        session — so a stale detach can never end the successor's
        control.
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise TerminalSessionError("not_found")
        if session.controller is None:
            raise TerminalSessionError("not_controller")
        if session.controller != connection_id:
            raise TerminalSessionError("taken_over")
        session.controller = None
        if not session.shell_gone:
            # A live session starts its detached clock now. A session
            # whose shell is gone keeps the deadline it already has:
            # there is nothing left to control, so it ages out instead
            # of living on.
            session.detached_since = self._clock()
        return self._info(session)

    def _check_controller(self, session: _Session, connection_id: str) -> _Session:
        """The session, if this connection is the one holding it."""
        controller = session.controller
        if controller is None:
            raise TerminalSessionError("not_controller")
        if controller != connection_id:
            raise TerminalSessionError("taken_over")
        return session

    def _require_open(self, session_id: str, connection_id: str) -> _Session:
        """The session, if it is still open and this connection holds it.

        A session whose shell is gone is refused with ``closed``: the
        session is over, and that is the truth about it. The check
        comes before the controller check, so a write to a shell that
        has ended says so instead of talking about control.
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise TerminalSessionError("not_found")
        if session.ended or session.shell_gone or session.process is None:
            raise TerminalSessionError("closed")
        return self._check_controller(session, connection_id)

    def _require_output_reader(
        self, session_id: str, connection_id: str
    ) -> _Session:
        """The session, for a reader of its output.

        A session whose shell has exited has released its controller —
        there is nothing left to control — but its final bytes stay
        readable by the connection that was holding it until the
        record expires, so the client that was watching it can still
        pick up the last output and the exit code.
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise TerminalSessionError("not_found")
        controller = session.controller
        if controller is not None:
            if controller != connection_id:
                raise TerminalSessionError("taken_over")
            return session
        if session.shell_gone and connection_id == session.last_controller:
            return session
        raise TerminalSessionError("not_controller")

    # ── using the session ──────────────────────────────────────────────────

    async def write_input(
        self, session_id: str, connection_id: str, data: bytes
    ) -> int:
        """Send input to the terminal; returns how many bytes were taken in.

        The bytes are *accepted*, not delivered: the caller never waits
        for a write that can block on a full pipe. They are queued in
        order and the session's flusher — the only writer of that queue
        — hands them to the terminal, which may take fewer bytes than
        it was given. The rest stays pending and is retried, and the
        pending backlog is bounded: a payload that would push it past
        the ceiling is refused with ``capacity`` rather than growing a
        buffer inside the engine.

        What is already pending goes out before the new payload, so a
        terminal that is slow to take bytes never reorders the input.

        The controller is checked and the bytes are queued in the same
        step, with no await in between: a write that was refused
        cannot reach the session's successor, and a write that was
        accepted is already part of the session's input.

        The size of the payload is checked before it is copied, so a
        payload that cannot be admitted is never copied at all.
        """
        session = self._require_open(session_id, connection_id)
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TerminalSessionError("invalid_input")
        size = len(data)
        if size == 0:
            return 0
        # The size is checked before the payload is copied, so a
        # payload that cannot be admitted is never copied at all.
        if size > TERMINAL_MAX_PENDING_WRITE_BYTES:
            raise TerminalSessionError("capacity")
        if (
            len(session.pending) + session.inflight + size
            > TERMINAL_MAX_PENDING_WRITE_BYTES
        ):
            raise TerminalSessionError("capacity")
        if session.failed:
            # The terminal already refused an input: the session is
            # failed, and saying so is truer than accepting bytes that
            # will never be written.
            raise TerminalSessionError("io_failed")
        # The queue belongs to the flusher: the bytes go in and the
        # flusher is woken. Nothing here waits for the terminal.
        session.pending.extend(bytes(data))
        session.flush_wake.set()
        return size

    async def resize(
        self, session_id: str, connection_id: str, cols: int, rows: int
    ) -> None:
        """Tell the terminal a new window size, in characters.

        The size is checked before the process is touched: a bool, a
        non-int or a size out of range is refused with
        ``invalid_size`` and the terminal is never asked.
        """
        session = self._require_open(session_id, connection_id)
        if type(cols) is not int or type(rows) is not int:  # noqa: E721
            raise TerminalSessionError("invalid_size")
        if not TERMINAL_MIN_COLS <= cols <= TERMINAL_MAX_COLS:
            raise TerminalSessionError("invalid_size")
        if not TERMINAL_MIN_ROWS <= rows <= TERMINAL_MAX_ROWS:
            raise TerminalSessionError("invalid_size")
        process = session.process
        if process is None:
            raise TerminalSessionError("closed")
        try:
            await asyncio.to_thread(process.resize, cols, rows)
        except Exception:
            raise TerminalSessionError("io_failed") from None
        # The manager is the one that asked for the size, so it is the
        # one that knows it.
        session.cols = cols
        session.rows = rows

    def read(
        self,
        session_id: str,
        connection_id: str,
        offset: int,
        limit: int = TERMINAL_REPLAY_CHUNK_BYTES,
    ) -> ReplaySlice:
        """Read live output from ``offset``.

        A live read is not an initial attach: an offset that has
        already been evicted is refused with ``slow_consumer`` rather
        than clamped, because a client that has lost bytes cannot be
        handed a stream with a hole in it. The client is expected to
        reconnect and replay instead.
        """
        session = self._require_output_reader(session_id, connection_id)
        try:
            return session.replay.read(offset, limit)
        except TerminalReplayError as exc:
            raise TerminalSessionError(_replay_reason(exc.reason)) from None

    def replay(
        self, session_id: str, connection_id: str, offset: int
    ) -> ReplaySnapshot:
        """The retained bytes from ``offset``, the way an attach sees them.

        A stale offset is clamped to the oldest retained byte and
        reported as ``truncated``: the caller resets its renderer
        rather than patching a gap.
        """
        session = self._require_output_reader(session_id, connection_id)
        try:
            return session.replay.snapshot(offset)
        except TerminalReplayError as exc:
            raise TerminalSessionError(_replay_reason(exc.reason)) from None

    async def wait_for_output(
        self,
        session_id: str,
        connection_id: str,
        offset: int,
        timeout: float = 1.0,
    ) -> bool:
        """Wait for output past ``offset``; ``True`` when some arrived.

        The wait is on an event, not on a queue: the ring holds the
        output and the client comes back for it by offset, so a slow
        client costs nothing but its own cursor.

        The wait is released by the things that end the waiter's claim
        on the session: a takeover raises ``taken_over`` and the end of
        the session raises ``not_found``, rather than leaving the
        waiter asleep until its timeout. A shell that exits on its own
        is neither: its final bytes are still readable, so the waiter
        keeps waiting and reports what it sees.
        """
        session = self._require_output_reader(session_id, connection_id)
        generation = session.generation
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            session.wake.clear()
            # The three facts are re-read with no await in between, so
            # a takeover or an end that arrives between the clear and
            # the wait is not lost.
            if session.ended:
                raise TerminalSessionError("not_found")
            if session.generation != generation:
                raise TerminalSessionError("taken_over")
            if session.replay.seq > offset:
                return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(session.wake.wait(), remaining)
            except asyncio.TimeoutError:
                return False

    # ── ending ─────────────────────────────────────────────────────────────

    async def end(self, session_id: str) -> None:
        """End a session: close its shell, then remove its record.

        The close comes first and the record leaves the registry only
        afterwards, so an end that was cancelled can be retried: the
        shell is closed either way, and the retry still finds the
        record it has to remove. The waiters are woken before the
        close, so a waiting client finds out that the session is gone
        instead of waiting for a shell that is on its way out.

        A session that is not there is refused with ``not_found`` — an
        end of a session that already ended is a mistake, not a no-op.
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise TerminalSessionError("not_found")
        session.close_requested = True
        session.ended = True
        session.wake.set()
        await self._finish_session(session)
        # The record goes only now: an end that was cancelled above
        # left it in place, and this call finishes the job.
        async with self._lock:
            self._sessions.pop(session_id, None)
        logger.info("terminal session %s ended", session.session_id)

    async def _finish_session(self, session: _Session) -> None:
        """Close the session's shell and join its tasks.

        The work runs in a manager-owned task and is shielded, so a
        cancelled caller cannot abandon a close that is already under
        way. The record stays in the registry until the caller removes
        it, so a cancelled end can be retried.
        """
        task = session.finish_task
        if task is None:
            task = self._manager_task(
                self._finish_session_inner(session),
                f"terminal-session-close-{session.session_id}",
            )
            session.finish_task = task
        await asyncio.shield(task)

    async def _finish_session_inner(self, session: _Session) -> None:
        """Stop the session's tasks and close its shell."""
        session.close_requested = True
        session.ended = True
        session.wake.set()
        # The reader is cancelled first: it is the task that touches
        # the process, and a reader still running would race the close
        # below for the same shell.
        reader = session.reader
        if reader is not None:
            reader.cancel()
            await asyncio.wait({reader})
        # A spawn that has not come back yet is waited for: the thread
        # behind it cannot be stopped, and the process it produces is
        # closed as soon as it arrives.
        spawn = session.spawn
        if spawn is not None and not spawn.done():
            await asyncio.wait({spawn}, timeout=_SPAWN_WAIT_S)
        # The pending bytes get one last chance to reach the terminal,
        # then the shell is closed — which releases a flusher that is
        # stuck in a blocked write — and then the flusher is joined.
        await self._final_flush(session)
        await self._close_session_process(session)
        await self._stop_flusher(session)

    # ── the reader ─────────────────────────────────────────────────────────

    async def _reader(self, session: _Session) -> None:
        """Move the terminal's output into the ring, until it ends.

        The read is non-blocking by contract, so it is polled inline
        and the reader sleeps between quiet ticks. The reader never
        waits for a consumer: the ring holds the output, and each
        client asks for it by offset. The reader never touches the
        pending input either, so a terminal that will not take its
        input cannot stop its output.

        The shell ending is ``read`` returning empty — not ``poll``
        reporting an exit code. A shell that has exited can still have
        bytes in flight, and stopping on the exit code would lose them.
        """
        process = session.process
        if process is None:
            return
        try:
            while True:
                if session.close_requested:
                    break
                try:
                    chunk = process.read(TERMINAL_READ_BYTES)
                except Exception:
                    session.failed = True
                    break
                if chunk is None:
                    await asyncio.sleep(_READER_IDLE_S)
                    continue
                if not chunk:
                    break
                try:
                    session.replay.append(chunk)
                except TerminalReplayError:
                    # The absolute sequence is exhausted; nothing more
                    # can be kept, so the session ends here.
                    break
                session.wake.set()
        finally:
            # The exit code is taken first, while the process is still
            # there to be asked, and the close is awaited before the
            # session is announced as exited: a caller that sees
            # ``exited`` can rely on the process being closed, and the
            # close (which reaps the shell) cannot be observed half
            # done.
            session.exit_code = _poll_quietly(process)
            session.shell_gone = True
            try:
                if not session.close_requested:
                    # A natural end releases the process here: the shell
                    # is gone and its job groups go with it.
                    await self._close_session_process(session)
                    # There is nothing left to control, so the
                    # controller is released and the detached deadline
                    # starts: an exited record ages out like any other
                    # detached one, keeping its final bytes and its
                    # exit code until it does.
                    session.last_controller = session.controller
                    session.controller = None
                    if session.detached_since is None:
                        session.detached_since = self._clock()
            finally:
                session.exited = True
                session.wake.set()

    # ── the flusher ────────────────────────────────────────────────────────

    async def _flusher(self, session: _Session) -> None:
        """Own the session's pending input: the only writer of it.

        The flusher is woken by :meth:`write_input`, takes the pending
        bytes out of the queue *before* it writes them and puts back
        exactly the bytes the terminal did not take, so a write that is
        cancelled or that fails can neither duplicate nor lose them.
        """
        while True:
            if session.process is None:
                # The shell is gone: there is nothing left to write to.
                return
            # The event is cleared before the queue is read, so a wake
            # that arrives after the clear is not lost: the queue is
            # read right after it.
            session.flush_wake.clear()
            if session.pending:
                try:
                    drained = await self._flush_once(session)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # The terminal refused the input: the session is
                    # failed, and the bytes stay where they are. A
                    # session that is ending is not failed: it ended
                    # on purpose.
                    if not session.close_requested and not session.shell_gone:
                        session.failed = True
                    return
                if drained:
                    continue
            # Nothing pending, or the terminal took only part of it:
            # wait for the next input, with a bounded idle so stuck
            # bytes are retried.
            try:
                await asyncio.wait_for(session.flush_wake.wait(), _FLUSH_IDLE_S)
            except asyncio.TimeoutError:
                pass

    async def _flush_once(self, session: _Session) -> bool:
        """Write what the terminal will take; ``True`` when it took all.

        The bytes leave the queue before the write, so a write that is
        cancelled or that fails cannot write them twice. Exactly the
        bytes the terminal did not take go back, at the front, so the
        input keeps its order.
        """
        process = session.process
        if process is None:
            return not session.pending
        while session.pending:
            chunk = bytes(session.pending)
            del session.pending[:]
            session.inflight = len(chunk)
            # The write owns the bookkeeping: on a cancellation it
            # puts back exactly the bytes the terminal did not take,
            # and on a failure it puts back the whole chunk. Nothing
            # else may touch the queue while the write is in flight.
            try:
                took = await self._write_chunk(session, process, chunk)
            finally:
                session.inflight = 0
            if took <= 0:
                # The terminal will not take anything right now.
                session.pending[:0] = chunk
                return False
            if took < len(chunk):
                # A partial write: the rest stays pending and is
                # written on the next turn.
                session.pending[:0] = chunk[took:]
                return False
        return True

    async def _write_chunk(
        self, session: _Session, process: TerminalProcess, chunk: bytes
    ) -> int:
        """Write ``chunk``; returns how many bytes the terminal took.

        The write runs in its own task and is shielded, so a cancelled
        flusher does not abandon a write that is already in the
        terminal's pipe — and the count it returns is what lets the
        flusher put back exactly the bytes the terminal did not take.
        """
        task = asyncio.create_task(asyncio.to_thread(process.write, chunk))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # The write is already in the pipe: it cannot be taken
            # back, so the flusher waits for the count and puts back
            # exactly the bytes the terminal did not take.
            took = await self._await_write(task)
            session.inflight = 0
            session.pending[:0] = chunk[took:]
            raise
        except Exception:
            # The write failed: the whole chunk goes back, unanswered.
            session.inflight = 0
            session.pending[:0] = chunk
            raise

    async def _await_write(self, task: asyncio.Task[int]) -> int:
        """How many bytes a cancelled write took, once it has finished.

        A second cancellation does not abandon the wait: the bytes are
        already in the terminal's pipe, and the flusher has to know how
        many of them the terminal took before it puts the rest back.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _WRITE_WAIT_S
        while not task.done():
            if loop.time() >= deadline:
                logger.warning("terminal write did not answer in time")
                return 0
            try:
                await asyncio.wait({task}, timeout=_WRITE_WAIT_S)
            except asyncio.CancelledError:
                continue
        if task.cancelled():
            return 0
        exc = task.exception()
        if exc is not None:
            return 0
        return task.result()

    async def _stop_flusher(self, session: _Session) -> None:
        """Give the flusher a last chance, then stop it.

        The pending bytes are offered to the terminal one more time, so
        a command typed just before the end is not silently dropped.
        Then the flusher is cancelled and joined: it is the only writer
        of the pending queue, and the queue dies with it.

        This runs *after* the close, so a flusher that is stuck in a
        blocked write has already been released by the close of the
        shell it was writing to.
        """
        flusher = session.flusher
        if flusher is None:
            return
        if not flusher.done():
            flusher.cancel()
        await asyncio.wait({flusher}, timeout=_FLUSH_WAIT_S)
        if not flusher.done():
            flusher.cancel()
            await asyncio.wait({flusher}, timeout=_FLUSH_WAIT_S)
        session.flusher = None

    async def _final_flush(self, session: _Session) -> None:
        """Offer the pending bytes to the terminal one last time.

        A command typed just before the end is not silently dropped:
        the flusher is woken and given a bounded window to hand the
        bytes over. What it cannot hand over stays pending, and the
        session is over either way.
        """
        flusher = session.flusher
        if flusher is None or not session.pending:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _FLUSH_WAIT_S
        session.flush_wake.set()
        while (session.pending or session.inflight) and loop.time() < deadline:
            await asyncio.sleep(_FLUSH_IDLE_S)

    # ── closing the process ────────────────────────────────────────────────

    async def _close_session_process(self, session: _Session) -> None:
        """Close the session's process, off the event loop, exactly once.

        The close runs in a manager-owned task and is shielded: a
        cancelled caller cannot abandon it, and a work item that is
        already in the executor's hands is never dropped. The process
        is marked closed and dropped only *after* the close has
        actually completed, so a caller that sees a closed session can
        rely on the shell being closed, and the shell is never closed
        twice.

        A close that still fails is logged, not retried: the platform
        adapter has already waited for its own threads, so a retry
        would only hold the session open for nothing.
        """
        task = session.close_task
        if task is None:
            process = session.process
            if process is None or session.process_closed:
                return
            task = self._manager_task(
                self._close_process(session, process),
                f"terminal-process-close-{session.session_id}",
            )
            session.close_task = task
        await asyncio.shield(task)

    async def _close_process(
        self, session: _Session, process: TerminalProcess
    ) -> None:
        """Run the close, off the event loop, and record the outcome.

        The process is marked closed and dropped only after the close
        has actually completed: a caller that sees a closed session can
        rely on the shell being closed, and a close that is still in
        flight is never started a second time.
        """
        try:
            await asyncio.to_thread(process.close)
        except Exception as exc:
            logger.warning("terminal close failed: %s", type(exc).__name__)
        session.process_closed = True
        session.process = None

    # ── manager-owned tasks ────────────────────────────────────────────────

    def _manager_task(
        self, coroutine: Coroutine[Any, Any, None], name: str
    ) -> asyncio.Task[None]:
        """Run a task the manager owns, and keep a reference to it."""
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._manager_task_done)
        return task

    def _manager_task_done(self, task: asyncio.Task[None]) -> None:
        """Observe a finished task, so nothing is reported unretrieved."""
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:  # pragma: no cover - defensive
            logger.warning("terminal session task failed: %s", type(exc).__name__)


def _replay_reason(reason: str) -> TerminalSessionReason:
    """How a refusal from the ring reads in this module's vocabulary."""
    return _REPLAY_REASONS.get(reason, "io_failed")


def _poll_quietly(process: TerminalProcess) -> int | None:
    """The shell's exit code, or ``None`` when it cannot be asked."""
    try:
        return process.poll()
    except Exception:  # pragma: no cover - defensive
        return None
