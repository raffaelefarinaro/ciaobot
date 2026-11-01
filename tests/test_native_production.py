"""The native coordinator's production wiring (#2340, #2404).

These tests drive the real CLI, the verified-uv tool directory lookup, the
engine install and receipt, the wizard argv and the production dependency
closures. Every external effect is a fake (a shell stub standing in for uv, a
recorder in place of a real step) or an isolated tmp path. Nothing reaches the
real launchd, the real home directory, the network or a real install.
"""

from __future__ import annotations

import dataclasses
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

import ciao
from ciao import install_receipt, native_handoff, native_lock, native_readiness
from ciao import native_installer as native
from ciao import server_host, server_host_install
from ciao.install_discovery import NativeInstallState
from ciao.native_installer import NativeCoordinatorError
from ciao.native_lock import NativeLockError, NativeRunLock
from ciao.native_readiness import NativeReadinessProof
from tests.test_native_installer import (
    RUN_ID,
    _available,
    _drain,
    _fresh_host_value,
    _handoff,
    _public_event_pipe,
    _record_bytes,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the native coordinator is macOS-only; its POSIX helpers are not on Windows",
)

ROOT = Path(__file__).resolve().parents[1]


def _outcome(terminal: str | None) -> native.NativeInstallOutcome:
    return native.NativeInstallOutcome(
        run_id=RUN_ID,
        terminal=terminal,
        failure_code=None if terminal == "service_ready" else "internal_error",
        host_step_attempted=False,
        host_step_completed=False,
        activation_attempted=False,
        activation_completed=False,
        selected_workspace=None,
        lock_retained=False,
    )


def _artifacts(tmp_path: Path) -> native.NativeInstallArtifacts:
    root = tmp_path / "artifacts"
    root.mkdir(mode=0o700)
    uv = root / "uv"
    uv.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    os.chmod(uv, 0o755)
    wheel = root / f"ciaobot-{ciao.__version__}-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    manifest = root / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    signature = root / "manifest.json.sig"
    signature.write_text("sig", encoding="utf-8")
    host_archive = root / "host.tar.gz"
    host_archive.write_bytes(b"archive")
    return native.NativeInstallArtifacts(
        version=ciao.__version__,
        uv=uv,
        wheel=wheel,
        manifest=manifest,
        signature=signature,
        host_archive=host_archive,
    )


def _script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    os.chmod(path, 0o755)
    return path


def _events_of(raw: bytes) -> list[dict[str, object]]:
    return [json.loads(line) for line in raw.splitlines()]


def _fake_uv(
    tmp_path: Path,
    *,
    rc: int = 0,
    shim: bool = True,
    python: bool = True,
    python_version: str | None = None,
    probe: tuple[Path, int] | None = None,
) -> Path:
    """A uv stub for ``tool install`` that writes the tool env and the shim.

    It logs its argv (one argument per line) to ``uv.log`` next to the stub.
    With ``probe``, both the uv stub and the tool interpreter record whether the
    given descriptor is open in them.
    """
    tools = tmp_path / "tools"
    bin_dir = tmp_path / "bin"
    stub = tmp_path / "python-stub"
    probe_line = ""
    if probe is not None:
        probe_path, fd = probe
        probe_line = (
            f'[ -e "/dev/fd/{fd}" ] && echo open >> "{probe_path}" '
            f'|| echo closed >> "{probe_path}"\n'
        )
    _script(
        stub,
        probe_line + f'printf "%s" "{python_version or ciao.__version__}"\n',
    )
    lines = []
    if probe is not None:
        probe_path, fd = probe
        lines.append(
            f'[ -e "/dev/fd/{fd}" ] && echo open >> "{probe_path}" '
            f'|| echo closed >> "{probe_path}"'
        )
    lines.append(
        f'for a in "$@"; do printf "%s\\n" "$a"; done >> "{tmp_path / "uv.log"}"'
    )
    lines.append(f"[ {rc} -eq 0 ] || exit {rc}")
    if python:
        lines += [
            f'mkdir -p "{tools / "ciaobot" / "bin"}" "{bin_dir}"',
            f'cp "{stub}" "{tools / "ciaobot" / "bin" / "python"}"',
            f'chmod 755 "{tools / "ciaobot" / "bin" / "python"}"',
        ]
    if shim:
        lines += [
            f'mkdir -p "{bin_dir}"',
            f'cp "{stub}" "{bin_dir / "ciao"}"',
            f'chmod 755 "{bin_dir / "ciao"}"',
        ]
    return _script(tmp_path / "uv", "\n".join(lines) + "\n")


# ── CLI contract ─────────────────────────────────────────────────────────────


def _cli_argv(artifacts: native.NativeInstallArtifacts, *, fd: str = "7") -> list[str]:
    return [
        "run",
        "--run-id",
        RUN_ID,
        "--events-fd",
        fd,
        "--first-seq",
        "4",
        "--prior-events",
        "3",
        "--version",
        artifacts.version,
        "--uv",
        str(artifacts.uv),
        "--wheel",
        str(artifacts.wheel),
        "--manifest",
        str(artifacts.manifest),
        "--signature",
        str(artifacts.signature),
        "--host-archive",
        str(artifacts.host_archive),
    ]


def test_main_parses_the_shell_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run(request, artifacts, *, cancellation=None):  # type: ignore[no-untyped-def]
        captured["request"] = request
        captured["artifacts"] = artifacts
        return _outcome("service_ready")

    monkeypatch.setattr(native, "run_native_install", fake_run)
    artifacts = _artifacts(tmp_path)
    code = native.main([*_cli_argv(artifacts), "--workspace", "/tmp/w"])
    assert code == 0
    assert captured["request"] == native.NativeInstallRequest(
        run_id=RUN_ID,
        events_fd=7,
        first_seq=4,
        prior_events=3,
        workspace=Path("/tmp/w"),
    )
    assert captured["artifacts"] == artifacts


@pytest.mark.parametrize(
    "case", ["no-sub", "missing-uv", "bad-fd", "bad-run-id"]
)
def test_main_rejects_bad_handover_arguments_without_writing(
    tmp_path: Path, case: str
) -> None:
    artifacts = _artifacts(tmp_path)
    with _public_event_pipe() as (read_fd, write_fd):
        argv = _cli_argv(artifacts, fd=str(write_fd))
        if case == "no-sub":
            argv = argv[1:]
            with pytest.raises(SystemExit) as caught:
                native.main(argv)
            assert caught.value.code == 2
        elif case == "missing-uv":
            index = argv.index("--uv")
            del argv[index : index + 2]
            with pytest.raises(SystemExit) as caught:
                native.main(argv)
            assert caught.value.code == 2
        elif case == "bad-fd":
            argv = _cli_argv(artifacts, fd="10")
            assert native.main(argv) == 2
        else:
            argv[argv.index("--run-id") + 1] = "NOT-A-UUID"
            assert native.main(argv) == 2
        assert _available(read_fd) == b""


def test_main_signal_handlers_only_cancel_and_are_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = signal.getsignal(signal.SIGTERM)
    seen: dict[str, bool] = {}

    def fake_run(request, artifacts, *, cancellation=None):  # type: ignore[no-untyped-def]
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        assert handler is not original
        handler(signal.SIGTERM, None)
        seen["cancelled"] = cancellation.cancelled
        return _outcome("failed")

    monkeypatch.setattr(native, "run_native_install", fake_run)
    code = native.main(_cli_argv(_artifacts(tmp_path)))
    assert code == 1
    assert seen == {"cancelled": True}
    assert signal.getsignal(signal.SIGTERM) is original


# ── artifacts, before any effect ─────────────────────────────────────────────


def _must_not_classify(workspace: object) -> NativeInstallState:
    raise AssertionError("classification ran before the artifacts were validated")


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("relative", "invalid_arguments"),
        ("missing", "invalid_arguments"),
        ("symlink", "invalid_arguments"),
        ("not-exec", "invalid_arguments"),
        ("bad-version", "invalid_arguments"),
        ("other-version", "verification_failed"),
    ],
)
def test_invalid_artifacts_fail_with_a_fixed_event_before_any_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, code: str
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    artifacts = _artifacts(tmp_path)
    if case == "relative":
        artifacts = dataclasses.replace(artifacts, wheel=Path("wheel.whl"))
    elif case == "missing":
        artifacts = dataclasses.replace(artifacts, manifest=tmp_path / "nope.json")
    elif case == "symlink":
        link = tmp_path / "link.json"
        link.symlink_to(artifacts.manifest)
        artifacts = dataclasses.replace(artifacts, manifest=link)
    elif case == "not-exec":
        os.chmod(artifacts.uv, 0o644)
    elif case == "bad-version":
        artifacts = dataclasses.replace(artifacts, version="bad version!")
    else:
        artifacts = dataclasses.replace(artifacts, version="0.0.0")
    deps = native._production_deps(
        native.NativeInstallRequest(run_id=RUN_ID, events_fd=3, first_seq=1, prior_events=0),
        artifacts,
        native.NativeCancellation(),
    )
    deps = dataclasses.replace(deps, classify=_must_not_classify)
    with _public_event_pipe() as (read_fd, write_fd):
        outcome = native._run_native_install(
            native.NativeInstallRequest(
                run_id=RUN_ID, events_fd=write_fd, first_seq=1, prior_events=0
            ),
            deps=deps,
        )
        events = _events_of(_drain(read_fd))
    assert outcome.failure_code == code
    assert [(event["type"], event.get("code")) for event in events] == [
        ("failed", code)
    ]


# ── tool directories from the verified uv ────────────────────────────────────


def test_tool_dirs_come_from_the_verified_uv_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uv = _script(
        tmp_path / "uv",
        'if [ "$3" = "--bin" ]; then printf "%s\\n" "$HOME/bin"; '
        'else printf "%s\\n" "$HOME/tools"; fi\n',
    )
    home = Path(os.environ["HOME"])
    assert native._tool_dirs(uv) == (home / "tools", home / "bin")


@pytest.mark.parametrize("case", ["rc", "relative", "two-lines", "hang"])
def test_unusable_tool_dirs_are_unknown_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    bodies = {
        "rc": "exit 3\n",
        "relative": 'printf "%s\\n" "relative/tools"\n',
        "two-lines": 'printf "%s\\n%s\\n" "$HOME/tools" "$HOME/other"\n',
        "hang": "exec sleep 60\n",
    }
    uv = _script(tmp_path / "uv", bodies[case])
    if case == "hang":
        monkeypatch.setattr(native, "TOOL_DIR_TIMEOUT_SECONDS", 0.2)
    with pytest.raises(NativeCoordinatorError) as caught:
        native._tool_dirs(uv)
    assert caught.value.failure_code == "unknown_state"
    assert caught.value.reason == "tool_dir_unknown"


# ── engine install ───────────────────────────────────────────────────────────


def test_engine_install_runs_uv_without_force_and_writes_the_one_receipt(
    tmp_path: Path,
) -> None:
    artifacts = _artifacts(tmp_path)
    uv = _fake_uv(tmp_path)
    artifacts = dataclasses.replace(artifacts, uv=uv)
    native._install_engine(
        artifacts, tmp_path / "tools", tmp_path / "bin", 30.0
    )
    argv = (tmp_path / "uv.log").read_text(encoding="utf-8").splitlines()
    assert argv == ["tool", "install", "--python", "3.13", str(artifacts.wheel)]
    assert "--force" not in argv
    receipt = install_receipt.read_receipt()
    assert receipt is not None
    assert receipt.version == artifacts.version
    assert receipt.executable == str(tmp_path / "bin" / "ciao")
    assert receipt.python == str(tmp_path / "tools" / "ciaobot" / "bin" / "python")
    assert receipt.service_backend == "launchd"
    assert receipt.service_label == "com.ciao.server"
    assert receipt.previous_version == ""
    assert receipt.previous_executable == ""
    assert receipt.uv == ""


@pytest.mark.parametrize(
    ("case", "kwargs"),
    [
        ("uv-rc", {"rc": 1}),
        ("no-shim", {"shim": False}),
        ("no-python", {"python": False}),
        ("wrong-version", {"python_version": "0.0.1"}),
    ],
)
def test_engine_install_failures_are_install_failed_and_leave_no_receipt(
    tmp_path: Path, case: str, kwargs: dict[str, object]
) -> None:
    artifacts = dataclasses.replace(_artifacts(tmp_path), uv=_fake_uv(tmp_path, **kwargs))  # type: ignore[arg-type]
    with pytest.raises(NativeCoordinatorError) as caught:
        native._install_engine(
            artifacts, tmp_path / "tools", tmp_path / "bin", 30.0
        )
    assert caught.value.failure_code == "install_failed"
    assert not install_receipt.default_receipt_path().exists()


def test_engine_install_children_never_see_the_public_descriptor(
    tmp_path: Path,
) -> None:
    probe = tmp_path / "probe.log"
    with _public_event_pipe() as (read_fd, write_fd):
        os.set_inheritable(write_fd, False)
        artifacts = dataclasses.replace(
            _artifacts(tmp_path),
            uv=_fake_uv(tmp_path, probe=(probe, write_fd)),
        )
        native._install_engine(
            artifacts, tmp_path / "tools", tmp_path / "bin", 30.0
        )
        assert _available(read_fd) == b""
    records = probe.read_text(encoding="utf-8").split()
    assert records  # the uv stub and the tool interpreter both ran
    assert "open" not in records


# ── wizard argv ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("case", "workspace", "expected", "tail"),
    [
        ("plain", None, None, ()),
        ("suggest", Path("/tmp/ws"), None, ("--installer-suggest-workspace=/tmp/ws",)),
        ("expected", None, Path("/tmp/ws"), ("--installer-native-workspace=/tmp/ws",)),
    ],
    ids=["plain", "suggest", "expected"],
)
def test_wizard_argv_is_the_hidden_native_bootstrap_command(
    case: str,
    workspace: Path | None,
    expected: Path | None,
    tail: tuple[str, ...],
) -> None:
    argv = native._wizard_argv(Path("/opt/bin"), RUN_ID, 5, workspace, expected)
    assert argv == (
        "/opt/bin/ciao",
        "run",
        "--installer-bootstrap",
        "--installer-run-id",
        RUN_ID,
        "--installer-native-fd=5",
        *tail,
    )


# ── production dependency closures ───────────────────────────────────────────


def test_production_deps_delegate_to_the_real_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = _artifacts(tmp_path)
    calls: dict[str, object] = {}
    sentinel = object()
    host = _fresh_host_value(tmp_path)
    handoff = _handoff(RUN_ID)
    scratch = tmp_path / "scratch"
    tool_dirs = (tmp_path / "tools", tmp_path / "bin")

    def record(name: str, value: object):  # type: ignore[no-untyped-def]
        def fake(*args, **kwargs):  # type: ignore[no-untyped-def]
            calls[name] = (args, kwargs)
            return value

        return fake

    monkeypatch.setattr(native, "_tool_dirs", lambda uv: tool_dirs)
    monkeypatch.setattr(
        native, "classify_native_install", record("classify", NativeInstallState.fresh)
    )
    monkeypatch.setattr(
        native_lock, "prepare_native_state_dir", record("prepare", sentinel)
    )
    monkeypatch.setattr(NativeRunLock, "acquire", staticmethod(record("acquire", sentinel)))
    monkeypatch.setattr(
        native_handoff, "prepare_bootstrap_scratch", record("scratch", scratch)
    )
    monkeypatch.setattr(native, "create_fresh_host", record("create", host))
    monkeypatch.setattr(native, "verify_owned_host", record("verify", host.ownership))
    monkeypatch.setattr(native, "_install_engine", record("engine", None))
    monkeypatch.setattr(native_handoff, "read_result", record("read", handoff))
    monkeypatch.setattr(native_handoff, "activate", record("activate", handoff))
    monkeypatch.setattr(
        native,
        "prove_native_readiness",
        record(
            "prove",
            NativeReadinessProof(ready=True, failure=None, port=handoff.port, version=artifacts.version),
        ),
    )
    monkeypatch.setattr(native, "make_native_readiness_deps", record("deps", sentinel))

    cancellation = native.NativeCancellation()
    deps = native._production_deps(
        native.NativeInstallRequest(
            run_id=RUN_ID, events_fd=3, first_seq=1, prior_events=0
        ),
        artifacts,
        cancellation,
    )
    assert deps.classify(None) is NativeInstallState.fresh
    state = deps.prepare_state_dir()
    assert state is sentinel
    assert calls["prepare"][0] == (install_receipt.default_receipt_path().parent,)  # type: ignore[index]
    lock = deps.acquire_lock(state, RUN_ID)  # type: ignore[arg-type]
    assert lock is sentinel
    assert deps.prepare_scratch() == scratch
    assert deps.create_host(lock, RUN_ID, 30.0) is host  # type: ignore[arg-type]
    _, create_kwargs = calls["create"]  # type: ignore[misc]
    assert create_kwargs["run_id"] == RUN_ID
    assert create_kwargs["manifest_bytes"] == artifacts.manifest.read_bytes()
    assert create_kwargs["archive_path"] == artifacts.host_archive
    deps.recheck_host(host)
    deps.install_engine(30.0)
    assert calls["engine"][0] == (artifacts, tool_dirs[0], tool_dirs[1], 30.0)  # type: ignore[index]
    argv = deps.wizard_command(5)
    assert argv[0] == str(tmp_path / "bin" / "ciao")
    assert deps.read_handoff(RUN_ID) is handoff
    assert deps.activate(RUN_ID, 30.0) is handoff
    assert calls["activate"][1] == {"run_id": RUN_ID}  # type: ignore[index]
    assert calls["read"][1] == {"expected_run_id": RUN_ID}  # type: ignore[index]
    proof = deps.prove_ready(host, handoff, 30.0)
    assert proof.ready is True
    (request,), prove_kwargs = calls["prove"]  # type: ignore[misc]
    assert request.version == artifacts.version
    assert request.port == handoff.port
    assert request.workspace == handoff.workspace
    assert request.host_bundle == host.bundle_path
    assert request.ownership_path == host.ownership_path
    assert request.engine_python == Path(handoff.engine_python)
    assert request.fresh_install_service_absent is True
    assert prove_kwargs["policy"].timeout <= native_readiness.MAX_TIMEOUT_SECONDS
    assert prove_kwargs["cancelled"]() is False
    cancellation.cancel()
    assert prove_kwargs["cancelled"]() is True


def test_recheck_refuses_a_host_that_changed_before_engine_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _fresh_host_value(tmp_path)
    changed = dataclasses.replace(host.ownership, executable_sha256="1" * 64)
    monkeypatch.setattr(native, "verify_owned_host", lambda *a, **k: changed)
    deps = native._production_deps(
        native.NativeInstallRequest(
            run_id=RUN_ID, events_fd=3, first_seq=1, prior_events=0
        ),
        _artifacts(tmp_path),
        native.NativeCancellation(),
    )
    with pytest.raises(server_host_install.ServerHostInstallError) as caught:
        deps.recheck_host(host)
    assert caught.value.code == server_host.NOT_OWNED


# ── lock diagnostic ──────────────────────────────────────────────────────────


def test_a_held_lock_reports_pid_liveness_on_stderr_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    os.chmod(state, 0o700)
    lock_dir = state / native_lock.LOCK_DIR_NAME
    lock_dir.mkdir(mode=0o700)
    record = lock_dir / native_lock.LOCK_RECORD_NAME
    record.write_bytes(_record_bytes(pid=dead.pid))
    os.chmod(record, 0o600)
    before = record.read_bytes()

    deps = native._production_deps(
        native.NativeInstallRequest(
            run_id=RUN_ID, events_fd=3, first_seq=1, prior_events=0
        ),
        _artifacts(tmp_path),
        native.NativeCancellation(),
    )
    deps = dataclasses.replace(
        deps,
        prepare_state_dir=lambda: native_lock.prepare_native_state_dir(
            state, trusted_root=tmp_path
        ),
    )
    handle = deps.prepare_state_dir()
    try:
        with pytest.raises(NativeLockError) as caught:
            deps.acquire_lock(handle, RUN_ID)
    finally:
        handle.close()
    assert caught.value.reason == "lock_busy"
    stderr = capsys.readouterr().err
    assert f"pid {dead.pid}, not running" in stderr
    assert str(tmp_path) not in stderr
    assert record.read_bytes() == before


# ── shell and readiness wiring facts ─────────────────────────────────────────


def test_python_version_matches_the_shell_installer() -> None:
    text = (ROOT / "scripts" / "install-engine.sh").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.startswith("PYTHON_VERSION=")]
    assert lines == [f"PYTHON_VERSION={native.ENGINE_PYTHON_VERSION}"]


@pytest.mark.skipif(
    sys.platform != "darwin" or not Path("/usr/sbin/lsof").exists(),
    reason="needs macOS lsof",
)
def test_real_lsof_adapter_names_the_owned_wizard_listener(
    tmp_path: Path,
) -> None:
    code = (
        "import socket, sys, time\n"
        "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen()\n"
        "print(s.getsockname()[1], flush=True)\n"
        "time.sleep(30)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        assert child.stdout is not None
        port = int(child.stdout.readline())
        owned = native._OwnedChild(
            child, term_grace=1, reap_timeout=1, poll_interval=0.05
        )
        deps = native._production_deps(
            native.NativeInstallRequest(
                run_id=RUN_ID, events_fd=3, first_seq=1, prior_events=0
            ),
            _artifacts(tmp_path),
            native.NativeCancellation(),
        )
        assert deps.wizard_listener(port, child.pid) == frozenset({child.pid})
        assert owned.owns_process(child.pid) is True

        def only_lsof(argv: tuple[str, ...], timeout: float):  # type: ignore[no-untyped-def]
            if argv[0] != "/usr/sbin/lsof":
                return None
            return native_readiness._bounded_run(argv, timeout)

        readiness = native_readiness.make_native_readiness_deps(runner=only_lsof)
        assert readiness.listener_pid(port) == child.pid
    finally:
        child.kill()
        child.wait()
