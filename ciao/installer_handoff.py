"""The result an installer's bootstrap child hands back to its parent.

The installer never runs the browser bootstrap in its own process: it starts
``ciao run --installer-bootstrap`` as a foreground child, the user completes the
wizard there, and the child exits 0 once setup has succeeded. The only thing the
parent trusts from that child is one bounded, owner-only result file in the
scratch directory — its run id (the value the parent generated), the startup id
of the process that wrote it, the resolved destination, the service backend and
definition it wrote, and the port.

Activation re-verifies all of that in the parent: the result must belong to this
run, the definition must be the live service definition, the destination must
be a completed workspace, and that definition must name the same destination.
Missing, stale or malformed means "incomplete or uncertain": no activation.
Nothing here ever deletes or reorganises the destination the user chose.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

SCHEMA_VERSION = 1
FILENAME = "handoff.json"
MAX_RESULT_BYTES = 8 * 1024
SUPPORTED_BACKENDS = ("macos", "windows")


class HandoffError(RuntimeError):
    """The handoff result is absent, stale, malformed, or does not verify."""


@dataclass(frozen=True)
class HandoffResult:
    run_id: str
    startup_id: str
    workspace: Path
    service_backend: str
    definition_path: Path
    port: int


def scratch_root() -> Path:
    """Where an installer bootstrap keeps its private state."""
    return Path.home() / ".ciao" / "installer-bootstrap"


def result_path() -> Path:
    return scratch_root() / FILENAME


def _definition_name(backend_name: str) -> str:
    from ciao import macos_service, windows_service

    if backend_name == "windows":
        return windows_service.TASK_FILE_NAME
    return f"{macos_service.SERVER_LABEL}.plist"


def _is_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _strict_regular_file(path: Path, *, label: str) -> None:
    """Require an existing regular file; a symlink is not one."""
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        raise HandoffError(f"{label} is missing: {path}") from None
    except OSError as exc:
        raise HandoffError(f"Could not inspect {label} {path}: {exc}") from exc
    if not stat.S_ISREG(status.st_mode):
        raise HandoffError(f"{label} is not a regular file: {path}")


def _require_private_result(path: Path) -> None:
    """The result must sit in the scratch directory and be owner-only."""
    directory = scratch_root()
    if directory.is_symlink():
        raise HandoffError(f"installer scratch directory is a symlink: {directory}")
    try:
        parent_is_scratch = path.parent.resolve() == directory.expanduser().resolve()
    except (OSError, ValueError) as exc:
        raise HandoffError(f"Could not resolve the installer scratch directory: {exc}") from exc
    if not parent_is_scratch:
        raise HandoffError(
            f"installer handoff result is not in the scratch directory: {path}"
        )
    if sys.platform != "win32":
        try:
            mode = stat.S_IMODE(os.lstat(path).st_mode)
        except OSError as exc:
            raise HandoffError(f"Could not inspect the installer handoff result: {exc}") from exc
        if mode & 0o077:
            raise HandoffError("installer handoff result is readable by other users")


def _resolved(value: str, *, label: str) -> Path:
    try:
        return Path(value).expanduser().resolve()
    except (OSError, ValueError) as exc:
        raise HandoffError(f"Could not resolve {label}: {exc}") from exc


def _no_duplicate_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise HandoffError(f"installer handoff result repeats the field {key!r}")
        payload[key] = value
    return payload


def _current_backend_name() -> str:
    from ciao import service_backend

    try:
        name = service_backend.current_backend().name
    except service_backend.UnsupportedPlatformError as exc:
        raise HandoffError(str(exc)) from exc
    if name not in SUPPORTED_BACKENDS:
        raise HandoffError(f"Installer bootstrap is unsupported on {name}")
    return name


def _definition_from_written(written: Iterable[Path | str], backend_name: str) -> Path:
    name = _definition_name(backend_name)
    matches = [Path(path) for path in written if Path(path).name == name]
    if len(matches) != 1:
        raise HandoffError(
            "setup did not report exactly one service definition "
            f"({name}); found {len(matches)}"
        )
    return matches[0]


def record_setup_completion(
    *,
    run_id: str,
    startup_id: str,
    workspace: Path | str,
    written: Iterable[Path | str],
    port: int,
) -> Path:
    """Write the installer handoff result after a successful guarded setup."""
    from ciao.jsonio import write_private_text

    if not _is_uuid(run_id):
        raise HandoffError("installer run id is not a valid uuid")
    if not isinstance(startup_id, str) or not startup_id.strip():
        raise HandoffError("this server did not record a startup id")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise HandoffError(f"invalid port for the handoff result: {port!r}")
    backend_name = _current_backend_name()
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise HandoffError(f"the chosen workspace does not exist: {root}")
    definition = _definition_from_written(written, backend_name)
    _strict_regular_file(definition, label="service definition")

    payload = {
        "schema": SCHEMA_VERSION,
        "run_id": run_id,
        "startup_id": startup_id,
        "workspace": str(root),
        "service_backend": backend_name,
        "definition_path": str(definition.expanduser().resolve()),
        "port": port,
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if len(text.encode("utf-8")) > MAX_RESULT_BYTES:
        raise HandoffError("the installer handoff result would exceed its size limit")

    directory = scratch_root()
    if directory.is_symlink():
        raise HandoffError(f"installer scratch directory is a symlink: {directory}")
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise HandoffError(f"Could not create the installer scratch directory: {exc}") from exc
    target = result_path()
    if target.is_symlink():
        raise HandoffError(f"installer handoff result is a symlink: {target}")
    temporary = target.with_name(target.name + ".tmp")
    if temporary.is_symlink():
        raise HandoffError(f"installer handoff temporary file is a symlink: {temporary}")
    try:
        write_private_text(temporary, text)
        os.replace(temporary, target)
    except OSError as exc:
        raise HandoffError(f"Could not write the installer handoff result: {exc}") from exc
    return target


def _field(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HandoffError(f"installer handoff result has no usable {key}")
    return value


def read_result(
    *, expected_run_id: str, path: Path | None = None
) -> HandoffResult:
    """Read and validate the result; anything unexpected is a refusal."""
    target = result_path() if path is None else path
    _strict_regular_file(target, label="installer handoff result")
    _require_private_result(target)
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise HandoffError(f"Could not inspect the installer handoff result: {exc}") from exc
    if size > MAX_RESULT_BYTES:
        raise HandoffError("installer handoff result is too large")
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise HandoffError(f"Could not read the installer handoff result: {exc}") from exc
    try:
        payload = json.loads(text, object_pairs_hook=_no_duplicate_fields)
    except ValueError as exc:
        raise HandoffError("installer handoff result is not valid json") from exc
    if not isinstance(payload, dict):
        raise HandoffError("installer handoff result is not a json object")
    if set(payload) != {
        "schema",
        "run_id",
        "startup_id",
        "workspace",
        "service_backend",
        "definition_path",
        "port",
    }:
        raise HandoffError("installer handoff result has unexpected fields")
    schema = payload.get("schema")
    if not isinstance(schema, int) or isinstance(schema, bool) or schema != SCHEMA_VERSION:
        raise HandoffError(f"unsupported installer handoff schema: {schema!r}")
    run_id = _field(payload, "run_id")
    if not _is_uuid(run_id) or run_id != expected_run_id:
        raise HandoffError(
            "installer handoff result belongs to a different installer run"
        )
    startup_id = _field(payload, "startup_id")
    backend_name = _field(payload, "service_backend")
    if backend_name not in SUPPORTED_BACKENDS:
        raise HandoffError(f"unknown service backend in the handoff result: {backend_name}")
    workspace = Path(_field(payload, "workspace"))
    definition = Path(_field(payload, "definition_path"))
    if not workspace.is_absolute() or not definition.is_absolute():
        raise HandoffError("installer handoff result paths must be absolute")
    port = payload.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise HandoffError(f"invalid port in the handoff result: {port!r}")
    return HandoffResult(
        run_id=run_id,
        startup_id=startup_id,
        workspace=_resolved(str(workspace), label="the handoff workspace"),
        service_backend=backend_name,
        definition_path=definition.expanduser(),
        port=port,
    )


def _definition_workspace(backend_name: str, definition: Path) -> Path:
    """The destination a service definition names; unreadable is an error."""
    try:
        if backend_name == "windows":
            from ciao import windows_service

            workspace = windows_service.task_workspace(definition)
        else:
            import plistlib

            with definition.open("rb") as handle:
                data = plistlib.load(handle)
            environment = data.get("EnvironmentVariables") if isinstance(data, dict) else None
            raw = environment.get("CIAO_WORKSPACE") if isinstance(environment, dict) else None
            workspace = Path(str(raw)) if raw else None
    except Exception as exc:  # noqa: BLE001 — every parse failure is a refusal
        raise HandoffError(f"Could not read the service definition: {exc}") from exc
    if workspace is None:
        raise HandoffError("the service definition does not name a workspace")
    named = Path(workspace).expanduser()
    if not named.is_absolute():
        raise HandoffError("the service definition names a relative workspace")
    return _resolved(str(named), label="the definition's workspace")


def activate(
    *,
    run_id: str,
    is_set_up: Callable[[Path], bool] | None = None,
) -> HandoffResult:
    """Verify the handoff result and start the service, or raise.

    The caller is the installer parent, and it must already have observed the
    child exit 0; this function cannot see that.
    """
    from ciao import service_backend
    from ciao.setup_marker import is_set_up_workspace

    if not _is_uuid(run_id):
        raise HandoffError("installer run id is not a valid uuid")
    try:
        backend = service_backend.current_backend()
    except service_backend.UnsupportedPlatformError as exc:
        raise HandoffError(str(exc)) from exc
    if backend.name not in SUPPORTED_BACKENDS:
        raise HandoffError(f"Installer activation is unsupported on {backend.name}")
    result = read_result(expected_run_id=run_id)
    if result.service_backend != backend.name:
        raise HandoffError(
            f"the handoff result was written for {result.service_backend}, "
            f"not {backend.name}"
        )
    try:
        expected = backend.live_agents_dir() / _definition_name(backend.name)
        recorded_is_live = result.definition_path.resolve() == expected.resolve()
    except (OSError, ValueError):
        recorded_is_live = False
    if not recorded_is_live:
        raise HandoffError(
            f"the recorded definition {result.definition_path} is not the live one {expected}"
        )
    _strict_regular_file(expected, label="live service definition")
    checker = is_set_up_workspace if is_set_up is None else is_set_up
    if not checker(result.workspace):
        raise HandoffError(f"{result.workspace} is not a completed Ciaobot workspace")
    served = _definition_workspace(result.service_backend, expected)
    if served != result.workspace:
        raise HandoffError(
            f"the service definition serves {served}, not {result.workspace}"
        )
    try:
        status = backend.load_agent(expected)
    except (OSError, RuntimeError) as exc:
        raise HandoffError(f"Could not start the engine: {exc}") from exc
    if status != 0:
        raise HandoffError(
            f"the service manager refused to start the engine (status {status})"
        )
    return result
