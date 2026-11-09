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
import signal
import stat
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

import ciao
from ciao import install_discovery, install_receipt, macos_service, native_lock, release_manifest, server_host, server_host_install
from ciao import native_installer as native
from ciao import native_repair as repair
from ciao.app_settings import DEFAULT_PWA_HOST
from ciao.native_installer import EventError, EventWriter, NativeCancellation
from ciao.native_lock import NativeRunLock
from ciao import native_readiness
from ciao.native_readiness import NativeReadinessProof
from ciao.server_host_install import ServerHostInstallError
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

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="native repair is a macOS path; Windows keeps the legacy installer",
)


@pytest.fixture(autouse=True)
def _as_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The coordinator refuses off macOS; Linux CI runs these tests as darwin.

    Every macOS boundary (launchctl, codesign, libproc) is faked per test, as in
    tests/test_native_installer.py, so only the platform gate needs forcing.
    """
    monkeypatch.setattr(sys, "platform", "darwin")


VERSION = ciao.__version__
PORT = 9123
INSTALL_PATH = "/opt/homebrew/bin:/Users/x/.local/bin:/usr/bin:/bin"
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
        "wizard_exited_before_bind",
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


def _tree(root: Path) -> dict[str, object]:
    """Every entry under ``root``: its kind and bytes (or link target), its mode and its mtime."""
    snapshot: dict[str, object] = {}
    for current, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(current) / name
            key = path.relative_to(root).as_posix()
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode):
                kind: object = ("link", os.readlink(path))
            elif stat.S_ISDIR(info.st_mode):
                kind = ("dir",)
            elif stat.S_ISREG(info.st_mode):
                kind = ("file", path.read_bytes())
            else:
                # A FIFO or device is recorded by its mode, never opened.
                kind = ("special", stat.S_IFMT(info.st_mode))
            snapshot[key] = (kind, stat.S_IMODE(info.st_mode), info.st_mtime_ns)
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
        host=DEFAULT_PWA_HOST,
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
        "classify": lambda _own=None: _target(tmp_path),
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
        "port_bindable": lambda _port, _host: True,
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
    deps, calls = _deps(tmp_path, classify=lambda _own=None: _target(tmp_path, job_registered=False))
    outcome, _events_ = _run(deps)
    assert "stop_service" not in calls
    assert "bootstrap" in calls
    assert outcome.terminal == "service_ready"


@pytest.mark.parametrize("code", ["not_installed", "unknown_state", "install_in_progress"])
def test_refusals_change_nothing(tmp_path: Path, code: str) -> None:
    _workspace(tmp_path)
    def refuse(_own: str | None = None) -> RepairTarget:
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
    deps, calls = _deps(tmp_path, classify=lambda _own=None: next(answers))
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
    ("dep", "expected", "never_after"),
    [
        ("stop_service", "install_failed",
         ["ensure_host", "reinstall_engine", "ensure_receipt", "render_definition", "write_definition", "bootstrap", "prove_ready"]),
        ("ensure_host", "install_failed",
         ["reinstall_engine", "ensure_receipt", "render_definition", "write_definition", "bootstrap", "prove_ready"]),
        ("reinstall_engine", "install_failed",
         ["ensure_receipt", "render_definition", "write_definition", "bootstrap", "prove_ready"]),
        ("ensure_receipt", "install_failed",
         ["render_definition", "write_definition", "bootstrap", "prove_ready"]),
        ("render_definition", "install_failed",
         ["write_definition", "bootstrap", "prove_ready"]),
        ("write_definition", "install_failed", ["bootstrap", "prove_ready"]),
        ("bootstrap", "activation_refused", ["prove_ready"]),
        ("prove_ready", "service_not_ready", []),
    ],
)
def test_failures_midway_never_touch_the_workspace(
    tmp_path: Path, dep: str, expected: str, never_after: list[str]
) -> None:
    ws = _workspace(tmp_path)
    before = _tree(ws)
    overrides: dict[str, Callable[..., object]] = {dep: _raise_runtime}
    if dep == "reinstall_engine":
        overrides["probe_engine"] = lambda: None
    deps, calls = _deps(tmp_path, **overrides)
    outcome, events = _run(deps)
    terminals = [event for event in events if event["type"] in {"failed", "service_ready"}]
    assert [(event["type"], event.get("code")) for event in terminals] == [("failed", expected)]
    assert outcome.failure_code == expected
    assert outcome.mutation_attempted is True
    assert dep in calls
    for later in never_after:
        assert later not in calls, later
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
    deps, calls = _deps(tmp_path, port_bindable=lambda _port, _host: False)
    outcome, events = _run(
        deps, policy=_RepairPolicy(readiness_interval=0.01, poll_interval=0.01, port_bind_timeout=0.05)
    )
    assert _phase_events(events)[-1] == ("failed", "activation_refused")
    assert "bootstrap" not in calls
    assert outcome.failure_code == "activation_refused"


def test_the_port_check_probes_the_configured_host(tmp_path: Path) -> None:
    _workspace(tmp_path)
    probes: list[tuple[int, str]] = []

    def bindable(port: int, host: str) -> bool:
        probes.append((port, host))
        return True

    deps, _calls = _deps(
        tmp_path,
        classify=lambda _own=None: _target(tmp_path, host="127.0.0.1"),
        port_bindable=bindable,
    )
    outcome, _events = _run(deps)
    assert outcome.terminal == "service_ready"
    assert probes == [(PORT, "127.0.0.1")]


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
    _write_receipt(home)


def _write_receipt(home: Path) -> None:
    tool_dir, bin_dir = _layout(home)
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
    own_run_id: str | None = None,
) -> RepairTarget:
    tool_dir, bin_dir = _layout(home)
    return repair._classify_repair(
        environ={} if environ is None else environ,
        tool_dir=tool_dir,
        bin_dir=bin_dir,
        job_absent=job_absent,
        override_disabled=override_disabled,
        own_run_id=own_run_id,
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


def test_classify_reads_the_configured_bind_host(home: Path) -> None:
    _baseline(home)
    assert _classify(home).host == DEFAULT_PWA_HOST

    (home / "Ciaobot" / ".runtime" / "app_settings.json").write_text(
        '{"pwa_host": "100.64.1.7"}\n'
    )
    target = _classify(home)
    assert target.host == "100.64.1.7"


def test_classify_without_definition_uses_the_default_workspace(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _make_workspace(home / "Ciaobot")
    (ws / ".env").write_text("CIAO_WORKSPACES=default\n")
    _write_receipt(home)  # native evidence; without it the install is not_installed (fix 3)
    monkeypatch.setattr("ciao.tool_path.login_shell_path", lambda: INSTALL_PATH)
    target = _classify(home)
    assert target.workspace == Path(os.path.realpath(ws))
    assert target.port == 8443
    assert target.service_path == INSTALL_PATH
    assert target.definition_present is False


def test_classify_without_a_definition_uses_the_users_install_path(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = _make_workspace(home / "Ciaobot")
    (ws / ".env").write_text("CIAO_WORKSPACES=default\n")
    _write_receipt(home)
    monkeypatch.setattr("ciao.tool_path.login_shell_path", lambda: INSTALL_PATH)
    target = _classify(home)
    assert target.service_path == INSTALL_PATH


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


# ── A reinstall keeps the receipt history it replaced ─────────────────────


def test_a_reinstall_keeps_the_receipt_installed_at_and_history(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool_dir, bin_dir = _layout(home)
    install_receipt.write_receipt(
        install_receipt.InstallReceipt(
            version=VERSION,
            executable=str(bin_dir / "ciao"),
            python=str(tool_dir / "ciaobot" / "bin" / "python"),
            service_backend="launchd",
            service_label=macos_service.SERVER_LABEL,
            installed_at="2026-01-02T03:04:05+00:00",
            previous_version="1.8.0",
            previous_executable="/old/ciao",
        )
    )
    previous = install_receipt.read_receipt()
    assert previous is not None
    sentinel = object()

    def fresh_install(*_args: object, **_kwargs: object) -> object:
        install_receipt.write_receipt(
            install_receipt.InstallReceipt(
                version="9.9.9",
                executable=str(bin_dir / "ciao"),
                python=str(tool_dir / "ciaobot" / "bin" / "python"),
                service_backend="launchd",
                service_label=macos_service.SERVER_LABEL,
                installed_at="2026-06-07T08:09:10+00:00",
                uv="/new/uv",
            )
        )
        return sentinel

    monkeypatch.setattr(native, "_install_engine", fresh_install)
    installed = repair._reinstall_engine_preserving_history(
        previous,
        _artifacts(tmp_path),
        tool_dir,
        bin_dir,
        30.0,
        cancellation=NativeCancellation(),
        env={},
        cwd=tmp_path,
    )
    assert installed is sentinel
    written = install_receipt.read_receipt()
    assert written is not None
    assert written.installed_at == "2026-01-02T03:04:05+00:00"
    assert written.previous_version == "1.8.0"
    assert written.previous_executable == "/old/ciao"
    assert written.version == "9.9.9"
    assert written.python == str(tool_dir / "ciaobot" / "bin" / "python")
    assert written.uv == "/new/uv"


def test_a_reinstall_with_no_previous_receipt_writes_the_fresh_one(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool_dir, bin_dir = _layout(home)
    sentinel = object()

    def fresh_install(*_args: object, **_kwargs: object) -> object:
        install_receipt.write_receipt(
            install_receipt.InstallReceipt(
                version="9.9.9",
                executable=str(bin_dir / "ciao"),
                python=str(tool_dir / "ciaobot" / "bin" / "python"),
                service_backend="launchd",
                service_label=macos_service.SERVER_LABEL,
                installed_at="2026-06-07T08:09:10+00:00",
            )
        )
        return sentinel

    monkeypatch.setattr(native, "_install_engine", fresh_install)
    installed = repair._reinstall_engine_preserving_history(
        None,
        _artifacts(tmp_path),
        tool_dir,
        bin_dir,
        30.0,
        cancellation=NativeCancellation(),
        env={},
        cwd=tmp_path,
    )
    assert installed is sentinel
    written = install_receipt.read_receipt()
    assert written is not None
    assert written.installed_at == "2026-06-07T08:09:10+00:00"
    assert written.previous_version == ""
    assert written.previous_executable == ""


def test_a_receipt_that_cannot_be_rewritten_is_install_failed(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_receipt(home)
    previous = install_receipt.read_receipt()
    assert previous is not None
    monkeypatch.setattr(native, "_install_engine", lambda *_a, **_k: object())

    def write_fails(_receipt: install_receipt.InstallReceipt) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(install_receipt, "write_receipt", write_fails)
    tool_dir, bin_dir = _layout(home)
    with pytest.raises(native.NativeCoordinatorError) as caught:
        repair._reinstall_engine_preserving_history(
            previous,
            _artifacts(tmp_path),
            tool_dir,
            bin_dir,
            30.0,
            cancellation=NativeCancellation(),
            env={},
            cwd=tmp_path,
        )
    assert caught.value.reason == "receipt_write_failed"
    assert caught.value.failure_code == "install_failed"


# ── Fix 1: the run's own lock is not a foreign lock ───────────────────────


def test_classify_own_held_lock_is_not_install_in_progress(home: Path) -> None:
    _baseline(home)
    state = native_lock.prepare_native_state_dir(
        install_receipt.default_receipt_path().parent, trusted_root=home
    )
    held = NativeRunLock.acquire(state, run_id=RUN_ID)
    try:
        target = _classify(home, own_run_id=RUN_ID)
    finally:
        held.release()
    assert target.port == 9123
    assert target.definition_present is True


def test_classify_foreign_held_lock_is_install_in_progress(home: Path) -> None:
    _baseline(home)
    state = native_lock.prepare_native_state_dir(
        install_receipt.default_receipt_path().parent, trusted_root=home
    )
    held = NativeRunLock.acquire(state, run_id=RUN_ID)
    try:
        with pytest.raises(repair._Refusal) as caught:
            _classify(home, own_run_id=OTHER_RUN_ID)
    finally:
        held.release()
    assert caught.value.code == "install_in_progress"


# ── Fix 3: native evidence is required ────────────────────────────────────


def test_classify_without_receipt_definition_or_host_record_is_not_installed(
    home: Path,
) -> None:
    tool_dir, bin_dir = _layout(home)
    _make_workspace(home / "Ciaobot")
    bin_dir.mkdir(parents=True)
    os.symlink(tool_dir / "ciaobot" / "bin" / "ciao", bin_dir / "ciao")
    with pytest.raises(repair._Refusal) as caught:
        _classify(home)
    assert caught.value.code == "not_installed"


# ── Fix 2: verify before the first stop ───────────────────────────────────


def _artifacts(tmp_path: Path) -> native.NativeInstallArtifacts:
    for name in ("manifest.json", "manifest.sig", "host.zip"):
        (tmp_path / name).write_bytes(b"x")
    return native.NativeInstallArtifacts(
        version=VERSION,
        uv=tmp_path / "uv",
        wheel=tmp_path / "w.whl",
        manifest=tmp_path / "manifest.json",
        signature=tmp_path / "manifest.sig",
        host_archive=tmp_path / "host.zip",
        uv_sha256="0" * 64,
        wheel_sha256="0" * 64,
    )


@pytest.fixture
def trusted_manifest(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Manifest and policy seams accept anything; the host functions are patched per test."""
    entry: dict[str, object] = {"name": "host"}
    monkeypatch.setattr(release_manifest, "verify_manifest", lambda *_a, **_k: {})
    monkeypatch.setattr(release_manifest, "select_server_host_artifact", lambda _m: entry)
    monkeypatch.setattr(server_host_install, "_entry_policy", lambda _e: object())
    return entry


def _archive_refused(*_args: object) -> None:
    raise ServerHostInstallError("bad archive", code=server_host_install.ARCHIVE_MISMATCH)


@pytest.mark.parametrize("case", ["absent", "replace"])
def test_plan_host_verifies_the_host_archive_when_it_would_install_or_replace(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trusted_manifest: dict[str, object],
    case: str,
) -> None:
    if case == "replace":
        record = server_host.default_ownership_path()
        record.parent.mkdir(parents=True)
        record.write_text("{}")
    monkeypatch.setattr(server_host_install, "verify_host_archive", _archive_refused)
    with pytest.raises(repair._Refusal) as caught:
        repair._plan_host(_artifacts(tmp_path))
    assert caught.value.code == "verification_failed"


def test_plan_host_verifies_a_current_host_before_any_mutation(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trusted_manifest: dict[str, object],
) -> None:
    bundle = server_host.default_bundle_path()
    bundle.mkdir(parents=True)
    owned = _ownership(tmp_path)
    archive_calls: list[object] = []
    owned_calls: list[object] = []
    monkeypatch.setattr(
        server_host_install,
        "_classify_host",
        lambda *_a, **_k: (server_host_install._CURRENT, owned),
    )
    monkeypatch.setattr(
        server_host_install,
        "verify_host_archive",
        lambda *a: archive_calls.append(a),
    )
    monkeypatch.setattr(
        server_host,
        "verify_owned_host",
        lambda *a, **k: owned_calls.append((a, k)) or owned,
    )
    plan, _entry, ownership = repair._plan_host(_artifacts(tmp_path))
    assert plan is HostPlan.current
    assert ownership is owned
    assert archive_calls == []
    assert len(owned_calls) == 1

    def refuse_owned(*_a: object, **_k: object) -> None:
        raise RuntimeError("unverifiable")

    monkeypatch.setattr(server_host, "verify_owned_host", refuse_owned)
    with pytest.raises(repair._Refusal) as caught:
        repair._plan_host(_artifacts(tmp_path))
    assert caught.value.code == "unknown_state"


def test_a_bad_host_archive_never_stops_the_service(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trusted_manifest: dict[str, object],
) -> None:
    _workspace(tmp_path)
    artifacts = _artifacts(tmp_path)
    monkeypatch.setattr(server_host_install, "verify_host_archive", _archive_refused)
    deps, calls = _deps(tmp_path, plan_host=lambda: repair._plan_host(artifacts)[0])
    before = _tree_outside_state(tmp_path)
    outcome, events = _run(deps)
    assert _phase_events(events) == [("failed", "verification_failed")]
    assert "stop_service" not in calls
    assert outcome.mutation_attempted is False
    assert _tree_outside_state(tmp_path) == before


def _tree_outside_state(root: Path) -> dict[str, bytes | str]:
    """The tree minus the run's own private state directory, which the run creates first."""
    return {key: value for key, value in _tree(root).items() if key != "state"}


# ── Fix 5: the service stop ───────────────────────────────────────────────


def test_stop_service_boots_out_then_returns_when_job_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter([False, False, True])
    bootouts: list[str] = []
    monkeypatch.setattr(macos_service, "bootout_agent", lambda label: bootouts.append(label))
    monkeypatch.setattr(macos_service, "server_job_absent", lambda: next(answers))
    monkeypatch.setattr(repair, "REPAIR_POLL_INTERVAL_SECONDS", 0.001)
    repair._stop_service(60.0, NativeCancellation())
    assert bootouts == [macos_service.SERVER_LABEL]


def test_stop_service_times_out_as_install_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(macos_service, "bootout_agent", lambda _label: None)
    monkeypatch.setattr(macos_service, "server_job_absent", lambda: False)
    monkeypatch.setattr(repair, "REPAIR_TEARDOWN_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(repair, "REPAIR_POLL_INTERVAL_SECONDS", 0.01)
    with pytest.raises(repair._Refusal) as caught:
        repair._stop_service(60.0, NativeCancellation())
    assert caught.value.code == "install_failed"


def test_stop_service_probe_error_is_install_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(macos_service, "bootout_agent", lambda _label: None)

    def probe() -> bool:
        raise RuntimeError("launchctl print failed")

    monkeypatch.setattr(macos_service, "server_job_absent", probe)
    with pytest.raises(repair._Refusal) as caught:
        repair._stop_service(60.0, NativeCancellation())
    assert caught.value.code == "install_failed"


def test_stop_service_cancellation_ends_the_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    cancellation = NativeCancellation()
    monkeypatch.setattr(macos_service, "bootout_agent", lambda _label: None)

    def stuck() -> bool:
        cancellation.cancel()  # a SIGTERM arrives while launchd is still tearing down
        return False

    monkeypatch.setattr(macos_service, "server_job_absent", stuck)
    monkeypatch.setattr(repair, "REPAIR_TEARDOWN_TIMEOUT_SECONDS", 30.0)
    started = time.monotonic()
    with pytest.raises(repair._Cancelled):
        repair._stop_service(60.0, cancellation)
    assert time.monotonic() - started < 5.0


# ── Fix 4: the plan and host glue ─────────────────────────────────────────


class _RecordingLock:
    """Records every assert_held in one shared log, so ordering against host calls is visible."""

    def __init__(self, log: list[str]) -> None:
        self.log = log

    def assert_held(self, *, run_id: str | None = None) -> None:
        assert run_id == RUN_ID
        self.log.append("assert")


@pytest.mark.parametrize(
    "case",
    [
        "absent",
        "orphan_record",
        "bundle_symlink",
        "current",
        "superseded",
        "host_exists",
        "policy_refused",
        "target_unsafe",
        "unexpected",
        "bad_signature",
    ],
)
def test_plan_host_decision_table(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    artifacts = _artifacts(tmp_path)
    bundle = server_host.default_bundle_path()
    record = server_host.default_ownership_path()
    owned = _ownership(tmp_path)
    monkeypatch.setattr(server_host_install, "verify_host_archive", lambda *_a: None)
    monkeypatch.setattr(server_host_install, "_entry_policy", lambda _e: object())
    monkeypatch.setattr(server_host, "verify_owned_host", lambda *_a, **_k: owned)
    if case != "bad_signature":
        monkeypatch.setattr(release_manifest, "verify_manifest", lambda *_a, **_k: {})
        monkeypatch.setattr(
            release_manifest, "select_server_host_artifact", lambda _m: {"name": "host"}
        )
    if case == "orphan_record":
        record.parent.mkdir(parents=True)
        record.write_text("{}")
    if case == "bundle_symlink":
        bundle.parent.mkdir(parents=True)
        os.symlink(tmp_path / "elsewhere", bundle)
    answers: dict[str, object] = {
        "current": server_host_install._CURRENT,
        "superseded": server_host_install._REPLACE,
    }
    codes: dict[str, str] = {
        "host_exists": server_host_install.HOST_EXISTS,
        "policy_refused": server_host_install.POLICY_REFUSED,
        "target_unsafe": server_host_install.TARGET_UNSAFE,
    }
    if case in answers or case in codes or case == "unexpected":
        bundle.mkdir(parents=True)

        def classify_host(*_a: object, **_k: object) -> tuple[str, None]:
            if case in answers:
                return answers[case], None  # type: ignore[return-value]
            if case in codes:
                raise ServerHostInstallError("refused", code=codes[case])
            raise RuntimeError("unexpected")

        monkeypatch.setattr(server_host_install, "_classify_host", classify_host)

    expected: dict[str, object] = {
        "absent": HostPlan.absent,
        "orphan_record": HostPlan.replace,
        "bundle_symlink": "unknown_state",
        "current": HostPlan.current,
        "superseded": HostPlan.replace,
        "host_exists": HostPlan.replace,
        "policy_refused": HostPlan.replace,
        "target_unsafe": "unknown_state",
        "unexpected": "unknown_state",
        "bad_signature": "verification_failed",
    }
    if case == "bad_signature":
        (tmp_path / "manifest.sig").write_text("not a signature")
    before = _tree(home)
    want = expected[case]
    if isinstance(want, HostPlan):
        plan, _entry, _owned = repair._plan_host(artifacts)
        assert plan is want
    else:
        with pytest.raises(repair._Refusal) as caught:
            repair._plan_host(artifacts)
        assert caught.value.code == want
    assert _tree(home) == before


def test_ensure_host_dispatches_per_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = _artifacts(tmp_path)
    entry: dict[str, object] = {"name": "host"}
    owned = _ownership(tmp_path)
    log: list[str] = []

    created: list[dict[str, object]] = []

    def fake_create(**kwargs: object) -> object:
        created.append(kwargs)
        return type("Fresh", (), {"ownership": owned})()

    monkeypatch.setattr(native, "create_fresh_host", fake_create)
    got = repair._ensure_host(_RecordingLock(log), RUN_ID, HostPlan.absent, entry, artifacts, None)
    assert got is owned
    assert created == [
        {
            "run_lock": created[0]["run_lock"],
            "run_id": RUN_ID,
            "manifest_bytes": b"x",
            "signature_text": "x",
            "archive_path": artifacts.host_archive,
        }
    ]

    replacement = _ownership(tmp_path / "other")

    def fake_repair(archive: Path, given: object) -> object:
        log.append("repair")
        assert archive == artifacts.host_archive
        assert given is entry
        return replacement

    monkeypatch.setattr(server_host_install, "repair_server_host", fake_repair)
    log.clear()
    got = repair._ensure_host(_RecordingLock(log), RUN_ID, HostPlan.replace, entry, artifacts, None)
    assert got is replacement
    assert log == ["assert", "repair", "assert"]

    def must_not_run(*_a: object, **_k: object) -> None:
        raise AssertionError("current must not re-verify or install")

    monkeypatch.setattr(server_host, "verify_owned_host", must_not_run)
    monkeypatch.setattr(native, "create_fresh_host", must_not_run)
    monkeypatch.setattr(server_host_install, "repair_server_host", must_not_run)
    log.clear()
    got = repair._ensure_host(_RecordingLock(log), RUN_ID, HostPlan.current, entry, artifacts, owned)
    assert got is owned
    assert log == []


def test_host_failure_maps_to_event_code(tmp_path: Path) -> None:
    _workspace(tmp_path)

    def conflict(*_a: object) -> object:
        raise ServerHostInstallError("conflict", code=server_host_install.TRANSACTION_CONFLICT)

    deps, _calls = _deps(tmp_path, ensure_host=conflict)
    outcome, events = _run(deps)
    assert _phase_events(events)[-1] == ("failed", "partial_install")
    assert outcome.failure_code == "partial_install"
    assert _lock_absent(tmp_path)


# ── Fix 5: the activation, probe, readiness and definition glue ───────────


def test_bootstrap_passes_the_digest_and_maps_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[Path, str]] = []
    status = [0]

    def load(definition: Path, *, expected_sha256: str) -> int:
        seen.append((definition, expected_sha256))
        return status[0]

    monkeypatch.setattr(macos_service, "load_fresh_agent", load)
    definition = tmp_path / "com.ciao.server.plist"
    repair._bootstrap(definition, "a" * 64)
    assert seen == [(definition, "a" * 64)]
    status[0] = 5
    with pytest.raises(repair._Refusal) as caught:
        repair._bootstrap(definition, "a" * 64)
    assert caught.value.code == "activation_refused"


@pytest.mark.parametrize(
    "case",
    ["missing_python", "interpreter_refused", "shim_refused", "tool_call_failed", "exit_1", "not_a_version"],
)
def test_probe_engine_reports_only_a_healthy_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    tool_dir, bin_dir = tmp_path / "tools", tmp_path / "bin"
    tool_python = tool_dir / "ciaobot" / "bin" / "python"
    if case != "missing_python":
        tool_python.parent.mkdir(parents=True)
        tool_python.write_text("#!/bin/sh\n")
        os.chmod(tool_python, 0o700)
    monkeypatch.setattr(native, "_interpreter_inside", lambda *_a: None)
    monkeypatch.setattr(native, "_shim_identity", lambda *_a: (None, None))
    if case == "interpreter_refused":

        def refuse(*_a: object) -> None:
            raise native.NativeCoordinatorError("engine_failed", "install_failed")

        monkeypatch.setattr(native, "_interpreter_inside", refuse)
    if case == "shim_refused":

        def refuse_shim(*_a: object) -> None:
            raise native.NativeCoordinatorError("shim_changed", "install_failed")

        monkeypatch.setattr(native, "_shim_identity", refuse_shim)
    argvs: list[tuple[str, ...]] = []

    def run_bounded(argv: tuple[str, ...], **_kwargs: object) -> tuple[int, bytes]:
        argvs.append(argv)
        if case == "tool_call_failed":
            raise native._ToolCallFailed()
        if case == "exit_1":
            return 1, b"1.8.0"
        if case == "not_a_version":
            return 0, b"not a version"
        return 0, b"1.8.0"

    monkeypatch.setattr(native, "_run_bounded", run_bounded)
    result = repair._probe_engine(
        tool_dir, bin_dir, env={}, cwd=tmp_path, cancellation=NativeCancellation()
    )
    assert result is None
    # Only the cases that get as far as the probe child run it at all.
    assert (argvs != []) is (case in {"tool_call_failed", "exit_1", "not_a_version"})


def test_probe_engine_healthy_version_and_argv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tool_dir, bin_dir = tmp_path / "tools", tmp_path / "bin"
    tool_python = tool_dir / "ciaobot" / "bin" / "python"
    tool_python.parent.mkdir(parents=True)
    tool_python.write_text("#!/bin/sh\n")
    os.chmod(tool_python, 0o700)
    monkeypatch.setattr(native, "_interpreter_inside", lambda *_a: None)
    monkeypatch.setattr(native, "_shim_identity", lambda *_a: (None, None))
    argvs: list[tuple[str, ...]] = []

    def run_bounded(argv: tuple[str, ...], **_kwargs: object) -> tuple[int, bytes]:
        argvs.append(argv)
        return 0, b"1.8.0"

    monkeypatch.setattr(native, "_run_bounded", run_bounded)
    result = repair._probe_engine(
        tool_dir, bin_dir, env={}, cwd=tmp_path, cancellation=NativeCancellation()
    )
    assert result == "1.8.0"
    assert argvs == [(str(tool_python), "-I", "-c", repair._PROBE_CODE)]


def test_probe_engine_child_cleanup_failure_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool_dir, bin_dir = tmp_path / "tools", tmp_path / "bin"
    tool_python = tool_dir / "ciaobot" / "bin" / "python"
    tool_python.parent.mkdir(parents=True)
    tool_python.write_text("#!/bin/sh\n")
    os.chmod(tool_python, 0o700)
    monkeypatch.setattr(native, "_interpreter_inside", lambda *_a: None)
    monkeypatch.setattr(native, "_shim_identity", lambda *_a: (None, None))

    def stuck(*_a: object, **_k: object) -> tuple[int, bytes]:
        raise native._ChildCleanupFailed()

    monkeypatch.setattr(native, "_run_bounded", stuck)
    with pytest.raises(native._ChildCleanupFailed):
        repair._probe_engine(tool_dir, bin_dir, env={}, cwd=tmp_path, cancellation=NativeCancellation())


def test_prove_ready_builds_the_request_from_the_target(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def prove(request: object, *, deps: object, policy: object, cancelled: object) -> object:
        captured.update(request=request, policy=policy, cancelled=cancelled)
        return NativeReadinessProof(ready=True, failure=None, port=PORT, version=VERSION)

    monkeypatch.setattr(native_readiness, "prove_native_readiness", prove)
    monkeypatch.setattr(native_readiness, "make_native_readiness_deps", lambda: "deps")
    host = _ownership(tmp_path)
    target = _target(tmp_path, workspace=tmp_path / "Ciaobot")
    tool_python = tmp_path / "tools" / "ciaobot" / "bin" / "python"
    cancellation = NativeCancellation()
    repair._prove_ready(
        target, host, 30.0, version=VERSION, tool_python=tool_python, cancellation=cancellation
    )
    request = captured["request"]
    assert request.version == VERSION
    assert request.port == target.port
    assert request.workspace == target.workspace
    assert request.host_bundle == Path(host.bundle_path)
    assert request.engine_python == tool_python
    assert request.fresh_install_service_absent is True
    assert request.ownership_path == server_host.default_ownership_path()
    assert captured["policy"].timeout == min(30.0, native_readiness.MAX_TIMEOUT_SECONDS)
    cancelled = captured["cancelled"]
    assert cancelled() is False
    cancellation.cancel()
    assert cancelled() is True


def test_render_then_write_is_idempotent_and_never_touches_the_workspace(
    home: Path, tmp_path: Path
) -> None:
    ws = _make_workspace(home / "Ciaobot")
    host = dataclasses.replace(
        _ownership(tmp_path), bundle_path=str(server_host.default_bundle_path())
    )
    tool_python = home / ".local" / "share" / "uv" / "tools" / "ciaobot" / "bin" / "python"
    target = _target(
        tmp_path,
        workspace=Path(os.path.realpath(ws)),
        port=PORT,
        host=DEFAULT_PWA_HOST,
        definition=home / "Library" / "LaunchAgents" / "com.ciao.server.plist",
        definition_present=False,
        service_path="/usr/bin:/bin",
    )
    workspace_before = _tree(ws)
    rendered = repair._render_definition(target, host, tool_python)
    data = plistlib.loads(rendered)
    assert data["Label"] == macos_service.SERVER_LABEL
    assert data["EnvironmentVariables"]["CIAO_WORKSPACE"] == os.path.realpath(ws)
    assert str(PORT) in str(data)
    assert str(tool_python) in data["ProgramArguments"]
    assert host.bundle_path in data["ProgramArguments"][0]
    digest = repair._write_definition(target, rendered)
    inode = os.lstat(target.definition).st_ino
    target2 = _target(
        tmp_path,
        workspace=target.workspace,
        port=PORT,
        host=DEFAULT_PWA_HOST,
        definition=target.definition,
        definition_present=True,
        service_path="/usr/bin:/bin",
    )
    again = repair._render_definition(target2, host, tool_python)
    assert repair._write_definition(target2, again) == digest
    assert os.lstat(target.definition).st_ino == inode
    assert _tree(ws) == workspace_before


def test_a_recreated_definition_carries_the_install_path(
    home: Path, tmp_path: Path
) -> None:
    ws = _make_workspace(home / "Ciaobot")
    host = dataclasses.replace(
        _ownership(tmp_path), bundle_path=str(server_host.default_bundle_path())
    )
    tool_python = home / ".local" / "share" / "uv" / "tools" / "ciaobot" / "bin" / "python"
    target = _target(
        tmp_path,
        workspace=Path(os.path.realpath(ws)),
        port=PORT,
        host=DEFAULT_PWA_HOST,
        definition=home / "Library" / "LaunchAgents" / "com.ciao.server.plist",
        definition_present=False,
        service_path=INSTALL_PATH,
    )
    rendered = repair._render_definition(target, host, tool_python)
    data = plistlib.loads(rendered)
    assert data["EnvironmentVariables"]["PATH"] == INSTALL_PATH


def test_write_definition_creates_missing_definition_at_0600_under_umask_0(tmp_path: Path) -> None:
    definition = tmp_path / "LaunchAgents" / "com.ciao.server.plist"
    target = _target(tmp_path, definition=definition, definition_present=False)
    previous = os.umask(0)
    try:
        repair._write_definition(target, b"<plist/>")
    finally:
        os.umask(previous)
    assert definition.read_bytes() == b"<plist/>"
    assert stat.S_IMODE(os.lstat(definition).st_mode) == 0o600


def test_write_definition_replaces_a_symlink_without_following_it(tmp_path: Path) -> None:
    definition = tmp_path / "LaunchAgents" / "com.ciao.server.plist"
    definition.parent.mkdir()
    decoy = tmp_path / "decoy.plist"
    decoy.write_bytes(b"decoy")
    os.symlink(decoy, definition)
    target = _target(tmp_path, definition=definition, definition_present=True)
    repair._write_definition(target, b"<plist>new</plist>")
    assert decoy.read_bytes() == b"decoy"
    assert not os.path.islink(definition)
    assert definition.read_bytes() == b"<plist>new</plist>"


def test_write_definition_failure_leaves_live_bytes_and_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    definition = tmp_path / "LaunchAgents" / "com.ciao.server.plist"
    definition.parent.mkdir()
    definition.write_bytes(b"<plist>live</plist>")
    target = _target(tmp_path, definition=definition, definition_present=True)

    def replace_fails(*_a: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(repair.os, "replace", replace_fails)
    with pytest.raises(native.NativeCoordinatorError) as caught:
        repair._write_definition(target, b"<plist>new</plist>")
    assert caught.value.failure_code == "install_failed"
    assert definition.read_bytes() == b"<plist>live</plist>"
    assert sorted(p.name for p in definition.parent.iterdir()) == ["com.ciao.server.plist"]


def _assert_definition_failure_probe(tmp_path: Path, failure: str) -> None:
    script = textwrap.dedent(
        """
        import errno
        import os
        import sys
        from pathlib import Path
        from unittest.mock import patch

        root = Path(sys.argv[1])
        failure = sys.argv[2]
        os.environ["CIAO_MEMORY_DIR"] = str(root / "memory")
        os.environ["CIAO_LAUNCH_AGENTS_DIR"] = str(root / "agents")
        sys.platform = "darwin"

        from ciao import native_repair as repair
        from ciao.native_installer import NativeCoordinatorError

        definition = root / "LaunchAgents" / "com.ciao.server.plist"
        definition.parent.mkdir()
        if failure != "interrupt":
            definition.write_bytes(b"<plist>live</plist>")
        target = repair.RepairTarget(
            root, 9123, "127.0.0.1", definition,
            failure != "interrupt", "", False,
        )
        captured = []
        fdopen_reached = []
        real_fchmod = repair.os.fchmod

        def fail_fchmod(fd, mode):
            if failure == "interrupt":
                captured.append(fd)
                raise KeyboardInterrupt
            if failure == "fchmod":
                captured.append(fd)
                raise OSError("injected fchmod failure")
            real_fchmod(fd, mode)

        def fail_fdopen(fd, *args, **kwargs):
            captured.append(fd)
            fdopen_reached.append(True)
            raise OSError("injected fdopen failure")

        try:
            with patch.object(repair.os, "fchmod", fail_fchmod):
                if failure == "fdopen":
                    with patch.object(repair.os, "fdopen", fail_fdopen):
                        try:
                            repair._write_definition(target, b"<plist>new</plist>")
                        except NativeCoordinatorError as exc:
                            assert exc.failure_code == "install_failed"
                        else:
                            raise AssertionError("fdopen failure was not mapped")
                    assert fdopen_reached
                elif failure == "interrupt":
                    try:
                        repair._write_definition(target, b"<plist/>")
                    except KeyboardInterrupt:
                        pass
                    else:
                        raise AssertionError("KeyboardInterrupt was not propagated")
                else:
                    try:
                        repair._write_definition(target, b"<plist>new</plist>")
                    except NativeCoordinatorError as exc:
                        assert exc.failure_code == "install_failed"
                    else:
                        raise AssertionError("fchmod failure was not mapped")

            assert captured
            try:
                os.fstat(captured[0])
            except OSError as exc:
                assert exc.errno == errno.EBADF
            else:
                raise AssertionError("descriptor remained open")

            if failure == "interrupt":
                assert not definition.exists()
                assert len(list(definition.parent.glob(f".{definition.name}.*.tmp"))) == 1
            else:
                assert definition.read_bytes() == b"<plist>live</plist>"
                assert sorted(path.name for path in definition.parent.iterdir()) == [definition.name]
        finally:
            for path in definition.parent.glob(f".{definition.name}.*.tmp"):
                path.unlink()
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), failure],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"child failed:\n{result.stdout}{result.stderr}"


def test_write_definition_fchmod_failure_closes_fd(tmp_path: Path) -> None:
    _assert_definition_failure_probe(tmp_path, "fchmod")


def test_write_definition_fdopen_failure_closes_fd(tmp_path: Path) -> None:
    _assert_definition_failure_probe(tmp_path, "fdopen")


def test_write_definition_interrupted_before_fdopen_closes_fd(tmp_path: Path) -> None:
    _assert_definition_failure_probe(tmp_path, "interrupt")


def test_reinstall_engine_wiring_passes_force_and_retains_private_on_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = _artifacts(tmp_path)
    monkeypatch.setattr(native, "_scrubbed_env", lambda **_k: {})
    deps, private = repair._repair_production_deps(artifacts, NativeCancellation())
    tool_dir, bin_dir = tmp_path / "tools", tmp_path / "bin"
    monkeypatch.setattr(native, "_tool_dirs", lambda *_a, **_k: (tool_dir, bin_dir))
    private.uv = tmp_path / "staged-uv"
    private.wheel = tmp_path / "staged-wheel"
    private.cwd = tmp_path
    calls: list[dict[str, object]] = []

    def install(staged: object, tdir: Path, bdir: Path, remaining: float, **kwargs: object) -> object:
        calls.append({"staged": staged, "tool_dir": tdir, "bin_dir": bdir, **kwargs})
        return object()

    monkeypatch.setattr(native, "_install_engine", install)
    deps.reinstall_engine(30.0)
    assert len(calls) == 1
    assert calls[0]["staged"].uv == private.uv
    assert calls[0]["staged"].wheel == private.wheel
    assert calls[0]["tool_dir"] == tool_dir
    assert calls[0]["force"] is True
    assert private.retained is False

    def stuck(*_a: object, **_k: object) -> object:
        raise native._ChildCleanupFailed()

    monkeypatch.setattr(native, "_install_engine", stuck)
    with pytest.raises(native._ChildCleanupFailed):
        deps.reinstall_engine(30.0)
    assert private.retained is True


def test_run_native_repair_removes_the_private_run_on_return_and_on_raise(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = _artifacts(tmp_path)
    request = NativeRepairRequest(run_id=RUN_ID, events_fd=3, first_seq=4, prior_events=3)
    removed: list[str] = []

    class Private:
        def remove(self) -> None:
            removed.append("removed")

    monkeypatch.setattr(repair, "_repair_production_deps", lambda *_a: (object(), Private()))
    outcome = repair.NativeRepairOutcome(
        run_id=RUN_ID, terminal="failed", failure_code="cancelled", mutation_attempted=False,
        activation_attempted=False, activation_completed=False, lock_retained=False,
        workspace=None, port=None,
    )
    monkeypatch.setattr(repair, "_run_native_repair", lambda *_a, **_k: outcome)
    assert repair.run_native_repair(request, artifacts) is outcome
    assert removed == ["removed"]

    def boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(repair, "_run_native_repair", boom)
    with pytest.raises(RuntimeError):
        repair.run_native_repair(request, artifacts)
    assert removed == ["removed", "removed"]


def test_run_native_repair_rejects_bad_request_before_staging(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = _artifacts(tmp_path)
    staged: list[object] = []
    monkeypatch.setattr(repair, "_repair_production_deps", lambda *a: staged.append(a))
    bad = NativeRepairRequest(run_id="not-a-uuid", events_fd=3, first_seq=4, prior_events=3)
    with pytest.raises(native.NativeCoordinatorError):
        repair.run_native_repair(bad, artifacts)
    assert staged == []


# ── Fix 6: end to end with the real classifier and lock ───────────────────


def test_run_with_real_lock_and_real_classify_reaches_ready(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dataclasses as _dc
    import hashlib

    # The real classifier refuses a redirected or in-chat environment; this
    # process may run inside a chat, so the run starts from a clean one.
    for key in (*install_discovery._REDIRECT_VARIABLES, *repair._REFUSED_ENVIRONMENT):
        monkeypatch.delenv(key, raising=False)
    _baseline(home)
    ws = home / "Ciaobot"
    tool_dir, bin_dir = _layout(home)
    before_tree = _tree(ws)
    state = install_receipt.default_receipt_path().parent
    artifacts = _artifacts(tmp_path)
    artifacts.uv.write_bytes(b"#!uv\n")
    os.chmod(artifacts.uv, 0o700)
    artifacts.wheel.write_bytes(b"wheel")
    artifacts = _dc.replace(
        artifacts,
        version=VERSION,
        uv_sha256=hashlib.sha256(b"#!uv\n").hexdigest(),
        wheel_sha256=hashlib.sha256(b"wheel").hexdigest(),
    )
    owned = _dc.replace(_ownership(tmp_path), bundle_path=str(server_host.default_bundle_path()))
    calls: list[str] = []
    booted = [False]
    mkdtemp_paths: list[str] = []
    real_mkdtemp = native.tempfile.mkdtemp

    def record_mkdtemp(*a: object, **k: object) -> str:
        path = real_mkdtemp(*a, **k)  # type: ignore[arg-type]
        mkdtemp_paths.append(path)
        return path

    def bootout(_label: str) -> None:
        calls.append("bootout")
        booted[0] = True

    def load(definition: Path, *, expected_sha256: str) -> int:
        calls.append("bootstrap")
        assert definition == _live_plist(home)
        return 0

    def prove(*_a: object, **_k: object) -> NativeReadinessProof:
        calls.append("readiness")
        return NativeReadinessProof(ready=True, failure=None, port=PORT, version=VERSION)

    installs: list[dict[str, object]] = []

    def install_engine(staged: object, tdir: Path, bdir: Path, remaining: float, **kwargs: object) -> object:
        calls.append("install_engine")
        installs.append(kwargs)
        return object()

    real_write = repair._write_definition

    def write(target: RepairTarget, rendered: bytes) -> str:
        calls.append("write")
        return real_write(target, rendered)

    monkeypatch.setattr(native.tempfile, "mkdtemp", record_mkdtemp)
    monkeypatch.setattr(native, "_tool_dirs", lambda *_a, **_k: (tool_dir, bin_dir))
    monkeypatch.setattr(native, "_scrubbed_env", lambda **_k: {})
    monkeypatch.setattr(native, "_install_engine", install_engine)
    monkeypatch.setattr(native, "_service_port_bindable", lambda _port, _host: True)
    monkeypatch.setattr(native, "create_fresh_host", lambda **_k: type("F", (), {"ownership": owned})())
    monkeypatch.setattr(server_host_install, "verify_host_archive", lambda *_a: None)
    monkeypatch.setattr(release_manifest, "verify_manifest", lambda *_a, **_k: {})
    monkeypatch.setattr(release_manifest, "select_server_host_artifact", lambda _m: {"name": "host"})
    monkeypatch.setattr(macos_service, "bootout_agent", bootout)
    monkeypatch.setattr(macos_service, "server_job_absent", lambda: booted[0])
    monkeypatch.setattr(macos_service, "launchd_override_disabled", lambda: False)
    monkeypatch.setattr(macos_service, "load_fresh_agent", load)
    monkeypatch.setattr(native_readiness, "make_native_readiness_deps", lambda: None)
    monkeypatch.setattr(native_readiness, "prove_native_readiness", prove)
    monkeypatch.setattr(repair, "_write_definition", write)
    monkeypatch.setattr(repair, "REPAIR_POLL_INTERVAL_SECONDS", 0.001)

    request_run = NativeRepairRequest(run_id=RUN_ID, events_fd=0, first_seq=4, prior_events=3)
    with _public_event_pipe() as (read_fd, events_fd):
        request = _dc.replace(request_run, events_fd=events_fd)
        outcome = repair.run_native_repair(request, artifacts)
        events = _events(read_fd)

    assert outcome.failure_code is None, outcome.failure_code
    assert outcome.terminal == "service_ready"
    assert outcome.exit_code == 0
    assert _phase_events(events) == [
        ("phase", "install"),
        ("phase", "activation"),
        ("phase", "readiness"),
        ("service_ready", f"http://127.0.0.1:{PORT}"),
    ]
    assert calls == [
        "bootout",
        "install_engine",
        "write",
        "bootstrap",
        "readiness",
    ]
    assert installs[0].get("force") is True
    assert native_lock.inspect_native_lock(state).state == "absent"
    assert all(not os.path.exists(path) for path in mkdtemp_paths)
    assert _tree(ws) == before_tree


# ── Fix 6: the CLI contract ───────────────────────────────────────────────


_CLI_ARGV = [
    "run",
    "--run-id", RUN_ID,
    "--events-fd", "3",
    "--first-seq", "4",
    "--prior-events", "3",
    "--version", VERSION,
    "--uv", "/abs/uv",
    "--uv-sha256", "a" * 64,
    "--wheel", "/abs/w.whl",
    "--wheel-sha256", "b" * 64,
    "--manifest", "/abs/m.json",
    "--signature", "/abs/m.sig",
    "--host-archive", "/abs/h.zip",
]


def test_repair_main_parses_the_shell_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[object, object]] = []

    def fake_run(request: object, artifacts: object, **_k: object) -> repair.NativeRepairOutcome:
        seen.append((request, artifacts))
        return repair.NativeRepairOutcome(
            run_id=RUN_ID, terminal="service_ready", failure_code=None,
            mutation_attempted=True, activation_attempted=True, activation_completed=True,
            lock_retained=False, workspace=None, port=PORT,
        )

    monkeypatch.setattr(repair, "run_native_repair", fake_run)
    assert repair.main(_CLI_ARGV) == 0
    request, artifacts = seen[0]
    assert request == NativeRepairRequest(run_id=RUN_ID, events_fd=3, first_seq=4, prior_events=3)
    assert artifacts.version == VERSION
    assert artifacts.uv == Path("/abs/uv")
    assert artifacts.host_archive == Path("/abs/h.zip")


@pytest.mark.parametrize("flag", ["--workspace", "--work"])
def test_repair_main_rejects_workspace_and_abbreviations(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    ran: list[object] = []
    monkeypatch.setattr(repair, "run_native_repair", lambda *a, **k: ran.append(a))
    with _public_event_pipe() as (read_fd, events_fd):
        argv = [*_CLI_ARGV[:]]
        argv[argv.index("--events-fd") + 1] = str(events_fd)
        with pytest.raises(SystemExit) as caught:
            repair.main([*argv, flag, "/secret/workspace"])
        assert _available(read_fd) == b""
    assert caught.value.code == 2
    assert ran == []
    err = capsys.readouterr().err
    assert err == native._HANDOVER_REFUSAL
    assert "/secret/workspace" not in err


def test_repair_main_maps_coordinator_error_to_exit_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*_a: object, **_k: object) -> None:
        raise native.NativeCoordinatorError("invalid_run_id", "invalid_arguments")

    monkeypatch.setattr(repair, "run_native_repair", refuse)
    assert repair.main(_CLI_ARGV) == 2
    assert capsys.readouterr().err == native._HANDOVER_REFUSAL


def test_repair_main_signal_handlers_only_cancel_and_are_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    numbers = native._cancel_signal_numbers()
    before = {number: signal.getsignal(number) for number in numbers}
    observed: list[bool] = []

    def fake_run(*_a: object, cancellation: NativeCancellation, **_k: object) -> repair.NativeRepairOutcome:
        for number in numbers:
            handler = signal.getsignal(number)
            assert callable(handler)
            handler(number, None)  # a signal only sets the flag; it never raises
        observed.append(cancellation.cancelled)
        return repair.NativeRepairOutcome(
            run_id=RUN_ID, terminal="failed", failure_code="cancelled", mutation_attempted=False,
            activation_attempted=False, activation_completed=False, lock_retained=False,
            workspace=None, port=None,
        )

    monkeypatch.setattr(repair, "run_native_repair", fake_run)
    assert repair.main(_CLI_ARGV) == 1
    assert observed == [True]
    assert {number: signal.getsignal(number) for number in numbers} == before



# ── Fix 7: refusals decided by the real classifier ────────────────────────


@pytest.mark.parametrize("case", ["not_installed", "unknown_state", "install_in_progress"])
def test_real_classification_refusals_emit_one_failed_event_and_change_nothing(
    home: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    state = install_receipt.default_receipt_path().parent
    if case == "unknown_state":
        _baseline(home)
        _mut_label(home)
    elif case == "install_in_progress":
        _baseline(home)
        held = NativeRunLock.acquire(
            native_lock.prepare_native_state_dir(state, trusted_root=home), run_id=OTHER_RUN_ID
        )
    before = _tree(home)
    deps, calls = _deps(
        home,
        classify=lambda own=None: _classify(home, own_run_id=own),
        prepare_state_dir=lambda: native_lock.prepare_native_state_dir(state, trusted_root=home),
    )
    try:
        outcome, events = _run(deps)
        assert _phase_events(events) == [("failed", case)]
        assert outcome.mutation_attempted is False
        assert MUTATIONS.isdisjoint(calls)
        assert _tree(home) == before
    finally:
        if case == "install_in_progress":
            held.release()


# ── Fix 8: readiness, lock and event-channel gaps ─────────────────────────


def _proof(**overrides: object) -> NativeReadinessProof:
    values: dict[str, object] = dict(ready=True, failure=None, port=PORT, version=VERSION)
    values.update(overrides)
    return NativeReadinessProof(**values)  # type: ignore[arg-type]


def test_readiness_retries_retryable_failure_then_ready(tmp_path: Path) -> None:
    _workspace(tmp_path)
    retryable = sorted(native._READINESS_RETRYABLE)[0]
    answers = iter([_proof(ready=False, failure=retryable), _proof()])
    deps, calls = _deps(tmp_path, prove_ready=lambda *_a: next(answers))
    outcome, _events_ = _run(deps)
    assert outcome.terminal == "service_ready"
    assert calls.count("prove_ready") == 2
    assert _phase_events(_events_)[-1] == ("service_ready", f"http://127.0.0.1:{PORT}")


def test_readiness_non_retryable_failure_is_service_not_ready(tmp_path: Path) -> None:
    _workspace(tmp_path)
    assert "not_ours" not in native._READINESS_RETRYABLE
    deps, _calls = _deps(tmp_path, prove_ready=lambda *_a: _proof(ready=False, failure="not_ours"))
    outcome, events = _run(deps)
    assert _phase_events(events)[-1] == ("failed", "service_not_ready")
    assert outcome.failure_code == "service_not_ready"


def test_readiness_port_mismatch_is_service_not_ready(tmp_path: Path) -> None:
    _workspace(tmp_path)
    deps, _calls = _deps(tmp_path, prove_ready=lambda *_a: _proof(port=9999))
    outcome, events = _run(deps)
    assert _phase_events(events)[-1] == ("failed", "service_not_ready")


def test_readiness_cancelled_proof_is_cancelled(tmp_path: Path) -> None:
    _workspace(tmp_path)
    deps, _calls = _deps(tmp_path, prove_ready=lambda *_a: _proof(ready=False, failure="cancelled"))
    outcome, events = _run(deps)
    assert _phase_events(events)[-1] == ("failed", "cancelled")
    assert outcome.failure_code == "cancelled"


def test_readiness_deadline_is_service_not_ready(tmp_path: Path) -> None:
    _workspace(tmp_path)
    retryable = sorted(native._READINESS_RETRYABLE)[0]
    deps, _calls = _deps(tmp_path, prove_ready=lambda *_a: _proof(ready=False, failure=retryable))
    policy = _RepairPolicy(readiness_interval=0.01, poll_interval=0.01, readiness_timeout=0.05)
    outcome, events = _run(deps, policy=policy)
    assert _phase_events(events)[-1] == ("failed", "service_not_ready")


def test_lock_lost_midway_fails_without_activation(tmp_path: Path) -> None:
    _workspace(tmp_path)
    owned = _ownership(tmp_path)

    def ensure_host(lock: NativeRunLock, _run_id: str, _plan: object) -> object:
        lock.release()  # the lock is lost while the host step runs
        return owned

    deps, calls = _deps(tmp_path, ensure_host=ensure_host)
    outcome, events = _run(deps)
    # A lock lost mid-run maps through the install-mode lock mapping.
    assert outcome.failure_code == "install_in_progress"
    assert outcome.terminal == "failed"
    assert "bootstrap" not in calls
    assert outcome.activation_attempted is False
    assert all(event["type"] != "service_ready" for event in events)


def test_child_cleanup_failure_retains_the_lock(tmp_path: Path) -> None:
    _workspace(tmp_path)

    def stuck(_remaining: float) -> None:
        raise native._ChildCleanupFailed()

    deps, calls = _deps(tmp_path, probe_engine=lambda: None, reinstall_engine=stuck)
    outcome, events = _run(deps)
    assert outcome.failure_code == "install_failed"
    assert outcome.lock_retained is True
    assert "bootstrap" not in calls
    assert (tmp_path / "state" / native_lock.LOCK_DIR_NAME / native_lock.LOCK_RECORD_NAME).exists()


def test_release_failure_prevents_service_ready(tmp_path: Path) -> None:
    _workspace(tmp_path)
    inner: list[NativeRunLock] = []

    class ReleaseFails:
        def __init__(self, lock: NativeRunLock) -> None:
            self.lock = lock
            inner.append(lock)

        def assert_held(self, *, run_id: str | None = None) -> None:
            self.lock.assert_held(run_id=run_id)

        def release(self) -> None:
            raise RuntimeError("cannot release")

    deps, _calls = _deps(
        tmp_path,
        acquire_lock=lambda st, run_id: ReleaseFails(NativeRunLock.acquire(st, run_id=run_id)),
    )
    try:
        outcome, events = _run(deps)
    finally:
        for lock in inner:
            lock.release()
    assert outcome.terminal == "failed"
    assert outcome.failure_code == "internal_error"
    assert outcome.lock_retained is True
    assert all(event["type"] != "service_ready" for event in events)


def test_failure_event_bounds(tmp_path: Path) -> None:
    _workspace(tmp_path)

    def boom(*_a: object) -> object:
        raise RuntimeError("probe died")

    deps, _calls = _deps(tmp_path, prove_ready=boom)
    with _public_event_pipe() as (read_fd, events_fd):
        outcome = repair._run_native_repair(
            NativeRepairRequest(run_id=RUN_ID, events_fd=events_fd, first_seq=29, prior_events=28),
            deps=deps,
            version=VERSION,
            policy=FAST,
        )
        lines = _drain(read_fd).splitlines()
    assert outcome.failure_code == "service_not_ready"
    assert len(lines) == 4
    assert all(len(line) <= 1024 for line in lines)
    decoded = [json.loads(line) for line in lines]
    assert [event["seq"] for event in decoded] == [29, 30, 31, 32]
    assert decoded[-1] == {
        "schema": 1,
        "run_id": RUN_ID,
        "seq": 32,
        "type": "failed",
        "code": "service_not_ready",
    }


def test_dead_event_channel_yields_event_channel_failed(tmp_path: Path) -> None:
    _workspace(tmp_path)
    deps, calls = _deps(tmp_path)
    with _public_event_pipe() as (read_fd, events_fd):
        os.close(read_fd)  # no reader remains: the first event write meets EPIPE
        os.dup2(os.open(os.devnull, os.O_RDONLY), read_fd)
        outcome = repair._run_native_repair(
            NativeRepairRequest(run_id=RUN_ID, events_fd=events_fd, first_seq=4, prior_events=3),
            deps=deps,
            version=VERSION,
            policy=FAST,
        )
    assert outcome.terminal is None
    assert outcome.failure_code == "event_channel_failed"
    assert "bootstrap" not in calls
