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

Coupling rule (#3130 deletes the old installers): this module's own imports
are only ``ciao`` (for ``__version__``), ``ciao.app_engine`` (the workspace
pointer and its private-record writes), ``ciao.macos_service``,
``ciao.os_support.definition``, ``ciao.os_support.locks`` and
``ciao.os_support.private`` (function-local, POSIX-only ``fcntl``), and
``ciao.package_version``. The old-install
paths, the shim marker, the receipt and the hosted ``Ciaobot Server serve
--python <abs>`` argv are literal constants parsed locally, so it never imports
``server_host``, ``install_receipt``, ``install_discovery`` or friends itself.
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
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from xml.parsers.expat import ExpatError

import ciao
from ciao import app_engine, macos_service
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
    active_chats: int = 0
    trash: tuple[TrashCandidate, ...] = ()
    left_in_place: tuple[str, ...] = ()
    dropped_env: tuple[str, ...] = ()
    resume_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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


def _unfinished_journal() -> str:
    """The id of the newest takeover whose journal is not finished or rolled back."""
    root = state_dir() / "takeover"
    try:
        entries = sorted((e for e in root.iterdir() if e.is_dir()), key=lambda e: e.name)
    except OSError:
        return ""
    for entry in reversed(entries):
        journal = _read_json(entry / "journal.json")
        state = journal.get("state") if journal is not None else None
        # An unreadable journal is unfinished work, not a clean slate.
        if state not in _FINISHED_STATES:
            return entry.name
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
    runner: Runner = subprocess.run,
) -> TakeoverPlan:
    """Classify this Mac's old engine for takeover. Writes nothing."""
    # 0. isolation guard: a test never boots out the live engine, and production
    # always names the real label.
    refusal = _isolation_refusal(label)
    if refusal:
        return _refuse(refusal, label)

    # 1. unfinished journal wins over everything.
    resume_id = _unfinished_journal()
    if resume_id:
        return TakeoverPlan(action=ACTION_RESUME, label=label, resume_id=resume_id)

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


def _load_journal(takeover_id: str) -> dict[str, Any] | None:
    """The journal of ``takeover_id``, when it is intact and shaped right.

    ``None`` for an unknown or malformed id, a missing, unreadable or foreign
    journal, and one whose label, plist path, workspace, runtime root, port or
    hash cannot name what a rollback would have to restore. A corrupt journal
    is unfinished work plan() does not trust; nothing in the transaction is
    allowed to act on one.
    """
    if not _safe_takeover_id(takeover_id):
        return None
    journal = _read_json(_journal_path(takeover_id))
    if journal is None or journal.get("schema") != JOURNAL_SCHEMA:
        return None
    label = journal.get("label")
    plist = journal.get("plist")
    sha = journal.get("plist_sha256")
    workspace = journal.get("workspace")
    runtime_root = journal.get("runtime_root")
    port = journal.get("port")
    state = journal.get("state")
    if not isinstance(label, str) or not label or "/" in label or label in (".", ".."):
        return None
    if not isinstance(plist, str) or not _absolute(plist):
        return None
    parts = PurePosixPath(plist)
    if parts.name != f"{label}.plist" or parts.parent.name != "LaunchAgents":
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
    return journal


def _make_entry(entry: Path) -> None:
    """Create the takeover entry and its parents inside the engine state dir.

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


def _write_journal(entry: Path, journal: dict[str, Any]) -> None:
    """Replace the journal in one atomic, owner-only, fsynced write."""
    from ciao.os_support.private import (
        is_private_descriptor_strict,
        make_private,
        mkstemp_private,
    )

    payload = {"schema": JOURNAL_SCHEMA, "updated": _utc_stamp(), **journal}
    fd, tmp = mkstemp_private(dir=entry, prefix=f".{JOURNAL_NAME}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            if not is_private_descriptor_strict(handle.fileno()):
                raise OSError(
                    errno.EPERM, f"the takeover journal was not writeable privately: {tmp}"
                )
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, entry / JOURNAL_NAME)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    make_private(entry / JOURNAL_NAME)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_copy(source: Path, target: Path) -> None:
    """Copy ``source`` to ``target`` with copied metadata and durable content."""
    shutil.copy2(source, target)
    with target.open("rb") as handle:
        os.fsync(handle.fileno())


def _run_launchctl(arguments: list[str], runner: Runner) -> subprocess.CompletedProcess[str]:
    """One ``launchctl`` call: fixed absolute argv, captured output, a timeout."""
    return runner(
        [LAUNCHCTL, *arguments],
        capture_output=True,
        text=True,
        timeout=_LAUNCHCTL_TIMEOUT_S,
        check=False,
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


def _new_takeover_id() -> str:
    stamp = _utc_stamp()
    candidate = stamp
    suffix = 1
    while _journal_path(candidate).parent.exists():
        suffix += 1
        candidate = f"{stamp}-{suffix}"
    return candidate


def _begin_takeover(current: TakeoverPlan) -> str:
    """Create the takeover entry, back up the plist, journal ``started``.

    Returns the takeover id. The backup exists — copied with metadata and
    fsynced, and verified against the hash the app approved — before any
    engine is stopped: if the takeover never gets past this point, nothing of
    the old install was touched.

    A journal id is the UTC stamp; a collision (two takes in one second) gets
    a ``-N`` suffix, which keeps the lexicographic order of directory names.
    """
    takeover_id = _new_takeover_id()
    entry = _takeover_root() / takeover_id
    _make_entry(entry)
    label = current.label
    try:
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
        # approved no longer names what is on disk. Remove the young journal
        # again: leaving it would pin every later plan() on a resume.
        shutil.rmtree(entry, ignore_errors=True)
        raise _ShaMismatch()
    journal: dict[str, Any] = {
        "id": takeover_id,
        "state": STATE_STARTED,
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
        "app_version": ciao.__version__,
    }
    _write_journal(entry, journal)
    return takeover_id


def _stop_old_engine(
    journal: dict[str, Any],
    *,
    runner: Runner,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> str | None:
    """Boot the old job out and wait for silence. A fixed refusal on timeout."""
    uid = _getuid()
    label = str(journal["label"])
    _run_launchctl(["bootout", f"gui/{uid}/{label}"], runner=runner)
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
    # restores what launchd was running.
    _run_launchctl(["bootstrap", f"gui/{uid}", str(journal["plist"])], runner=runner)
    return REF_DID_NOT_STOP


def _remove_plist(entry: Path, journal: dict[str, Any]) -> str | None:
    """Unlink the LaunchAgents definition once the backup is proven. ``None`` ok."""
    backup = _backup_path(entry, str(journal["label"]))
    if backup.is_symlink() or not backup.is_file() or _sha256_file(backup) != str(journal["plist_sha256"]):
        # Without the verified backup a rollback could not restore this file,
        # so the unlink is refused: the takeover stays resumable instead.
        return REF_BACKUP_UNUSABLE
    plist = Path(str(journal["plist"]))
    plist.unlink(missing_ok=True)
    return None


def apply(
    *,
    expect_sha256: str,
    launch_agents_dir: Path | None = None,
    label: str = SERVER_LABEL,
    runner: Runner = subprocess.run,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Stop the old engine and hand its workspace to the app's engine.

    Re-runs :func:`plan` and refuses unless it says ``takeover`` (or ``resume``
    of the takeover this journal already records) and the plist hash matches
    ``expect_sha256`` — the hash the caller saw in the plan JSON it acted on,
    so what is on disk now is what was approved. Every step is journalled and
    idempotent on a resume; a successful apply ends at ``pointer_written``,
    leaving the old plist backed up and the old job gone.
    """
    current = plan(launch_agents_dir=launch_agents_dir, label=label, runner=runner)
    if current.action == ACTION_RESUME:
        takeover_id = current.resume_id
        journal = _load_journal(takeover_id)
        if journal is None:
            return _refused(REF_NO_JOURNAL, id=takeover_id)
        if journal.get("state") in _FINISHED_STATES or journal.get("state") not in _KNOWN_STATES:
            return _refused(REF_ALREADY_SETTLED, id=takeover_id, state=journal.get("state"))
        if journal.get("plist_sha256") != expect_sha256:
            return _refused(REF_SHA_MISMATCH, id=takeover_id)
    elif current.action == ACTION_TAKEOVER:
        if current.plist_sha256 != expect_sha256:
            return _refused(REF_SHA_MISMATCH, id="")
        try:
            takeover_id = _begin_takeover(current)
        except _ShaMismatch:
            return _refused(REF_SHA_MISMATCH, id="")
    else:
        return _refused(REF_NOT_TAKEOVER, action=current.action)

    while True:
        journal = _load_journal(takeover_id)
        if journal is None:
            return _refused(REF_NO_JOURNAL, id=takeover_id)
        entry = _takeover_root() / takeover_id
        state = str(journal["state"])
        if state in _FINISHED_STATES:
            # A rollback (or a finish) settled this takeover while apply was
            # running; the journal on disk wins over this process's plan.
            return _refused(REF_ALREADY_SETTLED, id=takeover_id, state=state)
        if state == STATE_STARTED:
            refusal = _stop_old_engine(journal, runner=runner, clock=clock, sleep=sleep)
            if refusal is not None:
                journal["state"] = STATE_ROLLED_BACK
                _write_journal(entry, journal)
                return _refused(refusal, id=takeover_id, state=STATE_ROLLED_BACK)
            journal["state"] = STATE_STOPPING
            _write_journal(entry, journal)
        elif state == STATE_STOPPING:
            refusal = _remove_plist(entry, journal)
            if refusal is not None:
                return _refused(refusal, id=takeover_id, state=state)
            journal["state"] = STATE_PLIST_REMOVED
            _write_journal(entry, journal)
        elif state == STATE_PLIST_REMOVED:
            app_engine.write_pointer(
                Path(str(journal["workspace"])), Path(str(journal["runtime_root"]))
            )
            journal["state"] = STATE_POINTER_WRITTEN
            _write_journal(entry, journal)
        elif state == STATE_POINTER_WRITTEN:
            return {"ok": True, "id": takeover_id, "state": STATE_POINTER_WRITTEN}
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


def _health_problem(journal: dict[str, Any]) -> str | None:
    """The bundled engine's health proof, as the first check that fails.

    All of these must hold: the app-owned pointer names the journal's
    workspace; the ready record names a live pid and the same workspace and
    port; whatever answers the port identifies itself with that pid and
    startup id; and the runtime lock is held by that pid.
    """
    workspace = Path(str(journal["workspace"]))
    port = int(journal["port"])
    pointer = app_engine.read_pointer()
    if pointer is None or not _same_dir(pointer.workspace, workspace):
        return "pointer"
    ready = _read_json(state_dir() / app_engine.READY_RECORD_NAME)
    if ready is None:
        return "ready"
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
        return "ready"
    if ready_port != port or not _same_dir(Path(ready_workspace), workspace) or not _pid_alive(pid):
        return "ready"
    identity = engine_identity(port)
    if identity is None or identity.get("startup_id") != startup_id or identity.get("pid") != pid:
        return "listener"
    owner = read_lock_owner(_journal_runtime(journal))
    if not isinstance(owner, dict) or owner.get("pid") != pid:
        return "lock"
    return None


def _finish_trash(journal: dict[str, Any]) -> list[dict[str, str]]:
    """The journal's candidates that may still be trashed, re-filtered.

    Every candidate passes ``_safe`` again against the workspace and runtime
    root this journal took over, and must still exist — a candidate the app
    already trashed on an earlier finish drops out, so a lost answer can be
    re-fetched harmlessly.
    """
    workspace = Path(str(journal["workspace"]))
    runtime_root = Path(str(journal["runtime_root"]))
    protected = [workspace, runtime_root, Path.home() / ".ciao", state_dir()]
    found: list[dict[str, str]] = []
    for item in journal.get("trash") or []:
        if not isinstance(item, dict):
            continue
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
        path = str(checked)
        if all(existing["path"] != path for existing in found):
            found.append({"path": path, "proof": proof})
    return found


def _left_in_place(journal: dict[str, Any]) -> list[str]:
    values = journal.get("left_in_place")
    return [str(value) for value in values] if isinstance(values, list) else []


def finish(*, takeover_id: str, done: bool = False, runner: Runner = subprocess.run) -> dict[str, Any]:
    """Prove the bundled engine is healthy and name what the app may trash.

    With ``done`` the app closes the takeover after trashing (and recording
    ``trash.json`` itself): the journal ends ``finished``. Any missing proof
    keeps the journal open and refuses ``not_healthy``.
    """
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
    if done:
        if state != STATE_HEALTHY:
            return _refused(REF_NOT_HEALTHY, id=takeover_id, state=state)
        journal["state"] = STATE_FINISHED
        _write_journal(entry, journal)
        return settled
    if state not in (STATE_POINTER_WRITTEN, STATE_HEALTHY):
        return _refused(REF_NOT_HEALTHY, id=takeover_id, state=state)
    problem = _health_problem(journal)
    if problem is not None:
        return _refused(REF_NOT_HEALTHY, id=takeover_id, state=state, check=problem)
    journal["state"] = STATE_HEALTHY
    _write_journal(entry, journal)
    return {
        "ok": True,
        "id": takeover_id,
        "trash": _finish_trash(journal),
        "left_in_place": _left_in_place(journal),
    }


def rollback(
    *,
    takeover_id: str,
    runner: Runner = subprocess.run,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Restore the old service for a takeover that never became healthy.

    The app has already stopped its bundled engine (this refuses while its
    lock is still held), removes the workspace pointer if it names the
    journal's workspace, restores the old plist byte-identical from the
    verified backup, bootstraps the old job and waits for the old engine to
    answer. Ends with the journal ``rolled_back`` either way.
    """
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
    if _runtime_lock_held(runtime_root):
        return _refused(REF_BUNDLED_RUNNING, id=takeover_id, state=state)
    # Every refusal comes before the first mutation: a rollback that cannot
    # restore the old plist must not have cleared the pointer already.
    restore_from: Path | None = None
    if os.path.lexists(plist):
        if plist.is_symlink() or not plist.is_file() or _sha256_file(plist) != str(journal["plist_sha256"]):
            return _refused(REF_PLIST_CONFLICT, id=takeover_id, state=state)
    else:
        restore_from = _backup_path(entry, label)
        if (
            restore_from.is_symlink()
            or not restore_from.is_file()
            or _sha256_file(restore_from) != str(journal["plist_sha256"])
        ):
            return _refused(REF_BACKUP_UNUSABLE, id=takeover_id, state=state)
    app_engine.clear_pointer(expected_workspace=Path(str(journal["workspace"])))
    if restore_from is not None:
        _fsync_copy(restore_from, plist)
    _run_launchctl(["bootstrap", f"gui/{_getuid()}", str(plist)], runner=runner)
    journal["state"] = STATE_ROLLED_BACK
    _write_journal(entry, journal)
    if not _wait_until(
        lambda: engine_identity(int(journal["port"])) is not None,
        timeout_s=_ROLLBACK_START_S,
        clock=clock,
        sleep=sleep,
    ):
        return _refused(REF_DID_NOT_START, id=takeover_id, state=STATE_ROLLED_BACK)
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
        print(json.dumps({"action": "error", "reason": "error", "message": str(exc)}))
        return 1
    print(json.dumps(result))
    if action == "plan":
        return EXIT_REFUSED if result.get("action") == ACTION_REFUSE else EXIT_OK
    return EXIT_OK if result.get("ok") is True else EXIT_REFUSED
