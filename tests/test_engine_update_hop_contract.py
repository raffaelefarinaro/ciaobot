"""The 1.x -> 2.0 hop contract, pinned before #3130 deletes anything.

A 1.x install reaches 2.0 through its own updater: it stages a 2.0 wheel and
then spawns the *staged* engine's `ciao.engine_update` for the swap (`run-apply`,
`run-recover`), while the 0.5.x Mac app keeps parsing `status --json` and the
hosted 1.x server keeps its `ciao.cli supervise` entry point. These tests pin
that receiving half — the hidden verbs, the v1.10.0 `Operation` record shape,
the JSON keys the old app reads and the sentence it reads when there is no
record, the argv launchd really runs for the job and the recovery agent, the
status verb running for real in a fresh `-I` interpreter, the supervise
command, the wheel-only manifest a 1.x install can act on, the startup
recovery hook, and the modules the hop imports — so the slices that delete the
engine's own staging half cannot take the receiver with them. The tests change
no code; they are the guard P6 has to keep green unchanged.
"""

from __future__ import annotations

import importlib
import inspect
import json
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from ciao import engine_update, main, release_manifest, update_host

# The exact fields a v1.10.0 `Operation` record carries (`git show
# v1.10.0:ciao/engine_update.py`, class `Operation`). This is the durable
# contract of the hop: a record written by a 1.10.0 engine must still be
# readable by the 2.0 receiver, field name by field name.
V1_10_FIELDS = (
    "id",
    "phase",
    "from_version",
    "to_version",
    "started_at",
    "updated_at",
    "error",
    "stage_dir",
    "wheel",
    "wheel_sha256",
    "env_python",
    "env_freeze",
    "previous_receipt",
)

# The phases the 0.5.x app's EngineUpdateRecord treats as terminal
# (`terminalPhases` in `mac/Sources/CiaobotMac/EngineUpdater.swift` before #3130
# M3 deleted it; see git history). The client
# also accepts any phase it is polling, so the union with PHASES is the
# vocabulary its parser must never see a value outside of.
_TERMINAL_PHASES = frozenset({"applied", "rolled_back", "rollback_failed", "failed"})

# The modules the hop receiver lives in. Every 2.x release must keep each one
# importable: the staged engine's own interpreter loads them.
_HOP_RECEIVER_MODULES = (
    "ciao.engine_update",
    "ciao.update_host",
    "ciao.install_receipt",
    "ciao.server_host",
    "ciao.macos_service",
    "ciao.release_manifest",
)


def _v1_10_record() -> dict[str, str]:
    """A staged record exactly as a v1.10.0 engine leaves it.

    Empty where staging had nothing to say (`error` until one happens,
    `previous_receipt` until an apply renames the live env aside), filled where
    a staged half always writes: every distinct value here is what the
    field-name mapping is pinned against.
    """
    return {
        "id": "20261118T101500Z-op1",
        "phase": "staged",
        "from_version": "1.10.0",
        "to_version": "2.0.0",
        "started_at": "2026-11-18T10:15:00+00:00",
        "updated_at": "2026-11-18T10:18:00+00:00",
        "error": "",
        "stage_dir": "/home/me/.local/state/ciaobot/updates/stage-20261118T101500Z-op1",
        "wheel": "ciaobot-2.0.0-py3-none-any.whl",
        "wheel_sha256": "0" * 64,
        "env_python": "/home/me/.local/state/ciaobot/updates/stage-20261118T101500Z-op1/bin/python",
        "env_freeze": "ciaobot==2.0.0",
        "previous_receipt": "",
    }


class _DetachedStdioOnlyHost:
    """The update host `main`'s hidden verbs depend on, as far as they use it.

    The one-shot updater job only ever runs where a host exists; the detached
    helper asks the host for one thing — its detached stdio log — and then
    hands the host unchanged to the receiver, so a fake needs no other member.
    """

    def __init__(self) -> None:
        self.redirected: list[Path] = []

    def redirect_detached_stdio(self, root: Path) -> None:
        self.redirected.append(root)


def test_run_apply_verb_runs_named_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run-apply` is what the 1.x updater's LaunchAgent runs.

    The v1.10.0 `update_host` writes `<staged python> -I -m ciao.engine_update
    run-apply --operation <id>`, so the verb, the flag and the host handoff
    must keep exactly that shape: the named operation reaches the receiver,
    through the host the detached job was given.
    """
    host = _DetachedStdioOnlyHost()
    applied: list[str] = []

    def receiver(operation_id: str, *, host: object = None) -> None:  # noqa: ARG001
        applied.append(operation_id)

    monkeypatch.setattr(engine_update, "current_update_host", lambda: host)
    monkeypatch.setattr(engine_update, "run_apply", receiver)

    assert engine_update.main(["run-apply", "--operation", "op-1"]) == 0

    assert applied == ["op-1"]
    # The detached job's stdio was redirected before anything ran, the one
    # preparation main() owes a process launchd detached.
    assert len(host.redirected) == 1


def test_run_recover_verb_runs_named_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run-recover` is the same job in its recovery role, spelled by the
    `com.ciao.recover` LaunchAgent's program and by `recover_interrupted_apply`
    bootstrapping it; the named operation reaches `recover_apply`."""
    host = _DetachedStdioOnlyHost()
    recovered: list[str] = []

    def receiver(operation_id: str, *, host: object = None) -> None:  # noqa: ARG001
        recovered.append(operation_id)

    monkeypatch.setattr(engine_update, "current_update_host", lambda: host)
    monkeypatch.setattr(engine_update, "recover_apply", receiver)

    assert engine_update.main(["run-recover", "--operation", "op-1"]) == 0

    assert recovered == ["op-1"]
    assert len(host.redirected) == 1


def test_job_plist_argv_is_the_machine_launch_shape() -> None:
    """The updater and recovery jobs launch exactly
    `[<staged python>, -I, -m, ciao.engine_update, run-apply|run-recover,
    --operation <id>]`.

    A 1.x install caught between wheels owns a plist written by the 1.10.0
    `update_host`, and the staged engine reading it back must accept the argv
    that code spelled; symmetrically, the 2.0 writer has to keep spelling it
    the same way, or the job it installs for the next hop runs something
    that engine does not parse. `ProgramArguments` is therefore pinned here
    through the real `_job_plist`, with the same labels, log names and
    interval the real writers (`_write_updater_plist`, `_write_recover_plist`)
    pass — the interpreter is whatever python the staged env carries, so its
    exact path is free but its slot is not.
    """
    op = engine_update.Operation(
        id="20261118T101500Z-op1",
        phase="staged",
        from_version="1.10.0",
        to_version="2.0.0",
        started_at="2026-11-18T10:15:00+00:00",
        updated_at="2026-11-18T10:18:00+00:00",
    )
    python = "/home/me/.local/state/ciaobot/updates/stage-20261118T101500Z-op1/bin/python"
    applied = update_host._job_plist(
        op,
        python,
        label=update_host.UPDATER_LABEL,
        verb="run-apply",
        log_name="updater.log",
    )
    assert applied["ProgramArguments"] == [
        python,
        "-I",
        "-m",
        "ciao.engine_update",
        "run-apply",
        "--operation",
        "20261118T101500Z-op1",
    ]
    recovered = update_host._job_plist(
        op,
        python,
        label=update_host.RECOVER_LABEL,
        verb="run-recover",
        log_name="recover.log",
        start_interval=update_host._RECOVER_INTERVAL,
    )
    assert recovered["ProgramArguments"] == [
        python,
        "-I",
        "-m",
        "ciao.engine_update",
        "run-recover",
        "--operation",
        "20261118T101500Z-op1",
    ]


class _RealDispatchHost:
    """The platform host `main` hands to a real receiver, as far as these
    paths reach.

    `main` and the receiver both ask it for the engine port; the exercised
    paths touch two members more: the detached-stdio redirect every detached
    job is owed, and the recovery-agent retirement a stand-down performs. If
    anything on the way reached for the engine service the lookup succeeds but
    the call raises, so a dispatch that launched launchd jobs in a test would
    fail loudly rather than quietly pass.
    """

    def __init__(self) -> None:
        self.redirected: list[Path] = []
        self.retired: list[Path] = []

    def redirect_detached_stdio(self, root: Path) -> None:
        self.redirected.append(root)

    def engine_port(self) -> int:
        return 1

    def start_engine(self) -> object:
        raise AssertionError("this dispatch must not start the engine service")

    def retire_recovery_agent(self, root: Path) -> None:
        self.retired.append(root)


def test_run_apply_dispatch_runs_the_real_receiver(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`main` reaches the real `run_apply`, not just a named stand-in.

    The two verb tests above pin the argv-to-receiver mapping with fakes; this
    one lets the real `run_apply` run from the dispatched call until it
    refuses on its own terms: a record in `applying` phase under a redirected
    state dir, no install receipt for the machine whose home the suite
    isolates, and an engine port nothing listens on. The pre-flight refusal is
    the deepest a real dispatch can go inside a test — past it the receiver
    owns the engine's stop and the env move, machinery for the machine's
    launchd alone — and reaching it still proves the dispatch, the state-dir
    lock, the record read, the receipt read and the failed-record write all
    belong to `run-apply`.
    """
    host = _RealDispatchHost()
    monkeypatch.setattr(engine_update, "current_update_host", lambda: host)
    monkeypatch.setattr(engine_update, "default_state_dir", lambda: tmp_path)
    record = dict(_v1_10_record(), phase="applying")
    engine_update.write_operation(engine_update.Operation(**record), tmp_path)

    assert engine_update.main(["run-apply", "--operation", record["id"]]) == 0

    settled = engine_update.read_operation(tmp_path)
    assert settled is not None
    assert settled.phase == "failed"
    assert "no install receipt" in settled.error
    assert host.redirected == [tmp_path]


def test_run_recover_dispatch_runs_the_real_receiver(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`main` reaches the real `recover_apply`, which stands down over an
    already-settled record.

    This is the door the durable `com.ciao.recover` agent actually walks on
    the tick after a hop succeeded: lock the state dir, read the record, meet
    a terminal phase, and retire itself without touching anything else. The
    real receiver runs end to end here on exactly that input — the record must
    come back byte-identical in meaning (still `applied`, no error), and the
    stand-down must have reached the host's retirement hook, because that
    retirement is the only thing that stops launchd from re-running the
    agent forever over a finished update.
    """
    host = _RealDispatchHost()
    monkeypatch.setattr(engine_update, "current_update_host", lambda: host)
    monkeypatch.setattr(engine_update, "default_state_dir", lambda: tmp_path)
    record = dict(_v1_10_record(), phase="applied")
    engine_update.write_operation(engine_update.Operation(**record), tmp_path)

    assert engine_update.main(["run-recover", "--operation", record["id"]]) == 0

    settled = engine_update.read_operation(tmp_path)
    assert settled is not None
    assert settled.phase == "applied"
    assert settled.error == record["error"]
    assert host.retired == [tmp_path]
    assert host.redirected == [tmp_path]


def test_v1_10_operation_record_reads(tmp_path: Path) -> None:
    """A staged record written by a v1.10.0 engine reads back intact.

    Same field names, same values, and still a `staged` phase the receiver's
    phase list recognises — otherwise the hop's hand-staged record (the CP-2
    step of #3130) is unreadable by the engine it stages.

    The `Operation` dataclass still names exactly the v1.10.0 fields: widening
    the record under the receiver is a deliberate contract edit, made by
    updating this file, not a silent one.
    """
    record = _v1_10_record()
    assert set(record) == set(V1_10_FIELDS)
    assert "staged" in engine_update.PHASES
    assert set(engine_update.Operation.__dataclass_fields__) == set(V1_10_FIELDS)

    path = engine_update._operation_path(tmp_path)
    path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")

    op = engine_update.read_operation(tmp_path)
    assert op is not None
    for name, value in record.items():
        assert getattr(op, name) == value


def test_status_json_carries_old_app_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`status --json` is what the 0.5.x app's EngineUpdateRecord.parse reads.

    That parser wants str `id`, str `phase`, str `to_version` and `error`, and
    only lets a phase through if it can make sense of it: a terminal phase or
    anything on the engine's own lifecycle list. `to_version` is what the app
    shows as the version it is moving to, so a record whose JSON lost any key
    would read to the user as a blank update.
    """
    record = _v1_10_record()
    monkeypatch.setattr(engine_update, "default_state_dir", lambda: tmp_path)
    engine_update._operation_path(tmp_path).write_text(
        json.dumps(record, sort_keys=True) + "\n", encoding="utf-8"
    )

    assert engine_update.main(["status", "--json"]) == 0

    parsed = json.loads(capsys.readouterr().out)
    assert isinstance(parsed, dict)
    for key in ("id", "phase", "to_version", "error"):
        assert isinstance(parsed[key], str)
    assert parsed["id"] == record["id"]
    assert parsed["phase"] in _TERMINAL_PHASES | set(engine_update.PHASES)


def test_status_json_without_a_record_prints_the_v1_sentence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`status --json` with no record prints 1.x's plain sentence, not JSON.

    The 0.5.x app polls `status --json` and hands the stdout to
    `EngineUpdateRecord.parse`, whose own doc says plain text such as
    `no update staged` is nil — that sentence is its vocabulary for "nothing
    in flight", and v1.10.0 printed it for an empty state dir regardless of
    the flag (`git show v1.10.0:ciao/engine_update.py`, `main`). The receiver
    must keep exactly that output: the old poller has met nothing else on the
    hopped engine, so this is pinned word for word.
    """
    monkeypatch.setattr(engine_update, "default_state_dir", lambda: tmp_path)

    assert engine_update.main(["status", "--json"]) == 0
    assert capsys.readouterr().out == "no update staged\n"


def _staged_python(tmp_path: Path) -> Path:
    """A stand-in for the staged environment's interpreter.

    The machine launch's argv carries `-I`, which strips everything a process
    could borrow from its surroundings: no PYTHONPATH, no script or cwd dir,
    no user site. That is why the real job works at all — the staged python
    has this code base installed in its *own* site-packages. The editable
    install that runs these tests does not survive `-I` that way: with
    PYTHONPATH cut, `import ciao` falls through to a meta-path finder that
    resolves outside this checkout, so a child built from the bare
    `sys.executable` would run other code and no edit here would ever be
    visible in it. The throwaway venv below plays the staged env's role
    instead: built from `sys.executable`, carrying this checkout's `ciao`
    package in its site-packages — the same shape the real staged env has,
    and the only shape a `-I` import can succeed under.
    """
    env_dir = tmp_path / "stage-env"
    venv.create(env_dir, with_pip=False, symlinks=True)
    site_dir = (
        env_dir
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    assert site_dir.is_dir(), f"unexpected venv layout at {site_dir}"
    site_dir.joinpath("ciao").symlink_to(Path(engine_update.__file__).resolve().parent)
    python = env_dir / "bin" / "python"
    assert python.is_file(), f"unexpected venv layout: no python in {env_dir / 'bin'}"
    return python


def test_status_verb_runs_in_a_real_detached_process(tmp_path: Path) -> None:
    """`[<staged python>, -I, -m, ciao.engine_update, status, --json]` works
    as a real process — exit code, output and the state dir resolved from
    `HOME` alone.

    This is the argv the job plist writes (with `status` in the slot the
    update verbs take), and a stubbed import can prove none of it: under `-I`
    the child finds the receiver only through its own site-packages, resolves
    the state dir from `HOME` the way `default_state_dir` does on a machine,
    and runs `main` for real. The record below is left at the machine-shaped
    path the module derives from that `HOME`
    (`~/.local/state/ciaobot/updates` beside
    `install_receipt.default_receipt_path`'s receipt), so the second run only
    prints the record if the child's own resolution agrees — move the receiver
    away from that shape and this fails as "no update staged". (The suite's
    autouse fixture redirects `default_state_dir` inside the test process, so
    the path is literally derived here instead of through it.)
    The `__main__` guard is what turns a module import into that run, so
    deleting it makes the child exit 0 having printed nothing — an empty
    stdout fails the assertions below rather than slipping through. Both
    halves of what the 0.5.x app polls are asserted in the process: the
    no-record sentence and the record JSON.
    """
    # A detached job receives a bare environment from launchd; this is one.
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    python = _staged_python(tmp_path)
    argv = [str(python), "-I", "-m", "ciao.engine_update", "status", "--json"]

    first = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)
    assert first.returncode == 0
    assert first.stdout == "no update staged\n"
    assert first.stderr == ""

    record = _v1_10_record()
    state_root = tmp_path / ".local" / "state" / "ciaobot" / "updates"
    state_root.mkdir(parents=True)
    engine_update._operation_path(state_root).write_text(
        json.dumps(record, sort_keys=True) + "\n", encoding="utf-8"
    )

    second = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)
    assert second.returncode == 0
    assert second.stderr == ""
    parsed = json.loads(second.stdout)
    assert set(parsed) == set(V1_10_FIELDS)
    for key in ("id", "phase", "to_version", "error"):
        assert isinstance(parsed[key], str)
    assert parsed["id"] == record["id"]
    assert parsed["phase"] == "staged"
    assert parsed["to_version"] == record["to_version"]


def test_supervise_command_parses() -> None:
    """A hosted 1.x install execs `<interpreter> -I -m ciao.cli supervise`
    (`native/server-host/ServerHost.swift`, `SUPERVISOR_ARGUMENTS`), and there
    is no seam for additional engine arguments: the interpreter and its flags
    are consumed before the CLI starts, so the argv `build_parser` sees is
    exactly `["supervise"]` and the `child_args` REMAINDER slot arrives empty.
    The verb must still dispatch to `_supervise_command` — the member that
    owns the child and its restart loop, i.e. the engine a hosted install
    actually runs.
    """
    from ciao import cli

    args = cli.build_parser().parse_args(["supervise"])
    assert args.func is cli._supervise_command
    assert args.child_args == []


def test_wheel_only_manifest_is_installable_by_1x(tmp_path: Path) -> None:
    """A manifest built from the wheel alone passes the v1.10.0 schema checks
    and yields exactly one wheel to `_pick_wheel`.

    A 1.x install downloads `v<latest>/ciaobot-engine-manifest.json`, verifies
    the signature, then runs exactly these schema checks (`git show
    v1.10.0:ciao/release_manifest.py`, `verify_manifest`, L352-387): schema
    version, release version, tag matching the version, and `_check_artifact`
    on every entry. Its `_pick_wheel` then requires exactly one
    `kind == "wheel"` artifact — so every 2.x release must ship the wheel-only
    manifest, or 1.x installs stop updating with it.
    """
    wheel = tmp_path / "ciaobot-2.0.0-py3-none-any.whl"
    # `artifact_entry` records the digest and size of the bytes; the wheel the
    # installer ships differs at release time, and the manifest contract does
    # not read inside the archive.
    wheel.write_bytes(b"a wheel the signer digests")
    entry = release_manifest.artifact_entry(wheel, kind="wheel")

    manifest = release_manifest.build_manifest(
        "2.0.0", [entry], created="2026-11-18T12:00:00+00:00"
    )

    assert manifest["schema"] == release_manifest.SCHEMA_VERSION
    assert manifest["tag"] == "v2.0.0"
    for artifact in manifest["artifacts"]:
        release_manifest._check_artifact(artifact)  # must not raise
    picked = engine_update._pick_wheel(manifest)
    assert picked is entry
    assert picked["filename"] == wheel.name


def test_startup_runs_engine_update_recovery() -> None:
    """The startup path still runs the interrupted-update recovery.

    The call site lives inside `_run_server_locked`, which boots the whole
    server — nothing a test can reach without doing everything the function
    must not do here — so this is pinned against its source the way
    tests/test_main_startup_gate.py pins the writer wiring in that very
    function. No narrow harness exists either: the call site is inline in
    that one function, so a behavioural run with a fake tracker would mean
    booting the server itself; the receiver's behaviour is covered in
    `tests/test_engine_update.py` and `tests/test_update_host.py` — what is
    pinned here is the startup wiring: the tracker phase, the import, and
    exactly one call with the resolved host.
    Pinned against the code only (comments stripped): a call commented out in
    the source must read as gone, or the pin would not guard anything.
    """
    source = inspect.getsource(main._run_server_locked)
    code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
    assert 'tracker.start("recover_engine_update")' in code
    assert "from ciao.engine_update import recover_interrupted_apply" in code
    assert (
        code.count("asyncio.to_thread(recover_interrupted_apply, host=update_host)")
        == 1
    )


def test_hop_receiver_modules_import() -> None:
    """Every module the hop receiver needs is importable, and the startup hook
    survived whatever the surrounding slices trimmed.

    The staged engine's interpreter loads these six modules the moment the 1.x
    updater hands it the swap; one broken import there means a staged update
    that cannot apply at all.
    """
    for name in _HOP_RECEIVER_MODULES:
        importlib.import_module(name)
    # The startup recovery hook is receiver surface, not an internal detail:
    # the fast path runs it once per boot.
    assert hasattr(engine_update, "recover_interrupted_apply")
