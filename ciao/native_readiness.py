"""Read-only proof that a native install's service is the engine it started (#2340).

The native installer starts the engine through ``launchd`` and then has to answer
one question before it may emit ``service_ready``: is the process answering on
the chosen port *this* run's installed engine, and not an unrelated listener, a
stale run, or a different Ciaobot workspace? ``scripts/install-engine.sh``
answers it with ``native_host_probe_candidate`` plus ``fresh_startup_state``; this
module is the Python equivalent, with every external effect injected.

The proof is a chain of observations, and every link is bound to a concrete
object rather than to a readable string:

0. the request names the bundle this run installed and its owner-only record;
   the proof calls :func:`ciao.server_host.verify_owned_host` immediately before
   readiness, which re-reads the record and cryptographically re-inspects the
   bundle and signature through bounded, shell-free ``codesign`` probes. No
   caller-supplied dataclass or previously computed snapshot is accepted, and
   the canonical host executable is derived from the verified snapshot;
1. explicit fresh-only evidence is present: ``fresh_install_service_absent``
   must be true, meaning the preflight refused an existing service. This proof
   makes no claim for a resumed, reused or old host;
2. exactly one same-UID listener visible to lsof; foreign-UID listeners are not
   enumerated, and identity rests on the startup_id and server.lock binding;
3. the startup-status answer carries the exact installed version, the startup
   API version, a canonical ``startup_id`` and the engine's own pid, and its
   ``server_starting`` phase is done with ``overall_ready`` set;
4. exactly one same-UID listener visible to lsof still holds the port afterwards,
   and it is the same pid; foreign-UID listeners are not enumerated, and identity
   rests on the startup_id and server.lock binding;
5. the private runtime ``server.lock`` names that pid, the responder's
   ``startup_id``, the requested port, the resolved workspace and its resolved
   runtime directory; the lock is a single-link regular file owned by this user
   with mode ``0600``, opened no-follow, and the opened inode/device/size/mtime
   are rechecked;
6. launchd reports a running pid for the job whose argv is exactly the hosted
   ``Ciaobot Server serve --python <absolute interpreter>`` command naming the
   required literal ``engine_python``, and that pid is not the Python process
   holding the socket;
7. the listener's bounded parent chain (four hops) reaches launchd's pid, so the
   socket holder is a child of the host rather than an unrelated process;
8. the authoritative kernel executable path from macOS ``proc_pidpath`` equals
   the verified snapshot's host executable. This is the executable identity;
   ``lsof -d txt`` is **not** used, because a real body also lists dyld/cache
   mappings and any extra record could only create a false negative. The
   listener proof still uses its own ``lsof -t`` query;
9. the listener and the runtime lock are re-read after the ancestry/executable
   checks and must still name the same pid and ``startup_id``, so a restart in
   the middle of the proof cannot pass.

Only after the whole chain does the result say ``ready``. Anything missing,
unreadable, duplicated, changed or over a bound is a fixed failure code; no
failure reason interpolates a path, exception string, process name, URL, token
or startup id.

The native coordinator (``ciao.native_installer``) calls this proof after
activation, and nothing else calls it. No CLI, route or service-layer caller
exists. It never starts, stops, bootstraps, reloads, quiesces or
rolls back a service, and it writes nothing: the lock is opened ``O_RDONLY`` and
the only subprocesses a production adapter may run are read-only queries
(``lsof`` for the listener, ``ps`` and ``launchctl print``), the bounded
``codesign`` reinspection inside ``verify_owned_host``, plus one strict loopback
HTTP GET. A caller must not treat a successful proof as permission to mutate a
service.

What this is not:

* The wizard/handoff ``startup_id`` is **not** a service identity and is never
  accepted here. Only the running engine's lock id and its own answer are
  compared. The proof is fresh-only and has no previous-run or resume path; the
  caller must not use it to claim that an automatically resumed or old host is
  the newly installed engine.
* ``verify_owned_host`` is a real cryptographic reinspection, not a record read
  or a trusted dataclass, but it is still a check-then-use observation: a
  same-UID writer can replace bundle bytes after verification, and a prior host
  at the same canonical bundle path is prevented by the native fresh-only
  preflight (``require_absent``), not by this proof. The path check cannot
  distinguish two inodes at one path.
* This is not atomic and does not claim same-UID safety. Each step is a
  separate observation, and a same-UID writer can change what a later step
  sees: the lock can be replaced before ``lstat``/``open``, the process table
  can change between ``ps`` calls, and a file can be replaced after the kernel
  reads it. The final listener/lock re-read narrows the restart window and the
  inode/size/mtime recheck narrows the lock window; neither closes them.
* ``port_in_use`` is reported only from the engine's own startup-status answer.
  The external ``port-in-use.txt`` record is not read: without a timestamp bound
  to this run it cannot be attributed to this run, so this proof does not claim
  to diagnose a bind failure from it.
* A timeout is absence of evidence, not a negative: it reports ``timeout`` and
  never ``ready``. ``listener_missing`` and ``startup_not_ready`` are one-shot
  answers; a caller that is still inside its install budget may call the proof
  again after a bounded sleep. This function never waits for readiness itself.

Tests inject :class:`NativeReadinessDeps`; host verification is injectable only
through the private ``_prove_native_readiness`` seam. :func:`make_native_readiness_deps`
builds the real read-only adapters the native coordinator uses. The module imports
on every platform, but the default adapters use macOS paths and are only useful
on macOS.
"""

from __future__ import annotations

import ctypes
import ipaddress
import json
import os
import posixpath
import re
import selectors
import socket
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Callable, Final, Mapping, cast

from ciao.macos_service import (
    LAUNCHCTL,
    SERVER_LABEL,
    launchctl_print_field,
    parse_launchctl_print_arguments,
)
from ciao.server_host import (
    APP_NAME,
    EXECUTABLE_NAME,
    HostOwnership,
    ServerHostError,
    parse_service_command,
    verify_owned_host,
)

#: One bounded startup-status body, matching the shell's 65537-byte read.
MAX_STARTUP_RESPONSE_BYTES: Final[int] = 65536
#: One bounded private runtime lock. The engine writes a small JSON object.
MAX_LOCK_BYTES: Final[int] = 4096
#: A startup-status object is a small closed document; extra fields are ignored
#: but a flood of them is not.
MAX_STARTUP_FIELDS: Final[int] = 64
MAX_STARTUP_PHASES: Final[int] = 64
MAX_PHASE_NAME_CHARS: Final[int] = 128
MAX_PHASE_MESSAGE_CHARS: Final[int] = 4096
MAX_TIMESTAMP_CHARS: Final[int] = 64
MAX_VERSION_CHARS: Final[int] = 64
MAX_STARTUP_ID_CHARS: Final[int] = 32
MAX_PATH_CHARS: Final[int] = 4096
MAX_LABEL_CHARS: Final[int] = 128
MAX_JOB_ARGUMENTS: Final[int] = 8
MAX_ANCESTOR_HOPS: Final[int] = 4
#: One adapter query's output. ``lsof -F`` and ``launchctl print`` are small;
#: anything larger is not a shape this proof will parse.
MAX_ADAPTER_OUTPUT_BYTES: Final[int] = 65536
#: One strict loopback HTTP response. Headers and body are bounded separately;
#: a redirect, a proxy answer, a chunked body or an over-long header is refused
#: rather than followed or truncated into a "valid" answer.
MAX_HTTP_HEADER_BYTES: Final[int] = 8192
MAX_HTTP_HEADER_COUNT: Final[int] = 64
MAX_HTTP_STATUS_LINE_BYTES: Final[int] = 256
#: libproc's documented path buffer: ``PROC_PIDPATHINFO_MAXSIZE`` is
#: ``4 * MAXPATHLEN`` (4096) in ``sys/proc_info.h``.
PROC_PIDPATHINFO_MAXSIZE: Final[int] = 4096
#: The shell waits three times, one second apart, for the listener.
LISTENER_ATTEMPTS: Final[int] = 3
LISTENER_INTERVAL_SECONDS: Final[float] = 1.0
DEFAULT_TIMEOUT_SECONDS: Final[float] = 60.0
MAX_TIMEOUT_SECONDS: Final[float] = 60.0
DEFAULT_PROBE_TIMEOUT_SECONDS: Final[float] = 2.0
MAX_PROBE_TIMEOUT_SECONDS: Final[float] = 5.0
MAX_PID: Final[int] = 2**31 - 1
#: A single read-only adapter query. The whole proof's deadline still governs
#: the sum; this only stops one stuck command from outliving the caller.
ADAPTER_TIMEOUT_SECONDS: Final[float] = 5.0
JOB_PROBE_TIMEOUT_SECONDS: Final[float] = 10.0

SERVER_LOCK_NAME: Final[str] = "server.lock"
SERVER_RUNTIME_DIR: Final[str] = ".runtime"
LSOF: Final[str] = "/usr/sbin/lsof"
PS: Final[str] = "/bin/ps"
#: The pinned system library. ``ctypes.util.find_library`` may spawn external
#: developer tools; this proof loads exactly the system path instead.
LIBPROC: Final[str] = "/usr/lib/libproc.dylib"

#: A minimal environment for every spawned read-only tool and for the
#: ``codesign`` reinspection. ``DYLD_*``/``LD_*`` and other loader overrides from
#: the caller are deliberately absent.
_SAFE_ENV: Final[Mapping[str, str]] = MappingProxyType(
    {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C",
        "LC_ALL": "C",
    }
)

#: Every fixed failure this module can report. The result carries no other text.
FAILURE_CODES: Final[tuple[str, ...]] = (
    "cancelled",
    "timeout",
    "internal_error",
    "host_verification_failed",
    "listener_missing",
    "startup_status_unavailable",
    "startup_status_invalid",
    "version_mismatch",
    "startup_id_invalid",
    "startup_pid_mismatch",
    "startup_not_ready",
    "port_in_use",
    "listener_changed",
    "lock_missing",
    "lock_unsafe",
    "lock_invalid",
    "lock_pid_mismatch",
    "lock_startup_mismatch",
    "lock_port_mismatch",
    "lock_workspace_mismatch",
    "lock_runtime_mismatch",
    "lock_changed",
    "job_missing",
    "job_command_invalid",
    "job_program_mismatch",
    "job_python_mismatch",
    "job_is_listener",
    "ancestry_unreadable",
    "host_not_in_ancestry",
    "executable_unreadable",
    "executable_mismatch",
)
#: The only reason :class:`NativeReadinessError` carries. It never includes the
#: rejected value.
ERROR_REASONS: Final[tuple[str, ...]] = ("invalid_arguments",)

_VERSION_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\Z")
_LABEL_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_DECIMAL_RE: Final[re.Pattern[str]] = re.compile(r"[0-9]{1,10}\Z")
_STARTUP_ID_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}\Z")
_PHASE_STATUSES: Final[frozenset[str]] = frozenset(
    {"pending", "in_progress", "done", "failed"}
)
_BIND_PHASE: Final[str] = "server_starting"

#: A bounded subprocess runner. The production implementation reads stdout only
#: up to the cap while the child runs; tests inject a plain fake.
BoundedRunner = Callable[
    [tuple[str, ...], float], subprocess.CompletedProcess[str] | None
]


class NativeReadinessError(RuntimeError):
    """A fixed, path-free refusal for invalid proof inputs."""

    def __init__(self, reason: str) -> None:
        if reason not in ERROR_REASONS:
            raise ValueError("unknown native readiness error reason")
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class LaunchdJob:
    """The pid and exact argv launchd reports for one loaded job."""

    pid: int
    arguments: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NativeReadinessRequest:
    """The facts the caller fixed for one *fresh-only* native install.

    ``workspace`` is normalized to its resolved absolute path, because the
    engine records the resolved workspace and runtime in ``server.lock``. The
    host argv and interpreter are literal command paths and are never resolved
    behind the caller's back.

    ``host_bundle`` is the bundle this run installed and ``ownership_path`` is
    its owner-only record. The proof calls :func:`ciao.server_host.verify_owned_host`
    immediately before readiness for that exact pair: it re-inspects the bundle
    and verifies the signature/record cryptographically, then derives the
    canonical executable from the verified snapshot. A dataclass, a bare path or
    a previously computed snapshot is not accepted as proof. The reinspection is
    real, but it is still a check-then-use observation: a same-UID writer can
    replace bundle bytes after it, and a prior host at the same canonical path
    is the native fresh-only ``require_absent`` preflight's concern.

    ``engine_python`` is required and literal; the hosted launchd argv must name
    exactly it. ``fresh_install_service_absent`` must be explicitly true: this
    proof is only for the native fresh-only path, where the preflight refused an
    existing service. It is not a resume proof for an old or automatically
    resumed host, and the proof makes no claim for that case.
    """

    version: str
    port: int
    workspace: Path
    host_bundle: Path
    ownership_path: Path
    engine_python: Path
    fresh_install_service_absent: bool
    label: str = SERVER_LABEL


@dataclass(frozen=True, slots=True)
class NativeReadinessPolicy:
    """Finite bounds for one proof, including the injected seams' behaviour.

    ``timeout`` bounds the whole proof. ``listener_attempts`` and
    ``listener_interval`` bound the two listener observations the way the shell
    does. ``probe_timeout`` is the most the startup-status seam may spend on one
    request; the proof clamps it to what is left of ``timeout``. The seams are
    synchronous: the proof checks the deadline before and after each one, and a
    production adapter bounds its own command, but a seam that overruns is not
    preempted by this module.
    """

    timeout: float = DEFAULT_TIMEOUT_SECONDS
    listener_attempts: int = LISTENER_ATTEMPTS
    listener_interval: float = LISTENER_INTERVAL_SECONDS
    probe_timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        for name, maximum in (
            ("timeout", MAX_TIMEOUT_SECONDS),
            ("probe_timeout", MAX_PROBE_TIMEOUT_SECONDS),
        ):
            value = getattr(self, name)
            if type(value) not in (int, float):
                raise NativeReadinessError("invalid_arguments")
            number = float(value)
            if not (0 < number <= maximum):
                raise NativeReadinessError("invalid_arguments")
        if type(self.listener_attempts) is not int or not (
            1 <= self.listener_attempts <= 10
        ):
            raise NativeReadinessError("invalid_arguments")
        if type(self.listener_interval) not in (int, float):
            raise NativeReadinessError("invalid_arguments")
        interval = float(self.listener_interval)
        if not (0 <= interval <= MAX_TIMEOUT_SECONDS):
            raise NativeReadinessError("invalid_arguments")


@dataclass(frozen=True, slots=True)
class NativeReadinessDeps:
    """Every external observation, injected by tests and bound by the native coordinator.

    ``listener_pid`` answers the one pid holding the listening socket, or
    ``None`` for no listener, more than one, or a lookup that could not answer.
    ``startup_status`` returns at most :data:`MAX_STARTUP_RESPONSE_BYTES` raw
    bytes and enforces the absolute deadline it is given. ``job`` returns
    launchd's pid and argv for ``label`` or ``None``. ``ancestor_pids`` returns
    the listener pid followed by at most four ancestors, or ``None`` when the
    walk cannot answer. ``executable_path`` is the authoritative kernel
    executable path (``proc_pidpath``), or ``None`` when the kernel cannot
    answer. ``clock`` and ``sleep`` are the only time seams. Host ownership
    verification is deliberately not a public dependency; the proof always
    calls the real verifier and tests inject it only through the private
    ``_prove_native_readiness`` seam.
    """

    listener_pid: Callable[[int], int | None]
    startup_status: Callable[[int, float], bytes | None]
    job: Callable[[str], LaunchdJob | None]
    ancestor_pids: Callable[[int], tuple[int, ...] | None]
    executable_path: Callable[[int], str | None]
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True, slots=True)
class NativeReadinessProof:
    """The fixed outcome of one proof; no logs, paths or exception strings."""

    ready: bool
    failure: str | None
    port: int
    version: str
    startup_id: str | None = None
    listener_pid: int | None = None
    host_pid: int | None = None

    def __post_init__(self) -> None:
        if type(self.ready) is not bool:
            raise ValueError("native readiness success must be a boolean")
        if self.ready and self.failure is not None:
            raise ValueError("a ready proof cannot carry a failure")
        if not self.ready and self.failure not in FAILURE_CODES:
            raise ValueError("unknown native readiness failure")


@dataclass(frozen=True, slots=True)
class _StartupStatus:
    version: str
    startup_id: str
    pid: int


@dataclass(frozen=True, slots=True)
class _RuntimeLock:
    startup_id: str
    pid: int
    port: int
    workspace: str
    runtime: str


class _Invalid(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _pid(value: object) -> int | None:
    """A positive pid, rejecting ``bool`` (a subclass of ``int``) and overflow."""
    if type(value) is not int or value <= 0 or value > MAX_PID:
        return None
    return value


def _decimal_pid(text: str) -> int | None:
    """A canonical non-negative decimal pid from one command line."""
    if not _DECIMAL_RE.fullmatch(text):
        return None
    number = int(text)
    if str(number) != text:
        return None
    return _pid(number)


def _getuid() -> int | None:
    """This user's uid, or ``None`` on a platform without ``os.getuid``."""
    getuid: Callable[[], int] | None = getattr(os, "getuid", None)
    if getuid is None:
        return None
    return getuid()


def _absolute_path(value: object) -> str | None:
    """A normalized absolute POSIX path string, or ``None``.

    Mirrors ``ciao.server_host``'s path rule so a value compared with launchd's
    argv or ``lsof``'s text is the same string on every platform. A backslash is
    refused rather than treated as a macOS filename byte.
    """
    if not isinstance(value, Path):
        return None
    try:
        text = os.fspath(value)
    except TypeError:
        return None
    if not isinstance(text, str):
        return None
    if not text or len(text) > MAX_PATH_CHARS or "\x00" in text or "\\" in text:
        return None
    if text.startswith("//") or not PurePosixPath(text).is_absolute():
        return None
    if posixpath.normpath(text) != text:
        return None
    return text


def _host_executable(ownership: HostOwnership) -> Path:
    """The canonical host executable derived from a verified bundle snapshot."""
    return Path(ownership.bundle_path) / "Contents" / "MacOS" / EXECUTABLE_NAME


def _validate_request(request: object) -> NativeReadinessRequest:
    """Validate every input bound before any seam is touched."""
    if not isinstance(request, NativeReadinessRequest):
        raise NativeReadinessError("invalid_arguments")
    version = request.version
    if (
        not isinstance(version, str)
        or not (1 <= len(version) <= MAX_VERSION_CHARS)
        or not _VERSION_RE.fullmatch(version)
    ):
        raise NativeReadinessError("invalid_arguments")
    if type(request.port) is not int or not (1 <= request.port <= 65535):
        raise NativeReadinessError("invalid_arguments")
    workspace_text = _absolute_path(request.workspace)
    if workspace_text is None:
        raise NativeReadinessError("invalid_arguments")
    try:
        resolved_workspace = Path(os.path.realpath(workspace_text))
    except OSError:
        raise NativeReadinessError("invalid_arguments") from None
    if _absolute_path(resolved_workspace) is None:
        raise NativeReadinessError("invalid_arguments")

    host_bundle = _absolute_path(request.host_bundle)
    if host_bundle is None or PurePosixPath(host_bundle).name != APP_NAME:
        raise NativeReadinessError("invalid_arguments")
    ownership_path = _absolute_path(request.ownership_path)
    if ownership_path is None:
        raise NativeReadinessError("invalid_arguments")
    python = _absolute_path(request.engine_python)
    if python is None:
        raise NativeReadinessError("invalid_arguments")
    if (
        type(request.fresh_install_service_absent) is not bool
        or not request.fresh_install_service_absent
    ):
        # This proof is fresh-only. A caller that observed an existing service
        # is not the native fresh path and must not claim readiness for a
        # resumed or old host through this module.
        raise NativeReadinessError("invalid_arguments")

    label = request.label
    if (
        not isinstance(label, str)
        or not (1 <= len(label) <= MAX_LABEL_CHARS)
        or not _LABEL_RE.fullmatch(label)
    ):
        raise NativeReadinessError("invalid_arguments")
    return NativeReadinessRequest(
        version=version,
        port=request.port,
        workspace=resolved_workspace,
        host_bundle=Path(host_bundle),
        ownership_path=Path(ownership_path),
        engine_python=Path(python),
        fresh_install_service_absent=True,
        label=label,
    )


def _validate_deps(deps: object) -> NativeReadinessDeps:
    if not isinstance(deps, NativeReadinessDeps):
        raise NativeReadinessError("invalid_arguments")
    for name in (
        "listener_pid",
        "startup_status",
        "job",
        "ancestor_pids",
        "executable_path",
        "clock",
        "sleep",
    ):
        if not callable(getattr(deps, name)):
            raise NativeReadinessError("invalid_arguments")
    return deps


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError("invalid JSON constant")


def _decode_json(raw: bytes) -> object:
    """Strict UTF-8 only. ``json.loads`` on bytes would auto-detect UTF-16/32."""
    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_json_constant,
    )


def _bounded_string(value: object, maximum: int, *, allow_empty: bool) -> str | None:
    if not isinstance(value, str) or len(value) > maximum:
        return None
    if not allow_empty and not value:
        return None
    return value


def _parse_startup_status(raw: bytes, expected_version: str) -> _StartupStatus:
    """Strictly parse the bounded startup-status body into the proof's facts."""
    try:
        document = _decode_json(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _Invalid("startup_status_invalid") from None
    if type(document) is not dict or len(document) > MAX_STARTUP_FIELDS:
        raise _Invalid("startup_status_invalid")
    document = cast(dict[str, object], document)
    for key in document:
        if not isinstance(key, str):
            raise _Invalid("startup_status_invalid")
    reported_version = document.get("version")
    if reported_version != expected_version:
        raise _Invalid("version_mismatch")
    api_version = document.get("desktop_api_version")
    if type(api_version) is not int or api_version != 1:
        raise _Invalid("startup_status_invalid")
    for key in ("overall_ready", "port_in_use", "update_available"):
        if type(document.get(key)) is not bool:
            raise _Invalid("startup_status_invalid")
    if "latest_version" not in document:
        raise _Invalid("startup_status_invalid")
    latest_version = document["latest_version"]
    if latest_version is not None and (
        not isinstance(latest_version, str)
        or len(latest_version) > MAX_VERSION_CHARS
    ):
        raise _Invalid("startup_status_invalid")
    startup_id = document.get("startup_id")
    if not isinstance(startup_id, str) or not _STARTUP_ID_RE.fullmatch(startup_id):
        raise _Invalid("startup_id_invalid")
    pid = _pid(document.get("pid"))
    if pid is None:
        raise _Invalid("startup_status_invalid")
    # Phase-count and message-length caps are deliberate; over-cap answers fail closed.
    phases = document.get("phases")
    if type(phases) is not list or len(phases) > MAX_STARTUP_PHASES:
        raise _Invalid("startup_status_invalid")
    by_name: dict[str, dict[str, object]] = {}
    for phase in phases:
        if type(phase) is not dict:
            raise _Invalid("startup_status_invalid")
        phase = cast(dict[str, object], phase)
        name = _bounded_string(phase.get("name"), MAX_PHASE_NAME_CHARS, allow_empty=False)
        if name is None or name in by_name:
            raise _Invalid("startup_status_invalid")
        status = phase.get("status")
        if not isinstance(status, str) or status not in _PHASE_STATUSES:
            raise _Invalid("startup_status_invalid")
        message = _bounded_string(
            phase.get("message"), MAX_PHASE_MESSAGE_CHARS, allow_empty=True
        )
        if message is None:
            raise _Invalid("startup_status_invalid")
        for key in ("started_at", "finished_at"):
            if key not in phase:
                raise _Invalid("startup_status_invalid")
            timestamp = phase[key]
            if timestamp is not None and (
                not isinstance(timestamp, str) or len(timestamp) > MAX_TIMESTAMP_CHARS
            ):
                raise _Invalid("startup_status_invalid")
        by_name[name] = phase
    if document["port_in_use"]:
        raise _Invalid("port_in_use")
    bind = by_name.get(_BIND_PHASE)
    if not document["overall_ready"] or bind is None or bind.get("status") != "done":
        raise _Invalid("startup_not_ready")
    return _StartupStatus(version=expected_version, startup_id=startup_id, pid=pid)


def _read_runtime_lock(workspace: Path, port: int) -> _RuntimeLock:
    """Read the private runtime lock through one no-follow descriptor.

    ``workspace`` is already resolved. The engine writes the *resolved* workspace
    and runtime parent, so both comparisons resolve the runtime directory too.
    The host and interpreter command paths are deliberately not resolved here.
    """
    path = workspace / SERVER_RUNTIME_DIR / SERVER_LOCK_NAME
    try:
        listed = os.lstat(path)
    except FileNotFoundError:
        raise _Invalid("lock_missing") from None
    except OSError:
        raise _Invalid("lock_unsafe") from None
    uid = _getuid()
    if uid is None or stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
        raise _Invalid("lock_unsafe")
    if listed.st_uid != uid or stat.S_IMODE(listed.st_mode) != 0o600:
        raise _Invalid("lock_unsafe")
    if listed.st_nlink != 1 or listed.st_size > MAX_LOCK_BYTES:
        raise _Invalid("lock_unsafe")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise _Invalid("lock_unsafe") from None
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ) != (
            listed.st_dev,
            listed.st_ino,
            listed.st_size,
            listed.st_mtime_ns,
        ):
            raise _Invalid("lock_unsafe")
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != uid
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_size > MAX_LOCK_BYTES
        ):
            raise _Invalid("lock_unsafe")
        chunks: list[bytes] = []
        total = 0
        while total <= MAX_LOCK_BYTES:
            chunk = os.read(descriptor, MAX_LOCK_BYTES + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_LOCK_BYTES:
        raise _Invalid("lock_unsafe")
    if (
        (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
        or after.st_size != opened.st_size
        or after.st_mtime_ns != opened.st_mtime_ns
    ):
        raise _Invalid("lock_unsafe")
    try:
        document = _decode_json(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _Invalid("lock_invalid") from None
    if type(document) is not dict:
        raise _Invalid("lock_invalid")
    document = cast(dict[str, object], document)
    if document.get("status") != "running":
        raise _Invalid("lock_invalid")
    startup_id = document.get("startup_id")
    if not isinstance(startup_id, str) or not _STARTUP_ID_RE.fullmatch(startup_id):
        raise _Invalid("lock_invalid")
    pid = _pid(document.get("pid"))
    if pid is None:
        raise _Invalid("lock_invalid")
    recorded_port = document.get("port")
    if type(recorded_port) is not int:
        raise _Invalid("lock_invalid")
    if recorded_port != port:
        raise _Invalid("lock_port_mismatch")
    recorded_workspace = document.get("workspace")
    if not isinstance(recorded_workspace, str):
        raise _Invalid("lock_invalid")
    expected_workspace = os.fspath(workspace)
    if recorded_workspace != expected_workspace:
        raise _Invalid("lock_workspace_mismatch")
    recorded_runtime = document.get("runtime")
    if not isinstance(recorded_runtime, str):
        raise _Invalid("lock_invalid")
    try:
        resolved_runtime = Path(os.path.realpath(workspace / SERVER_RUNTIME_DIR))
    except OSError:
        raise _Invalid("lock_runtime_mismatch") from None
    expected_runtime = os.fspath(resolved_runtime)
    if recorded_runtime != expected_runtime:
        raise _Invalid("lock_runtime_mismatch")
    return _RuntimeLock(
        startup_id=startup_id,
        pid=pid,
        port=recorded_port,
        workspace=recorded_workspace,
        runtime=recorded_runtime,
    )


def _check(
    cancelled: Callable[[], bool], clock: Callable[[], float], deadline: float
) -> str | None:
    try:
        if cancelled():
            return "cancelled"
        if deadline - clock() <= 0:
            return "timeout"
    except Exception:
        return "internal_error"
    return None


def _wait_for_listener(
    port: int,
    deps: NativeReadinessDeps,
    policy: NativeReadinessPolicy,
    deadline: float,
    cancelled: Callable[[], bool],
) -> tuple[int | None, str | None]:
    for attempt in range(policy.listener_attempts):
        code = _check(cancelled, deps.clock, deadline)
        if code is not None:
            return None, code
        try:
            reported = deps.listener_pid(port)
        except Exception:
            return None, "internal_error"
        listener = _pid(reported)
        if listener is not None:
            return listener, None
        if attempt + 1 == policy.listener_attempts:
            break
        try:
            remaining = deadline - deps.clock()
        except Exception:
            return None, "internal_error"
        if remaining <= 0:
            return None, "timeout"
        delay = min(float(policy.listener_interval), remaining)
        try:
            deps.sleep(delay)
        except Exception:
            return None, "internal_error"
    code = _check(cancelled, deps.clock, deadline)
    if code is not None:
        return None, code
    return None, "listener_missing"


def _probe_startup_status(
    port: int,
    request: NativeReadinessRequest,
    deps: NativeReadinessDeps,
    policy: NativeReadinessPolicy,
    deadline: float,
    cancelled: Callable[[], bool],
) -> tuple[_StartupStatus | None, str | None]:
    code = _check(cancelled, deps.clock, deadline)
    if code is not None:
        return None, code
    try:
        remaining = deadline - deps.clock()
    except Exception:
        return None, "internal_error"
    if remaining <= 0:
        return None, "timeout"
    probe_timeout = min(float(policy.probe_timeout), remaining)
    try:
        raw = deps.startup_status(port, probe_timeout)
    except Exception:
        return None, "startup_status_unavailable"
    if not isinstance(raw, bytes) or not raw:
        return None, "startup_status_unavailable"
    if len(raw) > MAX_STARTUP_RESPONSE_BYTES:
        return None, "startup_status_invalid"
    try:
        status = _parse_startup_status(raw, request.version)
    except _Invalid as exc:
        return None, exc.code
    return status, None


def _validate_job(
    job: LaunchdJob,
    host_executable: str,
    engine_python: str,
    listener: int,
) -> tuple[int | None, str | None]:
    if not isinstance(job, LaunchdJob):
        return None, "job_missing"
    job_pid = _pid(job.pid)
    if job_pid is None:
        return None, "job_missing"
    if job_pid == listener:
        return None, "job_is_listener"
    arguments = job.arguments
    if (
        not isinstance(arguments, tuple)
        or not (1 <= len(arguments) <= MAX_JOB_ARGUMENTS)
        or any(
            not isinstance(argument, str)
            or not argument
            or len(argument) > MAX_PATH_CHARS
            or "\x00" in argument
            for argument in arguments
        )
    ):
        return None, "job_command_invalid"
    try:
        command = parse_service_command(arguments)
    except ServerHostError:
        return None, "job_command_invalid"
    if (
        command.mode != "hosted"
        or len(arguments) != 4
        or arguments[1] != "serve"
        or arguments[2] != "--python"
    ):
        return None, "job_command_invalid"
    if command.program != host_executable:
        return None, "job_program_mismatch"
    if command.python != engine_python:
        return None, "job_python_mismatch"
    return job_pid, None


def _fail(request: NativeReadinessRequest, code: str) -> NativeReadinessProof:
    return NativeReadinessProof(
        ready=False,
        failure=code,
        port=request.port,
        version=request.version,
    )


VerifyOwned = Callable[[Path, Path], HostOwnership]


def prove_native_readiness(
    request: NativeReadinessRequest,
    *,
    deps: NativeReadinessDeps,
    policy: NativeReadinessPolicy | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> NativeReadinessProof:
    """Run the read-only readiness chain and return one fixed outcome.

    ``deps`` is required so no test or caller reaches a real service or network
    by accident; :func:`make_native_readiness_deps` is the production adapter
    builder. Host ownership is always re-inspected through the real
    :func:`ciao.server_host.verify_owned_host`; the verifier is injectable only
    through the private :func:`_prove_native_readiness` seam for tests.
    ``cancelled`` is a plain predicate checked between every stage and across
    the listener waits. Invalid inputs raise :class:`NativeReadinessError` with
    ``invalid_arguments`` before any seam runs; every operational failure is a
    :class:`NativeReadinessProof` with a fixed failure code.
    """
    return _prove_native_readiness(
        request,
        deps=deps,
        policy=policy,
        cancelled=cancelled,
        verify_owned=_verify_owned_host,
    )


def _prove_native_readiness(
    request: NativeReadinessRequest,
    *,
    deps: NativeReadinessDeps,
    verify_owned: VerifyOwned,
    policy: NativeReadinessPolicy | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> NativeReadinessProof:
    """The private lifecycle seam; ``verify_owned`` is for tests only."""
    request = _validate_request(request)
    deps = _validate_deps(deps)
    if not callable(verify_owned):
        raise NativeReadinessError("invalid_arguments")
    policy = NativeReadinessPolicy() if policy is None else policy
    if not isinstance(policy, NativeReadinessPolicy):
        raise NativeReadinessError("invalid_arguments")
    cancel = (lambda: False) if cancelled is None else cancelled
    if not callable(cancel):
        raise NativeReadinessError("invalid_arguments")
    try:
        deadline = deps.clock() + policy.timeout
    except Exception:
        return _fail(request, "internal_error")

    code = _check(cancel, deps.clock, deadline)
    if code is not None:
        return _fail(request, code)
    try:
        ownership = verify_owned(request.host_bundle, request.ownership_path)
    except Exception:
        return _fail(request, "host_verification_failed")
    if not isinstance(ownership, HostOwnership):
        return _fail(request, "host_verification_failed")
    # The verified snapshot must be the bundle this request names. The real
    # verifier answers with the realpath of the bundle it inspected.
    if ownership.bundle_path != os.path.realpath(os.fspath(request.host_bundle)):
        return _fail(request, "host_verification_failed")
    host_executable = _host_executable(ownership)
    if _absolute_path(host_executable) is None:
        return _fail(request, "host_verification_failed")
    host_executable_text = os.fspath(host_executable)
    engine_python_text = os.fspath(request.engine_python)
    code = _check(cancel, deps.clock, deadline)
    if code is not None:
        return _fail(request, code)

    listener, code = _wait_for_listener(
        request.port, deps, policy, deadline, cancel
    )
    if code is not None:
        return _fail(request, code)
    assert listener is not None

    status, code = _probe_startup_status(
        request.port, request, deps, policy, deadline, cancel
    )
    if code is not None:
        return _fail(request, code)
    assert status is not None
    if status.pid != listener:
        return _fail(request, "startup_pid_mismatch")

    listener_after, code = _wait_for_listener(
        request.port, deps, policy, deadline, cancel
    )
    if code is not None:
        return _fail(request, code)
    if listener_after != listener:
        return _fail(request, "listener_changed")

    code = _check(cancel, deps.clock, deadline)
    if code is not None:
        return _fail(request, code)
    try:
        lock = _read_runtime_lock(request.workspace, request.port)
    except _Invalid as exc:
        return _fail(request, exc.code)
    except OSError:
        return _fail(request, "lock_unsafe")
    if lock.pid != listener:
        return _fail(request, "lock_pid_mismatch")
    if lock.startup_id != status.startup_id:
        return _fail(request, "lock_startup_mismatch")

    code = _check(cancel, deps.clock, deadline)
    if code is not None:
        return _fail(request, code)
    try:
        job = deps.job(request.label)
    except Exception:
        return _fail(request, "job_missing")
    if job is None:
        return _fail(request, "job_missing")
    job_pid, code = _validate_job(
        job, host_executable_text, engine_python_text, listener
    )
    if code is not None:
        return _fail(request, code)
    assert job_pid is not None

    code = _check(cancel, deps.clock, deadline)
    if code is not None:
        return _fail(request, code)
    try:
        chain = deps.ancestor_pids(listener)
    except Exception:
        return _fail(request, "ancestry_unreadable")
    if (
        not isinstance(chain, tuple)
        or not (1 <= len(chain) <= MAX_ANCESTOR_HOPS + 1)
        or chain[0] != listener
        or any(_pid(pid) is None for pid in chain)
        or len(set(chain)) != len(chain)
    ):
        return _fail(request, "ancestry_unreadable")
    if job_pid not in chain[1:]:
        return _fail(request, "host_not_in_ancestry")

    code = _check(cancel, deps.clock, deadline)
    if code is not None:
        return _fail(request, code)
    try:
        kernel_path = deps.executable_path(job_pid)
    except Exception:
        return _fail(request, "executable_unreadable")
    if not isinstance(kernel_path, str):
        return _fail(request, "executable_unreadable")
    try:
        normalized_kernel = _absolute_path(Path(kernel_path))
    except (TypeError, ValueError):
        return _fail(request, "executable_unreadable")
    if normalized_kernel is None:
        return _fail(request, "executable_unreadable")
    if normalized_kernel != host_executable_text:
        return _fail(request, "executable_mismatch")

    # Revalidate the same process instance at the end: the socket, the lock and
    # the startup identity are separate observations, and a service that
    # restarted while the ancestor/executable checks ran must not pass.
    final_listener, code = _wait_for_listener(
        request.port, deps, policy, deadline, cancel
    )
    if code is not None:
        return _fail(request, code)
    if final_listener != listener:
        return _fail(request, "listener_changed")
    code = _check(cancel, deps.clock, deadline)
    if code is not None:
        return _fail(request, code)
    try:
        final_lock = _read_runtime_lock(request.workspace, request.port)
    except _Invalid as exc:
        return _fail(request, exc.code)
    except OSError:
        return _fail(request, "lock_unsafe")
    if final_lock.startup_id != lock.startup_id or final_lock.pid != lock.pid:
        return _fail(request, "lock_changed")

    return NativeReadinessProof(
        ready=True,
        failure=None,
        port=request.port,
        version=request.version,
        startup_id=lock.startup_id,
        listener_pid=listener,
        host_pid=job_pid,
    )


# ── read-only production adapters (called by the native coordinator) ────────


def parse_listener_pid(stdout: str) -> int | None:
    """The one unique ``lsof -t`` pid, or ``None`` for none, several or junk.

    A process may hold two listening sockets (for example IPv4 and IPv6), and
    ``lsof`` can print its pid once per socket. Repeating the same pid is still
    one listener; two different pids are ambiguous and refused.
    """
    pids: set[int] = set()
    for line in stdout.splitlines():
        text = line.strip()
        if not text:
            continue
        pid = _decimal_pid(text)
        if pid is None:
            return None
        pids.add(pid)
    if len(pids) != 1:
        return None
    return next(iter(pids))


def parse_launchd_job(stdout: str) -> LaunchdJob | None:
    """launchd's job pid and argv from one ``launchctl print`` body."""
    pid = _decimal_pid(launchctl_print_field(stdout, "pid").strip())
    if pid is None:
        return None
    arguments = parse_launchctl_print_arguments(stdout)
    if arguments is None or not (1 <= len(arguments) <= MAX_JOB_ARGUMENTS):
        return None
    if any(
        not argument
        or len(argument) > MAX_PATH_CHARS
        or "\x00" in argument
        for argument in arguments
    ):
        return None
    return LaunchdJob(pid=pid, arguments=tuple(arguments))


def parse_ps_ppid(stdout: str) -> int | None:
    """One ``ps -o ppid=`` parent pid, or ``None`` for a non-answer."""
    text = stdout.strip()
    if not text or "\n" in text:
        return None
    return _decimal_pid(text)


def _reap_bounded(process: subprocess.Popen[bytes]) -> None:
    """Best-effort kill and reap of a bounded query; never raises."""
    try:
        if process.poll() is None:
            process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _read_bounded_stdout(
    process: subprocess.Popen[bytes], deadline: float, limit: int
) -> bytes | None:
    """Read stdout only up to ``limit`` while the child runs, or ``None``.

    ``selectors.DefaultSelector`` waits for readability against the *absolute*
    deadline (using poll/kqueue where available, so it does not hit
    ``FD_SETSIZE``) and ``os.read`` never asks for more than one byte past the
    cap, so an endlessly-writing child is killed instead of filling memory. A
    pipe that drips is bounded by the same deadline rather than one timeout per
    read.
    """
    stream = process.stdout
    if stream is None:
        return None
    descriptor = stream.fileno()
    chunks: list[bytes] = []
    total = 0
    try:
        selector = selectors.DefaultSelector()
    except (OSError, ValueError):
        return None
    try:
        try:
            selector.register(descriptor, selectors.EVENT_READ)
        except (OSError, KeyError, ValueError):
            return None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                events = selector.select(remaining)
            except (OSError, ValueError):
                return None
            if not events:
                return None
            try:
                chunk = os.read(descriptor, min(4096, limit + 1 - total))
            except OSError:
                return None
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                return None
        return b"".join(chunks)
    finally:
        selector.close()


def _bounded_run(
    argv: tuple[str, ...], timeout: float
) -> subprocess.CompletedProcess[str] | None:
    """Run one fixed, shell-free query and capture at most the output cap.

    Output is bounded *during* capture: the child's stdout is drained through a
    readability loop capped at :data:`MAX_ADAPTER_OUTPUT_BYTES`, and the child is
    killed when the absolute deadline or the cap is reached. ``subprocess.run``'s
    unbounded ``capture_output`` is deliberately not used.
    """
    if sys.platform != "darwin":
        return None
    if type(timeout) not in (int, float) or not (
        0 < float(timeout) <= JOB_PROBE_TIMEOUT_SECONDS
    ):
        return None
    deadline = time.monotonic() + float(timeout)
    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            shell=False,
            env=dict(_SAFE_ENV),
        )
    except OSError:
        return None
    try:
        raw = _read_bounded_stdout(process, deadline, MAX_ADAPTER_OUTPUT_BYTES)
        if raw is None:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            return None
        try:
            stdout = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return subprocess.CompletedProcess(list(argv), returncode, stdout, "")
    finally:
        if process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass
        _reap_bounded(process)


def _run_adapter(
    runner: BoundedRunner, argv: tuple[str, ...], timeout: float
) -> subprocess.CompletedProcess[str] | None:
    try:
        return runner(argv, timeout)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None


def _lsof_listeners(port: int, runner: BoundedRunner) -> str | None:
    """The bounded ``lsof`` listener query's stdout, or ``None`` when it failed."""
    if type(port) is not int or not (1 <= port <= 65535):
        return None
    completed = _run_adapter(
        runner,
        (LSOF, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"),
        ADAPTER_TIMEOUT_SECONDS,
    )
    if completed is None or completed.returncode != 0:
        return None
    return completed.stdout or ""


def _listener_pid(port: int, runner: BoundedRunner) -> int | None:
    stdout = _lsof_listeners(port, runner)
    return None if stdout is None else parse_listener_pid(stdout)


def parse_listener_pids(stdout: str) -> frozenset[int] | None:
    """Every distinct ``lsof -t`` pid, or ``None`` for junk or no listener at all.

    Unlike :func:`parse_listener_pid`, several different pids are all returned:
    the caller decides which of them it owns.
    """
    pids: set[int] = set()
    for line in stdout.splitlines():
        text = line.strip()
        if not text:
            continue
        pid = _decimal_pid(text)
        if pid is None:
            return None
        pids.add(pid)
    if not pids:
        return None
    return frozenset(pids)


def wizard_listener_pids(port: int) -> frozenset[int]:
    """Every pid with a listening TCP socket on ``port``, IPv4 and IPv6.

    The production lookup for the wizard child's reported port. It is bounded
    and read-only like the rest of this module. An empty set means the query
    failed or found nothing, and the coordinator refuses an empty answer.
    """
    stdout = _lsof_listeners(port, _bounded_run)
    pids = None if stdout is None else parse_listener_pids(stdout)
    return frozenset() if pids is None else pids


def _launchd_job(label: str, runner: BoundedRunner) -> LaunchdJob | None:
    uid = _getuid()
    if uid is None:
        return None
    if (
        not isinstance(label, str)
        or not (1 <= len(label) <= MAX_LABEL_CHARS)
        or not _LABEL_RE.fullmatch(label)
    ):
        return None
    completed = _run_adapter(
        runner,
        (LAUNCHCTL, "print", f"gui/{uid}/{label}"),
        JOB_PROBE_TIMEOUT_SECONDS,
    )
    if completed is None or completed.returncode != 0:
        return None
    return parse_launchd_job(completed.stdout or "")


def _ancestor_pids(pid: int, runner: BoundedRunner) -> tuple[int, ...] | None:
    if _pid(pid) is None:
        return None
    chain = [pid]
    current = pid
    for _ in range(MAX_ANCESTOR_HOPS):
        completed = _run_adapter(
            runner,
            (PS, "-o", "ppid=", "-p", str(current)),
            ADAPTER_TIMEOUT_SECONDS,
        )
        if completed is None or completed.returncode != 0:
            return None
        parent = parse_ps_ppid(completed.stdout or "")
        if parent is None:
            return None
        if parent <= 1:
            return tuple(chain)
        if parent in chain:
            return None
        chain.append(parent)
        current = parent
    return tuple(chain)


def _proc_pidpath(pid: int) -> str | None:
    """The kernel's executable path for ``pid`` through macOS ``libproc``.

    Validated against the local SDK: ``libproc.h`` declares
    ``int proc_pidpath(int, void *, uint32_t)`` and ``sys/proc_info.h`` defines
    ``PROC_PIDPATHINFO_MAXSIZE`` as ``4 * MAXPATHLEN`` (4096). The return value is
    handled conservatively for both documented conventions (with or without a
    trailing NUL); a non-normalized, non-UTF-8 or NUL-bearing answer is refused.
    This is the authoritative path lookup; no mapping-list heuristic is used.
    The library is the pinned ``/usr/lib/libproc.dylib`` rather than a
    ``ctypes.util.find_library`` lookup, which can spawn developer tools.
    """
    if sys.platform != "darwin" or _pid(pid) is None:
        return None
    try:
        library = ctypes.CDLL(LIBPROC, use_errno=True)
        function = library.proc_pidpath
    except (OSError, AttributeError):
        return None
    function.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    function.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(PROC_PIDPATHINFO_MAXSIZE)
    try:
        length = int(function(pid, buffer, len(buffer)))
    except (OSError, TypeError, ValueError):
        return None
    if length <= 0 or length > PROC_PIDPATHINFO_MAXSIZE:
        return None
    raw = buffer.raw[:length]
    if raw.endswith(b"\x00"):
        raw = raw[:-1]
    elif length == PROC_PIDPATHINFO_MAXSIZE:
        # A full buffer with no terminator cannot be distinguished from a
        # truncated path; an unknown path is not a proven path.
        return None
    if not raw or b"\x00" in raw:
        return None
    try:
        path = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        return _absolute_path(Path(path))
    except (TypeError, ValueError):
        return None


def _http_deadline(timeout: float) -> float | None:
    if type(timeout) not in (int, float) or not (
        0 < float(timeout) <= MAX_PROBE_TIMEOUT_SECONDS
    ):
        return None
    return time.monotonic() + float(timeout)


def _is_loopback_address(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if parsed.is_loopback:
        return True
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return parsed.ipv4_mapped.is_loopback
    return False


def _connect_loopback(port: int, deadline: float) -> socket.socket | None:
    """Connect only to a loopback literal and re-check the peer address."""
    for host in ("127.0.0.1", "::1"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            sock = socket.create_connection((host, port), timeout=remaining)
        except OSError:
            continue
        try:
            address = sock.getpeername()[0]
            if not _is_loopback_address(address):
                sock.close()
                return None
        except (OSError, ValueError, TypeError):
            sock.close()
            return None
        return sock
    return None


def _recv_headers(
    sock: socket.socket, deadline: float
) -> tuple[bytes, bytes] | None:
    buffer = b""
    while len(buffer) < MAX_HTTP_HEADER_BYTES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(min(4096, MAX_HTTP_HEADER_BYTES - len(buffer)))
        except OSError:
            return None
        if not chunk:
            return None
        buffer += chunk
        index = buffer.find(b"\r\n\r\n")
        if index >= 0:
            return buffer[: index + 4], buffer[index + 4 :]
    return None


def _parse_response_head(head: bytes) -> tuple[int, int | None] | None:
    """Parse a strict 2xx response into ``(status, content_length)``.

    A 1xx/3xx/4xx/5xx status, a malformed status line, an over-long or duplicate
    header, a chunked transfer coding, or a body advertised above the cap is
    refused. No redirect is ever followed.
    """
    lines = head[:-4].split(b"\r\n")
    if not lines or not lines[0] or len(lines[0]) > MAX_HTTP_STATUS_LINE_BYTES:
        return None
    match = re.fullmatch(rb"HTTP/1\.[01] ([0-9]{3})(?: [^\r\n]*)?", lines[0])
    if match is None:
        return None
    status = int(match.group(1))
    if not 200 <= status < 300:
        return None
    if len(lines) - 1 > MAX_HTTP_HEADER_COUNT:
        return None
    content_length: int | None = None
    transfer_encoding: bytes | None = None
    seen: set[bytes] = set()
    for line in lines[1:]:
        if not line:
            return None
        name, separator, value = line.partition(b":")
        if not separator or not name or name != name.strip():
            return None
        lowered = name.lower()
        if not lowered or any(byte < 0x21 or byte > 0x7E for byte in lowered):
            return None
        stripped = value.strip()
        if lowered in {b"content-length", b"transfer-encoding"}:
            if lowered in seen:
                return None
            seen.add(lowered)
            if lowered == b"content-length":
                if not re.fullmatch(rb"[0-9]{1,10}", stripped):
                    return None
                number = int(stripped)
                if str(number).encode("ascii") != stripped:
                    return None
                if number > MAX_STARTUP_RESPONSE_BYTES:
                    return None
                content_length = number
            else:
                transfer_encoding = stripped.lower()
    if transfer_encoding is not None:
        if transfer_encoding != b"identity" or content_length is not None:
            return None
    return status, content_length


def _read_body(
    sock: socket.socket,
    deadline: float,
    content_length: int | None,
    leftover: bytes,
) -> bytes | None:
    if content_length is not None:
        if len(leftover) > content_length:
            return None
        body = bytearray(leftover)
        while len(body) < content_length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            sock.settimeout(remaining)
            try:
                chunk = sock.recv(min(4096, content_length - len(body)))
            except OSError:
                return None
            if not chunk:
                return None
            body.extend(chunk)
        return bytes(body)
    body = bytearray(leftover)
    if len(body) > MAX_STARTUP_RESPONSE_BYTES:
        return None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(4096)
        except OSError:
            return None
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > MAX_STARTUP_RESPONSE_BYTES:
            return None


def http_startup_status(port: int, timeout: float) -> bytes | None:
    """One strict, bounded loopback GET of ``/api/startup-status``, or ``None``.

    The request goes to ``127.0.0.1``/``::1`` only and the peer address is
    re-checked. It never uses a proxy, never follows a redirect, rejects a
    chunked or over-large body, and enforces one *absolute* deadline across
    connect, send, headers and body, so a byte-dripping server cannot extend it.
    """
    if type(port) is not int or not (1 <= port <= 65535):
        return None
    deadline = _http_deadline(timeout)
    if deadline is None:
        return None
    sock = _connect_loopback(port, deadline)
    if sock is None:
        return None
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        sock.settimeout(remaining)
        request = (
            f"GET /api/startup-status HTTP/1.1\r\n"
            f"Host: localhost:{port}\r\n"
            "Accept: application/json\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        try:
            sock.sendall(request)
        except OSError:
            return None
        headers = _recv_headers(sock, deadline)
        if headers is None:
            return None
        head, leftover = headers
        parsed = _parse_response_head(head)
        if parsed is None:
            return None
        _status, content_length = parsed
        return _read_body(sock, deadline, content_length, leftover)
    except (OSError, ValueError, TypeError):
        return None
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _sanitized_run(
    argv: list[str], **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    """The runner ``verify_owned_host`` uses: fixed argv, no inherited loader env."""
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        argv,
        env=dict(_SAFE_ENV),
        stdin=subprocess.DEVNULL,
        **kwargs,
    )
    return completed


def _verify_owned_host(
    bundle_path: Path, ownership_path: Path
) -> HostOwnership:
    """Cryptographically re-inspect the owned host immediately before readiness.

    This calls :func:`ciao.server_host.verify_owned_host`, which reads the
    owner-only record and re-inspects the bundle/signature through bounded,
    shell-free ``codesign`` probes. It does not trust a caller-supplied
    dataclass or a prior snapshot. The verifier is injectable only through the
    private ``_prove_native_readiness`` seam for tests.
    """
    return verify_owned_host(
        bundle_path,
        ownership_path=ownership_path,
        runner=_sanitized_run,
    )


def make_native_readiness_deps(
    *,
    runner: BoundedRunner | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    http: Callable[[int, float], bytes | None] | None = None,
    executable_path: Callable[[int], str | None] | None = None,
) -> NativeReadinessDeps:
    """Build the real read-only adapters the native coordinator uses.

    The subprocess adapters use absolute macOS binaries and fixed argv, a
    sanitized environment and a bounded read and never use a shell. ``runner``,
    ``http`` and ``executable_path`` are seams so the adapters can be exercised
    without a real process or socket. Host verification is not part of the
    public deps; the proof always calls the real verifier.
    """
    bounded = _bounded_run if runner is None else runner
    http_reader = http_startup_status if http is None else http
    kernel_path = _proc_pidpath if executable_path is None else executable_path
    return NativeReadinessDeps(
        listener_pid=lambda port: _listener_pid(port, bounded),
        startup_status=http_reader,
        job=lambda label: _launchd_job(label, bounded),
        ancestor_pids=lambda pid: _ancestor_pids(pid, bounded),
        executable_path=kernel_path,
        clock=clock,
        sleep=sleep,
    )
