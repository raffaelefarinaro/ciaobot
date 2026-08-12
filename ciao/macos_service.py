"""UI-independent macOS service control for the Ciaobot desktop shell."""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Sequence

SERVER_LABEL = "com.ciao.server"
LEGACY_MENUBAR_LABEL = "com.ciao.menubar"
DEFAULT_PORT = 8443
MIGRATION_RECEIPT = "desktop-migration.json"
DESKTOP_BUNDLE_ID = "local.ciaobot.app"
DESKTOP_EXECUTABLE = "ciaobot-desktop"
LEGACY_EXECUTABLE = "CiaobotServer"

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class DesktopRuntime:
    workspace: str
    runtime_root: str
    port: int
    server_plist: str
    python_path: str


@dataclass(frozen=True, slots=True)
class ServiceResult:
    ok: bool
    action: str
    message: str
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] in {'"', "'"} and value[-1:] == value[:1]:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def discover_runtime(
    *,
    launch_agents_dir: Path | None = None,
    environ: dict[str, str] | None = None,
) -> DesktopRuntime:
    """Resolve workspace, port, and runtime root using the desktop contract."""

    env = os.environ if environ is None else environ
    agents = (
        Path.home() / "Library" / "LaunchAgents"
        if launch_agents_dir is None
        else Path(launch_agents_dir).expanduser()
    )
    plist_path = agents / f"{SERVER_LABEL}.plist"
    plist: dict[str, Any] = {}
    try:
        with plist_path.open("rb") as handle:
            loaded = plistlib.load(handle)
        if isinstance(loaded, dict):
            plist = loaded
    except (OSError, plistlib.InvalidFileException):
        pass

    plist_env = plist.get("EnvironmentVariables")
    if not isinstance(plist_env, dict):
        plist_env = {}
    workspace_raw = str(
        plist_env.get("CIAO_WORKSPACE")
        or plist.get("WorkingDirectory")
        or env.get("CIAO_WORKSPACE")
        or ""
    ).strip()
    workspace = Path(workspace_raw).expanduser().resolve() if workspace_raw else None
    dotenv = _read_dotenv(workspace / ".env") if workspace else {}

    raw_port = str(
        dotenv.get("PWA_PORT")
        or plist_env.get("CIAO_PORT")
        or env.get("CIAO_PORT")
        or DEFAULT_PORT
    )
    try:
        port = int(raw_port)
    except ValueError:
        port = DEFAULT_PORT
    if not 1 <= port <= 65535:
        port = DEFAULT_PORT

    runtime_raw = str(dotenv.get("CIAO_RUNTIME_ROOT") or "").strip()
    if runtime_raw:
        runtime = Path(runtime_raw).expanduser()
        if not runtime.is_absolute() and workspace:
            runtime = workspace / runtime
    elif workspace:
        runtime = workspace / ".runtime"
    else:
        runtime = Path(".runtime").resolve()

    arguments = plist.get("ProgramArguments")
    python_path = (
        str(arguments[0])
        if isinstance(arguments, list) and arguments
        else sys.executable
    )
    return DesktopRuntime(
        workspace=str(workspace or ""),
        runtime_root=str(runtime.resolve()),
        port=port,
        server_plist=str(plist_path),
        python_path=python_path,
    )


def _launchctl(
    args: Sequence[str],
    *,
    runner: Runner = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    return runner(
        ["launchctl", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _command_error(completed: subprocess.CompletedProcess[str]) -> str:
    return (completed.stderr or completed.stdout or "").strip()


def _active_chat_ids(port: int, *, timeout: float = 2.0) -> list[str]:
    try:
        with urllib.request.urlopen(
            f"http://localhost:{port}/api/active-chats",
            timeout=timeout,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, ValueError):
        return []
    values = payload.get("active_chat_ids") if isinstance(payload, dict) else None
    return [str(value) for value in values] if isinstance(values, list) else []


def _server_reachable(port: int, *, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://localhost:{port}/api/startup-status",
            timeout=timeout,
        ) as response:
            return 200 <= int(response.status) < 300
    except (OSError, urllib.error.URLError, ValueError):
        return False


def service_status(
    *,
    runtime: DesktopRuntime | None = None,
    uid: int | None = None,
    runner: Runner = subprocess.run,
) -> ServiceResult:
    runtime = runtime or discover_runtime()
    resolved_uid = os.getuid() if uid is None else uid
    installed = Path(runtime.server_plist).is_file()
    try:
        loaded_result = _launchctl(
            ["print", f"gui/{resolved_uid}/{SERVER_LABEL}"],
            runner=runner,
        )
        loaded = loaded_result.returncode == 0
    except OSError:
        loaded = False
    reachable = _server_reachable(runtime.port)
    active = _active_chat_ids(runtime.port) if reachable else []
    return ServiceResult(
        ok=True,
        action="status",
        message="Ciaobot engine is running." if reachable else "Ciaobot engine is stopped.",
        details={
            **asdict(runtime),
            "installed": installed,
            "loaded": loaded,
            "reachable": reachable,
            "active_chat_ids": active,
        },
    )


def start_service(
    *,
    runtime: DesktopRuntime | None = None,
    uid: int | None = None,
    runner: Runner = subprocess.run,
) -> ServiceResult:
    runtime = runtime or discover_runtime()
    plist = Path(runtime.server_plist)
    if not plist.is_file():
        return ServiceResult(
            False,
            "start",
            "The server LaunchAgent is not installed.",
            {**asdict(runtime), "setup_required": True},
        )
    resolved_uid = os.getuid() if uid is None else uid
    domain = f"gui/{resolved_uid}"
    try:
        _launchctl(["enable", f"{domain}/{SERVER_LABEL}"], runner=runner)
        bootstrap = _launchctl(["bootstrap", domain, str(plist)], runner=runner)
        kickstart = _launchctl(
            ["kickstart", "-k", f"{domain}/{SERVER_LABEL}"],
            runner=runner,
        )
    except OSError as exc:
        return ServiceResult(False, "start", str(exc), asdict(runtime))
    if kickstart.returncode != 0:
        detail = _command_error(kickstart) or _command_error(bootstrap)
        return ServiceResult(False, "start", detail or "launchctl start failed", asdict(runtime))
    return ServiceResult(True, "start", "Ciaobot engine started.", asdict(runtime))


def stop_service(
    *,
    runtime: DesktopRuntime | None = None,
    force: bool = False,
    uid: int | None = None,
    runner: Runner = subprocess.run,
) -> ServiceResult:
    runtime = runtime or discover_runtime()
    active = _active_chat_ids(runtime.port)
    if active and not force:
        return ServiceResult(
            False,
            "stop",
            "Active chats must be confirmed before stopping the engine.",
            {**asdict(runtime), "active_chat_ids": active, "requires_confirmation": True},
        )
    resolved_uid = os.getuid() if uid is None else uid
    try:
        completed = _launchctl(
            ["bootout", f"gui/{resolved_uid}/{SERVER_LABEL}"],
            runner=runner,
        )
    except OSError as exc:
        return ServiceResult(False, "stop", str(exc), asdict(runtime))
    if completed.returncode != 0 and "could not find service" not in _command_error(completed).lower():
        return ServiceResult(
            False,
            "stop",
            _command_error(completed) or "launchctl stop failed",
            asdict(runtime),
        )
    return ServiceResult(True, "stop", "Ciaobot engine stopped.", asdict(runtime))


def restart_service(
    *,
    runtime: DesktopRuntime | None = None,
    force: bool = False,
    uid: int | None = None,
    runner: Runner = subprocess.run,
) -> ServiceResult:
    runtime = runtime or discover_runtime()
    active = _active_chat_ids(runtime.port)
    if active and not force:
        return ServiceResult(
            False,
            "restart",
            "Active chats must be confirmed before restarting the engine.",
            {**asdict(runtime), "active_chat_ids": active, "requires_confirmation": True},
        )
    resolved_uid = os.getuid() if uid is None else uid
    try:
        completed = _launchctl(
            ["kickstart", "-k", f"gui/{resolved_uid}/{SERVER_LABEL}"],
            runner=runner,
        )
    except OSError as exc:
        return ServiceResult(False, "restart", str(exc), asdict(runtime))
    if completed.returncode != 0:
        return ServiceResult(
            False,
            "restart",
            _command_error(completed) or "launchctl restart failed",
            asdict(runtime),
        )
    return ServiceResult(True, "restart", "Ciaobot engine restarted.", asdict(runtime))


def set_login_enabled(
    enabled: bool,
    *,
    runtime: DesktopRuntime | None = None,
    uid: int | None = None,
    runner: Runner = subprocess.run,
) -> ServiceResult:
    runtime = runtime or discover_runtime()
    if not Path(runtime.server_plist).is_file():
        return ServiceResult(
            False,
            "login",
            "The server LaunchAgent is not installed.",
            {**asdict(runtime), "setup_required": True},
        )
    resolved_uid = os.getuid() if uid is None else uid
    verb = "enable" if enabled else "disable"
    try:
        completed = _launchctl(
            [verb, f"gui/{resolved_uid}/{SERVER_LABEL}"],
            runner=runner,
        )
    except OSError as exc:
        return ServiceResult(False, "login", str(exc), asdict(runtime))
    if completed.returncode != 0:
        return ServiceResult(
            False,
            "login",
            _command_error(completed) or f"launchctl {verb} failed",
            asdict(runtime),
        )
    return ServiceResult(
        True,
        "login",
        f"Engine start at login {'enabled' if enabled else 'disabled'}.",
        {**asdict(runtime), "enabled": enabled},
    )


def update_engine(
    *,
    runtime: DesktopRuntime | None = None,
    force: bool = False,
    runner: Runner = subprocess.run,
) -> ServiceResult:
    from ciao.package_version import update_package

    runtime = runtime or discover_runtime()
    active = _active_chat_ids(runtime.port)
    if active and not force:
        return ServiceResult(
            False,
            "update-engine",
            "Active chats must be confirmed before updating Ciaobot.",
            {
                **asdict(runtime),
                "active_chat_ids": active,
                "requires_confirmation": True,
            },
        )
    bundled_engine = bool(
        runtime.python_path
        and "Ciaobot.app/Contents/Resources/ciao-runtime"
        in str(runtime.python_path)
    )
    if bundled_engine:
        return ServiceResult(
            False,
            "update-engine",
            "The bundled engine updates together with Ciaobot.app.",
            {
                **asdict(runtime),
                "already_current": True,
                "bundled": True,
                "command": (
                    "curl -fsSL https://github.com/raffaelefarinaro/ciaobot/"
                    "releases/latest/download/install.sh | sh"
                ),
            },
        )
    result = update_package()
    if not result.get("ok"):
        # A no-op upgrade is not a failure for callers that update the engine
        # and the app together: the app half may still have work to do.
        return ServiceResult(
            False,
            "update-engine",
            str(result.get("error") or "Engine update failed."),
            {
                **asdict(runtime),
                "already_current": bool(result.get("already_current")),
                "update": result,
            },
        )
    restarted = restart_service(runtime=runtime, force=True, runner=runner)
    return ServiceResult(
        restarted.ok,
        "update-engine",
        "Ciaobot engine updated and restarted." if restarted.ok else restarted.message,
        {**asdict(runtime), "update": result},
    )


def _unique_destination(directory: Path, name: str) -> Path:
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return directory / f"{candidate.stem}-{stamp}{candidate.suffix}"


def _bundle_matches(app: Path, *, executable: str) -> bool:
    """Return whether *app* is one of Ciaobot's expected signed bundle shapes."""

    info_path = app / "Contents" / "Info.plist"
    try:
        with info_path.open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        return False
    return (
        isinstance(info, dict)
        and info.get("CFBundleIdentifier") == DESKTOP_BUNDLE_ID
        and info.get("CFBundleExecutable") == executable
        and (app / "Contents" / "MacOS" / executable).is_file()
    )


def _browser_pwa_paths(running_app: Path | None = None) -> list[str]:
    """Find browser-installed Ciaobot app shims without modifying them."""

    candidates = (
        Path.home() / "Applications" / "Chrome Apps.localized" / "Ciaobot.app",
        Path.home() / "Applications" / "Ciaobot.app",
    )
    running = Path(running_app).resolve() if running_app is not None else None
    return [
        str(candidate)
        for candidate in candidates
        if candidate.is_dir()
        and candidate.resolve() != running
        and not _bundle_matches(candidate, executable=DESKTOP_EXECUTABLE)
    ]


def migrate_legacy_companion(
    *,
    runtime: DesktopRuntime | None = None,
    running_app: Path | None = None,
    launch_agents_dir: Path | None = None,
    applications_dirs: Sequence[Path] | None = None,
    trash_dir: Path | None = None,
    uid: int | None = None,
    runner: Runner = subprocess.run,
) -> ServiceResult:
    runtime = runtime or discover_runtime(launch_agents_dir=launch_agents_dir)
    if running_app is not None:
        resolved_app = Path(running_app).resolve()
        allowed_roots = tuple(
            Path(root).resolve()
            for root in (
                applications_dirs
                or (Path("/Applications"), Path.home() / "Applications")
            )
        )
        if (
            not _bundle_matches(resolved_app, executable=DESKTOP_EXECUTABLE)
            or resolved_app.name != "Ciaobot.app"
            or resolved_app.parent not in allowed_roots
        ):
            return ServiceResult(
                False,
                "migrate",
                "Migration must be started by the installed Ciaobot.app.",
                {**asdict(runtime), "running_app": str(resolved_app)},
            )
    runtime_root = Path(runtime.runtime_root)
    migration_dir = runtime_root / "migration"
    migration_dir.mkdir(parents=True, exist_ok=True)
    agents = (
        Path.home() / "Library" / "LaunchAgents"
        if launch_agents_dir is None
        else Path(launch_agents_dir)
    )
    old_plist = agents / f"{LEGACY_MENUBAR_LABEL}.plist"
    backup_plist = migration_dir / old_plist.name
    receipt_path = migration_dir / MIGRATION_RECEIPT
    resolved_uid = os.getuid() if uid is None else uid
    existing_receipt: dict[str, Any] = {}
    try:
        loaded_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if isinstance(loaded_receipt, dict):
            existing_receipt = loaded_receipt
    except (OSError, ValueError):
        pass
    already_migrated = bool(existing_receipt) and not old_plist.exists()
    moved_app_from = str(existing_receipt.get("legacy_app_original") or "")
    moved_app_to = str(existing_receipt.get("legacy_app_backup") or "")

    try:
        _launchctl(
            ["bootout", f"gui/{resolved_uid}/{LEGACY_MENUBAR_LABEL}"],
            runner=runner,
        )
        _launchctl(
            ["disable", f"gui/{resolved_uid}/{LEGACY_MENUBAR_LABEL}"],
            runner=runner,
        )
        if old_plist.exists():
            if backup_plist.exists():
                return ServiceResult(
                    False,
                    "migrate",
                    "A legacy menubar plist backup already exists.",
                    {**asdict(runtime), "backup": str(backup_plist)},
                )
            shutil.move(str(old_plist), str(backup_plist))

        app_roots = list(applications_dirs or (Path("/Applications"), Path.home() / "Applications"))
        desktop_installed = any(
            _bundle_matches(root / "Ciaobot.app", executable=DESKTOP_EXECUTABLE)
            for root in app_roots
        )
        old_app = next(
            (
                root / "Ciaobot Server.app"
                for root in app_roots
                if _bundle_matches(
                    root / "Ciaobot Server.app",
                    executable=LEGACY_EXECUTABLE,
                )
            ),
            None,
        )
        if (
            not moved_app_to
            and desktop_installed
            and old_app is not None
            and _server_reachable(runtime.port)
        ):
            trash = Path.home() / ".Trash" if trash_dir is None else Path(trash_dir)
            trash.mkdir(parents=True, exist_ok=True)
            destination = _unique_destination(trash, old_app.name)
            shutil.move(str(old_app), str(destination))
            moved_app_from = str(old_app)
            moved_app_to = str(destination)

        prefs = runtime_root / "menubar_prefs.json"
        notifications_enabled: bool | None = None
        if prefs.is_file():
            try:
                payload = json.loads(prefs.read_text(encoding="utf-8"))
                value = payload.get("notifications_enabled")
                if isinstance(value, bool):
                    notifications_enabled = value
            except (OSError, ValueError):
                pass
        receipt = {
            "schema_version": 1,
            "migrated_at": existing_receipt.get("migrated_at")
            or datetime.now(UTC).isoformat(),
            "legacy_plist_original": existing_receipt.get("legacy_plist_original")
            or (str(old_plist) if backup_plist.exists() else ""),
            "legacy_plist_backup": existing_receipt.get("legacy_plist_backup")
            or (str(backup_plist) if backup_plist.exists() else ""),
            "legacy_app_original": moved_app_from,
            "legacy_app_backup": moved_app_to,
            "notifications_enabled": (
                existing_receipt.get("notifications_enabled")
                if existing_receipt.get("notifications_enabled") is not None
                else notifications_enabled
            ),
        }
        temporary = receipt_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, receipt_path)
    except OSError as exc:
        return ServiceResult(False, "migrate", str(exc), asdict(runtime))

    return ServiceResult(
        True,
        "migrate",
        (
            "Legacy companion was already migrated."
            if already_migrated and not moved_app_from
            else "Legacy companion disabled and migration backup recorded."
        ),
        {
            **asdict(runtime),
            "receipt": str(migration_dir / MIGRATION_RECEIPT),
            "already_migrated": already_migrated,
            "browser_pwa_paths": _browser_pwa_paths(running_app),
        },
    )


def rollback_legacy_companion(
    *,
    runtime: DesktopRuntime | None = None,
    launch_agents_dir: Path | None = None,
    uid: int | None = None,
    runner: Runner = subprocess.run,
) -> ServiceResult:
    runtime = runtime or discover_runtime(launch_agents_dir=launch_agents_dir)
    receipt_path = Path(runtime.runtime_root) / "migration" / MIGRATION_RECEIPT
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ServiceResult(
            False,
            "rollback",
            "No valid desktop migration receipt was found.",
            asdict(runtime),
        )
    try:
        original_plist_raw = str(receipt.get("legacy_plist_original") or "")
        backup_plist_raw = str(receipt.get("legacy_plist_backup") or "")
        original_plist = Path(original_plist_raw) if original_plist_raw else None
        backup_plist = Path(backup_plist_raw) if backup_plist_raw else None
        if original_plist is not None and backup_plist is not None and backup_plist.is_file():
            original_plist.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(backup_plist), str(original_plist))

        original_app_raw = str(receipt.get("legacy_app_original") or "")
        backup_app_raw = str(receipt.get("legacy_app_backup") or "")
        original_app = Path(original_app_raw) if original_app_raw else None
        backup_app = Path(backup_app_raw) if backup_app_raw else None
        if original_app is not None and backup_app is not None and backup_app.is_dir():
            original_app.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(backup_app), str(original_app))

        # Deliberately not re-registering the menu-bar LaunchAgent. Its
        # ProgramArguments exec `ciao menubar`, and both that subcommand and the
        # rumps helper behind it were deleted in this release -- bootstrapping
        # it would leave launchd retrying a command that exits non-zero forever.
        # Rollback still restores the files from the backup so nothing is lost;
        # only the agent is left unloaded.
    except OSError as exc:
        return ServiceResult(False, "rollback", str(exc), asdict(runtime))
    return ServiceResult(
        True,
        "rollback",
        "Legacy companion restored from the migration backup.",
        {**asdict(runtime), "receipt": str(receipt_path)},
    )


def print_result(result: ServiceResult, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(result.to_dict(), sort_keys=True))
    else:
        stream = sys.stdout if result.ok else sys.stderr
        print(result.message, file=stream)
    return 0 if result.ok else 1
