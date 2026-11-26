"""Read-only classification and owned takeover of a 1.x install (#3129).

``plan()`` answers one question: may the app's bundled engine take over this
Mac's launchd-managed engine, and which installer-owned leftovers could move to
the Trash once the bundled engine has proven healthy? It never writes, stops,
boots out or deletes anything.

``apply``/``finish``/``rollback`` own every mutation, journalled under
``state_dir()/takeover/<id>/`` so each step is idempotent on a resume: the
plist is copied into the takeover entry and its hash verified before the old
job is booted out of launchd, the plist is unlinked only once that backup is
proven, and the workspace pointer is written last. ``finish`` demands the
bundled engine's own proofs (pointer, ready record, listener, lock owner)
before naming anything for the Trash, and ``rollback`` restores the old plist
and service untouched when the takeover cannot become healthy. The Trash
itself belongs to the app (S7): these commands only name candidates.

State machine (every transition is journalled; the lock is held throughout)
---------------------------------------------------------------------------

Keep these invariants when changing this module: a resume never trusts what an
earlier run proved (it re-proves the old engine and the plist before every
destructive step), a state that cannot be reasoned about refuses and leaves
everything as it is, and the journal is never read through the live ``.env``.

======================  ==========================  ================================================  =================================
state                   allowed commands            what each does                                    a crash here
======================  ==========================  ================================================  =================================
(none)                  apply                       plan() says takeover: journal ``created``, back   a ``created`` skeleton stopped
                                                    up the plist, then ``started``                    nothing; the next apply discards it
started                 apply, rollback             apply: bootout + wait for silence, else           backup re-proven, stop repeats
                                                    bootstrap back and ``rolled_back``; rollback:
                                                    restore/keep plist, prove old engine
stopping                apply, rollback             apply: re-prove the old engine is gone (else      re-proves, then unlinks the
                                                    ``old_engine_back``), unlink the plist            plist (or sees it gone)
plist_removed           apply, rollback             apply: re-prove engine gone + plist absent,       writes the pointer again
                                                    write the pointer
pointer_written         apply, finish, rollback     apply: plist absent and job unloaded -> ok        none to repair
                                                    (else ``old_engine_back``); finish: two stable
                                                    health reads and the old service still gone
                                                    (else ``old_engine_back``) -> ``healthy``
healthy                 apply, finish, rollback     apply: ok; finish: lists the Trash candidates    nothing was trashed by us
                                                    (marks ``trash_offered`` first), ``--done`` ->
                                                    ``finished``; rollback: refused once
                                                    ``trash_offered``
finished/rolled_back    finish, rollback            terminal; finish: finished -> ok, empty           terminal
                                                    list, rolled_back -> already_settled;
                                                    rollback: already_settled; apply: plan()
                                                    skips a settled journal, plans afresh
======================  ==========================  ================================================  =================================

``rollback`` is allowed from every non-terminal state: it refuses while the
bundled engine holds the lock (the OLD engine's lock is told apart by the
loaded job's pid), restores the plist byte-identical, bootstraps, and ends
``rolled_back`` only when the restored job's pid is the listener's and the lock
owner's and the pointer is cleared. An unexpected error after the first
mutation is answered with ``reason=takeover_error`` plus ``id`` and ``state``.

Coupling rule (#3130 deletes the old installers): this module's own imports
are only ``ciao`` (for ``__version__``), ``ciao.app_engine`` (the workspace
pointer and its private-record writes), ``ciao.macos_service``,
``ciao.os_support.definition``, ``ciao.os_support.locks`` and
``ciao.os_support.private`` (function-local, POSIX-only ``fcntl``), and
``ciao.package_version``. The old-install
paths, the shim marker, the receipt and the hosted ``Ciaobot Server serve
--python <abs>`` argv are literal constants parsed locally, so it never imports
``server_host``, ``install_receipt`` or friends itself.
That is a statement about this file only: ``ciao.macos_service`` still imports
``ciao.server_host`` (``parse_service_command``), so deleting ``server_host``
also needs that import cut from ``macos_service`` first. The test checks the
direct imports, not the transitive closure.

Every read of a file the old install controls (plist, receipt, ``.env``, the
engine lock, the registry) is a no-follow, bounded, regular-file-only read, and
every HTTP probe goes to ``127.0.0.1`` with no proxy, no redirect, a bounded
body and one overall deadline. A file that exists but cannot be read that way
is a refusal, never an empty answer, because an empty answer would read as
"nothing to protect".

Every refusal is a fixed code in ``reason``; paths go in fields, never in the
reason.
"""

from __future__ import annotations

import errno
import hashlib
import http.client
import json
import os
import plistlib
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from xml.parsers.expat import ExpatError

import ciao
from ciao import app_engine, macos_service, upgrade_preflight
from ciao.macos_service import (
    LAUNCHCTL,
    SERVER_LABEL,
    SERVER_LOCK_NAME,
    DesktopRuntime,
    RestartBlockersUnavailable,
    Runner,
    _getuid,
    lock_owner_is_fresh,
    parse_dotenv,
    runtime_from_plist,
)
from ciao.os_support.definition import DefinitionReadError, read_bounded_file, read_definition
from ciao.package_version import _version_key

#: The oldest engine whose startup-status and lock carry what the takeover
#: needs to prove the listener is the engine this plist started.
MIN_TAKEOVER_VERSION = "1.10.0"

#: Marker the old installer writes into the ``~/.local/bin/ciao`` shim.
SHIM_MARKER = "# Ciaobot shim (managed by the Ciaobot installer)"
_HOST_APP_NAME = "Ciaobot Server.app"
_HOST_EXECUTABLE = "Ciaobot Server"
_LEGACY_DESKTOP_MARKER = "Ciaobot.app/Contents/Resources/ciao-runtime"
_ENV_ROOT_NAME = "ciaobot"
_DEFAULT_PORT = 8443
_MAX_SMALL_FILE = 64 * 1024
_MAX_DOTENV = 1024 * 1024
_MAX_REGISTRY = 1024 * 1024
#: One whole HTTP probe (connect, headers, body) must finish inside this.
_PROBE_DEADLINE_S = 2.0
#: The startup-status and restart-blockers answers are a few hundred bytes.
_PROBE_MAX_BYTES = 64 * 1024
#: ``launchctl print`` exit status for a job that is not loaded.
_JOB_ABSENT_STATUS = 113
_UV_NAME = "uv"

#: Plist environment keys that move user data. Only safe when the workspace
#: ``.env`` sets the same VALUE, because the bundled engine reads the ``.env``
#: and never this plist. ``CIAO_WORKSPACES`` is a legacy list, not a path.
_DATA_LOCATION_KEYS = (
    "CIAO_VAULT_ROOT",
    "CIAO_MEMORY_DIR",
    "CIAO_VAULT_DIR",
    "CIAO_WORKSPACES",
)
_PATH_DATA_KEYS = ("CIAO_VAULT_ROOT", "CIAO_MEMORY_DIR", "CIAO_VAULT_DIR")
_STANDARD_ENV_KEYS = frozenset(
    {"CIAO_WORKSPACE", "CIAO_RUNTIME_ROOT", "CIAO_PORT", "PATH", "HOME", "LANG"}
)

ACTION_NONE = "none"
ACTION_TAKEOVER = "takeover"
ACTION_RESUME = "resume"
ACTION_REFUSE = "refuse"

_FINISHED_STATES = frozenset({"finished", "rolled_back"})

EXIT_OK = 0
EXIT_REFUSED = 3

# --- the takeover journal (#3129 S4b) ------------------------------------------

JOURNAL_SCHEMA = 1
JOURNAL_NAME = "journal.json"

#: The states a takeover walks through, in order. ``started`` means the old
#: plist is already backed up inside the takeover entry (before anything is
#: stopped); ``stopping`` that the old job is out of launchd and silent;
#: ``plist_removed`` that the LaunchAgents definition is gone; and
#: ``pointer_written`` that the bundled engine's workspace pointer names the
#: old workspace — the point at which the app may start its own engine.
STATE_STARTED = "started"
STATE_STOPPING = "stopping"
STATE_PLIST_REMOVED = "plist_removed"
STATE_POINTER_WRITTEN = "pointer_written"
STATE_HEALTHY = "healthy"
STATE_FINISHED = "finished"
STATE_ROLLED_BACK = "rolled_back"

_APPLY_STATES = (STATE_STARTED, STATE_STOPPING, STATE_PLIST_REMOVED, STATE_POINTER_WRITTEN)
_KNOWN_STATES = frozenset((*_APPLY_STATES, STATE_HEALTHY, STATE_FINISHED, STATE_ROLLED_BACK))
_STATE_ORDER = {name: index for index, name in enumerate(_APPLY_STATES)}

#: Proofs whose candidate may be a symlink at the top level (the shim and the
#: uv tool symlink are links by design; everything else must be a real file).
_SYMLINK_PROOFS = frozenset({"uv_tool_symlink", "installer_shim"})

#: Every launchctl call gets captured output and one bounded wait.
_LAUNCHCTL_TIMEOUT_S = 30.0
#: The old engine's own restart gate gives a busy engine this long to die.
_APPLY_TEARDOWN_S = macos_service.RELOAD_TEARDOWN_TIMEOUT_S
#: The rollback bootstrap is allowed this long to bring the old engine back.
_ROLLBACK_START_S = 60.0
_POLL_S = macos_service.RELOAD_POLL_INTERVAL_S

#: Fixed refusal codes of the transaction, named once for the app's copy.
REF_NOT_TAKEOVER = "not_takeover"
REF_NO_JOURNAL = "no_journal"
REF_ALREADY_SETTLED = "already_settled"
REF_SHA_MISMATCH = "sha_mismatch"
REF_DID_NOT_STOP = "old_engine_did_not_stop"
REF_BACKUP_UNUSABLE = "backup_unusable"
REF_NOT_HEALTHY = "not_healthy"
REF_BUNDLED_RUNNING = "bundled_engine_running"
REF_PLIST_CONFLICT = "plist_conflict"
REF_DID_NOT_START = "old_engine_did_not_start"
REF_OLD_ENGINE_BACK = "old_engine_back"
REF_TRASH_OFFERED = "trash_offered"
REF_NOT_ROLLED_BACK = "pointer_not_cleared"
REF_JOURNAL_TOO_LARGE = "journal_too_large"
REF_NO_PROTECTED = "protected_missing"
REF_ERROR = "takeover_error"


@dataclass(frozen=True)
class TrashCandidate:
    path: str
    # "server_host_program" | "server_host_record" | "receipt_env" | "receipt"
    # | "installer_shim" | "uv_tool_symlink"
    proof: str


@dataclass(frozen=True)
class TakeoverPlan:
    action: str  # "none" | "takeover" | "resume" | "refuse"
    reason: str = ""
    label: str = SERVER_LABEL
    plist: str = ""
    plist_sha256: str = ""
    workspace: str = ""
    runtime_root: str = ""
    port: int = _DEFAULT_PORT
    program_kind: str = ""  # "server_host" | "installer_env" | "checkout" | "python"
    old_version: str = ""
    old_running: bool = False
    #: Chats an old engine would interrupt. -1 on a resume whose old engine
    #: answers but whose chats cannot be counted: the app asks without a number.
    active_chats: int = 0
    trash: tuple[TrashCandidate, ...] = ()
    left_in_place: tuple[str, ...] = ()
    dropped_env: tuple[str, ...] = ()
    resume_id: str = ""
    #: Which 1.10 step to finish, set with the `unfinished_migration` refusal.
    advice: str = ""
    #: The plan-time protected paths, persisted in the journal so finish()
    #: filters the Trash candidates against the same inputs plan() used. Not
    #: part of the plan JSON the app reads.
    protected: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        document = asdict(self)
        document.pop("protected")
        return document


def _refuse(reason: str, label: str, **fields: Any) -> TakeoverPlan:
    return TakeoverPlan(action=ACTION_REFUSE, reason=reason, label=label, **fields)


# --- the app engine's own state (contract of #3129 section 2) -------------------


def state_dir() -> Path:
    """``~/Library/Application Support/Ciaobot/engine``, derived from HOME."""
    return Path.home() / "Library" / "Application Support" / "Ciaobot" / "engine"


def read_pointer() -> dict[str, Any] | None:
    """The engine-owned ``workspace.json``, or ``None`` when absent or invalid."""
    value = _read_json(state_dir() / "workspace.json")
    if value is None or value.get("schema") != 1 or not value.get("workspace"):
        return None
    return value


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = read_definition(path, max_bytes=_MAX_SMALL_FILE).data
        value = json.loads(raw.decode("utf-8"))
    except (DefinitionReadError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# --- isolation guard -----------------------------------------------------------


def _is_darwin() -> bool:
    return sys.platform == "darwin"


def _real_home() -> Path | None:
    """This account's real home directory, ignoring ``$HOME``."""
    # The literal comparison narrows the platform for mypy; ``_is_darwin`` is the
    # seam a test replaces without flipping ``sys.platform`` process-wide.
    if sys.platform != "darwin" or not _is_darwin():
        return None
    import pwd

    try:
        return Path(pwd.getpwuid(_getuid()).pw_dir)
    except KeyError:
        return None


def _isolation_refusal(label: str) -> str:
    real = _real_home()
    if real is None:
        return "unsupported_platform"
    isolated = Path.home().resolve() != real.resolve()
    if isolated and label == SERVER_LABEL:
        return "isolated_home_needs_test_label"
    if not isolated and label != SERVER_LABEL:
        return "test_label_needs_isolated_home"
    return ""


# --- journal -------------------------------------------------------------------


_UNSAFE = "\x00unsafe"
_UNREADABLE = "\x00unreadable"
_JOURNALLESS = "\x00journal-less"
#: States in which nothing of the old install was touched yet.
STATE_CREATED = "created"
_YOUNG_STATES = frozenset({STATE_CREATED, _JOURNALLESS})


def _private_directory(path: Path) -> bool:
    """A real (not symlinked) directory, this user's, with no group/world access."""
    try:
        status = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISDIR(status.st_mode)
        and status.st_uid == _getuid()
        and status.st_mode & 0o077 == 0
    )


def _takeover_state_unsafe() -> bool:
    """Whether ``takeover/`` exists but is a symlink, a file or not private."""
    root = state_dir() / "takeover"
    try:
        os.lstat(root)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return not _private_directory(root)


def _read_journal_file(entry: Path) -> dict[str, Any] | None:
    """The entry's journal through the strict private reader, or ``None``."""
    return app_engine.read_private_json(entry / "journal.json", label="the takeover journal")


def _entry_state(entry: Path) -> str | None:
    """What a takeover entry holds: its journal state, a marker, or ``None``.

    ``None`` is not an entry at all (a stray file). A symlinked or non-private
    entry, and a journal that cannot be read strictly, are unfinished work: the
    plan never trusts them and never clears them.
    """
    try:
        status = os.lstat(entry)
    except OSError:
        return _UNSAFE
    if stat.S_ISLNK(status.st_mode):
        return _UNSAFE
    if not stat.S_ISDIR(status.st_mode):
        return None
    if not _private_directory(entry):
        return _UNSAFE
    try:
        os.lstat(entry / "journal.json")
    except FileNotFoundError:
        return _JOURNALLESS
    except OSError:
        return _UNSAFE
    journal = _read_journal_file(entry)
    state = journal.get("state") if journal is not None else None
    return state if isinstance(state, str) else _UNREADABLE


def _unfinished_journal() -> str:
    """The id of the newest takeover whose journal is not finished or rolled back.

    A journal-less entry and one still in its ``created`` skeleton state have
    stopped nothing: they are not unfinished work (apply discards them).
    """
    root = state_dir() / "takeover"
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return ""
    for name in reversed(names):
        if re.fullmatch(r"[A-Za-z0-9-]{1,64}", name) is None:
            continue
        state = _entry_state(root / name)
        if state is None or state in _FINISHED_STATES or state in _YOUNG_STATES:
            continue
        return name
    return ""


# --- program classification ----------------------------------------------------


def _absolute(value: str) -> bool:
    return value.startswith("/") and "\x00" not in value


def _hosted_python(argv: list[str]) -> str | None:
    """The ``--python`` of exactly ``<abs>/Ciaobot Server serve --python <abs>``."""
    if (
        len(argv) == 4
        and argv[1] == "serve"
        and argv[2] == "--python"
        and _absolute(argv[0])
        and PurePosixPath(argv[0]).name == _HOST_EXECUTABLE
        and _absolute(argv[3])
    ):
        return argv[3]
    return None


def _direct_python(argv: list[str]) -> str | None:
    """The engine python of a direct ``python [-I] -m ciao[.cli] run`` or ``<env>/bin/ciao run``."""
    if not argv or not _absolute(argv[0]):
        return None
    name = PurePosixPath(argv[0]).name
    rest = argv[1:]
    if name == "ciao" and rest == ["run"]:
        return str(PurePosixPath(argv[0]).parent / "python")
    if re.fullmatch(r"python(?:3(?:\.\d+)?)?", name):
        if rest and rest[0] == "-I":
            rest = rest[1:]
        if len(rest) == 3 and rest[0] == "-m" and rest[1] in ("ciao", "ciao.cli") and rest[2] == "run":
            return argv[0]
    return None


def _env_root(python: str) -> Path:
    return Path(os.path.abspath(python)).parent.parent


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _checkout_root(python: str) -> Path | None:
    """The git checkout holding ``python``, found below the home directory."""
    home = Path.home()
    for parent in Path(os.path.abspath(python)).parents:
        if parent == home or parent == parent.parent:
            return None
        if (parent / ".git").exists() and (parent / "pyproject.toml").is_file():
            return parent
    return None


def _read_receipt() -> dict[str, Any] | None:
    value = _read_json(Path.home() / ".local" / "state" / "ciaobot" / "install-receipt.json")
    # A relative interpreter would be resolved against the caller's cwd, so a
    # receipt that names one proves nothing about any environment.
    if value is None or not isinstance(value.get("python"), str) or not _absolute(value["python"]):
        return None
    return value


# --- hardened reads and probes --------------------------------------------------


def _read_optional(path: Path, *, max_bytes: int, label: str) -> bytes | None:
    """The bytes of a regular file, ``None`` when there is no such entry.

    Raises ``DefinitionReadError`` when something is there but is a symlink, a
    FIFO or other special file, too large, or changing: an unreadable answer is
    never allowed to read as an empty one.
    """
    if not os.path.lexists(path):
        return None
    return read_bounded_file(path, max_bytes=max_bytes, label=label).data


def _read_dotenv(path: Path) -> dict[str, str] | None:
    """The parsed workspace ``.env`` (``{}`` when absent), ``None`` when unreadable."""
    try:
        data = _read_optional(path, max_bytes=_MAX_DOTENV, label="the workspace .env")
        return {} if data is None else parse_dotenv(data.decode("utf-8"))
    except (DefinitionReadError, UnicodeDecodeError):
        return None


def _read_lock_owner(runtime: DesktopRuntime) -> dict[str, Any] | None:
    """The engine's ``server.lock`` metadata, or ``None`` when absent or unreadable."""
    try:
        data = _read_optional(
            Path(runtime.runtime_root) / SERVER_LOCK_NAME,
            max_bytes=_MAX_SMALL_FILE,
            label="the engine lock",
        )
        value = None if data is None else json.loads(data.decode("utf-8"))
    except (DefinitionReadError, ValueError):
        return None
    return value if isinstance(value, dict) else None


read_lock_owner = _read_lock_owner


class _ProbeFailed(Exception):
    """The loopback answer was not a small 2xx JSON document in time."""


def _get_json(port: int, path: str) -> Any:
    """GET ``http://127.0.0.1:<port><path>`` as JSON.

    ``http.client`` takes no proxy from the environment and follows no redirect
    (a 3xx is just a non-2xx answer here). The body is capped, and one deadline
    covers connect, headers and body: a timer shuts the socket at the deadline,
    so a peer that drips bytes cannot keep the plan waiting.
    """
    deadline_s = _PROBE_DEADLINE_S
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=deadline_s)

    def expire() -> None:
        sock = connection.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    timer = threading.Timer(deadline_s, expire)
    timer.daemon = True
    timer.start()
    started = time.monotonic()
    try:
        connection.request("GET", path, headers={"Accept": "application/json"})
        response = connection.getresponse()
        if not 200 <= response.status < 300:
            raise _ProbeFailed(f"HTTP {response.status}")
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > _PROBE_MAX_BYTES):
            raise _ProbeFailed("unacceptable Content-Length")
        body = bytearray()
        while True:
            chunk = response.read1(_PROBE_MAX_BYTES + 1 - len(body))
            if not chunk:
                break
            body += chunk
            if len(body) > _PROBE_MAX_BYTES:
                raise _ProbeFailed("response too large")
            if time.monotonic() - started > deadline_s:
                raise _ProbeFailed("deadline passed")
        if time.monotonic() - started > deadline_s:
            raise _ProbeFailed("deadline passed")
        return json.loads(bytes(body).decode("utf-8"))
    finally:
        timer.cancel()
        connection.close()


_PROBE_ERRORS = (_ProbeFailed, OSError, http.client.HTTPException, ValueError)


def engine_identity(port: int) -> dict[str, Any] | None:
    """The startup-status payload of whatever answers ``port``, if it is an engine."""
    try:
        payload = _get_json(port, "/api/startup-status")
    except _PROBE_ERRORS:
        return None
    if not isinstance(payload, dict):
        return None
    startup_id = payload.get("startup_id")
    if not isinstance(startup_id, str) or not startup_id:
        return None
    return payload


def restart_blockers(port: int) -> list[dict[str, Any]]:
    """Chats a restart would interrupt; ``[]`` when nothing listens, else raise on doubt."""
    try:
        payload = _get_json(port, "/api/admin/restart-blockers")
    except ConnectionRefusedError:
        return []
    except _PROBE_ERRORS as exc:
        raise RestartBlockersUnavailable(str(exc) or type(exc).__name__) from exc
    values = payload.get("restart_blockers") if isinstance(payload, dict) else None
    if not isinstance(values, list):
        raise RestartBlockersUnavailable("unexpected answer from the engine")
    return [
        {"chat_id": str(value["chat_id"]), "reasons": [str(r) for r in value.get("reasons") or []]}
        for value in values
        if isinstance(value, dict) and value.get("chat_id")
    ]


def _pid_alive(pid: int) -> bool:
    """Whether ``pid`` is a live process. Signal 0 delivers nothing."""
    if not _is_darwin() or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _default_runner() -> Runner:
    """The one place the real ``subprocess.run`` launchctl runner is chosen.

    Every public entry point and every helper that needs a runner and was not
    handed one resolves it here, so the test suite can replace this single
    factory and no code path can reach the real ``launchctl`` unnoticed.
    """
    return subprocess.run


def _job_state(label: str, runner: Runner) -> tuple[bool, int | None]:
    """``(loaded, pid)`` from a read-only ``launchctl print``.

    Only a confirmed "not loaded" answer reads as not loaded: a probe that
    cannot run or answers anything else is treated as loaded, because the
    caller refuses on "loaded with no known version".
    """
    try:
        completed = runner(
            [LAUNCHCTL, "print", f"gui/{_getuid()}/{label}"],
            capture_output=True,
            text=True,
            timeout=macos_service.JOB_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True, None
    if completed.returncode == _JOB_ABSENT_STATUS:
        return False, None
    if completed.returncode != 0:
        return True, None
    value = macos_service.launchctl_print_field(completed.stdout or "", "pid")
    return True, int(value) if value.isdigit() else None


# --- trash candidates ----------------------------------------------------------

_STATE_ROOT = (".local", "state", "ciaobot")
#: Where each proof's candidate must really live, relative to the real HOME.
_PROOF_ROOTS: dict[str, tuple[str, ...]] = {
    "server_host_program": ("Applications",),
    "server_host_record": _STATE_ROOT,
    "receipt": _STATE_ROOT,
    "receipt_env": (".local", "share", "uv", "tools"),
    "installer_shim": (".local", "bin"),
    "uv_tool_symlink": (".local", "bin"),
}


def _inside(path: Path, other: Path) -> bool:
    return path == other or other in path.parents


def _safe(
    path: Path,
    protected: list[Path],
    *,
    expected_root: tuple[str, ...] = (),
    allow_symlink: bool = False,
) -> Path | None:
    """The normalised ``path`` when it may be offered for the Trash, else ``None``.

    Absolute, with no ``..`` (collapsing it lexically would name a different
    place when an earlier component is a symlink), strictly under HOME and
    strictly under ``expected_root`` (relative to the real HOME) AFTER every
    intermediate component is resolved, not a top-level symlink unless the proof
    allows it, and neither equal to, inside, nor an ancestor of any protected
    path. The caller must emit the returned path: it is the one that was checked.
    """
    if not path.is_absolute() or ".." in path.parts or "\x00" in str(path):
        return None
    home = Path(os.path.abspath(Path.home()))
    lexical = Path(os.path.abspath(path))
    if home not in lexical.parents:
        return None
    try:
        home_real = home.resolve()
        root_real = home_real.joinpath(*expected_root)
        is_link = lexical.is_symlink()
        if is_link and not allow_symlink:
            return None
        resolved = lexical.parent.resolve() / lexical.name if is_link else lexical.resolve()
        targets = [guarded.resolve() for guarded in protected]
    except (OSError, RuntimeError):
        return None
    if home_real not in resolved.parents or root_real not in resolved.parents:
        return None
    for target in targets:
        if _inside(resolved, target) or _inside(target, resolved):
            return None
    return lexical


def _server_host_candidates(program: str) -> list[TrashCandidate]:
    host = Path.home() / "Applications" / _HOST_APP_NAME
    if ".." in PurePosixPath(program).parts:
        return []
    bundle = PurePosixPath(program).parent.parent.parent
    if str(bundle) != str(host) or not host.exists():
        return []
    found = [TrashCandidate(str(host), "server_host_program")]
    record_path = Path.home().joinpath(*_STATE_ROOT, "server-host.json")
    record = _read_json(record_path)
    if record is not None and record.get("bundle_path") == str(host):
        found.append(TrashCandidate(str(record_path), "server_host_record"))
    return found


def _installer_candidates(python: str, receipt: dict[str, Any]) -> list[TrashCandidate]:
    if ".." in PurePosixPath(python).parts:
        return []
    env = _env_root(python)
    if env.name != _ENV_ROOT_NAME or not env.is_dir():
        return []
    found = [TrashCandidate(str(env), "receipt_env")]
    state = Path.home().joinpath(*_STATE_ROOT)
    receipt_path = state / "install-receipt.json"
    if receipt_path.exists():
        found.append(TrashCandidate(str(receipt_path), "receipt"))
    uv = receipt.get("uv")
    if isinstance(uv, str) and _absolute(uv):
        uv_path = Path(uv)
        # The uv the installer built the env with: a regular file named ``uv``
        # that the receipt places inside the installer state dir.
        if (
            uv_path.name == _UV_NAME
            and state in Path(os.path.abspath(uv_path)).parents
            and uv_path.is_file()
            and not uv_path.is_symlink()
        ):
            found.append(TrashCandidate(str(uv_path), "receipt"))
    shim = Path.home() / ".local" / "bin" / "ciao"
    if shim.is_symlink():
        try:
            if _inside(shim.resolve(), env.resolve()):
                found.append(TrashCandidate(str(shim), "uv_tool_symlink"))
        except OSError:
            pass
    elif shim.is_file():
        try:
            text = read_definition(shim, max_bytes=_MAX_SMALL_FILE).data.decode("utf-8", "replace")
        except DefinitionReadError:
            text = ""
        if SHIM_MARKER in text:
            found.append(TrashCandidate(str(shim), "installer_shim"))
    return found


# --- data locations --------------------------------------------------------------


def _expand(value: str, base: Path) -> Path | None:
    """``value`` as an absolute, normalised path: ``~`` expanded, relative to ``base``."""
    value = value.strip()
    if not value or "\x00" in value:
        return None
    try:
        path = Path(value).expanduser()
    except RuntimeError:
        path = Path(value)
    if not path.is_absolute():
        path = base / path
    return Path(os.path.abspath(path))


def _registered_vaults(document: Any) -> list[str]:
    """The ``vault_root`` (or the name) of each workspace a registry document lists."""
    if isinstance(document, dict):
        items = [{"name": name, **value} for name, value in document.items() if isinstance(value, dict)]
    elif isinstance(document, list):
        items = [item for item in document if isinstance(item, dict)]
    else:
        return []
    found: list[str] = []
    for item in items:
        name = str(item.get("name", "")).strip()
        vault = str(item.get("vault_root", name)).strip() or name
        if vault:
            found.append(vault)
    return found


def _data_location_conflict(
    plist_env: dict[str, Any], dotenv: dict[str, str], workspace: Path
) -> bool:
    """Whether the bundled engine, which reads only the ``.env``, would see other data.

    Compares VALUES, not keys: a ``.env`` key that is empty or differs from the
    plist moves the data just as much as a missing one. A plist ``HOME`` that is
    not this account's home moves every ``~`` path too.
    """
    home_value = plist_env.get("HOME")
    if home_value is not None and not (
        isinstance(home_value, str)
        and _absolute(home_value)
        and _same_dir(Path(home_value), Path.home())
    ):
        return True
    for key in _DATA_LOCATION_KEYS:
        if key not in plist_env:
            continue
        plist_value = str(plist_env[key]).strip()
        if not plist_value:
            continue  # an empty value is ignored by the engine: nothing is moved
        env_value = dotenv.get(key, "").strip()
        if not env_value:
            return True
        if key in _PATH_DATA_KEYS:
            wanted, got = _expand(plist_value, workspace), _expand(env_value, workspace)
            if wanted is None or got is None or not _same_dir(wanted, got):
                return True
        elif plist_value != env_value:
            return True
    return False


def _protected_paths(
    workspace: Path,
    runtime_root: Path,
    dotenv: dict[str, str],
    plist_env: dict[str, Any],
    registry: Any,
) -> list[Path]:
    """Everything a Trash candidate must never be, contain or sit inside.

    The workspace, the runtime root, the vault (default, ``.env`` and plist
    values, ``~`` and relative forms expanded against the right base), the
    engine state dir, ``~/.ciao`` and every other workspace the registry (and a
    legacy ``CIAO_WORKSPACES`` value) lists.
    """
    protected: list[Path] = [workspace, runtime_root, Path.home() / ".ciao", state_dir()]
    vault_base = workspace / "memory-vault"
    for key in _PATH_DATA_KEYS:
        for source in (dotenv, plist_env):
            raw = source.get(key)
            expanded = _expand(raw, workspace) if isinstance(raw, str) else None
            if expanded is not None:
                protected.append(expanded)
    dotenv_vault = _expand(dotenv.get("CIAO_VAULT_ROOT", ""), workspace)
    if dotenv_vault is not None:
        vault_base = dotenv_vault
    protected.append(vault_base)
    documents = [registry]
    for source in (dotenv, plist_env):
        legacy = source.get("CIAO_WORKSPACES")
        if isinstance(legacy, str) and legacy.strip():
            try:
                documents.append(json.loads(legacy))
            except ValueError:
                pass
    for document in documents:
        for vault in _registered_vaults(document):
            for base in (vault_base, workspace):
                expanded = _expand(vault, base)
                if expanded is not None:
                    protected.append(expanded)
    return protected


# --- the plan ------------------------------------------------------------------


def plan(
    *,
    launch_agents_dir: Path | None = None,
    label: str = SERVER_LABEL,
    runner: Runner | None = None,
    describe_resume: bool = True,
) -> TakeoverPlan:
    """Classify this Mac's old engine for takeover. Writes nothing.

    ``describe_resume`` is False for apply's own re-plan: a resumed takeover is
    then answered from its journal alone, before any probe or launchctl call.
    """
    runner = runner if runner is not None else _default_runner()
    # 0. isolation guard: a test never boots out the live engine, and production
    # always names the real label.
    refusal = _isolation_refusal(label)
    if refusal:
        return _refuse(refusal, label)

    # 1. unfinished journal wins over everything; an unsafe state dir refuses.
    if _takeover_state_unsafe():
        return _refuse("takeover_state_unsafe", label)
    resume_id = _unfinished_journal()
    if resume_id:
        if not describe_resume:
            return TakeoverPlan(action=ACTION_RESUME, label=label, resume_id=resume_id)
        return _resume_plan(resume_id, label=label, launch_agents_dir=launch_agents_dir, runner=runner)

    agents = (
        macos_service.default_launch_agents_dir()
        if launch_agents_dir is None
        else Path(launch_agents_dir).expanduser()
    )
    plist_path = agents / f"{label}.plist"

    # 2. no plist: nothing to take over.
    if not plist_path.exists() and not plist_path.is_symlink():
        return TakeoverPlan(
            action=ACTION_NONE,
            reason="already_app" if read_pointer() is not None else "",
            label=label,
        )

    # 3. one bounded, strict read. The hash, argv, environment, workspace,
    # runtime root and port below all come from this one parsed document.
    try:
        read = read_definition(plist_path)
        document = plistlib.loads(read.data)
    except (DefinitionReadError, ValueError, ExpatError):
        return _refuse("unreadable_definition", label, plist=str(plist_path))
    if not isinstance(document, dict) or document.get("Label") != label:
        return _refuse("unreadable_definition", label, plist=str(plist_path))
    known: dict[str, Any] = {"plist": str(plist_path), "plist_sha256": read.sha256}

    # 4. workspace, runtime root and registry. environ={} means no shell fallback.
    dotenv_state: dict[str, Any] = {"values": {}, "failed": False}

    def read_env(path: Path) -> dict[str, str]:
        values = _read_dotenv(path)
        if values is None:
            dotenv_state["failed"] = True
            return {}
        dotenv_state["values"] = values
        return values

    runtime: DesktopRuntime = runtime_from_plist(
        document, plist_path=plist_path, environ={}, read_env=read_env
    )
    if not runtime.workspace:
        return _refuse("no_workspace", label, **known)
    workspace = Path(runtime.workspace)
    runtime_root = Path(runtime.runtime_root)
    known.update(workspace=str(workspace), runtime_root=str(runtime_root), port=runtime.port)
    if dotenv_state["failed"]:
        return _refuse("unreadable_dotenv", label, **known)
    dotenv: dict[str, str] = dotenv_state["values"]
    if not workspace.is_dir() or not runtime_root.is_dir():
        return _refuse("no_registry", label, **known)
    try:
        registry = json.loads(
            read_definition(runtime_root / "workspaces.json", max_bytes=_MAX_REGISTRY).data.decode("utf-8")
        )
    except (DefinitionReadError, ValueError):
        return _refuse("no_registry", label, **known)

    # 5. program kind.
    argv_value = document.get("ProgramArguments")
    argv = (
        list(argv_value)
        if isinstance(argv_value, list) and argv_value and all(isinstance(a, str) for a in argv_value)
        else []
    )
    if any(_LEGACY_DESKTOP_MARKER in item for item in argv):
        return _refuse("legacy_desktop", label, **known)
    receipt = _read_receipt()
    host_python = _hosted_python(argv)
    direct_python = None if host_python is not None else _direct_python(argv)
    checkout: Path | None = None
    if host_python is not None:
        program_kind = "server_host"
    elif direct_python is not None:
        checkout = _checkout_root(direct_python)
        if checkout is not None:
            program_kind = "checkout"
        elif receipt is not None and _same_dir(_env_root(receipt["python"]), _env_root(direct_python)):
            program_kind = "installer_env"
        else:
            program_kind = "python"
    else:
        return _refuse("unknown_program", label, **known)
    known["program_kind"] = program_kind

    # 6. data locations and non-standard environment.
    raw_env = document.get("EnvironmentVariables")
    plist_env: dict[str, Any] = raw_env if isinstance(raw_env, dict) else {}
    if _data_location_conflict(plist_env, dotenv, workspace):
        return _refuse("custom_data_location", label, **known)
    dropped = tuple(
        sorted(
            str(key)
            for key in plist_env
            if key not in _STANDARD_ENV_KEYS
            and not str(key).startswith("LC_")
            and key not in _DATA_LOCATION_KEYS
        )
    )

    # 7. the bundled engine serves the .env port or 8443.
    try:
        bundled_port = int(dotenv.get("PWA_PORT") or _DEFAULT_PORT)
    except ValueError:
        bundled_port = _DEFAULT_PORT
    if not 1 <= bundled_port <= 65535:
        bundled_port = _DEFAULT_PORT
    if bundled_port != runtime.port:
        return _refuse("port_mismatch", label, **known)

    # 8. liveness, version and active chats.
    identity = engine_identity(runtime.port)
    loaded, job_pid = _job_state(label, runner)
    old_version = ""
    if identity is not None:
        owner = read_lock_owner(runtime)
        if not _owns_listener(owner, identity, runtime, workspace):
            return _refuse("foreign_listener", label, **known)
        version = identity.get("version")
        old_version = version if isinstance(version, str) else ""
    if not old_version and receipt is not None and isinstance(receipt.get("version"), str):
        old_version = receipt["version"]
    if not old_version and (identity is not None or loaded):
        # A running or loaded engine of unknown version cannot be shown to be
        # new enough, or not a downgrade.
        return _refuse("unknown_version", label, **known)
    if old_version:
        if _version_key(old_version) > _version_key(ciao.__version__):
            return _refuse("downgrade", label, old_version=old_version, **known)
        if _version_key(old_version) < _version_key(MIN_TAKEOVER_VERSION):
            return _refuse("engine_too_old", label, old_version=old_version, **known)
    # 2.0 upgrades from a finished 1.10 only: a half-migrated install stays put.
    # After the version checks, so "too old" and "downgrade" keep their own codes.
    unfinished = upgrade_preflight.check(workspace, runtime_root)
    if unfinished is not None:
        return _refuse(
            upgrade_preflight.REFUSAL_CODE, label, old_version=old_version, advice=unfinished.message, **known
        )
    try:
        active_chats = len(restart_blockers(runtime.port))
    except RestartBlockersUnavailable:
        return _refuse("blockers_unavailable", label, old_version=old_version, **known)
    old_running = identity is not None or job_pid is not None

    # 9. trash candidates, only with proof; a checkout is never trashed.
    candidates: list[TrashCandidate] = []
    left_in_place: tuple[str, ...] = ()
    if program_kind == "server_host":
        candidates = _server_host_candidates(argv[0])
    elif program_kind == "installer_env" and receipt is not None and direct_python is not None:
        candidates = _installer_candidates(direct_python, receipt)
    elif program_kind == "checkout" and checkout is not None:
        left_in_place = (str(checkout),)
    protected = _protected_paths(workspace, runtime_root, dotenv, plist_env, registry)
    trash: list[TrashCandidate] = []
    for candidate in candidates:
        checked = _safe(
            Path(candidate.path),
            protected,
            expected_root=_PROOF_ROOTS[candidate.proof],
            allow_symlink=candidate.proof in _SYMLINK_PROOFS,
        )
        if checked is None:
            continue
        # Emit the path that was checked, not the one the proof was built from.
        item = TrashCandidate(str(checked), candidate.proof)
        if item not in trash:
            trash.append(item)

    return TakeoverPlan(
        action=ACTION_TAKEOVER,
        label=label,
        plist=str(plist_path),
        plist_sha256=read.sha256,
        workspace=str(workspace),
        runtime_root=str(runtime_root),
        port=runtime.port,
        program_kind=program_kind,
        old_version=old_version,
        old_running=old_running,
        active_chats=active_chats,
        trash=tuple(trash),
        left_in_place=left_in_place,
        dropped_env=dropped,
        protected=tuple(str(path) for path in protected),
    )


def _resume_plan(
    resume_id: str, *, label: str, launch_agents_dir: Path | None, runner: Runner
) -> TakeoverPlan:
    """The plan of an unfinished takeover, with what the app must ask before it moves.

    A journal at ``started`` or ``stopping`` may leave the old engine running,
    so its liveness and chats are read the way a fresh plan reads them. An
    unreadable journal carries only its id: apply refuses it, and the app says so.
    """
    journal = _load_journal(resume_id, launch_agents_dir=launch_agents_dir, label=label)
    if journal is None:
        return TakeoverPlan(action=ACTION_RESUME, label=label, resume_id=resume_id)
    port = int(journal["port"])
    running = False
    chats = 0
    if str(journal["state"]) in (STATE_STARTED, STATE_STOPPING):
        identity = engine_identity(port)
        loaded, _ = _job_state(label, runner)
        running = identity is not None or loaded
        try:
            chats = len(restart_blockers(port))
        except RestartBlockersUnavailable:
            chats = -1
    return TakeoverPlan(
        action=ACTION_RESUME,
        label=label,
        plist=str(journal["plist"]),
        plist_sha256=str(journal["plist_sha256"]),
        workspace=str(journal["workspace"]),
        runtime_root=str(journal["runtime_root"]),
        port=port,
        old_running=running,
        active_chats=chats,
        resume_id=resume_id,
    )


def _owns_listener(
    owner: dict[str, Any] | None,
    identity: dict[str, Any],
    runtime: DesktopRuntime,
    workspace: Path,
) -> bool:
    """The listener is the live engine this plist started.

    The lock must be a running, current-run record for this runtime and port
    (the macos_service freshness rule), carry the same ``startup_id`` and pid
    the listener reports, name this workspace, and the pid must be alive.
    """
    fresh = lock_owner_is_fresh(owner, runtime, None)
    if fresh is None:
        return False
    if fresh.get("startup_id") != identity.get("startup_id"):
        return False
    pid = identity.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or fresh.get("pid") != pid:
        return False
    owner_workspace = fresh.get("workspace")
    if not isinstance(owner_workspace, str) or not owner_workspace:
        return False
    return _same_dir(Path(owner_workspace), workspace) and _pid_alive(pid)


# --- the transaction: apply, finish, rollback ----------------------------------

REF_BUSY = "busy"
REF_STATE_UNSAFE = "takeover_state_unsafe"
REF_PLIST_CHANGED = "plist_changed"
REF_NOT_RESTARTED = "old_engine_not_restarted"
REF_RESTORE_FAILED = "plist_restore_failed"


def _refused(reason: str, **fields: Any) -> dict[str, Any]:
    """The JSON answer of a transaction that was refused; exit 3 for the CLI."""
    return {"ok": False, "reason": reason, **fields}


def _utc_stamp() -> str:
    """``yyyymmddTHHMMSSZ`` in UTC, the takeover id's shape."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _takeover_root() -> Path:
    return state_dir() / "takeover"


def _safe_takeover_id(takeover_id: str) -> bool:
    """Whether ``takeover_id`` can only name a directory inside the journal root.

    The id arrives from the app's command line and lands in paths that drive
    unlinks, so it is held to plain directory-name characters — never a slash,
    a dot-dot walk or a NUL.
    """
    if not isinstance(takeover_id, str) or re.fullmatch(r"[A-Za-z0-9-]{1,64}", takeover_id) is None:
        return False
    return takeover_id not in (".", "..")


def _journal_path(takeover_id: str) -> Path:
    return _takeover_root() / takeover_id / JOURNAL_NAME


def _backup_path(entry: Path, label: str) -> Path:
    return entry / f"{label}.plist"


class _Busy(Exception):
    """Another takeover command holds the transaction lock."""


class _StateUnsafe(Exception):
    """``takeover/`` or its lock file is a symlink, foreign or not private."""


class _NoState(Exception):
    """``takeover/`` does not exist, so there is no journal to act on."""


class _Settled(Exception):
    """The journal on disk is no longer in the state this process read."""


@contextmanager
def _transaction_lock(*, create: bool) -> Iterator[None]:
    """Hold the exclusive per-takeover lock for a whole apply/finish/rollback.

    A non-blocking ``flock`` on ``takeover/.lock``: a second command sees
    :class:`_Busy` instead of racing the first one's stop, unlink or restore.
    Every mutation of the journal root, the entries and the journals happens
    inside it, which is also why a young entry found under the lock is an
    orphan and never another run's work in progress.
    """
    from ciao.os_support.locks import lock_exclusive, unlock
    from ciao.os_support.private import is_private_descriptor_strict

    root = _takeover_root()
    if _takeover_state_unsafe():
        raise _StateUnsafe()
    if not os.path.lexists(root):
        if not create:
            raise _NoState()
        _make_entry(root)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(root / ".lock", flags, 0o600)
    except OSError as exc:
        raise _StateUnsafe() from exc
    try:
        if not is_private_descriptor_strict(fd):
            raise _StateUnsafe()
        try:
            lock_exclusive(fd, blocking=False)
        except OSError:
            raise _Busy() from None
        try:
            yield
        finally:
            unlock(fd)
    finally:
        os.close(fd)


def _guarded(action: Callable[[], dict[str, Any]], *, create: bool) -> dict[str, Any]:
    """Run ``action`` under the transaction lock, mapping its refusals."""
    try:
        with _transaction_lock(create=create):
            return action()
    except _Busy:
        return _refused(
            REF_BUSY, message="another Ciaobot takeover command is running; try again in a moment"
        )
    except _StateUnsafe:
        return _refused(REF_STATE_UNSAFE)
    except _Settled:
        return _refused(REF_ALREADY_SETTLED)


def _agents_dir(launch_agents_dir: Path | None) -> Path:
    if launch_agents_dir is None:
        return macos_service.default_launch_agents_dir()
    return Path(launch_agents_dir).expanduser()


def _same_path(a: str | Path, b: str | Path) -> bool:
    return os.path.abspath(a) == os.path.abspath(b)


def _journal_matches_backup(entry: Path, journal: dict[str, Any]) -> bool:
    """Whether the verified backup plist derives the journal's workspace and runtime root.

    The backup is hash-proven to be what the plan read, so what it says is the
    independent witness of the journal's workspace and runtime root. Only
    plist-derived fields are compared: the live workspace ``.env`` is never
    read here (a later ``PWA_PORT`` edit, or an unreadable ``.env``, must not
    make a journal unreadable once the plist is gone), and the port, which may
    come from that ``.env``, is only range-checked by the loader. A missing or
    unverified backup is not judged here (the steps that use it refuse with
    their own reason).
    """
    try:
        read = read_definition(_backup_path(entry, str(journal["label"])))
    except DefinitionReadError:
        return True
    if read.sha256 != journal["plist_sha256"]:
        return True
    try:
        document = plistlib.loads(read.data)
    except (ValueError, ExpatError):
        return False
    if not isinstance(document, dict):
        return False
    runtime = runtime_from_plist(
        document,
        plist_path=Path(str(journal["plist"])),
        environ={},
        read_env=lambda _path: {},
    )
    same: bool = (
        runtime.workspace == journal["workspace"]
        and runtime.runtime_root == journal["runtime_root"]
    )
    return same


def _load_journal(
    takeover_id: str, *, launch_agents_dir: Path | None = None, label: str | None = None
) -> dict[str, Any] | None:
    """The journal of ``takeover_id``, when it is intact, private and consistent.

    ``None`` for an unknown or malformed id, a symlinked or non-private
    ``takeover/`` directory or entry, a journal that is missing, unreadable,
    foreign or not this user's owner-only file, and one that does not name what
    a rollback would have to restore: the label (the plan's, when given, and
    always one the isolation guard accepts), the plist at
    ``<LaunchAgents>/<label>.plist`` (under the real home for the live label),
    and the workspace, runtime root and port the verified backup derives. A
    corrupt journal is unfinished work plan() does not trust; nothing in the
    transaction is allowed to act on one.
    """
    if not _safe_takeover_id(takeover_id):
        return None
    root = _takeover_root()
    entry = root / takeover_id
    if not _private_directory(root) or not _private_directory(entry):
        return None
    journal = _read_journal_file(entry)
    if journal is None or journal.get("schema") != JOURNAL_SCHEMA:
        return None
    jlabel = journal.get("label")
    plist = journal.get("plist")
    sha = journal.get("plist_sha256")
    workspace = journal.get("workspace")
    runtime_root = journal.get("runtime_root")
    port = journal.get("port")
    state = journal.get("state")
    if not isinstance(jlabel, str) or not jlabel or "/" in jlabel or jlabel in (".", ".."):
        return None
    if label is not None and jlabel != label:
        return None
    if _isolation_refusal(jlabel):
        return None
    if not isinstance(plist, str) or not _absolute(plist):
        return None
    agents = _agents_dir(launch_agents_dir)
    if not _same_path(plist, agents / f"{jlabel}.plist"):
        return None
    if jlabel == SERVER_LABEL:
        real = _real_home()
        if real is None or not _same_path(agents, real / "Library" / "LaunchAgents"):
            return None
    if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{64}", sha) is None:
        return None
    if (
        not isinstance(workspace, str)
        or not _absolute(workspace)
        or not isinstance(runtime_root, str)
        or not _absolute(runtime_root)
    ):
        return None
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        return None
    if not isinstance(state, str):
        return None
    if not _journal_matches_backup(entry, journal):
        return None
    return journal


def _make_entry(entry: Path) -> None:
    """Create ``entry`` and its missing parents inside the engine state dir.

    Every directory this helper creates is owner-only (0700). A pre-existing
    directory is left alone; a symlink anywhere on the chain that is yet to be
    created is refused, because the journal and the plist backup must be
    written where ``state_dir()`` named them and nowhere else.
    """
    from ciao.os_support.private import make_private_dir

    chain: list[Path] = []
    directory = entry
    while not directory.exists() and directory != directory.parent:
        if os.path.islink(directory):
            raise OSError(
                errno.EPERM,
                f"the takeover journal directory must not be a symlink: {directory}",
            )
        chain.append(directory)
        directory = directory.parent
    entry.mkdir(parents=True, exist_ok=True)
    for fresh in chain:
        make_private_dir(fresh)


def _create_entry() -> tuple[str, Path]:
    """Atomically claim a fresh takeover id: ``mkdir`` of a name that must not exist.

    A collision (another entry in the same second) retries with a ``-N`` suffix,
    which keeps the lexicographic order of directory names. Only the directory
    this call created is ever its to remove.
    """
    from ciao.os_support.private import make_private_dir

    root = _takeover_root()
    _make_entry(root)
    stamp = _utc_stamp()
    suffix = 1
    while True:
        candidate = stamp if suffix == 1 else f"{stamp}-{suffix}"
        entry = root / candidate
        try:
            entry.mkdir(mode=0o700, exist_ok=False)
        except FileExistsError:
            suffix += 1
            continue
        make_private_dir(entry)
        return candidate, entry


def _discard_young_entries() -> None:
    """Remove entries that stopped nothing: no journal, or the ``created`` skeleton.

    Only ever called under the transaction lock, where such an entry is the
    leftover of a crashed run rather than another run's work in progress.
    """
    root = _takeover_root()
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        if re.fullmatch(r"[A-Za-z0-9-]{1,64}", name) is None:
            continue
        entry = root / name
        if _entry_state(entry) in _YOUNG_STATES:
            shutil.rmtree(entry, ignore_errors=True)


#: Room left under the strict reader's limit for the fields later states add.
_JOURNAL_HEADROOM = 4096


class _JournalTooLarge(Exception):
    """The journal would exceed what the strict reader accepts."""


def _journal_text(journal: dict[str, Any], *, headroom: int = 0) -> str:
    payload = {"schema": JOURNAL_SCHEMA, **journal, "updated": _utc_stamp()}
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if len(text.encode("utf-8")) > app_engine._MAX_RECORD_BYTES - headroom:
        raise _JournalTooLarge()
    return text


def _write_journal(entry: Path, journal: dict[str, Any]) -> None:
    """Replace the journal in one atomic, owner-only, fsynced write.

    Refused (:class:`_JournalTooLarge`) before anything is written when the
    document is past the strict reader's limit: a journal that could be written
    but never read back would strand the takeover.
    """
    from ciao.os_support.private import (
        is_private_descriptor_strict,
        make_private,
        mkstemp_private,
    )

    # ``updated`` is stamped last: a journal dict read earlier must never carry
    # its old stamp over this write.
    text = _journal_text(journal)
    fd, tmp = mkstemp_private(dir=entry, prefix=f".{JOURNAL_NAME}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            if not is_private_descriptor_strict(handle.fileno()):
                raise OSError(
                    errno.EPERM, f"the takeover journal was not writeable privately: {tmp}"
                )
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, entry / JOURNAL_NAME)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    make_private(entry / JOURNAL_NAME)
    _fsync_dir(entry)


def _advance(entry: Path, expected: str, to: str, **extra: Any) -> None:
    """Move the journal from ``expected`` to ``to`` on a fresh read, or raise.

    The journal is re-read at the write, under the lock, so a dict held since
    an earlier step can never write an old state back; a journal that is no
    longer in ``expected`` means something settled it and raises
    :class:`_Settled`.
    """
    current = _read_journal_file(entry)
    if current is None or current.get("state") != expected:
        raise _Settled()
    current.update(extra)
    current["state"] = to
    _write_journal(entry, current)


def _note(entry: Path, state: str, **extra: Any) -> None:
    """Record fields on the journal without changing its state."""
    _advance(entry, state, state, **extra)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_dir(directory: Path) -> None:
    """Make a rename or a new file in ``directory`` durable (a no-op where directories cannot open)."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _fsync_copy(source: Path, target: Path) -> None:
    """Copy ``source`` to ``target`` with copied metadata and durable content."""
    shutil.copy2(source, target)
    with target.open("rb") as handle:
        os.fsync(handle.fileno())
    _fsync_dir(target.parent)


def _atomic_restore(source: Path, target: Path, *, sha256: str) -> None:
    """Put ``source`` at ``target`` in one rename: temp file, fsync, ``os.replace``.

    A crash or a full disk mid-copy leaves ``target`` exactly as it was (absent)
    and no temp file behind; the content is hashed before it can replace.
    """
    from ciao.os_support.private import mkstemp_private

    fd, tmp = mkstemp_private(dir=target.parent, prefix=f".{target.name}.")
    os.close(fd)
    try:
        _fsync_copy(source, Path(tmp))
        if _sha256_file(Path(tmp)) != sha256:
            raise OSError(errno.EIO, f"the restored definition did not verify: {tmp}")
        os.replace(tmp, target)
        _fsync_dir(target.parent)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _launchctl(arguments: list[str], runner: Runner) -> int | None:
    """One ``launchctl`` call: fixed absolute argv, captured output, a timeout.

    The return code, or ``None`` when launchctl could not run or did not answer
    in time — a timeout and a missing binary are outcomes, never exceptions.
    """
    try:
        completed = runner(
            [LAUNCHCTL, *arguments],
            capture_output=True,
            text=True,
            timeout=_LAUNCHCTL_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.returncode


def _job_confirmed_loaded(label: str, runner: Runner) -> bool:
    """Only an explicit success of ``launchctl print`` counts as loaded."""
    return _launchctl(["print", f"gui/{_getuid()}/{label}"], runner) == 0


def _restart_command(journal: dict[str, Any]) -> str:
    return f"{LAUNCHCTL} bootstrap gui/{_getuid()} {shlex.quote(str(journal['plist']))}"


def _not_restarted(takeover_id: str, journal: dict[str, Any], state: str) -> dict[str, Any]:
    command = _restart_command(journal)
    return _refused(
        REF_NOT_RESTARTED,
        id=takeover_id,
        state=state,
        command=command,
        message=(
            "The previous Ciaobot engine could not be started again automatically. "
            f"Start it yourself with: {command}"
        ),
    )


def _runtime_lock_held(runtime_root: Path) -> bool:
    """Whether a process holds ``runtime_root``'s engine ``server.lock``.

    A non-blocking exclusive lock proves the opposite; the same probe
    ``workspace_move._server_lock_held`` applies, keyed on the journal's
    runtime root rather than on a derived one.
    """
    from ciao.os_support.locks import lock_exclusive, unlock

    try:
        handle = (runtime_root / SERVER_LOCK_NAME).open("rb")
    except OSError:
        return False
    with handle:
        try:
            lock_exclusive(handle.fileno(), blocking=False)
        except OSError:
            return True
        unlock(handle.fileno())
        return False


def _wait_until(
    ready: Callable[[], bool],
    *,
    timeout_s: float,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> bool:
    """Poll ``ready`` every poll interval until it holds or the clock passes."""
    deadline = clock() + timeout_s
    while not ready():
        if clock() >= deadline:
            return False
        sleep(_POLL_S)
    return True


def _old_engine_gone(*, label: str, port: int, runtime_root: Path, runner: Runner) -> bool:
    """All three stop conditions: job unloaded, port silent, lock released.

    The launchctl probe answers "loaded" when it cannot run at all, so only a
    real, verified-absent job counts; the port probe is the same strict engine
    identity ``plan()`` used before the takeover started.
    """
    loaded, _ = _job_state(label, runner)
    return (
        not loaded
        and engine_identity(port) is None
        and not _runtime_lock_held(runtime_root)
    )


class _ShaMismatch(Exception):
    """The plist changed under the takeover between plan() and the copy."""


def _backup_unusable(entry: Path, journal: dict[str, Any]) -> bool:
    """Whether the plist backup is missing, not a regular file or not the journal's hash."""
    backup = _backup_path(entry, str(journal["label"]))
    try:
        return (
            backup.is_symlink()
            or not backup.is_file()
            or _sha256_file(backup) != str(journal["plist_sha256"])
        )
    except OSError:
        return True


def _begin_takeover(current: TakeoverPlan) -> str:
    """Create the takeover entry, journal ``created``, back up the plist, journal ``started``.

    Returns the takeover id. The journal skeleton is written before anything
    else, so no journal-less entry exists. The backup exists — copied with
    metadata and fsynced, and verified against the hash the app approved —
    before any engine is stopped: if the takeover never gets past this point,
    nothing of the old install was touched, and a crash leaves a ``created``
    skeleton that the next apply discards.
    """
    takeover_id, entry = _create_entry()
    label = current.label
    journal: dict[str, Any] = {
        "id": takeover_id,
        "state": STATE_CREATED,
        "created": _utc_stamp(),
        "label": label,
        "plist": current.plist,
        "plist_sha256": current.plist_sha256,
        "workspace": current.workspace,
        "runtime_root": current.runtime_root,
        "port": current.port,
        "program_kind": current.program_kind,
        "old_version": current.old_version,
        "active_chats": current.active_chats,
        "trash": [asdict(item) for item in current.trash],
        "left_in_place": list(current.left_in_place),
        "dropped_env": list(current.dropped_env),
        "protected": list(current.protected),
        "app_version": ciao.__version__,
    }
    try:
        # The skeleton is the largest the journal will be; refusing here, before
        # any engine is stopped, costs nothing. Later states add a few fields.
        _journal_text(journal, headroom=_JOURNAL_HEADROOM)
        _write_journal(entry, journal)
        backup = _backup_path(entry, label)
        plist_path = Path(current.plist)
        if plist_path.is_symlink() or not plist_path.is_file():
            raise DefinitionReadError(f"the service definition is not a regular file: {plist_path}")
        _fsync_copy(plist_path, backup)
        copied_sha = _sha256_file(backup)
    except BaseException:
        shutil.rmtree(entry, ignore_errors=True)
        raise
    if copied_sha != current.plist_sha256:
        # The plist changed between plan() and this copy, so the hash the app
        # approved no longer names what is on disk. Remove the young entry
        # again (this run created it): leaving it would pin every later plan.
        shutil.rmtree(entry, ignore_errors=True)
        raise _ShaMismatch()
    _advance(entry, STATE_CREATED, STATE_STARTED)
    return takeover_id


def _stop_old_engine(
    journal: dict[str, Any],
    *,
    runner: Runner,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> str | None:
    """Boot the old job out and wait for silence.

    ``None`` when the old engine is gone; :data:`REF_DID_NOT_STOP` when it did
    not stop and the job was put back; :data:`REF_NOT_RESTARTED` when it did not
    stop and putting it back failed too.
    """
    uid = _getuid()
    label = str(journal["label"])
    # A bootout that times out or cannot run is not decisive: the wait below
    # verifies the job, the port and the lock itself.
    _launchctl(["bootout", f"gui/{uid}/{label}"], runner)
    if _wait_until(
        lambda: _old_engine_gone(
            label=label,
            port=int(journal["port"]),
            runtime_root=Path(str(journal["runtime_root"])),
            runner=runner,
        ),
        timeout_s=_APPLY_TEARDOWN_S,
        clock=clock,
        sleep=sleep,
    ):
        return None
    # Put the old service back exactly as it was found: the job definition is
    # still in LaunchAgents (the unlink never happened), so bootstrapping it
    # restores what launchd was running. "Already loaded" is as good as a
    # clean bootstrap; anything unconfirmed is reported, never assumed.
    code = _launchctl(["bootstrap", f"gui/{uid}", str(journal["plist"])], runner)
    if code == 0 or _job_confirmed_loaded(label, runner):
        return REF_DID_NOT_STOP
    return REF_NOT_RESTARTED


def _remove_plist(entry: Path, journal: dict[str, Any]) -> str | None:
    """Unlink the LaunchAgents definition once the backup is proven. ``None`` ok.

    The live file must still be what the journal approved: its hash equals the
    journal's and the backup's. Anything else is the user's new edit, which is
    never unlinked. An already-absent file is the finished unlink of a crashed
    run.
    """
    if _backup_unusable(entry, journal):
        # Without the verified backup a rollback could not restore this file,
        # so the unlink is refused: the takeover stays resumable instead.
        return REF_BACKUP_UNUSABLE
    plist = Path(str(journal["plist"]))
    if not os.path.lexists(plist):
        return None
    backup_sha = _sha256_file(_backup_path(entry, str(journal["label"])))
    try:
        if plist.is_symlink() or not plist.is_file():
            return REF_PLIST_CHANGED
        live = _sha256_file(plist)
    except OSError:
        return REF_PLIST_CHANGED
    if live != str(journal["plist_sha256"]) or live != backup_sha:
        return REF_PLIST_CHANGED
    plist.unlink(missing_ok=True)
    return None


def _live_plist_problem(journal: dict[str, Any], expect_sha256: str) -> str | None:
    """On a resume before the unlink: the live plist must hash to what was approved."""
    plist = Path(str(journal["plist"]))
    if not os.path.lexists(plist):
        return REF_PLIST_CHANGED if journal.get("state") == STATE_STARTED else None
    try:
        if plist.is_symlink() or not plist.is_file():
            return REF_PLIST_CHANGED
        live = _sha256_file(plist)
    except OSError:
        return REF_PLIST_CHANGED
    return None if live == expect_sha256 else REF_PLIST_CHANGED


def apply(
    *,
    expect_sha256: str,
    launch_agents_dir: Path | None = None,
    label: str = SERVER_LABEL,
    runner: Runner | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Stop the old engine and hand its workspace to the app's engine.

    Re-runs :func:`plan` and refuses unless it says ``takeover`` (or ``resume``
    of the takeover this journal already records) and the plist hash matches
    ``expect_sha256`` — the hash the caller saw in the plan JSON it acted on,
    so what is on disk now is what was approved. Every step is journalled and
    idempotent on a resume; a successful apply ends at ``pointer_written``,
    leaving the old plist backed up and the old job gone. The whole transaction
    holds the per-takeover lock: a concurrent command answers ``busy``.
    """
    if _isolation_refusal(label):
        return _refused(REF_NOT_TAKEOVER, action=ACTION_REFUSE)
    chosen = runner if runner is not None else _default_runner()
    return _guarded(
        lambda: _apply_locked(
            expect_sha256=expect_sha256,
            launch_agents_dir=launch_agents_dir,
            label=label,
            runner=chosen,
            clock=clock,
            sleep=sleep,
        ),
        create=True,
    )


def _apply_locked(
    *,
    expect_sha256: str,
    launch_agents_dir: Path | None,
    label: str,
    runner: Runner,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    _discard_young_entries()
    current = plan(
        launch_agents_dir=launch_agents_dir, label=label, runner=runner, describe_resume=False
    )
    if current.action == ACTION_RESUME:
        takeover_id = current.resume_id
        journal = _load_journal(takeover_id, launch_agents_dir=launch_agents_dir, label=label)
        if journal is None:
            return _refused(REF_NO_JOURNAL, id=takeover_id)
        if journal.get("state") in _FINISHED_STATES or journal.get("state") not in _KNOWN_STATES:
            return _refused(REF_ALREADY_SETTLED, id=takeover_id, state=journal.get("state"))
        if journal.get("plist_sha256") != expect_sha256:
            return _refused(REF_SHA_MISMATCH, id=takeover_id)
        if journal.get("state") in (STATE_STARTED, STATE_STOPPING):
            problem = _live_plist_problem(journal, expect_sha256)
            if problem is not None:
                return _refused(problem, id=takeover_id, state=journal.get("state"))
    elif current.action == ACTION_TAKEOVER:
        if current.plist_sha256 != expect_sha256:
            return _refused(REF_SHA_MISMATCH, id="")
        try:
            takeover_id = _begin_takeover(current)
        except _ShaMismatch:
            return _refused(REF_SHA_MISMATCH, id="")
        except _JournalTooLarge:
            return _refused(
                REF_JOURNAL_TOO_LARGE,
                id="",
                message="the takeover journal would be too large to read back; nothing was changed",
            )
    else:
        return _refused(REF_NOT_TAKEOVER, action=current.action)

    # Past this point launchd may already have been touched. Whatever goes wrong
    # still answers with the id and the journal's state, so the app knows a
    # rollback may be needed instead of seeing a bare error.
    entry = _takeover_root() / takeover_id
    try:
        return _drive(takeover_id, entry, launch_agents_dir, label, runner, clock, sleep)
    except _Settled:
        return _refused(REF_ALREADY_SETTLED, id=takeover_id, state=_journal_state(entry))
    except Exception as exc:  # noqa: BLE001 - reported with id and state, never raised.
        return _refused(
            REF_ERROR,
            id=takeover_id,
            state=_journal_state(entry),
            message=f"the takeover stopped on an unexpected error: {exc}",
        )


def _journal_state(entry: Path) -> str | None:
    """The journal's state on disk, best effort (``None`` when it cannot be read)."""
    try:
        journal = _read_journal_file(entry)
    except Exception:  # noqa: BLE001 - this only decorates an error answer.
        return None
    state = journal.get("state") if journal is not None else None
    return state if isinstance(state, str) else None


def _plist_absent_and_job_unloaded(journal: dict[str, Any], runner: Runner) -> bool:
    """The old service is not defined and not loaded: what every resume past the unlink needs."""
    if os.path.lexists(Path(str(journal["plist"]))):
        return False
    loaded, _ = _job_state(str(journal["label"]), runner)
    return not loaded


def _old_engine_back(journal: dict[str, Any]) -> dict[str, Any]:
    return _refused(
        REF_OLD_ENGINE_BACK,
        id=str(journal["id"]) if "id" in journal else "",
        state=journal.get("state"),
        message=(
            "the previous Ciaobot engine or its service definition is back; the takeover "
            "was left as it is. Roll it back, or stop the previous engine and retry."
        ),
    )


def _drive(
    takeover_id: str,
    entry: Path,
    launch_agents_dir: Path | None,
    label: str,
    runner: Runner,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    while True:
        journal = _load_journal(takeover_id, launch_agents_dir=launch_agents_dir, label=label)
        if journal is None:
            return _refused(REF_NO_JOURNAL, id=takeover_id)
        state = str(journal["state"])
        if state in _FINISHED_STATES:
            # A rollback (or a finish) settled this takeover while apply was
            # running; the journal on disk wins over this process's plan.
            return _refused(REF_ALREADY_SETTLED, id=takeover_id, state=state)
        if state == STATE_STARTED:
            # The backup is proven again before every stop, resumed or fresh.
            if _backup_unusable(entry, journal):
                return _refused(REF_BACKUP_UNUSABLE, id=takeover_id, state=state)
            outcome = _stop_old_engine(journal, runner=runner, clock=clock, sleep=sleep)
            if outcome == REF_NOT_RESTARTED:
                _note(entry, state, problem=REF_NOT_RESTARTED)
                return _not_restarted(takeover_id, journal, state)
            if outcome is not None:
                _advance(entry, state, STATE_ROLLED_BACK)
                return _refused(outcome, id=takeover_id, state=STATE_ROLLED_BACK)
            _advance(entry, state, STATE_STOPPING)
        elif state == STATE_STOPPING:
            # Gone when this run stopped it is not gone now: a login, or an
            # interrupted rollback, may have brought it back. Refuse and leave
            # everything as it is; a rollback stays possible.
            if not _old_engine_gone(
                label=str(journal["label"]),
                port=int(journal["port"]),
                runtime_root=Path(str(journal["runtime_root"])),
                runner=runner,
            ):
                return _old_engine_back(journal)
            refusal = _remove_plist(entry, journal)
            if refusal is not None:
                return _refused(refusal, id=takeover_id, state=state)
            _advance(entry, state, STATE_PLIST_REMOVED)
        elif state == STATE_PLIST_REMOVED:
            if not _old_engine_gone(
                label=str(journal["label"]),
                port=int(journal["port"]),
                runtime_root=Path(str(journal["runtime_root"])),
                runner=runner,
            ) or not _plist_absent_and_job_unloaded(journal, runner):
                return _old_engine_back(journal)
            app_engine.write_pointer(
                Path(str(journal["workspace"])), Path(str(journal["runtime_root"]))
            )
            _advance(entry, state, STATE_POINTER_WRITTEN)
        elif state == STATE_POINTER_WRITTEN:
            # The bundled engine may be running by now, so only the old service
            # definition and job are re-proven absent.
            if not _plist_absent_and_job_unloaded(journal, runner):
                return _old_engine_back(journal)
            return {"ok": True, "id": takeover_id, "state": STATE_POINTER_WRITTEN}
        elif state == STATE_HEALTHY:
            return {"ok": True, "id": takeover_id, "state": STATE_HEALTHY}
        else:
            return _refused(REF_NO_JOURNAL, id=takeover_id, state=state)


def _journal_runtime(journal: dict[str, Any]) -> DesktopRuntime:
    return DesktopRuntime(
        workspace=str(journal["workspace"]),
        runtime_root=str(journal["runtime_root"]),
        port=int(journal["port"]),
        server_plist=str(journal["plist"]),
        python_path="",
    )


#: Pause between the two health reads: an engine that is crash-looping or about
#: to be replaced does not hold the same identity a second later.
_HEALTH_RECHECK_S = 1.0


def _health_snapshot(journal: dict[str, Any]) -> tuple[str | None, tuple[Any, ...]]:
    """One read of the bundled engine's health: ``(first failing check, identity tuple)``.

    All of these must hold: the app-owned pointer names the journal's
    workspace and runtime root; the ready record names a live pid and the same
    workspace and port; whatever answers the port identifies itself with that
    pid and startup id and runs this app's engine version (``ciao.__version__``,
    which is the bundled engine's: the CLI runs from it); and the runtime lock
    is actually held, by that pid.
    """
    workspace = Path(str(journal["workspace"]))
    runtime_root = Path(str(journal["runtime_root"]))
    port = int(journal["port"])
    pointer = app_engine.read_pointer()
    if (
        pointer is None
        or not _same_dir(pointer.workspace, workspace)
        or not _same_dir(pointer.runtime_root, runtime_root)
    ):
        return "pointer", ()
    ready = app_engine.read_private_json(
        state_dir() / app_engine.READY_RECORD_NAME, label="the app engine ready record"
    )
    if ready is None:
        return "ready", ()
    pid = ready.get("pid")
    startup_id = ready.get("startup_id")
    ready_port = ready.get("port")
    ready_workspace = ready.get("workspace")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or not isinstance(startup_id, str)
        or not startup_id
        or not isinstance(ready_port, int)
        or isinstance(ready_port, bool)
        or not isinstance(ready_workspace, str)
    ):
        return "ready", ()
    if ready_port != port or not _same_dir(Path(ready_workspace), workspace) or not _pid_alive(pid):
        return "ready", ()
    identity = engine_identity(port)
    if identity is None or identity.get("startup_id") != startup_id or identity.get("pid") != pid:
        return "listener", ()
    if identity.get("version") != ciao.__version__:
        return "version", ()
    if not _runtime_lock_held(runtime_root):
        return "lock", ()
    owner = read_lock_owner(_journal_runtime(journal))
    if not isinstance(owner, dict) or owner.get("pid") != pid:
        return "lock", ()
    return None, (pid, startup_id, identity.get("version"), owner.get("startup_id"))


def _health_problem(journal: dict[str, Any], sleep: Callable[[float], None]) -> str | None:
    """Healthy only when two reads a moment apart pass and agree.

    A snapshot that passes once and differs (or fails) the second time is
    ``unstable``: an engine that restarts is not one to trash the old install for.
    """
    problem, first = _health_snapshot(journal)
    if problem is not None:
        return problem
    sleep(_HEALTH_RECHECK_S)
    problem, second = _health_snapshot(journal)
    if problem is not None or second != first:
        return "unstable"
    return None


def _candidate_proven(path: Path, proof: str, env_roots: list[Path]) -> bool:
    """Whether the candidate still carries the proof that earned it a place in the plan.

    The plan proved each one when it listed it; a leftover could have been
    swapped for something else since, so the proof is checked again.
    """
    home = Path.home()
    try:
        if proof == "receipt_env":
            return path.name == _ENV_ROOT_NAME and path.is_dir() and not path.is_symlink()
        if proof == "receipt":
            state = home.joinpath(*_STATE_ROOT)
            return (
                path.parent == state or state in path.parents
            ) and path.is_file() and not path.is_symlink() and path.name in (
                "install-receipt.json",
                _UV_NAME,
            )
        if proof == "installer_shim":
            if path.is_symlink() or not path.is_file() or path != home / ".local" / "bin" / "ciao":
                return False
            text = read_definition(path, max_bytes=_MAX_SMALL_FILE).data.decode("utf-8", "replace")
            return SHIM_MARKER in text
        if proof == "uv_tool_symlink":
            if not path.is_symlink() or path != home / ".local" / "bin" / "ciao":
                return False
            return any(_inside(path.resolve(), env.resolve()) for env in env_roots)
        if proof == "server_host_program":
            return path == home / "Applications" / _HOST_APP_NAME and path.exists()
        if proof == "server_host_record":
            record = _read_json(path)
            host = home / "Applications" / _HOST_APP_NAME
            return bool(record is not None and record.get("bundle_path") == str(host))
    except (OSError, DefinitionReadError, RuntimeError):
        return False
    return False


def _finish_trash(journal: dict[str, Any]) -> list[dict[str, str]]:
    """The journal's candidates that may still be trashed, re-filtered.

    Every candidate passes ``_safe`` again against the SAME protected paths the
    plan used (persisted in the journal; a journal without them offers nothing),
    still carries its proof, and must still exist — a candidate the app already
    trashed on an earlier finish drops out, so a lost answer can be re-fetched
    harmlessly.
    """
    persisted = journal.get("protected")
    if not isinstance(persisted, list) or not all(isinstance(item, str) for item in persisted):
        return []
    workspace = Path(str(journal["workspace"]))
    runtime_root = Path(str(journal["runtime_root"]))
    protected = [
        workspace,
        runtime_root,
        Path.home() / ".ciao",
        state_dir(),
        *(Path(item) for item in persisted),
    ]
    items = [item for item in journal.get("trash") or [] if isinstance(item, dict)]
    env_roots = [
        Path(str(item["path"]))
        for item in items
        if item.get("proof") == "receipt_env" and isinstance(item.get("path"), str)
    ]
    found: list[dict[str, str]] = []
    for item in items:
        raw_path = item.get("path")
        proof = item.get("proof")
        if not isinstance(raw_path, str) or not isinstance(proof, str) or proof not in _PROOF_ROOTS:
            continue
        checked = _safe(
            Path(raw_path),
            protected,
            expected_root=_PROOF_ROOTS[proof],
            allow_symlink=proof in _SYMLINK_PROOFS,
        )
        if checked is None or not os.path.lexists(checked):
            continue
        if not _candidate_proven(checked, proof, env_roots):
            continue
        path = str(checked)
        if all(existing["path"] != path for existing in found):
            found.append({"path": path, "proof": proof})
    return found


def _left_in_place(journal: dict[str, Any]) -> list[str]:
    values = journal.get("left_in_place")
    return [str(value) for value in values] if isinstance(values, list) else []


def finish(
    *,
    takeover_id: str,
    done: bool = False,
    runner: Runner | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Prove the bundled engine is healthy and name what the app may trash.

    With ``done`` the app closes the takeover after trashing (and recording
    ``trash.json`` itself): the journal ends ``finished``. Any missing proof
    keeps the journal open and refuses ``not_healthy``.
    """
    chosen = runner if runner is not None else _default_runner()
    try:
        return _guarded(lambda: _finish_locked(takeover_id, done, chosen, sleep), create=False)
    except _NoState:
        return _refused(REF_NO_JOURNAL, id=takeover_id)


def _finish_locked(
    takeover_id: str, done: bool, runner: Runner, sleep: Callable[[float], None]
) -> dict[str, Any]:
    journal = _load_journal(takeover_id)
    if journal is None:
        return _refused(REF_NO_JOURNAL, id=takeover_id)
    entry = _takeover_root() / takeover_id
    state = str(journal["state"])
    settled: dict[str, Any] = {
        "ok": True,
        "id": takeover_id,
        "trash": [],
        "left_in_place": _left_in_place(journal),
    }
    if state == STATE_FINISHED:
        return settled
    if state == STATE_ROLLED_BACK:
        return _refused(REF_ALREADY_SETTLED, id=takeover_id, state=state)
    if done:
        if state != STATE_HEALTHY:
            return _refused(REF_NOT_HEALTHY, id=takeover_id, state=state)
        _advance(entry, state, STATE_FINISHED)
        return settled
    if state not in (STATE_POINTER_WRITTEN, STATE_HEALTHY):
        return _refused(REF_NOT_HEALTHY, id=takeover_id, state=state)
    protected = journal.get("protected")
    if not isinstance(protected, list) or not protected:
        # Without the plan's protected paths nothing can be proven safe to trash.
        return _refused(REF_NO_PROTECTED, id=takeover_id, state=state)
    problem = _health_problem(journal, sleep)
    if problem is not None:
        return _refused(REF_NOT_HEALTHY, id=takeover_id, state=state, check=problem)
    # Trashing the old install is only safe while the old service stays gone.
    if not _plist_absent_and_job_unloaded(journal, runner):
        return _old_engine_back(journal)
    trash = _finish_trash(journal)
    if trash:
        # Journalled BEFORE the list leaves this process: once the app may have
        # trashed anything, the old install is no longer whole and a rollback
        # must refuse.
        _advance(entry, state, STATE_HEALTHY, trash_offered=True)
    else:
        _advance(entry, state, STATE_HEALTHY)
    return {
        "ok": True,
        "id": takeover_id,
        "trash": trash,
        "left_in_place": _left_in_place(journal),
    }


def rollback(
    *,
    takeover_id: str,
    runner: Runner | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Restore the old service for a takeover that never became healthy.

    The app has already stopped its bundled engine (this refuses while its
    lock is still held). The old plist is restored byte-identical from the
    verified backup (atomically), the old job is bootstrapped, and only once
    the old engine is proven back (its job loaded, and its pid the one the
    listener and the lock owner report) does the workspace pointer go and the
    journal end ``rolled_back``. If the old engine cannot be confirmed back, or
    the pointer cannot be cleared, the journal and pointer stay as they were.
    After the app was handed a Trash list (``trash_offered``) nothing can be
    rolled back any more.
    """
    chosen = runner if runner is not None else _default_runner()
    try:
        return _guarded(
            lambda: _rollback_locked(takeover_id, chosen, clock, sleep), create=False
        )
    except _NoState:
        return _refused(REF_NO_JOURNAL, id=takeover_id)


#: How far up the parent chain a hosted engine may sit below its launchd job's pid.
_MAX_JOB_DEPTH = 8
_PS = "/bin/ps"


def _parent_pid(pid: int) -> int | None:
    """The parent of ``pid`` (``ps -o ppid=``), or ``None`` when it cannot be read."""
    try:
        completed = subprocess.run(
            [_PS, "-o", "ppid=", "-p", str(pid)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = (completed.stdout or "").strip()
    return int(text) if completed.returncode == 0 and text.isdigit() else None


def _pid_in_job(pid: Any, job_pid: int, journal: dict[str, Any]) -> bool:
    """Whether ``pid`` is the loaded job's engine.

    A direct-python install runs the engine as the job's own process, so the pid
    must equal the job's. A hosted install (``server_host``) has the host as the
    job's pid and spawns the Python engine as a child, so a descendant of the job
    (bounded walk up the parent chain, never through pid 1) counts too.
    """
    if not isinstance(pid, int) or isinstance(pid, bool):
        return False
    if pid == job_pid:
        return True
    if journal.get("program_kind") != "server_host":
        return False
    current = pid
    for _ in range(_MAX_JOB_DEPTH):
        parent = _parent_pid(current)
        if parent is None or parent <= 1:
            return False
        if parent == job_pid:
            return True
        current = parent
    return False


def _old_engine_proven(journal: dict[str, Any], runner: Runner) -> bool:
    """The old engine is back: the job loaded with a pid, that engine on the port and in the lock.

    "Something answers the port" is not enough (another engine could); the
    restored job's own pid (or, for a hosted install, a descendant of it) must be
    the listener's and the lock owner's.
    """
    loaded, job_pid = _job_state(str(journal["label"]), runner)
    if not loaded or job_pid is None:
        return False
    identity = engine_identity(int(journal["port"]))
    if identity is None or not _pid_in_job(identity.get("pid"), job_pid, journal):
        return False
    runtime = _journal_runtime(journal)
    return _owns_listener(
        read_lock_owner(runtime), identity, runtime, Path(runtime.workspace)
    )


def _lock_is_old_engine(journal: dict[str, Any], runner: Runner) -> bool:
    """Whether the held runtime lock belongs to the OLD engine's loaded job.

    The old engine is the one whose launchd job is loaded and whose pid (or a
    hosted install's descendant pid) owns the lock; the bundled one is the pid in
    ``ready.json``. A lock the job does not explain stays "bundled" (refused):
    the rollback never guesses.
    """
    loaded, job_pid = _job_state(str(journal["label"]), runner)
    if not loaded or job_pid is None:
        return False
    owner = read_lock_owner(_journal_runtime(journal))
    if not isinstance(owner, dict) or not _pid_in_job(owner.get("pid"), job_pid, journal):
        return False
    ready = app_engine.read_private_json(
        state_dir() / app_engine.READY_RECORD_NAME, label="the app engine ready record"
    )
    return ready is None or ready.get("pid") != owner.get("pid")


def _pointer_is_ours(pointer_file: Path, workspace: Path) -> bool:
    """Whether the pointer names ``workspace``, or cannot be read (so cannot be ruled out)."""
    pointer = app_engine.read_pointer(pointer_file)
    if pointer is None:
        return True
    return _same_dir(pointer.workspace, workspace)


def _rollback_locked(
    takeover_id: str,
    runner: Runner,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    journal = _load_journal(takeover_id)
    if journal is None:
        return _refused(REF_NO_JOURNAL, id=takeover_id)
    state = str(journal["state"])
    if state in _FINISHED_STATES:
        return _refused(REF_ALREADY_SETTLED, id=takeover_id, state=state)
    if state not in _KNOWN_STATES:
        return _refused(REF_NO_JOURNAL, id=takeover_id, state=state)
    entry = _takeover_root() / takeover_id
    runtime_root = Path(str(journal["runtime_root"]))
    plist = Path(str(journal["plist"]))
    label = str(journal["label"])
    sha = str(journal["plist_sha256"])
    if journal.get("trash_offered") is True:
        return _refused(
            REF_TRASH_OFFERED,
            id=takeover_id,
            state=state,
            message="the Trash list was already handed out; the old install is not whole any more",
        )
    if _runtime_lock_held(runtime_root) and not _lock_is_old_engine(journal, runner):
        return _refused(REF_BUNDLED_RUNNING, id=takeover_id, state=state)
    # Every refusal comes before the first mutation: a rollback that cannot
    # restore the old plist must not have changed anything.
    backup = _backup_path(entry, label)
    backup_ok = not _backup_unusable(entry, journal)
    restore = False
    if os.path.lexists(plist):
        try:
            if plist.is_symlink() or not plist.is_file():
                return _refused(REF_PLIST_CONFLICT, id=takeover_id, state=state)
            live = _sha256_file(plist)
        except OSError:
            return _refused(REF_PLIST_CONFLICT, id=takeover_id, state=state)
        # Equal to the journal's (or the backup's) hash is "already restored";
        # differing from both is somebody else's definition.
        if live != sha and not (backup_ok and live == _sha256_file(backup)):
            return _refused(REF_PLIST_CONFLICT, id=takeover_id, state=state)
    else:
        if not backup_ok:
            return _refused(REF_BACKUP_UNUSABLE, id=takeover_id, state=state)
        restore = True
    if restore:
        try:
            _atomic_restore(backup, plist, sha256=sha)
        except OSError as exc:
            return _refused(
                REF_RESTORE_FAILED,
                id=takeover_id,
                state=state,
                message=f"the previous service definition could not be restored: {exc}",
            )
    if not _job_confirmed_loaded(label, runner):
        code = _launchctl(["bootstrap", f"gui/{_getuid()}", str(plist)], runner)
        if code != 0 and not _job_confirmed_loaded(label, runner):
            _note(entry, state, problem=REF_NOT_RESTARTED)
            return _not_restarted(takeover_id, journal, state)
    if not _wait_until(
        lambda: _old_engine_proven(journal, runner),
        timeout_s=_ROLLBACK_START_S,
        clock=clock,
        sleep=sleep,
    ):
        # Neither the pointer nor the journal moves until the old engine
        # answers; a second rollback repeats from here.
        _note(entry, state, problem=REF_DID_NOT_START)
        return _refused(REF_DID_NOT_START, id=takeover_id, state=state)
    workspace = Path(str(journal["workspace"]))
    cleared = app_engine.clear_pointer(expected_workspace=workspace)
    pointer_file = state_dir() / app_engine.WORKSPACE_POINTER_NAME
    if not cleared and os.path.lexists(pointer_file) and _pointer_is_ours(pointer_file, workspace):
        # A pointer naming this workspace that could not be cleared is never
        # silently left behind a "rolled back". One naming another workspace
        # (or no readable workspace at all) is not ours to clear.
        _note(entry, state, problem=REF_NOT_ROLLED_BACK)
        return _refused(
            REF_NOT_ROLLED_BACK,
            id=takeover_id,
            state=state,
            message="the old engine is back, but the app's workspace pointer could not be cleared",
        )
    _advance(entry, state, STATE_ROLLED_BACK)
    return {"ok": True, "id": takeover_id, "state": STATE_ROLLED_BACK}


# --- CLI -----------------------------------------------------------------------


def run_cli(args: Any) -> int:
    """``ciao mac-takeover``: JSON on stdout; exit 0 ok, 3 refused, 1 error."""
    try:
        action = args.mac_takeover_action
        if action == "plan":
            result: dict[str, Any] = plan(
                launch_agents_dir=args.launch_agents_dir, label=args.label
            ).to_dict()
        elif action == "apply":
            result = apply(
                expect_sha256=args.expect_sha256,
                launch_agents_dir=args.launch_agents_dir,
                label=args.label,
            )
        elif action == "finish":
            result = finish(takeover_id=args.takeover_id, done=args.done)
        elif action == "rollback":
            result = rollback(takeover_id=args.takeover_id)
        else:  # pragma: no cover - argparse constrains the action.
            raise ValueError(f"unknown action {action}")
    except Exception as exc:  # noqa: BLE001 - the app reads one JSON document.
        document: dict[str, Any] = {"action": "error", "reason": "error", "message": str(exc)}
        takeover_id = getattr(args, "takeover_id", None)
        if isinstance(takeover_id, str) and _safe_takeover_id(takeover_id):
            # The app needs to know which takeover (and how far it got) to
            # decide on a rollback.
            document["id"] = takeover_id
            document["state"] = _journal_state(_takeover_root() / takeover_id)
        print(json.dumps(document))
        return 1
    print(json.dumps(result))
    if action == "plan":
        return EXIT_REFUSED if result.get("action") == ACTION_REFUSE else EXIT_OK
    return EXIT_OK if result.get("ok") is True else EXIT_REFUSED
