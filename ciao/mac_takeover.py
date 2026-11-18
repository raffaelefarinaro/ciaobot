"""Read-only classification of a 1.x install for the Ciaobot.app takeover (#3129).

``plan()`` answers one question: may the app's bundled engine take over this
Mac's launchd-managed engine, and which installer-owned leftovers could move to
the Trash once the bundled engine has proven healthy? It never writes, stops,
boots out or deletes anything; ``apply``/``finish``/``rollback`` are a later
slice and own every mutation.

Coupling rule (#3130 deletes the old installers): this module's own imports
are only ``ciao`` (for ``__version__``), ``ciao.macos_service``,
``ciao.os_support.definition`` and ``ciao.package_version``. The old-install
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

import http.client
import json
import os
import plistlib
import re
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from xml.parsers.expat import ExpatError

import ciao
from ciao import macos_service
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
    symlink_proofs = {"uv_tool_symlink", "installer_shim"}
    trash: list[TrashCandidate] = []
    for candidate in candidates:
        checked = _safe(
            Path(candidate.path),
            protected,
            expected_root=_PROOF_ROOTS[candidate.proof],
            allow_symlink=candidate.proof in symlink_proofs,
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


# --- CLI -----------------------------------------------------------------------


def run_cli(args: Any) -> int:
    """``ciao mac-takeover plan``: JSON on stdout; exit 0 ok, 3 refused, 1 error."""
    try:
        action = args.mac_takeover_action
        if action != "plan":  # pragma: no cover - argparse constrains the action.
            raise ValueError(f"unknown action {action}")
        result = plan(launch_agents_dir=args.launch_agents_dir, label=args.label)
    except Exception as exc:  # noqa: BLE001 - the app reads one JSON document.
        print(json.dumps({"action": "error", "reason": "error", "message": str(exc)}))
        return 1
    print(json.dumps(result.to_dict()))
    return EXIT_REFUSED if result.action == ACTION_REFUSE else EXIT_OK
