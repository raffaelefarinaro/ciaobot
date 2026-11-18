"""Read-only classification of a 1.x install for the Ciaobot.app takeover (#3129).

``plan()`` answers one question: may the app's bundled engine take over this
Mac's launchd-managed engine, and which installer-owned leftovers could move to
the Trash once the bundled engine has proven healthy? It never writes, stops,
boots out or deletes anything; ``apply``/``finish``/``rollback`` are a later
slice and own every mutation.

Coupling rule (#3130 deletes the old installers): this module imports only
``ciao.macos_service``, ``ciao.os_support.*``, ``ciao.package_version`` and
``ciao.__version__``. The old-install paths, the shim marker, the receipt and
the hosted ``Ciaobot Server serve --python <abs>`` argv are literal constants
and are parsed locally, so deleting ``server_host``, ``install_receipt``,
``install_discovery`` and friends leaves this module working.

Every refusal is a fixed code in ``reason``; paths go in fields, never in the
reason.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from xml.parsers.expat import ExpatError

import ciao
from ciao import macos_service
from ciao.macos_service import (
    LAUNCHCTL,
    SERVER_LABEL,
    DesktopRuntime,
    Runner,
    RestartBlockersUnavailable,
    _getuid,
    discover_runtime,
    engine_identity,
    read_dotenv,
    read_lock_owner,
    restart_blockers,
)
from ciao.os_support.definition import DefinitionReadError, read_definition
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

#: Plist environment keys that move user data. Only safe when the workspace
#: ``.env`` sets the same key, because the bundled engine reads the ``.env``
#: and never this plist.
_DATA_LOCATION_KEYS = (
    "CIAO_VAULT_ROOT",
    "CIAO_MEMORY_DIR",
    "CIAO_VAULT_DIR",
    "CIAO_WORKSPACES",
)
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


def _real_home() -> Path | None:
    """This account's real home directory, ignoring ``$HOME``."""
    if sys.platform != "darwin":
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
        if rest[:1] == ["-I"]:
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
    if value is None or not isinstance(value.get("python"), str) or not value["python"]:
        return None
    return value


# --- trash candidates ----------------------------------------------------------


def _inside(path: Path, other: Path) -> bool:
    return path == other or other in path.parents


def _safe(path: Path, protected: list[Path], *, allow_symlink: bool = False) -> bool:
    """Whether ``path`` may be offered for the Trash.

    Absolute, strictly under HOME, not a top-level symlink unless
    the proof allows it, and neither equal to, inside, nor an ancestor of any
    protected path.
    """
    if not path.is_absolute():
        return False
    home = Path.home()
    lexical = Path(os.path.abspath(path))
    if home not in lexical.parents:
        return False
    try:
        is_link = lexical.is_symlink()
        if is_link and not allow_symlink:
            return False
        resolved = lexical.parent.resolve() / lexical.name if is_link else lexical.resolve()
    except OSError:
        return False
    try:
        for guarded in protected:
            target = guarded.resolve()
            if _inside(resolved, target) or _inside(target, resolved):
                return False
    except OSError:
        return False
    return True


def _server_host_candidates(program: str) -> list[TrashCandidate]:
    host = Path.home() / "Applications" / _HOST_APP_NAME
    bundle = PurePosixPath(program).parent.parent.parent
    if str(bundle) != str(host) or not host.exists():
        return []
    found = [TrashCandidate(str(host), "server_host_program")]
    record_path = Path.home() / ".local" / "state" / "ciaobot" / "server-host.json"
    record = _read_json(record_path)
    if record is not None and record.get("bundle_path") == str(host):
        found.append(TrashCandidate(str(record_path), "server_host_record"))
    return found


def _installer_candidates(python: str, receipt: dict[str, Any]) -> list[TrashCandidate]:
    env = _env_root(python)
    if env.name != _ENV_ROOT_NAME or not env.is_dir():
        return []
    found = [TrashCandidate(str(env), "receipt_env")]
    state = Path.home() / ".local" / "state" / "ciaobot"
    receipt_path = state / "install-receipt.json"
    if receipt_path.exists():
        found.append(TrashCandidate(str(receipt_path), "receipt"))
    uv = receipt.get("uv")
    if isinstance(uv, str) and uv:
        uv_path = Path(uv)
        if uv_path.is_absolute() and state in Path(os.path.abspath(uv_path)).parents and uv_path.exists():
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


# --- liveness ------------------------------------------------------------------


def _job_pid(label: str, runner: Runner) -> int | None:
    """The pid launchd reports for the job, or ``None``. Read-only ``launchctl print``."""
    try:
        completed = runner(
            [LAUNCHCTL, "print", f"gui/{_getuid()}/{label}"],
            capture_output=True,
            text=True,
            timeout=macos_service.JOB_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    value = macos_service.launchctl_print_field(completed.stdout or "", "pid")
    return int(value) if value.isdigit() else None


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

    # 3. bounded, strict read.
    try:
        read = read_definition(plist_path)
        document = plistlib.loads(read.data)
    except (DefinitionReadError, ValueError, ExpatError):
        return _refuse("unreadable_definition", label, plist=str(plist_path))
    if not isinstance(document, dict) or document.get("Label") != label:
        return _refuse("unreadable_definition", label, plist=str(plist_path))
    known: dict[str, Any] = {"plist": str(plist_path), "plist_sha256": read.sha256}

    # 4. workspace, runtime root and registry. environ={} means no shell fallback.
    runtime: DesktopRuntime = discover_runtime(
        launch_agents_dir=agents, environ={}, label=label
    )
    if not runtime.workspace:
        return _refuse("no_workspace", label, **known)
    workspace = Path(runtime.workspace)
    runtime_root = Path(runtime.runtime_root)
    known.update(workspace=str(workspace), runtime_root=str(runtime_root), port=runtime.port)
    if not workspace.is_dir() or not runtime_root.is_dir():
        return _refuse("no_registry", label, **known)
    try:
        json.loads(read_definition(runtime_root / "workspaces.json").data.decode("utf-8"))
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
    dotenv = read_dotenv(workspace / ".env")
    for key in _DATA_LOCATION_KEYS:
        if key in plist_env and key not in dotenv:
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
    old_version = ""
    if identity is not None:
        owner = read_lock_owner(runtime)
        if not _owns_listener(owner, identity, workspace):
            return _refuse("foreign_listener", label, **known)
        version = identity.get("version")
        old_version = version if isinstance(version, str) else ""
    if not old_version and receipt is not None and isinstance(receipt.get("version"), str):
        old_version = receipt["version"]
    if old_version:
        if _version_key(old_version) > _version_key(ciao.__version__):
            return _refuse("downgrade", label, old_version=old_version, **known)
        if _version_key(old_version) < _version_key(MIN_TAKEOVER_VERSION):
            return _refuse("engine_too_old", label, old_version=old_version, **known)
    try:
        active_chats = len(restart_blockers(runtime.port))
    except RestartBlockersUnavailable:
        return _refuse("blockers_unavailable", label, old_version=old_version, **known)
    old_running = identity is not None or _job_pid(label, runner) is not None

    # 9. trash candidates, only with proof; a checkout is never trashed.
    candidates: list[TrashCandidate] = []
    left_in_place: tuple[str, ...] = ()
    if program_kind == "server_host":
        candidates = _server_host_candidates(argv[0])
    elif program_kind == "installer_env" and receipt is not None and direct_python is not None:
        candidates = _installer_candidates(direct_python, receipt)
    elif program_kind == "checkout" and checkout is not None:
        left_in_place = (str(checkout),)
    protected = _protected_paths(workspace, runtime_root, dotenv)
    symlink_proofs = {"uv_tool_symlink", "installer_shim"}
    trash: list[TrashCandidate] = []
    for candidate in candidates:
        if candidate in trash:
            continue
        if _safe(Path(candidate.path), protected, allow_symlink=candidate.proof in symlink_proofs):
            trash.append(candidate)

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
    owner: dict[str, Any] | None, identity: dict[str, Any], workspace: Path
) -> bool:
    """The lock names the same pid the listener reports, for this workspace."""
    if owner is None:
        return False
    pid = identity.get("pid")
    owner_pid = owner.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or owner_pid != pid:
        return False
    owner_workspace = owner.get("workspace")
    if not isinstance(owner_workspace, str) or not owner_workspace:
        return False
    return _same_dir(Path(owner_workspace), workspace)


def _protected_paths(
    workspace: Path, runtime_root: Path, dotenv: dict[str, str]
) -> list[Path]:
    protected = [workspace, runtime_root, Path.home() / ".ciao", state_dir()]
    for key in _DATA_LOCATION_KEYS:
        value = dotenv.get(key, "")
        if value and _absolute(value):
            protected.append(Path(value))
    return protected


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
