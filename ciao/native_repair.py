"""Native repair coordinator (#2485): ``python -I -m ciao.native_repair run ...``.

Started only by ``install.sh --native-repair``. It speaks the native-install
event contract unchanged (schema 1, at most 1024 bytes a line, at most 32
events, ``service_ready`` as the only success) but has no wizard:
``EventWriter(repair=True)`` allows the phases install, activation and
readiness only. Classification is read-only and refuses with ``not_installed``,
``install_in_progress`` or ``unknown_state`` before any mutation. A component is
replaced only when it fails verification: the server host, the uv tool
environment, the persisted uv and receipt, and the LaunchAgent. The job is then
restarted and must answer on the workspace's configured port. The coordinator
never writes the workspace, never removes a lock, and runs no rollback, like
install mode. SIGTERM ends the run as ``failed(cancelled)``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
import os
import plistlib
import signal
import stat
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Final, TypeVar

from ciao import (
    install_discovery,
    install_receipt,
    macos_service,
    native_handoff,
    native_installer,
    native_lock,
    native_readiness,
    release_manifest,
    server_host,
    server_host_install,
)
from ciao.app_settings import DEFAULT_PWA_HOST, read_app_settings
from ciao.native_installer import (
    EventError,
    EventWriter,
    NativeCancellation,
    NativeCoordinatorError,
    NativeInstallArtifacts,
    _check_stage,
    _ChildCleanupFailed,
    _Cancelled,
    _Deadline,
    _map_host_failure,
    _map_lock_failure,
    _Refusal,
    _remaining,
    _sub_deadline,
    _TimedOut,
    _wait_observing,
)
from ciao.native_lock import NativeLockError, NativeRunLock, NativeStateDir
from ciao.native_readiness import NativeReadinessProof
from ciao.os_support.definition import DefinitionReadError, read_definition
from ciao.provider_versions import compare_versions
from ciao.server_host import HostOwnership
from ciao.server_host_install import ServerHostInstallError

NATIVE_REPAIR_PROTOCOL: Final[int] = 1
REPAIR_EVENT_BUDGET: Final[int] = 4
REPAIR_TEARDOWN_TIMEOUT_SECONDS: Final[float] = 45.0
REPAIR_PORT_BIND_TIMEOUT_SECONDS: Final[float] = 10.0
REPAIR_POLL_INTERVAL_SECONDS: Final[float] = 0.5

#: Caller variables that name another install surface or run inside the engine.
_REFUSED_ENVIRONMENT: Final[tuple[str, ...]] = (
    "UV_TOOL_DIR",
    "UV_TOOL_BIN_DIR",
    "CIAO_CHAT_ID",
)
_PROBE_CODE: Final[str] = "import ciao, ciao.main, sys; sys.stdout.write(ciao.__version__)"

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class _RepairPolicy:
    """Finite deadlines for one repair run. Mirrors ``native_installer._CoordinatorPolicy``."""

    run_timeout: float = native_installer.COORDINATOR_RUN_TIMEOUT_SECONDS
    readiness_timeout: float = native_installer.COORDINATOR_READINESS_TIMEOUT_SECONDS
    readiness_interval: float = native_installer.COORDINATOR_READINESS_INTERVAL_SECONDS
    poll_interval: float = native_installer.COORDINATOR_POLL_INTERVAL_SECONDS
    port_bind_timeout: float = REPAIR_PORT_BIND_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        for name in (
            "run_timeout",
            "readiness_timeout",
            "readiness_interval",
            "poll_interval",
            "port_bind_timeout",
        ):
            value = getattr(self, name)
            if type(value) not in (int, float):
                raise NativeCoordinatorError("invalid_timeout", "internal_error")
            number = float(value)
            if (
                not math.isfinite(number)
                or number <= 0
                or number > native_installer.COORDINATOR_MAX_TIMEOUT_SECONDS
            ):
                raise NativeCoordinatorError("invalid_timeout", "internal_error")


@dataclass(frozen=True, slots=True)
class NativeRepairRequest:
    """The validated public inputs for one repair run. There is no workspace flag."""

    run_id: str
    events_fd: int
    first_seq: int
    prior_events: int


@dataclass(frozen=True, slots=True)
class RepairTarget:
    """What read-only classification proved about the installed engine."""

    workspace: Path
    port: int
    host: str
    definition: Path
    definition_present: bool
    service_path: str
    job_registered: bool


class HostPlan(Enum):
    """What the server host needs: nothing, nothing to change, or a verified swap."""

    absent = "absent"
    current = "current"
    replace = "replace"


@dataclass(frozen=True, slots=True)
class NativeRepairOutcome:
    """What the repair attempted and completed, without logs or strings."""

    run_id: str
    terminal: str | None
    failure_code: str | None
    mutation_attempted: bool
    activation_attempted: bool
    activation_completed: bool
    lock_retained: bool
    workspace: Path | None
    port: int | None

    @property
    def exit_code(self) -> int:
        """0 only when ``service_ready`` was emitted."""
        return 0 if self.terminal == "service_ready" else 1


@dataclass(frozen=True, slots=True)
class _RepairDeps:
    """Every external effect of a repair run. Tests inject all of them."""

    validate_inputs: Callable[[], None]
    classify: Callable[[str | None], RepairTarget]
    prepare_state_dir: Callable[[], NativeStateDir]
    acquire_lock: Callable[[NativeStateDir, str], NativeRunLock]
    plan_host: Callable[[], HostPlan]
    probe_engine: Callable[[], str | None]
    receipt_version: Callable[[], str | None]
    stop_service: Callable[[float], None]
    ensure_host: Callable[[NativeRunLock, str, HostPlan], HostOwnership]
    reinstall_engine: Callable[[float], None]
    ensure_receipt: Callable[[], None]
    render_definition: Callable[[RepairTarget, HostOwnership], bytes]
    write_definition: Callable[[RepairTarget, bytes], str]
    port_bindable: Callable[[int, str], bool]
    bootstrap: Callable[[Path, str], None]
    prove_ready: Callable[[RepairTarget, HostOwnership, float], NativeReadinessProof]


# ── Classification (read-only) ────────────────────────────────────────────


def _lstat_or_none(path: Path) -> os.stat_result | None:
    """``lstat`` with absence as ``None``; any other failure makes the state unknown."""
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        raise _Refusal("unknown_state") from None


def _configured_port(workspace: Path) -> int:
    """The workspace's ``PWA_PORT``, or the engine's default when it is unset."""
    env_file = workspace / ".env"
    if _lstat_or_none(env_file) is None:
        return macos_service.DEFAULT_PORT
    try:
        text = install_discovery._read_native_text(env_file)
    except Exception:
        raise _Refusal("unknown_state") from None
    raw = macos_service.parse_dotenv(text).get("PWA_PORT", "").strip()
    if not raw:
        return macos_service.DEFAULT_PORT
    if not raw.isdecimal():
        raise _Refusal("unknown_state")
    port = int(raw)
    if not 1 <= port <= 65535:
        raise _Refusal("unknown_state")
    return port


def _configured_host(workspace: Path) -> str:
    """The workspace's bind host, or the engine's default when unset or invalid.

    The bind address lives in Settings (``app_settings.json``), not ``.env``.
    ``read_app_settings`` is the pure read: it never writes, and degrades an
    unreadable file to the defaults, so this has no new refusal path.
    """
    settings = read_app_settings(workspace / ".runtime" / "app_settings.json")
    return settings.pwa_host or DEFAULT_PWA_HOST


def _install_mode_path() -> str:
    """The PATH setup bakes into a fresh definition: the user's login shell."""
    from ciao.tool_path import login_shell_path  # noqa: PLC0415

    return login_shell_path() or os.environ.get("PATH", "")


def _live_definition(
    definition: Path, *, tool_python: Path, bin_dir: Path
) -> tuple[Path, str]:
    """Verify the live LaunchAgent and return its workspace and service PATH."""
    if not stat.S_ISREG(os.lstat(definition).st_mode):
        raise _Refusal("unknown_state")
    try:
        parsed: object = plistlib.loads(
            read_definition(definition, max_bytes=macos_service.MAX_DEFINITION_BYTES).data
        )
    except Exception:
        raise _Refusal("unknown_state") from None
    if not isinstance(parsed, dict) or parsed.get("Label") != macos_service.SERVER_LABEL:
        raise _Refusal("unknown_state")
    env_vars: object = parsed.get("EnvironmentVariables")
    if not isinstance(env_vars, dict):
        raise _Refusal("unknown_state")
    workspace_text: object = env_vars.get("CIAO_WORKSPACE")
    if not isinstance(workspace_text, str) or not os.path.isabs(workspace_text):
        raise _Refusal("unknown_state")
    if env_vars.get("CIAO_RUNTIME_ROOT") != os.path.realpath(
        Path(workspace_text) / ".runtime"
    ):
        raise _Refusal("unknown_state")
    try:
        command = server_host.parse_service_command(parsed.get("ProgramArguments"))
    except Exception:
        raise _Refusal("unknown_state") from None
    owned = (
        command.mode == "hosted"
        and Path(command.program).is_relative_to(server_host.default_bundle_path())
        and command.python == str(tool_python)
    ) or (
        command.mode == "direct"
        and command.program in {str(tool_python), str(bin_dir / "ciao")}
    )
    if not owned:
        raise _Refusal("unknown_state")
    path_value: object = env_vars.get("PATH")
    if isinstance(path_value, str) and path_value:
        service_path = path_value
    else:
        service_path = _install_mode_path()
    return Path(workspace_text), service_path


def _classify_repair(
    *,
    environ: Mapping[str, str],
    tool_dir: Path,
    bin_dir: Path,
    job_absent: Callable[[], bool],
    override_disabled: Callable[[], bool],
    own_run_id: str | None = None,
) -> RepairTarget:
    """Prove the installed engine is repairable, or refuse. Read-only.

    Refusals: ``unknown_state`` for anything not provably ours, ``install_in_progress``
    for a lock another run holds, ``not_installed`` when there is no workspace or no
    native evidence (receipt, LaunchAgent or host record). A lock held by this run
    itself, ``own_run_id`` in this process, is not a conflict: the second
    classification runs under it.
    """
    if sys.platform != "darwin":
        raise _Refusal("unknown_state")
    for key in (*install_discovery._REDIRECT_VARIABLES, *_REFUSED_ENVIRONMENT):
        if environ.get(key, "").strip():
            raise _Refusal("unknown_state")
    tool_python = tool_dir / "ciaobot" / "bin" / "python"
    state_dir = install_receipt.default_receipt_path().parent
    if install_discovery._unsafe_native_state_dir(state_dir):
        raise _Refusal("unknown_state")
    try:
        lock = native_lock.inspect_native_lock(state_dir)
    except Exception:
        raise _Refusal("unknown_state") from None
    own = (
        own_run_id is not None
        and lock.state == "busy"
        and lock.run_id == own_run_id
        and lock.pid == os.getpid()
    )
    if lock.state == "absent" or own:
        pass
    elif lock.state in ("busy", "unknown"):
        native_installer._report_lock_holder(state_dir)
        raise _Refusal("install_in_progress")
    else:
        raise _Refusal("unknown_state")

    live_dir = macos_service.live_launch_agents_dir()
    for other in (
        state_dir / "migration",
        server_host_install.upgrade_journal_path(),
        live_dir / "com.ciao.menubar.plist",
    ):
        if _lstat_or_none(other) is not None:
            raise _Refusal("unknown_state")

    try:
        absent: object = job_absent()
        disabled: object = override_disabled()
    except Exception:
        raise _Refusal("unknown_state") from None
    if type(absent) is not bool or disabled is not False:
        raise _Refusal("unknown_state")
    registered = not absent

    definition = live_dir / f"{macos_service.SERVER_LABEL}.plist"
    present = _lstat_or_none(definition) is not None
    if present:
        workspace, service_path = _live_definition(
            definition, tool_python=tool_python, bin_dir=bin_dir
        )
    else:
        if registered:
            raise _Refusal("unknown_state")
        workspace = Path.home() / "Ciaobot"
        service_path = _install_mode_path()
    workspace = Path(os.path.realpath(workspace))
    try:
        set_up = native_handoff.workspace_is_set_up(workspace)
    except Exception:
        raise _Refusal("unknown_state") from None
    if not set_up:
        raise _Refusal("unknown_state" if present else "not_installed")
    receipt_path = install_receipt.default_receipt_path()
    has_receipt = _lstat_or_none(receipt_path) is not None
    has_host_record = _lstat_or_none(server_host.default_ownership_path()) is not None
    if not (present or has_receipt or has_host_record):
        # A workspace and a shim alone could be a user's own uv install; never reinstall that.
        raise _Refusal("not_installed")
    port = _configured_port(workspace)
    host = _configured_host(workspace)

    shim = bin_dir / "ciao"
    shim_info = _lstat_or_none(shim)
    if shim_info is not None:
        # A foreign ciao that uv --force would overwrite is not ours to repair.
        if not stat.S_ISLNK(shim_info.st_mode):
            raise _Refusal("unknown_state")
        try:
            link = os.readlink(shim)
        except OSError:
            raise _Refusal("unknown_state") from None
        resolved = Path(os.path.normpath(os.path.join(str(bin_dir), link)))
        if not resolved.is_relative_to(tool_dir / "ciaobot" / "bin"):
            raise _Refusal("unknown_state")

    if has_receipt:
        try:
            raw = install_discovery._read_native_text(receipt_path)
            receipt = install_receipt.parse_receipt(raw)
        except Exception:
            raise _Refusal("unknown_state") from None
        # A corrupt receipt parses to None and is rewritten later; a parsed one must be ours.
        if receipt is not None and (
            receipt.service_backend != "launchd"
            or receipt.service_label != macos_service.SERVER_LABEL
            or receipt.executable != str(bin_dir / "ciao")
            or receipt.python != str(tool_python)
        ):
            raise _Refusal("unknown_state")

    return RepairTarget(
        workspace=workspace,
        port=port,
        host=host,
        definition=definition,
        definition_present=present,
        service_path=service_path,
        job_registered=registered,
    )


def _verify_archive(artifacts: NativeInstallArtifacts, entry: Mapping[str, Any]) -> None:
    """The archive a create or swap would use must verify before the service stops."""
    try:
        server_host_install.verify_host_archive(artifacts.host_archive, entry)
    except Exception:
        raise _Refusal("verification_failed") from None


def _plan_host(
    artifacts: NativeInstallArtifacts,
) -> tuple[HostPlan, dict[str, Any], HostOwnership | None]:
    """Read-only: what the host at our install path needs, the verified entry, and
    for ``current`` the verified ownership. Every check that can fail runs here,
    before the service is stopped."""
    try:
        manifest = release_manifest.verify_manifest(
            artifacts.manifest.read_bytes(),
            artifacts.signature.read_text(encoding="utf-8"),
            release_manifest.RELEASE_PUBLIC_KEY,
        )
        entry = release_manifest.select_server_host_artifact(manifest)
    except Exception:
        raise _Refusal("verification_failed") from None
    bundle = server_host.default_bundle_path()
    record = server_host.default_ownership_path()
    if os.path.islink(bundle):
        raise _Refusal("unknown_state")
    if not os.path.lexists(bundle):
        if not os.path.lexists(record):
            _verify_archive(artifacts, entry)
            return HostPlan.absent, entry, None
        _verify_archive(artifacts, entry)
        return HostPlan.replace, entry, None
    try:
        policy = server_host_install._entry_policy(entry)
        state, _ = server_host_install._classify_host(
            bundle, record, subprocess.run, policy
        )
    except ServerHostInstallError as exc:
        if exc.code not in {server_host_install.HOST_EXISTS, server_host_install.POLICY_REFUSED}:
            raise _Refusal("unknown_state") from None
        state = server_host_install._REPLACE
    except Exception:
        raise _Refusal("unknown_state") from None
    if state == server_host_install._CURRENT:
        try:
            owned = server_host.verify_owned_host(bundle, ownership_path=record)
        except Exception:
            raise _Refusal("unknown_state") from None
        return HostPlan.current, entry, owned
    if state == server_host_install._REPLACE:
        _verify_archive(artifacts, entry)
        return HostPlan.replace, entry, None
    raise _Refusal("unknown_state")


def _probe_engine(
    tool_dir: Path,
    bin_dir: Path,
    *,
    env: Mapping[str, str],
    cwd: Path,
    cancellation: NativeCancellation,
) -> str | None:
    """The installed engine version when the tool environment is healthy, else None."""
    tool_python = tool_dir / "ciaobot" / "bin" / "python"
    if not os.path.isfile(tool_python) or not os.access(tool_python, os.X_OK):
        return None
    try:
        native_installer._interpreter_inside(tool_python, tool_dir)
        native_installer._shim_identity(bin_dir, tool_dir)
    except NativeCoordinatorError:
        return None
    try:
        code, output = native_installer._run_bounded(
            (str(tool_python), "-I", "-c", _PROBE_CODE),
            env=env,
            cwd=cwd,
            timeout=native_installer.ENGINE_PROBE_TIMEOUT_SECONDS,
            limit=native_installer.PROBE_OUTPUT_BYTES,
            cancellation=cancellation,
        )
    except native_installer._ToolCallFailed:
        return None
    if code != 0:
        return None
    text = output.decode("utf-8", errors="replace")
    if native_installer._VERSION_TEXT_RE.fullmatch(text) is None:
        return None
    return text


def _receipt_version() -> str | None:
    receipt = install_receipt.read_receipt()
    return None if receipt is None else receipt.version


# ── Mutations ─────────────────────────────────────────────────────────────


def _stop_service(remaining: float, cancellation: NativeCancellation) -> None:
    """Boot the registered job out and wait until launchd no longer has it.

    The wait observes the run's cancellation, so a SIGTERM during a slow teardown
    ends the run promptly as ``cancelled``.
    """
    macos_service.bootout_agent(macos_service.SERVER_LABEL)
    wait = _Deadline.after(min(remaining, REPAIR_TEARDOWN_TIMEOUT_SECONDS))
    while True:
        if cancellation.cancelled:
            raise _Cancelled
        try:
            if macos_service.server_job_absent():
                return
        except RuntimeError:
            raise _Refusal("install_failed") from None
        if wait.remaining() <= 0:
            raise _Refusal("install_failed")
        _wait_observing(
            min(REPAIR_POLL_INTERVAL_SECONDS, wait.remaining()),
            cancellation,
            REPAIR_POLL_INTERVAL_SECONDS,
        )


def _ensure_host(
    lock: NativeRunLock,
    run_id: str,
    plan: HostPlan,
    entry: Mapping[str, Any],
    artifacts: NativeInstallArtifacts,
    verified: HostOwnership | None,
) -> HostOwnership:
    """Create, replace or keep the host at our install path, per the plan.

    ``verified`` is the ownership :func:`_plan_host` already proved for ``current``.
    """
    if plan is HostPlan.absent:
        fresh = native_installer.create_fresh_host(
            run_lock=lock,
            run_id=run_id,
            manifest_bytes=artifacts.manifest.read_bytes(),
            signature_text=artifacts.signature.read_text(encoding="utf-8"),
            archive_path=artifacts.host_archive,
        )
        return fresh.ownership
    if plan is HostPlan.replace:
        lock.assert_held(run_id=run_id)
        replaced = server_host_install.repair_server_host(artifacts.host_archive, entry)
        lock.assert_held(run_id=run_id)
        return replaced
    if verified is None:
        raise NativeCoordinatorError("plan_missing", "internal_error")
    return verified


def _ensure_receipt(
    private: native_installer._PrivateRun,
    artifacts: NativeInstallArtifacts,
    tool_dir: Path,
    bin_dir: Path,
) -> None:
    """Re-provision the persisted uv, then rewrite the receipt only if a field differs."""
    uv_path = native_installer._persist_uv(
        native_installer._required(private.uv), artifacts.uv_sha256
    )
    expected: dict[str, str] = {
        "version": artifacts.version,
        "executable": str(bin_dir / "ciao"),
        "python": str(tool_dir / "ciaobot" / "bin" / "python"),
        "service_backend": "launchd",
        "service_label": macos_service.SERVER_LABEL,
        "uv": str(uv_path),
    }
    current = install_receipt.read_receipt()
    if current is None:
        receipt = install_receipt.InstallReceipt(
            version=artifacts.version,
            executable=expected["executable"],
            python=expected["python"],
            service_backend="launchd",
            service_label=macos_service.SERVER_LABEL,
            installed_at=datetime.now(UTC).isoformat(timespec="seconds"),
            uv=expected["uv"],
        )
    elif any(dataclasses.asdict(current)[key] != value for key, value in expected.items()):
        # Keeps installed_at and the previous_* fields, as the plan requires.
        receipt = dataclasses.replace(
            current,
            version=artifacts.version,
            executable=expected["executable"],
            python=expected["python"],
            service_backend="launchd",
            service_label=macos_service.SERVER_LABEL,
            uv=expected["uv"],
        )
    else:
        return
    _write_receipt_verified(receipt)


def _write_receipt_verified(receipt: install_receipt.InstallReceipt) -> None:
    """Write the receipt and require that reading it back returns the same one."""
    try:
        install_receipt.write_receipt(receipt)
        written = install_receipt.read_receipt()
    except (OSError, ValueError):
        raise NativeCoordinatorError("receipt_write_failed", "install_failed") from None
    if written != receipt:
        raise NativeCoordinatorError("receipt_write_failed", "install_failed")


def _carry_receipt_history(previous: install_receipt.InstallReceipt) -> None:
    """Keep installed_at and previous_* on the receipt a reinstall just wrote.

    Only a real update may rewrite those fields; a repair that reinstalls the
    same release must not erase the history the updater's rollback reads. The
    fresh install keeps ownership of version, executable, python and uv.
    """
    current = install_receipt.read_receipt()
    if current is None:
        raise NativeCoordinatorError("receipt_write_failed", "install_failed")
    carried = dataclasses.replace(
        current,
        installed_at=previous.installed_at,
        previous_version=previous.previous_version,
        previous_executable=previous.previous_executable,
    )
    _write_receipt_verified(carried)


def _reinstall_engine_preserving_history(
    previous: install_receipt.InstallReceipt | None,
    staged: NativeInstallArtifacts,
    tool_dir: Path,
    bin_dir: Path,
    remaining: float,
    *,
    cancellation: NativeCancellation,
    env: Mapping[str, str],
    cwd: Path,
) -> native_installer._EngineInstall:
    """Reinstall with ``--force``, then keep the receipt history it replaced."""
    installed = native_installer._install_engine(
        staged,
        tool_dir,
        bin_dir,
        remaining,
        cancellation=cancellation,
        env=env,
        cwd=cwd,
        force=True,
    )
    if previous is not None:
        _carry_receipt_history(previous)
    return installed


def _render_definition(target: RepairTarget, host: HostOwnership, tool_python: Path) -> bytes:
    from ciao.cli import _render_launchd_plist

    try:
        rendered = _render_launchd_plist(
            workspace=target.workspace,
            runtime_root=target.workspace / ".runtime",
            port=target.port,
            path=target.service_path,
            host=host,
            host_python=str(tool_python),
        )
    except ValueError:
        raise NativeCoordinatorError("definition_render_failed", "install_failed") from None
    return rendered.encode("utf-8")


def _write_definition(target: RepairTarget, rendered: bytes) -> str:
    """Write the LaunchAgent only when its bytes differ; return the sha256 of ``rendered``."""
    digest = hashlib.sha256(rendered).hexdigest()
    if target.definition_present:
        try:
            live: bytes | None = read_definition(
                target.definition, max_bytes=macos_service.MAX_DEFINITION_BYTES
            ).data
        except (DefinitionReadError, OSError):
            # Not a regular file (a symlink, say): the rename below replaces the
            # entry itself and never follows it.
            live = None
        if live == rendered:
            return digest
    parent = target.definition.parent
    tmp = parent / f".{target.definition.name}.{uuid.uuid4().hex}.tmp"
    created = False
    try:
        parent.mkdir(parents=True, exist_ok=True)
        # O_EXCL refuses an existing path, a symlink included; O_NOFOLLOW is the
        # same flag set as the install-mode definition write in ciao/cli.py.
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        created = True
        if sys.platform != "win32":  # os.fchmod is POSIX-only; the write is darwin-only anyway
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target.definition)
    except OSError:
        if created:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise NativeCoordinatorError("definition_write_failed", "install_failed") from None
    return digest


def _bootstrap(definition: Path, digest: str) -> None:
    status = macos_service.load_fresh_agent(definition, expected_sha256=digest)
    if status != 0:
        raise _Refusal("activation_refused")


def _prove_ready(
    target: RepairTarget,
    host: HostOwnership,
    remaining: float,
    *,
    version: str,
    tool_python: Path,
    cancellation: NativeCancellation,
) -> NativeReadinessProof:
    # fresh_install_service_absent is honest here: _stop_service (or the
    # classification) proved the job absent before this run's own bootstrap.
    return native_readiness.prove_native_readiness(
        native_readiness.NativeReadinessRequest(
            version=version,
            port=target.port,
            workspace=target.workspace,
            host_bundle=Path(host.bundle_path),
            ownership_path=server_host.default_ownership_path(),
            engine_python=tool_python,
            fresh_install_service_absent=True,
        ),
        deps=native_readiness.make_native_readiness_deps(),
        policy=native_readiness.NativeReadinessPolicy(
            timeout=min(remaining, native_readiness.MAX_TIMEOUT_SECONDS)
        ),
        cancelled=lambda: cancellation.cancelled,
    )


# ── Lifecycle ─────────────────────────────────────────────────────────────


def _validate_repair_request(request: NativeRepairRequest) -> None:
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
    if not native_installer.MIN_EVENT_FD <= request.events_fd <= native_installer.MAX_EVENT_FD:
        raise NativeCoordinatorError("invalid_events_fd", "invalid_arguments")
    if type(request.first_seq) is not int or request.first_seq < 1:
        raise NativeCoordinatorError("invalid_sequence", "invalid_arguments")
    if type(request.prior_events) is not int or request.prior_events < 0:
        raise NativeCoordinatorError("invalid_sequence", "invalid_arguments")
    if request.prior_events >= native_installer.MAX_EVENTS:
        raise NativeCoordinatorError("invalid_sequence", "invalid_arguments")
    if request.prior_events > request.first_seq - 1:
        raise NativeCoordinatorError("invalid_sequence", "invalid_arguments")
    if request.prior_events + REPAIR_EVENT_BUDGET > native_installer.MAX_EVENTS:
        raise NativeCoordinatorError("invalid_sequence", "invalid_arguments")


def _repair_failure_outcome(request: NativeRepairRequest, code: str) -> NativeRepairOutcome:
    return NativeRepairOutcome(
        run_id=request.run_id if type(request.run_id) is str else "",
        terminal=None,
        failure_code=code,
        mutation_attempted=False,
        activation_attempted=False,
        activation_completed=False,
        lock_retained=False,
        workspace=None,
        port=None,
    )


def _observed(step: Callable[[], _T]) -> _T:
    """A read-only step: an unexpected failure is an unknown state, never a guess."""
    try:
        return step()
    except (_Cancelled, _TimedOut, _Refusal, _ChildCleanupFailed):
        raise
    except Exception:
        raise _Refusal("unknown_state") from None


def _mutating(step: Callable[[], _T]) -> _T:
    """A mutating step: typed failures keep their code, anything else is install_failed."""
    try:
        return step()
    except (
        _Cancelled,
        _TimedOut,
        _Refusal,
        _ChildCleanupFailed,
        NativeCoordinatorError,
        NativeLockError,
        ServerHostInstallError,
    ):
        raise
    except Exception:
        raise _Refusal("install_failed") from None


def _activating(step: Callable[[], _T]) -> _T:
    try:
        return step()
    except (_Cancelled, _TimedOut, _Refusal):
        raise
    except Exception:
        raise _Refusal("activation_refused") from None


def _await_repair_port(
    deps: _RepairDeps,
    port: int,
    host: str,
    policy: _RepairPolicy,
    deadline: _Deadline,
    cancellation: NativeCancellation,
) -> None:
    """Return once the service port is bindable; otherwise refuse before activation."""
    bind = _Deadline.after(policy.port_bind_timeout)
    while True:
        _check_stage(deadline, cancellation)
        try:
            bindable = deps.port_bindable(port, host)
        except (_Cancelled, _TimedOut):
            raise
        except Exception:
            raise _Refusal("activation_refused") from None
        if not isinstance(bindable, bool):
            raise _Refusal("activation_refused")
        if bindable:
            return
        if bind.remaining() <= 0:
            raise _Refusal("activation_refused")
        _wait_observing(
            min(policy.poll_interval, bind.remaining()),
            cancellation,
            policy.poll_interval,
        )


def _run_native_repair(
    request: NativeRepairRequest,
    *,
    deps: _RepairDeps,
    version: str,
    cancellation: NativeCancellation | None = None,
    policy: _RepairPolicy | None = None,
) -> NativeRepairOutcome:
    """Drive the repair lifecycle; tests inject every effect.

    Order: classify, take the lock, classify again, then the read-only plan
    (host, probe, receipt, version rule). Only after that does the run mutate:
    stop the job, ensure the host, reinstall or re-provision, rewrite the
    LaunchAgent when it differs, wait for the port, bootstrap, and prove
    readiness. Nothing is rolled back. ``service_ready`` is emitted only after
    the lock was released cleanly.
    """
    cancellation = NativeCancellation() if cancellation is None else cancellation
    policy = _RepairPolicy() if policy is None else policy
    try:
        _validate_repair_request(request)
    except NativeCoordinatorError as exc:
        return _repair_failure_outcome(request, exc.failure_code)
    try:
        os.set_inheritable(request.events_fd, False)
    except OSError:
        return _repair_failure_outcome(request, "event_channel_failed")
    try:
        writer = EventWriter(
            request.events_fd,
            run_id=request.run_id,
            first_seq=request.first_seq,
            prior_events=request.prior_events,
            after_phase=native_installer.SHELL_LAST_PHASE,
            repair=True,
        )
    except EventError:
        native_installer._close_fd(request.events_fd)
        return _repair_failure_outcome(request, "event_channel_failed")

    held: NativeRunLock | None = None
    target: RepairTarget | None = None
    mutation_attempted = False
    activation_attempted = False
    activation_completed = False
    lock_retained = False
    ready_port: int | None = None
    failure: str | None = None
    channel_dead = False

    try:
        try:
            try:
                if sys.platform != "darwin":
                    raise _Refusal("unknown_state")
                deadline = _Deadline.after(policy.run_timeout)
                _check_stage(deadline, cancellation)
                try:
                    deps.validate_inputs()
                except (NativeCoordinatorError, _Cancelled, _TimedOut):
                    raise
                except Exception:
                    raise _Refusal("internal_error") from None
                _check_stage(deadline, cancellation)

                first = _observed(lambda: deps.classify(None))
                target = first
                _check_stage(deadline, cancellation)
                state_dir = deps.prepare_state_dir()
                try:
                    held = deps.acquire_lock(state_dir, request.run_id)
                finally:
                    state_dir.close()
                held.assert_held(run_id=request.run_id)
                # The second read-only classification refuses anything that changed while the lock was taken.
                if _observed(lambda: deps.classify(request.run_id)) != first:
                    raise _Refusal("unknown_state")
                held.assert_held(run_id=request.run_id)

                host_plan = _observed(deps.plan_host)
                probe = _observed(deps.probe_engine)
                if compare_versions(version, version) is None:
                    raise _Refusal("invalid_arguments")
                installed = probe if probe is not None else _observed(deps.receipt_version)
                if installed is not None and compare_versions(installed, version) == 1:
                    raise _Refusal("invalid_arguments")
                needs_reinstall = probe is None or probe != version

                writer.phase("install")
                mutation_attempted = True
                if target.job_registered:
                    held.assert_held(run_id=request.run_id)
                    _check_stage(deadline, cancellation)
                    _mutating(lambda: deps.stop_service(_remaining(deadline)))
                held.assert_held(run_id=request.run_id)
                _check_stage(deadline, cancellation)
                host = _mutating(
                    lambda: deps.ensure_host(held, request.run_id, host_plan)
                )
                held.assert_held(run_id=request.run_id)
                _check_stage(deadline, cancellation)
                if needs_reinstall:
                    _mutating(lambda: deps.reinstall_engine(_remaining(deadline)))
                else:
                    _mutating(deps.ensure_receipt)
                held.assert_held(run_id=request.run_id)
                _check_stage(deadline, cancellation)
                rendered = _mutating(lambda: deps.render_definition(target, host))
                digest = _mutating(lambda: deps.write_definition(target, rendered))
                held.assert_held(run_id=request.run_id)
                _check_stage(deadline, cancellation)
                _await_repair_port(deps, target.port, target.host, policy, deadline, cancellation)

                writer.phase("activation")
                activation_attempted = True
                _activating(lambda: deps.bootstrap(target.definition, digest))
                # The service may now be running. Nothing below stops or rolls it back.
                activation_completed = True
                _check_stage(deadline, cancellation)

                writer.phase("readiness")
                readiness_deadline = _sub_deadline(deadline, policy.readiness_timeout)
                while True:
                    _check_stage(deadline, cancellation)
                    remaining = readiness_deadline.remaining()
                    if remaining <= 0:
                        raise _Refusal("service_not_ready")
                    try:
                        proof = deps.prove_ready(target, host, remaining)
                    except (_Cancelled, _TimedOut, _Refusal):
                        raise
                    except Exception:
                        raise _Refusal("service_not_ready") from None
                    if not isinstance(proof, NativeReadinessProof):
                        raise _Refusal("service_not_ready")
                    if proof.ready:
                        if proof.failure is not None or proof.port != target.port:
                            raise _Refusal("service_not_ready")
                        ready_port = target.port
                        break
                    if proof.failure == "cancelled":
                        raise _Cancelled
                    if proof.failure not in native_installer._READINESS_RETRYABLE:
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
            except _ChildCleanupFailed:
                # A child may still be running: the lock is kept as evidence.
                lock_retained = True
                failure = "install_failed"
            except NativeCoordinatorError as exc:
                failure = exc.failure_code
            except NativeLockError as exc:
                failure = _map_lock_failure(exc)
            except ServerHostInstallError as exc:
                failure = _map_host_failure(exc)
            except EventError:
                failure = "event_channel_failed"
                channel_dead = True
            except OSError:
                failure = "internal_error"
            except Exception:
                failure = "internal_error"
        finally:
            if held is not None and not lock_retained:
                try:
                    held.release()
                except Exception:
                    lock_retained = True
                    if failure is None:
                        failure = "internal_error"

        terminal: str | None = None
        if failure is None and ready_port is not None:
            # Success only here: no failure, and the lock was released above.
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
    return NativeRepairOutcome(
        run_id=request.run_id,
        terminal=terminal,
        failure_code=None if terminal == "service_ready" else failure,
        mutation_attempted=mutation_attempted,
        activation_attempted=activation_attempted,
        activation_completed=activation_completed,
        lock_retained=lock_retained,
        workspace=None if target is None else target.workspace,
        port=None if target is None else target.port,
    )


def _repair_production_deps(
    artifacts: NativeInstallArtifacts,
    cancellation: NativeCancellation,
) -> tuple[_RepairDeps, native_installer._PrivateRun]:
    """Bind every seam to its real step for one run and its verified artifacts."""
    private = native_installer._PrivateRun()
    tool_dirs: list[tuple[Path, Path]] = []
    selected: list[tuple[dict[str, Any], HostOwnership | None]] = []

    def dirs() -> tuple[Path, Path]:
        if not tool_dirs:
            try:
                tool_dirs.append(
                    native_installer._tool_dirs(
                        native_installer._required(private.uv),
                        env=native_installer._scrubbed_env(keep_user_path=False),
                        cwd=native_installer._required(private.cwd),
                        cancellation=cancellation,
                    )
                )
            except native_installer._ChildCleanupFailed:
                private.retained = True
                raise
        return tool_dirs[0]

    def classify(own_run_id: str | None) -> RepairTarget:
        tool_dir, bin_dir = dirs()
        return _classify_repair(
            environ=os.environ,
            tool_dir=tool_dir,
            bin_dir=bin_dir,
            job_absent=macos_service.server_job_absent,
            override_disabled=macos_service.launchd_override_disabled,
            own_run_id=own_run_id,
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
                native_installer._report_lock_holder(state.path)
            raise

    def plan_host() -> HostPlan:
        plan, entry, verified = _plan_host(artifacts)
        selected[:] = [(entry, verified)]
        return plan

    def probe_engine() -> str | None:
        tool_dir, bin_dir = dirs()
        return _probe_engine(
            tool_dir,
            bin_dir,
            env=native_installer._scrubbed_env(keep_user_path=False),
            cwd=native_installer._required(private.cwd),
            cancellation=cancellation,
        )

    def ensure_host(lock: NativeRunLock, run_id: str, plan: HostPlan) -> HostOwnership:
        if not selected:
            raise NativeCoordinatorError("plan_missing", "internal_error")
        entry, verified = selected[0]
        return _ensure_host(lock, run_id, plan, entry, artifacts, verified)

    def reinstall_engine(remaining: float) -> None:
        tool_dir, bin_dir = dirs()
        staged = replace(
            artifacts,
            uv=native_installer._required(private.uv),
            wheel=native_installer._required(private.wheel),
        )
        previous = install_receipt.read_receipt()
        try:
            private.engine = _reinstall_engine_preserving_history(
                previous,
                staged,
                tool_dir,
                bin_dir,
                remaining,
                cancellation=cancellation,
                env=native_installer._scrubbed_env(keep_user_path=False),
                cwd=native_installer._required(private.cwd),
            )
        except native_installer._ChildCleanupFailed:
            private.retained = True
            raise

    def ensure_receipt() -> None:
        tool_dir, bin_dir = dirs()
        _ensure_receipt(private, artifacts, tool_dir, bin_dir)

    def render_definition(target: RepairTarget, host: HostOwnership) -> bytes:
        tool_dir, _bin_dir = dirs()
        return _render_definition(target, host, tool_dir / "ciaobot" / "bin" / "python")

    def prove_ready(
        target: RepairTarget, host: HostOwnership, remaining: float
    ) -> NativeReadinessProof:
        tool_dir, _bin_dir = dirs()
        return _prove_ready(
            target,
            host,
            remaining,
            version=artifacts.version,
            tool_python=tool_dir / "ciaobot" / "bin" / "python",
            cancellation=cancellation,
        )

    deps = _RepairDeps(
        validate_inputs=lambda: private.stage(artifacts),
        classify=classify,
        prepare_state_dir=prepare_state_dir,
        acquire_lock=acquire_lock,
        plan_host=plan_host,
        probe_engine=probe_engine,
        receipt_version=_receipt_version,
        stop_service=lambda remaining: _stop_service(remaining, cancellation),
        ensure_host=ensure_host,
        reinstall_engine=reinstall_engine,
        ensure_receipt=ensure_receipt,
        render_definition=render_definition,
        write_definition=_write_definition,
        port_bindable=native_installer._service_port_bindable,
        bootstrap=_bootstrap,
        prove_ready=prove_ready,
    )
    return deps, private


def run_native_repair(
    request: NativeRepairRequest,
    artifacts: NativeInstallArtifacts,
    *,
    cancellation: NativeCancellation | None = None,
) -> NativeRepairOutcome:
    """Run one native repair end to end with the real steps.

    The only caller is :func:`main`, reached through ``install.sh --native-repair``.
    A request or artifact digest that fails validation raises
    :class:`NativeCoordinatorError` before the descriptor is used. The private
    run directory is removed when the run ends.
    """
    _validate_repair_request(request)
    native_installer._validate_digests(artifacts)
    cancellation = NativeCancellation() if cancellation is None else cancellation
    deps, private = _repair_production_deps(artifacts, cancellation)
    try:
        return _run_native_repair(
            request, deps=deps, version=artifacts.version, cancellation=cancellation
        )
    finally:
        private.remove()


def main(argv: list[str] | None = None) -> int:
    """The internal CLI: ``python -I -m ciao.native_repair run ...``.

    Exit 0 means ``service_ready`` was emitted; 1 means any other end; 2 means
    the arguments were refused before the descriptor was used. There is no
    ``--workspace``: the workspace comes from the live LaunchAgent, else the default.
    """
    parser = native_installer._HandoverArgumentParser(
        prog="python -m ciao.native_repair", allow_abbrev=False
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", allow_abbrev=False, help="run the native repair")
    run.add_argument("--run-id", required=True)
    run.add_argument("--events-fd", type=int, required=True)
    run.add_argument("--first-seq", type=int, required=True)
    run.add_argument("--prior-events", type=int, required=True)
    run.add_argument("--version", required=True)
    run.add_argument("--uv", type=Path, required=True)
    run.add_argument("--uv-sha256", required=True)
    run.add_argument("--wheel", type=Path, required=True)
    run.add_argument("--wheel-sha256", required=True)
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--signature", type=Path, required=True)
    run.add_argument("--host-archive", type=Path, required=True)
    args = parser.parse_args(argv)

    request = NativeRepairRequest(
        run_id=args.run_id,
        events_fd=args.events_fd,
        first_seq=args.first_seq,
        prior_events=args.prior_events,
    )
    artifacts = NativeInstallArtifacts(
        version=args.version,
        uv=args.uv,
        wheel=args.wheel,
        manifest=args.manifest,
        signature=args.signature,
        host_archive=args.host_archive,
        uv_sha256=args.uv_sha256,
        wheel_sha256=args.wheel_sha256,
    )
    cancellation = NativeCancellation()

    def on_signal(_signum: int, _frame: object) -> None:
        cancellation.cancel()

    previous: list[tuple[int, Any]] = []
    try:
        for signum in native_installer._cancel_signal_numbers():
            previous.append((signum, signal.getsignal(signum)))
            signal.signal(signum, on_signal)
        try:
            outcome = run_native_repair(request, artifacts, cancellation=cancellation)
        except NativeCoordinatorError:
            sys.stderr.write(native_installer._HANDOVER_REFUSAL)
            return 2
        return outcome.exit_code
    finally:
        for signum, handler in previous:
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
