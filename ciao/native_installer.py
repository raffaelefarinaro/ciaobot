"""The native installer's bounded NDJSON channel and fresh-host step (#2340).

The Mac client that runs the native installer needs deterministic progress
without parsing stdout/stderr and without ever receiving a credential. The
contract is one compact JSON object per line on a writable descriptor the
client passes in (``--events-fd``); everything else the installer prints stays
diagnostics. The event writer is only that wire contract: the writer, its fixed
vocabulary, its bounds and its failure semantics. It starts no process, reads
no file, touches no service and writes no workspace.

The same module owns the run-bound native-install lock and, since #2340, the
native-only :func:`create_fresh_host` helper. That helper authenticates the
signed manifest and archive through the existing release and host APIs, requires
the caller's held :class:`NativeRunLock`, and writes only the existing
owner-only host ownership record. It starts no service and writes no onboarding
receipt. The native coordinator is its production caller.

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
and refuses a read-only or closed one. It also refuses (``stdio_alias``) a
descriptor that is the same underlying file (device and inode) as stdout or
stderr, so events and
diagnostics never share one stream. ``after_phase`` pre-sets the last phase
already announced by the shell, so the writer refuses to re-emit it. It accepts
any writable descriptor the platform can write to, including pipes and sockets;
it never requires a regular file. The native path is macOS-only: on Windows the constructor refuses
rather than pretend a descriptor protocol the legacy installer does not
implement.

The run-bound lock lives in :mod:`ciao.native_lock`.

The module also contains the native coordinator lifecycle (#2340).
``python -I -m ciao.native_installer run`` (:func:`main`) is its only entry
point, started by ``install.sh --native-install``. The coordinator owns the run
lock, both canonical classifications, the host install and the engine install
(``uv tool install`` plus the one install receipt). It runs before any of those
mutations, from a private venv the shell builds out of the verified wheel.
:func:`run_native_install` validates the request and the shell's verified
artifacts, then builds the real steps with :func:`_production_deps` and runs
``_run_native_install``. That private seam takes every step as a dependency, so
the order and the cleanup rules can also be driven with fakes and real
short-lived child processes. The setup child records schema 2 through its
explicit :class:`NativeBootstrapContext`, and the coordinator starts it with the
hidden ``--installer-native-fd`` command from :func:`_wizard_argv`. The
coordinator reaches ``service_ready`` only through the production readiness
proof :func:`ciao.native_readiness.prove_native_readiness`: a ``ready`` answer
for the handoff port, reached after clean child and lock cleanup. The
lifecycle enforces the fresh-only preflight and the run lock before the engine
install, creates the host through the real fresh-host helper, rechecks the lock
and host immediately before each mutation, never passes the public event descriptor to a child, and terminates
and reaps only the child it started before releasing the lock. Cancellation is
a flag the caller sets (a signal handler may only set it); the normal loop emits
``failed("cancelled")``. An activated service is never stopped, reloaded,
quiesced or rolled back: a later failure or cancellation preserves it and the
partial artifacts. ``SIGKILL`` runs no cleanup, so a held lock and a child are
left as evidence. The coordinator never writes an onboarding or install-success
receipt and adds no route.

The bootstrap child side is :class:`NativeBootstrapContext` plus
:func:`notify_native_bind`. ``capture`` binds the child to the already-created
host's verified ownership snapshot, the private scratch chain, the absent live
definition, handoff result and launchd job, and the bootstrap config/runtime
directory identities. The context is threaded through ``create_app`` to the
existing loopback-only, bootstrap-mode-only ``/api/setup/finish`` (no session
cookie; see docs/REMOTE_BOUNDARY.md); ``notify_native_bind`` makes the private
descriptor non-inheritable and writes one bounded record only when the server
reports its bind. Neither is a public entry point: the hidden CLI handover is
refused without installer bootstrap and run id, and the legacy schema-1 path
passes neither. The child cannot prove the host record belongs to this run; the
parent's :class:`FreshHostInstall` and activation remain the run binding, and
the same-UID check-to-write races remain evidence checks, not proof.
"""

from __future__ import annotations

import argparse
import errno
import json
import math
import os
import re
import select
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, NoReturn

from ciao import __version__ as _PACKAGE_VERSION
from ciao import install_receipt, native_handoff, native_lock, native_readiness
from ciao.install_discovery import NativeInstallState, classify_native_install
from ciao.native_handoff import NativeHandoffError, NativeHandoffResult
from ciao.native_lock import NativeLockError, NativeRunLock, NativeStateDir
from ciao.native_readiness import (
    NativeReadinessPolicy,
    NativeReadinessProof,
    NativeReadinessRequest,
    make_native_readiness_deps,
    prove_native_readiness,
)
from ciao.release_manifest import (
    RELEASE_PUBLIC_KEY,
    select_server_host_artifact,
    verify_manifest,
)
from ciao.server_host import (
    APP_NAME,
    INVALID_OWNERSHIP,
    NOT_OWNED,
    SIGNATURE_REJECTED,
    UNSUPPORTED_PLATFORM,
    UNSUPPORTED_SCHEMA,
    HostOwnership,
    Runner,
    ServerHostError,
    default_bundle_path,
    default_ownership_path,
    verify_owned_host,
)
from ciao.server_host_install import (
    ARCHIVE_MISMATCH,
    ARCHIVE_UNSAFE,
    HOST_EXISTS,
    INSTALL_FAILED,
    POLICY_REFUSED,
    TARGET_UNSAFE,
    TRANSACTION_BUSY,
    TRANSACTION_CONFLICT,
    ServerHostInstallError,
    install_server_host,
    upgrade_journal_path,
    verify_host_archive,
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
#: Test seam for the owned-host check the native bootstrap child performs.
_verify_owned_host = verify_owned_host

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
        after_phase: str | None = None,
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
        last_phase = -1 if after_phase is None else _phase_index(after_phase)
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
        # Diagnostics and events must never share one stream. Refuse before any
        # flag change: a descriptor on the same underlying file (device and
        # inode) as stdout or stderr would share its O_NONBLOCK flag.
        for stdio in (1, 2):
            try:
                other = os.fstat(stdio)
            except OSError:
                continue
            if (other.st_dev, other.st_ino) == (info.st_dev, info.st_ino):
                raise EventError("stdio_alias")
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
        self._last_phase = last_phase
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


# ── the native fresh-host creation helper (#2340) ───────────────────────────


ManifestVerifier = Callable[[bytes, str, str], dict[str, Any]]
HostEntrySelector = Callable[[dict[str, Any]], dict[str, Any]]
ArchiveVerifier = Callable[[Path, Mapping[str, Any]], None]
HostInstaller = Callable[..., HostOwnership]
OwnedHostVerifier = Callable[..., HostOwnership]


@dataclass(frozen=True, slots=True)
class FreshHostInstall:
    """One host this native run proved it created, and where it lives.

    A value is returned only after :func:`ciao.server_host.verify_owned_host`
    accepted the installed bundle against the record this run wrote. The run id
    ties the result to the caller's held :class:`NativeRunLock`; the two paths are
    the exact targets the install used. Nothing here is persisted: this is the
    in-memory result of the step, not a second install/onboarding receipt.
    """

    run_id: str
    ownership: HostOwnership
    bundle_path: Path
    ownership_path: Path


def _require_absent_path(
    path: Path,
    *,
    what: str,
    code: str,
    symlink_code: str,
) -> None:
    """Refuse any name at ``path``; an unreadable name is a refusal too.

    This is deliberately a name check only. A regular file, directory, special
    file or symlink is present, and presence is enough for a fresh install to
    refuse. A symlink gets the caller's more specific code so an unsafe target is
    not reported as an ordinary existing host. Any other OS error is fail-closed:
    "could not tell" is never "absent".
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ServerHostInstallError(
            f"the {what} at {path} cannot be examined: {exc}",
            code=INSTALL_FAILED,
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        raise ServerHostInstallError(
            f"the {what} at {path} is a symlink; a fresh install never follows "
            f"or reuses it",
            code=symlink_code,
        )
    raise ServerHostInstallError(
        f"a {what} already exists at {path}; a fresh install never reuses or "
        f"replaces it",
        code=code,
    )


def create_fresh_host(
    *,
    run_lock: NativeRunLock,
    run_id: str,
    manifest_bytes: bytes,
    signature_text: str,
    archive_path: Path,
    runner: Runner = subprocess.run,
) -> FreshHostInstall:
    """Create and prove one brand-new native server host, or refuse.

    This is the native-only fresh path. The native coordinator is its only
    production caller. It does not start, stop, reload, bootstrap or quiesce a service; it never writes a
    receipt and never calls the legacy ``main`` warning/skip path. The trust
    boundary is the caller's *manifest bytes and signature*, not an
    already-selected ``entry``: this function calls
    :func:`ciao.release_manifest.verify_manifest` and
    :func:`ciao.release_manifest.select_server_host_artifact` itself, then
    re-verifies the archive against the selected entry with
    :func:`ciao.server_host_install.verify_host_archive`. A missing or ambiguous
    host, a bad signature and a changed archive are all refusals; a wheel-only
    release is never silently treated as success.

    The host paths are fixed to :func:`ciao.server_host.default_bundle_path` and
    :func:`ciao.server_host.default_ownership_path` under the current home, and
    the release public key is :data:`ciao.release_manifest.RELEASE_PUBLIC_KEY`.
    There is deliberately no caller-supplied ownership path, bundle path, public
    key or pre-selected entry: those are the trust boundary, not tuning knobs.
    Tests exercise the internal seams in :func:`_create_fresh_host` with an
    isolated ``HOME``.

    The supplied ``run_lock`` must still be held, and its record must carry
    ``run_id``. The helper checks that again immediately before and after the
    install. It then requires the bundle, ownership record and upgrade journal all
    to be absent, and calls ``install_server_host(..., require_absent=True,
    trusted_root=Path.home())`` under the host upgrade lock. That second line of
    defence is what refuses a same-policy host inserted after this helper's own
    preflight: the installer never returns a ``_CURRENT`` host in fresh mode.

    After the install the helper re-runs ``verify_owned_host`` and requires the
    result to equal the snapshot the installer returned. A changed bundle or
    record is refused with :data:`ciao.server_host.NOT_OWNED`; the helper does not
    delete the changed object, because a concurrent writer may own it and this
    path preserves pre-existing and partially created state rather than rolling
    it back.

    A same-UID writer can still race between a check and the mutation it guards;
    those checks are refusal points, not proof. In the narrow lost-rename window
    the installer may leave the record it wrote while the run still refuses; see
    ``install_server_host``.
    """
    return _create_fresh_host(
        run_lock=run_lock,
        run_id=run_id,
        manifest_bytes=manifest_bytes,
        signature_text=signature_text,
        archive_path=archive_path,
        runner=runner,
        verify_manifest_fn=verify_manifest,
        select_entry_fn=select_server_host_artifact,
        verify_archive_fn=verify_host_archive,
        install_fn=install_server_host,
        verify_owned_fn=verify_owned_host,
    )


def _create_fresh_host(
    *,
    run_lock: NativeRunLock,
    run_id: str,
    manifest_bytes: bytes,
    signature_text: str,
    archive_path: Path,
    runner: Runner,
    verify_manifest_fn: ManifestVerifier,
    select_entry_fn: HostEntrySelector,
    verify_archive_fn: ArchiveVerifier,
    install_fn: HostInstaller,
    verify_owned_fn: OwnedHostVerifier,
) -> FreshHostInstall:
    """The private implementation; injection seams are for tests only."""
    if not isinstance(run_lock, NativeRunLock):
        raise NativeLockError("lock_not_held")
    run_lock.assert_held(run_id=run_id)
    if sys.platform != "darwin":
        raise ServerHostInstallError(
            "the native server host install is macOS-only", code=UNSUPPORTED_PLATFORM
        )

    manifest: dict[str, Any] = verify_manifest_fn(
        manifest_bytes, signature_text, RELEASE_PUBLIC_KEY
    )
    entry: dict[str, Any] = select_entry_fn(manifest)
    archive = Path(archive_path)
    verify_archive_fn(archive, entry)

    bundle = default_bundle_path()
    record = default_ownership_path()
    if not bundle.is_absolute():
        raise ServerHostInstallError(
            f"the host bundle path must be absolute: {bundle}", code=INSTALL_FAILED
        )
    if bundle.name != APP_NAME:
        raise ServerHostInstallError(
            f"the host bundle must be named {APP_NAME!r}: {bundle}",
            code=INSTALL_FAILED,
        )
    if not record.is_absolute():
        raise ServerHostInstallError(
            f"the ownership record path must be absolute: {record}",
            code=INSTALL_FAILED,
        )
    if Path(os.path.realpath(record)).is_relative_to(os.path.realpath(bundle)):
        raise ServerHostInstallError(
            "the ownership record must live outside the host bundle it describes",
            code=INSTALL_FAILED,
        )

    # Read-only preflight. The installer repeats these checks under the host
    # upgrade lock; this pass refuses before any expensive staging work and makes
    # the helper's own contract explicit. A journal is refused even when it is
    # terminal: this run never settles another operation's evidence.
    _require_absent_path(
        bundle,
        what="host bundle",
        code=HOST_EXISTS,
        symlink_code=TARGET_UNSAFE,
    )
    _require_absent_path(
        record,
        what="ownership record",
        code=HOST_EXISTS,
        symlink_code=HOST_EXISTS,
    )
    _require_absent_path(
        upgrade_journal_path(),
        what="host upgrade journal",
        code=TRANSACTION_CONFLICT,
        symlink_code=TRANSACTION_CONFLICT,
    )

    # The lock can have been released or substituted while the manifest and
    # archive were being read. Prove it again at the last read-only point.
    run_lock.assert_held(run_id=run_id)

    installed: HostOwnership = install_fn(
        archive,
        entry,
        bundle_path=bundle,
        ownership_path=record,
        runner=runner,
        require_absent=True,
        trusted_root=Path.home(),
    )
    # The run lock must also cover the mutation. A caller that released or lost
    # it while the host was being written gets a refusal; the host is left for
    # the next freshness preflight rather than reported as this run's success.
    run_lock.assert_held(run_id=run_id)
    verified: HostOwnership = verify_owned_fn(
        bundle, ownership_path=record, runner=runner
    )
    if verified != installed:
        raise ServerHostInstallError(
            "the freshly installed host no longer matches the snapshot this run "
            "returned; it is left in place for inspection",
            code=NOT_OWNED,
        )
    return FreshHostInstall(
        run_id=run_id,
        ownership=verified,
        bundle_path=Path(verified.bundle_path),
        ownership_path=record,
    )


# ── the native coordinator lifecycle skeleton (#2340) ────────────────────────

#: The highest descriptor the public channel may use. Both the shell and the
#: coordinator validate the POSIX-shell 3–9 range.
MAX_EVENT_FD: Final[int] = 9
#: One private bind notification: compact JSON plus newline and nothing else.
MAX_BIND_NOTIFICATION_BYTES: Final[int] = 128
#: The coordinator's own run-budget target. Injected synchronous steps can
#: overrun it and must bound themselves; child waits and pipe reads are clamped.
COORDINATOR_RUN_TIMEOUT_SECONDS: Final[float] = 3600.0
#: How long the wizard may take to report its successful bind.
COORDINATOR_BIND_TIMEOUT_SECONDS: Final[float] = 120.0
#: How long the wizard may keep running after it bound (user interaction).
COORDINATOR_WIZARD_EXIT_TIMEOUT_SECONDS: Final[float] = 1800.0
#: SIGTERM grace before an owned child is killed.
COORDINATOR_TERM_GRACE_SECONDS: Final[float] = 5.0
#: How long reaping may take after SIGKILL.
COORDINATOR_REAP_TIMEOUT_SECONDS: Final[float] = 5.0
#: Poll slice for child waits and the private bind pipe.
COORDINATOR_POLL_INTERVAL_SECONDS: Final[float] = 0.05
#: Hard ceiling for every configurable coordinator deadline.
COORDINATOR_MAX_TIMEOUT_SECONDS: Final[float] = 7200.0
#: The last phase the shell announces before handing the descriptor over. The
#: coordinator's writer refuses it and every earlier phase.
SHELL_LAST_PHASE: Final[str] = "download"
#: How long the coordinator may wait for a live readiness proof after activation.
COORDINATOR_READINESS_TIMEOUT_SECONDS: Final[float] = 60.0
#: Pause between readiness proofs that answered with a transient failure.
COORDINATOR_READINESS_INTERVAL_SECONDS: Final[float] = 1.0
#: Readiness answers that may be retried. Every other failure is final.
_READINESS_RETRYABLE: Final[frozenset[str]] = frozenset(
    {"listener_missing", "startup_status_unavailable", "startup_not_ready"}
)
#: The most events this coordinator can write on its longest path: four phases
#: (install, wizard, activation, readiness), ``wizard_ready``, ``setup_saved``
#: and one terminal event. The run must have room for all of them or it could
#: reach a mutation with no way to report an outcome.
COORDINATOR_EVENT_BUDGET: Final[int] = 7

_STATE_FAILURES: Final[dict[NativeInstallState, str]] = {
    NativeInstallState.configured: "existing_install",
    NativeInstallState.partial: "partial_install",
    NativeInstallState.unknown: "unknown_state",
}
_LOCK_IN_PROGRESS: Final[frozenset[str]] = frozenset(
    {
        "lock_busy",
        "lock_unknown",
        "lock_changed",
        "lock_not_held",
        "lock_run_mismatch",
    }
)
_LOCK_UNKNOWN: Final[frozenset[str]] = frozenset(
    {"lock_unsafe", "state_dir_unsafe"}
)
_HOST_IN_PROGRESS: Final[frozenset[str]] = frozenset({TRANSACTION_BUSY})
_HOST_VERIFICATION: Final[frozenset[str]] = frozenset(
    {
        ARCHIVE_MISMATCH,
        ARCHIVE_UNSAFE,
        INVALID_OWNERSHIP,
        NOT_OWNED,
        POLICY_REFUSED,
        SIGNATURE_REJECTED,
        UNSUPPORTED_SCHEMA,
    }
)
# Windows has no SIGKILL/getpgid/killpg. The native coordinator refuses before
# it spawns anything there; these aliases keep the module importable and make
# the POSIX-only child cleanup fail closed if a caller reaches it.
_SIGKILL: Final[int | None] = getattr(signal, "SIGKILL", None)
_GETPGID: Final[Callable[[int], int] | None] = getattr(os, "getpgid", None)
_KILLPG: Final[Callable[[int, int], None] | None] = getattr(os, "killpg", None)


class NativeCoordinatorError(RuntimeError):
    """A coordinator refusal with a fixed reason and a public failure code."""

    def __init__(self, reason: str, failure_code: str) -> None:
        if failure_code not in FAILURE_CODES:
            raise ValueError("unknown native coordinator failure code")
        super().__init__(reason)
        self.reason = reason
        self.failure_code = failure_code


class NativeCancellation:
    """The only cancellation primitive the coordinator reads.

    A signal handler may call :meth:`cancel` (a plain assignment) and return; it
    must never call the event writer, the lock or a child. The blocking waits
    this module owns (child waits and the private bind pipe) are bounded polls
    that observe this flag, so cancellation is acted on from the normal loop.
    Injected synchronous steps cannot be preempted and must bound themselves.
    """

    __slots__ = ("_cancelled",)

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled


@dataclass(frozen=True, slots=True)
class NativeInstallRequest:
    """The validated public inputs for one native install run.

    ``events_fd`` is the public inherited non-stdio descriptor (the shell's
    public range is 3–9), ``first_seq`` is the next sequence number after the
    shell's events, and ``prior_events`` is how many events the shell already
    wrote. The two are separate because ``seq`` may jump.

    ``workspace`` is only a classifier suggestion: the wizard may legitimately
    save a different workspace, so it is never compared with the handoff. Use
    ``expected_workspace`` only when the caller has already fixed the choice;
    then the handoff must name that same directory. The independently selected
    workspace is reported in :attr:`NativeInstallOutcome.selected_workspace`.
    """

    run_id: str
    events_fd: int
    first_seq: int
    prior_events: int
    workspace: Path | None = None
    expected_workspace: Path | None = None


@dataclass(frozen=True, slots=True)
class NativeInstallOutcome:
    """What the coordinator attempted and completed, without logs or strings.

    The ``*_attempted`` flags are set before the corresponding effect starts, so
    a step that mutates and then raises is never reported as "nothing happened".
    The ``*_completed`` flags are set only after the effect returned successfully.
    ``service_may_be_running`` is true from the moment activation is attempted.
    """

    run_id: str
    terminal: str | None
    failure_code: str | None
    host_step_attempted: bool
    host_step_completed: bool
    activation_attempted: bool
    activation_completed: bool
    selected_workspace: Path | None
    lock_retained: bool

    @property
    def service_may_be_running(self) -> bool:
        return self.activation_attempted

    @property
    def exit_code(self) -> int:
        return 0 if self.terminal == "service_ready" else 1


#: The coordinator protocol the shell probes before it hands over the descriptor.
NATIVE_COORDINATOR_PROTOCOL: Final[int] = 1
#: The engine Python the tool environment is built with. It must equal
#: ``PYTHON_VERSION`` in ``scripts/install-engine.sh``; a test pins that.
ENGINE_PYTHON_VERSION: Final[str] = "3.13"
#: The longest the tool install may run, clamped to the remaining run budget.
ENGINE_INSTALL_TIMEOUT_SECONDS: Final[float] = 900.0
#: The longest a ``uv tool dir`` answer may take.
TOOL_DIR_TIMEOUT_SECONDS: Final[float] = 30.0
#: The longest the installed interpreter may take to report its version.
ENGINE_PROBE_TIMEOUT_SECONDS: Final[float] = 60.0
_VERSION_TEXT_RE: Final[re.Pattern[str]] = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+-]*")


@dataclass(frozen=True, slots=True)
class NativeInstallArtifacts:
    """The shell's verified artifacts for one run, by absolute path.

    ``uv`` is the shell's digest-pinned uv, never a PATH uv. ``version`` must
    equal the running package's version; the coordinator refuses any other
    value before it classifies or mutates anything.
    """

    version: str
    uv: Path
    wheel: Path
    manifest: Path
    signature: Path
    host_archive: Path


class _Refusal(Exception):
    def __init__(self, code: str) -> None:
        if code not in FAILURE_CODES:
            raise AssertionError("unknown coordinator failure code")
        super().__init__(code)
        self.code = code


class _Cancelled(Exception):
    pass


class _TimedOut(Exception):
    pass


class _ChildCleanupFailed(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _Deadline:
    expires_at: float

    @classmethod
    def after(cls, seconds: float) -> _Deadline:
        return cls(time.monotonic() + seconds)

    def remaining(self) -> float:
        return self.expires_at - time.monotonic()


def _sub_deadline(run_deadline: _Deadline, seconds: float) -> _Deadline:
    """A child-wait deadline clamped to what is left of the whole run.

    Cleanup uses its own separately bounded grace/reap deadlines; those must not
    be shortened by an already-expired run budget, because a lock may only be
    released after the owned child is definitely reaped.
    """
    return _Deadline(min(run_deadline.expires_at, time.monotonic() + seconds))


@dataclass(frozen=True, slots=True)
class _CoordinatorPolicy:
    """Finite, bounded deadlines and child-cleanup allowances for one run.

    Only the coordinator's own child waits, private-pipe reads and cleanup are
    enforced here. Injected synchronous steps (classify, state/lock preparation,
    host install, engine install, handoff read and activation) cannot be
    preempted by a library; the coordinator checks the deadline before and after
    each one and the real implementations must bound themselves. Cleanup gets
    its own grace and reap deadlines even after the whole-run budget is spent.
    """

    run_timeout: float = COORDINATOR_RUN_TIMEOUT_SECONDS
    bind_timeout: float = COORDINATOR_BIND_TIMEOUT_SECONDS
    wizard_exit_timeout: float = COORDINATOR_WIZARD_EXIT_TIMEOUT_SECONDS
    term_grace: float = COORDINATOR_TERM_GRACE_SECONDS
    reap_timeout: float = COORDINATOR_REAP_TIMEOUT_SECONDS
    poll_interval: float = COORDINATOR_POLL_INTERVAL_SECONDS
    readiness_timeout: float = COORDINATOR_READINESS_TIMEOUT_SECONDS
    readiness_interval: float = COORDINATOR_READINESS_INTERVAL_SECONDS

    def __post_init__(self) -> None:
        for name in (
            "run_timeout",
            "bind_timeout",
            "wizard_exit_timeout",
            "term_grace",
            "reap_timeout",
            "poll_interval",
            "readiness_timeout",
            "readiness_interval",
        ):
            value = getattr(self, name)
            if type(value) not in (int, float):
                raise NativeCoordinatorError("invalid_timeout", "internal_error")
            number = float(value)
            if (
                not math.isfinite(number)
                or number <= 0
                or number > COORDINATOR_MAX_TIMEOUT_SECONDS
            ):
                raise NativeCoordinatorError("invalid_timeout", "internal_error")


@dataclass(frozen=True, slots=True)
class _CoordinatorDeps:
    """Every external effect, injected by tests and bound to the real steps by
    :func:`_production_deps` for the public entry point.

    The public entry point exposes none of this. Production binds the artifact
    check, the real classifier, the private state/lock helpers, the real
    :func:`create_fresh_host` step, the verified-uv engine install, the native
    bootstrap command, :func:`ciao.native_handoff.activate`, the listener lookup
    that proves the wizard child holds the reported port, and the readiness
    proof :func:`ciao.native_readiness.prove_native_readiness`.
    """

    validate_inputs: Callable[[], None]
    classify: Callable[[Path | None], NativeInstallState]
    prepare_state_dir: Callable[[], NativeStateDir]
    acquire_lock: Callable[[NativeStateDir, str], NativeRunLock]
    prepare_scratch: Callable[[], Path]
    create_host: Callable[[NativeRunLock, str, float], FreshHostInstall]
    recheck_host: Callable[[FreshHostInstall], None]
    install_engine: Callable[[float], None]
    wizard_command: Callable[[int], tuple[str, ...]]
    read_handoff: Callable[[str], NativeHandoffResult]
    activate: Callable[[str, float], NativeHandoffResult]
    #: ``(port, child_pid) -> frozenset`` of every pid holding a listening socket
    #: on ``port``. The lookup must cover every listening socket on that port,
    #: IPv4 and IPv6, so a foreign same-UID listener (``SO_REUSEPORT`` or a more
    #: specific bind) is returned too and fails the owned-listener check.
    wizard_listener: Callable[[int, int], frozenset[int]]
    prove_ready: Callable[
        [FreshHostInstall, NativeHandoffResult, float], NativeReadinessProof
    ]


class _OwnedChild:
    """A child this run started in its own session, and no other process.

    Cancellation sends SIGTERM to the child's own process group, falls back to
    SIGKILL after the configured grace, and waits (and therefore reaps) before
    the coordinator releases the run lock. If the group can no longer be proven
    to be this child's, only the child pid is signalled; a pid cannot be reused
    while it is unreaped. If reaping cannot be proven, the lock is retained and
    the run refuses instead of claiming cleanup.
    """

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        term_grace: float,
        reap_timeout: float,
        poll_interval: float,
    ) -> None:
        self._process = process
        self._term_grace = term_grace
        self._reap_timeout = reap_timeout
        self._poll_interval = poll_interval

    @property
    def pid(self) -> int:
        return self._process.pid

    def poll(self) -> int | None:
        return self._process.poll()

    def owns_process(self, pid: int) -> bool:
        """Whether ``pid`` is this child or in the process group it leads."""
        if type(pid) is not int or pid <= 0 or self._process.poll() is not None:
            return False
        if pid == self._process.pid:
            return True
        if _GETPGID is None:
            return False
        try:
            return _GETPGID(pid) == self._process.pid
        except OSError:
            return False

    def wait_for_exit(
        self, deadline: _Deadline, cancellation: NativeCancellation
    ) -> int:
        while True:
            code = self._process.poll()
            if code is not None:
                return code
            if cancellation.cancelled:
                raise _Cancelled
            remaining = deadline.remaining()
            if remaining <= 0:
                raise _TimedOut
            try:
                return self._process.wait(
                    timeout=min(remaining, self._poll_interval)
                )
            except subprocess.TimeoutExpired:
                continue

    def terminate_and_reap(self) -> None:
        if self._process.poll() is not None:
            return
        self._signal_owned(signal.SIGTERM)
        if self._wait_quiet(self._term_grace):
            return
        if _SIGKILL is None:
            raise _ChildCleanupFailed
        self._signal_owned(_SIGKILL)
        if self._wait_quiet(self._reap_timeout):
            return
        raise _ChildCleanupFailed

    def _signal_owned(self, signum: int) -> None:
        if _GETPGID is None or _KILLPG is None:
            raise _ChildCleanupFailed
        pid = self._process.pid
        try:
            group = _GETPGID(pid)
        except ProcessLookupError:
            return
        except OSError:
            raise _ChildCleanupFailed from None
        try:
            if group == pid:
                _KILLPG(pid, signum)
            else:
                # The child left the session this coordinator created. The pid
                # is still unreaped and therefore still this child's; only the
                # child is signalled, never an unrelated group.
                self._process.send_signal(signum)
        except ProcessLookupError:
            return
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return
            raise _ChildCleanupFailed from None

    def _wait_quiet(self, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while True:
            if self._process.poll() is not None:
                return True
            remaining = end - time.monotonic()
            if remaining <= 0:
                return False
            try:
                self._process.wait(
                    timeout=min(remaining, self._poll_interval)
                )
            except subprocess.TimeoutExpired:
                continue
            return True


def _close_fd(fd: int) -> None:
    if fd < 0:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _spawn_owned_child(
    argv: tuple[str, ...],
    *,
    notification_fd: int,
    public_fd: int,
    policy: _CoordinatorPolicy,
) -> _OwnedChild:
    """Start the wizard with only the private notification descriptor.

    ``close_fds=True`` plus an explicit ``pass_fds`` is the inheritance rule:
    the public event descriptor is not in that tuple, so the child cannot see
    it. ``start_new_session`` makes the child's process group this run's, which
    is what cancellation is allowed to signal.

    ``pass_fds`` deliberately makes the private notification descriptor
    inheritable in the child. The child integration contract is: write exactly
    one bounded record, then close the descriptor (or at least set it
    non-inheritable) before spawning any descendant, and never pass it on. The
    coordinator cannot set a descriptor flag inside the child after exec; the
    real bootstrap command must do that itself.
    """
    if type(argv) is not tuple or not argv:
        raise NativeCoordinatorError("invalid_child_command", "internal_error")
    if any(type(argument) is not str or not argument for argument in argv):
        raise NativeCoordinatorError("invalid_child_command", "internal_error")
    if type(notification_fd) is not int or notification_fd < MIN_EVENT_FD:
        raise NativeCoordinatorError("invalid_child_fd", "internal_error")
    if notification_fd == public_fd:
        raise NativeCoordinatorError("invalid_child_fd", "internal_error")
    try:
        os.set_inheritable(public_fd, False)
    except OSError:
        raise NativeCoordinatorError("invalid_child_fd", "internal_error") from None
    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            pass_fds=(notification_fd,),
            close_fds=True,
            start_new_session=True,
        )
    except (OSError, ValueError):
        raise NativeCoordinatorError(
            "child_spawn_failed", "wizard_failed"
        ) from None
    return _OwnedChild(
        process,
        term_grace=policy.term_grace,
        reap_timeout=policy.reap_timeout,
        poll_interval=policy.poll_interval,
    )


def _bind_pipe() -> tuple[int, int]:
    read_fd, write_fd = os.pipe()
    try:
        os.set_inheritable(read_fd, False)
        # pass_fds makes the write end inheritable in the child; mark it here
        # as well so the intent is explicit and testable.
        os.set_inheritable(write_fd, True)
    except OSError:
        _close_fd(read_fd)
        _close_fd(write_fd)
        raise NativeCoordinatorError(
            "bind_pipe_failed", "wizard_failed"
        ) from None
    return read_fd, write_fd


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("a private bind record repeats a key")
        result[key] = value
    return result


def _parse_bind_notification(line: bytes) -> int:
    if not line or len(line) > MAX_BIND_NOTIFICATION_BYTES:
        raise NativeCoordinatorError(
            "bind_record_invalid", "wizard_failed"
        ) from None
    try:
        payload = json.loads(
            line.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise NativeCoordinatorError(
            "bind_record_invalid", "wizard_failed"
        ) from None
    if type(payload) is not dict or set(payload) != {"schema", "port"}:
        raise NativeCoordinatorError("bind_record_invalid", "wizard_failed")
    schema = payload.get("schema")
    port = payload.get("port")
    if type(schema) is not int or schema != 1:
        raise NativeCoordinatorError("bind_record_invalid", "wizard_failed")
    if type(port) is not int or not 1 <= port <= 65535:
        raise NativeCoordinatorError("bind_record_invalid", "wizard_failed")
    return port


def _read_bind_notification(
    read_fd: int,
    deadline: _Deadline,
    cancellation: NativeCancellation,
    poll_interval: float,
) -> int:
    """Read one bounded bind record and require the producer to close the pipe.

    The producer's contract is exact: write one compact
    ``{"schema":1,"port":N}`` line, then close this private descriptor (or set
    ``FD_CLOEXEC``) before spawning any descendant. The coordinator keeps
    reading until EOF, so a record followed by a delayed second record, trailing
    bytes or a descriptor left open is a refusal, not a silent success. The
    deadline is the caller's bind deadline, already clamped to the whole-run
    budget.
    """
    buffer = bytearray()
    record: bytes | None = None
    while True:
        if cancellation.cancelled:
            raise _Cancelled
        remaining = deadline.remaining()
        if remaining <= 0:
            raise _TimedOut
        try:
            readable, _, _ = select.select(
                [read_fd], [], [], min(remaining, poll_interval)
            )
        except InterruptedError:
            continue
        except (OSError, ValueError):
            raise NativeCoordinatorError(
                "bind_record_invalid", "wizard_failed"
            ) from None
        if not readable:
            continue
        limit = MAX_BIND_NOTIFICATION_BYTES + 1 - len(buffer)
        if limit <= 0:
            raise NativeCoordinatorError("bind_record_invalid", "wizard_failed")
        try:
            chunk = os.read(read_fd, limit)
        except OSError:
            raise NativeCoordinatorError(
                "bind_record_invalid", "wizard_failed"
            ) from None
        if not chunk:
            if record is None:
                # The child closed the private pipe without reporting a bind.
                raise NativeCoordinatorError(
                    "bind_record_missing", "wizard_failed"
                )
            return _parse_bind_notification(record)
        if record is not None:
            # A second record or any trailing byte after the first line.
            raise NativeCoordinatorError("bind_record_invalid", "wizard_failed")
        buffer.extend(chunk)
        newline = buffer.find(b"\n")
        if newline >= 0:
            if newline + 1 != len(buffer):
                # Extra bytes in the same read as the first line.
                raise NativeCoordinatorError(
                    "bind_record_invalid", "wizard_failed"
                )
            record = bytes(buffer[:newline])
            buffer.clear()
            # Keep reading: success requires the producer to close promptly.
            continue
        if len(buffer) > MAX_BIND_NOTIFICATION_BYTES:
            raise NativeCoordinatorError("bind_record_invalid", "wizard_failed")


# ── the native bootstrap child context (#2340) ───────────────────────────────

#: The private bind notification's own schema. It is not the public event schema
#: and not the schema-2 handoff document.
NATIVE_BIND_SCHEMA_VERSION: Final[int] = 1
#: The only bind addresses a native bootstrap may report as its loopback origin.
_NATIVE_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset(
    {"127.0.0.1", "::1", "localhost"}
)
#: The exit code a native bootstrap child uses when it cannot establish the
#: context the parent prepared. The installer parent only reads nonzero.
NATIVE_BOOTSTRAP_REFUSED_EXIT_CODE: Final[int] = 65

#: Every refusal reason this context can raise. Each is a fixed token: no path,
#: port, password, environment value or exception text is interpolated.
NATIVE_BOOTSTRAP_REASONS: Final[frozenset[str]] = frozenset(
    {
        "invalid_context",
        "invalid_port",
        "unsupported_platform",
        "unsupported_backend",
        "config_changed",
        "scratch_changed",
        "definition_changed",
        "handoff_changed",
        "service_changed",
        "host_changed",
        "workspace_changed",
        "workspace_already_configured",
        "startup_changed",
        "engine_changed",
        "already_notified",
        "bind_notification_failed",
    }
)


class NativeBootstrapError(RuntimeError):
    """A native bootstrap refusal with a fixed reason and no interpolated data."""

    def __init__(self, reason: str) -> None:
        if reason not in NATIVE_BOOTSTRAP_REASONS:
            raise ValueError("unknown native bootstrap reason")
        super().__init__(reason)
        self.reason = reason


def _bootstrap_platform() -> str:
    """``sys.platform`` through a seam a focused test can force a branch on."""
    return sys.platform


def _server_job_absent() -> bool:
    """The read-only launchd absence probe; a test can replace this seam."""
    from ciao import macos_service

    return macos_service.server_job_absent()


def _canonical_bootstrap_run_id(value: object) -> str:
    if type(value) is not str:
        raise NativeBootstrapError("invalid_context")
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise NativeBootstrapError("invalid_context") from None
    if str(parsed) != value:
        raise NativeBootstrapError("invalid_context")
    return value


def _bootstrap_absolute_path(value: object) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise NativeBootstrapError("invalid_context")
    if any(part in {".", ".."} for part in value.parts):
        raise NativeBootstrapError("invalid_context")
    return value


def _bootstrap_dir_identity(path: Path) -> tuple[int, int, int]:
    """Device, inode and mode of a real directory; replacement is a refusal."""
    try:
        status = os.lstat(path)
    except OSError:
        raise NativeBootstrapError("config_changed") from None
    if not stat.S_ISDIR(status.st_mode):
        raise NativeBootstrapError("config_changed")
    return (status.st_dev, status.st_ino, stat.S_IMODE(status.st_mode))


def _require_bootstrap_absent(path: Path, reason: str) -> None:
    """The name must not exist; a symlink, file or unreadable name refuses."""
    try:
        os.lstat(path)
    except FileNotFoundError:
        return
    except OSError:
        raise NativeBootstrapError(reason) from None
    raise NativeBootstrapError(reason)


def _descriptor_access_mode(fd: int) -> int:
    """The descriptor's access mode (``O_RDONLY``, ``O_WRONLY`` or ``O_RDWR``)."""
    if sys.platform == "win32":  # pragma: no cover - the native path is macOS
        raise NativeBootstrapError("invalid_context")
    import fcntl

    try:
        return fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE
    except OSError:
        raise NativeBootstrapError("invalid_context") from None


def notify_native_bind(fd: int, port: int) -> None:
    """Write exactly one bounded private bind record, then close ``fd``.

    The child calls this only after uvicorn has set ``Server.started`` and the
    startup tracker has finished ``BIND_PHASE``, so the record reports the
    loopback socket the server actually bound. The wire contract is one
    ``{"schema":1,"port":N}`` line of at most
    :data:`MAX_BIND_NOTIFICATION_BYTES`; the descriptor is made non-inheritable
    and closed before returning, and a second call cannot reuse it. Refusals
    are fixed reasons and never echo the descriptor number or the OS error.
    """
    if type(fd) is not int or fd < MIN_EVENT_FD:
        raise NativeBootstrapError("bind_notification_failed")
    if type(port) is not int or not 1 <= port <= 65535:
        raise NativeBootstrapError("invalid_port")
    line = (
        json.dumps(
            {"schema": NATIVE_BIND_SCHEMA_VERSION, "port": port},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    if len(line) > MAX_BIND_NOTIFICATION_BYTES:
        raise NativeBootstrapError("bind_notification_failed")
    try:
        os.set_inheritable(fd, False)
        view = memoryview(line)
        while view:
            try:
                written = os.write(fd, view)
            except InterruptedError:
                continue
            if written <= 0:
                raise NativeBootstrapError("bind_notification_failed")
            view = view[written:]
    except OSError:
        raise NativeBootstrapError("bind_notification_failed") from None
    finally:
        _close_fd(fd)


@dataclass(slots=True)
class NativeBootstrapContext:
    """The verified facts the native bootstrap child may rely on.

    This is an internal value, not a public route, environment variable or
    on-disk receipt. It is built once, after the installer parent has created
    the host and before the wizard serves, by :meth:`capture`; the only
    consumers are the bootstrap server and the existing loopback-only,
    bootstrap-mode-only ``POST /api/setup/finish`` handler (no session cookie;
    see docs/REMOTE_BOUNDARY.md). Nothing here starts, stops, reloads or
    quiesces a service, and the context is never persisted.

    The fields are snapshots, not proof. ``config_root`` and ``runtime_root``
    are bound to the device/inode/mode they had when the child started; the
    service definition and the handoff result must be absent then and again
    immediately before setup writes; ``expected_host`` is the
    :func:`ciao.server_host.verify_owned_host` snapshot at capture and is
    re-verified and compared immediately before setup writes. A same-UID writer
    can still interleave between a check and the write it guards, so these are
    refusal points that narrow the window, not an atomic guarantee. Nor can
    this child prove that the ownership record belongs to *this* installer run:
    the record carries no run id, and the parent's :class:`FreshHostInstall` is
    the only run binding. The child proves the host is this user's owned host
    and has not changed while the wizard was open; activation re-establishes it
    in the parent.
    """

    run_id: str
    startup_id: str
    expected_workspace: Path | None
    engine_python: Path
    config_root: Path
    runtime_root: Path
    bootstrap_host: str
    bootstrap_port: int
    definition_path: Path
    handoff_path: Path
    expected_host: HostOwnership
    host_ownership_path: Path
    _config_identity: tuple[int, int, int] = field(repr=False)
    _runtime_identity: tuple[int, int, int] = field(repr=False)
    _notification_fd: int = field(repr=False)
    _notification_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )
    _notified: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _canonical_bootstrap_run_id(self.run_id)
        if type(self.startup_id) is not str or not self.startup_id.strip():
            raise NativeBootstrapError("invalid_context")
        for value in (
            self.config_root,
            self.runtime_root,
            self.definition_path,
            self.handoff_path,
            self.host_ownership_path,
            self.engine_python,
        ):
            _bootstrap_absolute_path(value)
        if self.expected_workspace is not None:
            _bootstrap_absolute_path(self.expected_workspace)
        if type(self.bootstrap_host) is not str or not self.bootstrap_host:
            raise NativeBootstrapError("invalid_context")
        if self.bootstrap_host not in _NATIVE_LOOPBACK_HOSTS:
            raise NativeBootstrapError("invalid_context")
        if type(self.bootstrap_port) is not int or not 1 <= self.bootstrap_port <= 65535:
            raise NativeBootstrapError("invalid_context")
        if type(self._notification_fd) is not int or self._notification_fd < MIN_EVENT_FD:
            raise NativeBootstrapError("invalid_context")
        if (
            type(self._config_identity) is not tuple
            or len(self._config_identity) != 3
            or type(self._runtime_identity) is not tuple
            or len(self._runtime_identity) != 3
        ):
            raise NativeBootstrapError("invalid_context")
        if Path(self.expected_host.bundle_path) != default_bundle_path():
            raise NativeBootstrapError("host_changed")

    @classmethod
    def capture(
        cls,
        *,
        run_id: str,
        startup_id: str,
        notification_fd: int,
        config_root: Path,
        runtime_root: Path,
        bootstrap_host: str,
        bootstrap_port: int,
        expected_workspace: Path | None,
        engine_python: Path,
    ) -> NativeBootstrapContext:
        """Bind this child to the state the native installer parent prepared.

        The platform must be macOS, the service backend must be the native
        ``macos`` one, the private scratch chain must already be the parent's
        validated chain, the live service definition and the schema-2 handoff
        must both be absent, and the installed host must verify against its
        owner-only record. Any other answer is a fixed refusal; nothing is
        created, chmod'ed or repaired here.
        """
        if _bootstrap_platform() != "darwin":
            raise NativeBootstrapError("unsupported_platform")
        run_id = _canonical_bootstrap_run_id(run_id)
        if type(startup_id) is not str or not startup_id.strip():
            raise NativeBootstrapError("invalid_context")
        if type(notification_fd) is not int or notification_fd < MIN_EVENT_FD:
            raise NativeBootstrapError("invalid_context")
        try:
            notification_status = os.fstat(notification_fd)
        except OSError:
            raise NativeBootstrapError("invalid_context") from None
        if not (
            stat.S_ISFIFO(notification_status.st_mode)
            or stat.S_ISSOCK(notification_status.st_mode)
        ):
            raise NativeBootstrapError("invalid_context")
        # The child only writes the record, so a read-only end is no channel.
        if _descriptor_access_mode(notification_fd) not in (os.O_WRONLY, os.O_RDWR):
            raise NativeBootstrapError("invalid_context")
        try:
            # This is the child's descriptor entry. Mark it now so no provider,
            # uvicorn or other descendant started before the bind can inherit
            # the private channel; notify_native_bind sets it again and closes
            # it after the one record.
            os.set_inheritable(notification_fd, False)
        except OSError:
            raise NativeBootstrapError("invalid_context") from None
        config_root = _bootstrap_absolute_path(config_root)
        runtime_root = _bootstrap_absolute_path(runtime_root)
        engine_python = _bootstrap_absolute_path(engine_python)
        if expected_workspace is not None:
            expected_workspace = _bootstrap_absolute_path(expected_workspace)
        if type(bootstrap_host) is not str or not bootstrap_host:
            raise NativeBootstrapError("invalid_context")
        if bootstrap_host not in _NATIVE_LOOPBACK_HOSTS:
            raise NativeBootstrapError("invalid_context")
        if type(bootstrap_port) is not int or not 1 <= bootstrap_port <= 65535:
            raise NativeBootstrapError("invalid_context")

        from ciao import service_backend

        try:
            backend = service_backend.current_backend()
        except service_backend.UnsupportedPlatformError:
            raise NativeBootstrapError("unsupported_backend") from None
        if backend.name != "macos":
            raise NativeBootstrapError("unsupported_backend")
        try:
            live_directory = backend.live_agents_dir()
        except (OSError, RuntimeError, ValueError):
            raise NativeBootstrapError("unsupported_backend") from None
        from ciao import macos_service

        definition_path = live_directory.expanduser() / (
            f"{macos_service.SERVER_LABEL}.plist"
        )
        definition_path = _bootstrap_absolute_path(definition_path)

        try:
            scratch = native_handoff.require_bootstrap_scratch()
        except NativeHandoffError:
            raise NativeBootstrapError("scratch_changed") from None
        try:
            config_matches_scratch = config_root.resolve() == scratch.resolve()
        except OSError:
            raise NativeBootstrapError("scratch_changed") from None
        if not config_matches_scratch:
            raise NativeBootstrapError("scratch_changed")
        handoff_path = native_handoff.result_path()
        try:
            handoff_is_in_config = handoff_path.parent.resolve() == config_root.resolve()
        except OSError:
            raise NativeBootstrapError("handoff_changed") from None
        if not handoff_is_in_config:
            raise NativeBootstrapError("handoff_changed")
        _require_bootstrap_absent(definition_path, "definition_changed")
        _require_bootstrap_absent(handoff_path, "handoff_changed")
        try:
            if not _server_job_absent():
                raise NativeBootstrapError("service_changed")
        except NativeBootstrapError:
            raise
        except RuntimeError:
            raise NativeBootstrapError("service_changed") from None

        config_identity = _bootstrap_dir_identity(config_root)
        runtime_identity = _bootstrap_dir_identity(runtime_root)

        try:
            ownership = _verify_owned_host(
                default_bundle_path(), ownership_path=default_ownership_path()
            )
        except (ServerHostError, OSError):
            raise NativeBootstrapError("host_changed") from None
        if Path(ownership.bundle_path) != default_bundle_path():
            raise NativeBootstrapError("host_changed")
        return cls(
            run_id=run_id,
            startup_id=startup_id,
            expected_workspace=expected_workspace,
            engine_python=engine_python,
            config_root=config_root,
            runtime_root=runtime_root,
            bootstrap_host=bootstrap_host,
            bootstrap_port=bootstrap_port,
            definition_path=definition_path,
            handoff_path=handoff_path,
            expected_host=ownership,
            host_ownership_path=default_ownership_path(),
            _config_identity=config_identity,
            _runtime_identity=runtime_identity,
            _notification_fd=notification_fd,
        )

    def assert_ready_to_write(
        self,
        *,
        startup_id: str,
        workspace: Path,
        port: int,
        engine_python: str,
        config_host: str,
        config_port: int,
    ) -> Path:
        """Re-establish every snapshot immediately before setup mutates anything.

        Returns the resolved workspace. A changed bootstrap config, scratch
        chain, live definition, handoff result, engine interpreter, host
        snapshot or already-configured chosen workspace is a fixed refusal, as
        is an explicit expected workspace the request does not match. The only
        service operation is the read-only launchd absence probe; nothing is
        started, stopped, loaded, unloaded or reloaded.
        """
        if type(startup_id) is not str or startup_id != self.startup_id:
            raise NativeBootstrapError("startup_changed")
        if type(port) is not int or not 1 <= port <= 65535:
            raise NativeBootstrapError("invalid_port")
        if type(config_host) is not str or config_host != self.bootstrap_host:
            raise NativeBootstrapError("config_changed")
        if type(config_port) is not int or config_port != self.bootstrap_port:
            raise NativeBootstrapError("config_changed")
        # The installer bootstrap clears these; a later value would move the
        # definition directory or the engine interpreter away from the ones
        # this context verified. Refuse before setup can read them.
        if (
            os.environ.get("CIAO_LAUNCH_AGENTS_DIR", "").strip()
            or os.environ.get("CIAO_ENGINE_PATH", "").strip()
        ):
            raise NativeBootstrapError("config_changed")
        try:
            resolved_workspace = Path(os.path.realpath(workspace))
        except OSError:
            raise NativeBootstrapError("workspace_changed") from None
        if not resolved_workspace.is_absolute():
            raise NativeBootstrapError("workspace_changed")
        if self.expected_workspace is not None:
            try:
                expected = Path(os.path.realpath(self.expected_workspace))
            except OSError:
                raise NativeBootstrapError("workspace_changed") from None
            if resolved_workspace != expected:
                raise NativeBootstrapError("workspace_changed")
        try:
            if resolved_workspace == self.config_root.resolve():
                raise NativeBootstrapError("workspace_changed")
        except OSError:
            raise NativeBootstrapError("workspace_changed") from None

        if _bootstrap_dir_identity(self.config_root) != self._config_identity:
            raise NativeBootstrapError("config_changed")
        if _bootstrap_dir_identity(self.runtime_root) != self._runtime_identity:
            raise NativeBootstrapError("config_changed")
        try:
            native_handoff.require_bootstrap_scratch()
        except NativeHandoffError:
            raise NativeBootstrapError("scratch_changed") from None

        try:
            if native_handoff.workspace_is_set_up(resolved_workspace):
                raise NativeBootstrapError("workspace_already_configured")
        except NativeHandoffError:
            raise NativeBootstrapError("workspace_changed") from None

        _require_bootstrap_absent(self.definition_path, "definition_changed")
        _require_bootstrap_absent(self.handoff_path, "handoff_changed")
        try:
            if not _server_job_absent():
                raise NativeBootstrapError("service_changed")
        except NativeBootstrapError:
            raise
        except RuntimeError:
            raise NativeBootstrapError("service_changed") from None

        from ciao import service_backend

        try:
            backend = service_backend.current_backend()
        except service_backend.UnsupportedPlatformError:
            raise NativeBootstrapError("unsupported_backend") from None
        if backend.name != "macos":
            raise NativeBootstrapError("unsupported_backend")
        try:
            definition_parent = self.definition_path.parent.resolve()
            live_directory = backend.live_agents_dir().expanduser().resolve()
        except OSError:
            raise NativeBootstrapError("config_changed") from None
        if definition_parent != live_directory:
            raise NativeBootstrapError("config_changed")

        try:
            resolved_engine = Path(os.path.realpath(engine_python))
            expected_engine = Path(os.path.realpath(self.engine_python))
        except OSError:
            raise NativeBootstrapError("engine_changed") from None
        if resolved_engine != expected_engine:
            raise NativeBootstrapError("engine_changed")

        try:
            live = _verify_owned_host(
                default_bundle_path(),
                ownership_path=self.host_ownership_path,
            )
        except (ServerHostError, OSError):
            raise NativeBootstrapError("host_changed") from None
        if live != self.expected_host:
            raise NativeBootstrapError("host_changed")
        return resolved_workspace

    def notify_bound(self, port: int) -> None:
        """Report the bound loopback port exactly once on the private pipe."""
        with self._notification_lock:
            if self._notified:
                raise NativeBootstrapError("already_notified")
            self._notified = True
            fd = self._notification_fd
            self._notification_fd = -1
        notify_native_bind(fd, port)


def _check_stage(
    deadline: _Deadline, cancellation: NativeCancellation
) -> None:
    if cancellation.cancelled:
        raise _Cancelled
    if deadline.remaining() <= 0:
        raise _TimedOut


def _wait_observing(
    seconds: float, cancellation: NativeCancellation, poll_interval: float
) -> None:
    """Sleep for ``seconds`` in slices so a cancellation is seen promptly."""
    end = time.monotonic() + seconds
    while True:
        if cancellation.cancelled:
            raise _Cancelled
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(poll_interval, left))


def _remaining(deadline: _Deadline) -> float:
    remaining = deadline.remaining()
    if remaining <= 0:
        raise _TimedOut
    return remaining


def _validate_request(request: NativeInstallRequest) -> None:
    run_id = request.run_id
    if type(run_id) is not str:
        raise NativeCoordinatorError("invalid_run_id", "invalid_arguments")
    try:
        parsed = uuid.UUID(run_id)
    except (AttributeError, TypeError, ValueError):
        raise NativeCoordinatorError("invalid_run_id", "invalid_arguments") from None
    if str(parsed) != run_id:
        raise NativeCoordinatorError("invalid_run_id", "invalid_arguments")
    if type(request.events_fd) is not int:
        raise NativeCoordinatorError("invalid_events_fd", "invalid_arguments")
    if not MIN_EVENT_FD <= request.events_fd <= MAX_EVENT_FD:
        raise NativeCoordinatorError("invalid_events_fd", "invalid_arguments")
    if type(request.first_seq) is not int or request.first_seq < 1:
        raise NativeCoordinatorError("invalid_sequence", "invalid_arguments")
    if type(request.prior_events) is not int or request.prior_events < 0:
        raise NativeCoordinatorError("invalid_sequence", "invalid_arguments")
    if request.prior_events >= MAX_EVENTS:
        raise NativeCoordinatorError("invalid_sequence", "invalid_arguments")
    if request.prior_events > request.first_seq - 1:
        raise NativeCoordinatorError("invalid_sequence", "invalid_arguments")
    if request.prior_events + COORDINATOR_EVENT_BUDGET > MAX_EVENTS:
        raise NativeCoordinatorError("invalid_sequence", "invalid_arguments")
    for value in (request.workspace, request.expected_workspace):
        if value is None:
            continue
        if not isinstance(value, Path) or not value.is_absolute():
            raise NativeCoordinatorError("invalid_workspace", "invalid_arguments")
        if any(part in {".", ".."} for part in value.parts):
            raise NativeCoordinatorError("invalid_workspace", "invalid_arguments")


def _failure_outcome(request: NativeInstallRequest, code: str) -> NativeInstallOutcome:
    run_id = request.run_id if type(request.run_id) is str else ""
    return NativeInstallOutcome(
        run_id=run_id,
        terminal=None,
        failure_code=code,
        host_step_attempted=False,
        host_step_completed=False,
        activation_attempted=False,
        activation_completed=False,
        selected_workspace=None,
        lock_retained=False,
    )


def _map_lock_failure(exc: NativeLockError) -> str:
    if exc.reason in _LOCK_IN_PROGRESS:
        return "install_in_progress"
    if exc.reason in _LOCK_UNKNOWN:
        return "unknown_state"
    if exc.reason == "invalid_run_id":
        return "invalid_arguments"
    if exc.reason == "lock_cleanup_refused":
        return "internal_error"
    return "install_failed"


def _map_host_failure(exc: ServerHostInstallError) -> str:
    if exc.code == HOST_EXISTS:
        return "existing_install"
    if exc.code == TRANSACTION_CONFLICT:
        return "partial_install"
    if exc.code == TARGET_UNSAFE:
        return "unknown_state"
    if exc.code in _HOST_IN_PROGRESS:
        return "install_in_progress"
    if exc.code in _HOST_VERIFICATION:
        return "verification_failed"
    return "install_failed"


def _map_classification(state: NativeInstallState) -> None:
    if state is NativeInstallState.fresh:
        return
    code = _STATE_FAILURES.get(state)
    if code is None:
        raise NativeCoordinatorError("invalid_classification", "internal_error")
    raise _Refusal(code)


def _require_regular_file(path: object, *, executable: bool) -> None:
    """An absolute, non-symlink regular file the shell verified; else a fixed refusal."""
    invalid = NativeCoordinatorError("invalid_artifacts", "invalid_arguments")
    if not isinstance(path, Path) or not path.is_absolute():
        raise invalid
    if any(part in {".", ".."} for part in path.parts):
        raise invalid
    try:
        info = os.lstat(path)
    except OSError:
        raise invalid from None
    if not stat.S_ISREG(info.st_mode):
        raise invalid
    if executable and not os.access(path, os.X_OK):
        raise invalid


def _validate_artifacts(artifacts: NativeInstallArtifacts) -> None:
    """Check the shell's artifacts before the first classification or mutation.

    Paths are checked first, so a bad path is never reported as a version
    mismatch. The version must be a plain token and must equal the version of
    the package that is running, which is what the shell verified.
    """
    _require_regular_file(artifacts.uv, executable=True)
    _require_regular_file(artifacts.wheel, executable=False)
    _require_regular_file(artifacts.manifest, executable=False)
    _require_regular_file(artifacts.signature, executable=False)
    _require_regular_file(artifacts.host_archive, executable=False)
    version = artifacts.version
    if type(version) is not str or not _VERSION_TEXT_RE.fullmatch(version):
        raise NativeCoordinatorError("invalid_artifacts", "invalid_arguments")
    if version != _PACKAGE_VERSION:
        raise NativeCoordinatorError("version_mismatch", "verification_failed")


def _tool_unknown() -> NativeCoordinatorError:
    return NativeCoordinatorError("tool_dir_unknown", "unknown_state")


def _uv_dir(uv: Path, extra: tuple[str, ...]) -> Path:
    """One absolute, single-line answer from ``uv tool dir``, or a refusal."""
    try:
        completed = subprocess.run(
            [str(uv), "tool", "dir", *extra],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=TOOL_DIR_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise _tool_unknown() from None
    if completed.returncode != 0:
        raise _tool_unknown()
    text: str = completed.stdout.strip()
    if not text or any(char in text for char in "\n\r\0"):
        raise _tool_unknown()
    path = Path(text)
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise _tool_unknown()
    return path


def _tool_dirs(uv: Path) -> tuple[Path, Path]:
    """The uv tool and bin directories, asked of the verified uv and of nothing else."""
    return _uv_dir(uv, ()), _uv_dir(uv, ("--bin",))


def _engine_failed() -> NativeCoordinatorError:
    return NativeCoordinatorError("engine_install_failed", "install_failed")


def _install_engine(
    artifacts: NativeInstallArtifacts,
    tool_dir: Path,
    bin_dir: Path,
    remaining: float,
) -> None:
    """Install the verified wheel with the verified uv, then write the one receipt.

    There is no ``--force``: the classifications proved there is no tool
    environment and no shim. The public event descriptor is never passed on,
    because every subprocess here uses ``close_fds=True``. The receipt is the
    existing install-receipt schema, written and read back in process.
    """
    from ciao import macos_service

    try:
        installed = subprocess.run(
            [
                str(artifacts.uv),
                "tool",
                "install",
                "--python",
                ENGINE_PYTHON_VERSION,
                str(artifacts.wheel),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            timeout=min(remaining, ENGINE_INSTALL_TIMEOUT_SECONDS),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise _engine_failed() from None
    if installed.returncode != 0:
        raise _engine_failed()

    ciao_path = bin_dir / "ciao"
    tool_python = tool_dir / "ciaobot" / "bin" / "python"
    for path in (ciao_path, tool_python):
        if not os.path.isfile(path) or not os.access(path, os.X_OK):
            raise _engine_failed()
    try:
        probe = subprocess.run(
            [
                str(tool_python),
                "-I",
                "-c",
                "import ciao, sys; sys.stdout.write(ciao.__version__)",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=ENGINE_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise _engine_failed() from None
    if probe.returncode != 0 or probe.stdout != artifacts.version:
        raise _engine_failed()

    receipt = install_receipt.InstallReceipt(
        version=artifacts.version,
        executable=str(ciao_path),
        python=str(tool_python),
        service_backend="launchd",
        service_label=macos_service.SERVER_LABEL,
        installed_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    try:
        install_receipt.write_receipt(receipt)
        written = install_receipt.read_receipt()
    except (OSError, ValueError):
        raise _engine_failed() from None
    if (
        written is None
        or written.version != artifacts.version
        or Path(written.executable) != ciao_path
        or Path(written.python) != tool_python
    ):
        raise _engine_failed()


def _wizard_argv(
    bin_dir: Path,
    run_id: str,
    fd: int,
    workspace: Path | None,
    expected: Path | None,
) -> tuple[str, ...]:
    """The hidden native bootstrap command the coordinator starts as its child."""
    argv: tuple[str, ...] = (
        str(bin_dir / "ciao"),
        "run",
        "--installer-bootstrap",
        "--installer-run-id",
        run_id,
        f"--installer-native-fd={fd}",
    )
    if workspace is not None:
        argv += (f"--installer-suggest-workspace={workspace}",)
    if expected is not None:
        argv += (f"--installer-native-workspace={expected}",)
    return argv


def _report_lock_holder(state_path: Path) -> None:
    """Say on stderr who holds the run lock: a pid and its liveness, never a path."""
    try:
        status = native_lock.inspect_native_lock(state_path)
    except Exception:
        return
    if status.pid is None:
        print(
            "Ciaobot native installer: a native-install lock with no readable "
            "owner is present. It is never removed automatically; see "
            "'Recovering a native-install lock' in docs/DEVELOPMENT.md.",
            file=sys.stderr,
        )
        return
    liveness = {True: "running", False: "not running", None: "unknown"}[
        status.pid_alive
    ]
    print(
        "Ciaobot native installer: another native install holds the run lock "
        f"(pid {status.pid}, {liveness}). It is never removed automatically; "
        "see 'Recovering a native-install lock' in docs/DEVELOPMENT.md.",
        file=sys.stderr,
    )


def _production_deps(
    request: NativeInstallRequest,
    artifacts: NativeInstallArtifacts,
    cancellation: NativeCancellation,
) -> _CoordinatorDeps:
    """Bind every seam to its real step for one validated request and its artifacts."""
    tool_dirs: list[tuple[Path, Path]] = []

    def dirs() -> tuple[Path, Path]:
        # Resolved once per run, by the first step that needs them; a refusal
        # there is an unknown classification through the seam's classify handler.
        if not tool_dirs:
            tool_dirs.append(_tool_dirs(artifacts.uv))
        return tool_dirs[0]

    def classify(workspace: Path | None) -> NativeInstallState:
        tool_dir, bin_dir = dirs()
        return classify_native_install(
            environ=os.environ,
            cwd=Path.cwd(),
            suggestion=workspace,
            tool_dir=tool_dir,
            bin_dir=bin_dir,
        )

    def prepare_state_dir() -> NativeStateDir:
        return native_lock.prepare_native_state_dir(
            install_receipt.default_receipt_path().parent
        )

    def acquire_lock(state: NativeStateDir, run_id: str) -> NativeRunLock:
        try:
            return NativeRunLock.acquire(state, run_id=run_id)
        except NativeLockError as exc:
            if exc.reason in {"lock_busy", "lock_unknown"}:
                _report_lock_holder(state.path)
            raise

    def create_host(
        lock: NativeRunLock, run_id: str, remaining: float
    ) -> FreshHostInstall:
        return create_fresh_host(
            run_lock=lock,
            run_id=run_id,
            manifest_bytes=artifacts.manifest.read_bytes(),
            signature_text=artifacts.signature.read_text(encoding="utf-8"),
            archive_path=artifacts.host_archive,
        )

    def recheck_host(host: FreshHostInstall) -> None:
        current = verify_owned_host(
            default_bundle_path(), ownership_path=host.ownership_path
        )
        if current != host.ownership:
            raise ServerHostInstallError(
                "the installed host changed before the engine install",
                code=NOT_OWNED,
            )

    def install_engine(remaining: float) -> None:
        tool_dir, bin_dir = dirs()
        _install_engine(artifacts, tool_dir, bin_dir, remaining)

    def wizard_command(fd: int) -> tuple[str, ...]:
        _, bin_dir = dirs()
        return _wizard_argv(
            bin_dir,
            request.run_id,
            fd,
            request.workspace,
            request.expected_workspace,
        )

    def read_handoff(run_id: str) -> NativeHandoffResult:
        return native_handoff.read_result(expected_run_id=run_id)

    def activate(run_id: str, remaining: float) -> NativeHandoffResult:
        # The launchctl calls inside are self-bounded; the budget is not passed on.
        return native_handoff.activate(run_id=run_id)

    def wizard_listener(port: int, child_pid: int) -> frozenset[int]:
        # Every listening pid on the port, IPv4 and IPv6. The coordinator
        # decides ownership; the child pid is not needed here.
        return native_readiness.wizard_listener_pids(port)

    def prove_ready(
        host: FreshHostInstall, handoff: NativeHandoffResult, remaining: float
    ) -> NativeReadinessProof:
        return prove_native_readiness(
            NativeReadinessRequest(
                version=artifacts.version,
                port=handoff.port,
                workspace=handoff.workspace,
                host_bundle=host.bundle_path,
                ownership_path=host.ownership_path,
                engine_python=Path(handoff.engine_python),
                fresh_install_service_absent=True,
            ),
            deps=make_native_readiness_deps(),
            policy=NativeReadinessPolicy(
                timeout=min(remaining, native_readiness.MAX_TIMEOUT_SECONDS)
            ),
            cancelled=lambda: cancellation.cancelled,
        )

    return _CoordinatorDeps(
        validate_inputs=lambda: _validate_artifacts(artifacts),
        classify=classify,
        prepare_state_dir=prepare_state_dir,
        acquire_lock=acquire_lock,
        prepare_scratch=native_handoff.prepare_bootstrap_scratch,
        create_host=create_host,
        recheck_host=recheck_host,
        install_engine=install_engine,
        wizard_command=wizard_command,
        read_handoff=read_handoff,
        activate=activate,
        wizard_listener=wizard_listener,
        prove_ready=prove_ready,
    )


def run_native_install(
    request: NativeInstallRequest,
    artifacts: NativeInstallArtifacts,
    *,
    cancellation: NativeCancellation | None = None,
) -> NativeInstallOutcome:
    """Run one native install end to end with the real steps.

    This is the production entry point. Its only caller is :func:`main`, which
    the shell reaches through ``install.sh --native-install``. A request that
    fails :func:`_validate_request` raises :class:`NativeCoordinatorError`
    before the descriptor is used. Every other outcome, including an invalid
    artifact, is a fixed ``failed`` event on the descriptor and is returned.
    :func:`_run_native_install` is the testable lifecycle seam and is not public
    API.
    """
    _validate_request(request)
    cancellation = NativeCancellation() if cancellation is None else cancellation
    deps = _production_deps(request, artifacts, cancellation)
    return _run_native_install(request, deps=deps, cancellation=cancellation)


def _cancel_signal_numbers() -> tuple[int, ...]:
    numbers = [signal.SIGINT, signal.SIGTERM]
    if sys.platform != "win32":
        numbers.append(signal.SIGHUP)
    return tuple(numbers)


def main(argv: list[str] | None = None) -> int:
    """The internal CLI the shell runs: ``python -I -m ciao.native_installer run ...``.

    Exit 0 means ``service_ready`` was emitted; 1 means any other end; 2 means
    the handover arguments were refused before the descriptor was used. A
    signal only sets the cancellation flag, and the previous handlers are
    restored before returning.
    """
    parser = argparse.ArgumentParser(prog="python -m ciao.native_installer")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run the native install")
    run.add_argument("--run-id", required=True)
    run.add_argument("--events-fd", type=int, required=True)
    run.add_argument("--first-seq", type=int, required=True)
    run.add_argument("--prior-events", type=int, required=True)
    run.add_argument("--version", required=True)
    run.add_argument("--uv", type=Path, required=True)
    run.add_argument("--wheel", type=Path, required=True)
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--signature", type=Path, required=True)
    run.add_argument("--host-archive", type=Path, required=True)
    run.add_argument("--workspace", type=Path, default=None)
    args = parser.parse_args(argv)

    request = NativeInstallRequest(
        run_id=args.run_id,
        events_fd=args.events_fd,
        first_seq=args.first_seq,
        prior_events=args.prior_events,
        workspace=args.workspace,
    )
    artifacts = NativeInstallArtifacts(
        version=args.version,
        uv=args.uv,
        wheel=args.wheel,
        manifest=args.manifest,
        signature=args.signature,
        host_archive=args.host_archive,
    )
    cancellation = NativeCancellation()

    def on_signal(_signum: int, _frame: object) -> None:
        cancellation.cancel()

    previous: list[tuple[int, Any]] = []
    try:
        for signum in _cancel_signal_numbers():
            previous.append((signum, signal.getsignal(signum)))
            signal.signal(signum, on_signal)
        try:
            outcome = run_native_install(
                request, artifacts, cancellation=cancellation
            )
        except NativeCoordinatorError:
            print(
                "Ciaobot native installer: invalid handover arguments.",
                file=sys.stderr,
            )
            return 2
        return outcome.exit_code
    finally:
        for signum, handler in previous:
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())


def _run_native_install(
    request: NativeInstallRequest,
    *,
    deps: _CoordinatorDeps,
    cancellation: NativeCancellation | None = None,
    policy: _CoordinatorPolicy | None = None,
) -> NativeInstallOutcome:
    """Drive the coordinator lifecycle; tests inject every effect.

    The order is deliberate: classify fresh, then prepare the private state and
    take the run lock, then prepare the bootstrap scratch chain, then create the
    host through the real fresh-host helper and recheck it, then install the
    engine, then start the wizard. Every mutation is preceded by a cancellation
    and deadline check and (after acquisition) by ``assert_held``. The engine
    install cannot begin on ``configured``, ``partial``, ``unknown`` or a held
    lock. After activation the injected ``prove_ready`` is polled within
    ``readiness_timeout``: transient answers are retried every
    ``readiness_interval``, any other answer is ``service_not_ready``, and
    ``service_ready`` is emitted only for a ready proof on the handoff port
    after the child and the lock were cleaned up successfully.

    The injected synchronous steps cannot be preempted by this module: the
    deadline is checked before and after each one and the real implementation
    must bound itself. The coordinator's own child waits and private-pipe reads
    are clamped to the whole-run budget, while cleanup uses its separately
    bounded grace/reap deadlines so an expired run budget cannot prevent a
    definite reaping decision. This is not a claim that every injected step is
    interruptible. ``KeyboardInterrupt``/``SystemExit`` and ``SIGKILL`` are not
    converted into an event; the lock is left as evidence in those paths.
    """
    cancellation = NativeCancellation() if cancellation is None else cancellation
    policy = _CoordinatorPolicy() if policy is None else policy
    try:
        _validate_request(request)
    except NativeCoordinatorError as exc:
        return _failure_outcome(request, exc.failure_code)
    try:
        os.set_inheritable(request.events_fd, False)
    except OSError:
        return _failure_outcome(request, "event_channel_failed")
    try:
        writer = EventWriter(
            request.events_fd,
            run_id=request.run_id,
            first_seq=request.first_seq,
            prior_events=request.prior_events,
            after_phase=SHELL_LAST_PHASE,
        )
    except EventError:
        _close_fd(request.events_fd)
        return _failure_outcome(request, "event_channel_failed")

    lock: NativeRunLock | None = None
    child: _OwnedChild | None = None
    host_step_attempted = False
    host_step_completed = False
    activation_attempted = False
    activation_completed = False
    selected_workspace: Path | None = None
    ready_port: int | None = None
    failure: str | None = None
    channel_dead = False
    lock_retained = False

    try:
        try:
            try:
                if sys.platform != "darwin":
                    raise _Refusal("unknown_state")
                deadline = _Deadline.after(policy.run_timeout)
                _check_stage(deadline, cancellation)
                try:
                    deps.validate_inputs()
                except NativeCoordinatorError:
                    raise
                except Exception:
                    raise _Refusal("internal_error") from None
                _check_stage(deadline, cancellation)

                try:
                    classification = deps.classify(request.workspace)
                except Exception:
                    # An unreadable surface is never fresh.
                    raise _Refusal("unknown_state") from None
                _map_classification(classification)
                _check_stage(deadline, cancellation)

                state_dir = deps.prepare_state_dir()
                try:
                    lock = deps.acquire_lock(state_dir, request.run_id)
                finally:
                    state_dir.close()
                lock.assert_held(run_id=request.run_id)
                try:
                    deps.prepare_scratch()
                except NativeHandoffError:
                    raise _Refusal("unknown_state") from None
                _check_stage(deadline, cancellation)
                # A second read-only classification is the last pre-mutation
                # check for a definition, workspace, receipt or tool directory
                # that appeared while the private state and lock were created.
                # The host helper repeats its own host/journal checks; this scan
                # narrows the window and is not proof.
                lock.assert_held(run_id=request.run_id)
                try:
                    confirmation = deps.classify(request.workspace)
                except Exception:
                    raise _Refusal("unknown_state") from None
                _map_classification(confirmation)
                lock.assert_held(run_id=request.run_id)

                writer.phase("install")
                host_step_attempted = True
                try:
                    host = deps.create_host(
                        lock, request.run_id, _remaining(deadline)
                    )
                except (_Cancelled, _TimedOut):
                    raise
                except (
                    NativeCoordinatorError,
                    NativeLockError,
                    ServerHostInstallError,
                ):
                    raise
                except Exception:
                    raise _Refusal("install_failed") from None
                host_step_completed = True
                try:
                    deps.recheck_host(host)
                except (_Cancelled, _TimedOut):
                    raise
                except (
                    NativeCoordinatorError,
                    NativeLockError,
                    ServerHostInstallError,
                ):
                    raise
                except Exception:
                    raise _Refusal("install_failed") from None
                lock.assert_held(run_id=request.run_id)
                _check_stage(deadline, cancellation)
                try:
                    deps.install_engine(_remaining(deadline))
                except (_Cancelled, _TimedOut):
                    raise
                except NativeCoordinatorError:
                    raise
                except Exception:
                    raise _Refusal("install_failed") from None
                lock.assert_held(run_id=request.run_id)
                _check_stage(deadline, cancellation)

                writer.phase("wizard")
                read_fd, write_fd = _bind_pipe()
                try:
                    try:
                        argv = deps.wizard_command(write_fd)
                    except NativeCoordinatorError:
                        raise
                    except Exception:
                        raise _Refusal("wizard_failed") from None
                    child = _spawn_owned_child(
                        argv,
                        notification_fd=write_fd,
                        public_fd=request.events_fd,
                        policy=policy,
                    )
                    _close_fd(write_fd)
                    write_fd = -1
                    try:
                        port = _read_bind_notification(
                            read_fd,
                            _sub_deadline(deadline, policy.bind_timeout),
                            cancellation,
                            policy.poll_interval,
                        )
                    except _TimedOut:
                        raise _Refusal("wizard_failed") from None
                finally:
                    _close_fd(write_fd)
                    _close_fd(read_fd)
                # The child's self-reported port is only a claim. The port must
                # have a listener owned by this child or its process group.
                try:
                    listeners = deps.wizard_listener(port, child.pid)
                except (_Cancelled, _TimedOut):
                    raise
                except Exception:
                    raise _Refusal("wizard_failed") from None
                if (
                    not isinstance(listeners, frozenset)
                    or not listeners
                    or not all(child.owns_process(pid) for pid in listeners)
                ):
                    raise _Refusal("wizard_failed")
                _check_stage(deadline, cancellation)
                writer.wizard_ready(port)
                try:
                    code = child.wait_for_exit(
                        _sub_deadline(deadline, policy.wizard_exit_timeout),
                        cancellation,
                    )
                except _TimedOut:
                    raise _Refusal("wizard_failed") from None
                if code != 0:
                    raise _Refusal("wizard_failed")
                _check_stage(deadline, cancellation)
                try:
                    handoff = deps.read_handoff(request.run_id)
                except NativeHandoffError:
                    raise _Refusal("wizard_failed") from None
                except OSError:
                    raise _Refusal("wizard_failed") from None
                except (_Cancelled, _TimedOut):
                    raise
                except Exception:
                    raise _Refusal("wizard_failed") from None
                if handoff.run_id != request.run_id:
                    raise _Refusal("wizard_failed")
                selected_workspace = handoff.workspace
                if request.expected_workspace is not None:
                    try:
                        expected = Path(
                            os.path.realpath(request.expected_workspace)
                        )
                    except OSError:
                        raise _Refusal("wizard_failed") from None
                    if handoff.workspace != expected:
                        raise _Refusal("wizard_failed")
                writer.setup_saved()
                lock.assert_held(run_id=request.run_id)
                _check_stage(deadline, cancellation)

                writer.phase("activation")
                activation_attempted = True
                try:
                    activated_result = deps.activate(
                        request.run_id, _remaining(deadline)
                    )
                except NativeHandoffError:
                    raise _Refusal("activation_refused") from None
                except OSError:
                    raise _Refusal("activation_refused") from None
                except (_Cancelled, _TimedOut):
                    raise
                except Exception:
                    raise _Refusal("activation_refused") from None
                # The service may now be running. Nothing below stops, reloads,
                # quiesces or rolls it back.
                activation_completed = True
                if (
                    activated_result.run_id != request.run_id
                    or activated_result.port != handoff.port
                ):
                    raise _Refusal("activation_refused")
                _check_stage(deadline, cancellation)

                writer.phase("readiness")
                readiness_deadline = _sub_deadline(
                    deadline, policy.readiness_timeout
                )
                while True:
                    _check_stage(deadline, cancellation)
                    remaining = readiness_deadline.remaining()
                    if remaining <= 0:
                        raise _Refusal("service_not_ready")
                    try:
                        proof = deps.prove_ready(host, handoff, remaining)
                    except (_Cancelled, _TimedOut):
                        raise
                    except Exception:
                        raise _Refusal("service_not_ready") from None
                    if not isinstance(proof, NativeReadinessProof):
                        raise _Refusal("service_not_ready")
                    if proof.ready:
                        if proof.failure is not None or proof.port != handoff.port:
                            raise _Refusal("service_not_ready")
                        ready_port = handoff.port
                        break
                    if proof.failure == "cancelled":
                        raise _Cancelled
                    if proof.failure not in _READINESS_RETRYABLE:
                        raise _Refusal("service_not_ready")
                    _wait_observing(
                        min(
                            policy.readiness_interval,
                            max(readiness_deadline.remaining(), 0.0),
                        ),
                        cancellation,
                        policy.poll_interval,
                    )
            except _Refusal as refusal:
                failure = refusal.code
            except _Cancelled:
                failure = "cancelled"
            except _TimedOut:
                failure = "cancelled" if cancellation.cancelled else "internal_error"
            except NativeCoordinatorError as exc:
                failure = exc.failure_code
            except NativeLockError as exc:
                failure = _map_lock_failure(exc)
            except ServerHostInstallError as exc:
                failure = _map_host_failure(exc)
            except NativeHandoffError:
                failure = (
                    "activation_refused"
                    if activation_attempted
                    else "wizard_failed"
                )
            except EventError:
                failure = "event_channel_failed"
                channel_dead = True
            except OSError:
                failure = "internal_error"
            except Exception:
                failure = "internal_error"
        finally:
            cleanup_failed = False
            if child is not None:
                try:
                    child.terminate_and_reap()
                except Exception:
                    # Includes _ChildCleanupFailed and any unexpected cleanup
                    # error: the child may still be alive. Keep the lock so a
                    # second run cannot install beside it, and continue to the
                    # terminal event below instead of skipping it.
                    lock_retained = True
                    cleanup_failed = True
            if lock is not None and not lock_retained:
                try:
                    lock.release()
                except Exception:
                    lock_retained = True
                    cleanup_failed = True
            if cleanup_failed and failure is None:
                failure = "internal_error"

        terminal: str | None = None
        if failure is None and ready_port is not None:
            # Success is reported only here: no failure, and child and lock
            # cleanup both succeeded in the finally above.
            try:
                writer.service_ready(ready_port)
                terminal = "service_ready"
            except EventError:
                channel_dead = True
                failure = "event_channel_failed"
        else:
            if failure is None:
                failure = "internal_error"
            if not channel_dead:
                try:
                    writer.failed(failure)
                    terminal = "failed"
                except EventError:
                    channel_dead = True
                    failure = "event_channel_failed"
        if channel_dead:
            terminal = None
    finally:
        writer.close()
    return NativeInstallOutcome(
        run_id=request.run_id,
        terminal=terminal,
        failure_code=None if terminal == "service_ready" else failure,
        host_step_attempted=host_step_attempted,
        host_step_completed=host_step_completed,
        activation_attempted=activation_attempted,
        activation_completed=activation_completed,
        selected_workspace=selected_workspace,
        lock_retained=lock_retained,
    )
