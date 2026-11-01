"""The read-only native readiness proof (#2340).

Every proof test injects :class:`NativeReadinessDeps`; no test starts, stops or
reloads a service. The lock and ownership record are real private files under
``tmp_path`` so the descriptor checks are exercised rather than mocked. The HTTP
adapter is exercised against a local, isolated loopback socket (never an external
network), and the bounded subprocess runner against short local Python children.
"""

from __future__ import annotations

import dataclasses
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, TypeVar

import pytest

from ciao import native_readiness as readiness
from ciao.macos_service import LAUNCHCTL, SERVER_LABEL
from ciao.native_readiness import (
    FAILURE_CODES,
    MAX_LOCK_BYTES,
    MAX_STARTUP_RESPONSE_BYTES,
    LaunchdJob,
    NativeReadinessDeps,
    NativeReadinessError,
    NativeReadinessPolicy,
    NativeReadinessRequest,
    NativeReadinessProof,
    make_native_readiness_deps,
    parse_launchd_job,
    parse_listener_pid,
    parse_ps_ppid,
)
from ciao.server_host import (
    APP_NAME,
    BUNDLE_ID,
    HOST_PROTOCOL,
    HOST_REVISION,
    REQUIRED_BUNDLE_FILES,
    HostOwnership,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the native host readiness proof is macOS-only"
)

VERSION = "1.7.0"
PORT = 8443
LISTENER = 4242
HOST_PID = 4000
STARTUP_ID = "0123456789abcdef0123456789abcdef"


def _bundle(tmp_path: Path) -> Path:
    return tmp_path / "Applications" / APP_NAME


def _ownership(bundle: Path) -> HostOwnership:
    """A schema-1 snapshot of ``bundle``, as a verified verifier would return it."""
    files = {name: "d" * 64 for name in REQUIRED_BUNDLE_FILES}
    files["Contents/MacOS/Ciaobot Server"] = "a" * 64
    return HostOwnership(
        schema=1,
        bundle_path=str(bundle),
        bundle_id=BUNDLE_ID,
        host_revision=HOST_REVISION,
        host_protocol=HOST_PROTOCOL,
        executable_sha256="a" * 64,
        per_arch_cdhashes={"arm64": "b" * 40, "x86_64": "c" * 40},
        bundle_files=files,
        signing=None,
    )


def _host_path(request: NativeReadinessRequest) -> Path:
    """The canonical host executable, derived the way the module derives it."""
    return readiness._host_executable(_ownership(request.host_bundle))


def _request(tmp_path: Path, **overrides: object) -> NativeReadinessRequest:
    values: dict[str, object] = {
        "version": VERSION,
        "port": PORT,
        "workspace": tmp_path / "Ciaobot Workspace",
        "host_bundle": _bundle(tmp_path),
        "ownership_path": tmp_path / "host-ownership.json",
        "engine_python": tmp_path / "python3.12",
        "fresh_install_service_absent": True,
        "label": SERVER_LABEL,
    }
    values.update(overrides)
    return NativeReadinessRequest(**values)  # type: ignore[arg-type]


def _verified(request: NativeReadinessRequest) -> Callable[[Path, Path], HostOwnership]:
    """The ``verify_owned`` double: the owned bundle verifies against its record."""

    def verify(_bundle: Path, _record: Path) -> HostOwnership:
        return _ownership(request.host_bundle)

    return verify


def _prove(
    request: NativeReadinessRequest,
    *,
    deps: NativeReadinessDeps,
    policy: NativeReadinessPolicy | None = None,
    cancelled: Callable[[], bool] | None = None,
    verify_owned: Callable[[Path, Path], HostOwnership] | None = None,
) -> NativeReadinessProof:
    """Run the proof with ownership verification injected through the private seam."""
    return readiness._prove_native_readiness(
        request,
        deps=deps,
        policy=policy,
        cancelled=cancelled,
        verify_owned=_verified(request) if verify_owned is None else verify_owned,
    )


def _payload(
    request: NativeReadinessRequest,
    *,
    startup_id: str = STARTUP_ID,
    pid: int = LISTENER,
    version: str | None = None,
    overall_ready: bool = True,
    port_in_use: bool = False,
    bind_status: str = "done",
    phases: list[dict[str, object]] | None = None,
    **overrides: object,
) -> dict[str, object]:
    if phases is None:
        phases = [
            {
                "name": "server_starting",
                "status": bind_status,
                "message": "",
                "started_at": "2026-11-01T00:00:00+00:00",
                "finished_at": "2026-11-01T00:00:01+00:00",
            }
        ]
    payload: dict[str, object] = {
        "phases": phases,
        "overall_ready": overall_ready,
        "startup_id": startup_id,
        "pid": pid,
        "version": request.version if version is None else version,
        "desktop_api_version": 1,
        "latest_version": None,
        "update_available": False,
        "port_in_use": port_in_use,
    }
    payload.update(overrides)
    return payload


def _startup_bytes(request: NativeReadinessRequest, **overrides: object) -> bytes:
    return json.dumps(_payload(request, **overrides)).encode("utf-8")


def _write_lock(
    workspace: Path,
    *,
    startup_id: str = STARTUP_ID,
    pid: int = LISTENER,
    port: int = PORT,
    workspace_value: str | None = None,
    runtime_value: str | None = None,
    status: str = "running",
    raw: bytes | None = None,
    mode: int = 0o600,
) -> Path:
    runtime = workspace / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    path = runtime / "server.lock"
    if raw is None:
        document: dict[str, object] = {
            "pid": pid,
            "port": port,
            "workspace": str(workspace) if workspace_value is None else workspace_value,
            "runtime": str(runtime) if runtime_value is None else runtime_value,
            "started_at": "2026-11-01T00:00:00Z",
            "status": status,
            "startup_id": startup_id,
        }
        raw = json.dumps(document, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    path.write_bytes(raw)
    path.chmod(mode)
    return path


def _host_job(request: NativeReadinessRequest, **overrides: object) -> LaunchdJob:
    values: dict[str, object] = {
        "pid": HOST_PID,
        "arguments": (
            str(_host_path(request)),
            "serve",
            "--python",
            str(request.engine_python),
        ),
    }
    values.update(overrides)
    return LaunchdJob(**values)  # type: ignore[arg-type]


def _deps(
    request: NativeReadinessRequest,
    *,
    listener_values: list[int | None] | None = None,
    startup_status: object | None = None,
    job: object | None = None,
    chain: object | None = None,
    executable_path: object | None = None,
    clock: object | None = None,
    sleep: object | None = None,
    listener_calls: list[int] | None = None,
) -> NativeReadinessDeps:
    values = list(listener_values) if listener_values is not None else [LISTENER, LISTENER]

    def listener_pid(port: int) -> int | None:
        assert port == request.port
        if listener_calls is not None:
            listener_calls.append(port)
        return values.pop(0) if values else LISTENER

    def default_startup_status(port: int, timeout: float) -> bytes:
        assert port == request.port
        assert 0 < timeout <= NativeReadinessPolicy().probe_timeout
        return _startup_bytes(request)

    def default_job(label: str) -> LaunchdJob:
        assert label == request.label
        return _host_job(request)

    return NativeReadinessDeps(
        listener_pid=listener_pid,
        startup_status=(
            default_startup_status
            if startup_status is None
            else startup_status  # type: ignore[arg-type]
        ),
        job=default_job if job is None else job,  # type: ignore[arg-type]
        ancestor_pids=(
            (lambda pid: (LISTENER, HOST_PID)) if chain is None else chain  # type: ignore[arg-type]
        ),
        executable_path=(
            (lambda _pid: str(_host_path(request)))
            if executable_path is None
            else executable_path  # type: ignore[arg-type]
        ),
        clock=(lambda: 0.0) if clock is None else clock,  # type: ignore[arg-type]
        sleep=(lambda _seconds: None) if sleep is None else sleep,  # type: ignore[arg-type]
    )


# ── the successful chain and its read-only shape ────────────────────────────


def test_complete_chain_proves_the_installed_engine(tmp_path: Path) -> None:
    request = _request(tmp_path)
    lock = _write_lock(request.workspace)
    listener_calls: list[int] = []

    proof = _prove(
        request, deps=_deps(request, listener_calls=listener_calls)
    )

    assert proof.ready is True
    assert proof.failure is None
    assert proof.version == VERSION
    assert proof.startup_id == STARTUP_ID
    assert proof.listener_pid == LISTENER
    assert proof.host_pid == HOST_PID
    # Three listener observations: before the request (step 2), after it (step 4)
    # and the re-read after the ancestry/executable checks (step 9).
    assert listener_calls == [PORT, PORT, PORT]
    assert lock.exists()


def test_proof_does_not_modify_the_lock(tmp_path: Path) -> None:
    request = _request(tmp_path)
    lock = _write_lock(request.workspace)
    before = (lock.stat().st_mode, lock.stat().st_mtime_ns, lock.read_bytes())

    assert _prove(request, deps=_deps(request)).ready is True

    after = (lock.stat().st_mode, lock.stat().st_mtime_ns, lock.read_bytes())
    assert after == before


def test_workspace_and_runtime_are_compared_as_resolved_paths(tmp_path: Path) -> None:
    real = tmp_path / "Real Workspace"
    real.mkdir()
    linked = tmp_path / "Linked Workspace"
    linked.symlink_to(real, target_is_directory=True)
    request = _request(tmp_path, workspace=linked)
    _write_lock(real)

    assert _prove(request, deps=_deps(request)).ready is True


def test_hosted_argv_must_name_the_required_engine_python(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)
    job = LaunchdJob(
        pid=HOST_PID,
        arguments=(
            str(_host_path(request)),
            "serve",
            "--python",
            str(tmp_path / "python3.11"),
        ),
    )

    proof = _prove(request, deps=_deps(request, job=lambda _label: job))

    assert proof.failure == "job_python_mismatch"


# ── input, dependency and policy bounds ─────────────────────────────────────


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", ""),
        ("version", "1.7.0\n"),
        ("version", "x" * 65),
        ("port", 0),
        ("port", 65536),
        ("port", True),
        ("workspace", Path("relative")),
        ("workspace", Path("/Users/../elsewhere")),
        ("ownership_path", Path("relative")),
        ("host_bundle", Path("relative/Ciaobot Server.app")),
        ("host_bundle", Path("/Applications/Other.app")),
        ("fresh_install_service_absent", 1),
        ("fresh_install_service_absent", False),
        ("label", "bad/label"),
        ("label", "x" * 129),
        ("engine_python", None),
        ("engine_python", Path("relative")),
    ],
)
def test_invalid_inputs_are_refused_before_any_seam(
    tmp_path: Path, field: str, value: object
) -> None:
    request = _request(tmp_path)
    bad = dataclasses.replace(request, **{field: value})
    called: list[int] = []

    with pytest.raises(NativeReadinessError) as caught:
        _prove(
            bad, deps=_deps(request, listener_calls=called)
        )

    assert caught.value.reason == "invalid_arguments"
    assert str(caught.value) == "invalid_arguments"
    assert called == []


def test_proof_refuses_without_fresh_install_service_absent_evidence(tmp_path: Path) -> None:
    request = _request(tmp_path)
    called: list[int] = []

    with pytest.raises(NativeReadinessError) as caught:
        _prove(
            dataclasses.replace(request, fresh_install_service_absent=False),
            deps=_deps(request, listener_calls=called),
        )

    assert caught.value.reason == "invalid_arguments"
    assert called == []


# ── ownership verification is the real verifier's answer, injected only here ─


def test_verify_owned_receives_the_request_bundle_and_record(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)
    seen: list[tuple[Path, Path]] = []

    def verify(bundle: Path, record: Path) -> HostOwnership:
        seen.append((bundle, record))
        return _ownership(request.host_bundle)

    proof = _prove(request, deps=_deps(request), verify_owned=verify)

    assert proof.ready is True
    assert seen == [(request.host_bundle, request.ownership_path)]


def test_a_verification_refusal_is_a_host_verification_failure(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)
    listener_calls: list[int] = []

    def refuse(_bundle: Path, _record: Path) -> HostOwnership:
        raise OSError("record does not match the bundle")

    proof = _prove(
        request,
        deps=_deps(request, listener_calls=listener_calls),
        verify_owned=refuse,
    )

    assert proof.failure == "host_verification_failed"
    assert listener_calls == []
    assert "record" not in repr(proof)


def test_a_verifier_that_returns_no_snapshot_is_a_host_verification_failure(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)

    proof = _prove(
        request,
        deps=_deps(request),
        verify_owned=lambda _bundle, _record: None,  # type: ignore[arg-type, return-value]
    )

    assert proof.failure == "host_verification_failed"


def test_a_verified_host_for_a_different_bundle_is_refused(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)
    other = _ownership(tmp_path / "Elsewhere" / APP_NAME)

    proof = _prove(
        request,
        deps=_deps(request),
        verify_owned=lambda _bundle, _record: other,
    )

    assert proof.failure == "host_verification_failed"


def test_invalid_deps_are_refused_before_any_read(tmp_path: Path) -> None:
    request = _request(tmp_path)
    with pytest.raises(NativeReadinessError):
        _prove(request, deps=None)  # type: ignore[arg-type]
    with pytest.raises(NativeReadinessError):
        _prove(
            request,
            deps=dataclasses.replace(
                _deps(request), listener_pid="not callable"  # type: ignore[arg-type]
            ),
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout": 0},
        {"timeout": -1},
        {"timeout": float("inf")},
        {"timeout": 61},
        {"timeout": 301},
        {"probe_timeout": 0},
        {"probe_timeout": 6},
        {"listener_attempts": 0},
        {"listener_attempts": 11},
        {"listener_attempts": True},
        {"listener_interval": -1},
        {"listener_interval": 301},
    ],
)
def test_policy_bounds_are_enforced(tmp_path: Path, kwargs: dict[str, object]) -> None:
    request = _request(tmp_path)
    with pytest.raises(NativeReadinessError):
        NativeReadinessPolicy(**kwargs)  # type: ignore[arg-type]
    with pytest.raises(NativeReadinessError):
        _prove(request, deps=_deps(request), policy=object())  # type: ignore[arg-type]


def test_policy_must_be_the_bound_type(tmp_path: Path) -> None:
    request = _request(tmp_path)
    with pytest.raises(NativeReadinessError):
        _prove(
            request,
            deps=_deps(request),
            policy=object(),  # type: ignore[arg-type]
        )


# ── listener before / after ─────────────────────────────────────────────────


def test_no_single_listener_before_the_request_is_refused(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)

    proof = _prove(
        request, deps=_deps(request, listener_values=[None, None, None])
    )

    assert proof.failure == "listener_missing"


def test_a_listener_replaced_across_the_request_is_refused(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)

    proof = _prove(
        request, deps=_deps(request, listener_values=[LISTENER, LISTENER + 1])
    )

    assert proof.failure == "listener_changed"


def test_listener_lookup_failure_is_internal(tmp_path: Path) -> None:
    request = _request(tmp_path)

    def broken(_port: int) -> int | None:
        raise RuntimeError("detail that must not escape")

    proof = _prove(
        request, deps=dataclasses.replace(_deps(request), listener_pid=broken)
    )

    assert proof.failure == "internal_error"
    assert "detail" not in repr(proof)


# ── startup-status schema and readiness ─────────────────────────────────────


@pytest.mark.parametrize(
    ("overrides", "failure"),
    [
        ({"version": "1.6.0"}, "version_mismatch"),
        ({"startup_id": STARTUP_ID.upper()}, "startup_id_invalid"),
        ({"pid": LISTENER + 1}, "startup_pid_mismatch"),
        ({"port_in_use": True}, "port_in_use"),
        ({"overall_ready": False}, "startup_not_ready"),
        ({"bind_status": "in_progress"}, "startup_not_ready"),
        ({"desktop_api_version": 2}, "startup_status_invalid"),
        ({"overall_ready": 1}, "startup_status_invalid"),
        ({"latest_version": 7}, "startup_status_invalid"),
        ({"pid": 0}, "startup_status_invalid"),
        ({"phases": "not-a-list"}, "startup_status_invalid"),
        (
            {
                "phases": [
                    {
                        "name": "server_starting",
                        "status": "done",
                        "message": "",
                        "started_at": None,
                        "finished_at": None,
                    },
                    {
                        "name": "server_starting",
                        "status": "done",
                        "message": "",
                        "started_at": None,
                        "finished_at": None,
                    },
                ]
            },
            "startup_status_invalid",
        ),
        (
            {
                "phases": [
                    {
                        "name": "server_starting",
                        "status": "unknown",
                        "message": "",
                        "started_at": None,
                        "finished_at": None,
                    }
                ]
            },
            "startup_status_invalid",
        ),
        (
            {
                "phases": [
                    {
                        "name": "server_starting",
                        "status": "done",
                        "message": 3,
                        "started_at": None,
                        "finished_at": None,
                    }
                ]
            },
            "startup_status_invalid",
        ),
        (
            {
                "phases": [
                    {
                        "name": "server_starting",
                        "status": "done",
                        "message": "",
                        "started_at": "x" * 65,
                        "finished_at": None,
                    }
                ]
            },
            "startup_status_invalid",
        ),
    ],
)
def test_startup_status_schema_failures_are_fixed(
    tmp_path: Path, overrides: dict[str, object], failure: str
) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)

    proof = _prove(
        request,
        deps=_deps(request, startup_status=lambda _p, _t: _startup_bytes(request, **overrides)),
    )

    assert proof.failure == failure


@pytest.mark.parametrize(
    ("raw", "failure"),
    [
        (b"", "startup_status_unavailable"),
        (b"{", "startup_status_invalid"),
        (b"[]", "startup_status_invalid"),
        (b'{"version": "1.7.0", "version": "1.7.0"}', "startup_status_invalid"),
        (b'{"latest_version": NaN}', "startup_status_invalid"),
        (b" " * (MAX_STARTUP_RESPONSE_BYTES + 1), "startup_status_invalid"),
    ],
    # Short ids: pytest puts the node id in an environment variable, and the
    # oversized body as an id passes Windows' 32767-character limit.
    ids=["empty", "truncated", "array", "duplicate-key", "nan", "oversized"],
)
def test_startup_status_bytes_are_strict_and_bounded(
    tmp_path: Path, raw: bytes, failure: str
) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)

    proof = _prove(
        request, deps=_deps(request, startup_status=lambda _p, _t: raw)
    )

    assert proof.failure == failure


def test_startup_status_failure_and_non_bytes_are_unavailable(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)

    def raises(_port: int, _timeout: float) -> bytes:
        raise OSError("connection refused")

    assert _prove(
        request, deps=_deps(request, startup_status=raises)
    ).failure == "startup_status_unavailable"
    assert _prove(
        request,
        deps=_deps(request, startup_status=lambda _p, _t: "not bytes"),  # type: ignore[arg-type]
    ).failure == "startup_status_unavailable"


def test_a_long_phase_message_is_still_a_ready_engine(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)
    phases = [
        {
            "name": "server_starting",
            "status": "done",
            "message": "x" * 2000,
            "started_at": None,
            "finished_at": None,
        }
    ]

    proof = _prove(
        request,
        deps=_deps(
            request,
            startup_status=lambda _p, _t: _startup_bytes(request, phases=phases),
        ),
    )

    assert proof.ready is True


def test_more_than_sixty_four_phases_is_invalid(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)
    phases = [
        {
            "name": f"phase-{index}",
            "status": "done",
            "message": "",
            "started_at": None,
            "finished_at": None,
        }
        for index in range(65)
    ]

    proof = _prove(
        request,
        deps=_deps(request, startup_status=lambda _p, _t: _startup_bytes(request, phases=phases)),
    )

    assert proof.failure == "startup_status_invalid"


# ── the private runtime lock ────────────────────────────────────────────────


def test_missing_lock_is_refused(tmp_path: Path) -> None:
    request = _request(tmp_path)
    request.workspace.mkdir(parents=True)

    assert _prove(request, deps=_deps(request)).failure == "lock_missing"


@pytest.mark.parametrize("mode", [0o644, 0o400, 0o660])
def test_lock_must_be_exactly_private(tmp_path: Path, mode: int) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace, mode=mode)

    assert _prove(request, deps=_deps(request)).failure == "lock_unsafe"


def test_lock_must_not_be_a_symlink(tmp_path: Path) -> None:
    request = _request(tmp_path)
    runtime = request.workspace / ".runtime"
    runtime.mkdir(parents=True)
    target = request.workspace / "elsewhere.json"
    target.write_bytes(_startup_bytes(request))
    target.chmod(0o600)
    (runtime / "server.lock").symlink_to(target)

    assert _prove(request, deps=_deps(request)).failure == "lock_unsafe"


def test_lock_must_have_one_link(tmp_path: Path) -> None:
    request = _request(tmp_path)
    lock = _write_lock(request.workspace)
    os.link(lock, request.workspace / "second-link")

    assert _prove(request, deps=_deps(request)).failure == "lock_unsafe"


def test_lock_is_read_with_a_bound(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace, raw=b"{" + b" " * MAX_LOCK_BYTES + b"}")

    assert _prove(request, deps=_deps(request)).failure == "lock_unsafe"


@pytest.mark.parametrize(
    ("kwargs", "failure"),
    [
        ({"raw": b"{not json"}, "lock_invalid"),
        ({"raw": b"[]"}, "lock_invalid"),
        ({"status": "stopped"}, "lock_invalid"),
        ({"startup_id": "short"}, "lock_invalid"),
        ({"pid": 0}, "lock_invalid"),
        ({"port": "8443"}, "lock_invalid"),
        ({"port": PORT + 1}, "lock_port_mismatch"),
        ({"workspace_value": "/somewhere/else"}, "lock_workspace_mismatch"),
        ({"runtime_value": "/somewhere/else/.runtime"}, "lock_runtime_mismatch"),
        ({"pid": LISTENER + 1}, "lock_pid_mismatch"),
        ({"startup_id": uuid.uuid4().hex}, "lock_startup_mismatch"),
    ],
)
def test_lock_fields_are_bound_to_the_observed_run(
    tmp_path: Path, kwargs: dict[str, object], failure: str
) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace, **kwargs)  # type: ignore[arg-type]

    assert _prove(request, deps=_deps(request)).failure == failure


# ── launchd job, ancestry and executable text ──────────────────────────────


def test_job_missing_or_is_the_listener_itself(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)

    assert _prove(
        request, deps=_deps(request, job=lambda _label: None)
    ).failure == "job_missing"
    assert _prove(
        request, deps=_deps(request, job=lambda _label: _host_job(request, pid=LISTENER))
    ).failure == "job_is_listener"


@pytest.mark.parametrize(
    ("job", "failure"),
    [
        (
            lambda request: LaunchdJob(
                pid=HOST_PID,
                arguments=(
                    str(
                        request.workspace
                        / "Other.app"
                        / "Contents"
                        / "MacOS"
                        / "Ciaobot Server"
                    ),
                    "serve",
                    "--python",
                    str(request.engine_python),
                ),
            ),
            "job_program_mismatch",
        ),
        (
            lambda request: LaunchdJob(
                pid=HOST_PID,
                arguments=(
                    str(_host_path(request)),
                    "serve",
                    "--python",
                    str(request.workspace / "python3.11"),
                ),
            ),
            "job_python_mismatch",
        ),
        (
            lambda request: LaunchdJob(
                pid=HOST_PID,
                arguments=(str(request.engine_python), "-I", "-m", "ciao.cli", "run"),
            ),
            "job_command_invalid",
        ),
        (
            lambda request: LaunchdJob(
                pid=HOST_PID,
                arguments=(str(_host_path(request)), "run-move", "--python", str(request.engine_python)),
            ),
            "job_command_invalid",
        ),
        (
            lambda request: LaunchdJob(
                pid=HOST_PID,
                arguments=(
                    str(_host_path(request)),
                    "run-move",
                    "--python",
                    str(request.engine_python),
                    "--operation",
                    "op",
                    "--state-dir",
                    str(request.workspace / "state"),
                ),
            ),
            "job_command_invalid",
        ),
        (
            lambda request: LaunchdJob(
                pid=HOST_PID,
                arguments=(str(_host_path(request)),) + ("x",) * 8,
            ),
            "job_command_invalid",
        ),
        (
            lambda request: LaunchdJob(
                pid=HOST_PID,
                arguments=(str(_host_path(request)), "serve", "--python", "relative"),
            ),
            "job_command_invalid",
        ),
        (
            lambda request: LaunchdJob(
                pid=HOST_PID,
                arguments=(str(_host_path(request)), "serve", "--python", "a\x00b"),
            ),
            "job_command_invalid",
        ),
    ],
)
def test_job_argv_is_strictly_the_owned_host_command(
    tmp_path: Path, job: object, failure: str
) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)

    proof = _prove(
        request, deps=_deps(request, job=lambda _label: job(request))  # type: ignore[operator]
    )

    assert proof.failure == failure


def test_job_lookup_failure_is_missing(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)

    def broken(_label: str) -> LaunchdJob:
        raise OSError("launchctl unavailable")

    assert _prove(
        request, deps=_deps(request, job=broken)
    ).failure == "job_missing"


@pytest.mark.parametrize(
    "chain",
    [
        None,
        [],
        (LISTENER + 1, HOST_PID),
        (LISTENER, HOST_PID, HOST_PID),
        (LISTENER, 2, 3, 4, 5, HOST_PID),
        [LISTENER, HOST_PID],
    ],
)
def test_ancestry_must_be_a_bounded_chain_from_the_listener(
    tmp_path: Path, chain: object
) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)

    proof = _prove(
        request, deps=_deps(request, chain=lambda _pid: chain)
    )

    assert proof.failure == "ancestry_unreadable"


@pytest.mark.parametrize("chain", [(LISTENER,), (LISTENER, 2, 3, 4, 5)])
def test_host_must_be_in_the_listener_ancestry(
    tmp_path: Path, chain: tuple[int, ...]
) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)

    assert _prove(
        request, deps=_deps(request, chain=lambda _pid: chain)
    ).failure == "host_not_in_ancestry"


def test_host_at_the_fourth_hop_is_accepted(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)

    proof = _prove(
        request, deps=_deps(request, chain=lambda _pid: (LISTENER, 2, 3, 4, HOST_PID))
    )

    assert proof.ready is True


def test_ancestry_lookup_failure_is_unreadable(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)

    def broken(_pid: int) -> tuple[int, ...]:
        raise OSError("ps unavailable")

    assert _prove(
        request, deps=_deps(request, chain=broken)
    ).failure == "ancestry_unreadable"


@pytest.mark.parametrize(
    ("path", "failure"),
    [
        (None, "executable_unreadable"),
        (123, "executable_unreadable"),
        ("relative", "executable_unreadable"),
        ("/some/other/host", "executable_mismatch"),
    ],
)
def test_executable_path_is_the_kernel_identity(
    tmp_path: Path, path: object, failure: str
) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)

    assert _prove(
        request, deps=_deps(request, executable_path=lambda _pid: path)
    ).failure == failure


# ── cancellation and time bounds ────────────────────────────────────────────


class _FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _SequenceClock:
    def __init__(self, values: list[float], default: float) -> None:
        self.values = list(values)
        self.default = default

    def __call__(self) -> float:
        return self.values.pop(0) if self.values else self.default


def test_cancellation_is_observed_during_the_listener_wait(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)
    state = {"cancelled": False}

    def sleep(_seconds: float) -> None:
        state["cancelled"] = True

    proof = _prove(
        request,
        deps=_deps(request, listener_values=[None, LISTENER], sleep=sleep),
        cancelled=lambda: state["cancelled"],
    )

    assert proof.failure == "cancelled"


def test_cancellation_before_the_request_skips_it(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)
    startup_calls: list[int] = []

    proof = _prove(
        request,
        deps=_deps(
            request,
            startup_status=lambda _p, _t: startup_calls.append(1) or b"{}",  # type: ignore[func-returns-value]
        ),
        cancelled=lambda: True,
    )

    assert proof.failure == "cancelled"
    assert startup_calls == []


def test_cancellation_predicate_failure_is_internal(tmp_path: Path) -> None:
    request = _request(tmp_path)

    def broken() -> bool:
        raise RuntimeError("no")

    assert _prove(
        request, deps=_deps(request), cancelled=broken
    ).failure == "internal_error"


def test_whole_proof_deadline_bounds_the_listener_wait(tmp_path: Path) -> None:
    request = _request(tmp_path)
    clock = _FakeClock()

    proof = _prove(
        request,
        deps=_deps(
            request,
            listener_values=[None, None, None],
            clock=clock,
            sleep=clock.advance,
        ),
        policy=NativeReadinessPolicy(
            timeout=10, listener_attempts=3, listener_interval=5
        ),
    )

    assert proof.failure == "timeout"


def test_probe_timeout_is_clamped_to_what_is_left(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)
    seen: list[float] = []
    clock = _SequenceClock([0.0, 9.5, 9.5, 9.5, 9.5, 9.5], 9.5)

    proof = _prove(
        request,
        deps=_deps(
            request,
            clock=clock,
            startup_status=lambda _port, timeout: seen.append(timeout)
            or _startup_bytes(request),
        ),
        policy=NativeReadinessPolicy(timeout=10, probe_timeout=2),
    )

    assert proof.ready is True
    assert seen == [0.5]


_SEAMS: tuple[str, ...] = ("startup_status", "job", "ancestor_pids", "executable_path")
_Answer = TypeVar("_Answer")


def _traced_deps(
    request: NativeReadinessRequest,
    *,
    seam: str,
    action: Callable[[], None],
    clock: object | None = None,
) -> tuple[NativeReadinessDeps, list[str]]:
    """The default doubles, recording every seam call in order.

    ``action`` runs inside ``seam`` before it answers, so a test can set the
    cancel flag or advance the clock at exactly that point.
    """
    base = _deps(request, clock=clock)
    trace: list[str] = []

    def record(name: str, answer: _Answer) -> _Answer:
        trace.append(name)
        if name == seam:
            action()
        return answer

    deps = dataclasses.replace(
        base,
        listener_pid=lambda port: record("listener_pid", base.listener_pid(port)),
        startup_status=lambda port, timeout: record(
            "startup_status", base.startup_status(port, timeout)
        ),
        job=lambda label: record("job", base.job(label)),
        ancestor_pids=lambda pid: record("ancestor_pids", base.ancestor_pids(pid)),
        executable_path=lambda pid: record(
            "executable_path", base.executable_path(pid)
        ),
    )
    return deps, trace


@pytest.mark.parametrize("seam", _SEAMS)
def test_cancellation_is_observed_at_every_seam(tmp_path: Path, seam: str) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)
    flag = {"cancelled": False}

    def cancel() -> None:
        flag["cancelled"] = True

    deps, trace = _traced_deps(request, seam=seam, action=cancel)
    proof = _prove(request, deps=deps, cancelled=lambda: flag["cancelled"])

    assert proof.failure == "cancelled"
    # The flag is observed before the next seam: nothing was called after it.
    assert trace[-1] == seam


@pytest.mark.parametrize("seam", _SEAMS)
def test_timeout_is_observed_at_every_seam(tmp_path: Path, seam: str) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)
    clock = _FakeClock()

    def pass_the_deadline() -> None:
        clock.advance(NativeReadinessPolicy().timeout + 1)

    deps, trace = _traced_deps(
        request, seam=seam, action=pass_the_deadline, clock=clock
    )
    proof = _prove(request, deps=deps)

    assert proof.failure == "timeout"
    assert trace[-1] == seam


def test_a_lock_rewritten_during_the_ancestry_check_is_refused(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)

    def rewrite_startup_id(_pid: int) -> tuple[int, ...]:
        _write_lock(request.workspace, startup_id="f" * 32)
        return (LISTENER, HOST_PID)

    proof = _prove(request, deps=_deps(request, chain=rewrite_startup_id))

    assert proof.failure == "lock_changed"


def test_a_lock_whose_pid_changes_during_the_ancestry_check_is_refused(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)

    def rewrite_pid(_pid: int) -> tuple[int, ...]:
        _write_lock(request.workspace, pid=HOST_PID)
        return (LISTENER, HOST_PID)

    proof = _prove(request, deps=_deps(request, chain=rewrite_pid))

    assert proof.failure == "lock_changed"


def test_a_lock_that_vanishes_before_the_final_reread_is_refused(tmp_path: Path) -> None:
    request = _request(tmp_path)
    lock = _write_lock(request.workspace)

    def remove_lock(_pid: int) -> tuple[int, ...]:
        lock.unlink()
        return (LISTENER, HOST_PID)

    proof = _prove(request, deps=_deps(request, chain=remove_lock))

    # The final re-read's lstat raises FileNotFoundError, which the reader maps
    # to lock_missing (a vanished lock is never treated as unsafe or as ready).
    assert proof.failure == "lock_missing"


def test_a_listener_that_changes_during_the_final_revalidation_is_refused(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)
    listener_calls: list[int] = []

    proof = _prove(
        request,
        deps=_deps(
            request,
            listener_values=[LISTENER, LISTENER, LISTENER + 1],
            listener_calls=listener_calls,
        ),
    )

    assert proof.failure == "listener_changed"
    # Before the request, after it, and the final revalidation.
    assert len(listener_calls) == 3


def test_a_utf16_startup_status_body_is_refused(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)
    body = json.dumps(_payload(request)).encode("utf-16")

    proof = _prove(request, deps=_deps(request, startup_status=lambda _p, _t: body))

    assert proof.failure == "startup_status_invalid"


def test_a_utf16_runtime_lock_is_refused(tmp_path: Path) -> None:
    request = _request(tmp_path)
    document = {
        "pid": LISTENER,
        "port": PORT,
        "workspace": str(request.workspace),
        "runtime": str(request.workspace / ".runtime"),
        "started_at": "2026-11-01T00:00:00Z",
        "status": "running",
        "startup_id": STARTUP_ID,
    }
    _write_lock(request.workspace, raw=json.dumps(document).encode("utf-16"))

    proof = _prove(request, deps=_deps(request))

    assert proof.failure == "lock_invalid"


def test_every_failure_is_a_fixed_code(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace, mode=0o644)

    proof = _prove(request, deps=_deps(request))

    assert proof.failure in FAILURE_CODES
    assert proof.failure == "lock_unsafe"
    assert str(tmp_path) not in repr(proof)


# ── pure parsers and the read-only adapters ─────────────────────────────────


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("4242\n", 4242),
        (" 4242 \n", 4242),
        ("4242\n4242\n", 4242),
        ("", None),
        ("4242\n4243\n", None),
        ("0\n", None),
        ("01\n", None),
        ("-1\n", None),
        ("abc\n", None),
    ],
)
def test_parse_listener_pid(stdout: str, expected: int | None) -> None:
    assert parse_listener_pid(stdout) == expected


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        ("1\n", 1),
        (" 42 \n", 42),
        ("", None),
        ("42\n43\n", None),
        ("x\n", None),
    ],
)
def test_parse_ps_ppid(stdout: str, expected: int | None) -> None:
    assert parse_ps_ppid(stdout) == expected


def _launchctl_text(request: NativeReadinessRequest, *, pid: int = HOST_PID) -> str:
    return (
        f"gui/501/{SERVER_LABEL} = {{\n"
        "\tactive count = 1\n"
        f"\tpid = {pid}\n"
        "\targuments = {\n"
        f"\t\t{_host_path(request)}\n"
        "\t\tserve\n"
        "\t\t--python\n"
        f"\t\t{request.engine_python}\n"
        "\t}\n"
        "}\n"
    )


def test_parse_launchd_job_reads_pid_and_multiline_argv(tmp_path: Path) -> None:
    request = _request(tmp_path)

    job = parse_launchd_job(_launchctl_text(request))

    assert job == LaunchdJob(
        pid=HOST_PID,
        arguments=(
            str(_host_path(request)),
            "serve",
            "--python",
            str(request.engine_python),
        ),
    )


@pytest.mark.parametrize(
    "text",
    [
        f"gui/501/{SERVER_LABEL} = {{\n\targuments = {{ /bin/true }}\n}}\n",
        f"gui/501/{SERVER_LABEL} = {{\n\tpid = 0\n\targuments = {{ /bin/true }}\n}}\n",
        f"gui/501/{SERVER_LABEL} = {{\n\tpid = not-a-pid\n\targuments = {{ /bin/true }}\n}}\n",
    ],
)
def test_parse_launchd_job_refuses_incomplete_reports(text: str) -> None:
    assert parse_launchd_job(text) is None


def _completed(
    argv: tuple[str, ...], stdout: str, returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout, "")


def test_adapters_use_fixed_argv_and_never_a_shell(tmp_path: Path) -> None:
    request = _request(tmp_path)
    seen: list[tuple[str, ...]] = []

    def runner(
        argv: tuple[str, ...], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        seen.append(tuple(argv))
        if argv[0] == readiness.LSOF and "-iTCP:8443" in argv:
            return _completed(argv, f"{LISTENER}\n")
        if argv[0] == readiness.LAUNCHCTL:
            return _completed(argv, _launchctl_text(request))
        if argv[0] == readiness.PS:
            pid = int(argv[-1])
            if pid == LISTENER:
                return _completed(argv, f"{HOST_PID}\n")
            if pid == HOST_PID:
                return _completed(argv, "1\n")
        raise AssertionError(f"unexpected argv: {argv!r}")

    deps = make_native_readiness_deps(
        runner=runner,
        http=lambda port, timeout: json.dumps(
            _payload(request)
        ).encode("utf-8"),
    )

    assert deps.listener_pid(PORT) == LISTENER
    assert deps.job(SERVER_LABEL) == _host_job(request)
    assert deps.ancestor_pids(LISTENER) == (LISTENER, HOST_PID)
    assert deps.startup_status(PORT, 1.5) == _startup_bytes(request)
    assert seen[0] == (
        readiness.LSOF,
        "-nP",
        "-iTCP:8443",
        "-sTCP:LISTEN",
        "-t",
    )
    assert seen[1][:2] == (readiness.LAUNCHCTL, "print")


def test_adapter_failures_and_oversized_output_are_none() -> None:
    def timeout(
        _argv: tuple[str, ...], _timeout: float
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("lsof", 5)

    deps = make_native_readiness_deps(runner=timeout)
    assert deps.listener_pid(PORT) is None
    assert deps.job(SERVER_LABEL) is None
    assert deps.ancestor_pids(LISTENER) is None

    def huge(
        argv: tuple[str, ...], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return _completed(argv, "x" * (readiness.MAX_ADAPTER_OUTPUT_BYTES + 1))

    deps = make_native_readiness_deps(runner=huge)
    assert deps.listener_pid(PORT) is None


def test_production_adapters_compose_with_the_proof(tmp_path: Path) -> None:
    request = _request(tmp_path)
    _write_lock(request.workspace)
    host = str(_host_path(request))

    def runner(
        argv: tuple[str, ...], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        if argv[0] == readiness.LSOF and "-iTCP:8443" in argv:
            return _completed(argv, f"{LISTENER}\n")
        if argv[0] == readiness.LAUNCHCTL:
            return _completed(argv, _launchctl_text(request))
        if argv[0] == readiness.PS:
            parent = HOST_PID if argv[-1] == str(LISTENER) else 1
            return _completed(argv, f"{parent}\n")
        raise AssertionError(f"unexpected argv: {argv!r}")

    deps = make_native_readiness_deps(
        runner=runner,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
        http=lambda _port, _timeout: _startup_bytes(request),
        executable_path=lambda _pid: host,
    )

    proof = _prove(request, deps=deps)

    assert proof.ready is True
    assert proof.listener_pid == LISTENER
    assert proof.host_pid == HOST_PID
    assert proof.startup_id == STARTUP_ID


def test_adapter_guards_reject_invalid_ids_without_running() -> None:
    calls: list[tuple[str, ...]] = []

    def runner(
        argv: tuple[str, ...], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(argv))
        raise AssertionError("a guarded adapter must not run")

    deps = make_native_readiness_deps(runner=runner)

    assert deps.listener_pid(0) is None
    assert deps.job("bad/label") is None
    assert deps.executable_path(0) is None
    assert calls == []
    assert readiness.http_startup_status(0, 1.0) is None
    assert readiness.http_startup_status(PORT, 0) is None
    assert readiness.http_startup_status(PORT, readiness.MAX_PROBE_TIMEOUT_SECONDS + 1) is None


def test_ancestor_walk_stops_at_init_and_rejects_cycles() -> None:
    def runner(
        argv: tuple[str, ...], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        pid = int(argv[-1])
        parent = {LISTENER: HOST_PID, HOST_PID: 1}[pid]
        return _completed(argv, f"{parent}\n")

    deps = make_native_readiness_deps(runner=runner)
    assert deps.ancestor_pids(LISTENER) == (LISTENER, HOST_PID)

    def cycle(
        argv: tuple[str, ...], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        pid = int(argv[-1])
        parent = {LISTENER: HOST_PID, HOST_PID: HOST_PID - 1, HOST_PID - 1: HOST_PID}[pid]
        return _completed(argv, f"{parent}\n")

    deps = make_native_readiness_deps(runner=cycle)
    assert deps.ancestor_pids(LISTENER) is None


def _serve_once(
    handler: object,
) -> tuple[int, threading.Thread]:
    """A local, isolated loopback server; no DNS and no external network."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = int(listener.getsockname()[1])

    def run() -> None:
        connection: socket.socket | None = None
        try:
            connection, _ = listener.accept()
            connection.settimeout(2.0)
            try:
                connection.recv(4096)
            except OSError:
                pass
            handler(connection)  # type: ignore[operator]
        except OSError:
            pass
        finally:
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass
            try:
                listener.close()
            except OSError:
                pass

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return port, thread


def test_http_adapter_reads_a_bounded_loopback_response() -> None:
    body = b'{"phases": []}'

    def handler(connection: socket.socket) -> None:
        connection.sendall(
            b"HTTP/1.1 200 OK\r\n"
            + f"Content-Length: {len(body)}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
            + body
        )

    port, thread = _serve_once(handler)
    try:
        assert readiness.http_startup_status(port, 0.5) == body
    finally:
        thread.join(timeout=2.0)


def test_http_adapter_refuses_redirects_without_following() -> None:
    redirected = threading.Event()
    target = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    target.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    target.bind(("127.0.0.1", 0))
    target.listen(1)
    target.settimeout(1.0)
    target_port = int(target.getsockname()[1])

    def watch_target() -> None:
        try:
            connection, _ = target.accept()
        except OSError:
            return
        redirected.set()
        try:
            connection.close()
        except OSError:
            pass

    watcher = threading.Thread(target=watch_target, daemon=True)
    watcher.start()

    def handler(connection: socket.socket) -> None:
        connection.sendall(
            b"HTTP/1.1 302 Found\r\n"
            + f"Location: http://127.0.0.1:{target_port}/never\r\n".encode("ascii")
            + b"Content-Length: 0\r\n"
            b"Connection: close\r\n\r\n"
        )

    port, thread = _serve_once(handler)
    try:
        assert readiness.http_startup_status(port, 0.5) is None
        watcher.join(timeout=1.5)
        assert redirected.is_set() is False
    finally:
        try:
            target.close()
        except OSError:
            pass
        thread.join(timeout=2.0)


def test_http_adapter_enforces_an_absolute_deadline_on_a_body_drip() -> None:
    stop = threading.Event()

    def handler(connection: socket.socket) -> None:
        # 200 bytes at 50 ms apiece is a ~10 s drip against a 0.5 s deadline. A
        # per-byte timeout would never fire here, so only the absolute deadline
        # stops it; the 4 s bound leaves slack for a loaded machine.
        connection.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: 200\r\n"
            b"Connection: close\r\n\r\n"
        )
        for _ in range(200):
            if stop.is_set():
                return
            try:
                connection.sendall(b"x")
            except OSError:
                return
            time.sleep(0.05)

    port, thread = _serve_once(handler)
    start = time.monotonic()
    try:
        assert readiness.http_startup_status(port, 0.5) is None
        assert time.monotonic() - start < 4.0
    finally:
        stop.set()
        thread.join(timeout=2.0)


def test_http_adapter_caps_headers_and_body() -> None:
    def huge_headers(connection: socket.socket) -> None:
        connection.sendall(
            b"HTTP/1.1 200 OK\r\nX-Pad: "
            + b"x" * (readiness.MAX_HTTP_HEADER_BYTES + 1)
            + b"\r\nContent-Length: 0\r\n\r\n"
        )

    port, thread = _serve_once(huge_headers)
    try:
        assert readiness.http_startup_status(port, 0.5) is None
    finally:
        thread.join(timeout=2.0)

    def huge_body(connection: socket.socket) -> None:
        connection.sendall(
            b"HTTP/1.1 200 OK\r\n"
            + f"Content-Length: {MAX_STARTUP_RESPONSE_BYTES + 1}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
        )

    port, thread = _serve_once(huge_body)
    try:
        assert readiness.http_startup_status(port, 0.5) is None
    finally:
        thread.join(timeout=2.0)


@pytest.mark.parametrize(
    "response",
    [
        b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
        (
            b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n"
            b"Content-Length: 1\r\n\r\nx"
        ),
    ],
)
def test_http_adapter_refuses_ambiguous_or_failed_answers(response: bytes) -> None:
    def handler(connection: socket.socket) -> None:
        connection.sendall(response)

    port, thread = _serve_once(handler)
    try:
        assert readiness.http_startup_status(port, 0.5) is None
    finally:
        thread.join(timeout=2.0)


@pytest.mark.skipif(sys.platform != "darwin", reason="libproc is macOS-only")
def test_proc_pidpath_is_the_authoritative_kernel_path() -> None:
    # A child running a known real binary: ``sys.executable`` can be a launcher
    # that execs another file (the python.org framework build execs Python.app),
    # so the kernel path of this process is not a fixed expectation.
    child = subprocess.Popen(
        ["/bin/sleep", "30"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        path = readiness._proc_pidpath(child.pid)
        assert path is not None
        assert os.path.samefile(path, "/bin/sleep")
    finally:
        child.kill()
        child.wait(timeout=10)
    assert readiness._proc_pidpath(0) is None


@pytest.mark.skipif(sys.platform != "darwin", reason="the bounded runner is macOS-only")
def test_bounded_run_caps_output_during_capture() -> None:
    start = time.monotonic()
    result = readiness._bounded_run(
        (sys.executable, "-c", "import sys; sys.stdout.write('x' * 70000)"),
        5.0,
    )
    assert result is None
    assert time.monotonic() - start < 5.0


@pytest.mark.skipif(sys.platform != "darwin", reason="the bounded runner is macOS-only")
def test_bounded_run_reaps_a_drip_on_the_absolute_deadline() -> None:
    start = time.monotonic()
    result = readiness._bounded_run(
        (sys.executable, "-c", "import time; time.sleep(5)"),
        0.2,
    )
    assert result is None
    assert time.monotonic() - start < 2.0


def test_ipv4_mapped_loopback_is_accepted() -> None:
    assert readiness._is_loopback_address("127.0.0.1") is True
    assert readiness._is_loopback_address("::1") is True
    assert readiness._is_loopback_address("::ffff:127.0.0.1") is True
    assert readiness._is_loopback_address("192.0.2.1") is False
    assert readiness._is_loopback_address("not-an-address") is False


