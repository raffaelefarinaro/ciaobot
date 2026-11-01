"""The native installer's bounded NDJSON event channel (#2340, first slice).

The Mac client that runs the native installer needs deterministic progress
without parsing stdout/stderr and without ever receiving a credential. The
contract is one compact JSON object per line on a writable descriptor the
client passes in (``--events-fd``); everything else the installer prints stays
diagnostics. This module is only that wire contract: the writer, its fixed
vocabulary, its bounds and its failure semantics. It starts no process, reads
no file, touches no service, writes no workspace and is imported by no
production caller yet.

An event has exactly four common keys — ``schema``, ``run_id``, ``seq`` and
``type`` — plus the keys its type allows:

* ``phase`` adds only ``phase``, one of :data:`PHASES`, each at most once and
  in that order (gaps are allowed);
* ``wizard_ready`` adds only ``origin``, the canonical
  ``http://127.0.0.1:<port>`` this run's wizard actually bound;
* ``setup_saved`` adds nothing;
* ``service_ready`` adds only ``origin`` at the verified activation port;
* ``failed`` adds only ``code``, one of :data:`FAILURE_CODES`.

``service_ready`` and ``failed`` are terminal. At most one is ever emitted, it
locks the writer, and :attr:`EventWriter.exit_code` is 0 only for
``service_ready``. Success is never emitted before ``setup_saved``, which is
never emitted before ``wizard_ready``, which is never emitted before the
``wizard`` phase. No method takes free-form text: callers pass a phase name, a
failure code or an integer port, so a password, path, URL, environment value,
exception string or subprocess log cannot reach the channel by construction.
Refusals are a fixed :class:`EventError` reason and never interpolate the
rejected value.

Bounds are enforced rather than documented hopes. A line is at most
:data:`MAX_EVENT_BYTES` including its newline, a run carries at most
:data:`MAX_EVENTS` events across the shell and this writer, and ``seq`` only
increases. The two facts are independent: ``first_seq`` is the next sequence
number and may have gaps, while ``prior_events`` is the count the shell already
wrote, because a gap means ``seq - 1`` is not a count. This writer can only
validate the value it was handed and that the count is not larger than the
sequence allows; the shell owns the values before handover. The writer reserves
one of the remaining slots for the terminal event, so a non-terminal success or
progress event can never leave a run that has no room to report its outcome.
Writing is a non-blocking, deadline-bounded loop that completes short writes; a
closed reader (``EPIPE``), a descriptor that turns unusable, or a pipe that
stays backpressured past :data:`WRITE_TIMEOUT_SECONDS` raises
``channel_closed`` or ``channel_blocked`` and closes the channel, so the
coordinator stops instead of hanging or continuing invisibly. A failure
part-way through a line can leave an incomplete line on the wire; the client
must treat any line it cannot parse as a channel failure.

The writer closes the descriptor it was given and restores the flags it found.
That is deliberate: the coordinator owns the public channel after the shell
hands it over, and closing it is how the reader observes end-of-run. The
public descriptor must never be inherited by the wizard, uv, setup or service
children; that pass-through policy belongs to the coordinator, not here.
Flag restoration and close happen on normal close, on every channel failure,
and when the writer is used as a context manager (``with EventWriter(...)``).
Two limitations are real. A process that dies from ``SIGKILL`` runs no cleanup,
so the shared open file description stays non-blocking and the descriptor
stays open in every other holder. And a descriptor duplicated or inherited by
another process shares its file-status flags with this writer: while the
writer is active, those holders observe ``O_NONBLOCK`` too. The shell should
close its copy when the coordinator takes over and must not use the public
channel itself after handover.

``SIGPIPE`` is a process-global disposition, and this writer never changes it,
blocks it, waits on it, or consumes a pending signal. It requires the process
disposition to already be ``SIG_IGN`` — CPython's default, which turns a
vanished reader into ``EPIPE`` — and refuses with ``sigpipe_not_ignored`` at
construction before changing any descriptor flags or writing, then re-checks
before every write. ``SIG_DFL`` or a custom handler is therefore a safe refusal
that attempts no write and leaves any pending ``SIGPIPE`` untouched. The
coordinator owns the run's signal dispositions: a concurrent change between the
check and ``os.write`` cannot be eliminated by a library, and under ``SIG_DFL``
that race can still terminate the process. This module does not claim
arbitrary-embedding safety; it claims the CPython-default contract.

The writer is not signal-safe and signal handlers must never call its methods.
A handler only sets a flag (or writes to a self-pipe/event) and returns; the
coordinator's normal loop observes that flag and emits ``failed("cancelled")``
outside the handler. The writer's thread lock is re-entrant and a re-entrant
emit is refused with ``reentrant_emit`` rather than deadlocking, but that guard
is a last resort, not a supported cancellation mechanism.

The supported public descriptor range is 3–9 (the POSIX shell portable range);
the shell and coordinator entry points own that check because they are the ones
parsing the command line. This writer requires a non-stdio descriptor (``>= 3``)
and refuses a read-only or closed one. It accepts any writable descriptor the
platform can write to, including pipes and sockets; it never requires a
regular file. The native path is macOS-only: on Windows the constructor refuses
rather than pretend a descriptor protocol the legacy installer does not
implement.

The module also owns the separate run-bound native-install lock
(``NativeRunLock``): one ``0700`` directory ``native-install.lock`` with one
``0600`` ``run.json`` provenance record under the caller's private
install-state directory. ``prepare_native_state_dir`` is the explicit,
mutating preparation step: call it after the read-only freshness preflight.
The target must lie below a trusted root (the home directory by default, or an
explicit caller-supplied root), and every component from that root down is
opened descriptor-relative and no-follow before anything is created; a symlink
anywhere in the controlled chain is refused rather than followed. It creates
only missing components with ``0700`` while never chmod'ing, repairing or
replacing an existing one; a umask or permission failure leaves what was
created as evidence and refuses. The trusted root keeps macOS system prefixes
such as ``/var`` outside the validated chain. ``NativeRunLock.acquire`` itself
never creates the state directory. Creation of a lock is one atomic ``mkdir``
plus one exclusive record create, both descriptor-relative. An existing lock
is classified honestly and refused: a valid record is ``lock_busy`` (a run may
be live or stale, and no same-run shortcut adopts it); an empty directory or a
missing, empty, truncated or undecodable private record is ``lock_unknown``
(concurrent or interrupted, never corruption — it may be a live creator
between ``mkdir`` and record write, an interrupted one, or a lock/record that
disappeared between classification steps during a concurrent release); a symlink,
non-directory, non-private directory, foreign entry, unsafe record shape or a complete
record this version cannot accept is ``lock_unsafe`` (not a corruption
diagnosis). Neither is repaired, deleted or
adopted; a partial retry stays refused and there is no stale-lock recovery, no
flock unlock as authorization, and no silent reinstall. Manual
recovery is an operator decision outside this primitive: if no native install is
running, re-run the read-only freshness preflight and inspect the fixed private
state-directory evidence; the tooling does not delete, adopt or roll it back
automatically, and its errors never print the inspected path. A released lock
is removed only when its directory and record still match the device/inode and
exact bytes this run created, it is the only entry, the record has no extra hard
link, and both are still private. A refusal before the record unlink leaves the
state untouched; a failure after the unlink (for example, a final ``rmdir``) has
already removed the record and leaves the directory, and still raises
``lock_cleanup_refused`` instead of reporting success. The error carries a
path-free ``LockCleanupReport`` (record/directory removed) and a
``original_reason`` where one exists, so the coordinator can record what
happened; ``cleanup`` can be ``None`` after an asynchronous interruption before
a report was recorded. There is no recursive deletion or chmod. Release is serialized by an
internal lock and a re-entrant call is refused with ``lock_reentrant_release``;
a failed release is not silently retried and keeps its cleanup report. A
``SIGKILL`` or unprovable cleanup leaves evidence for the next refusal. Signal
handlers must not call ``release`` either; cancellation belongs to the
coordinator's normal loop. A same-UID writer can still race between a check and
the mutation it guards, so these checks are evidence, not proof.
"""

from __future__ import annotations

import errno
import json
import math
import os
import select
import signal
import stat
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Final, NoReturn

from ciao.os_support.private import (
    descriptor_acl_grants_another_account,
    is_owned_descriptor,
    is_private_descriptor_strict,
)

#: The wire schema for every event this module writes.
SCHEMA_VERSION: Final[int] = 1
#: One compact NDJSON line, newline included.
MAX_EVENT_BYTES: Final[int] = 1024
#: The whole run: shell-written events plus this writer's events.
MAX_EVENTS: Final[int] = 32
#: The lowest descriptor the public channel may use; 0–2 are stdio.
MIN_EVENT_FD: Final[int] = 3
#: How long one event may wait for a backpressured descriptor to drain.
WRITE_TIMEOUT_SECONDS: Final[float] = 5.0
#: The longest test/embedding timeout this writer will accept.
MAX_WRITE_TIMEOUT_SECONDS: Final[float] = 60.0

#: The machine-level native-install lock directory, under ciaobot's state dir.
LOCK_DIR_NAME: Final[str] = "native-install.lock"
#: The one provenance record inside that directory.
LOCK_RECORD_NAME: Final[str] = "run.json"
#: The lock record schema. Independent of the public event schema.
LOCK_SCHEMA_VERSION: Final[int] = 1
#: The provenance record is one small JSON object.
MAX_LOCK_RECORD_BYTES: Final[int] = 1024

#: The fixed phase vocabulary, in the only order a run may emit it.
PHASES: Final[tuple[str, ...]] = (
    "preflight",
    "bootstrap",
    "download",
    "install",
    "wizard",
    "activation",
    "readiness",
)
#: The fixed failure vocabulary. Every code is safe to show to a client.
FAILURE_CODES: Final[tuple[str, ...]] = (
    "invalid_arguments",
    "existing_install",
    "partial_install",
    "unknown_state",
    "install_in_progress",
    "download_failed",
    "verification_failed",
    "install_failed",
    "wizard_failed",
    "activation_refused",
    "service_not_ready",
    "cancelled",
    "event_channel_failed",
    "internal_error",
)
#: Every event type this module can write.
EVENT_TYPES: Final[tuple[str, ...]] = (
    "phase",
    "wizard_ready",
    "setup_saved",
    "service_ready",
    "failed",
)
#: The types that end a run; exactly one of them may ever be written.
TERMINAL_TYPES: Final[tuple[str, ...]] = ("service_ready", "failed")

_PHASE_ORDER: Final[dict[str, int]] = {
    name: index for index, name in enumerate(PHASES)
}
_NONBLOCK: Final[int] = getattr(os, "O_NONBLOCK", 0)
_ACCMODE: Final[int] = getattr(os, "O_ACCMODE", 3)
#: Test seam: all lock inode checks go through this alias so a focused test
#: can inject an fstat failure without changing the private-descriptor helper.
_fstat = os.fstat

_SIGPIPE = getattr(signal, "SIGPIPE", None)


def _sigpipe_is_ignored() -> bool:
    """Whether the process disposition is the one this writer requires.

    CPython ignores ``SIGPIPE`` by default, so a vanished reader surfaces as
    ``BrokenPipeError`` instead of terminating the process. The writer never
    changes that disposition, blocks the signal, or consumes a pending one; it
    only reads it. A different or unreadable disposition is a refusal.
    """
    if sys.platform == "win32":
        # Windows has no SIGPIPE; the constructor refuses the descriptor path
        # separately rather than pretending this protocol exists there.
        return True
    if _SIGPIPE is None:
        return False
    try:
        return signal.getsignal(_SIGPIPE) is signal.SIG_IGN
    except (OSError, ValueError):
        return False


class EventError(RuntimeError):
    """A refusal with a fixed, public reason and no interpolated detail."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _canonical_run_id(value: object) -> str:
    """Return ``value`` only when it is already the canonical UUID spelling."""
    if type(value) is not str:
        raise EventError("invalid_run_id")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise EventError("invalid_run_id") from None
    if str(parsed) != value:
        raise EventError("invalid_run_id")
    return value


def _phase_index(name: object) -> int:
    if type(name) is not str or name not in _PHASE_ORDER:
        raise EventError("unknown_phase")
    return _PHASE_ORDER[name]


def _failure_code(code: object) -> str:
    if type(code) is not str or code not in FAILURE_CODES:
        raise EventError("unknown_failure_code")
    return code


def _loopback_origin(port: object) -> str:
    """The one origin spelling an event may carry, built from a port alone."""
    if type(port) is not int:
        raise EventError("invalid_origin")
    if not 1 <= port <= 65535:
        raise EventError("invalid_origin")
    return f"http://127.0.0.1:{port}"


def _descriptor_flags(fd: int) -> int:
    """The current file-status flags, or ``invalid_fd`` if it is not usable."""
    if sys.platform == "win32":  # pragma: no cover - the native path is macOS
        raise EventError("invalid_fd")
    import fcntl

    try:
        return int(fcntl.fcntl(fd, fcntl.F_GETFL))
    except OSError:
        raise EventError("invalid_fd") from None


def _set_descriptor_flags(fd: int, flags: int) -> None:
    if sys.platform == "win32":  # pragma: no cover - the native path is macOS
        raise EventError("invalid_fd")
    import fcntl

    try:
        fcntl.fcntl(fd, fcntl.F_SETFL, flags)
    except OSError:
        raise EventError("invalid_fd") from None


class EventWriter:
    """Write one run's bounded events to an inherited writable descriptor."""

    def __init__(
        self,
        fd: int,
        *,
        run_id: str,
        first_seq: int = 1,
        prior_events: int = 0,
        write_timeout: float = WRITE_TIMEOUT_SECONDS,
    ) -> None:
        self._run_id = _canonical_run_id(run_id)
        if type(first_seq) is not int or first_seq < 1:
            raise EventError("invalid_seq")
        if type(prior_events) is not int or prior_events < 0:
            raise EventError("invalid_prior_events")
        if prior_events >= MAX_EVENTS:
            raise EventError("sequence_exhausted")
        if prior_events > first_seq - 1:
            raise EventError("invalid_seq")
        if type(write_timeout) not in (int, float):
            raise EventError("invalid_timeout")
        timeout = float(write_timeout)
        if (
            not math.isfinite(timeout)
            or timeout <= 0
            or timeout > MAX_WRITE_TIMEOUT_SECONDS
        ):
            raise EventError("invalid_timeout")
        if type(fd) is not int or fd < MIN_EVENT_FD:
            raise EventError("invalid_fd")
        if not _sigpipe_is_ignored():
            # Fail closed before changing any descriptor flags or writing: a
            # SIG_DFL/custom disposition can terminate the process instead of
            # reporting EPIPE, and the writer must not change it.
            raise EventError("sigpipe_not_ignored")
        flags = _descriptor_flags(fd)
        if flags & _ACCMODE == os.O_RDONLY:
            raise EventError("read_only_fd")
        try:
            info = os.fstat(fd)
        except OSError:
            raise EventError("invalid_fd") from None
        if stat.S_ISDIR(info.st_mode):
            raise EventError("invalid_fd")
        try:
            _set_descriptor_flags(fd, flags | _NONBLOCK)
        except EventError:
            raise
        except OSError:
            raise EventError("invalid_fd") from None

        self._fd: int | None = fd
        self._original_flags = flags
        self._write_timeout = timeout
        self._prior_events = prior_events
        self._next_seq = first_seq
        self._emitted = 0
        self._phases: set[str] = set()
        self._last_phase = -1
        self._wizard_ready = False
        self._setup_saved = False
        self._terminal: str | None = None
        self._emitting = False
        self._lock = threading.RLock()

    # ── Public state ──────────────────────────────────────────────────────

    @property
    def terminal(self) -> str | None:
        """``service_ready``, ``failed``, or ``None`` if the run did not end."""
        return self._terminal

    @property
    def exit_code(self) -> int:
        """0 only when this run emitted ``service_ready``."""
        return 0 if self._terminal == "service_ready" else 1

    # ── The fixed vocabulary ──────────────────────────────────────────────

    def phase(self, name: str) -> None:
        """Announce one phase, once, in order, with no free-form payload."""
        with self._lock:
            self._require_open()
            index = _phase_index(name)
            if name in self._phases:
                raise EventError("duplicate_phase")
            if index <= self._last_phase:
                raise EventError("phase_out_of_order")
            self._emit_locked("phase", {"phase": name})
            self._phases.add(name)
            self._last_phase = index

    def wizard_ready(self, port: int) -> None:
        """This run's wizard bound ``port``; emits its canonical origin."""
        with self._lock:
            self._require_open()
            if "wizard" not in self._phases:
                raise EventError("wizard_not_started")
            if self._wizard_ready:
                raise EventError("wizard_already_ready")
            origin = _loopback_origin(port)
            self._emit_locked("wizard_ready", {"origin": origin})
            self._wizard_ready = True

    def setup_saved(self) -> None:
        """The guarded wizard save completed; carries no data at all."""
        with self._lock:
            self._require_open()
            if not self._wizard_ready:
                raise EventError("wizard_not_ready")
            if self._setup_saved:
                raise EventError("setup_already_saved")
            self._emit_locked("setup_saved", {})
            self._setup_saved = True

    def service_ready(self, port: int) -> None:
        """Verified activation and live readiness succeeded; terminal."""
        with self._lock:
            self._require_open()
            if not self._setup_saved:
                raise EventError("setup_not_saved")
            origin = _loopback_origin(port)
            self._emit_locked("service_ready", {"origin": origin})
            self._terminal = "service_ready"

    def failed(self, code: str) -> None:
        """End the run with one fixed failure code; terminal."""
        with self._lock:
            self._require_open()
            self._emit_locked("failed", {"code": _failure_code(code)})
            self._terminal = "failed"

    # ── Lifetime ──────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the channel and restore the flags this writer changed."""
        with self._lock:
            if self._emitting:
                # Same last-resort guard as an emit: a handler that closes the
                # channel mid-write is a programming error, not cancellation.
                raise EventError("reentrant_emit")
            self._release_locked()

    def __enter__(self) -> EventWriter:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    # ── Internals (all called with the lock held) ─────────────────────────

    def _require_open(self) -> None:
        if self._fd is None:
            raise EventError("channel_unavailable")
        if self._terminal is not None:
            raise EventError("after_terminal")

    def _emit_locked(self, event_type: str, extra: dict[str, object]) -> None:
        if self._emitting:
            # A signal handler that calls back into the writer is a programming
            # error, not a supported cancellation path. Refuse it instead of
            # deadlocking or interleaving a second line with the one in flight.
            raise EventError("reentrant_emit")
        used = self._prior_events + self._emitted
        terminal = event_type in TERMINAL_TYPES
        # Leave one slot for the terminal event no matter how many events the
        # shell already wrote or how far ``seq`` has advanced.
        if (used + 1 if terminal else used + 2) > MAX_EVENTS:
            raise EventError("too_many_events")
        payload: dict[str, object] = {
            "schema": SCHEMA_VERSION,
            "run_id": self._run_id,
            "seq": self._next_seq,
            "type": event_type,
        }
        payload.update(extra)
        line = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
        encoded = line.encode("utf-8") + b"\n"
        if len(encoded) > MAX_EVENT_BYTES:
            raise EventError("event_too_large")
        self._emitting = True
        try:
            self._write_all(encoded)
            self._emitted += 1
            self._next_seq += 1
        finally:
            self._emitting = False

    def _write_all(self, data: bytes) -> None:
        fd = self._fd
        if fd is None:
            raise EventError("channel_unavailable")
        view = memoryview(data)
        deadline = time.monotonic() + self._write_timeout
        while view:
            if not _sigpipe_is_ignored():
                # A disposition changed after construction (for example by an
                # embedding process) is not safe to write under. Refuse rather
                # than mutate it or wait on a signal.
                self._fail_locked("sigpipe_not_ignored")
            try:
                written = self._write_once(fd, view)
            except BlockingIOError:
                written = 0
            except OSError:
                self._fail_locked("channel_closed")
            if written < 0:
                self._fail_locked("channel_closed")
            if written:
                view = view[written:]
                continue
            failure = self._wait_writable(fd, deadline)
            if failure is not None:
                self._fail_locked(failure)

    def _write_once(self, fd: int, data: memoryview) -> int:
        """One ``os.write``; tests replace this to force short writes."""
        return os.write(fd, data)

    def _wait_writable(self, fd: int, deadline: float) -> str | None:
        """``None`` when writable, else the fixed channel failure to report."""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "channel_blocked"
            try:
                _, writable, exceptional = select.select(
                    [], [fd], [fd], remaining
                )
            except InterruptedError:
                continue
            except (OSError, ValueError, OverflowError):
                return "channel_closed"
            if exceptional:
                return "channel_closed"
            if writable:
                return None
            return "channel_blocked"

    def _fail_locked(self, reason: str) -> NoReturn:
        self._release_locked()
        raise EventError(reason) from None

    def _release_locked(self) -> None:
        fd = self._fd
        flags = self._original_flags
        self._fd = None
        if fd is None:
            return
        try:
            _set_descriptor_flags(fd, flags)
        except (EventError, OSError):
            pass
        try:
            os.close(fd)
        except OSError:
            pass


@dataclass(frozen=True, slots=True)
class LockCleanupReport:
    """What a refused cleanup actually removed, with no paths or inodes."""

    record_removed: bool
    directory_removed: bool


class NativeLockError(RuntimeError):
    """A fixed native-install lock refusal with no interpolated path detail.

    ``cleanup`` is a path-free :class:`LockCleanupReport` when a cleanup
    refusal recorded one. It is ``None`` for a non-cleanup error, and can be
    ``None`` after an asynchronous interruption (for example a signal or
    ``KeyboardInterrupt``) before any report could be recorded; callers must
    not fabricate a success or a removal state from a missing report.
    """

    def __init__(
        self,
        reason: str,
        *,
        original_reason: str | None = None,
        cleanup: LockCleanupReport | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.original_reason = original_reason
        self.cleanup = cleanup


def _is_missing(exc: OSError) -> bool:
    """Whether an OS error means the name disappeared during a concurrent release."""
    return isinstance(exc, FileNotFoundError) or exc.errno == errno.ENOENT


def _open_existing_dir(path: Path, *, exact_private: bool) -> int:
    """Open an existing real directory, optionally requiring exact ``0700``."""
    try:
        status = os.lstat(path)
    except OSError:
        raise NativeLockError("state_dir_unsafe") from None
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise NativeLockError("state_dir_unsafe")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise NativeLockError("state_dir_unsafe") from None
    try:
        try:
            opened = _fstat(descriptor)
        except OSError:
            raise NativeLockError("state_dir_unsafe") from None
        if (opened.st_dev, opened.st_ino) != (status.st_dev, status.st_ino):
            raise NativeLockError("state_dir_unsafe")
        if not stat.S_ISDIR(opened.st_mode):
            raise NativeLockError("state_dir_unsafe")
        if exact_private:
            try:
                private = is_private_descriptor_strict(descriptor)
            except OSError:
                raise NativeLockError("state_dir_unsafe") from None
            if not private:
                raise NativeLockError("state_dir_unsafe")
        else:
            try:
                owned = is_owned_descriptor(descriptor)
                acl_grant = descriptor_acl_grants_another_account(descriptor)
            except OSError:
                raise NativeLockError("state_dir_unsafe") from None
            if (
                not owned
                or acl_grant
                or stat.S_IMODE(opened.st_mode) & 0o022
            ):
                raise NativeLockError("state_dir_unsafe")
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    return descriptor


def _open_private_state_dir(state_dir: Path) -> int:
    """Open the caller's existing install-state directory, descriptor-bound.

    The native lock lives inside ``~/.local/state/ciaobot`` (or whatever the
    caller passes). That directory must already exist and be an owner-only real
    directory; this primitive never creates, chmods or repairs it. Use
    :func:`prepare_native_state_dir` explicitly after the read-only freshness
    preflight when the chain is still missing.
    """
    return _open_existing_dir(state_dir, exact_private=True)


def prepare_native_state_dir(
    state_dir: Path, *, trusted_root: Path | None = None
) -> Path:
    """Explicitly create the missing private install-state chain, or refuse.

    This is the only mutating preparation step. It must run after the native
    installer's read-only freshness preflight and before lock acquisition. The
    target must lie below ``trusted_root`` (the caller's home directory by
    default; tests and embedding callers may pass another explicit root). Every
    component from that root down to the target is opened descriptor-relative
    and no-follow: a symlink anywhere in the HOME-relative chain is a refusal,
    not something to follow. Existing components are judged and never
    chmod'ed, repaired, replaced or removed. Missing components are created
    ``0700`` and immediately checked; a restrictive umask or permission failure
    is ``state_dir_unsafe`` and leaves what was created as evidence. The final
    state directory is never created by :meth:`NativeRunLock.acquire` itself.

    The trusted root is the portability boundary: on macOS the system prefix
    above the user's home (including ``/var``) is not inspected, while every
    component the user controls below the root is. A same-UID writer can still
    race between validating a component and creating its child; each created
    component is checked, but that is evidence, not proof of exclusive
    ownership.
    """
    if sys.platform == "win32":
        raise NativeLockError("unsupported_platform")
    target = Path(state_dir)
    try:
        root = Path.home() if trusted_root is None else Path(trusted_root)
    except (OSError, RuntimeError):
        raise NativeLockError("state_dir_unsafe") from None
    for path in (target, root):
        if not path.is_absolute():
            raise NativeLockError("state_dir_unsafe")
        if any(part in {".", ".."} for part in path.parts):
            raise NativeLockError("state_dir_unsafe")
    if target != root and root not in target.parents:
        raise NativeLockError("state_dir_unsafe")

    parent_fd = _open_existing_dir(root, exact_private=target == root)
    current = root
    try:
        for part in target.relative_to(root).parts:
            component = current / part
            created = False
            try:
                os.lstat(component)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=parent_fd)
                    created = True
                except FileExistsError:
                    pass
                except OSError:
                    raise NativeLockError("state_dir_unsafe") from None
            except OSError:
                raise NativeLockError("state_dir_unsafe") from None

            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                child_fd = os.open(part, flags, dir_fd=parent_fd)
            except OSError:
                raise NativeLockError("state_dir_unsafe") from None
            try:
                try:
                    opened = _fstat(child_fd)
                except OSError:
                    raise NativeLockError("state_dir_unsafe") from None
                if not stat.S_ISDIR(opened.st_mode):
                    raise NativeLockError("state_dir_unsafe")
                try:
                    named = os.stat(
                        part, dir_fd=parent_fd, follow_symlinks=False
                    )
                except OSError:
                    raise NativeLockError("state_dir_unsafe") from None
                if (named.st_dev, named.st_ino) != (
                    opened.st_dev,
                    opened.st_ino,
                ):
                    raise NativeLockError("state_dir_unsafe")
                if created or component == target:
                    try:
                        private = is_private_descriptor_strict(child_fd)
                    except OSError:
                        raise NativeLockError("state_dir_unsafe") from None
                    if not private:
                        raise NativeLockError("state_dir_unsafe")
                else:
                    try:
                        owned = is_owned_descriptor(child_fd)
                        acl_grant = descriptor_acl_grants_another_account(
                            child_fd
                        )
                    except OSError:
                        raise NativeLockError("state_dir_unsafe") from None
                    if (
                        not owned
                        or acl_grant
                        or stat.S_IMODE(opened.st_mode) & 0o022
                    ):
                        raise NativeLockError("state_dir_unsafe")
            except BaseException:
                try:
                    os.close(child_fd)
                except OSError:
                    pass
                raise
            try:
                os.close(parent_fd)
            except OSError:
                pass
            parent_fd = child_fd
            current = component
        return target
    finally:
        try:
            os.close(parent_fd)
        except OSError:
            pass


def _lock_record_bytes(run_id: str) -> bytes:
    payload = {
        "schema": LOCK_SCHEMA_VERSION,
        "run_id": run_id,
        "pid": os.getpid(),
    }
    encoded = (
        json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        )
        + b"\n"
    )
    if len(encoded) > MAX_LOCK_RECORD_BYTES:
        raise NativeLockError("lock_record_failed")
    return encoded


def _write_record(fd: int, data: bytes) -> None:
    """Write the small provenance record, completing short writes."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("the lock record write made no progress")
        view = view[written:]


def _read_bounded_fd(fd: int, limit: int) -> bytes | None:
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            return None
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)


def _record_state(data: bytes) -> str:
    """Classify record bytes as ``busy``, ``unknown`` or ``unsafe``.

    Empty, truncated or undecodable bytes are ``unknown`` (an interrupted
    write). A complete JSON value this schema cannot accept is ``unsafe``
    (foreign or incompatible), never described as corruption.
    """
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "unknown"
    if not isinstance(payload, dict) or set(payload) != {"schema", "run_id", "pid"}:
        return "unsafe"
    if type(payload["schema"]) is not int or payload["schema"] != LOCK_SCHEMA_VERSION:
        return "unsafe"
    run_id = payload["run_id"]
    if type(run_id) is not str:
        return "unsafe"
    try:
        _canonical_run_id(run_id)
    except EventError:
        return "unsafe"
    pid = payload["pid"]
    if type(pid) is not int or pid <= 0:
        return "unsafe"
    return "busy"


def _lock_record_state(dir_fd: int) -> str:
    """Classify an existing record as ``busy``, ``unknown`` or ``unsafe``.

    A missing, empty, truncated or undecodable record in an otherwise private
    single-record directory is ``unknown``: a live creator may not have written
    it yet, or an earlier run was interrupted. A record with a foreign shape or
    privacy (symlink, hard link, non-regular file, oversized, wrong owner, mode
    or ACL) or a complete record this schema cannot accept is ``unsafe``.
    """
    try:
        entry = os.stat(LOCK_RECORD_NAME, dir_fd=dir_fd, follow_symlinks=False)
    except OSError as exc:
        return "unknown" if _is_missing(exc) else "unsafe"
    if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
        return "unsafe"
    if entry.st_size > MAX_LOCK_RECORD_BYTES:
        return "unsafe"
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(LOCK_RECORD_NAME, flags, dir_fd=dir_fd)
    except OSError as exc:
        return "unknown" if _is_missing(exc) else "unsafe"
    try:
        try:
            opened = _fstat(descriptor)
        except OSError as exc:
            return "unknown" if _is_missing(exc) else "unsafe"
        if (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino):
            return "unsafe"
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            return "unsafe"
        try:
            if not is_private_descriptor_strict(descriptor):
                return "unsafe"
        except OSError:
            return "unsafe"
        if opened.st_size > MAX_LOCK_RECORD_BYTES:
            return "unsafe"
        data = _read_bounded_fd(descriptor, MAX_LOCK_RECORD_BYTES)
        if data is None:
            return "unknown"
        return _record_state(data)
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _existing_lock_reason(parent_fd: int) -> str:
    """Classify a pre-existing lock directory without changing it.

    A valid record is ``lock_busy``. An empty directory or a missing, empty,
    truncated or undecodable record is ``lock_unknown``: concurrent (a live
    creator between ``mkdir`` and its record write) or interrupted, never
    corruption. A symlink, non-directory, non-private directory, foreign entry
    or unsafe record shape is ``lock_unsafe``. Every answer leaves the state
    untouched.
    """
    try:
        entry = os.stat(LOCK_DIR_NAME, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        return "lock_unknown" if _is_missing(exc) else "lock_unsafe"
    if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
        return "lock_unsafe"
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        dir_fd = os.open(LOCK_DIR_NAME, flags, dir_fd=parent_fd)
    except OSError as exc:
        return "lock_unknown" if _is_missing(exc) else "lock_unsafe"
    try:
        try:
            opened = _fstat(dir_fd)
        except OSError as exc:
            return "lock_unknown" if _is_missing(exc) else "lock_unsafe"
        if (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino):
            return "lock_unsafe"
        try:
            if not is_private_descriptor_strict(dir_fd):
                return "lock_unsafe"
        except OSError as exc:
            return "lock_unknown" if _is_missing(exc) else "lock_unsafe"
        try:
            names = sorted(os.listdir(dir_fd))
        except OSError as exc:
            return "lock_unknown" if _is_missing(exc) else "lock_unsafe"
        if not names:
            return "lock_unknown"
        if names != [LOCK_RECORD_NAME]:
            return "lock_unsafe"
        state = _lock_record_state(dir_fd)
        if state == "busy":
            return "lock_busy"
        return "lock_unknown" if state == "unknown" else "lock_unsafe"
    finally:
        try:
            os.close(dir_fd)
        except OSError:
            pass


def _remove_new_lock_best_effort(
    parent_fd: int,
    dir_fd: int,
    dir_identity: tuple[int, int],
    record_identity: tuple[int, int] | None,
) -> LockCleanupReport:
    """Remove only a lock this call just created, or leave it all in place.

    Used after a failed acquisition. Every step is descriptor-relative and
    inode-checked; a substituted directory, a foreign entry, a hard-linked
    record or any error leaves the remaining state for the next run to refuse.
    The returned report says exactly what was removed, without paths or inodes.
    """
    record_removed = False
    directory_removed = False
    try:
        named = os.stat(LOCK_DIR_NAME, dir_fd=parent_fd, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != dir_identity:
            return LockCleanupReport(record_removed, directory_removed)
        if not stat.S_ISDIR(named.st_mode):
            return LockCleanupReport(record_removed, directory_removed)
        names = sorted(os.listdir(dir_fd))
        if record_identity is not None and names == [LOCK_RECORD_NAME]:
            entry = os.stat(
                LOCK_RECORD_NAME, dir_fd=dir_fd, follow_symlinks=False
            )
            if (entry.st_dev, entry.st_ino) != record_identity:
                return LockCleanupReport(record_removed, directory_removed)
            if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
                return LockCleanupReport(record_removed, directory_removed)
            os.unlink(LOCK_RECORD_NAME, dir_fd=dir_fd)
            record_removed = True
        elif names:
            return LockCleanupReport(record_removed, directory_removed)
        if os.listdir(dir_fd):
            return LockCleanupReport(record_removed, directory_removed)
        named = os.stat(LOCK_DIR_NAME, dir_fd=parent_fd, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != dir_identity:
            return LockCleanupReport(record_removed, directory_removed)
        os.rmdir(LOCK_DIR_NAME, dir_fd=parent_fd)
        directory_removed = True
        return LockCleanupReport(record_removed, directory_removed)
    except OSError:
        return LockCleanupReport(record_removed, directory_removed)


class NativeRunLock:
    """A run-bound, exclusive native-install lock with inode-bound cleanup.

    Acquisition atomically creates ``<state_dir>/native-install.lock`` with
    ``0700`` and one ``0600`` ``run.json`` provenance record (schema, canonical
    run id, pid). An existing directory is classified and refused, never
    repaired: a valid record is ``lock_busy``; an empty directory or a partial,
    truncated or undecodable private record is ``lock_unknown`` (concurrent or
    interrupted, never corruption); a symlink, foreign entry, non-private
    directory, unsafe record shape or a complete record this version cannot
    accept is ``lock_unsafe`` (not a corruption diagnosis). There is deliberately no
    same-run shortcut and no stale-lock recovery — a dead process's valid record
    is still evidence that a native install may have run.

    Release removes only the directory and record this object created. Both are
    re-opened relative to held directory descriptors, required to match the
    recorded device/inode, required to have no extra link or foreign entry, and
    the record's exact bytes must still be present. A refusal before the record
    unlink leaves the state untouched; a refusal after it (for example, the
    final ``rmdir`` fails because a foreign entry appeared) has already removed
    the record and leaves the directory, and still raises
    ``lock_cleanup_refused`` — it is never reported as success and is not
    retried. There is no recursive deletion and no path is chmod'ed, replaced or
    recovered. Call :meth:`release` or use the context manager; dropping the
    object does not remove the lock. Release is serialized by an internal
    re-entrant lock; a same-thread re-entrant call raises
    ``lock_reentrant_release`` instead of reusing descriptors.

    Limits: a same-UID writer can race between an identity check and a
    ``mkdir``/``unlink``/``rmdir``; the checks are evidence, not proof, and this
    module does not claim otherwise. A ``SIGKILL`` leaves the lock in place, and
    the next acquisition refuses it as ``lock_busy`` or ``lock_unknown``. A
    failed acquisition removes the directory it just created only when that
    removal can be proven. When an attempted removal cannot be proven (a
    substituted name, a foreign entry, an extra hard link), it leaves the state
    and raises ``lock_cleanup_refused`` from the original refusal; a permission
    or umask failure preserves the new evidence and reports the original lock
    refusal without attempting an unsafe cleanup. Acquiring never creates or
    repairs the state directory: call :func:`prepare_native_state_dir`
    explicitly after the read-only freshness preflight.
    """

    def __init__(
        self,
        *,
        state_dir: Path,
        run_id: str,
        parent_fd: int,
        dir_fd: int,
        dir_identity: tuple[int, int],
        record_identity: tuple[int, int],
        record_bytes: bytes,
    ) -> None:
        self._state_dir = state_dir
        self._run_id = run_id
        self._parent_fd = parent_fd
        self._dir_fd = dir_fd
        self._dir_identity = dir_identity
        self._record_identity = record_identity
        self._record_bytes = record_bytes
        self._released = False
        self._cleanup_failed = False
        self._cleanup_report: LockCleanupReport | None = None
        self._releasing = False
        self._release_guard = threading.RLock()

    @classmethod
    def acquire(cls, state_dir: Path, *, run_id: str) -> NativeRunLock:
        """Atomically create this run's lock, or refuse an existing one."""
        if sys.platform == "win32":
            raise NativeLockError("unsupported_platform")
        try:
            canonical = _canonical_run_id(run_id)
        except EventError:
            raise NativeLockError("invalid_run_id") from None
        parent_fd = _open_private_state_dir(state_dir)
        dir_fd: int | None = None
        record_fd: int | None = None
        dir_identity: tuple[int, int] | None = None
        record_identity: tuple[int, int] | None = None
        record_bytes = b""
        cleanup_ok = False
        keep_open = False
        try:
            try:
                os.mkdir(LOCK_DIR_NAME, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                raise NativeLockError(_existing_lock_reason(parent_fd)) from None
            except OSError:
                raise NativeLockError("lock_create_failed") from None

            flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                dir_fd = os.open(LOCK_DIR_NAME, flags, dir_fd=parent_fd)
            except OSError:
                # The new name cannot be proven safe to remove without a
                # descriptor, so it is left for the next run to classify.
                raise NativeLockError("lock_create_failed") from None
            try:
                opened = _fstat(dir_fd)
            except OSError:
                raise NativeLockError("lock_create_failed") from None
            dir_identity = (opened.st_dev, opened.st_ino)
            try:
                private_dir = is_private_descriptor_strict(dir_fd)
            except OSError:
                raise NativeLockError("lock_create_failed") from None
            if not stat.S_ISDIR(opened.st_mode) or not private_dir:
                raise NativeLockError("lock_create_failed")
            try:
                named = os.stat(
                    LOCK_DIR_NAME, dir_fd=parent_fd, follow_symlinks=False
                )
            except OSError:
                raise NativeLockError("lock_unsafe") from None
            if (named.st_dev, named.st_ino) != dir_identity:
                raise NativeLockError("lock_unsafe")
            try:
                if os.listdir(dir_fd):
                    raise NativeLockError("lock_unsafe")
            except OSError:
                raise NativeLockError("lock_unsafe") from None
            cleanup_ok = True

            record_bytes = _lock_record_bytes(canonical)
            record_flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                record_fd = os.open(
                    LOCK_RECORD_NAME, record_flags, 0o600, dir_fd=dir_fd
                )
            except OSError:
                raise NativeLockError("lock_record_failed") from None
            try:
                try:
                    opened_record = _fstat(record_fd)
                except OSError:
                    raise NativeLockError("lock_record_failed") from None
                record_identity = (opened_record.st_dev, opened_record.st_ino)
                try:
                    private_record = is_private_descriptor_strict(record_fd)
                except OSError:
                    cleanup_ok = False
                    raise NativeLockError("lock_record_failed") from None
                if not private_record:
                    cleanup_ok = False
                    raise NativeLockError("lock_record_failed")
                try:
                    _write_record(record_fd, record_bytes)
                except OSError:
                    raise NativeLockError("lock_record_failed") from None
                try:
                    opened_record = _fstat(record_fd)
                except OSError:
                    raise NativeLockError("lock_record_failed") from None
                if (
                    not stat.S_ISREG(opened_record.st_mode)
                    or opened_record.st_nlink != 1
                    or opened_record.st_size != len(record_bytes)
                ):
                    cleanup_ok = False
                    raise NativeLockError("lock_record_failed")
            finally:
                if record_fd is not None:
                    try:
                        os.close(record_fd)
                    except OSError:
                        pass
                    record_fd = None

            if dir_fd is None or record_identity is None:
                raise NativeLockError("lock_record_failed")
            lock = cls(
                state_dir=state_dir,
                run_id=canonical,
                parent_fd=parent_fd,
                dir_fd=dir_fd,
                dir_identity=dir_identity,
                record_identity=record_identity,
                record_bytes=record_bytes,
            )
            keep_open = True
            return lock
        except BaseException as original:
            if cleanup_ok:
                report = LockCleanupReport(False, False)
                if dir_fd is not None and dir_identity is not None:
                    try:
                        report = _remove_new_lock_best_effort(
                            parent_fd, dir_fd, dir_identity, record_identity
                        )
                    except BaseException:
                        report = LockCleanupReport(False, False)
                if not report.directory_removed:
                    # The original failure and the failed cleanup are both
                    # surfaced; neither is hidden behind the other.
                    raise NativeLockError(
                        "lock_cleanup_refused",
                        original_reason=(
                            original.reason
                            if isinstance(original, NativeLockError)
                            else None
                        ),
                        cleanup=report,
                    ) from original
            raise
        finally:
            if record_fd is not None:
                try:
                    os.close(record_fd)
                except OSError:
                    pass
            if not keep_open:
                if dir_fd is not None:
                    try:
                        os.close(dir_fd)
                    except OSError:
                        pass
                try:
                    os.close(parent_fd)
                except OSError:
                    pass

    @property
    def path(self) -> Path:
        return self._state_dir / LOCK_DIR_NAME

    @property
    def record_path(self) -> Path:
        return self.path / LOCK_RECORD_NAME

    @property
    def run_id(self) -> str:
        return self._run_id

    def release(self) -> None:
        """Remove this run's own lock, or refuse and leave it untouched."""
        with self._release_guard:
            if self._released:
                return
            if self._cleanup_failed:
                # A previous attempt already refused. Do not turn that into a
                # silent success, and do not reuse descriptors that were closed.
                raise NativeLockError(
                    "lock_cleanup_refused", cleanup=self._cleanup_report
                )
            if self._releasing:
                # A signal handler or callback re-entered release on the same
                # thread while the first attempt owned the descriptors.
                raise NativeLockError("lock_reentrant_release")
            self._releasing = True
            parent_fd = self._parent_fd
            dir_fd = self._dir_fd
            self._parent_fd = -1
            self._dir_fd = -1
            try:
                self._release_checked(parent_fd, dir_fd)
            except BaseException as failure:
                self._cleanup_failed = True
                if isinstance(failure, NativeLockError):
                    self._cleanup_report = failure.cleanup
                raise
            else:
                self._released = True
            finally:
                self._releasing = False
                for descriptor in (dir_fd, parent_fd):
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass

    def _release_checked(self, parent_fd: int, dir_fd: int) -> None:
        record_removed = False
        directory_removed = False

        def refuse() -> NoReturn:
            raise NativeLockError(
                "lock_cleanup_refused",
                cleanup=LockCleanupReport(
                    record_removed, directory_removed
                ),
            ) from None

        try:
            named = os.stat(LOCK_DIR_NAME, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            refuse()
        if (named.st_dev, named.st_ino) != self._dir_identity:
            refuse()
        if not stat.S_ISDIR(named.st_mode):
            refuse()
        try:
            if not is_private_descriptor_strict(dir_fd):
                refuse()
        except OSError:
            refuse()
        try:
            names = sorted(os.listdir(dir_fd))
        except OSError:
            refuse()
        if names != [LOCK_RECORD_NAME]:
            refuse()
        try:
            entry = os.stat(
                LOCK_RECORD_NAME, dir_fd=dir_fd, follow_symlinks=False
            )
        except OSError:
            refuse()
        if (entry.st_dev, entry.st_ino) != self._record_identity:
            refuse()
        if not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
            refuse()
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(LOCK_RECORD_NAME, flags, dir_fd=dir_fd)
        except OSError:
            refuse()
        try:
            try:
                opened = _fstat(descriptor)
            except OSError:
                refuse()
            if (opened.st_dev, opened.st_ino) != self._record_identity:
                refuse()
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                refuse()
            try:
                if not is_private_descriptor_strict(descriptor):
                    refuse()
            except OSError:
                refuse()
            if opened.st_size != len(self._record_bytes):
                refuse()
            data = _read_bounded_fd(descriptor, MAX_LOCK_RECORD_BYTES)
            if data != self._record_bytes:
                refuse()
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.unlink(LOCK_RECORD_NAME, dir_fd=dir_fd)
        except OSError:
            refuse()
        record_removed = True
        try:
            named = os.stat(LOCK_DIR_NAME, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            refuse()
        if (named.st_dev, named.st_ino) != self._dir_identity:
            refuse()
        if not stat.S_ISDIR(named.st_mode):
            refuse()
        try:
            if os.listdir(dir_fd):
                refuse()
        except OSError:
            refuse()
        try:
            os.rmdir(LOCK_DIR_NAME, dir_fd=parent_fd)
        except OSError:
            refuse()
        directory_removed = True

    def __enter__(self) -> NativeRunLock:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            self.release()
        except NativeLockError as cleanup_error:
            if exc is not None:
                # Surface the cleanup refusal and keep the body's exception
                # visible as its cause instead of suppressing either one.
                raise cleanup_error from exc
            raise
