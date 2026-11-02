"""Native repair coordinator (#2485): ``python -I -m ciao.native_repair run``.

Real side effects are confined to ``tmp_path``; every launchd, host and engine
effect is an injected fake, so no test touches the real HOME, LaunchAgents,
``~/.local/state``, launchctl or the live engine.
"""

from __future__ import annotations

import dataclasses
import json
import os
import plistlib
import stat
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

import ciao
from ciao import install_receipt, macos_service, native_lock, server_host_install
from ciao import native_installer as native
from ciao import native_repair as repair
from ciao.native_installer import EventError, EventWriter, NativeCancellation
from ciao.native_lock import NativeRunLock
from ciao.native_readiness import NativeReadinessProof
from ciao.native_repair import (
    HostPlan,
    NativeRepairRequest,
    RepairTarget,
    _RepairDeps,
    _RepairPolicy,
)
from tests.test_native_installer import (
    OTHER_RUN_ID,
    RUN_ID,
    _available,
    _drain,
    _events,
    _ownership,
    _public_event_pipe,
)

VERSION = ciao.__version__
PORT = 9123
FAST = _RepairPolicy(readiness_interval=0.01, poll_interval=0.01)
MUTATIONS = frozenset(
    {
        "stop_service",
        "ensure_host",
        "reinstall_engine",
        "ensure_receipt",
        "render_definition",
        "write_definition",
        "bootstrap",
    }
)


# ── Event writer: repair mode ─────────────────────────────────────────────


def test_repair_writer_refuses_wizard_events() -> None:
    read_fd, write_fd = os.pipe()
    writer = EventWriter(write_fd, run_id=RUN_ID, repair=True)
    for call, args in (
        (writer.phase, ("wizard",)),
        (writer.wizard_ready, (1,)),
        (writer.setup_saved, ()),
    ):
        with pytest.raises(EventError) as caught:
            call(*args)
        assert caught.value.reason == "repair_has_no_wizard"
    with pytest.raises(EventError) as caught:
        writer.service_ready(9123)
    assert caught.value.reason == "readiness_not_started"
    writer.phase("install")
    writer.phase("activation")
    writer.phase("readiness")
    writer.service_ready(9123)
    writer.close()  # closes write_fd
    lines = _drain(read_fd).splitlines()
    os.close(read_fd)
    assert json.loads(lines[-1]) == {
        "schema": 1,
        "run_id": RUN_ID,
        "seq": 4,
        "type": "service_ready",
        "origin": "http://127.0.0.1:9123",
    }


def test_install_writer_still_requires_setup_saved() -> None:
    read_fd, write_fd = os.pipe()
    writer = EventWriter(write_fd, run_id=RUN_ID)
    writer.phase("readiness")
    with pytest.raises(EventError) as caught:
        writer.service_ready(9123)
    assert caught.value.reason == "setup_not_saved"
    writer.close()  # closes write_fd
    os.close(read_fd)


# ── Failure vocabulary and the tool-install argv ──────────────────────────


def test_not_installed_is_the_last_failure_code() -> None:
    assert native.FAILURE_CODES[-1] == "not_installed"
    assert native.FAILURE_CODES[:-1] == (
        "invalid_arguments",
        "existing_install",
        "partial_install",
        "unknown_state",
        "install_in_progress",
        "download_failed",
        "verification_failed",
        "install_failed",
        "wizard_failed",
        "activation_refused",
        "service_not_ready",
        "cancelled",
        "event_channel_failed",
        "internal_error",
    )


def test_install_engine_force_only_adds_the_reinstall_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifacts = native.NativeInstallArtifacts(
        version="1.3.0",
        uv=tmp_path / "uv",
        wheel=tmp_path / "ciaobot.whl",
        manifest=tmp_path / "manifest.json",
        signature=tmp_path / "manifest.sig",
        host_archive=tmp_path / "host.zip",
        uv_sha256="0" * 64,
        wheel_sha256="1" * 64,
    )
    argvs: list[tuple[str, ...]] = []

    def record(argv: tuple[str, ...], **_: object) -> tuple[int, bytes]:
        argvs.append(tuple(argv))
        raise native._ToolCallFailed()

    monkeypatch.setattr(native, "_run_bounded", record)
    cancellation = native.NativeCancellation()
    for force in (False, True):
        with pytest.raises(native.NativeCoordinatorError):
            native._install_engine(
                artifacts,
                tmp_path / "tools",
                tmp_path / "bin",
                60.0,
                cancellation=cancellation,
                env={},
                cwd=tmp_path,
                force=force,
            )
    default_argv, forced_argv = argvs
    assert forced_argv == (
        default_argv[: default_argv.index("install") + 1]
        + ("--force", "--reinstall")
        + default_argv[default_argv.index("install") + 1 :]
    )
    assert "--force" not in default_argv
    assert "--reinstall" not in default_argv


# ── Lifecycle helpers ─────────────────────────────────────────────────────


def _tree(root: Path) -> dict[str, bytes | str]:
    """Every entry under ``root`` by relative path: file bytes, link target or ``dir``."""
    snapshot: dict[str, bytes | str] = {}
    for current, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(current) / name
            key = path.relative_to(root).as_posix()
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                snapshot[key] = "link:" + os.readlink(path)
            elif stat.S_ISDIR(info.st_mode):
                snapshot[key] = "dir"
            elif stat.S_ISREG(info.st_mode):
                snapshot[key] = path.read_bytes()
            else:
                # A FIFO or device is recorded by its mode, never opened.
                snapshot[key] = f"special:{stat.S_IFMT(info.st_mode)}"
    return snapshot


def _workspace(tmp_path: Path) -> Path:
    return _make_workspace(tmp_path / "Ciaobot")


def _make_workspace(ws: Path) -> Path:
    """A set-up workspace: the registry, a .env with the port, a vault and a chat."""
    (ws / ".runtime").mkdir(parents=True)
    (ws / ".runtime" / "workspaces.json").write_text('{"workspaces": []}\n')
    (ws / ".env").write_text("PWA_PORT=9123\nCIAO_WORKSPACES=default\n")
    (ws / "vault").mkdir()
    (ws / "vault" / "note.md").write_text("note\n")
    (ws / "chats").mkdir()
    (ws / "chats" / "c.json").write_text("{}\n")
    return ws


def _target(tmp_path: Path, **overrides: object) -> RepairTarget:
    values: dict[str, object] = dict(
        workspace=tmp_path / "Ciaobot",
        port=PORT,
        definition=tmp_path / "LaunchAgents" / "com.ciao.server.plist",
        definition_present=True,
        service_path="/usr/bin:/bin",
        job_registered=True,
    )
    values.update(overrides)
    return RepairTarget(**values)  # type: ignore[arg-type]


def _deps(tmp_path: Path, **overrides: Callable[..., object]) -> tuple[_RepairDeps, list[str]]:
    """Recording fakes for every effect; the lock and state directory are real."""
    calls: list[str] = []

    def recording(name: str, result: Callable[..., object]) -> Callable[..., object]:
        def call(*args: object, **kwargs: object) -> object:
            calls.append(name)
            return result(*args, **kwargs)

        return call

    def ready(*_args: object) -> NativeReadinessProof:
        return NativeReadinessProof(ready=True, failure=None, port=PORT, version=VERSION)

    def write_definition(target: RepairTarget, rendered: bytes) -> str:
        import hashlib

        digest = hashlib.sha256(rendered).hexdigest()
        if target.definition.exists() and target.definition.read_bytes() == rendered:
            return digest
        target.definition.parent.mkdir(parents=True, exist_ok=True)
        target.definition.write_bytes(rendered)
        return digest

    state = tmp_path / "state"
    values: dict[str, Callable[..., object]] = {
        "validate_inputs": lambda: None,
        "classify": lambda: _target(tmp_path),
        "prepare_state_dir": lambda: native_lock.prepare_native_state_dir(
            state, trusted_root=tmp_path
        ),
        "acquire_lock": lambda st, run_id: NativeRunLock.acquire(st, run_id=run_id),
        "plan_host": lambda: HostPlan.current,
        "probe_engine": lambda: VERSION,
        "receipt_version": lambda: VERSION,
        "stop_service": lambda _remaining: None,
        "ensure_host": lambda _lock, _run_id, _plan: _ownership(tmp_path),
        "reinstall_engine": lambda _remaining: None,
        "ensure_receipt": lambda: None,
        "render_definition": lambda _target, _host: b"<plist/>",
        "write_definition": write_definition,
        "port_bindable": lambda _port: True,
        "bootstrap": lambda _definition, _digest: None,
        "prove_ready": ready,
    }
    values.update(overrides)
    names = {
        "validate_inputs",
        "classify",
        "plan_host",
        "probe_engine",
        "receipt_version",
        "stop_service",
        "ensure_host",
        "reinstall_engine",
        "ensure_receipt",
        "render_definition",
        "write_definition",
        "port_bindable",
        "bootstrap",
        "prove_ready",
    }
    wired: dict[str, Callable[..., object]] = {}
    for key, fake in values.items():
        wired[key] = recording(key, fake) if key in names else fake
    deps = _RepairDeps(**wired)  # type: ignore[arg-type]
    return deps, calls


def _run(
    deps: _RepairDeps,
    *,
    first_seq: int = 4,
    prior_events: int = 3,
    version: str = VERSION,
    policy: _RepairPolicy = FAST,
    cancellation: NativeCancellation | None = None,
) -> tuple[repair.NativeRepairOutcome, list[dict[str, object]]]:
    with _public_event_pipe() as (read_fd, events_fd):
        outcome = repair._run_native_repair(
            NativeRepairRequest(
                run_id=RUN_ID,
                events_fd=events_fd,
                first_seq=first_seq,
                prior_events=prior_events,
            ),
            deps=deps,
            version=version,
            cancellation=cancellation,
            policy=policy,
        )
        events = _events(read_fd)
    return outcome, events


def _phase_events(events: list[dict[str, object]]) -> list[tuple[str, object]]:
    """(type, phase-or-origin-or-code) per event, for compact assertions."""
    result: list[tuple[str, object]] = []
    for event in events:
        kind = str(event["type"])
        detail = event.get("phase", event.get("origin", event.get("code")))
        result.append((kind, detail))
    return result


def _lock_absent(tmp_path: Path) -> bool:
    return native_lock.inspect_native_lock(tmp_path / "state").state == "absent"


# ── Lifecycle ─────────────────────────────────────────────────────────────


def test_healthy_install_is_idempotent_and_ready(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    deps, calls = _deps(tmp_path)
    before = _tree(ws)
    for _ in range(2):
        calls.clear()
        outcome, events = _run(deps)
        assert _phase_events(events) == [
            ("phase", "install"),
            ("phase", "activation"),
            ("phase", "readiness"),
            ("service_ready", f"http://127.0.0.1:{PORT}"),
        ]
        assert [event["seq"] for event in events] == [4, 5, 6, 7]
        assert "reinstall_engine" not in calls
        assert calls.count("ensure_receipt") == 1
        assert calls.count("stop_service") == 1
        assert calls.count("bootstrap") == 1
        order = [calls.index(name) for name in (
            "stop_service",
            "ensure_host",
            "ensure_receipt",
            "write_definition",
            "bootstrap",
            "prove_ready",
        )]
        assert order == sorted(order)
        assert outcome.exit_code == 0
        assert _lock_absent(tmp_path)
        assert _tree(ws) == before


def test_broken_tool_env_is_reinstalled_then_ready_on_the_configured_port(
    tmp_path: Path,
) -> None:
    ws = _workspace(tmp_path)
    before = _tree(ws)
    deps, calls = _deps(
        tmp_path,
        probe_engine=lambda: None,
        receipt_version=lambda: VERSION,
    )
    outcome, events = _run(deps)
    assert calls.count("reinstall_engine") == 1
    assert "ensure_receipt" not in calls
    assert calls.index("stop_service") < calls.index("reinstall_engine")
    assert calls.index("reinstall_engine") < calls.index("bootstrap")
    assert events[-1]["type"] == "service_ready"
    assert events[-1]["origin"] == f"http://127.0.0.1:{PORT}"
    assert outcome.terminal == "service_ready"
    assert _tree(ws) == before


def test_missing_host_is_created(tmp_path: Path) -> None:
    seen: list[tuple[HostPlan, bool]] = []

    def ensure_host(lock: NativeRunLock, run_id: str, plan: HostPlan) -> object:
        lock.assert_held(run_id=RUN_ID)
        seen.append((plan, True))
        return _ownership(tmp_path)

    deps, _calls = _deps(tmp_path, plan_host=lambda: HostPlan.absent, ensure_host=ensure_host)
    outcome, _events_ = _run(deps)
    assert seen == [(HostPlan.absent, True)]
    assert outcome.terminal == "service_ready"


def test_wrong_host_is_replaced_after_the_service_stops(tmp_path: Path) -> None:
    seen: list[HostPlan] = []

    def ensure_host(_lock: NativeRunLock, _run_id: str, plan: HostPlan) -> object:
        seen.append(plan)
        return _ownership(tmp_path)

    deps, calls = _deps(tmp_path, plan_host=lambda: HostPlan.replace, ensure_host=ensure_host)
    outcome, _events_ = _run(deps)
    assert seen == [HostPlan.replace]
    assert calls.index("stop_service") < calls.index("ensure_host") < calls.index("bootstrap")
    assert outcome.terminal == "service_ready"


def test_no_registered_job_skips_the_bootout(tmp_path: Path) -> None:
    deps, calls = _deps(tmp_path, classify=lambda: _target(tmp_path, job_registered=False))
    outcome, _events_ = _run(deps)
    assert "stop_service" not in calls
    assert "bootstrap" in calls
    assert outcome.terminal == "service_ready"


@pytest.mark.parametrize("code", ["not_installed", "unknown_state", "install_in_progress"])
def test_refusals_change_nothing(tmp_path: Path, code: str) -> None:
    _workspace(tmp_path)
    def refuse() -> RepairTarget:
        raise repair._Refusal(code)

    deps, calls = _deps(tmp_path, classify=refuse)
    before = _tree(tmp_path)
    outcome, events = _run(deps)
    assert _phase_events(events) == [("failed", code)]
    assert MUTATIONS.isdisjoint(calls)
    assert outcome.mutation_attempted is False
    assert outcome.failure_code == code
    assert _tree(tmp_path) == before


def test_a_held_lock_is_install_in_progress_and_kept(tmp_path: Path) -> None:
    _workspace(tmp_path)
    state = native_lock.prepare_native_state_dir(tmp_path / "state", trusted_root=tmp_path)
    other = NativeRunLock.acquire(state, run_id=OTHER_RUN_ID)
    lock_dir = tmp_path / "state" / native_lock.LOCK_DIR_NAME
    record = (lock_dir / native_lock.LOCK_RECORD_NAME).read_bytes()
    deps, calls = _deps(tmp_path)
    try:
        outcome, events = _run(deps)
        assert (lock_dir / native_lock.LOCK_RECORD_NAME).read_bytes() == record
    finally:
        other.release()
    assert _phase_events(events) == [("failed", "install_in_progress")]
    assert MUTATIONS.isdisjoint(calls)
    assert outcome.failure_code == "install_in_progress"


def test_classification_changing_under_the_lock_is_unknown_state(tmp_path: Path) -> None:
    _workspace(tmp_path)
    answers = iter([_target(tmp_path), _target(tmp_path, port=9124)])
    deps, calls = _deps(tmp_path, classify=lambda: next(answers))
    outcome, events = _run(deps)
    assert _phase_events(events) == [("failed", "unknown_state")]
    assert MUTATIONS.isdisjoint(calls)
    assert outcome.mutation_attempted is False


def test_an_older_target_version_is_refused(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    before = _tree(ws)
    deps, calls = _deps(tmp_path, probe_engine=lambda: "1.2.0")
    outcome, events = _run(deps, version="1.0.0")
    assert _phase_events(events) == [("failed", "invalid_arguments")]
    assert MUTATIONS.isdisjoint(calls)
    assert _tree(ws) == before

    deps, calls = _deps(tmp_path, probe_engine=lambda: None, receipt_version=lambda: "1.2.0")
    outcome, events = _run(deps, version="1.0.0")
    assert _phase_events(events) == [("failed", "invalid_arguments")]
    assert MUTATIONS.isdisjoint(calls)
    assert _tree(ws) == before


def test_a_newer_target_version_reinstalls(tmp_path: Path) -> None:
    _workspace(tmp_path)
    deps, calls = _deps(tmp_path, probe_engine=lambda: "1.2.0")
    outcome, _events_ = _run(deps, version="1.3.0")
    assert calls.count("reinstall_engine") == 1
    assert outcome.terminal == "service_ready"


@pytest.mark.parametrize("installed", [None, "garbage"])
def test_an_unknown_installed_version_proceeds(tmp_path: Path, installed: str | None) -> None:
    _workspace(tmp_path)
    deps, calls = _deps(tmp_path, probe_engine=lambda: None, receipt_version=lambda: installed)
    outcome, _events_ = _run(deps)
    assert calls.count("reinstall_engine") == 1
    assert outcome.terminal == "service_ready"


def test_a_non_release_target_version_is_invalid_arguments(tmp_path: Path) -> None:
    _workspace(tmp_path)
    deps, calls = _deps(tmp_path)
    outcome, events = _run(deps, version="dev")
    assert _phase_events(events) == [("failed", "invalid_arguments")]
    assert MUTATIONS.isdisjoint(calls)
    assert outcome.mutation_attempted is False


def _raise_runtime(*_args: object) -> None:
    raise RuntimeError("injected failure")


@pytest.mark.parametrize(
    ("dep", "expected"),
    [
        ("stop_service", "install_failed"),
        ("ensure_host", "install_failed"),
        ("reinstall_engine", "install_failed"),
        ("write_definition", "install_failed"),
        ("bootstrap", "activation_refused"),
        ("prove_ready", "service_not_ready"),
    ],
)
def test_failures_midway_never_touch_the_workspace(
    tmp_path: Path, dep: str, expected: str
) -> None:
    ws = _workspace(tmp_path)
    before = _tree(ws)
    overrides: dict[str, Callable[..., object]] = {dep: _raise_runtime}
    if dep == "reinstall_engine":
        overrides["probe_engine"] = lambda: None
    deps, _calls = _deps(tmp_path, **overrides)
    outcome, events = _run(deps)
    terminals = [event for event in events if event["type"] in {"failed", "service_ready"}]
    assert [(event["type"], event.get("code")) for event in terminals] == [("failed", expected)]
    assert outcome.failure_code == expected
    assert _tree(ws) == before
    assert _lock_absent(tmp_path)


def test_cancellation_during_the_reinstall_is_cancelled(tmp_path: Path) -> None:
    _workspace(tmp_path)
    cancellation = NativeCancellation()

    def reinstall(_remaining: float) -> None:
        cancellation.cancel()

    deps, calls = _deps(
        tmp_path, probe_engine=lambda: None, reinstall_engine=reinstall
    )
    outcome, events = _run(deps, cancellation=cancellation)
    assert _phase_events(events)[-1] == ("failed", "cancelled")
    assert "bootstrap" not in calls
    assert outcome.failure_code == "cancelled"


def test_event_bounds(tmp_path: Path) -> None:
    _workspace(tmp_path)
    deps, _calls = _deps(tmp_path)
    with _public_event_pipe() as (read_fd, events_fd):
        outcome = repair._run_native_repair(
            NativeRepairRequest(run_id=RUN_ID, events_fd=events_fd, first_seq=29, prior_events=28),
            deps=deps,
            version=VERSION,
            policy=FAST,
        )
        lines = _drain(read_fd).splitlines()
    assert outcome.terminal == "service_ready"
    assert len(lines) == 4
    assert all(len(line) <= 1024 for line in lines)
    seqs = [json.loads(line)["seq"] for line in lines]
    assert seqs == [29, 30, 31, 32]
    assert seqs == sorted(set(seqs))

    deps, _calls = _deps(tmp_path)
    with _public_event_pipe() as (read_fd, events_fd):
        outcome = repair._run_native_repair(
            NativeRepairRequest(run_id=RUN_ID, events_fd=events_fd, first_seq=30, prior_events=29),
            deps=deps,
            version=VERSION,
            policy=FAST,
        )
        assert outcome.failure_code == "invalid_arguments"
        assert _available(read_fd) == b""


def test_foreign_listener_is_activation_refused(tmp_path: Path) -> None:
    _workspace(tmp_path)
    deps, calls = _deps(tmp_path, port_bindable=lambda _port: False)
    outcome, events = _run(
        deps, policy=_RepairPolicy(readiness_interval=0.01, poll_interval=0.01, port_bind_timeout=0.05)
    )
    assert _phase_events(events)[-1] == ("failed", "activation_refused")
    assert "bootstrap" not in calls
    assert outcome.failure_code == "activation_refused"


def test_repair_policy_rejects_a_non_positive_deadline() -> None:
    with pytest.raises(native.NativeCoordinatorError) as caught:
        _RepairPolicy(poll_interval=0)
    assert caught.value.failure_code == "internal_error"


def test_repair_protocol_and_budget_constants() -> None:
    assert repair.NATIVE_REPAIR_PROTOCOL == 1
    assert repair.REPAIR_EVENT_BUDGET == 4


# ── Classification (real filesystem, HOME isolated) ───────────────────────


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "platform", "darwin")
    return tmp_path


def _layout(home: Path) -> tuple[Path, Path]:
    return home / ".local" / "share" / "uv" / "tools", home / ".local" / "bin"


def _live_plist(home: Path) -> Path:
    return macos_service.live_launch_agents_dir() / f"{macos_service.SERVER_LABEL}.plist"


def _valid_plist(home: Path, workspace: str | None = None) -> dict[str, object]:
    tool_dir, _bin = _layout(home)
    ws = workspace or os.path.realpath(home / "Ciaobot")
    return {
        "Label": macos_service.SERVER_LABEL,
        "ProgramArguments": [str(tool_dir / "ciaobot" / "bin" / "python"), "-m", "ciao.cli", "run"],
        "EnvironmentVariables": {
            "CIAO_WORKSPACE": ws,
            "CIAO_RUNTIME_ROOT": ws + "/.runtime",
            "PATH": "/opt/x:/usr/bin",
        },
    }


def _write_plist(home: Path, data: dict[str, object]) -> None:
    path = _live_plist(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(data))


def _baseline(home: Path) -> None:
    """A repairable install: a set-up workspace, our LaunchAgent, our shim and our receipt."""
    tool_dir, bin_dir = _layout(home)
    ws = _make_workspace(home / "Ciaobot")
    _write_plist(home, _valid_plist(home, os.path.realpath(ws)))
    bin_dir.mkdir(parents=True, exist_ok=True)
    os.symlink(tool_dir / "ciaobot" / "bin" / "ciao", bin_dir / "ciao")
    install_receipt.write_receipt(
        install_receipt.InstallReceipt(
            version=VERSION,
            executable=str(bin_dir / "ciao"),
            python=str(tool_dir / "ciaobot" / "bin" / "python"),
            service_backend="launchd",
            service_label=macos_service.SERVER_LABEL,
            installed_at="2026-11-02T00:00:00+00:00",
        )
    )


def _classify(
    home: Path,
    *,
    environ: Mapping[str, str] | None = None,
    job_absent: Callable[[], bool] = lambda: True,
    override_disabled: Callable[[], bool] = lambda: False,
) -> RepairTarget:
    tool_dir, bin_dir = _layout(home)
    return repair._classify_repair(
        environ={} if environ is None else environ,
        tool_dir=tool_dir,
        bin_dir=bin_dir,
        job_absent=job_absent,
        override_disabled=override_disabled,
    )


def test_classify_reads_workspace_port_and_path_from_the_live_definition(home: Path) -> None:
    _baseline(home)
    elsewhere = _make_workspace(home / "Elsewhere")
    (elsewhere / ".env").write_text("PWA_PORT=9123\nCIAO_WORKSPACES=default\n")
    data = _valid_plist(home, os.path.realpath(elsewhere))
    _write_plist(home, data)
    before = _tree(home)
    target = _classify(home)
    assert target.workspace == Path(os.path.realpath(elsewhere))
    assert target.port == 9123
    assert target.service_path == "/opt/x:/usr/bin"
    assert target.definition_present is True
    assert target.job_registered is False
    assert _tree(home) == before


def test_classify_without_definition_uses_the_default_workspace(home: Path) -> None:
    ws = _make_workspace(home / "Ciaobot")
    (ws / ".env").write_text("CIAO_WORKSPACES=default\n")
    target = _classify(home)
    assert target.workspace == Path(os.path.realpath(ws))
    assert target.port == 8443
    assert target.definition_present is False


def test_classify_nothing_installed_is_not_installed(home: Path) -> None:
    with pytest.raises(repair._Refusal) as caught:
        _classify(home)
    assert caught.value.code == "not_installed"


def test_corrupt_receipt_is_accepted_for_repair(home: Path) -> None:
    _baseline(home)
    install_receipt.default_receipt_path().write_bytes(b"{not json")
    target = _classify(home)
    assert target.definition_present is True


def test_classify_busy_lock_is_install_in_progress(home: Path) -> None:
    _baseline(home)
    state = native_lock.prepare_native_state_dir(
        install_receipt.default_receipt_path().parent, trusted_root=home
    )
    held = NativeRunLock.acquire(state, run_id=OTHER_RUN_ID)
    try:
        with pytest.raises(repair._Refusal) as caught:
            _classify(home)
    finally:
        held.release()
    assert caught.value.code == "install_in_progress"


def _mut_symlink(home: Path) -> None:
    real = home / "elsewhere.plist"
    real.write_bytes(plistlib.dumps(_valid_plist(home)))
    _live_plist(home).unlink()
    os.symlink(real, _live_plist(home))


def _mut_label(home: Path) -> None:
    data = _valid_plist(home)
    data["Label"] = "com.other"
    _write_plist(home, data)


def _mut_developer_program(home: Path) -> None:
    data = _valid_plist(home)
    data["ProgramArguments"] = ["/Users/x/repo/.venv/bin/python", "-m", "ciao.cli", "run"]
    _write_plist(home, data)


def _mut_runtime_root(home: Path) -> None:
    data = _valid_plist(home)
    env = data["EnvironmentVariables"]
    assert isinstance(env, dict)
    env["CIAO_RUNTIME_ROOT"] = str(home / "other" / ".runtime")
    _write_plist(home, data)


def _mut_workspace_not_set_up(home: Path) -> None:
    _write_plist(home, _valid_plist(home, str(home / "Empty")))


def _mut_fifo_env(home: Path) -> None:
    env_file = home / "Ciaobot" / ".env"
    env_file.unlink()
    os.mkfifo(env_file)


def _mut_port_out_of_range(home: Path) -> None:
    (home / "Ciaobot" / ".env").write_text("PWA_PORT=99999\n")


def _mut_shim_regular_file(home: Path) -> None:
    _tool, bin_dir = _layout(home)
    (bin_dir / "ciao").unlink()
    (bin_dir / "ciao").write_text("#!/bin/sh\n")


def _mut_foreign_receipt(home: Path) -> None:
    receipt = install_receipt.read_receipt()
    assert receipt is not None
    install_receipt.write_receipt(dataclasses.replace(receipt, executable="/elsewhere/ciao"))


def _mut_upgrade_journal(home: Path) -> None:
    journal = server_host_install.upgrade_journal_path()
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("{}\n")


def _mut_migration(home: Path) -> None:
    marker = install_receipt.default_receipt_path().parent / "migration"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("pending\n")


@pytest.mark.parametrize(
    "mutate",
    [
        _mut_symlink,
        _mut_label,
        _mut_developer_program,
        _mut_runtime_root,
        _mut_workspace_not_set_up,
        _mut_fifo_env,
        _mut_port_out_of_range,
        _mut_shim_regular_file,
        _mut_foreign_receipt,
        _mut_upgrade_journal,
        _mut_migration,
    ],
    ids=lambda fn: fn.__name__,
)
def test_classify_unknown_state(home: Path, mutate: Callable[[Path], None]) -> None:
    _baseline(home)
    mutate(home)
    before = _tree(home)
    with pytest.raises(repair._Refusal) as caught:
        _classify(home)
    assert caught.value.code == "unknown_state"
    assert _tree(home) == before


def test_classify_registered_job_without_definition_is_unknown_state(home: Path) -> None:
    _baseline(home)
    _live_plist(home).unlink()
    with pytest.raises(repair._Refusal) as caught:
        _classify(home, job_absent=lambda: False)
    assert caught.value.code == "unknown_state"


def test_classify_disabled_override_is_unknown_state(home: Path) -> None:
    _baseline(home)
    with pytest.raises(repair._Refusal) as caught:
        _classify(home, override_disabled=lambda: True)
    assert caught.value.code == "unknown_state"


def test_classify_failing_launchd_probe_is_unknown_state(home: Path) -> None:
    _baseline(home)

    def boom() -> bool:
        raise RuntimeError("launchctl unavailable")

    with pytest.raises(repair._Refusal) as caught:
        _classify(home, job_absent=boom)
    assert caught.value.code == "unknown_state"


@pytest.mark.parametrize(
    "environ",
    [{"CIAO_CHAT_ID": "chat-1"}, {"CIAO_WORKSPACE": "/x"}, {"UV_TOOL_DIR": "/x"}],
)
def test_classify_refuses_a_redirected_or_in_engine_environment(
    home: Path, environ: dict[str, str]
) -> None:
    _baseline(home)
    with pytest.raises(repair._Refusal) as caught:
        _classify(home, environ=environ)
    assert caught.value.code == "unknown_state"


# ── Real side-effect steps ────────────────────────────────────────────────


def test_write_definition_skips_identical_bytes_and_replaces_different_ones(
    tmp_path: Path,
) -> None:
    import hashlib

    definition = tmp_path / "LaunchAgents" / "com.ciao.server.plist"
    definition.parent.mkdir()
    definition.write_bytes(b"<plist>same</plist>")
    target = _target(tmp_path, definition=definition, definition_present=True)
    before = os.lstat(definition)

    digest = repair._write_definition(target, b"<plist>same</plist>")
    after = os.lstat(definition)
    assert digest == hashlib.sha256(b"<plist>same</plist>").hexdigest()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)

    repair._write_definition(target, b"<plist>new</plist>")
    assert definition.read_bytes() == b"<plist>new</plist>"
    assert stat.S_IMODE(os.lstat(definition).st_mode) == 0o600
    assert sorted(p.name for p in definition.parent.iterdir()) == ["com.ciao.server.plist"]


def test_ensure_receipt_persists_uv_and_rewrites_only_on_change(home: Path) -> None:
    import hashlib

    tool_dir, bin_dir = _layout(home)
    payload = b"#!uv-bytes\n"
    source = home / "uv-src"
    source.write_bytes(payload)
    os.chmod(source, 0o700)
    private = native._PrivateRun()
    private.uv = source
    artifacts = native.NativeInstallArtifacts(
        version=VERSION,
        uv=source,
        wheel=home / "w.whl",
        manifest=home / "m.json",
        signature=home / "m.sig",
        host_archive=home / "h.zip",
        uv_sha256=hashlib.sha256(payload).hexdigest(),
        wheel_sha256="0" * 64,
    )
    repair._ensure_receipt(private, artifacts, tool_dir, bin_dir)
    receipt_path = install_receipt.default_receipt_path()
    first = receipt_path.read_bytes()
    written = install_receipt.read_receipt()
    assert written is not None
    assert written.uv == str(native.persisted_uv_path())

    repair._ensure_receipt(private, artifacts, tool_dir, bin_dir)
    assert receipt_path.read_bytes() == first

    stale = dataclasses.replace(
        written, version="0.0.1", installed_at="2020-01-01T00:00:00+00:00"
    )
    install_receipt.write_receipt(stale)
    repair._ensure_receipt(private, artifacts, tool_dir, bin_dir)
    rewritten = install_receipt.read_receipt()
    assert rewritten is not None
    assert rewritten.version == VERSION
    assert rewritten.installed_at == "2020-01-01T00:00:00+00:00"
