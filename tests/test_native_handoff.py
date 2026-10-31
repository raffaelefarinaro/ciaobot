"""The native installer handoff: schema 2, same scratch path, not wired.

The legacy ``ciao.installer_handoff`` module stays schema 1 and is the production
path until #2340. These tests pin the separate native module:

* it writes and reads the same ``handoff.json`` path, but schema 2 only;
* the legacy module still writes schema 1 and its activation is unchanged;
* recording validates the job semantics, the workspace and the port against the
  same definition bytes it hashes before it writes a result;
* the parent prepares the private scratch chain before the bootstrap child's
  `CiaoConfig` mkdirs run, refuses an existing `0755` chain unchanged, and a
  home directory that cannot be resolved is a native refusal;
* activation re-establishes the digest, the interpreter identity and the job
  semantics, then bootstraps fresh on macOS (never reloads);
* the schema-2 result is read by the shared bounded descriptor reader, with its
  owner-only mode checked on the opened descriptor, and activation's default
  completed-workspace check cannot block on a FIFO `.env`;
* native Windows is refused explicitly: task XML carries no port and the legacy
  registration path can replace an intervening task, so it cannot bind the same
  facts or promise fresh-only registration;
* a schema-1 result is refused by the native reader and a schema-2 result by the
  legacy reader.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import stat
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from xml.etree import ElementTree

import pytest

from ciao import (
    cli,
    installer_handoff,
    macos_service,
    native_handoff,
    server_host,
    service_backend,
    windows_service,
)
from ciao.config import CiaoConfig
from ciao.os_support import private as private_module
from ciao.os_support.limits import SERVER_NOFILE_TARGET
from ciao.os_support.private import is_private, open_private


#: The native path implements the macOS backend only
#: (``native_handoff.NATIVE_BACKENDS``): recording binds a launchd plist, an
#: absolute POSIX program and an absolute POSIX interpreter that exists, and
#: activation bootstraps that job. Windows keeps the legacy installer, so these
#: tests do not run there. The explicit Windows refusals below are deliberately
#: unmarked and run on every platform.
DARWIN_ONLY = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="the native handoff implements the macOS backend only",
)


def _definition_name() -> str:
    return f"{macos_service.SERVER_LABEL}.plist"


def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


class _Backend:
    def __init__(self, name: str, live: Path) -> None:
        self.name = name
        self.live = live
        self.loaded: list[Path] = []
        self.fresh_calls: list[tuple[Path, str]] = []
        self.status = 0

    def live_agents_dir(self) -> Path:
        return self.live

    def load_agent(self, definition: Path) -> int:
        self.loaded.append(definition)
        return self.status


def _approved_environment(workspace: Path) -> dict[str, str]:
    return {
        "CIAO_WORKSPACE": str(workspace),
        "CIAO_RUNTIME_ROOT": str(workspace / ".runtime"),
        "CIAO_PORT": "8443",
        "PATH": "/usr/bin:/bin",
    }


def _approved_plist(workspace: Path, **overrides: object) -> bytes:
    payload: dict[str, object] = {
        "Label": macos_service.SERVER_LABEL,
        "AssociatedBundleIdentifiers": ["local.ciaobot.app"],
        "ProgramArguments": [str(Path(sys.executable)), "-m", "ciao.cli", "run"],
        "WorkingDirectory": str(workspace),
        "EnvironmentVariables": _approved_environment(workspace),
        "RunAtLoad": True,
        "KeepAlive": True,
        "SoftResourceLimits": {"NumberOfFiles": SERVER_NOFILE_TARGET},
        "HardResourceLimits": {"NumberOfFiles": SERVER_NOFILE_TARGET},
        "StandardOutPath": str(workspace / ".runtime" / "ciao.stdout.log"),
        "StandardErrorPath": str(workspace / ".runtime" / "ciao.stderr.log"),
    }
    payload.update(overrides)
    return plistlib.dumps(payload)


def _definition(live: Path, workspace: Path) -> Path:
    live.mkdir(parents=True, exist_ok=True)
    definition = live / _definition_name()
    definition.write_bytes(_approved_plist(workspace))
    return definition


def _windows_task(*, extra_action: str = "", extra_trigger: str = "") -> str:
    """The legacy task template, optionally with a second action or trigger."""
    xml = windows_service.render_task_xml(
        python="C:\\Python312\\pythonw.exe",
        workspace="C:\\Ciaobot",
        user="DESKTOP-1\\me",
    )
    if extra_action:
        xml = xml.replace(
            "    </Exec>\n",
            "    </Exec>\n" + extra_action + "\n",
            1,
        )
    if extra_trigger:
        xml = xml.replace(
            "  </Triggers>\n",
            extra_trigger + "\n  </Triggers>\n",
            1,
        )
    assert extra_action in xml or not extra_action
    assert extra_trigger in xml or not extra_trigger
    return xml


def _digest(definition: Path) -> str:
    return hashlib.sha256(definition.read_bytes()).hexdigest()


def _recordable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    patch_fresh: bool = True,
) -> tuple[_Backend, Path, Path, str]:
    _home(monkeypatch, tmp_path)
    backend = _Backend("macos", tmp_path / "live")
    monkeypatch.setattr(service_backend, "current_backend", lambda: backend)

    def load_fresh(definition: Path, *, expected_sha256: str) -> int:
        backend.fresh_calls.append((definition, expected_sha256))
        return backend.load_agent(definition)

    if patch_fresh:
        monkeypatch.setattr(macos_service, "load_fresh_agent", load_fresh)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    definition = _definition(backend.live, workspace)
    return backend, workspace, definition, str(uuid.uuid4())


def _record(
    backend: _Backend,
    workspace: Path,
    definition: Path,
    run_id: str,
    *,
    engine_python: str | None = None,
) -> Path:
    if engine_python is None:
        engine_python = str(Path(sys.executable))
    return native_handoff.record_setup_completion(
        run_id=run_id,
        startup_id="startup-1",
        workspace=workspace,
        written=[definition],
        port=8443,
        engine_python=engine_python,
    )


def _activate(monkeypatch, backend: _Backend, *, is_set_up=lambda _ws: True):
    monkeypatch.setattr(service_backend, "current_backend", lambda: backend)
    return native_handoff.activate(
        run_id=json.loads(native_handoff.result_path().read_text())["run_id"],
        is_set_up=is_set_up,
    )


def _schema_2_document(
    *, run_id: str, workspace: Path, definition: Path, backend: str = "macos"
) -> dict[str, object]:
    """A schema-2 result document shaped like the one recording writes.

    Used by the reader tests. Recording itself is macOS-only — it rejects any
    program that is not an existing, absolute POSIX path — but what a *reader*
    does with a document is platform-neutral. Writing the document directly is
    the seam that keeps those tests running on Windows.
    """
    identity = {"device": 0, "inode": 0, "size": 0, "mtime_ns": 0}
    return {
        "schema": 2,
        "run_id": run_id,
        "startup_id": "startup-1",
        "workspace": str(workspace.resolve()),
        "service_backend": backend,
        "definition_path": str(definition.resolve()),
        "definition_sha256": _digest(definition),
        "engine_program": str(Path(sys.executable)),
        "engine_program_identity": identity,
        "engine_python": str(Path(sys.executable)),
        "engine_python_identity": identity,
        "environment": _approved_environment(workspace),
        "port": 8443,
    }


def _write_result_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict[str, object]
) -> Path:
    """Write ``payload`` at the native result path in a private scratch chain.

    The chain is prepared the way the native parent prepares it before a
    bootstrap child starts, so the reader's own chain check judges the same
    private directory on POSIX and on Windows (a protected DACL there).
    """
    _home(monkeypatch, tmp_path)
    native_handoff.prepare_bootstrap_scratch()
    path = native_handoff.result_path()
    descriptor = open_private(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(payload))
    return path


def _within(seconds: float, call):
    """Run ``call`` on a daemon thread and fail unless it returns promptly.

    A regressed reader on a FIFO never returns; running it in the test process
    would hang the suite instead of failing it. The thread is a daemon so a
    failure can still report the assertion and exit.
    """
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["value"] = call()
        except BaseException as exc:  # re-raised below, in the test's thread
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "the native path did not return promptly"
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def test_the_native_module_uses_the_existing_handoff_storage_path() -> None:
    assert native_handoff.result_path() == installer_handoff.result_path()
    assert native_handoff.scratch_root() == installer_handoff.scratch_root()
    assert native_handoff.SCHEMA_VERSION == 2
    assert installer_handoff.SCHEMA_VERSION == 1


@DARWIN_ONLY
def test_the_native_record_is_a_private_schema_2_result_on_the_legacy_path(
    monkeypatch, tmp_path
) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    path = _record(backend, workspace, definition, run_id)

    assert path == installer_handoff.result_path()
    assert is_private(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    status = os.stat(sys.executable)
    assert payload == {
        "schema": 2,
        "run_id": run_id,
        "startup_id": "startup-1",
        "workspace": str(workspace.resolve()),
        "service_backend": backend.name,
        "definition_path": str(definition.resolve()),
        "definition_sha256": _digest(definition),
        "engine_program": str(Path(sys.executable)),
        "engine_program_identity": {
            "device": status.st_dev,
            "inode": status.st_ino,
            "size": status.st_size,
            "mtime_ns": status.st_mtime_ns,
        },
        "engine_python": str(Path(sys.executable)),
        "engine_python_identity": {
            "device": status.st_dev,
            "inode": status.st_ino,
            "size": status.st_size,
            "mtime_ns": status.st_mtime_ns,
        },
        "environment": _approved_environment(workspace),
        "port": 8443,
    }


@DARWIN_ONLY
def test_the_native_record_creates_a_private_scratch_chain(monkeypatch, tmp_path) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    path = _record(backend, workspace, definition, run_id)
    scratch = path.parent
    assert path == installer_handoff.result_path()
    assert is_private(scratch)
    if os.name == "posix":
        assert stat.S_IMODE(scratch.stat().st_mode) == 0o700
        assert stat.S_IMODE(scratch.parent.stat().st_mode) == 0o700
    assert list(scratch.glob("*.tmp")) == []


@DARWIN_ONLY
def test_the_parent_prepares_the_scratch_before_the_bootstrap_config_creates_it(
    monkeypatch, tmp_path
) -> None:
    """Order: the parent's fresh preflight, prepare, spawn the child, record.

    The real ``CiaoConfig.from_env(installer_bootstrap=True)`` is the bootstrap
    child's directory creation. It runs ``mkdir(parents=True, exist_ok=True)``
    on the scratch root, launcher and runtime. Because the parent prepared the
    chain first (in production after its read-only fresh-install preflight),
    those calls find the scratch directory already there, leave its mode alone,
    and the native record can write its result. Running the same calls before
    preparation is a later test: the scratch is then ``0755`` and recording
    refuses it. No production caller wires preparation in this issue.
    """
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    home = Path.home()
    scratch = native_handoff.prepare_bootstrap_scratch()
    assert scratch == home / ".ciao" / "installer-bootstrap"
    if os.name == "posix":
        assert stat.S_IMODE((home / ".ciao").stat().st_mode) == 0o700
        assert stat.S_IMODE(scratch.stat().st_mode) == 0o700

    config = CiaoConfig.from_env(installer_bootstrap=True)
    assert config.workspace_root == scratch
    assert config.bootstrap_mode is True
    if os.name == "posix":
        assert stat.S_IMODE(scratch.stat().st_mode) == 0o700, (
            "the child's exist_ok mkdir must not alter the prepared chain"
        )
    assert (scratch / "launcher").is_dir()
    assert (scratch / "runtime").is_dir()

    path = _record(backend, workspace, definition, run_id)
    assert path == native_handoff.result_path()
    assert json.loads(path.read_text(encoding="utf-8"))["run_id"] == run_id


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
@DARWIN_ONLY
def test_preparing_refuses_a_preexisting_0755_scratch_without_chmodding_it(
    monkeypatch, tmp_path
) -> None:
    """A shared scratch directory is refused and left exactly as it was found."""
    _home(monkeypatch, tmp_path)
    scratch = native_handoff.scratch_root()
    scratch.mkdir(parents=True)
    scratch.chmod(0o755)

    with pytest.raises(native_handoff.NativeHandoffError, match="owner-only"):
        native_handoff.prepare_bootstrap_scratch()
    assert stat.S_IMODE(scratch.stat().st_mode) == 0o755


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
@DARWIN_ONLY
def test_preparing_accepts_a_shared_0755_ciao_directory_and_creates_the_leaf(
    monkeypatch, tmp_path
) -> None:
    """``~/.ciao`` is often ``0755`` on a real machine; only the leaf must be 0700."""
    _home(monkeypatch, tmp_path)
    ciao_dir = Path.home() / ".ciao"
    ciao_dir.mkdir()
    ciao_dir.chmod(0o755)

    scratch = native_handoff.prepare_bootstrap_scratch()
    assert stat.S_IMODE(ciao_dir.stat().st_mode) == 0o755, "an existing parent is unchanged"
    assert stat.S_IMODE(scratch.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
@DARWIN_ONLY
def test_the_bootstrap_config_running_first_makes_preparation_refuse_the_chain(
    monkeypatch, tmp_path
) -> None:
    """The child cannot prepare its own scratch: its mkdir wins the race."""
    _home(monkeypatch, tmp_path)
    scratch = native_handoff.scratch_root()
    CiaoConfig.from_env(installer_bootstrap=True)
    assert stat.S_IMODE(scratch.stat().st_mode) == 0o755

    with pytest.raises(native_handoff.NativeHandoffError, match="owner-only"):
        native_handoff.prepare_bootstrap_scratch()
    assert stat.S_IMODE(scratch.stat().st_mode) == 0o755, (
        "preparation reports the unsafe order instead of repairing it"
    )


def test_a_home_directory_that_cannot_be_resolved_is_a_native_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Path.home`` failing is a native refusal, not an unrelated exception."""

    def no_home(cls: type[Path]) -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", classmethod(no_home))
    with pytest.raises(native_handoff.NativeHandoffError, match="home directory"):
        native_handoff.prepare_bootstrap_scratch()
    with pytest.raises(native_handoff.NativeHandoffError, match="scratch directory"):
        native_handoff.scratch_root()
    with pytest.raises(native_handoff.NativeHandoffError, match="result path"):
        native_handoff.result_path()


@pytest.mark.skipif(os.name != "posix", reason="symlink semantics")
@DARWIN_ONLY
def test_recording_refuses_a_symlinked_scratch_parent(monkeypatch, tmp_path) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (Path.home() / ".ciao").symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(native_handoff.NativeHandoffError, match="symlink"):
        _record(backend, workspace, definition, run_id)
    assert list(elsewhere.iterdir()) == []
    assert not native_handoff.result_path().exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
@DARWIN_ONLY
def test_recording_refuses_a_world_writable_scratch_without_chmod(
    monkeypatch, tmp_path
) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    scratch = native_handoff.result_path().parent
    scratch.mkdir(parents=True)
    scratch.chmod(0o777)

    with pytest.raises(native_handoff.NativeHandoffError, match="owner-only"):
        _record(backend, workspace, definition, run_id)
    assert stat.S_IMODE(scratch.stat().st_mode) == 0o777, "a pre-existing directory is not repaired"
    assert not native_handoff.result_path().exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
@DARWIN_ONLY
def test_recording_leaves_an_existing_shared_ciao_directory_unchanged(
    monkeypatch, tmp_path
) -> None:
    """``~/.ciao`` may be 0755 on a real machine; it is judged, not chmod'ed."""
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    ciao_dir = Path.home() / ".ciao"
    ciao_dir.mkdir()
    ciao_dir.chmod(0o755)

    path = _record(backend, workspace, definition, run_id)
    assert stat.S_IMODE(ciao_dir.stat().st_mode) == 0o755
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name != "posix", reason="effective uid is POSIX")
@DARWIN_ONLY
def test_recording_refuses_a_scratch_owner_that_is_not_the_effective_user(
    monkeypatch, tmp_path
) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    real_geteuid = os.geteuid
    monkeypatch.setattr(private_module.os, "geteuid", lambda: real_geteuid() + 1)

    with pytest.raises(native_handoff.NativeHandoffError, match="not owned"):
        _record(backend, workspace, definition, run_id)
    assert not native_handoff.result_path().exists()


@DARWIN_ONLY
def test_recording_refuses_a_scratch_directory_with_a_granting_acl(
    monkeypatch, tmp_path
) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    scratch = native_handoff.result_path().parent
    scratch.mkdir(parents=True)
    scratch.chmod(0o700)
    subprocess.run(
        ["chmod", "+a", "everyone allow read", str(scratch)],
        capture_output=True,
        check=True,
    )
    try:
        with pytest.raises(native_handoff.NativeHandoffError, match="ACL"):
            _record(backend, workspace, definition, run_id)
    finally:
        subprocess.run(
            ["chmod", "-a", "everyone allow read", str(scratch)],
            capture_output=True,
            check=False,
        )
    assert not native_handoff.result_path().exists()


@DARWIN_ONLY
def test_a_collision_on_the_temporary_name_is_not_deleted(
    monkeypatch, tmp_path
) -> None:
    """Only a temp this invocation created may be removed on failure."""
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    # The first record creates the private scratch directory; remove only the
    # result so the colliding temp can be placed where this write will look.
    first = _record(backend, workspace, definition, run_id)
    first.unlink()
    token = uuid.UUID("00000000-0000-0000-0000-000000000001")
    monkeypatch.setattr(native_handoff.uuid, "uuid4", lambda: token)
    target = native_handoff.result_path()
    colliding = target.with_name(f"{target.name}.{token.hex}.tmp")
    colliding.write_text("someone else's file", encoding="utf-8")

    with pytest.raises(native_handoff.NativeHandoffError, match="Could not write"):
        _record(backend, workspace, definition, run_id)
    assert colliding.read_text(encoding="utf-8") == "someone else's file"
    assert not target.exists()


def _windows_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[_Backend, Path, Path, str]:
    """A Windows backend and the task XML setup would have written."""
    _home(monkeypatch, tmp_path)
    backend = _Backend("windows", tmp_path / "live")
    backend.live.mkdir(parents=True)
    monkeypatch.setattr(service_backend, "current_backend", lambda: backend)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    definition = backend.live / windows_service.TASK_FILE_NAME
    definition.write_text(
        windows_service.render_task_xml(
            python="C:\\Python312\\pythonw.exe",
            workspace="C:\\Ciaobot",
            user="DESKTOP-1\\me",
        ),
        encoding="utf-16",
    )
    return backend, workspace, definition, str(uuid.uuid4())


def _refuse_windows_record(
    backend: _Backend,
    workspace: Path,
    definition: Path,
    run_id: str,
) -> None:
    with pytest.raises(native_handoff.NativeHandoffError, match="does not implement Windows"):
        _record(backend, workspace, definition, run_id)
    assert not native_handoff.result_path().exists()


def test_the_native_handoff_refuses_a_well_formed_windows_task(
    monkeypatch, tmp_path
) -> None:
    backend, workspace, definition, run_id = _windows_backend(monkeypatch, tmp_path)
    _refuse_windows_record(backend, workspace, definition, run_id)


def test_a_windows_task_with_a_second_action_is_refused_before_a_result(
    monkeypatch, tmp_path
) -> None:
    """A second ``Exec`` would launch a second program; the native path refuses.

    Windows task XML carries no port and its registration path forces an
    overwrite, so the native module cannot bind the same facts as macOS. It
    refuses every Windows task, including one carrying an extra action, before
    a result is written. Re-enabling Windows requires exact action semantics.
    """
    backend, workspace, definition, run_id = _windows_backend(monkeypatch, tmp_path)
    definition.write_text(
        _windows_task(
            extra_action=(
                "    <Exec>\n"
                "      <Command>C:\\attacker\\payload.exe</Command>\n"
                "      <Arguments>--exfiltrate</Arguments>\n"
                "    </Exec>"
            )
        ),
        encoding="utf-16",
    )
    namespace = windows_service.TASK_NS
    root = ElementTree.fromstring(definition.read_text(encoding="utf-16"))
    assert len(root.findall(f"{namespace}Actions/{namespace}Exec")) == 2
    _refuse_windows_record(backend, workspace, definition, run_id)


def test_a_windows_task_with_a_second_trigger_is_refused_before_a_result(
    monkeypatch, tmp_path
) -> None:
    """A second trigger changes when the engine runs, not just how it starts."""
    backend, workspace, definition, run_id = _windows_backend(monkeypatch, tmp_path)
    definition.write_text(
        _windows_task(
            extra_trigger=(
                "    <TimeTrigger>\n"
                "      <Repetition>\n"
                "        <Interval>PT1M</Interval>\n"
                "      </Repetition>\n"
                "      <StartBoundary>2026-01-01T00:00:00</StartBoundary>\n"
                "      <Enabled>true</Enabled>\n"
                "    </TimeTrigger>"
            )
        ),
        encoding="utf-16",
    )
    namespace = windows_service.TASK_NS
    root = ElementTree.fromstring(definition.read_text(encoding="utf-16"))
    triggers = root.find(f"{namespace}Triggers")
    assert triggers is not None
    assert len(list(triggers)) == 2
    _refuse_windows_record(backend, workspace, definition, run_id)


def test_native_activation_refuses_windows_before_reading_a_result(
    monkeypatch, tmp_path
) -> None:
    backend, _workspace, _definition, run_id = _windows_backend(monkeypatch, tmp_path)

    with pytest.raises(native_handoff.NativeHandoffError, match="does not implement Windows"):
        native_handoff.activate(run_id=run_id, is_set_up=lambda _ws: True)

    assert backend.loaded == []
    assert backend.fresh_calls == []


def test_the_legacy_reader_refuses_a_schema_2_result(monkeypatch, tmp_path) -> None:
    _backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    _write_result_document(
        monkeypatch,
        tmp_path,
        _schema_2_document(run_id=run_id, workspace=workspace, definition=definition),
    )

    with pytest.raises(installer_handoff.HandoffError):
        installer_handoff.read_result(expected_run_id=run_id)


def test_the_native_reader_refuses_a_windows_result(monkeypatch, tmp_path) -> None:
    _backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    _write_result_document(
        monkeypatch,
        tmp_path,
        _schema_2_document(
            run_id=run_id,
            workspace=workspace,
            definition=definition,
            backend="windows",
        ),
    )

    with pytest.raises(native_handoff.NativeHandoffError, match="does not implement Windows"):
        native_handoff.read_result(expected_run_id=run_id)


def test_the_native_reader_refuses_a_schema_1_result(monkeypatch, tmp_path) -> None:
    _backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    # The chain is prepared privately first, exactly as the native parent must
    # before the bootstrap child runs: the legacy writer's own ``mkdir`` leaves
    # the directory at the process umask, which is a refusal the native reader
    # would otherwise report before it ever reaches the schema.
    native_handoff.prepare_bootstrap_scratch()
    installer_handoff.record_setup_completion(
        run_id=run_id,
        startup_id="startup-1",
        workspace=workspace,
        written=[definition],
        port=8443,
    )

    with pytest.raises(native_handoff.NativeHandoffError, match="schema 1"):
        native_handoff.read_result(expected_run_id=run_id)


@DARWIN_ONLY
def test_legacy_recording_and_activation_stay_schema_1_and_use_load_agent(
    monkeypatch, tmp_path
) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    path = installer_handoff.record_setup_completion(
        run_id=run_id,
        startup_id="startup-1",
        workspace=workspace,
        written=[definition],
        port=8443,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["schema"] == 1
    assert set(payload) == {
        "schema",
        "run_id",
        "startup_id",
        "workspace",
        "service_backend",
        "definition_path",
        "port",
    }

    result = installer_handoff.activate(run_id=run_id, is_set_up=lambda _ws: True)

    assert result.run_id == run_id
    assert backend.loaded == [definition]
    assert backend.fresh_calls == []


@DARWIN_ONLY
def test_native_activation_verifies_and_bootstraps_the_live_definition_once(
    monkeypatch, tmp_path
) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    _record(backend, workspace, definition, run_id)

    result = _activate(monkeypatch, backend)

    expected = backend.live / _definition_name()
    assert result.workspace == workspace.resolve()
    assert backend.loaded == [expected]
    assert result.definition_sha256 == _digest(expected)
    if backend.name == "macos":
        assert backend.fresh_calls == [(expected, _digest(expected))]
    else:
        assert backend.fresh_calls == []


@DARWIN_ONLY
def test_native_activation_accepts_the_legacy_workspaces_env(
    monkeypatch, tmp_path
) -> None:
    """Without a registry, a non-empty ``CIAO_WORKSPACES`` is the same answer."""
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    _record(backend, workspace, definition, run_id)
    (workspace / ".env").write_text("CIAO_WORKSPACES=main\n", encoding="utf-8")
    monkeypatch.setattr(service_backend, "current_backend", lambda: backend)

    assert _within(5.0, lambda: native_handoff.activate(run_id=run_id)).workspace == (
        workspace.resolve()
    )
    assert backend.loaded == [backend.live / _definition_name()]


@DARWIN_ONLY
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
def test_native_activation_refuses_a_fifo_workspace_env_promptly(
    monkeypatch, tmp_path
) -> None:
    """The native completed-workspace check must not read a FIFO ``.env``."""
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    _record(backend, workspace, definition, run_id)
    os.mkfifo(workspace / ".env")
    monkeypatch.setattr(service_backend, "current_backend", lambda: backend)

    with pytest.raises(native_handoff.NativeHandoffError, match="not a regular file"):
        _within(5.0, lambda: native_handoff.activate(run_id=run_id))
    assert backend.loaded == []


@DARWIN_ONLY
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
def test_native_activation_uses_the_registry_without_reading_a_fifo_env(
    monkeypatch, tmp_path
) -> None:
    """A registry is a complete setup; a FIFO ``.env`` cannot change that answer."""
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    _record(backend, workspace, definition, run_id)
    runtime = workspace / ".runtime"
    runtime.mkdir()
    (runtime / "workspaces.json").write_text("{}\n", encoding="utf-8")
    os.mkfifo(workspace / ".env")
    monkeypatch.setattr(service_backend, "current_backend", lambda: backend)

    result = _within(5.0, lambda: native_handoff.activate(run_id=run_id))

    assert result.workspace == workspace.resolve()
    assert backend.loaded == [backend.live / _definition_name()]


@DARWIN_ONLY
def test_native_activation_refuses_an_unreadable_workspace_env(
    monkeypatch, tmp_path
) -> None:
    """An incomplete workspace is a refusal, and so is one that cannot be read."""
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    _record(backend, workspace, definition, run_id)
    (workspace / ".env").write_text("CIAO_OTHER=1\n", encoding="utf-8")
    monkeypatch.setattr(service_backend, "current_backend", lambda: backend)
    monkeypatch.setattr(
        native_handoff,
        "read_bounded_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            native_handoff.DefinitionReadError("the workspace .env is unreadable")
        ),
    )

    with pytest.raises(native_handoff.NativeHandoffError, match="unreadable"):
        native_handoff.activate(run_id=run_id)
    assert backend.loaded == []


@DARWIN_ONLY
def test_native_activation_refuses_a_disabled_override_before_bootstrap(
    monkeypatch, tmp_path
) -> None:
    """The native activation inherits the fresh loader's read-only fail-closed.

    The loader is the real one here: the absence probe answers, the override
    listing says the label is disabled, and no ``bootstrap`` (or ``enable``,
    ``unload``, ``load``, ``kickstart``) may run.
    """
    backend, workspace, definition, run_id = _recordable(
        monkeypatch, tmp_path, patch_fresh=False
    )
    _record(backend, workspace, definition, run_id)
    monkeypatch.setattr(macos_service, "server_job_absent", lambda **_kwargs: True)
    monkeypatch.setattr(
        macos_service, "launchd_override_disabled", lambda **_kwargs: True
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(
        macos_service.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(list(command)),
    )

    with pytest.raises(native_handoff.NativeHandoffError, match="disabled"):
        _activate(monkeypatch, backend)

    assert calls == []
    assert backend.loaded == []


@DARWIN_ONLY
def test_native_activation_refuses_a_changed_definition(monkeypatch, tmp_path) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    _record(backend, workspace, definition, run_id)
    definition.write_bytes(
        plistlib.dumps(
            {
                "Label": macos_service.SERVER_LABEL,
                "ProgramArguments": [str(Path(sys.executable)), "x"],
                "EnvironmentVariables": _approved_environment(workspace),
            }
        )
    )

    with pytest.raises(native_handoff.NativeHandoffError, match="changed after setup"):
        _activate(monkeypatch, backend)
    assert backend.loaded == []


@DARWIN_ONLY
def test_the_native_reader_refuses_duplicate_fields(monkeypatch, tmp_path) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    path = _record(backend, workspace, definition, run_id)
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            '"schema": 2,', '"schema": 2, "schema": 2,', 1
        ),
        encoding="utf-8",
    )

    with pytest.raises(native_handoff.NativeHandoffError, match="repeats the field"):
        native_handoff.read_result(expected_run_id=run_id)


@DARWIN_ONLY
def test_the_native_reader_refuses_a_non_private_result(monkeypatch, tmp_path) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    path = _record(backend, workspace, definition, run_id)
    text = path.read_text(encoding="utf-8")
    # A fresh name inherits the directory's permissions (0644 here) instead of
    # the owner-only mode open_private created; chmod alone would not loosen a
    # Windows DACL, so it could not express this on every platform.
    path.unlink()
    path.write_text(text, encoding="utf-8")
    if os.name == "posix":
        path.chmod(0o644)

    with pytest.raises(native_handoff.NativeHandoffError, match="owner-only"):
        native_handoff.read_result(expected_run_id=run_id)


@DARWIN_ONLY
def test_the_native_reader_refuses_a_result_whose_acl_grants_access(
    monkeypatch, tmp_path
) -> None:
    """The mode can be 0600 while an extended ACL still lets everyone read."""
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    path = _record(backend, workspace, definition, run_id)
    assert is_private(path)
    subprocess.run(
        ["chmod", "+a", "everyone allow read", str(path)],
        capture_output=True,
        check=True,
    )
    try:
        with pytest.raises(native_handoff.NativeHandoffError, match="owner-only"):
            native_handoff.read_result(expected_run_id=run_id)
    finally:
        subprocess.run(
            ["chmod", "-a", "everyone allow read", str(path)],
            capture_output=True,
            check=False,
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits are the swap")
@DARWIN_ONLY
def test_the_native_reader_checks_privacy_on_the_opened_descriptor(
    monkeypatch, tmp_path
) -> None:
    """The pathname may still look owner-only when the opened inode is not.

    The mode is loosened after the reader's ``lstat`` and before its ``open``:
    the inode is unchanged, so only the descriptor-bound check can catch it.
    """
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    path = _record(backend, workspace, definition, run_id)
    real_open = os.open
    loosened = False

    def loosening_open(target, flags, *args, **kwargs):
        nonlocal loosened
        if not loosened and os.fspath(target) == os.fspath(path):
            loosened = True
            path.chmod(0o644)
        return real_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", loosening_open)

    with pytest.raises(native_handoff.NativeHandoffError, match="owner-only"):
        native_handoff.read_result(expected_run_id=run_id)
    assert loosened


@DARWIN_ONLY
def test_the_native_reader_refuses_an_oversized_result(monkeypatch, tmp_path) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    path = _record(backend, workspace, definition, run_id)
    path.write_text("x" * (native_handoff.MAX_RESULT_BYTES + 1), encoding="utf-8")

    with pytest.raises(native_handoff.NativeHandoffError, match="larger than"):
        native_handoff.read_result(expected_run_id=run_id)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
@DARWIN_ONLY
def test_the_native_reader_refuses_a_fifo_result_promptly(monkeypatch, tmp_path) -> None:
    """A FIFO where the result belongs must be a refusal, not a hung installer."""
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    path = native_handoff.result_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    os.mkfifo(path)

    with pytest.raises(native_handoff.NativeHandoffError, match="not a regular file"):
        _within(5.0, lambda: native_handoff.read_result(expected_run_id=run_id))


@pytest.mark.skipif(os.name != "posix", reason="symlink semantics")
@DARWIN_ONLY
def test_the_native_reader_refuses_a_symlinked_result(monkeypatch, tmp_path) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    path = _record(backend, workspace, definition, run_id)
    target = tmp_path / "elsewhere.json"
    os.replace(path, target)
    path.symlink_to(target)

    with pytest.raises(native_handoff.NativeHandoffError, match="not a regular file"):
        native_handoff.read_result(expected_run_id=run_id)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")
@DARWIN_ONLY
def test_a_result_swapped_for_a_fifo_between_the_check_and_the_open_is_refused(
    monkeypatch, tmp_path
) -> None:
    """The type is re-checked on the descriptor, not only on the pathname.

    The swap is deterministic: the real ``os.open`` is wrapped so the result is
    replaced by a FIFO after the reader's ``lstat`` and immediately before its
    open. ``O_NONBLOCK`` is what makes a regressed reader fail instead of hang.
    """
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    path = _record(backend, workspace, definition, run_id)
    real_open = os.open
    swapped = False

    def swapping_open(target, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and os.fspath(target) == os.fspath(path):
            swapped = True
            path.unlink()
            os.mkfifo(path)
        return real_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swapping_open)

    with pytest.raises(native_handoff.NativeHandoffError, match="not a regular file"):
        _within(5.0, lambda: native_handoff.read_result(expected_run_id=run_id))
    assert swapped


@DARWIN_ONLY
def test_the_native_reader_refuses_a_result_that_is_not_utf8(monkeypatch, tmp_path) -> None:
    """Malformed UTF-8 is refused, never replacement-decoded into a document."""
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    path = _record(backend, workspace, definition, run_id)
    text = json.dumps(json.loads(path.read_text(encoding="utf-8")))
    path.write_bytes(text.encode("utf-8")[:-1] + b"\xff\xfe\n")

    with pytest.raises(native_handoff.NativeHandoffError, match="not valid UTF-8"):
        native_handoff.read_result(expected_run_id=run_id)


@DARWIN_ONLY
def test_the_native_reader_refuses_a_partial_read(monkeypatch, tmp_path) -> None:
    """An early EOF is refused even though the metadata did not move."""
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    _record(backend, workspace, definition, run_id)
    real_read = os.read
    calls = 0

    def short_read(descriptor: int, size: int) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_read(descriptor, size)[:-1]
        return b""

    monkeypatch.setattr(os, "read", short_read)

    with pytest.raises(
        native_handoff.NativeHandoffError, match="changed while it was being read"
    ):
        native_handoff.read_result(expected_run_id=run_id)


@DARWIN_ONLY
@pytest.mark.parametrize(
    ("key", "label"),
    [
        ("engine_program_identity", "engine_program_identity"),
        ("engine_python_identity", "engine_python_identity"),
    ],
)
def test_the_native_reader_names_the_identity_it_rejects(
    monkeypatch, tmp_path, key: str, label: str
) -> None:
    """The program and interpreter identities are separate files and named so."""
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    path = _record(backend, workspace, definition, run_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload[key] = {"device": -1, "inode": 0, "size": 0, "mtime_ns": 0}
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(native_handoff.NativeHandoffError, match=label):
        native_handoff.read_result(expected_run_id=run_id)


@DARWIN_ONLY
def test_native_activation_refuses_a_replaced_or_removed_interpreter(
    monkeypatch, tmp_path
) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    engine = tmp_path / "bin" / "python3"
    engine.parent.mkdir()
    engine.write_bytes(b"#!/bin/sh\nexit 0\n")
    definition.write_bytes(
        _approved_plist(
            workspace,
            ProgramArguments=[str(engine), "-m", "ciao.cli", "run"],
        )
    )
    _record(backend, workspace, definition, run_id, engine_python=str(engine))
    engine.write_bytes(b"#!/bin/sh\nexit 1\n")

    # The direct Python shape runs the interpreter itself, so program and
    # interpreter are the same file and the program check reports first.
    with pytest.raises(native_handoff.NativeHandoffError, match="changed after setup"):
        _activate(monkeypatch, backend)

    engine.unlink()
    with pytest.raises(native_handoff.NativeHandoffError, match="is missing"):
        _activate(monkeypatch, backend)
    assert backend.loaded == []


@DARWIN_ONLY
def test_native_activation_refuses_an_environment_changed_in_the_handoff(
    monkeypatch, tmp_path
) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    path = _record(backend, workspace, definition, run_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["environment"]["PATH"] = "/tmp/attacker-first:/usr/bin:/bin"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(native_handoff.NativeHandoffError, match="environment changed"):
        _activate(monkeypatch, backend)


@DARWIN_ONLY
@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"Extra": True}, "keys this installer never writes"),
        ({"Label": "com.example.other"}, "label is"),
        ({"StandardOutPath": "/tmp/elsewhere.log"}, "workspace log"),
        ({"SoftResourceLimits": {"NumberOfFiles": 1}}, "resource limits"),
        (
            {
                "ProgramArguments": [
                    str(Path(sys.executable)),
                    "-m",
                    "ciao.cli",
                    "run-move",
                ]
            },
            "approved engine command",
        ),
    ],
)
def test_recording_refuses_unapproved_job_semantics(
    monkeypatch, tmp_path, overrides, match
) -> None:
    """The same bytes that would be digested are judged before a result exists."""
    backend, workspace, _default, run_id = _recordable(monkeypatch, tmp_path)
    definition = backend.live / _definition_name()
    definition.write_bytes(_approved_plist(workspace, **overrides))

    with pytest.raises(native_handoff.NativeHandoffError, match=match):
        _record(backend, workspace, definition, run_id)
    assert not native_handoff.result_path().exists()
    assert backend.loaded == []


@DARWIN_ONLY
def test_native_activation_refuses_a_definition_environment_changed_after_recording(
    monkeypatch, tmp_path
) -> None:
    backend, workspace, _default, run_id = _recordable(monkeypatch, tmp_path)
    definition = backend.live / _definition_name()
    _record(backend, workspace, definition, run_id)

    # Same workspace and port, different PATH: the definition still names this
    # workspace, so only the recorded environment comparison catches it.
    environment = _approved_environment(workspace)
    environment["PATH"] = "/tmp/first:/usr/bin:/bin"
    definition.write_bytes(_approved_plist(workspace, EnvironmentVariables=environment))

    with pytest.raises(native_handoff.NativeHandoffError, match="changed after setup"):
        _activate(monkeypatch, backend)
    assert backend.loaded == []


@DARWIN_ONLY
def test_recording_refuses_a_definition_environment_key_setup_never_writes(
    monkeypatch, tmp_path
) -> None:
    backend, workspace, _default, run_id = _recordable(monkeypatch, tmp_path)
    definition = backend.live / _definition_name()
    environment = _approved_environment(workspace)
    environment["PYTHONPATH"] = "/tmp/interpreter-injection"
    definition.write_bytes(_approved_plist(workspace, EnvironmentVariables=environment))

    with pytest.raises(native_handoff.NativeHandoffError, match="never writes"):
        _record(backend, workspace, definition, run_id)
    assert not native_handoff.result_path().exists()
    assert backend.loaded == []


@DARWIN_ONLY
def test_native_activation_accepts_the_real_template_direct_shape(
    monkeypatch, tmp_path
) -> None:
    backend, workspace, _default, run_id = _recordable(monkeypatch, tmp_path)
    engine = str(Path(sys.executable))
    definition = _real_definition(backend, workspace, engine_path=engine)
    rendered = plistlib.loads(definition.read_bytes())
    assert rendered["HardResourceLimits"] == {"NumberOfFiles": SERVER_NOFILE_TARGET}
    assert rendered["SoftResourceLimits"] == {"NumberOfFiles": SERVER_NOFILE_TARGET}
    assert rendered["StandardOutPath"] == str(workspace / ".runtime" / "ciao.stdout.log")
    assert rendered["StandardErrorPath"] == str(workspace / ".runtime" / "ciao.stderr.log")
    assert rendered["ProgramArguments"][0] == engine

    _record(backend, workspace, definition, run_id, engine_python=engine)

    assert _activate(monkeypatch, backend).workspace == workspace.resolve()
    assert backend.loaded == [definition]


@DARWIN_ONLY
def test_native_activation_accepts_the_real_template_hosted_shape(
    monkeypatch, tmp_path
) -> None:
    from ciao import server_host

    backend, workspace, _default, run_id = _recordable(monkeypatch, tmp_path)
    host = _real_host(monkeypatch, Path.home())
    engine = str(Path(sys.executable))
    definition = _real_definition(backend, workspace, host=host, host_python=engine)

    rendered = plistlib.loads(definition.read_bytes())
    assert rendered["ExitTimeOut"] == server_host.EXIT_TIMEOUT_SECONDS
    assert rendered["ProgramArguments"][1:] == ["serve", "--python", engine]

    _record(backend, workspace, definition, run_id, engine_python=engine)

    assert _activate(monkeypatch, backend).workspace == workspace.resolve()
    assert backend.loaded == [definition]


@DARWIN_ONLY
def test_hosted_setup_records_the_interpreter_the_job_serves(
    monkeypatch, tmp_path
) -> None:
    from ciao import server_host

    backend, workspace, _default, run_id = _recordable(monkeypatch, tmp_path)
    host = _real_host(monkeypatch, Path.home())
    console = Path.home() / ".local" / "bin" / "ciao"
    console.parent.mkdir(parents=True, exist_ok=True)
    console.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    engine = str(Path(sys.executable))
    definition = _real_definition(backend, workspace, host=host, host_python=engine)

    path = _record(backend, workspace, definition, run_id, engine_python=str(console))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["engine_python"] == engine
    assert payload["engine_program"] == str(
        Path(host.bundle_path) / "Contents" / "MacOS" / server_host.EXECUTABLE_NAME
    )
    assert _activate(monkeypatch, backend).workspace == workspace.resolve()


@DARWIN_ONLY
def test_the_real_installer_path_is_recorded_exactly_and_not_policed(
    monkeypatch, tmp_path
) -> None:
    """The legacy wizard persists a development PATH; native activation keeps it.

    A directory allowlist derived from the machine at activation time would
    refuse the bytes setup legitimately wrote. The digest binds the definition,
    so the recorded environment is compared exactly, ``PATH`` included.
    """
    backend, workspace, _default, run_id = _recordable(monkeypatch, tmp_path)
    engine = str(Path(sys.executable))
    inherited = os.pathsep.join(
        [
            f"{Path(engine).parent}/",
            "/tmp/not-a-service-tool-dir",
            "/usr/bin",
            "/bin",
            "/usr/bin",
        ]
    )
    definition = _real_definition(
        backend, workspace, engine_path=engine, path=inherited
    )
    rendered = plistlib.loads(definition.read_bytes())
    assert rendered["EnvironmentVariables"]["PATH"] == os.pathsep.join(
        [f"{Path(engine).parent}/", "/tmp/not-a-service-tool-dir", "/usr/bin", "/bin"]
    )

    _record(backend, workspace, definition, run_id, engine_python=engine)
    assert _activate(monkeypatch, backend).workspace == workspace.resolve()


@DARWIN_ONLY
def test_a_direct_console_records_the_script_and_the_shebang_interpreter(
    monkeypatch, tmp_path
) -> None:
    backend, workspace, _default, run_id = _recordable(monkeypatch, tmp_path)
    interpreter = tmp_path / "venv" / "bin" / "python3"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"#!/bin/sh\nexit 0\n")
    console = tmp_path / "bin" / "ciao"
    console.parent.mkdir()
    console.write_text(f"#!{interpreter}\nprint('ciao')\n", encoding="utf-8")
    definition = backend.live / _definition_name()
    definition.write_bytes(
        _approved_plist(
            workspace,
            ProgramArguments=[str(console), "run"],
        )
    )

    path = _record(backend, workspace, definition, run_id, engine_python=str(console))
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["engine_program"] == str(console)
    assert payload["engine_python"] == str(interpreter)
    assert payload["engine_program_identity"] == {
        "device": os.stat(console).st_dev,
        "inode": os.stat(console).st_ino,
        "size": os.stat(console).st_size,
        "mtime_ns": os.stat(console).st_mtime_ns,
    }
    assert payload["engine_python_identity"] == {
        "device": os.stat(interpreter).st_dev,
        "inode": os.stat(interpreter).st_ino,
        "size": os.stat(interpreter).st_size,
        "mtime_ns": os.stat(interpreter).st_mtime_ns,
    }
    assert _activate(monkeypatch, backend).workspace == workspace.resolve()

    console.write_text(f"#!{interpreter}\nprint('replaced')\n", encoding="utf-8")
    with pytest.raises(native_handoff.NativeHandoffError, match="executed program changed"):
        _activate(monkeypatch, backend)


@DARWIN_ONLY
def test_activation_refuses_a_replaced_console_interpreter(
    monkeypatch, tmp_path
) -> None:
    backend, workspace, _default, run_id = _recordable(monkeypatch, tmp_path)
    interpreter = tmp_path / "venv" / "bin" / "python3"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"#!/bin/sh\nexit 0\n")
    console = tmp_path / "bin" / "ciao"
    console.parent.mkdir()
    console.write_text(f"#!{interpreter}\nprint('ciao')\n", encoding="utf-8")
    definition = backend.live / _definition_name()
    definition.write_bytes(
        _approved_plist(workspace, ProgramArguments=[str(console), "run"])
    )
    _record(backend, workspace, definition, run_id, engine_python=str(console))

    interpreter.write_bytes(b"#!/bin/sh\nexit 1\n")

    with pytest.raises(native_handoff.NativeHandoffError, match="engine interpreter changed"):
        _activate(monkeypatch, backend)
    assert backend.loaded == []


@DARWIN_ONLY
@pytest.mark.parametrize(
    ("shebang", "match"),
    [
        ("#!/usr/bin/env python3", "one absolute interpreter"),
        ("#!python3", "not absolute"),
        ("#!/usr/bin/python3 -I", "one absolute interpreter"),
        ("#!/bin/sh", "not a Python executable"),
        ("print('no shebang')\n", "no supported shebang"),
    ],
)
def test_an_unsupported_console_is_refused_before_a_result_is_written(
    monkeypatch, tmp_path, shebang: str, match: str
) -> None:
    backend, workspace, _default, run_id = _recordable(monkeypatch, tmp_path)
    console = tmp_path / "bin" / "ciao"
    console.parent.mkdir()
    console.write_text(f"{shebang}\nexit 0\n", encoding="utf-8")
    definition = backend.live / _definition_name()
    definition.write_bytes(
        _approved_plist(workspace, ProgramArguments=[str(console), "run"])
    )

    with pytest.raises(native_handoff.NativeHandoffError, match=match):
        _record(backend, workspace, definition, run_id, engine_python=str(console))

    assert not native_handoff.result_path().exists()


@pytest.mark.parametrize(
    ("key", "match"),
    [
        ("Label", "label is"),
        ("AssociatedBundleIdentifiers", "AssociatedBundleIdentifiers"),
        ("ProgramArguments", "approved engine command"),
        ("WorkingDirectory", "does not name its WorkingDirectory"),
        ("EnvironmentVariables", "does not name a workspace"),
        ("RunAtLoad", "does not set RunAtLoad"),
        ("KeepAlive", "does not set KeepAlive"),
        ("SoftResourceLimits", "setup resource limit \\(SoftResourceLimits\\)"),
        ("HardResourceLimits", "setup resource limit \\(HardResourceLimits\\)"),
        ("StandardOutPath", "StandardOutPath is not an absolute path"),
        ("StandardErrorPath", "StandardErrorPath is not an absolute path"),
    ],
)
@DARWIN_ONLY
def test_recording_refuses_a_definition_missing_a_template_key(
    monkeypatch, tmp_path, key: str, match: str
) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    payload = plistlib.loads(definition.read_bytes())
    payload.pop(key)
    definition.write_bytes(plistlib.dumps(payload))

    with pytest.raises(native_handoff.NativeHandoffError, match=match):
        _record(backend, workspace, definition, run_id)
    assert not native_handoff.result_path().exists()
    assert backend.loaded == []


@DARWIN_ONLY
def test_recording_refuses_a_direct_definition_with_an_exit_timeout(
    monkeypatch, tmp_path
) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    definition.write_bytes(
        _approved_plist(workspace, ExitTimeOut=45)
    )

    with pytest.raises(native_handoff.NativeHandoffError, match="ExitTimeOut"):
        _record(backend, workspace, definition, run_id)
    assert not native_handoff.result_path().exists()
    assert backend.loaded == []


@DARWIN_ONLY
def test_recording_refuses_a_hosted_definition_missing_its_exit_timeout(
    monkeypatch, tmp_path
) -> None:
    backend, workspace, _default, run_id = _recordable(monkeypatch, tmp_path)
    host = _real_host(monkeypatch, Path.home())
    definition = _real_definition(
        backend, workspace, host=host, host_python=str(Path(sys.executable))
    )
    payload = plistlib.loads(definition.read_bytes())
    payload.pop("ExitTimeOut")
    definition.write_bytes(plistlib.dumps(payload))

    with pytest.raises(native_handoff.NativeHandoffError, match="ExitTimeOut"):
        _record(backend, workspace, definition, run_id)
    assert not native_handoff.result_path().exists()
    assert backend.loaded == []


@DARWIN_ONLY
def test_recording_refuses_a_definition_missing_a_template_environment_key(
    monkeypatch, tmp_path
) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    environment = _approved_environment(workspace)
    environment.pop("PATH")
    definition.write_bytes(
        _approved_plist(workspace, EnvironmentVariables=environment)
    )

    with pytest.raises(native_handoff.NativeHandoffError, match="environment keys"):
        _record(backend, workspace, definition, run_id)
    assert not native_handoff.result_path().exists()
    assert backend.loaded == []


@DARWIN_ONLY
def test_recording_refuses_a_foreign_associated_bundle_identifier(
    monkeypatch, tmp_path
) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    definition.write_bytes(
        _approved_plist(
            workspace, AssociatedBundleIdentifiers=["local.example.other"]
        )
    )

    with pytest.raises(native_handoff.NativeHandoffError, match="AssociatedBundleIdentifiers"):
        _record(backend, workspace, definition, run_id)
    assert not native_handoff.result_path().exists()
    assert backend.loaded == []


@DARWIN_ONLY
def test_recording_refuses_a_definition_for_a_different_workspace(
    monkeypatch, tmp_path
) -> None:
    """The workspace comes from the same bytes that are hashed, not a second read."""
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    other = tmp_path / "other-workspace"
    other.mkdir()
    definition.write_bytes(_approved_plist(other))

    with pytest.raises(
        native_handoff.NativeHandoffError,
        match="serves .*not the chosen workspace",
    ):
        _record(backend, workspace, definition, run_id)
    assert not native_handoff.result_path().exists()
    assert backend.loaded == []


@DARWIN_ONLY
def test_recording_refuses_a_definition_for_a_different_port(
    monkeypatch, tmp_path
) -> None:
    backend, workspace, definition, run_id = _recordable(monkeypatch, tmp_path)
    environment = _approved_environment(workspace)
    environment["CIAO_PORT"] = "9443"
    definition.write_bytes(
        _approved_plist(workspace, EnvironmentVariables=environment)
    )

    with pytest.raises(
        native_handoff.NativeHandoffError,
        match="CIAO_PORT is '9443', not the port setup chose \\(8443\\)",
    ):
        _record(backend, workspace, definition, run_id)
    assert not native_handoff.result_path().exists()
    assert backend.loaded == []


def _real_definition(
    backend: _Backend,
    workspace: Path,
    *,
    path: str | None = None,
    engine_path: str | None = None,
    host: server_host.HostOwnership | None = None,
    host_python: str | None = None,
) -> Path:
    backend.live.mkdir(parents=True, exist_ok=True)
    definition = backend.live / _definition_name()
    definition.write_text(
        cli._render_launchd_plist(
            workspace=workspace,
            port=8443,
            path=os.environ.get("PATH", "") if path is None else path,
            engine_path=engine_path,
            host=host,
            host_python=host_python,
        ),
        encoding="utf-8",
    )
    return definition


def _real_host(monkeypatch: pytest.MonkeyPatch, home: Path):
    from ciao import server_host

    bundle = home / "Applications" / server_host.APP_NAME
    executable = bundle / "Contents" / "MacOS" / server_host.EXECUTABLE_NAME
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    ownership = server_host.HostOwnership(
        schema=server_host.SCHEMA_VERSION,
        bundle_path=str(bundle),
        bundle_id=server_host.BUNDLE_ID,
        host_revision=server_host.HOST_REVISION,
        host_protocol=server_host.HOST_PROTOCOL,
        executable_sha256="a" * 64,
        per_arch_cdhashes={"arm64": "b" * 40},
        bundle_files={},
    )
    monkeypatch.setattr(server_host, "verify_owned_host", lambda _path: ownership)
    return ownership
